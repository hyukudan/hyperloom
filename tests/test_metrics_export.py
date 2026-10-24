import contextlib
import http.client
import io
import json
from pathlib import Path
import socket
import threading
import time
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

    def test_bad_upstream_marks_only_that_source_down(self):
        base = fake_fetch(healthy())
        errors = {"/metrics": http.client.BadStatusLine("garbage"), "/api/status": http.client.IncompleteRead(b"")}

        def fetch(endpoint, suffix, text=False):
            if endpoint["address"] == "192.0.2.2":
                raise errors[suffix]
            return base(endpoint, suffix, text)
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fetch))
        two = dict(profile="qwen", replica="halo-2", machine="halo-2")
        one = dict(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_metrics", **two))], 0)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_status", **two))], 0)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_status", **one))], 1)

    def test_router_endpoint_without_port_or_address_is_source_down(self):
        # The real fetcher raises KeyError / TypeError for such endpoints; neither escapes the scrape.
        routers = {"noport": {"address": "127.0.0.1"}, "noaddr": {"port": 1}, "badaddr": {"address": 5, "port": 1}}
        samples = prom.parse(metrics_export.collect(routers))
        for name in routers:
            self.assertEqual(samples[("hyperloom_source_up", labels(source="router", profile=name))], 0)

    def test_unexpected_fetch_exception_is_source_down(self):
        def fetch(endpoint, suffix, text=False):
            raise ZeroDivisionError()
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fetch))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="router", profile="qwen"))], 0)

    def test_overall_deadline_reports_unfinished_sources_down(self):
        base = fake_fetch(healthy())
        release = threading.Event()

        def fetch(endpoint, suffix, text=False):
            if endpoint["address"] == "192.0.2.2":
                release.wait(10)
            return base(endpoint, suffix, text)
        start = time.monotonic()
        try:
            samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fetch, deadline_s=0.5))
        finally:
            release.set()
        self.assertLess(time.monotonic() - start, 3)
        two = dict(profile="qwen", replica="halo-2", machine="halo-2")
        one = dict(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_metrics", **two))], 0)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="engine_metrics", **one))], 1)
        self.assertNotIn(("hyperloom_engine_generated_tokens_total", labels(**two)), samples)

    def test_routers_are_fetched_in_parallel(self):
        def fetch(endpoint, suffix, text=False):
            time.sleep(0.4)
            raise OSError("down")
        routers = {f"p{i}": dict(ROUTER) for i in range(6)}
        start = time.monotonic()
        samples = prom.parse(metrics_export.collect(routers, fetch))
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(sum(v == 0 for k, v in samples.items() if k[0] == "hyperloom_source_up"), 6)

    def test_slot_units_and_state_are_separate_families(self):
        samples = prom.parse(metrics_export.collect({"ds": ROUTER}, fake_fetch(healthy("deepseek-status.json"))))
        one = dict(profile="ds", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_engine_slots", labels(kind="total", **one))], 2)
        self.assertEqual(samples[("hyperloom_engine_slot_positions", labels(kind="pool_positions", **one))], 65536)
        self.assertEqual(samples[("hyperloom_engine_slot_pool_bytes", labels(**one))], 2000)
        self.assertFalse(any(key[0] == "hyperloom_engine_slots" and dict(key[1])["kind"] in ("pool_bytes", "context_tokens")
                             for key in samples))
        self.assertEqual(samples[("hyperloom_engine_state", labels(state="ready", **one))], 1)
        info = [dict(key[1]) for key in samples if key[0] == "hyperloom_engine_info"]
        self.assertTrue(info and all("state" not in row for row in info))

    def test_prefill_services_from_router_view(self):
        documents = healthy()
        status = json.loads(documents[("127.0.0.1", "/api/status")])
        status["prefill"]["services"] = [{"id": "rtx", "node": "workstation", "endpoint": "127.0.0.1:8095",
                                          "state": "up", "inflight": 1, "max_inflight": 2, "assigned": 7,
                                          "completed": 6, "failed": 0, "last_error": ""}]
        documents[("127.0.0.1", "/api/status")] = json.dumps(status)
        samples = prom.parse(metrics_export.collect({"ds": ROUTER}, fake_fetch(documents)))
        svc = labels(profile="ds", service="rtx", machine="workstation")
        self.assertEqual(samples[("hyperloom_router_prefill_service_up", svc)], 1)
        self.assertEqual(samples[("hyperloom_router_prefill_service_assigned_total", svc)], 7)
        self.assertEqual(samples[("hyperloom_router_prefill_service_inflight", svc)], 1)
        # The service itself is not fetched (no /metrics until R13).
        self.assertFalse(any(dict(key[1]).get("replica") == "rtx" for key in samples))

    def test_duplicate_replica_ids_are_flagged(self):
        documents = healthy()
        status = json.loads(documents[("127.0.0.1", "/api/status")])
        status["replicas"].append(dict(status["replicas"][1], id="halo-1", assigned=999))
        documents[("127.0.0.1", "/api/status")] = json.dumps(status)
        samples = prom.parse(metrics_export.collect({"qwen": ROUTER}, fake_fetch(documents)))
        self.assertEqual(samples[("hyperloom_router_duplicate_ids", labels(profile="qwen", kind="replica"))], 1)
        self.assertEqual(samples[("hyperloom_router_duplicate_ids", labels(profile="qwen", kind="prefill_service"))], 0)
        one = labels(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_router_replica_assigned_total", one)], 59)
        clean = prom.parse(metrics_export.collect({"qwen": ROUTER}, fake_fetch(healthy())))
        self.assertEqual(clean[("hyperloom_router_duplicate_ids", labels(profile="qwen", kind="replica"))], 0)

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

    def test_query_string_is_ignored(self):
        self.assertEqual(self.request("GET", "/metrics?x=1")[0].status, 200)

    def test_collector_failure_is_500(self):
        def broken():
            raise KeyError("boom")
        self.server.collector = broken
        response, body = self.request("GET")
        self.assertEqual(response.status, 500)
        self.assertIn(b"KeyError", body)
        self.server.collector = lambda: "hyperloom_up 1\n"
        self.assertEqual(self.request("GET")[0].status, 200)

    def test_concurrent_scrapes_are_bounded(self):
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(10)
            return "hyperloom_up 1\n"
        self.server.collector = slow
        self.server.scrapes = threading.BoundedSemaphore(1)
        first = {}
        worker = threading.Thread(target=lambda: first.setdefault("status", self.request("GET")[0].status))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            response, _ = self.request("GET")
            self.assertEqual((response.status, response.getheader("Retry-After")), (503, "5"))
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(first["status"], 200)

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
