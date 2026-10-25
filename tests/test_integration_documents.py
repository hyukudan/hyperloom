"""Integration: every registered feature document and CSV export answers on the fixture portal (all packages
together, with their browser fixture seeds), and no feature failed to load."""

import http.client
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "ui"))
import smoke  # noqa: E402

from hyperloom.web import features  # noqa: E402


# Prefix endpoints need an id; these are the ones the fixture seeds provide or that answer a listing.
PREFIX_SAMPLES = {"history/": "gpu_busy_percent"}
NEEDS_QUERY = {"bench/compare"}      # answers 400 naming its required arguments when called bare
EXPORTS = ("devices", "stats", "ledger", "energy", "bench/runs", "history/gpu_busy_percent")


class FeatureDocumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = smoke.FixtureServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def get(self, path):
        host, port = self.server.url.split("//", 1)[1].rstrip("/").split(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=30)
        connection.request("GET", path, headers={"Host": f"{host}:{port}"})
        response = connection.getresponse()
        return response.status, response.read()

    def test_every_feature_loaded(self):
        status, body = self.get("/hyperloom/api/settings")
        self.assertEqual(status, 200)
        loaded = json.loads(body).get("features") or {}
        self.assertEqual(loaded.get("failed"), {}, loaded.get("failed"))
        self.assertEqual(set(loaded.get("loaded", [])), set(features.load().loaded))

    def test_every_document_answers(self):
        registry = features.load()
        failures = {}
        endpoints = sorted(set(registry.gets) - NEEDS_QUERY) + [prefix + PREFIX_SAMPLES[prefix] for prefix in registry.prefixes
                                             if prefix in PREFIX_SAMPLES]
        for endpoint in endpoints:
            status, body = self.get("/hyperloom/api/" + endpoint)
            if status != 200:
                failures[endpoint] = (status, body[:200])
            else:
                json.loads(body)
        self.assertEqual(failures, {})

    def test_csv_exports(self):
        if "integrations" not in features.load().loaded:
            self.skipTest("integrations feature not present")
        failures = {}
        for name in EXPORTS:
            status, body = self.get("/hyperloom/api/integrations/export/" + name + ".csv")
            if status not in (200, 409):     # 409: the source says it is unavailable (named reason), not a crash
                failures[name] = (status, body[:200])
        self.assertEqual(failures, {})


if __name__ == "__main__":
    unittest.main()
