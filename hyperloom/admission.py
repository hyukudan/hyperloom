"""Lab-wide admission of benchmark-class runs against deploys and stops (contract C11).

One small SQLite database under the configuration's run directory is shared by every portal process and every
CLI invocation on the control machine. A hold names its class, the resource ids it uses (machine ids, devices,
``router``; ``*`` = everything), its owner process and a lease that the owner renews. Rules:

- ``bench`` (benchmarks and demos): refused while any ``core`` (deploy, start, install, probe) or ``stop``
  hold exists, while another bench overlaps its resources, or when ``BENCH_CAP`` benches run lab-wide.
- ``core``: refused while a bench overlaps its resources.
- ``stop``: never refused; registering it marks every overlapping bench cancelled (the bench sees it on its
  next ``cancelled()`` check and cleans up) and it keeps its hold until the stop finishes, so no bench starts
  meanwhile.

A cancelled bench keeps owning its resources (and counting against the cap) until its owner releases the hold
after its cleanup; the owner keeps renewing it until then.

A hold whose lease expired (owner gone or stopped renewing, e.g. an abandoned portal tab) is dropped on the next
decision. Decisions run inside ``BEGIN IMMEDIATE`` so two processes cannot both admit overlapping work.
"""

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


BENCH_CAP = 2
KINDS = ("bench", "core", "stop")
DEFAULT_LEASE_S = 120.0


class Refused(PermissionError):
    pass


def _overlap(a, b):
    return bool(set(a) & set(b)) or "*" in a or "*" in b


def _alive(pid):
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, AttributeError):
        return False


class Admission:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("CREATE TABLE IF NOT EXISTS holds (id TEXT PRIMARY KEY, kind TEXT NOT NULL, resources TEXT NOT NULL,"
                         " label TEXT, pid INTEGER NOT NULL, started REAL NOT NULL, expires REAL NOT NULL,"
                         " cancelled TEXT)")

    def close(self):
        self._db.close()

    def _purge(self, now):
        for hid, pid, expires in self._db.execute("SELECT id, pid, expires FROM holds").fetchall():
            if expires < now or not _alive(pid):
                self._db.execute("DELETE FROM holds WHERE id=?", (hid,))

    def _holds(self):
        return [{"id": h, "kind": k, "resources": json.loads(r), "label": label, "pid": pid, "started": s,
                 "expires": e, "cancelled": c}
                for h, k, r, label, pid, s, e, c in self._db.execute(
                    "SELECT id, kind, resources, label, pid, started, expires, cancelled FROM holds ORDER BY started")]

    def _acquire_locked(self, kind, resources, label="", lease_s=DEFAULT_LEASE_S, hold_id=None):
        """Admit a hold or raise Refused; returns its id. A stop cancels overlapping benches."""
        if kind not in KINDS:
            raise ValueError("unknown admission class " + str(kind))
        resources = sorted({str(r) for r in resources}) or ["*"]
        hold_id = hold_id or uuid.uuid4().hex
        now = self.clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._purge(now)
            holds = self._holds()
            if kind == "bench":
                blockers = [h for h in holds if h["kind"] in ("core", "stop")]
                if blockers:
                    raise Refused(f"a {'stop' if blockers[0]['kind'] == 'stop' else 'deploy, start or install'} is running"
                                  f" ({blockers[0]['label'] or blockers[0]['id']})")
                benches = [h for h in holds if h["kind"] == "bench"]
                busy = [h for h in benches if _overlap(h["resources"], resources)]
                if busy:
                    raise Refused("the target's resources are held by " + (busy[0]["label"] or busy[0]["id"]))
                if len(benches) >= BENCH_CAP:
                    raise Refused(f"at most {BENCH_CAP} benchmark or demo runs at a time")
            elif kind == "core":
                busy = [h for h in holds if h["kind"] == "bench" and _overlap(h["resources"], resources)]
                if busy:
                    raise Refused("a benchmark or demo holds these resources (" + (busy[0]["label"] or busy[0]["id"])
                                  + "); cancel it first")
            else:
                for h in holds:
                    if h["kind"] == "bench" and not h["cancelled"] and _overlap(h["resources"], resources):
                        self._db.execute("UPDATE holds SET cancelled=? WHERE id=?",
                                         ("stopped: " + (label or "a stop") + " overlaps its resources", h["id"]))
            self._db.execute("INSERT INTO holds(id, kind, resources, label, pid, started, expires, cancelled)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                             (hold_id, kind, json.dumps(resources), label, os.getpid(), now, now + lease_s))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return hold_id

    def _renew_locked(self, hold_id, lease_s=DEFAULT_LEASE_S):
        return self._db.execute("UPDATE holds SET expires=? WHERE id=?", (self.clock() + lease_s, hold_id)).rowcount == 1

    def _release_locked(self, hold_id):
        self._db.execute("DELETE FROM holds WHERE id=?", (hold_id,))

    def _cancel_locked(self, hold_id, reason):
        return self._db.execute("UPDATE holds SET cancelled=? WHERE id=? AND cancelled IS NULL", (reason, hold_id)).rowcount == 1

    def _cancelled_locked(self, hold_id):
        """The cancel reason of a hold, "expired" when it is gone, else None."""
        row = self._db.execute("SELECT cancelled, expires FROM holds WHERE id=?", (hold_id,)).fetchone()
        if row is None:
            return "admission expired"
        if row[1] < self.clock():
            return "abandoned: the owner stopped renewing it"
        return row[0]

    def _holds_locked(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._purge(self.clock())
            holds = self._holds()
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return holds

    def acquire(self, kind, resources, label="", lease_s=DEFAULT_LEASE_S, hold_id=None):
        """Admit a hold or raise Refused; returns its id. A stop cancels overlapping benches."""
        with self._lock:
            return self._acquire_locked(kind, resources, label, lease_s, hold_id)

    def renew(self, hold_id, lease_s=DEFAULT_LEASE_S):
        with self._lock:
            return self._renew_locked(hold_id, lease_s)

    def release(self, hold_id):
        with self._lock:
            return self._release_locked(hold_id)

    def cancel(self, hold_id, reason):
        with self._lock:
            return self._cancel_locked(hold_id, reason)

    def cancelled(self, hold_id):
        """The cancel reason of a hold, "admission expired"/"abandoned" when it lapsed, else None."""
        with self._lock:
            return self._cancelled_locked(hold_id)

    def holds(self):
        with self._lock:
            return self._holds_locked()


_opened = {}
_opened_lock = threading.Lock()


def for_config(config_path, clock=time.time):
    """The admission database of a configuration (``<config dir>/run/admission.sqlite3``)."""
    # HYPERLOOM_ADMISSION_DB overrides the location (the test suite uses ":memory:" so it never touches a real
    # configuration directory).
    path = os.environ.get("HYPERLOOM_ADMISSION_DB") or str(Path(config_path).parent / "run" / "admission.sqlite3")
    with _opened_lock:
        if path not in _opened:
            _opened[path] = Admission(path, clock=clock)
        return _opened[path]


def profile_resources(cluster, profile, orchestrate):
    """The resource ids a profile occupies: its nodes, the machine of its prefill service (when it has one) and the
    router, as configured, plus every machine its saved running state names (a running profile keeps the placement
    it was launched with, also when the configuration changed since). Raises when the profile is unknown or its
    saved state is unreadable (callers then hold everything)."""
    machines = [str(m) for m in orchestrate._profile(cluster, profile)[2]]
    service = (cluster.profiles.get(profile) or {}).get("prefill_service")
    if isinstance(service, dict) and service.get("machine"):
        machines.append(str(service["machine"]))
    state = Path(orchestrate._state_path(profile))
    if state.exists():
        saved = json.loads(state.read_text(encoding="utf-8"))
        machines += [str(m) for m in (saved.get("machines") or {})]
    return {*machines, "router"}


def read_holds(config_path, database=None, clock=time.time):
    """The live holds, read without writing anything (for GET documents): expired or ownerless records are skipped,
    never purged; no database file or directory is created. ``database``: an Admission this process already opened
    (else the one opened for this configuration, else the file read-only). Raises sqlite3.Error when unreadable."""
    path = os.environ.get("HYPERLOOM_ADMISSION_DB") or str(Path(config_path).parent / "run" / "admission.sqlite3")
    if database is None:
        with _opened_lock:
            database = _opened.get(path)
    if database is not None:
        with database._lock:
            holds = database._holds()
    elif path == ":memory:" or not Path(path).is_file():
        return []
    else:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            holds = [{"id": h, "kind": k, "resources": json.loads(r), "label": label, "pid": pid, "started": s,
                      "expires": e, "cancelled": c}
                     for h, k, r, label, pid, s, e, c in connection.execute(
                         "SELECT id, kind, resources, label, pid, started, expires, cancelled FROM holds"
                         " ORDER BY started")]
        finally:
            connection.close()
    now = (database.clock if database is not None else clock)()
    return [h for h in holds if h["expires"] >= now and _alive(h["pid"])]
