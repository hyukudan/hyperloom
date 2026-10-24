"""Feature registry, API dispatch, page assets and the history feature (the portal foundation)."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import telemetry
from hyperloom.telemetry.store import Store
from hyperloom.web import features, server
from hyperloom.web.api import API, APIError


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
        reg.capability("demo", False, "needs data")
        features.reset(reg)
        self.addCleanup(features.reset)
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")

    def test_get_documents_are_redacted_and_errors_mapped(self):
        document = self.api.read("demo", {"x": "1"})
        self.assertEqual((document["schema"], document["value"], document["token"]), ("hyperloom/ui-demo-v1", "1", "[redacted]"))
        self.assertEqual(self.api.read("demo/a/b", {}), {"rest": "a/b"})
        with self.assertRaises(APIError) as caught:
            self.api.read("missing", {})
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(APIError):
            self.api.read("nothing", {})

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

    def test_exclusive_jobs_take_the_single_slot(self):
        first = self.api.mutate("demo/exclusive", {})
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/exclusive", {})
        self.assertEqual(caught.exception.status, 409)
        self.api.mutate("demo/job", {"n": 1})   # non-exclusive jobs still run
        self.assertEqual(first["state"], "running")


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

    def test_feature_posts_and_jobs_need_post(self):
        self.assertEqual(self.get("/hyperloom/api/demo/echo")[0], 405)
        self.assertEqual(self.get("/hyperloom/api/demo/job")[0], 405)
        self.assertEqual(self.post("/hyperloom/api/demo/echo", {"a": 1}), (200, {"echo": {"a": 1}}))
        status, job = self.post("/hyperloom/api/demo/job", {})
        self.assertEqual((status, job["action"]), (202, "demo/job"))
        self.assertEqual(self.post("/hyperloom/api/demo/none", {})[0], 404)


if __name__ == "__main__":
    unittest.main()


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
