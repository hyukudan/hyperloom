"""CLI of the ledger package: the same functions and audited dispatch as the portal."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import cli
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.features import ledger
from tests.test_ledger_documents import full_record


FIXTURES = Path(__file__).parent / "fixtures" / "ledger"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "cluster.json"
        features.reset(features.load(("ledger",)))
        self.addCleanup(features.reset, None)
        patcher = mock.patch.object(ledger, "_states", lambda api: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            try:
                code = cli.main([*argv, "--config", str(self.config)])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def store(self):
        store = Store(self.config.parent / "telemetry.sqlite3")
        self.addCleanup(store.close)
        return store

    def test_reads_never_create_the_store(self):
        code, out, _ = self.run_cli("tokens")
        self.assertEqual(code, 0)
        self.assertIn(ledger.NO_STORE, out)
        self.assertIn("unavailable", self.run_cli("census")[1])
        self.assertIn("unavailable", self.run_cli("token-value")[1])
        self.assertFalse((self.config.parent / "telemetry.sqlite3").exists())

    def test_tokens_show_json_export_and_reset(self):
        store = self.store()
        full_record(store, int(time.time() // 3600) * 3600 - 3600, 10, 100, 20, model="=evil")
        code, out, _ = self.run_cli("tokens", "--range", "all", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["kpis"]["total"], 110)
        code, out, _ = self.run_cli("tokens", "export", "--range", "all")
        self.assertIn("\"'=evil\"", out)
        self.assertEqual(self.run_cli("tokens", "reset")[0], "hyperloom: tokens reset deletes every ledger record; repeat with --yes")
        code, out, _ = self.run_cli("tokens", "reset", "--yes")
        self.assertEqual(json.loads(out)["records_deleted"], 4)
        audit = [json.loads(line) for line in (self.config.parent / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(a["action"], a["result"]["state"]) for a in audit], [("ledger/reset", "succeeded")])

    def test_config_prices_and_rtx_parse_go_through_the_audited_dispatch(self):
        runs = Path(self.tmp.name) / "runs"
        runs.mkdir()
        shutil.copy(FIXTURES / "rtx-run.log", runs)
        code, out, _ = self.run_cli("tokens", "config", "--logs-dir", str(runs))
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("rtx-cache")
        run_id = out.split("run ", 1)[1].split()[0]
        code, out, _ = self.run_cli("rtx-cache", "parse", run_id)
        self.assertIn("hits 31/60", out)
        code, out, _ = self.run_cli("token-value", "set", "-", stdin=json.dumps({"currency": "EUR", "default": {"input": -1}}))
        self.assertIn("refused", str(code))
        code, out, _ = self.run_cli("token-value", "set", "-",
                                    stdin=json.dumps({"currency": "EUR", "default": {"input": 1, "cached": 0.1, "output": 4}}))
        self.assertEqual(code, 0)
        actions = [(a["action"], a["result"]["state"]) for a in
                   (json.loads(line) for line in (self.config.parent / "audit.jsonl").read_text(encoding="utf-8").splitlines())]
        self.assertEqual(actions, [("ledger/settings", "succeeded"), ("ledger/rtx-parse", "succeeded"),
                                   ("ledger/prices", "refused"), ("ledger/prices", "succeeded")])

    def test_value_text_names_every_qualifier(self):
        from tests.test_ledger_documents import put
        store = self.store()
        hour = int(time.time() // 3600) * 3600 - 3600
        put(store, hour, "generated", 1_000_000)
        put(store, hour, "prompt", 1_000_000)
        put(store, hour, "cached", 0, span=0.0)                 # cache-restored tokens not reported
        put(store, hour, "imported", 0, span=0.0)               # imported not reported: computed is an upper bound
        self.run_cli("token-value", "set", "-", stdin=json.dumps({"currency": "EUR", "default": {"input": 1, "cached": 0.1, "output": 1}}))
        code, out, _ = self.run_cli("token-value", "--range", "86400")
        self.assertEqual(code, 0)
        self.assertIn("QUALIFIED", out)
        self.assertIn("input partly an upper bound", out)
        self.assertIn("cache-restored tokens not reported", out)
        self.assertNotIn("(exact)", out)

    def test_engine_key_never_reaches_output_or_audit(self):
        secret = "f" * 64
        with mock.patch("hyperloom.secrets.get", return_value=secret):
            for argv in (("tokens", "--json"), ("census", "--json"), ("rtx-cache", "--json"), ("token-value", "--json")):
                code, out, err = self.run_cli(*argv)
                self.assertNotIn(secret, out + err)


if __name__ == "__main__":
    unittest.main()
