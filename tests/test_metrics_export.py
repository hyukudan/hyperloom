import contextlib
import http.client
import io
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest import mock

from hyperloom import cli, metrics_export
from hyperloom.adapters import prom


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters"
ROUTER = {"address": "127.0.0.1", "port": 18600}


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_fetch(documents):
    def fetch(endpoint, suffix, text=False):
        key = (endpoint["address"], suffix)
        if key not in documents:
            raise OSError("connection refused")
        value = documents[key]
        return value if text else json.loads(value)
    return fetch


def healthy(engine_status="engine-status.json"):
    return {("127.0.0.1", "/api/status"): _fixture("router-status.json"),
            ("192.0.2.1", "/metrics"): _fixture("engine-metrics.txt"),
            ("192.0.2.1", "/api/status"): _fixture(engine_status),
            ("192.0.2.2", "/metrics"): _fixture("engine-metrics.txt"),
            ("192.0.2.2", "/api/status"): _fixture(engine_status)}


def labels(**items):
    return tuple(sorted(items.items()))


class ExportTests(unittest.TestCase):
    def test_round_trip_through_prometheus_parser(self):
        text = metrics_export.collect({"qwen": ROUTER}, fake_fetch(healthy()))
        samples = prom.parse(text)
        one = labels(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_router_replica_assigned_total", one)], 59)
        self.assertEqual(samples[("hyperloom_router_prefill_placements_total", labels(profile="qwen", placement="cache"))], 9)
        self.assertEqual(samples[("hyperloom_engine_generated_tokens_total", one)], 30)
        self.assertEqual(samples[("hyperloom_engine_waiting", one)], 1)
        self.assertEqual(samples[("hyperloom_engine_context_tokens", one)], 12288)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="router", profile="qwen"))], 1)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_status", profile="qwen",
                                                                 replica="halo-2", machine="halo-2"))], 1)
        self.assertIn("# TYPE hyperloom_engine_generated_tokens_total counter", text)
        self.assertIn("# TYPE hyperloom_engine_waiting gauge", text)
        self.assertEqual(text.count("# TYPE hyperloom_source_up "), 1)
        build = [key for key in samples if key[0] == "hyperloom_build_info"]
        self.assertEqual(len(build), 1)
        self.assertIn("engine_commit", dict(build[0][1]))

    def test_deepseek_memory_and_identity(self):
        samples = prom.parse(metrics_export.collect({"ds": ROUTER}, fake_fetch(healthy("deepseek-status.json"))))
        one = dict(profile="ds", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_engine_memory_bytes", labels(kind="trellis_bytes", **one))], 20000)
        info = [dict(key[1]) for key in samples if key[0] == "hyperloom_engine_info"]
        self.assertTrue(all(row["trellis_identity"] == "quality-gated" for row in info))

    def test_missing_sources_are_not_fabricated(self):
        documents = healthy()
        del documents[("192.0.2.2", "/metrics")]
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER, "down": None}, fake_fetch(documents)))
        two = dict(profile="qwen", replica="halo-2", machine="halo-2")
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_metrics", **two))], 0)
        self.assertNotIn(("hyperloom_engine_generated_tokens_total", labels(**two)), samples)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="router", profile="down"))], 0)
        self.assertFalse(any(dict(key[1]).get("profile") == "down" and key[0] != "hyperloom_source_up" for key in samples))

    def test_router_down_exports_no_engines(self):
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fake_fetch({})))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="router", profile="qwen"))], 0)
        self.assertFalse(any(key[0].startswith(("hyperloom_engine", "hyperloom_router")) for key in samples))

    def test_unsupported_schema_is_source_down(self):
        documents = healthy()
        documents[("192.0.2.1", "/api/status")] = json.dumps({"schema": "other"})
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fake_fetch(documents)))
        one = dict(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_status", **one))], 0)
        self.assertNotIn(("hyperloom_engine_uptime_seconds", labels(**one)), samples)

    def test_label_escaping(self):
        out = metrics_export.Exposition()
        out.add("hyperloom_engine_info", "gauge", {"model": 'a"b\\c\nd'}, 1)
        out.add("hyperloom_engine_info", "gauge", {"model": "nan"}, float("nan"))
        text = out.text()
        self.assertIn('model="a\\"b\\\\c\\nd"', text)
        self.assertEqual(prom.parse(text), {("hyperloom_engine_info", (("model", 'a"b\\c\nd'),)): 1.0})


class ServerTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.server = metrics_export.MetricsServer(f"127.0.0.1:{port}", collector=lambda: "hyperloom_up 1\n")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path="/metrics", host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.server.server_port}")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_get_metrics(self):
        response, body = self.request("GET")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), metrics_export.CONTENT_TYPE)
        self.assertEqual(body, b"hyperloom_up 1\n")

    def test_foreign_host_rejected(self):
        self.assertEqual(self.request("GET", host="attacker.example:80")[0].status, 403)

    def test_other_paths_and_methods_rejected(self):
        self.assertEqual(self.request("GET", "/api/status")[0].status, 404)
        response, _ = self.request("POST")
        self.assertEqual((response.status, response.getheader("Allow")), (405, "GET"))

    def test_non_loopback_listen_refused(self):
        for listen in ("192.0.2.10:18701", "10.0.0.1:18701"):
            with self.assertRaises(ValueError):
                metrics_export.MetricsServer(listen)


class CliTests(unittest.TestCase):
    def test_dump_prints_exposition(self):
        stream = io.StringIO()
        with mock.patch.object(metrics_export, "collect", return_value="hyperloom_up 1\n") as collect, \
                contextlib.redirect_stdout(stream):
            self.assertEqual(cli.main(["metrics", "dump", "qwen"]), 0)
        collect.assert_called_once_with(profile="qwen")
        self.assertEqual(stream.getvalue(), "hyperloom_up 1\n")

    def test_serve_refuses_lan_listen(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["metrics", "serve", "--listen", "192.0.2.10:18701"]), 1)


if __name__ == "__main__":
    unittest.main()
