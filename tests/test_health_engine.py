"""Health findings engine (F20, C10): rules, holds, persistence across restarts, the evaluator lease, documents."""

import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom import admission
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.features import health


T0 = 1_800_000_000.0
SECRET_URL = "https://" + ".".join(("secret", "example", "test")) + "/abc"     # a reserved test name, built at run time


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def machine(mid, system="linux", access="ssh", devices=()):
    return SimpleNamespace(id=mid, os=system, access=access, devices=list(devices))


class FakeAPI:
    def __init__(self, store, clock, directory, machines=None, profiles=None):
        self.telemetry, self.clock, self.sampler = store, clock, None
        self.config_path = Path(directory) / "cluster.json"
        self.audit_path = Path(directory) / "audit.jsonl"
        self.admission_db = admission.Admission(":memory:", clock=clock)
        self.audits, self.node_calls = [], []
        self._cached = None
        self.machines = machines if machines is not None else {
            "workstation": machine("workstation", "windows", "local", [{"id": "gpu1", "excluded": True}]),
            "halo-1": machine("halo-1"), "halo-2": machine("halo-2")}
        self.profiles = profiles if profiles is not None else {"qwen-replicas": {"nodes": ["halo-1", "halo-2"]}}
        self.version = {}

    def cluster(self):
        return SimpleNamespace(machines=self.machines, profiles=self.profiles)

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", "server_time": self.clock(), "why": {}, **fields}

    def audit(self, action, arguments, result):
        self.audits.append((action, arguments, result))

    def node(self, target, verb, payload=None):
        self.node_calls.append((target.id, verb))
        reply = self.version.get(target.id)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read(self, name, query):
        handler, _ = features.registry().find_get(name)
        return handler(self, query)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.api = FakeAPI(self.store, self.clock, self.dir.name)
        self.addCleanup(self.api.admission_db.close)
        for name, kind, legend in (("node_gpu_temp_celsius", "gauge", None), ("node_gpu_busy_percent", "gauge", None),
                                   ("node_gpu_clock_mhz", "gauge", None), ("node_up", "state", {0: "down", 1: "up"}),
                                   ("node_gpu_throttled", "state", {0: "none", 1: "power cap", 2: "thermal", 3: "other"}),
                                   ("node_mem_available_bytes", "gauge", None), ("node_commit_available_bytes", "gauge", None),
                                   ("node_gpu_mem_used_bytes", "gauge", None), ("node_gpu_mem_total_bytes", "gauge", None),
                                   ("node_disk_used_bytes", "gauge", None), ("node_disk_total_bytes", "gauge", None),
                                   ("engine_up", "state", {0: "unreachable", 1: "up"}), ("usb4_link_gbps", "gauge", None)):
            self.store.describe(name, kind, interval=5, legend=legend)

    def at(self, seconds):
        self.clock.now = T0 + seconds
        return self.clock.now

    def record(self, *points):
        self.store.record(list(points), ts=self.clock.now)

    def run_eval(self, api=None):
        api = api or self.api
        return health.evaluate(api, self.clock.now, api.telemetry)

    def states(self):
        return {key: value for key, _, value in self.store.items(health.STATE)}

    def health_events(self, store=None):
        return [e for e in (store or self.store).events(category="health", limit=500)]


class StateMachineTests(Base):
    def hot(self, value, machine_id="halo-1"):
        self.record(("node_gpu_temp_celsius", {"machine": machine_id, "device": "card0", "sensor": "edge"}, value))

    def test_hold_before_firing_and_before_clearing(self):
        for t in (0, 30, 60):
            self.at(t)
            self.hot(92)
            self.run_eval()
        finding = self.states()["gpu-hot:halo-1/card0"]
        self.assertEqual((finding["state"], finding["severity"]), ("pending", "critical"))
        self.assertEqual(self.health_events(), [])
        self.at(90)
        self.hot(92)
        self.run_eval()
        finding = self.states()["gpu-hot:halo-1/card0"]
        self.assertEqual((finding["state"], finding["since"]), ("firing", T0 + 90))
        self.assertTrue(finding["occurrence"])
        self.assertEqual([e["data"]["change"] for e in self.health_events()], ["appeared"])
        self.at(100)
        self.hot(70)
        self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["state"], "clearing")
        self.at(120)
        self.hot(70)
        self.run_eval()
        self.assertIn("gpu-hot:halo-1/card0", self.states())
        self.at(130)
        self.hot(70)
        self.run_eval()
        self.assertNotIn("gpu-hot:halo-1/card0", self.states())
        self.assertEqual([e["data"]["change"] for e in self.health_events()], ["cleared", "appeared"])

    def test_flapping_during_the_hold_never_fires(self):
        for t, value in ((0, 92), (30, 70), (60, 92), (90, 92)):
            self.at(t)
            self.hot(value)
            self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["state"], "pending")
        self.assertEqual(self.health_events(), [])

    def test_unknown_values_neither_fire_nor_clear_and_gaps_restart_holds(self):
        self.at(0)
        self.hot(92)
        self.run_eval()
        self.at(120)                 # no fresh sample: unknown
        self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["state"], "pending")
        self.at(130)
        self.hot(92)                 # evidence again after a 130 s gap: the hold restarts here
        self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["first_true"], T0 + 130)
        for t in range(140, 240, 10):
            self.at(t)
            self.hot(92)
            self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["state"], "firing")
        self.at(1000)                # the source goes away for a long time: still firing, never resolved silently
        self.run_eval()
        self.at(5000)
        self.run_eval()
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["state"], "firing")

    def test_escalation_and_de_escalation_are_logged(self):
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0})
        self.at(0)
        self.hot(86)
        self.run_eval()
        self.at(10)
        self.hot(91)
        self.run_eval()
        self.at(20)
        self.hot(87)
        self.run_eval()
        self.assertEqual([e["data"]["change"] for e in reversed(self.health_events())], ["appeared", "escalated", "de-escalated"])
        self.assertEqual(self.states()["gpu-hot:halo-1/card0"]["severity"], "warning")

    def test_restart_neither_resolves_nor_re_announces(self):
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0})
        self.at(0)
        self.hot(95)
        self.run_eval()
        before = self.states()["gpu-hot:halo-1/card0"]
        events = len(self.health_events())
        # A new portal process (new API object, new owner) after the old one's lease lapsed.
        restarted = FakeAPI(self.store, self.clock, self.dir.name)
        self.addCleanup(restarted.admission_db.close)
        self.at(200)
        self.run_eval(restarted)       # no fresh value yet: unknown
        self.at(210)
        self.hot(95)
        self.run_eval(restarted)
        after = self.states()["gpu-hot:halo-1/card0"]
        self.assertEqual((after["state"], after["since"], after["occurrence"]), ("firing", before["since"], before["occurrence"]))
        self.assertEqual(len(self.health_events()), events)

    def test_observations_of_removed_machines_never_come_back(self):
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0})
        self.at(0)
        self.hot(95, "halo-2")
        self.run_eval()
        del self.api.machines["halo-2"]
        for t in (10, 20, 30):
            self.at(t)
            self.hot(95, "halo-2")
            self.run_eval()
        self.assertNotIn("gpu-hot:halo-2/card0", self.states())
        self.assertEqual([e["data"]["change"] for e in reversed(self.health_events())], ["appeared", "cleared"])

    def test_a_rule_change_during_an_evaluation_aborts_its_commit(self):
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0})
        self.at(0)
        self.hot(95)
        original = health.configs

        def racing(store):
            result = original(store)
            health.set_rule(store, {"rule": "gpu-hot", "enabled": False})       # lands after the settings were read
            return result
        with mock.patch.object(health, "configs", racing), self.assertRaises(sqlite3.IntegrityError):
            self.run_eval()
        self.assertEqual(self.states(), {})

    def test_disabled_rules_and_removed_machines_resolve_explicitly(self):
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0})
        self.at(0)
        self.hot(95)
        self.hot(95, "halo-2")
        self.run_eval()
        del self.api.machines["halo-2"]
        self.at(10)
        self.hot(95)
        self.run_eval()
        self.assertNotIn("gpu-hot:halo-2/card0", self.states())
        self.assertIn("no longer configured", self.health_events()[0]["message"])
        health.set_rule(self.store, {"rule": "gpu-hot", "enabled": False})
        self.at(20)
        self.run_eval()
        self.assertEqual(self.states(), {})
        self.assertIn("rule disabled", self.health_events()[0]["message"])

    def test_finding_count_is_bounded(self):
        states = {}
        results = {"gpu-hot": [health.Obs(f"m/{i}", True, "warning", "hot", "m") for i in range(5)]}
        cfg = health.configs(self.store)
        changes = health.advance(states, results, cfg, T0, limit=3)
        self.assertEqual((len(changes["states"]), changes["overflow"]), (3, 2))


class LeaseAndCommitTests(Base):
    def test_one_evaluator_per_store_across_connections(self):
        path = Path(self.dir.name) / "telemetry.sqlite3"
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        a, b = FakeAPI(first, self.clock, self.dir.name), FakeAPI(second, self.clock, self.dir.name)
        self.addCleanup(a.admission_db.close)
        self.addCleanup(b.admission_db.close)
        health.evaluate(a, self.clock.now, first)
        self.assertIsNotNone(first.get(health.META, "last"))
        first.delete(health.META)
        health.evaluate(b, self.clock.now, second)
        self.assertIsNone(second.get(health.META, "last"))          # not the holder: no writes
        self.at(LEASE := health.LEASE_TTL + 1)
        health.evaluate(b, self.clock.now, second)                    # the holder went quiet: taken over
        self.assertEqual(health.held(second, health.owner_of(b), self.clock.now), 2)
        self.assertIsNone(health.held(first, health.owner_of(a), self.clock.now))
        self.assertGreater(LEASE, 0)

    def test_a_commit_after_losing_the_lease_writes_nothing(self):
        health.prepare(self.store)
        owner = health.owner_of(self.api)
        generation = health.acquire(self.store, owner, T0)
        self.store.execute("UPDATE health_lease SET owner='someone-else', generation=generation+1")
        with self.assertRaises(sqlite3.IntegrityError):
            health.commit(self.store, health.guard(owner, generation, T0) + [
                health.upsert_doc(health.STATE, "x", {"id": "x"}, T0), health.insert_event(T0, "health", "info", "x")])
        self.assertIsNone(self.store.get(health.STATE, "x"))
        self.assertEqual(self.store.events(), [])

    def test_a_failing_statement_rolls_back_the_whole_evaluation(self):
        health.prepare(self.store)
        with self.assertRaises(sqlite3.Error):
            health.commit(self.store, [health.upsert_doc(health.STATE, "x", {"id": "x"}, T0),
                                       health.insert_event(T0, "health", "info", "x"), ("INSERT INTO nowhere VALUES (1)", ())])
        self.assertIsNone(self.store.get(health.STATE, "x"))
        self.assertEqual(self.store.events(), [])


class RuleTests(Base):
    def evaluate_rule(self, rule_id, params=None):
        ctx = health.Context(self.api, self.store, self.clock.now, health.owner_of(self.api))
        rule = health.BY_ID[rule_id]
        merged = {name: spec[0] for name, spec in rule.params.items()}
        merged.update(params or {})
        return rule.evaluate(ctx, merged), ctx

    def verdicts(self, rule_id, params=None):
        out, _ = self.evaluate_rule(rule_id, params)
        return None if out is None else {o.subject: (o.verdict, o.severity) for o in out}

    def test_memory_domains_are_judged_separately(self):
        g = health.GIB
        self.record(("node_mem_available_bytes", {"machine": "halo-1"}, 3 * g),
                    ("node_mem_available_bytes", {"machine": "halo-2"}, 20 * g),
                    ("node_mem_available_bytes", {"machine": "workstation"}, 1 * g),       # Windows physical: no threshold
                    ("node_commit_available_bytes", {"machine": "workstation"}, 6 * g),
                    *[("node_gpu_mem_total_bytes", {"machine": "halo-1", "device": "card0", "domain": d}, v) for d, v in (("vram", 64 * g), ("gtt", 48 * g))],
                    *[("node_gpu_mem_used_bytes", {"machine": "halo-1", "device": "card0", "domain": d}, v) for d, v in (("vram", 60 * g), ("gtt", 45 * g))],
                    ("node_gpu_mem_total_bytes", {"machine": "workstation", "device": "gpu0", "domain": "vram"}, 96 * g),
                    ("node_gpu_mem_used_bytes", {"machine": "workstation", "device": "gpu0", "domain": "vram"}, 94.5 * g),
                    ("node_gpu_mem_total_bytes", {"machine": "workstation", "device": "gpu1", "domain": "vram"}, 24 * g),
                    ("node_gpu_mem_used_bytes", {"machine": "workstation", "device": "gpu1", "domain": "vram"}, 24 * g))
        self.assertEqual(self.verdicts("memory-headroom"), {
            "halo-1/system": (True, "critical"), "halo-2/system": (False, None),
            "workstation/commit": (True, "warning"), "halo-1/card0/gpu-visible": (True, "warning"),
            "workstation/gpu0/vram": (True, "warning")})

    def test_fleet_thresholds_are_used_when_saved(self):
        self.store.put("prefs", "fleet/thresholds", {"unified": {"amber_gib": 2.0, "red_gib": 1.0}})
        self.record(("node_mem_available_bytes", {"machine": "halo-1"}, 3 * health.GIB))
        out, ctx = self.evaluate_rule("memory-headroom")
        self.assertEqual([(o.subject, o.verdict) for o in out], [("halo-1/system", False)])
        self.assertIn(ctx.sources["thresholds"], ("fleet thresholds document", "fleet thresholds setting"))
        self.assertEqual(health.parse_thresholds({"thresholds": {"commit": {"amber_gib": 4, "red_gib": None}}})["commit"],
                         {"amber": 4 * health.GIB, "red": None})

    def test_stale_and_missing_values_are_unknown(self):
        bare = Store(":memory:", clock=self.clock)
        self.addCleanup(bare.close)
        ctx = health.Context(self.api, bare, self.clock.now, "x")
        self.assertIsNone(health.BY_ID["disk-full"].evaluate(ctx, {"percent": 90, "critical_percent": 97}))  # never recorded
        self.assertEqual(self.verdicts("disk-full"), {})                  # described, no sample yet
        self.at(-100)
        self.record(("node_disk_used_bytes", {"machine": "halo-1", "mount": "/"}, 95),
                    ("node_disk_total_bytes", {"machine": "halo-1", "mount": "/"}, 100))
        self.at(0)
        self.assertEqual(self.verdicts("disk-full"), {})                  # stale: no verdict at all
        self.record(("node_disk_used_bytes", {"machine": "halo-1", "mount": "/"}, 98),
                    ("node_disk_total_bytes", {"machine": "halo-1", "mount": "/"}, 100))
        self.assertEqual(self.verdicts("disk-full"), {"halo-1/disk:/": (True, "critical")})

    def test_low_clock_throttle_and_unreachable(self):
        labels = {"machine": "workstation", "device": "gpu0"}
        self.record(("node_gpu_busy_percent", labels, 95), ("node_gpu_clock_mhz", labels, 400),
                    ("node_gpu_throttled", labels, 2), ("node_up", {"machine": "halo-2"}, 0), ("node_up", {"machine": "halo-1"}, 1))
        self.assertEqual(self.verdicts("gpu-low-clock"), {"workstation/gpu0": (True, "warning")})
        out, _ = self.evaluate_rule("gpu-throttled")
        self.assertEqual((out[0].verdict, out[0].facts["reason"]), (True, "thermal"))
        self.assertEqual(self.verdicts("machine-unreachable"), {"halo-1": (False, "critical"), "halo-2": (True, "critical")})

    def test_a_stop_of_another_profile_never_exempts(self):
        self.api.profiles["other"] = {"nodes": ["halo-1"]}
        self.record(("engine_up", {"profile": "qwen-replicas", "replica": "halo-2", "machine": "halo-2"}, 0))
        self.api.admission_db.acquire("stop", {"halo-1", "router"}, "down other")
        self.assertEqual(self.verdicts("engine-down")["qwen-replicas/halo-2"][0], True)
        self.api.admission_db.acquire("stop", {"halo-2", "router"}, "cli down qwen-replicas")
        self.assertEqual(self.verdicts("engine-down")["qwen-replicas/halo-2"][0], False)

    def test_untyped_or_unknown_state_codes_never_judge(self):
        self.record(("node_up", {"machine": "halo-1"}, 2), ("node_up", {"machine": "halo-2"}, 0.5),
                    ("engine_up", {"profile": "qwen-replicas", "replica": "halo-1"}, 7),
                    ("node_gpu_throttled", {"machine": "workstation", "device": "gpu0"}, 9))
        self.assertEqual({k: v[0] for k, v in self.verdicts("machine-unreachable").items()}, {"halo-1": None, "halo-2": None})
        self.assertEqual(self.verdicts("engine-down"), {"qwen-replicas/halo-1": (None, None)})
        self.assertEqual(self.verdicts("gpu-throttled"), {"workstation/gpu0": (None, None)})
        bare = Store(":memory:", clock=self.clock)
        self.addCleanup(bare.close)
        bare.record([("node_up", {"machine": "halo-1"}, 0)], ts=self.clock.now)      # a gauge, not a state
        ctx = health.Context(self.api, bare, self.clock.now, "x")
        self.assertIsNone(health.BY_ID["machine-unreachable"].evaluate(ctx, {}))

    def test_engine_down_is_expected_during_a_stop_from_any_process(self):
        self.record(("engine_up", {"profile": "qwen-replicas", "replica": "halo-1", "machine": "halo-1"}, 0),
                    ("engine_up", {"profile": "qwen-replicas", "replica": "halo-2", "machine": "halo-2"}, 1))
        self.assertEqual(self.verdicts("engine-down"), {"qwen-replicas/halo-1": (True, "critical"),
                                                       "qwen-replicas/halo-2": (False, "critical")})
        other = admission.Admission(":memory:")      # the same database seen by another process: share the handle
        self.addCleanup(other.close)
        self.api.admission_db.acquire("stop", {"halo-1", "router"}, "down qwen-replicas")
        out, _ = self.evaluate_rule("engine-down")
        planned = {o.subject: (o.verdict, o.note) for o in out}
        self.assertEqual(planned["qwen-replicas/halo-1"], (False, "draining · planned stop"))
        broken = mock.Mock()
        broken.holds.side_effect = OSError("locked")
        self.api.admission_db = broken
        self.assertEqual(self.verdicts("engine-down")["qwen-replicas/halo-1"][0], None)

    def test_replica_state_needs_a_fresh_cached_snapshot_and_a_typed_state(self):
        self.assertIsNone(self.verdicts("replica-down"))
        replicas = [{"id": "halo-1", "node": "halo-1", "state": "down"}, {"id": "halo-2", "node": "halo-2", "state": "up"},
                    {"id": "halo-3", "node": "halo-3", "state": "probing"}]
        self.api._cached = {"devices": {"server_time": self.clock.now - 5, "machines": []},
                            "stats": {"routers": [{"profile": "qwen-replicas", "replicas": replicas}]}}
        self.assertEqual(self.verdicts("replica-down"), {"qwen-replicas/halo-1": (True, "warning"),
                                                        "qwen-replicas/halo-2": (False, "warning"),
                                                        "qwen-replicas/halo-3": (None, "warning")})
        self.api._cached["devices"]["server_time"] = self.clock.now - 60
        self.assertIsNone(self.verdicts("replica-down"))

    def test_route_state_files_are_bounded_and_contained(self):
        runtime = Path(self.dir.name) / "run"
        runtime.mkdir()
        state = runtime / "route-state.json"
        state.write_text(json.dumps({"at": self.clock.now - 45}), encoding="utf-8")
        record = {"profile": "qwen-replicas", "recipe": {"route_table": {"state_file": str(state)}}}
        (runtime / "up-qwen-replicas.json").write_text(json.dumps(record), encoding="utf-8")
        from hyperloom import orchestrate, secrets
        with mock.patch.object(secrets, "runtime_dir", lambda: runtime), \
                mock.patch.object(orchestrate, "_state_path", lambda profile: runtime / ("up-" + profile + ".json")):
            self.assertEqual(self.verdicts("route-state-stale"), {"qwen-replicas": (True, "warning")})
            outside = Path(self.dir.name) / "elsewhere.json"
            outside.write_text(json.dumps({"at": self.clock.now}), encoding="utf-8")
            record["recipe"]["route_table"]["state_file"] = str(outside)
            (runtime / "up-qwen-replicas.json").write_text(json.dumps(record), encoding="utf-8")
            out, ctx = self.evaluate_rule("route-state-stale")
            self.assertEqual([(o.subject, o.verdict) for o in out], [("qwen-replicas", None)])
            self.assertIn("unreadable", ctx.sources["route-state:qwen-replicas"])
        budget = health.Budget(10)
        with self.assertRaises(ValueError):
            budget.read_json(state, 1 << 20, runtime)

    def test_usb4_rate_and_per_boot_kernel_evidence(self):
        self.record(("usb4_link_gbps", {"machine": "halo-1", "router": "0-2", "direction": "rx"}, 20),
                    ("usb4_link_gbps", {"machine": "halo-1", "router": "1-2", "direction": "rx"}, 40))
        self.assertEqual(self.verdicts("usb4-below-rate"), {"halo-1/usb4/0-2/rx": (True, "warning"),
                                                           "halo-1/usb4/1-2/rx": (False, "warning")})
        self.assertIsNone(self.verdicts("usb4-kernel-evidence"))
        journal = {"status": "ok", "boot_id": "boot-a", "totals": {"reset": 0, "teardown": 0}, "complete": False,
                   "read_at": self.clock.now}
        self.store.put("usb4/journal", "halo-1", journal)
        self.assertEqual(self.verdicts("usb4-kernel-evidence"), {"halo-1/usb4-evidence": (None, None)})
        self.store.put("usb4/journal", "halo-1", {**journal, "complete": True})
        self.assertEqual(self.verdicts("usb4-kernel-evidence"), {"halo-1/usb4-evidence": (False, "warning")})
        self.store.put("usb4/journal", "halo-1", {**journal, "totals": {"reset": 2, "teardown": 0}})
        self.assertEqual(self.verdicts("usb4-kernel-evidence"), {"halo-1/usb4-evidence": (True, "warning")})
        self.store.put("usb4/journal", "halo-1", {**journal, "read_at": self.clock.now - 600})
        self.assertEqual(self.verdicts("usb4-kernel-evidence"), {"halo-1/usb4-evidence": (None, None)})

    def test_kernel_evidence_increases_are_activity_events(self):
        journal = {"status": "ok", "boot_id": "boot-a", "generation": 1, "cursor": "c1", "totals": {"reset": 1},
                   "complete": True, "read_at": self.clock.now}

        def step(seconds, **change):
            self.at(seconds)
            self.store.put("usb4/journal", "halo-1", {**journal, **change, "read_at": self.clock.now})
            self.run_eval()
        step(0)                                                              # first sight: checkpoint only
        self.assertEqual(self.store.events(category="usb4"), [])
        step(10, cursor="c2", totals={"reset": 3})                           # the cursor moved: 2 new lines
        step(20, generation=2, cursor="c9", totals={"reset": 4})             # a replacement scan: rebaseline only
        step(30, generation=2, cursor="c9", totals={"reset": 4})             # unchanged
        step(40, boot_id="boot-b", generation=1, cursor="d1", totals={"reset": 1})   # a new boot's evidence
        self.assertEqual([(e["data"]["new"], "new boot" in e["message"]) for e in self.store.events(category="usb4")],
                         [({"reset": 1}, True), ({"reset": 2}, False)])

    def test_turns_follow_the_producers_fresh_verdicts_only(self):
        registry = features.Registry()
        registry.gets["turns"] = lambda api, query: {"nodes": [
            {"machine": "halo-1", "state": "ok", "holder": {"label": "bench", "parsed": True, "since": 0.0,
                                                             "elapsed_s": 900.0, "cap_s": 480.0, "past_cap": True}},
            # the node clock differs from ours: the producer's corrected verdict wins
            {"machine": "halo-2", "state": "ok", "holder": {"label": "x", "since": self.clock.now - 9000,
                                                             "elapsed_s": 60.0, "cap_s": 480.0, "past_cap": False}},
            {"machine": "halo-3", "state": "unavailable", "holder": None},                 # unreadable: unknown
            {"machine": "halo-4", "state": "stale", "holder": {"past_cap": True}},          # stale: unknown
            {"machine": "halo-5", "state": "ok", "holder": None},                           # fresh, nobody holds
            {"machine": "halo-6", "state": "ok", "holder": {"label": "y", "parsed": False}}]}
        features.reset(registry)
        self.addCleanup(features.reset)
        verdicts = {k: v[0] for k, v in self.verdicts("turn-over-cap").items()}
        self.assertEqual(verdicts, {"halo-1/turn": True, "halo-2/turn": False, "halo-3/turn": None, "halo-4/turn": None,
                                    "halo-5/turn": False, "halo-6/turn": None})

    def test_node_tool_from_the_operations_records_without_node_calls(self):
        from hyperloom import remote
        self.assertIsNone(self.verdicts("node-tool-stale"))
        self.store.put("ops", "node_tool", {
            "halo-1": {"checked_at": self.clock.now - 60, "deployed": "0" * 64},
            "halo-2": {"checked_at": self.clock.now - 60, "deployed": remote.tool_digest()},
            "halo-3": {"checked_at": self.clock.now - 60, "deployed": "1" * 64},          # not configured
            "workstation": {"checked_at": self.clock.now - 7200, "deployed": "0" * 64}})   # too old: unknown
        self.assertEqual(self.verdicts("node-tool-stale"), {"halo-1/node-tool": (True, "info"), "halo-2/node-tool": (False, "info"),
                                                           "workstation/node-tool": (None, None)})
        self.run_eval()
        self.assertEqual(self.states()["node-tool-stale:halo-1/node-tool"]["state"], "firing")
        self.assertEqual(self.api.node_calls, [])

    def test_collector_and_producer_sources_go_stale_only_on_evidence(self):
        self.assertIsNone(self.verdicts("collector-stale"))
        sampler = mock.Mock()
        sampler.snapshot.return_value = {
            "fleet": {"interval_s": 5, "last_ok": self.clock.now - 2, "failures": 0, "error": None},
            "usb4": {"interval_s": 10, "last_ok": self.clock.now - 100, "failures": 4, "error": "OSError: token=abc /secret/path"},
            "observe": {"interval_s": 2, "last_ok": None, "failures": 0, "error": None},
            "health": {"interval_s": 10, "last_ok": None}}
        self.api.sampler = sampler
        self.store.put("usb4/node", "halo-1", {"sources": {
            "journal": {"interval_s": 60, "last_ok": self.clock.now - 400, "last_attempt": self.clock.now - 10},
            "links": {"interval_s": 10, "last_ok": self.clock.now - 3, "last_attempt": self.clock.now - 3},
            "layout": {"interval_s": 10, "last_ok": None, "last_attempt": None}}})
        out, _ = self.evaluate_rule("collector-stale")
        verdicts = {o.subject: o.verdict for o in out}
        self.assertEqual(verdicts, {"fleet": False, "usb4": True, "observe": None, "halo-1/usb4-journal": True,
                                    "halo-1/usb4-links": False, "halo-1/usb4-layout": None})
        detail = next(o.detail for o in out if o.subject == "usb4")
        self.assertIn("last OSError", detail)
        self.assertNotIn("token", detail)
        self.assertNotIn("secret", detail)


class LeaseEventTests(Base):
    def snapshot(self, leases, node_error=None):
        status = {"nodes": {"halo-1": {"leases": [], **({"error": node_error} if node_error else {})}}}
        self.api._cached = {"devices": {"server_time": self.clock.now, "machines": [
            {"id": "halo-1", "health": "online", "leases": leases, "why": {}}]},
            "profiles": {"profiles": [{"id": "qwen-replicas", "status": status}]}}

    def test_leases_only_from_authoritative_status(self):
        self.snapshot([{"lease": "l1", "label": "hl-qwen"}])
        self.run_eval()                                           # first sight: silent
        self.at(10)
        self.snapshot([], node_error="ssh timed out")             # unknown: keep the previous set
        self.run_eval()
        self.assertEqual(self.store.events(category="leases"), [])
        self.at(20)
        self.snapshot([{"lease": "l2", "label": "hl-bench"}])
        self.run_eval()
        messages = [e["message"] for e in self.store.events(category="leases")]
        self.assertEqual(sorted(messages), ["lease acquired on halo-1 · hl-bench", "lease released on halo-1 · hl-qwen"])


class DocumentTests(Base):
    def test_documents_summary_and_banner(self):
        self.store.put(health.CONFIG, "gpu-throttled", {"hold_s": 0})
        self.store.put(health.CONFIG, "disk-full", {"hold_s": 0})
        labels = {"machine": "workstation", "device": "gpu0"}
        self.record(("node_gpu_throttled", labels, 1), ("node_disk_used_bytes", {"machine": "halo-1", "mount": "/"}, 92),
                    ("node_disk_total_bytes", {"machine": "halo-1", "mount": "/"}, 100))
        self.run_eval()
        summary = health.summary_document(self.api, {})
        self.assertEqual(summary["counts"], {"info": 0, "warning": 1, "critical": 1})
        self.assertEqual([item["severity"] for item in summary["strip"]], ["critical", "warning"])
        self.assertIsNone(summary["banner"])                       # power cap: throttling, but not thermal
        self.at(10)
        self.record(("node_gpu_throttled", labels, 2))
        self.run_eval()
        self.assertTrue(health.summary_document(self.api, {})["banner"].startswith("GPU thermal slowdown on workstation gpu0"))
        document = health.findings_document(self.api, {})
        self.assertEqual([f["rule"] for f in document["findings"]], ["gpu-throttled", "disk-full"])
        self.assertFalse(document["stale"])
        rules = {rule["id"]: rule for rule in health.rules_document(self.api, {})["rules"]}
        self.assertEqual((rules["gpu-throttled"]["active"], rules["gpu-throttled"]["overridden"]), (1, True))
        self.assertFalse(rules["turn-over-cap"]["available"])
        self.at(100)
        self.assertTrue(health.summary_document(self.api, {})["stale"])
        with self.assertRaises(ValueError):
            health.summary_document(self.api, {"x": "1"})

    def test_without_a_store_documents_say_why_and_never_create_one(self):
        self.api.telemetry = None
        for build in (health.findings_document, health.rules_document, health.summary_document):
            document = build(self.api, {})
            self.assertTrue(document["why"])

    def test_rule_changes_are_validated_and_audited_with_safe_values_only(self):
        result = health.set_rule_post(self.api, {"rule": "gpu-hot", "params": {"warn_c": 80}, "hold_s": 30})
        self.assertEqual((result["effective"]["params"]["warn_c"], result["effective"]["hold_s"]), (80, 30))
        bad = [{"rule": "nope"}, {"rule": "gpu-hot", "params": {"warn_c": 500}}, {"rule": "gpu-hot", "params": {"x": 1}},
               {"rule": "gpu-hot", "hold_s": -1}, {"rule": "gpu-hot", "enabled": "yes"},
               {"rule": "gpu-hot", "params": {"warn_c": 95}}, {"rule": "gpu-hot", "url": SECRET_URL}]
        for arguments in bad:
            with self.assertRaises((ValueError, LookupError), msg=arguments):
                health.set_rule_post(self.api, arguments)
        text = json.dumps(self.api.audits)
        self.assertNotIn(SECRET_URL, text)
        self.assertEqual([a[2]["state"] for a in self.api.audits], ["succeeded"] + ["refused"] * len(bad))
        health.set_rule_post(self.api, {"rule": "gpu-hot", "reset": True})
        self.assertIsNone(self.store.get(health.CONFIG, "gpu-hot"))

    def test_concurrent_partial_rule_updates_both_survive(self):
        original, calls = health._merged_rule, []

        def racing(rule, arguments, stored, now):
            if not calls:
                calls.append(1)
                health.set_rule(self.store, {"rule": "gpu-hot", "enabled": False})     # lands between read and commit
            return original(rule, arguments, stored, now)
        with mock.patch.object(health, "_merged_rule", racing):
            health.set_rule(self.store, {"rule": "gpu-hot", "hold_s": 5})
        self.assertEqual(self.store.get(health.CONFIG, "gpu-hot"), {"enabled": False, "hold_s": 5.0})

    def test_registration(self):
        reg = features.Registry()
        health.register(reg)
        self.assertEqual(sorted(reg.gets), ["health", "health/rules", "health/summary"])
        self.assertEqual(list(reg.posts), ["health/rules/set"])
        self.assertFalse(reg.audited)
        self.assertEqual(reg.collectors["health"][1], 10.0)

    def test_the_collector_describes_its_gauge(self):
        sampler_api = self.api
        points = health.collect(sampler_api, self.clock.now)
        self.assertEqual(sorted(p[1]["severity"] for p in points), ["critical", "info", "warning"])
        self.assertEqual(self.store.descriptor("health_findings_active")["kind"], "gauge")


if __name__ == "__main__":
    unittest.main()
