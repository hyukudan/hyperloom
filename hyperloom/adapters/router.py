"""TensorMeld replica router: status v1, and the request log, sessions and histograms of the request timeline."""

from dataclasses import dataclass
import math

from . import AdapterError, Reader, check_schema
from .engine import identity_text


SCHEMA = "tensormeld/replica-router-status-v1"
_REPLICA = {"id": str, "node": str, "endpoint": str, "state": str,
            "inflight": int, "max_inflight": int, "assigned": int,
            "completed": int, "failed": int, "last_error": str}


@dataclass
class RouterStatus:
    status: str | None
    held: bool | None
    waiting: int | None
    rejected: int | None
    affinity: dict | None
    replicas: list | None
    prefill: dict | None
    why: dict[str, str]


def parse_status(obj) -> RouterStatus:
    check_schema(obj, SCHEMA)
    r = Reader(obj)
    status = r.get("status", str)
    held = r.get("held", bool)
    waiting, rejected = r.get("waiting", int), r.get("rejected", int)
    affinity = r.record("affinity", {"mode": str, "hits": int, "yields": int, "none": int})
    replicas = r.records("replicas", {**_REPLICA, "admitting": bool, "last_health_ms": float})
    prefill = r.record("prefill", {"policy": str, "offload_ms_total": float, "pending_s": dict})
    if prefill is not None:
        prefill["placements"] = r.record("prefill.placements", dict.fromkeys(
            ("local", "offload", "fallback", "cache"), int))
        prefill["services"] = r.records("prefill.services", _REPLICA)
    return RouterStatus(status, held, waiting, rejected, affinity, replicas, prefill, r.why)


# Request log (C1), sessions (F16) and histograms (C2) of the request-timeline router. Unknown fields are ignored;
# wrong types become None with a reason; null means "not reached / not known" and carries no reason; every string
# and list is bounded.

REQUESTS_SCHEMA = "tensormeld/router-requests-v1"
SESSIONS_SCHEMA = "tensormeld/router-sessions-v1"
STAMPS = ("arrived", "body_read", "tokenized", "slot_acquired", "route_decided", "handoff_started", "handoff_done",
          "upstream_headers", "first_body_byte", "first_data_event", "last_byte")
OUTCOMES = ("served", "client_gone", "upstream_error", "refused", "no_replica", "tokenize_failed", "cancelled")
STATES = ("in_flight", "done")
USAGE = ("prompt_tokens", "completion_tokens", "cached_tokens", "imported_tokens", "uncached_tokens")
ENGINE = {"queue_ms": float, "prefill_ms": float, "decode_ms": float, "first_token_ms": float,
          "mtp_accepted": int, "mtp_proposed": int}
MAX_RECORDS, MAX_ATTEMPTS, MAX_DECISIONS, MAX_TEXT, MAX_SESSIONS, MAX_TURNS = 512, 16, 16, 200, 2000, 16
_ATTEMPT = {"n": int, "kind": str, "target": str, "at_ms": float, "outcome": str, "why": str}
_DECISION = {"at_ms": float, "what": str, "target": str, "why": str}
_RECORD = {"seq": int, "id": str, "client_id": str, "at": float, "path": str, "policy": str, "caller": str,
           "session": str, "state": str, "outcome": str, "status": int, "why": str, "replica": str, "route": str,
           "prefill": str, "stream": bool, "queue_ms": float, "data_events": int, "bytes": int, "model": str,
           "attempts_dropped": int, "decisions_dropped": int, "route_trace_dropped": int, "route_uncached_tokens": int,
           "usage_why": str}
_TRACE = {"route": str, "result": str}
MAX_TRACE = 16
_SESSION = {"key": str, "kind": str, "path": str, "first_seen": float, "last_seen": float, "turns": int,
            "replica": str}
_TURN = {"at": float, "id": str, "replica": str, "outcome": str, "prompt_tokens": int, "cached_tokens": int,
         "imported_tokens": int, "uncached_tokens": int}
TURN_LISTS = ("recent", "last_turns", "turn_log", "history")
MAX_BUCKETS, MAX_LABELS, MAX_LABEL_TEXT = 64, 8, 128


def _text(value):
    return value[:MAX_TEXT] if isinstance(value, str) else value


def _fields(value, fields, why=None, prefix=""):
    """The given fields of a dict: absent or null -> None (no reason); a wrong type -> None with a reason."""
    reader = Reader(value)
    out = {}
    for key, kind in fields.items():
        if value.get(key) is None:
            out[key] = None
            continue
        out[key] = _text(reader.get(key, kind))
    if why is not None:
        why.update({prefix + key: reason for key, reason in reader.why.items()})
    return out


def _items(value, fields, limit, why, name):
    rows = []
    if isinstance(value, list):
        for item in value[:limit]:
            if isinstance(item, dict):
                rows.append(_fields(item, fields))
        if len(value) > limit:
            why[name] = f"{len(value) - limit} further entries not kept"
    return rows


def _attempts(value, why):
    """At most 16 attempts, always including the ones a join needs (the served or last decode attempt, the last
    prefill attempt and the last successful one), whatever their position."""
    if not isinstance(value, list):
        return []
    rows = [_fields(item, _ATTEMPT) for item in value if isinstance(item, dict)]
    if len(rows) <= MAX_ATTEMPTS:
        return rows
    decode = [r for r in rows if r["kind"] == "decode"]
    served = [r for r in decode if r["outcome"] == "served"]
    prefill = [r for r in rows if r["kind"] == "prefill"]
    ok = [r for r in prefill if r["outcome"] == "ok"]
    key = [r for r in ((served or decode or [None])[-1], (prefill or [None])[-1], (ok or [None])[-1]) if r is not None]
    kept = rows[:MAX_ATTEMPTS - len(key)]
    kept += [r for r in key if r not in kept]
    why["attempts"] = f"{len(rows) - len(kept)} further attempts not kept"
    return sorted(kept, key=lambda r: (r["n"] is None, r["n"] or 0))


@dataclass
class RouterRequests:
    instance: str | None
    started_at: float | None
    capacity: int | None
    seq: int | None
    evicted: int | None
    armed: bool | None
    next: int | None
    more: bool | None
    records: list
    why: dict[str, str]


def _schema(obj, expected, why):
    if not isinstance(obj, dict):
        raise AdapterError("router document must be an object")
    if "schema" not in obj:
        why["schema"] = "router document has no schema id"
    elif obj["schema"] != expected:
        raise AdapterError(f"expected schema {expected}")


def parse_record(value):
    """One request record -> (record or None, parse reasons). The router's own ``why`` (its text reason for the
    outcome) is kept as is; this adapter's reasons go to ``parse_why``."""
    if not isinstance(value, dict):
        return None, {"record": "expected object"}
    why = {}
    record = _fields(value, _RECORD, why)
    if record["id"] is None or record["seq"] is None:
        return None, {**why, "record": "record without id or seq"}
    if record["state"] not in (None, *STATES):
        why["state"] = "unknown state " + record["state"]
        record["state"] = "other"
    if record["outcome"] not in (None, *OUTCOMES):
        why["outcome"] = "unknown outcome " + record["outcome"]
        record["outcome"] = "other"
    stamps = value.get("stamps") if isinstance(value.get("stamps"), dict) else {}
    record["stamps"] = {k: v for k, v in _fields(stamps, dict.fromkeys(STAMPS, float), why, "stamps.").items()
                        if v is not None}
    record["attempts"] = _attempts(value.get("attempts"), why)
    record["decisions"] = _items(value.get("decisions"), _DECISION, MAX_DECISIONS, why, "decisions")
    record["route_trace"] = _items(value.get("route_trace"), _TRACE, MAX_TRACE, why, "route_trace")
    usage, engine = value.get("usage"), value.get("engine")
    record["usage"] = _fields(usage, dict.fromkeys(USAGE, int), why, "usage.") if isinstance(usage, dict) else None
    record["engine"] = _fields(engine, ENGINE, why, "engine.") if isinstance(engine, dict) else None
    record["parse_why"] = why
    return record, why


def parse_requests(obj) -> RouterRequests:
    why = {}
    _schema(obj, REQUESTS_SCHEMA, why)
    instance = obj.get("instance")
    if instance is not None and identity_text(instance) is None:
        # The page's identity decides restarts: one that is not kept exactly makes the page unusable.
        raise AdapterError("router request log instance is not a printable identity of at most 128 characters")
    head = _fields(obj, {"instance": str, "started_at": float, "capacity": int, "seq": int, "evicted": int,
                         "armed": bool, "next": int, "more": bool}, why)
    head["instance"] = identity_text(instance)
    rows = obj.get("records")
    if not isinstance(rows, list):
        raise AdapterError("router request log without records")
    records = []
    for index, value in enumerate(rows[:MAX_RECORDS]):
        record, reasons = parse_record(value)
        if record is None:
            why[f"records[{index}]"] = reasons.get("record", "invalid record")
            continue
        records.append(record)
    if len(rows) > MAX_RECORDS:
        why["records"] = f"{len(rows) - MAX_RECORDS} records beyond {MAX_RECORDS} ignored"
    return RouterRequests(**head, records=records, why=why)


def parse_sessions(obj):
    """-> (head {instance, idle_s, armed, count}, sessions, why). A session carries the router's pseudonymous key,
    never conversation text."""
    why = {}
    _schema(obj, SESSIONS_SCHEMA, why)
    head = _fields(obj, {"instance": str, "idle_s": float, "armed": bool, "count": int}, why)
    rows = obj.get("sessions")
    if not isinstance(rows, list):
        raise AdapterError("router sessions document without sessions")
    sessions = []
    for value in rows[:MAX_SESSIONS]:
        if not isinstance(value, dict):
            continue
        session = _fields(value, _SESSION)
        if session["key"] is None:
            continue
        replicas = value.get("replicas")
        session["replicas"] = ({str(k)[:MAX_TEXT]: v for k, v in list(replicas.items())[:8] if type(v) is int and v >= 0}
                               if isinstance(replicas, dict) else None)
        turns = next((value[name] for name in TURN_LISTS if isinstance(value.get(name), list)), None)
        session["recent"] = None if turns is None else [
            _fields(item, _TURN) for item in turns[-MAX_TURNS:] if isinstance(item, dict)]
        sessions.append(session)
    if len(rows) > MAX_SESSIONS:
        why["sessions"] = f"{len(rows) - MAX_SESSIONS} sessions beyond {MAX_SESSIONS} ignored"
    return head, sessions, why


def histograms(samples, prefix):
    """Histogram families of parsed metrics-text samples whose name starts with ``prefix``:
    {family: [{labels, buckets: [[le, cumulative count]], sum, count}]}, cumulative since the component started.
    A label set whose buckets decrease, lack +Inf or hold non-finite counts is dropped. Bounds: 64 buckets per label
    set, 8 labels of at most 128 characters (longer label sets are dropped)."""
    grouped = {}
    for (name, labels), value in samples.items():
        if not name.startswith(prefix) or not name.endswith("_bucket"):
            continue
        family = name[len(prefix):-len("_bucket")]
        le = dict(labels).get("le")
        if not family or le is None or len(labels) > MAX_LABELS + 1 or any(len(v) > MAX_LABEL_TEXT for _, v in labels):
            continue
        try:
            bound = float(le)
        except ValueError:
            continue
        rest = tuple(item for item in labels if item[0] != "le")
        grouped.setdefault(family, {}).setdefault(rest, []).append([bound, value])
    result = {}
    for family, sets in sorted(grouped.items()):
        lines = []
        for labels, buckets in sorted(sets.items()):
            buckets.sort(key=lambda b: b[0])
            counts = [b[1] for b in buckets]
            if (len(buckets) > MAX_BUCKETS or buckets[-1][0] != math.inf or any(not math.isfinite(c) or c < 0 for c in counts)
                    or any(later < earlier for earlier, later in zip(counts, counts[1:]))):
                continue
            total = samples.get((prefix + family + "_count", labels))
            total_sum = samples.get((prefix + family + "_sum", labels))
            lines.append({"labels": dict(labels), "buckets": [[b, c] for b, c in buckets],
                          "sum": total_sum if total_sum is not None and math.isfinite(total_sum) else None,
                          "count": total if total is not None and math.isfinite(total) else counts[-1]})
        if lines:
            result[family] = lines
    return result


def quantile(buckets, q):
    """Estimate quantile ``q`` from cumulative ``[[le, count], ...]`` by linear interpolation inside the bucket that
    holds it (an estimate: a histogram keeps no samples). None without samples; the last finite bound when the
    quantile falls in the +Inf bucket."""
    total = buckets[-1][1] if buckets else 0
    if not total:
        return None
    rank = q * total
    lower_bound, lower_count = 0.0, 0.0
    for bound, count in buckets:
        if count >= rank and count > lower_count:
            if bound == math.inf:
                return lower_bound
            return lower_bound + (bound - lower_bound) * (rank - lower_count) / (count - lower_count)
        if bound != math.inf:
            lower_bound = bound
        lower_count = count
    return lower_bound
