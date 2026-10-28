import copy
import json
import math
from pathlib import Path
import unittest

from hyperloom.adapters import AdapterError
from hyperloom.adapters import engine, prefill, prom, router
from tests.portable_fixtures import read_fixture


FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


def fixture(name):
    return json.loads(read_fixture(FIXTURES / name))


class PromTests(unittest.TestCase):
    def test_router_metrics(self):
        result = prom.parse((FIXTURES / "router-metrics.txt").read_text())
        self.assertEqual(result[("tensormeld_router_waiting", ())], 0)
        self.assertEqual(result[("tensormeld_router_replica_completed_total",
                                 (("node", "halo-1"), ("replica", "halo-1")))], 59)
        self.assertFalse(any("prefill" in key[0] for key in result))

    def test_labels_escapes_timestamp_and_exponent(self):
        result = prom.parse(r'metric:z{z="a}b,c", a="quote\" slash\\ newline\n",} -1.5e+2 1234')
        self.assertEqual(result[("metric:z", (("a", 'quote" slash\\ newline\n'),
                                              ("z", "a}b,c")))], -150)

    def test_empty_comments_and_unlabelled(self):
        self.assertEqual(prom.parse("# comment\n\n"), {})
        self.assertEqual(prom.parse("x{} 2\nx_2 0.5\n"), {("x", ()): 2., ("x_2", ()): .5})

    def test_special_floats(self):
        result = prom.parse("a NaN\nb +Inf\nc -Inf")
        self.assertTrue(math.isnan(result[("a", ())]))
        self.assertEqual(result[("b", ())], math.inf)
        self.assertEqual(result[("c", ())], -math.inf)

    def test_malformed(self):
        for text in ('x', '1x 1', 'x 1junk', 'x 1 2.5', 'x{a="b" 1',
                     'x{a="b",a="c"} 1', r'x{a="bad\t"} 1', 'x{a="b" z="c"} 1',
                     'x{a=b} 1', 'x 1\nx 2', 'x{a="1",b="2"} 1\nx{b="2",a="1"} 2'):
            with self.subTest(text=text), self.assertRaisesRegex(AdapterError, "line"):
                prom.parse(text)

    def test_nontext(self):
        with self.assertRaises(AdapterError):
            prom.parse(None)


class RouterTests(unittest.TestCase):
    def test_captured_status(self):
        result = router.parse_status(fixture("router-status.json"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.replicas[0]["completed"], 59)
        self.assertEqual(result.prefill["placements"]["local"], 108)
        self.assertEqual(result.why, {})

    def test_service_data(self):
        result = router.parse_status(fixture("router-services.json"))
        self.assertEqual(result.prefill["services"][0]["inflight"], 1)
        self.assertEqual(result.prefill["pending_s"], {"workstation": .2})
        self.assertEqual(result.why, {})

    def test_missing_fields(self):
        result = router.parse_status({"schema": router.SCHEMA, "replicas": [{"id": "halo-1"}]})
        self.assertIsNone(result.waiting)
        self.assertIsNone(result.prefill)
        self.assertIsNone(result.replicas[0]["completed"])
        self.assertIn("waiting", result.why)
        self.assertIn("replicas[0].completed", result.why)

    def test_invalid_nested_values(self):
        obj = fixture("router-status.json")
        obj.update(waiting=True, rejected=-1, held=1)
        obj["replicas"] = [None, {"id": "halo-1", "last_health_ms": float("nan")}]
        result = router.parse_status(obj)
        self.assertIsNone(result.waiting)
        self.assertIsNone(result.rejected)
        self.assertIsNone(result.held)
        self.assertIsNone(result.replicas[0])
        self.assertIn("replicas[0]", result.why)
        self.assertIn("replicas[1].last_health_ms", result.why)

    def test_schema_required(self):
        for obj in ({}, None, [], {"schema": "tensormeld/replica-router-status-v2"}):
            with self.subTest(obj=obj), self.assertRaises(AdapterError):
                router.parse_status(obj)


class EngineTests(unittest.TestCase):
    def test_captured_status_and_engine_ttft(self):
        obj = fixture("engine-status.json")
        before = copy.deepcopy(obj)
        result = engine.parse_status(obj)
        request = result.requests[0]
        self.assertAlmostEqual(request["engine_ttft_ms"], 15.271742 + 3741.59993)
        self.assertEqual(result.ttft_label, "Engine-side TTFT (queue+prefill)")
        self.assertEqual(result.totals["generated_tokens"], 9910)
        self.assertIsNone(result.memory)
        self.assertIn("memory", result.why)
        self.assertEqual(obj, before)

    def test_deepseek_nested_extras(self):
        result = engine.parse_status(fixture("deepseek-status.json"))
        self.assertEqual(result.memory["trellis_identity"], "quality-gated")
        self.assertEqual(result.memory["device_total_bytes"], 20000000)
        self.assertEqual(result.slots["pool_largest_free"], 65536)
        self.assertEqual(result.group["transport"], "usb4gpu")
        self.assertEqual(result.batch["rows"], 20)
        self.assertEqual(result.prefill["path"], "device")
        self.assertEqual(result.why, {"prefill.prefix_cache": "missing from component response"})

    def test_missing_and_invalid_requests(self):
        result = engine.parse_status({"schema": engine.SCHEMA, "requests": [None, {"id": "request-1", "queue_ms": 1}]})
        self.assertIsNone(result.state)
        self.assertIsNone(result.requests[0])
        self.assertIsNone(result.requests[1]["engine_ttft_ms"])
        self.assertIn("requests[1].engine_ttft_ms", result.why)
        self.assertIn("requests[0]", result.why)

    def test_nonfinite_negative_boolean_are_unavailable(self):
        result = engine.parse_status({"schema": engine.SCHEMA, "context_tokens": True,
                                      "uptime_seconds": float("inf"), "queue": {"running": -1}})
        self.assertIsNone(result.context_tokens)
        self.assertIsNone(result.uptime_seconds)
        self.assertIsNone(result.queue["running"])

    def test_schema_required(self):
        for obj in ({}, [], {"schema": "strix-serve-status-v2"}):
            with self.subTest(obj=obj), self.assertRaises(AdapterError):
                engine.parse_status(obj)

    def test_metrics(self):
        result = engine.parse_metrics((FIXTURES / "engine-metrics.txt").read_text())
        self.assertEqual(result.completed, 3)
        self.assertEqual(result.generated_tokens, 30)
        self.assertEqual(result.cached_tokens, 20)
        self.assertEqual(result.busy_seconds, 2.5)
        self.assertEqual(result.group_exchanges, 12)
        self.assertEqual(result.why, {})
        self.assertEqual(result.throughput_label, "completed tok/s over the sampling window")

    def test_missing_metrics_and_nonfinite(self):
        result = engine.parse_metrics('strix_generated_tokens_total NaN\nstrix_queue_running -1\n'
                                      'strix_requests_completed_total{node="halo-1"} 2')
        self.assertIsNone(result.generated_tokens)
        self.assertIsNone(result.running)
        self.assertIsNone(result.completed)
        self.assertIn("generated_tokens", result.why)


class PrefillTests(unittest.TestCase):
    def test_health(self):
        result = prefill.parse_health(fixture("prefill-health.json"))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.busy)
        self.assertEqual(result.served, 4)
        self.assertIsNone(result.eta_s)
        self.assertIn("schema", result.why)
        self.assertIn("eta_s", result.why)

    def test_progress_and_nulls(self):
        result = prefill.parse_health(fixture("prefill-progress.json"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.position, 0)
        self.assertFalse(result.cancel_pending)
        self.assertIsNone(result.host_free_commit_bytes)
        self.assertEqual(result.why["eta_s"], "component reported null")

    def test_partial_health(self):
        result = prefill.parse_health({"status": "new-state", "busy": 1})
        self.assertIsNone(result.status)
        self.assertIsNone(result.busy)
        self.assertIn("queued", result.why)

    def test_describe(self):
        result = prefill.parse_describe(fixture("prefill-describe.json"))
        self.assertEqual(result.service["max_positions"], 32768)
        self.assertTrue(result.service["expert_residency"])
        self.assertEqual(result.device_free_gb, 80.)
        self.assertEqual(set(result.why), {"schema"})

    def test_partial_describe(self):
        result = prefill.parse_describe({})
        self.assertIsNone(result.describe)
        self.assertIsNone(result.service)
        self.assertIn("device_total_gb", result.why)

    def test_explicit_schemas_rejected(self):
        for parser in (prefill.parse_health, prefill.parse_describe):
            for obj in ([], {"schema": "prefill-v2"}, {"schema": None}):
                with self.subTest(parser=parser.__name__, obj=obj), self.assertRaises(AdapterError):
                    parser(obj)


if __name__ == "__main__":
    unittest.main()
