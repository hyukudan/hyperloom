"""Adapters of the observability package: router request log, sessions, histograms; engine and MiniMax extras."""

import json
import math
import unittest

from hyperloom.adapters import AdapterError, engine, minimax, prom, router
from tests.fixtures.observe.fakes import HERE, fixture


LEGACY = json.loads((HERE.parent / "adapters" / "engine-status.json").read_text(encoding="utf-8"))


class RouterRequestLogTests(unittest.TestCase):
    def test_page_fields_and_records(self):
        page = router.parse_requests(fixture("router-requests.json"))
        self.assertEqual((page.instance, page.capacity, page.seq, page.next, page.more), ("9f1c0a5b2d3e4f60", 512, 10, 10, False))
        self.assertEqual([r["seq"] for r in page.records], [7, 8, 9, 10])
        first = page.records[0]
        self.assertEqual(first["stamps"]["first_data_event"], 845.2)
        self.assertEqual(first["attempts"][1], {"n": 2, "kind": "decode", "target": "halo-1", "at_ms": 812.6,
                                                "outcome": "served", "why": None})
        self.assertEqual(first["usage"]["imported_tokens"], 7168)
        self.assertEqual(first["engine"]["first_token_ms"], 15.0)
        # null = not reached: absent from stamps, no reason
        refused = page.records[1]
        self.assertNotIn("tokenized", refused["stamps"])
        self.assertEqual((refused["why"], refused["parse_why"]), ("queue full", {}))
        self.assertIsNone(refused["usage"])

    def test_wrong_types_unknown_values_and_bounds(self):
        doc = fixture("router-requests.json")
        doc["records"][0]["status"] = "200"
        doc["records"][0]["outcome"] = "teleported"
        doc["records"][0]["stamps"]["last_byte"] = -1
        doc["records"][0]["attempts"] = [{"n": i, "kind": "decode"} for i in range(40)]
        doc["records"][0]["model"] = "m" * 1000
        doc["records"].append({"seq": 11})                          # no id: dropped with a reason
        doc["records"].append("not a record")
        page = router.parse_requests(doc)
        record = page.records[0]
        self.assertIsNone(record["status"])
        self.assertEqual(record["outcome"], "other")
        self.assertIn("unknown outcome", record["parse_why"]["outcome"])
        self.assertNotIn("last_byte", record["stamps"])
        self.assertEqual(len(record["attempts"]), router.MAX_ATTEMPTS)
        self.assertIn("further attempts", record["parse_why"]["attempts"])
        self.assertEqual(len(record["model"]), router.MAX_TEXT)
        self.assertEqual(len(page.records), 4)
        self.assertIn("records[4]", page.why)

    def test_the_attempts_a_join_needs_survive_the_cap(self):
        doc = fixture("router-requests.json")
        attempts = [{"n": i, "kind": "decode", "target": "halo-2", "outcome": "connect_failed"} for i in range(1, 30)]
        attempts.append({"n": 30, "kind": "decode", "target": "halo-1", "outcome": "served"})
        doc["records"][0]["attempts"] = attempts
        kept = router.parse_requests(doc).records[0]["attempts"]
        self.assertEqual(len(kept), router.MAX_ATTEMPTS)
        self.assertEqual(kept[-1], {"n": 30, "kind": "decode", "target": "halo-1", "at_ms": None, "outcome": "served",
                                    "why": None})

    def test_schema(self):
        doc = fixture("router-requests.json")
        doc["schema"] = "something-else"
        with self.assertRaises(AdapterError):
            router.parse_requests(doc)
        del doc["schema"]
        self.assertIn("schema", router.parse_requests(doc).why)
        with self.assertRaises(AdapterError):
            router.parse_requests({"schema": router.REQUESTS_SCHEMA})
        with self.assertRaises(AdapterError):
            router.parse_requests([])

    def test_record_cap(self):
        doc = fixture("router-requests.json")
        base = doc["records"][0]
        doc["records"] = [dict(base, seq=i, id=f"{i:016x}") for i in range(600)]
        page = router.parse_requests(doc)
        self.assertEqual(len(page.records), router.MAX_RECORDS)
        self.assertIn("ignored", page.why["records"])

    def test_sessions(self):
        head, sessions, why = router.parse_sessions(fixture("router-sessions.json"))
        self.assertEqual(head["idle_s"], 1800)
        self.assertEqual(sessions[0]["turns"], 2)
        self.assertEqual(sessions[0]["replicas"], {"halo-1": 2})
        self.assertEqual([t["cached_tokens"] for t in sessions[0]["recent"]], [1024, 8192])
        doc = fixture("router-sessions.json")
        doc["sessions"].append({"turns": 3})                       # no key: dropped
        doc["sessions"][0]["recent"] = [{"at": 1.0}] * 40
        _, sessions, _ = router.parse_sessions(doc)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]["recent"]), router.MAX_TURNS)


class HistogramTests(unittest.TestCase):
    def test_families_and_quantiles(self):
        families = router.histograms(prom.parse(fixture("router-metrics.txt")), "tensormeld_router_")
        self.assertEqual(sorted(families), ["delivery_gap_seconds", "ttft_seconds"])
        line = families["delivery_gap_seconds"][0]
        self.assertEqual(line["labels"], {"caller": "default", "model": "qwen3.8-flash-next", "route": "rtx-long"})
        self.assertEqual(line["count"], 100)
        self.assertAlmostEqual(router.quantile(line["buckets"], 0.5), 0.01 + 0.04 * 40 / 80)
        self.assertEqual(router.quantile(line["buckets"], 1.0), 0.1)
        self.assertIsNone(router.quantile([[math.inf, 0]], 0.5))
        self.assertEqual(router.quantile([[1.0, 0], [math.inf, 5]], 0.5), 1.0)    # in +Inf: the last finite bound

    def test_broken_families_are_dropped(self):
        text = ('x_h_bucket{le="1"} 5\nx_h_bucket{le="2"} 3\nx_h_bucket{le="+Inf"} 6\n'       # decreasing
                'x_g_bucket{le="1"} 1\nx_g_bucket{le="2"} 2\n')                               # no +Inf
        self.assertEqual(router.histograms(prom.parse(text), "x_"), {})


class EngineExtrasTests(unittest.TestCase):
    def test_existing_parsing_is_unchanged(self):
        status = engine.parse_status(LEGACY)
        self.assertEqual(status.requests[0]["engine_ttft_ms"], 15.271742 + 3741.59993)
        observed = engine.parse_observed(LEGACY)
        self.assertEqual(observed["requests"][0]["cached_tokens"], 0)              # reused_tokens fallback
        self.assertEqual(observed["requests"][0]["draft_proposed"], 138)           # mtp_* fallback
        self.assertIsNone(observed["requests"][0]["first_token_ms"])
        self.assertIsNone(observed["instance"])

    def test_c1_fields(self):
        observed = engine.parse_observed(fixture("engine-status-c1.json"))
        self.assertEqual((observed["instance"], observed["started_at"], observed["armed"]), ("5a5a5a5a5a5a5a5a", 1799990000, True))
        self.assertEqual(observed["totals"], {"imported_tokens": 7168, "computed_tokens": 5710})
        first = observed["requests"][0]
        self.assertEqual((first["first_token_ms"], first["imported_tokens"], first["request_id"]),
                         (15.0, 7168, "0123456789abcdef.2"))
        spec = observed["speculative"]
        self.assertEqual((spec["method"], spec["since"], spec["accepted"], spec["proposed"], spec["cycles"]),
                         ("mtp", "armed", 84, 115, 65))
        self.assertEqual(spec["window_rows"][2], 20)
        self.assertEqual(observed["prefix_cache"]["host_hits"], 1)
        self.assertEqual(observed["prefix_cache"]["disk.writes"], 0)
        self.assertNotIn("published", observed["prefix_cache"])

    def test_depth_forms(self):
        doc = fixture("engine-status-c1.json")
        doc["speculative"] = {"method": "dspark", "cycles": 10, "accepted": 12, "proposed": 30,
                              "depth": [{"depth": 1, "reached": 10, "hit": 8}, {"depth": 2, "reached": 8, "hit": 4}]}
        self.assertEqual(engine.parse_observed(doc)["speculative"]["depth"][1], {"depth": 2, "reached": 8, "hit": 4})
        doc["speculative"] = {"method": "dspark", "reached": [10, 8], "hit": [8]}
        self.assertEqual(engine.parse_observed(doc)["speculative"]["depth"],
                         [{"depth": 1, "reached": 10, "hit": 8}, {"depth": 2, "reached": 8, "hit": None}])

    def test_metrics_extra(self):
        extra = engine.parse_metrics_extra(fixture("engine-metrics-c1.txt"))
        self.assertEqual((extra["imported_tokens"], extra["instance"], extra["start_time"], extra["armed"]),
                         (7168, "5a5a5a5a5a5a5a5a", 1799990000, 1))
        self.assertEqual(extra["histograms"]["ttft_seconds"][0]["count"], 2)


class MiniMaxTests(unittest.TestCase):
    def test_plain_lines(self):
        metrics, extra = minimax.parse_metrics(fixture("minimax-metrics-cand7.txt"))
        self.assertEqual((metrics.completed, metrics.generated_tokens, metrics.waiting, metrics.running), (12, 2048, 0, 1))
        self.assertEqual(metrics.why["cached_tokens"], "not reported by this MiniMax build")
        self.assertIsNone(minimax.speculative(extra))

    def test_scheduled_loop_and_shared_names_win(self):
        metrics, extra = minimax.parse_metrics(fixture("minimax-metrics-sched.txt"))
        self.assertEqual((metrics.batch_steps, metrics.batch_rows), (900, 1800))
        spec = minimax.speculative(extra)
        self.assertEqual((spec["accepted"], spec["proposed"], spec["cycles"], spec["draft_seconds"]), (1400, 2100, 700, 6.5))
        metrics, _ = minimax.parse_metrics(fixture("minimax-metrics-cand7.txt") + "strix_generated_tokens_total 4000\n")
        self.assertEqual(metrics.generated_tokens, 4000)

    def test_status_goes_through_the_shared_parser(self):
        status = engine.parse_status(fixture("minimax-status.json"))
        self.assertEqual((status.model, status.state, status.queue["running"]), ("minimax-m3", "generating", 1))
        observed = engine.parse_observed(fixture("minimax-status.json"))
        self.assertEqual(observed["instance"], "c0ffee00c0ffee00")
        self.assertEqual(observed["requests"][0]["computed_tokens"], 512)


class SpeculativeShapeTests(unittest.TestCase):
    def test_dspark_block(self):
        doc = fixture("engine-status-c1.json")
        del doc["speculative"]
        doc["dspark"] = {"steps": 100, "drafted_steps": 80, "drafts_verified": 240, "drafts_accepted": 180,
                         "draft_seconds": 1.5, "decode_tokens": 260, "window_rows": [0, 5, 20],
                         "depth": [{"depth": 1, "reached": 80, "hit": 70, "hit_rate": 0.875}]}
        spec = engine.parse_observed(doc)["speculative"]
        self.assertEqual((spec["method"], spec["cycles"], spec["proposed"], spec["accepted"], spec["since"]),
                         ("dspark", 80, 240, 180, "process start"))
        self.assertEqual(spec["depth"], [{"depth": 1, "reached": 80, "hit": 70}])
        self.assertEqual(spec["window_rows"], [0, 5, 20])

    def test_shared_block_without_method_and_verifier_rows(self):
        doc = fixture("engine-status-c1.json")
        doc["speculative"] = {"scope": "all draft sources", "since": "armed (completed)", "cycles": 10, "proposed": 30,
                              "accepted": 20, "verifier_window_rows": [1, 2, 3]}
        spec = engine.parse_observed(doc)["speculative"]
        self.assertEqual((spec["method"], spec["window_rows"]), ("mtp", [1, 2, 3]))
        self.assertIn("verifier", spec["window_label"])

    def test_status_histograms(self):
        observed = engine.parse_observed(fixture("engine-status-c1.json"))
        line = observed["histograms"]["ttft_seconds"][0]
        self.assertEqual(line["buckets"], [[0.05, 1], [0.5, 2], [math.inf, 2]])
        self.assertEqual(engine.json_histogram({"le": [0.1, 1], "counts": [1, 3, 4], "sum": 2.0, "count": 4})["buckets"],
                         [[0.1, 1], [1.0, 3], [math.inf, 4]])
        self.assertIsNone(engine.json_histogram({"buckets": [{"le": 1, "count": 5}, {"le": "+Inf", "count": 3}]}))
        self.assertIsNone(engine.json_histogram({"buckets": [{"le": 1, "count": 5}]}))
        self.assertEqual(engine.parse_observed(fixture("engine-status-c1.json"))["armed_since"], 1799990100)


if __name__ == "__main__":
    unittest.main()
