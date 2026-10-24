"""Diagnostic-run mode: verified preset table, production render unchanged, non-production marking."""
import contextlib
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hyperloom import cli, config, orchestrate as orch
from hyperloom.config import ConfigError, parse
from hyperloom.render import render_processes
from tests import test_orchestrate, test_render
from tests.test_render import FIXTURES


REPO = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location("lease_diag_under_test", REPO / "node" / "hyperloom_lease.py")
lease = importlib.util.module_from_spec(loader)
loader.loader.exec_module(lease)


class PresetTableTests(unittest.TestCase):
    def test_every_diagnostic_is_cited_at_the_pin_and_cannot_touch_production_env(self):
        for kind in ("qwen-replicas", "ds41-pair"):
            data = config.preset(kind)
            self.assertTrue(data["diagnostics"])
            for name, row in data["diagnostics"].items():
                with self.subTest(kind=kind, name=name):
                    self.assertRegex(name, r"^[a-z0-9-]{1,40}$")
                    self.assertTrue(row["description"] and row["output"])
                    for source in row["source"]:
                        self.assertRegex(source, r"^[\w./-]+:\d+(-\d+)?@" + data["engine_sha"][:8] + "$")
                    for key, value in row["env"].items():
                        self.assertRegex(key, r"^STRIX_[A-Z0-9_]+$")
                        self.assertNotIn(key, data["env"])
                        self.assertNotIn(key, {knob["target"] for knob in data.get("advanced", {}).values()})
                        # File outputs only under the node's log directory.
                        if "/" in value or "{" in value:
                            self.assertTrue(value.startswith("{log_dir}/diag-{run}-{role}-"), value)


class DiagnosticRenderTests(unittest.TestCase):
    setUp = test_render.RenderTests.setUp
    def test_production_render_is_unchanged_without_diagnostics(self):
        for kind in ("qwen-replicas", "ds41-pair"):
            expected = json.loads((FIXTURES / (kind + ".golden.json")).read_text())
            for kwargs in ({}, {"diagnostics": ()}, {"diagnostics": [], "run": "abcdef012345"}):
                self.assertEqual([asdict(p) for p in render_processes(parse(self.obj), kind, **kwargs)], expected)

    def test_diagnostics_add_only_their_env_with_log_paths(self):
        cluster = parse(self.obj)
        base = render_processes(cluster, "ds41-pair")
        diag = render_processes(cluster, "ds41-pair", diagnostics=["rank1-prefill-log", "dist-latency-trace"],
                                run="abcdef012345")
        for rank, (plain, traced) in enumerate(zip(base, diag)):
            self.assertEqual(plain.argv, traced.argv)
            added = {k: v for k, v in traced.env.items() if k not in plain.env}
            self.assertEqual({k: traced.env[k] for k in plain.env}, plain.env)
            root = cluster.machines[plain.machine_id].install_root
            self.assertEqual(added, {"STRIX_DS41_SERVE_PREFILL_LOG": "1", "STRIX_DIST_LATENCY_TRACE":
                                     f"{root}/log/diag-abcdef012345-rank{rank}-dist-latency"})
            lease.environment(traced.env)  # node env rules still apply

    def test_unknown_or_cross_preset_names_and_missing_run_are_refused(self):
        cluster = parse(self.obj)
        for names in (["nope"], ["rank1-prefill-log"]):
            with self.subTest(names=names), self.assertRaisesRegex(ConfigError, "unknown diagnostic"):
                render_processes(cluster, "qwen-replicas", diagnostics=names, run="abcdef012345")
        with self.assertRaisesRegex(ConfigError, "run id"):
            render_processes(cluster, "qwen-replicas", diagnostics=["prefill-timing"])


class DiagnosticUpTests(unittest.TestCase):
    # Reuse the fixture only; inheriting would rerun every orchestration test.
    setUp, local = test_orchestrate.OrchestrationTests.setUp, test_orchestrate.OrchestrationTests.local

    def processes(self, cluster, profile, **kwargs):
        self.render_kwargs = kwargs
        return test_orchestrate.OrchestrationTests.processes(self, cluster, profile)

    def test_up_marks_state_leases_and_status_non_production(self):
        state = orch.up(self.cluster, "qwen-replicas", diagnostics=["prefill-timing", "prefill-timing"])
        self.assertEqual((state["production"], state["diagnostic"]), (False, ["prefill-timing"]))
        self.assertEqual(self.render_kwargs["diagnostics"], ["prefill-timing"])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{12}", self.render_kwargs["run"]))
        leases = [payload for _, verb, payload in self.calls if verb == "lease"]
        self.assertTrue(leases and all(p["diagnostic"] == ["prefill-timing"] for p in leases))
        status = orch.status("qwen-replicas")
        self.assertEqual((status["production"], status["diagnostic"]), (False, ["prefill-timing"]))

    def test_production_up_sends_no_diagnostic_field(self):
        state = orch.up(self.cluster, "qwen-replicas")
        self.assertEqual((state["production"], state["diagnostic"]), (True, []))
        self.assertEqual(self.render_kwargs, {})
        self.assertFalse(any("diagnostic" in p for _, verb, p in self.calls if verb == "lease"))

    def test_unknown_diagnostic_fails_before_remote_work(self):
        with self.assertRaisesRegex(ValueError, "unknown diagnostic"):
            orch.up(self.cluster, "qwen-replicas", diagnostics=["dist-latency-trace"])
        self.assertEqual(self.calls, [])

    def test_cli_passes_repeatable_flag(self):
        with patch.object(cli, "_cluster", return_value=self.cluster), \
                patch.object(orch, "up", return_value={}) as up, patch("builtins.print"):
            self.assertEqual(cli.main(["up", "qwen-replicas", "--diagnostic", "prefill-timing",
                                       "--diagnostic", "cycle-timing"]), 0)
        self.assertEqual(up.call_args.kwargs["diagnostics"], ["prefill-timing", "cycle-timing"])


class NodeLeaseDiagnosticTests(unittest.TestCase):
    def test_lease_spec_diagnostic_names_are_bounded(self):
        spec = {"root": "/opt/root", "lab_dir": None, "label": "hl-qwen-x", "kind": "none",
                "processes": [{"name": "engine", "argv": ["/opt/root/bin/x"], "ready": "http-health",
                               "health_url": "http://127.0.0.1:18501/health"}]}
        if not Path("/opt/root").is_absolute():
            self.skipTest("POSIX paths")
        self.assertEqual(lease.validate_spec(dict(spec, diagnostic=["prefill-timing"]))["diagnostic"], ["prefill-timing"])
        for bad in ([], "prefill-timing", ["Bad Name"], ["x"] * 17):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lease.validate_spec(dict(spec, diagnostic=bad))

    def test_peer_lease_record_is_marked_non_production(self):
        child = SimpleNamespace(proc={"name": "rank1"}, process=SimpleNamespace(pid=7, returncode=None))
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "lease.json"
            with (patch.object(lease, "state_lock", lambda *_: contextlib.nullcontext()),
                  patch.object(lease, "record_path", lambda *_: record)):
                lease.peer_state(Path(tmp), {"lease": "a1", "diagnostic": ["dist-latency-trace"]}, child)
                state = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual((state["production"], state["diagnostic"]), (False, ["dist-latency-trace"]))
                record.unlink()
                lease.peer_state(Path(tmp), {"lease": "a1"}, child)
                self.assertNotIn("production", json.loads(record.read_text(encoding="utf-8")))
        with self.assertRaises(ValueError):
            lease.diagnostic_names(["Bad Name"])


if __name__ == "__main__":
    unittest.main()
