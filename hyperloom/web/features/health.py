"""Health findings (F20, contract C10): explainable rules over the shared series, collector outcomes and core
documents, with holds before firing and clearing and state that survives portal restarts.

A rule returns observations with a verdict: True (positive evidence the condition holds), False (positive evidence it
does not) or None (unknown: a missing, stale or untyped input). Unknown never fires and never clears a finding; a hold
counts only time covered by consecutive observations of the same verdict. Findings resolve on evidence, when their rule
is disabled, or when their machine or profile is no longer configured; never because data went missing.

Documents: ``health`` (active and pending findings), ``health/rules`` (rules, parameters, availability),
``health/summary`` (counts, the exceptions strip and the throttle banner for the portal shell). POST
``health/rules/set`` changes a rule's enablement, holds or parameters (audited). The ``health`` collector evaluates
every 10 s in the one process that holds the evaluator lease; every state change, its activity-log event and the alert
deliveries it implies commit in one guarded transaction.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from hyperloom import telemetry
from hyperloom.telemetry.store import MAX_EVENTS


STATE, CONFIG, META = "health/state", "health/config", "health/meta"
LEASES = "events/leases"
SEVERITIES = ("info", "warning", "critical")
RANK = {name: index for index, name in enumerate(SEVERITIES)}
INTERVAL = 10.0
LEASE_TTL = 120.0
GAP_S = max(3 * INTERVAL, 30.0)
MAX_FINDINGS = 500
PENDING_EXPIRY_S = 3600.0
GIB = 1 << 30
LABEL = re.compile(r"[A-Za-z0-9._:@+-]{1,64}")
PROFILE_RULES = ("engine-down", "replica-down", "route-state-stale")
DOWN_STATES = frozenset(("down", "unhealthy", "failed", "error", "unreachable"))
SNAPSHOT_MAX_AGE_S = 30.0
UNAVAILABLE = "health evaluation not running in this portal process (telemetry store not open)"


# -- store adapter: the only SQL this package runs on the store's own tables ---------------------------------------

def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


def upsert_doc(namespace, key, value, ts):
    return ("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
            "DO UPDATE SET ts=excluded.ts, value=excluded.value", (namespace, str(key), float(ts), _json(value)))


def delete_doc(namespace, key):
    return ("DELETE FROM docs WHERE namespace=? AND key=?", (namespace, str(key)))


def insert_event(ts, category, severity, message, machine=None, data=None):
    """The same row Store.event writes; follow it with RETENTION in the same transaction."""
    if severity not in SEVERITIES or not category or not isinstance(message, str):
        raise ValueError("invalid event")
    payload = None if data is None else json.dumps(data, ensure_ascii=True, allow_nan=False, default=str)
    return ("INSERT INTO events(ts, category, severity, machine, message, data) VALUES (?, ?, ?, ?, ?, ?)",
            (float(ts), category, severity, machine, message[:2000], payload))


RETENTION = ("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,))

_TABLES = """
CREATE TABLE IF NOT EXISTS health_lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL, generation INTEGER NOT NULL,
                                         until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS health_guard (x INTEGER CHECK (x = 0));
"""
_owners = {}
_owners_lock = threading.Lock()


def prepare(store):
    if not getattr(store, "_hyperloom_health_tables", False):
        store.ensure_table(_TABLES)
        store._hyperloom_health_tables = True


def owner_of(api):
    """This portal instance's lease owner id (a process may hold several API objects in tests)."""
    with _owners_lock:
        return _owners.setdefault(id(api), f"{os.getpid()}-{uuid.uuid4().hex[:12]}")


def acquire(store, owner, now):
    """Take or renew the evaluator lease; the generation when held, else None."""
    prepare(store)
    store.execute(
        "INSERT INTO health_lease(name, owner, generation, until) VALUES ('evaluator', ?, 1, ?) "
        "ON CONFLICT(name) DO UPDATE SET generation=CASE WHEN health_lease.owner=excluded.owner "
        "THEN health_lease.generation ELSE health_lease.generation + 1 END, owner=excluded.owner, until=excluded.until "
        "WHERE health_lease.owner=excluded.owner OR health_lease.until < ?", (owner, now + LEASE_TTL, now))
    return held(store, owner, now)


def held(store, owner, now):
    prepare(store)
    rows = store.execute("SELECT owner, generation, until FROM health_lease WHERE name='evaluator'")
    if rows and rows[0][0] == owner and rows[0][2] >= now:
        return rows[0][1]
    return None


def guard(owner, generation, now):
    """Statements that abort the transaction unless this owner still holds this lease generation (and renew it)."""
    return [("INSERT INTO health_guard(x) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM health_lease "
             "WHERE name='evaluator' AND owner=? AND generation=?)", (owner, generation)),
            ("UPDATE health_lease SET until=? WHERE name='evaluator' AND owner=? AND generation=?",
             (now + LEASE_TTL, owner, generation))]


def unchanged(namespace, key, text):
    """A statement that aborts the transaction unless the stored document text is still ``text`` (None: absent)."""
    return ("INSERT INTO health_guard(x) SELECT 1 WHERE (SELECT value FROM docs WHERE namespace=? AND key=?) IS NOT ?",
            (namespace, str(key), text))


def _version_sql(namespace):
    return ("(SELECT group_concat(entry, ',') FROM (SELECT key || '@' || ts AS entry FROM docs WHERE namespace=? "
            "ORDER BY key))")


def version(store, namespace):
    """A revision of a whole document namespace (keys and update times)."""
    return store.execute("SELECT " + _version_sql(namespace), (namespace,))[0][0]


def same_version(namespace, value):
    """A statement that aborts the transaction when the namespace changed since ``version`` returned ``value``."""
    return ("INSERT INTO health_guard(x) SELECT 1 WHERE " + _version_sql(namespace) + " IS NOT ?", (namespace, value))


def stored_text(store, namespace, key):
    rows = store.execute("SELECT value FROM docs WHERE namespace=? AND key=?", (namespace, str(key)))
    return rows[0][0] if rows else None


def commit(store, statements):
    store.transaction(statements)


# -- rules ----------------------------------------------------------------------------------------------------------

@dataclass
class Obs:
    subject: str
    verdict: object            # True, False or None
    severity: str = None
    detail: str = ""
    machine: str = None
    profile: str = None
    note: str = None
    facts: dict = field(default_factory=dict)


@dataclass
class Rule:
    id: str
    title: str
    description: str
    severity: str
    inputs: tuple
    hint: str
    evaluate: object
    params: dict = field(default_factory=dict)      # name -> (default, low, high, unit)
    hold_s: float = 90.0
    clear_s: float = 30.0
    gap_s: object = None                             # continuity gap; None = GAP_S; callable(params) -> seconds


def _clean(value):
    return value if isinstance(value, str) and LABEL.fullmatch(value) else None


def _where(machine, device=None):
    return machine + (" " + device if device else "")


def _subject(*parts):
    return "/".join(part for part in parts if part)


class Context:
    """What one evaluation reads; every source is read at most once and its availability recorded."""

    def __init__(self, api, store, now, owner):
        self.api, self.store, self.now, self.owner = api, store, now, owner
        self.sources = {}
        self.writes = []           # statements committed with the evaluation (e.g. persisted check results)
        self._memo = {}

    def _once(self, name, read):
        if name not in self._memo:
            try:
                self._memo[name] = read()
                self.sources.setdefault(name, "ok")
            except Exception as exc:     # a failing source makes its rules unknown, never the evaluation fail
                self._memo[name] = None
                self.sources[name] = "unavailable · " + type(exc).__name__
        return self._memo[name]

    def latest(self, name):
        """Fresh samples (within 3 described intervals) of a series; None when it was never recorded."""
        def read():
            descriptor = self.store.descriptor(name)
            if descriptor is None:
                self.sources[name] = "no samples recorded yet"
                return None
            interval = descriptor.get("interval_s") or INTERVAL
            rows = self.store.latest(name, max_age=3 * interval)
            if not rows:
                self.sources[name] = "no fresh samples (stale source)"
            return rows
        return self._once("series:" + name, read)

    def legend(self, name):
        descriptor = self.store.descriptor(name) or {}
        return descriptor.get("legend") or {}

    def cluster(self):
        return self._once("configuration", self.api.cluster)

    def machines(self):
        cluster = self.cluster()
        if cluster is None:
            return None
        machines = cluster.machines
        return dict(machines) if isinstance(machines, dict) else {m.id: m for m in machines}

    def profiles(self):
        cluster = self.cluster()
        return None if cluster is None else dict(cluster.profiles)

    def snapshot(self):
        """The core documents the portal already holds (cache only: never a refresh, so never a node, router or
        engine request), or None when absent or older than SNAPSHOT_MAX_AGE_S."""
        def read():
            cached = getattr(self.api, "_cached", None)
            devices = cached.get("devices") if isinstance(cached, dict) else None
            at = devices.get("server_time") if isinstance(devices, dict) else None
            if type(at) not in (int, float) or self.now - at > SNAPSHOT_MAX_AGE_S:
                self.sources["snapshot"] = "core documents not refreshed recently (no portal page open)"
                return None
            return cached
        return self._once("snapshot", read)

    def stop_resources(self):
        """Resources held by live stops from any process (CLI included), or None when unreadable."""
        def read():
            from hyperloom import admission
            database = getattr(self.api, "admission_db", None) or admission.for_config(self.api.config_path)
            return [set(hold["resources"]) for hold in database.holds() if hold.get("kind") == "stop"]
        return self._once("admission", read)

    def profile_machines(self, profile):
        declared = (self.profiles() or {}).get(profile)
        if not isinstance(declared, dict):
            return set()
        nodes = declared.get("nodes") or [declared[key] for key in ("rank0", "rank1") if key in declared]
        return {str(node) for node in nodes if isinstance(node, str)}

    def stopping(self, profile, machine=None):
        """True when a live stop covers this profile (planned stop), None when the holds are unreadable."""
        holds = self.stop_resources()
        if holds is None:
            return None
        mine = self.profile_machines(profile) | ({machine} if machine else set()) | {"router"}
        return any("*" in hold or hold & mine for hold in holds)

    def feature(self, name):
        """A registered feature's GET document (e.g. another package's), or None with the reason."""
        def read():
            from hyperloom.web import features
            handler, rest = features.registry().find_get(name)
            if handler is None or rest is not None:
                self.sources["feature:" + name] = "needs the " + name + " document (feature not loaded)"
                return None
            return self.api.read(name, {})
        return self._once("feature:" + name, read)


MOUNT = re.compile(r"[A-Za-z0-9._:@+/ -]{1,128}")


def _label(key, value):
    if key == "mount":
        return value if isinstance(value, str) and MOUNT.fullmatch(value) else None
    return _clean(value)


def _keyed(rows, *keys):
    out = {}
    for row in rows or []:
        labels = row["labels"]
        values = tuple(_label(key, labels.get(key)) if labels.get(key) is not None else "" for key in keys)
        if values[0] and None not in values:
            out[values] = row
    return out


def _gpu_hot(ctx, p):
    rows = ctx.latest("node_gpu_temp_celsius")
    if rows is None:
        return None
    hottest = {}
    for (machine, device, sensor), row in _keyed(rows, "machine", "device", "sensor").items():
        if (machine, device) not in hottest or row["value"] > hottest[(machine, device)][0]:
            hottest[(machine, device)] = (row["value"], sensor)
    out = []
    for (machine, device), (value, sensor) in sorted(hottest.items()):
        severity = "critical" if value >= p["crit_c"] else "warning" if value >= p["warn_c"] else None
        out.append(Obs(_subject(machine, device), severity is not None, severity,
                       f"{_where(machine, device)} at {value:.0f} °C" + (f" ({sensor})" if sensor else ""), machine))
    return out


def _gpu_low_clock(ctx, p):
    busy, clock = ctx.latest("node_gpu_busy_percent"), ctx.latest("node_gpu_clock_mhz")
    if busy is None or clock is None:
        return None
    busy, clock = _keyed(busy, "machine", "device"), _keyed(clock, "machine", "device")
    out = []
    for key in sorted(set(busy) & set(clock)):
        b, c = busy[key]["value"], clock[key]["value"]
        out.append(Obs(_subject(*key), b >= p["busy_percent"] and c < p["min_clock_mhz"], "warning",
                       f"{_where(*key)} at {c:.0f} MHz while {b:.0f} % busy", key[0]))
    return out


def _gpu_throttled(ctx, p):
    rows = ctx.latest("node_gpu_throttled")
    if rows is None:
        return None
    legend = ctx.legend("node_gpu_throttled")
    out = []
    for (machine, device), row in sorted(_keyed(rows, "machine", "device").items()):
        code = int(row["value"])
        reason = legend.get(str(code))
        reason = reason if isinstance(reason, str) and len(reason) <= 64 else None
        out.append(Obs(_subject(machine, device), code != 0, "critical",
                       f"{_where(machine, device)} reports " + (reason or f"throttle state {code}"), machine,
                       facts={"reason": reason, "code": code}))
    return out


DEFAULT_THRESHOLDS = {"unified": {"amber": 8 * GIB, "red": 4 * GIB}, "rtx": {"amber": 2 * GIB, "red": 1 * GIB},
                      "commit": {"amber": 8 * GIB, "red": None}}


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def parse_thresholds(raw):
    """Headroom thresholds (bytes) from the fleet package's settings; tolerant of *_bytes / *_gib keys."""
    out = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    if not isinstance(raw, dict):
        return out
    raw = raw.get("thresholds", raw)
    for domain in out:
        given = raw.get(domain) if isinstance(raw, dict) else None
        if not isinstance(given, dict):
            continue
        for level in ("amber", "red"):
            for suffix, scale in (("_bytes", 1), ("", 1), ("_gib", GIB)):
                value = _number(given.get(level + suffix))
                if value is not None:
                    out[domain][level] = value * scale
                    break
    return out


def _thresholds(ctx):
    def read():
        document = ctx.feature("fleet/thresholds")
        if isinstance(document, dict):
            return parse_thresholds(document), "fleet thresholds document"
        stored = ctx.store.get("prefs", "fleet/thresholds")
        if isinstance(stored, dict):
            return parse_thresholds(stored), "fleet thresholds setting"
        return parse_thresholds(None), "defaults (F2)"
    value = ctx._once("thresholds", read)
    return value or (parse_thresholds(None), "defaults (F2)")


def _memory(ctx, p):
    machines = ctx.machines()
    if machines is None:
        return None
    limits, source = _thresholds(ctx)
    ctx.sources["thresholds"] = source
    system = lambda m: str(getattr(machines.get(m), "os", "")).lower()
    out = []

    def judge(subject, machine, domain, free, what):
        amber, red = limits[domain]["amber"], limits[domain]["red"]
        severity = "critical" if red is not None and free < red else "warning" if amber is not None and free < amber else None
        out.append(Obs(subject, severity is not None, severity, f"{what} {free / GIB:.1f} GiB free", machine))

    seen = False
    for name, domain, wanted in (("node_mem_available_bytes", "unified", "linux"), ("node_commit_available_bytes", "commit", "windows")):
        rows = ctx.latest(name)
        seen = seen or rows is not None
        for (machine,), row in sorted(_keyed(rows, "machine").items()):
            if system(machine) == wanted:
                judge(_subject(machine, "commit" if domain == "commit" else "system"), machine, domain, row["value"],
                      f"{machine} {'commit headroom' if domain == 'commit' else 'system memory'}")
    total, used = ctx.latest("node_gpu_mem_total_bytes"), ctx.latest("node_gpu_mem_used_bytes")
    if total is not None and used is not None:
        seen = True
        total, used = _keyed(total, "machine", "device", "domain"), _keyed(used, "machine", "device", "domain")
        devices = sorted({(m, d) for m, d, _ in set(total) & set(used)})
        for machine, device in devices:
            if device in _excluded(machines.get(machine)):
                continue
            free = lambda domain: total[(machine, device, domain)]["value"] - used[(machine, device, domain)]["value"] \
                if (machine, device, domain) in total and (machine, device, domain) in used else None
            if system(machine) == "windows" and free("vram") is not None:
                judge(_subject(machine, device, "vram"), machine, "rtx", max(0.0, free("vram")), f"{_where(machine, device)} VRAM")
            elif system(machine) == "linux" and free("vram") is not None and free("gtt") is not None:
                # GPU-visible memory of a unified node: VRAM + GTT, its own domain (never merged with system memory).
                judge(_subject(machine, device, "gpu-visible"), machine, "unified", max(0.0, free("vram") + free("gtt")),
                      f"{_where(machine, device)} GPU-visible memory")
    return out if seen else None


def _excluded(machine):
    """Device ids the configuration excludes by policy (never judged)."""
    out = set()
    for device in getattr(machine, "devices", None) or ():
        if isinstance(device, dict) and device.get("excluded"):
            out.update(str(device[key]) for key in ("id", "uuid", "device") if device.get(key) is not None)
    return out


def _disk(ctx, p):
    used, total = ctx.latest("node_disk_used_bytes"), ctx.latest("node_disk_total_bytes")
    if used is None or total is None:
        return None
    used, total = _keyed(used, "machine", "mount"), _keyed(total, "machine", "mount")
    out = []
    for key in sorted(set(used) & set(total)):
        if total[key]["value"] <= 0:
            continue
        percent = 100.0 * used[key]["value"] / total[key]["value"]
        severity = "critical" if percent >= p["critical_percent"] else "warning" if percent >= p["percent"] else None
        out.append(Obs(_subject(key[0], "disk:" + key[1]), severity is not None, severity,
                       f"{key[0]} {key[1]} {percent:.0f} % used", key[0]))
    return out


def _unreachable(ctx, p):
    rows = ctx.latest("node_up")
    if rows is None:
        return None
    return [Obs(machine, int(row["value"]) == 0, "critical", f"{machine} does not answer the node tool", machine)
            for (machine,), row in sorted(_keyed(rows, "machine").items())]


_first_seen = {}


def _collector_stale(ctx, p):
    status = telemetry.status(ctx.api)
    if status is None:
        ctx.sources["collectors"] = "samplers not running in this process"
        return None
    out = []
    for name, state in sorted(status.items()):
        if name == "health" or not LABEL.fullmatch(name):
            continue
        interval = float(state.get("interval_s") or INTERVAL)
        first = _first_seen.setdefault((ctx.owner, name), ctx.now)
        last_ok = state.get("last_ok")
        failures = int(state.get("failures") or 0)
        error = state.get("error")
        kind = error.split(":", 1)[0] if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", error.split(":", 1)[0]) else None
        if last_ok is not None and ctx.now - last_ok <= 3 * interval:
            out.append(Obs(name, False, "warning", f"collector {name} succeeded {ctx.now - last_ok:.0f} s ago"))
            continue
        if ctx.now - (last_ok if last_ok is not None else first) <= 3 * interval:
            out.append(Obs(name, None))           # startup grace: unknown, never evidence of recovery
            continue
        since = f"no success for {ctx.now - last_ok:.0f} s" if last_ok is not None else "no success since the portal started"
        out.append(Obs(name, True, "warning", f"collector {name}: {since} (every {interval:g} s; {failures} failures"
                       + (f", last {kind}" if kind else "") + ")"))
    out.extend(_producer_sources(ctx))
    return out


def _source_obs(ctx, subject, machine, text, last_ok, attempted, interval):
    if last_ok is not None and ctx.now - last_ok <= 3 * interval:
        return Obs(subject, False, "warning", f"{text} current", machine)
    if attempted is not None and ctx.now - attempted <= 3 * interval:
        age = f"no success for {ctx.now - last_ok:.0f} s" if last_ok is not None else "never succeeded"
        return Obs(subject, True, "warning", f"{text}: {age}", machine)
    return Obs(subject, None)        # not attempted recently: the collector's own staleness covers it


def _usb4_sources(ctx):
    """USB4 per-machine sources: docs usb4/node[machine].sources[name] = {last_ok, last_attempt, interval_s}."""
    out = []
    for machine, _, doc in ctx.store.items("usb4/node"):
        sources = doc.get("sources") if isinstance(doc, dict) else None
        if not _clean(machine) or not isinstance(sources, dict):
            continue
        for name, source in sorted(sources.items()):
            if _clean(name) and isinstance(source, dict):
                out.append(_source_obs(ctx, _subject(machine, "usb4-" + name), machine, f"USB4 source {name} on {machine}",
                                       _number(source.get("last_ok")), _number(source.get("last_attempt")),
                                       _number(source.get("interval_s")) or INTERVAL))
    return out


def _fleet_sources(ctx):
    """Fleet host reads: docs fleet/host[machine] = {ok, ts, last_ok, failed_at}. The fleet collector succeeds even
    when one machine's read fails; a machine it marks down (node_up = 0) is judged by machine-unreachable instead."""
    descriptor = ctx.store.descriptor("node_up") or {}
    interval = _number(descriptor.get("interval_s")) or 5.0
    down = {key[0] for key, row in _keyed(ctx.latest("node_up"), "machine").items() if int(row["value"]) == 0}
    out = []
    for machine, _, doc in ctx.store.items("fleet/host"):
        if not _clean(machine) or not isinstance(doc, dict):
            continue
        subject = _subject(machine, "fleet-host")
        if machine in down:
            out.append(Obs(subject, None))
            continue
        attempts = [t for t in (_number(doc.get("ts")), _number(doc.get("failed_at"))) if t is not None]
        out.append(_source_obs(ctx, subject, machine, f"host readings of {machine}", _number(doc.get("last_ok")),
                               max(attempts) if attempts else None, _number(doc.get("interval_s")) or interval))
    return out


def _producer_sources(ctx):
    """Per-machine sources the producers report themselves (a collector can succeed while one of its sources fails):
    a source the producer keeps attempting without success for 3 of its intervals is stale."""
    return _usb4_sources(ctx) + _fleet_sources(ctx)


def _engine_down(ctx, p):
    rows = ctx.latest("engine_up")
    if rows is None:
        return None
    machines = ctx.machines() or {}
    out = []
    for (profile, replica), row in sorted(_keyed(rows, "profile", "replica").items()):
        machine = _clean(row["labels"].get("machine")) or (replica if replica in machines else None)
        subject, what = _subject(profile, replica), f"engine {replica} of {profile}" if replica else f"engine of {profile}"
        if int(row["value"]) != 0:
            out.append(Obs(subject, False, "critical", what + " is up", machine, profile))
            continue
        planned = ctx.stopping(profile, machine)
        if planned:
            out.append(Obs(subject, False, "critical", what + " is draining", machine, profile, note="draining · planned stop"))
        else:      # unreadable stop holds: unknown, never a finding that a planned stop would explain
            out.append(Obs(subject, None if planned is None else True, "critical", what + " is down", machine, profile))
    return out


def _replica_down(ctx, p):
    snapshot = ctx.snapshot()
    if snapshot is None:
        return None
    routers = (snapshot.get("stats") or {}).get("routers")
    if not isinstance(routers, list):
        ctx.sources["routers"] = "router status unavailable"
        return None
    out = []
    for router in routers:
        profile = _clean(router.get("profile")) if isinstance(router, dict) else None
        for replica in (router.get("replicas") or []) if profile else []:
            if not isinstance(replica, dict):
                continue
            rid, node, state = _clean(str(replica.get("id"))), _clean(replica.get("node")), replica.get("state")
            if not rid:
                continue
            verdict = True if state in DOWN_STATES else False if state == "up" else None
            note = None
            if verdict:
                planned = ctx.stopping(profile, node)
                verdict = False if planned else None if planned is None else True
                note = "draining · planned stop" if planned else None
            out.append(Obs(_subject(profile, rid), verdict, "warning",
                           f"router replica {rid} of {profile} is {state if isinstance(state, str) and LABEL.fullmatch(state) else 'unknown'}",
                           node, profile, note=note))
    return out


class Budget:
    """An aggregate byte budget for the local files one evaluation reads."""

    def __init__(self, total):
        self.left = total

    def read_json(self, path, limit, root):
        """A regular, non-link file whose resolved path is inside ``root``, at most ``limit`` bytes."""
        path, root = Path(path), Path(root).resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise PermissionError("outside the runtime directory or a link")
        if not path.is_file():
            raise FileNotFoundError(path.name)
        cap = min(limit, self.left)
        with path.open("rb") as stream:
            data = stream.read(cap + 1)
        if len(data) > cap:
            raise ValueError("file exceeds its read budget")
        self.left -= len(data)
        return json.loads(data.decode("utf-8"))


ROUTE_PROFILES, ROUTE_BYTES = 32, 4 << 20


def _route_state(ctx, p):
    profiles = ctx.profiles()
    if profiles is None:
        return None
    from hyperloom import orchestrate, secrets
    root, budget, out = secrets.runtime_dir(), Budget(ROUTE_BYTES), []
    for profile in sorted(name for name in profiles if _clean(name))[:ROUTE_PROFILES]:
        try:
            record = budget.read_json(orchestrate._state_path(profile), 1 << 20, root)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            ctx.sources["route-state:" + profile] = "launch record unreadable"
            continue
        table = ((record.get("recipe") or {}).get("route_table") or {}) if isinstance(record, dict) else {}
        state_file = table.get("state_file") if isinstance(table, dict) else None
        if not isinstance(state_file, str) or not state_file:
            continue
        try:
            at = budget.read_json(state_file, 64 * 1024, root).get("at")
        except (OSError, ValueError, AttributeError):
            out.append(Obs(profile, None, profile=profile))
            ctx.sources["route-state:" + profile] = "route state file unreadable"
            continue
        if type(at) not in (int, float) or not math.isfinite(at):
            out.append(Obs(profile, None, profile=profile))
            continue
        age = ctx.now - at
        out.append(Obs(profile, age > p["stale_s"], "warning", f"route state of {profile} is {age:.0f} s old", None, profile))
    return out


def _usb4_rate(ctx, p):
    rows = ctx.latest("usb4_link_gbps")
    if rows is None:
        return None
    rated = {telemetry.canonical_labels(row["labels"]): row["value"] for row in ctx.latest("usb4_link_rated_gbps") or []}
    out = []
    for (machine, router, direction), row in sorted(_keyed(rows, "machine", "router", "direction").items()):
        full = rated.get(telemetry.canonical_labels(row["labels"]), p["rated_gbps"])
        out.append(Obs(_subject(machine, "usb4", router, direction), row["value"] < full * 0.99, "warning",
                       f"{machine} USB4 {router} {direction} trained at {row['value']:g} Gb/s of {full:g}", machine))
    return out


JOURNAL_NS, JOURNAL_MAX_AGE_S, EVIDENCE = "usb4/journal", 180.0, "health/usb4-evidence"


def _evidence_events(ctx, machine, doc, boot, counts):
    """Activity-log events for new kernel evidence, committed with a per-machine checkpoint {boot, generation,
    cursor, totals}. Within one boot and one scan generation, an increase after the cursor moved is new evidence; a
    new boot's totals are all evidence of that boot; a replacement scan of the same boot (a new generation) only
    rebaselines. The first sight of a machine records its checkpoint silently (the finding announces it)."""
    generation = doc.get("generation") if type(doc.get("generation")) is int else None
    cursor = doc.get("cursor") if isinstance(doc.get("cursor"), str) and len(doc["cursor"]) <= 512 else None
    checkpoint = ctx.store.get(EVIDENCE, machine)
    mark = {"boot": boot, "generation": generation, "cursor": cursor, "totals": counts}
    if checkpoint == mark:
        return
    new = {}
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("totals"), dict):
        if checkpoint.get("boot") != boot:
            new, why = {kind: n for kind, n in counts.items() if n}, "in the new boot"
        elif generation is not None and checkpoint.get("generation") == generation and cursor != checkpoint.get("cursor"):
            before = checkpoint["totals"]
            new, why = {kind: n - int(before.get(kind) or 0) for kind, n in counts.items() if n > int(before.get(kind) or 0)}, "new"
        if new:
            text = ", ".join(f"{kind} {n}" for kind, n in sorted(new.items()))
            ctx.writes.append(insert_event(ctx.now, "usb4", "warning",
                                           f"{sum(new.values())} USB4 kernel evidence lines {why} on {machine} ({text})",
                                           machine, {"new": new, "generation": generation}))
    ctx.writes.append(upsert_doc(EVIDENCE, machine, mark, ctx.now))


def _usb4_evidence(ctx, p):
    """Evidence of the current boot from the USB4 package's per-machine journal state (it counts only lines after
    its stored per-boot cursor); a reboot without new evidence clears the finding."""
    journals = ctx.store.items(JOURNAL_NS)
    if not journals:
        ctx.sources["usb4/journal"] = "needs the USB4 collector's journal state"
        return None
    out = []
    for machine, _, doc in sorted(journals):
        machine = _clean(machine)
        if not machine or not isinstance(doc, dict):
            continue
        read_at, boot, totals = doc.get("read_at"), doc.get("boot_id"), doc.get("totals")
        fresh = type(read_at) in (int, float) and ctx.now - read_at <= JOURNAL_MAX_AGE_S
        counts = {kind: n for kind, n in totals.items() if _clean(kind) and type(n) is int and n >= 0} \
            if isinstance(totals, dict) else None
        if doc.get("status") != "ok" or not fresh or not isinstance(boot, str) or counts is None:
            out.append(Obs(_subject(machine, "usb4-evidence"), None))
            continue
        lines = sum(counts.values())
        kinds = ", ".join(f"{kind} {n}" for kind, n in sorted(counts.items()) if n)
        _evidence_events(ctx, machine, doc, boot[:64], counts)
        if not lines and doc.get("complete") is not True:
            out.append(Obs(_subject(machine, "usb4-evidence"), None))   # an incomplete scan never proves absence
            continue
        out.append(Obs(_subject(machine, "usb4-evidence"), lines > 0, "warning",
                       f"{machine}: {lines} USB4 kernel evidence lines this boot ({kinds})" if lines else
                       f"{machine}: no USB4 kernel evidence this boot", machine, facts={"boot": boot[:36]}))
    return out


def _turns(ctx, p):
    document = ctx.feature("turns")
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        return None
    out = []
    for node in document["nodes"]:
        machine = _clean(node.get("machine")) if isinstance(node, dict) else None
        if not machine or "holder" not in node:
            continue
        holder = node["holder"]
        if holder is None:
            out.append(Obs(_subject(machine, "turn"), False, "warning", f"{machine}: no GPU turn held", machine))
            continue
        if not isinstance(holder, dict):
            continue
        since = holder.get("since", holder.get("started_at"))
        cap = holder.get("cap_s")
        label = holder.get("label") if isinstance(holder.get("label"), str) and len(holder["label"]) <= 80 else "holder"
        if holder.get("past_cap") is True:
            verdict = True
        elif _number(since) is not None and _number(cap) is not None:
            verdict = ctx.now - since > cap
        else:
            verdict = None
        detail = f"{machine}: GPU turn {label}" + (f" held {(ctx.now - since) / 60:.0f} min of a {cap / 60:.0f} min cap"
                                                  if _number(since) is not None and _number(cap) else "")
        out.append(Obs(_subject(machine, "turn"), verdict, "warning", detail, machine))
    return out


OPS_NS, OPS_TOOL = "ops", "node_tool"
DIGEST = re.compile(r"[0-9a-f]{64}")
_expected = {"at": -math.inf, "value": None}


def _checkout_digest(now):
    """This checkout's node tool digest (recomputed at most once a minute)."""
    if now - _expected["at"] >= 60:
        from hyperloom import remote
        _expected.update(at=now, value=remote.tool_digest())
    return _expected["value"]


def _node_tool(ctx, p):
    """The deployed node tool digests the operations package records (store docs ops/node_tool: {machine: {checked_at,
    deployed, error}}; this package calls no node verb), compared with this checkout; a record older than max_age_s
    is unknown (the rule's continuity gap is max_age_s)."""
    known = ctx.store.get(OPS_NS, OPS_TOOL)
    if not isinstance(known, dict):
        ctx.sources["ops/node_tool"] = "needs the operations package's node tool records"
        return None
    machines = ctx.machines() or {}
    expected = _checkout_digest(ctx.now)
    out = []
    for mid, row in sorted(known.items()):
        if not _clean(mid) or mid not in machines or not isinstance(row, dict):
            continue
        checked, deployed = _number(row.get("checked_at")), row.get("deployed")
        if checked is None or ctx.now - checked > p["max_age_s"] or not isinstance(deployed, str) or not DIGEST.fullmatch(deployed):
            out.append(Obs(_subject(mid, "node-tool"), None))
            continue
        stale = deployed != expected
        out.append(Obs(_subject(mid, "node-tool"), stale, "info",
                       f"{mid} runs node tool {deployed[:12]} · this checkout {expected[:12]}" if stale else f"{mid} node tool current",
                       mid))
    return out


RULES = (
    Rule("gpu-hot", "GPU hot", "GPU temperature at or above the warning or critical threshold (hottest sensor).",
         "warning", ("node_gpu_temp_celsius",), "Check airflow and fan state; sustained heat throttles decode.", _gpu_hot,
         {"warn_c": (85.0, 40.0, 120.0, "celsius"), "crit_c": (90.0, 40.0, 120.0, "celsius")}),
    Rule("gpu-low-clock", "GPU clock low while busy", "A busy GPU running at a low clock.", "warning",
         ("node_gpu_busy_percent", "node_gpu_clock_mhz"), "Look for a power cap, thermal limit or power profile.",
         _gpu_low_clock, {"busy_percent": (80.0, 1.0, 100.0, "%"), "min_clock_mhz": (800.0, 50.0, 5000.0, "MHz")}),
    Rule("gpu-throttled", "GPU throttling", "The GPU reports an active throttle reason (RTX cards report them).",
         "critical", ("node_gpu_throttled",), "Throttling lowers throughput; check cooling and the power limit.",
         _gpu_throttled),
    Rule("memory-headroom", "Memory headroom low", "Free memory per accounting domain below the F2 thresholds.",
         "warning", ("node_mem_available_bytes", "node_commit_available_bytes", "node_gpu_mem_total_bytes",
                     "node_gpu_mem_used_bytes"), "Stop an engine or reduce context/slots before allocations fail.",
         _memory),
    Rule("disk-full", "Disk nearly full", "A mounted filesystem at or above the used-space threshold.", "warning",
         ("node_disk_used_bytes", "node_disk_total_bytes"), "Free space: old releases, logs or model copies.", _disk,
         {"percent": (90.0, 50.0, 100.0, "%"), "critical_percent": (97.0, 50.0, 100.0, "%")}),
    Rule("machine-unreachable", "Machine unreachable", "The node tool on a configured machine does not answer.",
         "critical", ("node_up",), "Check power, network and the SSH host key.", _unreachable),
    Rule("collector-stale", "Data source stale", "A collector, or a per-machine source it reports, has not succeeded "
         "within three of its intervals; its values are not judged meanwhile.", "warning",
         ("collectors", "usb4/node", "fleet/host"), "Open the metrics history page for the collector's last error; "
         "check the machine it reads.", _collector_stale),
    Rule("engine-down", "Engine down", "An engine of a running profile is down (a planned stop is expected, not a finding).",
         "critical", ("engine_up", "admission stop holds"), "Open the engine's logs; restart the profile if it failed.",
         _engine_down),
    Rule("replica-down", "Router replica down", "The router reports a replica in a down state.", "warning",
         ("stats.routers",), "The router sends no work to it; check that engine's health.", _replica_down),
    Rule("route-state-stale", "Route state stale", "The route state file of a recipe deployment is older than allowed; "
         "the router then refuses the routes that need it.", "warning", ("launch records",),
         "Check the router helper process of that deployment.", _route_state, {"stale_s": (30.0, 5.0, 3600.0, "s")}),
    Rule("usb4-below-rate", "USB4 link below full rate", "A USB4 link trained below its rated speed.", "warning",
         ("usb4_link_gbps", "usb4_link_rated_gbps"), "Reseat or replace the cable; a retrain shows in the link history.",
         _usb4_rate, {"rated_gbps": (40.0, 1.0, 200.0, "Gb/s")}),
    Rule("usb4-kernel-evidence", "New USB4 kernel evidence", "New kernel log lines about the USB4 links this boot "
         "(host interface reset, teardown pending, quarantine).", "warning", ("usb4/journal",),
         "Read the evidence lines on the USB4 page before the next run.", _usb4_evidence, hold_s=0.0),
    Rule("turn-over-cap", "GPU turn past its cap", "A GPU turn holder has held its turn longer than its cap.", "warning",
         ("turns",), "Ask the holder to finish; the queue waits behind it.", _turns),
    Rule("node-tool-stale", "Node tool out of date", "The deployed node tool differs from this checkout.", "info",
         ("ops/node_tool",), "Update the node tool from the operations page when no lease is live.", _node_tool,
         {"max_age_s": (1800.0, 60.0, 86400.0, "s")}, hold_s=0.0, clear_s=0.0, gap_s=lambda params: params["max_age_s"]),
)
BY_ID = {rule.id: rule for rule in RULES}


# -- configuration --------------------------------------------------------------------------------------------------

def effective(rule, override):
    override = override if isinstance(override, dict) else {}
    params = {name: spec[0] for name, spec in rule.params.items()}
    for name, value in (override.get("params") or {}).items():
        if name in rule.params and type(value) in (int, float):
            params[name] = float(value)
    return {"enabled": override.get("enabled", True) is not False,
            "hold_s": float(override.get("hold_s", rule.hold_s)), "clear_s": float(override.get("clear_s", rule.clear_s)),
            "params": params}


def configs(store):
    stored = {key: value for key, _, value in store.items(CONFIG)}
    return {rule.id: effective(rule, stored.get(rule.id)) for rule in RULES}


def _seconds(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3600:
        raise ValueError(name + " must be a number of seconds in 0..3600")
    return float(value)


def set_rule(store, arguments):
    """Validate and store a rule override (shared by the portal POST and the CLI); returns the effective rule."""
    unknown = set(arguments) - {"rule", "enabled", "hold_s", "clear_s", "params", "reset"}
    if unknown:
        raise ValueError("unknown arguments (allowed: rule, enabled, hold_s, clear_s, params, reset)")
    rule = BY_ID.get(arguments.get("rule")) if isinstance(arguments.get("rule"), str) else None
    if rule is None:
        raise LookupError("unknown rule")
    if "reset" in arguments and arguments["reset"] is not True:
        raise ValueError("reset must be true")
    if arguments.get("reset") is True:
        store.delete(CONFIG, rule.id)
        return effective(rule, None)
    override = dict(store.get(CONFIG, rule.id) or {})
    if "enabled" in arguments:
        if not isinstance(arguments["enabled"], bool):
            raise ValueError("enabled must be true or false")
        override["enabled"] = arguments["enabled"]
    for name in ("hold_s", "clear_s"):
        if name in arguments:
            override[name] = _seconds(arguments[name], name)
    if "params" in arguments:
        params = arguments["params"]
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        merged = dict(override.get("params") or {})
        for name, value in params.items():
            if name not in rule.params:
                raise ValueError(f"rule {rule.id} has no parameter of that name")
            _, low, high, unit = rule.params[name]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be in {low:g}..{high:g} {unit}")
            merged[name] = float(value)
        override["params"] = merged
    result = effective(rule, override)
    for low_name, high_name in (("warn_c", "crit_c"), ("percent", "critical_percent")):
        if low_name in result["params"] and result["params"][low_name] > result["params"][high_name]:
            raise ValueError(f"{low_name} must not exceed {high_name}")
    store.put(CONFIG, rule.id, override)
    return result


# -- state machine --------------------------------------------------------------------------------------------------

def _event(changes, now, severity, message, finding, change):
    """An activity-log event and the matching alert transition (appeared, escalated, de-escalated, cleared)."""
    changes["events"].append(insert_event(now, "health", severity, message, finding.get("machine"),
                                          {"finding": finding["id"], "rule": finding["rule"], "change": change}))
    changes["transitions"].append((change, dict(finding)))


def _resolve(changes, now, finding, reason):
    changes["removed"].add(finding["id"])
    changes["changed"].pop(finding["id"], None)
    if finding["state"] in ("firing", "clearing"):
        _event(changes, now, "info", f"Cleared · {finding['title']}: {finding['detail']}" + (f" ({reason})" if reason else ""),
               finding, "cleared")


def _gap(rule, settings, default):
    if callable(rule.gap_s):
        return max(default, float(rule.gap_s(settings["params"])))
    return default if rule.gap_s is None else float(rule.gap_s)


def _change(previous, severity):
    return "escalated" if RANK[severity] > RANK.get(previous, 0) else "de-escalated"


def advance(states, results, cfg, now, machines=None, profiles=None, gap_s=GAP_S, limit=MAX_FINDINGS):
    """One evaluation step. ``results`` maps rule id -> observations (None: source unavailable). Returns
    {changed: {id: state}, removed: set, events: [statements], overflow: int, states: {id: state after}};
    ``states`` is not modified."""
    states = {key: dict(value) for key, value in states.items()}
    changes = {"changed": {}, "removed": set(), "events": [], "transitions": [], "overflow": 0}
    for fid, finding in list(states.items()):
        rule = BY_ID.get(finding.get("rule"))
        reason = ("rule removed" if rule is None else "rule disabled" if not cfg[rule.id]["enabled"]
                  else "no longer configured" if machines is not None and finding.get("machine") and finding["machine"] not in machines
                  else "no longer configured" if profiles is not None and finding.get("profile") and finding["profile"] not in profiles
                  else None)
        if reason:
            _resolve(changes, now, finding, reason)
            del states[fid]
        elif finding["state"] == "pending" and now - (finding.get("last_true") or 0) > PENDING_EXPIRY_S:
            changes["removed"].add(fid)
            del states[fid]
    for rule in RULES:
        observations = results.get(rule.id)
        settings = cfg[rule.id]
        if not settings["enabled"] or observations is None:
            continue
        gap = _gap(rule, settings, gap_s)
        for obs in observations:
            if obs.verdict is None or not isinstance(obs.subject, str) or not obs.subject:
                continue
            fid = rule.id + ":" + obs.subject
            if fid in changes["removed"]:
                continue
            finding = states.get(fid)
            severity = obs.severity if obs.severity in SEVERITIES else rule.severity
            if obs.verdict:
                if finding is None:
                    if len(states) >= limit:
                        changes["overflow"] += 1
                        continue
                    finding = {"id": fid, "rule": rule.id, "subject": obs.subject, "title": rule.title, "hint": rule.hint,
                               "state": "pending", "first_true": now, "since": None, "false_since": None,
                               "last_false": None, "occurrence": None}
                    states[fid] = finding
                elif finding["state"] == "pending" and now - (finding.get("last_true") or -math.inf) > gap:
                    finding["first_true"] = now          # a gap in the evidence restarts the hold
                previous = finding.get("severity")
                finding.update(severity=severity, detail=obs.detail, machine=obs.machine, profile=obs.profile,
                               facts=obs.facts or {}, last_true=now, last_observed=now, note=obs.note)
                if finding["state"] == "pending":
                    if now - finding["first_true"] >= settings["hold_s"]:
                        finding.update(state="firing", since=now, occurrence=uuid.uuid4().hex[:16])
                        _event(changes, now, severity, f"{rule.title}: {obs.detail}", finding, "appeared")
                elif finding["state"] == "clearing":
                    finding.update(state="firing", false_since=None)
                    if previous != severity:
                        _event(changes, now, severity, f"{rule.title} {_change(previous, severity)}: {obs.detail}", finding,
                               _change(previous, severity))
                elif previous != severity:
                    _event(changes, now, severity, f"{rule.title} {_change(previous, severity)}: {obs.detail}", finding,
                           _change(previous, severity))
                changes["changed"][fid] = finding
            elif finding is not None:
                finding.update(last_observed=now, note=obs.note)
                if finding["state"] == "pending":
                    changes["removed"].add(fid)
                    del states[fid]
                    continue
                if finding["state"] == "firing" or now - (finding.get("last_false") or -math.inf) > gap:
                    finding.update(state="clearing", false_since=now)   # a gap in the evidence restarts the hold
                finding["last_false"] = now
                if now - finding["false_since"] >= settings["clear_s"]:
                    _resolve(changes, now, finding, None)
                    del states[fid]
                    continue
                changes["changed"][fid] = finding
    for fid in changes["removed"]:
        changes["changed"].pop(fid, None)
    changes["states"] = states
    return changes


# -- evaluation (the collector) -------------------------------------------------------------------------------------

_eval_lock = threading.Lock()


def _lease_events(ctx, previous):
    """Lease acquired/released events from the cached core devices document: only machines whose lease list is
    authoritative (a list, the machine not offline, no reason given for missing leases) are compared; the others keep
    their previous set. The first sight of a machine records its set silently. Returns (sets, statements)."""
    snapshot = ctx.snapshot()
    machines = ((snapshot or {}).get("devices") or {}).get("machines")
    profiles = ((snapshot or {}).get("profiles") or {}).get("profiles")
    previous = previous if isinstance(previous, dict) else {}
    if not isinstance(machines, list) or not isinstance(profiles, list):
        return previous, []
    # Authority comes from the lease status records themselves: a machine whose status failed in any profile (or a
    # failed status read) has no authoritative lease list this round, whatever its devices row shows.
    unknown, statuses = set(), [p.get("status") for p in profiles if isinstance(p, dict)]
    for status in statuses:
        if not isinstance(status, dict) or status.get("error"):
            return previous, []
        for mid, node in (status.get("nodes") or {}).items():
            if not isinstance(node, dict) or node.get("error") or not isinstance(node.get("leases", []), list):
                unknown.add(mid)
    current, events = dict(previous), []
    for machine in machines:
        mid = _clean(machine.get("id")) if isinstance(machine, dict) else None
        leases = machine.get("leases") if mid else None
        if (not mid or mid in unknown or not isinstance(leases, list) or machine.get("health") == "offline"
                or (machine.get("why") or {}).get("leases")):
            continue
        now_set = {}
        for lease in leases:
            lid = _clean(str(lease.get("lease"))) if isinstance(lease, dict) and lease.get("lease") is not None else None
            if lid:
                label = lease.get("label")
                now_set[lid] = label if isinstance(label, str) and len(label) <= 80 else ""
        before = previous.get(mid)
        current[mid] = now_set
        if not isinstance(before, dict):
            continue
        for lid in sorted(set(now_set) - set(before)):
            events.append(insert_event(ctx.now, "leases", "info", f"lease acquired on {mid} · {now_set[lid] or lid}", mid,
                                       {"lease": lid}))
        for lid in sorted(set(before) - set(now_set)):
            label = before[lid] if isinstance(before[lid], str) else ""
            events.append(insert_event(ctx.now, "leases", "info", f"lease released on {mid} · {label or lid}", mid,
                                       {"lease": lid}))
    return current, events


def evaluate(api, now, store=None):
    """One evaluation (the ``health`` collector body). Returns the gauge points."""
    store = store or telemetry.open_store(api)
    prepare(store)
    owner = owner_of(api)
    generation = acquire(store, owner, now)
    if generation is None:
        return _gauge(store)
    with _eval_lock:
        ctx = Context(api, store, now, owner)
        cfg = configs(store)
        results = {}
        for rule in RULES:
            if not cfg[rule.id]["enabled"]:
                results[rule.id] = None
                continue
            try:
                results[rule.id] = rule.evaluate(ctx, cfg[rule.id]["params"])
            except Exception as exc:
                results[rule.id] = None
                ctx.sources["rule:" + rule.id] = "evaluation failed · " + type(exc).__name__
        states = {key: value for key, _, value in store.items(STATE)}
        machines, profiles = ctx.machines(), ctx.profiles()
        changes = advance(states, results, cfg, now, machines=None if machines is None else set(machines),
                          profiles=None if profiles is None else set(profiles))
        statements = guard(owner, generation, api.clock())
        statements += [upsert_doc(STATE, fid, finding, now) for fid, finding in changes["changed"].items()
                       if finding != states.get(fid)]
        statements += [delete_doc(STATE, fid) for fid in changes["removed"]]
        statements += changes["events"] + ctx.writes
        previous = store.get(LEASES, "current")
        leases, lease_events = _lease_events(ctx, previous)
        if leases != previous:
            statements.append(upsert_doc(LEASES, "current", leases, now))
        statements += lease_events
        from hyperloom.web.features import alerts
        with alerts.BOOK_LOCK:       # planning and its commit exclude delivery acknowledgements in this process
            # A planning failure fails the whole evaluation: no transition is committed without its deliveries.
            statements += alerts.plan(store, changes["transitions"], changes["states"], now)
            meta = {"evaluated_at": now, "duration_s": max(0.0, api.clock() - now), "sources": ctx.sources,
                    "overflow": changes["overflow"],
                    "rules": {rule.id: (None if results.get(rule.id) is None else len(results[rule.id])) for rule in RULES}}
            statements.append(upsert_doc(META, "last", meta, now))
            statements.append(RETENTION)
            commit(store, statements)
    return _gauge(store)


def _gauge(store):
    counts = dict.fromkeys(SEVERITIES, 0)
    for _, _, finding in store.items(STATE):
        if finding.get("state") in ("firing", "clearing") and finding.get("severity") in counts:
            counts[finding["severity"]] += 1
    return [("health_findings_active", {"severity": severity}, count, "gauge") for severity, count in counts.items()]


def collect(api, now):
    store = telemetry.open_store(api)
    if not getattr(store, "_hyperloom_health_described", False):
        store.describe("health_findings_active", "gauge", unit="findings", interval=INTERVAL)
        store._hyperloom_health_described = True
    return evaluate(api, now, store)


# -- documents ------------------------------------------------------------------------------------------------------

def _active(store):
    findings = [value for _, _, value in store.items(STATE)]
    order = lambda f: (-RANK.get(f.get("severity"), 0), f.get("since") or f.get("first_true") or 0, f["id"])
    active = sorted((f for f in findings if f.get("state") in ("firing", "clearing")), key=order)
    pending = sorted((f for f in findings if f.get("state") == "pending"), key=order)
    return active, pending


PUBLIC = ("id", "rule", "title", "severity", "state", "detail", "hint", "machine", "profile", "since", "first_true",
          "last_true", "last_observed", "note")


def _public(finding):
    return {key: finding.get(key) for key in PUBLIC}


def _meta(api, store):
    meta = store.get(META, "last") or {}
    evaluated = meta.get("evaluated_at")
    age = None if evaluated is None else max(0.0, api.clock() - evaluated)
    return meta, evaluated, age


def findings_document(api, query):
    if query:
        raise ValueError("health takes no query arguments")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("health", findings=None, pending=None, evaluated_at=None, evaluation_age_s=None,
                            sources=None, why={"findings": UNAVAILABLE, "pending": UNAVAILABLE})
    active, pending = _active(store)
    meta, evaluated, age = _meta(api, store)
    why = {} if evaluated is not None else {"evaluated_at": "no evaluation has run yet"}
    return api.document("health", findings=[_public(f) for f in active], pending=[_public(f) for f in pending],
                        evaluated_at=evaluated, evaluation_age_s=age, stale=age is None or age > 3 * INTERVAL,
                        sources=meta.get("sources"), overflow=meta.get("overflow", 0), why=why)


def rules_document(api, query):
    if query:
        raise ValueError("health/rules takes no query arguments")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("health-rules", rules=None, why={"rules": UNAVAILABLE})
    cfg, stored = configs(store), {key for key, _, _ in store.items(CONFIG)}
    states = [value for _, _, value in store.items(STATE)]
    meta, evaluated, _ = _meta(api, store)
    sources = meta.get("sources") or {}
    counted = meta.get("rules") or {}
    rules = []
    for rule in RULES:
        settings = cfg[rule.id]
        mine = [f for f in states if f.get("rule") == rule.id]
        reasons = [why for name, why in sources.items() if why != "ok" and (
            name == "rule:" + rule.id or any(name in ("series:" + i, "feature:" + i, i) for i in rule.inputs))]
        available = None if evaluated is None or not settings["enabled"] else counted.get(rule.id) is not None
        rules.append({"id": rule.id, "title": rule.title, "description": rule.description, "severity": rule.severity,
                      "hint": rule.hint, "inputs": list(rule.inputs), "enabled": settings["enabled"],
                      "hold_s": settings["hold_s"], "clear_s": settings["clear_s"], "params": settings["params"],
                      "defaults": {"hold_s": rule.hold_s, "clear_s": rule.clear_s,
                                   "params": {name: spec[0] for name, spec in rule.params.items()}},
                      "bounds": {name: {"low": spec[1], "high": spec[2], "unit": spec[3]} for name, spec in rule.params.items()},
                      "overridden": rule.id in stored, "available": available,
                      "why": "; ".join(sorted(set(reasons))) or (None if available or available is None else "source unavailable"),
                      "active": sum(f.get("state") in ("firing", "clearing") for f in mine),
                      "pending": sum(f.get("state") == "pending" for f in mine),
                      "notes": sorted({f["note"] for f in mine if f.get("note")})})
    return api.document("health-rules", rules=rules, thresholds_source=sources.get("thresholds"), evaluated_at=evaluated,
                        why={} if evaluated is not None else {"evaluated_at": "no evaluation has run yet"})


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def summary_document(api, query):
    if query:
        raise ValueError("health/summary takes no query arguments")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("health-summary", counts=None, strip=None, banner=None, evaluated_at=None, stale=True,
                            why={"counts": UNAVAILABLE, "strip": UNAVAILABLE})
    active, _ = _active(store)
    _, evaluated, age = _meta(api, store)
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in active:
        counts[finding.get("severity", "warning")] = counts.get(finding.get("severity", "warning"), 0) + 1
    banner = None
    for finding in active:
        reason = (finding.get("facts") or {}).get("reason")
        # Only a state the producer names thermal asserts thermal slowdown; other throttling stays a finding.
        if finding.get("rule") == "gpu-throttled" and finding.get("state") == "firing" and isinstance(reason, str) \
                and "thermal" in reason.lower() and finding.get("since"):
            banner = f"GPU thermal slowdown on {finding.get('subject', '').replace('/', ' ')} since {_utc(finding['since'])}"
            break
    strip = [{"id": f["id"], "title": f["title"], "since": f.get("since"), "severity": f.get("severity"),
              "machine": f.get("machine")} for f in active[:20]]
    return api.document("health-summary", counts=counts, strip=strip, banner=banner, evaluated_at=evaluated,
                        stale=age is None or age > 3 * INTERVAL,
                        why={} if evaluated is not None else {"evaluated_at": "no evaluation has run yet"})


def audit_arguments(arguments):
    """Only validated values reach the audit log (never an arbitrary client string)."""
    rule = BY_ID.get(arguments.get("rule")) if isinstance(arguments.get("rule"), str) else None
    safe = {"rule": rule.id if rule else "[unknown]"}
    for key in ("enabled", "reset"):
        if isinstance(arguments.get(key), bool):
            safe[key] = arguments[key]
    for key in ("hold_s", "clear_s"):
        if type(arguments.get(key)) in (int, float) and math.isfinite(arguments[key]):
            safe[key] = arguments[key]
    params = arguments.get("params")
    if rule and isinstance(params, dict):
        safe["params"] = {name: value for name, value in params.items()
                          if name in rule.params and type(value) in (int, float) and math.isfinite(value)}
    return safe


def set_rule_post(api, arguments):
    store = telemetry.open_store(api)
    try:
        result = {"rule": arguments.get("rule"), "effective": set_rule(store, arguments)}
    except (ValueError, LookupError) as exc:
        api.audit("health/rules/set", audit_arguments(arguments), {"state": "refused", "error": str(exc)})
        raise
    api.audit("health/rules/set", audit_arguments(arguments), {"state": "succeeded", "result": result})
    return result


def register(reg):
    reg.get("health", findings_document)
    reg.get("health/rules", rules_document)
    reg.get("health/summary", summary_document)
    reg.post("health/rules/set", set_rule_post)        # audited by the handler with validated values only
    reg.collector("health", collect, interval=INTERVAL)
    reg.capability("health", True, "findings engine · rules over the shared series and collector outcomes")
