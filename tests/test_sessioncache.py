"""Session cache A1 contract, transport bounds, credentials and passive GET."""
import contextlib
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hyperloom.adapters import AdapterError, sessions
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.features import observe, sessioncache as sc
from tests.test_observe_http import Server, fixed

FIXTURES = Path(__file__).parent / "fixtures" / "sessioncache"


def fixture(kind):
    return json.loads((FIXTURES / (kind + ".json")).read_text())


class AdapterTests(unittest.TestCase):
    def test_engine_fields(self):
        for kind, size, entries in (("deepseek", 4096, 2), ("minimax", 76800, 1)):
            f = fixture(kind)
            stats = sessions.parse_stats(kind, f["stats"])
            self.assertEqual((stats["ram_bytes"], stats["entries"]), (size, entries))
            rows = sessions.parse_list(kind, f["list"])["sessions"]
            self.assertEqual(rows[0]["id"], "conversation-A")
            self.assertEqual(rows[0]["location"], "unknown")
            self.assertTrue(all(rows[0][k] is None and k in rows[0]["why"] for k in
                                ("tokens", "bytes", "generation", "last_used")))
        self.assertEqual(sessions.parse_stats("deepseek", fixture("deepseek")["stats"])["refusals"], 4)

    def test_qwen_serializer_fields(self):
        f = fixture("qwen")
        rows = sessions.parse_list("qwen", f["list"])["sessions"]
        self.assertEqual([r["location"] for r in rows], ["ram+disk", "ram", "disk"])
        self.assertTrue(all(r[k] is None and k in r["why"] for r in rows
                            for k in ("tokens", "position", "bytes", "generation", "last_used")))
        stats = sessions.parse_stats("qwen", f["stats"])
        self.assertEqual([stats[k] for k in sessions.STATS],
                         [1048576, 8388608, 2, 8, 3, 5, 7, None])
        self.assertEqual(set(stats["why"]), {"refusals"})
        self.assertNotIn("tombstones", stats)

    def test_qwen_missing_invalid_and_unreported_fields(self):
        self.assertIsNone(sessions.parse_list("qwen", {})["sessions"])
        self.assertTrue(all(sessions.parse_stats("qwen", {})[k] is None for k in sessions.STATS))
        for parked, disk in ((False, False), (1, True), (True, "false"), (None, False)):
            row = sessions.parse_list("qwen", {"sessions": [
                {"id": "valid", "parked": parked, "disk": disk, "tokens": 99}]})["sessions"][0]
            self.assertEqual(row["location"], "unknown")
            self.assertIn("location", row["why"])
            self.assertIsNone(row["tokens"])
        for key in ("ram_bytes", "ram_budget", "parked", "max_parked", "evicted", "hits", "misses"):
            for invalid in (True, -1, 1.5, "1", None):
                payload = {**fixture("qwen")["stats"], key: invalid}
                result = sessions.parse_stats("qwen", payload)
                target = {"ram_budget": "ram_budget_bytes", "parked": "entries",
                          "max_parked": "entry_limit", "evicted": "evictions"}.get(key, key)
                self.assertIsNone(result[target])
                self.assertIn(target, result["why"])
        result = sessions.parse_list("qwen", {"sessions": ["raw-id", {},
            {"id": "../secret", "parked": True, "disk": True}]})
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["why"]["invalid_entries"]["count"], 3)
        self.assertNotIn("../secret", json.dumps(result))

    def test_strict_types_missing_and_extra_fields(self):
        for value in (True, -1, 1.5, "1", None, float("inf"), float("nan")):
            result = sessions.parse_stats("deepseek", {"ram_bytes": value, "extra": "ignored"})
            self.assertIsNone(result["ram_bytes"])
            self.assertIn("ram_bytes", result["why"])
            self.assertNotIn("extra", result)
        for kind in sessions.KINDS:
            with self.assertRaises(AdapterError):
                sessions.parse_list(kind, [])
        with self.assertRaises(AdapterError):
            sessions.parse_stats("other", {})
        self.assertIsNone(sessions.parse_list("deepseek", {})["sessions"])

    def test_invalid_ids_dropped_never_echoed(self):
        invalid = ["../secret", "<script>", "", "x" * 129, 42, {}, None, "a b", "é"]
        result = sessions.parse_list("deepseek", {"sessions": ["A_1-", *invalid]})
        self.assertEqual([r["id"] for r in result["sessions"]], ["A_1-"])
        self.assertEqual(result["why"]["invalid_entries"]["count"], len(invalid))
        self.assertLessEqual(len(result["why"]["invalid_entries"]["examples"]), 5)
        self.assertNotIn("../secret", json.dumps(result))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = SimpleNamespace(document=lambda name, **kw: {"schema": "hyperloom/ui-" + name + "-v1", **kw},
                                   orchestrate=SimpleNamespace(_secret_name=lambda p, n: p + "-" + n),
                                   config_path=Path("cluster.json"),
                                   cluster=lambda: SimpleNamespace(profiles={"example": {}}, machines={"node0": SimpleNamespace(http_address="engine.example")}))
        self.launch = {"profile": "example", "family": "ds41-pair", "replicas": [
            {"id": "rank0", "node": "node0", "address": "engine.example", "port": 18501, "loopback": False}]}
        self.discovery = patch.object(sc.observe, "launches", return_value=([self.launch], {"profiles": 0, "replicas": 0}))
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def collect(self, http):
        self.store.delete(sc.NAMESPACE, "example")
        with patch.object(sc, "_http", side_effect=http), patch.object(observe.secrets, "get", return_value="secret-key"):
            sc.collect(self.api, 1000, self.store)
        return sc.document(self.api, {}, self.store)["profiles"][0]

    def test_all_requests_are_read_only_and_key_is_header_only(self):
        calls = []
        def http(url, headers, deadline, **kw):
            calls.append((url, headers, deadline, kw))
            self.assertGreater(deadline, time.monotonic())
            if url.endswith("/health"):
                return {"mode": "session-cache"}
            if kw:
                field = "op" if self.launch["family"] == "ds41-pair" else "operation"
                self.assertEqual(kw["method"], "POST")
                self.assertEqual(set(kw["json_body"]), {field})
                op = kw["json_body"][field]
                self.assertIn(op, sc.READ_ONLY_OPS)
                return fixture("deepseek" if field == "op" else "minimax")[op]
            self.assertFalse(kw)
            self.assertTrue(url.endswith(("/v1/sessions", "/v1/cache/stats")))
            return fixture("qwen")["list" if url.endswith("/v1/sessions") else "stats"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            for family in sc.FAMILIES:
                self.launch["family"] = family
                row = self.collect(http)
                self.assertTrue(row["supported"])
                self.assertNotIn("secret-key", json.dumps(row))
        self.assertNotIn("secret-key", out.getvalue())
        self.assertTrue(all(c[1] == {"Authorization": "Bearer secret-key"} for c in calls))
        self.assertEqual(sc.READ_ONLY_OPS, frozenset(("list", "stats")))
        with patch.object(sc, "_http") as transport:
            for op in ("delete", "checkpoint", "park", "resume", "restore", "start", "turn", "LIST", ""):
                for kind in sessions.KINDS:
                    with self.assertRaises(ValueError):
                        sc._query("http://example:1", kind, op, {}, time.monotonic())
            transport.assert_not_called()

    def test_qwen_collector_uses_only_two_gets_and_maps_fields(self):
        self.launch["family"] = "qwen-replicas"
        calls = []
        def http(url, headers, deadline, **kw):
            calls.append(url)
            self.assertEqual(kw, {})
            return fixture("qwen")["list" if url.endswith("/v1/sessions") else "stats"]
        row = self.collect(http)
        self.assertEqual(calls, ["http://engine.example:18501/v1/sessions",
                                 "http://engine.example:18501/v1/cache/stats"])
        self.assertTrue(row["supported"])
        self.assertEqual(row["stats"]["ram_budget_bytes"], 8388608)
        self.assertEqual(row["stats"]["hits"], 5)
        self.assertEqual([s["location"] for s in row["sessions"]], ["ram+disk", "ram", "disk"])
        self.assertTrue(row["coverage"]["complete"])

    def test_unsupported_timeout_errors_are_sanitized(self):
        for error, supported in ((observe.Unavailable("secret-key"), False), (TimeoutError("secret-key"), None),
                                  (ValueError("secret-key"), None), (OSError("secret-key"), None)):
            row = self.collect(lambda *a, **kw: (_ for _ in ()).throw(error))
            self.assertIs(row["supported"], supported)
            self.assertNotIn("secret-key", json.dumps(row))
        for answer in ({"error": "unknown route secret-key"}, {"error": {"message": "session cache is disabled"}}):
            row = self.collect(lambda *a, **kw: answer)
            self.assertFalse(row["supported"])
            self.assertEqual(row["why"]["supported"], sc.UNSUPPORTED)

    def test_minimax_normal_mode_never_posts(self):
        self.launch["family"] = "minimax-pair"
        calls = []
        def normal(url, headers, deadline, **kw):
            calls.append((url, kw))
            return {"status": "ok"}
        self.assertFalse(self.collect(normal)["supported"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/health"))
        self.assertFalse(calls[0][1])

    def test_get_only_store_and_filter(self):
        self.collect(lambda *a, **kw: fixture("deepseek")[kw["json_body"]["op"]])
        self.discovery.stop()
        with patch.object(sc.observe, "launches", side_effect=AssertionError("discovery")),                 patch.object(sc, "_http", side_effect=AssertionError("network")),                 patch.object(sc.telemetry, "open_store", side_effect=AssertionError("create")),                 patch.object(self.store, "put", side_effect=AssertionError("write")):
            self.assertEqual(len(sc.document(self.api, {"profile": "example"}, self.store)["profiles"]), 1)
            self.assertEqual(sc.document(self.api, {"profile": "other"}, self.store)["profiles"], [])
        for query in ({"profile": "../x"}, {"profile": "http://x"}, {"profile": []}, {"limit": "1"}):
            with self.assertRaises(ValueError):
                sc.document(self.api, query, self.store)
        with patch.object(sc.telemetry, "existing_store", return_value=None):
            self.assertIsNone(sc.document(self.api, {})["profiles"])

    def test_cap_and_stopped_reconciliation(self):
        row = self.collect(lambda *a, **kw: {"sessions": ["s" + str(i) for i in range(300)]}
                           if kw["json_body"]["op"] == "list" else fixture("deepseek")["stats"])
        self.assertEqual(len(row["sessions"]), 256)
        self.assertEqual(row["coverage"]["omitted"], 44)
        self.assertFalse(row["coverage"]["complete"])
        self.discovery.stop()
        with patch.object(sc.observe, "launches", return_value=([], {"profiles": 0, "replicas": 0})):
            sc.collect(self.api, 1005, self.store)
        self.assertEqual(sc.document(self.api, {}, self.store)["profiles"], [])

    def test_large_invalid_list_has_bounded_retained_value_and_get(self):
        for kind in sessions.KINDS:
            with self.subTest(kind=kind):
                parsed = sessions.parse_list(kind, {"sessions": ["../private-id"] * 30000})
                self.assertEqual(parsed["why"]["invalid_entries"]["count"], sessions.PARSE_LIMIT)
                self.assertEqual(parsed["why"]["omitted_entries"], 30000 - sessions.PARSE_LIMIT)
                self.assertLessEqual(len(parsed["why"]["invalid_entries"]["examples"]), 5)
                self.assertNotIn("private-id", json.dumps(parsed))
        row = self.collect(lambda *a, **kw: {"sessions": [None] * 30000}
                           if kw["json_body"]["op"] == "list" else fixture("deepseek")["stats"])
        retained = self.store.get(sc.NAMESPACE, "example")
        response = sc.document(self.api, {}, self.store)
        self.assertLess(len(json.dumps(retained).encode()), 16 * 1024)
        self.assertLess(len(json.dumps(response).encode()), 16 * 1024)
        self.assertEqual(row["sessions"], [])
        self.assertFalse(row["coverage"]["complete"])
        self.assertEqual(row["sources"][0]["why"]["omitted_entries"], 30000 - sessions.PARSE_LIMIT)

    def test_get_bounds_diagnostics_from_preexisting_store(self):
        self.collect(lambda *a, **kw: fixture("deepseek")[kw["json_body"]["op"]])
        retained = self.store.get(sc.NAMESPACE, "example")
        retained["sources"][0]["why"] = {str(i): "invalid entry" for i in range(30000)}
        self.store.put(sc.NAMESPACE, "example", retained)
        response = sc.document(self.api, {}, self.store)
        self.assertLess(len(json.dumps(response).encode()), 16 * 1024)
        self.assertIn("omitted", response["profiles"][0]["sources"][0]["why"]["collection"])

    def test_credentials_in_ids_are_dropped(self):
        row = self.collect(lambda *a, **kw: {"sessions": ["secret-key", "safe"]}
                           if kw["json_body"]["op"] == "list" else fixture("deepseek")["stats"])
        self.assertEqual([r["id"] for r in row["sessions"]], ["safe"])
        self.assertNotIn("secret-key", json.dumps(row))
        self.assertFalse(row["coverage"]["complete"])

    def test_address_mismatch_never_reads_key_or_contacts_engine(self):
        for address, loopback, node in (("foreign.example", False, "node0"),
                                        ("foreign.example", True, "node0"),
                                        ("engine.example", False, "unknown")):
            self.launch["replicas"][0].update(address=address, loopback=loopback, node=node)
            with patch.object(observe, "_bearer", side_effect=AssertionError("key read")):
                row = self.collect(lambda *a, **kw: self.fail("foreign endpoint queried"))
            self.assertIsNone(row["supported"])
            self.assertIn("does not match", row["sources"][0]["why"]["collection"])

    def test_unsupported_profile_backoff(self):
        self.collect(lambda *a, **kw: {"error": "unknown route"})
        with patch.object(sc, "_http") as http:
            sc.collect(self.api, 1599, self.store)
            http.assert_not_called()
        with patch.object(sc, "_http", return_value={"error": "unknown route"}) as http:
            sc.collect(self.api, 1600, self.store)
            self.assertGreater(http.call_count, 0)
        self.discovery.stop()
        with patch.object(observe, "launches", return_value=([], {"profiles": 0, "replicas": 0})):
            sc.collect(self.api, 1601, self.store)
        self.assertEqual(sc.document(self.api, {}, self.store)["profiles"], [])

    def test_node_loopback_is_never_queried(self):
        self.launch["replicas"][0]["loopback"] = True
        row = self.collect(lambda *a, **kw: self.fail("queried node loopback"))
        self.assertIsNone(row["supported"])
        self.assertIn("loopback", row["sources"][0]["why"]["collection"])

    def test_budget_exhaustion_is_disclosed(self):
        with patch.object(sc.time, "monotonic", side_effect=[0, observe.RUN_BUDGET_S + 1]),                 patch.object(sc, "_http", side_effect=AssertionError("budget")):
            sc.collect(self.api, 1000, self.store)
        row = sc.document(self.api, {}, self.store)["profiles"][0]
        self.assertIsNone(row["supported"])
        self.assertIn("budget", row["sources"][0]["why"]["collection"])

    def test_multiple_replicas_are_summed_and_identified(self):
        self.launch["replicas"].append({**self.launch["replicas"][0], "id": "replica2"})
        row = self.collect(lambda *a, **kw: fixture("deepseek")[kw["json_body"]["op"]])
        self.assertEqual(row["stats"]["ram_bytes"], 8192)
        self.assertEqual(row["stats"]["entries"], 4)
        self.assertEqual({r["replica"] for r in row["sessions"]}, {"rank0", "replica2"})
        self.assertTrue(row["coverage"]["complete"])
        self.assertIsNone(row["stats"]["hits"])

    def test_registration_is_read_only(self):
        reg = features.Registry()
        sc.register(reg)
        self.assertEqual(set(reg.gets), {"sessioncache"})
        self.assertFalse(reg.posts or reg.jobs)
        self.assertEqual(reg.collectors["sessioncache"][1], sc.INTERVAL)


class FixtureTests(unittest.TestCase):
    def test_seeded_route_document(self):
        from tests.ui.smoke import FixtureServer, API
        with FixtureServer() as server:
            status, _, body = server.request("GET", API + "sessioncache")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual(data["schema"], "hyperloom/ui-sessioncache-v1")
            profile = next(p for p in data["profiles"] if p["profile"] == "session-demo")
            self.assertEqual(profile["sessions"][0]["id"], "conversation-A")
            self.assertEqual(profile["stats"]["ram_bytes"], 4096)
            self.assertFalse(next(p for p in data["profiles"] if p["profile"] == "normal-minimax")["supported"])
            qwen = next(p for p in data["profiles"] if p["profile"] == "qwen-session-demo")
            self.assertEqual(qwen["sessions"][0]["location"], "ram+disk")
            self.assertEqual(qwen["stats"]["ram_budget_bytes"], 8388608)


class TransportTests(unittest.TestCase):
    def test_post_boundary_rejects_mutations_before_network(self):
        with patch.object(observe, "_bounded_http") as transport:
            for body in ({"op": "delete"}, {"op": "park"}, {"op": "stats", "id": "a"},
                         {"operation": "resume"}, {"op": ["list"]}, None):
                with self.assertRaises(ValueError):
                    sc._http("http://example:1/admin/session-cache", method="POST", json_body=body)
            for path in ("/v1/sessions", "/v1/cache/stats", "/other"):
                with self.assertRaises(ValueError):
                    sc._http("http://example:1" + path, method="POST", json_body={"op": "list"})
            transport.assert_not_called()

    def test_shared_transport_also_rejects_non_query_posts(self):
        with patch.object(observe.socket, "create_connection") as connect:
            for body in ({"op": "delete"}, {"op": "list", "id": "a"}, {"op": None}, {}):
                with self.assertRaises(ValueError):
                    observe._bounded_http("http://example:1/admin/session-cache", method="POST", json_body=body)
            with self.assertRaises(ValueError):
                observe._bounded_http("http://example:1/v1/sessions", method="POST", json_body={"op": "list"})
            connect.assert_not_called()

    def test_post_wire_and_size_cap(self):
        server = Server(fixed(200, b"{}"))
        try:
            sc._http(server.url("/admin/session-cache"), {"Authorization": "Bearer test"}, method="POST", json_body={"op": "list"})
            self.assertTrue(server.requests[0].startswith(b"POST /admin/session-cache HTTP/1.1"))
            self.assertIn(b'"op": "list"', server.requests[0])
        finally:
            server.close()
        server = Server(fixed(200, b"x" * (observe.HTTP_CAP + 1)))
        try:
            with self.assertRaises(ValueError):
                sc._http(server.url("/admin/session-cache"), method="POST", json_body={"op": "stats"})
        finally:
            server.close()

    def test_timeout_and_unsupported(self):
        def trickle(request, conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n")
            for _ in range(30):
                conn.sendall(b"x")
                time.sleep(.03)
        server = Server(trickle)
        try:
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                sc._http(server.url("/admin/session-cache"), deadline=start + .15, method="POST", json_body={"op": "stats"})
            self.assertLess(time.monotonic() - start, .6)
        finally:
            server.close()
        server = Server(fixed(404, b"{}"))
        try:
            with self.assertRaises(observe.Unavailable):
                sc._http(server.url("/admin/session-cache"), method="POST", json_body={"op": "stats"})
        finally:
            server.close()
