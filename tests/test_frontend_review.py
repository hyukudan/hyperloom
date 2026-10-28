"""Regression coverage for the frontend review boundaries and launch evidence."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom.web.features import launch, results
from hyperloom.winproc import no_window
from tests import test_launch_feature


class LaunchReviewTests(unittest.TestCase):
    setUp = test_launch_feature.LaunchTests.setUp
    phases = test_launch_feature.LaunchTests.phases
    def test_engines_ready_wait_for_router_and_prefill(self):
        self.profile["processes"][0].update(state="ready", ready_after_s=20)
        self.profile["configuration"]["prefill_service"] = {"machine": "local"}
        router = self.profile["processes"].pop()
        for extra in ([], [router]):
            self.profile["processes"] = self.profile["processes"][:1] + extra
            _, phases = self.phases({"phase": "launching"}, self.profile)
            self.assertEqual(phases["loading"]["state"], "running")
            self.assertEqual(phases["ready"]["state"], "pending")
        self.profile["processes"].append({"machine": "local", "name": "prefill", "state": "ready", "start_time": 120})
        _, phases = self.phases({"phase": "launching"}, self.profile)
        self.assertEqual(phases["ready"]["state"], "done")

    def test_nullable_unavailable_snapshot_retains_job_evidence(self):
        self.job.update(state="succeeded", finished_at=140, result={"phase": "running", "profile": "qwen"})
        with mock.patch.object(self.api, "_cluster", side_effect=ValueError("invalid configuration")):
            self.api.cached_snapshot.return_value = self.api._snapshot()
        self.assertIsNone(self.api.cached_snapshot.return_value["profiles"]["profiles"])
        self.assertIsNone(self.api.cached_snapshot.return_value["logs"]["entries"])
        with mock.patch.object(launch.service, "read_progress", return_value=None):
            doc = self.api.read("launch/abc123")
            latest = self.api.read("launch")
        self.assertEqual(doc["phases"][-1]["state"], "done")
        self.assertIn("profiles", doc["why"])
        self.assertIn("logs", doc["why"])
        self.assertEqual(len(latest["launches"]), 1)

    def test_requested_stop_progress_and_completed_result(self):
        self.job["action"] = "recipes/deploy"
        _, phases = self.phases({"phase": "launching", "stopped": ["old"],
                                  "stop_states": {"old": "stop requested"}}, self.profile)
        self.assertEqual(phases["stopping"]["state"], "running")
        self.assertEqual(phases["stopping"]["detail"]["requested"], ["old"])
        self.assertEqual(phases["stopping"]["detail"]["stopped"], [])
        self.job.update(state="succeeded", finished_at=145, result={"up": {"phase": "running"},
            "stopped": [{"profile": "old", "down": {"state": "stop-requested"}}]})
        _, phases = self.phases()
        self.assertEqual(phases["stopping"]["state"], "unknown")
        self.assertEqual(phases["stopping"]["detail"]["stop_states"]["old"], "stop requested")
        self.job["result"]["stopped"][0]["down"]["state"] = "stopped"
        self.assertEqual(self.phases()[1]["stopping"]["state"], "done")
        self.job["arguments"]["stop"] = ["old", "older"]
        self.assertEqual(self.phases()[1]["stopping"]["state"], "unknown")

    def test_history_sharing_retry_and_unreadable_preserved(self):
        launch.record_ready(self.api, self.job, {"phase": "running", "profile": "qwen"}, 120)
        path = launch.history_path(self.api)
        original_read, original_replace = Path.read_text, Path.replace
        failures = {"read": 1, "replace": 1}
        def read(p, *a, **kw):
            if p == path and failures["read"]:
                failures["read"] -= 1
                raise PermissionError("sharing")
            return original_read(p, *a, **kw)
        def replace(p, target):
            if failures["replace"]:
                failures["replace"] -= 1
                raise PermissionError("sharing")
            return original_replace(p, target)
        with mock.patch.object(Path, "read_text", read), mock.patch.object(Path, "replace", replace):
            launch.record_ready(self.api, self.job, {"phase": "running", "profile": "qwen"}, 130)
        self.assertEqual(len(launch.history(self.api)), 2)
        old = path.read_bytes()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("sharing")), \
                mock.patch("time.sleep"):
            answer = launch.record_ready(self.api, self.job, {"phase": "running", "profile": "qwen"}, 140)
            self.assertIn("unreadable", answer["why"])
            self.assertIn("unreadable", launch.estimate(self.api, "qwen")["why"])
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        with mock.patch.object(Path, "replace", side_effect=PermissionError("sharing")), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                launch.record_ready(self.api, self.job, {"phase": "running", "profile": "qwen"}, 140)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_history_concurrent_process_writers(self):
        code = """import sys, time
from pathlib import Path
from types import SimpleNamespace
from hyperloom.web.features import launch
api = SimpleNamespace(config_path=Path(sys.argv[1]))
while not Path(sys.argv[2]).exists(): time.sleep(.01)
for i in range(8):
 launch.record_ready(api, {'action':'up', 'started_at':100, 'arguments':{}},
                    {'phase':'running', 'profile':sys.argv[3]}, 120+i)
"""
        barrier = self.directory / "go"
        children = [subprocess.Popen([sys.executable, "-c", code, str(self.api.config_path), str(barrier), name],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window())
                    for name in ("a", "b", "c")]
        barrier.touch()
        for child in children:
            _, error = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, error.decode())
        rows = launch.history(self.api)
        self.assertEqual({name: sum(r["profile"] == name for r in rows) for name in ("a", "b", "c")},
                         {"a": 8, "b": 8, "c": 8})
        self.assertFalse(list(launch.history_path(self.api).parent.glob("*.tmp")))


class ConfigurationReviewTests(unittest.TestCase):
    def test_published_survives_missing_and_invalid_configuration(self):
        from hyperloom.web.api import API
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            api = API(path, audit_path=Path(directory) / "audit.jsonl")
            for contents in (None, "invalid"):
                if contents is not None:
                    path.write_text(contents, encoding="utf-8")
                doc = results.results(api, {})
                self.assertTrue(doc["published"]["models"])
                self.assertEqual(doc["live"], [])
                self.assertIn("configuration", doc["why"]["live"])


class SourceReviewTests(unittest.TestCase):
    def test_committed_counting_basis_notes(self):
        rows = [r for m in results.published()[0]["models"] for r in m["rows"]]
        wall = [r for r in rows if r["best"] in (52.4, 91.3, 156.8)]
        self.assertEqual(len(wall), 3)
        for row in wall:
            self.assertIn("wall aggregate", row["note"].lower())
        for row in rows:
            if row["metric"] == "aggregate_tok_s" and row["mode"].startswith("Server, two replicas"):
                self.assertIn("wall", row["note"].lower())
            if row["metric"] == "aggregate_tok_s" and row["best"] in (111.9, 61.4):
                self.assertIn("calculated", row["note"].lower())
            if row["mode"] == "Server, summed per-request decode":
                self.assertIn("Sum of completion tokens / each request decode time", row["note"])
                self.assertIn("excludes prompt and queueing", row["note"])

    def test_counting_basis_against_optional_readme(self):
        path = Path(__file__).resolve().parents[1] / ".frontend-src" / "engine-README.md"
        if not path.exists():
            self.skipTest("source README is not available")
        text = path.read_text(encoding="utf-8")
        for needle in ("52.4 / 91.3 / 156.8 tok/s** wall aggregate", "111.9 tok/s aggregate, calculated",
                       "61.4 tok/s aggregate**, calculated", "68.2 tok/s** wall", "136.4 tok/s** wall",
                       "146.8 tok/s** wall", "158.1 tok/s** wall",
                       "90.7 / 181.5 / 201.6 / 255.0 tok/s** summed per-request decode"):
            self.assertIn(needle, text)
        self.test_committed_counting_basis_notes()

    def test_branch_added_files_have_no_numeric_ip_literals(self):
        import ipaddress
        root = Path(__file__).resolve().parents[1]
        # Explicit branch inventory: this test also runs without repository history.
        names = (
          "hyperloom/adapters/sessions.py",
          "hyperloom/data/results.json",
          "hyperloom/web/features/launch.py",
          "hyperloom/web/features/results.py",
          "hyperloom/web/features/sessioncache.py",
          "hyperloom/web/static/pages/launch.css",
          "hyperloom/web/static/pages/launch.js",
          "hyperloom/web/static/pages/results.css",
          "hyperloom/web/static/pages/results.js",
          "hyperloom/web/static/pages/sessioncache.css",
          "hyperloom/web/static/pages/sessioncache.js",
          "tests/fixtures/frontend/polling.js",
          "tests/fixtures/sessioncache/README.md",
          "tests/fixtures/sessioncache/deepseek.json",
          "tests/fixtures/sessioncache/minimax.json",
          "tests/fixtures/sessioncache/qwen.json",
          "tests/test_api_docs.py",
          "tests/test_frontend_review.py",
          "tests/test_launch_feature.py",
          "tests/test_results_feature.py",
          "tests/test_sessioncache.py",
          "tests/ui/seeds/sessioncache.py"
)
        for name in names:
            text = (root / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text), name)
            for token in re.findall(r"[0-9A-Fa-f:.%]+", text):
                if ":" not in token:
                    continue
                try:
                    address = ipaddress.ip_address(token)
                except ValueError:
                    continue
                self.fail(f"{name}: numeric IPv{address.version} literal")


class PollingReviewTests(unittest.TestCase):
    def test_restart_eviction_and_timeline_failure_do_not_gate_actions(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "tests/fixtures/frontend/polling.js"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

class RenderReviewTests(unittest.TestCase):
    def test_sessioncache_renders_configuration_unavailable_before_empty(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const H = {state: {docs: {}, errors: {}}, esc: s => String(s),
           card: (title, body) => title + body, unavailable: why => 'Unavailable: ' + why};
const context = {window: {Hyperloom: H}};
vm.runInNewContext(fs.readFileSync('hyperloom/web/static/pages/sessioncache.js', 'utf8'), context);
const render = () => context.window.HyperloomPages.sessioncache.render();
H.state.docs.sessioncache = {profiles: [], why: {profiles: 'configuration unavailable'}};
assert(render().includes('Unavailable: configuration unavailable'));
assert(!render().includes('No running profiles collected yet.'));
H.state.docs.sessioncache = {profiles: [], why: {}};
assert(render().includes('No running profiles collected yet.'));
"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
