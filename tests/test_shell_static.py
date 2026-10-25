"""Portal shell static contract: installable shell files and the /sw.js route, worker caching rules, the string
tables, the page registry contract and the shell helpers, theme tokens."""

import http.client
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import unittest
from unittest import mock

from hyperloom.web import features, server
from hyperloom.web.api import API
from hyperloom.web.features import prefs
from tests.ui import smoke


STATIC = Path(server.__file__).with_name("static")


def i18n_tables():
    text = (STATIC / "pages" / "shell-i18n.js").read_text(encoding="utf-8")
    match = re.search(r"/\* i18n-table-start \*/(.*)/\* i18n-table-end \*/", text, re.S)
    return json.loads(match[1])


class ShellFilesTests(unittest.TestCase):
    def test_web_manifest_and_icons(self):
        manifest = json.loads((STATIC / "assets" / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual((manifest["start_url"], manifest["scope"], manifest["display"]), ("/", "/", "standalone"))
        listed = json.loads((STATIC / "assets" / "manifest.json").read_text(encoding="utf-8"))["files"]
        self.assertIn("sw.js", listed)
        self.assertIn("manifest.webmanifest", listed)
        for icon in manifest["icons"]:
            name = icon["src"].removeprefix("/assets/")
            self.assertIn(name, listed)
            data = (STATIC / "assets" / name).read_bytes()
            if name.endswith(".png"):
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack("!II", data[16:24])
                self.assertEqual(f"{width}x{height}", icon["sizes"])
        for name in listed:
            self.assertTrue(server.ASSET_NAME.fullmatch(name), name)
            self.assertTrue((STATIC / "assets" / name).is_file(), name)

    def test_index_links_and_no_inline_script(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for needle in ('rel="manifest" href="/assets/manifest.webmanifest"', 'id="favicon" href="/assets/icon.svg"',
                       'id="palette"', 'id="palette-input"', 'role="combobox"', 'id="tabbar"', 'id="exceptions"',
                       'id="throttle-banner"', 'id="heartbeat"', 'id="nav-machines"', '<option value="black">'):
            self.assertIn(needle, html)
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [], "no inline scripts (CSP)")
        self.assertNotRegex(html, r"https?://", "no external URLs")
        pages = json.loads((STATIC / "pages" / "manifest.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(pages[:2], ["timeseries.css", "timeseries.js"])
        self.assertIn("shell-i18n.js", pages)

    def test_worker_answers_only_static_files(self):
        source = (STATIC / "assets" / "sw.js").read_text(encoding="utf-8")
        pattern = re.search(r"const STATIC = /(.+)/;\n", source)[1]
        static = re.compile(pattern)
        for path in ("/app.js", "/app.css", "/recipes.js", "/pages/history.js", "/pages/timeseries.css",
                     "/assets/icon.svg", "/assets/icon-192.png", "/assets/manifest.webmanifest", "/hyperloom/app.js"):
            self.assertTrue(static.fullmatch(path), path)
        for path in ("/", "/index.html", "/hyperloom/", "/hyperloom/api/devices", "/hyperloom/api/prefs/save", "/sw.js",
                     "/assets/sw.js", "/pages/../app.js", "/pages/sub/x.js", "/assets/x.json", "/favicon.ico", "/app.js/"):
            self.assertFalse(static.fullmatch(path), path)
        handler = source[source.index('self.addEventListener("fetch"'):]
        # Navigations return before any respondWith; only GETs reach it; the query string is never cached.
        self.assertLess(handler.index('request.mode === "navigate"'), handler.index("respondWith"))
        self.assertLess(handler.index('request.method !== "GET"'), handler.index("respondWith"))
        self.assertIn("url.search", handler)
        self.assertIn('PREFIX = "hyperloom-shell-"', source)
        self.assertIn("self.registration.unregister()", source)


class ServiceWorkerRouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.static = Path(self.dir.name) / "static"
        (self.static / "pages").mkdir(parents=True)
        (self.static / "assets").mkdir()
        for name in ("index.html", "app.css", "app.js", "recipes.js"):
            (self.static / name).write_bytes((STATIC / name).read_bytes())
        (self.static / "pages" / "manifest.json").write_text('{"files": []}')
        (self.static / "assets" / "sw.js").write_bytes(b"// worker\n")
        self.listing(["sw.js"])
        features.reset(features.Registry())
        self.addCleanup(features.reset)
        api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                  audit_path=Path(self.dir.name) / "audit.jsonl")
        self.server = server.create_server(0, api=api, static_dir=self.static)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def listing(self, names):
        (self.static / "assets" / "manifest.json").write_text(json.dumps({"files": names}))

    def request(self, method, path, host=None):
        address, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(address, port, timeout=5)
        try:
            connection.request(method, path, headers={"Host": host or f"{address}:{port}"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_served_at_the_root_with_the_portal_headers(self):
        status, headers, body = self.request("GET", "/sw.js")
        self.assertEqual((status, headers["Content-Type"], body), (200, "text/javascript; charset=utf-8", b"// worker\n"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("HEAD", "/sw.js")[:1], (200,))
        address, port = self.server.server_address[:2]
        self.assertEqual(self.request("GET", "/sw.js", host=f"{address}:{port + 1}")[0], 403)
        for path in ("/hyperloom/sw.js", "/sw.js/", "/SW.js", "/sw.js%00", "/assets/../sw.js", "/sw.json"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        self.assertEqual(self.request("POST", "/sw.js")[0], 403)   # no Origin/CSRF: refused before anything

    def test_not_listed_or_linked_is_not_served(self):
        self.listing([])
        self.assertEqual(self.request("GET", "/sw.js")[0], 404)
        self.assertEqual(self.request("HEAD", "/sw.js")[0], 404)
        self.listing(["sw.js"])
        outside = Path(self.dir.name) / "outside.js"
        outside.write_text("// elsewhere\n")
        (self.static / "assets" / "sw.js").unlink()
        try:
            os.symlink(outside, self.static / "assets" / "sw.js")
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        self.assertEqual(self.request("GET", "/sw.js")[0], 404)


class ShellStringsTests(unittest.TestCase):
    def test_languages_have_the_same_keys_and_placeholders(self):
        tables = i18n_tables()
        self.assertEqual(sorted(tables), ["en", "es"])
        self.assertEqual(set(tables["en"]), set(tables["es"]))
        for key, text in tables["en"].items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", text)), sorted(re.findall(r"\{(\w+)\}", tables["es"][key])), key)
            self.assertTrue(tables["es"][key].strip(), key)

    def test_every_shell_string_used_exists(self):
        tables = i18n_tables()["en"]
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r'\bt\("([a-z][a-z0-9_.-]*[a-z0-9_])"', script))
        self.assertTrue(used)
        self.assertEqual(sorted(k for k in used if k not in tables), [])
        families = {"route.": ["devices", "topology", "models", "recipes", "engines", "stats", "logs", "settings"],
                    "group.": ["workspace", "inference", "benchmarks", "operations", "other"],
                    "theme.": prefs.CHOICES["theme"], "density.": prefs.CHOICES["density"],
                    "related.": [row[0] for row in prefs.RELATED]}
        for prefix, names in families.items():
            for name in names:
                self.assertIn(prefix + name, tables)


class ShellScriptContractTests(unittest.TestCase):
    def setUp(self):
        self.script = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_page_registry_contract_is_unchanged(self):
        self.assertIn("const pages = window.HyperloomPages || {};", self.script)
        for hook in ("page?.render", "page.title", "page.icon", ".order ??", "?.group", ".endpoints?.()", "feature.ready",
                     "feature.error", ".actions()", "feature.after", ".refresh?.()", "?.home"):
            self.assertIn(hook, self.script, hook)
        exports = self.script[self.script.index("window.Hyperloom = {"):]
        exports = exports[:exports.index("};")]
        for name in ("esc", "display", "unavailable", "statusPill", "tag", "button", "card", "table", "cells", "kv", "empty",
                     "rows", "request", "mutate", "refresh", "render", "announce", "doc", "machines", "meter", "kpi", "chart",
                     "lineChart", "spark", "field", "csrf", "download", "state", "own", "info", "problem", "filter",
                     "filterValue", "matches", "prefs", "setPref", "temperature", "time", "zone", "t", "i18n"):
            self.assertRegex(exports, rf"\b{name}\b", name)

    def test_client_choices_match_the_server(self):
        match = re.search(r"const PREF_CHOICES = (\{.*?\});", self.script)
        client = json.loads(re.sub(r"(\w+):", r'"\1":', match[1]))
        self.assertEqual(client, prefs.CHOICES)
        match = re.search(r"const PREF_DEFAULTS = (\{.*?\});", self.script)
        self.assertEqual(json.loads(re.sub(r"(\w+):", r'"\1":', match[1])), prefs.DEFAULTS)

    def test_optional_documents_never_block_controls(self):
        self.assertIn('const OPTIONAL = ["prefs", "health/summary", "overview"];', self.script)
        poll = self.script[self.script.index("async function refresh()"):self.script.index("function fetchDocument(")]
        self.assertIn('state.fresh = results.every(r => r.status === "fulfilled");', poll)
        self.assertIn("refreshOptional(generation, prefsEpoch, own);", poll)
        self.assertNotIn("optionalDue()", poll, "the screen's own poll never waits for the optional documents")


class ShellSeedTests(unittest.TestCase):
    """The browser smoke's scenario wrapper answers the same whether or not a feature registers the document."""

    def test_scenarios_override_registered_and_absent_documents(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("shell_seed", Path(__file__).with_name("ui") / "seeds" / "shell.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        api = mock.Mock()
        api.document = lambda name, **fields: {"schema": name, **fields}
        with tempfile.TemporaryDirectory() as directory:
            for registered in (True, False):
                def real_load(modules=features.MODULES):
                    reg = features.Registry()
                    if registered:
                        reg.get("health/summary", lambda api, query: {"real": True})
                    return reg
                with mock.patch.object(features, "load", real_load):
                    patched = dict(((owner, name), value) for owner, name, value in seed.patches(directory, 0))
                    handler = patched[(features, "load")]().gets["health/summary"]
                path = Path(directory) / "shell-scenario.json"
                path.unlink(missing_ok=True)
                if registered:
                    self.assertEqual(handler(api, {}), {"real": True})
                else:
                    self.assertRaises(LookupError, handler, api, {})
                path.write_text(json.dumps({"health/summary": "missing"}))
                self.assertRaises(LookupError, handler, api, {})
                path.write_text(json.dumps({"health/summary": "fail"}))
                self.assertRaises(RuntimeError, handler, api, {})
                path.write_text(json.dumps({"health/summary": {"counts": {}}}))
                self.assertEqual(handler(api, {})["counts"], {})


class ThemeTokenTests(unittest.TestCase):
    def test_light_dark_and_true_black_meet_aa(self):
        self.assertEqual(smoke.check_contrast(themes_checked=("light", "dark", "black")), 45)
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"][data-shade="black"]{', css)
        self.assertIn("--bg:#000000", css)


if __name__ == "__main__":
    unittest.main()
