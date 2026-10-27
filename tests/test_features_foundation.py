"""Feature registry, API dispatch, page assets and the history feature (the portal foundation)."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import admission, cliext, telemetry
from hyperloom.telemetry.store import Store
from hyperloom.web import features, server
from hyperloom.web.api import API, APIError, JobFailed


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class RegistryTests(unittest.TestCase):
    def test_names_are_checked_and_unique(self):
        reg = features.Registry()
        reg.get("overview", lambda api, q: {})
        with self.assertRaises(ValueError):
            reg.get("overview", lambda api, q: {})
        with self.assertRaises(ValueError):
            reg.post("Bad Name", lambda api, a: {})
        with self.assertRaises(ValueError):
            reg.get_prefix("history", lambda api, rest, q: {})
        reg.get_prefix("history/", lambda api, rest, q: {"rest": rest})
        handler, rest = reg.find_get("history/gpu_busy")
        self.assertEqual(rest, "gpu_busy")
        self.assertEqual(reg.find_get("history/"), (None, None))
        with self.assertRaises(ValueError):
            reg.collector("x", lambda api, now: [], interval=0.1)

    def test_reserved_names_and_longest_prefix(self):
        reg = features.Registry()
        for name in ("devices", "jobs", "recipes/x", "up", "settings/x"):
            with self.assertRaises(ValueError, msg=name):
                reg.get(name, lambda api, q: {})
        reg.get_prefix("bench/", lambda api, rest, q: "short")
        reg.get_prefix("bench/runs/", lambda api, rest, q: "long")
        reg.get("bench/runs/latest", lambda api, q: "exact")
        self.assertEqual(reg.find_get("bench/runs/7")[1], "7")
        self.assertEqual(reg.find_get("bench/runs/7")[0](None, None, None), "long")
        self.assertEqual(reg.find_get("bench/other")[0](None, None, None), "short")
        self.assertEqual(reg.find_get("bench/runs/latest")[0](None, None), "exact")
        with self.assertRaises(ValueError):
            reg.job("bench/run", lambda a, b, c: {}, admission="bench")     # admitted jobs name resources

    def test_failed_registration_leaves_nothing_behind(self):
        module = mock.Mock()

        def register(reg):
            reg.get("half", lambda api, q: {})
            reg.collector("half", lambda api, now: [])
            raise RuntimeError("late failure")
        module.register = register
        with mock.patch("importlib.import_module", return_value=module):
            reg = features.load(("overview",))
        self.assertEqual((reg.gets, reg.collectors, reg.loaded), ({}, {}, []))
        self.assertIn("late failure", reg.failed["overview"])

    def test_conflicting_feature_is_rejected_whole(self):
        first, second = mock.Mock(), mock.Mock()
        first.register = lambda reg: reg.get("shared", lambda api, q: 1)
        second.register = lambda reg: (reg.get("mine", lambda api, q: 2), reg.get("shared", lambda api, q: 3))
        with mock.patch("importlib.import_module", side_effect=[first, second]):
            reg = features.load(("overview", "machines"))
        self.assertEqual(sorted(reg.gets), ["shared"])
        self.assertIn("registered twice", reg.failed["machines"])

    def test_broken_feature_is_reported_not_fatal(self):
        with mock.patch("importlib.import_module", side_effect=[RuntimeError("boom")]):
            reg = features.load(("overview",))
        self.assertEqual(reg.loaded, [])
        self.assertIn("boom", reg.failed["overview"])

    def test_missing_feature_module_is_skipped(self):
        reg = features.load(("no-such-feature-module",))
        self.assertEqual((reg.loaded, reg.failed), ([], {}))


class APIDispatchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        reg = features.Registry()
        reg.get("demo", lambda api, q: api.document("demo", value=q.get("x"), token="secret-value"))
        reg.get_prefix("demo/", lambda api, rest, q: {"rest": rest})
        reg.get("missing", lambda api, q: (_ for _ in ()).throw(LookupError("no such thing")))
        reg.post("demo/echo", lambda api, a: {"echo": a}, audited=True)
        reg.job("demo/job", lambda api, a, progress: (progress("half"), {"done": a["n"]})[1],
                validate=lambda api, a: a if isinstance(a.get("n"), int) else (_ for _ in ()).throw(ValueError("n required")))
        reg.job("demo/exclusive", lambda api, a, progress: time.sleep(0.2) or {}, exclusive=True)

        def bench(api, a, ctx):
            while not ctx.wait(0.01):
                pass
            return None
        reg.job("demo/bench", bench, admission="bench", resources=lambda api, a: set(a["on"]), deadline_s=30)
        reg.job("demo/short", lambda api, a, ctx: (ctx.wait(5), {"cancelled": ctx.reason})[1],
                admission="bench", resources=lambda api, a: {"x"}, deadline_s=1)
        reg.job("demo/leak", lambda api, a, ctx: (_ for _ in ()).throw(RuntimeError("password=hunter2 Bearer abc")))
        reg.job("demo/exclusive-on", lambda api, a, ctx: {}, exclusive=True, resources=lambda api, a: {"halo-1"})

        def definite(api, a, ctx):
            while not ctx.wait(0.01):
                pass
            raise JobFailed("node refused the change", outcome=a.get("outcome"))
        reg.job("demo/definite", definite, admission="bench", resources=lambda api, a: {"halo-3"}, deadline_s=30)
        reg.capability("demo", False, "needs data")
        features.reset(reg)
        self.addCleanup(features.reset)
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.api.orchestrate.down.return_value = {"stopped": True}
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)

    def test_token_data_explanations_and_series_links_survive_redaction(self):
        from hyperloom.web.api import redact
        document = {"generated_tokens": {"delta": None, "unobserved_s": 3.0, "uncertain": False, "stale": True},
                    "why": {"computed_tokens": "not reported by this engine build", "x": "token=abc123"},
                    "series": {"engine_generated_tokens": "history/engine_generated_tokens?profile=qwen"},
                    "refresh_tokens": {"a": "ghp_abc"}, "github_tokens": "ghp_abc", "token_ids": [1, 2],
                    "prompt_token_ids": [3], "tokens": [4, 5], "access_tokens": [12345]}
        self.assertEqual(redact(document), dict(document, why={"computed_tokens": "not reported by this engine build",
                                                               "x": "token=[redacted]"},
                                                refresh_tokens="[redacted]", github_tokens="[redacted]",
                                                access_tokens="[redacted]"))
        for text in (True, False):
            for name in ("why", "series"):
                self.assertEqual(redact({name: {"api_key": "opaque", "password_tokens": "x y", "access_token": "z",
                                                "ok": "fine"}}, text),
                                 {name: {"api_key": "[redacted]", "password_tokens": "[redacted]",
                                         "access_token": "[redacted]", "ok": "fine"}})

    def test_get_documents_are_redacted_and_errors_mapped(self):
        document = self.api.read("demo", {"x": "1"})
        self.assertEqual((document["schema"], document["value"], document["token"]), ("hyperloom/ui-demo-v1", "1", "[redacted]"))
        self.assertEqual(self.api.read("demo/a/b", {}), {"rest": "a/b"})
        with self.assertRaises(APIError) as caught:
            self.api.read("missing", {})
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(APIError):
            self.api.read("nothing", {})

    def test_node_by_machine_id_and_cached_snapshot(self):
        from types import SimpleNamespace
        machine = SimpleNamespace(id="halo-1", access="ssh")
        self.api.orchestrate.load_cluster.return_value = SimpleNamespace(machines={"halo-1": machine})
        with mock.patch.object(self.api, "_node", return_value={"verb": "host"}) as node:
            self.assertEqual(self.api.node("halo-1", "host"), {"verb": "host"})
            node.assert_called_once_with(machine, "host", None)
            with self.assertRaises(LookupError):
                self.api.node("nope", "host")
        self.assertIsNone(self.api.cached_snapshot())

    def test_sync_post_is_audited(self):
        self.assertEqual(self.api.feature_post("demo/echo", {"a": 1}), (200, {"echo": {"a": 1}}))
        lines = (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["action"], "demo/echo")

    def test_jobs_validate_run_and_report_progress(self):
        with self.assertRaises(APIError):
            self.api.mutate("demo/job", {})
        job = self.api.mutate("demo/job", {"n": 3})
        for _ in range(100):
            current = self.api.read("jobs/" + job["id"])
            if current["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual((current["state"], current["result"]), ("succeeded", {"done": 3}))

    def wait(self, jid):
        for _ in range(500):
            current = self.api.read("jobs/" + jid)
            if current["state"] != "running":
                return current
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_admission_resources_cap_and_cancel(self):
        first = self.api.mutate("demo/bench", {"on": ["halo-1", "router"]})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/bench", {"on": ["halo-1"]})          # overlapping resources
        self.assertEqual(caught.exception.status, 409)
        second = self.api.mutate("demo/bench", {"on": ["halo-2"]})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/bench", {"on": ["workstation"]})     # lab-wide cap of two
        self.assertIn("at most 2", str(caught.exception))
        # A deploy over held resources is refused (resources unknown -> everything overlaps).
        with self.assertRaises(APIError) as caught:
            self.api.mutate("up", {"profile": "qwen"})
        self.assertIn("benchmark or demo holds", str(caught.exception))
        # Cancelling through the core cancel action reaches the feature job.
        cancel = self.api.mutate("cancel", {"job": first["id"]})
        self.assertEqual(self.wait(cancel["id"])["result"], {"cancelled": first["id"]})
        done = self.wait(first["id"])
        self.assertEqual((done["state"], done["error"]), ("cancelled", "cancelled by request"))
        # A stop of a profile cancels overlapping admitted jobs (priority: it never waits).
        self.api.mutate("down", {"profile": "qwen"})
        self.assertEqual(self.wait(second["id"])["state"], "cancelled")

    def test_a_definite_failure_survives_a_cancel_request(self):
        audit = Path(self.dir.name) / "audit.jsonl"
        for outcome, kept in ((None, None), ("unresolved", "unresolved"), ("x" * 25 + " password=abc", "invalid")):
            with self.subTest(outcome=outcome):
                job = self.api.mutate("demo/definite", {"outcome": outcome})
                self.wait(self.api.mutate("cancel", {"job": job["id"]})["id"])
                done = self.wait(job["id"])
                self.assertEqual((done["state"], done["error"], done.get("outcome")),
                                 ("failed", "node refused the change", kept))
                line = [json.loads(text) for text in audit.read_text().splitlines()
                        if json.loads(text)["result"].get("job") == job["id"]][-1]
                self.assertEqual((line["result"]["state"], line["result"]["error"], line["result"].get("outcome")),
                                 ("failed", "node refused the change", kept))

    def test_a_failed_audit_write_keeps_the_job_outcome(self):
        job = self.api.mutate("demo/definite", {"outcome": "unresolved"})
        with mock.patch.object(self.api, "_audit", side_effect=OSError("disk full")):
            with self.api._job_lock:
                self.api._features_running[job["id"]].cancel("cancelled by request")
            done = self.wait(job["id"])
        self.assertEqual((done["state"], done.get("outcome")), ("failed", "unresolved"))
        self.assertIn("audit write failed", done["error"])

    def test_admission_is_shared_across_portal_processes(self):
        other = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                    audit_path=Path(self.dir.name) / "audit2.jsonl")
        other.admission_db = self.api.admission_db          # the same database file in production
        first = self.api.mutate("demo/bench", {"on": ["halo-1"]})
        with self.assertRaises(APIError) as caught:
            other.mutate("demo/bench", {"on": ["halo-1"]})
        self.assertEqual(caught.exception.status, 409)
        # An exclusive feature job over held resources is refused too.
        with self.assertRaises(APIError):
            self.api.mutate("demo/exclusive-on", {})
        self.api.mutate("cancel", {"job": first["id"]})
        self.wait(first["id"])

    def test_a_running_stop_keeps_benchmarks_out(self):
        gate = threading.Event()
        self.api.orchestrate.down.side_effect = lambda *a, **kw: (gate.wait(5), {"stopped": True})[1]
        stop = self.api.mutate("down", {"profile": "qwen"})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/bench", {"on": ["halo-1"]})
        self.assertIn("stop", str(caught.exception))
        gate.set()
        self.wait(stop["id"])
        bench = self.api.mutate("demo/bench", {"on": ["halo-1"]})
        self.api.mutate("cancel", {"job": bench["id"]})
        self.wait(bench["id"])

    def test_a_profile_cancel_is_a_stop(self):
        bench = self.api.mutate("demo/bench", {"on": ["halo-1"]})
        gate = threading.Event()
        self.api.orchestrate.down.side_effect = lambda *a, **kw: (gate.wait(5), {"stopped": True})[1]
        stop = self.api.mutate("cancel", {"profile": "qwen"})
        self.assertEqual(self.wait(bench["id"])["state"], "cancelled")     # overlapping benchmarks are cancelled
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/bench", {"on": ["halo-1"]})               # and kept out while it runs
        self.assertIn("stop", str(caught.exception))
        gate.set()
        self.wait(stop["id"])
        self.assertEqual(self.api._holds, {})

    def test_an_unaudited_acceptance_starts_nothing_and_holds_nothing(self):
        for action, arguments in (("demo/bench", {"on": ["halo-1"]}), ("demo/exclusive", {}),
                                  ("down", {"profile": "qwen"})):
            with self.subTest(action=action):
                with mock.patch.object(self.api, "_audit", side_effect=OSError("disk full")):
                    with self.assertRaises(APIError) as caught:
                        self.api.mutate(action, arguments)
                self.assertEqual(caught.exception.status, 503)
                self.assertIn("not started", str(caught.exception))
                self.assertEqual((self.api._holds, self.api._active, self.api._features_running), ({}, None, {}))
                self.assertEqual(self.api.admission_db.holds(), [])
                self.assertFalse([j for j in self.api._jobs.values() if j["state"] == "running"])

    def test_cancel_takes_a_job_or_a_profile(self):
        bench = self.api.mutate("demo/bench", {"on": ["halo-1"]})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("cancel", {"job": bench["id"], "profile": "other"})
        self.assertIn("not both", str(caught.exception))
        self.wait(self.api.mutate("cancel", {"job": bench["id"]})["id"])
        self.wait(bench["id"])

    def test_an_admission_error_releases_the_slot(self):
        import sqlite3
        with mock.patch.object(self.api.admission_db, "acquire", side_effect=sqlite3.OperationalError("locked")):
            # (A down goes ahead without the admission: tests/test_web_api.py and tests/test_cli.py.)
            for action, arguments in (("demo/exclusive-on", {}), ("demo/bench", {"on": ["halo-1"]}),
                                      ("up", {"profile": "qwen"})):
                with self.subTest(action=action):
                    with self.assertRaises(APIError) as caught:
                        self.api.mutate(action, arguments)
                    self.assertEqual(caught.exception.status, 503)
                    self.assertEqual((self.api._active, self.api._holds), (None, {}))
        self.wait(self.api.mutate("demo/exclusive", {})["id"])             # the slot is free

    def test_a_reserved_slot_not_yet_listed_refuses_with_409(self):
        with self.api._job_lock:
            self.api._active = "reserved-not-listed"
        try:
            with self.assertRaises(APIError) as caught:
                self.api.mutate("demo/bench", {"on": ["halo-1"]})
            self.assertEqual(caught.exception.status, 409)
            self.assertIn("(starting)", str(caught.exception))
        finally:
            self.api._active = None

    def test_a_job_not_started_rolls_its_validation_back(self):
        reserved = set()

        def validate(api, a):
            if "machines" in a:
                raise ValueError("unknown argument machines")
            reserved.add(a["on"])
            return {"machines": [a["on"]]}

        def rollback(api, a):
            reserved.difference_update(a["machines"])
        reg = features.registry()
        reg.job("demo/reserving", lambda api, a, ctx: {}, validate=validate, exclusive=True, rollback=rollback)
        reserved.add("taken-by-another")
        # Not audited: the reservation is undone.
        with mock.patch.object(self.api, "_audit", side_effect=OSError("disk full")):
            with self.assertRaises(APIError):
                self.api.mutate("demo/reserving", {"on": "halo-1"})
        self.assertEqual(reserved, {"taken-by-another"})
        # Refused (the slot is taken): undone too.
        with self.api._job_lock:
            self.api._active = "other"
        try:
            with self.assertRaises(APIError):
                self.api.mutate("demo/reserving", {"on": "halo-1"})
        finally:
            self.api._active = None
        self.assertEqual(reserved, {"taken-by-another"})
        # A refused validation rolls nothing back (the client's arguments never reach the rollback).
        with self.assertRaises(APIError):
            self.api.mutate("demo/reserving", {"on": "x", "machines": ["taken-by-another"]})
        self.assertEqual(reserved, {"taken-by-another"})

    def test_a_feature_job_finishing_before_its_cancel_runs_is_never_a_stop(self):
        bench = self.api.mutate("demo/bench", {"on": ["halo-2"]})
        job = self.api.mutate("demo/job", {"n": 1})
        self.wait(job["id"])
        checked = self.api._arguments
        with mock.patch.object(self.api, "_arguments", side_effect=lambda a, b: b if a == "cancel" else checked(a, b)):
            cancel = self.api.mutate("cancel", {"job": job["id"]})     # as if it finished after the check
        done = self.wait(cancel["id"])
        self.assertEqual(done["state"], "failed")
        self.assertIn("has already succeeded", done["error"])
        self.assertEqual(self.api.read("jobs/" + bench["id"])["state"], "running")   # no stop was taken
        self.api.orchestrate.down.assert_not_called()
        self.wait(self.api.mutate("cancel", {"job": bench["id"]})["id"])
        self.wait(bench["id"])

    def test_a_feature_job_evicted_before_its_cancel_runs_is_never_a_stop(self):
        bench = self.api.mutate("demo/bench", {"on": ["halo-2"]})
        job = self.api.mutate("demo/job", {"n": 1})
        self.wait(job["id"])
        checked = self.api._arguments

        def evicting(action, arguments):
            if action == "cancel":
                with self.api._job_lock:
                    self.api._jobs.pop(job["id"], None)        # pruned by the 100-job limit meanwhile
                return arguments
            return checked(action, arguments)
        with mock.patch.object(self.api, "_arguments", side_effect=evicting):
            cancel = self.api.mutate("cancel", {"job": job["id"]})
        self.assertEqual(self.wait(cancel["id"])["state"], "failed")
        self.assertEqual(self.api.read("jobs/" + bench["id"])["state"], "running")   # no stop was taken
        self.api.orchestrate.down.assert_not_called()
        self.wait(self.api.mutate("cancel", {"job": bench["id"]})["id"])
        self.wait(bench["id"])

    def test_concurrent_holds_wait_for_the_renewal_thread(self):
        from hyperloom.web import api as api_module
        started, release = threading.Event(), threading.Event()
        real_start = api_module.threading.Thread.start

        def slow_failing_start(thread):
            if thread.name == "hyperloom-admission":
                started.set()
                release.wait(5)
                raise RuntimeError("no thread")
            return real_start(thread)
        results = []
        with mock.patch.object(api_module.threading.Thread, "start", slow_failing_start):
            first = threading.Thread(target=lambda: results.append(self._try(("demo/bench", {"on": ["halo-1"]}))))
            first.start()
            started.wait(5)
            second = threading.Thread(target=lambda: results.append(self._try(("demo/bench", {"on": ["halo-2"]}))))
            second.start()
            time.sleep(0.2)
            self.assertEqual(self.api._holds, {})       # the second hold is not published without a renewal thread
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(sorted(results), [503, 503])
        self.assertEqual((self.api._holds, self.api._heartbeat), ({}, None))
        self.assertEqual(self.api.admission_db.holds(), [])

    def _try(self, call):
        try:
            self.api.mutate(*call)
            return 202
        except APIError as exc:
            return exc.status

    def test_an_unreadable_cancellation_state_never_breaks_a_stop_or_the_renewals(self):
        import sqlite3
        from hyperloom.web.api import JobContext
        context = JobContext(30, self.api.admission_db, "hold-id")
        with mock.patch.object(self.api.admission_db, "cancelled", side_effect=sqlite3.OperationalError("locked")):
            self.assertFalse(context.cancelled())
            bench = self.api.mutate("demo/bench", {"on": ["halo-1"]})
            stop = self.wait(self.api.mutate("down", {"profile": "qwen"})["id"])
        self.assertEqual(stop["state"], "succeeded")
        self.assertEqual(self.wait(bench["id"])["state"], "cancelled")
        self.assertEqual(self.api._holds, {})

    def test_a_worker_that_cannot_start_releases_everything(self):
        from hyperloom.web import api as api_module
        with mock.patch.object(api_module.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            for action, arguments in (("demo/bench", {"on": ["halo-1"]}), ("down", {"profile": "qwen"})):
                with self.assertRaises(APIError) as caught:      # the renewal thread itself cannot start
                    self.api.mutate(action, arguments)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual((self.api._holds, self.api._active, self.api._heartbeat), ({}, None, None))
                self.assertEqual(self.api.admission_db.holds(), [])
        self.wait(self.api.mutate("down", {"profile": "qwen"})["id"])      # starts the renewal thread
        self.assertIsNotNone(self.api._heartbeat)
        for action, arguments in (("demo/bench", {"on": ["halo-1"]}), ("demo/exclusive", {}),
                                  ("down", {"profile": "qwen"})):
            with self.subTest(action=action):
                with mock.patch.object(api_module.threading.Thread, "start",
                                       side_effect=RuntimeError("can't start new thread")):
                    with self.assertRaises(APIError) as caught:
                        self.api.mutate(action, arguments)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual((self.api._holds, self.api._active, self.api._features_running), ({}, None, {}))
                self.assertEqual(self.api.admission_db.holds(), [])
                self.assertFalse([j for j in self.api._jobs.values() if j["state"] == "running"])

    def test_a_finished_feature_job_cancel_is_409(self):
        job = self.api.mutate("demo/job", {"n": 1})
        self.wait(job["id"])
        with self.assertRaises(APIError) as caught:
            self.api.mutate("cancel", {"job": job["id"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(str(caught.exception), f"job {job['id']} has already succeeded")

    def test_abandoned_portal_jobs_are_cancelled(self):
        from hyperloom.web import api as api_module
        with mock.patch.object(api_module, "OWNER_LEASE_S", 0.3):
            job = self.api.mutate("demo/bench", {"on": ["halo-2"]})
            self.assertEqual(self.api.renew_jobs({"jobs": [job["id"], "gone"]}),
                             (200, {"renewed": [job["id"]], "not_running": ["gone"]}))
            done = self.wait(job["id"])
        self.assertEqual(done["state"], "cancelled")
        self.assertIn("abandoned", done["error"])
        with self.assertRaises(APIError):
            self.api.renew_jobs({"jobs": "x"})

    def test_admitted_jobs_wait_for_core_jobs_and_meet_deadlines(self):
        exclusive = self.api.mutate("demo/exclusive", {})
        self.api._active = exclusive["id"]
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/bench", {"on": ["halo-1"]})
        self.assertIn("deploy, stop or install", str(caught.exception))
        self.wait(exclusive["id"])
        short = self.wait(self.api.mutate("demo/short", {})["id"])
        self.assertEqual(short["result"], {"cancelled": "deadline reached"})

    def test_job_errors_and_token_data_through_redaction(self):
        failed = self.wait(self.api.mutate("demo/leak", {})["id"])
        self.assertNotIn("hunter2", json.dumps(failed))
        self.assertNotIn("abc", failed["error"])
        from hyperloom.web.api import redact
        kept = redact({"prompt_tokens": 5, "tokens": 12, "token_ids": [1, 2, 3], "tokens_by_kind": {"cached": 4},
                       "a_token_ids": "gz:b64:H4sIAAAA", "tokenizer_id": "qwen3-bpe-151k", "token_count": 3,
                       "token": "s3cret", "api_key": 7, "csrf_token": 9, "tokens_note": "Bearer zz", "access_token": 12345,
                       "token_ids_raw": "not compressed ids", "tokenizer": "Bearer abc"})
        self.assertEqual((kept["prompt_tokens"], kept["tokens"], kept["token_ids"], kept["tokens_by_kind"],
                          kept["a_token_ids"], kept["tokenizer_id"], kept["token_count"]),
                         (5, 12, [1, 2, 3], {"cached": 4}, "gz:b64:H4sIAAAA", "qwen3-bpe-151k", 3))
        for key in ("token", "api_key", "csrf_token", "tokens_note", "access_token", "token_ids_raw", "tokenizer"):
            self.assertEqual(kept[key], "[redacted]", key)
        credentials = redact({"password_tokens": "plain-credential", "api_key_tokens": 42, "secret_tokens": [1],
                              "prompt_tokens": 7, "max_tokens": 64})
        self.assertEqual(credentials, {"password_tokens": "[redacted]", "api_key_tokens": "[redacted]",
                                       "secret_tokens": "[redacted]", "prompt_tokens": 7, "max_tokens": 64})

    def test_exclusive_jobs_take_the_single_slot(self):
        first = self.api.mutate("demo/exclusive", {})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/exclusive", {})
        self.assertEqual(caught.exception.status, 409)
        other = self.api.mutate("demo/job", {"n": 1})   # non-exclusive jobs still run
        self.assertEqual(first["state"], "running")
        self.wait(first["id"])
        self.wait(other["id"])


class PortalAssetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        static = Path(self.dir.name) / "static"
        (static / "pages").mkdir(parents=True)
        (static / "assets").mkdir()
        source = Path(server.__file__).with_name("static")
        for name in ("index.html", "app.css", "app.js", "recipes.js"):
            (static / name).write_bytes((source / name).read_bytes())
        (static / "pages" / "demo.js").write_text("window.x = 1;")
        (static / "pages" / "demo.css").write_text(".x{}")
        (static / "pages" / "index.html").write_text("<p>not a page</p>")
        (static / "assets" / "icon.svg").write_text("<svg/>")
        (static / "pages" / "unlisted.js").write_text("window.y = 1;")
        (static / "pages" / "manifest.json").write_text(json.dumps({"files": ["demo.css", "demo.js", "index.html", "../app.js"]}))
        (static / "assets" / "manifest.json").write_text(json.dumps({"files": ["icon.svg"]}))
        reg = features.Registry()
        reg.post("demo/echo", lambda api, a: {"echo": a})
        reg.job("demo/job", lambda api, a, p: {})
        features.reset(reg)
        self.addCleanup(features.reset)
        api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                  audit_path=Path(self.dir.name) / "audit.jsonl")
        self.server = server.create_server(0, api=api, static_dir=static)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()

    def post(self, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode()
        connection.request("POST", path, body=data, headers={
            "Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
            "X-Hyperloom-CSRF": self.server.csrf_token, "Content-Type": "application/json",
            "Content-Length": str(len(data))})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_pages_are_served_by_exact_name_and_injected_before_app_js(self):
        status, kind, body = self.get("/pages/demo.js")
        self.assertEqual((status, kind), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(self.get("/hyperloom/assets/icon.svg")[:2], (200, "image/svg+xml"))
        for path in ("/pages/index.html", "/pages/../app.js", "/pages/unlisted.js", "/pages/manifest.json",
                     "/pages/demo.js:stream", "/pages/demo.js::$DATA", "/pages/demo.js%5c",  "/pages/%2e%2e/app.js", "/pages/sub/demo.js",
                     "/pages/Demo.js", "/pages/", "/other/demo.js"):
            self.assertEqual(self.get(path)[0], 404, path)
        html = self.get("/")[2].decode()
        self.assertLess(html.index('/pages/demo.js'), html.index('/app.js'))
        self.assertIn('<link rel="stylesheet" href="/pages/demo.css">', html)
        self.assertNotIn("unlisted.js", html)

    def test_feature_directory_must_not_be_a_link(self):
        import os
        import subprocess
        outside = Path(self.dir.name) / "outside"
        outside.mkdir()
        (outside / "x.js").write_text("window.z = 1;")
        (outside / "manifest.json").write_text(json.dumps({"files": ["x.js"]}))
        assets = self.server.static_dir / "assets"
        for child in assets.iterdir():
            child.unlink()
        assets.rmdir()
        try:
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(assets), str(outside)], check=True, capture_output=True)
            else:
                assets.symlink_to(outside, target_is_directory=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("cannot create a directory link here")
        self.assertEqual(self.get("/assets/x.js")[0], 404)

    def test_shell_tags_refuse_a_linked_pages_directory(self):
        with mock.patch.object(Path, "is_symlink", lambda self: self.name == "pages"):
            self.assertEqual(server.page_tags(self.server.static_dir), "")
            self.assertEqual(self.get("/pages/demo.js")[0], 404)

    def test_job_renewal_route(self):
        status, body = self.post("/hyperloom/api/jobs/renew", {"jobs": []})
        self.assertEqual((status, body), (200, {"renewed": [], "not_running": []}))

    def test_feature_posts_and_jobs_need_post(self):
        self.assertEqual(self.get("/hyperloom/api/demo/echo")[0], 405)
        self.assertEqual(self.get("/hyperloom/api/demo/job")[0], 405)
        self.assertEqual(self.post("/hyperloom/api/demo/echo", {"a": 1}), (200, {"echo": {"a": 1}}))
        status, job = self.post("/hyperloom/api/demo/job", {})
        self.assertEqual((status, job["action"]), (202, "demo/job"))
        self.assertEqual(self.post("/hyperloom/api/demo/none", {})[0], 404)



class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.db = admission.Admission(":memory:")
        self.addCleanup(self.db.close)

    def test_rules(self):
        bench = self.db.acquire("bench", {"halo-1"}, label="b1")
        with self.assertRaises(admission.Refused):
            self.db.acquire("bench", {"halo-1", "router"})
        with self.assertRaises(admission.Refused):
            self.db.acquire("core", {"halo-1"})
        core = self.db.acquire("core", {"halo-2"})
        with self.assertRaises(admission.Refused):
            self.db.acquire("bench", {"workstation"})       # any deploy keeps benchmarks out
        self.db.release(core)
        stop = self.db.acquire("stop", {"halo-1"}, label="down qwen")
        self.assertIn("down qwen", self.db.cancelled(bench))
        # A cancelled bench still owns its resources while it cleans up (until its owner releases the hold).
        self.db.release(stop)
        with self.assertRaises(admission.Refused):
            self.db.acquire("bench", {"halo-1"})
        with self.assertRaises(admission.Refused):
            self.db.acquire("core", {"halo-1"})
        self.db.release(bench)
        self.assertEqual(self.db.cancelled(bench), "admission expired")
        a = self.db.acquire("bench", {"a"})
        self.db.acquire("bench", {"b"})
        with self.assertRaises(admission.Refused):
            self.db.acquire("bench", {"c"})                  # lab-wide cap
        self.db.release(a)

    def test_lapsed_and_dead_holds_are_dropped(self):
        clock = Clock()
        db = admission.Admission(":memory:", clock=clock)
        self.addCleanup(db.close)
        hold = db.acquire("bench", {"x"}, lease_s=10)
        clock.now += 11
        self.assertIn("abandoned", db.cancelled(hold))
        db.acquire("bench", {"x"})                            # the lapsed hold no longer blocks
        db._db.execute("UPDATE holds SET pid=?", (2 ** 22 + 12345,))
        self.assertEqual(db.holds(), [])


class CLIExtensionTests(unittest.TestCase):
    def test_failure_on_any_step_leaves_the_parser_untouched(self):
        import argparse
        import sys
        calls = []

        def add(commands):
            calls.append(1)
            commands.add_parser("good-cmd")
            if len(calls) > 1:
                raise RuntimeError("second call")
        module = mock.Mock(add=add)
        clash = mock.Mock(add=lambda commands: commands.add_parser("config"))
        broken = mock.Mock(add=lambda commands: (commands.add_parser("half"), 1 / 0))
        parser = argparse.ArgumentParser(prog="hyperloom")
        commands = parser.add_subparsers(dest="command")
        commands.add_parser("config")
        with mock.patch.object(cliext, "MODULES", ("one", "two", "three")), \
                mock.patch.dict(sys.modules, {"hyperloom.cliext.one": module, "hyperloom.cliext.two": clash,
                                              "hyperloom.cliext.three": broken}), \
                mock.patch("builtins.__import__", side_effect=lambda name, *a, **kw: sys.modules[name]):
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()) as err:
                cliext.add_all(commands)
        self.assertIn("command names already used: config", err.getvalue())
        self.assertEqual(sorted(commands.choices), ["config", "good-cmd"])
        self.assertEqual(len(calls), 1)                      # built once, then grafted
        self.assertEqual(parser.parse_args(["good-cmd"]).command, "good-cmd")


class HistoryFeatureTests(unittest.TestCase):
    def setUp(self):
        from hyperloom.web.features import history
        self.history = history
        self.clock = Clock()
        self.api = mock.Mock()
        self.api.clock = self.clock
        self.api.telemetry = Store(":memory:", clock=self.clock)
        self.addCleanup(self.api.telemetry.close)
        self.api.sampler = None
        self.api.document = lambda name, **fields: {"schema": f"hyperloom/ui-{name}-v1", **fields}

    def test_series_query_filters_and_validates(self):
        for i in range(5):
            self.api.telemetry.record([("gpu_busy", {"machine": "a"}, i), ("gpu_busy", {"machine": "b"}, 9)],
                                      ts=self.clock.now - 100 + i)
        doc = self.history.series(self.api, "gpu_busy", {"range": "600", "l.machine": "a"})
        self.assertEqual((doc["kind"], doc["tier"], len(doc["lines"])), ("gauge", "raw", 1))
        self.assertEqual([p[1] for p in doc["lines"][0]["points"]], [0, 1, 2, 3, 4])
        self.assertEqual(self.history.series(self.api, "none", {})["lines"], [])
        for bad in ({"range": "x"}, {"range": "nan"}, {"range": str(40 * 86400)}, {"path": "C:/x"},
                    {"l.Bad": "1"}, {"start": "10", "end": "5"}):
            with self.assertRaises(ValueError, msg=bad):
                self.history.series(self.api, "gpu_busy", bad)
        with self.assertRaises(ValueError):
            self.history.series(self.api, "../x", {})
        self.assertEqual(self.history.index(self.api, {})["series"], ["gpu_busy"])

    def test_kind_aware_value_and_no_store(self):
        self.api.telemetry.describe("tokens", "delta", interval=2)
        self.api.telemetry.record([("tokens", {}, 5)], ts=self.clock.now - 10)
        self.assertEqual(self.history.series(self.api, "tokens", {})["value"], "sum")
        self.api.telemetry = None
        doc = self.history.series(self.api, "tokens", {})
        self.assertIsNone(doc["lines"])
        self.assertIn("not open", doc["why"]["lines"])
        self.assertIsNone(self.history.index(self.api, {})["series"])


if __name__ == "__main__":
    unittest.main()


class CLIAdmissionTests(unittest.TestCase):
    def test_cli_deploys_respect_benchmark_holds_and_stops_cancel_them(self):
        import contextlib
        import io
        from hyperloom import cli
        db = admission.Admission(":memory:")
        self.addCleanup(db.close)
        bench = db.acquire("bench", {"halo-1"}, label="bench decode")
        err = io.StringIO()
        with mock.patch.object(admission, "for_config", lambda *a, **kw: db), \
                mock.patch.object(cli, "_cluster", side_effect=ValueError("no config")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["up", "qwen-replicas"]), 1)
            self.assertIn("benchmark or demo holds", err.getvalue())
            fake = mock.Mock()
            fake.down.return_value = {"stopped": True}
            with mock.patch.object(cli, "_module", side_effect=lambda name: __import__(name, fromlist=["x"]) if name == "hyperloom.admission" else fake),                     contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["down", "qwen-replicas"]), 0)
        self.assertIn("cli down", db.cancelled(bench))
        self.assertEqual([h["kind"] for h in db.holds()], ["bench"])     # the stop released its hold

    def test_cli_holds_live_next_to_the_selected_configuration(self):
        import contextlib
        import io
        from hyperloom import cli
        seen = []
        db = admission.Admission(":memory:")
        self.addCleanup(db.close)

        def for_config(path, *a, **kw):
            seen.append(Path(path))
            return db
        fake = mock.Mock()
        fake.down.return_value = {}
        with mock.patch.object(admission, "for_config", for_config), \
                mock.patch.object(cli, "_cluster", side_effect=ValueError("no config")), \
                mock.patch.object(cli, "_module", side_effect=lambda name: __import__(name, fromlist=["x"]) if name == "hyperloom.admission" else fake), \
                contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["install", "qwen-replicas", "--config", "elsewhere/cluster.json"])
        self.assertEqual(seen, [Path("elsewhere/cluster.json")])
