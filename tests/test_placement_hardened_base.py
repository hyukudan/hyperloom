"""Placement integration with hardened ownership, stop and polling paths; CPU fakes only."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from hyperloom import admission, cli, orchestrate, pollbudget, replica_spool, rtx, secrets
from hyperloom.recipes import service
from hyperloom.web.api import API, APIError


class HardenedBaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "cluster.json"
        self.config.write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(secrets, "runtime_dir", lambda: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.helper = self.root / "helper"
        self.helper.mkdir()
        orchestrate._write(self.helper / "state.json", {"state": "ready"})
        self.state = {"profile": "placement", "machines": {}, "leases": [], "router": None,
                      "keys": [], "known_hosts": str(self.root / "known_hosts"),
                      "prefill_service": {"spool": str(self.root / "spool"), "directory": str(self.helper)}}

    def save(self, *, known=True):
        state = dict(self.state)
        if known:
            state["config"] = orchestrate.config_record(self.config)
        orchestrate._write(orchestrate._state_path("placement"), state)

    def test_unknown_placement_is_listed_and_cli_down_needs_no_admission_database(self):
        self.save(known=False)
        with mock.patch.object(replica_spool.Client, "ready", return_value=True):
            status = orchestrate.status(config_path=self.config)
        self.assertEqual(status["placement"]["config_identity"], "unknown")
        self.assertIn("unknown configuration", status["placement"]["why_config"])
        self.assertTrue(status["placement"]["prefill_service"]["ready"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admission, "for_config", side_effect=sqlite3.DatabaseError("unavailable")), \
                mock.patch.object(rtx, "stop_service") as stop, \
                redirect_stdout(out), redirect_stderr(err):
            result = cli.main(["down", "placement", "--config", str(self.config)])
        self.assertEqual(result, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["state"], "stopped")
        stop.assert_called()
        self.assertTrue(all(call.args == (self.state["prefill_service"],) for call in stop.call_args_list))
        self.assertFalse(orchestrate._state_path("placement").exists())
        self.assertIn("stopping without its hold", err.getvalue())

    def test_unknown_deploy_target_keeps_409_before_any_admission_or_stop(self):
        self.save(known=False)
        api = API(self.config, audit_path=self.root / "audit.jsonl")
        with service.configured(self.config), \
                mock.patch("hyperloom.recipes.preflight.cli",
                           side_effect=lambda *a, **kw: service.reviewed_stops([], profile="placement")), \
                mock.patch.object(admission, "for_config", side_effect=AssertionError("admitted")), \
                mock.patch.object(orchestrate, "down", side_effect=AssertionError("stopped")):
            with self.assertRaises(APIError) as caught:
                api.mutate("recipes/deploy", {"inputs": "a" * 64})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("unknown configuration", str(caught.exception))
        self.assertEqual(api._jobs, {})

    def test_spool_status_retries_and_post_job_cycle_keep_the_polling_budget(self):
        self.state["machines"] = {"node": {"install_root": "/opt/test"}}
        self.save()
        now = [1000.0]
        api = API(self.config, clock=lambda: now[0], audit_path=self.root / "audit.jsonl")
        reads, nodes = [], []
        with mock.patch.object(orchestrate, "_client", lambda machine, known=None: machine), \
                mock.patch.object(orchestrate, "_call", side_effect=lambda *a, **kw:
                                  nodes.append(now[0]) or {"leases": []}), \
                mock.patch.object(replica_spool.Client, "ready", side_effect=lambda: reads.append(now[0]) or True), \
                mock.patch.object(replica_spool.Client, "_request",
                                  side_effect=AssertionError("passive status enqueued work")):
            budget = pollbudget.for_api(api)
            for step in range(60):
                if step % 15 == 0 and step:
                    budget.node("node", "turns", now=now[0] - 1)
                if step == 15:
                    api._status_stale = True
                api._statuses()
                now[0] += 2
        self.assertGreaterEqual(len(nodes), 4)
        self.assertGreaterEqual(len(reads), 4)
        self.assertTrue(all(b - a >= 30 for a, b in zip(reads, reads[1:])), reads)
        with mock.patch.object(replica_spool.Client, "ready", side_effect=AssertionError("paused read")):
            result = orchestrate.status("placement", config_path=self.config,
                                        skip=lambda *a: "paused by budget")
        self.assertEqual(result["prefill_service"], {"error": "paused by budget"})
