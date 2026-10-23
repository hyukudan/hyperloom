import unittest

from hyperloom.adapters import engine
from hyperloom.stats import Window


def request(index, stamp, value):
    return {"id": f"request-{index:03}", "time": stamp, "queue_ms": 2., "prefill_ms": value,
            "decode_ms": value * 2, "decode_tok_s": value}


class RateTests(unittest.TestCase):
    def test_rate_and_zero(self):
        w = Window()
        self.assertIsNone(w.rate("generated", 100, now=1).value)
        result = w.rate("generated", 130, now=3)
        self.assertEqual(result.value, 15)
        self.assertIn("completed", result.label)
        self.assertEqual(w.rate("generated", 130, now=4).value, 0)

    def test_restart(self):
        w = Window()
        w.rate("generated", 100, now=1)
        w.add_requests([request(1, 1, 10)], now=1)
        result = w.rate("generated", 5, now=2)
        self.assertIsNone(result.value)
        self.assertTrue(result.reset)
        self.assertIn("restarted", result.why)
        self.assertEqual(w.quantile("decode_ms", .5, now=2).n, 0)
        self.assertEqual(w.rate("generated", 15, now=4).value, 5)

    def test_missing_breaks_baseline(self):
        for value in (None, True, -1, float("inf"), float("nan")):
            with self.subTest(value=value):
                w = Window()
                w.rate("generated", 10, now=1)
                self.assertIsNone(w.rate("generated", value, now=2).value)
                self.assertIsNone(w.rate("generated", 30, now=3).value)

    def test_separate_counters(self):
        w = Window()
        w.rate("prompt", 10, now=1)
        w.rate("generated", 100, now=1)
        self.assertEqual(w.rate("prompt", 20, now=2).value, 10)
        self.assertEqual(w.rate("generated", 120, now=2).value, 20)

    def test_duplicate_and_backward_times_preserve_baseline(self):
        w = Window()
        w.rate("generated", 10, now=10)
        self.assertIsNone(w.rate("generated", 20, now=10).value)
        self.assertIsNone(w.rate("generated", 20, now=9).value)
        self.assertEqual(w.rate("generated", 30, now=12).value, 10)

    def test_metrics_integration(self):
        w = Window()
        for stamp, tokens in ((1, 10), (3, 20)):
            metrics = engine.parse_metrics(f"strix_generated_tokens_total {tokens}")
            result = w.rate("generated_tokens", metrics.generated_tokens, now=stamp)
        self.assertEqual(result.value, 5)


    def test_restart_invalidates_other_counter_baselines(self):
        w = Window()
        w.rate("generated", 100, now=1)
        w.rate("prompt", 200, now=1)
        self.assertTrue(w.rate("generated", 5, now=2).reset)
        self.assertIsNone(w.rate("prompt", 500, now=2).value)
        self.assertEqual(w.rate("prompt", 520, now=4).value, 10)


class RequestTests(unittest.TestCase):
    def test_deduplicated_by_id_and_not_modified(self):
        w = Window()
        records = [request(1, 100, 10), request(2, 101, 20)]
        self.assertEqual(w.add_requests(records, now=102), 2)
        records[0]["prefill_ms"] = 500
        self.assertEqual(w.add_requests(records, now=103), 2)
        result = w.quantile("prefill_ms", .5, now=103)
        self.assertEqual((result.value, result.n), (15, 2))

    def test_last_fifty_out_of_order(self):
        w = Window()
        records = [request(i, 1000-i, i) for i in range(60)]
        self.assertEqual(w.add_requests(records, now=1000), 50)
        self.assertEqual(w.quantile("prefill_ms", 1, now=1000).value, 49)
        self.assertEqual(w.add_requests(reversed(records), now=1000), 50)
        self.assertEqual(w.quantile("prefill_ms", 1, now=1000).value, 49)

    def test_tied_timestamps_do_not_rotate_on_repoll(self):
        w = Window()
        records = [request(i, 1000, i) for i in range(60)]
        w.add_requests(records, now=1000)
        result = w.quantile("prefill_ms", .5, now=1000)
        w.add_requests(reversed(records), now=1001)
        self.assertEqual(w.quantile("prefill_ms", .5, now=1001), result)

    def test_ten_minute_expiry_without_new_polls(self):
        w = Window()
        w.add_requests([request(1, 1000, 10)], now=1000)
        self.assertEqual(w.quantile("prefill_ms", .5, now=1600).n, 1)
        self.assertEqual(w.quantile("prefill_ms", .5, now=1601).n, 0)
        self.assertEqual(w.add_requests([request(1, 1000, 10)], now=1601), 0)

    def test_old_future_malformed_and_missing_ids(self):
        w = Window()
        records = [None, {}, {"id": "", "time": 1000}, {"id": "x", "time": True},
                   request(1, 1, 10), request(2, 1001, 10), {"id": [], "time": 1000}]
        self.assertEqual(w.add_requests(records, now=1000), 0)
        self.assertEqual(w.add_requests(None, now=1000), 0)

    def test_missing_values_and_sample_count(self):
        w = Window()
        records = [request(1, 100, 10), request(2, 100, 20), request(3, 100, 30)]
        records[2]["prefill_ms"] = None
        w.add_requests(records + [{"id": "missing", "time": 100}], now=100)
        result = w.quantile("prefill_ms", .95, now=100)
        self.assertAlmostEqual(result.value, 19.5)
        self.assertEqual(result.n, 2)
        empty = w.quantile("unsupported", .5, now=100)
        self.assertIsNone(empty.value)
        self.assertEqual(empty.n, 0)
        self.assertTrue(empty.why)

    def test_engine_ttft_label_and_formula(self):
        w = Window()
        w.add_requests([request(1, 100, 10), request(2, 100, 20)], now=100)
        result = w.quantile("engine_ttft_ms", .5, now=100)
        self.assertEqual((result.value, result.n), (17, 2))
        self.assertEqual(result.label, "Engine-side TTFT (queue+prefill)")

    def test_clear_and_injected_clock(self):
        w = Window(clock=lambda: 100)
        w.add_requests([request(1, 100, 10)])
        w.rate("generated", 100)
        w.clear()
        self.assertEqual(w.quantile("prefill_ms", .5).n, 0)
        self.assertIsNone(w.rate("generated", 200).value)

    def test_limits_and_invalid_arguments(self):
        for kwargs in ({"max_requests": 0}, {"max_requests": True}, {"max_age_s": 0},
                       {"max_age_s": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Window(**kwargs)
        w = Window()
        for q in (-.1, 1.1, True, None):
            with self.subTest(q=q), self.assertRaises(ValueError):
                w.quantile("prefill_ms", q, now=1)
        with self.assertRaises(ValueError):
            w.add_requests([], now=float("inf"))


if __name__ == "__main__":
    unittest.main()
