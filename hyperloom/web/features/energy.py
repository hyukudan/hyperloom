"""Energy estimate (F18) and thermals (F19).

Energy is estimated from power sensors, never metered. Per machine every power reading is one sensor domain:
``gpu:<device>`` from ``node_gpu_power_watts`` (the GPU package sensor of an APU node, or the board sensor of one
discrete card) and ``cpu`` from ``node_cpu_power_watts`` where a sensor reports it. GPU devices of one machine are
separate devices and are added; a CPU package reading on a machine that also reports GPU power is treated as
overlapping (an APU's GPU package and CPU package share the socket) and is shown but not added, unless its series
declares ``disjoint="1"``. Board, memory, fans, disks and network are never covered and are listed as such.

Integration. Every raw sample carries the sampling interval it was taken at, and it stands for exactly that long:
energy = sum of value x interval, observed time = sum of intervals, both exact whatever the cadence and its changes.
Samples are grouped per minute (units) and per 15-minute quarter (rows). Gaps lower the coverage; a sample is never
stretched over a gap and nothing is extrapolated. Samples without a recorded interval are not integrated.

Accounting rows are kept per quarter (UTC); every hour, day and month boundary of any time zone falls on a quarter
(offsets and their changes are multiples of 15 minutes). The collector ``energy`` (every 60 s) recomputes the
quarters not yet archived from the raw tier (8 h); a quarter is final 15 minutes after it ends and is then added
once to the permanent monthly archive (quarters older than the raw tier keep the rows computed while their samples
were there). Every statement of one run is guarded by a revision value that the run moves to a fresh random value,
and the run inserts a receipt only the winning transaction can write, so a run that lost a race with another
process changes nothing and knows it. A reset archives everything first, then clears the quarter rows and sets a
cutoff: only samples taken after the reset instant are integrated afterwards. The inventory of known domains
survives resets.

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
RAW_KEEP_S = telemetry.TIERS[0][2]  # the raw tier keeps every sample with its interval this long (8 h)
BACKFILL_S = 30 * DAY - QUARTER     # the 15-minute tier's retention
KEEP_ROWS_S = 35 * DAY              # quarter rows kept (the archive is permanent)
EXPECTED_S = 30 * DAY               # a known domain is expected in coverage while it reported within this time
INVENTORY_S = DAY                   # a power domain is an expected observer while it reported within this lookback
TOKEN_LOOKBACK_S = HOUR             # an engine is an expected observer while it reported tokens within this lookback
MAX_LINES = 64                      # per input series (power, tokens, temperatures)
MAX_THROTTLE = 200                  # throttle periods per machine
MAX_THERMAL_LINES = 96              # temperature and throttle lines read for one thermals document
MAX_LABELS = 512                    # characters of one line's label set
COMPUTE_BUDGET_S = 20.0             # cooperative: checked between reads and before the commit (nothing written)
MAX_ROWS = 20000                    # quarter rows written by one run (a backlog is worked off over several runs)
MAX_SAMPLES = 400000                # raw rows one run may read for one series (8 h at 2 s is 14,400 per line)
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024  # serialized size of one energy or thermals document
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
PAIR_RULE = ("only minutes completely observed by every expected power domain and "
             "every running engine; a domain silent for 24 h or an engine silent for 1 h is no longer expected")
CURRENCY = re.compile(r"[A-Z]{3}")
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

DDL = """
CREATE TABLE IF NOT EXISTS energy_quarters (q INTEGER NOT NULL, machine TEXT NOT NULL, domain TEXT NOT NULL,
    wh REAL NOT NULL, observed_s REAL NOT NULL, peak_w REAL, counted INTEGER NOT NULL,
    PRIMARY KEY(q, machine, domain));
CREATE TABLE IF NOT EXISTS energy_counts (q INTEGER NOT NULL, machine TEXT NOT NULL, domain TEXT NOT NULL,
    samples INTEGER NOT NULL, PRIMARY KEY(q, machine, domain));
CREATE TABLE IF NOT EXISTS energy_pairs (q INTEGER PRIMARY KEY, wh_both REAL NOT NULL, tokens_both REAL NOT NULL,
    both_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_monthly (month TEXT NOT NULL, machine TEXT NOT NULL, wh REAL NOT NULL,
    observed_s REAL NOT NULL, priced_wh REAL NOT NULL, cost REAL, currency TEXT, zone TEXT,
    PRIMARY KEY(month, machine));
CREATE TABLE IF NOT EXISTS energy_monthly_tokens (month TEXT PRIMARY KEY, wh_both REAL NOT NULL,
    tokens_both REAL NOT NULL, both_s REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_domains (machine TEXT NOT NULL, domain TEXT NOT NULL, counted INTEGER NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(machine, domain));
CREATE TABLE IF NOT EXISTS energy_receipts (token TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS energy_state (key TEXT PRIMARY KEY, value REAL);
"""
TABLES = ("energy_quarters", "energy_counts", "energy_pairs", "energy_monthly", "energy_monthly_tokens", "energy_domains",
          "energy_receipts", "energy_state")
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
        # The default zone is named by the control machine's own standard-time name, so a change of the machine's
        # zone shows up in the archive as a different (or "mixed") zone.
        self.name = name or _local_identity()

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

    def offsets(self, start, end):
        """[[from_ts, utc_offset_s], ...] covering [start, end] (quarter resolution): enough for any client to show
        every time in this zone without knowing its name."""
        out = []
        t = _floor(start, QUARTER)
        while t <= end + QUARTER:
            offset = int(self.local(max(t, start)).utcoffset().total_seconds())
            if not out or out[-1][1] != offset:
                out.append([max(t, start), offset])
            t += QUARTER
        return out

    def describe(self, now):
        out = {"name": self.name, "source": self.source, "offset": _offset(self.local(now).utcoffset())}
        if self.why:
            out["why"] = self.why
        return out


def _local_identity():
    """The control machine's zone as names and standard/daylight offsets, so two different zones that share a name
    never share an identity."""
    standard = _offset(timedelta(seconds=-time.timezone))
    daylight = _offset(timedelta(seconds=-time.altzone)) if time.daylight else standard
    names = "/".join(dict.fromkeys(n for n in time.tzname if n)) or "unnamed"
    return f"control machine local time ({names}; {standard}" + (f", {daylight}" if daylight != standard else "") + ")"


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


# -- reading the store ---------------------------------------------------------------------------------------------
def _domain(series, labels):
    if series == GPU_POWER:
        return "gpu:" + labels["device"] if labels.get("device") else "gpu"
    return "cpu"


def _line_count(store, series):
    # The store's series index; checked before any line is materialised.
    return store.execute("SELECT COUNT(*) FROM series WHERE name=?", (series,))[0][0]


def _longest_labels(store, series):
    return store.execute("SELECT COALESCE(MAX(LENGTH(labels)), 0) FROM series WHERE name=?", (series,))[0][0]


def _admit(store, series):
    """Refuse the computation before anything of a series is read when it is too wide."""
    if _line_count(store, series) > MAX_LINES:
        raise ValueError(f"{series} has more than {MAX_LINES} lines; energy was not computed")
    if _longest_labels(store, series) > MAX_LABELS:
        raise ValueError(f"{series}: a label set longer than {MAX_LABELS} characters; energy was not computed")


def _raw_rows(store, series, start):
    """How many raw rows of a series lie at or after start: admission before anything is materialised."""
    return store.execute("SELECT COUNT(*) FROM raw WHERE ts >= ? AND series IN (SELECT id FROM series WHERE name=?)",
                         (start, series))[0][0]


def _samples(store, series, start, end, deadline):
    """{labels key: (labels, [(ts, value, interval), ...])} of every raw row in [start, end] (rows sharing a
    timestamp are all kept), read in non-overlapping windows small enough that the store never cuts a query (a cut
    window is split and read again)."""
    if _raw_rows(store, series, start) > MAX_SAMPLES:
        raise ValueError(f"{series} has more than {MAX_SAMPLES} raw samples to read; energy was not computed")
    out = {}
    windows = [(start, end, True)]                  # (from, to, to included)
    while windows:
        if time.monotonic() > deadline:
            raise ValueError(f"energy computation exceeded {COMPUTE_BUDGET_S:g} s; nothing was written")
        a, b, closed = windows.pop()
        result = store.query(series, a, b, tier="raw")
        if result["truncated"]:
            if b - a <= MINUTE:
                raise ValueError(f"{series}: more samples per minute than the store returns; energy was not computed")
            middle = (a + b) / 2
            windows += [(a, middle, False), (middle, b, closed)]
            continue
        for line in result["lines"]:
            key = telemetry.canonical_labels(line["labels"])
            entry = out.setdefault(key, (line["labels"], []))
            entry[1].extend((t, value, interval) for t, value, *_rest, interval in line["points"] if closed or t < b)
    return out


def _units(samples, cutoff):
    """{minute: (value x interval sum, observed s, max value, longest interval, samples)} of samples that carry their interval
    and come after the reset cutoff. A sample stands for exactly the interval it was taken at (as recorded with it),
    so the energy of a unit is the exact interval-weighted sum; samples without a recorded interval are not
    integrable."""
    units = {}
    for t, value, interval in samples:
        if interval is None or interval <= 0 or (cutoff is not None and t <= cutoff):
            continue
        u = _floor(t, MINUTE)
        weighted, observed, top, longest, count = units.get(u, (0.0, 0.0, None, 0.0, 0))
        units[u] = (weighted + value * interval, observed + interval, value if top is None else max(top, value),
                    max(longest, interval), count + 1)
    return units


def _seen(store, series, start, end):
    """{labels key: (labels, set of quarters with samples)} from the 15-minute tier, unfiltered: observer
    expectations never depend on resets or on which samples are integrable."""
    out = {}
    for line in store.query(series, start, end, tier="m15")["lines"]:
        quarters = {int(p[0]) for p in line["points"] if p[4]}
        out[telemetry.canonical_labels(line["labels"])] = (line["labels"], quarters, line["points"])
    return out


def _power(store, start, end, now, cutoff, deadline):
    """{(machine, domain): {"labels", "counted", "units": {minute: (wh, observed, max)}, "seen": quarters}}."""
    lines = {}
    for series in (GPU_POWER, CPU_POWER):
        _admit(store, series)
        seen = _seen(store, series, start - INVENTORY_S, end)
        samples = _samples(store, series, start, end, deadline)
        for key in set(seen) | set(samples):
            labels = (samples.get(key) or seen.get(key))[0]
            lines[(series, key)] = {"series": series, "labels": labels,
                                    "seen": seen.get(key, (None, set()))[1],
                                    "units": _units(samples.get(key, (None, []))[1], cutoff)}
    domains, gpu_machines = {}, set()
    # The plainest label set first: of two lines for one domain, the one with extra labels is the duplicate.
    for (series, key), line in sorted(lines.items(), key=lambda item: (item[0][0], len(item[1]["labels"]), item[0][1])):
        machine = line["labels"].get("machine")
        if not machine:
            continue
        if series == GPU_POWER:
            gpu_machines.add(machine)
        domain = _domain(series, line["labels"])
        duplicate = (machine, domain) in domains
        if duplicate:
            n = 2
            while (machine, f"{domain}#{n}") in domains:
                n += 1
            domain = f"{domain}#{n}"
        units = {u: (weighted / HOUR, observed, top, longest, count)
                 for u, (weighted, observed, top, longest, count) in line["units"].items()}
        domains[(machine, domain)] = {"labels": line["labels"], "units": units, "seen": line["seen"],
                                      "duplicate": duplicate}
    # GPU presence also comes from the series index (all time), so the overlap rule never depends on the window.
    for line in store.query(GPU_POWER, now, now, tier="m15")["lines"]:
        if line["labels"].get("machine"):
            gpu_machines.add(line["labels"]["machine"])
    for (machine, domain), info in domains.items():
        info["counted"] = not info["duplicate"] and not (
            domain == "cpu" and machine in gpu_machines and info["labels"].get("disjoint") != "1")
    return domains


def _tokens(store, start, end, cutoff, deadline):
    """Token observers grouped by (profile, replica) across engine instances (a restart is the same observer):
    {group: {minute: (tokens, observed)}} where the tokens of every instance add up (exact counts) and the observed
    time is that of the best-covered single instance (instances may overlap, so their times never add); the quarters
    each group reported in; and the quarters in which its engine reported up."""
    for series in (TOKENS, ENGINE_UP):
        _admit(store, series)
    groups, seen = {}, {}
    for key, (labels, quarters, _points) in _seen(store, TOKENS, start - TOKEN_LOOKBACK_S, end).items():
        seen.setdefault((labels.get("profile", ""), labels.get("replica", "")), set()).update(quarters)
    for key, (labels, samples) in _samples(store, TOKENS, start, end, deadline).items():
        group = (labels.get("profile", ""), labels.get("replica", ""))
        units = groups.setdefault(group, {})
        seen.setdefault(group, set())
        # Token deltas are counts: sum the values (not value x interval) per minute.
        per_minute = {}
        for t, value, interval in samples:
            if interval is None or interval <= 0 or (cutoff is not None and t <= cutoff):
                continue
            u = _floor(t, MINUTE)
            tokens, observed, longest = per_minute.get(u, (0.0, 0.0, 0.0))
            per_minute[u] = (tokens + value, observed + interval, max(longest, interval))
        for u, (tokens, observed, longest) in per_minute.items():
            total, best, worst = units.get(u, (0.0, 0.0, 0.0))
            units[u] = (total + tokens, max(best, observed), max(worst, longest))
    up = {}
    for key, (labels, _quarters, points) in _seen(store, ENGINE_UP, start, end).items():
        marks = up.setdefault((labels.get("profile", ""), labels.get("replica", "")), set())
        marks.update(int(p[0]) for p in points if p[4] and p[2] >= 1)
    return groups, seen, up


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
def plan(store, now, finalize_all=False, deadline=None):
    """The guarded statements of one computation (not executed) and a summary."""
    deadline = deadline or time.monotonic() + COMPUTE_BUDGET_S
    _ensure(store)
    state = _state(store)
    revision = state.get("revision", 0)
    g = (revision,)
    statements = []
    cutoff = state.get("cutoff")
    oldest = _floor(now - BACKFILL_S, QUARTER) + QUARTER
    stored = int(state["final_through"]) if state.get("final_through") is not None else None
    first = max(oldest, stored) if stored is not None else oldest
    current = _floor(now, QUARTER)
    # Samples carry their interval only in the raw tier (8 h). Quarters older than that keep the rows computed
    # while their samples were there; only newer quarters are recomputed.
    recompute = max(first, _floor(now - RAW_KEEP_S, QUARTER) + QUARTER)
    domains = _power(store, recompute, now, now, cutoff, deadline)
    groups, token_seen, up = _tokens(store, recompute, now, cutoff, deadline)
    zone = zone_of(store)
    price = price_of(store)

    by_quarter = {key: _by_quarter(info["units"]) for key, info in domains.items()}
    reported = {key: sorted(info["seen"]) for key, info in domains.items()}
    token_reported = {key: sorted(quarters) for key, quarters in token_seen.items()}

    rows, pairs, counts = [], [], []
    for q in range(recompute, current + 1, QUARTER):
        if time.monotonic() > deadline:
            raise ValueError(f"energy computation exceeded {COMPUTE_BUDGET_S:g} s; nothing was written")
        if len(rows) >= MAX_ROWS:
            # A backlog: stop before this quarter; the next runs continue from where this one archived to.
            if finalize_all:
                raise PermissionError("energy is still catching up on older samples; retry the reset in a few minutes")
            current = q - QUARTER
            break
        for key, info in domains.items():
            present = [info["units"][u] for u in by_quarter[key].get(q, ())]
            if present:
                rows.append((q, key[0], key[1], sum(v[0] for v in present), sum(v[1] for v in present),
                             max(v[2] for v in present), int(info["counted"])))
                counts.append((q, key[0], key[1], sum(v[4] for v in present)))
        inventory = [key for key, info in domains.items() if info["counted"]
                     and _reported_within(reported[key], q - INVENTORY_S, q)]
        engines = [key for key in token_reported if _reported_within(token_reported[key], q - TOKEN_LOOKBACK_S, q)]
        engines += [key for key, quarters in up.items() if key not in engines and q in quarters]
        if not inventory or not engines:
            continue
        wh_both = tokens_both = both_s = 0.0
        for u in sorted({u for key in inventory for u in by_quarter[key].get(q, ())}):
            power = [domains[key]["units"].get(u) for key in inventory]
            tokens = [groups.get(key, {}).get(u) for key in engines]
            # Complete observation of this minute by every expected observer, by samples no longer than the minute
            # (a longer sample's energy belongs to more than this minute). A unit's samples may reach a few
            # seconds past the minute; each observer's contribution is scaled to exactly the minute it covers.
            if any(v is None or v[1] < MINUTE - 1e-6 or v[3] > MINUTE for v in power):
                continue
            if any(t is None or t[1] < MINUTE - 1e-6 or t[2] > MINUTE for t in tokens):
                continue
            wh_both += sum(v[0] * MINUTE / v[1] for v in power)
            tokens_both += sum(t[0] * MINUTE / t[1] for t in tokens)
            both_s += MINUTE
        if both_s:
            pairs.append((q, wh_both, tokens_both, both_s))
    if time.monotonic() > deadline:
        raise ValueError(f"energy computation exceeded {COMPUTE_BUDGET_S:g} s; nothing was written")

    final_through = current + QUARTER if finalize_all else max(first, min(_floor(now - GRACE_S, QUARTER), current + QUARTER))
    # Rows of quarters before `recompute` that are now final come from the table (computed while samples existed).
    kept = store.execute("SELECT q, machine, domain, wh, observed_s, peak_w, counted FROM energy_quarters "
                         "WHERE q >= ? AND q < ?", (first, recompute)) if recompute > first else []
    kept_pairs = store.execute("SELECT q, wh_both, tokens_both, both_s FROM energy_pairs WHERE q >= ? AND q < ?",
                               (first, recompute)) if recompute > first else []
    monthly, monthly_tokens = {}, {}
    for q, machine, _domain_id, wh, observed, _peak, counted in [*kept, *rows]:
        if counted and q < final_through:
            entry = monthly.setdefault((zone.month(q), machine), [0.0, 0.0])
            entry[0] += wh
            entry[1] += observed
    for q, wh_both, tokens_both, both_s in [*kept_pairs, *pairs]:
        if q < final_through:
            entry = monthly_tokens.setdefault(zone.month(q), [0.0, 0.0, 0.0])
            entry[0] += wh_both
            entry[1] += tokens_both
            entry[2] += both_s
    missed = 0
    if recompute > first:
        # Quarters about to be archived from the table whose samples were not all integrated before they left the
        # raw tier: the 15-minute tier still counts the samples each domain recorded, the table counts the samples
        # integrated. Compared per machine and domain (duplicates of a domain summed on both sides); the quarter
        # holding a reset is left out (samples before the reset were archived by it). Reported once.
        integrated = {}
        for q, machine, domain, samples in store.execute(
                "SELECT q, machine, domain, samples FROM energy_counts WHERE q >= ? AND q < ?", (first, recompute)):
            key = (q, machine, domain.split("#")[0])
            integrated[key] = integrated.get(key, 0) + samples
        recorded = {}
        for series in (GPU_POWER, CPU_POWER):
            for _key, (labels, _quarters, points) in _seen(store, series, first, recompute - 1).items():
                for t, *_mid, n, _sum, _last, _cov, _cad in points:
                    q = int(t)
                    if n and first <= q < min(recompute, final_through) and labels.get("machine") and \
                            not (cutoff is not None and q <= cutoff < q + QUARTER):
                        key = (q, labels["machine"], _domain(series, labels))
                        recorded[key] = recorded.get(key, 0) + n
        missed = sum(1 for key, n in recorded.items() if n > integrated.get(key, 0))

    statements += [
        (f"DELETE FROM energy_quarters WHERE q >= ? AND q <= ? AND {GUARD}", (recompute, current, *g)),
        (f"DELETE FROM energy_pairs WHERE q >= ? AND q <= ? AND {GUARD}", (recompute, current, *g)),
        (f"DELETE FROM energy_counts WHERE q >= ? AND q <= ? AND {GUARD}", (recompute, current, *g)),
        (f"DELETE FROM energy_counts WHERE q < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)),
        (f"DELETE FROM energy_quarters WHERE q < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)),
        (f"DELETE FROM energy_pairs WHERE q < ? AND {GUARD}", (now - KEEP_ROWS_S, *g)),
    ]
    statements += [("INSERT INTO energy_quarters(q, machine, domain, wh, observed_s, peak_w, counted) "
                    f"SELECT ?, ?, ?, ?, ?, ?, ? WHERE {GUARD}", (*row, *g)) for row in rows]
    statements += [(f"INSERT INTO energy_pairs(q, wh_both, tokens_both, both_s) SELECT ?, ?, ?, ? WHERE {GUARD}",
                    (*row, *g)) for row in pairs]
    statements += [(f"INSERT INTO energy_counts(q, machine, domain, samples) SELECT ?, ?, ?, ? WHERE {GUARD}",
                    (*row, *g)) for row in counts]
    for (machine, domain), info in sorted(domains.items()):
        if info["units"]:
            statements.append((
                "INSERT INTO energy_domains(machine, domain, counted, first_seen, last_seen) "
                f"SELECT ?, ?, ?, ?, ? WHERE {GUARD} ON CONFLICT(machine, domain) DO UPDATE SET "
                "counted=excluded.counted, first_seen=MIN(first_seen, excluded.first_seen), "
                "last_seen=MAX(last_seen, excluded.last_seen)",
                (machine, domain, int(info["counted"]), min(info["units"]), max(info["units"]) + MINUTE, *g)))
    for (month, machine), (wh, observed) in sorted(monthly.items()):
        cost = wh / 1000 * price["price_per_kwh"] if price else None
        statements.append((
            "INSERT INTO energy_monthly(month, machine, wh, observed_s, priced_wh, cost, currency, zone) "
            f"SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE {GUARD} ON CONFLICT(month, machine) DO UPDATE SET "
            "wh=wh+excluded.wh, observed_s=observed_s+excluded.observed_s, "
            "cost=CASE WHEN excluded.cost IS NULL THEN cost WHEN currency IS NULL THEN excluded.cost "
            "WHEN currency=excluded.currency THEN cost+excluded.cost ELSE NULL END, "
            "currency=CASE WHEN excluded.currency IS NULL THEN currency WHEN currency IS NULL THEN excluded.currency "
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
    summary = {"from": first, "recomputed_from": recompute, "quarters": (current - first) // QUARTER + 1,
               "rows": len(rows), "pairs": len(pairs), "archived_quarters": max(0, (final_through - first) // QUARTER),
               "final_through": final_through, "revision": revision, "missed_quarters": missed}
    return statements, summary


def _set(key, value, revision):
    return ("INSERT INTO energy_state(key, value) SELECT ?, ? "
            f"WHERE {GUARD} ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value, revision))


def _bump(revision, new):
    """The last statement of a run: the revision moves to a fresh random value, so readers can tell a run committed
    while they read."""
    return ("UPDATE energy_state SET value=? WHERE key='revision' AND value=?", (new, revision))


def _new_revision():
    return float(secrets.randbits(52) + 1)


def _receipt(token, revision, now):
    """A guarded row only the winning transaction inserts: an outcome no later commit can change."""
    return (f"INSERT INTO energy_receipts(token, ts) SELECT ?, ? WHERE {GUARD}", (token, now, revision))


def _applied(store, token):
    found = bool(store.execute("SELECT 1 FROM energy_receipts WHERE token=?", (token,)))
    store.execute("DELETE FROM energy_receipts WHERE token=? OR ts < ?", (token, time.time() - DAY))
    return found


def compute(store, now):
    """One computation: recompute the quarters not yet archived and archive the final ones. A run that lost a race
    with another process changes nothing (``applied`` false); the next run redoes it."""
    deadline = time.monotonic() + COMPUTE_BUDGET_S
    statements, summary = plan(store, now, deadline=deadline)
    if time.monotonic() > deadline:
        raise ValueError(f"energy computation exceeded {COMPUTE_BUDGET_S:g} s; nothing was written")
    new, token = _new_revision(), secrets.token_hex(16)
    store.transaction(statements + [_receipt(token, summary["revision"], now), _bump(summary["revision"], new)])
    summary["applied"] = _applied(store, token)
    if summary["applied"] and summary["missed_quarters"]:
        store.event("energy", "warning", f"{summary['missed_quarters']} quarter hours of power samples were never "
                    "integrated: no energy run read them before they left the raw tier")
    return summary


def collect(api, now):
    compute(telemetry.open_store(api), now)
    return []


def reset_store(store, now):
    """Archive every quarter not yet archived (the current partial one too), then clear the quarter rows; only
    samples taken after the reset instant are integrated afterwards, so nothing is counted twice."""
    statements, summary = plan(store, now, finalize_all=True)
    revision, new, token = summary["revision"], _new_revision(), secrets.token_hex(16)
    g = (revision,)
    statements += [(f"DELETE FROM energy_quarters WHERE {GUARD}", g), (f"DELETE FROM energy_pairs WHERE {GUARD}", g),
                   (f"DELETE FROM energy_counts WHERE {GUARD}", g),
                   _set("reset_at", now, revision), _set("cutoff", now, revision),
                   _set("final_through", _floor(now, QUARTER), revision), _receipt(token, revision, now),
                   _bump(revision, new)]
    store.transaction(statements)
    if not _applied(store, token):
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


def _scrub(value):
    """Replace non-finite floats (an overflowing JSON literal such as 1e400 parses to infinity) by text in place,
    in objects and arrays at any depth, so the refusal that follows can be written to the audit log."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in list(items):
        if type(item) is float and not math.isfinite(item):
            value[key] = "non-finite number"
        else:
            _scrub(item)
    return value


def _only(arguments, allowed):
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(map(str, unknown)))


def _number(value, name, low, high, low_open=False):
    # Compared before any float conversion, so an integer of any size is refused, never an overflow.
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or value > high or \
            (value <= low if low_open else value < low):
        raise ValueError(f"{name} must be a number in {'(' if low_open else '['}{low:g}, {high:g}]")
    return float(value)


def set_price(api, arguments):
    _scrub(arguments)
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
    _scrub(arguments)
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
    _scrub(arguments)
    _only(arguments, ("confirm",))
    if arguments.get("confirm") != "reset":
        raise ValueError('reset needs exactly {"confirm": "reset"}')
    store = telemetry.open_store(api)
    return api.document("energy-reset", **reset_store(store, api.clock()))


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
        if _line_count(store, series) > MAX_LINES or _longest_labels(store, series) > MAX_LABELS:
            continue                    # the computation refuses such a series too; the page says so
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
        if status and status.get("last_ok"):
            fields["labels"]["collector_ok"] = zone.label(status["last_ok"])
        if _state(store).get("revision") == before:
            document = api.document("energy", **base, store=True, collector=status, **fields)
            size = len(json.dumps(document, ensure_ascii=True, default=str))
            if size > MAX_DOCUMENT_BYTES:
                return api.document("energy", **base, store=True, **empty, price=price_of(store), collector=status,
                                    why={"machines": f"the document would be {size // 1024} KiB, over the limit"})
            return document
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
        # Incomplete: a machine with expected domains lacking samples, or a configured machine whose power was
        # never recorded (its energy is unknown, so the lab total is a partial sum).
        partial = [m["id"] for m in machines_out if m["partial"] or m["wh"] is None]
        totals = {"wh": sum(m["wh"] for m in measured),
                  "avg_w": sum(m["avg_w"] for m in measured) if not partial else None,
                  "partial": partial, "coverage": min(1.0, observed / (len(inventory) * elapsed)) if elapsed else None,
                  "peak": peak, "since": since, "since_label": zone.label(since)}
        if partial:
            why["avg_w"] = "unavailable: no samples in this range for part of " + ", ".join(partial)
            why["totals"] = ("partial: no energy for " + ", ".join(partial)
                             + "; coverage counts only the expected power domains of machines with readings")
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
    labels = {"collector_ok": None, "reset_at": zone.label(reset_at) if reset_at is not None else None}
    return dict(timezone=zone.describe(now), offsets=zone.offsets(_floor(start, QUARTER) - HOUR, now), labels=labels,
                bucket="hour" if name == "24h" else "day", machines=machines_out,
                machine_order=order, buckets=by_bucket, totals=totals, cost=cost, efficiency=efficiency,
                archive=archive, archive_more=more, price=price, reset_at=reset_at,
                why=why)


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
            _add_cost(entry, cost, currency)
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
    elif entry["currency"] is None:
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
    found = _consistent(store, lambda: _months(store, before))
    if found is None:
        return api.document("energy-archive", months=None, more=False, before=before,
                            why={"months": "the archive changed while being read; the next poll retries"})
    months, more = found
    return api.document("energy-archive", months=[_finish(months[m]) for m in sorted(months, reverse=True)],
                        more=more, before=before)


def _consistent(store, read):
    """read() between two equal revisions (a run commits atomically and always moves the revision), or None."""
    for _ in range(READ_ATTEMPTS):
        before = _state(store).get("revision")
        result = read()
        if _state(store).get("revision") == before:
            return result
    return None


def archive_months(store):
    """Every archived month, newest first (the CLI's full listing)."""
    if not _has_tables(store):
        return []
    found = _consistent(store, lambda: _months(store, limit=10 ** 6))
    if found is None:
        raise PermissionError("the archive changed while being read; retry")
    months, _more = found
    return [_finish(months[m]) for m in sorted(months, reverse=True)]


# -- thermals ------------------------------------------------------------------------------------------------------
def _thermal_label(kind, labels):
    head = {"gpu": "GPU", "cpu": "CPU", "sensor": "Sensor"}[kind]
    return " ".join([head, *[labels[k] for k in ("device", "sensor") if labels.get(k)]])


def _regroup(points, step, native, start, end):
    """[[t, avg, max]] in buckets of `step` seconds from start's bucket on (averages weighted by sample count, the
    maximum kept); at most ceil((end - start) / step) + 1 points."""
    groups = {}
    for t, avg, mx, _mn, n, *_rest in points:
        if not n or t < _floor(start, native):
            continue
        key = _floor(t, step)
        g = groups.setdefault(key, [0.0, 0, None])
        g[0] += avg * n
        g[1] += n
        g[2] = mx if g[2] is None else max(g[2], mx)
    limit = math.ceil((end - start) / step) + 1
    return [[t, round(s / n, 2), round(mx, 2)] for t, (s, n, mx) in sorted(groups.items())][-limit:]


def _style(identity):
    """(colour slot 0..4, dash pattern 0..2) of a sensor identity: fixed by the identity alone, so a sensor keeps its
    look whatever other sensors appear or disappear; two sensors share both only by a 1-in-15 chance."""
    h = zlib.crc32(identity.encode("utf-8"))
    return h % 5, (h // 5) % 3


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
    # Keyed by the thresholds and zone too: a build that read older settings can never be served for newer ones.
    key = (id(store), name, json.dumps(thresholds_of(store), sort_keys=True), zone_of(store).name)
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

    total = 0
    for series, kind in (*TEMPS, (THROTTLE, None)):
        # Bounded before anything is read: at most MAX_LINES lines per series and MAX_THERMAL_LINES per document.
        count = _line_count(store, series)
        if count > MAX_LINES or total + count > MAX_THERMAL_LINES:
            why[series] = f"{count} lines exceed the limit of this page ({MAX_LINES} per series); not read"
            continue
        if _longest_labels(store, series) > MAX_LABELS:
            why[series] = f"a label set longer than {MAX_LABELS} characters; not read"
            continue
        total += count
        result = store.query(series, start, now, tier=tier)
        if result["truncated"]:
            why[series] = "oldest buckets beyond the point limit were cut"
        for line in result["lines"]:
            labels = line["labels"]
            machine = labels.get("machine")
            if not machine:
                continue
            if kind is None:
                # Runs of directly adjacent sampled buckets whose last state is non-zero; a missing or zero bucket
                # ends a run (never bridged), and runs are clipped to the range.
                periods, current = [], None
                for t, _avg, _mx, _mn, n, _sum, last, *_rest in line["points"]:
                    if not n or t < _floor(start, native):
                        continue
                    if last:
                        if current and t == current[1]:
                            current[1] = t + native
                        else:
                            current = [t, t + native]
                            periods.append(current)
                    else:
                        current = None
                periods = [[max(a, start), min(b, now)] for a, b in periods if min(b, now) > max(a, start)]
                if periods:
                    machine_entry(machine)["throttle"] += [{"device": labels.get("device"), "start": s, "end": e}
                                                           for s, e in periods]
                continue
            points = _regroup(line["points"], step, native, start, now)
            if not points:
                continue
            limit = thresholds.get(kind)
            label = _thermal_label(kind, labels)
            colour, dash = _style(series + telemetry.canonical_labels(labels))
            machine_entry(machine)["lines"].append({
                "series": series, "kind": kind, "label": label, "colour": colour, "dash": dash, "labels": labels,
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
        entry["throttle"].sort(key=lambda p: p["start"])
        if len(entry["throttle"]) > MAX_THROTTLE:
            entry["throttle_more"] = len(entry["throttle"]) - MAX_THROTTLE
            entry["throttle"] = entry["throttle"][-MAX_THROTTLE:]
        out.append(entry)
    if not out:
        why["machines"] = "no temperature samples recorded in this range"
    throttle_read = THROTTLE not in why
    document = api.document(
        "thermals", range=name, step=step, start=start, end=now, machines=out, thresholds=thresholds,
        throttle_read=throttle_read, timezone=zone.describe(now), offsets=zone.offsets(_floor(start, step) - HOUR, now),
        observer="node and control-machine temperature sensors (measured)",
        notes={"warn": f"{step // 60}-minute buckets whose recorded maximum reached the warn threshold, out of the "
                       "buckets that had samples; time between samples is unknown",
               "throttle": f"sampled periods at {native // 60}-minute resolution; a bucket without samples ends a period"},
        why=why)
    size = len(json.dumps(document, ensure_ascii=True, default=str))
    if size > MAX_DOCUMENT_BYTES:
        document = api.document("thermals", range=name, machines=None, thresholds=thresholds,
                                why={"machines": f"the history would be {size // 1024} KiB, over the page limit of "
                                                 f"{MAX_DOCUMENT_BYTES // 1024} KiB; use the CLI or the week view"})
    for old in [k for k, (t, _d) in _thermal_cache.items() if not 0 <= now - t < THERMAL_CACHE_S]:
        _thermal_cache.pop(old, None)
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
