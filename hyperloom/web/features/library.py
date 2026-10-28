"""Model library (F36): model units per machine.

GET library -> hyperloom/ui-library-v1: every machine with its model roots (config assets and prefs
    ``library/roots``), its last scan, verification results and library jobs kept from earlier releases, and the
    RTX big-job ledger guard state. It reads the portal store only: no node call, no file named by the client,
    nothing created. Copies between machines and model verification are not part of this release; the document
    says so (``copy``, ``verify_job``) and there is no route that starts either.
POST library/roots {machine, roots} (audited): the extra model roots of one machine.
POST library/ledger-accept {} (audited): accept the current RTX big-job ledger as the guard's baseline.
Job: library/scan {machine?} (a bounded, read-only inventory; it takes no GPU turn). The CLI family
    ``hyperloom models`` runs the same functions.
"""

import re
import threading
import time

from hyperloom import downloads, telemetry


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
SCAN_STALE_S = 600
COPY = {"available": False, "why": "copies between machines are not available in this release"}
VERIFY = {"available": False, "why": downloads.VERIFY_HELD}
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
                        ledger=_ledger(api, store).describe(), why=why, copy=COPY, verify_job=VERIFY)


# --- synchronous POSTs -------------------------------------------------------------------------------------------

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


def register(reg):
    reg.get("library", overview)
    reg.post("library/roots", roots, audited=True)
    reg.post("library/ledger-accept", ledger_accept, audited=True)
    reg.job("library/scan", scan, validate=scan_validate, deadline_s=600, rollback=scan_rollback)
