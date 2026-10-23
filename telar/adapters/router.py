"""TensorMeld replica router status v1."""

from dataclasses import dataclass

from . import Reader, check_schema


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
