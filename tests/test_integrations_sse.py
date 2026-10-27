"""F44: GET /hyperloom/api/stream on the real portal server with a fake API."""

import http.client
import json
import threading
import time
import unittest
from unittest import mock

from hyperloom.web import features, server
from hyperloom.web.features import integrations


SECRET = "sentinel-secret-7f3a"


class FakeAPI:
    def __init__(self):
        self.version = 0
        self.clock = time.time
        self.fail = False
        self.empty = False

    def snapshot(self):
        raise AssertionError("the stream built a snapshot (node and engine calls)")

    def cached_snapshot(self):
        if self.empty:
            return None
        if self.fail:
            raise RuntimeError("password=" + SECRET + " at the peer")
        return {
            "devices": {"summary": {"online": 2, "endpoint": "http://" + SECRET}, "machines": [
                {"id": "halo-1", "name": "Halo 1", "health": "online", "ssh_target": SECRET, "http_address": SECRET,
                 "roles": [{"profile": "qwen", "role": "replica", "argv": ["--api-key", SECRET]}],
                 "accelerators": [{"name": "gpu", "utilization_pct": 40 + self.version, "path": SECRET}],
                 "why": {"os": "probe failed: " + SECRET}}]},
            "profiles": {"profiles": [{"id": "qwen", "state": "running", "configuration": {"key": SECRET},
                                      "effective_configuration": {"argv": [SECRET]},
                                      "processes": [{"argv": [SECRET]}], "history": [SECRET]}]},
            "stats": {"engines": [{"id": "halo-1", "profile": "qwen", "throughput": {"value": 12.5, "endpoint": SECRET},
                                   "status": {"state": "ready", "description": SECRET}}]},
            "settings": {"configuration": {"secret": SECRET}}, "logs": {"entries": [SECRET]},
        }

    def read(self, name, query):
        return {"kpis": {"throughput": {"value": 3, "url": SECRET}}, "machines": [{"id": "halo-1", "headroom": {
            "limiting": "unified", "address": SECRET}}], "secret_field": SECRET}


class StreamTests(unittest.TestCase):
    def setUp(self):
        reg = features.Registry()
        integrations.register(reg)
        features.reset(reg)
        self.addCleanup(features.reset)
        self.api = FakeAPI()
        self.server = server.create_server(0, api=self.api)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.host = self.server.origin.split("://", 1)[1]
        for name, value in (("STREAM_INTERVAL_S", 0.05), ("STREAM_HEARTBEAT_S", 0.2)):
            patcher = mock.patch.object(integrations, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, path="/hyperloom/api/stream", method="GET", host=None, headers=None):
        connection = http.client.HTTPConnection(self.server.server_address[0], self.port, timeout=5)
        connection.request(method, path, headers={"Host": host or self.host, **(headers or {})})
        self.addCleanup(connection.close)
        return connection, connection.getresponse()

    @staticmethod
    def events(response, count):
        events, current = [], {}
        while len(events) < count:
            line = response.fp.readline().decode("utf-8")
            if line == "":
                break
            line = line.rstrip("\n")
            if not line:
                if current:
                    events.append(current)
                current = {}
            elif line.startswith(":"):
                current.setdefault("comment", line)
            else:
                key, _, value = line.partition(": ")
                current[key] = value
        return events

    def test_headers_and_projected_snapshot(self):
        _, response = self.open()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual((response.getheader("Cache-Control"), response.getheader("Connection")), ("no-store", "close"))
        first, second = self.events(response, 2)
        self.assertEqual(first, {"retry": "5000"})
        self.assertEqual((second["event"], second["id"]), ("snapshot", "1"))
        self.assertNotIn(SECRET, second["data"])
        document = json.loads(second["data"])
        self.assertEqual((document["schema"], document["seq"]), ("hyperloom/stream-v1", 1))
        content = document["content"]
        self.assertEqual(sorted(content), ["devices", "overview", "profiles", "stats", "why"])
        machine = content["devices"]["machines"][0]
        self.assertEqual((machine["id"], machine["health"], machine["accelerators"][0]["utilization_pct"]),
                         ("halo-1", "online", 40))
        self.assertEqual(machine["roles"], [{"profile": "qwen", "role": "replica"}])
        self.assertEqual(content["stats"]["engines"][0]["throughput"], {"value": 12.5})
        self.assertEqual(content["why"]["overview"], "overview feature not loaded")

    def test_overview_projection_without_sensitive_fields(self):
        reg = features.registry()
        reg.get("overview", lambda api, q: {})
        _, response = self.open()
        data = self.events(response, 2)[1]["data"]
        self.assertNotIn(SECRET, data)
        overview = json.loads(data)["content"]["overview"]
        self.assertEqual(overview["kpis"], {"throughput": {"value": 3}})
        self.assertEqual(overview["machines"][0]["headroom"], {"limiting": "unified"})

    def test_change_only_events_and_heartbeats(self):
        _, response = self.open()
        events = self.events(response, 4)
        self.assertEqual([e.get("event") or e.get("comment") for e in events[1:]],
                         ["snapshot", ": keep-alive", ": keep-alive"])
        self.api.version = 1
        changed = next(e for e in self.events(response, 10) if e.get("event") == "snapshot")
        self.assertEqual(changed["id"], "2")

    def test_the_stream_only_projects_the_cached_snapshot(self):
        self.api.empty = True                    # no page refreshed one yet: the stream builds none
        _, response = self.open()
        document = json.loads(self.events(response, 2)[1]["data"])
        self.assertEqual(document["content"]["why"]["devices"], "unavailable")
        self.assertIsNone(document["content"]["devices"])

    def test_errors_use_fixed_text(self):
        self.api.fail = True
        _, response = self.open()
        event = self.events(response, 2)[1]
        self.assertEqual((event["event"], json.loads(event["data"])), ("error", {"why": "snapshot unavailable"}))

    def test_limits_host_query_and_methods(self):
        _, response = self.open(host=f"workstation:{self.port}")
        self.assertEqual(response.status, 403)
        _, response = self.open("/hyperloom/api/stream?x=1")
        self.assertEqual(response.status, 400)
        _, response = self.open(method="HEAD")
        self.assertEqual((response.status, response.getheader("Allow"), response.read()), (405, "GET", b""))
        connection = http.client.HTTPConnection(self.server.server_address[0], self.port, timeout=5)
        connection.request("POST", "/hyperloom/api/stream", body=b"{}", headers={
            "Host": self.host, "Origin": self.server.origin,
            "X-Hyperloom-CSRF": self.server.csrf_token, "Content-Type": "application/json", "Content-Length": "2"})
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()
        open_streams = []
        for _ in range(integrations.STREAM_MAX_CLIENTS):
            connection, response = self.open()
            self.assertEqual(response.status, 200)
            self.events(response, 1)
            open_streams.append((connection, response))
        self.assertEqual(integrations.stream_clients(self.api), integrations.STREAM_MAX_CLIENTS)
        # Streams left open (closed or stuck tabs) never lock a page out: a new one ends the oldest.
        _, newest = self.open()
        self.assertEqual(newest.status, 200)
        oldest = open_streams[0][1]
        ending = [e for e in self.events(oldest, 50) if e.get("event") == "end"]
        self.assertIn("newer page", json.loads(ending[0]["data"])["why"])
        self.assertEqual(integrations.stream_clients(self.api), integrations.STREAM_MAX_CLIENTS)
        newest.close()
        for connection, response in open_streams:
            response.close()
            connection.close()
        deadline = time.monotonic() + 5
        while integrations.stream_clients(self.api) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(integrations.stream_clients(self.api), 0)      # slots freed when clients go away
        _, response = self.open()
        self.assertEqual(response.status, 200)

    def test_another_origin_is_refused(self):
        for headers in ({"Origin": "http://elsewhere.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                _, response = self.open(headers=headers)
                self.assertEqual(response.status, 403)
        _, response = self.open(headers={"Origin": self.server.origin, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 200)

    def test_connection_ends_after_its_maximum_time(self):
        with mock.patch.object(integrations, "STREAM_MAX_S", 0.3):
            _, response = self.open()
            events = self.events(response, 20)
        self.assertEqual(events[-1]["event"], "end")
        self.assertEqual(response.fp.readline(), b"")


if __name__ == "__main__":
    unittest.main()
