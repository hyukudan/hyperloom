"""CUDA prefill endpoints (the pinned component emits no schema field)."""

from dataclasses import dataclass

from . import AdapterError, Reader


@dataclass
class PrefillHealth:
    status: str | None
    busy: bool | None
    queued: int | None
    handoff_pending: int | None
    served: int | None
    batches: int | None
    max_positions: int | None
    position: int | None
    total: int | None
    elapsed_s: float | None
    cancel_pending: bool | None
    eta_s: float | None
    host_free_commit_bytes: int | None
    why: dict[str, str]


@dataclass
class PrefillDescribe:
    describe: dict | None
    service: dict | None
    device_free_gb: float | None
    device_total_gb: float | None
    why: dict[str, str]


def _reader(obj):
    if not isinstance(obj, dict):
        raise AdapterError("prefill response must be an object")
    if "schema" in obj:
        raise AdapterError("unsupported prefill schema; pinned endpoints are unversioned")
    r = Reader(obj)
    r.why["schema"] = "pinned prefill endpoint has no schema id"
    return r


def parse_health(obj) -> PrefillHealth:
    r = _reader(obj)
    fields = {"status": str, "busy": bool, "queued": int, "handoff_pending": int,
              "served": int, "batches": int, "max_positions": int, "position": int,
              "total": int, "elapsed_s": float, "cancel_pending": bool, "eta_s": float,
              "host_free_commit_bytes": int}
    values = {key: r.get(key, kind) for key, kind in fields.items()}
    if values["status"] not in (None, "ok", "failed"):
        values["status"] = None
        r.why["status"] = "unknown prefill status"
    return PrefillHealth(**values, why=r.why)


def parse_describe(obj) -> PrefillDescribe:
    r = _reader(obj)
    describe = r.get("describe", dict)
    service = r.record("service", {
        **dict.fromkeys(("fast_dense", "fast_moe", "fast_attention", "fast_attention_available",
                        "expert_residency", "auto_budget", "timing", "pipeline", "failed", "busy"), bool),
        **dict.fromkeys(("fast_attention_min_rows", "max_positions", "max_queue", "handoff_queue",
                        "batch_max", "batch_sum_tokens", "batch_alone_rows", "batch_max_overtakes",
                        "batch_contexts", "served", "queued", "batches"), int),
        "vram_cap_gib": float, "load_s": float,
        "device_gb": float, "pinned_gb": float, "prefill_seconds_total": float})
    return PrefillDescribe(describe, service, r.get("device_free_gb", float),
                           r.get("device_total_gb", float), r.why)
