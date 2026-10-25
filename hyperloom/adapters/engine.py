"""Shared engine status/metrics, including optional DeepSeek telemetry."""

from dataclasses import dataclass
import math

from . import Reader, check_schema, prom


SCHEMA = "strix-serve-status-v1"
TTFT_LABEL = "Engine-side TTFT (queue+prefill)"
THROUGHPUT_LABEL = "completed tok/s over the sampling window"
_REQUEST = {"id": str, "route": str, "time": int, "status": str, "stop_reason": str,
            **dict.fromkeys(("prompt_tokens", "reused_tokens", "output_tokens",
                             "mtp_cycles", "mtp_accepted", "mtp_proposed"), int),
            **dict.fromkeys(("queue_ms", "prefill_ms", "decode_ms", "duration_ms",
                             "prefill_tok_s", "decode_tok_s"), float)}
METRICS = {
    "completed": "strix_requests_completed_total",
    "failed": "strix_requests_failed_total",
    "cancelled": "strix_requests_cancelled_total",
    "prompt_tokens": "strix_prompt_tokens_total",
    "cached_tokens": "strix_prompt_tokens_cached_total",
    "generated_tokens": "strix_generated_tokens_total",
    "busy_seconds": "strix_engine_busy_seconds_total",
    "waiting": "strix_queue_waiting",
    "running": "strix_queue_running",
    "slots_busy": "strix_slots_busy",
    "batch_steps": "strix_batch_steps_total",
    "batch_rows": "strix_batch_rows_total",
    "group_exchanges": "strix_group_exchanges_total",
}


@dataclass
class EngineStatus:
    model: str | None
    engine: str | None
    state: str | None
    context_tokens: int | None
    uptime_seconds: float | None
    queue: dict | None
    totals: dict | None
    requests: list | None
    slots: dict | None
    memory: dict | None
    batch: dict | None
    prefill: dict | None
    group: dict | None
    why: dict[str, str]
    ttft_label: str = TTFT_LABEL


@dataclass
class EngineMetrics:
    completed: float | None
    failed: float | None
    cancelled: float | None
    prompt_tokens: float | None
    cached_tokens: float | None
    generated_tokens: float | None
    busy_seconds: float | None
    waiting: float | None
    running: float | None
    slots_busy: float | None
    batch_steps: float | None
    batch_rows: float | None
    group_exchanges: float | None
    why: dict[str, str]
    throughput_label: str = THROUGHPUT_LABEL


def parse_status(obj) -> EngineStatus:
    check_schema(obj, SCHEMA)
    r = Reader(obj)
    values = {key: r.get(key, kind) for key, kind in {
        "model": str, "engine": str, "state": str, "context_tokens": int,
        "uptime_seconds": float}.items()}
    values["queue"] = r.record("queue", dict.fromkeys(
        ("queued", "running", "completed", "failed", "cancelled"), int))
    values["totals"] = r.record("totals", {**dict.fromkeys(
        ("requests", "prompt_tokens", "cached_tokens", "generated_tokens"), int), "busy_seconds": float})
    values["requests"] = r.records("requests", _REQUEST)
    for index, request in enumerate(values["requests"] or []):
        if request is None:
            continue
        queue, prefill = request["queue_ms"], request["prefill_ms"]
        request["engine_ttft_ms"] = None if queue is None or prefill is None else queue + prefill
        if request["engine_ttft_ms"] is None:
            r.why[f"requests[{index}].engine_ttft_ms"] = "requires queue_ms and prefill_ms"
    values["slots"] = r.record("slots", dict.fromkeys(
        ("total", "busy", "free", "context_tokens", "pool_positions", "pool_bytes", "pool_largest_free"), int))
    values["memory"] = r.record("memory", {**dict.fromkeys(
        ("weights_bytes", "state_bytes_per_slot", "state_bytes", "workspace_bytes",
         "trellis_bytes", "device_free_bytes", "device_total_bytes"), int), "trellis_identity": str})
    values["batch"] = r.record("batch", {**dict.fromkeys(
        ("steps", "rows", "decode_rows", "prompt_rows"), int),
        "mean_rows_per_step": float, "mean_step_ms": float})
    values["prefill"] = r.record("prefill", {"path": str, "prefills": int, "prefill_tokens": int,
        "cached_tokens": int, "prefill_seconds": float, "prefix_cache": dict})
    values["group"] = r.record("group", {"group_id": str, "epoch": int, "rank": int,
        "world": int, "state": str, "transport": str, "exchanges": int,
        "bytes_sent": int, "exchange_seconds": float, "mean_exchange_us": float})
    return EngineStatus(**values, why=r.why)


def parse_metrics(text) -> EngineMetrics:
    samples = prom.parse(text)
    r = Reader({field: samples.get((name, ())) for field, name in METRICS.items()})
    return EngineMetrics(**{field: r.get(field, float) for field in METRICS}, why=r.why)


# Observability fields (C1, C2, C8) the instrumented engines add. Additive: parse_status/parse_metrics above are
# unchanged; these read only the new, optional fields; absent or null ones are None without a reason.

METRICS_EXTRA = {"imported_tokens": "strix_prompt_tokens_imported_total",
                 "computed_tokens": "strix_prompt_tokens_computed_total",
                 "start_time": "strix_engine_start_time_seconds", "armed": "strix_instrumentation_armed"}
REQUEST_EXTRA = {"first_token_ms": float, "cached_tokens": int, "imported_tokens": int, "computed_tokens": int,
                 "request_id": str, "response_id": str, "stream": bool}
FALLBACK = {"cached_tokens": "reused_tokens"}
OBSERVED = {"since": str, "requests": int, "prompt_tokens": int, "cached_tokens": int, "imported_tokens": int,
            "computed_tokens": int}
MAX_DEPTH, MAX_FLAT, MAX_WINDOW = 64, 64, 16


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _optional(value, fields):
    reader = Reader(value)
    return {key: reader.get(key, kind) if isinstance(value, dict) and value.get(key) is not None else None
            for key, kind in fields.items()}


def _counts(value, limit=MAX_WINDOW):
    return [v for v in value[:limit] if type(v) is int and v >= 0] if isinstance(value, list) else None


def request_fields(value):
    """The C1/C8 fields of one recent-request record and its draft counts. ``reused_tokens`` stands for
    ``cached_tokens`` on builds that predate C8; draft counts come from ``draft`` or the ``mtp_*`` names."""
    if not isinstance(value, dict):
        return None
    fields = _optional(value, REQUEST_EXTRA)
    for key, old in FALLBACK.items():
        if fields[key] is None and value.get(old) is not None:
            fields[key] = _optional(value, {old: int})[old]
    draft = value.get("draft")
    if isinstance(draft, dict):
        counts = _optional(draft, {"cycles": int, "proposed": int, "accepted": int})
    else:
        legacy = _optional(value, {"mtp_cycles": int, "mtp_proposed": int, "mtp_accepted": int})
        counts = {"cycles": legacy["mtp_cycles"], "proposed": legacy["mtp_proposed"], "accepted": legacy["mtp_accepted"]}
    fields.update(draft_cycles=counts["cycles"], draft_proposed=counts["proposed"], draft_accepted=counts["accepted"])
    for key in ("request_id", "response_id"):
        if fields[key] is not None:
            fields[key] = fields[key][:80]
    return fields


MAX_IDENTITY = 128


def identity_text(value):
    """An engine or router instance id exactly as reported, or None when it is not a printable string of at most
    128 characters: an identity is never truncated (two processes sharing a prefix must stay apart)."""
    if isinstance(value, str) and value and len(value) <= MAX_IDENTITY and value.isprintable():
        return value
    return None


def instance_of(obj):
    """(instance id, start time) from ``instance: {id, start_time}`` (or a plain id string with ``started_at``)."""
    value = obj.get("instance")
    if isinstance(value, dict):
        inner = _optional(value, {"id": str, "start_time": float})
        return identity_text(inner["id"]), inner["start_time"]
    started = _optional(obj, {"started_at": float})["started_at"]
    return identity_text(value), started


def json_histogram(value):
    """A histogram in a status document -> {buckets: [[le, cumulative count]], sum, count}, or None. Accepts
    ``{buckets: [{le, count}], sum, count}`` and ``{le: [...], counts: [...], sum, count}`` (the last bound +Inf)."""
    if not isinstance(value, dict):
        return None
    pairs = []
    if isinstance(value.get("buckets"), list):
        for item in value["buckets"][:64]:
            if isinstance(item, dict):
                pairs.append((item.get("le"), item.get("count")))
    elif isinstance(value.get("le"), list) and isinstance(value.get("counts"), list):
        bounds = list(value["le"][:64])
        if len(value["counts"]) == len(bounds) + 1:
            bounds.append("+Inf")
        pairs = list(zip(bounds, value["counts"][:64]))
    buckets = []
    for le, count in pairs:
        try:
            bound = math.inf if le in ("+Inf", "Inf", None) else float(le)
        except (TypeError, ValueError):
            return None
        if not _number(count) or (buckets and (bound <= buckets[-1][0] or count < buckets[-1][1])):
            return None
        buckets.append([bound, count])
    if not buckets or buckets[-1][0] != math.inf:
        return None
    total = value.get("count")
    return {"buckets": buckets, "sum": value.get("sum") if _number(value.get("sum")) else None,
            "count": total if _number(total) else buckets[-1][1]}


def _flat(value, limit=MAX_FLAT):
    """A nested dict of counters flattened to at most ``limit`` non-negative numbers (``a.b`` keys); other values
    dropped."""
    flat = {}
    for key, item in list(value.items())[:limit]:
        if isinstance(item, dict):
            for inner, number in list(item.items())[:limit]:
                if _number(number) and len(flat) < limit:
                    flat[f"{key}.{inner}"[:64]] = number
        elif _number(item) and len(flat) < limit:
            flat[str(key)[:64]] = item
    return flat


def _depth(rows):
    out = []
    for item in (rows or [])[:MAX_DEPTH]:
        if isinstance(item, dict):
            row = _optional(item, {"depth": int, "reached": int, "hit": int})
            if row["depth"] is not None:
                out.append(row)
    return out or None


def speculative_of(obj):
    """The speculative block in one shape: {method, scope, since, cycles, proposed, accepted, generated_tokens,
    requests, draft_seconds, depth [{depth, reached, hit}], window_rows, window_label}. Reads the DeepSeek ``dspark``
    block (speculator counters for the process lifetime) or the shared ``speculative`` block (counts since arming)."""
    dspark = obj.get("dspark")
    if isinstance(dspark, dict):
        values = _optional(dspark, {"drafted_steps": int, "drafts_verified": int, "drafts_accepted": int,
                                    "draft_seconds": float, "steps": int, "decode_tokens": int})
        return {"method": "dspark", "scope": "speculator", "since": "process start", "cycles": values["drafted_steps"],
                "proposed": values["drafts_verified"], "accepted": values["drafts_accepted"],
                "generated_tokens": values["decode_tokens"], "requests": None, "draft_seconds": values["draft_seconds"],
                "depth": _depth(dspark.get("depth")), "window_rows": _counts(dspark.get("window_rows")),
                "window_label": "rows per drafted window"}
    spec = obj.get("speculative")
    if not isinstance(spec, dict):
        return None
    values = _optional(spec, {"method": str, "scope": str, "since": str, "cycles": int, "proposed": int,
                              "accepted": int, "generated_tokens": int, "requests": int, "draft_seconds": float})
    depth = _depth(spec.get("depth"))
    if depth is None and isinstance(spec.get("reached"), list):
        hits = spec.get("hit") if isinstance(spec.get("hit"), list) else []
        depth = [{"depth": i + 1, "reached": r if type(r) is int and r >= 0 else None,
                  "hit": hits[i] if i < len(hits) and type(hits[i]) is int and hits[i] >= 0 else None}
                 for i, r in enumerate(spec["reached"][:MAX_DEPTH])]
    window, label = _counts(spec.get("verifier_window_rows")), "cycles by target rows the verifier chose"
    if window is None:
        window, label = _counts(spec.get("window_rows")), "rows per drafted window"
    return {**values, "method": values["method"] or "mtp", "since": values["since"] or "unknown",
            "depth": depth, "window_rows": window, "window_label": label if window is not None else None}


def parse_observed(obj):
    """{instance, started_at, armed, armed_since, observed, totals, speculative, prefix_cache, histograms, requests,
    why} of an engine status document (schema checked). ``observed`` is the C8 block counted since arming;
    ``histograms`` the status histograms in the /metrics family shape."""
    check_schema(obj, SCHEMA)
    instance, started = instance_of(obj)
    armed = obj.get("armed")
    since = _optional(obj, {"armed_since": float})["armed_since"]
    if isinstance(armed, dict):
        since = since if since is not None else _optional(armed, {"since": float})["since"]
        armed = True
    result = {"instance": instance, "started_at": started, "armed": armed if isinstance(armed, bool) else None,
              "armed_since": since}
    totals = obj.get("totals") if isinstance(obj.get("totals"), dict) else {}
    result["totals"] = _optional(totals, {"imported_tokens": int, "computed_tokens": int})
    observed = obj.get("observed")
    result["observed"] = _optional(observed, OBSERVED) if isinstance(observed, dict) else None
    families = {}
    for name in ("ttft_seconds", "decode_tokens_per_second"):
        source = (observed or {}).get(name) if isinstance(observed, dict) else None
        if source is None and isinstance(obj.get("latency"), dict):
            source = obj["latency"].get(name if name != "decode_tokens_per_second" else "decode_tok_s")
        parsed = json_histogram(source)
        if parsed:
            families[name] = [{"labels": {}, **parsed}]
    result["histograms"] = families or None
    result["speculative"] = speculative_of(obj)
    prefill = obj.get("prefill") if isinstance(obj.get("prefill"), dict) else {}
    cache = prefill.get("prefix_cache")
    result["prefix_cache"] = _flat(cache) if isinstance(cache, dict) else None
    requests = []
    for value in (obj.get("requests") if isinstance(obj.get("requests"), list) else [])[:512]:
        fields = request_fields(value)
        if fields is None or not isinstance(value.get("id"), str):
            continue
        requests.append({"id": value["id"][:80], **fields})
    result["requests"] = requests
    result["why"] = {}
    return result


def parse_metrics_extra(text):
    """{imported_tokens, computed_tokens, start_time, armed, instance, histograms} from an engine's /metrics text
    (histograms: the generic strix_*_bucket families)."""
    from .router import histograms
    samples = prom.parse(text)
    result = {}
    for key, name in METRICS_EXTRA.items():
        value = samples.get((name, ()))
        result[key] = value if value is not None and _number(value) else None
    result["instance"] = next((identity_text(dict(labels)["instance"]) for (name, labels), _ in samples.items()
                               if name == "strix_engine_info" and dict(labels).get("instance")), None)
    result["histograms"] = histograms(samples, "strix_")
    return result
