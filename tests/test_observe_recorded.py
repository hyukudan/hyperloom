"""The observability package against answers recorded from the producers' own code (router request log, sessions
and histograms; the instrumented MiniMax serve status and metrics)."""

import tempfile
import unittest

from hyperloom.adapters import engine, minimax, prom, router
from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import LOOPBACK, NODE1, ROUTER, Env, fixture


class RecordedRouterTests(unittest.TestCase):
    def test_request_page(self):
        page = router.parse_requests(fixture("router-requests.recorded.json"))
        self.assertEqual((page.capacity, page.more, len(page.records)), (512, False, 3))
        self.assertEqual(page.next, page.records[-1]["seq"])
        served, refused, flight = page.records
        self.assertEqual((served["outcome"], served["route"], served["usage"]["imported_tokens"]), ("served", "rtx-long", 7168))
        self.assertEqual([a["kind"] for a in served["attempts"]], ["prefill", "decode"])
        self.assertEqual(served["engine"]["first_token_ms"], 15.0)
        self.assertEqual((refused["outcome"], refused["why"]), ("no_replica", "queue full"))
        self.assertEqual(flight["state"], "in_flight")
        for record in page.records:
            self.assertEqual(record["parse_why"], {}, record["id"])           # nothing the adapter could not read

    def test_sessions_and_histograms(self):
        head, sessions, why = router.parse_sessions(fixture("router-sessions.recorded.json"))
        self.assertEqual((head["idle_s"], head["count"], why), (1800, 1, {}))
        self.assertEqual(sessions[0]["recent"][0]["cached_tokens"], 1024)
        families = router.histograms(prom.parse(fixture("router-metrics.recorded.txt")), "tensormeld_router_")
        self.assertEqual(set(families), {"ttft_seconds", "request_seconds", "queue_wait_seconds", "decode_tokens_per_second"})
        ttft = families["ttft_seconds"][0]
        self.assertEqual((ttft["count"], ttft["labels"]["route"]), (1, "rtx-long"))
        self.assertLessEqual(router.quantile(ttft["buckets"], 0.5), 1.0)


class RecordedMiniMaxTests(unittest.TestCase):
    def test_status_and_metrics(self):
        doc = fixture("minimax-status.recorded.json")
        status = engine.parse_status(doc)
        observed = engine.parse_observed(doc)
        self.assertEqual(observed["instance"], doc["instance"]["id"])
        self.assertEqual(observed["totals"]["computed_tokens"], doc["totals"]["computed_tokens"])
        self.assertIsNotNone(observed["histograms"]["ttft_seconds"])
        self.assertEqual(status.queue["completed"], doc["queue"]["completed"])
        request = observed["requests"][0]
        self.assertEqual(request["first_token_ms"], doc["requests"][0]["first_token_ms"])
        metrics, extra = minimax.parse_metrics(fixture("minimax-metrics.recorded.txt"))
        self.assertEqual(metrics.completed, doc["queue"]["completed"])
        self.assertEqual(extra["instance"], doc["instance"]["id"])

    def test_collected_through_the_node_sampler(self):
        with tempfile.TemporaryDirectory() as directory, Env(directory) as env:
            env.launch("mm", [("halo-1", LOOPBACK, 18601)], family="minimax-pair", router=(NODE1, 18600))
            env.router(page=fixture("router-requests.recorded.json"), metrics=fixture("router-metrics.recorded.txt"),
                       sessions=fixture("router-sessions.recorded.json"), base=f"http://{NODE1}:18600")
            doc = fixture("minimax-status.recorded.json")
            env.store.put("fleet/engine", "halo-1:mm:18601", {
                "ts": env.clock(), "machine": "halo-1", "profile": "mm", "port": 18601, "replica": "halo-1",
                "instance": doc["instance"]["id"], "start": doc["instance"]["start_time"], "health": True,
                "status": doc, "metrics_text": fixture("minimax-metrics.recorded.txt")})
            env.collect()
            row = observe.engines_document(env.api, {})["engines"][0]
            self.assertEqual((row["instance"], row["instance_basis"]), (doc["instance"]["id"], "engine instance id"))
            life = row["prompt_accounting"]["lifetime"]
            self.assertEqual((life["prompt_tokens"], life["computed_tokens"], life["imported_tokens"]),
                             (doc["totals"]["prompt_tokens"], doc["totals"]["computed_tokens"], 0))
            requests = observe.requests_document(env.api, {})
            self.assertEqual(len(requests["requests"]), 3)
            latency = observe.latency_document(env.api, {"by": "route"})
            self.assertTrue(any(h["family"] == "ttft_seconds" and h["observer"] == "router" for h in latency["histograms"]))
            self.assertTrue(any(h["observer"] == "engine" for h in latency["histograms"]))
            self.assertEqual(observe.sessions_document(env.api, {})["sessions"][0]["turns"], 1)
            self.assertNotIn(ROUTER, str(env.http.calls))           # the router of this profile is on its node


if __name__ == "__main__":
    unittest.main()
