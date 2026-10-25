"""Model library (F36): model units per machine and verification against their manifests.

GET library -> hyperloom/ui-library-v1: every machine with its model roots (config assets and prefs
    ``library/roots``), its last scan, the last verification of each unit, recent library jobs and the RTX big-job
    ledger guard state. It reads the portal store only: no node call, no file named by the client, nothing created.
    Copies between machines are not part of this release; the document says so (``copy``).
POST library/job {machine, job}: a node job record. POST library/cancel {machine, job} (audited).
POST library/roots {machine, roots} (audited): the extra model roots of one machine.
POST library/ledger-accept {} (audited): accept the current RTX big-job ledger as the guard's baseline.
Jobs: library/scan {machine?}, library/verify {machine, model_root, name}. They take no GPU turn (no admission
    hold): the node guards make them pause while a turn is held or a lease is live. The CLI family
    ``hyperloom models`` runs the same functions.
"""

import re
import threading
import time
import uuid

from hyperloom import downloads, telemetry
from hyperloom.web.api import JobFailed


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
SCAN_STALE_S = 600
COPY = {"available": False, "why": "copies between machines are not available in this release"}
_scans = {}
_scan_lock = threading.Lock()


def _arguments(arguments, required, optional=()):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    extra = set(arguments) - set(required) - set(optional)
    missing = set(required) - set(arguments)
    if extra or missing:
        raise ValueError("unexpected arguments " + str(sorted(extra)) + "; missing arguments " + str(sorted(missing)))
    for key, value in arguments.items():
        if key != "roots" and (not isinstance(value, str) or not value or len(value) > 1024):
            raise ValueError(key + " must be a nonempty string")
    return arguments


def _machine(cluster, value):
    if not ID.fullmatch(value) or value not in cluster.machines:
        raise LookupError("unknown machine " + value)
    return value


def _ledger(api, store):
    return downloads.ledger_guard(api.config_path, store)


def overview(api, query):
    if query:
        raise ValueError("library takes no query arguments")
    store = telemetry.existing_store(api)
    cluster = api.cluster()
    roots = downloads.library_roots(cluster, store)
    why = {}
    if store is None:
        why["inventory"] = "the portal store is not open in this process: scans and results are not kept"
    verify = {}
    jobs = []
    if store is not None:
        for key, ts, value in store.items("library/verify", limit=500):
            verify[key] = value
        jobs = [value for _, _, value in store.items("library/jobs", limit=30)]
    machines = []
    for machine in cluster.machines.values():
        inventory = store.get("library/inventory", machine.id) if store is not None else None
        row = {"id": machine.id, "os": machine.os, "access": machine.access, "lab_mode": machine.lab_mode,
               "roots": roots.get(machine.id, []), "inventory": inventory, "why": {}}
        if not row["roots"]:
            row["why"]["roots"] = "no model roots: none of the configured assets is on this machine and prefs " \
                                  "library/roots has none"
        elif inventory is None:
            row["why"]["inventory"] = "not scanned yet" if store is not None else why["inventory"]
        machines.append(row)
    with _scan_lock:
        scanning = sorted(mid for mid, since in _scans.items() if time.time() - since < SCAN_STALE_S)
    return api.document("library", machines=machines, verify=verify, jobs=jobs, scanning=scanning,
                        ledger=_ledger(api, store).describe(), why=why, copy=COPY)


# --- synchronous POSTs -------------------------------------------------------------------------------------------

def job_state(api, arguments):
    a = _arguments(arguments, ("machine", "job"))
    if not ID.fullmatch(a["job"]):
        raise ValueError("invalid job id")
    cluster = api.cluster()
    return {"job": downloads.library_job(cluster, _machine(cluster, a["machine"]), a["job"])}


def cancel(api, arguments):
    a = _arguments(arguments, ("machine", "job"))
    if not ID.fullmatch(a["job"]):
        raise ValueError("invalid job id")
    cluster = api.cluster()
    return {"job": downloads.library_job(cluster, _machine(cluster, a["machine"]), a["job"], cancel=True)}


def roots(api, arguments):
    a = _arguments(arguments, ("machine", "roots"))
    cluster = api.cluster()
    if not isinstance(a["roots"], list) or any(not isinstance(value, str) for value in a["roots"]):
        raise ValueError("roots must be a list of absolute directories")
    return downloads.set_roots(cluster, telemetry.open_store(api), _machine(cluster, a["machine"]), a["roots"])


def ledger_accept(api, arguments):
    _arguments(arguments, ())
    store = telemetry.open_store(api)
    state = _ledger(api, store).accept()
    return {"accepted_at": state["accepted_at"], "size": state["size"]}


# --- jobs ----------------------------------------------------------------------------------------------------

def portal_job(api, ctx):
    """The portal job id of a running feature job (the page cancels it through the core ``cancel`` action)."""
    running = getattr(api, "_features_running", {}) or {}
    return next((jid for jid, context in list(running.items()) if context is ctx), None)


class Row:
    """The job's row in store docs ``library/jobs`` (shown on the page), updated as it runs; it carries the portal
    job id from the start and the node job id as soon as the node allocates one."""

    def __init__(self, api, kind, ctx=None, **fields):
        self.store = telemetry.open_store(api)
        self.id = uuid.uuid4().hex
        self.value = {"id": self.id, "kind": kind, "state": "running", "started": time.time(),
                      "portal_job": portal_job(api, ctx) if ctx is not None else None, **fields}
        self.save()

    def save(self):
        self.store.put("library/jobs", self.id, self.value)

    def update(self, **fields):
        self.value.update(fields)
        self.save()


def fail(row, exc):
    """Record a failed or unresolved outcome on the row, then end the portal job with the same outcome (JobFailed:
    it stays failed, with that outcome on the job and its audit line, even when a cancel was requested)."""
    unresolved = isinstance(exc, downloads.Unresolved) or "unresolved" in str(exc)
    state = "unresolved" if unresolved and row.value.get("node_job") else "failed"
    row.update(state=state, finished=time.time(), reason=str(exc))
    raise JobFailed(str(exc), outcome=state) from exc


def finish(ctx, row, record):
    """Map a node job's terminal record to the portal job: succeeded returns it, failed raises with the node's
    reason, cancelled (here or on the node) ends the portal job cancelled."""
    state = record.get("state")
    row.update(state=state, finished=time.time(), node=_summary(record),
               **({"reason": record["reason"]} if record.get("reason") else {}))
    if state == "succeeded":
        return record
    if state == "cancelled":
        if not ctx.cancelled():
            ctx.cancel("the node job was cancelled")
        return None
    message = record.get("reason") or "the node job failed"
    raise JobFailed(message, outcome="failed")


def _summary(record):
    keep = ("job", "kind", "mode", "state", "progress", "paused", "reason", "result", "files", "bytes", "finished")
    return {key: record[key] for key in keep if key in record}


def _progress(ctx, row):
    last = [0.0]

    def report(record):
        text = record.get("state", "running")
        progress = record.get("progress") or {}
        if record.get("paused") and (record["paused"] or {}).get("reason"):
            text = "paused: " + record["paused"]["reason"]
        elif progress.get("path"):
            text = f"{progress.get('file', '?')}/{progress.get('files', '?')} {progress['path']}"
        elif progress.get("bytes_done") is not None:
            text = f"hashed {progress['bytes_done']} bytes"
        ctx(text)
        if record.get("job") and not row.value.get("node_job"):
            row.update(node_job=record["job"])
        if time.monotonic() - last[0] >= 2:
            last[0] = time.monotonic()
            row.update(progress=text, node=_summary(record))
    return report


def scan_validate(api, arguments):
    a = _arguments(arguments, (), ("machine",))
    cluster = api.cluster()
    machines = [_machine(cluster, a["machine"])] if "machine" in a else list(cluster.machines)
    with _scan_lock:
        now = time.time()
        busy = [mid for mid in machines if now - _scans.get(mid, 0) < SCAN_STALE_S]
        if busy:
            raise PermissionError("a scan of " + ", ".join(busy) + " is already running")
        for mid in machines:
            _scans[mid] = now
    return {"machines": machines}


def scan_rollback(api, arguments):
    """The scan was validated but not started (refused or not audited): its machines are free again."""
    with _scan_lock:
        for mid in arguments.get("machines") or []:
            _scans.pop(mid, None)


def scan(api, arguments, ctx):
    machines = arguments["machines"]
    try:
        cluster = api.cluster()
        store = telemetry.open_store(api)
        result = {}
        for mid in machines:
            if ctx.cancelled():
                return None
            ctx("scanning " + mid)
            try:
                inventory = downloads.inventory(cluster, mid, store)
            except Exception as exc:          # one machine failing must not hide the others
                inventory = {"machine": mid, "scanned_at": time.time(), "roots": [], "error": str(exc)}
            if inventory.get("busy"):
                result[mid] = "busy (another scan is running on the node; the previous result is kept)"
                continue
            store.put("library/inventory", mid, inventory)
            result[mid] = "error: " + inventory["error"] if inventory.get("error") else "scanned"
        return {"machines": result}
    finally:
        with _scan_lock:
            for mid in machines:
                _scans.pop(mid, None)


_sleep = time.sleep


def _paced(ctx):
    """The jobs' wait: ``ctx.wait`` (wakes early on cancellation) until cancelled; after that a real pause, so
    settling a cancelled node job still polls it at its interval instead of continuously."""
    def wait(seconds):
        if ctx.cancelled():
            _sleep(seconds)
            return True
        return ctx.wait(seconds)
    return wait


def verify_validate(api, arguments):
    a = _arguments(arguments, ("machine", "model_root", "name"))
    _machine(api.cluster(), a["machine"])
    return a


def verify(api, arguments, ctx):
    cluster = api.cluster()
    store = telemetry.open_store(api)
    a = arguments
    row = Row(api, "verify", ctx, machine=a["machine"], model_root=a["model_root"], name=a["name"])
    key = f"{a['machine']}|{a['model_root']}|{a['name']}"
    report = _progress(ctx, row)
    try:
        started = downloads.verify_start(cluster, a["machine"], a["model_root"], a["name"], store=store,
                                         ledger=_ledger(api, store), cancelled=ctx.cancelled, progress=report,
                                         sleep=_paced(ctx))
        if started["mode"] == "node":
            row.update(node_job=started["job"])
            record = downloads.watch_job(cluster, a["machine"], started["job"], cancelled=ctx.cancelled,
                                         progress=report, sleep=_paced(ctx))
        else:
            record = started
    except downloads.Cancelled:
        row.update(state="cancelled", finished=time.time())
        if not ctx.cancelled():
            ctx.cancel("cancelled")
        return None
    except Exception as exc:
        fail(row, exc)
    if record.get("state") in ("succeeded", "failed"):
        store.put("library/verify", key, {"at": time.time(), "state": record["state"], "result": record.get("result"),
                                          "reason": record.get("reason")})
    return finish(ctx, row, record)


def register(reg):
    reg.get("library", overview)
    reg.post("library/job", job_state)
    reg.post("library/cancel", cancel, audited=True)
    reg.post("library/roots", roots, audited=True)
    reg.post("library/ledger-accept", ledger_accept, audited=True)
    reg.job("library/scan", scan, validate=scan_validate, deadline_s=600, rollback=scan_rollback)
    reg.job("library/verify", verify, validate=verify_validate, deadline_s=12 * 3600)
