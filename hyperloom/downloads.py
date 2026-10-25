"""Pinned model sources and user-confirmed downloads (SPEC §5.8).

Nothing downloads without a confirmation: ``plan`` asks the node for the exact commands
and a confirmation code; ``start`` runs only when the caller passes that code and the node
recomputes the same plan. Conversions and merges are documented, never run.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from . import orchestrate
from .config import ConfigError, PRESETS, preset, read_json, record
from .paths import repo_root


STATUS = ("download", "unavailable")


def catalog():
    data = read_json(PRESETS / "sources.json")
    record(data, "sources", ("schema", "engine_sha", "note", "profiles"))
    if data["schema"] != "hyperloom/sources-v1":
        raise ConfigError("sources: unknown schema")
    # Sources come from the ledgers at the pins: a pin move needs the catalog re-checked.
    if data["engine_sha"] != read_json(repo_root() / "versions.lock")["components"]["engine"]["commit"]:
        raise ConfigError("sources: catalog is not checked at the pinned engine commit")
    for profile, assets in data["profiles"].items():
        for name, row in assets.items():
            where = f"sources.{profile}.{name}"
            if row.get("status") not in STATUS:
                raise ConfigError(f"{where}: unknown status")
            if row["status"] == "unavailable":
                record(row, where, ("status", "why"))
                continue
            record(row, where, ("status", "repo", "revision", "files", "ledger", "license", "metadata"), ("after",))
            if not re.fullmatch(r"[0-9a-f]{40}", row["revision"]):
                raise ConfigError(f"{where}: revision must be an immutable commit")
            for item in row["files"]:
                record(item, where + ".files", ("file", "bytes", "sha256"))
    return data


def sources(cluster, profile):
    """Every asset of a profile with its pinned source or the reason it has none (read-only)."""
    _, kind, _ = orchestrate._profile(cluster, profile)
    rows = catalog()["profiles"].get(kind, {})
    return {name: rows.get(name, {"status": "unavailable", "why": "no pinned source"})
            for name in preset(kind)["assets"]}


def _request(cluster, profile, asset, machine_id):
    _, _, mids = orchestrate._profile(cluster, profile)
    if machine_id not in mids:
        raise ValueError(f"{machine_id} is not a machine of {profile}")
    row = sources(cluster, profile).get(asset)
    if row is None:
        raise ValueError(f"{profile} has no asset {asset}")
    if row["status"] != "download":
        raise ValueError(f"{asset} is not downloadable: {row['why']}")
    manifest = cluster.assets.get(machine_id, {}).get(asset)
    if manifest is None:
        raise ValueError(f"{machine_id}: register {asset} (path, bytes, sha256) in the asset manifest first")
    if not manifest["bytes"] or set(manifest["sha256"]) == {"0"}:
        raise ValueError(f"{machine_id}: {asset} needs its real bytes and sha256 in the manifest before a download")
    directory = PurePosixPath(manifest["path"]).parent
    machine = cluster.machines[machine_id]
    files = [{"repo": row["repo"], "revision": row["revision"], "file": item["file"], "bytes": item["bytes"],
              "sha256": item["sha256"], "directory": str(directory),
              "destination": str(directory / PurePosixPath(item["file"]).name)} for item in row["files"]]
    return machine, {"root": machine.install_root, "lab_dir": orchestrate._lab(cluster, machine_id), "files": files}, row


def plan(cluster, profile, asset, machine_id):
    machine, payload, row = _request(cluster, profile, asset, machine_id)
    result = orchestrate._call(orchestrate._client(machine), "download-plan", payload)
    return {"machine": machine.id, "asset": asset, "profile": profile, "files": result["files"],
            "disk": result["disk"], "after": row.get("after"), "confirm": result["confirm"],
            "next": f"hyperloom assets download {profile} {asset} --machine {machine.id} --confirm {result['confirm']}"}


def start(cluster, profile, asset, machine_id, confirm):
    if not isinstance(confirm, str) or not re.fullmatch(r"[0-9a-f]{12}", confirm):
        raise ValueError("--confirm takes the 12-character code printed by the plan")
    machine, payload, row = _request(cluster, profile, asset, machine_id)
    result = orchestrate._call(orchestrate._client(machine), "download", dict(payload, confirm=confirm))
    return {"machine": machine.id, "job": result["job"], "log": result["log"], "after": row.get("after")}


def job(cluster, machine_id, job_id, cancel=False):
    machine = cluster.machines[machine_id]
    payload = {"root": machine.install_root, "job": job_id}
    return orchestrate._call(orchestrate._client(machine), "download-cancel" if cancel else "job", payload)


# ---------------------------------------------------------------------------------------------------------------
# Model library (inventory and verification). The portal feature ``library`` and the CLI
# family ``hyperloom models`` both call these functions. Node work goes through the node tool's model verbs; the
# control machine's own roots are read in process with the same module (node/hyperloom_models.py).

import hashlib as _hashlib
import importlib.util as _importlib_util
import json as _json
import os as _os
from pathlib import Path as _Path, PureWindowsPath as _PureWindowsPath
import sys as _sys
import threading as _threading
import time as _time

from .winproc import retry_sharing

LIBRARY_PREFS = "library/roots"
LEDGER_PREF = "turns/ledger_path"
LEDGER_MAX = 64 * 1024 * 1024
LEDGER_LINE = re.compile(r"- (\d{2}):(\d{2}) ([^\s:]+): PC-BIG (start|end)\b")
PAUSE_LIMIT_S = 600.0
WATCH_FAILURES_S = 600.0
MAX_ROOTS = 16
_models_module = None


class Unresolved(RuntimeError):
    """A node job's outcome is not known (the node could not be read); it is not a failure of the job."""


class Cancelled(Exception):
    """The operation was cancelled (by the caller, the portal job, Ctrl+C or on the node)."""


def models_module():
    """The node's model library module, loaded from this checkout (the control machine runs the same code)."""
    global _models_module
    if _models_module is None:
        node = repo_root() / "node"
        _sys.path.insert(0, str(node))
        try:
            spec = _importlib_util.spec_from_file_location("hyperloom_node_models", node / "hyperloom_models.py")
            module = _importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            _sys.path.remove(str(node))
        _models_module = module
    return _models_module


def _pref(store, key, default=None):
    """A portal preference (store docs namespace ``prefs``); None without a store."""
    if store is None:
        return default
    value = store.get("prefs", key)
    return default if value is None else value


def control_machine(cluster):
    """The machine this process runs on (``access: local``), or None."""
    return next((m for m in cluster.machines.values() if m.access == "local"), None)


def _absolute_for(machine, value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError("a model root must be an absolute directory path")
    if machine.os == "windows":
        path = _PureWindowsPath(value)
        if not path.drive or not path.root or ".." in path.parts:
            raise ValueError(f"{machine.id}: model roots are drive-absolute Windows paths: {value}")
        return str(path)
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{machine.id}: model roots are absolute POSIX paths: {value}")
    return str(path)


def library_roots(cluster, store=None):
    """{machine id: [{path, source}]}: parents of the configured assets (``control_path`` ones belong to the control
    machine) and the operator's extra roots in prefs ``library/roots``."""
    roots = {mid: [] for mid in cluster.machines}
    local = control_machine(cluster)

    def add(mid, path, source):
        if mid in roots and path not in [row["path"] for row in roots[mid]] and len(roots[mid]) < MAX_ROOTS:
            roots[mid].append({"path": path, "source": source})

    for mid, assets in cluster.assets.items():
        machine = cluster.machines.get(mid)
        for asset in assets.values():
            if machine is not None:
                parent = (_PureWindowsPath if machine.os == "windows" else PurePosixPath)(asset["path"]).parent
                add(mid, str(parent), "config")
            if local is not None and "control_path" in asset:
                add(local.id, str(_PureWindowsPath(asset["control_path"]).parent if local.os == "windows"
                                  else PurePosixPath(asset["control_path"]).parent), "config")
    extra = _pref(store, LIBRARY_PREFS, {})
    if isinstance(extra, dict):
        for mid, values in extra.items():
            machine = cluster.machines.get(mid)
            if machine is None or not isinstance(values, list):
                continue
            for value in values:
                try:
                    add(mid, _absolute_for(machine, value), "prefs")
                except ValueError:
                    continue
    return roots


def check_roots(cluster, machine_id, values):
    """Validated prefs roots for one machine (POST library/roots, ``models roots --set``)."""
    machine = _machine(cluster, machine_id)
    if not isinstance(values, list) or len(values) > MAX_ROOTS:
        raise ValueError("roots must be a list of at most 16 absolute directories")
    result = []
    for value in values:
        path = _absolute_for(machine, value)
        if path not in result:
            result.append(path)
    return result


def set_roots(cluster, store, machine_id, values):
    if store is None:
        raise ValueError("the portal store is not open; preferences cannot be saved")
    roots = check_roots(cluster, machine_id, values)
    current = _pref(store, LIBRARY_PREFS, {})
    current = dict(current) if isinstance(current, dict) else {}
    if roots:
        current[machine_id] = roots
    else:
        current.pop(machine_id, None)
    store.put("prefs", LIBRARY_PREFS, current)
    return {"machine": machine_id, "roots": roots}


def _machine(cluster, machine_id):
    if not isinstance(machine_id, str) or machine_id not in cluster.machines:
        raise LookupError("unknown machine: " + str(machine_id))
    return cluster.machines[machine_id]


def _root_of(cluster, machine, value, store):
    """A model root of a machine, only when the configuration lists it."""
    configured = [row["path"] for row in library_roots(cluster, store)[machine.id]]
    if value not in configured:
        raise ValueError(f"{value} is not a configured model root of {machine.id}")
    return value


def _node(machine, verb, payload, timeout=60):
    """A node verb under the shared node gate (one call per machine at a time across processes)."""
    from . import cli, nodegate
    with nodegate.hold(cli.config_path(), machine.id, wait=15.0):
        result = orchestrate._client(machine).call(verb, payload, timeout=timeout)
    if result.get("error"):
        raise ValueError(f"{machine.id}: {verb}: {result['error']}")
    return result


def _policy(cluster, machine):
    """The guard policy the node checks: its install root, the lab directory and whether lab mode requires one."""
    try:
        lab = orchestrate._lab(cluster, machine.id)
    except orchestrate.OrchestrationError as exc:
        raise ValueError(str(exc) + " (the GPU turn guard needs it)") from None
    return {"root": machine.install_root, "lab_dir": lab, "lab_required": bool(machine.lab_mode)}


def _is_local(machine):
    return machine.access == "local"


def inventory(cluster, machine_id, store=None, now=None):
    """One machine's model units per root (node ``models`` verb; the control machine in process)."""
    machine = _machine(cluster, machine_id)
    roots = library_roots(cluster, store)[machine.id]
    now = _time.time() if now is None else now
    if not roots:
        return {"machine": machine.id, "scanned_at": now, "roots": [],
                "why": {"roots": "no model roots configured for this machine (config assets or prefs library/roots)"}}
    paths = [row["path"] for row in roots]
    if _is_local(machine):
        reply = models_module().run("models", {"roots": paths})
        if reply.get("error"):
            raise ValueError(f"{machine.id}: models: {reply['error']}")
    else:
        reply = _node(machine, "models", {**_policy(cluster, machine), "roots": paths}, timeout=30)
    if reply.get("busy"):
        return {"machine": machine.id, "busy": True, "scanned_at": None}
    sources = {row["path"]: row["source"] for row in roots}
    for row in reply.get("roots", []):
        row["source"] = sources.get(row.get("path"), "config")
    return {"machine": machine.id, "scanned_at": now, "roots": reply.get("roots", []), "cache": reply.get("cache")}


# --- RTX PRO big-job ledger guard (control machine) -------------------------------------------------------------

class _FileLock:
    """An exclusive lock on an existing lock file (msvcrt on Windows, flock on POSIX), waited for at most
    ``wait_s``; ``create`` only for ledger-accept, which provisions the file."""

    def __init__(self, path, wait_s=1.0, create=False):
        self.path, self.wait_s, self.create, self.stream = _Path(path), wait_s, create, None

    def __enter__(self):
        if not self.create and not self.path.is_file():
            raise ValueError("the RTX big-job ledger has no accepted baseline: run hyperloom models ledger-accept")
        if self.create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as stream:
                if stream.tell() == 0:
                    stream.write(b"\0")
        self.stream = open(self.path, "r+b")
        deadline = _time.monotonic() + self.wait_s
        while True:
            try:
                if _os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if _time.monotonic() >= deadline:
                    self.stream.close()
                    raise TimeoutError("the RTX big-job ledger could not be checked in time (its state is locked)") \
                        from None
                _time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if _os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.stream.close()


class LedgerGuard:
    """No verification on the control machine while an RTX PRO big job is open in the coordination ledger
    (prefs ``turns/ledger_path``). The whole file is evaluated at every check against a monotonic checkpoint of
    everything observed so far (identity, size, sha256 of that prefix): the file may only grow; a replaced, truncated
    or rewritten ledger blocks until the operator accepts it again (``hyperloom models ledger-accept``). Every
    evaluation that sees the file grow advances the checkpoint before it looks for open ``PC-BIG start`` lines, so a
    start once observed can never disappear. Lock waits and evaluations are bounded (1 s, 2 s)."""

    BUDGET_S = 2.0

    def __init__(self, config_dir, ledger_path):
        self.directory = _Path(config_dir)
        self.ledger = _Path(ledger_path) if ledger_path else None
        self.state_path = self.directory / "library-ledger.json"
        self.lock_path = self.directory / "library-ledger.lock"

    @property
    def configured(self):
        return self.ledger is not None

    def _read(self, deadline):
        """The ledger's identity and bytes, both from one open descriptor (a rotation between looking the path up
        and reading it cannot pair the old identity with the new content)."""
        chunks = []
        with open(self.ledger, "rb") as stream:
            info = _os.fstat(stream.fileno())
            if info.st_size > LEDGER_MAX:
                raise ValueError("the RTX big-job ledger is larger than 64 MiB; it cannot be checked")
            while True:
                if _time.monotonic() > deadline:
                    raise TimeoutError("the RTX big-job ledger could not be checked in time")
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > LEDGER_MAX:
                    raise ValueError("the RTX big-job ledger is larger than 64 MiB; it cannot be checked")
            after = _os.fstat(stream.fileno())
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("the RTX big-job ledger changed while it was read")
        return [info.st_dev, info.st_ino], b"".join(chunks)

    def _state(self):
        if not self.state_path.is_file():
            raise ValueError("the RTX big-job ledger has no accepted baseline: run hyperloom models ledger-accept")
        try:
            state = _json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not {"identity", "size", "sha256"} <= set(state):
                raise ValueError
            return state
        except (OSError, ValueError):
            raise ValueError("the ledger checkpoint is unreadable; review the ledger and run hyperloom models "
                             "ledger-accept") from None

    @staticmethod
    def open_starts(data):
        """Labels with a start and no later end (complete lines only) and whether a trailing line is incomplete."""
        text = data.decode("utf-8", "replace")
        complete, _, partial = text.rpartition("\n")
        if "\n" not in text:
            complete, partial = "", text
        open_starts = {}
        for line in complete.splitlines():
            match = LEDGER_LINE.match(line)
            if not match:
                continue
            if match[4] == "start":
                open_starts[match[3]] = line[:200]
            else:
                open_starts.pop(match[3], None)
        return open_starts, bool(partial.strip())

    def check(self):
        """None when clear, else the reason it blocks."""
        if self.ledger is None:
            return None
        deadline = _time.monotonic() + self.BUDGET_S
        try:
            with _FileLock(self.lock_path):
                state = self._state()
                identity, data = self._read(deadline)
                if (identity != state["identity"] or len(data) < state["size"]
                        or _hashlib.sha256(data[:state["size"]]).hexdigest() != state["sha256"]):
                    return ("the RTX big-job ledger was replaced, truncated or rewritten since it was last checked; "
                            "review it and run hyperloom models ledger-accept")
                if len(data) > state["size"]:
                    # Advance over every observed byte (an incomplete trailing line too) before evaluating: whatever
                    # was seen, an open start in particular, can only disappear through a rewrite, which blocks.
                    self._write({**state, "size": len(data), "sha256": _hashlib.sha256(data).hexdigest(),
                                 "advanced_at": _time.time()})
                starts, partial = self.open_starts(data)
                if _time.monotonic() > deadline:
                    raise TimeoutError("the RTX big-job ledger could not be checked in time")
        except (OSError, ValueError) as exc:     # TimeoutError is an OSError
            return str(exc) if "ledger" in str(exc) else "the RTX big-job ledger cannot be checked: " + str(exc)
        if starts:
            label, line = next(iter(starts.items()))
            return "an RTX PRO big job is open in the ledger (" + label + "): " + line
        if partial:
            return "the RTX big-job ledger has a line being written"
        return None

    def _write(self, state):
        """Durable replacement of the checkpoint: content fsynced before the rename (and the directory after it
        where the platform allows), so a crash never rolls the checkpoint back to an unwritten state."""
        temporary = self.state_path.with_name(self.state_path.name + "." + _os.urandom(6).hex() + ".tmp")
        try:
            with open(temporary, "xb") as stream:
                stream.write(_json.dumps(state).encode("utf-8"))
                stream.flush()
                _os.fsync(stream.fileno())
            if _os.name == "nt":
                # Write-through rename: the replacement itself is durable when the call returns.
                retry_sharing(lambda: models_module().windows_replace(temporary, self.state_path))
            else:
                _os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)
        if _os.name == "posix":
            directory = _os.open(self.directory, _os.O_RDONLY)
            try:
                _os.fsync(directory)
            finally:
                _os.close(directory)

    def accept(self, now=None):
        if self.ledger is None:
            raise ValueError("no RTX big-job ledger is configured (prefs turns/ledger_path)")
        with _FileLock(self.lock_path, wait_s=10, create=True):
            identity, data = self._read(_time.monotonic() + 30)
            state = {"identity": identity, "size": len(data), "sha256": _hashlib.sha256(data).hexdigest(),
                     "accepted_at": _time.time() if now is None else now}
            self._write(state)
        return state

    def describe(self):
        if self.ledger is None:
            return {"configured": False, "why": "prefs turns/ledger_path is not set: the RTX big-job guard is off"}
        try:
            state = self._state()
        except ValueError as exc:
            return {"configured": True, "baseline": None, "why": str(exc)}
        return {"configured": True, "baseline": {"accepted_at": state.get("accepted_at"),
                                                 "advanced_at": state.get("advanced_at"), "size": state["size"]}}


class LedgerMonitor:
    """Evaluates the ledger in its own thread every second; ``reason()`` is the latest result, or "overdue" when
    the latest evaluation is older than 3 s, so a slow or contended ledger never delays the caller's loop."""

    def __init__(self, guard, interval=1.0, stale_s=3.0):
        self._init(guard, interval, stale_s)

    def _init(self, guard, interval, stale_s):
        self.guard, self.interval, self.stale_s = guard, interval, stale_s
        self.result, self.at = "the RTX big-job ledger has not been checked yet", 0.0
        self.stop = _threading.Event()
        self.thread = None

    def __enter__(self):
        if self.guard is None or not self.guard.configured:
            return self
        self.result, self.at = self.guard.check(), _time.monotonic()
        self.thread = _threading.Thread(target=self._run, name="hyperloom-ledger", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.wait(self.interval):
            result = self.guard.check()
            self.result, self.at = result, _time.monotonic()

    def reason(self):
        if self.guard is None or not self.guard.configured:
            return None
        if _time.monotonic() - self.at > self.stale_s:
            return "the RTX big-job ledger check is overdue"
        return self.result

    def __exit__(self, *exc):
        self.stop.set()


def ledger_guard(config_path, store=None):
    return LedgerGuard(_Path(config_path).parent, _pref(store, LEDGER_PREF))


# --- node jobs -----------------------------------------------------------------------------------------------

def watch_job(cluster, machine_id, job, *, cancelled=lambda: False, progress=lambda record: None, poll=2.0,
              sleep=_time.sleep):
    """Poll a node job to its end; a cancellation request cancels it on the node and waits for its record."""
    machine = _machine(cluster, machine_id)
    asked, failing_since = None, None
    while True:
        try:
            record = _node(machine, "copy-job", {"root": machine.install_root, "job": job}, timeout=30)
            failing_since = None
        except (ValueError, RuntimeError, OSError) as exc:
            # A busy gate or an unreachable node: retried (cancellation still sent); after 10 min of failures the
            # watcher gives up and says the job is unresolved (it keeps running on the node, follow it there).
            now = _time.monotonic()
            failing_since = failing_since or now
            if now - failing_since > WATCH_FAILURES_S:
                raise Unresolved(f"the node job {job} on {machine.id} could not be read for 10 minutes ({exc}); "
                                 "it may still be running: follow it with hyperloom models job") from None
            record = {"state": "unknown", "job": job, "reason": str(exc)}
        progress(record)
        if record["state"] in ("succeeded", "failed", "cancelled"):
            return record
        if cancelled() and (asked is None or _time.monotonic() - asked > 30):
            # Asked again every 30 s until the node's job sees it; its real final record is what this returns.
            asked = _time.monotonic()
            try:
                _node(machine, "copy-cancel", {"root": machine.install_root, "job": job}, timeout=30)
            except (ValueError, RuntimeError, OSError):
                pass
        sleep(poll)


# --- verification --------------------------------------------------------------------------------------------

def verify_start(cluster, machine_id, model_root, name, *, store=None, ledger=None, cancelled=lambda: False,
                 progress=lambda record: None, sleep=_time.sleep, clock=_time.monotonic):
    """Node: start the node job and return {mode: node, machine, job}. Control machine: hash here (cache-free,
    protected opens, the ledger guard from a monitor thread) and return the result record."""
    machine = _machine(cluster, machine_id)
    model_root = _root_of(cluster, machine, model_root, store)
    if not _is_local(machine):
        reply = _node(machine, "model-verify", {**_policy(cluster, machine), "model_root": model_root, "name": name},
                      timeout=60)
        return {"mode": "node", "machine": machine.id, "job": reply["job"]}
    models = models_module()
    if models.cache_control() is None:
        raise ValueError("this machine cannot read without filling the file cache; refused")
    if machine.lab_mode and (ledger is None or not ledger.configured):
        raise PermissionError("the RTX big-job ledger is not configured (prefs turns/ledger_path); lab mode requires "
                              "it for verification on this machine")
    paused = [0.0]
    with LedgerMonitor(ledger) as monitor:
        def check():
            if cancelled():
                raise Cancelled("cancelled")
            reason = monitor.reason()
            if not reason:
                return
            started = clock()
            while reason:
                progress({"state": "paused", "paused": {"reason": reason}})
                if paused[0] + clock() - started > PAUSE_LIMIT_S:
                    raise models.Stopped("paused for more than 10 minutes (" + reason + ")")
                if cancelled():
                    raise Cancelled("cancelled")
                sleep(1.0)
                reason = monitor.reason()
            paused[0] += clock() - started

        try:
            result = models.verify_unit(model_root, name, check,
                                        lambda done: progress({"state": "running", "progress": {"bytes_done": done}}))
        except models.Stopped as exc:
            raise ValueError(str(exc)) from None
    return {"mode": "local", "machine": machine.id, "state": "succeeded", "result": result}


def library_job(cluster, machine_id, job, cancel=False):
    """A model library job on a node (a verification): its record, or a cancellation request."""
    machine = _machine(cluster, machine_id)
    if _is_local(machine):
        raise ValueError(machine.id + " runs no node jobs (control-machine work runs in the portal or the CLI)")
    payload = {"root": machine.install_root, "job": job}
    return _node(machine, "copy-cancel" if cancel else "copy-job", payload, timeout=30)
