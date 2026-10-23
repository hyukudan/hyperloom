"""API contract and transport checks with no real workers or GPU activity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyperloom import cli, config
from hyperloom.web.api import API, APIError, SCREENS, redact
from hyperloom.web.server import create_server


def fixture():
    machine = config.Machine("halo-1", "linux", "ssh", "192.0.2.10", "halo-1", 22,
                             "/opt/hyperloom/halo-1", True, [{"id": "gpu"}])
    profiles = {"qwen-replicas": {"nodes": ["halo-1"]}}
    return config.Cluster({machine.id: machine}, {"halo-1": {"model": {
        "path": "/opt/assets/model.gguf", "bytes": 64, "sha256": "a" * 64}}},
        profiles, {}, {}, {"schema": "hyperloom/cluster-v1", "profiles": profiles})


def wait_job(api, job):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        value = api.read("jobs/" + job["job"])
        if value["state"] != "running":
            return value
        time.sleep(.01)
    raise AssertionError("job did not finish")


class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-web-api-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cluster = fixture()
        self.now = 1000.
        self.orch = Mock()
        self.orch.load_cluster.return_value = self.cluster
        self.orch._known_hosts.return_value = self.directory / "known_hosts"
        self.orch._read.side_effect = FileNotFoundError
        self.orch.status.return_value = {"qwen-replicas": {"state": "stopped"}}
        self.orch._profile.return_value = ({}, "qwen-replicas", ["halo-1"])
        self.orch.up.return_value = {"state": "running"}
        self.orch.down.return_value = {"state": "stopped"}
        self.orch.install.return_value = "a" * 12
        self.orch.logs.return_value = {"stdout": "READY\napi_key=hidden", "stderr": ""}
        self.remote = Mock()
        self.calls = []

        def node(verb, payload, timeout):
            self.calls.append(verb)
            checks = {"os": {"status": "ok", "value": "Linux"},
                      "disk": {"status": "ok", "value": {"free_bytes": 1234}}} if verb == "doctor" else {
                          "ram": {"status": "ok", "value": {"total_bytes": 4096}},
                          "amd": {"status": "ok", "value": []}}
            return {"schema": "hyperloom/node-v1", "verb": verb, "checks": checks}
        self.remote.Remote.return_value.call.side_effect = node
        self.api = API(self.directory / "cluster.json", orchestrator=self.orch,
                       remote_module=self.remote, clock=lambda: self.now,
                       audit_path=self.directory / "audit.jsonl")

    def test_every_screen_has_version_time_and_missing_source_reasons(self):
        for screen in SCREENS:
            with self.subTest(screen=screen):
                document = self.api.read(screen)
                self.assertEqual(document["schema"], "hyperloom/ui-" + screen + "-v1")
                self.assertEqual(document["server_time"], self.now)
                self.assertEqual(document["poll_interval_s"], 2)
                self.assertIsInstance(document["why"], dict)
                json.dumps(document, allow_nan=False)
        self.assertIsNone(self.api.read("stats")["step_breakdown"])
        self.assertIn("R16", self.api.read("stats")["why"]["step_breakdown"])
        self.assertIsNone(self.api.read("models")["models"][0]["presence"])
        self.assertIsNone(self.api.read("devices")["machines"][0]["cpu_model"])
        self.orch.up.assert_not_called()
        self.orch.install.assert_not_called()
        self.orch.down.assert_not_called()

    def test_concurrent_tabs_share_one_poll_across_every_screen(self):
        with ThreadPoolExecutor(max_workers=14) as pool:
            documents = list(pool.map(self.api.read, list(SCREENS) * 2))
        self.assertEqual(len(documents), 14)
        self.assertEqual(self.calls, ["doctor", "gpu"])
        self.orch.status.assert_called_once_with()
        self.now += 2
        self.api.read("devices")
        self.assertEqual(self.calls, ["doctor", "gpu"] * 2)

    def test_offline_keeps_last_seen_without_fake_values(self):
        first = self.api.read("devices")["machines"][0]
        self.remote.Remote.return_value.call.side_effect = RuntimeError("machine offline")
        self.now += 3
        row = self.api.read("devices")["machines"][0]
        self.assertEqual(row["health"], "offline")
        self.assertEqual(row["last_seen"], first["last_seen"])
        self.assertIsNone(row["accelerators"])
        self.assertEqual(row["why"]["accelerators"], "machine offline")

    def test_unpinned_machine_is_unpaired_and_trusted_alias_is_not_an_address(self):
        self.remote.Remote.return_value.call.side_effect = RuntimeError("unknown host key; confirm a fingerprint before connecting")
        self.assertEqual(self.api.read("devices")["machines"][0]["health"], "unpaired")
        (self.directory / "known_hosts").write_text("192.0.2.10,halo-1 ssh-ed25519 fixture\n", encoding="utf-8", newline="\n")
        self.remote._hostkey.return_value = SimpleNamespace(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:fixture")
        self.now += 2
        trusted = self.api.read("settings")["trusted_machines"][0]
        self.assertEqual(trusted["machine"], "halo-1")
        self.assertEqual(trusted["fingerprint"], "SHA256:fixture")

    def test_missing_config_is_a_document_with_null_and_why(self):
        self.orch.load_cluster.side_effect = ValueError("not configured")
        for screen in SCREENS:
            document = self.api.read(screen)
            self.assertTrue(document["why"])
            for field in document["why"]:
                self.assertIsNone(document[field])

    def test_failed_status_is_unavailable_never_stopped_or_zero(self):
        self.orch.status.side_effect = RuntimeError("status unavailable")
        profiles = self.api.read("profiles")["profiles"]
        self.assertIsNone(profiles[0]["state"])
        self.assertEqual(profiles[0]["why"]["state"], "status unavailable")
        self.assertIsNone(self.api.read("stats")["engines"])

    def test_inventory_preserves_passive_measurements_and_lease_deadline(self):
        def node(verb, payload, timeout):
            checks = {"cpu_model": {"status": "ok", "value": "Fixture CPU"},
                      "usb4": {"module": {"status": "ok", "value": True}}} if verb == "doctor" else {
                "amd": {"status": "ok", "value": [{"id": "card0", "vram_total_bytes": {"status": "ok", "value": 100},
                    "vram_used_bytes": {"status": "ok", "value": 40}, "busy_percent": {"status": "ok", "value": 75}}]}}
            return {"schema": "hyperloom/node-v1", "checks": checks}
        self.remote.Remote.return_value.call.side_effect = node
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": [
            {"lease": "fixture", "deadline": 1100., "state": "ready", "processes": [
                {"name": "replica", "ready": True, "exe_sha256": "b" * 64}]}]}}}}
        with patch.object(self.api, "_probe", return_value={"devices": [{"vendor": "amd", "name": "Presence only"}]}):
            machine = self.api.read("devices")["machines"][0]
        self.assertEqual(machine["cpu"], "Fixture CPU")
        self.assertEqual(len(machine["devices"]), 1)
        self.assertEqual(machine["devices"][0]["memory_free_bytes"], 60)
        self.assertEqual(machine["devices"][0]["utilization_pct"], 75)
        self.assertEqual(machine["lease"]["remaining_s"], 100)
        process = self.api.read("profiles")["profiles"][0]["processes"][0]
        self.assertEqual(process["state"], "ready")
        self.assertEqual(process["binary_sha256"], "b" * 64)

    def test_default_logs_merge_captured_streams_and_portal_failures(self):
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}
        self.api._audit("up", {"profile": "qwen-replicas"}, {"state": "failed", "error": "fixture failure"})
        document = self.api.read("logs")
        self.assertTrue(any(row.get("event") == "READY" for row in document["entries"]))
        self.assertEqual(document["failures"][0]["reason"], "fixture failure")
        self.assertNotIn("hidden", json.dumps(document))

    def test_local_doctor_on_first_run_does_not_require_configuration(self):
        self.orch.load_cluster.side_effect = FileNotFoundError
        with patch.object(self.api, "_node", return_value={"schema": "hyperloom/node-v1"}) as node:
            self.assertEqual(wait_job(self.api, self.api.mutate("doctor", {}))["state"], "succeeded")
            self.assertEqual(node.call_args.args[0].access, "local")
            self.orch.load_cluster.assert_not_called()

    def test_up_uses_cli_arguments_and_audits_without_secrets(self):
        arguments = {"profile": "qwen-replicas", "router": "halo-1", "node": ["halo-1"],
                     "device": ["halo-1:gpu"], "deadline": 1140}
        result = wait_job(self.api, self.api.mutate("up", arguments))
        self.assertEqual(result["state"], "succeeded")
        self.orch.up.assert_called_once_with(self.cluster, "qwen-replicas", router="halo-1",
                                             nodes=["halo-1"], devices=["halo-1:gpu"], deadline_s=1140, approval=None)
        entries = [json.loads(line) for line in self.api.audit_path.read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["action"], "up")
        self.assertEqual(entries[-1]["result"]["state"], "succeeded")

    def test_install_down_doctor_rotate_and_host_pin_endpoints(self):
        for action, arguments in (("install", {"profile": "qwen-replicas"}),
                                  ("down", {"profile": "qwen-replicas", "force": True}),
                                  ("doctor", {"machine": "halo-1"})):
            with self.subTest(action=action):
                self.assertEqual(wait_job(self.api, self.api.mutate(action, arguments))["state"], "succeeded")
        self.orch.install.assert_called_once_with(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        self.orch.down.assert_called_once_with("qwen-replicas", force=True)
        with patch("hyperloom.web.api.secrets.rotate", return_value="do-not-expose") as rotate:
            job = wait_job(self.api, self.api.mutate("keys/rotate", {"name": "engine"}))
            self.assertEqual(job["state"], "succeeded")
            rotate.assert_called_once_with("engine")
            self.assertNotIn("do-not-expose", self.api.audit_path.read_text())
        self.remote.keyscan.return_value = [SimpleNamespace(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:fixture")]
        job = wait_job(self.api, self.api.mutate("hosts/pin", {"machine": "halo-1", "fingerprint": "SHA256:fixture"}))
        self.assertEqual(job["state"], "succeeded")
        self.remote.KnownHosts.return_value.pin.assert_called_once()

    def test_host_pin_never_pins_a_different_fingerprint(self):
        self.remote.keyscan.return_value = []
        job = wait_job(self.api, self.api.mutate("hosts/pin", {"machine": "halo-1", "fingerprint": "SHA256:fixture"}))
        self.assertEqual(job["state"], "failed")
        self.remote.KnownHosts.return_value.pin.assert_not_called()

    def test_failure_audited_and_next_job_allowed(self):
        self.orch.up.side_effect = RuntimeError("api_key=hidden failed")
        job = wait_job(self.api, self.api.mutate("up", {"profile": "qwen-replicas"}))
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("hidden", json.dumps(job))
        self.assertNotIn("hidden", self.api.audit_path.read_text())
        self.assertEqual(wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))["state"], "succeeded")

    def test_running_job_refuses_mutation_but_stop_and_cancel_run(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def up(*args, **kwargs):
            started.set()
            release.wait(3)
            return {"state": "stopped"}
        self.orch.up.side_effect = up
        job = self.api.mutate("up", {"profile": "qwen-replicas"})
        self.assertTrue(started.wait(1))
        with self.assertRaises(APIError) as refused:
            self.api.mutate("install", {"profile": "qwen-replicas"})
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))["state"], "succeeded")
        self.assertEqual(wait_job(self.api, self.api.mutate("cancel", {"job": job["job"]}))["state"], "succeeded")
        release.set()
        wait_job(self.api, job)

    def test_logs_query_is_read_only_cached_and_redacted(self):
        query = {"profile": "qwen-replicas", "process": "halo-1", "tail": "5"}
        first = self.api.read("logs", query)
        self.assertEqual(first, self.api.read("logs", query))
        self.orch.logs.assert_called_once_with("qwen-replicas", "halo-1", 5)
        self.assertNotIn("hidden", json.dumps(first))

    def test_unknown_arguments_and_bad_types_refused(self):
        for action, args in (("up", {}), ("up", {"profile": "qwen-replicas", "env": {}}),
                             ("up", {"profile": "qwen-replicas", "deadline": True}),
                             ("up", {"profile": "qwen-replicas", "node": "halo-1"}),
                             ("down", {"profile": "qwen-replicas", "force": "yes"}),
                             ("hosts/pin", {"machine": "halo-1"}), ("cancel", {})):
            with self.subTest(action=action, arguments=args), self.assertRaises(APIError):
                self.api.mutate(action, args)

    def test_engine_metrics_quantiles_counter_resets_and_route_blocker(self):
        raw = {"schema": "tensormeld/replica-router-status-v1", "status": "ok", "replicas": [
            {"id": "replica", "endpoint": "http://127.0.0.1:18501"}]}
        self.orch.status.return_value = {"qwen-replicas": {"router": raw}}
        count = [100]
        def fetch(endpoint, suffix, text=False):
            if text:
                return f"strix_generated_tokens_total {count[0]}\nstrix_queue_waiting 2\nstrix_queue_running 1\n"
            return {"schema": "strix-serve-status-v1", "requests": [
                {"id": "request", "time": 1000, "queue_ms": 10., "prefill_ms": 20., "route": "fake"}]}
        with patch("hyperloom.web.api._http", side_effect=fetch):
            first = self.api.read("stats")
            self.assertIsNone(first["engines"][0]["throughput"]["value"])
            self.assertEqual(first["engines"][0]["ttft_p50"]["value"], 30)
            self.assertIsNone(first["recent_requests"][0]["route"])
            count[0] = 200
            self.now += 10
            second = self.api.read("stats")
            self.assertEqual(second["engines"][0]["throughput"]["value"], 10)
            self.assertEqual(second["engines"][0]["ttft_p50"]["n"], 1)
            count[0] = 1
            self.now += 2
            reset = self.api.read("stats")["engines"][0]["throughput"]
            self.assertIsNone(reset["value"])
            self.assertTrue(reset["reset"])

    def test_redaction_keeps_measurements_and_key_file_paths(self):
        value = redact({"api_key": "hidden", "generated_tokens": 123, "api_key_file": "/opt/key", "argv": ["--api-key", "hidden"]})
        self.assertEqual(value["generated_tokens"], 123)
        self.assertEqual(value["api_key_file"], "/opt/key")
        self.assertNotIn("hidden", json.dumps(value))
        self.assertIsInstance(self.api.read("settings")["secrets"], dict)
        self.assertEqual(redact({"secrets": {"engine": "hidden"}}), {"secrets": "[redacted]"})


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-web-http-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "index.html").write_text('<html><head></head><body></body></html>', encoding="utf-8", newline="\n")
        self.api = Mock()
        self.api.read.return_value = {"why": {}}
        self.api.mutate.return_value = {"job": "fixture", "state": "running"}
        self.server = create_server(0, api=self.api, static_dir=directory)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(2)))
        self.headers = {"Origin": self.server.origin, "Content-Type": "application/json", "X-Hyperloom-CSRF": self.server.csrf_token}

    def request(self, method, path="/hyperloom/api/up", body=b'{}', headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=self.headers if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_server_loopback_static_injection_and_no_cors(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, data = self.request("GET", "/", body=None)
        self.assertEqual(status, 200)
        self.assertIn(self.server.csrf_token.encode(), data)
        self.assertIn('name="hyperloom-csrf"', data.decode())
        self.assertFalse(any(name.lower().startswith("access-control-") for name in headers))
        for path in ("/../api.py", "/%2e%2e/api.py", "/server.py", "/static/", "/index.html/.."):
            self.assertEqual(self.request("GET", path, body=None)[0], 404)

    def test_security_negatives_never_reach_backend(self):
        for headers, expected in (({**self.headers, "Origin": "http://192.0.2.10"}, 403),
                                  ({**self.headers, "Host": "192.0.2.10"}, 403),
                                  ({**self.headers, "X-Hyperloom-CSRF": "wrong"}, 403),
                                  ({key: value for key, value in self.headers.items() if key != "X-Hyperloom-CSRF"}, 403),
                                  ({**self.headers, "Content-Type": "text/plain"}, 415)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", headers=headers)[0], expected)
        self.assertEqual(self.request("POST", body=b"x" * 65537)[0], 413)
        for body in (b"{", b"[]", b'{"x":NaN}', b'{"x":1,"x":2}'):
            self.assertEqual(self.request("POST", body=body)[0], 400)
        self.assertEqual(self.request("GET", body=None)[0], 405)
        self.api.mutate.assert_not_called()

    def test_every_endpoint_dispatches_and_mutation_returns_job(self):
        for screen in SCREENS:
            self.assertEqual(self.request("GET", "/hyperloom/api/" + screen, body=None)[0], 200)
            self.api.read.assert_called_with(screen, {})
        for action in ("up", "down", "install", "doctor", "hosts/pin", "keys/rotate", "cancel"):
            self.assertEqual(self.request("POST", "/hyperloom/api/" + action)[0], 202)
            self.api.mutate.assert_called_with(action, {})

    def test_cli_ui_only_calls_server(self):
        with patch("hyperloom.web.server.serve") as serve, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["ui", "--port", "18701", "--no-browser"]), 0)
            serve.assert_called_once_with(18701, no_browser=True)


if __name__ == "__main__":
    unittest.main()
