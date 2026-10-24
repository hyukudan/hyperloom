"""Telemetry store: typed series, rollups, retention, counter baselines, events, documents and samplers."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import telemetry
from hyperloom.telemetry.store import MAX_EVENTS, MAX_POINTS, Store


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
        self.assertEqual(len(raw["lines"]), 1)
        self.assertEqual(len(raw["lines"][0]["points"]), 31)
        minute = self.store.query("gpu_busy", t0, tier="m1")["lines"][0]
        buckets = [p[0] for p in minute["points"]]
        self.assertEqual(buckets, sorted(set(buckets)))
        self.assertTrue(all(b % 60 == 0 for b in buckets))
        self.assertEqual(sum(p[4] for p in minute["points"]), 31)
        self.assertEqual(max(p[2] for p in minute["points"]), 100)
        self.assertEqual(self.store.query("gpu_busy", t0, labels={"machine": "b"}, tier="raw")["lines"], [])
        self.assertEqual(self.store.query("bad", t0)["lines"], [])

    def test_tier_choice_full_ranges_and_retention(self):
        now = self.clock.now
        self.assertEqual(Store.pick_tier(now - 1800, now, now)[0], "raw")
        self.assertEqual(Store.pick_tier(now - 86400, now, now)[0], "m1")
        self.assertEqual(Store.pick_tier(now - 7 * 86400, now, now)[0], "m15")
        self.assertGreaterEqual(MAX_POINTS, 30 * 86400 // 900)
        # A full 30-day range of 15 min buckets comes back whole.
        start = now - 30 * 86400 + 1
        ts = start
        while ts < now:
            self.store.record([("x", {}, 1)], ts=ts)
            ts += 900
        result = self.store.query("x", start, now, tier="m15")
        self.assertEqual(result["truncated"], [])
        self.assertGreaterEqual(len(result["lines"][0]["points"]), 2879)
        self.store.prune(now)
        self.assertEqual(len(self.store.query("x", now - 9 * 3600, tier="raw")["lines"][0]["points"]), 32)

    def test_events_are_retained_on_their_own_path(self):
        for i in range(MAX_EVENTS + 6):
            self.store.event("health", "info", f"e{i}")
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM events"), [(MAX_EVENTS,)])

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
        with self.assertRaises(ValueError):
            self.store.events(severity="fatal")
        self.store.put("bench", "r1", {"score": 1})
        self.store.put("bench", "r1", {"score": 2})
        self.assertEqual(self.store.get("bench", "r1"), {"score": 2})
        self.assertEqual([k for k, _, _ in self.store.items("bench")], ["r1"])
        self.assertEqual(self.store.delete("bench"), 1)

    def test_labels_must_be_flat(self):
        with self.assertRaises(ValueError):
            self.store.record([("x", {"a": {"b": 1}}, 1)])

    def test_failed_batch_never_redirects_later_samples(self):
        self.store.describe("kinded", "delta")
        with self.assertRaises(ValueError):
            self.store.record([("fresh", {"m": "a"}, 1), ("kinded", {}, 1, "gauge")])
        self.store.record([("other", {"m": "b"}, 7)])
        self.store.record([("fresh", {"m": "a"}, 3)])
        self.assertEqual([p[1] for p in self.store.query("fresh", 0, tier="raw")["lines"][0]["points"]], [3])
        self.assertEqual([p[1] for p in self.store.query("other", 0, tier="raw")["lines"][0]["points"]], [7])


class TypedSeriesTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def test_kind_is_fixed_per_metric_and_deltas_sum(self):
        t0 = self.clock.now - 120
        self.store.describe("tokens", "delta", unit="tokens", interval=2)
        for i in range(10):
            self.store.record([("tokens", {"m": "a"}, 5), ("busy", {}, i)], ts=t0 + 2 * i)
        for bad in (("tokens", {"m": "b"}, 1, "gauge"), ("tokens", {"m": "a"}, -1), ("x", {}, 1, "counter")):
            with self.assertRaises(ValueError, msg=bad):
                self.store.record([bad])
        result = self.store.query("tokens", t0, tier="m1")
        self.assertEqual(result["descriptor"]["kind"], "delta")
        self.assertEqual(sum(p[5] for p in result["lines"][0]["points"]), 50)
        self.assertTrue(all(0 < p[7] <= 1 for p in result["lines"][0]["points"]))   # coverage from the interval
        with self.assertRaises(ValueError):
            self.store.describe("tokens", "gauge")

    def test_state_rollup_keeps_the_last_value(self):
        self.store.describe("engine_state", "state", legend={0: "stopped", 1: "ready"})
        base = int(self.clock.now // 60) * 60 - 120
        self.store.record([("engine_state", {}, 0)], ts=base + 1)
        self.store.record([("engine_state", {}, 1)], ts=base + 30)
        point = self.store.query("engine_state", base, tier="m1")["lines"][0]["points"][0]
        self.assertEqual(point[6], 1)
        self.assertEqual(self.store.descriptor("engine_state")["legend"], {"0": "stopped", "1": "ready"})
        with self.assertRaises(ValueError):
            self.store.describe("busy2", "gauge", legend={0: "x"})

    def test_counter_baselines_restarts_gaps_and_order(self):
        self.assertEqual(self.store.counter_delta("e1", "i1", 100, 10)[0], None)
        delta, info = self.store.counter_delta("e1", "i1", 130, 12, max_gap=10)
        self.assertEqual((delta, info["unobserved_s"], info["uncertain"]), (30, 0.0, False))
        self.assertEqual(self.store.counter_delta("e1", "i1", 140, 11)[1]["stale"], True)   # out of order
        delta, info = self.store.counter_delta("e1", "i1", 5, 14)                         # went backwards
        self.assertEqual((delta, info["unobserved_s"]), (None, 2))
        delta, info = self.store.counter_delta("e1", "i2", 9, 20)                        # new instance
        self.assertEqual((delta, info["unobserved_s"]), (None, 6))
        delta, info = self.store.counter_delta("e1", "i2", 19, 500, max_gap=10)          # long gap: kept, flagged
        self.assertEqual((delta, info["uncertain"]), (10, True))
        # Two views of the same key share one baseline (no overlapping deltas).
        a, b = telemetry.Baselines(self.store), telemetry.Baselines(self.store)
        self.assertEqual(a.delta("e1", "i2", 29, 502)[0], 10)
        self.assertEqual(b.delta("e1", "i2", 39, 504)[0], 10)

    def test_counter_accounting_commits_with_the_baseline(self):
        self.store.ensure_table("CREATE TABLE IF NOT EXISTS ledger (tokens INTEGER)")
        self.store.counter_delta("e", "i", 0, 1)

        def failing(delta, info):
            return {"sql": [("INSERT INTO ledger VALUES (?)", (delta,)), ("INSERT INTO nope VALUES (1)", ())]}
        with self.assertRaises(Exception):
            self.store.counter_delta("e", "i", 50, 2, apply=failing)
        # Nothing was accounted and the baseline did not move: the same increase is counted once on retry.
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM ledger"), [(0,)])
        delta, _ = self.store.counter_delta("e", "i", 50, 3, apply=lambda d, i: {
            "sql": [("INSERT INTO ledger VALUES (?)", (d,))], "points": [("gen", {}, d, "delta")]})
        self.assertEqual(delta, 50)
        self.assertEqual(self.store.execute("SELECT SUM(tokens) FROM ledger"), [(50,)])
        self.assertEqual(self.store.latest("gen")[0]["value"], 50)

    def test_feature_tables(self):
        self.store.ensure_table("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
        self.store.transaction([("INSERT INTO t VALUES (?)", (1,)), ("INSERT INTO t VALUES (?)", (2,))])
        self.assertEqual(self.store.execute("SELECT SUM(a) FROM t"), [(3,)])
        with self.assertRaises(ValueError):
            self.store.ensure_table("DROP TABLE t")


class SamplerTests(unittest.TestCase):
    def test_failures_are_counted_and_others_keep_running(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        sampler = telemetry.Sampler(object(), store, {
            "good": (lambda api, now: [("g", {}, 1.0)], 1.0),
            "bad": (lambda api, now: 1 / 0, 1.0)})
        sampler.run_once("good")
        sampler.run_once("bad")
        state = sampler.snapshot()
        self.assertEqual(state["good"]["points"], 1)
        self.assertEqual(state["bad"]["failures"], 1)
        self.assertIn("ZeroDivisionError", state["bad"]["error"])
        self.assertEqual(len(store.latest("g")), 1)

    def test_runs_never_overlap_and_refresh_coalesces(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        gate, calls = threading.Event(), []

        def slow(api, now):
            calls.append(now)
            gate.wait(5)
            return []
        sampler = telemetry.Sampler(object(), store, {"slow": (slow, 1.0)})
        first = threading.Thread(target=sampler.run_once, args=("slow",))
        first.start()
        for _ in range(100):
            if calls:
                break
            time.sleep(0.01)
        self.assertFalse(sampler.run_once("slow"))
        self.assertEqual(sampler.snapshot()["slow"]["coalesced"], 1)
        gate.set()
        first.join(5)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            sampler.set_interval("slow", 0.1)
        sampler.set_interval("slow", 5)
        self.assertEqual(sampler.snapshot()["slow"]["interval_s"], 5)

    def test_start_is_atomic_and_stop_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(spec=["config_path", "clock", "telemetry", "sampler"])
            api.config_path = Path(directory) / "cluster.json"
            api.clock = time.time
            api.telemetry = None
            registry = mock.Mock(collectors={"c": (lambda a, n: [("v", {}, 1)], 60)})
            started = []
            threads = [threading.Thread(target=lambda: started.append(telemetry.start(api, registry))) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(len({id(s) for s in started}), 1)
            self.assertTrue((Path(directory) / "telemetry.sqlite3").exists())
            telemetry.stop(api)
            self.assertTrue(all(not t.is_alive() for t in started[0]._threads))
            api.telemetry.close()
            telemetry._stores.clear()

    def test_existing_store_never_creates_files(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(spec=["config_path", "clock", "telemetry"])
            api.config_path = Path(directory) / "cluster.json"
            api.telemetry = None
            self.assertIsNone(telemetry.existing_store(api))
            self.assertFalse(any(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
