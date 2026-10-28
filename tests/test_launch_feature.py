"""Launch evidence, historical estimates and read-only API dispatch."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from hyperloom.recipes import service
from hyperloom.web.api import API, APIError, redact
from hyperloom.web.features import launch


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = API(self.directory / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       audit_path=self.directory / "audit.jsonl", clock=lambda: 150)
        self.job = {"job": "abc123", "action": "up", "arguments": {"profile": "qwen"},
                    "state": "running", "started_at": 100, "finished_at": None, "result": None, "error": None}
        self.api._jobs[self.job["job"]] = self.job
        self.profile = {"id": "qwen", "started_at": 102,
                        "configuration": {"nodes": ["node"]},
                        "leases": [{"machine": "node", "state": "starting"}],
                        "processes": [{"machine": "node", "name": "replica", "state": "starting", "start_time": 110},
                                      {"machine": "local", "name": "router", "state": "ready",
                                       "start_time": 110, "ready_after_s": 20}]}
        self.api.cached_snapshot = mock.Mock(return_value={})

    def phases(self, progress=None, profile=None, events=()):
        doc = launch.derive(self.job, progress, profile, events, now=150)
        return doc, {p["id"]: p for p in doc["phases"]}

    def test_plain_up_loading_and_ready(self):
        doc, phases = self.phases({"phase": "launching"}, self.profile)
        self.assertEqual([p["id"] for p in doc["phases"]], ["checks", "stopping", "lease", "processes", "loading", "ready"])
        self.assertEqual([phases[k]["state"] for k in ("checks", "stopping", "lease", "processes", "loading")],
                         ["done", "skipped", "done", "done", "running"])
        self.assertEqual(doc["elapsed_s"], 50)
        self.profile["processes"][0].update(state="ready", ready_after_s=30)
        doc, phases = self.phases({"phase": "launching"}, self.profile)
        self.assertEqual(phases["ready"]["state"], "done")
        self.assertEqual(phases["loading"]["finished_at"], 140)

    def test_deploy_names_stops(self):
        self.job.update(action="recipes/deploy")
        _, phases = self.phases({"phase": "stopping", "profile": "qwen", "stopped": ["old"], "stopping": "older"})
        self.assertEqual(phases["stopping"]["state"], "running")
        self.assertEqual(phases["stopping"]["detail"], {"stopped": [], "requested": [], "stopping": "older", "stop_states": {}})
        self.assertEqual(phases["loading"]["state"], "pending")
        _, phases = self.phases({"phase": "launching", "stopped": ["old", "older"],
                                   "stop_states": {"old": "stopped", "older": "stopped"}}, self.profile)
        self.assertEqual(phases["stopping"]["state"], "done")

    def test_deploy_result_preserves_stops_without_progress(self):
        self.job.update(action="recipes/deploy", state="succeeded", finished_at=145,
                        result={"profile": "qwen", "stopped": [{"profile": "old", "down": {"state": "stopped"}}],
                                "up": {"phase": "running", "profile": "qwen"}})
        _, phases = self.phases()
        self.assertEqual(phases["stopping"]["state"], "done")
        self.assertEqual(phases["stopping"]["detail"]["stopped"], ["old"])
        self.job["result"].pop("stopped")
        _, phases = self.phases()
        self.assertEqual(phases["stopping"]["state"], "unknown")
        self.assertIn("why", phases["stopping"])

    def test_poisoned_process_and_timed_captured_event(self):
        self.profile["processes"][0].update(state="poisoned", reason="bad weights")
        doc, phases = self.phases({"phase": "launching"}, self.profile)
        self.assertEqual(doc["failure"], {"phase": "loading", "reason": "bad weights"})
        self.assertEqual(phases["loading"]["state"], "failed")
        self.profile["processes"][0]["state"] = "starting"
        event = {"profile": "qwen", "process": "node", "message": json.dumps(
            {"event": "POISONED", "time": 125, "reason": "bad allocation"})}
        doc, _ = self.phases({"phase": "launching"}, self.profile, [event])
        self.assertEqual(doc["failure"]["reason"], "bad allocation")

    def test_ready_and_stopped_events(self):
        event = {"profile": "qwen", "process": "node", "event": "READY", "when": 130}
        _, phases = self.phases({"phase": "launching"}, self.profile, [event])
        self.assertEqual(phases["ready"]["state"], "done")
        self.assertEqual(phases["loading"]["finished_at"], 130)
        _, phases = self.phases({"phase": "launching"}, self.profile,
                                [event, dict(event, event="STOPPED", when=140)])
        self.assertEqual(phases["loading"]["state"], "unknown")
        self.assertIn("why", phases["loading"])

    def test_cancelled_before_handoff_and_during_load(self):
        self.job.update(state="failed", finished_at=145, error="up cancelled: nothing was started")
        doc, phases = self.phases({"phase": "failed", "launched": False})
        self.assertIsNone(doc["failure"]["phase"])
        self.assertIn("why", doc["failure"])
        self.assertEqual(phases["loading"]["state"], "skipped")
        self.job.update(state="cancelled", error=None)
        doc, phases = self.phases({"phase": "failed", "launched": True}, self.profile)
        self.assertEqual(doc["failure"], {"phase": "loading", "reason": "Launch cancelled"})
        self.assertEqual(phases["loading"]["state"], "failed")
        self.assertEqual(doc["elapsed_s"], 45)

    def test_missing_sources_unknown_with_why(self):
        _, phases = self.phases()
        for key in ("checks", "lease", "processes", "loading", "ready"):
            self.assertEqual(phases[key]["state"], "unknown")
            self.assertTrue(phases[key]["why"])
            self.assertIsNone(phases[key]["started_at"])
        self.job.update(state="failed", finished_at=140, error="refused")
        doc, _ = self.phases()
        self.assertEqual(doc["failure"]["reason"], "refused")
        self.assertIsNone(doc["failure"]["phase"])

    def test_old_and_later_profile_and_untimed_logs_not_attributed(self):
        for timestamp in (90, 200):
            with self.subTest(timestamp=timestamp):
                self.profile["started_at"] = timestamp
                _, phases = self.phases(profile=self.profile)
                self.assertEqual(phases["loading"]["state"], "unknown")
        event = {"profile": "qwen", "event": "POISONED", "when": None, "message": "POISONED old log"}
        doc, phases = self.phases(events=[event])
        self.assertIsNone(doc["failure"])
        self.assertIn("timestamp", phases["loading"]["why"])
        doc, _ = self.phases(events=[dict(event, when=99)])
        self.assertIsNone(doc["failure"])

    def test_success_without_snapshot(self):
        self.job.update(state="succeeded", finished_at=145, result={"up": {"profile": "qwen", "phase": "running"}})
        _, phases = self.phases()
        self.assertEqual(phases["ready"]["state"], "done")
        self.assertEqual(phases["loading"]["state"], "done")
        self.assertIsNone(phases["loading"]["started_at"])
        self.assertEqual(phases["processes"]["state"], "unknown")

    def test_estimate_empty_median_and_bounded_history(self):
        empty = launch.estimate(self.api, "qwen")
        self.assertEqual((empty["n"], empty["seconds"]), (0, None))
        self.assertTrue(empty["why"])
        for seconds in (30, 10, 20):
            launch.record_ready(self.api, self.job, {"profile": "qwen", "phase": "running", "release_id": "r1"}, 100 + seconds)
        eta = launch.estimate(self.api, "qwen")
        self.assertEqual((eta["seconds"], eta["n"], eta["min"], eta["max"]), (20, 3, 10, 30))
        self.assertEqual(eta["label"], "estimate from 3 previous starts on this portal")
        for seconds in range(25):
            launch.record_ready(self.api, self.job, {"up": {"profile": "qwen", "phase": "running"}}, 100 + seconds)
        self.assertEqual([r["seconds"] for r in launch.history(self.api)], list(range(5, 25)))

    def test_history_is_kept_per_configuration(self):
        # Two portal configurations in one directory share the history file but not their estimates (review round 5).
        other = API(self.directory / "cluster-b.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                    audit_path=self.directory / "audit-b.jsonl", clock=lambda: 150)
        self.assertEqual(launch.history_path(other), launch.history_path(self.api))
        launch.record_ready(self.api, self.job, {"profile": "qwen", "phase": "running"}, 130)
        self.assertEqual(launch.estimate(self.api, "qwen")["n"], 1)
        self.assertEqual(launch.estimate(other, "qwen")["n"], 0)
        for seconds in range(25):
            launch.record_ready(other, self.job, {"profile": "qwen", "phase": "running"}, 100 + seconds)
        # The other configuration's 20 kept rows do not evict this one's observation.
        self.assertEqual(launch.estimate(self.api, "qwen")["n"], 1)
        self.assertEqual(launch.estimate(other, "qwen")["n"], 20)

    def test_failed_nonlaunch_and_invalid_results_not_recorded(self):
        for result in (None, {"phase": "failed"}, {"state": "running"}, {"up": []}):
            launch.record_ready(self.api, self.job, result, 150)
        launch.record_ready(self.api, dict(self.job, action="down"), {"phase": "running", "profile": "qwen"}, 150)
        self.assertFalse(launch.history_path(self.api).exists())

    def test_get_is_read_only_even_for_success(self):
        self.job.update(state="succeeded", finished_at=140, result={"profile": "qwen", "phase": "running"})
        with mock.patch.object(service, "read_progress", return_value=None), \
                mock.patch.object(launch, "record_ready", side_effect=AssertionError("GET wrote history")), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("GET created directory")), \
                mock.patch.object(Path, "write_text", side_effect=AssertionError("GET wrote file")):
            self.assertEqual(self.api.read("launch/abc123")["eta"]["n"], 0)
            self.assertEqual(len(self.api.read("launch")["launches"]), 1)
        self.assertFalse(launch.history_path(self.api).exists())

    def test_dispatch_id_validation_unknown_and_queries(self):
        for path, query, status in (("launch/deadbeef", {}, 404), ("launch/xyz", {}, 400),
                                    ("launch/../file", {}, 400), ("launch/abc123", {"path": "x"}, 400),
                                    ("launch", {"job": "abc123"}, 400)):
            with self.subTest(path=path), self.assertRaises(APIError) as caught:
                self.api.read(path, query)
            self.assertEqual(caught.exception.status, status)
        self.api._jobs["aa"] = dict(self.job, job="aa", action="down")
        with self.assertRaises(APIError) as caught:
            self.api.read("launch/aa")
        self.assertEqual(caught.exception.status, 404)

    def test_config_key_falls_back_when_orchestrate_has_no_key(self):
        import hashlib
        import os
        expected = hashlib.sha256(os.path.normcase(os.path.abspath(str(self.api.config_path))).encode("utf-8")).hexdigest()[:16]
        with mock.patch("hyperloom.orchestrate._config_key", return_value=None):
            self.assertEqual(launch.config_key(self.api), expected)
            self.assertEqual(launch.estimate(self.api, "qwen")["n"], 0)

    def test_latest_reads_history_once_and_skips_superseded_finished_jobs(self):
        self.job.update(state="failed", finished_at=105)
        for i in range(20):
            jid = format(i, "x")
            self.api._jobs[jid] = dict(self.job, job=jid, started_at=110 + i)
        with mock.patch.object(launch, "read_history", return_value=([], None)) as history, mock.patch.object(service, "read_progress", return_value=None) as progress:
            doc = self.api.read("launch")
        self.assertEqual([r["job"] for r in doc["launches"]], ["13"])
        history.assert_called_once_with(self.api)
        self.assertEqual(progress.call_count, 1)

    def test_latest_skips_failed_deploys_named_only_by_progress(self):
        # A failed recipes/deploy names its profile only in its progress file: read once, then remembered.
        launch._resolved.clear()
        self.api._jobs.clear()
        for i in range(20):
            jid = format(i, "x")
            self.api._jobs[jid] = {"job": jid, "action": "recipes/deploy", "arguments": {"inputs": "d" * 64, "stop": []},
                                   "state": "failed", "started_at": 110 + i, "finished_at": 140, "result": None, "error": "x"}
        progress = {"phase": "failed", "profile": "qwen", "stopped": []}
        with mock.patch.object(launch, "read_history", return_value=([], None)),                 mock.patch.object(service, "read_progress", return_value=progress) as read:
            self.api.read("launch")
            first = read.call_count
            self.api._expires = 0
            doc = self.api.read("launch")
        self.assertEqual([r["job"] for r in doc["launches"]], ["13"])
        self.assertEqual(read.call_count - first, 1)

    def test_latest_skips_job_evicted_between_roster_and_detail(self):
        self.api._jobs["bcd"] = dict(self.job, job="bcd", arguments={"profile": "other"})
        real = launch.launch
        def evict(api, jid, query, **kw):
            if jid == "abc123":
                api._jobs.pop(jid, None)
            return real(api, jid, query, **kw)
        with mock.patch.object(launch, "launch", side_effect=evict), mock.patch.object(service, "read_progress", return_value=None):
            doc = self.api.read("launch")
        self.assertEqual([r["job"] for r in doc["launches"]], ["bcd"])

    def test_history_lock_does_not_block_recipe_lock(self):
        entered, release, recipe = threading.Event(), threading.Event(), threading.Event()
        path = self.directory / "history.lock"
        def hold():
            with service._file_lock(path, thread_lock=launch._history_lock):
                entered.set()
                release.wait(5)
        def transact():
            with service._file_lock(self.directory / "recipe.lock"):
                recipe.set()
        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(entered.wait(2))
            transaction = threading.Thread(target=transact)
            transaction.start()
            self.assertTrue(recipe.wait(2), "history held the recipe service lock")
            transaction.join(3)
        finally:
            release.set()
            holder.join(3)

    def test_latest_and_redaction(self):
        self.job.update(state="failed", error="password=hunter2", finished_at=140)
        self.api._jobs["bcd"] = dict(self.job, job="bcd", started_at=110)
        self.api._jobs["def"] = dict(self.job, job="def", arguments={"profile": "other"})
        with mock.patch.object(service, "read_progress", return_value=None):
            doc = self.api.read("launch")
            self.assertEqual({d["job"] for d in doc["launches"]}, {"bcd", "def"})
            self.assertTrue(all(row["failure"]["reason"] == redact("password=hunter2") for row in doc["launches"]))
            self.assertEqual(self.api.read("launch/abc123")["failure"]["reason"], redact("password=hunter2"))

    def test_corrupt_progress_and_history(self):
        launch.history_path(self.api).parent.mkdir(parents=True)
        launch.history_path(self.api).write_text("invalid", encoding="utf-8")
        with mock.patch.object(service, "read_progress", side_effect=service.RecipeError("runtime-dir/private/launch.json")):
            doc = self.api.read("launch/abc123")
        self.assertEqual(doc["why"]["progress"], "launch progress is unavailable or unreadable")
        self.assertNotIn("runtime-dir/private", json.dumps(doc))
        self.assertEqual(doc["eta"]["n"], 0)

    def test_history_global_cap_and_only_used_fields(self):
        path = launch.history_path(self.api)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"config": str(i), "profile": "qwen", "seconds": i,
                 "release": "unused", "identity": {"unused": True}} for i in range(520)]
        path.write_text(json.dumps(rows), encoding="utf-8")
        launch.record_ready(self.api, self.job, {"profile": "qwen", "phase": "running"}, 150)
        kept = launch.history(self.api)
        self.assertEqual(len(kept), 500)
        self.assertEqual(kept[0]["config"], "21")
        self.assertTrue(all(set(row) == {"config", "profile", "seconds"} for row in kept))

    def test_history_hook_runs_only_in_job_worker(self):
        from tests.test_web_api import fixture, wait_job
        self.api.orchestrate.load_cluster.return_value = fixture()
        self.api.orchestrate._profile.return_value = ({}, "qwen-replicas", ["halo-1"])
        threads = []
        recorded = threading.Event()
        worker = threading.current_thread()
        def observe(api, job, result, finished):
            self.assertEqual(job["state"], "succeeded")
            self.assertIsNotNone(job["finished_at"])
            self.assertFalse(api._job_lock.locked())
            self.assertTrue(api.audit_path.exists())
            threads.append(threading.current_thread())
            recorded.set()
        for action in ("up", "recipes/deploy"):
            with self.subTest(action=action), \
                    mock.patch.object(self.api, "_hold"), \
                    mock.patch.object(self.api, "_unhold"), \
                    mock.patch.object(self.api, "_resources_of_profile", return_value=set()), \
                    mock.patch.object(self.api, "_execute", return_value={"phase": "running", "profile": "qwen"}), \
                    mock.patch.object(launch, "record_ready", side_effect=observe),                     mock.patch("hyperloom.recipes.preflight.cli", return_value=None):
                recorded.clear()
                # Validation and admission are covered separately; this checks the real common worker.
                with mock.patch.object(self.api, "_arguments", return_value={"profile": "qwen"} if action == "up" else {"inputs": "a" * 64, "stop": []}):
                    job = self.api.mutate(action, {"profile": "qwen"})
                    wait_job(self.api, job)
                    self.assertTrue(recorded.wait(5))
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread is not worker for thread in threads))


if __name__ == "__main__":
    unittest.main()
