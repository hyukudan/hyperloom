"""Benchmarks and evaluation (F24-F32): portal documents, synchronous POSTs and admitted jobs.

GET  bench/targets                     running profiles and allow-listed ad-hoc targets with what each honours
GET  bench/guard[?target=kind:id]      admission rules, holds, running runs, refusal preview (read-only)
GET  bench/corpus                      prompt sets, quality categories, tool presets
GET  bench/runs?kind=&target=&limit=   stored runs (summaries)
GET  bench/runs/<id>                   one run with its detail (live while it runs)
GET  bench/compare?a=<id>&b=<id>       two runs of one kind side by side (quality: paired statistics)
GET  bench/share/<id>                  the allow-listed share summary (+ text)
GET  bench/receipt/<id>                an A/B run's exactness receipt
POST bench/targets/save | bench/targets/remove | bench/targets/capabilities | bench/runs/clear   (audited)
POST bench/preview {kind, arguments}   the equivalent CLI command (no side effect)
POST bench/decode | prefill | ab | quality | tools | demo   admitted jobs (C11), 202 + job

GETs never create the store and never touch the network; ids come from the store only.
"""

import re

from hyperloom import telemetry
from hyperloom.bench import ab, commands, data, decode, demo, guard, history, prefill, quality, runs, targets, tools
from hyperloom.bench import store as bench_store


NO_STORE = "history store not open in this portal process"
KIND_MODULES = {"decode": decode, "prefill": prefill, "quality": quality, "tools": tools, "demo": demo}


def _targets(api, query):
    if query:
        raise ValueError("bench/targets takes no query arguments")
    rows, why = targets.listing(api)
    return api.document("bench-targets", targets=rows, why=why)


def _corpus(api, query):
    prompts = data("prompts.json")["sets"]
    book = tools.catalogue()
    return api.document("bench-corpus", prompt_sets={k: {"count": len(v), "examples": v[:2]} for k, v in prompts.items()},
                        quality={"suite": quality.SUITE, "categories": list(quality.CATEGORIES), "slugs": commands.SLUGS,
                                 "recall_sizes": list(quality.RECALL_SIZES)},
                        tools={"presets": book["presets"],
                               "scenarios": [{"id": s["id"], "category": s["category"]} for s in book["scenarios"]]},
                        limits={"levels": [1, 32], "demo_tiles": demo.MAX_TILES, "demo_text": demo.MAX_TEXT,
                                "demo_body_bytes": demo.MAX_BODY, "token_budget": [1000, runs.MAX_BUDGET],
                                "default_budget": runs.DEFAULT_BUDGET})


def _runs(api, query):
    unknown = set(query) - {"kind", "target", "limit"}
    if unknown:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(unknown)))
    kind = query.get("kind")
    if kind is not None and kind not in bench_store.KINDS:
        raise ValueError("kind must be one of " + ", ".join(bench_store.KINDS))
    target = query.get("target")
    if target is not None and not re.fullmatch(r"(profile|target):[A-Za-z0-9._-]{1,64}", target):
        raise ValueError("target must be profile:<name> or target:<id>")
    try:
        limit = int(query.get("limit", 50))
    except ValueError:
        raise ValueError("limit must be an integer") from None
    if not 1 <= limit <= 200:
        raise ValueError("limit must be in 1..200")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("bench-runs", runs=None, why={"runs": NO_STORE})
    return api.document("bench-runs", runs=bench_store.runs(store, kind, target, limit))


def _store(api):
    store = telemetry.existing_store(api)
    if store is None:
        raise LookupError(NO_STORE)
    return store


def _run(api, rest, query):
    if query:
        raise ValueError("bench/runs/<id> takes no query arguments")
    run = bench_store.get(_store(api), rest)
    return api.document("bench-run", run=run)


def _compare(api, query):
    if set(query) != {"a", "b"}:
        raise ValueError("compare takes a=<run id>&b=<run id>")
    a, b = history.load_pair(_store(api), query["a"], query["b"])
    return api.document("bench-compare", comparison=history.compare(a, b))


def _share(api, rest, query):
    if query:
        raise ValueError("bench/share/<id> takes no query arguments")
    summary = history.share(bench_store.get(_store(api), rest, detail=False))
    return api.document("bench-share", share=summary, text=history.share_text(summary))


def _receipt(api, rest, query):
    if query:
        raise ValueError("bench/receipt/<id> takes no query arguments")
    run = bench_store.get(_store(api), rest)
    if run["kind"] != "ab":
        raise ValueError("only A/B runs have an exactness receipt")
    receipt = (run.get("detail") or {}).get("receipt")
    if not receipt:
        raise LookupError("this A/B run has no receipt (it did not complete)")
    return api.document("bench-receipt", receipt=receipt, run_id=run["id"])


def _clear(api, arguments):
    if not isinstance(arguments, dict) or arguments.get("confirm") != "clear" or set(arguments) - {"confirm", "kind", "run"}:
        raise ValueError('clear takes {confirm: "clear"} with an optional kind or run id')
    if arguments.get("kind") is not None and arguments["kind"] not in bench_store.KINDS:
        raise ValueError("kind must be one of " + ", ".join(bench_store.KINDS))
    if arguments.get("run") is not None and not bench_store.RUN_ID.fullmatch(str(arguments["run"])):
        raise ValueError("invalid run id")
    removed = bench_store.clear(bench_store.opened(api), arguments.get("kind"), arguments.get("run"))
    return {"removed": len(removed)}


def _preview(api, arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"kind", "arguments"}:
        raise ValueError("preview takes {kind, arguments}")
    return {"command": commands.command_line(arguments["kind"], arguments["arguments"])}


def _job(kind):
    if kind == "ab":
        return lambda api, arguments, ctx: ab.execute(api, arguments, ctx)
    module = KIND_MODULES[kind]
    return lambda api, arguments, ctx: runs.execute(api, kind, arguments, ctx, module.run)


def _resources(api, arguments):
    return targets.resources(api, targets.parse(arguments["target"]))


def register(reg):
    reg.get("bench/targets", _targets)
    reg.get("bench/guard", guard.document)
    reg.get("bench/corpus", _corpus)
    reg.get("bench/runs", _runs)
    reg.get_prefix("bench/runs/", _run)
    reg.get("bench/compare", _compare)
    reg.get_prefix("bench/share/", _share)
    reg.get_prefix("bench/receipt/", _receipt)
    reg.post("bench/targets/save", targets.save, audited=True)
    reg.post("bench/targets/remove", targets.remove, audited=True)
    reg.post("bench/targets/capabilities", targets.save_capabilities, audited=True)
    reg.post("bench/runs/clear", _clear, audited=True)
    reg.post("bench/preview", _preview)
    for kind in ("decode", "prefill", "quality", "tools", "demo"):
        reg.job("bench/" + kind, _job(kind), validate=KIND_MODULES[kind].validate, admission="bench",
                resources=_resources, deadline_s=runs.DEADLINES[kind])
    reg.job("bench/ab", _job("ab"), validate=ab.validate, admission="bench", resources=ab.resources,
            deadline_s=runs.DEADLINES["ab"])
    reg.capability("bench", True, "benchmarks run against running profiles and allow-listed endpoints")
