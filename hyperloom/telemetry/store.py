"""Persistent metrics history, event log and small document store (stdlib sqlite3).

Every metric name has one descriptor (``describe``): its kind, unit, sampling interval and, for states, a
legend. Kinds:

- ``gauge``: a level; read avg/min/max per bucket.
- ``delta``: a non-negative increment over the sample interval; read the bucket sum. Counters are never
  recorded directly: ``counter_delta`` turns them into deltas against a persisted baseline, in the same
  transaction as whatever the caller records from the delta (series points, ledger rows), so a failure never
  loses or double-counts work. A restart or a new source instance starts a new baseline; the interval between
  the last good sample and the new one is reported as unobserved, never as zero or negative work.
- ``state``: an enumeration stored as a numeric code; read the last value per bucket (never an average).

Every sample goes to the raw tier and is folded into coarser tiers (sum, count, min, max, last per bucket).
Buckets are anchored to wall time (bucket = floor(ts / step) * step), so history never shifts. A bucket without
samples does not exist: readers show a gap, never a zero. ``None`` and non-finite values are not recorded for
the same reason. Timestamps are UTC epoch seconds.
"""

import json
import math
from pathlib import Path
import sqlite3
import threading
import time


TIERS = (("raw", 2, 8 * 3600), ("m1", 60, 48 * 3600), ("m15", 900, 30 * 86400))
MAX_POINTS = 3000          # >= 30 days of 15 min buckets (2,880): a supported range is never truncated
MAX_EVENTS = 2000
SEVERITIES = ("info", "warning", "critical")
KINDS = ("gauge", "delta", "state")
BASELINES = "telemetry/baseline"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, kind TEXT NOT NULL, unit TEXT, interval_s REAL,
                                    legend TEXT);
CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, labels TEXT NOT NULL,
                                   UNIQUE(name, labels));
CREATE TABLE IF NOT EXISTS raw (series INTEGER NOT NULL, ts REAL NOT NULL, value REAL NOT NULL, interval REAL);
CREATE INDEX IF NOT EXISTS raw_series_ts ON raw(series, ts);
CREATE TABLE IF NOT EXISTS agg (tier TEXT NOT NULL, series INTEGER NOT NULL, bucket INTEGER NOT NULL,
                                sum REAL NOT NULL, count INTEGER NOT NULL, min REAL NOT NULL, max REAL NOT NULL,
                                last REAL NOT NULL, last_ts REAL NOT NULL, observed REAL NOT NULL DEFAULT 0,
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
        self._series = {}       # (name, labels) -> id; only committed ids
        self._metrics = {}      # name -> descriptor; only committed descriptors
        self._last_prune = 0.0

    def close(self):
        with self._lock:
            self._db.close()

    # -- transactions ---------------------------------------------------------------------------------------------
    def _atomic(self, work):
        """Run work(pending) in one transaction; caches learn new ids/descriptors only after COMMIT."""
        with self._lock:
            pending = {"series": {}, "metrics": {}}
            self._db.execute("BEGIN")
            try:
                result = work(pending)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._series.update(pending["series"])
            self._metrics.update(pending["metrics"])
            return result

    # -- descriptors ----------------------------------------------------------------------------------------------
    def _descriptor(self, name, pending):
        found = pending["metrics"].get(name) or self._metrics.get(name)
        if found is None:
            row = self._db.execute("SELECT kind, unit, interval_s, legend FROM metrics WHERE name=?", (name,)).fetchone()
            if row is not None:
                found = {"kind": row[0], "unit": row[1], "interval_s": row[2], "legend": json.loads(row[3]) if row[3] else None}
                self._metrics[name] = found
        return found

    def _describe(self, pending, name, kind, unit=None, interval=None, legend=None):
        if not isinstance(name, str) or not name:
            raise ValueError("series name required")
        if kind not in KINDS:
            raise ValueError("series kind must be one of " + ", ".join(KINDS))
        if interval is not None and not (_finite(interval) and interval > 0):
            raise ValueError("interval must be positive")
        if legend is not None and (kind != "state" or not isinstance(legend, dict)):
            raise ValueError("a legend maps state codes to names (state series only)")
        current = self._descriptor(name, pending)
        if current is not None and current["kind"] != kind:
            raise ValueError(f"series {name} is a {current['kind']}, not a {kind}")
        merged = {"kind": kind,
                  "unit": unit if unit is not None else (current or {}).get("unit"),
                  "interval_s": float(interval) if interval is not None else (current or {}).get("interval_s"),
                  "legend": {str(k): str(v) for k, v in legend.items()} if legend is not None else (current or {}).get("legend")}
        if merged != current:
            self._db.execute("INSERT INTO metrics(name, kind, unit, interval_s, legend) VALUES (?, ?, ?, ?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, unit=excluded.unit, "
                             "interval_s=excluded.interval_s, legend=excluded.legend",
                             (name, kind, merged["unit"], merged["interval_s"],
                              json.dumps(merged["legend"]) if merged["legend"] is not None else None))
            pending["metrics"][name] = merged
        return merged

    def describe(self, name, kind, unit=None, interval=None, legend=None):
        """Declare (or update the unit/interval/legend of) a metric; its kind can never change."""
        return dict(self._atomic(lambda pending: self._describe(pending, name, kind, unit, interval, legend)))

    def descriptor(self, name):
        with self._lock:
            found = self._descriptor(name, {"metrics": {}})
            return dict(found) if found else None

    # -- series ---------------------------------------------------------------------------------------------------
    def _series_id(self, pending, name, labels):
        key = (name, labels)
        sid = pending["series"].get(key) or self._series.get(key)
        if sid is None:
            self._db.execute("INSERT OR IGNORE INTO series(name, labels) VALUES (?, ?)", key)
            sid = self._db.execute("SELECT id FROM series WHERE name=? AND labels=?", key).fetchone()[0]
            pending["series"][key] = sid
        return sid

    def _rows(self, points):
        rows = []
        for point in points:
            name, labels, value = point[:3]
            kind = point[3] if len(point) > 3 else None
            if not isinstance(name, str) or not name:
                raise ValueError("series name required")
            if kind is not None and kind not in KINDS:
                raise ValueError("series kind must be one of " + ", ".join(KINDS))
            if isinstance(value, bool):
                value = int(value)
            if _finite(value):
                rows.append((name, canonical_labels(labels), float(value), kind))
        return rows

    def _insert(self, pending, rows, ts, interval=None):
        for name, labels, value, kind in rows:
            descriptor = self._descriptor(name, pending)
            if descriptor is None or (kind is not None and kind != descriptor["kind"]):
                descriptor = self._describe(pending, name, kind or "gauge")
            if descriptor["kind"] == "delta" and value < 0:
                raise ValueError("delta samples must be non-negative")
            sid = self._series_id(pending, name, labels)
            # The time this sample stands for is fixed when it is taken (the sampling interval then), so later
            # interval changes never rewrite the coverage of history.
            observed = interval if interval is not None else descriptor.get("interval_s") or 0.0
            self._db.execute("INSERT INTO raw(series, ts, value, interval) VALUES (?, ?, ?, ?)",
                             (sid, ts, value, observed or None))
            for tier, step, _ in TIERS[1:]:
                bucket = int(ts // step) * step
                self._db.execute(
                    "INSERT INTO agg(tier, series, bucket, sum, count, min, max, last, last_ts, observed) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?) ON CONFLICT(tier, series, bucket) DO UPDATE SET "
                    "sum=sum+excluded.sum, count=count+1, min=MIN(min, excluded.min), max=MAX(max, excluded.max), "
                    "last=CASE WHEN excluded.last_ts >= last_ts THEN excluded.last ELSE last END, "
                    "last_ts=MAX(last_ts, excluded.last_ts), observed=observed+excluded.observed",
                    (tier, sid, bucket, value, value, value, value, ts, observed))
        return len(rows)

    def record(self, points, ts=None, interval=None):
        """Record (name, labels, value[, kind]) points at ts (default now). The kind must match the metric's
        descriptor (an undescribed metric is described by its first point, default gauge). ``interval`` is the
        sampling interval these points stand for (the sampler passes its collector's current interval; default:
        the metric's described interval); it sets their coverage permanently."""
        ts = self.clock() if ts is None else float(ts)
        if interval is not None and not (_finite(interval) and interval > 0):
            raise ValueError("interval must be positive")
        rows = self._rows(points)
        if not rows:
            return 0
        count = self._atomic(lambda pending: self._insert(pending, rows, ts, interval))
        if ts - self._last_prune > 300:
            self.prune(ts)
        return count

    def counter_delta(self, key, instance, value, ts, *, max_gap=None, apply=None):
        """Advance the persisted baseline of a counter and account its increase atomically.

        Returns ``(delta, info)``. ``delta`` is None for the first sample of a source instance, for a counter
        that went backwards (restart) and for an out-of-order sample (``info["stale"]``). ``info`` holds
        ``unobserved_s`` (time with no attributable observation) and ``uncertain`` (the increase is complete but
        spread over a gap longer than ``max_gap``, so its placement in time is approximate). When ``apply`` is
        given, it is called as ``apply(delta, info)`` for a non-None delta and returns
        ``{"points": [(name, labels, value[, kind]), ...], "sql": [(statement, args), ...]}``; those writes and the
        baseline commit together or not at all."""
        if not _finite(value):
            return None, {"unobserved_s": None, "uncertain": False, "stale": False}
        ts = float(ts)

        def work(pending):
            row = self._db.execute("SELECT value FROM docs WHERE namespace=? AND key=?", (BASELINES, key)).fetchone()
            last = json.loads(row[0]) if row else None
            info = {"unobserved_s": 0.0, "uncertain": False, "stale": False}
            delta = None
            if last is not None and ts <= last["ts"]:
                info["stale"] = True
                return None, info
            if last is None:
                info["unobserved_s"] = None
            else:
                gap = ts - last["ts"]
                if last["instance"] != str(instance) or value < last["value"]:
                    info["unobserved_s"] = gap
                else:
                    delta = value - last["value"]
                    info["uncertain"] = max_gap is not None and gap > max_gap
            self._db.execute("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
                             "DO UPDATE SET ts=excluded.ts, value=excluded.value",
                             (BASELINES, key, ts, json.dumps({"instance": str(instance), "value": value, "ts": ts})))
            if delta is not None and apply is not None:
                writes = apply(delta, dict(info)) or {}
                self._insert(pending, self._rows(writes.get("points", ())), ts)
                for statement, args in writes.get("sql", ()):
                    self._db.execute(statement, args)
            return delta, info

        return self._atomic(work)

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
        for sid, raw in self._db.execute("SELECT id, labels FROM series WHERE name=?", (name,)):
            have = json.loads(raw)
            if all(have.get(k) == v for k, v in want.items()):
                found.append((sid, have))
        return found

    @staticmethod
    def pick_tier(start, end, now):
        """The finest tier that still holds `start` and returns at most MAX_POINTS buckets."""
        for tier, step, keep in TIERS:
            if start >= now - keep and (end - start) / step <= MAX_POINTS:
                return tier, step
        return TIERS[-1][0], TIERS[-1][1]

    def query(self, name, start, end=None, labels=None, tier=None):
        """{descriptor, lines: [{labels, points: [[t, avg, max, min, n, sum, last, coverage, cadence], ...]}], tier,
        step, truncated}. ``cadence`` is the sampling interval in force for that point (raw) or the mean interval of
        the bucket's samples, both as recorded at the time (None when unknown); charts use it to tell a gap from
        sparse sampling. ``coverage`` = observed seconds / step (capped at 1), from the interval each sample was taken
        at; None when no sample carried an interval and on the raw tier. ``truncated`` names the lines whose oldest buckets were cut at MAX_POINTS (never silent)."""
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
        lines, truncated = [], []
        with self._lock:
            descriptor = self._descriptor(name, {"metrics": {}})

            def coverage(observed):
                return None if not observed else min(1.0, observed / step)

            for sid, have in self._matching(name, labels):
                if tier == "raw":
                    rows = self._db.execute("SELECT ts, value, interval FROM raw WHERE series=? AND ts>=? AND ts<=? ORDER BY ts",
                                            (sid, start, end)).fetchall()
                    points = [[t, v, v, v, 1, v, v, None, i] for t, v, i in rows]
                else:
                    rows = self._db.execute("SELECT bucket, sum, count, min, max, last, observed FROM agg WHERE tier=? "
                                            "AND series=? AND bucket>=? AND bucket<=? ORDER BY bucket",
                                            (tier, sid, int(start // step) * step, end)).fetchall()
                    points = [[b, s / c, mx, mn, c, s, last, coverage(o), (o / c) if o else None]
                              for b, s, c, mn, mx, last, o in rows]
                if len(points) > MAX_POINTS:
                    truncated.append(have)
                    points = points[-MAX_POINTS:]
                lines.append({"labels": have, "points": points})
        return {"descriptor": dict(descriptor) if descriptor else None, "tier": tier, "step": step,
                "lines": lines, "truncated": truncated}

    def latest(self, name, labels=None, max_age=None):
        """[{labels, kind, ts, value}] of the newest raw sample of every matching series."""
        now = self.clock()
        out = []
        with self._lock:
            descriptor = self._descriptor(name, {"metrics": {}})
            for sid, have in self._matching(name, labels):
                row = self._db.execute("SELECT ts, value FROM raw WHERE series=? ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
                if row and (max_age is None or now - row[0] <= max_age):
                    out.append({"labels": have, "kind": (descriptor or {}).get("kind"), "ts": row[0], "value": row[1]})
        return out

    # -- events ---------------------------------------------------------------------------------------------------
    def event(self, category, severity, message, machine=None, data=None, ts=None):
        if severity not in SEVERITIES:
            raise ValueError("severity must be one of " + ", ".join(SEVERITIES))
        if not isinstance(category, str) or not category or not isinstance(message, str):
            raise ValueError("category and message required")
        payload = None if data is None else json.dumps(data, ensure_ascii=True, allow_nan=False, default=str)

        def work(_):
            cursor = self._db.execute(
                "INSERT INTO events(ts, category, severity, machine, message, data) VALUES (?, ?, ?, ?, ?, ?)",
                (self.clock() if ts is None else ts, category, severity, machine, message[:2000], payload))
            # Retention is enforced on the event path itself, independent of metric collection.
            self._db.execute("DELETE FROM events WHERE id <= ?", (cursor.lastrowid - MAX_EVENTS,))
            return cursor.lastrowid

        return self._atomic(work)

    def events(self, *, category=None, machine=None, severity=None, since=None, before=None, limit=200):
        clauses, args = [], []
        for column, value in (("category", category), ("machine", machine)):
            if value:
                clauses.append(column + "=?")
                args.append(value)
        if severity:
            if severity not in SEVERITIES:
                raise ValueError("severity must be one of " + ", ".join(SEVERITIES))
            allowed = SEVERITIES[SEVERITIES.index(severity):]
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
        def work(_):
            for sql, args in statements:
                self._db.execute(sql, args)
        self._atomic(work)


class Baselines:
    """Counter -> delta conversion (contract C3); a thin view over ``Store.counter_delta``, so every user of a
    key shares the one persisted baseline and its transaction."""

    def __init__(self, store):
        self.store = store

    def delta(self, key, instance, value, ts, *, max_gap=None, apply=None):
        return self.store.counter_delta(key, instance, value, ts, max_gap=max_gap, apply=apply)
