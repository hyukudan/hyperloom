"""Edge cases of the observability package: malformed framing, page atomicity of caller labels, hand-off accounting,
unknown reachability, outcomes without a last byte, stale router views, bounded families."""

import math
import tempfile
import unittest
from unittest import mock

from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import NODE1, ROUTER, Env, fixture


QWEN = [("halo-1", NODE1, 18501)]


class FramingTests(unittest.TestCase):
    def test_malformed_chunks_are_errors(self):
        for body in (b"-6\r\nabcdef\r\n0\r\n\r\n", b"zz\r\nab\r\n0\r\n\r\n", b"10\r\nab\r\n0\r\n\r\n",
                     b"2\r\nabXY0\r\n\r\n", b"2\r\nab\r\n", b"123456789\r\n"):
            with self.assertRaises(ValueError, msg=body):
                observe._dechunk(body)
        self.assertEqual(observe._dechunk(b"2;ext=1\r\nab\r\n1\r\nc\r\n0\r\n\r\n"), b"abc")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()
        self.env.launch("qwen", QWEN)

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()


class PageAtomicityTests(Base):
    def test_caller_labels_commit_with_their_page(self):
        env = self.env
        page = fixture("router-requests.json")
        base = page["records"][1]
        first = dict(page, records=[dict(base, id=f"{i:016x}", seq=i + 1, caller=f"team-{i}") for i in range(20)],
                     next=20, more=True)
        env.http.routes[ROUTER + "/api/requests"] = lambda url, h: first if "after=0" in url else OSError("broken")
        env.http.routes[ROUTER + "/metrics"] = ""
        env.http.routes[ROUTER + "/api/sessions"] = observe.Unavailable("HTTP 404")
        env.collect()
        self.assertEqual(len(env.store.get("observe/callers", "qwen")), 20)
        second = dict(page, records=[dict(base, id=f"{i + 100:016x}", seq=i + 21, caller=f"other-{i}") for i in range(20)],
                      next=40, more=False)
        env.http.routes[ROUTER + "/api/requests"] = lambda url, h: second
        env.collect()
        callers = {row[0] for row in env.store.execute("SELECT DISTINCT caller FROM observe_requests")}
        self.assertEqual(len(callers - {"anonymous"}), observe.MAX_CALLERS)
        self.assertEqual(len(env.store.get("observe/callers", "qwen")), observe.MAX_CALLERS)

    def test_a_failed_page_registers_no_caller(self):
        env = self.env
        page = fixture("router-requests.json")
        env.router(page=page)
        real = env.store.counter_delta

        def failing(key, instance, value, ts, **kw):
            if key.endswith("router-log") and value > 0:
                raise RuntimeError("crash")
            return real(key, instance, value, ts, **kw)
        with mock.patch.object(env.store, "counter_delta", failing):
            env.collect()
        self.assertIsNone(env.store.get("observe/callers", "qwen"))


class HandoffTests(Base):
    def record(self, attempts):
        page = fixture("router-requests.json")
        record = dict(page["records"][0], attempts=attempts)
        self.env.router(page=dict(page, records=[record]))
        self.env.engine(NODE1, 18501)
        self.env.http.routes["http://" + observe._host(str(observe.ipaddress.ip_address(observe.socket.INADDR_LOOPBACK)))
                             + ":18700/api/status"] = {"requests": [
            {"id": "0123456789abcdef.1", "time": 1.0, "computed_tokens": 5000, "outcome": "handoff_failed"},
            {"id": "0123456789abcdef.3", "time": 2.0, "computed_tokens": 7168, "outcome": "served"}]}
        self.env.launch("qwen", QWEN, service_port=18700)
        self.env.collect()
        return observe.request_document(self.env.api, "0123456789abcdef", {})["request"]

    def test_the_used_handoff_counts_and_failed_ones_are_listed(self):
        row = self.record([{"n": 1, "kind": "prefill", "target": "rtx", "at_ms": 1.0, "outcome": "failed", "why": "io"},
                           {"n": 2, "kind": "decode", "target": "halo-1", "at_ms": 9.0, "outcome": "connect_failed", "why": None},
                           {"n": 3, "kind": "prefill", "target": "rtx", "at_ms": 10.0, "outcome": "ok", "why": None},
                           {"n": 4, "kind": "decode", "target": "halo-1", "at_ms": 20.0, "outcome": "served", "why": None}])
        self.assertEqual((row["service"]["attempt"], row["service"]["computed_tokens"]), (3, 7168))
        self.assertEqual([(f["attempt"], f["computed_tokens"]) for f in row["failed_handoffs"]], [(1, 5000)])

    def test_no_successful_handoff_counts_nothing(self):
        row = self.record([{"n": 1, "kind": "prefill", "target": "rtx", "at_ms": 1.0, "outcome": "failed", "why": "io"},
                           {"n": 2, "kind": "decode", "target": "halo-1", "at_ms": 9.0, "outcome": "served", "why": None}])
        self.assertIsNone(row["service"])
        self.assertIn("nothing is counted", row["why"]["service"])

    def test_trim_keeps_the_successful_handoff(self):
        attempts = [{"n": i, "kind": "prefill", "target": "rtx", "outcome": "failed", "why": "x" * 190} for i in range(1, 3)]
        attempts.append({"n": 3, "kind": "prefill", "target": "rtx", "outcome": "ok", "why": None})
        attempts += [{"n": i, "kind": "prefill", "target": "rtx", "outcome": "failed", "why": "x" * 190} for i in range(4, 16)]
        attempts.append({"n": 16, "kind": "decode", "target": "halo-1", "outcome": "served", "why": None})
        record = dict(fixture("router-requests.json")["records"][0], attempts=attempts,
                      decisions=[{"at_ms": 1.0, "what": "w" * 150, "target": "t", "why": "y" * 190}] * 16)
        kept = [a["n"] for a in observe.json.loads(observe._encode(record))["attempts"]]
        self.assertIn(3, kept)
        self.assertIn(16, kept)


class ReachabilityTests(Base):
    def test_a_timed_out_reading_is_unknown_not_down(self):
        env = self.env
        env.router()
        env.engine(NODE1, 18501)
        env.collect()
        before = len(env.store.query("engine_up", 0, 2e9)["lines"][0]["points"])
        for path in ("/health", "/api/status", "/metrics"):
            env.http.routes[f"http://{NODE1}:18501{path}"] = TimeoutError("deadline")
        env.collect()
        self.assertEqual(len(env.store.query("engine_up", 0, 2e9)["lines"][0]["points"]), before)
        self.assertEqual(env.store.get("observe/engine", "qwen/halo-1")["state"], "not read")


class OutcomeTests(Base):
    def test_terminal_outcomes_without_a_last_byte_are_counted(self):
        page = fixture("router-requests.json")
        gone = dict(page["records"][2], seq=11, state="done", outcome="client_gone")
        self.env.router(page=dict(page, records=[gone], next=11))
        self.env.collect()
        groups = {g["group"]: g for g in observe.latency_document(self.env.api, {"by": "route"})["groups"]}
        self.assertEqual(groups["nodes"]["outcomes"], {"client_gone": 1})


class FreshnessAndBoundsTests(Base):
    def test_a_stale_router_session_view_is_not_current(self):
        self.env.router()
        self.env.collect()
        self.env.http.routes[ROUTER + "/api/sessions"] = OSError("down")
        self.env.clock.now += 60
        self.env.collect()
        document = observe.sessions_document(self.env.api, {})
        self.assertEqual(document["coverage"]["qwen"]["source"], "retained request records")
        self.assertIn("not read", document["coverage"]["qwen"]["why"])

    def test_family_cuts_are_reported(self):
        line = {"labels": {}, "buckets": [[1.0, 1], [math.inf, 1]], "sum": 1.0, "count": 1}
        families = {f"f{i}": [line] * 70 for i in range(40)}
        kept = observe._bound_families(families)
        self.assertEqual(kept["_cut"], {"families": 8, "label_sets": 32 * 6})
        notes = [h for h in observe._estimated({"families": kept, "ts": 1.0}, "router", "qwen", None, None)
                 if h["family"] == "_cut"]
        self.assertIn("not kept", notes[0]["basis"])


if __name__ == "__main__":
    unittest.main()


class CadenceAndAgeTests(Base):
    def test_counted_series_follow_the_configured_interval(self):
        env = self.env
        env.router()
        env.engine(NODE1, 18501)
        env.api.sampler = type("S", (), {"collectors": {"observe-engines": (None, 20.0), "observe-router": (None, 10.0)}})()
        try:
            env.collect()
            self.assertEqual(env.store.descriptor("engine_generated_tokens")["interval_s"], 20.0)
            self.assertEqual(env.store.descriptor("router_requests")["interval_s"], 10.0)
        finally:
            del env.api.sampler

    def test_retained_histograms_carry_their_age(self):
        env = self.env
        env.router()
        env.engine(NODE1, 18501)
        env.collect()
        env.http.routes[ROUTER + "/metrics"] = OSError("down")
        env.clock.now += 600
        env.collect()
        router = [h for h in observe.latency_document(env.api, {})["histograms"] if h["observer"] == "router"]
        self.assertTrue(router and all(h["stale"] and h["age_s"] >= 600 for h in router))
        engine = [h for h in observe.latency_document(env.api, {})["histograms"] if h["observer"] == "engine"]
        self.assertTrue(engine and not any(h["stale"] for h in engine))

    def test_inventory_and_session_cuts_reach_the_documents(self):
        env = self.env
        for i in range(10):
            env.launch(f"p{i}", QWEN)
        env.router()
        env.collect()
        self.assertEqual(observe.requests_document(env.api, {})["inventory"]["omitted"]["profiles"], 3)
        self.assertEqual(observe.engines_document(env.api, {})["inventory"]["omitted"]["profiles"], 3)
