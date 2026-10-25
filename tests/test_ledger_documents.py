"""Token ledger documents (F11), value report (F47), CSV export and the audited writes."""

import json
from pathlib import Path
import unittest
from unittest import mock

from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import APIError
from hyperloom.web.features import ledger
from tests.test_ledger_collector import HOUR, make_api


DAY = 86400


def put(store, hour, kind, tokens, *, model="qwen3.8", machine="halo-1", profile="qwen", replica="halo-1",
        span=3600.0, record=3600.0, observed=None, uncertain=0.0, blind=0.0, late=0.0):
    ledger._prepared(store)
    store.execute(ledger.UPSERT, (hour, profile, replica, machine, model, kind, float(tokens), span, record,
                                  record if observed is None else observed, uncertain, blind, late))


def full_record(store, hour, generated, prompt, cached, imported=0.0, **names):
    for kind, tokens in (("generated", generated), ("prompt", prompt), ("cached", cached), ("imported", imported)):
        put(store, hour, kind, tokens, **names)


class LedgerDocumentTests(unittest.TestCase):
    def setUp(self):
        self.api, self.clock = make_api(now=HOUR + 1800.0)
        self.addCleanup(self.api._directory.cleanup)
        self.store = self.api.telemetry
        self.store.put("prefs/ui", "timezone", "UTC")

    def test_kpis_models_and_derived_computed(self):
        full_record(self.store, HOUR - 3600, 100, 1000, 400)
        full_record(self.store, HOUR, 50, 500, 100, model="other", machine="halo-2", replica="halo-2")
        doc = ledger.ledger(self.api, {"range": "86400"})
        k = doc["kpis"]
        self.assertEqual((k["total"], k["input"], k["generated"], k["cached"], k["computed"]), (1650, 1500, 150, 500, 1000))
        self.assertEqual(k["computed_tag"], "measured")
        self.assertAlmostEqual(k["cached_share"], 500 / 1500)
        self.assertAlmostEqual(k["output_input"], 0.1)
        self.assertEqual(k["tokens_saved"], 500)
        self.assertEqual([m["model"] for m in doc["models"]], ["qwen3.8", "other"])
        self.assertEqual(doc["bucket"], "hour")
        self.assertEqual([b["values"]["computed"] for b in doc["buckets"]], [600.0, 400.0])
        self.assertTrue(any("prefix cache restored 40.0 %" in text for text in doc["insights"]))

    def test_missing_cached_is_an_upper_bound_never_zero(self):
        put(self.store, HOUR, "generated", 10)
        put(self.store, HOUR, "prompt", 100)
        put(self.store, HOUR, "cached", 0, span=0.0)            # not reported for the record
        put(self.store, HOUR, "imported", 0)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertIsNone(doc["kpis"]["cached"])
        self.assertIsNone(doc["kpis"]["cached_share"])
        self.assertIn("kpis.cached_share", doc["why"])
        self.assertEqual((doc["kpis"]["computed_estimated"], doc["kpis"]["computed_tag"]), (100.0, "partly estimated"))
        self.assertIsNone(doc["models"][0]["cached"])

    def test_partly_reported_prompt_makes_computed_unavailable(self):
        put(self.store, HOUR, "generated", 10)
        put(self.store, HOUR, "prompt", 100, span=1800.0)
        put(self.store, HOUR, "cached", 0)
        put(self.store, HOUR, "imported", 0)
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertEqual((doc["kpis"]["computed"], doc["kpis"]["computed_unavailable_input"]), (0.0, 100.0))
        records, _ = ledger._records(self.store, 0, self.clock.now)
        self.assertEqual(records[0]["computed_tag"], "unavailable")

    def test_inconsistent_counters_are_unavailable_not_clamped(self):
        full_record(self.store, HOUR, 10, 100, 150)
        records, _ = ledger._records(self.store, 0, self.clock.now)
        self.assertEqual((records[0]["computed"], records[0]["computed_tag"]), (None, "unavailable"))

    def test_days_follow_the_configured_zone_across_a_daylight_saving_change(self):
        self.store.put("prefs/ui", "timezone", "Europe/Madrid")
        # 2026-10-25 is the autumn change in this zone: the local day has 25 hours (22:00 UTC on 24th .. 23:00 UTC).
        start = 1792879200        # 2026-10-24T22:00:00Z = local midnight
        self.clock.now = start + 26 * 3600
        for h in range(26):
            full_record(self.store, start + h * 3600, 1, 1, 0)
        doc = ledger.ledger(self.api, {"range": "604800"})
        days = {b["label"]: b["tokens"] for b in doc["buckets"]}
        self.assertEqual(days["2026-10-25"], 50.0)          # 25 local hours x 2 tokens
        self.assertEqual(days["2026-10-26"], 2.0)
        self.assertEqual(doc["timezone"], "Europe/Madrid")

    def test_coverage_counts_unreachable_time_as_expected(self):
        full_record(self.store, HOUR, 10, 100, 0, span=1800.0, record=1800.0, observed=1800.0)
        self.store.execute("INSERT INTO ledger_intervals(source, start, end, observed) VALUES ('qwen/halo-1', ?, ?, 1)",
                           (HOUR, HOUR + 1800))
        self.store.execute("INSERT INTO ledger_unreachable(source, profile, replica, machine, since, until, why) "
                           "VALUES ('qwen/halo-1', 'qwen', 'halo-1', 'halo-1', ?, NULL, 'down')", (HOUR + 1800,))
        self.clock.now = HOUR + 3600
        doc = ledger.ledger(self.api, {"range": "3600"})
        self.assertAlmostEqual(doc["buckets"][0]["coverage"], 0.5)
        self.assertTrue(any(g["reason"].startswith("engine unreachable") for g in doc["gaps"]))

    def test_a_record_without_any_cached_count_makes_cached_totals_partial(self):
        full_record(self.store, HOUR - 3600, 10, 100, 40)
        put(self.store, HOUR, "generated", 10)
        put(self.store, HOUR, "prompt", 100)
        put(self.store, HOUR, "imported", 0)                    # no cached row at all in this hour
        doc = ledger.ledger(self.api, {"range": "86400"})
        self.assertEqual((doc["kpis"]["cached"], doc["kpis"]["cached_tag"][:7]), (40.0, "partial"))
        self.assertAlmostEqual(doc["kpis"]["cached_share"], 0.4)  # only the whole-record hour enters the share
        self.assertEqual(doc["models"][0]["cached_tag"], "partial")

    def test_an_incomplete_prompt_record_stays_out_of_the_cached_share(self):
        full_record(self.store, HOUR - 3600, 10, 100, 40)
        put(self.store, HOUR, "generated", 10)
        put(self.store, HOUR, "prompt", 100, span=1800.0)
        put(self.store, HOUR, "cached", 90)
        put(self.store, HOUR, "imported", 0)
        doc = ledger.ledger(self.api, {"range": "86400"})
        self.assertAlmostEqual(doc["kpis"]["cached_share"], 0.4)

    def test_a_truncated_range_is_never_an_exact_value(self):
        self.store.put(ledger.PREFS, "prices", {"currency": "EUR", "default": {"input": 1.0, "cached": 0.1, "output": 1.0}, "models": {}})
        full_record(self.store, HOUR - 3600, 10, 100, 0)
        full_record(self.store, HOUR, 10, 100, 0)
        with mock.patch.object(ledger, "MAX_ROWS", 4):
            value = ledger.value(self.api, {"range": "86400"})
        self.assertFalse(value["totals"]["exact"])
        self.assertIn("range too long: only the newest records were aggregated", value["totals"]["qualifiers"])

    def test_buckets_distinguish_observed_zero_from_unobserved_time(self):
        full_record(self.store, HOUR, 0, 0, 0)
        self.store.execute("INSERT INTO ledger_intervals(source, start, end, observed) VALUES ('qwen/halo-1', ?, ?, 1)",
                           (HOUR, HOUR + 1800))
        self.store.execute("INSERT INTO ledger_listed(source, start, end) VALUES ('qwen/halo-1', ?, ?)",
                           (HOUR - 7200, HOUR - 3600))
        self.clock.now = HOUR + 3600
        doc = ledger.ledger(self.api, {"range": "86400"})
        states = {b["start"]: b["state"] for b in doc["buckets"]}
        self.assertEqual(states, {HOUR - 7200: "expected, not observed", HOUR: "observed"})

    def test_empty_and_missing_store(self):
        doc = ledger.ledger(self.api, {})
        self.assertIsNone(doc["kpis"])
        self.assertIn("no ledger records yet", doc["why"]["kpis"])
        self.api.telemetry = None
        doc = ledger.ledger(self.api, {})
        self.assertEqual(doc["why"]["kpis"], ledger.NO_STORE)

    def test_query_validation(self):
        for query in ({"range": "7"}, {"by": "color"}, {"other": "1"}):
            with self.assertRaises(ValueError, msg=query):
                ledger.ledger(self.api, query)

    def test_csv_escapes_formula_prefixes(self):
        full_record(self.store, HOUR, 10, 100, 0, model="=HYPERLINK(1)", machine="+halo", profile="-p", replica="@r")
        text, truncated = ledger.csv_text(self.api, {"range": "3600"})
        self.assertEqual(truncated, 0)
        line = text.splitlines()[1]
        self.assertIn('"\'-p","\'@r","\'+halo","\'=HYPERLINK(1)"', line)
        self.assertEqual(len(text.splitlines()), 1 + 5)      # four counters + the derived computed row
        self.assertIn('"computed","100","measured"', text)

    def test_gets_never_write(self):
        full_record(self.store, HOUR, 10, 100, 0)
        before = (self.store.execute("SELECT COUNT(*) FROM ledger"), self.store.items("prefs/ledger"),
                  self.store.execute("SELECT COUNT(*) FROM docs"))
        for handler, query in ((ledger.ledger, {}), (ledger.export, {}), (ledger.census, {}), (ledger.rtx, {}),
                               (ledger.value, {})):
            with mock.patch.object(ledger, "_states", lambda api: []):
                handler(self.api, query)
        after = (self.store.execute("SELECT COUNT(*) FROM ledger"), self.store.items("prefs/ledger"),
                 self.store.execute("SELECT COUNT(*) FROM docs"))
        self.assertEqual(before, after)

    def test_gets_without_a_store_create_nothing(self):
        self.api.telemetry = None
        with mock.patch.object(ledger, "_states", lambda api: []):
            for handler in (ledger.ledger, ledger.census, ledger.rtx, ledger.value):
                handler(self.api, {})
            with self.assertRaises(PermissionError):
                ledger.export(self.api, {})
        self.assertFalse((self.api.config_path.parent / "telemetry.sqlite3").exists())


class AuditedWriteTests(unittest.TestCase):
    def setUp(self):
        self.api, self.clock = make_api()
        self.addCleanup(self.api._directory.cleanup)
        features.reset(features.load(("ledger",)))
        self.addCleanup(features.reset, None)

    def audit(self):
        path = self.api.audit_path
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_reset_needs_confirmation_and_is_audited(self):
        full_record(self.api.telemetry, HOUR, 10, 100, 0)
        with self.assertRaises(APIError):
            self.api.feature_post("ledger/reset", {})
        status, document = self.api.feature_post("ledger/reset", {"confirm": "reset"})
        self.assertEqual((status, document["records_deleted"]), (200, 4))
        actions = [(entry.get("action"), (entry.get("result") or {}).get("state")) for entry in self.audit()]
        self.assertEqual(actions, [("ledger/reset", "refused"), ("ledger/reset", "succeeded")])
        self.assertEqual(self.api.telemetry.execute("SELECT COUNT(*) FROM ledger")[0][0], 0)

    def test_prices_validation_and_value_report(self):
        bad = [{"currency": "EUR", "default": {"input": -1, "cached": 0, "output": 1}},
               {"currency": "EUR", "default": {"input": 1, "cached": 0}},
               {"currency": "EURO-LONG!", "default": None},
               {"currency": "EUR", "default": {"input": True, "cached": 0, "output": 1}},
               {"currency": "EUR", "extra": 1}]
        for arguments in bad:
            with self.assertRaises(APIError, msg=arguments):
                self.api.feature_post("ledger/prices", arguments)
        self.api.feature_post("ledger/prices", {"currency": "EUR", "default": {"input": 2.0, "cached": 0.5, "output": 8.0},
                                                "models": {"other": {"input": 1.0, "cached": 0.1, "output": 4.0}}})
        full_record(self.api.telemetry, HOUR, 1_000_000, 3_000_000, 1_000_000)
        full_record(self.api.telemetry, HOUR, 500_000, 1_000_000, 0, model="other", machine="halo-2", replica="halo-2")
        put(self.api.telemetry, HOUR, "generated", 10, model="unpriced", replica="x")
        doc = ledger.value(self.api, {"range": "3600"})
        rows = {row["model"]: row for row in doc["models"]}
        q = rows["qwen3.8"]
        self.assertEqual((q["input_tokens"], q["cached_tokens"], q["output_tokens"]), (2_000_000, 1_000_000, 1_000_000))
        self.assertAlmostEqual(q["input_cost"], 4.0)
        self.assertAlmostEqual(q["cached_cost"], 0.5)
        self.assertAlmostEqual(q["output_cost"], 8.0)
        self.assertAlmostEqual(q["savings"], 1.5)
        self.assertEqual(rows["other"]["price_source"], "model")
        self.assertEqual(rows["unpriced"]["price_source"], "default")
        self.assertAlmostEqual(doc["totals"]["total"], 12.5 + 3.0 + 10 / 1e6 * 8.0)
        self.api.feature_post("ledger/prices", {"currency": "EUR", "models": {"other": {"input": 1.0, "cached": 0.1, "output": 4.0}}})
        doc = ledger.value(self.api, {"range": "3600"})
        rows = {row["model"]: row for row in doc["models"]}
        self.assertIsNone(rows["qwen3.8"]["total"])
        self.assertIn("qwen3.8", doc["why"]["totals"])
        self.assertAlmostEqual(doc["totals"]["total"], 3.0)
        self.assertEqual([e["action"] for e in self.audit()].count("ledger/prices"), 7)

    def test_settings_refuse_relative_missing_and_linked_directories(self):
        root = Path(self.api._directory.name)
        for value in ("relative/dir", str(root / "missing"), 7):
            with self.assertRaises(APIError, msg=value):
                self.api.feature_post("ledger/settings", {"census_dir": value})
        (root / "census").mkdir()
        self.api.feature_post("ledger/settings", {"census_dir": str(root / "census")})
        self.assertEqual(ledger.settings(self.api)["census_dir"], str(root / "census"))
        self.api.feature_post("ledger/settings", {"census_dir": None})
        self.assertIsNone(ledger.settings(self.api)["census_dir"])


if __name__ == "__main__":
    unittest.main()
