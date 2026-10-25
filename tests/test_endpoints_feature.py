"""Other chat-completions-compatible endpoints (F17): allow-list, transport bounds, identification, collector."""

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import config
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API, redact
from hyperloom.web.features import endpoints


FIXTURES = Path(__file__).parent / "fixtures" / "endpoints"
KEY = "ab" * 32
# Addresses the allow-list must refuse, built from numbers so that no such literal appears in the repository.
PUBLIC = str(ipaddress.IPv4Address(0x08080808))
LINK_LOCAL = str(ipaddress.IPv4Address(0xA9FE0101))
UNSPECIFIED = str(ipaddress.IPv4Address(0))


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeServer:
    """A loopback HTTP server answering from a route table; records every request's path and headers."""

    def __init__(self, routes=None, default=(404, "text/plain", b"not found"), host="127.0.0.1", port=0):
        self.routes, self.default, self.seen = dict(routes or {}), default, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.seen.append((self.path, dict(self.headers)))
                route = owner.routes.get(self.path.split("?")[0])
                if route is None and self.path.startswith("/hl-probe-"):
                    route = owner.routes.get("*")
                if callable(route):
                    try:
                        return route(self)
                    except OSError:          # the client gave up (the bound under test)
                        return None
                status, kind, body = route or owner.default
                body = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                if 300 <= status < 400:
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [path for path, _ in self.seen]


def json_route(obj, status=200):
    return (status, "application/json", json.dumps(obj))


MODELS = json_route({"object": "list", "data": [{"id": "model-a", "object": "model"}]})


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def cluster():
    machines = {}
    for mid, system, access, address in (("workstation", "windows", "local", "127.0.0.1"),
                                          ("halo-1", "linux", "ssh", "192.0.2.10"),
                                          ("halo-2", "linux", "ssh", "192.0.2.11")):
        machines[mid] = config.Machine(mid, system, access, address, mid, 22, "/opt/hyperloom", True, [{"id": "gpu"}])
    return config.Cluster(machines, {}, {}, {}, {}, {})


def target(port, **extra):
    return {"id": "t1", "rev": "0" * 16, "label": "test", "scheme": "http", "host": "127.0.0.1", "port": port,
            "key_name": None, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       clock=self.clock, audit_path=Path(self.dir.name) / "audit.jsonl")
        self.cluster = cluster()
        self.api.orchestrate.load_cluster.return_value = self.cluster
        self.api.telemetry = Store(":memory:", clock=self.clock)
        self.addCleanup(self.api.telemetry.close)
        registry = features.Registry()
        endpoints.register(registry)
        features.reset(registry)
        self.addCleanup(features.reset)

    def serve(self, routes, **kw):
        server = FakeServer(routes, **kw)
        self.addCleanup(server.close)
        return server


class AllowListTests(Base):
    def save(self, **arguments):
        return self.api.feature_post("endpoints/save", arguments)[1]

    def test_hosts_ports_ids_and_keys_are_validated(self):
        for bad in ({"host": "name-not-resolved"}, {"host": PUBLIC}, {"host": LINK_LOCAL}, {"host": UNSPECIFIED},
                    {"host": "192.0.2.10%eth0"}, {"port": 0}, {"port": "80"}, {"port": True}, {"scheme": "ftp"},
                    {"label": ""}, {"label": "x" * 65}, {"label": "a\nb"}, {"key_name": "Bad Name"}, {"id": "-x"},
                    {"extra": 1}, {"host": "127.0.0.1", "port": 18700}):
            arguments = {"label": "box", "host": "192.0.2.10", "port": 8080, **bad}
            with self.subTest(bad=bad):
                with self.assertRaises(Exception) as caught:
                    self.api.feature_post("endpoints/save", arguments)
                self.assertEqual(getattr(caught.exception, "status", None), 400)
        saved = self.save(label="RTX box", host="halo-1", port=8080, key_name="box-key")["target"]
        self.assertEqual((saved["id"], saved["host"], saved["address"], saved["key_name"]),
                         ("rtx-box", "halo-1", "192.0.2.10", "box-key"))
        again = self.save(label="RTX box", host="203.0.113.5", port=8080)["target"]
        self.assertEqual(again["id"], "rtx-box-2")
        with self.assertRaises(Exception) as caught:            # same destination through the machine alias
            self.save(label="dup", host="192.0.2.10", port=8080)
        self.assertIn("already the target rtx-box", str(caught.exception))
        audit = [json.loads(line) for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(audit[-1]["result"]["state"], "refused")
        self.assertTrue(any(a["result"]["state"] == "succeeded" for a in audit))

    def test_limit_update_remove_and_new_revision(self):
        for number in range(endpoints.MAX_TARGETS):
            self.save(label=f"e{number}", host=f"198.51.100.{number + 1}", port=8000)
        with self.assertRaises(Exception) as caught:
            self.save(label="one too many", host="203.0.113.1", port=8000)
        self.assertIn("at most 32", str(caught.exception))
        store = self.api.telemetry
        before = {t["id"]: t["rev"] for t in endpoints.load(store)}
        self.save(id="e0", label="renamed", host="203.0.113.2", port=9000)          # update in place, still 32
        after = {t["id"]: t["rev"] for t in endpoints.load(store)}
        self.assertNotEqual(before["e0"], after["e0"])
        self.assertEqual(before["e1"], after["e1"])
        store.put(endpoints.NAMESPACE, "probe/e1", {"rev": after["e1"]})
        self.assertEqual(self.api.feature_post("endpoints/remove", {"id": "e1"})[1], {"removed": "e1"})
        self.assertIsNone(store.get(endpoints.NAMESPACE, "probe/e1"))
        with self.assertRaises(Exception) as caught:
            self.api.feature_post("endpoints/remove", {"id": "e1"})
        self.assertEqual(caught.exception.status, 404)

    def test_concurrent_edits_from_two_processes_lose_nothing(self):
        path = str(Path(self.dir.name) / "telemetry.sqlite3")
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        add = lambda tid, rev: (lambda rows: (rows + [dict(target(len(rows) + 1), id=tid, rev=rev)], tid))
        endpoints._edit(first, add("base", "0" * 16), 1.0)
        real = endpoints._raw
        raced = []

        def racing(store):
            text = real(store)
            if store is first and not raced:
                # Another process saves between our read and our compare-and-swap.
                rows = endpoints._parse_targets(text) + [dict(target(9), id="other", rev="1" * 16)]
                second.execute("UPDATE docs SET value=? WHERE namespace=? AND key=?",
                               (endpoints._encode({"version": 1, "targets": rows}), endpoints.PREFS, endpoints.TARGETS_KEY))
                raced.append(True)
            return text
        with mock.patch.object(endpoints, "_raw", side_effect=racing):
            endpoints._edit(first, add("mine", "2" * 16), 2.0)
        self.assertEqual(raced, [True])
        self.assertEqual(sorted(t["id"] for t in endpoints.load(second)), ["base", "mine", "other"])

    def test_documents_without_a_store_and_get_never_creates_it(self):
        self.api.telemetry = None
        document = self.api.read("endpoints", {})
        self.assertIsNone(document["targets"])
        self.assertEqual(document["why"]["targets"], endpoints.NO_STORE)
        self.assertIsNone(self.api.read("endpoints/exposure", {})["rows"])
        self.assertIsNone(getattr(self.api, "telemetry", None))
        self.assertFalse((Path(self.dir.name) / "telemetry.sqlite3").exists())
        with self.assertRaises(Exception):
            self.api.read("endpoints", {"x": "1"})


class TransportTests(Base):
    def test_redirects_are_not_followed_and_credentials_never_reach_them(self):
        elsewhere = self.serve({"/v1/models": MODELS})
        server = self.serve({"/v1/models": (302, "text/plain", "moved")})
        reply = endpoints.Session("http", "127.0.0.1", server.port, KEY).get("/v1/models")
        self.assertEqual(reply.status, 302)
        self.assertIn("redirect refused", reply.error)
        self.assertIsNone(reply.body)
        self.assertEqual(elsewhere.seen, [])
        self.assertEqual(server.seen[0][1].get("Authorization"), "Bearer " + KEY)
        anonymous = endpoints.Session("http", "127.0.0.1", server.port, KEY).get("/v1/models", auth=False)
        self.assertIsNone(server.seen[1][1].get("Authorization"))
        self.assertEqual(anonymous.status, 302)

    def test_response_cap_and_elapsed_time_bound(self):
        big = self.serve({"/v1/models": (200, "application/json", b"x" * (endpoints.CAP_BYTES + 10))})
        self.assertEqual(endpoints.Session("http", "127.0.0.1", big.port).get("/v1/models").error, "response exceeds 256 KiB")

        def trickle(handler):
            handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
            handler.wfile.flush()
            for _ in range(40):          # one header byte every 0.1 s: every recv succeeds, the total never ends
                handler.wfile.write(b"X")
                handler.wfile.flush()
                time.sleep(0.1)
        slow = self.serve({"/v1/models": trickle})
        started = time.monotonic()
        reply = endpoints.Session("http", "127.0.0.1", slow.port, timeout_s=0.5).get("/v1/models")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(reply.error.startswith("timed out"), reply.error)

        def slow_body(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "1000")
            handler.end_headers()
            for _ in range(40):
                handler.wfile.write(b"{")
                handler.wfile.flush()
                time.sleep(0.1)
        body = self.serve({"/v1/models": slow_body})
        started = time.monotonic()
        reply = endpoints.Session("http", "127.0.0.1", body.port, timeout_s=0.5).get("/v1/models")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(reply.error.startswith("timed out"), reply.error)

    def test_trickling_chunked_body_is_cut_at_the_budget(self):
        def chunked(handler):
            handler.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            handler.wfile.flush()
            for _ in range(40):          # a chunk-size line that never ends
                handler.wfile.write(b"1")
                handler.wfile.flush()
                time.sleep(0.1)
        server = self.serve({"/v1/models": chunked})
        started = time.monotonic()
        reply = endpoints.Session("http", "127.0.0.1", server.port, timeout_s=0.3).get("/v1/models")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(reply.error.startswith("timed out"), reply.error)

    def test_host_header_is_the_configured_host(self):
        server = self.serve({"/v1/models": MODELS})
        self.cluster.machines["halo-9"] = config.Machine("halo-9", "linux", "ssh", "127.0.0.1", "halo-9", 22, "/opt", True, [])
        endpoints.probe(dict(target(server.port), host="halo-9"), self.cluster, None, now=self.clock())
        self.assertEqual(server.seen[0][1]["Host"], f"halo-9:{server.port}")

    def test_request_budget_and_refused_connections(self):
        server = self.serve({"/v1/models": MODELS})
        session = endpoints.Session("http", "127.0.0.1", server.port, max_requests=1)
        self.assertTrue(session.get("/v1/models").ok())
        self.assertIn("budget", session.get("/v1/models").error)
        closed = FakeServer()
        port = closed.port
        closed.close()
        self.assertEqual(endpoints.Session("http", "127.0.0.1", port).get("/v1/models").error, "connection refused")


class IdentificationTests(Base):
    def probe(self, server, previous=None, identify=False, **extra):
        return endpoints.probe(target(server.port, **extra), self.cluster, previous, now=self.clock(), identify=identify,
                               key_lookup=lambda name: KEY)

    def test_each_positive_signature(self):
        ours = self.serve({"/api/status": (200, "application/json", fixture("strix-status.json")),
                           "/metrics": (200, "text/plain", fixture("strix-metrics.txt")), "/v1/models": MODELS})
        result = self.probe(ours)
        self.assertEqual((result["kind"], result["up"], result["models_total"]), ("strix", True, 1))
        self.assertEqual(result["panel"]["generated_tokens_total"], 9910)
        self.assertTrue(result["instance"].startswith("start-"))
        props = self.serve({"/props": json_route({"default_generation_settings": {"n_ctx": 8192}}), "/v1/models": MODELS})
        self.assertEqual(self.probe(props)["kind"], "props-server")
        versioned = self.serve({"/version": json_route({"version": "1.2.3"}),
                                "/v1/models": json_route({"object": "list", "data": [{"id": "m", "max_model_len": 32768}]})})
        result = self.probe(versioned)
        self.assertEqual((result["kind"], result["models"]), ("version-server", [{"id": "m", "max_model_len": 32768}]))
        generic = self.serve({"/v1/models": MODELS})
        self.assertEqual(self.probe(generic)["kind"], "generic")
        nothing = self.serve({"/v1/models": json_route({"error": "unauthorized"}, 401)})
        result = self.probe(nothing)
        self.assertEqual((result["kind"], result["up"]), ("unknown", False))
        self.assertIn("refused", result["why"]["kind"])

    def test_catch_all_never_identifies_nor_overrides(self):
        catch_all = self.serve({}, default=(200, "text/html", "<html>hello</html>"))
        result = self.probe(catch_all)
        self.assertEqual((result["catch_all"], result["kind"], result["up"]), (True, "unknown", False))
        self.assertIn("catch-all", result["why"]["kind"])
        self.assertTrue(catch_all.paths()[0].startswith("/hl-probe-"))
        # A catch-all that also has a real signature keeps it; the generic shape never replaces it.
        both = self.serve({"/props": json_route({"default_generation_settings": {}}), "/v1/models": MODELS},
                          default=(200, "application/json", json.dumps({"object": "list", "data": []})))
        result = self.probe(both)
        self.assertEqual((result["catch_all"], result["kind"]), (True, "props-server"))

    def test_identification_is_cached_per_revision_and_forced(self):
        server = self.serve({"/v1/models": MODELS})
        first = self.probe(server)
        self.assertEqual(len(first["requests"]), 5)          # no max_model_len: /version is not asked
        self.clock.now += 15
        second = self.probe(server, previous=first)
        self.assertEqual(second["requests"], ["/v1/models"])
        self.assertEqual(second["identified_at"], first["identified_at"])
        self.assertEqual(len(self.probe(server, previous=first, identify=True)["requests"]), 5)
        moved = dict(first, source="f" * 16)                   # another revision or destination inherits nothing
        self.assertEqual(len(self.probe(server, previous=moved)["requests"]), 5)
        self.clock.now += endpoints.IDENTIFY_EVERY_S
        self.assertEqual(len(self.probe(server, previous=first)["requests"]), 5)

    def test_keys_come_from_the_store_and_echoes_are_scrubbed(self):
        echo = self.serve({"/v1/models": json_route({"object": "list", "data": [{"id": KEY}, {"id": "x-" + KEY}]}),
                           "/api/status": json_route({"schema": "strix-serve-status-v1", "model": KEY,
                                                      "uptime_seconds": 10.0, "state": "ready"})})
        result = self.probe(echo, key_name="box-key")
        self.assertEqual(echo.seen[0][1]["Authorization"], "Bearer " + KEY)
        text = json.dumps(redact(result))
        self.assertNotIn(KEY, text)
        self.assertEqual(result["models"][0]["id"], "[redacted]")
        self.assertEqual(result["panel"]["model"], "[redacted]")
        missing = endpoints.probe(target(echo.port, key_name="absent"), self.cluster, None, now=self.clock(),
                                  key_lookup=lambda name: None)
        self.assertIn("not in the secret store", missing["why"]["key"])
        self.assertNotIn("Authorization", echo.seen[-1][1])


class CollectorTests(Base):
    def setUp(self):
        super().setUp()
        # Fake engines report a scripted uptime, so replies are stamped at the round's start (no real latency).
        patcher = mock.patch.object(endpoints, "_since", return_value=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, port, **extra):
        return self.api.feature_post("endpoints/save", {"label": "box", "host": "127.0.0.1", "port": port, **extra})[1]["target"]

    def test_series_documents_and_token_deltas(self):
        uptime = {"value": 100.0}
        counter = {"value": 500}

        def status(handler):
            body = json.loads(fixture("strix-status.json"))
            body["uptime_seconds"] = uptime["value"]
            raw = json.dumps(body).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)

        def metrics(handler):
            raw = fixture("strix-metrics.txt").replace("strix_generated_tokens_total 9910",
                                                       f"strix_generated_tokens_total {counter['value']}").encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        server = self.serve({"/api/status": status, "/metrics": metrics, "/v1/models": MODELS})
        saved = self.add(server.port)
        store = self.api.telemetry
        points = endpoints.collect(self.api, self.clock())
        store.record(points, ts=self.clock(), interval=15)
        self.assertIn(("endpoint_up", {"endpoint": saved["id"]}, 1, "state"), points)
        self.assertIn(("endpoint_models", {"endpoint": saved["id"]}, 1, "gauge"), points)
        self.assertEqual(store.descriptor("endpoint_up")["legend"], {"0": "down", "1": "up"})
        for step, (count, up) in enumerate(((560, 115.0), (600, 130.0))):
            self.clock.now += 15
            counter["value"], uptime["value"] = count, up
            endpoints.collect(self.api, self.clock())
        lines = store.query("endpoint_generated_tokens", self.clock() - 100, self.clock() + 1, tier="raw")["lines"]
        self.assertEqual([p[1] for p in lines[0]["points"]], [60.0, 40.0])
        # A restart (new start time) whose counter is already above the old value is a new instance, not a delta.
        self.clock.now += 15
        counter["value"], uptime["value"] = 900, 5.0
        endpoints.collect(self.api, self.clock())
        lines = store.query("endpoint_generated_tokens", self.clock() - 100, self.clock() + 1, tier="raw")["lines"]
        self.assertEqual(len(lines[0]["points"]), 2)
        document = self.api.read("endpoints", {})
        probe = document["targets"][0]["probe"]
        self.assertEqual(probe["kind"], "strix")
        self.assertIsNone(probe["counters"]["generated_tokens"]["delta"])
        self.assertNotIn("instance", probe)

    def engine(self, state, **where):
        """A fake engine whose uptime and generated-token counter the test sets."""
        def status(handler):
            body = json.loads(fixture("strix-status.json"))
            body["uptime_seconds"] = state["uptime"]
            raw = json.dumps(body).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)

        def metrics(handler):
            raw = fixture("strix-metrics.txt").replace("strix_generated_tokens_total 9910",
                                                       f"strix_generated_tokens_total {state['count']}").encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        return self.serve({"/api/status": status, "/metrics": metrics, "/v1/models": MODELS}, **where)

    def generated(self):
        lines = self.api.telemetry.query("endpoint_generated_tokens", 0, self.clock() + 1, tier="raw")["lines"]
        return [p[1] for line in lines for p in line["points"]]

    def test_a_restart_with_a_higher_counter_and_a_close_start_is_a_new_instance(self):
        state = {"uptime": 5.0, "count": 100}
        self.add(self.engine(state).port)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 15                     # restarted 10 s ago: uptime 10, counter already above the old one
        state.update(uptime=10.0, count=150)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [])
        self.clock.now += 15
        state.update(uptime=25.0, count=180)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [30.0])

    def test_a_moved_machine_address_starts_a_new_source(self):
        first_state = {"uptime": 100.0, "count": 100}
        first = self.engine(first_state)
        second_state = {"uptime": 115.0, "count": 1000}           # matching uptime, a much higher counter
        second = self.engine(second_state, host="127.0.0.2", port=first.port)
        machines = self.cluster.machines
        machine = lambda address: config.Machine("halo-9", "linux", "ssh", address, "halo-9", 22, "/opt/hyperloom", True, [])
        machines["halo-9"] = machine("127.0.0.1")
        saved = self.api.feature_post("endpoints/save", {"label": "box", "host": "halo-9", "port": first.port})[1]["target"]
        endpoints.collect(self.api, self.clock())
        before = self.api.telemetry.get(endpoints.NAMESPACE, "probe/" + saved["id"])
        machines["halo-9"] = machine("127.0.0.2")                 # the alias moves; the target is not saved
        self.assertIsNone(self.api.read("endpoints", {})["targets"][0]["probe"])      # the old destination's result
        self.clock.now += 15
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [])                    # never continued across destinations
        after = self.api.telemetry.get(endpoints.NAMESPACE, "probe/" + saved["id"])
        self.assertNotEqual(before["source"], after["source"])
        self.assertEqual(len(after["requests"]), 5)               # identified again (stray, status, metrics, models, status)
        self.assertTrue(second.seen)
        self.clock.now += 15
        second_state.update(uptime=130.0, count=1100)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [100.0])

    def test_conditional_delete_spares_a_recreated_target(self):
        server = self.serve({"/v1/models": MODELS})
        saved = self.add(server.port)
        store = self.api.telemetry
        old = endpoints.load(store)[0]["rev"]
        endpoints.collect(self.api, self.clock())
        self.api.feature_post("endpoints/remove", {"id": saved["id"]})
        again = self.add(server.port)                       # the same id, a new revision
        self.clock.now += 15
        endpoints.collect(self.api, self.clock())
        endpoints._drop(store, again["id"], old)            # the delayed delete of the removed revision
        self.assertIsNotNone(store.get(endpoints.NAMESPACE, "probe/" + again["id"]))

    def test_a_reported_instance_id_wins_over_the_uptime_inference(self):
        state = {"uptime": 2.0, "count": 100, "instance": "first"}

        def status(handler):
            body = json.loads(fixture("strix-status.json"))
            body.update(uptime_seconds=state["uptime"], instance_id=state["instance"])
            raw = json.dumps(body).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)

        def metrics(handler):
            raw = fixture("strix-metrics.txt").replace("strix_generated_tokens_total 9910",
                                                       f"strix_generated_tokens_total {state['count']}").encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        self.add(self.serve({"/api/status": status, "/metrics": metrics, "/v1/models": MODELS}).port)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 15          # uptime advanced like the clock, but the engine says it is another instance
        state.update(uptime=16.0, count=150, instance="second")
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [])

    def test_manual_rounds_never_account_tokens(self):
        state = {"uptime": 100.0, "count": 100}
        self.add(self.engine(state).port)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 1
        state.update(uptime=101.0, count=120)
        results = endpoints.run_round(self.api, self.clock(), identify=True)
        self.assertEqual(self.generated(), [])
        self.assertIn("sampler", next(iter(results.values()))["why"]["counters"])
        self.assertIn("sampler", self.api.read("endpoints", {})["targets"][0]["probe"]["why"]["counters"])
        self.clock.now += 14
        state.update(uptime=115.0, count=150)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [50.0])         # the sampler's interval carries the whole increase

    def test_counters_read_across_a_replacement_are_not_accounted(self):
        replies = {"n": 0}

        def status(handler):
            replies["n"] += 1
            body = json.loads(fixture("strix-status.json"))
            # Rounds 1-2 see engine A on both sides of /metrics; in round 2 the second reply comes from engine B.
            body.update(uptime_seconds=100.0, instance_id="a" if replies["n"] < 4 else "b")
            raw = json.dumps(body).encode()
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        self.add(self.serve({"/api/status": status, "/metrics": (200, "text/plain", fixture("strix-metrics.txt")),
                             "/v1/models": MODELS}).port)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 15
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [])
        probe = self.api.read("endpoints", {})["targets"][0]["probe"]
        self.assertIn("around the metrics read", probe["why"]["counters"])

    def test_one_accounting_per_interval_across_sampler_instances(self):
        state = {"uptime": 100.0, "count": 100}
        self.add(self.engine(state).port)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 15
        state.update(uptime=115.0, count=130)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 1                    # another portal's sampler, one second later
        state.update(uptime=116.0, count=150)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [30.0])
        self.assertIn("at most once per", self.api.read("endpoints", {})["targets"][0]["probe"]["why"]["counters"])
        self.clock.now += 14
        state.update(uptime=130.0, count=170)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [30.0, 40.0])         # the skipped sample's tokens are not lost

    def test_claims_survive_a_configuration_that_fails_to_load(self):
        state = {"uptime": 100.0, "count": 100}
        server = self.engine(state)
        self.cluster.machines["halo-9"] = config.Machine("halo-9", "linux", "ssh", "127.0.0.1", "halo-9", 22, "/opt", True, [])
        self.api.feature_post("endpoints/save", {"label": "box", "host": "halo-9", "port": server.port})
        endpoints.collect(self.api, self.clock())
        self.clock.now += 15
        state.update(uptime=115.0, count=120)
        endpoints.collect(self.api, self.clock())
        self.clock.now += 1
        self.api.orchestrate.load_cluster.side_effect = ValueError("configuration unreadable")
        endpoints.collect(self.api, self.clock())
        self.api.orchestrate.load_cluster.side_effect = None
        self.clock.now += 1
        state.update(uptime=117.0, count=140)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [20.0])           # no early accounting after the failure
        self.clock.now += 13
        state.update(uptime=130.0, count=160)
        endpoints.collect(self.api, self.clock())
        self.assertEqual(self.generated(), [20.0, 40.0])

    def test_a_rejected_publication_returns_no_points(self):
        server = self.serve({"/v1/models": MODELS})
        self.add(server.port)
        with mock.patch.object(endpoints, "_publish", return_value=False):
            self.assertEqual(endpoints.collect(self.api, self.clock()), [])

    def test_without_uptime_no_token_deltas(self):
        server = self.serve({"/metrics": (200, "text/plain", fixture("strix-metrics.txt")), "/v1/models": MODELS})
        self.add(server.port)
        for _ in range(3):
            endpoints.collect(self.api, self.clock())
            self.clock.now += 15
        self.assertEqual(self.api.telemetry.query("endpoint_generated_tokens", 0, self.clock())["lines"], [])
        probe = self.api.read("endpoints", {})["targets"][0]["probe"]
        self.assertIn("no instance identity", probe["why"]["counters"])

    def test_removed_or_resaved_targets_are_not_published_late(self):
        server = self.serve({"/v1/models": MODELS})
        saved = self.add(server.port)
        store = self.api.telemetry
        old_rev = endpoints.load(store)[0]["rev"]
        result = endpoints.probe(endpoints.load(store)[0], self.cluster, None, now=self.clock())
        self.api.feature_post("endpoints/save", {"id": saved["id"], "label": "box", "host": "127.0.0.1",
                                                 "port": server.port})
        endpoints._publish(store, saved["id"], result, self.clock())          # finished after the re-save
        self.assertIsNone(store.get(endpoints.NAMESPACE, "probe/" + saved["id"]))
        current = endpoints.load(store)[0]
        self.assertNotEqual(current["rev"], old_rev)
        fresh = endpoints.probe(current, self.cluster, None, now=self.clock() + 10)
        endpoints._publish(store, saved["id"], fresh, self.clock() + 10)
        older = dict(fresh, checked_at=self.clock(), up=False)
        endpoints._publish(store, saved["id"], older, self.clock())           # an older observation arrives late
        self.assertTrue(store.get(endpoints.NAMESPACE, "probe/" + saved["id"])["up"])
        self.api.feature_post("endpoints/remove", {"id": saved["id"]})
        endpoints._publish(store, saved["id"], fresh, self.clock() + 20)
        self.assertIsNone(store.get(endpoints.NAMESPACE, "probe/" + saved["id"]))

    def test_round_bound_with_the_maximum_population(self):
        def hang(handler):
            time.sleep(2)
        slow = self.serve({"/v1/models": hang, "*": hang})
        self.add(slow.port, id="t0")
        for number in range(1, endpoints.MAX_TARGETS):          # nothing listens there: refused or timed out
            self.api.feature_post("endpoints/save", {"label": "box", "host": f"127.0.0.{number + 1}",
                                                     "port": slow.port, "id": f"t{number}"})
        factory = partial(endpoints.Session, timeout_s=0.3, deadline_s=1.0)
        started = time.monotonic()
        with mock.patch.object(endpoints, "ROUND_WAIT_S", 2.0):
            results = endpoints.run_round(self.api, self.clock(), session_factory=factory)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len(results), endpoints.MAX_TARGETS)
        self.assertFalse(any(r["up"] for r in results.values()))

    def test_check_post_forces_identification_and_runs_exposure(self):
        server = self.serve({"/v1/models": MODELS})
        self.add(server.port)
        endpoints.collect(self.api, self.clock())
        seen = len(server.seen)
        self.clock.now += 1
        result = self.api.feature_post("endpoints/check", {})[1]
        self.assertEqual(result["probed"], ["box"])
        self.assertGreaterEqual(len(server.seen) - seen, 5)                  # re-identified, not the cached path
        self.assertIsNotNone(self.api.read("endpoints/exposure", {})["rows"])
        with self.assertRaises(Exception):
            self.api.feature_post("endpoints/check", {"now": True})


class TextRuleTests(unittest.TestCase):
    def test_package_files_carry_only_loopback_and_documentation_addresses(self):
        root = Path(__file__).resolve().parents[1]
        files = [root / "hyperloom/web/features/endpoints.py", root / "hyperloom/cliext/endpoints.py",
                 root / "hyperloom/web/static/pages/endpoints.js", root / "hyperloom/web/static/pages/endpoints.css",
                 root / "tests/ui/seeds/endpoints.py", *root.glob("tests/test_endpoints_*.py"),
                 *(root / "tests/fixtures/endpoints").iterdir()]
        allowed = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for literal in re.findall(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])", text):
                try:
                    address = ipaddress.ip_address(literal)
                except ValueError:
                    continue
                self.assertTrue(any(address in net for net in allowed), f"{path.name}: {literal}")


if __name__ == "__main__":
    unittest.main()
