"""MiniMax serve telemetry: its own metric names mapped onto the shared engine fields.

The MiniMax server exposes ``mm_*`` lines (queue, totals and, on the scheduled serve loop, step/prefill/draft
counters); an instrumented build adds the shared ``strix_*`` names and ``/api/status`` in the shared schema. When both
names of a field are present, the shared one wins. ``/api/status`` goes through ``engine.parse_status``.
"""

from dataclasses import asdict
import math

from . import Reader, engine, prom


# shared EngineMetrics field -> MiniMax name
NAMES = {
    "completed": "mm_completed",
    "failed": "mm_failed",
    "prompt_tokens": "mm_prompt_tokens",
    "generated_tokens": "mm_generated_tokens",
    "busy_seconds": "mm_busy_seconds",
    "waiting": "mm_queued",
    "running": "mm_running",
    "batch_steps": "mm_steps",
    "batch_rows": "mm_step_rows",
}
# MiniMax-only counters (scheduled serve loop), kept under these keys
EXTRA = {"step_seconds": "mm_step_seconds", "prefill_seconds": "mm_prefill_seconds", "draft_seconds": "mm_draft_seconds",
         "idle_seconds": "mm_idle_seconds", "decode_stall_seconds": "mm_decode_stall_seconds",
         "prefill_units": "mm_prefill_units", "draft_cycles": "mm_draft_cycles", "drafted": "mm_drafted",
         "accepted": "mm_accepted"}


def parse_metrics(text):
    """-> (EngineMetrics, extra dict). Accepts the plain ``mm_*`` exposition (no HELP/TYPE) and the shared one."""
    samples = prom.parse(text)
    shared = {field: samples.get((name, ())) for field, name in engine.METRICS.items()}
    values = {}
    for field in engine.METRICS:
        value = shared[field]
        if value is None and field in NAMES:
            value = samples.get((NAMES[field], ()))
        values[field] = value
    r = Reader(values)
    metrics = engine.EngineMetrics(**{field: r.get(field, float) for field in engine.METRICS}, why=r.why)
    for field in engine.METRICS:
        if values[field] is None:
            metrics.why[field] = "not reported by this MiniMax build"
    extra = engine.parse_metrics_extra(text)
    for key, name in EXTRA.items():
        value = samples.get((name, ()))
        extra[key] = value if value is not None and math.isfinite(value) and value >= 0 else None
    return metrics, extra


def speculative(extra):
    """Cumulative draft counters of the scheduled serve loop as the shared speculative block, or None."""
    if extra.get("drafted") is None or extra.get("accepted") is None:
        return None
    return {"method": "draft", "scope": "serve loop", "since": "process start",
            "cycles": None if extra.get("draft_cycles") is None else int(extra["draft_cycles"]),
            "accepted": int(extra["accepted"]), "proposed": int(extra["drafted"]), "generated_tokens": None,
            "requests": None, "draft_seconds": extra.get("draft_seconds"), "depth": None, "window_rows": None,
            "window_label": None}


def as_dict(metrics):
    return asdict(metrics)
