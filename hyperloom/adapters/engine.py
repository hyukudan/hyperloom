"""Shared engine status/metrics, including optional DeepSeek telemetry."""

from dataclasses import dataclass

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
