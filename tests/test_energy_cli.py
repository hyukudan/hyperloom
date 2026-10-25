"""`hyperloom energy` and `hyperloom thermals`: the portal's functions and dispatcher, audited the same way."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import cli, telemetry
from hyperloom.web import features
from hyperloom.web.features import energy


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = Path(self.dir.name) / "hyperloom" / "cluster.json"
        patcher = mock.patch.object(cli, "config_path", lambda: self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        features.reset()
        self.addCleanup(features.reset)
        self.addCleanup(self.close_stores)
        self.store = telemetry.Store(self.config.parent / "telemetry.sqlite3")
        telemetry._stores[str(self.config.parent / "telemetry.sqlite3")] = self.store
        now = time.time()
        energy._mark_verified(self.store, now - 86400, now + 86400)       # stands for earlier portal runs
        t = now - 2 * 3600
        while t < now:
            self.store.record([("node_gpu_power_watts", {"machine": "halo-1", "device": "0"}, 100.0),
                               ("node_gpu_temp_celsius", {"machine": "halo-1", "device": "0", "sensor": "edge"}, 70.0),
                               ("engine_generated_tokens", {"profile": "p", "replica": "0", "instance": "a"}, 300.0, "delta")],
                              ts=t, interval=60)
            t += 60

    def close_stores(self):
        for path in list(telemetry._stores):
            if path.startswith(self.dir.name):
                telemetry._stores.pop(path).close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def audit(self):
        path = self.config.parent / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_show_before_and_after_compute(self):
        code, out, _ = self.run_cli("energy")
        self.assertEqual(code, 0)
        self.assertIn("Estimated from sensors, not metered", out)
        self.assertIn("unavailable", out)
        code, out, _ = self.run_cli("energy", "compute")
        self.assertEqual(code, 0)
        self.assertIn("Recomputed", out)
        code, out, _ = self.run_cli("energy", "show", "--range", "7d")
        self.assertEqual(code, 0)
        self.assertIn("halo-1", out)
        self.assertIn("GPU 0 (GPU power sensor)", out)
        code, out, _ = self.run_cli("energy", "--json")
        document = json.loads(out)
        self.assertAlmostEqual(document["totals"]["wh"], 200.0, delta=2.0)
        self.assertIsNotNone(document["efficiency"])

    def test_price_thresholds_and_reset_are_audited(self):
        self.assertEqual(self.run_cli("energy", "price", "--set", "0.3", "--currency", "EUR")[0], 0)
        self.assertEqual(energy.price_of(self.store), {"price_per_kwh": 0.3, "currency": "EUR"})
        code, _, err = self.run_cli("energy", "price", "--set", "0.3", "--currency", "euro")
        self.assertEqual(code, 2)
        self.assertIn("three-letter", err)
        with self.assertRaises(SystemExit):                   # refused by the parser, like the portal's JSON parser
            self.run_cli("energy", "price", "--set", "nan", "--currency", "EUR")
        self.assertEqual(self.run_cli("energy", "price", "--clear")[0], 0)
        self.assertIsNone(energy.price_of(self.store))
        self.assertEqual(self.run_cli("energy", "thresholds", "--gpu", "80", "95", "--cpu", "85", "100")[0], 0)
        self.assertEqual(energy.thresholds_of(self.store)["cpu"], {"warn": 85.0, "crit": 100.0})
        self.assertEqual(self.run_cli("energy", "thresholds", "--no-cpu")[0], 0)
        self.assertNotIn("cpu", energy.thresholds_of(self.store))
        self.assertEqual(energy.thresholds_of(self.store)["gpu"], {"warn": 80.0, "crit": 95.0})
        code, _, err = self.run_cli("energy", "reset")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        code, out, _ = self.run_cli("energy", "reset", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("reset", out)
        actions = [(e["action"], e["result"]["state"]) for e in self.audit()]
        self.assertEqual(actions, [("energy/price", "succeeded"), ("energy/price", "refused"), ("energy/price", "succeeded"),
                                   ("energy/thresholds", "succeeded"), ("energy/thresholds", "succeeded"),
                                   ("energy/reset", "succeeded")])
        code, out, _ = self.run_cli("energy", "archive")
        self.assertEqual(code, 0)
        self.assertIn("Wh", out)

    def test_thermals(self):
        code, out, _ = self.run_cli("thermals")
        self.assertEqual(code, 0)
        self.assertIn("GPU 0 edge: last 70.0 C", out)
        code, out, _ = self.run_cli("thermals", "--range", "week", "--machine", "halo-1", "--json")
        self.assertEqual(json.loads(out)["machines"][0]["id"], "halo-1")
        code, out, _ = self.run_cli("thermals", "--machine", "other")
        self.assertIn("No temperature samples", out)


if __name__ == "__main__":
    unittest.main()
