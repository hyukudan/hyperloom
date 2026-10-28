"""Decode throughput benchmark (F24).

Per concurrency level N, N streams start together (a barrier releases them), each with a prompt of the chosen set
and the same ``max_tokens``. Figures per level (observer in brackets):

- TTFT p50 / p95 [client]: first event carrying output, from the request write.
- streams completed / failed.
- aggregate completed-token rate [client window]: engine-reported completion tokens of the completed streams /
  (last output event of the level - first request write).
- per-stream decode rate [client] p50 / min / max: (completion tokens - 1) / (last output - first output).
- engine decode rate p50 [engine]: (completion tokens - 1) / decode_ms, where the engine reports it.
"""

import threading

from hyperloom.bench import data, runs


SETS = ("structured", "prose", "code", "json")
OWN = ("prompt_set", "levels", "max_tokens")
DEFAULT_LEVELS = [1, 2, 4, 8]


def prompts(name):
    return data("prompts.json")["sets"][name]


def messages_for(prompt_set, index):
    items = prompts(prompt_set)
    return [{"role": "user", "content": items[index % len(items)]}]


def validate(api, arguments):
    args, _ = runs.validate_common(api, arguments, OWN)
    prompt_set = arguments.get("prompt_set", "prose")
    if prompt_set not in SETS:
        raise ValueError("prompt_set must be one of " + ", ".join(SETS))
    levels = arguments.get("levels", DEFAULT_LEVELS)
    if (not isinstance(levels, list) or not 1 <= len(levels) <= 8
            or any(type(v) is not int or not 1 <= v <= 32 for v in levels) or len(set(levels)) != len(levels)):
        raise ValueError("levels: 1..8 distinct concurrency levels in 1..32")
    args.update(prompt_set=prompt_set, levels=sorted(levels), max_tokens=runs._int(arguments, "max_tokens", 16, 4096, 256))
    plan = {"model": "m" * 200, "messages": None, "max_tokens": args["max_tokens"], "stream": True,
            "stream_options": {"include_usage": True}, "return_token_ids": True, "temperature": 0}
    planned = sum(runs.request_bound({**plan, "messages": messages_for(prompt_set, i)})
                  for level in args["levels"] for i in range(level))
    return runs.check_plan(args, planned)


def summarise(level, rows, sent, last):
    ok = [r for r in rows if r["ok"] and r["completion_tokens"] is not None]
    counted = [r for r in rows if r["ok"]]
    tokens = sum(r["completion_tokens"] for r in ok)
    window = (max(last) - min(sent)) if ok and last and sent else None
    rates = [r["client_rate"] for r in ok if r["client_rate"] is not None]
    e2e = [r["e2e_rate"] for r in ok if r["e2e_rate"] is not None]
    engine = [r["engine_rate"] for r in ok if r["engine_rate"] is not None]
    why = {}
    if not ok:
        why["aggregate_rate"] = "no stream completed with engine usage"
    if not rates:
        why["per_stream_rate"] = "no stream had two or more tokens over a measurable interval"
    if not engine:
        why["engine_rate_p50"] = "the engine reported no decode time"
    if not counted:
        why["ttft_p50_s"] = why["ttft_p95_s"] = "no stream completed"
    errors = sorted({r["error"] for r in rows if not r["ok"] and r["error"]})
    return {"level": level, "streams": len(rows), "completed": len(counted), "failed": len(rows) - len(counted),
            "completion_tokens": tokens,
            "ttft_p50_s": runs.rounded(runs.percentile([r["ttft_s"] for r in counted], 50), 4),
            "ttft_p95_s": runs.rounded(runs.percentile([r["ttft_s"] for r in counted], 95), 4),
            "aggregate_rate": runs.rounded(tokens / window, 3) if window and window > 0 and ok else None,
            "per_stream_rate_p50": runs.rounded(runs.percentile(rates, 50), 3),
            "per_stream_rate_min": runs.rounded(min(rates), 3) if rates else None,
            "per_stream_rate_max": runs.rounded(max(rates), 3) if rates else None,
            "per_stream_e2e_rate_p50": runs.rounded(runs.percentile(e2e, 50), 3),
            "engine_rate_p50": runs.rounded(runs.percentile(engine, 50), 3),
            "replicas": sorted({r["replica"] for r in rows if r.get("replica")}),
            "errors": errors[:5], "why": why}


def run(run):
    args = run.arguments
    timeout = 120 + args["max_tokens"] * 0.5
    run.summary.update(prompt_set=args["prompt_set"], max_tokens=args["max_tokens"], levels=[],
                       sampling="greedy" if not args.get("sampling") else "sampled",
                       observers={"ttft": "client, measured", "aggregate_rate": "measured: client window, engine token counts",
                                  "per_stream_rate": runs.RATE_BASIS["client_rate"],
                                  "per_stream_e2e_rate": runs.RATE_BASIS["e2e_rate"],
                                  "engine_rate": runs.RATE_BASIS["engine_rate"]})
    run.detail["levels"] = []
    for level in args["levels"]:
        if run.cancelled():
            return
        run.progress(f"level {level}: {level} stream{'s' if level > 1 else ''}")
        barrier = threading.Barrier(level)
        live = {"done": 0}
        lock = threading.Lock()

        def stream(index):
            request = runs.body(run, messages_for(args["prompt_set"], index), args["max_tokens"])
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass
            reply = run.chat(request, timeout)
            row = runs.measure(reply)
            with lock:
                live["done"] += 1
            run.progress(f"level {level}: {live['done']}/{level} streams finished")
            return reply, row
        results = runs.parallel(stream, list(range(level)), level)
        rows = [row for _, row in results]
        sent = [reply.sent_at for reply, _ in results if reply.sent_at is not None]
        last = [reply.sent_at + reply.last_output_s for reply, _ in results
                if reply.ok and reply.sent_at is not None and reply.last_output_s is not None and reply.completion_tokens is not None]
        summary = summarise(level, rows, sent, last)
        run.summary["levels"].append(summary)
        run.detail["levels"].append({**summary, "streams_detail": rows})
        run.live(force=True)
    run.summary["best_aggregate_rate"] = max((l["aggregate_rate"] for l in run.summary["levels"]
                                              if l["aggregate_rate"] is not None), default=None)


def share(summary):
    """The numbers a share card may show (no prompts, replies or addresses)."""
    return {"prompt_set": summary.get("prompt_set"), "max_tokens": summary.get("max_tokens"),
            "levels": [{k: level.get(k) for k in ("level", "streams", "completed", "ttft_p50_s", "aggregate_rate",
                                                  "per_stream_rate_p50", "per_stream_e2e_rate_p50", "engine_rate_p50")}
                       for level in summary.get("levels") or []]}

