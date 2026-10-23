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
from . import assets, procs, remote, render, rtx, secrets
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
    mids = settings["nodes"] if kind == "qwen-replicas" else [settings["rank0"], settings["rank1"]]
    return settings, kind, list(mids)


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


def _required_flags(kind, targets):
    """Flags the frozen preset passes to its binary; the node checks they exist in the built binary."""
    from .config import preset
    flags = sorted({token for token in preset(kind)["argv"] if token.startswith("--")})
    binary = preset(kind)["binary"]
    return {target: flags for target in targets if target == binary}


def install(cluster, build_machine, targets, *, profile=None):
    """Build once and verify copies before activating a profile on every node."""
    if profile is not None:
        _, kind, mids = _profile(cluster, profile)
    else:
        profiles = [name for name in cluster.profiles if
                    ("strix-deepseek41-serve" in targets) ==
                    (cluster.profiles[name].get("preset", name) == "ds41-pair")]
        mids = list(dict.fromkeys(mid for name in profiles for mid in _profile(cluster, name)[2]))
        kind = "ds41-pair" if "strix-deepseek41-serve" in targets else "qwen-replicas"
    if build_machine not in mids:
        raise ValueError("build machine must belong to the profile")
    clients = {mid: _client(cluster.machines[mid]) for mid in mids}
    for mid in mids:
        _lab(cluster, mid)
        clients[mid].bootstrap()
    # Resolve all peer routes before starting any build.
    peers = {mid: _peer(cluster, build_machine, mid) for mid in mids if mid != build_machine}
    versions = _versions()
    payload = {"root": cluster.machines[build_machine].install_root,
               "lab_dir": _lab(cluster, build_machine),
               "asset_paths": [asset["path"] for asset in cluster.assets.get(build_machine, {}).values()],
               "components": {name: {key: row[key] for key in ("repo", "commit")}
                              for name, row in versions["components"].items()},
               "recipe": {"configure": versions["toolchains"]["hip_configure"],
                          "targets": list(targets),
                          "ctest_regex": "^strix-(serve-(frontend|deepseek-chat|api)|host-upload)-tests$",
                          "trellis": kind == "ds41-pair",
                          # Checked statically on the node (flag strings in the binary); never executed.
                          "required_flags": _required_flags(kind, targets)}}
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


def _release(client, machine, profile, configured):
    data = _call(client, "releases", {"root": machine.install_root})
    release = configured or data.get("active", {}).get(profile)
    rows = data.get("releases", [])
    if isinstance(rows, dict):
        rows = [dict(value, release_id=key) for key, value in rows.items()]
    row = next((row for row in rows if row.get("release_id", row.get("id")) == release), None)
    if not release or row is None:
        raise OrchestrationError(f"{machine.id}: no installed release for {profile}")
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
                                     f"expected {expected[:12]}); run 'hyperloom nodes update {profile}'")


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


def up(cluster, profile, router="local", nodes=None, devices=None, *, deadline_s=1140, approval=None, queue_timeout_s=3600,
       diagnostics=None):
    _approval(deadline_s, approval)
    if type(queue_timeout_s) is not int or not 60 <= queue_timeout_s <= MAX_QUEUE_S:
        raise ValueError(f"queue timeout must be 60..{MAX_QUEUE_S} seconds")
    settings, kind, defaults = _profile(cluster, profile)
    diagnostics = list(dict.fromkeys(diagnostics or ()))
    unknown = set(diagnostics) - set(render.preset(kind).get("diagnostics", {}))
    if unknown:
        raise ValueError("unknown diagnostic for this profile: " + ", ".join(sorted(unknown)))
    mids = list(defaults if nodes is None else nodes)
    for mid in mids:
        if mid not in cluster.machines:
            raise ValueError("unknown machine: " + mid)
    if not mids or len(mids) != len(set(mids)) or any(mid not in defaults for mid in mids):
        raise ValueError("declared nodes must be distinct machines of the profile")
    if kind == "ds41-pair" and mids != defaults:
        raise ValueError("pair requires both rank machines in rank order")
    declared = list(mids)
    service = settings.get("prefill_service")
    if service is not None and router != "local":
        raise ValueError("the RTX prefill service is loopback-only: use --router local")
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
             "production": not diagnostics, "diagnostic": diagnostics}
    keys = []
    local_values = {}
    owned = False
    handlers = ExitStack()
    try:
        if path.exists():
            raise OrchestrationError("profile has saved running state; run down before up")
        path.with_suffix(".stop").unlink(missing_ok=True)
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
        peer = _peer(cluster, mids[0], mids[1]) if kind == "ds41-pair" else None
        selected = deepcopy(cluster)
        if kind == "qwen-replicas":
            selected.profiles[profile]["nodes"] = mids
        releases = {}
        _check_tools(clients, mids, profile)
        state["assets"] = assets.inspect(cluster, profile, clients, mids, strict=True)
        state["asset_cache_note"] = assets.CACHE_NOTE
        for mid in mids:
            releases[mid] = _release(clients[mid], cluster.machines[mid], profile, settings.get("release_id"))
        if len(set(releases.values())) != 1:
            raise OrchestrationError("profile nodes must run the same release id: "
                                     + ", ".join(f"{mid} {rid}" for mid, rid in releases.items()))
        selected.profiles[profile]["release_id"] = releases[mids[0]]
        node_values = {}
        node_paths = {}
        for mid in mids:
            name = _secret_name(profile, mid)
            node_values[mid] = secrets.ensure(name)
            node_paths[mid] = _put(clients[mid], cluster.machines[mid], name, node_values[mid])
        selected.profiles[profile]["api_key_files"] = node_paths
        selected.router["machine"] = router
        selected.router["replica_api_key_files"] = {}
        router_key = _secret_name(profile, "router")
        if router == "local":
            for mid in mids:
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
            for mid in mids:
                selected.router["replica_api_key_files"][mid] = _put(
                    clients[router], machine, _secret_name(profile, mid), node_values[mid])
            selected.router["api_key_file"] = _put(clients[router], machine, router_key, secrets.ensure(router_key))
            host = machine.http_address
            selected.router["listen"] = f"[{host}]:18600" if ":" in host else f"{host}:18600"
        # Diagnostic runs are labelled non-production everywhere they are recorded (state, leases, status).
        processes = render.render_processes(selected, profile, **(
            {"diagnostics": diagnostics, "run": state["token"][:12]} if diagnostics else {}))
        rendered = render.render_router(selected, profile)
        state.update(release_id=releases[mids[0]], nodes=mids, devices=list(devices or []),
                     started_at=time.time(), process_specs=[asdict(spec) for spec in processes],
                     rendered_router=rendered, launch=render.launch_summary(selected, profile))
        _write(path, state)
        # Persist each returned lease immediately, including while it is queued.
        def start(mid, payload, role):
            _check_stop(path)
            result = _call(clients[mid], "lease", payload)
            record = {"machine": mid, "lease": result["lease"], "role": role,
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
            # DeepSeek rank 1 has no HTTP listener; its READY event accompanies
            # the pair's health endpoint on rank 0.
            health_machine = cluster.machines[mids[0]] if kind == "ds41-pair" else machine
            port = int(spec.argv[spec.argv.index("--http-port") + 1])
            return {"name": spec.role if kind == "ds41-pair" else _node_name(spec.machine_id),
                    "machine": location, "argv": spec.argv, "env": spec.env,
                    "ready": spec.ready,
                    "health_url": _url({"address": health_machine.http_address, "port": port}, "/health"),
                    "key_file": node_paths[spec.machine_id], "ready_timeout_s": 600}

        for index, mid in enumerate(mids if kind == "qwen-replicas" else mids[:1]):
            payload = {"root": cluster.machines[mid].install_root, "lab_dir": _lab(cluster, mid),
                       "label": f"hl-qwen-{_node_name(mid)}-{uuid.uuid4().hex[:4]}" if kind == "qwen-replicas" else f"hl-ds41-{uuid.uuid4().hex[:4]}",
                       "kind": "single" if kind == "qwen-replicas" else "pair", "deadline_s": deadline_s,
                       "approval": approval, "profile": profile, "layout_required": "1" if kind == "ds41-pair" else None,
                       "peer_ssh": peer, "peer_root": cluster.machines[mids[1]].install_root if peer else None,
                       "peer_lab_dir": _lab(cluster, mids[1]) if peer else None,
                       "processes": [process(processes[index], "local")] if kind == "qwen-replicas" else
                       [process(spec, "local" if i == 0 else "peer") for i, spec in enumerate(processes)]}
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
        if service is not None:
            # After the decode side is READY (its import route is the handoff), before the router admits.
            spec = rtx.render_service(selected, profile, api_key=secrets.ensure(_secret_name(profile, "prefill")),
                                      handoff_api_key=node_values[mids[0]],
                                      free_physical_gib=rtx.memory_status()["free_physical_gib"])
            spec = rtx.ServiceSpec(**dict(spec.__dict__, deadline=min(spec.deadline, state["deadline"])))
            # Recorded before the helper exists, so down (or the watchdog) can always find and stop it.
            state["prefill_service"] = rtx.prepare_service(spec)
            _write(path, state)
            rtx.launch_service(state["prefill_service"], spec, detached=_detached,
                               owner={"state_path": str(path), "token": state["token"]},
                               check=lambda: _check_stop(path))
            _write(path, state)
            _check_stop(path)
        if router == "local":
            for key, value in local_values.items():
                secrets._write(Path(key), value.encode("ascii"))
            render.validate_router(rendered)
            state["router"] = _start_local_router(profile, rendered, keys, ownership=state)
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


def endpoint(profile):
    state = _read(_state_path(profile))
    if not state.get("router"):
        raise OrchestrationError("profile router has not started")
    return {"url": _url(state["router"]["endpoint"], ""),
            "key_name": _secret_name(profile, "router")}


def _node_name(mid):
    if re.fullmatch(r"[a-z0-9-]{1,29}", mid):
        return mid
    import hashlib
    return re.sub(r"[^a-z0-9-]", "-", mid.lower())[:20] + "-" + hashlib.sha256(mid.encode()).hexdigest()[:8]


def _start_local_router(profile, rendered, keys, *, ownership=None):
    directory = secrets.runtime_dir() / ("router-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    spec = directory / "spec.json"
    _write(spec, {"rendered": rendered, "keys": keys, "profile": profile,
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
    try:
        child.start()
        end = time.monotonic() + 15
        ready = False
        next_check = 0
        while child.alive() and not interrupted and not (directory / "stop").exists():
            if time.time() >= spec["deadline"]:
                break
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


def status(profile=None):
    if profile is None:
        return {path.stem[3:]: status(path.stem[3:]) for path in secrets.runtime_dir().glob("up-*.json")}
    path = _state_path(profile)
    if not path.exists():
        return {"profile": profile, "state": "stopped"}
    state = _read(path)
    result = {"profile": profile, "nodes": {}, "router": None,
              "assets": state.get("assets", []), "asset_cache_note": assets.CACHE_NOTE, "launch": state.get("launch"),
              "production": state.get("production", True), "diagnostic": state.get("diagnostic", [])}
    for mid, machine in state["machines"].items():
        try:
            result["nodes"][mid] = _call(_client(SimpleNamespace(**machine), state["known_hosts"]), "status", {"root": machine["install_root"]})
        except (remote.RemoteError, OrchestrationError) as exc:
            result["nodes"][mid] = {"error": str(exc)}
    if state.get("router"):
        try:
            result["router"] = _http_json(state["router"]["endpoint"])
        except (OSError, ValueError) as exc:
            result["router"] = {"error": str(exc)}
    if (state.get("prefill_service") or {}).get("port"):
        try:
            result["prefill_service"] = _http_json({"address": "127.0.0.1", "port": state["prefill_service"]["port"]}, "/health")
        except (OSError, ValueError) as exc:
            result["prefill_service"] = {"error": str(exc)}
    return result


def logs(profile, process, tail=100):
    if type(tail) is not int or not 1 <= tail <= 65536:
        raise ValueError("tail must be in 1..65536")
    state = _read(_state_path(profile))
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
