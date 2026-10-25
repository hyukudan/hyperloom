"""Prefill benchmark (F25): a context sweep with cold trials.

Every trial's prompt starts with a unique nonce before any reusable text, so no prefix cache can serve it; a trial
counts only when the engine reports ``cached_tokens == 0`` (a missing count excludes it: "cache state not
reported"). Sizes are nominal token counts; a calibration request turns characters into tokens from the engine's
own prompt count, and every trial reports the engine-measured ``prompt_tokens`` beside its nominal size.

Figures per size: TTFT [client] as TTFT, never as throughput; decode-engine computed prefill throughput
[engine counts, engine prefill time] = (prompt - cached - imported) / (prefill_ms / 1000), only when the engine
reports prompt and cached counts and a positive ``prefill_ms``, and the imported count is either reported or known
to be 0 because the router placement header says the prefill ran on the decode engine (``local``); the route that
served each size [router headers]. Timeouts grow with size.
"""

import random
import uuid

from hyperloom.bench import runs


OWN = ("sizes", "max_context", "max_tokens", "trials")
WORDS = ("amber", "basin", "cedar", "delta", "ember", "fjord", "grove", "harbor", "island", "juniper", "kettle", "lagoon",
         "meadow", "nectar", "orchard", "pebble", "quarry", "ridge", "summit", "thicket", "upland", "valley", "willow",
         "yonder", "zephyr", "lantern", "compass", "granite", "harvest", "mosaic", "pioneer", "ripple", "saddle", "timber")
CALIBRATION_CHARS = 4000
MIN_SIZE = 1024


def filler(chars, seed):
    rng = random.Random(seed)
    words, total = [], 0
    while total < chars:
        sentence = " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 12))).capitalize() + ". "
        words.append(sentence)
        total += len(sentence)
    return "".join(words)[:max(0, chars)]


def prompt(chars):
    """Nonce first (no reusable prefix), then seeded filler, then a short instruction."""
    nonce = uuid.uuid4().hex
    head = f"nonce {nonce}. Read the notes below and reply with the single word OK.\n\n"
    return head + filler(max(0, chars - len(head) - 40), seed=1) + "\n\nReply with OK."


def default_sizes(limit):
    sizes, size = [], MIN_SIZE
    while size <= limit:
        sizes.append(size)
        size *= 2
    return sizes


def validate(api, arguments):
    args, row = runs.validate_common(api, arguments, OWN)
    if args.get("sampling"):
        raise ValueError("the prefill sweep is greedy only")
    args["max_tokens"] = runs._int(arguments, "max_tokens", 1, 64, 8)
    args["trials"] = runs._int(arguments, "trials", 1, 5, 2)
    max_context = arguments.get("max_context")
    if max_context is not None:
        max_context = runs._int(arguments, "max_context", 2048, 4_194_304)
    args["max_context"] = max_context
    sizes = arguments.get("sizes")
    if sizes is not None and (not isinstance(sizes, list) or not 1 <= len(sizes) <= 16
                              or any(type(v) is not int or not MIN_SIZE <= v <= 4_194_304 for v in sizes)):
        raise ValueError(f"sizes: 1..16 token counts in {MIN_SIZE}..4194304")
    limit = row.get("context_tokens") or max_context
    args["sizes"] = sorted(set(sizes)) if sizes else None
    # The reported context is only known once the run starts: plan for the largest sweep it could allow.
    # (A declared max_context does not cap it: the endpoint's own report wins at run start.)
    planned_sizes = args["sizes"] or default_sizes(4_194_304 - (args["max_tokens"] + 64))
    planned = 1 + len(planned_sizes) * args["trials"] * args["max_tokens"]     # calibration + every trial
    return runs.check_plan(args, planned)


def _timeout(size):
    return 60.0 + 30.0 * size / 8192


def trial(run, size, chars, reply_tokens):
    request = runs.body(run, [{"role": "user", "content": prompt(chars)}], reply_tokens)
    reply = run.chat(request, _timeout(size))
    row = {"nominal_tokens": size, "ok": reply.ok, "error": reply.error, "ttft_s": runs.rounded(reply.ttft_s, 4),
           "prompt_tokens": reply.prompt_tokens, "cached_tokens": reply.cached_tokens,
           "replica": reply.headers.get("x-tensormeld-replica"), "prefill_place": reply.headers.get("x-tensormeld-prefill"),
           "route_id": reply.headers.get("x-tensormeld-route-id"), "counted": False, "throughput": None, "why": {}}
    prefill_ms = reply.engine_ms("prefill_ms")
    row["prefill_ms"] = runs.rounded(prefill_ms, 3)
    imported = reply.imported_tokens
    if imported is None and (row["prefill_place"] or "").split(":")[0] == "local":
        imported, row["imported_basis"] = 0, "router placement: local prefill on the decode engine"
    elif imported is not None:
        row["imported_basis"] = "reported by the engine"
    row["imported_tokens"] = imported
    if not reply.ok:
        row["why"]["counted"] = reply.error or f"HTTP {reply.status}"
        return row
    if reply.cached_tokens is None:
        row["why"]["counted"] = "excluded: cache state not reported"
        return row
    if reply.cached_tokens != 0:
        row["why"]["counted"] = f"excluded: {reply.cached_tokens} tokens restored from a cache"
        return row
    if reply.prompt_tokens is None:
        row["why"]["counted"] = "excluded: prompt count not reported"
        return row
    row["counted"] = True
    if imported is None:
        row["why"]["throughput"] = "imported count not reported (prefill placement unknown or offloaded)"
    elif reply.cached_tokens + imported > reply.prompt_tokens:
        row["counted"] = False
        row["why"]["counted"] = "invalid: cached + imported exceeds the prompt count"
    elif not prefill_ms or prefill_ms <= 0:
        row["why"]["throughput"] = "the engine reported no prefill time"
    else:
        row["computed_tokens"] = reply.prompt_tokens - reply.cached_tokens - imported
        row["throughput"] = round(row["computed_tokens"] / (prefill_ms / 1000.0), 2)
    return row


def run(run):
    args = run.arguments
    reply_tokens = args["max_tokens"]
    limit = run.context_tokens or args.get("max_context")
    run.summary.update(max_tokens=reply_tokens, trials=args["trials"], sizes=[],
                       context_limit=limit, context_basis="reported" if run.context_tokens else
                       ("declared" if args.get("max_context") else "unavailable"),
                       observers={"ttft": "client", "throughput": "decode-engine computed prefill (engine counts, engine prefill time)",
                                  "route": "router headers"})
    if limit is None:
        raise ValueError("the context limit is unknown: the endpoint does not report it; give max_context")
    run.progress("calibrating characters per token")
    calibration = run.chat(runs.body(run, [{"role": "user", "content": prompt(CALIBRATION_CHARS)}], 1), _timeout(1024))
    if not calibration.ok or not calibration.prompt_tokens:
        raise ValueError("calibration failed: " + (calibration.error or "the endpoint reported no prompt count"))
    chars_per_token = CALIBRATION_CHARS / calibration.prompt_tokens
    run.summary["chars_per_token"] = round(chars_per_token, 3)
    reserve = reply_tokens + 64
    sizes = args["sizes"] or default_sizes(limit - reserve)
    run.detail["trials"] = []
    for size in sizes:
        if run.cancelled():
            return
        entry = {"nominal_tokens": size, "trials": 0, "counted": 0, "excluded": [], "why": {}}
        if size + reserve > limit:
            entry["why"]["skipped"] = f"{size} + {reserve} reserved for the reply exceed the context limit {limit}"
            run.summary["sizes"].append(entry)
            continue
        rows = []
        for index in range(args["trials"]):
            if run.cancelled():
                break
            run.progress(f"size {size}: trial {index + 1}/{args['trials']}")
            rows.append(trial(run, size, int(size * chars_per_token), reply_tokens))
            run.detail["trials"].append(rows[-1])
            run.live()
        counted = [r for r in rows if r["counted"]]
        entry.update(trials=len(rows), counted=len(counted),
                     excluded=sorted({r["why"].get("counted") for r in rows if not r["counted"] and r["why"].get("counted")}),
                     measured_prompt_tokens=runs.percentile([r["prompt_tokens"] for r in counted], 50),
                     ttft_p50_s=runs.rounded(runs.percentile([r["ttft_s"] for r in counted], 50), 4),
                     throughput_p50=runs.rounded(runs.percentile([r["throughput"] for r in counted if r["throughput"]], 50), 2),
                     routes=sorted({f"{r['replica'] or 'unknown'} · {r['prefill_place'] or 'no placement header'}" for r in rows}))
        if not counted:
            entry["why"]["ttft_p50_s"] = "no trial counted"
        if entry["throughput_p50"] is None:
            entry["why"]["throughput_p50"] = next((r["why"].get("throughput") for r in counted if r["why"].get("throughput")),
                                                  "no trial counted")
        run.summary["sizes"].append(entry)
        run.live(force=True)
        if rows and not any(r["ok"] for r in rows):
            run.summary["stopped_at"] = f"size {size}: every trial failed ({rows[-1]['error']})"
            break


def share(summary):
    return {"max_tokens": summary.get("max_tokens"), "context_basis": summary.get("context_basis"),
            "sizes": [{k: s.get(k) for k in ("nominal_tokens", "measured_prompt_tokens", "counted", "ttft_p50_s", "throughput_p50")}
                      for s in summary.get("sizes") or []]}

