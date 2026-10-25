"""Collectors of the observability package on fake launch records, fake HTTP and fake fleet documents."""

import json
import tempfile
import unittest
from unittest import mock

from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import NODE1, NODE2, LOOPBACK, ROUTER, KEY, Env, fixture


QWEN = [("halo-1", NODE1, 18501), ("halo-2", NODE2, 18501)]


def rows(env, sql, args=()):
    return env.store.execute(sql, args)


class RouterCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_pinned_router_says_what_it_needs(self):
        env = self.env
        env.launch("qwen", QWEN)
        env.http.routes[ROUTER + "/api/requests"] = observe.Unavailable("HTTP 404")
        env.http.routes[ROUTER + "/metrics"] = fixture("router-metrics.txt").split("# TYPE tensormeld_router_ttft")[0]
        env.http.routes[ROUTER + "/api/sessions"] = observe.Unavailable("HTTP 404")
        self.assertEqual(env.collect(), ["no source answered (2 tried)"])        # router parts answer; engines fail
        source = env.store.get("observe/source", "qwen")
        self.assertIn(observe.NEEDS_LOG, source["why"]["requests"])
        self.assertIn(observe.NEEDS_HIST, source["why"]["metrics"])
        self.assertIn(observe.NEEDS_SESSIONS, source["why"]["sessions"])
        document = observe.requests_document(env.api, {})
        self.assertEqual(document["requests"], [])
        self.assertIn(observe.NEEDS_LOG, document["why"]["requests"])

    def test_key_only_in_the_header_and_never_stored(self):
        env = self.env
        env.launch("qwen", QWEN)
        env.router()
        env.collect()
        keyed = [(url, headers) for url, headers in env.http.calls if headers]
        self.assertTrue(keyed)
        self.assertTrue(all(url.split("?")[0].endswith(("/api/requests", "/api/sessions")) for url, _ in keyed))
        self.assertTrue(all(headers == {"Authorization": "Bearer " + KEY} for _, headers in keyed))
        dump = json.dumps([list(r) for r in env.store.execute("SELECT * FROM docs")]) + json.dumps(
            [list(r) for r in env.store.execute("SELECT * FROM observe_requests")])
        self.assertNotIn(KEY, dump)
        for name in ("requests", "latency", "engines", "speculative", "sessions"):
            self.assertNotIn(KEY, json.dumps(getattr(observe, name + "_document")(env.api, {})))

    def test_records_counts_and_cursor_move_together(self):
        env = self.env
        env.launch("qwen", QWEN)
        env.router()
        env.collect()
        self.assertEqual(rows(env, "SELECT count(*) FROM observe_requests")[0][0], 4)
        self.assertEqual(observe._cursor(env.store, "qwen")["value"], 10)
        counts = {tuple(sorted(line["labels"].items())): line["points"][0][5]
                  for line in env.store.query("router_requests", 0, 2e9)["lines"]}
        self.assertEqual(counts[(("outcome", "served"), ("profile", "qwen"), ("route", "rtx-long"))], 1)
        self.assertEqual(sum(counts.values()), 3)                              # the in-flight one is not final
        # The same page again: nothing new, nothing counted twice.
        env.collect()
        self.assertEqual(sum(line["points"][0][5] for line in env.store.query("router_requests", 0, 2e9)["lines"]), 3)
        # The in-flight record finishes (a higher seq): counted once.
        page = fixture("router-requests.json")
        done = dict(page["records"][2], seq=11, state="done", outcome="served")
        done["stamps"] = dict(done["stamps"], last_byte=900.0)
        page.update(records=[done], seq=11, next=11)
        env.router(page=page)
        env.collect()
        self.assertEqual(sum(sum(p[5] for p in line["points"]) for line in env.store.query("router_requests", 0, 2e9)["lines"]), 4)
        self.assertEqual(rows(env, "SELECT state FROM observe_requests WHERE id='00112233445566aa'")[0][0], "done")
        self.assertIn("after=10", [url for url, _ in env.http.calls if "/api/requests" in url][-1])

    def test_a_failing_transaction_changes_nothing(self):
        env = self.env
        env.launch("qwen", QWEN)
        env.router()
        real = env.store.counter_delta

        def failing(key, instance, value, ts, **kw):
            if key.endswith("router-log") and value > 0:
                def apply(delta, info):
                    kw["apply"](delta, info)
                    raise RuntimeError("crash inside the transaction")
                return real(key, instance, value, ts, apply=apply)
            return real(key, instance, value, ts, **kw)
        with mock.patch.object(env.store, "counter_delta", failing):
            env.collect()
        self.assertEqual(rows(env, "SELECT count(*) FROM observe_requests")[0][0], 0)
        self.assertEqual(observe._cursor(env.store, "qwen")["value"], 0)
        self.assertEqual(env.store.query("router_requests", 0, 2e9)["lines"], [])
        env.collect()                                                          # the next run replays the page
        self.assertEqual(rows(env, "SELECT count(*) FROM observe_requests")[0][0], 4)
        self.assertEqual(sum(line["points"][0][5] for line in env.store.query("router_requests", 0, 2e9)["lines"]), 3)

    def test_paging_restart_and_coverage(self):
        env = self.env
        env.launch("qwen", QWEN)
        page = fixture("router-requests.json")
        first = dict(page, records=page["records"][:2], next=8, more=True)
        second = dict(page, records=page["records"][2:], next=10, more=False)
        env.http.routes[ROUTER + "/api/requests"] = lambda url, h: first if "after=0" in url else second
        env.http.routes[ROUTER + "/metrics"] = ""
        env.http.routes[ROUTER + "/api/sessions"] = observe.Unavailable("HTTP 404")
        env.collect()
        self.assertEqual(rows(env, "SELECT count(*) FROM observe_requests")[0][0], 4)
        # A new router instance: read from 0 again, in-flight records of the old one become unknown.
        restarted = dict(page, instance="1111111111111111", records=[dict(page["records"][0], id="1" * 16, seq=1)],
                         seq=1, next=1, more=False)
        env.http.routes[ROUTER + "/api/requests"] = lambda url, h: restarted
        env.collect()
        self.assertEqual(rows(env, "SELECT state FROM observe_requests WHERE id='00112233445566aa'")[0][0], "unknown")
        self.assertEqual(observe._cursor(env.store, "qwen")["instance"], "1111111111111111")
        self.assertIn("restarted", env.store.get("observe/gaps", "qwen")["why"])
        # More updates than the ring holds since the last read: disclosed, not counted as lost requests.
        crowded = dict(restarted, seq=900, next=1, records=[])
        env.http.routes[ROUTER + "/api/requests"] = lambda url, h: crowded
        env.collect()
        self.assertIn("exceed its ring", env.store.get("observe/gaps", "qwen")["why"])

    def test_oversized_page_is_retried_smaller(self):
        env = self.env
        env.launch("qwen", QWEN)
        page = fixture("router-requests.json")
        env.http.routes[ROUTER + "/api/requests"] = (
            lambda url, h: ValueError("response exceeds 1 MiB") if "limit=128" in url else page)
        env.http.routes[ROUTER + "/metrics"] = ""
        env.http.routes[ROUTER + "/api/sessions"] = observe.Unavailable("HTTP 404")
        original = env.http.__call__

        def call(url, headers=None, text=False, deadline=None):
            answer = original(url, headers, text, deadline)
            return answer
        env.collect()
        self.assertEqual(rows(env, "SELECT count(*) FROM observe_requests")[0][0], 4)
        self.assertTrue(any("limit=32" in url for url, _ in env.http.calls))

    def test_callers_are_bounded_at_ingestion(self):
        env = self.env
        env.launch("qwen", QWEN)
        page = fixture("router-requests.json")
        base = page["records"][1]
        page["records"] = [dict(base, id=f"{i:016x}", seq=i + 1, caller=f"team-{i}") for i in range(40)]
        page["records"].append(dict(base, id="f" * 16, seq=99, caller="Not A Label"))
        page.update(seq=99, next=99)
        env.router(page=page)
        env.collect()
        callers = [row[0] for row in rows(env, "SELECT DISTINCT caller FROM observe_requests")]
        self.assertEqual(len([c for c in callers if c != "anonymous"]), observe.MAX_CALLERS)
        self.assertIn("anonymous", callers)

    def test_abandoned_in_flight(self):
        env = self.env
        env.launch("qwen", QWEN)
        env.router()
        env.collect()
        env.clock.now += observe.ABANDONED_S + 10
        env.router(page=dict(fixture("router-requests.json"), records=[], more=False))
        env.collect()
        self.assertEqual(rows(env, "SELECT state FROM observe_requests WHERE id='00112233445566aa'")[0][0], "unknown")
        row = observe.request_document(env.api, "00112233445566aa", {})["request"]
        self.assertNotIn("elapsed_ms", row)
        self.assertIn("final state not observed", row["why"]["state"])

    def test_rotation_and_budget(self):
        env = self.env
        for name in ("a", "b", "c"):
            env.launch(name, [])
        order = [[launch["profile"] for launch in observe._rotated(env.api, "router", observe.launches(env.api)[0])]
                 for _ in range(3)]
        self.assertEqual(order, [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]])
        with mock.patch.object(observe, "RUN_BUDGET_S", 0.0):
            points, attempted, succeeded = observe.collect_router(env.api, env.clock(), env.store)
        self.assertEqual(attempted, 0)
        self.assertIn("budget", env.store.get("observe/source", "a")["why"]["requests"])


class EngineCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def series(self, name, **labels):
        return self.env.store.query(name, 0, 2e9, labels=labels)["lines"]

    def test_counters_through_baselines_and_restart(self):
        env = self.env
        env.launch("qwen", QWEN[:1])
        env.router()
        status, metrics = fixture("engine-status-c1.json"), fixture("engine-metrics-c1.txt")
        env.engine(NODE1, 18501, status, metrics)
        env.collect()
        self.assertEqual(self.series("engine_generated_tokens"), [])           # the first sample is a baseline
        env.engine(NODE1, 18501, status, metrics.replace("strix_generated_tokens_total 9910", "strix_generated_tokens_total 10010"))
        env.collect()
        lines = self.series("engine_generated_tokens")
        self.assertEqual(lines[0]["labels"]["instance"], "5a5a5a5a5a5a5a5a")
        self.assertEqual(sum(p[5] for p in lines[0]["points"]), 100)
        # A restart: a new instance id and counters back near zero. No negative or duplicated work.
        restarted = dict(status, instance={"id": "6b6b6b6b6b6b6b6b", "start_time": 1800000100})
        env.engine(NODE1, 18501, restarted, metrics.replace("strix_generated_tokens_total 9910", "strix_generated_tokens_total 50")
                   .replace('instance="5a5a5a5a5a5a5a5a"', 'instance="6b6b6b6b6b6b6b6b"')
                   .replace('strix_engine_start_time_seconds 1799990000', 'strix_engine_start_time_seconds 1800000100'))
        env.collect()
        total = sum(p[5] for line in self.series("engine_generated_tokens") for p in line["points"])
        self.assertEqual(total, 100)
        snapshot = env.store.get("observe/engine", "qwen/halo-1")
        self.assertEqual(snapshot["instance"], "6b6b6b6b6b6b6b6b")
        self.assertEqual(snapshot["counter_resets"][-1]["instance"], "6b6b6b6b6b6b6b6b")

    def test_identity_from_uptime_tolerates_jitter(self):
        env = self.env
        key = "p/r"
        def read(at, **extra):
            return {"observed_at": at, "status": {}, "metrics_text": None, **extra}
        first = observe._identity(env.store, key, {}, None, read(1000.0), 400.0)
        second = observe._identity(env.store, key, {}, None, read(1005.0), 404.8)    # 0.2 s of jitter
        moved = observe._identity(env.store, key, {}, None, read(1010.0), 408.0)     # 1.8 s off: another process
        self.assertEqual((first[0], first[2], first[4]), (second[0], "derived from uptime", "status"))
        self.assertNotEqual(second[0], moved[0])
        young = observe._identity(env.store, key, {}, None, read(1015.0), 3.0)       # restarted: too young to tell
        self.assertEqual((young[0], young[4]), (None, None))
        self.assertIn("younger", young[3])
        self.assertEqual(observe._identity(env.store, key, {}, None, read(1.0), None)[:3], (None, None, "unavailable"))
        conflict = observe._identity(env.store, key, {"instance": "a"}, {"instance": "b"}, read(1.0, metrics_text=""), 50.0)
        self.assertEqual((conflict[0], conflict[2]), (None, "conflict"))
        starts = observe._identity(env.store, key, {"started_at": 50.0}, {"start_time": 60.0}, read(1.0, metrics_text=""), 50.0)
        self.assertEqual(starts[2], "conflict")
        # The identity must come with the counters: a metrics answer without one uses the status totals instead.
        status_only = observe._identity(env.store, key, {"instance": "a"}, {}, read(1.0, metrics_text=""), 50.0)
        self.assertEqual((status_only[0], status_only[4]), ("a", "status"))
        metrics_id = observe._identity(env.store, key, {}, {"instance": "m"}, read(1.0, metrics_text=""), 50.0)
        self.assertEqual((metrics_id[0], metrics_id[4]), ("m", "metrics"))
        fleet = observe._identity(env.store, key, {}, {}, read(1.0, fleet_instance="f", fleet_start=3.0), None)
        self.assertEqual((fleet[0], fleet[2]), ("f", "node sampler's engine identity"))

    def test_ratios_need_matching_intervals(self):
        env = self.env
        self.assertIsNone(observe._paired(env.store, "k", "i", 10, 20, 1.0))          # first sample
        self.assertEqual(observe._paired(env.store, "k", "i", 15, 30, 2.0), (5, 10))
        self.assertIsNone(observe._paired(env.store, "k", "i", 20, None, 3.0))        # denominator missing: neither moves
        self.assertEqual(observe._paired(env.store, "k", "i", 25, 40, 4.0), (10, 10))  # both over the same 2..4 interval
        self.assertIsNone(observe._paired(env.store, "k", "i", 30, 5, 5.0))           # denominator went back (restart)
        self.assertEqual(observe._paired(env.store, "k", "i", 35, 15, 6.0), (5, 10))
        self.assertIsNone(observe._paired(env.store, "k", "i", 35, 15, 7.0))          # zero denominator

    def test_window_quantiles_acceptance_and_ring(self):
        env = self.env
        env.clock.now = 1_800_000_020.0
        env.launch("qwen", QWEN[:1])
        env.router()
        env.engine(NODE1, 18501)
        env.collect()
        ttft = {line["labels"]["basis"]: line for line in self.series("engine_ttft_seconds_p50", kind="engine")}
        self.assertEqual(set(ttft), {"first_token", "queue_prefill"})      # the older record is an approximation
        self.assertAlmostEqual(ttft["first_token"]["points"][0][1], 0.015)
        self.assertAlmostEqual(ttft["queue_prefill"]["points"][0][1], 0.0501)
        engine_rows = rows(env, "SELECT id, rid, attempt FROM observe_engine_requests ORDER BY id")
        self.assertIn(("0123456789abcdef.2", "0123456789abcdef", 2), engine_rows)
        self.assertIn(("chatcmpl-77", None, None), engine_rows)
        # Speculative counters: the second sample gives the acceptance of the interval.
        status = fixture("engine-status-c1.json")
        status["speculative"].update(accepted=184, proposed=255, cycles=105)
        env.engine(NODE1, 18501, status)
        env.collect()
        acceptance = self.series("engine_draft_acceptance")
        self.assertAlmostEqual(acceptance[0]["points"][-1][1], 100 / 140)
        self.assertAlmostEqual(self.series("engine_draft_accepted_per_cycle")[0]["points"][-1][1], 100 / 40)

    def test_partial_answers_and_unreachable(self):
        env = self.env
        env.launch("qwen", QWEN[:1])
        env.router()
        env.http.routes[f"http://{NODE1}:18501/health"] = {"status": "ok"}
        env.http.routes[f"http://{NODE1}:18501/api/status"] = observe.Unavailable("HTTP 404")
        env.http.routes[f"http://{NODE1}:18501/metrics"] = fixture("engine-metrics-c1.txt")
        env.collect()
        self.assertEqual(self.series("engine_up")[0]["points"][-1][6], 1)
        snapshot = env.store.get("observe/engine", "qwen/halo-1")
        self.assertIn("serves no status", snapshot["why"]["status"])
        self.assertEqual(snapshot["instance"], "5a5a5a5a5a5a5a5a")            # identity from the metrics
        env.http.routes.pop(f"http://{NODE1}:18501/health")
        errors = env.collect()
        self.assertEqual(self.series("engine_up")[0]["points"][-1][6], 0)
        self.assertEqual(env.store.get("observe/engine", "qwen/halo-1")["state"], "unreachable")
        self.assertTrue(errors)                                                # every engine source failed

    def test_loopback_engines_come_from_the_fleet_sampler(self):
        env = self.env
        env.launch("mm", [("halo-1", LOOPBACK, 18601)], family="minimax-pair", router=(NODE1, 18600))
        env.router(base=f"http://{NODE1}:18600")
        with mock.patch.object(env.api, "node", side_effect=AssertionError("no node calls")):
            env.collect()
            self.assertIn(observe.NEEDS_NODE, env.store.get("observe/engine", "mm/halo-1")["why"]["status"])
            env.store.put("fleet/engine", "halo-1:mm:18601", {
                "ts": env.clock(), "machine": "halo-1", "profile": "mm", "port": 18601, "replica": "halo-1",
                "outcomes": {"/health": "ok", "/api/status?detail=1": "ok", "/metrics": "ok"}, "health": True,
                "status": fixture("minimax-status.json"), "metrics_text": fixture("minimax-metrics-sched.txt")})
            env.collect()
        snapshot = env.store.get("observe/engine", "mm/halo-1")
        self.assertEqual((snapshot["via"], snapshot["instance"], snapshot["model"]),
                         ("node (fleet sampler)", "c0ffee00c0ffee00", "minimax-m3"))
        self.assertEqual(snapshot["observed"]["speculative"]["proposed"], 2100)
        self.assertFalse(any(f"{LOOPBACK}:18601" in url for url, _ in env.http.calls))
        # A stale node sample is shown as stale and not recorded as current.
        env.clock.now += 60
        before = len(self.series("engine_up")[0]["points"])
        env.collect()
        self.assertEqual(len(self.series("engine_up")[0]["points"]), before)
        self.assertIn("old", env.store.get("observe/engine", "mm/halo-1")["why"]["stale"])

    def test_prefill_service_records(self):
        env = self.env
        env.launch("qwen", QWEN[:1], service_port=18700)
        env.router()
        env.engine(NODE1, 18501)
        env.http.routes[f"http://{LOOPBACK}:18700/api/status"] = {
            "instance": "7777777777777777",
            "requests": [{"id": "0123456789abcdef.1", "time": 1800000000.0, "computed_tokens": 7168, "prompt_tokens": 7168,
                          "prefill_ms": 790.0, "handoff_ms": 18.0}]}
        env.collect()
        row = observe.request_document(env.api, "0123456789abcdef", {})["request"]
        self.assertEqual(row["service"]["computed_tokens"], 7168)
        self.assertEqual(row["service"]["observer"], "prefill service")
        env.http.routes[f"http://{LOOPBACK}:18700/api/status"] = observe.Unavailable("HTTP 404")
        env.collect()
        self.assertIn(observe.NEEDS_SERVICE, env.store.get("observe/service", "qwen")["why"])


if __name__ == "__main__":
    unittest.main()
