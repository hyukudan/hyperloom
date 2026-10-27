"""Control-machine builds, bounded engine leases, and router lifecycle."""

from copy import deepcopy
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener
import uuid

from .winproc import no_window
from . import assets, bundles, engines, procs, remote, render, rtx, secrets
from .config import ConfigError
from .paths import repo_root


_controllers = []


class OrchestrationError(RuntimeError):
    pass


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("invalid profile or machine id")
    return value


def _state_path(profile):
    return secrets.runtime_dir() / ("up-" + _identifier(profile) + ".json")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        # The prefill helper reads this file every 2 s; Windows refuses a replace while it is open.
        rtx._retry(lambda: temporary.replace(path))
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    return rtx._retry(lambda: json.loads(path.read_text(encoding="utf-8")))


def config_record(config_path):
    """What a launch state records about the configuration it was launched from: the resolved absolute path (its
    identity, the one `launch_states` compares) and the file's sha256 at launch (for the record only, never compared:
    an edit of the configuration does not hide the profiles it launched)."""
    import hashlib
    path = Path(config_path).resolve()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"path": str(path), "sha256": digest}


def _config_key(path):
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def owns(state, config_path):
    """Whether a launch state was launched from the configuration at ``config_path`` (its recorded path is the same
    file); a state without the record, or not a state, belongs to none."""
    record = state.get("config") if isinstance(state, dict) else None
    recorded = record.get("path") if isinstance(record, dict) else None
    wanted = _config_key(config_path)
    return wanted is not None and isinstance(recorded, str) and _config_key(recorded) == wanted


UNKNOWN_CONFIG = ("launched before launch states recorded their configuration: shown with an unknown "
                  "configuration (it can be stopped here)")


def ownership(state, config_path):
    """"own" (launched from the configuration at ``config_path``), "other" (it records another one) or "unknown" (a
    state written before launch states recorded their configuration)."""
    record = state.get("config") if isinstance(state, dict) else None
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        return "unknown"
    return "own" if owns(state, config_path) else "other"


def launch_states(config_path, read=None, unknown=False):
    """{profile: state} of the saved launch states (the up-*.json files) launched from the configuration at
    ``config_path``, in profile order. A state that records another configuration is left out: a portal or a command
    on one configuration never shows or polls the profiles of another. A state without the record (written before
    launch states recorded it) belongs to no known configuration: the readers that poll leave it out; ``unknown``
    (the status listing) includes it, so it is shown, marked, and can be stopped. ``read``: the state reader (default
    `_read`); an unreadable state is skipped."""
    read = read or _read
    found = {}
    for path in sorted(secrets.runtime_dir().glob("up-*.json")):
        try:
            state = read(path)
        except (OSError, ValueError):
            continue
        kind = ownership(state, config_path)
        if kind == "own" or (unknown and kind == "unknown"):
            found[path.stem[3:]] = state
    return found


def _known_hosts():
    return secrets._directory().parent / "known_hosts"


def _client(machine, known_hosts=None):
    if machine.access != "ssh":
        raise OrchestrationError(f"{machine.id}: Phase 1a node work requires ssh access")
    return remote.Remote(machine, known_hosts or _known_hosts())


def _call(client, verb, payload, timeout=60):
    result = client.call(verb, payload, timeout=timeout)
    if result.get("error"):
        raise OrchestrationError(f"{client.machine.id}: {verb}: {result['error']}")
    return result


def _machine_settings(cluster, mid):
    rows = cluster.raw.get("machines", [])
    return next((row for row in rows if row["id"] == mid), {})


def _lab(cluster, mid):
    machine = cluster.machines[mid]
    lab = _machine_settings(cluster, mid).get("lab_dir")
    if machine.lab_mode and not lab:
        raise OrchestrationError(f"{mid}: lab_mode requires a configured lab_dir")
    return lab if machine.lab_mode else None


def _peer(cluster, source, target):
    alias = _machine_settings(cluster, source).get("peer_ssh", {}).get(target)
    if not alias:
        raise OrchestrationError(f"{source}: missing node-side peer_ssh for {target}")
    return alias


def _profile(cluster, profile):
    _identifier(profile)
    if profile not in cluster.profiles:
        raise ValueError("unknown profile: " + profile)
    settings = cluster.profiles[profile]
    kind = render.family(settings.get("preset", profile))
    return settings, kind, engines.members(settings, kind)


def _preset_name(cluster, profile):
    return cluster.profiles[profile].get("preset", profile)


def _versions():
    return _read(repo_root() / "versions.lock")


def load_cluster(path, config_module=None):
    """Load the shared cluster-v1 schema."""
    if config_module is None:
        from . import config as config_module
    return config_module.load(path)


def _check_stop(path):
    if path.with_suffix(".stop").exists():
        raise OrchestrationError("profile stop requested")


def _pause(seconds, check):
    end = time.monotonic() + seconds
    while True:
        check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


@contextmanager
def _up_handlers(path):
    previous = {}
    console = None

    def request(*_):
        path.with_suffix(".stop").touch()

    try:
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                if hasattr(signal, name):
                    sig = getattr(signal, name)
                    previous[sig] = signal.signal(sig, request)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def close_console(event):
                if event not in (0, 1, 2, 5, 6):
                    return False
                request()
                if event in (2, 5, 6):
                    # Windows gives console-close handlers only a short window.
                    # The detached watcher retries if this cleanup is interrupted.
                    try:
                        if path.exists():
                            _down_state(_read(path))
                    except Exception:
                        pass
                return True

            console = callback(close_console)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetConsoleCtrlHandler.argtypes = [callback, wintypes.BOOL]
            kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
            if not kernel.SetConsoleCtrlHandler(console, True):
                raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        if console is not None:
            kernel.SetConsoleCtrlHandler(console, False)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _detached(argv, log):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root())
    options = {"start_new_session": True}
    if os.name == "nt":
        # A hidden console of its own (never shown, even with Windows Terminal as the default host);
        # the router child shares it, so CTRL_BREAK from this helper still reaches it.
        options = no_window()
    with log.open("wb") as stream:
        helper = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, env=env, **options)
    _controllers.append(helper)
    return helper


def _start_up_watch(path, token):
    helper = _detached([str(Path(sys.executable).absolute()), "-m", "hyperloom", "_up-watch",
                        str(path), token], path.with_suffix(".watch.log"))
    end = time.monotonic() + 10
    ready = path.with_suffix(".watch-ready")
    while time.monotonic() < end:
        if ready.exists() and _read(ready).get("token") == token:
            ready.unlink(missing_ok=True)
            return helper
        if helper.poll() is not None:
            raise OrchestrationError("cleanup watcher failed; see " + str(path.with_suffix(".watch.log")))
        time.sleep(0.05)
    helper.kill()
    helper.wait(timeout=5)
    raise OrchestrationError("cleanup watcher did not start")


def up_watch(path, token):
    """Recover a starting profile after an uncatchable controller termination."""
    path = Path(path)
    _write(path.with_suffix(".watch-ready"), {"token": token})
    while path.exists():
        state = _read(path)
        if state.get("token") != token or state.get("phase") != "starting":
            return
        lock = procs.InstanceLock(path.with_suffix(".lock"))
        try:
            lock.acquire()
        except procs.ProcessError:
            time.sleep(0.1)
            continue
        try:
            if path.exists():
                state = _read(path)
                if state.get("token") == token and state.get("phase") == "starting":
                    path.with_suffix(".stop").touch()
                    try:
                        _down_state(state)
                    except Exception:
                        time.sleep(1)
                        continue
                    path.unlink(missing_ok=True)
            return
        finally:
            lock.close()


def _wait_job(client, root, job, timeout=3600):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = _call(client, "job", {"root": root, "job": job})
        if state.get("state") in ("succeeded", "success", "complete", "completed", "done"):
            return state
        if state.get("state") in ("failed", "aborted", "cancelled"):
            raise OrchestrationError("build job failed: " + str(state.get("error", state.get("reason", job))))
        time.sleep(0.2)
    raise OrchestrationError("build job timed out: " + str(job))


def _recipe_preset(name):
    """The preset whose argv and knobs define a build's static checks. A variant that declares no ``components``
    (ds41-pair-long) builds its family's release exactly as main did, so its recipe and release id stay equal to
    the family preset's; a preset with its own pins (the w5 and MiniMax presets) is its own build."""
    data = render.preset(name)
    return name if data.get("components") else render.family(name)


def _required_flags(kind, targets):
    """Flags the frozen preset passes to its binary, plus every advanced knob's argv flag or env name; the
    node checks they exist as strings in the built binary (never executed)."""
    from .config import preset
    data = preset(_recipe_preset(kind))
    flags = {token for token in data["argv"] if token.startswith("--")}
    flags |= {knob["target"] for knob in data.get("advanced", {}).values()}
    return {target: sorted(flags) for target in targets if target == data["binary"]}


def _knob_targets(settings, kind):
    """Targets of the advanced knobs this launch sets to a non-production value (they render argv/env)."""
    from .config import preset
    from . import knobs
    data = preset(_recipe_preset(kind))
    changed = knobs.summary(settings, data)["advanced"]
    return data["binary"], [data["advanced"][name]["target"] for name in changed]


def _components(data, versions, sources, staged):
    """The install payload's pinned components: the preset's pins over versions.lock. A component given with
    --source is marked unpublished and carries its bundle's sha256; any other pin must be a full SHA."""
    rows = {}
    for name, row in engines.components(data, versions).items():
        if name in staged:
            rows[name] = {"repo": row["repo"], "commit": staged[name].commit, "published": False,
                          "bundle_sha256": staged[name].sha256}
        elif row.get("commit"):
            rows[name] = {"repo": row["repo"], "commit": row["commit"]}
        else:
            raise OrchestrationError(f"{name}: the preset pins only {row.get('commit_prefix')}; "
                                     f"install with --source {name}=<checkout>@<full sha>")
    unknown = set(sources) - set(rows)
    if unknown:
        raise ValueError("--source names no component of this preset: " + ", ".join(sorted(unknown)))
    return rows


def _stage_bundles(cluster, client, build_machine, data, versions, sources):
    """Bundle each --source commit locally, copy it staged into <root>/incoming, and have the node verify and
    rename it. Returns {name: Bundle}."""
    import tempfile
    if not sources:
        return {}
    if "tensormeld" in sources:
        raise ValueError("--source tensormeld is not supported: the router package is fetched by peers too")
    pins = engines.components(data, versions)
    machine = cluster.machines[build_machine]
    staged = {}
    with tempfile.TemporaryDirectory(prefix="hyperloom-sources-") as scratch:
        for name, source in sorted(sources.items()):
            if name not in pins:
                raise ValueError("--source names no component of this preset: " + name)
            commit = bundles.resolve(source, pins[name])
            bundle = bundles.create(source, commit, scratch)
            reply = _call(client, "bundle-prepare", {"root": machine.install_root, "lab_dir": _lab(cluster, build_machine),
                                                     "asset_paths": [a["path"] for a in cluster.assets.get(build_machine, {}).values()],
                                                     "commit": commit})
            try:
                client.upload(bundle.path, reply["stage"])
            except BaseException:
                # Best effort: a partial stage is also swept by a later bundle-prepare after an hour.
                try:
                    _call(client, "bundle-discard", {"root": machine.install_root, "stage": reply["stage"]})
                except Exception:
                    pass
                raise
            _call(client, "bundle-accept", {"root": machine.install_root, "commit": commit, "stage": reply["stage"],
                                            "sha256": bundle.sha256, "bytes": bundle.bytes}, timeout=3600)
            staged[name] = bundle
    return staged


def unpublished(manifest):
    """Components of a release manifest built from a local bundle (published: false)."""
    return sorted(name for name, row in ((manifest or {}).get("components") or {}).items()
                  if isinstance(row, dict) and row.get("published") is False)


def install(cluster, build_machine, targets=None, *, profile=None, sources=None):
    """Build once and verify copies before activating a profile on every node. ``sources`` maps a component to a
    bundles.Source (``--source NAME=DIR[@COMMIT]``) for a pinned commit that is not on any remote."""
    if profile is not None:
        _, kind, mids = _profile(cluster, profile)
        name = _preset_name(cluster, profile)
    else:
        kind = next((family for family, row in engines.FAMILIES.items() if set(targets or ()) & set(row.targets)), None)
        if kind is None:
            raise ValueError("targets name no engine family")
        profiles = [p for p in cluster.profiles if render.family(_preset_name(cluster, p)) == kind]
        mids = list(dict.fromkeys(mid for p in profiles for mid in _profile(cluster, p)[2]))
        name = kind
    adapter = engines.adapter(kind)
    targets = list(targets or adapter.targets)
    if build_machine not in mids:
        raise ValueError("build machine must belong to the profile")
    data = render.preset(name)
    sources = dict(sources or {})
    pins = engines.components(data)
    unknown = set(sources) - set(pins)
    if unknown:
        raise ValueError("--source names no component of this preset: " + ", ".join(sorted(unknown)))
    unresolved = sorted(c for c, row in pins.items() if not row.get("commit") and c not in sources)
    if unresolved:
        raise OrchestrationError(f"{unresolved[0]}: the preset pins only {pins[unresolved[0]].get('commit_prefix')}; "
                                 f"install with --source {unresolved[0]}=<checkout>@<full sha>")
    clients = {mid: _client(cluster.machines[mid]) for mid in mids}
    for mid in mids:
        _lab(cluster, mid)
    # The node tool reaches the nodes only through `hyperloom nodes update`: an absent or different one is refused.
    _check_tools(clients, mids, profile)
    # Resolve all peer routes before starting any build.
    peers = {mid: _peer(cluster, build_machine, mid) for mid in mids if mid != build_machine}
    versions = _versions()
    staged = _stage_bundles(cluster, clients[build_machine], build_machine, data, versions, sources)
    options = engines.recipe_options(data, kind)
    recipe = {"configure": options.pop("configure", versions["toolchains"]["hip_configure"]),
              "targets": targets,
              "ctest_regex": "^strix-(serve-(frontend|deepseek-chat|api)|host-upload)-tests$",
              "trellis": adapter.trellis is not None,
              # Checked statically on the node (flag strings in the binary); never executed.
              "required_flags": _required_flags(name, targets)}
    recipe.update(options)
    payload = {"root": cluster.machines[build_machine].install_root,
               "lab_dir": _lab(cluster, build_machine),
               "asset_paths": [asset["path"] for asset in cluster.assets.get(build_machine, {}).values()],
               "components": _components(data, versions, sources, staged),
               "recipe": recipe}
    response = _call(clients[build_machine], "install", payload)
    job = _wait_job(clients[build_machine], payload["root"], response["job"])
    release = job.get("release_id")
    if not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{12}", release):
        raise OrchestrationError("build job did not return a release id")
    for mid, alias in peers.items():
        _call(clients[build_machine], "push-release", {
            "root": payload["root"], "release_id": release, "peer_ssh": alias,
            "peer_root": cluster.machines[mid].install_root, "peer_lab_dir": _lab(cluster, mid),
            "peer_asset_paths": [asset["path"] for asset in cluster.assets.get(mid, {}).values()]}, timeout=3600)
    if profile is not None:
        previous = {}
        for mid in mids:
            leases = _leases(_call(clients[mid], "status", {"root": cluster.machines[mid].install_root}))
            if any(row.get("state") not in ("stopped", "failed", "aborted", "cancelled") and
                   row.get("profile", profile) == profile for row in leases):
                raise OrchestrationError(f"{mid}: stop the profile before activating its release")
            data = _call(clients[mid], "releases", {"root": cluster.machines[mid].install_root})
            previous[mid] = data.get("active", {}).get(profile)
        activated = []
        try:
            for mid in mids:
                _call(clients[mid], "activate", {"root": cluster.machines[mid].install_root,
                                                "profile": profile, "release_id": release})
                activated.append(mid)
        except BaseException as failure:
            incomplete = []
            for mid in reversed(activated):
                if previous[mid]:
                    try:
                        _call(clients[mid], "activate", {"root": cluster.machines[mid].install_root,
                                                        "profile": profile, "release_id": previous[mid]})
                    except Exception:
                        incomplete.append(mid)
                else:
                    incomplete.append(mid)
            if incomplete:
                raise OrchestrationError("activation failed; rollback unconfirmed on " + ", ".join(incomplete)) from failure
            raise
    return release


def _leases(result):
    leases = result.get("leases", [])
    if isinstance(leases, dict):
        return [dict(row, lease=key) for key, row in leases.items()]
    return leases


# A queued GPU turn waits behind the lab FIFO; busy evenings exceed an hour.
MAX_QUEUE_S = 6 * 3600


def _wait_ready(client, root, lease, timeout=600, queue_timeout=3600, poll_s=2.0, check=lambda: None):
    """Wait while the lease is queued for its GPU turn (up to queue_timeout), then up to timeout for readiness.
    Each poll is one ssh round trip, so poll every couple of seconds, not faster."""
    queued_until = time.monotonic() + queue_timeout
    ready_until = None
    while True:
        check()
        rows = _leases(_call(client, "status", {"root": root}))
        row = next((row for row in rows if row.get("lease", row.get("id")) == lease), {})
        state = row.get("state")
        if state == "ready":
            check()
            return row
        if state in ("failed", "aborted", "stopped", "cancelled"):
            raise OrchestrationError(f"{client.machine.id}: lease {lease}: {row.get('reason', state)}")
        now = time.monotonic()
        if state in (None, "queued"):
            if now > queued_until:
                raise OrchestrationError(f"{client.machine.id}: lease {lease} still queued for its GPU turn")
        else:
            ready_until = ready_until or now + timeout
            if now > ready_until:
                raise OrchestrationError(f"{client.machine.id}: lease readiness timed out")
        _pause(poll_s, check)


def _url(endpoint, suffix):
    address, port = endpoint["address"], endpoint["port"]
    return f"http://{'[' + address + ']' if ':' in address else address}:{port}{suffix}"


def _http_json(endpoint, suffix="/api/status"):
    with build_opener(ProxyHandler({})).open(_url(endpoint, suffix), timeout=2) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise OrchestrationError("router response exceeds 1 MiB")
    return json.loads(data)


def _approval(deadline_s, approval):
    if type(deadline_s) not in (int, float) or not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("deadline must be positive and finite")
    if approval is not None:
        if (not isinstance(approval, dict) or not isinstance(approval.get("ref"), str)
                or not approval["ref"].strip() or type(approval.get("lease_s")) is not int
                or not math.isfinite(approval["lease_s"]) or approval["lease_s"] < deadline_s + 60
                or type(approval.get("end_epoch")) not in (int, float)
                or not math.isfinite(approval["end_epoch"]) or approval["end_epoch"] <= time.time()):
            raise ValueError("approval needs a reference, sufficient duration, and a future end")
    elif deadline_s > 1140:
        raise ValueError("deadline above 1140 seconds requires explicit approval")


def _release(client, machine, profile, configured, needs=None, rows_out=None):
    """The release to run; with needs=(binary, strings), each string must be in the release's static check.
    rows_out, if given, receives the release's listing row (its manifest)."""
    data = _call(client, "releases", {"root": machine.install_root})
    release = configured or data.get("active", {}).get(profile)
    rows = data.get("releases", [])
    if isinstance(rows, dict):
        rows = [dict(value, release_id=key) for key, value in rows.items()]
    row = next((row for row in rows if row.get("release_id", row.get("id")) == release), None)
    if not release or row is None:
        raise OrchestrationError(f"{machine.id}: no installed release for {profile}")
    if needs and needs[1]:
        recipe = (row.get("manifest") or {}).get("recipe") or {}
        checked = set((recipe.get("required_flags") or {}).get(needs[0]) or ())
        missing = [value for value in needs[1] if value not in checked]
        if missing:
            raise OrchestrationError(f"{machine.id}: release {release} was not checked for advanced knob target "
                                     f"{', '.join(missing)}; rebuild it with 'hyperloom install {profile}'")
    if rows_out is not None:
        rows_out[machine.id] = row
    return release


def _tool(client):
    reply = _call(client, "version", {})
    return ((reply.get("checks") or {}).get("tool") or {}).get("value")


def _check_tools(clients, mids, profile):
    """Refuse to start work with a node tool that differs from this checkout's (it may lack checks up relies on)."""
    expected = remote.tool_digest()
    for mid in mids:
        deployed = _tool(clients[mid])
        if deployed != expected:
            raise OrchestrationError(f"{mid}: node tool differs from this checkout (deployed {str(deployed)[:12]}, "
                                     f"expected {expected[:12]}); run 'hyperloom nodes update {profile or '<profile>'}'")


def nodes_update(cluster, profile, machine=None, force=False):
    """Redeploy the node tool to the profile's machines; refused while a lease of the root is live."""
    _, _, mids = _profile(cluster, profile)
    if machine is not None:
        if machine not in mids:
            raise ValueError("machine is not part of the profile: " + machine)
        mids = [machine]
    result = {}
    for mid in mids:
        client = _client(cluster.machines[mid])
        if not force:
            live = [row for row in _call(client, "status", {"root": cluster.machines[mid].install_root}).get("leases", [])
                    if row.get("state") not in ("stopped", "failed", "aborted", "cancelled", "canceled", "finished")]
            if live:
                raise OrchestrationError(f"{mid}: a lease is live ({live[0].get('lease')}); stop it or use --force")
        client.bootstrap()
        result[mid] = {"tool": _tool(client), "expected": remote.tool_digest()}
    return result


def _put(client, machine, name, value):
    import base64
    _call(client, "put-secret", {"root": machine.install_root, "name": name,
                                 "value_b64": base64.b64encode(value.encode()).decode()})
    return str(PurePosixPath(machine.install_root) / "etc/keys" / name)


def prelaunch(cluster, profile, settings, kind, clients, mids):
    """The node checks and keys every launch path shares (up and service install), in this order: the deployed
    node tool matches this checkout, the profile's assets pass the strict inspection, every node runs the same
    installed release (checked for the advanced knob targets), then each node gets its API key file.

    Returns {"assets", "release_id", "node_values", "node_paths"}; raises before any key is pushed when a
    check fails."""
    _check_tools(clients, mids, profile)
    inspected = assets.inspect(cluster, profile, clients, mids, strict=True)
    release_rows = {}
    releases = {mid: _release(clients[mid], cluster.machines[mid], profile, settings.get("release_id"),
                              needs=_knob_targets(settings, _preset_name(cluster, profile)), rows_out=release_rows)
                for mid in mids}
    if len(set(releases.values())) != 1:
        raise OrchestrationError("profile nodes must run the same release id: "
                                 + ", ".join(f"{mid} {rid}" for mid, rid in releases.items()))
    node_values, node_paths = {}, {}
    # Engines without an API key option (MiniMax serve) bind loopback instead; they get no key file.
    for mid in (mids if engines.release_adapter(_preset_name(cluster, profile)).key_file else ()):
        secret = _secret_name(profile, mid)
        node_values[mid] = secrets.ensure(secret)
        node_paths[mid] = _put(clients[mid], cluster.machines[mid], secret, node_values[mid])
    return {"assets": inspected, "release_id": releases[mids[0]], "node_values": node_values,
            "node_paths": node_paths, "release_rows": release_rows}


def check_launch(cluster, profile, router="local", nodes=None, *, diagnostics=None, recipe=False):
    """The profile, node and router checks up makes before any I/O (also `hyperloom recipe plan`'s preflight)."""
    settings, kind, defaults = _profile(cluster, profile)
    name = _preset_name(cluster, profile)
    engine = engines.release_adapter(name)
    pair = engine.topology == "pair"
    diagnostics = list(dict.fromkeys(diagnostics or ()))
    unknown = set(diagnostics) - set(render.preset(name).get("diagnostics", {}))
    if unknown:
        raise ValueError("unknown diagnostic for this profile: " + ", ".join(sorted(unknown)))
    mids = list(defaults if nodes is None else nodes)
    for mid in mids:
        if mid not in cluster.machines:
            raise ValueError("unknown machine: " + mid)
    if not mids or len(mids) != len(set(mids)) or any(mid not in defaults for mid in mids):
        raise ValueError("declared nodes must be distinct machines of the profile")
    if pair and mids != defaults:
        raise ValueError("pair requires both rank machines in rank order")
    if recipe and engine.bind == "loopback":
        raise ValueError(f"--recipe needs the router on this machine, and {kind} engines bind loopback on their node "
                         "(only a router on rank 0 reaches them): this profile does not run with a recipe yet")
    if engine.bind == "loopback" and router != mids[0]:
        raise ValueError(f"{kind} engines bind loopback only (no API key support): "
                         f"run the router on rank 0 with --router {mids[0]}")
    if recipe:
        if mids != list(defaults):
            raise ValueError("--recipe binds the profile's own nodes: drop --node")
        if router != "local":
            # The router reads the route state hyperloom writes on this machine every second.
            raise ValueError("--recipe runs the router locally (it reads the route state written here): use --router local")
    return settings, kind, defaults, engine, name, pair, diagnostics, mids


def up(cluster, profile, router="local", nodes=None, devices=None, *, deadline_s=1140, approval=None, queue_timeout_s=3600,
       diagnostics=None, vram_cap_gib=None, recipe=None, cancelled=None):
    _approval(deadline_s, approval)
    if type(queue_timeout_s) is not int or not 60 <= queue_timeout_s <= MAX_QUEUE_S:
        raise ValueError(f"queue timeout must be 60..{MAX_QUEUE_S} seconds")
    settings, kind, defaults, engine, name, pair, diagnostics, mids = check_launch(
        cluster, profile, router, nodes, diagnostics=diagnostics, recipe=recipe is not None)
    declared = list(mids)
    service = settings.get("prefill_service")
    launch = None
    if recipe is not None:
        from .recipes import launch as recipe_launch
        launch = recipe_launch.prepare(cluster, profile, recipe, router=router, vram_cap_gib=vram_cap_gib,
                                       state_dir=_state_path(profile).parent)
        if not launch.service_start:
            service = None  # every prefill stays on the nodes (the why is recorded); no PRO lease, no start
    if service is not None and router != "local":
        raise ValueError("the RTX prefill service is loopback-only: use --router local")
    if vram_cap_gib is not None and service is None:
        raise ValueError("--vram-cap-gib places an RTX prefill service; this profile has none")
    for device in devices or []:
        try:
            mid, did = device.split(":", 1)
            machine = cluster.machines[mid]
        except (ValueError, KeyError) as exc:
            raise ValueError("unknown machine or device: " + device) from exc
        if service is not None and (mid, did) == (service["machine"], service["device"]):
            continue  # the profile's own prefill device, on this machine
        if mid not in mids or not any(d["id"] == did and not d.get("excluded", False) for d in machine.devices):
            raise ValueError("unknown, excluded, or unsupported profile device: " + device)
        declared.append(mid)
    if router != "local":
        declared.append(router)
    for mid in declared:
        if mid not in cluster.machines:
            raise ValueError("unknown machine: " + mid)
    path = _state_path(profile)
    lock = procs.InstanceLock(path.with_suffix(".lock")).acquire()
    state = {"profile": profile, "leases": [], "router": None,
             "known_hosts": str(_known_hosts()), "machines": {},
             "token": uuid.uuid4().hex, "phase": "starting", "keys": [],
             "production": not diagnostics and (launch is None or launch.production), "diagnostic": diagnostics}
    from .recipes import service as recipe_service
    if recipe_service.configured_file() is not None:
        # The configuration this launch belongs to (the portal and the CLI set it for the call): only a portal or a
        # command on the same configuration lists or polls it.
        state["config"] = config_record(recipe_service.configured_file())
    if launch is not None:
        state["recipe"] = launch.record()
    keys = []
    local_values = {}
    owned = False
    handlers = ExitStack()
    try:
        if path.exists():
            raise OrchestrationError("profile has saved running state; run down before up")
        path.with_suffix(".stop").unlink(missing_ok=True)
        if cancelled is not None and cancelled():
            # A portal job (an up or a recipe deploy) cancelled while this launch was being handed over: start nothing.
            raise OrchestrationError("profile stop requested")
        owned = True
        handlers.enter_context(_up_handlers(path))
        _write(path, state)
        _start_up_watch(path, state["token"])
        clients = {mid: _client(cluster.machines[mid]) for mid in dict.fromkeys(declared)}
        state["machines"] = {mid: asdict(cluster.machines[mid]) for mid in clients}
        _write(path, state)
        for mid, client in clients.items():
            machine = cluster.machines[mid]
            state["machines"][mid] = asdict(machine)
            try:
                lab = _lab(cluster, mid) if mid in mids else _machine_settings(cluster, mid).get("lab_dir")
                _call(client, "doctor", {"lab_dir": lab}, timeout=60)
            except (remote.RemoteError, OrchestrationError) as exc:
                raise OrchestrationError(f"{mid}: doctor failed: {exc}") from exc
        peer = _peer(cluster, mids[0], mids[1]) if pair else None
        selected = deepcopy(cluster)
        if not pair:
            selected.profiles[profile]["nodes"] = mids
        if launch is not None and service is None:
            selected.profiles[profile].pop("prefill_service", None)
        prepared = prelaunch(cluster, profile, settings, kind, clients, mids)
        state["assets"] = prepared["assets"]
        state["asset_cache_note"] = assets.CACHE_NOTE
        selected.profiles[profile]["release_id"] = prepared["release_id"]
        node_values, node_paths = prepared["node_values"], prepared["node_paths"]
        release_rows = prepared["release_rows"]
        selected.profiles[profile]["api_key_files"] = node_paths
        selected.router["machine"] = router
        selected.router["replica_api_key_files"] = {}
        router_key = _secret_name(profile, "router")
        if router == "local":
            for mid in node_values:
                key = secrets.runtime_dir() / ("router-" + uuid.uuid4().hex + ".key")
                keys.append(str(key))
                local_values[str(key)] = node_values[mid]
                selected.router["replica_api_key_files"][mid] = str(key)
            key = secrets.runtime_dir() / ("router-" + uuid.uuid4().hex + ".key")
            keys.append(str(key))
            local_values[str(key)] = secrets.ensure(router_key)
            selected.router["api_key_file"] = str(key)
            if service is not None:
                key = secrets.runtime_dir() / ("router-" + uuid.uuid4().hex + ".key")
                keys.append(str(key))
                local_values[str(key)] = secrets.ensure(_secret_name(profile, "prefill"))
                selected.router["replica_api_key_files"][service["machine"]] = str(key)
            selected.router["listen"] = cluster.pc.get("router_listen", "127.0.0.1:18600")
        else:
            machine = cluster.machines[router]
            for mid in node_values:
                selected.router["replica_api_key_files"][mid] = _put(
                    clients[router], machine, _secret_name(profile, mid), node_values[mid])
            selected.router["api_key_file"] = _put(clients[router], machine, router_key, secrets.ensure(router_key))
            host = machine.http_address
            selected.router["listen"] = f"[{host}]:18600" if ":" in host else f"{host}:18600"
        # Diagnostic runs are labelled non-production everywhere they are recorded (state, leases, status).
        processes = render.render_processes(selected, profile, **(
            {"diagnostics": diagnostics, "run": state["token"][:12]} if diagnostics else {}))
        rendered = render.render_router(selected, profile)
        if launch is not None:
            recipe_launch.verify_launch(launch, selected, profile, processes, release_rows)
            key_file = selected.router["replica_api_key_files"].get(service["machine"]) if service else None
            rendered["prefill"] = recipe_launch.router_prefill(launch, selected, profile, key_file)
        state.update(release_id=prepared["release_id"], nodes=mids, devices=list(devices or []),
                     started_at=time.time(), process_specs=[asdict(spec) for spec in processes],
                     rendered_router=rendered, launch=render.launch_summary(selected, profile))
        # Components built from a local bundle (never published) are named on every launch record.
        state["launch"]["unpublished_sources"] = unpublished((release_rows.get(mids[0]) or {}).get("manifest"))
        # The release's component commits (its manifest), for build info; absent when the node lists none.
        manifest_components = ((release_rows.get(mids[0]) or {}).get("manifest") or {}).get("components") or {}
        state["launch"]["components"] = {name: row["commit"] for name, row in manifest_components.items()
                                         if isinstance(row, dict) and isinstance(row.get("commit"), str)}
        # A non-production advanced knob makes the whole run non-production, like a diagnostic does.
        state["production"] = state["production"] and state["launch"]["production"]
        _write(path, state)
        # Persist each returned lease immediately, including while it is queued.
        def start(mid, payload, role):
            _check_stop(path)
            result = _call(clients[mid], "lease", payload)
            record = {"machine": mid, "lease": result["lease"], "role": role, "label": payload.get("label"),
                      "processes": [proc["name"] for proc in payload["processes"]],
                      "process_machines": {proc["name"]: mid if proc["machine"] == "local" else mids[1]
                                           for proc in payload["processes"]}}
            state["leases"].append(record)
            state["keys"] = keys
            _write(path, state)
            _check_stop(path)
            return record

        def process(spec, location):
            machine = cluster.machines[spec.machine_id]
            # A pair's rank 1 has no HTTP listener; its readiness accompanies the pair's health endpoint on rank 0
            # (checked by the supervisor on rank 0's machine, so a loopback-bound pair is checked on 127.0.0.1).
            health_machine = cluster.machines[mids[0]] if pair else machine
            address = health_machine.http_address if engine.bind == "lan" else "127.0.0.1"
            port = int(spec.argv[spec.argv.index(render.port_flag(render.preset(name))) + 1])
            return {"name": spec.role if pair else _node_name(spec.machine_id),
                    "machine": location, "argv": spec.argv, "env": spec.env,
                    "ready": spec.ready,
                    "health_url": _url({"address": address, "port": port}, engine.health_path),
                    "key_file": node_paths.get(spec.machine_id), "ready_timeout_s": 600}

        for index, mid in enumerate(engines.router_members(kind, mids) if pair else mids):
            payload = {"root": cluster.machines[mid].install_root, "lab_dir": _lab(cluster, mid),
                       "label": (f"{engine.label}-{uuid.uuid4().hex[:4]}" if pair
                                 else f"{engine.label}-{_node_name(mid)}-{uuid.uuid4().hex[:4]}"),
                       "kind": "pair" if pair else "single", "deadline_s": deadline_s,
                       "approval": approval, "profile": profile, "layout_required": engine.layout_required,
                       "peer_ssh": peer, "peer_root": cluster.machines[mids[1]].install_root if peer else None,
                       "peer_lab_dir": _lab(cluster, mids[1]) if peer else None,
                       "processes": [process(spec, "local" if i == 0 else "peer") for i, spec in enumerate(processes)]
                       if pair else [process(processes[index], "local")]}
            if diagnostics:
                payload["diagnostic"] = diagnostics
            start(mid, payload, "engine")
        deadlines = []
        for lease in state["leases"]:
            ready = _wait_ready(clients[lease["machine"]], cluster.machines[lease["machine"]].install_root,
                                lease["lease"], queue_timeout=queue_timeout_s, check=lambda: _check_stop(path))
            deadlines.append(ready.get("deadline") or time.time() + deadline_s)
        state["deadline"] = min(deadlines)
        _check_stop(path)
        node_ram = {}
        if launch is not None:
            # The node engines' allocations are done: every node keeps the recipe's free-RAM floor (Sol-5 #4).
            def read_ram(mid):
                try:
                    node_ram[mid] = {"free_ram_gib": recipe_launch.node_ram(
                        _call(clients[mid], "gpu", {"lab_dir": _lab(cluster, mid)}, timeout=60)), "at": time.time()}
                except (remote.RemoteError, OrchestrationError):
                    return None
                return node_ram[mid]["free_ram_gib"]
            low = recipe_launch.node_floors(launch, read_ram)
            if low is not None:
                raise OrchestrationError("recipe: " + low)
        skip = None
        if service is not None and launch is not None:
            skip = recipe_launch.recheck_before_service(launch, selected)
            if skip is not None:
                state["recipe"]["service"].update(start=False, why="at launch: " + skip)
                _write(path, state)
        if service is not None and skip is None:
            # After the decode side is READY (its import route is the handoff), before the router admits.
            try:
                with recipe_launch.planning(selected) if launch is not None else ExitStack():
                    spec = rtx.render_service(
                        selected, profile, api_key=secrets.ensure(_secret_name(profile, "prefill")),
                        handoff_api_key=node_values[mids[0]],
                        free_physical_gib=rtx.memory_status()["free_physical_gib"],
                        vram_cap_gib=launch.vram_cap_gib if launch is not None else vram_cap_gib,
                        host_gib_exact=launch.host_gib if launch is not None else None)
            except (ConfigError, recipe_launch.descriptors.DescriptorError) as exc:
                if launch is None:
                    raise
                # The recipe's budget no longer fits this start (memory moved since up began): skip, never clamp.
                skip = "at launch: " + str(exc)
                state["recipe"]["service"].update(start=False, why=skip)
                _write(path, state)
        if service is not None and skip is None:
            spec = rtx.ServiceSpec(**dict(spec.__dict__, deadline=min(spec.deadline, state["deadline"])))
            # Recorded before the helper exists, so down (or the watchdog) can always find and stop it.
            state["prefill_service"] = rtx.prepare_service(spec)
            _write(path, state)
            rtx.launch_service(state["prefill_service"], spec, detached=_detached,
                               owner={"state_path": str(path), "token": state["token"]},
                               check=lambda: _check_stop(path))
            _write(path, state)
            _check_stop(path)
            low = recipe_launch.recheck_after_ready(launch, selected) if launch is not None else None
            if low is not None:
                # Allocations done but memory below its floors: stop the service; the nodes keep every prefill.
                rtx.stop_service(state["prefill_service"])
                state["recipe"]["service"].update(start=False, why="after READY: " + low)
                _write(path, state)
        if router == "local":
            for key, value in local_values.items():
                secrets._write(Path(key), value.encode("ascii"))
            render.validate_router(rendered)
            route_state = None
            if launch is not None:
                reservation = cluster.pc.get("cuda", {}).get("reservation")
                route_state = recipe_launch.route_state_spec(
                    launch, state.get("prefill_service") if state["recipe"]["service"]["start"] else None,
                    rtx.reservation_end(reservation, time.time()) if reservation else None, state)
                route_state["initial_nodes"] = node_ram
                if route_state["disk"] and service is not None:
                    data = rtx._service(selected, profile)[1]
                    asset = selected.assets.get(service["machine"], {}).get((data.get("placement") or {}).get("model_asset"))
                    route_state["disk"] = {m: (asset or {}).get("path") for m in route_state["disk"]}
            state["router"] = _start_local_router(profile, rendered, keys, ownership=state, route_state=route_state)
        else:
            machine = cluster.machines[router]
            commit = _versions()["components"]["tensormeld"]["commit"]
            # The wrapper sets the pinned import path itself: node spec env forbids
            # PYTHON* variables for engine processes.
            source = str(PurePosixPath(machine.install_root) / "src" / ("tensormeld-" + commit) / "src")
            wrapper = ("import os,runpy,sys,tempfile\n"
                       "if not os.path.isfile(os.path.join(sys.argv[3],'tensormeld','__main__.py')): "
                       "raise SystemExit('pinned TensorMeld source unavailable')\n"
                       "fd,path=tempfile.mkstemp(suffix='.json',dir=sys.argv[1])\n"
                       "os.write(fd,sys.argv[2].encode());os.close(fd)\n"
                       "os.environ['PYTHONPATH']=sys.argv[3];sys.path.insert(0,sys.argv[3])\n"
                       "sys.argv=['tensormeld','serve-replicas',path]\n"
                       "try: runpy.run_module('tensormeld',run_name='__main__')\n"
                       "finally: os.unlink(path)\n")
            wrapper = "exec(" + repr(wrapper) + ")"
            payload = {"root": machine.install_root, "lab_dir": None, "kind": "none",
                       "label": "hl-router-" + uuid.uuid4().hex[:4], "deadline_s": deadline_s,
                       "approval": approval, "layout_required": None, "peer_ssh": None, "peer_root": None,
                       "processes": [{"name": "router", "machine": "local",
                           "argv": ["/usr/bin/python3", "-c", wrapper, machine.install_root + "/run", json.dumps(rendered), source],
                           "env": {},
                           "ready": "http-health", "health_url": _url(rendered["listen"], "/health"), "ready_timeout_s": 30}]}
            record = start(router, payload, "router")
            state["router"] = dict(record, endpoint=rendered["listen"])
            _write(path, state)
            _wait_ready(clients[router], machine.install_root, record["lease"], timeout=40,
                        queue_timeout=queue_timeout_s, check=lambda: _check_stop(path))
        _check_stop(path)
        state["phase"] = "running"
        _write(path, state)
        return state
    except BaseException:
        if owned:
            try:
                _down_state(state)
                path.unlink(missing_ok=True)
            except Exception:
                _write(path, state)  # Retain ownership so down can retry cleanup.
        for key in keys:
            Path(key).unlink(missing_ok=True)
        raise
    finally:
        handlers.close()
        lock.close()


def _secret_name(profile, suffix):
    import hashlib
    return "key-" + hashlib.sha256((profile + ":" + suffix).encode()).hexdigest()[:32]


def export_key(profile, destination):
    name = _secret_name(_identifier(profile), "router")
    secrets.export(name, destination)
    return {"key_name": name, "out": str(destination)}


def endpoint(profile, config_path=None):
    """The profile's router URL and key name; with ``config_path``, only of a launch of that configuration (`owns`)."""
    state = _read(_state_path(profile))
    if config_path is not None and not owns(state, config_path):
        raise OrchestrationError(f"{profile}: launched from another configuration")
    if not state.get("router"):
        raise OrchestrationError("profile router has not started")
    return {"url": _url(state["router"]["endpoint"], ""),
            "key_name": _secret_name(profile, "router")}


def _node_name(mid):
    if re.fullmatch(r"[a-z0-9-]{1,29}", mid):
        return mid
    import hashlib
    return re.sub(r"[^a-z0-9-]", "-", mid.lower())[:20] + "-" + hashlib.sha256(mid.encode()).hexdigest()[:8]


def _start_local_router(profile, rendered, keys, *, ownership=None, route_state=None):
    directory = secrets.runtime_dir() / ("router-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    spec = directory / "spec.json"
    if route_state is not None:
        # Fresh before the router's first request (the nodes as measured after their engines loaded; the replicas
        # assumed up until the router reports them); the helper refreshes it every second.
        from .recipes import launch as recipe_launch
        recipe_launch.write_route_state(route_state, nodes=route_state.get("initial_nodes"),
                                        replicas={mid: "up" for mid in route_state["nodes"]})
    _write(spec, {"rendered": rendered, "keys": keys, "profile": profile, "route_state": route_state,
                  "ownership": ownership, "deadline": (ownership or {}).get("deadline", time.time() + 1140)})
    record = {"machine": "local", "directory": str(directory), "endpoint": rendered["listen"]}
    if ownership is not None:
        ownership["router"] = record
        _write(_state_path(profile), ownership)
    argv = [str(Path(sys.executable).absolute()), "-m", "hyperloom", "_up-router", str(spec)]
    helper = _detached(argv, directory / "controller.log")
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if ownership is not None:
                _check_stop(_state_path(profile))
            ready = directory / "state.json"
            if ready.exists() and _read(ready).get("state") == "ready":
                return record
            if helper.poll() is not None:
                raise OrchestrationError("local router failed; see " + str(directory / "controller.log"))
            time.sleep(0.1)
        raise OrchestrationError("local router readiness timed out")
    except BaseException:
        (directory / "stop").touch()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait(timeout=5)
        raise


def poll_nodes_once(route_state, polled):
    """Free RAM of every node over ssh (the node tool's read-only `gpu` verb) into ``polled``. Any failure or bad
    answer leaves that node's last reading, which ages out (fail closed); it never raises."""
    from .recipes import launch as recipe_launch
    for mid, machine in route_state["machines"].items():
        try:
            result = _call(_client(SimpleNamespace(**machine), route_state["known_hosts"]), "gpu", {}, timeout=5)
            polled[mid] = {"free_ram_gib": recipe_launch.node_ram(result), "at": time.time()}
        except Exception:  # noqa: BLE001
            pass


def local_router_run(spec_path):
    """Persistent CLI helper owns LocalChild's Job Object and plaintext keys."""
    path = Path(spec_path)
    spec = _read(path)
    directory = path.parent
    rendered = spec["rendered"]
    config = directory / "router.json"
    _write(config, rendered)
    env = dict(os.environ)
    source = repo_root() / "third_party/tensormeld/src"
    if source.is_dir():
        env["PYTHONPATH"] = str(source)
    else:
        env.pop("PYTHONPATH", None)
    child = procs.LocalChild([str(Path(sys.executable).absolute()), "-m", "tensormeld", "serve-replicas", str(config)],
                             env, directory, "router", port=(rendered["listen"]["address"], rendered["listen"]["port"]))
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    route_state = spec.get("route_state")
    next_state = 0
    polled = dict((route_state or {}).get("initial_nodes") or {})
    replicas = {}
    halt = threading.Event()

    def poll_nodes():
        from .recipes import launch as recipe_launch
        while not halt.wait(recipe_launch.NODE_POLL_S):
            poll_nodes_once(route_state, polled)

    poller = threading.Thread(target=poll_nodes, daemon=True) if route_state else None
    try:
        child.start()
        if poller is not None:
            poller.start()
        end = time.monotonic() + 15
        ready = False
        next_check = 0
        while child.alive() and not interrupted and not (directory / "stop").exists():
            if time.time() >= spec["deadline"]:
                break
            if route_state is not None and time.monotonic() >= next_state:
                from .recipes import launch as recipe_launch
                try:
                    if ready:
                        status = _http_json(rendered["listen"])
                        replicas = {r.get("node"): r.get("state") for r in status.get("replicas", [])}
                    recipe_launch.write_route_state(route_state, nodes=dict(polled), replicas=replicas)
                except (OSError, ValueError):
                    pass  # a missed write ages the state; the router fails closed when it goes stale
                next_state = time.monotonic() + 1
            if spec.get("ownership") and time.monotonic() >= next_check:
                if _replicas_stopped(spec["ownership"]):
                    break
                next_check = time.monotonic() + 2
            if not ready:
                try:
                    status = _http_json(rendered["listen"])
                    if status.get("schema") != "tensormeld/replica-router-status-v1":
                        raise ValueError("unexpected router status schema")
                    _write(directory / "state.json", {"state": "ready", "pid": child.process.pid})
                    ready = True
                except (OSError, ValueError):
                    if time.monotonic() >= end:
                        raise OrchestrationError("router status readiness timed out")
            time.sleep(0.1)
        try:
            _write(directory / "final.json", _http_json(rendered["listen"]))
        except (OSError, ValueError):
            pass
    finally:
        halt.set()
        try:
            child.stop(5)
        finally:
            for key in spec["keys"]:
                Path(key).unlink(missing_ok=True)
            config.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            _write(directory / "state.json", {"state": "stopped"})


def _replicas_stopped(state):
    engines = [lease for lease in state["leases"] if lease["role"] == "engine"]
    if not engines:
        return False
    try:
        for lease in engines:
            mid = lease["machine"]
            machine = state["machines"][mid]
            rows = _leases(_call(_client(SimpleNamespace(**machine), state["known_hosts"]),
                                 "status", {"root": machine["install_root"]}, timeout=2))
            row = next((row for row in rows if row.get("lease", row.get("id")) == lease["lease"]), None)
            if row is None or row.get("state") not in ("stopped", "failed", "aborted", "cancelled"):
                return False
        return True
    except (OSError, remote.RemoteError, OrchestrationError):
        return False


def _stop_local_router(router):
    directory = Path(router["directory"])
    (directory / "stop").touch()
    end = time.monotonic() + 15
    while time.monotonic() < end:
        path = directory / "state.json"
        if path.exists() and _read(path).get("state") == "stopped":
            for helper in _controllers[:]:
                if helper.poll() is not None:
                    _controllers.remove(helper)
            return
        lock = procs.InstanceLock(directory / "router.lock")
        try:
            lock.acquire()
        except procs.ProcessError:
            pass
        else:
            # A crashed controller's Job Object has already killed its router.
            try:
                spec = directory / "spec.json"
                if spec.exists():
                    for key in _read(spec).get("keys", []):
                        Path(key).unlink(missing_ok=True)
                    spec.unlink()
                (directory / "router.json").unlink(missing_ok=True)
                _write(directory / "state.json", {"state": "stopped"})
                return
            finally:
                lock.close()
        time.sleep(0.1)
    raise OrchestrationError("local router did not confirm stop; engines remain owned")


def _down_state(state):
    clients = {mid: _client(SimpleNamespace(**machine), state["known_hosts"])
               for mid, machine in state["machines"].items()}
    router = state.get("router")
    if router and router["machine"] == "local":
        _stop_local_router(router)
    errors = []
    service = state.get("prefill_service")
    if service and service.get("directory"):
        try:
            rtx.stop_service(service)
        except Exception as exc:  # still stop the engines; the error is reported below
            errors.append("prefill service: " + str(exc))
    leases = sorted(state["leases"], key=lambda row: row["role"] != "router")
    for lease in leases:
        mid = lease["machine"]
        root = state["machines"][mid]["install_root"]
        try:
            rows = _leases(_call(clients[mid], "status", {"root": root}))
            current = next((row for row in rows if row.get("lease", row.get("id")) == lease["lease"]), {})
            if current.get("state") in ("stopped", "failed", "aborted", "cancelled"):
                continue
            _call(clients[mid], "cancel" if current.get("state") == "queued" else "stop",
                  {"root": root, "lease": lease["lease"]})
            end = time.monotonic() + 40
            while current and current.get("state") not in ("stopped", "failed", "aborted", "cancelled"):
                rows = _leases(_call(clients[mid], "status", {"root": root}))
                current = next((row for row in rows if row.get("lease", row.get("id")) == lease["lease"]), {})
                if time.monotonic() >= end:
                    raise OrchestrationError(f"{mid}: lease {lease['lease']} did not stop")
                if current.get("state") not in ("stopped", "failed", "aborted", "cancelled"):
                    time.sleep(0.1)
        except Exception as exc:
            if lease["role"] == "router":
                raise OrchestrationError("router did not stop; engine ownership retained: " + str(exc)) from exc
            errors.append(str(exc))
    if errors:
        raise OrchestrationError("; ".join(errors))
    for key in state.get("keys", []):
        Path(key).unlink(missing_ok=True)


def down(profile, force=False):
    path = _state_path(profile)
    # This marker is separate from the atomically replaced ownership document.
    # It survives concurrent up writes and also covers a lease call in flight.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".stop").touch()
    if path.exists():
        try:
            state = _read(path)
        except FileNotFoundError:
            state = None  # The waiting controller may have just completed cleanup.
        if state is not None:
            _down_state(state)
    # Never remove ownership while up can still append an accepted lease.
    lock = procs.InstanceLock(path.with_suffix(".lock"))
    try:
        lock.acquire()
    except procs.ProcessError:
        return {"profile": profile, "state": "stop-requested"}
    try:
        if path.exists():
            _down_state(_read(path))
            path.unlink(missing_ok=True)
        return {"profile": profile, "state": "stopped"}
    finally:
        lock.close()


def status(profile=None, config_path=None, skip=None):
    """One profile's status, or every saved launch's ({profile: status}); with ``config_path``, only the launches of
    that configuration (`owns`): another configuration's launch is neither listed nor polled, and asked by name it
    answers stopped, with the reason. A launch without the record (written before launch states recorded it) is
    listed and read, marked ``config_identity: unknown`` with the reason: shown, never hidden, and stoppable.
    ``skip(machine id or "local", kind, profile)``: a reason not to read that node ("node"), the profile's router
    ("router", on that machine or this one) or its prefill service ("prefill", this machine) now (the portal's
    polling budget), or None."""
    if profile is None:
        names = (list(launch_states(config_path, unknown=True)) if config_path is not None
                 else [path.stem[3:] for path in secrets.runtime_dir().glob("up-*.json")])
        return {name: status(name, config_path, skip) for name in names}
    path = _state_path(profile)
    if not path.exists():
        return {"profile": profile, "state": "stopped"}
    state = _read(path)
    kind = ownership(state, config_path) if config_path is not None else None
    if kind == "other":
        return {"profile": profile, "state": "stopped", "why": "launched from another configuration"}
    result = {"profile": profile, "nodes": {}, "router": None,
              "assets": state.get("assets", []), "asset_cache_note": assets.CACHE_NOTE, "launch": state.get("launch"),
              "unpublished_sources": (state.get("launch") or {}).get("unpublished_sources", []),
              "production": state.get("production", True), "diagnostic": state.get("diagnostic", [])}
    if "recipe" in state:
        result["recipe"] = state["recipe"]
    if kind == "unknown":
        result["config_identity"], result["why_config"] = "unknown", UNKNOWN_CONFIG
    for mid, machine in state["machines"].items():
        why = skip(mid, "node", profile) if skip is not None else None
        if why:
            result["nodes"][mid] = {"error": why}       # not read (the caller's polling budget: a GPU turn holds it)
            continue
        try:
            result["nodes"][mid] = _call(_client(SimpleNamespace(**machine), state["known_hosts"]), "status", {"root": machine["install_root"]})
        except (remote.RemoteError, OrchestrationError) as exc:
            result["nodes"][mid] = {"error": str(exc)}
    if state.get("router"):
        router_machine = state["router"].get("machine")
        result["router_machine"] = router_machine
        why = skip(router_machine or "local", "router", profile) if skip is not None else None
        if why:
            result["router"] = {"error": why}
        else:
            try:
                result["router"] = _http_json(state["router"]["endpoint"])
            except (OSError, ValueError) as exc:
                result["router"] = {"error": str(exc)}
    if (state.get("prefill_service") or {}).get("port"):
        why = skip("local", "prefill", profile) if skip is not None else None
        if why:
            result["prefill_service"] = {"error": why}
        else:
            try:
                result["prefill_service"] = _http_json({"address": "127.0.0.1", "port": state["prefill_service"]["port"]}, "/health")
            except (OSError, ValueError) as exc:
                result["prefill_service"] = {"error": str(exc)}
    return result


def log_machine(profile, process, read=None):
    """Where ``logs`` reads a process's output, without reading it: "local" (the control machine's files), a machine
    id (an ssh call to that node), or None (not a process of this launch)."""
    state = (read or _read)(_state_path(profile))
    local = {"router": state.get("router") or {}, "prefill": state.get("prefill_service") or {}}.get(process, {})
    if local.get("directory") and (process == "prefill" or local.get("machine") == "local"):
        return "local"
    matches = [row for row in state.get("leases") or [] if row["role"] == "router" and process == "router"
               or row["role"] == "engine" and (row["machine"] == process or process in ("rank0", "rank1"))]
    if len(matches) != 1:
        return None
    lease = matches[0]
    name = _node_name(process) if process == lease["machine"] else process
    return lease.get("process_machines", {}).get(name, lease["machine"])


def logs(profile, process, tail=100, config_path=None):
    """A process's recent output; with ``config_path``, only of a launch of that configuration (`owns`): another
    configuration's launch (or one without the record) is refused before any read or call."""
    if type(tail) is not int or not 1 <= tail <= 65536:
        raise ValueError("tail must be in 1..65536")
    state = _read(_state_path(profile))
    if config_path is not None and not owns(state, config_path):
        raise OrchestrationError(f"{profile}: launched from another configuration")
    local = {"router": state.get("router") or {}, "prefill": state.get("prefill_service") or {}}.get(process, {})
    if local.get("directory") and (process == "prefill" or local.get("machine") == "local"):
        directory = Path(local["directory"])
        result = {}
        for stream in ("stdout", "stderr"):
            with (directory / (("router" if process == "router" else "service") + "." + stream + ".log")).open("rb") as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 32768))
                text = "\n".join(source.read().decode("utf-8", errors="replace").splitlines()[-tail:])
                if process == "prefill":  # the service takes its keys as literal arguments (R12)
                    for name in [_secret_name(profile, "prefill")] + [_secret_name(profile, m) for m in state["machines"]]:
                        value = secrets.get(name)
                        if value:
                            text = text.replace(value, "<redacted>")
                result[stream] = text
        return result
    matches = [row for row in state["leases"] if row["role"] == "router" and process == "router"
               or row["role"] == "engine" and (row["machine"] == process or process in ("rank0", "rank1"))]
    if len(matches) != 1:
        raise ValueError("choose a machine id, rank0, rank1, or router")
    lease = matches[0]
    name = _node_name(process) if process == lease["machine"] else process
    mid = lease.get("process_machines", {}).get(name, lease["machine"])
    machine = state["machines"][mid]
    return _call(_client(SimpleNamespace(**machine), state["known_hosts"]), "logs",
                 {"root": machine["install_root"], "lease": lease["lease"], "name": name, "tail": tail})
