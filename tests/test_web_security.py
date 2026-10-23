"""Security checks through TCP against the real ``hyperloom ui`` CLI/server."""
import importlib.util
import http.client
import json
from html.parser import HTMLParser
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("hyperloom_ui_smoke", Path(__file__).with_name("ui") / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

MUTATIONS = ("up", "down", "install", "hosts/pin", "doctor", "keys/rotate", "cancel")


class CSRFParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "meta" and "csrf" in values.get("name", "").lower():
            self.token = values.get("content")


class PortalSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = smoke.FixtureServer(blocked=True)
        self.addCleanup(self.server.close)
        status, _, body = self.server.request("GET", "/")
        self.assertEqual(status, 200)
        parser = CSRFParser()
        parser.feed(body.decode("utf-8"))
        self.assertTrue(parser.token, "index.html must expose the per-launch CSRF token")
        self.token = parser.token
        self.headers = {"Origin": self.server.origin, "Content-Type": "application/json",
                        "X-Hyperloom-CSRF": self.token}

    def post(self, action="up", *, headers=None, body=None):
        return self.server.request("POST", smoke.API + action,
                                   body if body is not None else json.dumps({"profile": "qwen-replicas"}),
                                   self.headers if headers is None else headers)

    def raw_post(self, headers, body=b"{}"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            connection.putrequest("POST", smoke.API + "up", skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def assert_no_mutation(self, *, allow_reads=False):
        allowed = {"remote:doctor", "remote:gpu"} if allow_reads else set()
        self.assertEqual([row for row in self.server.calls() if row["action"] not in allowed], [])

    def assert_no_cors(self, headers):
        self.assertFalse([name for name in headers if name.lower().startswith("access-control-")])

    def test_all_read_endpoints_are_json_and_read_only(self):
        for endpoint in ("devices", "topology", "models", "profiles", "stats", "logs", "settings"):
            with self.subTest(endpoint=endpoint):
                status, headers, body = self.server.request("GET", smoke.API + endpoint)
                self.assertEqual(status, 200, body)
                self.assertIn("application/json", headers.get("Content-Type", ""))
                self.assertIsInstance(json.loads(body), dict)
                self.assert_no_cors(headers)
        self.assert_no_mutation(allow_reads=True)

    def test_foreign_origin_refused_for_every_mutation(self):
        headers = {**self.headers, "Origin": "http://192.0.2.10"}
        for action in MUTATIONS:
            with self.subTest(action=action):
                status, response_headers, _ = self.post(action, headers=headers)
                self.assertEqual(status, 403)
                self.assert_no_cors(response_headers)
        self.assert_no_mutation()

    def test_missing_null_and_prefix_origins_refused(self):
        for origin in (None, "null", self.server.origin + ".invalid", self.server.origin + "/",
                       self.server.origin.replace("http:", "https:")):
            with self.subTest(origin=origin):
                headers = dict(self.headers)
                if origin is None:
                    del headers["Origin"]
                else:
                    headers["Origin"] = origin
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.assert_no_mutation()

    def test_dns_rebinding_host_refused_on_static_and_api(self):
        for host in (f"192.0.2.10:{self.server.port}", f"127.0.0.1:{self.server.port}.invalid",
                     "127.0.0.1", f"localhost:{self.server.port + 1}"):
            for path in ("/", smoke.API + "devices"):
                with self.subTest(host=host, path=path):
                    status, headers, _ = self.server.request("GET", path, headers={"Host": host})
                    self.assertEqual(status, 403)
                    self.assert_no_cors(headers)
            self.assertEqual(self.post(headers={**self.headers, "Host": host})[0], 403)
        self.assert_no_mutation()

    def test_localhost_host_is_allowed(self):
        status, _, _ = self.server.request("GET", smoke.API + "profiles",
                                            headers={"Host": f"localhost:{self.server.port}"})
        self.assertEqual(status, 200)
        self.assert_no_mutation(allow_reads=True)

    def test_missing_and_wrong_csrf_refused_for_every_mutation(self):
        for token in (None, "wrong-token"):
            headers = dict(self.headers)
            if token is None:
                del headers["X-Hyperloom-CSRF"]
            else:
                headers["X-Hyperloom-CSRF"] = token
            for action in MUTATIONS:
                with self.subTest(token=token, action=action):
                    self.assertEqual(self.post(action, headers=headers)[0], 403)
        self.assert_no_mutation()

    def test_get_never_dispatches_mutations_even_with_credentials(self):
        for action in MUTATIONS:
            with self.subTest(action=action):
                status, _, _ = self.server.request("GET", smoke.API + action + "?profile=qwen-replicas",
                                                    headers=self.headers)
                self.assertIn(status, (404, 405))
        self.assert_no_mutation()

    def test_non_post_methods_never_dispatch_mutations(self):
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, _ = self.server.request(method, smoke.API + "up", headers=self.headers)
                self.assertGreaterEqual(status, 400)
                self.assert_no_cors(headers)
        self.assert_no_mutation()

    def test_wrong_or_missing_content_type_refused(self):
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "text/json"):
            with self.subTest(content_type=content_type):
                headers = dict(self.headers)
                if content_type is None:
                    del headers["Content-Type"]
                else:
                    headers["Content-Type"] = content_type
                self.assertEqual(self.post(headers=headers)[0], 415)
        self.assert_no_mutation()

    def test_malformed_json_and_non_objects_refused(self):
        for body in (b"{", b'{"profile":}', b"\xff", b"[]", b"null", b'"up"',
                     b'{"profile":"qwen-replicas","profile":"qwen-replicas"}',
                     b'{"profile":"qwen-replicas","deadline":NaN}'):
            with self.subTest(body=body):
                self.assertEqual(self.post(body=body)[0], 400)
        self.assert_no_mutation()

    def test_oversize_body_refused(self):
        body = json.dumps({"profile": "qwen-replicas", "padding": "x" * 65536})
        self.assertEqual(self.post(body=body)[0], 413)
        self.assert_no_mutation()

    def test_duplicate_security_headers_are_refused(self):
        headers = [("Host", f"127.0.0.1:{self.server.port}"), *self.headers.items(), ("Content-Length", "2")]
        for name, expected in (("Host", 403), ("Origin", 403), ("X-Hyperloom-CSRF", 403),
                               ("Content-Type", 415), ("Content-Length", 411)):
            with self.subTest(header=name):
                value = next(value for key, value in headers if key == name)
                self.assertEqual(self.raw_post([*headers, (name, value)]), expected)
        self.assert_no_mutation()

    def test_ambiguous_or_invalid_body_framing_is_refused(self):
        headers = [("Host", f"127.0.0.1:{self.server.port}"), *self.headers.items()]
        for extra, expected in (([], 411), ([("Content-Length", "-1")], 411),
                                ([("Content-Length", "no")], 411),
                                ([("Content-Length", "0")], 400),
                                ([("Content-Length", "65537")], 413),
                                ([("Content-Length", "2"), ("Transfer-Encoding", "chunked")], 400)):
            with self.subTest(headers=extra):
                self.assertEqual(self.raw_post([*headers, *extra]), expected)
        self.assert_no_mutation()

    def test_preflight_grants_no_cross_origin_access(self):
        status, headers, _ = self.server.request("OPTIONS", smoke.API + "up", headers={
            "Origin": "http://192.0.2.10", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Hyperloom-CSRF, Content-Type"})
        self.assertGreaterEqual(status, 400)
        self.assert_no_cors(headers)
        self.assert_no_mutation()

    def test_valid_post_dispatches_and_concurrent_mutation_is_refused(self):
        status, _, body = self.post()
        self.assertEqual(status, 202, body)
        self.assertIsInstance(json.loads(body), dict)
        smoke.eventually(lambda: any(row["action"] == "up" for row in self.server.calls()))
        self.assertEqual(self.post("install")[0], 409)
        self.assertEqual([row["action"] for row in self.server.calls()].count("up"), 1)
        self.assertNotIn("install", [row["action"] for row in self.server.calls()])

    def test_stop_is_not_blocked_by_running_job(self):
        self.assertEqual(self.post()[0], 202)
        smoke.eventually(lambda: any(row["action"] == "up" for row in self.server.calls()))
        status, _, body = self.post("down")
        self.assertIn(status, (200, 202), body)
        smoke.eventually(lambda: any(row["action"] == "down" for row in self.server.calls()))

    def test_cancel_is_not_blocked_by_running_job(self):
        status, _, body = self.post()
        self.assertEqual(status, 202)
        job = json.loads(body)["job"]
        smoke.eventually(lambda: any(row["action"] == "up" for row in self.server.calls()))
        status, _, body = self.post("cancel", body=json.dumps({"job": job}))
        self.assertEqual(status, 202, body)
        smoke.eventually(lambda: any(row["action"] == "down" for row in self.server.calls()))

    def test_static_files_do_not_expose_source_or_directory_listings(self):
        for path in ("/../api.py", "/%2e%2e/api.py", "/hyperloom/api.py", "/static/",
                     "/%5c..%5capi.py", "/tests/", "/cluster.json"):
            with self.subTest(path=path):
                self.assertEqual(self.server.request("GET", path)[0], 404)
        self.assert_no_mutation()

    def test_job_result_is_pollable_and_audited(self):
        self.server.release()
        status, _, body = self.post()
        self.assertEqual(status, 202)
        job = json.loads(body)["job"]

        def completed():
            status, _, body = self.server.request("GET", smoke.API + "jobs/" + job)
            self.assertEqual(status, 200)
            row = json.loads(body)
            return row if row["state"] != "running" else None

        result = smoke.eventually(completed)
        self.assertEqual(result["state"], "succeeded", result)
        audit = self.server.directory / "hyperloom/audit.jsonl"
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["action"] == "up" and row["result"].get("state") == "succeeded" for row in rows))
        self.assertNotIn(self.token, audit.read_text(encoding="utf-8"))

    def test_csrf_token_changes_between_server_launches(self):
        with smoke.FixtureServer() as other:
            parser = CSRFParser()
            parser.feed(other.request("GET", "/")[2].decode("utf-8"))
            self.assertTrue(parser.token)
            self.assertNotEqual(self.token, parser.token)
            status, _, _ = other.request("POST", smoke.API + "up", json.dumps({"profile": "qwen-replicas"}),
                                          {**self.headers, "Origin": other.origin})
            self.assertEqual(status, 403)
            self.assertEqual(other.calls(), [])


class PortalContrastTests(unittest.TestCase):
    def test_text_tokens_meet_aa_in_both_themes(self):
        self.assertEqual(smoke.check_contrast(), 30)


if __name__ == "__main__":
    unittest.main()
