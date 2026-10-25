"""Token ledger (F11), hardware-ceiling meter (F12), RTX prefill panel (F13) and token value report (F47).

Ledger (contract C3). Additive hour records per (hour, profile, replica, machine, model, kind) of the engines' own
token counters (kinds ``generated``, ``prompt``, ``cached``, ``imported``), fed by the ``ledger`` collector every 10 s.

One scrape of one engine is one transaction. The collector passes the scrape time to ``Store.counter_delta`` as the
source's sample clock (instance ``clock``, value = time), so ``apply`` runs inside the store's transaction for every
scrape after the first and receives the exact interval (``delta`` = seconds since the previous scrape). Inside that
transaction ``apply`` reads the per-kind counter state (``ledger_counters``), the engine instance (``ledger_instances``)
and the latest reset, and returns every write of the scrape: the ledger increments of all four kinds for the same
interval, the disclosure rows (first observation, restart, decrease, collection gap, interval across a reset), the new
counter state and the ``ledger_tokens`` history points. A failure anywhere leaves nothing behind: the next scrape covers
the same interval again. Nothing is ever negative or counted twice.

``computed = prompt - cached - imported`` (C8) is derived per record when read. It is *measured* when prompt, cached and
imported were each reported over the record's whole scraped time (``span_s == record_s``), an *upper bound* when cached
or imported was missing for part of it, and *unavailable* when prompt was missing or the counters are inconsistent.

GET  ledger?range=&by=          KPIs, per-hour/day buckets, by-model table, insights, gaps, sources
GET  ledger/export?range=       {csv} (formula prefixes escaped)
POST ledger/reset {confirm: "reset"}                     audited; fenced inside every scrape transaction
POST ledger/settings {census_dir?, logs_dir?}            audited
GET  ledger/census              census-v1 artifacts (C9) checked against the running profiles
GET  ledger/rtx                 RTX prefill services + batch-run log listing + last parsed run
POST ledger/rtx-parse {id}      audited; parse the last strix-stream stats line of a listed run log
GET  ledger/value?range=        token value report from the price table
POST ledger/prices {...}        audited

Settings live in the store's documents: namespace ``prefs/ledger`` (census_dir, logs_dir with their directory identity,
prices); the display time zone is ``prefs/ui`` / ``timezone`` (default: the control machine's local zone). GET handlers
never create the store, never write and never open a file named by a request: directories come from the stored
settings, run logs are chosen by an id from our own listing, and every read is anchored to the configured directory.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import threading
import time

from hyperloom import telemetry


KINDS = ("generated", "prompt", "cached", "imported")
COUNTERS = {
    "generated": ("strix_generated_tokens_total", "mm_generated_tokens"),
    "prompt": ("strix_prompt_tokens_total", "mm_prompt_tokens"),
    "cached": ("strix_prompt_tokens_cached_total",),
    "imported": ("strix_prompt_tokens_imported_total",),
}
INTERVAL = 10.0
MAX_GAP = 30.0               # an increase spread over a longer gap is complete but its hour is approximate
START_SLACK = 2.5            # derived start times this close are the same process ...
MATURE_UPTIME = 10.0         # ... only once its previous sample was at least this old (a successor binds the port later)
ROUND_DEADLINE = 8.0
FETCH_DEADLINE, FETCH_BYTES = 2.0, 1024 * 1024
FLEET_STALE = 15.0            # three fleet collection intervals
MAX_PROFILES, MAX_REPLICAS = 32, 64
CLOCK = "clock"
PREFS = "prefs/ledger"
SOURCES = "ledger/sources"
BATCH = "ledger/rtx-batch"
FLEET_ENGINE = "fleet/engine"
LOOPBACK = socket.inet_ntoa(struct.pack("!I", socket.INADDR_LOOPBACK))
OBSERVER = "control machine, from the engines' own token counters"
NO_STORE = "telemetry store not open in this portal process (samplers disabled)"
RANGES = {"3600": 3600, "86400": 86400, "604800": 604800, "2592000": 2592000, "all": None}
HOURLY_MAX = 48 * 3600
MAX_DAY_BUCKETS = 400
MAX_ROWS = 400_000
CSV_ROWS = 100_000
CACHE_S = 5.0
SCAN_ENTRIES = 5000
LOOPBACK_NEEDS = "needs the fleet collector's engine-status reads (node host verb)"

DDL = """
CREATE TABLE IF NOT EXISTS ledger (hour INTEGER NOT NULL, profile TEXT NOT NULL, replica TEXT NOT NULL,
  machine TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL, tokens REAL NOT NULL DEFAULT 0,
  span_s REAL NOT NULL DEFAULT 0, record_s REAL NOT NULL DEFAULT 0, observed_s REAL NOT NULL DEFAULT 0,
  uncertain_tokens REAL NOT NULL DEFAULT 0, blind_s REAL NOT NULL DEFAULT 0, late_tokens REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(hour, profile, replica, machine, model, kind));
CREATE TABLE IF NOT EXISTS ledger_counters (source TEXT NOT NULL, kind TEXT NOT NULL, instance TEXT NOT NULL,
  value REAL NOT NULL, ts REAL NOT NULL, PRIMARY KEY(source, kind));
CREATE TABLE IF NOT EXISTS ledger_instances (source TEXT PRIMARY KEY, instance TEXT NOT NULL, start REAL,
  uptime REAL);
CREATE TABLE IF NOT EXISTS ledger_gaps (id INTEGER PRIMARY KEY AUTOINCREMENT, ident TEXT NOT NULL UNIQUE, start REAL,
  end REAL NOT NULL, profile TEXT, replica TEXT, machine TEXT, model TEXT, kind TEXT, reason TEXT NOT NULL,
  tokens REAL, instance TEXT);
CREATE INDEX IF NOT EXISTS ledger_gaps_end ON ledger_gaps(end);
CREATE TABLE IF NOT EXISTS ledger_unreachable (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
  profile TEXT, replica TEXT, machine TEXT, since REAL NOT NULL, until REAL, closed_by TEXT, why TEXT);
CREATE INDEX IF NOT EXISTS ledger_unreachable_source ON ledger_unreachable(source, until);
CREATE TABLE IF NOT EXISTS ledger_resets (at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_intervals (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL, observed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_intervals_source ON ledger_intervals(source, end);
CREATE INDEX IF NOT EXISTS ledger_intervals_end ON ledger_intervals(end);
CREATE TABLE IF NOT EXISTS ledger_rates (source TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
  tokens REAL NOT NULL, instance TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_rates_source ON ledger_rates(source, end);
CREATE TABLE IF NOT EXISTS ledger_listed (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, start REAL NOT NULL,
  end REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_listed_source ON ledger_listed(source, end);
"""
TABLES = ("ledger", "ledger_counters", "ledger_instances", "ledger_gaps", "ledger_unreachable", "ledger_resets",
          "ledger_intervals", "ledger_rates", "ledger_listed")
UPSERT = ("INSERT INTO ledger(hour, profile, replica, machine, model, kind, tokens, span_s, record_s, observed_s, "
          "uncertain_tokens, blind_s, late_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
          "ON CONFLICT(hour, profile, replica, machine, model, kind) DO UPDATE SET tokens=tokens+excluded.tokens, "
          "span_s=span_s+excluded.span_s, record_s=record_s+excluded.record_s, observed_s=observed_s+excluded.observed_s, "
          "uncertain_tokens=uncertain_tokens+excluded.uncertain_tokens, blind_s=blind_s+excluded.blind_s, "
          "late_tokens=late_tokens+excluded.late_tokens")
GAP = ("INSERT OR IGNORE INTO ledger_gaps(ident, start, end, profile, replica, machine, model, kind, reason, tokens, "
       "instance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
STATE = ("INSERT INTO ledger_counters(source, kind, instance, value, ts) VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, kind) "
         "DO UPDATE SET instance=excluded.instance, value=excluded.value, ts=excluded.ts")
INSTANCE = ("INSERT INTO ledger_instances(source, instance, start, uptime) VALUES (?, ?, ?, ?) ON CONFLICT(source) "
            "DO UPDATE SET instance=excluded.instance, start=excluded.start, uptime=excluded.uptime")
REACHED = "UPDATE ledger_unreachable SET until=?, closed_by='scrape' WHERE source=? AND until IS NULL"


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value):
    return _finite(value) and value >= 0


def _prepared(store):
    if not getattr(store, "_ledger_ready", False):
        store.ensure_table(DDL)
        store.describe("ledger_tokens", "delta", unit="tokens", interval=INTERVAL)
        store._ledger_ready = True
    return store


def _tables_exist(store):
    found = store.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ledger%'")
    return set(TABLES) <= {row[0] for row in found}


def _invalidate(store):
    if store is not None:
        store._ledger_cache = {}


# ---------------------------------------------------------------------------------------------------------------------
# Sources: running profiles and their engines (read-only)

def _states(api):
    """Launch records of the running profiles (the files `orchestrate.status()` reads; no ssh here)."""
    from hyperloom import orchestrate, secrets
    states = []
    for path in sorted(secrets.runtime_dir().glob("up-*.json"))[:MAX_PROFILES]:
        try:
            state = orchestrate._read(path)
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and isinstance(state.get("profile"), str):
            states.append(state)
    return states


def _adapter(family):
    from hyperloom import engines
    try:
        return engines.adapter(family)
    except (ValueError, KeyError):
        return None


def sources_of(state):
    """One source per router replica of a running profile (only rank 0 serves HTTP: a pair is one replica)."""
    profile = state["profile"]
    launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
    family = launch.get("family")
    adapter = _adapter(family)
    rendered = state.get("rendered_router") if isinstance(state.get("rendered_router"), dict) else {}
    machines = state.get("machines") if isinstance(state.get("machines"), dict) else {}
    out = []
    for replica in (rendered.get("replicas") or [])[:MAX_REPLICAS]:
        if not isinstance(replica, dict):
            continue
        rid = str(replica.get("id") or replica.get("node") or "0")[:64]
        node = replica.get("node") if isinstance(replica.get("node"), str) else None
        port = replica.get("port")
        source = {"key": f"{profile}/{rid}", "profile": profile, "replica": rid, "machine": node or "unknown",
                  "address": replica.get("address"), "port": port, "family": family,
                  "bind": adapter.bind if adapter else None, "keyed": bool(adapter and adapter.key_file),
                  "import_path": bool(adapter and "import-state" in adapter.capabilities),
                  "preset": launch.get("preset"), "invalid": None}
        recorded = (machines.get(node) or {}).get("http_address") if node else None
        if type(port) is not int or not 1024 <= port <= 65535:
            source["invalid"] = "replica port outside 1024..65535 in the launch record"
        elif source["bind"] is None:
            source["invalid"] = f"unknown engine family {family!r}"
        elif source["bind"] == "lan" and (not isinstance(source["address"], str) or source["address"] != recorded):
            source["invalid"] = "replica address differs from the launch record of its machine"
        out.append(source)
    return out


class FetchError(OSError):
    """An engine read that did not return a 2xx body (status None: no HTTP answer)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _fetch(address, port, path, text=False, key=None, deadline=None):
    """HTTP/1.1 GET on a raw socket: connect, send and every receive end by one absolute deadline (monotonic; at most
    2 s from now); headers at most 64 KiB, body at most 1 MiB; no proxy; a redirect is an error, never followed."""
    end = min(deadline if deadline is not None else math.inf, time.monotonic() + FETCH_DEADLINE)

    def left():
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("engine read exceeded its deadline")
        return remaining
    host = f"[{address}]" if ":" in address else address
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close", "Accept-Encoding: identity",
             "User-Agent: hyperloom-ledger"]
    if key:
        lines.append("Authorization: Bearer " + key)
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    with socket.create_connection((address, port), timeout=left()) as sock:
        sock.settimeout(left())
        sock.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data:
            if len(data) > 65536:
                raise ValueError("engine response headers exceed 64 KiB")
            sock.settimeout(left())
            chunk = sock.recv(65536)
            if not chunk:
                raise FetchError("engine closed the connection before its headers ended")
            data += chunk
        head, body = data.split(b"\r\n\r\n", 1)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
            raise FetchError("not an HTTP response")
        status = int(parts[1])
        headers = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if 300 <= status < 400:
            raise FetchError(f"HTTP {status}: redirects are not followed", status)
        if not 200 <= status < 300:
            raise FetchError(f"HTTP {status}", status)
        chunked = "chunked" in headers.get("transfer-encoding", "").lower()
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > FETCH_BYTES):
            raise ValueError("engine response exceeds 1 MiB" if length.isdigit() else "invalid content length")
        while length is None or len(body) < int(length):
            if len(body) > FETCH_BYTES + (65536 if chunked else 0):
                raise ValueError("engine response exceeds 1 MiB")
            sock.settimeout(left())
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    if length is not None:
        if len(body) < int(length):
            raise FetchError("engine response ended early")
        body = body[:int(length)]
    elif chunked:
        decoded, rest = b"", body
        while True:
            size_line, sep, rest = rest.partition(b"\r\n")
            if not sep:
                raise FetchError("truncated chunked response")
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                break
            decoded += rest[:size]
            rest = rest[size + 2:]
            if len(decoded) > FETCH_BYTES:
                raise ValueError("engine response exceeds 1 MiB")
        body = decoded
    if len(body) > FETCH_BYTES:
        raise ValueError("engine response exceeds 1 MiB")
    text_body = body.decode("utf-8")
    return text_body if text else json.loads(text_body)


class KeyUnavailable(OSError):
    pass


def _key(source):
    """The engine's key for a keyed LAN engine (C4), from the secret store; never returned, logged or audited."""
    if not source["keyed"]:
        return None
    try:
        from hyperloom import orchestrate, secrets
        value = secrets.get(orchestrate._secret_name(source["profile"], source["machine"]))
    except Exception:
        value = None
    if not value:
        raise KeyUnavailable("engine key unavailable in the secret store (keyed engines are read with their key)")
    return value


def _get(source, path, text=False, deadline=None):
    return _fetch(source["address"], source["port"], path, text=text, key=_key(source), deadline=deadline)


def _why(exc, what):
    if isinstance(exc, KeyUnavailable):
        return str(exc)
    if isinstance(exc, FetchError) and exc.status == 401:
        return f"engine {what}: the engine refused its key (HTTP 401)"
    return f"engine {what} unavailable: {type(exc).__name__}: {str(exc)[:200]}"


def _identity(status):
    """(explicit id, uptime) of one status reply."""
    if not isinstance(status, dict):
        return None, None
    explicit = status.get("instance_id")
    explicit = explicit if isinstance(explicit, str) and 0 < len(explicit) <= 128 else None
    uptime = status.get("uptime_seconds")
    return explicit, (float(uptime) if _count(uptime) else None)


def _fleet(store, source, now):
    """(observation, why) from the fleet collector's loopback engine document (its key, fields and bracket outcome)."""
    record = store.get(FLEET_ENGINE, f"{source['machine']}:{source['profile']}:{source['port']}")
    if not isinstance(record, dict):
        return None, LOOPBACK_NEEDS
    if (record.get("machine"), record.get("profile"), record.get("port")) != (source["machine"], source["profile"], source["port"]):
        return None, "fleet engine-status document names another engine"
    if record.get("replica") is not None and str(record.get("replica")) != source["replica"]:
        return None, "fleet engine-status document names another replica"
    ts = record.get("ts")
    if not _finite(ts) or now - ts > FLEET_STALE or ts > now + 5:
        return None, f"fleet engine-status read is stale (older than {FLEET_STALE:.0f} s)"
    text = record.get("metrics_text")
    if not isinstance(text, str):
        return None, "engine /metrics text unavailable in the fleet engine-status read"
    status = record.get("status") if isinstance(record.get("status"), dict) else None
    instance = record.get("instance")
    # The fleet collector brackets identity (status, metrics, status) and sets `instance` only when both status reads
    # name the same process; anything else is an observation without identity evidence.
    verified = isinstance(instance, str) and 0 < len(instance) <= 160
    return {"status": status, "metrics": text, "ts": float(ts), "explicit": ("fleet:" + instance) if verified else None,
            "uptime": None, "bracketed": verified}, None


def observe(api, store, source, now, deadline):
    """(observation, why): counters and engine identity from one coherent acquisition.

    LAN engines are read as /api/status, /metrics, /api/status. The two status replies must name the same process:
    equal explicit ids; or, without ids, uptimes whose derived starts agree within 2.5 s, a grown uptime and a first
    uptime of at least 10 s (a process started between the reads would report a smaller uptime than the first read).
    Otherwise the observation is refused for this round. Without status replies the counters carry no identity."""
    if source["invalid"]:
        return None, source["invalid"]
    if source["bind"] == "loopback":
        return _fleet(store, source, now)
    try:
        first = _get(source, "/api/status", deadline=deadline)
    except KeyUnavailable as exc:
        return None, str(exc)
    except (OSError, ValueError) as exc:
        first = exc
    begun = api.clock()
    try:
        text = _get(source, "/metrics", text=True, deadline=deadline)
    except (OSError, ValueError) as exc:
        return None, _why(exc, "/metrics")
    ts = api.clock()
    if isinstance(first, Exception):
        return {"status": None, "metrics": text, "ts": ts, "explicit": None, "uptime": None, "bracketed": False}, None
    try:
        second = _get(source, "/api/status", deadline=deadline)
    except (OSError, ValueError) as exc:
        return None, _why(exc, "/api/status (second read of the identity bracket)")
    (id1, up1), (id2, up2) = _identity(first), _identity(second)
    end = api.clock()
    if id1 or id2:
        if id1 != id2:
            return None, "engine identity changed while reading (restart in progress); read again next round"
        return {"status": second, "metrics": text, "ts": ts, "explicit": id2, "uptime": None, "bracketed": True}, None
    if up1 is None or up2 is None:
        return {"status": second if isinstance(second, dict) else None, "metrics": text, "ts": ts, "explicit": None,
                "uptime": None, "bracketed": False}, None
    if up1 < MATURE_UPTIME:
        return None, f"engine younger than {MATURE_UPTIME:.0f} s: identity not yet provable; read again next round"
    if not (up2 >= up1 and abs((begun - up1) - (end - up2)) <= START_SLACK):
        return None, "engine identity changed while reading (restart in progress); read again next round"
    return {"status": second, "metrics": text, "ts": ts, "explicit": None, "uptime": up2 - (end - ts),
            "bracketed": True}, None


def counters_of(observation):
    from hyperloom.adapters import prom
    samples = prom.parse(observation["metrics"])
    counters = {}
    for kind, names in COUNTERS.items():
        for name in names:
            value = samples.get((name, ()))
            if _count(value):
                counters[kind] = value
                break
    totals = (observation["status"] or {}).get("totals")
    imported = totals.get("imported_tokens") if isinstance(totals, dict) else None
    if "imported" not in counters and _count(imported):
        counters["imported"] = imported
    return counters


def scrape_writes(store, source, model, counters, observation, ts, gap, uncertain):
    """Every write of one scrape, decided inside its transaction (called from `apply`).

    Returns (writes, instance, generated delta when it is a valid certain interval of this instance, else None)."""
    key, start = source["key"], ts - gap
    hour = int(ts // 3600) * 3600
    names = (source["profile"], source["replica"], source["machine"], model)
    reset_at = store.execute("SELECT MAX(at) FROM ledger_resets")[0][0]
    reset_at = -1.0 if reset_at is None else reset_at
    sql, points, gaps = [], [], []
    # Engine instance (C3): an explicit id; else the derived start, kept only with evidence it is the same process.
    row = store.execute("SELECT instance, start, uptime FROM ledger_instances WHERE source=?", (key,))
    previous = row[0] if row else None
    blind = False
    if observation["explicit"]:
        instance = observation["explicit"] if observation["explicit"].startswith("fleet:") else "id:" + observation["explicit"]
    elif observation["uptime"] is not None and observation["bracketed"]:
        born = ts - observation["uptime"]
        same = (previous is not None and previous[1] is not None and abs(previous[1] - born) <= START_SLACK
                and previous[2] is not None and observation["uptime"] > previous[2] and previous[2] >= MATURE_UPTIME)
        instance = previous[0] if same else "start:%.3f" % born
        sql.append((INSTANCE, (key, instance, born, observation["uptime"])))
    else:
        instance, blind = "unknown", True
        sql.append((INSTANCE, (key, instance, None, None)))
    state = {kind: (inst, value, at) for kind, inst, value, at in
             store.execute("SELECT kind, instance, value, ts FROM ledger_counters WHERE source=?", (key,))}
    if not state:
        gap, start = 0.0, ts                      # the first scrape of a source covers no interval yet
    fenced = start < reset_at                     # the interval began before the latest reset
    record_s = 0.0 if fenced else gap
    rows, generated, broken = {}, None, False
    for kind in KINDS:
        value = counters.get(kind)
        tokens = span = late = 0.0
        if value is not None:
            before = state.get(kind)
            reason = since = None
            if before is None:
                reason = "first observation of this engine counter"
            elif before[0] != instance:
                reason, since = "engine restarted (new instance)", before[2]
            elif value < before[1]:
                reason, since = "counter decreased (engine restarted)", before[2]
            if reason:
                gaps.append((f"{key}|{kind}|{instance}|{before[2] if before else 0}", since, ts, kind, reason, value))
                broken = broken or kind == "generated"
            else:
                delta = value - before[1]
                if fenced or before[2] < reset_at:
                    gaps.append((f"{key}|{kind}|reset|{before[2]}", before[2], ts, kind,
                                 "interval before or across a ledger reset (not counted)", delta))
                    broken = broken or kind == "generated"
                elif abs(before[2] - start) > 1e-6:
                    # This counter missed the previous scrape: its increase covers a longer interval than the
                    # others, so it is kept apart (never subtracted from or compared with the aligned counters).
                    gaps.append((f"{key}|{kind}|late|{before[2]}", before[2], ts, kind,
                                 "counter missing at the previous scrape: increase kept apart, placed in this hour", delta))
                    late = delta
                    broken = broken or kind == "generated"
                else:
                    tokens, span = delta, gap
                    if uncertain:
                        gaps.append((f"{key}|{kind}|gap|{start}", start, ts, kind,
                                     "collection gap: the increase is complete, its hour approximate", delta))
                        broken = broken or kind == "generated"
                    elif kind == "generated" and gap > 0:
                        generated = delta
                if tokens or late:
                    points.append(("ledger_tokens", {"kind": kind, "model": model, "machine": source["machine"],
                                                     "profile": source["profile"], "replica": source["replica"]},
                                   tokens + late, "delta"))
            sql.append((STATE, (key, kind, instance, float(value), ts)))
        elif kind == "imported" and not source["import_path"] and "prompt" in counters:
            span = gap                             # this engine family has no import path: a known zero
        rows[kind] = (tokens, span, late)
    # Observed (coverage) = the interval when the generated counter gave a valid, certain delta for it.
    observed = rows["generated"][1] if not uncertain else 0.0
    for kind, (tokens, span, late) in rows.items():
        if not fenced and (record_s or tokens or late):
            sql.append((UPSERT, (hour, *names, kind, float(tokens), float(span), record_s, observed,
                                 float(tokens) if uncertain else 0.0, record_s if blind else 0.0, float(late))))
    for ident, since, end, kind, reason, tokens in gaps:
        if end >= reset_at:
            sql.append((GAP, (ident, since, end, *names, kind, reason, float(tokens), instance)))
    sql.append((REACHED, (ts, key)))
    # Durable scrape interval (coverage): observed when the generated counter gave a valid, certain delta for it.
    if gap > 0 and not fenced:
        valid = 1 if rows["generated"][1] > 0 and not uncertain else 0
        last = store.execute("SELECT id, end, observed FROM ledger_intervals WHERE source=? ORDER BY end DESC LIMIT 1", (key,))
        if last and abs(last[0][1] - start) <= 1e-6 and last[0][2] == valid:
            sql.append(("UPDATE ledger_intervals SET end=? WHERE id=?", (ts, last[0][0])))
        else:
            sql.append(("INSERT INTO ledger_intervals(source, start, end, observed) VALUES (?, ?, ?, ?)",
                        (key, start, ts, valid)))
    # Live-rate entries: valid certain generated intervals of an identified instance, only; any discontinuity of the
    # generated counter (restart, decrease, late, gap, reset) removes the older ones, in this same transaction.
    if broken or blind or "generated" not in counters:
        sql.append(("DELETE FROM ledger_rates WHERE source=?", (key,)))
    elif generated is not None:
        sql.append(("DELETE FROM ledger_rates WHERE source=? AND (instance<>? OR end<?)", (key, instance, ts - 300)))
        sql.append(("INSERT INTO ledger_rates(source, start, end, tokens, instance) VALUES (?, ?, ?, ?, ?)",
                    (key, start, ts, float(generated), instance)))
    return {"points": points, "sql": sql}, instance, (None if blind else generated)


def account_scrape(store, source, model, counters, observation):
    """Account one scrape atomically; returns (instance or None when the sample is stale, live-rate entry or None)."""
    ts = observation["ts"]
    clock_key = f"ledger/{source['key']}/{CLOCK}"
    if store.get("telemetry/baseline", clock_key) is None:
        store.counter_delta(clock_key, CLOCK, ts - 1e-3, ts - 1e-3)   # starts this source's sample clock
    result = {}

    def apply(delta, info):
        writes, result["instance"], result["generated"] = scrape_writes(
            store, source, model, counters, observation, ts, delta, bool(info.get("uncertain")))
        result["gap"] = delta
        return writes

    delta, _ = store.counter_delta(clock_key, CLOCK, ts, ts, max_gap=MAX_GAP, apply=apply)
    return result.get("instance") if delta is not None else None


_rounds = {"n": 0}


def collect(api, now):
    """The `ledger` collector: one bounded round over every running engine (all writes happen in `account_scrape`)."""
    store = _prepared(telemetry.open_store(api))
    sources = [source for state in _states(api) for source in sources_of(state)]
    _reconcile(store, {source["key"] for source in sources}, now)
    _mark_listed(store, [source["key"] for source in sources], now)
    if sources:
        offset = _rounds["n"] % len(sources)
        sources = sources[offset:] + sources[:offset]
    _rounds["n"] += 1
    deadline = time.monotonic() + ROUND_DEADLINE
    failures = []
    for index, source in enumerate(sources):
        if time.monotonic() >= deadline:
            for late in sources[index:]:
                _note(store, late, now, "round deadline reached; read in a later round", unreachable=False)
            break
        try:
            _collect_source(api, store, source, now, deadline)
        except Exception as exc:  # one failing source never stops the others; nothing of its scrape was written
            failures.append(f"{source['key']}: {type(exc).__name__}: {exc}"[:300])
            _note(store, source, now, failures[-1], unreachable=False)
    _invalidate(store)
    if failures:
        raise RuntimeError("ledger: " + "; ".join(failures)[:600])
    return []


def _mark_listed(store, keys, now):
    """Every round, each engine a launch record lists is expected: its listed lifetime grows by the round, whatever
    its read gives (skipped by the round deadline, unreachable, refused or counted)."""
    statements = []
    for key in keys:
        row = store.execute("SELECT id, end FROM ledger_listed WHERE source=? ORDER BY end DESC LIMIT 1", (key,))
        if row and 0 <= now - row[0][1] <= 3 * INTERVAL:
            statements.append(("UPDATE ledger_listed SET end=? WHERE id=?", (now, row[0][0])))
        elif not row or now > row[0][1]:
            statements.append(("INSERT INTO ledger_listed(source, start, end) VALUES (?, ?, ?)", (key, now, now)))
    if statements:
        store.transaction(statements)


def _reconcile(store, listed, now):
    """Close the open unreachable intervals of engines no longer listed by any launch record (stopped profiles): the
    interval ends at the last round that still listed the engine."""
    for row_id, source in store.execute("SELECT id, source FROM ledger_unreachable WHERE until IS NULL"):
        if source not in listed:
            last = (store.get(SOURCES, source) or {}).get("last_try")
            store.execute("UPDATE ledger_unreachable SET until=?, closed_by='removed' WHERE id=?",
                          (last if _finite(last) else now, row_id))


def _note(store, source, now, error, unreachable=True):
    try:
        previous = store.get(SOURCES, source["key"]) or {}
        store.put(SOURCES, source["key"], {**previous, **{k: source[k] for k in ("profile", "replica", "machine", "family")},
                                           "last_try": now, "error": error})
        if unreachable and not store.execute("SELECT 1 FROM ledger_unreachable WHERE source=? AND until IS NULL",
                                             (source["key"],)):
            last = store.execute("SELECT MAX(ts) FROM ledger_counters WHERE source=?", (source["key"],))[0][0]
            store.transaction([("INSERT INTO ledger_unreachable(source, profile, replica, machine, since, until, why) "
                                "VALUES (?, ?, ?, ?, ?, NULL, ?)", (source["key"], source["profile"], source["replica"],
                                                                     source["machine"], last if last else now, error[:300]))])
    except Exception:
        pass


def _collect_source(api, store, source, now, deadline):
    observation, why = observe(api, store, source, now, deadline)
    previous = store.get(SOURCES, source["key"]) or {}
    record = {k: source[k] for k in ("profile", "replica", "machine", "family", "import_path")}
    record.update(last_try=now, last_ok=previous.get("last_ok"), instance=previous.get("instance"),
                  reported=previous.get("reported") or [], model=previous.get("model"),
                  observer="loopback engine via the fleet collector" if source["bind"] == "loopback" else OBSERVER,
                  identity=previous.get("identity"), error=why)
    if observation is None:
        store.put(SOURCES, source["key"], record)
        _note(store, source, now, why)
        return
    try:
        counters = counters_of(observation)
    except ValueError as exc:
        _note(store, source, now, f"engine /metrics unreadable: {exc}")
        return
    status = observation["status"] or {}
    model = ((status.get("model") if isinstance(status.get("model"), str) else None) or source["preset"]
             or source["profile"])[:128]
    record.update(model=model, reported=sorted(counters))
    if not counters:
        record["error"] = "engine reports no token counters"
        store.put(SOURCES, source["key"], record)
        return
    instance = account_scrape(store, source, model, counters, observation)
    if instance is not None:
        record.update(last_ok=now, instance=instance,
                      identity="engine instance id" if instance.startswith("id:") else
                      "fleet collector's bracketed identity" if instance.startswith("fleet:") else
                      "derived start time (bracketed status reads)" if instance.startswith("start:") else
                      "none: restarts detected by counter decrease only")
    store.put(SOURCES, source["key"], record)


# ---------------------------------------------------------------------------------------------------------------------
# Time zone and ranges

def zone_of(api):
    """(tzinfo or None for the control machine's local zone, name, source text)."""
    store = telemetry.existing_store(api)
    name = None
    if store is not None:
        try:
            name = store.get("prefs/ui", "timezone")
        except Exception:
            name = None
    local = datetime.now().astimezone().tzname()
    if isinstance(name, str) and name and len(name) <= 64:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name, "configured time zone"
        except Exception:
            return None, local, f"configured zone {name[:64]!r} unknown here; control machine local zone used"
    return None, local, "control machine local zone (no zone configured)"


def _local(ts, zone):
    return datetime.fromtimestamp(ts, zone) if zone is not None else datetime.fromtimestamp(ts).astimezone()


def _range(query, now, store, allowed=("range", "by")):
    unknown = [k for k in query if k not in allowed]
    if unknown:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(unknown)))
    text = str(query.get("range", "86400"))
    if text not in RANGES:
        raise ValueError("range must be one of 3600, 86400, 604800, 2592000, all")
    span = RANGES[text]
    if span is None:
        first = None
        if store is not None and _tables_exist(store):
            first = store.execute("SELECT MIN(m) FROM (SELECT MIN(hour) AS m FROM ledger UNION ALL SELECT MIN(start) FROM "
                                  "ledger_intervals UNION ALL SELECT MIN(since) FROM ledger_unreachable)")[0][0]
        start = int(first // 3600) * 3600 if first is not None else int(now // 3600) * 3600
    else:
        start = int((now - span) // 3600) * 3600
    return text, start, now


# ---------------------------------------------------------------------------------------------------------------------
# Ledger records and documents

def _records(store, start, end):
    """(hour records with the derived computed prefill and its tag, rows truncated).

    Per kind: ``<kind>`` = every counted token (aligned + late), ``aligned_<kind>`` = increments over the record's
    common scrape intervals, ``late_<kind>`` = increments of a counter that missed a scrape (kept apart).
    ``computed`` uses aligned increments only: measured when prompt, cached and imported cover the whole record,
    an upper bound when cached or imported cover only part of it, unavailable when prompt does not."""
    rows = store.execute("SELECT hour, profile, replica, machine, model, kind, tokens, span_s, record_s, observed_s, "
                         "uncertain_tokens, blind_s, late_tokens FROM ledger WHERE hour >= ? AND hour <= ? "
                         "ORDER BY hour DESC LIMIT ?", (start, end, MAX_ROWS + 1))
    truncated = len(rows) > MAX_ROWS
    grouped = {}
    for hour, profile, replica, machine, model, kind, tokens, span, record_s, observed, uncertain, blind, late in rows[:MAX_ROWS]:
        item = grouped.setdefault((hour, profile, replica, machine, model), {
            "hour": hour, "profile": profile, "replica": replica, "machine": machine, "model": model, "rows": {}})
        item["rows"][kind] = (tokens, span, record_s, observed, uncertain, blind, late)
    out = []
    for item in grouped.values():
        rows_ = item.pop("rows")
        record_s = max(r[2] for r in rows_.values())
        item.update(record_s=record_s, observed_s=max(r[3] for r in rows_.values()),
                    blind_s=max(r[5] for r in rows_.values()),
                    uncertain=sum(rows_[k][4] + rows_[k][6] for k in ("generated", "prompt") if k in rows_))
        full, partial = {}, {}
        for kind in KINDS:
            tokens, span, late = (rows_[kind][0], rows_[kind][1], rows_[kind][6]) if kind in rows_ else (0.0, 0.0, 0.0)
            item["aligned_" + kind], item["late_" + kind], item[kind] = tokens, late, tokens + late
            full[kind] = record_s > 0 and abs(span - record_s) <= 1e-3
            partial[kind] = not full[kind] and (span > 0 or late > 0)
            item["has_" + kind] = full[kind] or partial[kind]
        item["full"], item["partial"] = full, partial
        item["computed"], item["computed_tag"], item["computed_why"] = None, "unavailable", None
        raw = item["aligned_prompt"] - item["aligned_cached"] - item["aligned_imported"]
        bounded = [k for k in ("cached", "imported") if not full[k]]
        if record_s == 0:
            item["computed_why"] = "no scrape interval in this hour"
        elif not full["prompt"]:
            item["computed_why"] = "input not reported for the whole hour record"
        elif raw < 0:
            item["computed_why"] = "cached plus imported exceed input (inconsistent counters)"
        else:
            item["computed"] = raw
            item["computed_tag"] = "measured" if not bounded else "estimated"
            if bounded:
                item["computed_why"] = "upper bound: " + " and ".join(bounded) + " not reported for the whole record"
        # Input whose computed share is unknown: all of it when computed is unavailable, else the late increments.
        item["computed_unavailable_input"] = item["prompt"] if item["computed"] is None else item["late_prompt"]
        out.append(item)
    out.sort(key=lambda r: r["hour"])
    return out, truncated


def _gaps(store, start, end, limit=50):
    rows = store.execute("SELECT start, end, profile, replica, machine, model, kind, reason, tokens, instance "
                         "FROM ledger_gaps WHERE end >= ? AND end <= ? ORDER BY end DESC, id DESC LIMIT 2000",
                         (start, end + 1))
    merged = {}
    for s, e, profile, replica, machine, model, kind, reason, tokens, instance in rows:
        item = merged.setdefault((e, profile, replica, reason), {"start": s, "end": e, "profile": profile,
                                                                 "replica": replica, "machine": machine, "model": model,
                                                                 "reason": reason, "instance": instance, "counts": {}})
        item["counts"][kind] = tokens
    for source, profile, replica, machine, since, until, why in store.execute(
            "SELECT source, profile, replica, machine, since, until, why FROM ledger_unreachable "
            "WHERE since <= ? AND (until IS NULL OR until >= ?) ORDER BY since DESC LIMIT 500", (end, start)):
        merged[(until or end, profile, replica, "unreachable", since)] = {
            "start": since, "end": until, "profile": profile, "replica": replica, "machine": machine, "model": None,
            "reason": "engine unreachable" + ("" if until else " (still)") + (f": {why}" if why else ""),
            "instance": None, "counts": {}}
    gaps = sorted(merged.values(), key=lambda g: g["end"] if g["end"] is not None else end, reverse=True)
    return gaps[:limit], len(gaps), gaps


def _share(part, whole):
    return part / whole if whole else None


def _sources(store):
    out = []
    for key, _, value in store.items(SOURCES, limit=200):
        if not isinstance(value, dict):
            continue
        reported = set(value.get("reported") or [])
        if not value.get("import_path") and "prompt" in reported:
            reported.add("imported")
        missing = [k for k in ("cached", "imported") if k not in reported]
        limits = []
        if missing:
            limits.append("computed prefill is an upper bound (" + " and ".join(missing) + " not reported)")
        if value.get("instance") == "unknown":
            limits.append("no instance identity: restarts detected by counter decrease only")
        out.append({k: value.get(k) for k in ("profile", "replica", "machine", "family", "model", "last_ok", "last_try",
                                              "error", "instance", "observer", "identity")}
                   | {"key": key, "reported": sorted(value.get("reported") or []), "not_reported": missing,
                      "limits": limits})
    return sorted(out, key=lambda s: s["key"])


def _offset_note(zone, now):
    offset = _local(now, zone).utcoffset()
    if offset is not None and offset.total_seconds() % 3600:
        return "local boundaries rounded to the UTC hour (this zone's offset is not a whole hour)"
    return None


def ledger(api, query):
    store = telemetry.existing_store(api)
    now = api.clock()
    zone, zone_name, zone_source = zone_of(api)
    by = query.get("by", "kind")
    if by not in ("kind", "model", "machine"):
        raise ValueError("by must be kind, model or machine")
    text, start, end = _range(query, now, store)
    base = {"range": text, "by": by, "timezone": zone_name, "timezone_iana": zone_name if zone is not None else None,
            "timezone_source": zone_source, "start": start, "end": end,
            "range_note": "whole UTC hours from the start shown (the first hour may begin before the nominal range)",
            "observer": OBSERVER}
    if store is None or not _tables_exist(store):
        why = NO_STORE if store is None else "no ledger records yet (the ledger collector has not counted any engine)"
        fields = dict.fromkeys(("kpis", "buckets", "models", "gaps", "coverage", "sources", "insights"))
        if store is not None:
            fields["sources"] = _sources(store)
        return api.document("ledger", **base, **fields, why={k: why for k in fields if fields[k] is None})
    lock = store.__dict__.setdefault("_ledger_lock", threading.Lock())
    with lock:                                       # concurrent GETs wait for one computation and share it
        cache = getattr(store, "_ledger_cache", None)
        if cache is None:
            store._ledger_cache = cache = {}
        key = ("ledger", text, by, zone_name, zone_source, int(now // CACHE_S))
        if key in cache:
            return {**cache[key], "server_time": now}
        records, truncated = _records(store, start, end)
        document = api.document("ledger", **base, **summarize(store, records, zone, by, start, end, now, truncated))
        if len(cache) > 16:
            cache.clear()
        cache[key] = document
        return document


SPAN_ROWS = 100_000


def _merge(items):
    out = []
    for lo, hi in sorted(items):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _spans(store, start, end, now):
    """{source: (expected, observed)} merged interval lists, clipped to the range and to the latest reset.

    Expected = every committed scrape interval of a listed engine plus every interval it was listed but unreadable
    (open, closed by its recovery, or closed when it stopped being listed); observed = the scrape intervals with a
    valid, certain generated-counter delta. Both come from durable tables independent of chart retention."""
    reset_at = store.execute("SELECT MAX(at) FROM ledger_resets")[0][0]
    lo_bound = max(start, -1.0 if reset_at is None else reset_at)
    expected, observed, truncated = {}, {}, []
    rows = store.execute("SELECT source, start, end, observed FROM ledger_intervals WHERE end >= ? AND start <= ? "
                         "ORDER BY end DESC LIMIT ?", (lo_bound, end, SPAN_ROWS + 1))
    if len(rows) > SPAN_ROWS:
        truncated.append("scrape intervals")
    listed = store.execute("SELECT source, start, end FROM ledger_listed WHERE end >= ? AND start <= ? ORDER BY end DESC "
                           "LIMIT ?", (lo_bound, end, SPAN_ROWS + 1))
    if len(listed) > SPAN_ROWS:
        truncated.append("listed lifetimes")
    for source, lo, hi in listed[:SPAN_ROWS]:
        lo, hi = max(lo, lo_bound), min(hi, end)
        if hi > lo:
            expected.setdefault(source, []).append((lo, hi))
    for source, lo, hi, valid in rows[:SPAN_ROWS]:
        lo, hi = max(lo, lo_bound), min(hi, end)
        if hi > lo:
            expected.setdefault(source, []).append((lo, hi))
            if valid:
                observed.setdefault(source, []).append((lo, hi))
    unreachable = store.execute("SELECT source, since, until FROM ledger_unreachable WHERE since <= ? AND "
                                "(until IS NULL OR until >= ?) ORDER BY since DESC LIMIT ?", (end, lo_bound, SPAN_ROWS + 1))
    if len(unreachable) > SPAN_ROWS:
        truncated.append("unreachable intervals")
    for source, since, until in unreachable[:SPAN_ROWS]:
        lo, hi = max(since, lo_bound), min(until if until is not None else now, end)
        if hi > lo:
            expected.setdefault(source, []).append((lo, hi))
    spans = {source: (_merge(items), _merge(observed.get(source, []))) for source, items in expected.items()}
    return spans, truncated


def _overlap(intervals, lo, hi):
    return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in intervals)


def summarize(store, records, zone, by, start, end, now, truncated=False):
    gaps, gap_count, all_gaps = _gaps(store, start, end)
    sources = _sources(store)
    hourly = (end - start) <= HOURLY_MAX
    total = lambda k, rows=records: sum(r[k] for r in rows)
    generated, inputs, uncertain = total("generated"), total("prompt"), total("uncertain")
    # Completeness is carried by the flags, never inferred from amounts: a record whose input or output counter was
    # missing for part of it makes the totals partial and stays out of every ratio.
    complete = [r for r in records if r["full"]["prompt"] and r["full"]["generated"]]
    input_partial = any(not r["full"]["prompt"] for r in records)
    output_partial = any(not r["full"]["generated"] for r in records)
    measured = sum(r["computed"] for r in records if r["computed_tag"] == "measured")
    estimated = sum(r["computed"] for r in records if r["computed_tag"] == "estimated")
    unavailable_input = total("computed_unavailable_input")
    computed_missing = sum(1 for r in records if r["computed"] is None)
    cached_known = [r for r in records if r["has_cached"]]
    cached_full = [r for r in records if r["full"]["cached"] and r["full"]["prompt"]]
    imported_known = [r for r in records if r["has_imported"]]
    blind_tokens = sum(r["prompt"] + r["generated"] for r in records if r["blind_s"] > 0)
    cached = total("cached", cached_known)
    grand = inputs + generated
    label_of = lambda ts: _local(ts, zone).strftime("%Y-%m-%d %H:00" if hourly else "%Y-%m-%d")
    buckets, days = {}, {}
    drawn_from = start if hourly else max(start, end - MAX_DAY_BUCKETS * 86400)

    def bucket_for(hour):
        label = label_of(hour)
        bucket = buckets.setdefault(label, {"label": label, "start": hour, "end": hour + 3600, "values": {},
                                            "unavailable": [], "partial": False, "tokens": 0.0, "observed_s": 0.0,
                                            "expected_s": 0.0, "estimated": 0.0, "unavailable_input": 0.0,
                                            "uncertain": 0.0, "records": 0, "_has": set()})
        bucket["start"], bucket["end"] = min(bucket["start"], hour), max(bucket["end"], hour + 3600)
        return bucket
    for r in records:
        day = _local(r["hour"], zone).strftime("%Y-%m-%d")
        days[day] = days.get(day, 0.0) + r["prompt"] + r["generated"]
        if r["hour"] + 3600 <= drawn_from:
            continue
        bucket = bucket_for(r["hour"])
        bucket["records"] += 1
        if by == "kind":
            values = [("generated", r["generated"]), ("computed", r["computed"] or 0.0), ("cached", r["cached"]),
                      ("imported", r["imported"])]
            for kind, present in (("generated", r["has_generated"]), ("computed", r["computed"] is not None),
                                  ("cached", r["has_cached"]), ("imported", r["has_imported"])):
                if present:
                    bucket["_has"].add(kind)
        else:
            values = [(r[by], r["prompt"] + r["generated"])]
        for name, amount in values:
            bucket["values"][name] = bucket["values"].get(name, 0.0) + amount
        bucket["partial"] = bucket["partial"] or not (r["full"]["prompt"] and r["full"]["generated"])
        bucket["tokens"] += r["prompt"] + r["generated"]
        bucket["uncertain"] += r["uncertain"]
        bucket["unavailable_input"] += r["computed_unavailable_input"]
        if r["computed_tag"] == "estimated":
            bucket["estimated"] += r["computed"]
    # Expected time counts against coverage, also in hours without any record (those buckets exist without values).
    spans, spans_truncated = _spans(store, start, end, now)
    for expected, _ in spans.values():
        for lo, hi in expected:
            hour = int(max(lo, drawn_from) // 3600) * 3600
            while hour < hi:
                bucket_for(hour)
                hour += 3600
    ordered = sorted(buckets.values(), key=lambda b: b["start"])
    for bucket in ordered:
        if by == "kind":
            bucket["unavailable"] = [k for k in ("generated", "computed", "cached", "imported") if k not in bucket["_has"]]
        del bucket["_has"]
        # Per engine, the union of its expected and observed intervals inside this bucket (hours split exactly).
        bucket["expected_s"] = sum(_overlap(exp, bucket["start"], bucket["end"]) for exp, _ in spans.values())
        bucket["observed_s"] = sum(_overlap(obs, bucket["start"], bucket["end"]) for _, obs in spans.values())
        bucket["coverage"] = min(1.0, bucket["observed_s"] / bucket["expected_s"]) if bucket["expected_s"] else None
        bucket["state"] = ("not expected" if not bucket["expected_s"] and not bucket["records"] else
                           "expected, not observed" if not bucket["observed_s"] and not bucket["tokens"] else "observed")
    dropped = sum(1 for d in days if d < label_of(drawn_from)[:10]) if not hourly else 0
    busiest = max(days.items(), key=lambda item: item[1]) if days else None
    partial_cached = any(not r["full"]["cached"] for r in records if r["has_prompt"] or r["has_cached"])
    complete_input, complete_output = total("prompt", complete), total("generated", complete)
    kpis = {"total": grand, "generated": generated, "input": inputs,
            "input_tag": "partial (input not reported for part of the time)" if input_partial else "measured",
            "output_tag": "partial (output not reported for part of the time)" if output_partial else "measured",
            "computed": measured + estimated, "computed_measured": measured, "computed_estimated": estimated,
            "computed_unavailable_input": unavailable_input,
            "computed_tag": "measured" if not estimated and not unavailable_input and not computed_missing else "partly estimated",
            "cached": cached if cached_known else None,
            "cached_tag": None if not cached_known else "partial (some engines reported it for part of the time)"
            if partial_cached else "measured",
            "imported": total("imported", imported_known) if imported_known else None,
            "uncertain": uncertain, "unverified_identity_tokens": blind_tokens,
            "generated_share": _share(complete_output, complete_input + complete_output),
            "cached_share": _share(total("aligned_cached", cached_full), total("aligned_prompt", cached_full)) if cached_full else None,
            "tokens_saved": cached if cached_known else None, "output_input": _share(complete_output, complete_input),
            "ratio_basis": "ratios use only hour records where input and output were reported for the whole record",
            "avg_per_day": grand / len(days) if days else None, "observed_days": len(days),
            "busiest_day": {"day": busiest[0], "tokens": busiest[1]} if busiest else None}
    why = {}
    reasons = {"generated_share": "no complete record in range", "output_input": "no complete record with input in range",
               "avg_per_day": "no observed day in range", "busiest_day": "no observed day in range",
               "cached": "no engine in range reports cache-restored tokens",
               "cached_share": "no engine in range reported cache-restored tokens for a whole hour record (or no input)",
               "tokens_saved": "no engine in range reports cache-restored tokens",
               "imported": "no engine in range reports imported tokens"}
    for name, reason in reasons.items():
        if kpis[name] is None:
            why["kpis." + name] = reason
    models = {}
    for r in records:
        m = models.setdefault(r["model"], {"model": r["model"], "generated": 0.0, "prompt": 0.0, "computed": 0.0,
                                           "estimated": 0.0, "unavailable_input": 0.0, "cached": 0.0, "imported": 0.0,
                                           "uncertain": 0.0, "observed_s": 0.0, "machines": set(), "profiles": set(),
                                           "full_cached": 0.0, "full_input": 0.0, "has_cached": False,
                                           "cached_partial": False, "blind_tokens": 0.0, "input_partial": False,
                                           "output_partial": False, "computed_missing": False,
                                           "complete_input": 0.0, "complete_output": 0.0})
        for name in ("generated", "prompt", "imported", "uncertain", "observed_s"):
            m[name] += r[name]
        m["input_partial"] = m["input_partial"] or not r["full"]["prompt"]
        m["output_partial"] = m["output_partial"] or not r["full"]["generated"]
        if r["full"]["prompt"] and r["full"]["generated"]:
            m["complete_input"] += r["prompt"]
            m["complete_output"] += r["generated"]
        if r["has_cached"]:
            m["cached"] += r["cached"]
            m["has_cached"] = True
        if r["has_prompt"] or r["has_cached"]:
            m["cached_partial"] = m["cached_partial"] or not r["full"]["cached"]
        if r["full"]["cached"] and r["full"]["prompt"]:
            m["full_cached"] += r["aligned_cached"]
            m["full_input"] += r["aligned_prompt"]
        if r["computed"] is not None:
            m["computed"] += r["computed"]
            if r["computed_tag"] == "estimated":
                m["estimated"] += r["computed"]
        else:
            m["computed_missing"] = True
        m["unavailable_input"] += r["computed_unavailable_input"]
        if r["blind_s"] > 0:
            m["blind_tokens"] += r["prompt"] + r["generated"]
        m["machines"].add(r["machine"])
        m["profiles"].add(r["profile"])
    table = []
    for m in models.values():
        m.update(input=m["prompt"], total=m["prompt"] + m["generated"],
                 cached_share=_share(m["full_cached"], m["full_input"]) if m["full_input"] else None,
                 cached_input=m["full_input"], output_input=_share(m["complete_output"], m["complete_input"]),
                 machines=sorted(m["machines"]), profiles=sorted(m["profiles"]),
                 computed_tag="measured" if not m["estimated"] and not m["unavailable_input"] and not m["computed_missing"]
                 else "partly estimated",
                 cached_tag=None if not m["has_cached"] else "partial" if m["cached_partial"] else "measured",
                 input_tag="partial" if m["input_partial"] else "measured",
                 output_tag="partial" if m["output_partial"] else "measured")
        if not m["has_cached"]:
            m["cached"] = None
        table.append(m)
    table.sort(key=lambda m: m["total"], reverse=True)
    coverage = {"observed_s": sum(_overlap(obs, start, end) for _, obs in spans.values()),
                "expected_s": sum(_overlap(exp, start, end) for exp, _ in spans.values()), "span_s": max(1.0, end - start),
                "note": "coverage = seconds with a valid generated-token interval / expected seconds (every second an "
                        "engine was listed by a launch record, read or not, per engine, split exactly across hours); "
                        "hours without any engine listed were not expected and are drawn as empty, never as zero"}
    notes = [n for n in (_offset_note(zone, now),
                         f"{dropped} older day(s) not drawn (the newest {MAX_DAY_BUCKETS} are shown; KPIs cover all)" if dropped else None,
                         f"range too long: only the newest {MAX_ROWS} rows were aggregated" if truncated else None,
                         ("coverage incomplete: only the newest " + str(SPAN_ROWS) + " " + ", ".join(spans_truncated)
                          + " were used") if spans_truncated else None) if n]
    return {"kpis": kpis, "buckets": ordered, "bucket": "hour" if hourly else "day", "models": table, "gaps": gaps,
            "gap_count": gap_count, "coverage": coverage, "sources": sources, "notes": notes,
            "complete": not truncated and not spans_truncated,
            "insights": insights(kpis, table, gap_count, all_gaps, sources), "why": why}


def _n(value):
    value = float(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f}"


def _pct(value):
    return f"{value * 100:.1f} %"


def insights(kpis, models, gap_count, gaps, sources):
    """Plain-language observations, each derived from the numbers in the same document."""
    if not kpis["total"]:
        return ["No tokens were counted in this range."]
    share = f" ({_pct(kpis['generated_share'])} generated in complete records)" if kpis["generated_share"] is not None else ""
    out = [f"{_n(kpis['total'])} tokens in range: {_n(kpis['input'])} input and {_n(kpis['generated'])} generated{share}."]
    for name, tag in (("input", kpis.get("input_tag", "")), ("output", kpis.get("output_tag", ""))):
        if tag.startswith("partial"):
            out.append(f"The {name} counter was not reported for part of the time: {name} totals are a lower bound.")
    if models:
        top = models[0]
        out.append(f"{top['model']} accounts for {_pct(top['total'] / kpis['total'])} of all tokens.")
    cached = [m for m in models if m["cached_share"]]
    if cached:
        best = max(cached, key=lambda m: m["cached_share"])
        out.append(f"The prefix cache restored {_pct(best['cached_share'])} of input on {best['model']} "
                   f"({_n(best['cached'])} tokens of prefill not recomputed in range).")
    elif kpis["cached"] is None:
        out.append("No engine in range reports cache-restored tokens; cache savings are unavailable.")
    if kpis["output_input"] is not None:
        out.append(f"Output to input ratio: {kpis['output_input']:.2f} generated tokens per input token.")
    if kpis["busiest_day"]:
        out.append(f"Busiest day: {kpis['busiest_day']['day']} with {_n(kpis['busiest_day']['tokens'])} tokens.")
    if gap_count:
        outside = sum(v for g in gaps for k, v in g["counts"].items() if k in ("prompt", "generated") and _finite(v)
                      and not g["reason"].startswith(("collection gap", "counter missing")))
        out.append(f"{gap_count} unobserved interval(s) in range (engine starts, restarts, unreachable engines or a "
                   f"reset): {_n(outside)} tokens the engines counted there are not in the ledger.")
    if kpis["uncertain"]:
        out.append(f"{_n(kpis['uncertain'])} tokens were counted after a collection gap; their hour is approximate.")
    if kpis["computed_estimated"]:
        out.append(f"{_n(kpis['computed_estimated'])} tokens of computed prefill are an upper bound "
                   "(cache-restored or imported tokens not reported by those engines).")
    if kpis["computed_unavailable_input"]:
        out.append(f"Computed prefill is unavailable for {_n(kpis['computed_unavailable_input'])} input tokens.")
    if kpis["unverified_identity_tokens"]:
        out.append(f"{_n(kpis['unverified_identity_tokens'])} tokens come from engines without an instance identity: "
                   "a restart whose counter overtakes the old value within one interval would be under-counted.")
    return out


FORMULA = re.compile(r"^[=+\-@\t\r]")


def _cell(value):
    text = "" if value is None else (f"{value:.0f}" if isinstance(value, float) and value.is_integer() else str(value))
    if FORMULA.match(text):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def csv_text(api, query):
    """(csv, truncated rows): the counter rows of a range plus one derived computed row per hour record."""
    store = telemetry.existing_store(api)
    if store is None:
        raise PermissionError(NO_STORE)
    zone, _, _ = zone_of(api)
    _, start, end = _range(query, api.clock(), store, allowed=("range",))
    header = ["hour_utc", "day_local", "profile", "replica", "machine", "model", "kind", "tokens", "tag", "record_s",
              "observed_s", "uncertain_tokens"]
    lines = [",".join(_cell(h) for h in header)]
    truncated = 0
    if _tables_exist(store):
        stamp = lambda hour: datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m-%dT%H:00Z")
        records, _ = _records(store, start, end)
        for r in records:
            day = _local(r["hour"], zone).strftime("%Y-%m-%d")
            names = (r["profile"], r["replica"], r["machine"], r["model"])
            out = [(kind, r[kind], "counter (whole record)" if r["full"][kind] else
                    "counter (partly reported)" if r["has_" + kind] else "not reported") for kind in KINDS]
            out.append(("computed", r["computed"],
                        r["computed_tag"] + (f" ({r['computed_why']})" if r["computed_why"] else "")))
            for kind, amount, tag in out:
                if len(lines) > CSV_ROWS:
                    truncated += 1
                    continue
                lines.append(",".join(_cell(v) for v in (stamp(r["hour"]), day, *names, kind, amount, tag, r["record_s"],
                                                         r["observed_s"], r["uncertain"])))
    return "\n".join(lines) + "\n", truncated


def export(api, query):
    text, truncated = csv_text(api, query)
    return api.document("ledger-export", csv=text, filename="hyperloom-token-ledger.csv", truncated_rows=truncated,
                        why={"csv": f"{truncated} rows beyond {CSV_ROWS} not exported"} if truncated else {})


def _strict(arguments, allowed):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(unknown))


def reset(api, arguments):
    _strict(arguments, ("confirm",))
    if arguments.get("confirm") != "reset":
        raise ValueError('reset needs {"confirm": "reset"}')
    store = _prepared(telemetry.open_store(api))
    rows = store.execute("SELECT COUNT(*) FROM ledger")[0][0]
    gaps = store.execute("SELECT COUNT(*) FROM ledger_gaps")[0][0]
    at = api.clock()
    store.transaction([("DELETE FROM ledger", ()), ("DELETE FROM ledger_gaps", ()),
                       ("DELETE FROM ledger_unreachable WHERE until IS NOT NULL", ()),
                       ("UPDATE ledger_unreachable SET since=MAX(since, ?) WHERE until IS NULL", (at,)),
                       ("DELETE FROM ledger_intervals", ()), ("DELETE FROM ledger_rates", ()),
                       ("DELETE FROM ledger_listed WHERE end < ?", (at - 3 * INTERVAL,)),
                       ("INSERT INTO ledger_resets(at) VALUES (?)", (at,))])
    _invalidate(store)
    return {"reset": True, "at": at, "records_deleted": rows, "gaps_deleted": gaps,
            "note": "counting continues from the next sample; an interval that began before the reset is disclosed, "
                    "not counted"}


# ---------------------------------------------------------------------------------------------------------------------
# Configured directories: identity recorded at save, every read anchored to it

def _is_junction(path):
    probe = getattr(os.path, "isjunction", None)
    return bool(probe and probe(path))


def _final_path_of(handle):
    """The real path of an open Windows handle (links and junctions resolved by the system)."""
    import ctypes
    from ctypes import wintypes
    function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    length = function(handle, buffer, 1024, 0)
    if not 0 < length < 1024:
        raise OSError("cannot resolve the opened file's path")
    path = buffer.value
    prefix = "\\\\?\\"                         # the extended-length path prefix the call returns
    if path.startswith(prefix + "UNC\\"):
        return "\\\\" + path[len(prefix) + 4:]
    return path[len(prefix):] if path.startswith(prefix) else path


def _final_path(fd):
    """The real path of an open file (Windows), or None where the platform cannot say."""
    if os.name != "nt":
        return None
    import msvcrt
    return _final_path_of(msvcrt.get_osfhandle(fd))


def _win_pin(path):
    """(handle, real path) of a directory opened for listing without delete sharing (Windows): while the handle is
    open the directory and every ancestor can be neither renamed nor removed, so its path keeps naming it. A reparse
    point (link, junction) is refused."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    list_directory, read_attributes, share_read_write, open_existing = 0x1, 0x80, 0x1 | 0x2, 3
    backup_semantics, open_reparse_point = 0x02000000, 0x00200000
    handle = kernel.CreateFileW(str(path), list_directory | read_attributes, share_read_write, None, open_existing,
                                backup_semantics | open_reparse_point, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "cannot open the configured directory")
    try:
        attributes = kernel.GetFileAttributesW(str(path))
        if attributes == 0xFFFFFFFF or attributes & 0x400 or not attributes & 0x10:
            raise ValueError("configured directory became a link")
        return handle, _final_path_of(handle), kernel.CloseHandle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


class Directory:
    """A configured directory pinned to the identity (device, inode) it had when it was saved.

    POSIX: listing and opening go through a directory descriptor opened without following links (dir_fd), so a
    renamed or replaced directory or ancestor cannot redirect a read. Windows (no dir_fd): the directory is held open
    without delete sharing for the whole read (neither it nor an ancestor can be renamed or replaced meanwhile), its
    identity must equal the saved one, every opened file must be the listed one (identity) and lie directly in the
    directory's real path (GetFinalPathNameByHandleW of the open file)."""

    def __init__(self, setting):
        self.path = Path(setting["path"])
        self.identity = (setting.get("dev"), setting.get("ino"))
        self.fd = None
        self.pin = None
        self.real = None

    def __enter__(self):
        if self.fd is not None or self.pin is not None:
            return self                      # already open (entered once by `_listing`)
        if os.name != "nt" and os.open in os.supports_dir_fd and os.scandir in os.supports_fd:
            self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            st = os.fstat(self.fd)
            self.real = os.path.normcase(str(self.path.resolve(strict=True)))
        else:
            handle, real, closer = _win_pin(self.path)
            self.pin = (handle, closer)
            self.real = os.path.normcase(real)
            st = os.lstat(self.path)
            if stat.S_ISLNK(st.st_mode) or _is_junction(self.path):
                self.close()
                raise ValueError("configured directory became a link")
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self.identity:
            self.close()
            raise ValueError("configured directory was replaced since it was saved (save it again)")
        return self

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pin is not None:
            handle, closer = self.pin
            closer(handle)
            self.pin = None

    def __exit__(self, *exc):
        self.close()

    def entries(self, suffixes, limit, deadline=None):
        """([(name, lstat)], scanned): regular files directly inside, newest first (at most SCAN_ENTRIES scanned,
        stopping at `deadline`)."""
        found, scanned = [], 0
        with os.scandir(self.fd if self.fd is not None else self.path) as listing:
            for entry in listing:
                scanned += 1
                if scanned > SCAN_ENTRIES or (deadline is not None and time.monotonic() > deadline):
                    scanned = SCAN_ENTRIES + 1
                    break
                if not entry.name.lower().endswith(suffixes) or entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False) if self.fd is not None else os.lstat(self.path / entry.name)
                if stat.S_ISREG(st.st_mode):
                    found.append((entry.name, st))
        found.sort(key=lambda item: (-item[1].st_mtime_ns, item[0]))
        return found[:limit], scanned

    def open(self, name, listed, same_size=True):
        """Open a listed file; refuse anything that is no longer that regular file directly in this directory."""
        if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
            raise ValueError("invalid file name")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if self.fd is not None:
            fd = os.open(name, flags, dir_fd=self.fd)
        else:
            if stat.S_ISLNK(os.lstat(self.path / name).st_mode):
                raise ValueError("file became a link since it was listed")
            fd = os.open(self.path / name, flags)
        try:
            now = os.fstat(fd)
            if (not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (listed.st_dev, listed.st_ino)
                    or (same_size and now.st_size != listed.st_size)):
                raise ValueError("file changed since it was listed")
            real = _final_path(fd)
            if real is not None and os.path.normcase(real) != os.path.join(self.real, os.path.normcase(name)):
                raise ValueError("file is not directly in the configured directory")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise


def _directory_setting(value, name):
    """The stored form of a directory setting: {path, dev, ino}, or None to unset."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 400 or "\x00" in value:
        raise ValueError(name + " must be an absolute directory path (at most 400 characters) or null")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(name + " must be an absolute path")
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or _is_junction(path):
            raise ValueError(name + " must be a plain directory (not a link)")
        if os.path.normcase(str(path.resolve(strict=True))) != os.path.normcase(os.path.abspath(path)):
            raise ValueError(name + " path goes through a link; give the real path")
    except FileNotFoundError:
        raise ValueError(name + " does not exist") from None
    except OSError as exc:
        raise ValueError(f"{name} unreadable: {exc.strerror or exc}") from None
    return {"path": os.path.abspath(path), "dev": st.st_dev, "ino": st.st_ino}


def _setting(api, name):
    store = telemetry.existing_store(api)
    value = store.get(PREFS, name) if store is not None else None
    return value if isinstance(value, dict) and isinstance(value.get("path"), str) else None


def settings(api):
    return {name: (_setting(api, name) or {}).get("path") for name in ("census_dir", "logs_dir")}


def save_settings(api, arguments):
    _strict(arguments, ("census_dir", "logs_dir"))
    if not arguments:
        raise ValueError("give census_dir and/or logs_dir")
    values = {k: _directory_setting(v, k) for k, v in arguments.items()}
    store = telemetry.open_store(api)
    for key, value in values.items():
        if value is None:
            store.delete(PREFS, key)
        else:
            store.put(PREFS, key, value)
    _refresh_files(api)
    return {"saved": {k: (v or {}).get("path") for k, v in values.items()}}


def _refresh_files(api):
    try:
        telemetry.refresh(api, "ledger-files")      # coalesced with a run in progress
    except (PermissionError, LookupError):
        pass                                         # no sampler in this process (CLI): its commands rebuild directly


def _listing(setting, suffixes, limit, deadline=None):
    """(open Directory or None, [(name, lstat)], scanned, why); the caller closes the directory."""
    if setting is None:
        return None, [], 0, "not configured"
    directory = Directory(setting)
    try:
        directory.__enter__()
        files, scanned = directory.entries(suffixes, limit, deadline)
    except ValueError as exc:
        directory.close()
        return None, [], 0, str(exc)
    except OSError as exc:
        directory.close()
        return None, [], 0, f"configured directory unreadable: {exc.strerror or exc}"
    return directory, files, scanned, None


# ---------------------------------------------------------------------------------------------------------------------
# F12 census (C9)

CENSUS_FILES, CENSUS_BYTES, CENSUS_BUDGET, CENSUS_SECONDS, CENSUS_CELLS = 64, 1024 * 1024, 16 * 1024 * 1024, 1.5, 500
ENGINES = {"ds": "ds41-pair", "deepseek": "ds41-pair", "minimax": "minimax-pair", "mm": "minimax-pair",
           "qwen": "qwen-replicas"}
DIMS = ("machines", "devices", "link_mode", "prompt_tokens", "context_tokens", "batch_rows", "spec_mode", "spec_depth",
        "precision", "recipe_digest")
WORKLOAD = ("prompt_tokens", "context_tokens", "batch_rows", "spec_mode", "spec_depth")
NOT_LIVE = ("link_mode", "precision")
NUMBERS = ("wall_ms", "tok_s", "bytes_gb", "floor_ms", "pct_ceiling", "ceiling_tok_s")
TAGS = {"M": "measured", "E": "estimated", "ND": "not determinable"}
IDENT = re.compile(r"[0-9a-fA-F]{8,64}")
WORKLOAD_WHY = ("workload of live traffic (prompt/context length, batch rows, speculation) and the link mode and "
                "precision in use are not observable at this engine pin")
_census_cache = {}


def _tag(text):
    if not isinstance(text, str) or not text.strip():
        return None
    first = text.strip().split()[0].rstrip(":;,")
    return first if first in TAGS else None


def _short(value, limit=400):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, bool) or value is None:
        return value
    if _finite(value):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    return None


def _na(value):
    return isinstance(value, dict) and set(value) == {"na"} and isinstance(value["na"], str) and bool(value["na"].strip())


def parse_cell(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("cell"), str):
        return None
    cell = {"cell": raw["cell"][:200], "why": {}}
    for key in NUMBERS:
        value = raw.get(key)
        cell[key] = value if _count(value) else None
        if cell[key] is None:
            cell["why"][key] = ("withheld by the census (not computed for this cell)" if key == "pct_ceiling" and key in raw
                                else "not in the artifact" if value is None else "not a finite non-negative number")
    for key, source in (("ceiling", "ceiling_tag"), ("wall", "wall_tag")):
        tag = _tag(raw.get(source))
        cell[key + "_tag"] = tag
        cell[key + "_tag_text"] = _short(raw.get(source), 300)
        if tag is None:
            cell["why"][key + "_tag"] = "tag not given" if raw.get(source) is None else "tag not M, E or ND"
    cell["bytes_tag"] = "E (inventory arithmetic)" if cell["bytes_gb"] is not None or cell["floor_ms"] is not None else None
    cell["compatible"] = raw.get("compatible") is not False
    dims = raw.get("dims")
    cell["dims"] = {k: dims[k] for k in DIMS if k in dims} if isinstance(dims, dict) else None
    cell["details"] = {k: _short(v) for k, v in list(raw.items())[:40]
                       if k not in (*NUMBERS, "cell", "dims", "ceiling_tag", "wall_tag")}
    return cell


def parse_artifact(data, name):
    """(artifact, None) or (None, why) for one census file's bytes."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not JSON"
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    declared = obj.get("schema")
    if declared is not None and declared != "census-v1":
        return None, f"schema {str(declared)[:40]!r} is not census-v1"
    if not isinstance(obj.get("engine"), str) or not isinstance(obj.get("sha"), str) or not isinstance(obj.get("cells"), list):
        return None, "engine, sha and cells are required"
    cells = [parse_cell(c) for c in obj["cells"][:CENSUS_CELLS]]
    artifact = {"file": name[:200], "engine": obj["engine"][:40], "sha": obj["sha"][:64],
                "binsha": obj["binsha"][:64] if isinstance(obj.get("binsha"), str) else None,
                "schema_declared": declared == "census-v1",
                "notes": [str(n)[:400] for n in obj.get("notes", [])[:20]] if isinstance(obj.get("notes"), list) else [],
                "cells": [c for c in cells if c is not None], "cells_dropped": sum(c is None for c in cells),
                "cells_truncated": max(0, len(obj["cells"]) - CENSUS_CELLS), "why": {}}
    if not artifact["schema_declared"]:
        artifact["why"]["schema"] = "schema field absent (accepted as the census producer's current output)"
    return artifact, None


def read_census(setting):
    """(artifacts or None, rejected, why, notes): bounded in files, bytes and time; cached by file identity."""
    ends = time.monotonic() + CENSUS_SECONDS
    directory, files, scanned, why = _listing(setting, (".json",), CENSUS_FILES, deadline=ends)
    if directory is None:
        return None, [], why, []
    artifacts, rejected, notes, budget = [], [], [], CENSUS_BUDGET
    if scanned > SCAN_ENTRIES:
        notes.append(f"listing stopped after {SCAN_ENTRIES} entries or its time budget; later files not shown")
    with directory:
        for name, st in files:
            ident = (str(directory.path), name, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
            if ident in _census_cache:
                artifact, reason = _census_cache[ident]
            elif st.st_size > CENSUS_BYTES:
                artifact, reason = None, f"larger than {CENSUS_BYTES} bytes"
            elif st.st_size > budget or time.monotonic() > ends:
                notes.append(f"{name[:200]}: not read in this request (read budget reached; shown on a later refresh)")
                continue
            else:
                budget -= st.st_size
                try:
                    with directory.open(name, st) as handle:
                        data = handle.read(CENSUS_BYTES + 1)
                    artifact, reason = parse_artifact(data, name)
                except (OSError, ValueError) as exc:
                    artifact, reason = None, str(exc)[:200]
                if len(_census_cache) > 256:
                    _census_cache.clear()
                _census_cache[ident] = (artifact, reason)
            if artifact is None:
                rejected.append({"file": name[:200], "why": reason})
            else:
                artifacts.append(json.loads(json.dumps(artifact)))
    return artifacts, rejected, None, notes


def _live_profiles(api):
    """{profile: {family, engine_sha, components, binaries, machines, devices, recipe_digest}} of running profiles."""
    out = {}
    for state in _states(api):
        launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
        recipe = state.get("recipe") if isinstance(state.get("recipe"), dict) else {}
        out[state["profile"]] = {"family": launch.get("family"), "engine_sha": launch.get("engine_sha"),
                                 "components": launch.get("components") if isinstance(launch.get("components"), dict) else {},
                                 "binaries": [], "machines": sorted(map(str, state.get("nodes") or [])),
                                 "devices": sorted(map(str, state.get("devices") or [])),
                                 "recipe_digest": recipe.get("digest") or recipe.get("effective_digest")}
    if out:
        try:
            # Only an already computed core snapshot: a GET never triggers the core's node probes.
            snapshot = getattr(api, "_cached", None) or {}
            for profile in (snapshot.get("profiles") or {}).get("profiles") or []:
                if profile.get("id") in out:
                    out[profile["id"]]["binaries"] = sorted({
                        p.get("binary_sha256") for p in profile.get("processes") or []
                        if isinstance(p.get("binary_sha256"), str) and str(p.get("name", "")).lower() not in ("router", "prefill")})
        except Exception:
            pass
    return out


def _same(declared, live):
    if isinstance(declared, list) and isinstance(live, list):
        return sorted(map(str, declared)) == sorted(map(str, live))
    return str(declared) == str(live)


def match_cell(artifact, cell, live):
    """{profile, build, dims: {dim: verdict}, comparable, why} for one cell against the running profiles (C9).

    A live value is set beside a cell only when the build and every dimension match with evidence; at this engine pin
    the workload of live traffic is not observable, so every check ends there and says so."""
    if not cell["compatible"]:
        return {"profile": None, "comparable": False, "why": "the census marks this cell incompatible (no comparison)"}
    family = ENGINES.get(artifact["engine"].lower())
    candidates = [(name, info) for name, info in live.items() if family and info["family"] == family]
    if not candidates:
        return {"profile": None, "comparable": False, "why": "no running profile of this engine"}
    if cell["dims"] is None:
        return {"profile": None, "comparable": False,
                "why": "cell does not declare its dimensions (hardware, topology, workload, precision)"}
    undeclared = [d for d in DIMS if d not in cell["dims"]]
    if undeclared:
        return {"profile": None, "comparable": False, "why": "cell does not declare: " + ", ".join(undeclared)}
    reasons = []
    for name, info in candidates:
        build = None
        binsha, sha = artifact["binsha"], artifact["sha"]
        if binsha and IDENT.fullmatch(binsha) and any(b.lower().startswith(binsha.lower()) for b in info["binaries"]):
            build = "binary sha"
        elif IDENT.fullmatch(sha) and any(isinstance(c, str) and c.lower().startswith(sha.lower())
                                          for c in (info["engine_sha"], *info["components"].values())):
            build = "engine commit"
        if build is None:
            reasons.append(f"{name}: running build differs from {sha[:16]}"
                           + ("" if info["binaries"] else
                              " (running binary sha unavailable: portal snapshot not computed yet or not reported)"))
            continue
        verdicts = {}
        for dim in DIMS:
            declared = cell["dims"][dim]
            if dim in WORKLOAD or dim in NOT_LIVE:
                verdicts[dim] = "not observable live"
            elif _na(declared):
                verdicts[dim] = "declared not applicable: " + declared["na"][:120]
            else:
                observed = info.get(dim)
                verdicts[dim] = ("matches" if observed is not None and _same(declared, observed)
                                 else f"differs ({_short(declared, 60)} vs {_short(observed, 60)})")
        differing = [d for d in DIMS if verdicts[d].startswith("differs")]
        why = (f"{name}: " + "; ".join(f"{d} {verdicts[d]}" for d in differing)) if differing else f"{name}: {WORKLOAD_WHY}"
        return {"profile": name, "build": build, "dims": verdicts, "comparable": False, "why": why}
    return {"profile": None, "comparable": False, "why": "; ".join(reasons)[:600]}


def live_rate(store, profile, now, current, window=60.0):
    """(aggregate tok/s, sources, why, measured window s): completed-token throughput of a profile.

    Only the profile's sources listed now (`current` keys) count. Per source: the rate entries (valid, certain
    generated-counter intervals of one identified instance, written in the scrape transaction and removed by any
    discontinuity or reset) that lie entirely inside the last `window` seconds; the newest must end within three
    collection intervals and together they must cover at least half the window. Its rate is tokens / seconds of those
    intervals; the profile's throughput is the sum over its sources (replicas serve in parallel). Any source failing a
    condition withholds the value."""
    if store is None:
        return None, 0, NO_STORE, None
    keys = sorted(k for k in current if k.startswith(profile + "/"))
    if not keys:
        return None, 0, "no listed engine for this profile", None
    if not _tables_exist(store):
        return None, 0, "no counted interval yet", None
    total, measured = 0.0, window
    for key in keys:
        value = store.get(SOURCES, key) or {}
        if value.get("error"):
            return None, 0, f"{key}: {value['error']}", None
        entries = store.execute("SELECT start, end, tokens, instance FROM ledger_rates WHERE source=? AND start>=? AND end<=? "
                                "ORDER BY end", (key, now - window, now + 1e-6))
        if not entries or now - entries[-1][1] > 3 * INTERVAL:
            return None, 0, f"{key}: no fresh counter interval", None
        if len({e[3] for e in entries}) > 1 or entries[-1][3] != value.get("instance"):
            return None, 0, f"{key}: engine restarted within the last {window:.0f} s", None
        seconds = sum(e[1] - e[0] for e in entries)
        if seconds < window / 2:
            return None, 0, f"{key}: less than {window / 2:.0f} s of valid counter intervals in the last {window:.0f} s", None
        total += sum(e[2] for e in entries) / seconds
        measured = min(measured, seconds)
    return total, len(keys), None, measured


def build_census(api, now):
    """The census document's fields (bounded filesystem work; run by the `ledger-files` collector and the CLI)."""
    store = telemetry.existing_store(api)
    setting = _setting(api, "census_dir")
    artifacts, rejected, why, notes = read_census(setting)
    directory = (setting or {}).get("path")
    if artifacts is None:
        hint = " (set it on this page or with hyperloom tokens config --census-dir)" if why == "not configured" else ""
        return dict(directory_set=bool(setting), directory=directory, artifacts=None, rejected=rejected, notes=notes,
                    live=None, why={"artifacts": "census directory " + why + hint})
    live = _live_profiles(api) if artifacts else {}
    current = {source["key"] for state in _states(api) for source in sources_of(state)} if live else set()
    rates = {}
    for name in live:
        value, used, reason, seconds = live_rate(store, name, now, current)
        rates[name] = {"tok_s": value, "sources": used, "why": reason, "window_s": seconds,
                       "label": "aggregate completed-token throughput over the last 60 s (not compared with a cell)"}
    for artifact in artifacts:
        for cell in artifact["cells"]:
            cell["match"] = match_cell(artifact, cell, live)
    return dict(directory_set=True, directory=directory, artifacts=artifacts, rejected=rejected, notes=notes,
                live=rates, required_dims=list(DIMS),
                observer="census artifacts as written; live throughput: control machine, from certain generated-token "
                         "counter intervals",
                why={} if artifacts else {"artifacts": "no census JSON files in the configured directory"})


SNAPSHOT = "ledger/snapshot"
FILES_INTERVAL = 15.0
SNAPSHOT_NEEDS = "not built yet: the ledger-files collector builds it every 15 s (or run the CLI command)"


def _snapshot(api, name, fields, query):
    """A GET over the last snapshot the `ledger-files` collector built (no filesystem or network work here)."""
    if query:
        raise ValueError(name + " takes no query arguments")
    store = telemetry.existing_store(api)
    snap = store.get(SNAPSHOT, name) if store is not None else None
    if not isinstance(snap, dict):
        why = NO_STORE if store is None else SNAPSHOT_NEEDS
        return api.document("ledger-" + name, **dict.fromkeys(fields), snapshot_ts=None, why={f: why for f in fields})
    fields_ = dict(snap["fields"])
    stalled = store.get(SNAPSHOT, "stalled")
    if isinstance(stalled, dict):
        fields_.setdefault("notes", []).append(
            f"the last rebuild has not finished within {FILES_DEADLINE:.0f} s (a directory or service is blocking); "
            "this is the previous snapshot")
    age = api.clock() - snap["ts"]
    if age > 4 * FILES_INTERVAL:
        fields_.setdefault("notes", []).append(f"snapshot is {age:.0f} s old (the ledger-files collector is behind)")
    if name == "rtx":                               # the last parsed run is written by its POST, not the collector
        fields_["last"] = store.get(BATCH, "last")
        if fields_["last"] is None:
            fields_.setdefault("why", {})["last"] = "no batch run parsed yet"
        else:
            fields_.get("why", {}).pop("last", None)
    return api.document("ledger-" + name, **fields_, snapshot_ts=snap["ts"])


def census(api, query):
    return _snapshot(api, "census", ("artifacts", "live"), query)


def collect_files(api, now):
    """The `ledger-files` collector: census directory, run-log listing and RTX service reads, each bounded, into
    snapshots that the GETs read."""
    store = _prepared(telemetry.open_store(api))
    previous = _file_builds.get(id(store))
    if previous is not None and previous.is_alive():
        # The last build is still blocked (a filesystem or service call that cannot be interrupted): never start a
        # second one; the GETs keep the last snapshot and say it is stalled.
        store.put(SNAPSHOT, "stalled", {"since": previous.started, "ts": now})
        return []
    result = {}

    def work():
        result["census"] = build_census(api, now)
        result["rtx"] = build_rtx(api, now)
    worker = threading.Thread(target=work, name="hyperloom-ledger-files", daemon=True)
    worker.started = now
    _file_builds[id(store)] = worker
    worker.start()
    worker.join(FILES_DEADLINE)
    if worker.is_alive():
        store.put(SNAPSHOT, "stalled", {"since": now, "ts": now})
        return []
    store.put(SNAPSHOT, "census", {"ts": now, "fields": result["census"]})
    store.put(SNAPSHOT, "rtx", {"ts": now, "fields": result["rtx"]})
    store.delete(SNAPSHOT, "stalled")
    return []


_file_builds = {}
FILES_DEADLINE = 10.0


# ---------------------------------------------------------------------------------------------------------------------
# F13 RTX prefill service + batch runs

STATS_MARK = b"mm-rtx-prefill: ss stats "
LOG_SUFFIXES = (".log", ".txt", ".out")
LOG_TAIL, LINE_MAX, RUNS = 8 * 1024 * 1024, 1024 * 1024, 200
SS_TOTAL = ("routed", "hits", "admitted", "transient", "evicted", "prefetched", "spec_issued", "spec_used", "spec_dropped",
            "bytes_pinned", "bytes_disk", "wait_ns")
SERVICE_NEEDS = "needs RTX prefill service status with provider counters"


def _run_id(name, st):
    return hashlib.sha256(f"{name}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}".encode()).hexdigest()[:16]


def list_runs(setting, deadline=None):
    directory, files, scanned, why = _listing(setting, LOG_SUFFIXES, RUNS, deadline)
    if directory is None:
        return None, why, None
    directory.close()
    note = f"listing stopped after {SCAN_ENTRIES} entries or its time budget; later files not shown" if scanned > SCAN_ENTRIES else None
    return [{"id": _run_id(n, st), "name": n[:200], "bytes": st.st_size, "mtime": st.st_mtime} for n, st in files], None, note


def _ss_layer(raw):
    return {k: raw.get(k) if _count(raw.get(k)) else None for k in SS_TOTAL} if isinstance(raw, dict) else None


def parse_stats(obj):
    """strix-stream/stats-v1 -> panel values (counters as reported; shares only where both sides exist)."""
    if not isinstance(obj, dict) or obj.get("schema") != "strix-stream/stats-v1":
        raise ValueError("stats line is not strix-stream/stats-v1")
    total = _ss_layer(obj.get("total"))
    if total is None:
        raise ValueError("stats line has no total")
    layers = [_ss_layer(layer) for layer in obj["layers"][:1024]] if isinstance(obj.get("layers"), list) else []

    def share(a, b):
        return a / b if _count(a) and _count(b) and b else None
    out = {"schema": obj["schema"], "backend": obj["backend"][:40] if isinstance(obj.get("backend"), str) else None,
           "forwards": obj.get("forwards") if _count(obj.get("forwards")) else None, "total": total,
           "hit_rate": share(total["hits"], total["routed"]),
           "spec_used_share": share(total["spec_used"], total["spec_issued"]),
           "spec_dropped_share": share(total["spec_dropped"], total["spec_issued"]),
           "wait_s": total["wait_ns"] / 1e9 if _count(total["wait_ns"]) else None,
           "layers": [{"layer": i, "hit_rate": share((layer or {}).get("hits"), (layer or {}).get("routed")),
                       "wait_s": (layer or {})["wait_ns"] / 1e9 if _count((layer or {}).get("wait_ns")) else None,
                       **(layer or {})} for i, layer in enumerate(layers)],
           "readahead": {k: v for k, v in obj["readahead"].items() if isinstance(k, str) and _count(v)}
           if isinstance(obj.get("readahead"), dict) else None,
           "exclusive": {k: v for k, v in obj["exclusive"].items() if isinstance(k, str) and _count(v)}
           if isinstance(obj.get("exclusive"), dict) else None,
           "sources_direct": sum(1 for v in obj["sources_direct"] if v is True) if isinstance(obj.get("sources_direct"), list) else None,
           "why": {}}
    for key, reason in (("hit_rate", "no routed units"), ("spec_used_share", "no speculative reads issued"),
                        ("spec_dropped_share", "no speculative reads issued"), ("wait_s", "wait not reported")):
        if out[key] is None:
            out["why"][key] = reason
    for key in ("readahead", "exclusive"):
        if out[key] is None:
            out["why"][key] = "not in this run's stats (feature off)"
    return out


def last_stats(handle):
    """The last `ss stats` line of an open run log (reads at most the last 8 MiB)."""
    size = os.fstat(handle.fileno()).st_size
    handle.seek(max(0, size - LOG_TAIL))
    tail = handle.read(LOG_TAIL)
    found = None
    for line in tail.split(b"\n"):
        at = line.find(STATS_MARK)
        if at >= 0:
            found = line[at + len(STATS_MARK):]
    if found is None:
        raise LookupError("no `ss stats` line in the last 8 MiB of this run log")
    if len(found) > LINE_MAX:
        raise ValueError("stats line exceeds 1 MiB")
    try:
        obj = json.loads(found.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
        raise ValueError("stats line is not JSON") from None
    return parse_stats(obj)


def parse_run(api, arguments):
    _strict(arguments, ("id",))
    run_id = arguments.get("id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{16}", run_id):
        raise ValueError("id must be a run id from the listing")
    directory, files, _, why = _listing(_setting(api, "logs_dir"), LOG_SUFFIXES, RUNS)
    if directory is None:
        raise PermissionError("run logs directory " + why)
    with directory:
        chosen = next(((n, st) for n, st in files if _run_id(n, st) == run_id), None)
        if chosen is None:
            raise LookupError("no listed run log with this id (the directory changed: refresh the listing)")
        try:
            with directory.open(chosen[0], chosen[1], same_size=False) as handle:
                parsed = last_stats(handle)
        except OSError as exc:
            raise ValueError(f"run log unreadable: {exc.strerror or exc}") from None
    result = {"id": run_id, "name": chosen[0][:200], "bytes": chosen[1].st_size, "mtime": chosen[1].st_mtime,
              "parsed_at": api.clock(), "stats": parsed}
    telemetry.open_store(api).put(BATCH, "last", result)
    return result


def _service_status(port, deadline=None):
    """(health, status, why) of an RTX prefill service on the control machine's loopback interface."""
    try:
        health = _fetch(LOOPBACK, port, "/health", deadline=deadline)
    except (OSError, ValueError) as exc:
        return None, None, f"service unreachable: {str(exc)[:200]}"
    try:
        status = _fetch(LOOPBACK, port, "/api/status", deadline=deadline)
    except (OSError, ValueError):
        return health, None, SERVICE_NEEDS
    return health, status if isinstance(status, dict) else None, None


def service_panel(health, status):
    out = {"health": {k: health.get(k) for k in ("status", "busy", "queued", "served", "batches", "handoff_pending")
                      if k in health} if isinstance(health, dict) else None,
           "provider": None, "strix_stream": None, "why": {}}
    provider = (status or {}).get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("counters"), dict):
        counters = []
        for name, value in list(provider["counters"].items())[:64]:
            number = value.get("value") if isinstance(value, dict) else value
            if isinstance(name, str) and not isinstance(number, bool) and _finite(number):
                unit = value.get("unit") if isinstance(value, dict) else None
                counters.append({"name": name[:80], "value": number, "unit": unit[:16] if isinstance(unit, str) else None})
        out["provider"] = {"name": provider["name"][:80] if isinstance(provider.get("name"), str) else None,
                           "counters": counters}
    else:
        out["why"]["provider"] = SERVICE_NEEDS
    stream = (status or {}).get("strix_stream")
    if isinstance(stream, dict):
        try:
            out["strix_stream"] = parse_stats(stream)
        except ValueError as exc:
            out["why"]["strix_stream"] = str(exc)
    else:
        out["why"]["strix_stream"] = "service not built on strix-stream, or its snapshot is not exposed"
    return out


def build_rtx(api, now):
    """The RTX document's fields (bounded service reads and listing; run by `ledger-files` and the CLI)."""
    services = []
    deadline = time.monotonic() + 4.0                 # the whole document, every service included
    for state in _states(api)[:8]:
        service = state.get("prefill_service") if isinstance(state.get("prefill_service"), dict) else None
        port = (service or {}).get("port")
        if type(port) is not int or not 1024 <= port <= 65535:
            continue
        health, status, why = _service_status(port, deadline)
        panel = service_panel(health, status)
        if why:
            panel["why"]["service"] = why
        services.append({"profile": state["profile"], "port": port, **panel})
    setting = _setting(api, "logs_dir")
    runs, runs_why, note = list_runs(setting, deadline=time.monotonic() + 1.5)
    why = {}
    if not services:
        why["services"] = "no running profile with an RTX prefill service"
    if runs is None:
        why["runs"] = "run logs directory " + runs_why
    return dict(services=services, runs=runs, logs_dir_set=bool(setting), logs_dir=(setting or {}).get("path"),
                notes=[note] if note else [], why=why,
                observer="RTX prefill service (its own counters) and strix-stream stats as the run wrote them")


def rtx(api, query):
    return _snapshot(api, "rtx", ("services", "runs"), query)


# ---------------------------------------------------------------------------------------------------------------------
# F47 token value

PRICE_KEYS = ("input", "cached", "output")
CURRENCY = re.compile(r"[A-Za-z$€£¥ ]{1,8}")


def _price_set(value, where):
    if not isinstance(value, dict):
        raise ValueError(where + " must be an object with input, cached and output prices")
    unknown = sorted(set(value) - set(PRICE_KEYS))
    if unknown:
        raise ValueError(where + ": unknown keys " + ", ".join(unknown))
    out = {}
    for key in PRICE_KEYS:
        price = value.get(key)
        if isinstance(price, bool) or not _finite(price) or not 0 <= price <= 100000:
            raise ValueError(f"{where}.{key} must be a price per million tokens in 0..100000")
        out[key] = float(price)
    return out


def validate_prices(arguments):
    _strict(arguments, ("currency", "default", "models"))
    currency = arguments.get("currency")
    if not isinstance(currency, str) or not CURRENCY.fullmatch(currency) or not currency.strip():
        raise ValueError("currency must be 1-8 letters or a currency sign")
    table = {"currency": currency.strip(), "default": None, "models": {}}
    if arguments.get("default") is not None:
        table["default"] = _price_set(arguments["default"], "default")
    models = arguments.get("models") or {}
    if not isinstance(models, dict) or len(models) > 64:
        raise ValueError("models must map at most 64 model names to prices")
    for name, prices in models.items():
        if not isinstance(name, str) or not 0 < len(name) <= 128:
            raise ValueError("model names must be 1-128 characters")
        table["models"][name] = _price_set(prices, "models." + name)
    return table


def save_prices(api, arguments):
    table = validate_prices(arguments)
    store = telemetry.open_store(api)
    store.put(PREFS, "prices", table)
    _invalidate(store)
    return {"saved": table}


def value(api, query):
    if "by" in query:
        raise ValueError("unknown query arguments: by")
    store = telemetry.existing_store(api)
    prices = store.get(PREFS, "prices") if store is not None else None
    report = ledger(api, query)
    why = {}
    if prices is None:
        why["prices"] = "no price table yet (edit it on this page or with hyperloom token-value set)"
    if report.get("models") is None:
        why["models"] = report["why"].get("models", "ledger unavailable")
        return api.document("ledger-value", range=report["range"], prices=prices, models=None, totals=None,
                            timezone=report["timezone"], why=why)
    keys = ("input_cost", "cached_cost", "output_cost", "total", "savings")
    rows, totals, unpriced = [], dict.fromkeys(keys, 0.0), []
    flags = {"estimated_input": 0.0, "unavailable_input": 0.0, "uncertain": 0.0, "unverified_identity_tokens": 0.0,
             "cache_unreported_models": []}
    for model in report["models"]:
        table = (prices or {}).get("models") or {}
        price = table.get(model["model"]) or (prices or {}).get("default")
        row = {"model": model["model"], "input_tokens": model["computed"] + model["imported"],
               "cached_tokens": model["cached"], "output_tokens": model["generated"],
               "input_tag": model["computed_tag"], "estimated_input": model["estimated"],
               "unavailable_input": model["unavailable_input"], "uncertain_tokens": model["uncertain"],
               "unverified_identity_tokens": model["blind_tokens"], "price_source": None, **dict.fromkeys(keys), "why": {}}
        for flag, name in (("estimated_input", "estimated"), ("unavailable_input", "unavailable_input"),
                           ("uncertain", "uncertain"), ("unverified_identity_tokens", "blind_tokens")):
            flags[flag] += model[name]
        row["cached_tag"] = model["cached_tag"]
        if model["cached"] is None:
            flags["cache_unreported_models"].append(model["model"])
            row["why"]["cached_cost"] = row["why"]["savings"] = "this model's engines do not report cache-restored tokens"
        elif model["cached_tag"] == "partial":
            flags.setdefault("cache_partial_models", []).append(model["model"])
            row["why"]["savings"] = "partial: cache-restored tokens reported for part of the range only"
        if model["unavailable_input"]:
            row["why"]["input_cost"] = f"excludes {model['unavailable_input']:.0f} input tokens whose computed share is unavailable"
        if price is None:
            unpriced.append(model["model"])
            row["why"]["total"] = "no price for this model and no default price"
        else:
            row["price_source"] = "model" if model["model"] in table else "default"
            row["input_cost"] = row["input_tokens"] / 1e6 * price["input"]
            row["output_cost"] = row["output_tokens"] / 1e6 * price["output"]
            if model["cached"] is not None:
                row["cached_cost"] = model["cached"] / 1e6 * price["cached"]
                # C8: cache savings count only cache-restored tokens, priced at the difference to computing them.
                row["savings"] = model["cached"] / 1e6 * max(0.0, price["input"] - price["cached"])
            row["total"] = row["input_cost"] + (row["cached_cost"] or 0.0) + row["output_cost"]
            for key in keys:
                totals[key] += row[key] or 0.0
        row["qualifiers"] = _qualifiers(model) + (["range too long: only the newest records were aggregated"]
                                                  if report.get("complete") is False else [])
        row["exact"] = not row["qualifiers"]
        rows.append(row)
    if unpriced:
        why["totals"] = "totals exclude unpriced models: " + ", ".join(unpriced)
    total_qualifiers = sorted({q for row in rows if row["total"] is not None for q in row["qualifiers"]})
    if report.get("complete") is False:
        total_qualifiers = sorted(set(total_qualifiers) | {"range too long: only the newest records were aggregated"})
    if prices:
        totals.update(qualifiers=total_qualifiers, exact=not total_qualifiers and not unpriced)
    return api.document("ledger-value", range=report["range"], timezone=report["timezone"], prices=prices,
                        currency=(prices or {}).get("currency"), models=rows, totals=totals if prices else None,
                        unpriced=unpriced, flags=flags,
                        basis="input = computed prefill + RTX-imported tokens; cached = restored from the engine's prefix "
                              "cache; output = generated tokens (C8); prices per million tokens",
                        why=why)


def _qualifiers(model):
    """The material uncertainty of a model's priced quantities, in words (empty when every quantity is exact)."""
    out = []
    if model["input_partial"]:
        out.append("input not reported for part of the range (input tokens are a lower bound)")
    if model["output_partial"]:
        out.append("output not reported for part of the range (output tokens are a lower bound)")
    if model["estimated"]:
        out.append("input partly an upper bound (cache-restored or imported tokens not reported)")
    if model["unavailable_input"]:
        out.append("some input not priced (its computed share is unavailable)")
    if model["cached"] is None:
        out.append("cache-restored tokens not reported (no cached cost or savings)")
    elif model["cached_tag"] == "partial":
        out.append("cache-restored tokens reported for part of the range only")
    if model["uncertain"]:
        out.append("some tokens have an approximate hour")
    if model["blind_tokens"]:
        out.append("some tokens come from engines without an instance identity")
    return out


# ---------------------------------------------------------------------------------------------------------------------

def register(reg):
    reg.get("ledger", ledger)
    reg.get("ledger/export", export)
    reg.get("ledger/census", census)
    reg.get("ledger/rtx", rtx)
    reg.get("ledger/value", value)
    reg.post("ledger/reset", reset, audited=True)
    reg.post("ledger/settings", save_settings, audited=True)
    reg.post("ledger/prices", save_prices, audited=True)
    reg.post("ledger/rtx-parse", parse_run, audited=True)
    reg.collector("ledger", collect, interval=INTERVAL)
    reg.collector("ledger-files", collect_files, interval=FILES_INTERVAL)
    reg.capability("token_ledger", True, "engine token counters every 10 s; hour records per model, machine and kind")
