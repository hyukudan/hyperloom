"""Model library portal feature and CLI family: documents, validation, audits, jobs and their outcomes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import admission, cliext, downloads
from hyperloom.config import parse
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API, APIError
from hyperloom.web.features import library
from tests.test_config import example


class LibraryFeatureCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cluster = parse(example())
        reg = features.Registry()
        library.register(reg)
        features.reset(reg)
        self.addCleanup(features.reset)
        orchestrator = mock.Mock()
        orchestrator.load_cluster.return_value = self.cluster
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=orchestrator, remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        node = mock.patch.object(downloads, "_node", side_effect=AssertionError("no node call expected"))
        self.node = node.start()
        self.addCleanup(node.stop)

    def wait(self, jid):
        for _ in range(500):
            current = self.api.read("jobs/" + jid)
            if current["state"] != "running":
                return current
            time.sleep(0.01)
        self.fail("job did not finish")

    def audit(self):
        return [json.loads(line) for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]


class DocumentTests(LibraryFeatureCase):
    def test_without_a_store_nothing_is_created_and_no_node_is_called(self):
        before = sorted(p.name for p in Path(self.dir.name).iterdir())
        document = self.api.read("library", {})
        self.assertEqual(document["schema"], "hyperloom/ui-library-v1")
        self.assertIn("not open", document["why"]["inventory"])
        rows = {m["id"]: m for m in document["machines"]}
        self.assertEqual(rows["halo-1"]["roots"], [{"path": "/opt/assets", "source": "config"}])
        self.assertEqual(document["copy"], {"available": False,
                                            "why": "copies between machines are not available in this release"})
        self.assertEqual(document["ledger"]["configured"], False)
        self.assertEqual(sorted(p.name for p in Path(self.dir.name).iterdir()), before)
        with self.assertRaises(APIError):
            self.api.read("library", {"path": "C:/x"})
        self.node.assert_not_called()

    def test_cached_scan_verify_and_jobs(self):
        self.api.telemetry = self.store
        self.store.put("library/inventory", "halo-1", {"machine": "halo-1", "scanned_at": 1, "roots": []})
        self.store.put("library/verify", "halo-1|/opt/assets|m", {"state": "succeeded"})
        self.store.put("library/jobs", "j", {"id": "j", "kind": "verify", "state": "running"})
        document = self.api.read("library", {})
        rows = {m["id"]: m for m in document["machines"]}
        self.assertEqual(rows["halo-1"]["inventory"]["scanned_at"], 1)
        self.assertIn("not scanned yet", rows["halo-2"]["why"]["inventory"])
        self.assertEqual(document["verify"], {"halo-1|/opt/assets|m": {"state": "succeeded"}})
        self.assertEqual([j["id"] for j in document["jobs"]], ["j"])


class PostTests(LibraryFeatureCase):
    def test_roots_are_validated_and_audited(self):
        self.api.telemetry = self.store
        status, result = self.api.feature_post("library/roots", {"machine": "halo-2", "roots": ["/srv/models"]})
        self.assertEqual(result, {"machine": "halo-2", "roots": ["/srv/models"]})
        self.assertEqual(self.store.get("prefs", "library/roots"), {"halo-2": ["/srv/models"]})
        for bad in ({"machine": "halo-2", "roots": ["relative"]}, {"machine": "nobody", "roots": []},
                    {"machine": "halo-2", "roots": "x"}, {"machine": "halo-2"}):
            with self.subTest(bad), self.assertRaises(APIError):
                self.api.feature_post("library/roots", bad)
        states = [row["result"]["state"] for row in self.audit() if row["action"] == "library/roots"]
        self.assertEqual(states.count("succeeded"), 1)
        self.assertEqual(states.count("refused"), 4)

    def test_cancel_is_audited_and_job_ids_checked(self):
        with mock.patch.object(downloads, "library_job", return_value={"state": "cancelled"}) as call:
            self.api.feature_post("library/cancel", {"machine": "halo-1", "job": "a" * 32})
        call.assert_called_once_with(self.cluster, "halo-1", "a" * 32, cancel=True)
        with self.assertRaises(APIError):
            self.api.feature_post("library/job", {"machine": "halo-1", "job": "../x"})
        self.assertEqual([row["action"] for row in self.audit()], ["library/cancel"])


class JobTests(LibraryFeatureCase):
    def setUp(self):
        super().setUp()
        self.api.telemetry = self.store

    def test_scan_stores_results_and_refuses_an_overlap(self):
        def inventory(cluster, mid, store=None):
            if mid == "halo-2":
                raise RuntimeError("halo-2 unreachable")
            return {"machine": mid, "scanned_at": 5, "roots": []}
        with mock.patch.object(downloads, "inventory", side_effect=inventory):
            job = self.api.mutate("library/scan", {})
            result = self.wait(job["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["machines"]["halo-2"], "error: halo-2 unreachable")
        self.assertEqual(self.store.get("library/inventory", "halo-1")["scanned_at"], 5)
        library._scans["halo-1"] = time.time()
        self.addCleanup(library._scans.clear)
        with self.assertRaises(APIError) as caught:
            self.api.mutate("library/scan", {"machine": "halo-1"})
        self.assertEqual(caught.exception.status, 409)

    def run_verify(self, final):
        with mock.patch.object(downloads, "verify_start", return_value={"mode": "node", "machine": "halo-1",
                                                                        "job": "n1"}), \
                mock.patch.object(downloads, "watch_job", return_value=final):
            job = self.api.mutate("library/verify", {"machine": "halo-1", "model_root": "/opt/assets", "name": "m"})
            return self.wait(job["id"])

    def test_node_outcomes_map_to_the_portal_job(self):
        done = self.run_verify({"state": "succeeded", "result": {"ok": True}})
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(self.store.get("library/verify", "halo-1|/opt/assets|m")["result"], {"ok": True})
        failed = self.run_verify({"state": "failed", "reason": "the scan reached a bound"})
        self.assertEqual(failed["state"], "failed")
        self.assertIn("bound", failed["error"])
        cancelled = self.run_verify({"state": "cancelled"})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIn("node job was cancelled", cancelled["error"])
        rows = [value for _, _, value in self.store.items("library/jobs")]
        self.assertEqual(sorted(r["state"] for r in rows), ["cancelled", "failed", "succeeded"])

    def run_watched_verify(self, node, after_cancel=lambda: None):
        """A real verification watcher (fake node gate) driven by the portal job; the portal job is cancelled for
        real (its JobContext) once the node job is running."""
        running = threading.Event()

        def gate(machine, verb, payload, timeout=60):
            if verb == "model-job" and not running.is_set():
                running.set()
                return {"state": "running", "job": "n1"}
            return node(machine, verb, payload)
        self.node.side_effect = gate
        with mock.patch.object(downloads, "verify_start", return_value={"mode": "node", "machine": "halo-1",
                                                                        "job": "n1"}), \
                mock.patch.object(library, "_sleep", lambda s: time.sleep(0.01)):
            job = self.api.mutate("library/verify", {"machine": "halo-1", "model_root": "/opt/assets", "name": "m"})
            self.assertTrue(running.wait(10))
            self.api.mutate("cancel", {"job": job["id"]})
            done = self.wait(job["id"])
        row = [value for _, _, value in self.store.items("library/jobs")][0]
        final = [entry for entry in self.audit() if entry["action"] == "library/verify"][-1]
        return done, row, final

    def test_a_real_portal_cancel_keeps_a_node_failure_and_its_reason(self):
        def node(machine, verb, payload):
            if verb == "model-cancel":
                return {"state": "running"}
            return {"state": "failed", "job": "n1", "reason": "the unit changed while it was hashed"}
        done, row, final = self.run_watched_verify(node)
        self.assertEqual((row["state"], row["node_job"]), ("failed", "n1"))            # the library's record
        self.assertIn("changed while it was hashed", row["reason"])
        # The core job and its audit line agree with the row although a cancel was requested.
        self.assertEqual((done["state"], done.get("outcome")), ("failed", "failed"))
        self.assertIn("changed while it was hashed", done["error"])
        self.assertEqual((final["result"]["state"], final["result"].get("outcome")), ("failed", "failed"))

    def test_a_real_portal_cancel_keeps_an_unresolved_outcome(self):
        def node(machine, verb, payload):
            raise TimeoutError("node halo-1 is busy with another call")
        with mock.patch.object(downloads, "WATCH_FAILURES_S", 0.2):
            done, row, final = self.run_watched_verify(node)
        self.assertEqual((row["state"], row["node_job"]), ("unresolved", "n1"))
        self.assertEqual((done["state"], done.get("outcome")), ("failed", "unresolved"))
        self.assertIn("could not be read", done["error"])
        self.assertEqual((final["result"]["state"], final["result"].get("outcome")), ("failed", "unresolved"))

    def test_a_cancelled_node_job_cancels_the_portal_job(self):
        def node(machine, verb, payload):
            return {"state": "running" if verb == "model-cancel" else "cancelled", "job": "n1"}
        done, row, final = self.run_watched_verify(node)
        self.assertEqual((row["state"], done["state"]), ("cancelled", "cancelled"))

    def test_node_progress_is_recorded_on_the_row(self):
        records = iter([{"state": "running", "job": "n2", "progress": {"bytes_done": 5}},
                        {"state": "succeeded", "job": "n2", "result": {"ok": True}}])
        self.node.side_effect = lambda machine, verb, payload, timeout=60: next(records)
        seen = []
        original = library._progress

        def spy(ctx, row):
            report = original(ctx, row)
            return lambda record: (seen.append(record), report(record))
        with mock.patch.object(downloads, "verify_start", return_value={"mode": "node", "machine": "halo-1",
                                                                        "job": "n2"}), \
                mock.patch.object(library, "_progress", spy), mock.patch.object(library, "_sleep", lambda s: None):
            job = self.api.mutate("library/verify", {"machine": "halo-1", "model_root": "/opt/assets", "name": "m"})
            self.assertEqual(self.wait(job["id"])["state"], "succeeded")
        self.assertEqual([r["state"] for r in seen], ["running", "succeeded"])

    def test_local_cancellation_returns_none(self):
        with mock.patch.object(downloads, "verify_start", side_effect=downloads.Cancelled("cancelled")):
            job = self.api.mutate("library/verify", {"machine": "workstation", "model_root": "C:\\x", "name": "m"})
            self.assertEqual(self.wait(job["id"])["state"], "cancelled")

    def test_settlement_after_cancel_keeps_its_polling_interval(self):
        from hyperloom.web.api import JobContext
        ctx = JobContext(60)
        ctx.cancel("cancel requested")
        records = iter([{"state": "running", "job": "n1"}] * 5 + [{"state": "succeeded", "job": "n1"}])
        self.node.side_effect = lambda machine, verb, payload, timeout=60: (
            next(records) if verb == "model-job" else {"state": "running"})
        pauses = []
        with mock.patch.object(library, "_sleep", pauses.append):
            record = downloads.watch_job(self.cluster, "halo-1", "n1", cancelled=ctx.cancelled,
                                         sleep=library._paced(ctx))
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(pauses, [2.0] * 5)                       # a real pause between polls, never a busy loop

def _smoke():
    import importlib.util
    path = Path(__file__).resolve().parent / "ui" / "smoke.py"
    spec = importlib.util.spec_from_file_location("hyperloom_ui_smoke_for_library", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _smoke()


@unittest.skipUnless(smoke.find_chrome(), "no headless browser found (see tests/ui/README.md)")
class LibraryPageInBrowser(unittest.TestCase):
    """The page against the fixture portal (its seed has a running verification, a failed one whose last node record
    still names a pause, and an unresolved one)."""

    @classmethod
    def setUpClass(cls):
        import os
        import subprocess
        import sys
        from urllib.parse import urlsplit
        cls.server = smoke.FixtureServer()
        host = urlsplit(cls.server.origin).hostname
        cls.profile = tempfile.TemporaryDirectory(prefix="hyperloom-browser-", ignore_cleanup_errors=True)
        port = smoke.free_port()
        command = [smoke.find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   "--disable-extensions", "--proxy-server=direct://",
                   f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {host}",
                   f"--remote-debugging-address={host}", f"--remote-debugging-port={port}",
                   f"--user-data-dir={cls.profile.name}", "about:blank"]
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.append("--no-sandbox")
        cls.browser = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def ready():
            try:
                return next((row for row in smoke.get_json(port, "/json/list") if row["type"] == "page"), None)
            except OSError:
                return None
        target = smoke.eventually(ready)
        cls.client = smoke.DevTools(target["webSocketDebuggerUrl"])
        cls.client.call("Runtime.enable")
        cls.client.call("Page.enable")

    @classmethod
    def tearDownClass(cls):
        import subprocess
        try:
            cls.client.close()
            cls.browser.terminate()
            try:
                cls.browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.browser.kill()
                cls.browser.wait(timeout=5)
        finally:
            for _ in range(10):
                try:
                    cls.server.close()
                    break
                except OSError:
                    time.sleep(0.5)
            cls.profile.cleanup()

    def test_finished_and_unresolved_jobs_show_their_own_state_and_reason(self):
        self.client.call("Page.navigate", url=self.server.url + "#/library")
        smoke.eventually(lambda: self.client.evaluate(
            "document.readyState === 'complete' && !!document.querySelector('[role=region][aria-label=\"Library jobs\"]')"),
            timeout=30)
        rows = self.client.evaluate("""[...document.querySelectorAll('[role=region][aria-label="Library jobs"] tbody tr')]
            .map(tr => [...tr.children].map(td => td.innerText.trim()))""")
        by_name = {row[1].split(" ")[0]: row for row in rows}
        failed, unresolved = by_name["ds-trellis-k5"], by_name["draft-small.gguf"]
        self.assertEqual(failed[2], "failed")                          # not the stale "paused" of its node record
        self.assertIn("paused for more than 10 minutes", failed[3])
        self.assertIn("not available in this release", self.client.evaluate(
            "document.querySelector('[data-page-action=\"library-copy\"]').title"))     # copy: disabled, says why
        self.assertEqual(unresolved[2], "unresolved")
        self.assertIn("could not be read", unresolved[3])
        self.assertIn("Cancel on the node", unresolved[5])
        running = by_name["ds-trellis-k4"]
        self.assertEqual(running[2], "running")


class PageAndCLITests(unittest.TestCase):
    def test_page_is_in_the_manifest_after_the_shared_chart_files(self):
        root = Path(__file__).resolve().parents[1] / "hyperloom" / "web" / "static" / "pages"
        files = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(files[:2], ["timeseries.css", "timeseries.js"])
        self.assertIn("library.js", files)
        self.assertIn("library.css", files)
        script = (root / "library.js").read_text(encoding="utf-8")
        self.assertNotIn("http://", script)
        self.assertNotIn("https://", script)
        self.assertNotIn("innerHTML", script)
        self.assertIn('data-page-action', script)

    def test_cli_family_parses(self):
        from hyperloom import cli
        parser = cli.build_parser()
        args = parser.parse_args(["models", "verify", "halo-1", "/opt/assets", "m"])
        self.assertEqual((args.models_operation, args.machine, args.name), ("verify", "halo-1", "m"))
        self.assertIsNotNone(args.ext_run)
        for argv in (["models", "copy", "halo-1", "m"], ["models", "copy-plan", "halo-1", "m"]):
            with self.subTest(argv), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(argv)                                   # not part of this release
        for argv in (["models", "list", "--refresh"], ["models", "verify", "halo-1", "/opt/assets", "m", "--wait"],
                     ["models", "job", "halo-1", "abc"], ["models", "cancel", "halo-1", "abc"],
                     ["models", "roots", "halo-1", "--set", "/srv/a"], ["models", "ledger-accept"]):
            with self.subTest(argv):
                parser.parse_args(argv)

    def test_cli_interruption_during_startup_cancels_the_new_node_job(self):
        from hyperloom import cli
        from hyperloom.cliext import library as cli_library
        flag_holder = {}
        real = cli_library._Interrupt

        class Interrupted(real):
            def __init__(self):
                super().__init__()
                flag_holder["flag"] = self

        def verify_start(*args, cancelled, **kwargs):
            flag_holder["flag"].event.set()                 # Ctrl+C while the node job is being started
            return {"mode": "node", "machine": "halo-1", "job": "n1"}
        watched = []

        def watch_job(cluster, machine, job, cancelled, progress=None):
            watched.append((job, cancelled()))
            return {"state": "cancelled", "job": job}
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli, "config_path", return_value=Path(tmp) / "cluster.json"), \
                mock.patch.object(cli, "_cluster", return_value=parse(example())), \
                mock.patch.object(cli_library, "_Interrupt", Interrupted), \
                mock.patch.object(downloads, "verify_start", side_effect=verify_start), \
                mock.patch.object(downloads, "watch_job", side_effect=watch_job), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["models", "verify", "halo-1", "/opt/assets", "m"])
        self.assertEqual((code, watched), (2, [("n1", True)]))

    def test_cli_list_without_a_store_says_why(self):
        from hyperloom import cli
        from hyperloom.cliext import library as cli_library
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli, "config_path", return_value=Path(tmp) / "cluster.json"), \
                mock.patch.object(cli, "_cluster", return_value=parse(example())), \
                contextlib.redirect_stdout(out):
            code = cli.main(["models", "list", "--machine", "halo-1"])
            self.assertFalse((Path(tmp) / "telemetry.sqlite3").exists())
        self.assertEqual(code, 0)
        self.assertIn("not scanned yet", json.loads(out.getvalue())["halo-1"]["why"])
        self.assertTrue(callable(cli_library.run))


if __name__ == "__main__":
    unittest.main()
