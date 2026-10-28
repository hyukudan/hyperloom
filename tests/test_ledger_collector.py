"""Token ledger collector (F11, C3/C8): engine counters -> hour records through the store's atomic baselines."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.portable_fixtures import read_fixture
from hyperloom.telemetry.store import Store
from hyperloom.web.api import API
from hyperloom.web.features import ledger


FIXTURES = Path(__file__).parent / "fixtures"
HOUR = 1_800_000_000 // 3600 * 3600


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_api(now=HOUR + 600.0):
    directory = tempfile.TemporaryDirectory()
    clock = Clock(now)
    api = API(Path(directory.name) / "cluster.json", orchestrator=mock.Mock(), clock=clock,
              audit_path=Path(directory.name) / "audit.jsonl")
    api.telemetry = Store(":memory:", clock=clock)
    api._directory = directory          # kept alive with the api
    return api, clock


def state(profile="qwen", family="qwen-replicas", replicas=(("halo-1", 18080),), **extra):
    return {"profile": profile, "nodes": [r[0] for r in replicas],
            "launch": {"family": family, "preset": profile + "-preset", "engine_sha": "a" * 40},
            "machines": {node: {"id": node, "http_address": node} for node, _ in replicas},
            "rendered_router": {"replicas": [{"id": node, "node": node, "address": node, "port": port}
                                             for node, port in replicas]}, **extra}


def metrics(generated, prompt, cached=None, imported=None):
    lines = [f"strix_generated_tokens_total {generated}", f"strix_prompt_tokens_total {prompt}"]
    if cached is not None:
        lines.append(f"strix_prompt_tokens_cached_total {cached}")
    if imported is not None:
        lines.append(f"strix_prompt_tokens_imported_total {imported}")
    return "\n".join(lines) + "\n"


class Engine:
    """Fake engine HTTP: /metrics text and /api/status, per port."""

    def __init__(self, clock):
        self.clock = clock
        self.text, self.status, self.down = {}, {}, set()

    def __call__(self, source, suffix, text=False, deadline=None):
        port = source["port"]
        if port in self.down:
            raise OSError("connection refused")
        if suffix == "/metrics":
            return self.text[port]
        if port not in self.status:
            raise OSError("404")
        status = dict(self.status[port])
        if "started" in status:        # uptime grows with the clock, as an engine's does
            status["uptime_seconds"] = self.clock.now - status.pop("started") + status.pop("jitter", 0.0)
        return status


def run(api, clock, engine, states, seconds=None):
    if seconds is not None:
        clock.now += seconds
    with mock.patch.object(ledger, "_states", lambda api: states), mock.patch.object(ledger, "_get", engine):
        return ledger.collect(api, clock.now)


def rows(api):
    return api.telemetry.execute("SELECT hour, model, machine, profile, kind, tokens, observed_s, uncertain_tokens "
                                 "FROM ledger ORDER BY hour, kind")


def kinds(api):
    out = {}
    for _, _, _, _, kind, tokens, _, _ in rows(api):
        out[kind] = out.get(kind, 0) + tokens
    return out


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.api, self.clock = make_api()
        self.addCleanup(self.api._directory.cleanup)
        self.engine = Engine(self.clock)
        self.states = [state()]
        self.engine.status[18080] = {"schema": "strix-serve-status-v1", "model": "qwen3.8", "started": self.clock.now - 100}

    def collect(self, seconds=None):
        return run(self.api, self.clock, self.engine, self.states, seconds)

    def test_first_sample_is_disclosed_and_later_deltas_are_counted(self):
        self.engine.text[18080] = metrics(100, 1000, 200)
        self.assertEqual(self.collect(), [])
        self.assertEqual(rows(self.api), [])
        gaps = self.api.telemetry.execute("SELECT reason, tokens, kind FROM ledger_gaps ORDER BY kind")
        self.assertEqual([g[0] for g in gaps], ["first observation of this engine counter"] * 3)
        self.assertEqual({g[2]: g[1] for g in gaps}, {"cached": 200.0, "generated": 100.0, "prompt": 1000.0})
        self.engine.text[18080] = metrics(150, 1600, 500)
        self.collect(10)
        self.assertEqual(kinds(self.api), {"generated": 50.0, "prompt": 600.0, "cached": 300.0, "imported": 0.0})
        hour, model, machine, profile, kind, tokens, observed, uncertain = rows(self.api)[0]
        self.assertEqual((hour, model, machine, profile, observed, uncertain), (HOUR, "qwen3.8", "halo-1", "qwen", 10.0, 0.0))
        points = self.api.telemetry.query("ledger_tokens", HOUR, self.clock.now, labels={"kind": "generated"}, tier="raw")
        self.assertEqual([p[5] for p in points["lines"][0]["points"]], [50.0])

    def test_restart_never_produces_negative_or_duplicated_work(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.text[18080] = metrics(160, 1300)
        self.collect(10)
        last = self.clock.now
        # The engine restarts: a new process, counters start again from small values.
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now + 2}
        self.engine.text[18080] = metrics(20, 90)
        self.collect(10)                  # 8 s old: identity not provable yet, nothing written
        self.engine.text[18080] = metrics(30, 120)
        self.collect(10)
        self.assertEqual(kinds(self.api), {"generated": 60.0, "prompt": 300.0, "cached": 0.0, "imported": 0.0})
        gaps = self.api.telemetry.execute("SELECT reason, tokens, start, end FROM ledger_gaps WHERE kind='generated' "
                                          "ORDER BY id")
        self.assertEqual(gaps[-1][:2], ("engine restarted (new instance)", 30.0))
        self.assertEqual((gaps[-1][2], gaps[-1][3]), (last, self.clock.now))
        self.engine.text[18080] = metrics(50, 190)
        self.collect(10)
        self.assertEqual(kinds(self.api), {"generated": 80.0, "prompt": 370.0, "cached": 0.0, "imported": 0.0})

    def test_counter_decrease_without_status_is_a_restart(self):
        self.engine.status.clear()
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.text[18080] = metrics(10, 50)
        self.collect(10)
        self.assertEqual(kinds(self.api), {"generated": 0.0, "prompt": 0.0, "cached": 0.0, "imported": 0.0})
        reason = self.api.telemetry.execute("SELECT reason FROM ledger_gaps WHERE kind='generated' ORDER BY id DESC LIMIT 1")
        self.assertEqual(reason[0][0], "counter decreased (engine restarted)")

    def test_uptime_jitter_keeps_the_instance(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now - 100, "jitter": 2.0}   # clock jitter
        self.engine.text[18080] = metrics(110, 1100)
        self.collect(10)
        self.assertEqual(kinds(self.api)["generated"], 10.0)

    def test_long_gap_counts_the_increase_as_uncertain(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.down.add(18080)
        self.collect(10)
        source = self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")
        self.assertIn("unavailable", source["error"])
        self.engine.down.clear()
        self.engine.text[18080] = metrics(400, 2000)
        self.collect(110)
        generated = [r for r in rows(self.api) if r[4] == "generated"][0]
        self.assertEqual((generated[5], generated[6], generated[7]), (300.0, 0.0, 300.0))

    def test_persisted_baseline_survives_a_portal_restart(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.api.telemetry._ledger_ready = False         # a new portal process on the same store
        self.engine.text[18080] = metrics(130, 1000)
        self.collect(10)
        self.assertEqual(kinds(self.api)["generated"], 30.0)

    def test_imported_counter_and_unreported_kinds(self):
        self.engine.text[18080] = metrics(0, 0, 0, 0)
        self.collect()
        self.engine.text[18080] = metrics(10, 1000, 100, 400)
        self.collect(10)
        self.assertEqual(kinds(self.api), {"generated": 10.0, "prompt": 1000.0, "cached": 100.0, "imported": 400.0})
        records, _ = ledger._records(self.api.telemetry, 0, self.clock.now)
        self.assertEqual(records[0]["computed"], 500.0)
        self.assertEqual(self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")["reported"],
                         ["cached", "generated", "imported", "prompt"])

    def fleet(self, ts, **changes):
        record = json.loads(read_fixture(FIXTURES / "ledger" / "fleet-engine.json"))
        record.update(ts=ts, **changes)
        self.api.telemetry.put(ledger.FLEET_ENGINE, "halo-1:mm:18090", record)
        return record

    def test_minimax_counters_from_the_fleet_engine_documents(self):
        mm = state("mm", "minimax-pair", (("halo-1", 18090),))
        self.api.node = mock.Mock(side_effect=AssertionError("the ledger never calls the node tool"))
        record = self.fleet(self.clock.now - 3)
        run(self.api, self.clock, self.engine, [mm])
        self.fleet(self.clock.now + 7, metrics_text=record["metrics_text"].replace("mm_generated_tokens 1536",
                                                                                   "mm_generated_tokens 1636"))
        run(self.api, self.clock, self.engine, [mm], 10)
        self.assertEqual(kinds(self.api), {"generated": 100.0, "prompt": 0.0, "cached": 0.0, "imported": 0.0})
        generated = [r for r in rows(self.api) if r[4] == "generated"][0]
        self.assertEqual(generated[6], 10.0)          # the fleet sample times are the counter times
        source = self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")
        self.assertEqual((source["model"], source["instance"], source["reported"]), ("mm-preset", "unknown", ["generated", "prompt"]))
        blind = self.api.telemetry.execute("SELECT MAX(blind_s) FROM ledger")[0][0]
        self.assertEqual(blind, 10.0)                 # no identity evidence: counted as unverified
        # The same fleet sample read again is not a new observation.
        run(self.api, self.clock, self.engine, [mm], 1)
        self.assertEqual(kinds(self.api)["generated"], 100.0)

    def test_fleet_bracketed_instance_is_the_identity(self):
        mm = state("mm", "minimax-pair", (("halo-1", 18090),))
        self.fleet(self.clock.now, instance="launch-1/start-100.0")
        run(self.api, self.clock, self.engine, [mm])
        self.assertEqual(self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")["instance"], "fleet:launch-1/start-100.0")

    def test_fleet_documents_of_another_engine_or_stale_are_refused(self):
        mm = state("mm", "minimax-pair", (("halo-1", 18090),))
        run(self.api, self.clock, self.engine, [mm])
        self.assertIn(ledger.LOOPBACK_NEEDS, self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")["error"])
        self.fleet(self.clock.now - 120)
        run(self.api, self.clock, self.engine, [mm], 1)
        self.assertIn("stale", self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")["error"])
        self.fleet(self.clock.now, profile="other")
        run(self.api, self.clock, self.engine, [mm], 1)
        self.assertIn("another engine", self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")["error"])
        self.fleet(self.clock.now, replica="halo-2")
        run(self.api, self.clock, self.engine, [mm], 1)
        self.assertIn("another replica", self.api.telemetry.get(ledger.SOURCES, "mm/halo-1")["error"])
        self.assertEqual(rows(self.api), [])

    def test_real_engine_fixture_is_read(self):
        self.engine.text[18080] = (FIXTURES / "adapters" / "engine-metrics.txt").read_text(encoding="utf-8")
        self.engine.status[18080] = json.loads((FIXTURES / "adapters" / "engine-status.json").read_text(encoding="utf-8"))
        self.collect()
        source = self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")
        self.assertEqual((source["model"], source["reported"], source["error"]), ("qwen3.8-flash-next", ["cached", "generated", "prompt"], None))

    def test_two_replicas_on_two_machines_are_separate_sources(self):
        self.states = [state(replicas=(("halo-1", 18080), ("halo-2", 18081)))]
        self.engine.status[18081] = {"model": "qwen3.8", "started": self.clock.now - 50}
        self.engine.text[18080] = metrics(0, 0)
        self.engine.text[18081] = metrics(0, 0)
        self.collect()
        self.engine.text[18080] = metrics(5, 50)
        self.engine.text[18081] = metrics(7, 70)
        self.collect(10)
        machines = {r[2]: r[5] for r in rows(self.api) if r[4] == "generated"}
        self.assertEqual(machines, {"halo-1": 5.0, "halo-2": 7.0})

    def test_failed_ledger_write_rolls_back_the_baseline(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.text[18080] = metrics(150, 1100)
        with mock.patch.object(ledger, "UPSERT", "INSERT INTO no_such_table VALUES (?, ?, ?, ?, ?, ?, ?, ?)"):
            with self.assertRaises(RuntimeError):
                self.collect(10)
        self.assertIn("no_such_table", self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")["error"])
        self.collect()        # same counters again: the baseline did not advance, so the work is counted once
        self.assertEqual(kinds(self.api)["generated"], 50.0)

    def test_reset_fences_the_interval_that_spans_it(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.text[18080] = metrics(150, 1100)
        self.collect(10)
        self.clock.now += 5
        ledger.reset(self.api, {"confirm": "reset"})
        self.assertEqual(rows(self.api), [])
        self.engine.text[18080] = metrics(170, 1150)
        self.collect(5)                          # interval [t, t + 10] straddles the reset at t + 5
        self.assertEqual(kinds(self.api), {})
        fenced = self.api.telemetry.execute("SELECT kind, tokens FROM ledger_gaps WHERE reason LIKE 'interval before%' "
                                            "ORDER BY kind")
        self.assertEqual(fenced, [("generated", 20.0), ("prompt", 50.0)])
        self.engine.text[18080] = metrics(180, 1160)
        self.collect(10)
        self.assertEqual(kinds(self.api)["generated"], 10.0)

    def test_a_scrape_taken_before_a_reset_commits_nothing_after_it(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.text[18080] = metrics(150, 1100)
        real = ledger.account_scrape

        def late(store, *args):
            # A reset commits after the sample was taken and before its transaction.
            self.clock.now += 1
            ledger.reset(self.api, {"confirm": "reset"})
            return real(store, *args)
        with mock.patch.object(ledger, "account_scrape", late):
            self.collect(10)
        self.assertEqual(rows(self.api), [])

    def test_identity_change_between_reads_refuses_the_scrape(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        calls = {"n": 0}
        real = self.engine.__call__

        def flaky(source, suffix, text=False, deadline=None):
            if suffix == "/api/status":
                calls["n"] += 1
                if calls["n"] == 2:     # the second status of the bracket comes from a new process
                    return {"model": "qwen3.8", "uptime_seconds": 1.0}
            return real(source, suffix, text, deadline)
        self.engine.text[18080] = metrics(5, 50)
        with mock.patch.object(ledger, "_get", flaky), mock.patch.object(ledger, "_states", lambda api: self.states):
            self.clock.now += 10
            ledger.collect(self.api, self.clock.now)
        self.assertIn("identity changed", self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")["error"])
        self.assertEqual(rows(self.api), [])

    def test_young_process_is_never_merged_with_its_successor(self):
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now - 12}
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        # The process stops right after; its successor starts 1 s later and has overtaken the old counter.
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now + 1}
        self.engine.text[18080] = metrics(130, 1200)
        self.collect(10)               # the successor is 9 s old: its identity cannot be proved yet, nothing counted
        self.assertIn("younger than", self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")["error"])
        self.engine.text[18080] = metrics(140, 1300)
        self.collect(10)
        self.assertEqual(kinds(self.api).get("generated", 0.0), 0.0)
        reasons = [r[0] for r in self.api.telemetry.execute("SELECT reason FROM ledger_gaps WHERE kind='generated' ORDER BY id")]
        self.assertEqual(reasons[-1], "engine restarted (new instance)")

    def test_counter_that_missed_a_scrape_is_counted_as_uncertain(self):
        self.engine.text[18080] = metrics(100, 1000, 10)
        self.collect()
        self.engine.text[18080] = metrics(110, 1100)          # cached missing in this scrape
        self.collect(10)
        self.engine.text[18080] = metrics(120, 1200, 40)
        self.collect(10)
        records, _ = ledger._records(self.api.telemetry, 0, self.clock.now)
        self.assertEqual((records[0]["cached"], records[0]["full"]["cached"], records[0]["computed_tag"]),
                         (30.0, False, "estimated"))
        reasons = [r[0] for r in self.api.telemetry.execute("SELECT reason FROM ledger_gaps WHERE kind='cached' ORDER BY id")]
        self.assertTrue(reasons[-1].startswith("counter missing at the previous scrape"))

    def test_invalid_replica_address_is_never_read(self):
        bad = state()
        bad["rendered_router"]["replicas"][0]["address"] = "elsewhere"
        run(self.api, self.clock, mock.Mock(side_effect=AssertionError("must not be read")), [bad])
        self.assertIn("differs from the launch record", self.api.telemetry.get(ledger.SOURCES, "qwen/halo-1")["error"])

    def test_unreachable_interval_is_opened_and_closed(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.down.add(18080)
        self.collect(10)
        self.collect(10)
        open_rows = self.api.telemetry.execute("SELECT since, until FROM ledger_unreachable")
        self.assertEqual(len(open_rows), 1)
        self.assertIsNone(open_rows[0][1])
        self.engine.down.clear()
        self.engine.text[18080] = metrics(120, 1100)
        self.collect(10)
        since, until = self.api.telemetry.execute("SELECT since, until FROM ledger_unreachable")[0]
        self.assertEqual(until - since, 30.0)

    def test_round_deadline_stops_starting_sources(self):
        self.states = [state(replicas=(("halo-1", 18080), ("halo-2", 18081)))]
        self.engine.text[18080] = self.engine.text[18081] = metrics(0, 0)
        self.engine.status[18081] = {"model": "qwen3.8", "started": self.clock.now - 50}
        times = iter([0.0, 0.0, 9.0])
        with mock.patch.object(ledger.time, "monotonic", lambda: next(times, 9.0)):
            self.collect()
        errors = [self.api.telemetry.get(ledger.SOURCES, k)["error"] for k in ("qwen/halo-1", "qwen/halo-2")]
        self.assertEqual(sum("round deadline" in (e or "") for e in errors), 1)

    def test_late_counter_is_never_subtracted(self):
        self.engine.text[18080] = metrics(100, 1000, 10)
        self.collect()
        self.engine.text[18080] = metrics(110, 1100)          # cached missing in this scrape
        self.collect(10)
        self.engine.text[18080] = metrics(120, 1200, 40)
        self.collect(10)
        records, _ = ledger._records(self.api.telemetry, 0, self.clock.now)
        r = records[0]
        self.assertEqual((r["aligned_cached"], r["late_cached"], r["aligned_prompt"]), (0.0, 30.0, 200.0))
        self.assertEqual(r["computed"], 200.0)

    def test_keyed_engine_without_its_key_is_not_read(self):
        source = ledger.sources_of(state())[0]
        fetch = mock.Mock(side_effect=AssertionError("no unkeyed request"))
        with mock.patch("hyperloom.secrets.get", return_value=None), mock.patch.object(ledger, "_fetch", fetch):
            observation, why = ledger.observe(self.api, self.api.telemetry, source, self.clock.now, None)
        self.assertIsNone(observation)
        self.assertIn("engine key unavailable", why)
        fetch.assert_not_called()

    def rate(self, now=None, current=("qwen/halo-1",)):
        return ledger.live_rate(self.api.telemetry, "qwen", self.clock.now if now is None else now, set(current))

    def test_live_rate_uses_valid_fresh_intervals_of_listed_sources(self):
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        for step in range(1, 8):
            self.engine.text[18080] = metrics(50 * step, 0)
            self.collect(10)
        self.assertEqual(self.rate(), (5.0, 1, None, 60.0))
        self.assertEqual(self.rate(current=())[2], "no listed engine for this profile")
        self.assertIn("no fresh counter interval", self.rate(now=self.clock.now + 60)[2])
        # A restart removes the intervals of the old epoch (in the scrape transaction).
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now - 20}
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)
        self.assertEqual(self.api.telemetry.execute("SELECT COUNT(*) FROM ledger_rates")[0][0], 0)
        self.assertIsNone(self.rate()[0])

    def test_live_rate_counts_only_intervals_inside_the_window(self):
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        self.engine.text[18080] = metrics(300, 0)
        self.collect(30)                       # one 30 s interval holding 300 tokens
        for step in range(1, 4):
            self.engine.text[18080] = metrics(300, 0)
            self.collect(10)
        # At t + 60 the 300-token interval started exactly at the window's start; 10 s later it lies outside it.
        rate, _, why, seconds = self.rate(now=self.clock.now)
        self.assertEqual((rate, seconds), (5.0, 60.0))
        rate, _, why, seconds = self.rate(now=self.clock.now + 5)
        self.assertEqual((rate, seconds), (0.0, 30.0))      # the 300 tokens began before the window: not counted

    def test_a_decrease_with_the_same_explicit_identity_clears_the_rate(self):
        self.engine.status[18080] = {"model": "qwen3.8", "instance_id": "run-7"}
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        for step in range(1, 8):
            self.engine.text[18080] = metrics(50 * step, 0)
            self.collect(10)
        self.assertEqual(self.rate()[0], 5.0)
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)
        self.assertEqual(self.api.telemetry.execute("SELECT COUNT(*) FROM ledger_rates")[0][0], 0)
        self.assertIsNone(self.rate()[0])

    def test_reset_removes_the_rate_entries_with_the_ledger(self):
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        for step in range(1, 8):
            self.engine.text[18080] = metrics(50 * step, 0)
            self.collect(10)
        ledger.reset(self.api, {"confirm": "reset"})
        self.assertEqual(self.api.telemetry.execute("SELECT COUNT(*) FROM ledger_rates")[0][0], 0)
        self.assertIsNone(self.rate()[0])

    def test_coverage_of_an_initially_unreachable_engine(self):
        self.engine.down.add(18080)
        self.collect()
        self.collect(30)
        self.collect(30)                       # unreadable for the first 60 s
        self.engine.down.clear()
        self.engine.text[18080] = metrics(0, 0)
        self.collect(0)
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertAlmostEqual(doc["coverage"]["observed_s"] / doc["coverage"]["expected_s"], 10 / 70)

    def test_an_outage_across_hours_is_split_exactly(self):
        self.clock.now = HOUR + 3500
        self.engine.status[18080] = {"model": "qwen3.8", "started": self.clock.now - 100}
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)                       # [HOUR + 3500, HOUR + 3510] observed
        self.engine.down.add(18080)
        self.collect(10)
        self.clock.now = HOUR + 2 * 3600 + 100
        self.collect()                         # still unreadable
        self.engine.down.clear()
        self.engine.text[18080] = metrics(20, 0)
        self.collect(10)                       # recovery: an uncertain interval [HOUR + 3510, HOUR + 7310]
        doc = ledger.ledger(self.api, {"range": "86400"})
        buckets = {b["start"]: b for b in doc["buckets"]}
        self.assertEqual(sorted(buckets), [HOUR, HOUR + 3600, HOUR + 7200])
        self.assertAlmostEqual(buckets[HOUR]["coverage"], 10 / 100)
        self.assertEqual(buckets[HOUR + 3600]["coverage"], 0.0)
        self.assertEqual(buckets[HOUR + 3600]["expected_s"], 3600.0)
        self.assertEqual(buckets[HOUR + 7200]["expected_s"], 110.0)

    def test_time_skipped_by_the_round_deadline_is_still_expected(self):
        times = iter([0.0, 9.0])                 # the round starts past its deadline: the engine is not read
        with mock.patch.object(ledger.time, "monotonic", lambda: next(times, 9.0)):
            self.collect()
            for _ in range(6):
                times = iter([0.0, 9.0])
                self.collect(10)
        self.engine.text[18080] = metrics(0, 0)
        self.collect(0)
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertAlmostEqual(doc["coverage"]["observed_s"] / doc["coverage"]["expected_s"], 10 / 70)

    def test_time_after_a_reset_in_a_fenced_interval_is_expected(self):
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        self.engine.text[18080] = metrics(10, 0)
        self.collect(10)
        self.clock.now += 5
        ledger.reset(self.api, {"confirm": "reset"})
        self.engine.text[18080] = metrics(15, 0)
        self.collect(5)                          # fenced: 5 s after the reset, not observed
        self.engine.text[18080] = metrics(25, 0)
        self.collect(10)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertAlmostEqual(doc["coverage"]["observed_s"] / doc["coverage"]["expected_s"], 10 / 15)

    def test_a_missing_prompt_counter_is_never_measured_or_exact(self):
        self.engine.text[18080] = metrics(0, 0)
        self.collect()
        self.engine.text[18080] = "strix_generated_tokens_total 1000000\n"      # prompt counter missing
        self.collect(10)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertTrue(doc["kpis"]["input_tag"].startswith("partial"))
        self.assertEqual(doc["kpis"]["computed_tag"], "partly estimated")
        self.assertIsNone(doc["kpis"]["output_input"])
        self.api.telemetry.put(ledger.PREFS, "prices", {"currency": "EUR", "default": {"input": 1.0, "cached": 0.1, "output": 1.0}, "models": {}})
        value = ledger.value(self.api, {"range": "3600"})
        self.assertFalse(value["models"][0]["exact"])
        self.assertIn("input not reported for part of the range (input tokens are a lower bound)", value["models"][0]["qualifiers"])
        self.assertFalse(value["totals"]["exact"])

    def test_stopped_engine_closes_its_unreachable_interval(self):
        self.engine.text[18080] = metrics(100, 1000)
        self.collect()
        self.engine.down.add(18080)
        self.collect(10)
        stopped_at = self.clock.now
        self.states = []
        self.collect(10)
        until, closed_by = self.api.telemetry.execute("SELECT until, closed_by FROM ledger_unreachable")[0]
        self.assertEqual((until, closed_by), (stopped_at, "removed"))


class TextRuleTests(unittest.TestCase):
    """The package's code, fixtures and tests name no address, home path, machine or person of the lab."""

    def test_no_address_literals_or_identities(self):
        import base64
        import re
        root = Path(__file__).parent.parent
        files = [root / "hyperloom" / "web" / "features" / "ledger.py", root / "hyperloom" / "cliext" / "ledger.py",
                 *sorted((root / "tests").glob("test_ledger_*.py")), *sorted((root / "tests" / "fixtures" / "ledger").iterdir()),
                 *sorted((root / "hyperloom" / "web" / "static" / "pages").glob("*.*")), root / "tests" / "ui" / "seeds" / "ledger.py"]
        own = [f for f in files if f.exists() and (f.parent.name != "pages" or f.stem in ("tokens", "ceiling", "rtx", "value"))]
        # Encoded so this test itself stays clean: lab host names, user names, review and agent identities.
        words = [base64.b64decode(w).decode() for w in ("Y2x1c3RlcjE=", "Y2x1c3RlcjI=", "VXN1YXJpbw==", "Y2xhdWRl", "Y29kZXg=",
                                                         "c29s", "aHl1a3VkYW4=", "YW50aHJvcGlj")]
        address = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
        for path in own:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(address.search(text), f"address literal in {path.name}")
            home = re.compile(r"(?i)[a-z]:[\\/]" + "users" + r"[\\/]|/" + "home/")   # split so this file stays clean
            self.assertIsNone(home.search(text), f"home path in {path.name}")
            for word in words:
                self.assertIsNone(re.search(rf"(?i)\b{word}\b", text), f"identity {word[:2]}.. in {path.name}")


class FetchTests(unittest.TestCase):
    """The HTTP helper: no redirects, a key only as a header, a total deadline."""

    def serve(self, handler):
        import http.server
        import threading
        server = http.server.ThreadingHTTPServer((ledger.LOOPBACK, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_redirects_are_refused_and_the_key_is_a_header(self):
        import http.server
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/moved":
                    self.send_response(302)
                    self.send_header("Location", "/metrics")
                    self.end_headers()
                    return
                body = b"strix_generated_tokens_total 1\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        port = self.serve(Handler)
        self.assertEqual(ledger._fetch(ledger.LOOPBACK, port, "/metrics", text=True, key="k" * 64),
                         "strix_generated_tokens_total 1\n")
        self.assertEqual(seen[-1], ("/metrics", "Bearer " + "k" * 64))
        with self.assertRaises(ledger.FetchError):
            ledger._fetch(ledger.LOOPBACK, port, "/moved", text=True)
        self.assertEqual([p for p, _ in seen], ["/metrics", "/moved"])

    def test_a_slow_body_is_cut_at_the_deadline(self):
        import http.server
        import time as _time

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(100):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except OSError:
                        return
                    _time.sleep(0.05)
        port = self.serve(Handler)
        started = _time.monotonic()
        with self.assertRaises((TimeoutError, OSError)):
            ledger._fetch(ledger.LOOPBACK, port, "/metrics", text=True, deadline=_time.monotonic() + 0.5)
        self.assertLess(_time.monotonic() - started, 1.5)

    def test_trickled_headers_are_cut_at_the_deadline(self):
        import socketserver
        import time as _time

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.recv(4096)
                    for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"y" * 200:
                        self.request.sendall(bytes([byte]))
                        _time.sleep(0.02)
                except OSError:
                    pass
        server = socketserver.ThreadingTCPServer((ledger.LOOPBACK, 0), Handler)
        server.daemon_threads = True
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        started = _time.monotonic()
        with self.assertRaises((TimeoutError, OSError)):
            ledger._fetch(ledger.LOOPBACK, server.server_address[1], "/metrics", deadline=_time.monotonic() + 0.3)
        self.assertLess(_time.monotonic() - started, 0.8)

    def test_chunked_bodies_are_decoded(self):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for part in (b'{"a": ', b'1}'):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.write(b"0\r\n\r\n")
        self.assertEqual(ledger._fetch(ledger.LOOPBACK, self.serve(Handler), "/api/status"), {"a": 1})

    def test_key_comes_from_the_secret_store_for_keyed_engines_only(self):
        source = ledger.sources_of(state())[0]
        with mock.patch("hyperloom.secrets.get", return_value="s" * 64) as get:
            self.assertEqual(ledger._key(source), "s" * 64)
        get.assert_called_once()
        mm = ledger.sources_of(state("mm", "minimax-pair"))[0]
        self.assertIsNone(ledger._key(mm))

    def test_a_key_file_release_is_read_over_the_lan_with_its_key(self):
        # The launched release decides the bind: a serve_auth "key-file" preset binds the LAN and is read with its key.
        from hyperloom import engines
        real = engines.load_preset
        keyed = lambda name: (dict(real(name), serve_auth="key-file",
                                   argv=real(name)["argv"] + ["--api-key-file", "{key_file}"])
                              if name == "minimax-pair" else real(name))
        record = state("mm", "minimax-pair")
        record["launch"]["preset"] = "minimax-pair"
        self.assertEqual((ledger.sources_of(record)[0]["bind"], ledger.sources_of(record)[0]["keyed"]), ("loopback", False))
        with mock.patch.object(engines, "load_preset", side_effect=keyed):
            source = ledger.sources_of(record)[0]
        self.assertEqual((source["bind"], source["keyed"], source["invalid"]), ("lan", True, None))
        with mock.patch("hyperloom.secrets.get", return_value="s" * 64):
            self.assertEqual(ledger._key(source), "s" * 64)


if __name__ == "__main__":
    unittest.main()
