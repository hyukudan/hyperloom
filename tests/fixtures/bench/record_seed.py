"""Records the fixture benchmark runs the browser smoke shows (tests/fixtures/bench/seed-runs.json): every kind run
once (decode and quality twice) by the real runners against the fake chat-completions server. Test data only: the
numbers come from the fake server, not from any engine. Run: python tests/fixtures/bench/record_seed.py"""

from pathlib import Path
import sys
import uuid


def seed(store, now):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_server import FakeServer
    import responders
    from hyperloom.bench import ab, client, decode, demo, prefill, quality, runs, targets, tools

    store.put(targets.PREFS_NAMESPACE, targets.PREFS_KEY, {"targets": [
        {"id": "halo-2-server", "label": "halo-2 server", "scheme": "http", "host": "halo-2", "port": 18080,
         "key": None, "machines": ["halo-2"], "sampling": True, "thinking": False, "context_tokens": 32768}]})

    def resolved(server, profile=True):
        endpoint = client.Endpoint("http", "127.0.0.1", server.port)
        if profile:
            return targets.Resolved("profile:qwen-replicas", "qwen-replicas", "profile", endpoint, "qwen-replicas", {}, 12288,
                                    {"family": "qwen-replicas", "release": "a" * 12, "binary_sha256": ["a" * 64],
                                     "build_source": "launch-record", "recipe_digest": None, "recipe_source": "unavailable",
                                     "router_policy": "nodes", "router_policy_source": "router-status"})
        return targets.Resolved("target:halo-2-server", "halo-2 server", "adhoc", endpoint, None, {}, 32768,
                                {"family": None, "release": None, "binary_sha256": None, "build_source": "unavailable",
                                 "recipe_digest": None, "recipe_source": "unavailable", "router_policy": None,
                                 "router_policy_source": "unavailable"})

    def record(kind, target, arguments, runner, age_s, model="qwen3.8-flash-next"):
        context = runs.CLIContext(600, quiet=True)
        arguments = {"run_id": uuid.uuid4().hex[:16], "token_budget": 400000, "trace": False, "sampling": None,
                     "thinking": None, **arguments}
        run = runs.Run(None, kind, arguments, context, target, model, target.context_tokens, None, store)
        run.started = now - age_s
        runs.finish_with(run, context, runner)
        return arguments["run_id"]

    with FakeServer(token_delay=0.003, prefill_rate=2400.0, decode_ms_per_token=38.0, model="qwen3.8-flash-next") as fast, \
            FakeServer(token_delay=0.0004, prefill_rate=1800.0, decode_ms_per_token=45.0, diverge_at=5,
                       diverge_match="lighthouse", model="qwen3.8-flash-next") as other:
        node = resolved(fast)
        record("decode", node, {"prompt_set": "prose", "levels": [1, 2, 4, 8], "max_tokens": 48}, decode.run, 5400)
        record("decode", resolved(other, False), {"prompt_set": "code", "levels": [1, 2, 4], "max_tokens": 48}, decode.run, 3600)
        record("prefill", node, {"sizes": None, "max_context": None, "max_tokens": 8, "trials": 2}, prefill.run, 3000)
        fast.config["responder"] = responders.quality_responder(miss={"qa-07", "arith-03", "inst-06"}, context_limit=12288)
        record("quality", node, {"categories": list(quality.CATEGORIES), "max_tokens": None, "concurrency": 8}, quality.run, 2700)
        fast.config["responder"] = responders.quality_responder(miss={"qa-07", "state-02", "inst-06", "inst-10"}, context_limit=12288)
        record("quality", node, {"categories": list(quality.CATEGORIES), "max_tokens": None, "concurrency": 8}, quality.run, 2400)
        fast.config["responder"] = responders.tools_responder(wrong={"types-cart"})
        record("tools", node, {"preset": "regular", "repeats": 2, "max_tokens": 256, "repeat_mode": "determinism check (greedy decoding)"},
               tools.run, 1800)
        fast.config.update(responder=responders.demo_responder(), token_delay=0.012)
        record("demo", node, {"prompt_set": "prose", "count": 6, "texts": None, "max_tokens": 160,
                              "prompts": demo_prompts()}, demo.run, 900)
        fast.config.pop("responder")
        fast.config["token_delay"] = 0.0004
        arms = {}
        for name, server, profile in (("a", fast, True), ("b", other, False)):
            target = resolved(server, profile)
            arms[name] = {"name": name, "model": "qwen3.8-flash-next", "context": target.context_tokens,
                          "endpoint": target.endpoint,
                          "identity": ab.transportable(runs.identity(target, "qwen3.8-flash-next", None, "qwen3-bpe-151k"))}
        arguments = {"arms": {}, "tokenizers": {"a": "qwen3-bpe-151k", "b": "qwen3-bpe-151k"}, "prompt_set": "prose",
                     "cases": 6, "max_tokens": 32, "seed": 1}
        node_ab = resolved(fast)
        node_ab.label = "qwen-replicas vs halo-2 server"
        def ab_run(run):
            run.summary["families"] = ["qwen-replicas", None]
            ab.passes(run, arms, arguments)
        record("ab", node_ab, arguments, ab_run, 600)


def demo_prompts():
    from hyperloom.bench import data
    items = data("prompts.json")["sets"]["prose"]
    return [items[i % len(items)] for i in range(6)]


ALLOW_LIST = [{"id": "halo-2-server", "label": "halo-2 server", "scheme": "http", "host": "halo-2", "port": 18080,
               "key": None, "machines": ["halo-2"], "sampling": True, "thinking": False, "context_tokens": 32768}]


if __name__ == "__main__":
    import json
    import time
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from hyperloom.telemetry.store import Store
    from hyperloom.bench import store as bench_store
    memory = Store(":memory:")
    now = time.time()
    seed(memory, now)
    rows = []
    for run in reversed(bench_store.runs(memory, limit=200)):
        full = bench_store.get(memory, run["id"])
        full["age_s"] = round(now - full["started"], 3)
        full["duration_s"] = round((full["finished"] or full["started"]) - full["started"], 3)
        for key in ("started", "finished"):
            full.pop(key)
        rows.append(full)
    out = Path(__file__).with_name("seed-runs.json")
    text = json.dumps({"targets": ALLOW_LIST, "runs": rows}, sort_keys=True, separators=(",", ":"))
    out.write_text(text + chr(10), encoding="utf-8", newline=chr(10))
    print("wrote", out, len(rows), "runs")
