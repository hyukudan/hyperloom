"""Persistent metrics history, event log and small document store (stdlib sqlite3).

Series are identified by a name and a label set and have a kind: ``gauge`` (a level: read avg/min/max),
``delta`` (a non-negative increment over the sample interval: read the bucket sum) or ``state`` (an
enumeration stored as a numeric code). Counters are never recorded directly: collectors turn them into deltas
with ``Baselines`` (restarts and new engine instances start a new baseline instead of a negative delta). Every
sample goes to the raw tier and is folded into coarser tiers (sum, count, min, max per bucket). Buckets are anchored to wall time (bucket = floor(ts /
step) * step), so history never shifts. A bucket without samples simply does not exist: readers show a
gap, never a zero. ``None`` values are not recorded for the same reason.
"""

import json
import math
from pathlib import Path
import sqlite3
import threading
import time


TIERS = (("raw", 2, 8 * 3600), ("m1", 60, 48 * 3600), ("m15", 900, 30 * 86400))
MAX_POINTS = 2000
MAX_EVENTS = 2000
SEVERITIES = ("info", "warning", "critical")
KINDS = ("gauge", "delta", "state")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, labels TEXT NOT NULL,
                                   kind TEXT NOT NULL DEFAULT 'gauge', UNIQUE(name, labels));
CREATE TABLE IF NOT EXISTS raw (series INTEGER NOT NULL, ts REAL NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS raw_series_ts ON raw(series, ts);
CREATE TABLE IF NOT EXISTS agg (tier TEXT NOT NULL, series INTEGER NOT NULL, bucket INTEGER NOT NULL,
                                sum REAL NOT NULL, count INTEGER NOT NULL, min REAL NOT NULL, max REAL NOT NULL,
                                PRIMARY KEY(tier, series, bucket));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, category TEXT NOT NULL,
                                   severity TEXT NOT NULL, machine TEXT, message TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS docs (namespace TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL, value TEXT NOT NULL,
                                 PRIMARY KEY(namespace, key));
"""


def canonical_labels(labels):
    labels = labels or {}
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool)
                                           for k, v in labels.items()):
        raise ValueError("labels must map strings to strings")
    return json.dumps({k: str(v) for k, v in sorted(labels.items())}, separators=(",", ":"), ensure_ascii=True)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._series = {}
        self._last_prune = 0.0

    def close(self):
        with self._lock:
            self._db.close()

    # -- series ---------------------------------------------------------------------------------------------------
    def _series_id(self, name, labels, kind):
        key = (name, labels)
        found = self._series.get(key)
        if found is None:
            self._db.execute("INSERT OR IGNORE INTO series(name, labels, kind) VALUES (?, ?, ?)", (name, labels, kind))
            found = self._db.execute("SELECT id, kind FROM series WHERE name=? AND labels=?", key).fetchone()
            self._series[key] = found
        if found[1] != kind:
            raise ValueError(f"series {name} is a {found[1]}, not a {kind}")
        return found[0]

    def record(self, points, ts=None):
        """Record (name, labels, value[, kind]) points at ts (default now; UTC epoch seconds). None and
        non-finite values are skipped (a gap, never a zero); a delta must be non-negative."""
        ts = self.clock() if ts is None else float(ts)
        rows = []
        for point in points:
            name, labels, value = point[:3]
            kind = point[3] if len(point) > 3 else "gauge"
            if not isinstance(name, str) or not name:
                raise ValueError("series name required")
            if kind not in KINDS:
                raise ValueError("series kind must be one of " + ", ".join(KINDS))
            if isinstance(value, bool):
                value = int(value)
            if _finite(value):
                if kind == "delta" and value < 0:
                    raise ValueError("delta samples must be non-negative")
                rows.append((name, canonical_labels(labels), float(value), kind))
        if not rows:
            return 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for name, labels, value, kind in rows:
                    sid = self._series_id(name, labels, kind)
                    self._db.execute("INSERT INTO raw(series, ts, value) VALUES (?, ?, ?)", (sid, ts, value))
                    for tier, step, _ in TIERS[1:]:
                        bucket = int(ts // step) * step
                        self._db.execute(
                            "INSERT INTO agg(tier, series, bucket, sum, count, min, max) VALUES (?, ?, ?, ?, 1, ?, ?) "
                            "ON CONFLICT(tier, series, bucket) DO UPDATE SET sum=sum+excluded.sum, count=count+1, "
                            "min=MIN(min, excluded.min), max=MAX(max, excluded.max)",
                            (tier, sid, bucket, value, value, value))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            if ts - self._last_prune > 300:
                self.prune(ts)
        return len(rows)

    def prune(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            self._last_prune = now
            self._db.execute("DELETE FROM raw WHERE ts < ?", (now - TIERS[0][2],))
            for tier, _, keep in TIERS[1:]:
                self._db.execute("DELETE FROM agg WHERE tier=? AND bucket < ?", (tier, now - keep))
            self._db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,))

    def names(self):
        with self._lock:
            return sorted({row[0] for row in self._db.execute("SELECT name FROM series")})

    def _matching(self, name, labels):
        """Series ids of `name` whose labels contain every given label."""
        want = {k: str(v) for k, v in (labels or {}).items()}
        found = []
        for sid, raw, kind in self._db.execute("SELECT id, labels, kind FROM series WHERE name=?", (name,)):
            have = json.loads(raw)
            if all(have.get(k) == v for k, v in want.items()):
                found.append((sid, have, kind))
        return found

    @staticmethod
    def pick_tier(start, end, now):
        """The finest tier that still holds `start` and returns at most MAX_POINTS buckets."""
        for tier, step, keep in TIERS:
            if start >= now - keep and (end - start) / step <= MAX_POINTS:
                return tier, step
        return TIERS[-1][0], TIERS[-1][1]

    def query(self, name, start, end=None, labels=None, tier=None):
        """[{labels, kind, tier, step, points: [[t, avg, max, min, n, sum], ...]}] for every matching series."""
        now = self.clock()
        end = now if end is None else end
        if not (_finite(start) and _finite(end)) or end < start:
            raise ValueError("invalid time range")
        if tier is None:
            tier, step = self.pick_tier(start, end, now)
        else:
            steps = {t: s for t, s, _ in TIERS}
            if tier not in steps:
                raise ValueError("unknown tier " + str(tier))
            step = steps[tier]
        out = []
        with self._lock:
            for sid, have, kind in self._matching(name, labels):
                if tier == "raw":
                    rows = self._db.execute("SELECT ts, value FROM raw WHERE series=? AND ts>=? AND ts<=? ORDER BY ts",
                                            (sid, start, end)).fetchall()
                    points = [[t, v, v, v, 1, v] for t, v in rows]
                else:
                    rows = self._db.execute("SELECT bucket, sum, count, min, max FROM agg WHERE tier=? AND series=? "
                                            "AND bucket>=? AND bucket<=? ORDER BY bucket",
                                            (tier, sid, int(start // step) * step, end)).fetchall()
                    points = [[b, s / c, mx, mn, c, s] for b, s, c, mn, mx in rows]
                out.append({"labels": have, "kind": kind, "tier": tier, "step": step, "points": points[-MAX_POINTS:]})
        return out

    def latest(self, name, labels=None, max_age=None):
        """[{labels, ts, value}] of the newest raw sample of every matching series (optionally not older than max_age)."""
        now = self.clock()
        out = []
        with self._lock:
            for sid, have, kind in self._matching(name, labels):
                row = self._db.execute("SELECT ts, value FROM raw WHERE series=? ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
                if row and (max_age is None or now - row[0] <= max_age):
                    out.append({"labels": have, "kind": kind, "ts": row[0], "value": row[1]})
        return out

    # -- events ---------------------------------------------------------------------------------------------------
    def event(self, category, severity, message, machine=None, data=None, ts=None):
        if severity not in SEVERITIES:
            raise ValueError("severity must be one of " + ", ".join(SEVERITIES))
        if not isinstance(category, str) or not category or not isinstance(message, str):
            raise ValueError("category and message required")
        payload = None if data is None else json.dumps(data, ensure_ascii=True, allow_nan=False, default=str)
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO events(ts, category, severity, machine, message, data) VALUES (?, ?, ?, ?, ?, ?)",
                (self.clock() if ts is None else ts, category, severity, machine, message[:2000], payload))
            return cursor.lastrowid

    def events(self, *, category=None, machine=None, severity=None, since=None, before=None, limit=200):
        clauses, args = [], []
        for column, value in (("category", category), ("machine", machine)):
            if value:
                clauses.append(column + "=?")
                args.append(value)
        if severity:
            allowed = SEVERITIES[SEVERITIES.index(severity):] if severity in SEVERITIES else (severity,)
            clauses.append("severity IN (%s)" % ",".join("?" * len(allowed)))
            args.extend(allowed)
        if since is not None:
            clauses.append("ts>=?")
            args.append(since)
        if before is not None:
            clauses.append("id<?")
            args.append(int(before))
        sql = "SELECT id, ts, category, severity, machine, message, data FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), MAX_EVENTS)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{"id": i, "ts": t, "category": c, "severity": s, "machine": m, "message": msg,
                 "data": None if d is None else json.loads(d)} for i, t, c, s, m, msg, d in rows]

    def clear_events(self, older_than_s=None):
        with self._lock:
            if older_than_s is None:
                return self._db.execute("DELETE FROM events").rowcount
            return self._db.execute("DELETE FROM events WHERE ts < ?", (self.clock() - older_than_s,)).rowcount

    # -- documents ------------------------------------------------------------------------------------------------
    def put(self, namespace, key, value):
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
        with self._lock:
            self._db.execute("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
                             "DO UPDATE SET ts=excluded.ts, value=excluded.value", (namespace, str(key), self.clock(), text))

    def get(self, namespace, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM docs WHERE namespace=? AND key=?", (namespace, str(key))).fetchone()
        return default if row is None else json.loads(row[0])

    def items(self, namespace, limit=None):
        """[(key, ts, value)] newest first."""
        sql = "SELECT key, ts, value FROM docs WHERE namespace=? ORDER BY ts DESC, key"
        args = [namespace]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            return [(k, t, json.loads(v)) for k, t, v in self._db.execute(sql, args).fetchall()]

    def delete(self, namespace, key=None):
        with self._lock:
            if key is None:
                return self._db.execute("DELETE FROM docs WHERE namespace=?", (namespace,)).rowcount
            return self._db.execute("DELETE FROM docs WHERE namespace=? AND key=?", (namespace, str(key))).rowcount

    # -- feature-owned tables -------------------------------------------------------------------------------------
    def ensure_table(self, ddl):
        """Create a feature-owned table/index (``CREATE ... IF NOT EXISTS`` statements only)."""
        if not ddl.lstrip().upper().startswith("CREATE"):
            raise ValueError("ensure_table takes CREATE statements")
        with self._lock:
            self._db.executescript(ddl)

    def execute(self, sql, args=()):
        """Run one statement on a feature-owned table under the store lock; returns all rows."""
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def transaction(self, statements):
        """Run [(sql, args), ...] atomically."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for sql, args in statements:
                    self._db.execute(sql, args)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise


class Baselines:
    """Counter -> delta conversion with persisted baselines (contract C3).

    ``delta(key, instance, value, ts)`` returns ``(delta, unobserved_s)``: the increase since the last sample of
    the same source instance, or ``(None, gap)`` when this is the first sample of a new instance or the counter
    went backwards (a restart): the new value becomes the baseline and the time since the last good sample is
    reported as unobserved, never as zero or negative work. Baselines live in the store's documents, so a portal
    restart continues from them (the time the portal was down is reported as unobserved)."""

    NAMESPACE = "telemetry/baseline"

    def __init__(self, store):
        self.store = store
        self._cache = {}
        self._lock = threading.Lock()

    def delta(self, key, instance, value, ts):
        if not _finite(value):
            return None, None
        with self._lock:
            last = self._cache.get(key)
            if last is None:
                last = self.store.get(self.NAMESPACE, key)
            self._cache[key] = {"instance": str(instance), "value": value, "ts": ts}
            self.store.put(self.NAMESPACE, key, self._cache[key])
        if last is None:
            return None, None
        gap = max(0.0, ts - last["ts"])
        if last["instance"] != str(instance) or value < last["value"]:
            return None, gap
        return value - last["value"], 0.0
