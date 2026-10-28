"""Benchmark history: listing, comparison of any two runs of a kind, and the share summary (F29).

The share summary is the only thing a share card (text or PNG) is drawn from. It carries no configured or
endpoint-reported string: the target is the engine family id from hyperloom's own adapter table (for a profile) or
"ad-hoc endpoint", the model is not included, the recipe digest only as 64 hex, and the numbers of the kind's own
allow-list (no prompts, replies, addresses, paths, request or session ids).
"""

import math
import re
import time

from hyperloom import engines

from . import ab, decode, demo, prefill, quality, store as bench_store, tools


SHARE = {"decode": decode.share, "prefill": prefill.share, "ab": ab.share, "quality": quality.share,
         "tools": tools.share, "demo": demo.share}


def _clean(value):
    """Numbers stay finite numbers, strings must be short plain words; anything else is dropped."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if re.fullmatch(r"[A-Za-z0-9 .,()%+\-]{0,120}", value) else None
    if isinstance(value, list):
        return [_clean(v) for v in value[:64]]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in list(value.items())[:64] if re.fullmatch(r"[A-Za-z0-9_ .-]{1,40}", str(k))}
    return None


def share(run):
    label = lambda family: family if family in engines.FAMILIES else "ad-hoc endpoint"
    summary = run["summary"] or {}
    if run["kind"] == "ab":
        families = summary.get("families") if isinstance(summary.get("families"), list) else [None, None]
        target = "A " + label(families[0]) + " vs B " + label(families[-1])
    else:
        target = label(summary.get("family"))
    digest = run.get("recipe_digest")
    numbers = SHARE[run["kind"]](run["summary"] or {})
    return {"kind": run["kind"], "state": run["state"], "started": round(run["started"]),
            "finished": round(run["finished"]) if run.get("finished") else None, "target": target,
            "recipe_digest": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
            "numbers": _clean(numbers)}


def share_text(summary):
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(summary["started"]))
    lines = [f"hyperloom bench · {summary['kind']} · {summary['state']} · {when}", f"target: {summary['target']}"]
    if summary.get("recipe_digest"):
        lines.append("recipe: " + summary["recipe_digest"][:12])

    def walk(prefix, value):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}{key}.", item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(f"{prefix}{index}.", item)
        elif value is not None:
            lines.append(f"{prefix.rstrip('.')}: {value}")
    walk("", summary.get("numbers") or {})
    return "\n".join(lines)


def _by(rows, key):
    return {row.get(key): row for row in rows or []}


def compare(a, b):
    """Two stored runs of the same kind side by side (any targets or machines)."""
    if a["kind"] != b["kind"]:
        raise ValueError(f"runs of different kinds ({a['kind']} and {b['kind']}) cannot be compared")
    sa, sb = a["summary"] or {}, b["summary"] or {}
    result = {"kind": a["kind"], "runs": [{k: run.get(k) for k in ("id", "target_label", "model", "state", "started", "recipe_digest")}
                                          for run in (a, b)], "rows": []}
    if a["kind"] == "decode":
        la, lb = _by(sa.get("levels"), "level"), _by(sb.get("levels"), "level")
        for level in sorted(set(la) | set(lb), key=lambda v: v or 0):
            x, y = la.get(level) or {}, lb.get(level) or {}
            result["rows"].append({"level": level, "aggregate_rate": [x.get("aggregate_rate"), y.get("aggregate_rate")],
                                   "ttft_p50_s": [x.get("ttft_p50_s"), y.get("ttft_p50_s")],
                                   "per_stream_rate_p50": [x.get("per_stream_rate_p50"), y.get("per_stream_rate_p50")],
                                   "ratio": _ratio(y.get("aggregate_rate"), x.get("aggregate_rate"))})
    elif a["kind"] == "prefill":
        la, lb = _by(sa.get("sizes"), "nominal_tokens"), _by(sb.get("sizes"), "nominal_tokens")
        for size in sorted(set(la) | set(lb), key=lambda v: v or 0):
            x, y = la.get(size) or {}, lb.get(size) or {}
            result["rows"].append({"nominal_tokens": size, "ttft_p50_s": [x.get("ttft_p50_s"), y.get("ttft_p50_s")],
                                   "throughput_p50": [x.get("throughput_p50"), y.get("throughput_p50")],
                                   "ratio": _ratio(y.get("throughput_p50"), x.get("throughput_p50"))})
    elif a["kind"] == "quality":
        if sa.get("suite") != sb.get("suite"):
            raise ValueError("quality runs of different suites cannot be paired")
        result["paired"] = quality.compare((a.get("detail") or {}).get("items") or [], (b.get("detail") or {}).get("items") or [])
        result["scores"] = [sa.get("score"), sb.get("score")]
    elif a["kind"] == "tools":
        result["scores"] = [sa.get("score"), sb.get("score")]
        ra = {(r["scenario"]): r for r in (a.get("detail") or {}).get("results") or [] if r.get("trial") == 0}
        rb = {(r["scenario"]): r for r in (b.get("detail") or {}).get("results") or [] if r.get("trial") == 0}
        for scenario in sorted(set(ra) | set(rb)):
            result["rows"].append({"scenario": scenario, "passed": [(ra.get(scenario) or {}).get("passed"),
                                                                   (rb.get(scenario) or {}).get("passed")]})
    else:
        keys = {"ab": ("identical", "cases", "claim", "speed_delta_median"),
                "demo": ("tiles", "completed", "e2e_rate_p50", "aggregate_events_per_s_peak")}[a["kind"]]
        for key in keys:
            result["rows"].append({"field": key, "values": [sa.get(key), sb.get(key)]})
    return result


def _ratio(new, old):
    if isinstance(new, (int, float)) and isinstance(old, (int, float)) and old:
        return round(new / old, 4)
    return None


def load_pair(store, a_id, b_id):
    return bench_store.get(store, a_id), bench_store.get(store, b_id)
