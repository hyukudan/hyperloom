"""Energy estimate (F18) and thermals (F19).

Energy is estimated from power sensors, never metered. Per machine every power reading is one sensor domain:
``gpu:<device>`` from ``node_gpu_power_watts`` (the GPU package sensor of an APU node, or the board sensor of one
discrete card) and ``cpu`` from ``node_cpu_power_watts`` where a sensor reports it. GPU devices of one machine are
separate devices and are added; a CPU package reading on a machine that also reports GPU power is treated as
overlapping (an APU's GPU package and CPU package share the socket) and is shown but not added, unless its series
declares ``disjoint="1"``. Board, memory, fans, disks and network are never covered and are listed as such.

Integration. A unit is one store bucket: one minute (tier m1) for the last 47 hours, 15 minutes (tier m15) before
that. A unit contributes ``avg W x observed s``, where ``observed`` is the time its samples stand for (the sampling
interval of each sample, capped at the unit length). That equals the interval-weighted sum exactly when every
sample of the unit was taken at one cadence. The cadence is verified, not assumed: each run of the collector
records the sampling intervals of every collector of this portal; time between two runs that saw the same
intervals is "verified" (persisted), and only units lying entirely inside verified time are integrated. A cadence
change therefore costs at most the time between two runs, and history no run vouched for (before the feature first
ran, or samples recorded while no portal sampler ran) is not integrated: it counts as unobserved. Gaps lower the
coverage; a sample is never stretched over a gap and nothing is extrapolated.

Accounting rows are kept per 15-minute quarter (UTC); every hour, day and month boundary of any time zone falls on a
quarter (offsets and their changes are multiples of 15 minutes). The collector ``energy`` (every 60 s) recomputes
the quarters not yet archived; a quarter is final 15 minutes after it ends (and after verification reached it) and
is then added once to the permanent monthly archive. Every statement of one run is guarded by a revision value and
the run moves the revision to a fresh random value, so a run that lost a race with another process changes nothing
and knows it. A reset archives everything first, then clears the quarter rows and sets a cutoff: only units that
start after the minute that held the reset are integrated afterwards. The inventory of known domains survives
resets.

GET energy?range=24h|7d|30d     GET energy/archive?before=YYYY-MM     GET thermals?range=day|week
POST energy/price, energy/thresholds, energy/reset (audited)
"""

import bisect
from datetime import datetime, timedelta
import json
import math
import re
import secrets
import threading
import time
import zlib

from hyperloom import telemetry

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover - Python < 3.9
    ZoneInfo = None


MINUTE, QUARTER, HOUR, DAY = 60, 900, 3600, 86400
GRACE_S = 900                       # a quarter is final this long after it ends (late samples)
FINE_S = 47 * HOUR                  # minute buckets are used while the store keeps them (48 h)
BACKFILL_S = 30 * DAY - QUARTER     # the 15-minute tier's retention
KEEP_ROWS_S = 35 * DAY              # quarter rows and verified spans kept (the archive is permanent)
EXPECTED_S = 30 * DAY               # a known domain is expected in coverage while it reported within this time
INVENTORY_S = DAY                   # a power domain is an expected observer while it reported within this lookback
TOKEN_LOOKBACK_S = HOUR             # an engine is an expected observer while it reported tokens within this lookback
MAX_LINES = 64                      # per input series (power, tokens, temperatures)
MAX_THROTTLE = 200                  # throttle periods per machine
ARCHIVE_MONTHS = 24
READ_ATTEMPTS = 3
BANNER = "Estimated from sensors, not metered"
GPU_POWER, CPU_POWER = "node_gpu_power_watts", "node_cpu_power_watts"
TOKENS, ENGINE_UP = "engine_generated_tokens", "engine_up"
TEMPS = (("node_gpu_temp_celsius", "gpu"), ("node_cpu_temp_celsius", "cpu"), ("node_sensor_temp_celsius", "sensor"))
THROTTLE = "node_gpu_throttled"
PREFS = "prefs"
PRICE_KEY, THRESHOLD_KEY = "energy/price", "energy/thermal_thresholds"
UI_PREFS, ZONE_KEY = "prefs/ui", "timezone"
DEFAULT_THRESHOLDS = {"gpu": {"warn": 85.0, "crit": 90.0}}
RANGES = {"24h": 1, "7d": 7, "30d": 30}
THERMAL_RANGES = {"day": (DAY, "m1", 300, MINUTE), "week": (7 * DAY, "m15", QUARTER, QUARTER)}
THERMAL_CACHE_S = 15
NO_STORE = "metrics history not open in this portal process (samplers disabled)"
NOT_RUN = "the energy collector has not run yet"
UNCOVERED = ["board", "memory", "fans", "disks", "network"]
CONTROL_HOST = "the control machine's host (CPU, board, memory, fans, disks)"
CPU_PART = "CPU (not reported separately; the GPU package reading may include part of it)"
OVERLAP = "overlaps the GPU package reading on this machine (not declared disjoint)"
DUPLICATE = "a second reading of the same domain"
NO_SAMPLES = "no samples in this range"
PAIR_RULE = ("only minutes (15-minute buckets beyond 47 h) completely observed by every expected power domain and "
             "every running engine; a domain silent for 24 h or an engine silent for 1 h is no longer expected")
CURRENCY = re.compile(r"[A-Z]{3}")
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

DDL = """
CREATE TABLE IF NOT EXISTS energy_quarters (q INTEGER NOT NULL, machine TEXT NOT NULL, domain TEXT NOT NULL,
    wh REAL NOT NULL, observed_s REAL NOT NULL, peak_w REAL, counted INTEGER NOT NULL,
    PRIMARY KEY(q, machine, domain));
CREATE TABLE IF NOT EXISTS energy_pairs (q INTEGER PRIMARY KEY, wh_both REAL NOT NULL, tokens_both REAL NOT NULL,
    both_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_monthly (month TEXT NOT NULL, machine TEXT NOT NULL, wh REAL NOT NULL,
    observed_s REAL NOT NULL, priced_wh REAL NOT NULL, cost REAL, currency TEXT, zone TEXT,
    PRIMARY KEY(month, machine));
CREATE TABLE IF NOT EXISTS energy_monthly_tokens (month TEXT PRIMARY KEY, wh_both REAL NOT NULL,
    tokens_both REAL NOT NULL, both_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_domains (machine TEXT NOT NULL, domain TEXT NOT NULL, counted INTEGER NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(machine, domain));
CREATE TABLE IF NOT EXISTS energy_verified (start REAL PRIMARY KEY, end REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_state (key TEXT PRIMARY KEY, value REAL);
"""
TABLES = ("energy_quarters", "energy_pairs", "energy_monthly", "energy_monthly_tokens", "energy_domains",
          "energy_verified", "energy_state")
GUARD = "EXISTS (SELECT 1 FROM energy_state WHERE key='revision' AND value=?)"

_ensure_lock = threading.Lock()
_thermal_cache = {}


def _floor(value, step):
    return int(value // step) * step


def _ensure(store):
    """Create the feature tables (collector, POST and CLI paths only; a GET never calls this)."""
    with _ensure_lock:
        if not getattr(store, "_energy_tables", False):
            store.ensure_table(DDL)
            store.execute("INSERT OR IGNORE INTO energy_state(key, value) VALUES ('revision', 0)")
            store._energy_tables = True


def _has_tables(store):
    names = {row[0] for row in store.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'energy_%'")}
    return all(name in names for name in TABLES)


def _state(store):
    return {key: value for key, value in store.execute("SELECT key, value FROM energy_state")}


# -- time zone (prefs/ui timezone, default the control machine's local zone) ------------------------------------------
class Zone:
    def __init__(self, zone=None, name=None, source="control machine", why=None):
        self.zone, self.source, self.why = zone, source, why
        self.name = name or "local time of the control machine"

    def local(self, ts):
        return datetime.fromtimestamp(ts, self.zone) if self.zone is not None else datetime.fromtimestamp(ts).astimezone()

    def midnight(self, day):
        if self.zone is not None:
            return datetime(day.year, day.month, day.day, tzinfo=self.zone).timestamp()
        return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))

    def hour_key(self, ts):
        """The local wall-clock hour holding ts, told apart by its UTC offset (a repeated hour is a new hour)."""
        local = self.local(ts)
        return local.date(), local.hour, local.utcoffset()

    def month(self, ts):
        return self.local(ts).strftime("%Y-%m")

    def label(self, ts, fmt="%Y-%m-%d %H:%M"):
        local = self.local(ts)
        return local.strftime(fmt) + " " + _offset(local.utcoffset())

    def describe(self, now):
        out = {"name": self.name, "source": self.source, "offset": _offset(self.local(now).utcoffset())}
        if self.why:
            out["why"] = self.why
        return out


def _offset(delta):
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def zone_of(store):
    name = store.get(UI_PREFS, ZONE_KEY) if store is not None else None
    if isinstance(name, str) and name:
        if ZoneInfo is None:
            return Zone(why=f"time zone data unavailable for {name}; using the control machine's zone")
        try:
            return Zone(ZoneInfo(name), name, "configured")
        except Exception:
            return Zone(why=f"configured zone {name} is unknown on this machine; using the control machine's zone")
    return Zone()


# -- cadence verification --------------------------------------------------------------------------------------------
def cadence_of(api):
    """{collector: interval_s} of this portal's running samplers, or None when no sampler runs in this process."""
    status = telemetry.status(api)
    if status is None:
        return None
    return {name: float(state.get("interval_s") or 0) for name, state in status.items()}


def _fingerprint(cadence):
    return float(zlib.crc32(json.dumps(sorted(cadence.items())).encode("utf-8")))


class Verified:
    """Sorted, disjoint spans of verified sampling time."""

    def __init__(self, spans):
        self.spans = sorted(spans)
        self.starts = [s for s, _ in self.spans]

    def covers(self, start, end):
        i = bisect.bisect_right(self.starts, start) - 1
        return i >= 0 and self.spans[i][1] >= end

    def overlap(self, start, end):
        return sum(max(0.0, min(e, end) - max(s, start)) for s, e in self.spans)

    @property
    def until(self):
        return self.spans[-1][1] if self.spans else None


def _mark_verified(store, start, end):
    """Record [start, end] as verified sampling time (used by the collector through plan; fixtures use it to stand
    for runs of a portal that happened before they were recorded)."""
    _ensure(store)
    store.execute("INSERT OR REPLACE INTO energy_verified(start, end) VALUES (?, ?)", (float(start), float(end)))


# -- reading the store ---------------------------------------------------------------------------------------------
def _domain(series, labels):
    if series == GPU_POWER:
        return "gpu:" + labels["device"] if labels.get("device") else "gpu"
    return "cpu"


def _line_count(store, series):
    # The store's series index; checked before any line is materialised.
    return store.execute("SELECT COUNT(*) FROM series WHERE name=?", (series,))[0][0]


def _read(store, series, start, end, now, cutoff, verified):
    """Lines of one series as [{labels, units: {unit: (avg, observed, max, count, step)}}]: minute units for quarters
    starting within FINE_S of now, quarter units before (the boundary is quarter-aligned, so a quarter never mixes
    both). Only units entirely inside verified time and at or after the reset cutoff are kept (``verified`` None:
    no filter, for state series that are not integrated). More than MAX_LINES lines or a truncated query refuses
    the whole computation."""
    if _line_count(store, series) > MAX_LINES:
        raise ValueError(f"{series} has more than {MAX_LINES} lines; energy was not computed")
    fine_from = _floor(now - FINE_S, QUARTER) + QUARTER
    lines = {}
    spans = []
    if start < fine_from:
        spans.append(("m15", QUARTER, start, min(end, fine_from - 1)))
    if end >= fine_from:
        spans.append(("m1", MINUTE, max(start, fine_from), end))
    for tier, step, a, b in spans:
        result = store.query(series, a, b, tier=tier)
        if result["truncated"]:
            raise ValueError(f"{series}: the store cut the query; energy was not computed")
        for line in result["lines"]:
            key = telemetry.canonical_labels(line["labels"])
            entry = lines.setdefault(key, {"labels": line["labels"], "units": {}})
            for t, avg, mx, _mn, n, _sum, _last, coverage in line["points"]:
                t = int(t)
                if coverage is None or not n or t < _floor(a, step) or t > b:
                    continue                    # no sampling interval recorded: not integrable
                if cutoff is not None and t < cutoff:
                    continue
                if verified is not None and not verified.covers(t, t + step):
                    continue
                entry["units"][t] = (avg, min(1.0, coverage) * step, mx, n, step)
    # The plainest label set first: of two lines for one domain, the one with extra labels is the duplicate.
    return sorted(lines.values(), key=lambda e: (len(e["labels"]), telemetry.canonical_labels(e["labels"])))


def _power(store, start, end, now, cutoff, verified):
    """{(machine, domain): {"labels", "counted", "units": {u: (wh, observed, max, step)}}}."""
    domains, gpu_machines = {}, set()
    for series in (GPU_POWER, CPU_POWER):
        for line in _read(store, series, start, end, now, cutoff, verified):
            labels = line["labels"]
            machine = labels.get("machine")
            if not machine:
                continue
            if series == GPU_POWER:
                gpu_machines.add(machine)
            domain = _domain(series, labels)
            duplicate = (machine, domain) in domains
            if duplicate:
                n = 2
                while (machine, f"{domain}#{n}") in domains:
                    n += 1
                domain = f"{domain}#{n}"
            units = {u: (avg * observed / HOUR, observed, mx, step)
                     for u, (avg, observed, mx, _n, step) in line["units"].items()}
            domains[(machine, domain)] = {"labels": labels, "units": units, "duplicate": duplicate}
    # GPU presence also comes from the series index (all time), so the overlap rule never depends on the window.
    for line in store.query(GPU_POWER, now, now, tier="m15")["lines"]:
        if line["labels"].get("machine"):
            gpu_machines.add(line["labels"]["machine"])
    for (machine, domain), info in domains.items():
        info["counted"] = not info["duplicate"] and not (
            domain == "cpu" and machine in gpu_machines and info["labels"].get("disjoint") != "1")
    return domains


def _tokens(store, start, end, now, cutoff, verified):
    """Token observers grouped by (profile, replica) across engine instances (a restart is the same observer):
    {group: {unit: (tokens, observed)}}, and the quarters in which each group's engine reported up."""
    groups = {}
    for line in _read(store, TOKENS, start, end, now, cutoff, verified):
        key = (line["labels"].get("profile", ""), line["labels"].get("replica", ""))
        units = groups.setdefault(key, {})
        for u, (avg, observed, _mx, n, step) in line["units"].items():
            tokens, seen = units.get(u, (0.0, 0.0))
            units[u] = (tokens + avg * n, min(float(step), seen + observed))
    up = {}
    for line in _read(store, ENGINE_UP, start, end, now, cutoff, None):
        key = (line["labels"].get("profile", ""), line["labels"].get("replica", ""))
        marks = up.setdefault(key, set())
        for u, (_avg, _observed, mx, _n, _step) in line["units"].items():
            if mx >= 1:
                marks.add(_floor(u, QUARTER))
    return groups, up


def _by_quarter(units):
    out = {}
    for u in units:
        out.setdefault(_floor(u, QUARTER), []).append(u)
    return out


def _reported_within(quarters, low, high):
    """True when a sorted list of quarters has one in [low, high]."""
    i = bisect.bisect_left(quarters, low)
    return i < len(quarters) and quarters[i] <= high


# -- the computation -----------------------------------------------------------------------------------------------
def plan(store, now, cadence=None, finalize_all=False):
    """The guarded statements of one computation (not executed) and a summary. ``cadence`` is this process's
    sampler intervals (None: no sampler here, so this run cannot extend verified time)."""
    _ensure(store)
    state = _state(store)
    revision = state.get("revision", 0)
    g = (revision,)
    statements = []

    spans = [tuple(row) for row in store.execute("SELECT start, end FROM energy_verified ORDER BY start")]
    if cadence is not None:
        fingerprint = _fingerprint(cadence)
        previous, since = state.get("cadence"), state.get("cadence_ts")
        if previous == fingerprint and since is not None and now > since:
            if spans and spans[-1][1] == since:
                statements.append((f"UPDATE energy_verified SET end=? WHERE start=? AND {GUARD}", (now, spans[-1][0], *g)))
                spans[-1] = (spans[-1][0], now)
            else:
                statements.append((f"INSERT OR REPLACE INTO energy_verified(start, end) SELECT ?, ? WHERE {GUARD}",
                                   (since, now, *g)))
                spans.append((since, now))
        statements += [_set("cadence", fingerprint, revision), _set("cadence_ts", now, revision)]
    statements.append((f"DELETE FROM energy_verified WHERE end < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)))
    verified = Verified(spans)

    cutoff = int(state["cutoff"]) if state.get("cutoff") is not None else None
    oldest = _floor(now - BACKFILL_S, QUARTER) + QUARTER
    stored = int(state["final_through"]) if state.get("final_through") is not None else None
    first = max(oldest, stored) if stored is not None else oldest
    lost = verified.overlap(stored, oldest) if stored is not None and stored < oldest else 0.0
    current = _floor(now, QUARTER)
    domains = _power(store, first - INVENTORY_S, now, now, cutoff, verified)
    groups, up = _tokens(store, first - TOKEN_LOOKBACK_S, now, now, cutoff, verified)
    zone = zone_of(store)
    price = price_of(store)

    by_quarter = {key: _by_quarter(info["units"]) for key, info in domains.items()}
    reported = {key: sorted(quarters) for key, quarters in by_quarter.items()}
    token_reported = {key: sorted(_by_quarter(units)) for key, units in groups.items()}

    rows, pairs = [], []
    for q in range(first, current + 1, QUARTER):
        for key, info in domains.items():
            present = [info["units"][u] for u in by_quarter[key].get(q, ())]
            if present:
                rows.append((q, key[0], key[1], sum(v[0] for v in present), sum(v[1] for v in present),
                             max(v[2] for v in present), int(info["counted"])))
        inventory = [key for key, info in domains.items() if info["counted"]
                     and _reported_within(reported[key], q - INVENTORY_S, q)]
        engines = [key for key in groups if _reported_within(token_reported[key], q - TOKEN_LOOKBACK_S, q)]
        engines += [key for key, quarters in up.items() if key not in engines and q in quarters]
        if not inventory or not engines:
            continue
        wh_both = tokens_both = both_s = 0.0
        for u in sorted({u for key in inventory for u in by_quarter[key].get(q, ())}):
            power = [domains[key]["units"].get(u) for key in inventory]
            # Complete observation by every expected observer: the paired seconds are common seconds.
            if any(v is None or v[1] < v[3] - 1e-6 for v in power) or len({v[3] for v in power}) != 1:
                continue
            step = power[0][3]
            tokens = [groups.get(key, {}).get(u) for key in engines]
            if any(t is None or t[1] < step - 1e-6 for t in tokens):
                continue
            wh_both += sum(v[0] for v in power)
            tokens_both += sum(t[0] for t in tokens)
            both_s += step
        if both_s:
            pairs.append((q, wh_both, tokens_both, both_s))

    limit = None if verified.until is None else min(now, verified.until)
    if finalize_all:
        final_through = current + QUARTER
    elif limit is None:
        final_through = first
    else:
        final_through = max(first, _floor(limit - GRACE_S, QUARTER))
    monthly, monthly_tokens = {}, {}
    for q, machine, _domain_id, wh, observed, _peak, counted in rows:
        if counted and q < final_through:
            entry = monthly.setdefault((zone.month(q), machine), [0.0, 0.0])
            entry[0] += wh
            entry[1] += observed
    for q, wh_both, tokens_both, both_s in pairs:
        if q < final_through:
            entry = monthly_tokens.setdefault(zone.month(q), [0.0, 0.0, 0.0])
            entry[0] += wh_both
            entry[1] += tokens_both
            entry[2] += both_s

    statements += [
        (f"DELETE FROM energy_quarters WHERE q >= ? AND {GUARD}", (first, *g)),
        (f"DELETE FROM energy_pairs WHERE q >= ? AND {GUARD}", (first, *g)),
        (f"DELETE FROM energy_quarters WHERE q < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)),
        (f"DELETE FROM energy_pairs WHERE q < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)),
    ]
    statements += [("INSERT INTO energy_quarters(q, machine, domain, wh, observed_s, peak_w, counted) "
                    f"SELECT ?, ?, ?, ?, ?, ?, ? WHERE {GUARD}", (*row, *g)) for row in rows]
    statements += [(f"INSERT INTO energy_pairs(q, wh_both, tokens_both, both_s) SELECT ?, ?, ?, ? WHERE {GUARD}",
                    (*row, *g)) for row in pairs]
    for (machine, domain), info in sorted(domains.items()):
        if info["units"]:
            step = next(iter(info["units"].values()))[3]
            statements.append((
                "INSERT INTO energy_domains(machine, domain, counted, first_seen, last_seen) "
                f"SELECT ?, ?, ?, ?, ? WHERE {GUARD} ON CONFLICT(machine, domain) DO UPDATE SET "
                "counted=excluded.counted, first_seen=MIN(first_seen, excluded.first_seen), "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (machine, domain, int(info["counted"]), min(info["units"]), max(info["units"]) + step, *g)))
    for (month, machine), (wh, observed) in sorted(monthly.items()):
        cost = wh / 1000 * price["price_per_kwh"] if price else None
        statements.append((
            "INSERT INTO energy_monthly(month, machine, wh, observed_s, priced_wh, cost, currency, zone) "
            f"SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE {GUARD} ON CONFLICT(month, machine) DO UPDATE SET "
            "wh=wh+excluded.wh, observed_s=observed_s+excluded.observed_s, "
            "cost=CASE WHEN excluded.cost IS NULL THEN cost WHEN priced_wh=0 THEN excluded.cost "
            "WHEN currency=excluded.currency THEN cost+excluded.cost ELSE NULL END, "
            "currency=CASE WHEN excluded.currency IS NULL THEN currency WHEN priced_wh=0 THEN excluded.currency "
            "WHEN currency=excluded.currency THEN currency ELSE 'mixed' END, "
            "priced_wh=priced_wh+excluded.priced_wh, "
            "zone=CASE WHEN zone=excluded.zone THEN zone ELSE 'mixed' END",
            (month, machine, wh, observed, wh if price else 0.0, cost, price["currency"] if price else None,
             zone.name, *g)))
    for month, (wh_both, tokens_both, both_s) in sorted(monthly_tokens.items()):
        statements.append((
            "INSERT INTO energy_monthly_tokens(month, wh_both, tokens_both, both_s) "
            f"SELECT ?, ?, ?, ? WHERE {GUARD} ON CONFLICT(month) DO UPDATE SET wh_both=wh_both+excluded.wh_both, "
            "tokens_both=tokens_both+excluded.tokens_both, both_s=both_s+excluded.both_s",
            (month, wh_both, tokens_both, both_s, *g)))
    statements.append(_set("final_through", final_through, revision))
    summary = {"from": first, "quarters": (current - first) // QUARTER + 1, "rows": len(rows), "pairs": len(pairs),
               "archived_quarters": max(0, (final_through - first) // QUARTER), "final_through": final_through,
               "verified_until": verified.until, "revision": revision, "lost_s": lost}
    return statements, summary


def _set(key, value, revision):
    return ("INSERT INTO energy_state(key, value) SELECT ?, ? "
            f"WHERE {GUARD} ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value, revision))


def _bump(revision, new):
    """The last statement of a run: the revision moves to a fresh random value, so a run can tell afterwards whether
    its own guarded statements were the ones applied."""
    return ("UPDATE energy_state SET value=? WHERE key='revision' AND value=?", (new, revision))


def _new_revision():
    return float(secrets.randbits(52) + 1)


def _applied(store, new):
    return _state(store).get("revision") == new


def compute(store, now, cadence=None):
    """One computation: recompute the quarters not yet archived and archive the final ones. A run that lost a race
    with another process changes nothing (``applied`` false); the next run redoes it."""
    statements, summary = plan(store, now, cadence)
    new = _new_revision()
    store.transaction(statements + [_bump(summary["revision"], new)])
    summary["applied"] = _applied(store, new)
    if summary["applied"] and summary["lost_s"]:
        store.event("energy", "warning", f"energy for {summary['lost_s'] / 3600:.1f} h of verified sampling time could "
                    "not be computed: the samples were no longer retained")
    return summary


def collect(api, now):
    compute(telemetry.open_store(api), now, cadence_of(api))
    return []


def reset_store(store, now, cadence=None):
    """Archive every quarter not yet archived (the current partial one too), then clear the quarter rows; only units
    starting after the minute that held the reset are integrated afterwards, so nothing is counted twice."""
    statements, summary = plan(store, now, cadence, finalize_all=True)
    revision, new = summary["revision"], _new_revision()
    g = (revision,)
    statements += [(f"DELETE FROM energy_quarters WHERE {GUARD}", g), (f"DELETE FROM energy_pairs WHERE {GUARD}", g),
                   _set("reset_at", now, revision), _set("cutoff", _floor(now, MINUTE) + MINUTE, revision),
                   _set("final_through", _floor(now, QUARTER), revision), _bump(revision, new)]
    store.transaction(statements)
    if not _applied(store, new):
        raise PermissionError("another energy computation ran at the same time; nothing was changed, retry the reset")
    return {"reset_at": now, "archived_quarters": summary["archived_quarters"]}


# -- preferences and writes ----------------------------------------------------------------------------------------
def price_of(store):
    value = store.get(PREFS, PRICE_KEY) if store is not None else None
    if isinstance(value, dict) and type(value.get("price_per_kwh")) in (int, float) and \
            isinstance(value.get("currency"), str):
        return {"price_per_kwh": float(value["price_per_kwh"]), "currency": value["currency"]}
    return None


def thresholds_of(store):
    value = store.get(PREFS, THRESHOLD_KEY) if store is not None else None
    out = {kind: dict(limits) for kind, limits in DEFAULT_THRESHOLDS.items()}
    if isinstance(value, dict):
        for kind in ("gpu", "cpu", "sensor"):
            limits = value.get(kind)
            if isinstance(limits, dict) and all(type(limits.get(k)) in (int, float) for k in ("warn", "crit")):
                out[kind] = {"warn": float(limits["warn"]), "crit": float(limits["crit"])}
    return out


def _only(arguments, allowed):
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(map(str, unknown)))


def _number(value, name, low, high, low_open=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value > high or \
            (value <= low if low_open else value < low):
        raise ValueError(f"{name} must be a number in {'(' if low_open else '['}{low:g}, {high:g}]")
    return float(value)


def set_price(api, arguments):
    _only(arguments, ("price_per_kwh", "currency", "clear"))
    if "clear" in arguments:
        if arguments["clear"] is not True or set(arguments) != {"clear"}:
            raise ValueError('clearing takes exactly {"clear": true}')
        telemetry.open_store(api).delete(PREFS, PRICE_KEY)
        return api.document("energy-price", price=None, why={"price": "no energy price configured"})
    if set(arguments) != {"price_per_kwh", "currency"}:
        raise ValueError("price_per_kwh and currency are required")
    price = _number(arguments["price_per_kwh"], "price_per_kwh", 0, 100, low_open=True)
    currency = arguments["currency"]
    if not isinstance(currency, str) or not CURRENCY.fullmatch(currency):
        raise ValueError("currency must be a three-letter code such as EUR")
    value = {"price_per_kwh": price, "currency": currency}
    telemetry.open_store(api).put(PREFS, PRICE_KEY, value)
    return api.document("energy-price", price=value)


def set_thresholds(api, arguments):
    _only(arguments, ("gpu", "cpu", "sensor"))
    if arguments.get("gpu") is None:
        raise ValueError("gpu thresholds are required")
    value = {}
    for kind, limits in arguments.items():
        if limits is None:
            continue
        if not isinstance(limits, dict) or set(limits) != {"warn", "crit"}:
            raise ValueError(f"{kind} takes exactly {{warn, crit}} in degrees Celsius")
        warn = _number(limits["warn"], kind + ".warn", 30, 120)
        crit = _number(limits["crit"], kind + ".crit", 30, 120)
        if warn >= crit:
            raise ValueError(f"{kind}: warn must be below crit")
        value[kind] = {"warn": warn, "crit": crit}
    store = telemetry.open_store(api)
    store.put(PREFS, THRESHOLD_KEY, value)
    _thermal_cache.clear()
    return api.document("energy-thresholds", thresholds=thresholds_of(store))


def reset(api, arguments):
    _only(arguments, ("confirm",))
    if arguments.get("confirm") != "reset":
        raise ValueError('reset needs exactly {"confirm": "reset"}')
    store = telemetry.open_store(api)
    return api.document("energy-reset", **reset_store(store, api.clock(), cadence_of(api)))


# -- the energy document -------------------------------------------------------------------------------------------
def _machines(api):
    """[(id, os)] in configuration order; an unreadable configuration yields []."""
    try:
        machines = api.cluster().machines
    except Exception:
        return []
    values = machines.values() if isinstance(machines, dict) else machines
    return [(m.id, getattr(m, "os", None)) for m in values]


def _label(machine_os, domain):
    if domain.startswith("cpu"):
        return "CPU package", "CPU package sensor"
    device = domain.split(":", 1)[1].split("#")[0] if ":" in domain else "0"
    if machine_os == "windows":
        return f"GPU card {device}", "GPU board power sensor"
    if machine_os == "linux":
        return f"GPU package {device}", "GPU package power sensor"
    return f"GPU {device}", "GPU power sensor"


def _hours(zone, now):
    """The last 24 local wall-clock hours as [(start, end, label)], built from quarters, so an hour shortened or
    lengthened by a clock change keeps its real length."""
    q = _floor(now, QUARTER)
    end = q + QUARTER
    key = zone.hour_key(q)
    while zone.hour_key(end) == key and end - q < 2 * HOUR:
        end += QUARTER
    groups = [[key, q, end]]
    while True:
        q -= QUARTER
        key = zone.hour_key(q)
        if key == groups[-1][0]:
            groups[-1][1] = q
            continue
        if len(groups) == 24:
            break
        groups.append([key, q, groups[-1][1]])
    return [(start, stop, zone.label(start, "%H:%M")) for _key, start, stop in reversed(groups)]


def _windows(zone, now, name):
    """[(start, end, label)] of the display buckets: local hours for 24h, local days otherwise."""
    if name == "24h":
        return _hours(zone, now)
    today = zone.local(now).date()
    days = [today - timedelta(days=i) for i in range(RANGES[name] - 1, -1, -1)]
    return [(zone.midnight(d), zone.midnight(d + timedelta(days=1)), d.isoformat()) for d in days]


def _latest(store):
    out = {}
    for series in (GPU_POWER, CPU_POWER):
        descriptor = store.descriptor(series) or {}
        max_age = 3 * float(descriptor.get("interval_s") or 10)
        for item in store.latest(series, max_age=max_age):
            machine = item["labels"].get("machine")
            if machine:
                out.setdefault((machine, _domain(series, item["labels"])), (item["value"], item["ts"]))
    return out


def _span(start, end, since, now):
    a, b = max(start, since), min(end, now)
    return b - a if b > a else 0.0


def energy_document(api, query):
    _only(query, ("range",))
    name = query.get("range", "24h")
    if name not in RANGES:
        raise ValueError("range must be one of " + ", ".join(RANGES))
    store = telemetry.existing_store(api)
    base = {"banner": BANNER, "range": name, "observer": "control machine, from node power sensors (estimated)"}
    empty = dict(machines=None, buckets=None, totals=None, cost=None, efficiency=None, archive=None)
    if store is None:
        return api.document("energy", **base, store=False, **empty, price=None, why={"machines": NO_STORE})
    zone = zone_of(store)
    status = (telemetry.status(api) or {}).get("energy")
    if not _has_tables(store):
        return api.document("energy", **base, store=True, timezone=zone.describe(api.clock()), **empty,
                            price=price_of(store), collector=status, why={"machines": NOT_RUN})
    # One consistent view: the revision must not move while the document is built (a run commits atomically).
    for _ in range(READ_ATTEMPTS):
        before = _state(store).get("revision")
        fields = _energy_fields(api, store, zone, name)
        if _state(store).get("revision") == before:
            return api.document("energy", **base, store=True, collector=status, **fields)
    return api.document("energy", **base, store=True, timezone=zone.describe(api.clock()), **empty,
                        price=price_of(store), collector=status,
                        why={"machines": "the energy rows changed while being read; the next poll retries"})


def _energy_fields(api, store, zone, name):
    now = api.clock()
    price = price_of(store)
    state = _state(store)
    reset_at, cutoff, final_through = state.get("reset_at"), state.get("cutoff"), state.get("final_through")
    windows = _windows(zone, now, name)
    start, end = windows[0][0], windows[-1][1]
    since = max(start, cutoff) if cutoff is not None else start
    rows = store.execute("SELECT q, machine, domain, wh, observed_s, peak_w, counted FROM energy_quarters "
                         "WHERE q >= ? AND q < ? ORDER BY q", (_floor(start, QUARTER), end))
    pairs = store.execute("SELECT q, wh_both, tokens_both, both_s FROM energy_pairs WHERE q >= ? AND q < ?",
                          (_floor(start, QUARTER), end))
    # Expected domains: the persisted inventory (it survives resets) of domains that reported within 30 days and
    # were known before the range ended; one without samples in the range still counts against coverage.
    known = store.execute("SELECT machine, domain, counted FROM energy_domains WHERE last_seen >= ? AND first_seen < ?",
                          (now - EXPECTED_S, end))
    configured = _machines(api)
    if configured:
        allowed = {m for m, _ in configured}
        rows = [r for r in rows if r[1] in allowed]
        known = [k for k in known if k[0] in allowed]
        order = [m for m, _ in configured]
    else:
        order = sorted({k[0] for k in known} | {r[1] for r in rows})
    os_of = dict(configured)
    elapsed = _span(start, end, since, now)

    stats = {}
    for machine, domain, counted in known:
        stats[(machine, domain)] = {"wh": 0.0, "observed_s": 0.0, "peak_w": None, "counted": bool(counted)}
    for _q, machine, domain, wh, observed, peak, counted in rows:
        d = stats.setdefault((machine, domain), {"wh": 0.0, "observed_s": 0.0, "peak_w": None, "counted": bool(counted)})
        d["wh"] += wh
        d["observed_s"] += observed
        if peak is not None:
            d["peak_w"] = peak if d["peak_w"] is None else max(d["peak_w"], peak)
    inventory = {key for key, d in stats.items() if d["counted"]}

    latest = _latest(store)
    machines_out, why = [], {}
    for machine in order:
        keys = sorted((k for k in stats if k[0] == machine), key=lambda k: (k[1].startswith("cpu"), k[1]))
        doms = []
        for key in keys:
            d = stats[key]
            label, source = _label(os_of.get(machine), key[1])
            current = latest.get(key)
            sampled = d["observed_s"] > 0
            doms.append({"domain": key[1], "label": label, "source": source, "counted": d["counted"],
                         "overlap": None if d["counted"] else (DUPLICATE if "#" in key[1] else OVERLAP),
                         "latest_w": current[0] if current else None, "latest_ts": current[1] if current else None,
                         "wh": d["wh"] if sampled else None, "observed_s": d["observed_s"],
                         "coverage": min(1.0, d["observed_s"] / elapsed) if elapsed else None,
                         "avg_w": d["wh"] * HOUR / d["observed_s"] if sampled else None, "peak_w": d["peak_w"],
                         "why": {} if sampled else {"wh": NO_SAMPLES, "avg_w": NO_SAMPLES}})
        mine = [k for k in keys if k in inventory]
        sampled = [k for k in mine if stats[k]["observed_s"] > 0]
        missing = [k[1] for k in mine if k not in sampled]
        uncovered = list(UNCOVERED)
        if not any(k[1].startswith("cpu") for k in keys):
            uncovered = ([CONTROL_HOST, "network"] if os_of.get(machine) == "windows" else
                         [CPU_PART if os_of.get(machine) == "linux" else "CPU"] + uncovered)
        observed = sum(stats[k]["observed_s"] for k in mine)
        entry = {"id": machine, "domains": doms, "uncovered": uncovered,
                 "wh": sum(stats[k]["wh"] for k in sampled) if sampled else None,
                 "partial": missing, "observed_s": observed,
                 "coverage": min(1.0, observed / (len(mine) * elapsed)) if mine and elapsed else None,
                 "avg_w": sum(stats[k]["wh"] * HOUR / stats[k]["observed_s"] for k in sampled)
                 if sampled and not missing else None, "why": {}}
        if not keys:
            entry["why"]["wh"] = "no power reading recorded for this machine"
        elif not mine:
            entry["why"]["wh"] = "no counted power reading for this machine"
        elif not sampled:
            entry["why"]["wh"] = NO_SAMPLES
        if missing and sampled:
            entry["why"]["wh"] = "partial: no samples in this range for " + ", ".join(missing)
        if missing:
            entry["why"]["avg_w"] = "no samples in this range for " + ", ".join(missing)
        machines_out.append(entry)

    # Display buckets: counted energy per machine; coverage = observed domain time / (expected domains x span).
    by_bucket = []
    for b_start, b_end, label in windows:
        values, observed = {}, 0.0
        for q, machine, domain, wh, obs, _peak, _counted in rows:
            if b_start <= q < b_end and (machine, domain) in inventory:
                values[machine] = values.get(machine, 0.0) + wh
                observed += obs
        span = _span(b_start, b_end, since, now)
        coverage = min(1.0, observed / (len(inventory) * span)) if inventory and span else None
        by_bucket.append({"start": b_start, "end": b_end, "label": label, "machines": values, "coverage": coverage})

    hourly = {}
    for q, machine, domain, wh, _obs, _peak, _counted in rows:
        if (machine, domain) in inventory:
            key = zone.hour_key(q)
            entry = hourly.setdefault(key, [q, 0.0])
            entry[0] = min(entry[0], q)
            entry[1] += wh
    peak = None
    if hourly:
        top_start, top_wh = max(hourly.values(), key=lambda v: (v[1], v[0]))
        peak = {"start": top_start, "wh": top_wh, "label": zone.label(top_start)}
    totals = None
    measured = [m for m in machines_out if m["wh"] is not None]
    if measured:
        observed = sum(stats[k]["observed_s"] for k in inventory)
        partial = [m["id"] for m in machines_out if m["partial"] or (m["wh"] is None and
                                                                     any(k[0] == m["id"] for k in inventory))]
        totals = {"wh": sum(m["wh"] for m in measured),
                  "avg_w": sum(m["avg_w"] for m in measured) if not partial else None,
                  "partial": partial, "coverage": min(1.0, observed / (len(inventory) * elapsed)) if elapsed else None,
                  "peak": peak, "since": since, "since_label": zone.label(since)}
        if partial:
            why["avg_w"] = "unavailable: no samples in this range for part of " + ", ".join(partial)
            why["totals"] = "partial: " + ", ".join(partial) + " had expected power domains without samples"
    else:
        why["totals"] = "no power samples in this range" if inventory else "no power readings recorded"
    cost = None
    if price and totals:
        cost = {"value": totals["wh"] / 1000 * price["price_per_kwh"], "currency": price["currency"],
                "price_per_kwh": price["price_per_kwh"], "basis": "integrated energy at the current price"
                + (" (partial)" if totals["partial"] else "")}
    else:
        why["cost"] = "no energy price configured" if not price else why.get("totals", "no energy in this range")
    wh_both = sum(p[1] for p in pairs)
    tokens_both = sum(p[2] for p in pairs)
    both_s = sum(p[3] for p in pairs)
    efficiency = None
    if both_s and tokens_both > 0:
        efficiency = {"wh_per_1k_tokens": wh_both / tokens_both * 1000, "generated_tokens": tokens_both,
                      "wh": wh_both, "both_s": both_s,
                      "observer": "node power sensors (estimated) and engine generated-token counters",
                      "rule": PAIR_RULE}
    else:
        why["efficiency"] = ("no interval in which power and generated tokens were both completely observed"
                             if not both_s else "no tokens generated while power and tokens were both observed")
    archive, more = _archive(store, zone, final_through, price)
    return dict(timezone=zone.describe(now), bucket="hour" if name == "24h" else "day", machines=machines_out,
                machine_order=order, buckets=by_bucket, totals=totals, cost=cost, efficiency=efficiency,
                archive=archive, archive_more=more, price=price, reset_at=reset_at,
                verified_until=state.get("cadence_ts"), why=why)


# -- the archive -----------------------------------------------------------------------------------------------------
def _month_entry(months, month):
    return months.setdefault(month, {"month": month, "machines": {}, "wh": 0.0, "priced_wh": 0.0, "cost": None,
                                     "currency": None, "zone": None, "open": False, "cost_why": None,
                                     "wh_per_1k_tokens": None})


def _add_cost(entry, cost, currency):
    if currency == "mixed" or (cost is not None and entry["currency"] not in (None, currency)):
        entry["currency"] = "mixed"
        return
    if cost is not None:
        entry["currency"] = currency
        entry["cost"] = (entry["cost"] or 0.0) + cost


def _months(store, before=None, limit=ARCHIVE_MONTHS):
    """Archived months older than `before` (all when None), newest first, at most `limit`; and how many remain."""
    clause, args = ("WHERE month < ?", [before]) if before else ("", [])
    names = [row[0] for row in store.execute(
        f"SELECT DISTINCT month FROM energy_monthly {clause} ORDER BY month DESC LIMIT ?", (*args, limit + 1))]
    more = len(names) > limit
    names = names[:limit]
    months = {}
    if names:
        marks = ",".join("?" * len(names))
        for month, machine, wh, priced_wh, cost, currency, month_zone in store.execute(
                "SELECT month, machine, wh, priced_wh, cost, currency, zone FROM energy_monthly "
                f"WHERE month IN ({marks})", names):
            entry = _month_entry(months, month)
            entry["machines"][machine] = wh
            entry["wh"] += wh
            entry["priced_wh"] += priced_wh
            entry["zone"] = month_zone if entry["zone"] in (None, month_zone) else "mixed"
            _add_cost(entry, cost if priced_wh else None, currency)
        for month, wh_both, tokens_both, both_s in store.execute(
                f"SELECT month, wh_both, tokens_both, both_s FROM energy_monthly_tokens WHERE month IN ({marks})", names):
            entry = months[month]
            entry["_pairs"] = [wh_both, tokens_both, both_s]
    return months, more


def _finish(entry):
    pairs = entry.pop("_pairs", None)
    if pairs and pairs[1] > 0:
        entry["wh_per_1k_tokens"] = pairs[0] / pairs[1] * 1000
    if entry["currency"] == "mixed":
        entry["cost"], entry["currency"], entry["cost_why"] = None, None, "price currency changed during the month"
    elif not entry["priced_wh"]:
        entry["cost"], entry["cost_why"] = None, "no energy price configured while this month was archived"
    return entry


def _archive(store, zone, final_through, price):
    """The newest ARCHIVE_MONTHS months (archived rows plus the quarters not yet archived) and whether older ones
    exist (GET energy/archive?before= pages through them)."""
    months, more = _months(store)
    if final_through is not None:
        for q, machine, wh in store.execute("SELECT q, machine, SUM(wh) FROM energy_quarters WHERE q >= ? AND counted=1 "
                                            "GROUP BY q, machine", (final_through,)):
            entry = _month_entry(months, zone.month(q))
            entry["open"] = True
            entry["machines"][machine] = entry["machines"].get(machine, 0.0) + wh
            entry["wh"] += wh
            entry["zone"] = zone.name if entry["zone"] in (None, zone.name) else "mixed"
            if price:
                entry["priced_wh"] += wh
                _add_cost(entry, wh / 1000 * price["price_per_kwh"], price["currency"])
        for q, a, b, c in store.execute("SELECT q, wh_both, tokens_both, both_s FROM energy_pairs WHERE q >= ?",
                                        (final_through,)):
            entry = _month_entry(months, zone.month(q))
            pairs = entry.setdefault("_pairs", [0.0, 0.0, 0.0])
            pairs[0] += a
            pairs[1] += b
            pairs[2] += c
    ordered = sorted(months, reverse=True)
    return [_finish(months[m]) for m in ordered[:ARCHIVE_MONTHS]], more or len(ordered) > ARCHIVE_MONTHS


def archive_document(api, query):
    _only(query, ("before",))
    before = query.get("before")
    if before is not None and not MONTH.fullmatch(before):
        raise ValueError("before must be a month YYYY-MM")
    store = telemetry.existing_store(api)
    if store is None or not _has_tables(store):
        return api.document("energy-archive", months=None, more=False,
                            why={"months": NO_STORE if store is None else NOT_RUN})
    months, more = _months(store, before)
    return api.document("energy-archive", months=[_finish(months[m]) for m in sorted(months, reverse=True)],
                        more=more, before=before)


def archive_months(store):
    """Every archived month, newest first (the CLI's full listing)."""
    if not _has_tables(store):
        return []
    months, _more = _months(store, limit=10 ** 6)
    return [_finish(months[m]) for m in sorted(months, reverse=True)]


# -- thermals ------------------------------------------------------------------------------------------------------
def _thermal_label(kind, labels):
    head = {"gpu": "GPU", "cpu": "CPU", "sensor": "Sensor"}[kind]
    return " ".join([head, *[labels[k] for k in ("device", "sensor") if labels.get(k)]])


def _regroup(points, step, native, start, end):
    """[[t, avg, max]] in buckets of `step` seconds from start's bucket on (averages weighted by sample count, the
    maximum kept); at most ceil((end - start) / step) + 1 points."""
    groups = {}
    for t, avg, mx, _mn, n, _sum, _last, _cov in points:
        if not n or t < _floor(start, native):
            continue
        key = _floor(t, step)
        g = groups.setdefault(key, [0.0, 0, None])
        g[0] += avg * n
        g[1] += n
        g[2] = mx if g[2] is None else max(g[2], mx)
    limit = math.ceil((end - start) / step) + 1
    return [[t, round(s / n, 2), round(mx, 2)] for t, (s, n, mx) in sorted(groups.items())][-limit:]


def _colour(label):
    """The preferred colour slot of a sensor identity (stable across polls and ranges)."""
    return zlib.crc32(label.encode("utf-8")) % 5


def thermals_document(api, query):
    _only(query, ("range",))
    name = query.get("range", "day")
    if name not in THERMAL_RANGES:
        raise ValueError("range must be day or week")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("thermals", range=name, machines=None, thresholds=dict(DEFAULT_THRESHOLDS),
                            why={"machines": NO_STORE})
    now = api.clock()
    key = (id(store), name)
    cached = _thermal_cache.get(key)
    if cached and 0 <= now - cached[0] < THERMAL_CACHE_S:
        return cached[1]
    span, tier, step, native = THERMAL_RANGES[name]
    start = now - span
    buckets = (_floor(now, step) - _floor(start, step)) // step + 1     # bucket slots the range touches
    thresholds = thresholds_of(store)
    zone = zone_of(store)
    machines, why = {}, {}

    def machine_entry(machine):
        return machines.setdefault(machine, {"id": machine, "lines": [], "throttle": [], "throttle_more": 0})

    for series, kind in (*TEMPS, (THROTTLE, None)):
        if _line_count(store, series) > MAX_LINES * 8:
            why[series] = f"more than {MAX_LINES * 8} lines; not read"
            continue
        result = store.query(series, start, now, tier=tier)
        if result["truncated"]:
            why[series] = "oldest buckets beyond the point limit were cut"
        for line in result["lines"]:
            labels = line["labels"]
            machine = labels.get("machine")
            if not machine:
                continue
            if kind is None:
                # A state sample stands for its sampling interval (at least one bucket); samples whose spans touch
                # join one period, a longer silence ends it (never bridged).
                stand = max(native, float((store.descriptor(series) or {}).get("interval_s") or native))
                periods, current = [], None
                for t, _avg, _mx, _mn, n, _sum, last, _cov in line["points"]:
                    if not n or t < _floor(start, native):
                        continue
                    if last:
                        if current and t <= current[1]:
                            current[1] = t + stand
                        else:
                            current = [t, t + stand]
                            periods.append(current)
                    else:
                        current = None
                if periods:
                    machine_entry(machine)["throttle"] += [{"device": labels.get("device"), "start": s, "end": e}
                                                           for s, e in periods]
                continue
            points = _regroup(line["points"], step, native, start, now)
            if not points:
                continue
            limit = thresholds.get(kind)
            label = _thermal_label(kind, labels)
            machine_entry(machine)["lines"].append({
                "series": series, "kind": kind, "label": label, "colour": _colour(label), "labels": labels,
                "points": points, "max": max(p[2] for p in points), "last": points[-1][1], "last_ts": points[-1][0],
                "sampled_buckets": len(points), "buckets": buckets,
                "warn_buckets": sum(1 for p in points if p[2] >= limit["warn"]) if limit else None})
    order = [m for m, _ in _machines(api)]
    out = []
    for machine in order + sorted(set(machines) - set(order)):
        if machine not in machines:
            continue
        entry = machines[machine]
        entry["lines"].sort(key=lambda l: (["gpu", "cpu", "sensor"].index(l["kind"]), l["label"]))
        if len(entry["lines"]) > MAX_LINES:
            why[machine] = f"{len(entry['lines']) - MAX_LINES} more sensors not shown"
            entry["lines"] = entry["lines"][:MAX_LINES]
        # Each sensor starts from its own slot (stable as other sensors come and go) and moves to the next free
        # one only on a collision inside this chart, so up to five lines never share a colour.
        taken = set()
        for line in entry["lines"]:
            slot = line["colour"]
            for shift in range(5):
                if (slot + shift) % 5 not in taken:
                    slot = (slot + shift) % 5
                    break
            taken.add(slot)
            line["colour"] = slot
            if len(taken) == 5:
                taken = set()
        entry["throttle"].sort(key=lambda p: p["start"])
        if len(entry["throttle"]) > MAX_THROTTLE:
            entry["throttle_more"] = len(entry["throttle"]) - MAX_THROTTLE
            entry["throttle"] = entry["throttle"][-MAX_THROTTLE:]
        out.append(entry)
    if not out:
        why["machines"] = "no temperature samples recorded in this range"
    document = api.document(
        "thermals", range=name, step=step, start=start, end=now, machines=out, thresholds=thresholds,
        timezone=zone.describe(now), observer="node and control-machine temperature sensors (measured)",
        notes={"warn": f"{step // 60}-minute buckets whose recorded maximum reached the warn threshold, out of the "
                       "buckets that had samples; time between samples is unknown",
               "throttle": f"sampled periods at {native // 60}-minute resolution; each sample stands for its sampling interval and a longer silence ends a period"},
        why=why)
    _thermal_cache[key] = (now, document)
    return document


def register(reg):
    reg.get("energy", energy_document)
    reg.get("energy/archive", archive_document)
    reg.get("thermals", thermals_document)
    reg.post("energy/price", set_price, audited=True)
    reg.post("energy/thresholds", set_thresholds, audited=True)
    reg.post("energy/reset", reset, audited=True)
    reg.collector("energy", collect, interval=60)
