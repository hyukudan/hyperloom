"""Read-only session-cache responses; supplied engine serializers define the contract."""
import re

from . import AdapterError, Reader

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
PARSE_LIMIT = 1024
MAX_EXAMPLES = 5
KINDS = ("qwen", "deepseek", "minimax")
STATS = ("ram_bytes", "ram_budget_bytes", "entries", "entry_limit", "evictions", "hits", "misses", "refusals")


def _check(kind, obj):
    if kind not in KINDS:
        raise AdapterError("unsupported session cache engine")
    if not isinstance(obj, dict):
        raise AdapterError("expected session cache object")


def parse_list(engine_kind, obj):
    _check(engine_kind, obj)
    r = Reader(obj)
    values = r.get("sessions", list)
    if values is None:
        return {"sessions": None, "why": r.why}
    rows = []
    invalid = 0
    examples = []
    for value in values[:PARSE_LIMIT]:
        identity = value.get("id") if engine_kind == "qwen" and isinstance(value, dict) else value
        if not isinstance(identity, str) or not SESSION_ID.fullmatch(identity) or (engine_kind == "qwen" and not isinstance(value, dict)):
            invalid += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append("invalid session id dropped")
            continue
        rows.append({"id": identity, "location": "unknown", "tokens": None, "position": None,
                     "bytes": None, "generation": None, "last_used": None,
                     "why": {key: "not reported by this engine" for key in
                             ("location", "tokens", "position", "bytes", "generation", "last_used")}})
        if engine_kind == "qwen":
            presence = Reader(value)
            parked, disk = presence.get("parked", bool), presence.get("disk", bool)
            row = rows[-1]
            if parked is not None and disk is not None and (parked or disk):
                row["location"] = "ram+disk" if parked and disk else "ram" if parked else "disk"
                row["why"].pop("location")
            else:
                row["why"]["location"] = "invalid or absent RAM/disk presence" if presence.why else "no RAM/disk presence reported"
            row["why"].update(presence.why)
    if invalid:
        r.why["invalid_entries"] = {"count": invalid, "examples": examples}
    if len(values) > PARSE_LIMIT:
        r.why["omitted_entries"] = len(values) - PARSE_LIMIT
    return {"sessions": rows, "why": r.why}


def parse_stats(engine_kind, obj):
    _check(engine_kind, obj)
    mapping = {"qwen": {"ram_bytes": "ram_bytes", "ram_budget_bytes": "ram_budget",
                        "entries": "parked", "entry_limit": "max_parked", "evictions": "evicted",
                        "hits": "hits", "misses": "misses"},
               "deepseek": {"ram_bytes": "ram_bytes", "entries": "ram_entries",
                             "evictions": "evictions", "refusals": "refused"},
               "minimax": {"ram_bytes": "bytes", "entries": "entries", "evictions": "evictions"}}.get(engine_kind, {})
    values, why = {}, {}
    r = Reader(obj)
    for field in STATS:
        source = mapping.get(field)
        values[field] = r.get(source, int) if source else None
        if source in r.why:
            why[field] = r.why[source]
        elif not source:
            why[field] = "not reported by this engine"
    return {**values, "why": why}
