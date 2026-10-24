"""Telemetry store, typed series, baselines and samplers."""

from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import telemetry
from hyperloom.telemetry.store import MAX_POINTS, Store


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def test_raw_and_rollups_with_gaps_not_zeros(self):
        t0 = self.clock.now - 600
        for i in range(30):
            self.store.record([("gpu_busy", {"machine": "a"}, i), ("gpu_busy", {"machine": "b"}, None),
                               ("bad", {}, float("nan"))], ts=t0 + 2 * i)
        self.store.record([("gpu_busy", {"machine": "a"}, 100)], ts=t0 + 300)  # after a gap
        raw = self.store.query("gpu_busy", t0, labels={"machine": "a"}, tier="raw")
        self.assertEqual(len(raw), 1)
        self.assertEqual(len(raw[0]["points"]), 31)
        minute = self.store.query("gpu_busy", t0, tier="m1")[0]
        buckets = [p[0] for p in minute["points"]]
        self.assertEqual(buckets, sorted(set(buckets)))
        self.assertTrue(all(b % 60 == 0 for b in buckets))
        self.assertEqual(sum(p[4] for p in minute["points"]), 31)
        self.assertEqual(max(p[2] for p in minute["points"]), 100)
        # Nothing was invented for the missing series or the silent minutes.
        self.assertEqual(self.store.query("gpu_busy", t0, labels={"machine": "b"}, tier="raw"), [])
        self.assertEqual(self.store.query("bad", t0), [])

    def test_tier_choice_and_retention(self):
        now = self.clock.now
        self.assertEqual(Store.pick_tier(now - 1800, now, now)[0], "raw")
        self.assertEqual(Store.pick_tier(now - 86400, now, now)[0], "m1")
        self.assertEqual(Store.pick_tier(now - 7 * 86400, now, now)[0], "m15")
        self.store.record([("x", {}, 1)], ts=now - 9 * 3600)
        self.store.prune(now)
        self.assertEqual(self.store.query("x", now - 10 * 3600, tier="raw")[0]["points"], [])
        self.assertEqual(len(self.store.query("x", now - 10 * 3600, tier="m1")[0]["points"]), 1)
        self.assertLessEqual(MAX_POINTS, 2000)

    def test_latest_events_and_docs(self):
        self.store.record([("t", {"m": "a"}, 1)], ts=self.clock.now - 100)
        self.store.record([("t", {"m": "a"}, 2)], ts=self.clock.now - 1)
        self.assertEqual(self.store.latest("t")[0]["value"], 2)
        self.assertEqual(self.store.latest("t", max_age=0.5), [])
        ids = [self.store.event("health", s, "msg " + s, machine="a") for s in ("info", "warning", "critical")]
        self.assertEqual([e["id"] for e in self.store.events(severity="warning")], ids[:0:-1])
        self.assertEqual(len(self.store.events(before=ids[2])), 2)
        with self.assertRaises(ValueError):
            self.store.event("health", "fatal", "x")
        self.store.put("bench", "r1", {"score": 1})
        self.store.put("bench", "r1", {"score": 2})
        self.assertEqual(self.store.get("bench", "r1"), {"score": 2})
        self.assertEqual([k for k, _, _ in self.store.items("bench")], ["r1"])
        self.assertEqual(self.store.delete("bench"), 1)

    def test_labels_must_be_flat(self):
        with self.assertRaises(ValueError):
            self.store.record([("x", {"a": {"b": 1}}, 1)])


class SamplerTests(unittest.TestCase):
    def test_failures_are_counted_and_others_keep_running(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        sampler = telemetry.Sampler(object(), store, {
            "good": (lambda api, now: [("g", {}, 1.0)], 1.0),
            "bad": (lambda api, now: 1 / 0, 1.0)})
        sampler.run_once("good")
        sampler.run_once("bad")
        self.assertEqual(sampler.state["good"]["points"], 1)
        self.assertEqual(sampler.state["bad"]["failures"], 1)
        self.assertIn("ZeroDivisionError", sampler.state["bad"]["error"])
        self.assertEqual(len(store.latest("g")), 1)

    def test_store_for_uses_the_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(spec=["config_path", "clock"])
            api.config_path = Path(directory) / "cluster.json"
            api.clock = time.time
            store = telemetry.store_for(api)
            self.assertTrue((Path(directory) / "telemetry.sqlite3").exists())
            self.assertIs(telemetry.store_for(api), store)
            store.close()
            telemetry._stores.clear()


class TypedSeriesTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def test_kinds_are_fixed_per_series_and_deltas_sum(self):
        t0 = self.clock.now - 120
        for i in range(10):
            self.store.record([("tokens", {"m": "a"}, 5, "delta"), ("busy", {}, i)], ts=t0 + 2 * i)
        with self.assertRaises(ValueError):
            self.store.record([("tokens", {"m": "a"}, 1, "gauge")])
        with self.assertRaises(ValueError):
            self.store.record([("tokens", {"m": "a"}, -1, "delta")])
        with self.assertRaises(ValueError):
            self.store.record([("x", {}, 1, "counter")])
        rows = self.store.query("tokens", t0, tier="m1")[0]
        self.assertEqual(rows["kind"], "delta")
        self.assertEqual(sum(p[5] for p in rows["points"]), 50)

    def test_baselines_survive_restarts_without_negative_work(self):
        base = telemetry.Baselines(self.store)
        self.assertEqual(base.delta("e1", "i1", 100, 10), (None, None))
        self.assertEqual(base.delta("e1", "i1", 130, 12), (30, 0.0))
        self.assertEqual(base.delta("e1", "i1", 5, 14), (None, 2))          # counter went backwards
        self.assertEqual(base.delta("e1", "i2", 9, 20), (None, 6))          # new engine instance
        again = telemetry.Baselines(self.store)                            # portal restart
        self.assertEqual(again.delta("e1", "i2", 19, 80), (10, 0.0))
        self.assertEqual(again.delta("e1", "i2", float("nan"), 81), (None, None))

    def test_feature_tables(self):
        self.store.ensure_table("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
        self.store.transaction([("INSERT INTO t VALUES (?)", (1,)), ("INSERT INTO t VALUES (?)", (2,))])
        self.assertEqual(self.store.execute("SELECT SUM(a) FROM t"), [(3,)])
        with self.assertRaises(ValueError):
            self.store.ensure_table("DROP TABLE t")


if __name__ == "__main__":
    unittest.main()
