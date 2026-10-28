"""Published measurements beside cached live observations. GETs never collect telemetry."""

import json
import math
from pathlib import Path
import re

from hyperloom import config, telemetry
from hyperloom.web.features import machines, observe


DATA = Path(__file__).resolve().parents[2] / "data" / "results.json"
MODEL_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
METRICS = {"decode_tok_s": "tok/s", "ttft_s": "s", "ms_per_token": "ms/token",
           "ms_per_step": "ms/step", "aggregate_tok_s": "tok/s", "prefill_tok_s": "tok/s"}
FAMILIES = {"qwen-replicas": "qwen3.8-flash-next", "ds41-pair": "deepseek-v4.1-flash",
            "minimax-pair": "minimax-m3.1-flash"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(value):
    """Validate the packaged, hand-transcribed document; never trust JSON's NaN extension."""
    def check(ok):
        if not ok:
            raise ValueError("invalid published results document")

    def strings(row, fields):
        check(isinstance(row, dict))
        check(all(isinstance(row.get(k), str) and row[k] for k in fields))

    check(isinstance(value, dict) and value.get("schema") == "hyperloom/results-v1")
    strings(value.get("source"), ("repo", "ref", "file", "retrieved"))
    check(isinstance(value.get("models"), list))
    ids = set()
    for model in value["models"]:
        strings(model, ("id", "name", "quant", "hardware", "status"))
        check(MODEL_ID.fullmatch(model["id"]) and model["id"] not in ids)
        ids.add(model["id"])
        check(isinstance(model.get("rows"), list))
        for row in model["rows"]:
            strings(row, ("mode", "hardware", "prompt", "metric", "unit", "date", "build", "note", "section"))
            check(row["metric"] in METRICS and row["unit"] == METRICS[row["metric"]])
            for key in ("best", "typical"):
                check(key in row and (row[key] is None or (_finite(row[key]) and row[key] >= 0)))
            check(row["best"] is not None or row["typical"] is not None)
            counts = row.get("concurrency")
            counts = counts if isinstance(counts, list) else [counts]
            check(counts and all(type(n) is int and n > 0 for n in counts))
            check(row["build"] == "release" or re.fullmatch(r"[0-9a-f]{7,40}", row["build"]))
    # Also reject non-finite numbers in any optional metadata.
    def finite_tree(item):
        if type(item) in (int, float):
            check(_finite(item))
        elif isinstance(item, dict):
            for child in item.values():
                finite_tree(child)
        elif isinstance(item, list):
            for child in item:
                finite_tree(child)
    finite_tree(value)
    return value


def published():
    try:
        return validate(json.loads(DATA.read_text(encoding="utf-8"))), None
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        return None, "packaged published results file is missing or invalid"


def model_identity(profile, record, declared, ids):
    recipe = ((record.get("recipe") or {}).get("effective") or {}).get("match") or {}
    model = recipe.get("model") or declared.get("model")
    if model:
        return (model, None) if model in ids else (None, "profile model has no published measurements")
    preset = declared.get("preset", profile)
    try:
        family = (record.get("launch") or {}).get("family") or config.family(preset)
    except (OSError, ValueError, KeyError):
        family = None
    model = FAMILIES.get(family)
    return (model, None) if model in ids else (None, "profile model identity cannot be mapped to published results")


def _value(value, label, unit, why=None, **fields):
    return {"value": value if _finite(value) else None, "unit": unit, "label": label, **fields,
            **({"why": why or "no cached observation available"} if not _finite(value) else {})}


def live(api, ids, cluster=None, coverage=None):
    cluster = cluster if cluster is not None else api.cluster()
    snapshot = api.cached_snapshot() or {}
    stats = snapshot.get("stats") or {}
    profiles = {p["id"]: p for p in (snapshot.get("profiles") or {}).get("profiles") or []}
    records = machines.launch_records(api)
    if coverage is not None:
        coverage.update(ignored_profiles=0, why="launch discovery includes only this portal's configuration")
    running = set(records)
    declared = cluster.profiles
    store = telemetry.existing_store(api)
    control = machines.control_machine(cluster) if getattr(cluster, "machines", None) else None
    fleet_values = [dict(e, machine=mid) for mid in getattr(cluster, "machines", {})
                    for e in machines.engine_rows(store, mid, records, control.id if control else None, api.clock())
                    if e.get("role") in ("rank 0", "replica") and _finite(e.get("throughput"))]
    rows = []
    for profile in sorted(running):
        # A cached authoritative stopped state overrides a leftover launch record.
        if profiles.get(profile, {}).get("state") == "stopped":
            continue
        model, why = model_identity(profile, records.get(profile, {}), declared.get(profile, {}), ids)
        values, core_ttft = [], []
        stale = api.clock() - stats.get("server_time", 0) > 3 * stats.get("poll_interval_s", 2)
        for engine in stats.get("engines") or []:
            if engine.get("profile") != profile:
                continue
            rate = engine.get("throughput") or {}
            window = rate.get("window_s")
            values.append(_value(None if stale else rate.get("value"),
                f"live · engine-side · completed tok/s over {round(window) if _finite(window) else 'unknown'} s window",
                "tok/s", "cached stats are stale" if stale else rate.get("why") or (engine.get("why") or {}).get("throughput"),
                metric="aggregate_tok_s", replica=engine.get("id"), window_s=window, n=None,
                as_of=stats.get("server_time")))
            for percentile in ("p50", "p95"):
                q = engine.get("ttft_" + percentile) or {}
                number = q.get("value")
                core_ttft.append(_value(number / 1000 if not stale and _finite(number) else None,
                    f"live · engine-side TTFT {percentile} approximation (queue + prefill), last 50 requests / 600 s",
                    "s", "cached stats are stale" if stale else q.get("why"), metric="ttft_s",
                    percentile=percentile, n=q.get("n", 0), window_s=600, request_limit=50,
                    replica=engine.get("id"), as_of=stats.get("server_time")))
        if not any(v["metric"] == "aggregate_tok_s" and v["value"] is not None for v in values):
            fallback = [e for e in fleet_values if e["profile"] == profile]
            if fallback:
                values = [v for v in values if v["metric"] != "aggregate_tok_s"]
                for engine in fallback:
                    window = engine.get("throughput_window_s", 60)
                    values.append(_value(engine["throughput"],
                        f"live · engine-side · completed tok/s over {window} s window (fleet/observe counters)",
                        "tok/s", metric="aggregate_tok_s", replica=engine["machine"], window_s=window, n=None))
        latency = observe.latency_document(api, {"profile": profile, "range": "600", "by": "model"})
        for group in latency.get("groups") or []:
            for key, metric, unit, scale in (("ttft_ms", "ttft_s", "s", 1000), ("decode_tok_s", "decode_tok_s", "tok/s", 1)):
                quantiles = group["metrics"][key]["engine"]
                for percentile in (("p50", "p95") if key == "ttft_ms" else ("p50",)):
                    number = quantiles.get(percentile)
                    values.append(_value(number / scale if _finite(number) else None,
                        f"live · engine-side {'TTFT' if key == 'ttft_ms' else 'decode tok/s'} {percentile} over last 600 s",
                        unit, quantiles.get("why"), metric=metric, percentile=percentile,
                        n=quantiles.get("n", 0), window_s=600, group=group["group"]))
        if not any(v.get("metric") == "decode_tok_s" for v in values):
            values.append(_value(None, "live · engine-side decode p50 over last 600 s", "tok/s",
                (latency.get("why") or {}).get("groups"), metric="decode_tok_s", percentile="p50", n=0, window_s=600))
        if not any(v.get("metric") == "aggregate_tok_s" for v in values):
            values.append(_value(None, "live · engine-side completed tok/s", "tok/s", metric="aggregate_tok_s", n=None, window_s=None))
        if not any(v.get("metric") == "ttft_s" and v["value"] is not None for v in values) and core_ttft:
            values = [v for v in values if v.get("metric") != "ttft_s"] + core_ttft
        for percentile in ("p50", "p95"):
            if not any(v.get("metric") == "ttft_s" and v.get("percentile") == percentile for v in values):
                values.append(_value(None, f"live · engine-side TTFT {percentile} over last 600 s", "s",
                    (latency.get("why") or {}).get("groups"), metric="ttft_s", percentile=percentile, n=0, window_s=600))
        rows.append({"profile": profile, "model": model, "values": values, **({"why": why} if why else {})})
    return rows


def results(api, query):
    if set(query) - {"model"} or ("model" in query and (not isinstance(query["model"], str) or not MODEL_ID.fullmatch(query["model"]))):
        raise ValueError("results accepts only model=[A-Za-z0-9._-]{1,128}")
    data, why = published()
    ids = {m["id"] for m in data["models"]} if data else set(FAMILIES.values()) | {"glm-5.3-flash"}
    reasons = {"published": why} if why else {}
    coverage = {}
    try:
        cluster = api.cluster()
    except (OSError, ValueError):
        current = []
        reasons["live"] = "current configuration is unavailable or invalid"
    else:
        current = live(api, ids, cluster, coverage)
    if "model" in query:
        if data:
            data = {**data, "models": [m for m in data["models"] if m["id"] == query["model"]]}
        current = [p for p in current if p["model"] == query["model"]]
    return api.document("results", published=data, live=current, why=reasons, coverage=coverage)


def register(reg):
    reg.get("results", results)
