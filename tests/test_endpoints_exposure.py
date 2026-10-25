"""Endpoint exposure check (F40): configured rows, listener evidence, the decision table, rounds and documents."""

import itertools
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from hyperloom import config
from hyperloom.web.features import endpoints
from tests.test_endpoints_feature import MODELS, Base, json_route


FIXTURES = Path(__file__).parent / "fixtures" / "endpoints"


def lab_cluster(router=None, pc=None, profiles=None):
    machines = {}
    for mid, system, access, address in (("workstation", "windows", "local", "127.0.0.1"),
                                          ("halo-1", "linux", "ssh", "192.0.2.10"),
                                          ("halo-2", "linux", "ssh", "192.0.2.11")):
        machines[mid] = config.Machine(mid, system, access, address, mid, 22, "/opt/hyperloom", True, [{"id": "gpu"}])
    if profiles is None:
        profiles = {"qwen-replicas": {"nodes": ["halo-1", "halo-2"], "port": 18501,
                                      "api_key_files": {"halo-1": "/opt/hyperloom/secrets/engine.key"}},
                    "minimax-pair": {"rank0": "halo-1", "rank1": "halo-2"}}
    return config.Cluster(machines, {}, profiles, router or {}, pc or {}, {})


def host_doc(now, listeners, ok=True, status="ok"):
    section = {"status": status, "value": listeners if status == "ok" else None}
    if status != "ok":
        section["why"] = "proc not readable"
    return {"ts": now, "ok": ok, "error": None if ok else "ssh failed",
            "host": {"schema": "hyperloom/node-v1", "verb": "host", "checks": {"listeners": section}}}


class ConfiguredRowsTests(unittest.TestCase):
    def test_engines_router_and_targets(self):
        cluster = lab_cluster()
        targets = [{"id": "zz", "rev": "1" * 16, "label": "z", "scheme": "https", "host": "halo-2", "port": 9000, "key_name": "k"},
                   {"id": "aa", "rev": "2" * 16, "label": "a", "scheme": "http", "host": "127.0.0.1", "port": 8080, "key_name": None}]
        rows = {row["id"]: row for row in endpoints.configured(cluster, targets)}
        order = [row["id"] for row in endpoints.configured(cluster, targets)]
        self.assertEqual(order[0], "router")
        self.assertEqual(order[-2:], ["target/aa", "target/zz"])
        self.assertEqual((rows["engine/qwen-replicas/halo-1"]["configured_scope"], rows["engine/qwen-replicas/halo-1"]["key_configured"],
                          rows["engine/qwen-replicas/halo-1"]["port"], rows["engine/qwen-replicas/halo-1"]["probe_address"]),
                         ("lan", True, 18501, "192.0.2.10"))
        self.assertFalse(rows["engine/qwen-replicas/halo-2"]["key_configured"])
        self.assertIn("engine/minimax-pair/halo-1", rows)                    # pair: rank 0 serves HTTP
        self.assertNotIn("engine/minimax-pair/halo-2", rows)
        self.assertEqual(rows["engine/minimax-pair/halo-1"]["configured_scope"], "loopback")
        self.assertEqual(rows["engine/minimax-pair/halo-1"]["probe_address"], "192.0.2.10")   # loopback must not answer here
        self.assertEqual((rows["router"]["machine"], rows["router"]["probe_address"], rows["router"]["port"]),
                         ("workstation", "127.0.0.1", 18600))
        self.assertEqual((rows["target/zz"]["machine"], rows["target/zz"]["probe_address"], rows["target/zz"]["scheme"]),
                         ("halo-2", "192.0.2.11", "https"))
        self.assertEqual(rows["target/aa"]["machine"], "workstation")
        changed = [dict(targets[0], rev="3" * 16), targets[1]]
        again = {row["id"]: row for row in endpoints.configured(cluster, changed)}
        self.assertNotEqual(again["target/zz"]["basis"], rows["target/zz"]["basis"])
        self.assertEqual(again["router"]["basis"], rows["router"]["basis"])

    def test_router_address_is_exactly_the_rendered_one(self):
        local = lab_cluster(pc={"router_listen": "192.0.2.20:18610"})
        self.assertEqual(endpoints.router_listen(local)[1], {"address": "192.0.2.20", "port": 18610})
        remote = lab_cluster(router={"machine": "halo-1"}, pc={"router_listen": "192.0.2.20:18610"})
        self.assertEqual(endpoints.router_listen(remote), ("halo-1", {"address": "192.0.2.10", "port": 18600}))
        explicit = lab_cluster(router={"machine": "halo-1", "listen": "192.0.2.10:18700"})
        self.assertEqual(endpoints.router_listen(explicit)[1]["port"], 18700)
        from hyperloom import render
        cluster = lab_cluster(router={"machine": "halo-1", "api_key_file": "/opt/k", "replica_api_key_files": {"halo-1": "/k", "halo-2": "/k"}},
                              pc={"router_listen": "192.0.2.20:18610"},
                              profiles={"qwen-replicas": {"nodes": ["halo-1", "halo-2"], "port": 18501}})
        self.assertEqual(render.render_router(cluster, "qwen-replicas")["listen"], endpoints.router_listen(cluster)[1])


class ListenerEvidenceTests(Base):
    def test_fleet_host_documents(self):
        cluster = lab_cluster()
        store, now = self.api.telemetry, self.clock()
        recorded = json.loads((FIXTURES / "host-listeners.json").read_text(encoding="utf-8"))
        store.put("fleet/host", "halo-1", dict(recorded, ts=now - 5))
        self.assertEqual(endpoints.listener(store, cluster, "halo-1", 18501, now), {"state": "lan"})
        self.assertEqual(endpoints.listener(store, cluster, "halo-1", 18521, now), {"state": "loopback"})
        self.assertEqual(endpoints.listener(store, cluster, "halo-1", 22, now), {"state": "any"})       # widest wins
        self.assertEqual(endpoints.listener(store, cluster, "halo-1", 9999, now)["state"], "not-listening")
        store.put("fleet/host", "halo-1", dict(recorded, ts=now - 95))
        stale = endpoints.listener(store, cluster, "halo-1", 18501, now)
        self.assertEqual(stale["state"], "unavailable")
        self.assertIn("95 s ago", stale["why"])
        store.put("fleet/host", "halo-1", host_doc(now, [], ok=False))
        self.assertIn("failed", endpoints.listener(store, cluster, "halo-1", 18501, now)["why"])
        store.put("fleet/host", "halo-1", host_doc(now, None, status="unavailable"))
        self.assertIn("proc not readable", endpoints.listener(store, cluster, "halo-1", 18501, now)["why"])
        store.put("fleet/host", "halo-1", host_doc(now, []))
        self.assertEqual(endpoints.listener(store, cluster, "halo-1", 18501, now)["state"], "not-listening")
        self.assertIn("no host answer", endpoints.listener(store, cluster, "halo-2", 18501, now)["why"])
        self.assertIn("runs windows", endpoints.listener(store, cluster, "workstation", 18600, now)["why"])
        self.assertEqual(endpoints.listener(store, cluster, None, 1, now)["state"], "unavailable")


class DecisionTableTests(unittest.TestCase):
    def row(self, P, S, C, K, L, **extra):
        return {"unauthenticated": {"outcome": P, "detail": "detail"}, "probe_scope": S, "configured_scope": C,
                "key_configured": K, "listener": {"state": L}, "probe_address": "192.0.2.10", "port": 1, **extra}

    def test_every_combination_has_exactly_one_verdict(self):
        outcomes = ("answered", "refused", "other", "not_reachable")
        listeners = ("any", "lan", "loopback", "not-listening", "unavailable")
        for P, S, C, K, L in itertools.product(outcomes, ("lan", "loopback"), ("lan", "loopback"), (True, False), listeners):
            verdict, reason = endpoints.verdict(self.row(P, S, C, K, L))
            self.assertIn(verdict, endpoints.VERDICTS)
            self.assertTrue(reason)
            if P == "answered" and (S == "lan" or L in ("any", "lan")):
                self.assertEqual(verdict, "red", (P, S, C, K, L))
            else:
                self.assertNotEqual(verdict, "red", (P, S, C, K, L))
            if verdict == "green":
                # Green needs a refusal, loopback evidence, a not-listening table, or ...
                self.assertTrue(P == "refused" or L in ("loopback", "not-listening"), (P, S, C, K, L))

    def test_listed_rows(self):
        v = lambda *a, **kw: endpoints.verdict(self.row(*a, **kw))[0]
        self.assertEqual(v("answered", "lan", "loopback", False, "unavailable"), "red")        # rule 3
        self.assertEqual(v("other", "lan", "loopback", False, "unavailable"), "amber")         # rule 8
        self.assertEqual(v("refused", "lan", "lan", True, "lan"), "green")
        self.assertEqual(v("not_reachable", "lan", "loopback", False, "unavailable"), "amber")  # rule 14
        self.assertEqual(v("not_reachable", "lan", "loopback", False, "loopback"), "green")
        self.assertEqual(v("not_reachable", "lan", "lan", True, "not-listening"), "green")
        self.assertEqual(v("not_reachable", "lan", "lan", True, "unavailable"), "amber")
        self.assertEqual(v("not_reachable", "lan", "lan", True, "lan"), "amber")
        self.assertEqual(v("answered", "loopback", "loopback", False, "loopback"), "green")
        self.assertEqual(v("answered", "loopback", "loopback", False, "unavailable"), "amber")
        self.assertEqual(v("answered", "loopback", "loopback", False, "any"), "red")
        self.assertEqual(v("refused", "lan", "lan", False, "lan", key_supported=False), "amber")
        self.assertIn("no key support", endpoints.verdict(self.row("refused", "lan", "lan", False, "lan", key_supported=False))[1])
        self.assertEqual(v("not_reachable", "loopback", "loopback", False, "unavailable"), "unknown")
        self.assertEqual(endpoints.verdict({"error": "broken"})[0], "unknown")
        self.assertEqual(endpoints.verdict({"unauthenticated": {"outcome": None, "detail": "late"}})[1], "late")


class RoundTests(Base):
    def setUp(self):
        super().setUp()
        self.api.orchestrate.load_cluster.return_value = self.cluster = lab_cluster(profiles={})

    def add(self, port, host="127.0.0.1", **extra):
        return self.api.feature_post("endpoints/save", {"label": "box", "host": host, "port": port, **extra})[1]["target"]

    def test_round_publishes_verdicts_and_never_sends_credentials(self):
        open_server = self.serve({"/v1/models": MODELS})
        guarded = self.serve({"/v1/models": json_route({"error": "key"}, 401)})
        self.add(open_server.port, id="open", key_name="box-key")
        self.add(guarded.port, id="guarded")
        self.api.telemetry.put("fleet/host", "workstation", host_doc(self.clock(), [
            {"port": open_server.port, "family": "ipv4", "scope": "loopback"}]))
        with mock.patch.object(endpoints.secrets, "get", return_value="cd" * 32):
            document = endpoints.exposure_round(self.api, self.clock())
        rows = {row["id"]: row for row in document["rows"]}
        self.assertEqual(rows["target/open"]["verdict"], "green")           # loopback answer with loopback evidence
        self.assertEqual(rows["target/guarded"]["verdict"], "green")        # refused without credentials
        self.assertEqual(rows["target/guarded"]["unauthenticated"]["outcome"], "refused")
        self.assertTrue(all("Authorization" not in headers for _, headers in open_server.seen + guarded.seen))
        self.assertEqual([p for p, _ in open_server.seen], ["/v1/models"])
        shown = self.api.read("endpoints/exposure", {})
        self.assertEqual(shown["summary"]["green"], 3)                     # open, guarded, router not listening
        self.clock.now += endpoints.STALE_EXPOSURE_S + 1
        stale = self.api.read("endpoints/exposure", {})
        self.assertTrue(all(row["verdict"] == "unknown" for row in stale["rows"]))
        self.assertIn("stale", stale["why"])

    def test_older_rounds_never_replace_newer_and_edits_invalidate_rows(self):
        server = self.serve({"/v1/models": MODELS})
        saved = self.add(server.port, id="box")
        newer = endpoints.exposure_round(self.api, self.clock() + 10)
        endpoints.exposure_round(self.api, self.clock())                  # a slow round that started earlier
        self.assertEqual(self.api.telemetry.get(endpoints.NAMESPACE, endpoints.EXPOSURE_KEY)["checked_at"], newer["checked_at"])
        self.clock.now += 10
        self.add(server.port, id="box", host="127.0.0.2")                  # re-pointed during/after the round
        rows = {row["id"]: row for row in self.api.read("endpoints/exposure", {})["rows"]}
        self.assertEqual((rows["target/box"]["verdict"], rows["target/box"]["reason"]),
                         ("unknown", "configuration changed since the last check"))
        self.add(server.port + 1 if server.port < 65535 else 1, id="new")
        rows = {row["id"]: row for row in self.api.read("endpoints/exposure", {})["rows"]}
        self.assertEqual(rows["target/new"]["reason"], "not checked yet")
        self.api.feature_post("endpoints/remove", {"id": "new"})
        self.assertNotIn("target/new", {row["id"] for row in self.api.read("endpoints/exposure", {})["rows"]})
        self.assertEqual(saved["id"], "box")

    def test_population_beyond_the_worker_cap_meets_one_deadline(self):
        def hang(handler):
            time.sleep(3)
        slow = self.serve({"/v1/models": hang})
        self.add(slow.port, id="t00")
        for number in range(1, endpoints.MAX_TARGETS):
            self.add(slow.port, host=f"127.0.0.{number + 1}", id=f"t{number:02d}")
        profiles = {f"qwen-{n}": {"preset": "qwen-replicas", "nodes": ["halo-1", "halo-2"], "port": 18500 + n} for n in range(4)}
        self.api.orchestrate.load_cluster.return_value = lab_cluster(profiles=profiles)
        class Slow(endpoints.Session):
            """Every address hangs until its budget runs out (the 192.0.2.x engines too: nothing is contacted)."""
            def get(self, path, auth=True):
                time.sleep(max(0.0, min(0.4, self.deadline - time.monotonic())))
                return endpoints.Reply(error="timed out (3 s)")
        factory = Slow
        started = time.monotonic()
        with mock.patch.object(endpoints, "EXPOSURE_WORKERS", 8):
            document = endpoints.exposure_round(self.api, self.clock(), session_factory=factory, deadline_s=0.6)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(len(document["rows"]), 1 + 8 + endpoints.MAX_TARGETS)
        late = [row for row in document["rows"] if (row.get("unauthenticated") or {}).get("detail", "").startswith("exposure deadline")]
        self.assertTrue(late)
        self.assertTrue(all(row["verdict"] == "unknown" for row in late))


if __name__ == "__main__":
    unittest.main()
