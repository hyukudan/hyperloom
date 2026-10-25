"""Energy estimate (F18) and thermals (F19): integration, domains, archive, reset, pairing, zones, bounds, writes."""

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

from hyperloom import telemetry
from hyperloom.telemetry.store import Store
from hyperloom.web import features, server
from hyperloom.web.api import API, APIError, redact
from hyperloom.web.features import energy


HOUR, QUARTER = 3600, 900
GPU = "node_gpu_power_watts"
CPU = "node_cpu_power_watts"
TOKENS = "engine_generated_tokens"


class Clock:
    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now


class FakeAPI:
    """The parts of the portal API the feature uses."""

    def __init__(self, store, clock, machines=None):
        self.telemetry, self.clock = store, clock
        self.machines = machines
        self.config_path = None

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", "server_time": self.clock(), "why": {}, **fields}

    def cluster(self):
        if self.machines is None:
            raise RuntimeError("no configuration")
        return types.SimpleNamespace(machines={m: types.SimpleNamespace(id=m, os=o) for m, o in self.machines})


# 2027-01-15 10:00:00 UTC, an exact hour.
NOW = datetime(2027, 1, 15, 10, 0, tzinfo=timezone.utc).timestamp()


def feed(store, points, start, end, step=60, interval=None):
    """Record points(t) -> [(series, labels, value[, kind])] every `step` seconds in [start, end)."""
    t = start
    while t < end:
        store.record(points(t), ts=t, interval=interval or step)
        t += step


def energy_rows(store):
    return store.execute("SELECT q, machine, domain, wh, observed_s, counted FROM energy_quarters ORDER BY q, machine, domain")


def archived(store):
    return sum(row[0] for row in store.execute("SELECT wh FROM energy_monthly"))


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(NOW)
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.api = FakeAPI(self.store, self.clock)
        energy._thermal_cache.clear()
        # Stand for earlier collector runs that saw one sampling cadence throughout (cadence tests clear this).
        energy._mark_verified(self.store, NOW - 60 * 86400, NOW + 60 * 86400)

    def doc(self, **query):
        return energy.energy_document(self.api, query)


class IntegrationTests(Base):
    def test_constant_power_integrates_exactly_and_gaps_lower_coverage_not_rate(self):
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 3 * HOUR, NOW - 2 * HOUR)
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 1.5 * HOUR, NOW)          # 30 min offline
        energy.compute(self.store, NOW)
        d = self.doc()
        machine = d["machines"][0]
        self.assertAlmostEqual(machine["wh"], 250.0)                                   # 2.5 h x 100 W, no fill-in
        self.assertAlmostEqual(machine["avg_w"], 100.0)
        self.assertAlmostEqual(machine["observed_s"], 2.5 * HOUR)
        self.assertAlmostEqual(machine["coverage"], 2.5 / 23)          # 23 elapsed hours of the 24-hour window
        self.assertAlmostEqual(d["totals"]["wh"], 250.0)
        hours = {b["start"]: b for b in d["buckets"]}
        self.assertAlmostEqual(hours[NOW - 2 * HOUR]["machines"]["halo-1"], 50.0)     # half an hour sampled
        self.assertAlmostEqual(hours[NOW - 2 * HOUR]["coverage"], 0.5)
        self.assertEqual(hours[NOW - 5 * HOUR]["machines"], {})                        # no samples: no value, not 0

    def test_samples_never_stand_for_more_than_their_interval(self):
        # One sample every 5 minutes taken at a 60 s interval: each covers one minute only.
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 120.0)], NOW - HOUR, NOW,
             step=300, interval=60)
        energy.compute(self.store, NOW)
        machine = self.doc()["machines"][0]
        self.assertAlmostEqual(machine["wh"], 12 * 120.0 * 60 / HOUR)
        self.assertAlmostEqual(machine["avg_w"], 120.0)

    def test_uneven_sample_counts_at_one_cadence_are_kept(self):
        gpu = {"machine": "halo-1", "device": "0"}
        stamps = [NOW - 600 + i * 5 + (0.4 if i % 7 == 0 else 0) for i in range(120)]   # jittered 5 s samples
        for t in stamps:
            self.store.record([(GPU, gpu, 200.0)], ts=t, interval=5)
        energy.compute(self.store, NOW)
        machine = self.doc()["machines"][0]
        self.assertAlmostEqual(machine["observed_s"], 600.0)
        self.assertAlmostEqual(machine["wh"], 200.0 * 600 / HOUR)

    def test_history_beyond_the_minute_tier_uses_quarters(self):
        self.clock.now = NOW
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 90.0)], NOW - 72 * HOUR, NOW - 70 * HOUR)
        energy.compute(self.store, NOW)
        d = self.doc(range="7d")
        self.assertAlmostEqual(d["totals"]["wh"], 180.0)
        self.assertTrue(all(row[4] == QUARTER for row in energy_rows(self.store)))


class CadenceTests(Base):
    def setUp(self):
        super().setUp()
        self.store.execute("DELETE FROM energy_verified")

    def test_only_time_between_runs_with_one_cadence_is_integrated(self):
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 4 * HOUR, NOW - 2.5 * HOUR, step=10)
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 2.5 * HOUR, NOW, step=2)
        for t, cadence in ((NOW - 4 * HOUR, {"fleet": 10.0}), (NOW - 3 * HOUR, {"fleet": 10.0}),
                           (NOW - 2 * HOUR, {"fleet": 2.0}), (NOW - HOUR, {"fleet": 2.0}), (NOW, {"fleet": 2.0})):
            energy.compute(self.store, t, cadence)
        # Verified: [-4 h, -3 h] and [-2 h, now]; the hour in which the cadence changed is not integrated.
        machine = self.doc()["machines"][0]
        self.assertAlmostEqual(machine["observed_s"], 3 * HOUR)
        self.assertAlmostEqual(machine["wh"], 300.0)
        self.assertEqual(self.store.execute("SELECT start, end FROM energy_verified ORDER BY start"),
                         [(NOW - 4 * HOUR, NOW - 3 * HOUR), (NOW - 2 * HOUR, NOW)])

    def test_a_cadence_changed_and_restored_between_two_runs_is_not_integrated(self):
        gpu = {"machine": "halo-1", "device": "0"}
        t = NOW - HOUR
        while t < NOW:
            changed = NOW - 40 * 60 <= t < NOW - 30 * 60 + 30        # 10 s cadence for a while, then back to 2 s
            self.store.record([(GPU, gpu, 200.0 if changed else 100.0)], ts=t, interval=10 if changed else 2)
            t += 10 if changed else 2
        energy.compute(self.store, NOW - HOUR, {"fleet": 2.0})
        energy.compute(self.store, NOW, {"fleet": 2.0})                     # both runs saw 2 s
        machine = self.doc()["machines"][0]
        # Only minutes sampled entirely at 2 s count: the 10 s minutes and the mixed one are unobserved.
        self.assertAlmostEqual(machine["observed_s"], HOUR - 11 * 60)
        self.assertAlmostEqual(machine["wh"], 100.0 * (HOUR - 11 * 60) / HOUR)

    def test_history_before_the_first_run_and_runs_without_a_sampler_are_not_integrated(self):
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 2 * HOUR, NOW)
        summary = energy.compute(self.store, NOW - HOUR, None)                      # a CLI run: vouches for nothing
        self.assertIsNone(summary["verified_until"])
        energy.compute(self.store, NOW - HOUR, {"fleet": 60.0})
        energy.compute(self.store, NOW, {"fleet": 60.0})
        self.assertAlmostEqual(self.doc()["machines"][0]["wh"], 100.0)               # only the last hour
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM energy_monthly")[0][0], 1)


class DomainTests(Base):
    def test_overlap_disjoint_cards_duplicates_and_uncovered(self):
        self.api.machines = [("workstation", "windows"), ("halo-1", "linux"), ("halo-2", "linux")]
        points = lambda t: [
            (GPU, {"machine": "workstation", "device": "0"}, 300.0),
            (GPU, {"machine": "workstation", "device": "1"}, 150.0),
            (GPU, {"machine": "halo-1", "device": "0"}, 100.0),
            (CPU, {"machine": "halo-1"}, 40.0),
            (GPU, {"machine": "halo-2", "device": "0"}, 100.0),
            (GPU, {"machine": "halo-2", "device": "0", "sensor": "input"}, 101.0),
            (CPU, {"machine": "halo-2", "disjoint": "1"}, 20.0),
        ]
        feed(self.store, points, NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        d = self.doc()
        by = {m["id"]: m for m in d["machines"]}
        self.assertEqual([m["id"] for m in d["machines"]], ["workstation", "halo-1", "halo-2"])
        self.assertAlmostEqual(by["workstation"]["wh"], 450.0)                     # both cards added
        self.assertAlmostEqual(by["halo-1"]["wh"], 100.0)                          # CPU overlaps the package
        cpu = next(x for x in by["halo-1"]["domains"] if x["domain"] == "cpu")
        self.assertFalse(cpu["counted"])
        self.assertIn("overlaps", cpu["overlap"])
        self.assertAlmostEqual(cpu["wh"], 40.0)                                    # shown, not added
        self.assertAlmostEqual(by["halo-2"]["wh"], 120.0)                          # declared disjoint; duplicate not added
        duplicate = next(x for x in by["halo-2"]["domains"] if "#" in x["domain"])
        self.assertFalse(duplicate["counted"])
        self.assertAlmostEqual(d["totals"]["wh"], 670.0)
        self.assertIn(energy.CONTROL_HOST, by["workstation"]["uncovered"])
        self.assertEqual(by["halo-1"]["uncovered"], energy.UNCOVERED)
        self.assertEqual(by["halo-2"]["domains"][0]["source"], "GPU package power sensor")
        self.assertEqual(by["workstation"]["domains"][0]["source"], "GPU board power sensor")
        self.assertEqual(d["banner"], "Estimated from sensors, not metered")

    def test_overlap_rule_does_not_depend_on_the_window(self):
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - 70 * HOUR, NOW - 69 * HOUR)
        feed(self.store, lambda t: [(CPU, {"machine": "halo-1"}, 30.0)], NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        cpu = [r for r in energy_rows(self.store) if r[2] == "cpu"]
        self.assertTrue(cpu and all(r[5] == 0 for r in cpu))

    def test_missing_domain_keeps_average_unavailable_and_lowers_coverage(self):
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0),
                                    (GPU, {"machine": "halo-1", "device": "1"}, 50.0)], NOW - 30 * HOUR, NOW - 29 * HOUR)
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        machine = self.doc()["machines"][0]
        self.assertIsNone(machine["avg_w"])
        self.assertIn("gpu:1", machine["why"]["avg_w"])
        self.assertAlmostEqual(machine["coverage"], HOUR / (2 * 23 * HOUR))
        d7 = self.doc(range="7d")["machines"][0]
        self.assertAlmostEqual(d7["avg_w"], 150.0)                                  # each domain over its own time


class ArchiveTests(Base):
    def test_final_quarters_are_archived_once(self):
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW - 2 * HOUR, NOW)
        energy.compute(self.store, NOW)
        energy.compute(self.store, NOW)
        # Final: quarters ending at least 15 minutes ago (everything up to NOW - 15 min).
        self.assertAlmostEqual(archived(self.store), 100.0 * 1.75)
        self.clock.now = NOW + HOUR
        feed(self.store, lambda t: [(GPU, gpu, 100.0)], NOW, NOW + HOUR)
        energy.compute(self.store, self.clock.now)
        energy.compute(self.store, self.clock.now)
        self.assertAlmostEqual(archived(self.store), 100.0 * 2.75)
        month = self.doc()["archive"][0]
        self.assertTrue(month["open"])
        self.assertAlmostEqual(month["wh"], 300.0)                                  # archived + open quarters

    def test_a_stale_run_from_another_process_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.sqlite3"
            first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
            try:
                feed(first, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - 2 * HOUR, NOW)
                energy._mark_verified(first, NOW - 86400, NOW + 86400)
                stale, summary = energy.plan(first, NOW)
                self.assertTrue(energy.compute(second, NOW)["applied"])
                before = (archived(first), energy_rows(first), energy._state(first))
                first.transaction(stale + [energy._bump(summary["revision"], 12345.0)])
                self.assertFalse(energy._applied(first, 12345.0))                     # the stale run knows it lost
                self.assertEqual((archived(first), energy_rows(first), energy._state(first)), before)
                self.assertAlmostEqual(archived(first), 175.0)
                # A stale reset is refused and changes nothing either.
                with mock.patch.object(energy, "plan", lambda store, now, cadence=None, finalize_all=False: (stale, summary)):
                    with self.assertRaises(PermissionError):
                        energy.reset_store(first, NOW)
                self.assertEqual((archived(first), energy_rows(first), energy._state(first)), before)
            finally:
                first.close()
                second.close()

    def test_currency_change_makes_the_month_cost_unavailable(self):
        gpu = {"machine": "halo-1", "device": "0"}
        self.store.put("prefs", "energy/price", {"price_per_kwh": 0.2, "currency": "EUR"})
        feed(self.store, lambda t: [(GPU, gpu, 1000.0)], NOW - 3 * HOUR, NOW - 2 * HOUR)
        energy.compute(self.store, NOW - 1.5 * HOUR)
        month = self.doc()["archive"][0]
        self.assertAlmostEqual(month["cost"], 0.2)
        self.assertEqual(month["currency"], "EUR")
        self.store.put("prefs", "energy/price", {"price_per_kwh": 0.3, "currency": "USD"})
        feed(self.store, lambda t: [(GPU, gpu, 1000.0)], NOW - 2 * HOUR, NOW - HOUR)
        energy.compute(self.store, NOW)
        month = self.doc()["archive"][0]
        self.assertIsNone(month["cost"])
        self.assertIn("currency changed", month["cost_why"])


class InventoryTests(Base):
    def test_known_domains_survive_a_reset_and_missing_energy_is_unavailable(self):
        self.api.machines = [("halo-1", "linux"), ("halo-2", "linux")]
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0),
                                    (GPU, {"machine": "halo-2", "device": "0"}, 100.0)], NOW - 2 * HOUR, NOW - HOUR)
        energy.compute(self.store, NOW - HOUR)
        self.clock.now = NOW - HOUR
        energy.reset_store(self.store, self.clock.now)
        self.clock.now = NOW
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - HOUR + 60, NOW)
        energy.compute(self.store, NOW)
        d = self.doc()
        by = {m["id"]: m for m in d["machines"]}
        self.assertIsNone(by["halo-2"]["wh"])                                        # unavailable, never 0
        self.assertEqual(by["halo-2"]["why"]["wh"], energy.NO_SAMPLES)
        self.assertIsNone(by["halo-2"]["domains"][0]["wh"])
        self.assertEqual(by["halo-2"]["coverage"], 0.0)                              # still expected after reset
        self.assertAlmostEqual(d["totals"]["coverage"], 0.5)
        self.assertEqual(d["totals"]["partial"], ["halo-2"])
        self.assertIsNone(d["totals"]["avg_w"])
        self.assertIn("partial", d["why"]["totals"])

    def test_a_configured_machine_without_readings_is_listed_unavailable(self):
        self.api.machines = [("workstation", "windows"), ("halo-1", "linux")]
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        d = self.doc()
        by = {m["id"]: m for m in d["machines"]}
        self.assertIsNone(by["workstation"]["wh"])
        self.assertIn("no power reading", by["workstation"]["why"]["wh"])
        self.assertEqual(d["totals"]["partial"], ["workstation"])                 # the lab total is partial
        self.assertIsNone(d["totals"]["avg_w"])
        self.assertIn("workstation", d["why"]["totals"])
        self.assertIn(energy.CONTROL_HOST, by["workstation"]["uncovered"])

    def test_documents_are_built_from_one_revision(self):
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        real = energy._energy_fields
        calls = []

        def racing(api, store, zone, name):
            calls.append(1)
            if len(calls) == 1:
                store.execute("UPDATE energy_state SET value=value+1 WHERE key='revision'")
            return real(api, store, zone, name)
        with mock.patch.object(energy, "_energy_fields", racing):
            self.assertAlmostEqual(self.doc()["totals"]["wh"], 100.0)
        self.assertEqual(len(calls), 2)

        def always(api, store, zone, name):
            store.execute("UPDATE energy_state SET value=value+1 WHERE key='revision'")
            return real(api, store, zone, name)
        with mock.patch.object(energy, "_energy_fields", always):
            d = self.doc()
        self.assertIsNone(d["machines"])
        self.assertIn("changed while being read", d["why"]["machines"])


class OutcomeTests(Base):
    def test_the_outcome_survives_a_later_commit(self):
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - HOUR, NOW)
        real = energy._applied

        done = []

        def later_commit(store, token):
            if not done:
                done.append(1)
                energy.compute(store, NOW)              # another run commits before this one looks at its outcome
            return real(store, token)
        with mock.patch.object(energy, "_applied", later_commit):
            result = energy.reset_store(self.store, NOW)
        self.assertEqual(result["reset_at"], NOW)
        self.assertEqual(self.store.execute("SELECT COUNT(*) FROM energy_receipts")[0][0], 0)

    def test_archive_reads_are_consistent(self):
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], NOW - 2 * HOUR, NOW)
        energy.compute(self.store, NOW)
        real = energy._months
        calls = []

        def racing(store, before=None, limit=energy.ARCHIVE_MONTHS):
            calls.append(1)
            if len(calls) == 1:
                store.execute("UPDATE energy_state SET value=value+1 WHERE key='revision'")
            return real(store, before, limit)
        with mock.patch.object(energy, "_months", racing):
            self.assertEqual(len(energy.archive_document(self.api, {})["months"]), 1)
        self.assertEqual(len(calls), 2)


class ResetTests(Base):
    def test_reset_keeps_the_archive_and_counts_nothing_twice(self):
        gpu = {"machine": "halo-1", "device": "0"}
        end = NOW + HOUR
        feed(self.store, lambda t: [(GPU, gpu, 360.0)], NOW - HOUR, end)            # 360 W = 0.1 Wh per second
        self.clock.now = NOW - 20 * 60                                               # an exact minute boundary
        energy.compute(self.store, self.clock.now)
        result = energy.reset_store(self.store, self.clock.now)
        self.assertGreater(result["archived_quarters"], 0)
        # 40 minutes before the reset plus the minute unit that starts at the reset instant (6 Wh per minute).
        self.assertAlmostEqual(archived(self.store), 41 * 6.0)
        self.assertEqual(energy_rows(self.store), [])
        self.clock.now = end + HOUR
        energy.compute(self.store, self.clock.now)
        # Everything once, except the minute that held the reset (archived only up to the reset itself).
        total = archived(self.store)
        self.assertAlmostEqual(total, 120 * 6.0)
        self.assertLessEqual(total, 120 * 6.0 + 1e-6)
        d = self.doc()
        self.assertEqual(d["reset_at"], NOW - 20 * 60)
        self.assertEqual(d["totals"]["since"], NOW - 20 * 60 + 60)

    def test_repeated_resets_within_one_quarter(self):
        gpu = {"machine": "halo-1", "device": "0"}
        feed(self.store, lambda t: [(GPU, gpu, 360.0)], NOW - HOUR, NOW + HOUR)
        for offset in (-10 * 60, -7 * 60, -2 * 60):
            self.clock.now = NOW + offset
            energy.reset_store(self.store, self.clock.now)
        self.clock.now = NOW + 2 * HOUR
        energy.compute(self.store, self.clock.now)
        self.assertLessEqual(archived(self.store), 120 * 6.0 + 1e-6)
        self.assertGreaterEqual(archived(self.store), 120 * 6.0 - 3 * 6.0 - 1e-6)

    def test_reset_requires_the_exact_confirmation(self):
        for arguments in ({}, {"confirm": "yes"}, {"confirm": "reset", "x": 1}, {"confirm": True}):
            with self.assertRaises(ValueError, msg=arguments):
                energy.reset(self.api, arguments)


class PairingTests(Base):
    def power_and_tokens(self, start, end, token_gap=None, tokens=True, power=True, instance=lambda t: "a"):
        def points(t):
            out = []
            if power:
                out += [(GPU, {"machine": "halo-1", "device": "0"}, 100.0), (GPU, {"machine": "halo-2", "device": "0"}, 100.0)]
            if tokens and not (token_gap and token_gap[0] <= t < token_gap[1]):
                out.append((TOKENS, {"profile": "p", "replica": "0", "model": "m", "instance": instance(t)}, 600.0, "delta"))
            return out
        feed(self.store, points, start, end)

    def test_ratio_over_fully_covered_minutes(self):
        self.power_and_tokens(NOW - HOUR, NOW)
        energy.compute(self.store, NOW)
        eff = self.doc()["efficiency"]
        self.assertAlmostEqual(eff["both_s"], HOUR)
        self.assertAlmostEqual(eff["wh_per_1k_tokens"], 200.0 / 36000 * 1000)         # 200 Wh, 10 tok/s
        self.assertEqual(redact(eff), eff)

    def test_minutes_missing_tokens_are_excluded_from_both_sides(self):
        self.power_and_tokens(NOW - HOUR, NOW, token_gap=(NOW - 40 * 60, NOW - 30 * 60))
        energy.compute(self.store, NOW)
        eff = self.doc()["efficiency"]
        self.assertAlmostEqual(eff["both_s"], 50 * 60)
        self.assertAlmostEqual(eff["wh"], 200.0 * 50 / 60)
        self.assertAlmostEqual(eff["generated_tokens"], 50 * 600)

    def test_a_machine_offline_blocks_pairing(self):
        self.power_and_tokens(NOW - 3 * HOUR, NOW - 2 * HOUR)
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0),
                                    (TOKENS, {"profile": "p", "replica": "0", "model": "m", "instance": "a"}, 600.0, "delta")],
             NOW - HOUR, NOW)                                                          # halo-2 silent
        energy.compute(self.store, NOW)
        eff = self.doc()["efficiency"]
        self.assertAlmostEqual(eff["both_s"], HOUR)                                    # only the first hour

    def test_tokens_without_power_and_power_without_tokens(self):
        self.power_and_tokens(NOW - HOUR, NOW, power=False)
        energy.compute(self.store, NOW)
        d = self.doc()
        self.assertIsNone(d["efficiency"])
        self.assertIn("completely observed", d["why"]["efficiency"])

    def test_an_engine_restart_is_the_same_observer(self):
        self.power_and_tokens(NOW - HOUR, NOW, instance=lambda t: "a" if t < NOW - 30 * 60 else "b")
        energy.compute(self.store, NOW)
        self.assertAlmostEqual(self.doc()["efficiency"]["both_s"], HOUR)


class ZoneTests(Base):
    def test_days_follow_a_fractional_offset_zone_exactly(self):
        self.store.put("prefs/ui", "timezone", "Asia/Kolkata")                       # UTC+05:30
        start = datetime(2027, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() - 5.5 * HOUR   # local midnight
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], start, NOW, step=QUARTER, interval=60)
        # One 60 s sample per quarter: 100 W x 60 s = 1.6667 Wh per quarter, 96 quarters per local day.
        energy.compute(self.store, NOW)
        d = self.doc(range="7d")
        days = {b["label"]: b for b in d["buckets"]}
        self.assertAlmostEqual(days["2027-01-13"]["machines"]["halo-1"], 96 * 100 * 60 / HOUR)
        self.assertEqual(d["timezone"], {"name": "Asia/Kolkata", "source": "configured", "offset": "UTC+05:30"})
        local_midnight = datetime(2027, 1, 13, 0, 0, tzinfo=energy.ZoneInfo("Asia/Kolkata")).timestamp()
        self.assertEqual(days["2027-01-13"]["start"], local_midnight)

    def test_a_daylight_saving_day_has_its_real_length(self):
        zone = energy.ZoneInfo("Europe/Madrid")
        self.store.put("prefs/ui", "timezone", "Europe/Madrid")
        now = datetime(2026, 10, 27, 12, 0, tzinfo=zone).timestamp()
        self.clock.now = now
        energy._mark_verified(self.store, now - 10 * 86400, now)
        start = datetime(2026, 10, 24, 0, 0, tzinfo=zone).timestamp()
        feed(self.store, lambda t: [(GPU, {"machine": "halo-1", "device": "0"}, 100.0)], start, now, step=QUARTER, interval=60)
        energy.compute(self.store, now)
        days = {b["label"]: b for b in self.doc(range="7d")["buckets"]}
        self.assertAlmostEqual(days["2026-10-25"]["machines"]["halo-1"], 100 * 100 * 60 / HOUR)   # 25 h
        self.assertAlmostEqual(days["2026-10-26"]["machines"]["halo-1"], 96 * 100 * 60 / HOUR)
        self.assertEqual(len(self.doc()["buckets"]), 24)

    def test_hours_keep_their_real_length_across_a_half_hour_clock_change(self):
        zone = energy.Zone(energy.ZoneInfo("Australia/Lord_Howe"), "Australia/Lord_Howe", "configured")
        now = datetime(2026, 10, 4, 6, 10, tzinfo=energy.ZoneInfo("Australia/Lord_Howe")).timestamp()
        hours = energy._hours(zone, now)
        self.assertEqual(len(hours), 24)
        for (a, b, _), (c, _d, _l) in zip(hours, hours[1:]):
            self.assertEqual(b, c)                                                   # contiguous
        lengths = {label.split()[0]: end - start for start, end, label in hours}
        self.assertEqual(lengths["02:30"], 1800)                                     # 02:00 jumped to 02:30
        self.assertTrue(all(v == HOUR for k, v in lengths.items() if k != "02:30"))

    def test_an_unknown_zone_falls_back_and_says_so(self):
        self.store.put("prefs/ui", "timezone", "Nowhere/Invalid")
        zone = energy.zone_of(self.store)
        self.assertEqual(zone.source, "control machine")
        self.assertIn("unknown", zone.why)


class BoundsTests(Base):
    def test_too_many_lines_is_a_collector_error(self):
        feed(self.store, lambda t: [(GPU, {"machine": f"m{i:02d}", "device": "0"}, 10.0) for i in range(70)],
             NOW - 600, NOW)
        with self.assertRaises(ValueError):
            energy.collect(self.api, NOW)
        self.assertEqual(energy_rows(self.store), [])                                # refused whole, nothing written

    def test_downtime_beyond_retention_is_reported(self):
        self.store.execute("INSERT INTO energy_state(key, value) VALUES ('final_through', ?)", (NOW - 40 * 86400,))
        summary = energy.compute(self.store, NOW)
        self.assertGreater(summary["lost_s"], 9 * 86400)
        self.assertEqual(self.store.events(category="energy")[0]["severity"], "warning")

    def test_archive_is_paged_and_thermal_output_is_capped(self):
        for i in range(26):
            self.store.execute("INSERT INTO energy_monthly VALUES (?, 'halo-1', 1000, 1, 0, NULL, NULL, 'UTC')",
                               (f"20{24 + i // 12:02d}-{i % 12 + 1:02d}",))
        d = self.doc()
        self.assertEqual((len(d["archive"]), d["archive_more"]), (24, True))
        self.assertEqual(len(energy.archive_months(self.store)), 26)
        older = energy.archive_document(self.api, {"before": d["archive"][-1]["month"]})
        self.assertEqual([m["month"] for m in older["months"]], ["2024-02", "2024-01"])
        self.assertFalse(older["more"])
        with self.assertRaises(ValueError):
            energy.archive_document(self.api, {"before": "2024-13"})
        t = NOW - 7 * 86400 + 60
        while t < NOW:
            self.store.record([("node_gpu_throttled", {"machine": "halo-1", "device": "0"}, int((t // QUARTER) % 2), "state"),
                               ("node_gpu_temp_celsius", {"machine": "halo-1", "device": "0", "sensor": "edge"}, 70.0)],
                              ts=t, interval=QUARTER)
            t += QUARTER
        doc = energy.thermals_document(self.api, {"range": "week"})
        machine = doc["machines"][0]
        self.assertEqual(len(machine["throttle"]), energy.MAX_THROTTLE)
        self.assertGreater(machine["throttle_more"], 0)
        self.assertLessEqual(len(machine["lines"][0]["points"]), 7 * 96 + 1)


class DocumentTests(Base):
    def test_gets_never_write_or_create(self):
        self.assertIsNone(energy.energy_document(FakeAPI(None, self.clock), {})["machines"])
        self.assertIn("not open", energy.thermals_document(FakeAPI(None, self.clock), {})["why"]["machines"])
        fresh = Store(":memory:", clock=self.clock)
        self.addCleanup(fresh.close)
        api = FakeAPI(fresh, self.clock)
        changes = fresh._db.total_changes
        d = energy.energy_document(api, {})
        self.assertEqual(d["why"]["machines"], energy.NOT_RUN)
        energy.thermals_document(api, {"range": "day"})
        energy.archive_document(api, {})
        self.assertEqual(fresh._db.total_changes, changes)
        self.assertFalse(energy._has_tables(fresh))
        energy.compute(self.store, NOW)
        changes = self.store._db.total_changes
        self.doc(range="30d")
        self.assertEqual(self.store._db.total_changes, changes)

    def test_queries_are_strict(self):
        for query in ({"range": "1y"}, {"other": "x"}):
            with self.assertRaises(ValueError):
                energy.energy_document(self.api, query)
            with self.assertRaises(ValueError):
                energy.thermals_document(self.api, query)

    def test_thermals_lines_thresholds_and_throttle_periods(self):
        self.api.machines = [("halo-1", "linux")]
        t = NOW - 3 * HOUR
        while t < NOW:
            hot = NOW - 2 * HOUR <= t < NOW - 1.5 * HOUR
            self.store.record([("node_gpu_temp_celsius", {"machine": "halo-1", "device": "0", "sensor": "edge"}, 91.0 if hot else 60.0),
                               ("node_cpu_temp_celsius", {"machine": "halo-1", "sensor": "package"}, 55.0),
                               ("node_gpu_throttled", {"machine": "halo-1", "device": "0"}, 1 if hot else 0, "state")],
                              ts=t, interval=60)
            t += 60
        doc = energy.thermals_document(self.api, {"range": "day"})
        machine = doc["machines"][0]
        gpu = machine["lines"][0]
        self.assertEqual((gpu["kind"], gpu["label"], gpu["max"]), ("gpu", "GPU 0 edge", 91.0))
        self.assertEqual((gpu["warn_buckets"], gpu["sampled_buckets"], gpu["buckets"]), (6, 36, 289))
        self.assertIsNone(machine["lines"][1]["warn_buckets"])                       # no CPU threshold by default
        self.assertEqual(machine["throttle"], [{"device": "0", "start": NOW - 2 * HOUR, "end": NOW - 1.5 * HOUR}])
        self.assertEqual(doc["thresholds"], {"gpu": {"warn": 85.0, "crit": 90.0}})
        self.assertLessEqual(len(gpu["points"]), 289)
        self.assertEqual((gpu["colour"], gpu["dash"]), energy._style(
            "node_gpu_temp_celsius" + telemetry.canonical_labels({"machine": "halo-1", "device": "0", "sensor": "edge"})))
        self.assertIn("recorded maximum", doc["notes"]["warn"])
        self.assertEqual(doc["step"], 300)
        self.assertTrue(doc["notes"]["warn"].startswith("5-minute"))

    def test_a_sensor_keeps_its_style_when_others_disappear(self):
        def styles():
            energy._thermal_cache.clear()
            lines = energy.thermals_document(self.api, {"range": "day"})["machines"][0]["lines"]
            return {line["label"]: (line["colour"], line["dash"]) for line in lines}
        sensors = [{"machine": "halo-1", "sensor": f"s{i}"} for i in range(6)]
        for labels in sensors:
            self.store.record([("node_sensor_temp_celsius", labels, 40.0)], ts=NOW - 23.5 * HOUR, interval=60)
        before = styles()
        for labels in sensors[2:]:
            self.store.record([("node_sensor_temp_celsius", labels, 41.0)], ts=NOW - 60, interval=60)
        self.clock.now = NOW + HOUR                                                 # the first two fall out of range
        after = styles()
        self.assertEqual(len(after), 4)
        self.assertTrue(all(after[k] == before[k] for k in after))

    def test_throttle_periods_never_bridge_missing_buckets(self):
        gpu = {"machine": "halo-1", "device": "0"}
        for t in (NOW - 600, NOW - 540, NOW - 360, NOW - 300):        # a missing stretch between
            self.store.record([("node_gpu_throttled", gpu, 1, "state")], ts=t, interval=60)
        self.store.describe("node_gpu_throttled", "state", interval=300)        # today's cadence never stretches history
        doc = energy.thermals_document(self.api, {"range": "day"})
        self.assertEqual(doc["machines"][0]["throttle"], [{"device": "0", "start": NOW - 600, "end": NOW - 480},
                                                          {"device": "0", "start": NOW - 360, "end": NOW - 240}])
        self.assertTrue(doc["throttle_read"])


class WriteTests(unittest.TestCase):
    """Writes through the portal's own dispatcher (API.feature_post): validation and one audit record each."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        features.reset()
        self.addCleanup(features.reset)
        self.audit = Path(self.dir.name) / "audit.jsonl"
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       clock=Clock(NOW), audit_path=self.audit)
        self.api.telemetry = Store(":memory:", clock=self.api.clock)
        self.addCleanup(self.api.telemetry.close)

    def entries(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def test_price_set_clear_and_refusals_are_audited_once_each(self):
        status, document = self.api.feature_post("energy/price", {"price_per_kwh": 0.25, "currency": "EUR"})
        self.assertEqual((status, document["price"]), (200, {"price_per_kwh": 0.25, "currency": "EUR"}))
        self.assertEqual(energy.price_of(self.api.telemetry)["currency"], "EUR")
        bad = [{"price_per_kwh": True, "currency": "EUR"}, {"price_per_kwh": 0, "currency": "EUR"},
               {"price_per_kwh": 101, "currency": "EUR"}, {"price_per_kwh": 0.2, "currency": "eur"},
               {"price_per_kwh": 0.2}, {"clear": True, "currency": "EUR"}, {"clear": False}, {"price": 1},
               {"price_per_kwh": "0.2", "currency": "EUR"}, {"price_per_kwh": 10 ** 400, "currency": "EUR"},
               {"price_per_kwh": -10 ** 400, "currency": "EUR"}]
        for arguments in bad:
            with self.assertRaises(APIError, msg=arguments):
                self.api.feature_post("energy/price", arguments)
        self.api.feature_post("energy/price", {"clear": True})
        self.assertIsNone(energy.price_of(self.api.telemetry))
        entries = self.entries()
        self.assertEqual(len(entries), 2 + len(bad))
        self.assertEqual([e["result"]["state"] for e in entries].count("refused"), len(bad))
        self.assertTrue(all(e["action"] == "energy/price" for e in entries))

    def test_thresholds_are_validated_exactly(self):
        self.api.feature_post("energy/thresholds", {"gpu": {"warn": 80, "crit": 95}, "cpu": {"warn": 85, "crit": 100},
                                                    "sensor": None})
        self.assertEqual(energy.thresholds_of(self.api.telemetry),
                         {"gpu": {"warn": 80.0, "crit": 95.0}, "cpu": {"warn": 85.0, "crit": 100.0}})
        for arguments in ({}, {"gpu": None}, {"gpu": {"warn": 90, "crit": 85}}, {"gpu": {"warn": 80, "crit": 95, "x": 1}},
                          {"gpu": {"warn": 20, "crit": 95}}, {"gpu": {"warn": 80, "crit": 95}, "disk": None},
                          {"gpu": {"warn": False, "crit": 95}}, {"gpu": [80, 95]}):
            with self.assertRaises(APIError, msg=arguments):
                self.api.feature_post("energy/thresholds", arguments)

    def test_reset_through_the_dispatcher(self):
        status, document = self.api.feature_post("energy/reset", {"confirm": "reset"})
        self.assertEqual(status, 200)
        self.assertEqual(document["reset_at"], NOW)
        self.assertEqual(self.entries()[-1]["action"], "energy/reset")

    def test_registration(self):
        reg = features.registry()
        self.assertIn("energy", reg.loaded)
        self.assertEqual(reg.collectors["energy"][1], 60.0)
        self.assertTrue({"energy/price", "energy/thresholds", "energy/reset"} <= reg.audited)
        self.assertIn("thermals", reg.gets)


class HttpTests(unittest.TestCase):
    """The energy POSTs sit behind the portal's Origin, CSRF and strict JSON checks; the pages are in the manifest."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        features.reset()
        self.addCleanup(features.reset)
        api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                  audit_path=Path(self.dir.name) / "audit.jsonl")
        api.telemetry = Store(":memory:")
        self.addCleanup(api.telemetry.close)
        self.server = server.create_server(0, api=api)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def request(self, method, path, body=None, origin=True, csrf=True):
        host = self.server.server_address[0]
        connection = http.client.HTTPConnection(host, self.port, timeout=5)
        headers = {"Host": f"{host}:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
            if origin:
                headers["Origin"] = f"http://{host}:{self.port}"
            if csrf:
                headers["X-Hyperloom-CSRF"] = self.server.csrf_token
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()

    def test_post_checks_and_documents(self):
        body = {"price_per_kwh": 0.2, "currency": "EUR"}
        self.assertEqual(self.request("POST", "/hyperloom/api/energy/price", body, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/hyperloom/api/energy/price", body, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", "/hyperloom/api/energy/price", body)[0], 200)
        self.assertEqual(self.request("GET", "/hyperloom/api/energy/price")[0], 405)       # no GET of a write name
        status, raw = self.request("GET", "/hyperloom/api/energy?range=7d")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["schema"], "hyperloom/ui-energy-v1")
        self.assertEqual(self.request("GET", "/hyperloom/api/thermals?range=year")[0], 400)
        for name in ("energy.js", "energy.css", "thermals.js"):
            self.assertEqual(self.request("GET", "/pages/" + name)[0], 200, name)


if __name__ == "__main__":
    unittest.main()
