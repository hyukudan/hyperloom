"""CLI equivalents: hyperloom health ... and hyperloom alerts ... call the portal's functions."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hyperloom import cli, secrets
from hyperloom.telemetry.store import Store
from hyperloom.web.features import alerts, health

HOST = ".".join(("hooks", "example", "test"))           # a reserved test name, built at run time


class CLITests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = Path(self.dir.name) / "cluster.json"
        patcher = mock.patch.object(secrets, "_directory", lambda: Path(self.dir.name) / "secrets")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main([*argv, "--config", str(self.config)])
        return code, out.getvalue(), err.getvalue()

    def store(self):
        store = Store(self.config.parent / "telemetry.sqlite3")
        self.addCleanup(store.close)
        return store

    def audit(self):
        path = self.config.parent / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_reads_need_a_store_and_never_create_one(self):
        code, _, err = self.run_cli("health")
        self.assertEqual(code, 1)
        self.assertIn("has not run", err)
        self.assertFalse((self.config.parent / "telemetry.sqlite3").exists())

    def test_findings_rules_and_rule_changes(self):
        store = self.store()
        store.put(health.STATE, "gpu-hot:halo-1/card0", {"id": "gpu-hot:halo-1/card0", "rule": "gpu-hot", "title": "GPU hot",
                                                         "state": "firing", "severity": "critical", "detail": "92 °C",
                                                         "since": 1.0})
        code, out, _ = self.run_cli("health")
        self.assertEqual(code, 0)
        self.assertIn("critical GPU hot · 92 °C", out)
        code, out, _ = self.run_cli("health", "set-rule", "gpu-hot", "--hold", "30", "--param", "warn_c=80")
        self.assertEqual(code, 0)
        self.assertEqual(store.get(health.CONFIG, "gpu-hot"), {"hold_s": 30.0, "params": {"warn_c": 80.0}})
        code, _, err = self.run_cli("health", "set-rule", "gpu-hot", "--param", "warn_c=500")
        self.assertEqual(code, 1)
        self.assertIn("warn_c must be in", err)
        code, out, _ = self.run_cli("health", "rules")
        self.assertIn("gpu-hot", out)
        self.assertIn("warn_c=80", out)
        self.assertEqual([(a["action"], a["result"]["state"]) for a in self.audit()],
                         [("health/rules/set", "succeeded"), ("health/rules/set", "refused")])

    def test_the_configuration_is_kept_in_either_position(self):
        other = Path(self.dir.name) / "other" / "cluster.json"
        other.parent.mkdir()
        store = Store(other.parent / "telemetry.sqlite3")
        self.addCleanup(store.close)
        store.event("health", "info", "x", ts=1.0)
        mine = self.store()
        mine.event("health", "info", "mine", ts=1.0)
        for argv in (["health", "--config", str(other), "clear-events", "--older-than", "all", "--yes"],
                     ["health", "clear-events", "--config", str(other), "--older-than", "all", "--yes"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 0)
        self.assertEqual([e["message"] for e in mine.events()], ["mine"])          # the default one is untouched
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["alerts", "--config", str(other), "list"]), 0)

    def test_events_and_clear(self):
        store = self.store()
        store.event("health", "warning", "GPU hot", machine="halo-1", ts=100.0)
        code, out, _ = self.run_cli("health", "events", "--json")
        document = json.loads(out)
        self.assertEqual(document["events"][0]["message"], "GPU hot")
        self.assertNotIn("clear", document)
        code, _, err = self.run_cli("health", "clear-events", "--older-than", "all")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        code, out, _ = self.run_cli("health", "clear-events", "--older-than", "all", "--yes")
        self.assertEqual((code, json.loads(out)["removed"]), (0, 1))
        self.assertEqual([e["message"] for e in store.events()], ["activity log cleared (all events)"])

    def test_alert_channels_read_the_url_from_stdin_and_never_print_it(self):
        url = f"https://{HOST}/topic-" + "7" * 20
        code, out, _ = self.run_cli("alerts", "add", "--name", "lab", "--kind", "push", "--url-stdin", stdin=url + "\n")
        self.assertEqual(code, 0)
        channel = json.loads(out)
        self.assertEqual(channel["url_masked"], f"https://{HOST}/…")
        code, out, _ = self.run_cli("alerts")
        self.assertIn(channel["id"], out)
        self.assertNotIn("7" * 20, out)
        code, _, _ = self.run_cli("alerts", "edit", channel["id"], "--min-severity", "critical")
        self.assertEqual(code, 0)
        stored = self.store().get(alerts.CHANNELS, channel["id"])
        self.assertEqual((stored["min_severity"], alerts.load_url(channel["id"], stored["url_version"])), ("critical", url))
        with mock.patch.object(alerts, "send", lambda *a, **kw: {"ok": False, "error": "connection failed (OSError)"}):
            code, out, _ = self.run_cli("alerts", "test", channel["id"])
        self.assertEqual((code, json.loads(out)["ok"]), (1, False))
        code, _, err = self.run_cli("alerts", "remove", channel["id"])
        self.assertEqual(code, 1)
        code, _, _ = self.run_cli("alerts", "remove", channel["id"], "--yes")
        self.assertEqual(code, 0)
        self.assertNotIn("7" * 20, json.dumps(self.audit()))
        self.assertEqual([a["action"] for a in self.audit()], ["alerts/channels", "alerts/channels", "alerts/test",
                                                              "alerts/channels/delete"])
        code, _, err = self.run_cli("alerts", "add", "--name", "x", "--kind", "push", "--url-stdin", stdin=f"http://{HOST}/x\n")
        self.assertEqual(code, 1)
        self.assertIn("loopback", err)


if __name__ == "__main__":
    unittest.main()
