"""Documents of the observability package (GET, store only), the settings POST, the registration and the CLI."""

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from hyperloom import cliext
from hyperloom.cliext import observe as observe_cli
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API, APIError
from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import NODE1, NODE2, LOOPBACK, ROUTER, Env, fixture


QWEN = [("halo-1", NODE1, 18501), ("halo-2", NODE2, 18501)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()
        self.env.launch("qwen", QWEN)
        self.env.router()
        self.env.engine(NODE1, 18501)
        self.env.http.routes[f"http://{NODE2}:18501/health"] = {"status": "ok"}
        self.env.http.routes[f"http://{NODE2}:18501/metrics"] = fixture("engine-metrics-c1.txt")
        self.env.http.routes[f"http://{NODE2}:18501/api/status"] = observe.Unavailable("HTTP 404")

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()


class RequestDocumentTests(Base):
    def test_join_observers_and_phases(self):
        self.env.collect()
        document = observe.requests_document(self.env.api, {})
        self.assertEqual(document["schema"], "hyperloom/ui-observe-requests-v1")
        rows = {row["id"]: row for row in document["requests"]}
        offload = rows["0123456789abcdef"]
        self.assertEqual(offload["router"]["observer"], "router")
        self.assertEqual(offload["router"]["ttft_ms"], 845.2)
        self.assertIn("RTX hand-off", [p["phase"] for p in offload["router"]["phases"]])
        self.assertEqual(offload["engine"]["observer"], "engine")
        self.assertEqual((offload["engine"]["attempt"], offload["engine"]["replica"]), (2, "halo-1"))
        self.assertEqual(offload["engine"]["ttft_ms"], 15.0)
        self.assertEqual(offload["engine"]["computed_tokens"], 0)
        self.assertEqual([p["phase"] for p in offload["engine"]["phases"]], ["queue", "prefill", "decode"])
        self.assertEqual(offload["engine"]["phases"][0]["start_ms"], 0.0)          # own clock, never the router axis
        self.assertIn("prefill service", offload["why"]["service"])
        # The served attempt was the second decode attempt, on halo-1 (the first one failed to connect to halo-2).
        retried = rows["aabbccddeeff0011"]
        self.assertEqual(retried["engine"]["attempt"], 2)
        self.assertIsNone(retried["router"]["ttft_ms"])
        self.assertIn("non-stream", retried["why"]["ttft_ms"])
        in_flight = rows["00112233445566aa"]
        self.assertEqual(in_flight["state"], "in_flight")
        self.assertAlmostEqual(in_flight["elapsed_ms"], (self.env.clock() - 1_800_000_004.0) * 1000)
        self.assertEqual(in_flight["why"]["engine"], "no engine record yet")
        refused = rows["fedcba9876543210"]
        self.assertEqual(refused["outcome"], "no_replica")
        self.assertEqual(refused["why"]["engine"], "no decode attempt")
        self.assertEqual([u["id"] for u in document["unjoined"]], ["chatcmpl-77"])
        self.assertEqual(document["filters"]["route"], ["nodes", "rtx-long"])

    def test_filters_limits_and_validation(self):
        self.env.collect()
        api = self.env.api
        self.assertEqual([r["id"] for r in observe.requests_document(api, {"outcome": "no_replica"})["requests"]],
                         ["fedcba9876543210"])
        self.assertEqual(len(observe.requests_document(api, {"limit": "2"})["requests"]), 2)
        self.assertEqual(len(observe.requests_document(api, {"before": "1800000004"})["requests"]), 2)
        for query in ({"limit": "0"}, {"limit": "x"}, {"model": "a b"}, {"nope": "1"}, {"before": "nan"}):
            with self.assertRaises(ValueError, msg=query):
                observe.requests_document(api, query)
        with self.assertRaises(ValueError):
            observe.request_document(api, "../etc", {})
        with self.assertRaises(LookupError):
            observe.request_document(api, "f" * 16, {})

    def test_target_and_ambiguity(self):
        self.env.collect()
        # A second engine claims the same id and attempt on another replica: the attempt's target decides.
        self.env.store.transaction([("INSERT INTO observe_engine_requests(profile, replica, instance, id, rid, attempt, time, "
                                     "ended, seen_at, record) VALUES ('qwen','halo-2','x','0123456789abcdef.2',"
                                     "'0123456789abcdef',2,1,1,1,'{\"id\":\"0123456789abcdef.2\",\"queue_ms\":99}')", ())])
        row = observe.request_document(self.env.api, "0123456789abcdef", {})["request"]
        self.assertEqual(row["engine"]["replica"], "halo-1")
        self.assertEqual(len(row["observations"]), 2)
        self.env.store.transaction([("INSERT INTO observe_engine_requests(profile, replica, instance, id, rid, attempt, time, "
                                     "ended, seen_at, record) VALUES ('qwen','halo-1','y','0123456789abcdef.2',"
                                     "'0123456789abcdef',2,1,1,1,'{\"id\":\"0123456789abcdef.2\"}')", ())])
        row = observe.request_document(self.env.api, "0123456789abcdef", {})["request"]
        self.assertIsNone(row["engine"])
        self.assertIn("ambiguous", row["why"]["engine"])

    def test_no_store_and_not_collected(self):
        api = API(config_path=self.env.directory / "c.json", clock=self.env.clock, audit_path=self.env.directory / "a.jsonl")
        self.assertEqual(observe.requests_document(api, {})["why"]["requests"], observe.NO_STORE)
        self.assertEqual(observe.latency_document(api, {})["why"]["groups"], observe.NO_STORE)
        self.assertEqual(observe.engines_document(api, {})["why"]["engines"], observe.NO_STORE)
        api.telemetry = Store(":memory:")
        self.assertEqual(observe.requests_document(api, {})["why"]["requests"], observe.NOT_COLLECTED)
        self.assertEqual(observe.sessions_document(api, {})["why"]["sessions"], observe.NOT_COLLECTED)
        # GETs never create the collectors' tables.
        self.assertEqual(api.telemetry.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'observe_%'")[0][0], 0)

    def test_nothing_is_lost_to_redaction(self):
        from hyperloom.web.api import redact
        self.env.collect()
        status = fixture("engine-status-c1.json")
        status["speculative"].update(accepted=184, proposed=255, cycles=105)
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        api = self.env.api
        documents = [handler(api, {}) for handler in (observe.requests_document, observe.latency_document,
                                                      observe.engines_document, observe.speculative_document,
                                                      observe.sessions_document)]
        documents.append(observe.request_document(api, "0123456789abcdef", {}))
        for document in documents:
            self.assertNotIn("[redacted]", json.dumps(redact(document)), document["schema"])

    def test_gets_do_no_io(self):
        self.env.collect()
        calls = len(self.env.http.calls)
        api = self.env.api
        for handler in (observe.requests_document, observe.latency_document, observe.engines_document,
                        observe.speculative_document, observe.sessions_document):
            handler(api, {})
        observe.request_document(api, "0123456789abcdef", {})
        self.assertEqual(len(self.env.http.calls), calls)


class C8Tests(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(observe._c8(100, 20, 30), (50, None))
        self.assertEqual(observe._c8(100, 20, None)[0], None)
        self.assertIn("imported", observe._c8(100, 20, None)[1])
        self.assertEqual(observe._c8(100, 80, 30)[0], None)                       # never negative
        self.assertEqual(observe._c8(100, 20, None, 80), (80, None))              # reported by the engine
        computed, why = observe._c8(100, 20, 30, 49)
        self.assertEqual(computed, 49)
        self.assertIn("50", why)


class LatencyTests(Base):
    def test_groups_definitions_and_histograms(self):
        self.env.collect()
        document = observe.latency_document(self.env.api, {"by": "route", "range": "86400000"[:5]})
        groups = {g["group"]: g for g in document["groups"]}
        rtx = groups["rtx-long"]["metrics"]
        self.assertEqual(rtx["ttft_ms"]["router"]["p50"], 845.2)
        self.assertEqual(rtx["ttft_ms"]["engine"]["p50"], 15.0)
        self.assertAlmostEqual(rtx["decode_tok_s"]["router"]["p50"], 99 / ((5845.0 - 845.2) / 1000))
        self.assertEqual(rtx["ttft_ms"]["router"]["definition"], observe.DEFINITIONS["router"]["ttft_ms"])
        nodes = groups["nodes"]["metrics"]
        self.assertEqual(nodes["ttft_ms"]["router"]["n"], 0)                       # non-stream: no router TTFT
        self.assertEqual(nodes["ttft_ms"]["engine"]["p50"], 301.0)
        self.assertEqual(groups["direct"]["outcomes"], {"no_replica": 1})
        families = {(h["observer"], h["family"]) for h in document["histograms"]}
        self.assertIn(("router", "delivery_gap_seconds"), families)
        self.assertIn(("engine", "ttft_seconds"), families)
        gap = next(h for h in document["histograms"] if h["family"] == "delivery_gap_seconds")
        self.assertEqual(gap["label"], observe.GAP_LABEL)
        self.assertIn("estimated", gap["basis"])
        self.assertEqual(gap["instance"], "9f1c0a5b2d3e4f60")
        callers = {g["group"] for g in observe.latency_document(self.env.api, {"by": "caller"})["groups"]}
        self.assertEqual(callers, {"default", "bench"})
        with self.assertRaises(ValueError):
            observe.latency_document(self.env.api, {"by": "machine"})
        with self.assertRaises(ValueError):
            observe.latency_document(self.env.api, {"range": "10"})


class EngineDocumentTests(Base):
    def test_panel_and_speculative(self):
        self.env.collect()
        status = fixture("engine-status-c1.json")
        status["speculative"].update(accepted=184, proposed=255, cycles=105)
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        engines = {row["replica"]: row for row in observe.engines_document(self.env.api, {})["engines"]}
        one = engines["halo-1"]
        self.assertEqual((one["instance"], one["instance_basis"], one["armed"]), ("5a5a5a5a5a5a5a5a", "engine instance id", True))
        since = one["prompt_accounting"]["since_armed"]
        self.assertEqual((since["prompt_tokens"], since["cached_tokens"], since["imported_tokens"], since["computed_tokens"]),
                         (17192, 9216, 7168, 808))
        self.assertEqual(one["prompt_accounting"]["lifetime"]["prompt_tokens"], 12878)
        self.assertIn("each engine", one["prompt_accounting"]["scope"])
        self.assertEqual(one["prefix_cache"]["hits"], 1)
        self.assertEqual(one["why"]["slots"], "not reported by this engine")
        self.assertIsNotNone(one["last_served"])
        two = engines["halo-2"]                                                    # metrics only (status 404)
        self.assertIn("serves no status", two["why"]["status"])
        self.assertEqual(two["queue"], {"queued": 0, "running": 0})
        spec = {row["replica"]: row for row in observe.speculative_document(self.env.api, {})["engines"]}
        self.assertAlmostEqual(spec["halo-1"]["acceptance"], 184 / 255)
        self.assertEqual(spec["halo-1"]["basis"], "cumulative since the engine was armed")
        self.assertIn("per-depth", spec["halo-1"]["why"]["depth"])
        self.assertIn("draft time", spec["halo-1"]["why"]["saved_seconds"])
        self.assertIn("engine_draft_acceptance", spec["halo-1"]["acceptance_series"])

    def test_lifetime_split_in_the_producer_scope(self):
        observed = {"totals": {"imported_tokens": 0, "computed_tokens": 4096}, "observed": None}
        accounting = observe._accounting({}, {"prompt_tokens": 4096, "cached_tokens": 0}, observed, {})
        self.assertEqual((accounting["lifetime"]["computed_tokens"], accounting["lifetime"]["imported_tokens"]), (4096, 0))
        self.assertIn("since arming", accounting["why"]["computed_tokens"])
        none = observe._accounting({"prompt_tokens": 10}, {}, {"totals": {}}, {})
        self.assertIsNone(none["lifetime"]["computed_tokens"])
        self.assertIn("lifetime", none["lifetime"]["why"]["computed_tokens"])

    def test_staleness_follows_the_configured_interval(self):
        self.assertEqual(observe.stale_after(self.env.api), 3 * observe.INTERVAL)
        self.env.api.sampler = type("S", (), {"collectors": {"observe-engines": (None, 20.0)}})()
        self.assertEqual(observe.stale_after(self.env.api, "observe-engines"), 60.0)
        del self.env.api.sampler

    def test_stale_snapshot(self):
        self.env.collect()
        self.env.clock.now += 120
        row = next(r for r in observe.engines_document(self.env.api, {})["engines"] if r["replica"] == "halo-1")
        self.assertTrue(row["stale"])
        self.assertEqual(row["state"], "stale")


class SessionTests(Base):
    def test_router_sessions_and_expiry(self):
        self.env.collect()
        document = observe.sessions_document(self.env.api, {})
        self.assertEqual(document["label"], observe.AFFINITY_LABEL)
        session = document["sessions"][0]
        self.assertEqual((session["key"], session["turns"], session["replica"]), ("5e55a0b1c2d3", 2, "halo-1"))
        self.assertEqual(session["reused_per_turn"], [1024, 8192])
        self.assertEqual(document["coverage"]["qwen"]["router_idle_s"], 1800)
        observe.save_settings(self.env.api, {"affinity_idle_s": 7200})
        self.assertIn("forgets", observe.sessions_document(self.env.api, {})["coverage"]["qwen"]["why"])
        observe.save_settings(self.env.api, {"affinity_idle_s": 1800})
        self.env.clock.now += 1900
        self.assertEqual(observe.sessions_document(self.env.api, {})["sessions"], [])

    def test_from_request_records_without_a_sessions_endpoint(self):
        self.env.http.routes[ROUTER + "/api/sessions"] = observe.Unavailable("HTTP 404")
        self.env.collect()
        sessions = {s["key"]: s for s in observe.sessions_document(self.env.api, {})["sessions"]}
        self.assertEqual(sessions["5e55a0b1c2d3"]["turns"], 2)
        self.assertEqual(sessions["5e55a0b1c2d3"]["source"], "retained request records")
        coverage = observe.sessions_document(self.env.api, {})["coverage"]["qwen"]
        self.assertIn("not available", coverage["why"])
        self.assertEqual(sessions["5e55a0b1c2d3"]["reused_per_turn"], [1024, 8192])


class SettingsAndRegistrationTests(Base):
    def test_settings_post_is_validated_and_audited(self):
        api = self.env.api
        registry = features.Registry()
        observe.register(registry)
        with mock.patch.object(features, "registry", lambda: registry):
            _, result = api.feature_post("observe/settings/save", {"affinity_idle_s": 600})
            self.assertEqual(result["settings"]["affinity_idle_s"], 600)
            with self.assertRaises(APIError):
                api.feature_post("observe/settings/save", {"affinity_idle_s": 5})
            with self.assertRaises(APIError):
                api.feature_post("observe/settings/save", {"color": "red"})
            document = api.read("observe/settings")
        self.assertEqual(document["settings"]["affinity_idle_s"], 600)
        audit = [json.loads(line) for line in (self.env.directory / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(a["action"], a["result"]["state"]) for a in audit],
                         [("observe/settings/save", "succeeded"), ("observe/settings/save", "refused"),
                          ("observe/settings/save", "refused")])
        self.assertIn("observe-router", registry.collectors)
        self.assertEqual(registry.collectors["observe-engines"][1], observe.INTERVAL)

    def test_api_dispatch_redacts_and_maps_errors(self):
        self.env.collect()
        registry = features.Registry()
        observe.register(registry)
        with mock.patch.object(features, "registry", lambda: registry):
            document = self.env.api.read("observe/requests", {"limit": "5"})
            self.assertEqual(len(document["requests"]), 4)
            with self.assertRaises(APIError) as caught:
                self.env.api.read("observe/requests/" + "e" * 16)
            self.assertEqual(caught.exception.status, 404)


class CliTests(Base):
    def run_cli(self, *argv):
        import argparse
        parser = argparse.ArgumentParser(prog="hyperloom")
        commands = parser.add_subparsers(dest="command")
        observe_cli.add(commands)
        args = parser.parse_args(list(argv))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(observe_cli, "_api", lambda: self.env.api):
            code = args.ext_run(args)
        return code, output.getvalue()

    def test_commands_share_the_documents(self):
        code, text = self.run_cli("requests")
        self.assertEqual(code, 0)
        self.assertIn(observe_cli.ONE_SHOT, text)
        self.assertIn("0123456789abcdef", text)
        code, text = self.run_cli("requests", "--json", "--outcome", "served")
        self.assertEqual({r["outcome"] for r in json.loads(text)["requests"]}, {"served"})
        code, text = self.run_cli("request", "0123456789abcdef")
        self.assertIn("RTX hand-off", text)
        self.assertEqual(self.run_cli("request", "nothex")[0], 2)
        code, text = self.run_cli("latency", "--by", "route")
        self.assertIn("rtx-long", text)
        code, text = self.run_cli("engine-panel")
        self.assertIn("qwen/halo-1", text)
        code, text = self.run_cli("speculative", "--json")
        self.assertEqual(json.loads(text)["schema"], "hyperloom/ui-observe-speculative-v1")
        code, text = self.run_cli("sessions")
        self.assertIn(observe.AFFINITY_LABEL, text)

    def test_cli_sample_writes_nothing_to_the_portal_store(self):
        self.run_cli("requests")
        self.assertEqual(self.env.store.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'observe_%'")[0][0], 0)

    def test_settings_command(self):
        code, text = self.run_cli("observe-settings", "--retention-days", "3")
        self.assertEqual((code, json.loads(text)["retention_days"]), (0, 3))
        self.assertEqual(self.run_cli("observe-settings", "--retention-days", "99")[0], 2)
        audit = [json.loads(line) for line in (self.env.directory / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([a["result"]["state"] for a in audit], ["succeeded", "refused"])

    def test_cli_reads_the_portal_node_samples_read_only(self):
        from hyperloom.telemetry.store import Store as FileStore
        path = self.env.directory / "telemetry.sqlite3"
        portal = FileStore(path)
        portal.put("fleet/engine", "halo-1:mm:18601", {"ts": 1.0, "machine": "halo-1"})
        portal.close()
        before = path.stat().st_mtime_ns
        store = Store(":memory:")
        self.assertEqual(observe_cli._fleet_documents(self.env.api, store), 1)
        self.assertEqual(store.get("fleet/engine", "halo-1:mm:18601")["machine"], "halo-1")
        self.assertEqual(path.stat().st_mtime_ns, before)
        (self.env.directory / "telemetry.sqlite3").unlink()
        for name in ("telemetry.sqlite3-wal", "telemetry.sqlite3-shm"):
            (self.env.directory / name).unlink(missing_ok=True)
        self.assertEqual(observe_cli._fleet_documents(self.env.api, Store(":memory:")), 0)
        self.assertFalse(path.exists())

    def test_latency_text_shows_histogram_cuts(self):
        real = observe.latency_document

        def with_cut(api, query, store=None):
            document = real(api, query, store)
            document["histograms"] = list(document.get("histograms") or []) + [
                {"observer": "router", "profile": "qwen", "replica": None, "family": "_cut",
                 "cut": {"families": 3, "label_sets": 0}, "as_of": None, "basis": "3 families not kept (bounds)"}]
            return document
        with mock.patch.object(observe, "latency_document", with_cut):
            code, text = self.run_cli("latency")
        self.assertEqual(code, 0)
        self.assertIn("3 families not kept (bounds)", text)

    def test_extension_is_registered(self):
        self.assertIn("observe", cliext.MODULES)


# These tests drive the collectors with their own clock to check accounting, not pacing: the polling budget
# (hyperloom.pollbudget, tested in tests/test_pollbudget.py) grants every read here.
_unpaced = None


def setUpModule():
    global _unpaced
    from unittest import mock as _mock
    from hyperloom import pollbudget as _pollbudget
    _unpaced = _mock.patch.multiple(_pollbudget, CYCLE_S=0.0, NODE_SPACING_S=0.0, ENGINE_CYCLE_S=0.0, TURN_CHECK_S=0.0)
    _unpaced.start()


def tearDownModule():
    _unpaced.stop()


if __name__ == "__main__":
    unittest.main()
