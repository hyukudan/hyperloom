"""Bench runs end to end through the portal API against the fake server: every kind, budgets, the frozen target,
documents that survive redaction unchanged, GETs that never create the store, history, compare and share."""

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))

import responders  # noqa: E402
import support  # noqa: E402

from hyperloom.bench import history, runs, targets  # noqa: E402
from hyperloom.web import api as web_api  # noqa: E402
from hyperloom.web.api import APIError, redact  # noqa: E402


def stable(test, document):
    test.assertEqual(redact(document), document, "a bench document must survive the portal's redaction unchanged")


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.002)

    def test_decode_levels_on_a_profile(self):
        record, run = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [1, 4], "max_tokens": 16})
        self.assertEqual(record["state"], "succeeded", record["error"])
        self.assertEqual(record["resources"], ["halo-1", "router"])
        levels = run["summary"]["levels"]
        self.assertEqual([l["level"] for l in levels], [1, 4])
        self.assertEqual((levels[1]["streams"], levels[1]["completed"], levels[1]["completion_tokens"]), (4, 4, 64))
        for level in levels:
            self.assertGreater(level["aggregate_rate"], 0)
            self.assertGreater(level["ttft_p50_s"], 0)
            self.assertGreater(level["engine_rate_p50"], 0)
        self.assertEqual(run["summary"]["tokens_used"], 80)
        self.assertEqual(run["summary"]["family"], "qwen-replicas")
        self.assertIn("estimated", run["summary"]["observers"]["per_stream_rate"])
        sent = self.bench.server.requests[-1]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer " + support.KEY)
        self.assertTrue(sent["body"]["return_token_ids"])
        self.assertEqual(sent["body"]["temperature"], 0)
        document = self.bench.api.read("bench/runs/" + run["id"])
        stable(self, document)
        self.assertNotIn(support.KEY, json.dumps(document))
        self.assertNotIn(support.KEY, json.dumps(self.bench.api.read("jobs")))

    def test_validation_refuses_what_the_target_does_not_honour(self):
        for arguments, message in (({"target": {"profile": "qwen"}, "sampling": {"temperature": 0.5}}, "sampling is not available"),
                                   ({"target": {"profile": "qwen"}, "thinking": True}, "thinking cannot be switched"),
                                   ({"target": {"profile": "other"}}, "start it first"),
                                   ({"target": {"profile": "nope"}}, "not offered"),
                                   ({"target": {"profile": "qwen"}, "levels": [0]}, "levels"),
                                   ({"target": {"profile": "qwen"}, "levels": [32], "max_tokens": 4096, "token_budget": 1000}, "token budget"),
                                   ({"target": {"profile": "qwen"}, "surprise": 1}, "unknown arguments")):
            with self.subTest(message=message), self.assertRaises(APIError) as caught:
                self.bench.start("bench/decode", arguments)
            self.assertIn(message, str(caught.exception))

    def test_sampling_and_thinking_where_declared(self):
        store = self.bench.api.telemetry
        entry = targets.allow_list(store)[0]
        targets.save(self.bench.api, {**entry, "sampling": True, "thinking": True})
        record, run = self.bench.run("bench/decode", {"target": {"target": "fake"}, "levels": [1], "max_tokens": 16,
                                                      "sampling": {"temperature": 0.5, "seed": 7}, "thinking": False})
        self.assertEqual(record["state"], "succeeded", record["error"])
        body = self.bench.server.requests[-1]["body"]
        self.assertEqual((body["temperature"], body["seed"], body["chat_template_kwargs"]), (0.5, 7, {"enable_thinking": False}))

    def test_budget_reservation_stops_before_overrunning(self):
        arguments = runs.check_plan
        with mock.patch.object(runs, "check_plan", lambda args, planned: args):     # let the plan through
            record, run = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [1, 2, 4], "max_tokens": 400,
                                                          "token_budget": 1000})
        self.assertIs(arguments, runs.check_plan)
        self.assertEqual(run["state"], "budget reached")
        self.assertLessEqual(run["summary"]["tokens_used"], 1000)

    def test_endpoint_exceeding_max_tokens_stops_the_run(self):
        self.bench.server.config["responder"] = lambda body: {"text": " ".join(["x"] * (body["max_tokens"] + 5))}
        record, run = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [1, 2], "max_tokens": 16})
        self.assertEqual(run["state"], "budget bound violated")
        self.assertIn("max_tokens 16", run["summary"]["reason"])

    def test_a_target_changed_after_validation_is_refused(self):
        job = self.bench.start("bench/decode", {"target": {"target": "fake"}, "levels": [1], "max_tokens": 16})
        self.bench.wait(job)
        store = self.bench.api.telemetry
        validated = self.bench.api.read("jobs/" + job["id"])["arguments"]
        targets.save(self.bench.api, {**targets.allow_list(store)[0], "port": 9})
        context = runs.CLIContext(30)
        arguments = {**validated, "run_id": "f" * 16}
        with self.assertRaises(PermissionError):
            runs.execute(self.bench.api, "decode", arguments, context, lambda run: None)
        failed = self.bench.api.read("bench/runs/" + "f" * 16)["run"]
        self.assertEqual(failed["state"], "failed")

    def test_cancel_stops_the_run(self):
        self.bench.server.config["token_delay"] = 0.2
        job = self.bench.start("bench/decode", {"target": {"profile": "qwen"}, "levels": [2], "max_tokens": 200})
        import time
        time.sleep(0.5)
        self.bench.api.mutate("cancel", {"job": job["id"]})
        record = self.bench.wait(job, 15)
        self.assertEqual(record["state"], "cancelled")
        run = self.bench.api.read("bench/runs/" + job["arguments"]["run_id"])["run"]
        self.assertEqual(run["state"], "cancelled")
        self.assertEqual(self.bench.api.admission_db.holds(), [])


class KindsTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.0005)

    def test_prefill_counts_only_cold_trials(self):
        record, run = self.bench.run("bench/prefill", {"target": {"target": "fake"}, "sizes": [1024, 2048], "trials": 2})
        self.assertEqual(record["state"], "succeeded", record["error"])
        sizes = run["summary"]["sizes"]
        self.assertEqual([s["counted"] for s in sizes], [2, 2])
        self.assertTrue(all(s["throughput_p50"] for s in sizes))
        bodies = [r["body"] for r in self.bench.server.requests if r["method"] == "POST"]
        self.assertTrue(all(b["messages"][0]["content"].startswith("nonce ") for b in bodies))
        self.assertEqual(len({b["messages"][0]["content"][:38] for b in bodies}), len(bodies))
        trial = run["detail"]["trials"][0]
        self.assertEqual(trial["computed_tokens"], trial["prompt_tokens"])
        self.assertAlmostEqual(trial["throughput"], trial["computed_tokens"] / (trial["prefill_ms"] / 1000), delta=1)
        stable(self, self.bench.api.read("bench/runs/" + run["id"]))

    def test_prefill_excludes_cache_hits_and_unknown_imports(self):
        self.bench.server.config["prefix_cache"] = True
        original = __import__("hyperloom.bench.prefill", fromlist=["prompt"]).prompt
        with mock.patch("hyperloom.bench.prefill.prompt", lambda chars: "same reusable prefix " * 4 + original(chars)):
            record, run = self.bench.run("bench/prefill", {"target": {"target": "fake"}, "sizes": [1024], "trials": 2})
        size = run["summary"]["sizes"][0]
        self.assertEqual(size["counted"], 0)
        self.assertTrue(any("restored from a cache" in reason for reason in size["excluded"]))

    def test_quality_scores_and_paired_comparison(self):
        self.bench.server.config["responder"] = responders.quality_responder(miss={"qa-01", "arith-01"}, context_limit=65536)
        _, first = self.bench.run("bench/quality", {"target": {"target": "fake"}, "categories": ["short QA", "arithmetic chains", "instruction following"]})
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual((first["summary"]["scored"], first["summary"]["passed"]), (50, 48))
        self.bench.server.config["responder"] = responders.quality_responder(miss={"qa-01", "qa-02", "inst-03"}, context_limit=65536)
        _, second = self.bench.run("bench/quality", {"target": {"target": "fake"}, "categories": ["short QA", "arithmetic chains", "instruction following"]})
        paired = self.bench.api.read("bench/compare", {"a": second["id"], "b": first["id"]})["comparison"]["paired"]
        self.assertEqual(paired["paired"], 50)
        self.assertEqual((paired["only_this"], paired["only_other"]), (1, 2))
        self.assertEqual(paired["verdict"], "no statistically significant difference detected")
        self.assertEqual(paired["identical_replies"], 47)
        _, third = self.bench.run("bench/quality", {"target": {"target": "fake"}, "categories": ["short QA"], "max_tokens": 64})
        paired = self.bench.api.read("bench/compare", {"a": third["id"], "b": first["id"]})["comparison"]["paired"]
        self.assertEqual(paired["paired"], 0)
        self.assertEqual(paired["exclusions"]["request differs (generation settings)"], 20)

    def test_tools_scoring_determinism_and_inert_mocks(self):
        self.bench.server.config["responder"] = responders.tools_responder(wrong={"types-thermostat"})
        record, run = self.bench.run("bench/tools", {"target": {"target": "fake"}, "preset": "short", "repeats": 2})
        self.assertEqual(record["state"], "succeeded", record["error"])
        results = {r["scenario"]: r for r in run["detail"]["results"] if r["trial"] == 0}
        self.assertFalse(results["types-thermostat"]["passed"])
        for scenario in ("single-weather", "parallel-weather", "decline-delete", "follow-weather"):
            self.assertTrue(results[scenario]["passed"], results[scenario])
        self.assertEqual(run["summary"]["deterministic"], "6/6")
        self.assertNotIn("pass_at_k", run["summary"])
        follow = [r["body"] for r in self.bench.server.requests if r["method"] == "POST" and r["body"]["messages"][-1]["role"] == "tool"]
        self.assertTrue(follow and all("temperature_c" in b["messages"][-1]["content"] for b in follow))
        stable(self, self.bench.api.read("bench/runs/" + run["id"]))

    def test_schema_invalid_arguments_fail(self):
        def respond(body):
            if body["messages"][-1]["role"] == "tool":
                return {"text": "27"}
            return {"text": "", "tool_calls": [{"id": "c0", "function": {"name": "get_weather", "arguments": '{"city": "Paris", "unit": "kelvin"}'}},
                                               {"id": "c1", "function": {"name": "get_weather", "arguments": '{"city": "Rome", "unit": "kelvin"}'}}]}
        self.bench.server.config["responder"] = respond
        _, run = self.bench.run("bench/tools", {"target": {"target": "fake"}, "preset": "short"})
        weather = next(r for r in run["detail"]["results"] if r["scenario"] == "parallel-weather")
        self.assertFalse(weather["passed"])
        self.assertIn("is not one of celsius, fahrenheit", weather["reason"])

    def test_stored_errors_are_redacted(self):
        self.bench.server.config.update(status=500)
        from hyperloom.bench import client as bench_client
        original = bench_client._error_text
        with mock.patch.object(bench_client, "_error_text", lambda data: ": upstream said Bearer sk-secret-value-123"):
            _, run = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [1], "max_tokens": 16, "trace": True})
        self.assertIs(bench_client._error_text, original)
        from hyperloom.bench import store as bench_store
        stored = bench_store.get(self.bench.api.telemetry, run["id"])
        self.assertNotIn("sk-secret-value-123", json.dumps(stored))

    def test_cancelled_ab_keeps_its_trials(self):
        self.bench.server.config["token_delay"] = 0.05
        job = self.bench.start("bench/ab", {"arms": {"a": {"profile": "qwen"}, "b": {"target": "fake"}}, "cases": 4, "max_tokens": 8})
        import time
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                run = self.bench.api.read("bench/runs/" + job["arguments"]["run_id"])["run"]
            except APIError:        # the job has not written its row yet
                run = {}
            if len((run.get("detail") or {}).get("trials") or []) >= 2:
                break
            time.sleep(0.1)
        self.bench.api.mutate("cancel", {"job": job["id"]})
        self.bench.wait(job, 20)
        run = self.bench.api.read("bench/runs/" + job["arguments"]["run_id"])["run"]
        self.assertEqual(run["state"], "cancelled")
        self.assertGreaterEqual(len(run["detail"]["trials"]), 2)
        self.assertIsNone(run["detail"]["receipt"])

    def test_demo_session(self):
        self.bench.server.config["responder"] = responders.demo_responder()
        record, run = self.bench.run("bench/demo", {"target": {"target": "fake"}, "texts": ["Tell a story.", "List three rivers."], "max_tokens": 64})
        self.assertEqual(record["state"], "succeeded", record["error"])
        tiles = run["detail"]["tiles"]
        self.assertEqual([t["state"] for t in tiles], ["done", "done"])
        self.assertTrue(all(t["e2e_rate"] for t in tiles))
        with self.assertRaises(APIError):
            self.bench.start("bench/demo", {"target": {"target": "fake"}, "texts": ["x" * 4000] * 15})
        stable(self, self.bench.api.read("bench/runs/" + run["id"]))


class DocumentsTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.0005)

    def test_gets_never_create_the_store(self):
        self.bench.api.telemetry = None
        listing = self.bench.api.read("bench/runs")
        self.assertIsNone(listing["runs"])
        self.assertIn("not open", listing["why"]["runs"])
        with self.assertRaises(APIError):
            self.bench.api.read("bench/runs/" + "a" * 16)
        targets_doc = self.bench.api.read("bench/targets")
        self.assertIn("adhoc", targets_doc["why"])
        self.assertIsNone(getattr(self.bench.api, "telemetry"))
        self.assertFalse((Path(self.bench.dir.name) / "telemetry.sqlite3").exists())

    def test_targets_document_and_allow_list_rules(self):
        document = self.bench.api.read("bench/targets")
        stable(self, document)
        keys = {t["key"]: t for t in document["targets"]}
        self.assertIsNone(keys["profile:qwen"]["blocker"])
        self.assertIn("start it first", keys["profile:other"]["blocker"])
        self.assertFalse(keys["profile:qwen"]["sampling"])
        self.assertNotIn(support.KEY, json.dumps(document))
        self.assertIsNone(keys["target:fake"]["key_ref"])
        for bad in ({"id": "x", "scheme": "ftp", "host": "a", "port": 1}, {"id": "x", "scheme": "http", "host": "a/b", "port": 1},
                    {"id": "x", "scheme": "http", "host": "user@a", "port": 1}, {"id": "x", "scheme": "http", "host": "a", "port": 0},
                    {"id": "x", "scheme": "http", "host": "a", "port": 1, "machines": ["nowhere"]},
                    {"id": "x", "scheme": "http", "host": "a", "port": 1, "key": "Not A Secret"}):
            with self.subTest(entry=bad), self.assertRaises(APIError):
                self.bench.api.feature_post("bench/targets/save", bad)
        self.assertEqual(targets.resources(self.bench.api, ("target", "fake")), {"host:127.0.0.1"})
        targets.save(self.bench.api, {"id": "undeclared", "scheme": "http", "host": "halo-1", "port": 18080})
        self.assertEqual(targets.resources(self.bench.api, ("target", "undeclared")), {"*"})
        targets.save(self.bench.api, {"id": "forwarder", "scheme": "http", "host": "halo-1", "port": 18080, "machines": ["halo-1", "halo-2"]})
        self.assertEqual(targets.resources(self.bench.api, ("target", "forwarder")), {"halo-1", "halo-2"})
        self.bench.api.feature_post("bench/targets/capabilities", {"profile": "qwen", "thinking": True})
        self.assertTrue({t["key"]: t for t in self.bench.api.read("bench/targets")["targets"]}["profile:qwen"]["thinking"])
        audit = Path(self.bench.dir.name, "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("bench/targets/capabilities", audit)

    def test_history_share_and_clear(self):
        _, a = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [1], "max_tokens": 16})
        _, b = self.bench.run("bench/decode", {"target": {"target": "fake"}, "levels": [1, 2], "max_tokens": 16})
        comparison = self.bench.api.read("bench/compare", {"a": a["id"], "b": b["id"]})["comparison"]
        self.assertEqual([row["level"] for row in comparison["rows"]], [1, 2])
        share = self.bench.api.read("bench/share/" + a["id"])
        stable(self, share)
        text = json.dumps(share)
        for leaked in ("127.0.0.1", str(self.bench.server.port), "fixture-model", "qwen\"", "Write", "hlb-"):
            self.assertNotIn(leaked, text)
        self.assertEqual(share["share"]["target"], "qwen-replicas")
        self.assertEqual(self.bench.api.read("bench/share/" + b["id"])["share"]["target"], "ad-hoc endpoint")
        self.assertIn("hyperloom bench · decode", share["text"])
        listing = self.bench.api.read("bench/runs", {"kind": "decode"})["runs"]
        self.assertEqual(len(listing), 2)
        with self.assertRaises(APIError):
            self.bench.api.feature_post("bench/runs/clear", {"kind": "decode"})
        self.assertEqual(self.bench.api.feature_post("bench/runs/clear", {"confirm": "clear", "run": a["id"]})[1], {"removed": 1})
        self.assertEqual(self.bench.api.feature_post("bench/runs/clear", {"confirm": "clear"})[1], {"removed": 1})
        self.assertEqual(self.bench.api.read("bench/runs")["runs"], [])
        with self.assertRaises(APIError) as caught:
            self.bench.api.read("bench/compare", {"a": a["id"], "b": b["id"]})
        self.assertEqual(caught.exception.status, 404)

    def test_traces_are_kept_with_the_run(self):
        _, run = self.bench.run("bench/decode", {"target": {"profile": "qwen"}, "levels": [2], "max_tokens": 16, "trace": True})
        traces = run["detail"]["traces"]
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(len(t["body_sha256"]) == 64 and t["request_id"].startswith("hlb-" + run["id"]) for t in traces))
        stable(self, self.bench.api.read("bench/runs/" + run["id"]))

    def test_preview_and_corpus(self):
        _, document = self.bench.api.feature_post("bench/preview", {"kind": "tools", "arguments": {"target": {"profile": "qwen"}, "preset": "hard", "repeats": 3}})
        self.assertEqual(document["command"], "hyperloom bench tools --profile qwen --preset hard --repeats 3")
        from hyperloom.bench import commands
        self.assertEqual(commands._quote("it's $HOME", "powershell"), "'it''s $HOME'")
        self.assertEqual(commands._quote("it's $(id)", "posix"), "'it'\"'\"'s $(id)'")
        corpus = self.bench.api.read("bench/corpus")
        self.assertEqual(sorted(corpus["prompt_sets"]), ["code", "json", "prose", "structured"])
        self.assertEqual(len(corpus["tools"]["presets"]["short"]), 6)

    def test_guard_reads_without_writing(self):
        document = self.bench.api.read("bench/guard", {"target": "profile:qwen"})
        self.assertTrue(document["preview"]["admitted"])
        self.assertEqual(document["preview"]["resources"], ["halo-1", "router"])
        hold = self.bench.api.admission_db.acquire("core", {"halo-1"}, label="up qwen")
        document = self.bench.api.read("bench/guard", {"target": "profile:qwen"})
        self.assertFalse(document["preview"]["admitted"])
        self.assertIn("deploy, start or install is running", document["preview"]["refusal"])
        self.assertEqual([h["kind"] for h in document["holds"]], ["core"])
        self.bench.api.admission_db.release(hold)
        stable(self, document)


class HistoryUnitTests(unittest.TestCase):
    def test_share_cleaning_drops_free_text(self):
        run = {"kind": "decode", "state": "succeeded", "started": 1.5, "finished": 2.5, "recipe_digest": "not-hex",
               "summary": {"family": "qwen-replicas", "prompt_set": "prose", "max_tokens": 64,
                           "levels": [{"level": 1, "aggregate_rate": float("nan"), "streams": 1}]}}
        share = history.share(run)
        self.assertIsNone(share["recipe_digest"])
        self.assertIsNone(share["numbers"]["levels"][0]["aggregate_rate"])
        run["summary"]["prompt_set"] = "http://halo-1/x"
        self.assertIsNone(history.share(run)["numbers"]["prompt_set"])
        run["summary"]["family"] = "my-secret-host"
        self.assertEqual(history.share(run)["target"], "ad-hoc endpoint")


if __name__ == "__main__":
    unittest.main()
