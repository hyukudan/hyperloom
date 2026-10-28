"""Published measurements, cached live identity and read-only API dispatch."""

import copy
import json
from pathlib import Path
import re
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom.web import features
from hyperloom.web.api import API, APIError, redact
from hyperloom.web.features import results


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(results.DATA.read_text(encoding="utf-8"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "results.json"
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self.patch = mock.patch.object(results, "DATA", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.api = SimpleNamespace(
            document=lambda name, **fields: {"schema": "hyperloom/ui-" + name + "-v1", **fields},
            cached_snapshot=lambda: {}, clock=lambda: 1000, config_path=Path(self.temp.name) / "cluster.json",
            cluster=lambda: SimpleNamespace(profiles={}))
        self.records = mock.patch.object(results.machines, "launch_records", return_value={}).start()
        self.addCleanup(mock.patch.stopall)
        self.latency_patch = mock.patch.object(results.observe, "latency_document", return_value={
            "groups": [], "why": {"groups": "no samples"}})
        self.latency = self.latency_patch.start()

    def test_good_file(self):
        data, why = results.published()
        self.assertEqual(data, self.data)
        self.assertIsNone(why)

    def test_missing_file_is_unavailable(self):
        self.path.unlink()
        data = results.results(self.api, {})
        self.assertIsNone(data["published"])
        self.assertIn("invalid", data["why"]["published"])
        self.assertNotIn(str(self.path), json.dumps(data))

    def test_bad_schema_is_unavailable(self):
        self.data["schema"] = "other"
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertIsNone(results.published()[0])

    def test_nonfinite_is_unavailable(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.data["models"][0]["rows"][0]["best"] = value
                self.path.write_text(json.dumps(self.data), encoding="utf-8")
                self.assertIsNone(results.published()[0])

    def test_wrong_types_and_duplicate_identity(self):
        for path, value in (("concurrency", True), ("best", "10"), ("typical", False),
                            ("hardware", 42), ("build", "unknown"), ("metric", "guessed")):
            with self.subTest(path=path):
                data = copy.deepcopy(self.data)
                data["models"][0]["rows"][0][path] = value
                with self.assertRaises(ValueError):
                    results.validate(data)
        data = copy.deepcopy(self.data)
        data["models"].append(data["models"][0])
        with self.assertRaises(ValueError):
            results.validate(data)

    def test_filter(self):
        doc = results.results(self.api, {"model": "glm-5.3-flash"})
        self.assertEqual([m["id"] for m in doc["published"]["models"]], ["glm-5.3-flash"])
        self.assertEqual(results.results(self.api, {"model": "unknown"})["published"]["models"], [])
        for value in ("../x", "", "x/y", "x y", "x" * 129, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                results.results(self.api, {"model": value})
        with self.assertRaises(ValueError):
            results.results(self.api, {"path": "x"})

    def running(self, record=None):
        self.records.return_value = {"custom": {"config_path": str(self.api.config_path), **(record or {})}}
        self.api.cluster = lambda: SimpleNamespace(profiles={"custom": {"preset": "qwen-replicas-w5"}})
        self.api.cached_snapshot = lambda: {"stats": {"server_time": 1000, "engines": [
            {"id": "custom/replica", "profile": "custom", "throughput": {"value": 31.5, "window_s": 10}}]}}

    def test_live_mapping_and_labels(self):
        self.running()
        self.latency.return_value = {"groups": [{"group": "qwen3.8-flash-next", "metrics": {
            "ttft_ms": {"engine": {"n": 7, "p50": 1200, "p95": 2300}},
            "decode_tok_s": {"engine": {"n": 5, "p50": 50}}}}]}
        profile = results.results(self.api, {})["live"][0]
        self.assertEqual(profile["model"], "qwen3.8-flash-next")
        values = profile["values"]
        self.assertEqual(values[0]["value"], 31.5)
        self.assertEqual(values[0]["window_s"], 10)
        self.assertIn("completed tok/s", values[0]["label"])
        ttft = [v for v in values if v["metric"] == "ttft_s"]
        self.assertEqual([v["value"] for v in ttft], [1.2, 2.3])
        self.assertTrue(all(v["n"] == 7 and v["window_s"] == 600 for v in ttft))
        self.assertTrue(all(v["label"].startswith("live · engine-side") for v in values))
        self.latency.assert_called_once_with(self.api, {"profile": "custom", "range": "600", "by": "model"})
        self.assertEqual(results.results(self.api, {"model": "glm-5.3-flash"})["live"], [])

    def test_recipe_identity_precedes_preset(self):
        self.running({"recipe": {"effective": {"match": {"model": "minimax-m3.1-flash"}}}})
        self.assertEqual(results.results(self.api, {})["live"][0]["model"], "minimax-m3.1-flash")

    def test_cached_ttft_approximation_is_labelled(self):
        self.running()
        snap = self.api.cached_snapshot()
        snap["stats"]["engines"][0]["ttft_p50"] = {"value": 500, "n": 9}
        snap["stats"]["engines"][0]["ttft_p95"] = {"value": 800, "n": 9}
        self.api.cached_snapshot = lambda: snap
        values = results.results(self.api, {})["live"][0]["values"]
        ttft = [v for v in values if v["metric"] == "ttft_s"]
        self.assertEqual([v["value"] for v in ttft], [0.5, 0.8])
        self.assertTrue(all("approximation (queue + prefill)" in v["label"] for v in ttft))
        self.assertTrue(all(v["n"] == 9 and v["window_s"] == 600 for v in ttft))

    def test_existing_fleet_rate_fills_missing_core_stats(self):
        self.running()
        self.api.cached_snapshot = lambda: {}
        self.api.cluster = lambda: SimpleNamespace(profiles={"custom": {"preset": "qwen-replicas"}},
                                                   machines={"node": SimpleNamespace(id="node", access="ssh")})
        with mock.patch.object(results.machines, "engine_rows", return_value=[
                {"profile": "custom", "role": "rank 0", "throughput": 27, "throughput_window_s": 5}]), \
                mock.patch.object(results.machines, "control_machine", return_value=None):
            values = results.results(self.api, {})["live"][0]["values"]
        rate = next(v for v in values if v["metric"] == "aggregate_tok_s")
        self.assertEqual((rate["value"], rate["window_s"], rate["replica"]), (27, 5, "node"))

    def test_unknown_identity_has_why(self):
        self.running({"recipe": {"effective": {"match": {"model": "other"}}}})
        row = results.results(self.api, {})["live"][0]
        self.assertIsNone(row["model"])
        self.assertTrue(row["why"])

    def test_no_running_profile_is_empty(self):
        self.assertEqual(results.results(self.api, {})["live"], [])

    def test_cached_running_profile_requires_main_discovery_record(self):
        self.running()
        self.records.return_value = {}
        self.api.cached_snapshot = lambda: {"profiles": {"profiles": [{"id": "custom", "state": "running"}]}}
        self.assertEqual(results.results(self.api, {})["live"], [])

    def test_stopped_record_and_stale_stats(self):
        self.running()
        snap = self.api.cached_snapshot()
        snap["stats"]["server_time"] = 900
        self.api.cached_snapshot = lambda: snap
        self.assertIsNone(results.results(self.api, {})["live"][0]["values"][0]["value"])
        snap["profiles"] = {"profiles": [{"id": "custom", "state": "stopped"}]}
        self.assertEqual(results.results(self.api, {})["live"], [])

    def test_get_does_not_poll_or_create_store(self):
        self.running()
        self.latency_patch.stop()
        self.api.snapshot = mock.Mock(side_effect=AssertionError("snapshot refresh"))
        with mock.patch("hyperloom.telemetry.existing_store", return_value=None), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")), \
                mock.patch("hyperloom.telemetry.Store", side_effect=AssertionError("store creation")):
            row = results.results(self.api, {})["live"][0]
        self.assertEqual(row["model"], "qwen3.8-flash-next")
        self.assertTrue(all(v["why"] for v in row["values"] if v["value"] is None))
        self.api.snapshot.assert_not_called()

    def test_dispatch_redacts_and_maps_filter_to_400(self):
        api = API(Path(self.temp.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                  audit_path=Path(self.temp.name) / "audit.jsonl")
        reg = features.Registry()
        results.register(reg)
        features.reset(reg)
        self.addCleanup(features.reset)
        data = copy.deepcopy(self.data)
        data["models"][0]["rows"][0]["note"] = "password=hunter2"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(api, "cluster", self.api.cluster), \
                mock.patch.object(api, "cached_snapshot", return_value={}):
            result = api.read("results", {})
        self.assertEqual(result["published"]["models"][0]["rows"][0]["note"], redact("password=hunter2"))
        self.assertEqual(result["schema"], "hyperloom/ui-results-v1")
        with self.assertRaises(APIError) as caught:
            api.read("results", {"model": "../x"})
        self.assertEqual(caught.exception.status, 400)


class PackagedResultsTests(unittest.TestCase):
    def test_committed_file_valid_and_private_identifiers_absent(self):
        text = results.DATA.read_text(encoding="utf-8")
        document = results.validate(json.loads(text))
        self.assertEqual(document["source"], {"repo": "hyukudan/strix-halo-engine-lab", "ref": "62d7b0ac",
                                              "file": "README.md", "retrieved": "2026-10-26"})
        # The mandatory source.repo contains "halo-"; scan it after removing that exact allowed reference.
        private_text = text.replace(document["source"]["repo"], "")
        for pattern in (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", r"(?i)\b(?:cluster\d+|halo-)",
                        r"(?i)[a-z]:[\\/](?:users|documents and settings)[\\/]",
                        r"/(?:home|Users|root)/", r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b"):
            self.assertIsNone(re.search(pattern, private_text), pattern)
        for model in ("minimax-m3.1-flash", "glm-5.3-flash"):
            self.assertTrue(next(m for m in document["models"] if m["id"] == model)["rows"])


if __name__ == "__main__":
    unittest.main()
