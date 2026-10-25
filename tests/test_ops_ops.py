"""Operations (F38, F39, F41, F42): node tool status/update, Wake-on-LAN and shutdown, the machine configuration
editor, refresh-now and collector intervals (portal endpoints, jobs and the command line)."""

import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

from hyperloom import admission, config
from hyperloom.telemetry import Sampler
from hyperloom.web import features
from hyperloom.web.features import ops
from tests.test_ops_turns import APIFixture

RAW = {"schema": "hyperloom/cluster-v1",
       "machines": [{"id": "workstation", "os": "windows", "access": "local", "http_address": "127.0.0.1",
                     "install_root": "C:/hl"},
                    {"id": "halo-1", "os": "linux", "access": "ssh", "http_address": "192.0.2.10", "ssh_target": "halo-1",
                     "install_root": "/opt/hl", "devices": [{"id": "gpu"}]},
                    {"id": "halo-2", "os": "linux", "access": "ssh", "http_address": "192.0.2.11", "ssh_target": "halo-2",
                     "install_root": "/opt/hl", "devices": [{"id": "gpu"}]}],
       "assets": {}, "profiles": {"qwen-replicas": {"nodes": ["halo-1"]}}}


class Base(APIFixture):
    def setUp(self):
        self.api = self.make_api()
        self.config = Path(self.api.config_path)
        self.config.write_text(json.dumps(RAW), encoding="utf-8")
        self.api._cluster = lambda: config.load(self.config)
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)
        self.replies = {}
        self.calls = []

        def node(api, machine, verb, payload=None):
            self.calls.append((machine.id, verb, payload))
            reply = self.replies.get((machine.id, verb), self.replies.get(verb))
            if isinstance(reply, Exception):
                raise reply
            return reply if reply is not None else {"schema": "hyperloom/node-v1", "verb": verb}
        patcher = mock.patch.object(ops, "_node_call", node)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, job):
        for _ in range(500):
            current = self.api.read("jobs/" + job["id"])
            if current["state"] != "running":
                return current
            time.sleep(0.01)
        self.fail("job did not finish")


class NodeToolTests(Base, unittest.TestCase):
    def test_status_document_from_the_collector(self):
        expected = ops.expected_digest()
        self.replies[("halo-1", "version")] = {"checks": {"tool": {"value": expected}}}
        self.replies[("halo-2", "version")] = RuntimeError("OpenSSH command failed (exit 255)")
        self.replies[("halo-1", "host")] = {"interfaces": [{"name": "eth0", "mac": "02:00:5E:10:00:01"},
                                                           {"name": "x", "mac": "01:00:5e:00:00:01"}]}
        before = self.api.read("ops/nodes", {})
        self.assertIsNone(next(m for m in before["machines"] if m["machine"] == "halo-1")["status"])
        ops.sample_nodes(self.api, 1000.0)
        doc = self.api.read("ops/nodes", {})
        rows = {m["machine"]: m for m in doc["machines"]}
        self.assertEqual((rows["halo-1"]["status"], rows["halo-2"]["status"], rows["workstation"]["status"]),
                         ("current", "unreachable", "local"))
        self.assertEqual(rows["halo-1"]["actions"], [{"profile": "qwen-replicas", "machine": "halo-1", "blocked": None}])
        self.assertIn("not part of any profile", rows["halo-2"]["why"]["actions"])
        self.assertEqual(self.api.telemetry.get("ops", "macs")["halo-1"]["macs"], ["02:00:5e:10:00:01"])

    def test_update_job_is_profile_scoped_unforced_and_refused_while_a_lease_lives(self):
        orchestrate = self.api.orchestrate
        orchestrate.nodes_update = mock.Mock(return_value={"halo-1": {"tool": "new"}})
        with self.assertRaises(Exception):
            self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas", "force": True})
        self.replies[("halo-1", "status")] = {"leases": [{"lease": "l1", "state": "ready"}]}
        refused = self.wait(self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas"}))
        self.assertEqual(refused["state"], "failed")
        self.assertIn("lease l1 is live", refused["error"])
        orchestrate.nodes_update.assert_not_called()
        self.replies[("halo-1", "status")] = RuntimeError("unreachable")
        unknown = self.wait(self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas"}))
        self.assertIn("nothing was changed", unknown["error"])
        self.replies[("halo-1", "status")] = {"leases": [{"lease": "l0", "state": "stopped"}]}
        done = self.wait(self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas", "machine": "halo-1"}))
        self.assertEqual(done["state"], "succeeded", done)
        orchestrate.nodes_update.assert_called_once()
        self.assertFalse(orchestrate.nodes_update.call_args.kwargs["force"])
        self.assertEqual(self.api.telemetry.get("ops", "node_tool")["halo-1"]["deployed"], "new")

    def test_update_is_refused_while_a_saved_launch_or_a_benchmark_holds_the_machine(self):
        self.api.orchestrate.statuses = {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}
        self.api.orchestrate.nodes_update = mock.Mock()
        job = self.wait(self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas"}))
        self.assertIn("saved launch", job["error"])
        self.api.orchestrate.statuses = {}
        hold = self.api.admission_db.acquire("bench", {"halo-1"}, label="bench x")
        with self.assertRaises(Exception) as caught:
            self.api.mutate("ops/nodes-update", {"profile": "qwen-replicas"})
        self.assertIn("benchmark", str(caught.exception))
        self.assertIn("benchmark", self.api.read("ops/nodes", {})["machines"][1]["actions"][0]["blocked"])
        self.api.admission_db.release(hold)
        self.api.orchestrate.nodes_update.assert_not_called()


class PowerTests(Base, unittest.TestCase):
    def test_magic_packet_and_wake(self):
        packet = ops.magic_packet("02:00:5e:10:00:02")
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], bytes.fromhex("02005e100002") * 16)
        with self.assertRaises(ValueError):
            ops.magic_packet("02:00:5e")
        with self.assertRaises(Exception) as caught:
            self.api.feature_post("ops/wake", {"machine": "halo-2"})
        self.assertIn("no MAC", str(caught.exception))
        self.api.feature_post("ops/machine-extras", {"machine": "halo-2", "mac": "02:00:5E:10:00:02", "wol_broadcast": "192.0.2.255"})
        sent = []

        class Sock:
            def __init__(self, *a):
                pass

            def setsockopt(self, *a):
                sent.append(("opt", a))

            def sendto(self, data, target):
                sent.append((data, target))

            def close(self):
                pass
        with mock.patch.object(ops.socket, "socket", Sock):
            _, result = self.api.feature_post("ops/wake", {"machine": "halo-2"})
        self.assertEqual([t for d, t in sent[1:]], [("255.255.255.255", 9), ("192.0.2.255", 9)])
        self.assertEqual(result["mac_source"], "configured")
        self.api.telemetry.put("ops", "node_tool", {"halo-2": {"seen_at": self.api.clock()}})
        with self.assertRaises(Exception):
            self.api.feature_post("ops/wake", {"machine": "halo-2"})          # it answered: online
        with self.assertRaises(Exception):
            self.api.feature_post("ops/wake", {"machine": "workstation"})
        with self.assertRaises(Exception):
            self.api.feature_post("ops/machine-extras", {"machine": "halo-2", "wol_broadcast": "8.8.8.8"})

    def test_shutdown_needs_a_cli_configured_helper_and_typed_id(self):
        doc = self.api.read("ops/power", {})
        self.assertIn("no shutdown helper", doc["machines"][1]["shutdown"]["blocked"])
        with self.assertRaises(Exception):
            self.api.mutate("ops/shutdown", {"machine": "halo-2", "confirm": "halo-2"})
        self.assertNotIn("ops/settings", features.registry().posts)        # never settable from the portal
        helper = Path(self.dir.name) / "helper.cmd"
        helper.write_text("x")
        ops.settings(self.api, {"shutdown_helper": str(helper)})
        with self.assertRaises(Exception):
            self.api.mutate("ops/shutdown", {"machine": "halo-2", "confirm": "halo-1"})
        with self.assertRaises(Exception):
            self.api.mutate("ops/shutdown", {"machine": "workstation", "confirm": "workstation"})
        ran = []
        with mock.patch.object(ops, "_bounded_run", lambda argv, timeout, cancelled: ran.append(argv) or (0, b"ok", b"", None)):
            self.replies[("halo-2", "status")] = {"leases": [{"lease": "x", "state": "running"}]}
            refused = self.wait(self.api.mutate("ops/shutdown", {"machine": "halo-2", "confirm": "halo-2"}))
            self.assertIn("live", refused["error"])
            self.replies[("halo-2", "status")] = RuntimeError("no route")
            unknown = self.wait(self.api.mutate("ops/shutdown", {"machine": "halo-2", "confirm": "halo-2"}))
            self.assertIn("nothing was changed", unknown["error"])
            self.replies[("halo-2", "status")] = {"leases": []}
            done = self.wait(self.api.mutate("ops/shutdown", {"machine": "halo-2", "confirm": "halo-2"}))
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(ran, [[str(helper), "halo-2"]])

    def test_bounded_helper_run(self):
        code, out, err, stopped = ops._bounded_run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"], 20)
        self.assertEqual((code, len(out), stopped), (0, ops.HELPER_OUTPUT, None))
        code, out, err, stopped = ops._bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertEqual(stopped, "timed out")


class ConfigTests(Base, unittest.TestCase):
    def version(self):
        return self.api.read("ops/config", {})["version"]

    def test_add_edit_move_remove_with_validation_and_atomic_write(self):
        new = {"id": "halo-3", "os": "linux", "access": "ssh", "http_address": "192.0.2.12", "ssh_target": "halo-3",
               "install_root": "/opt/hl"}
        _, check = self.api.feature_post("ops/config/validate", {"op": "add", "machine": {**new, "http_address": "8.8.8.8"}})
        self.assertFalse(check["valid"])
        self.assertIn("private", check["error"])
        with self.assertRaises(Exception):
            self.api.feature_post("ops/config/save", {"op": "add", "machine": new, "version": "0" * 64})
        self.api.feature_post("ops/config/save", {"op": "add", "machine": new, "version": self.version()})
        self.assertEqual([m["id"] for m in json.loads(self.config.read_text())["machines"]][-1], "halo-3")
        self.assertTrue(self.config.with_name("cluster.json.prev").exists())
        self.api.feature_post("ops/config/save", {"op": "move", "id": "halo-3", "index": 0})
        self.replies[("halo-3", "status")] = {"leases": []}
        self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-3", "machine": {**new, "http_address": "192.0.2.13"}})
        rows = json.loads(self.config.read_text())["machines"]
        self.assertEqual((rows[0]["id"], rows[0]["http_address"]), ("halo-3", "192.0.2.13"))
        with self.assertRaises(Exception):          # used by a profile: the schema refuses the removal
            self.api.feature_post("ops/config/save", {"op": "remove", "id": "halo-1"})
        self.api.feature_post("ops/config/save", {"op": "remove", "id": "halo-3"})
        self.assertEqual([m["id"] for m in json.loads(self.config.read_text())["machines"]], ["workstation", "halo-1", "halo-2"])
        self.assertFalse([p for p in self.config.parent.iterdir() if p.name.endswith(".tmp")])
        audit = [json.loads(line)["action"] for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]
        self.assertIn("ops/config/save", audit)

    def test_identity_is_frozen_while_in_use_or_unknown(self):
        row = dict(RAW["machines"][2])
        self.replies[("halo-2", "status")] = {"leases": [{"lease": "l9", "state": "ready"}]}
        with self.assertRaises(Exception) as caught:
            self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-2", "machine": {**row, "http_address": "192.0.2.99"}})
        self.assertIn("frozen: http_address", str(caught.exception))
        with self.assertRaises(Exception):
            self.api.feature_post("ops/config/save", {"op": "remove", "id": "halo-2"})
        # a non-identity change (a device's type) is still allowed while in use
        self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-2", "machine": {**row, "devices": [{"id": "gpu", "type": "igpu"}]}})
        self.replies[("halo-2", "status")] = RuntimeError("unknown host key")
        with self.assertRaises(Exception) as caught:
            self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-2", "machine": {**row, "ssh_target": "x"}})
        self.assertIn("cannot be read", str(caught.exception))
        with self.assertRaises(Exception):
            self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-2", "machine": row, "assume_idle": "halo-2"})
        self.replies[("halo-2", "status")] = {"leases": []}
        self.api.feature_post("ops/config/save", {"op": "move", "id": "halo-2", "index": 0})       # order is not identity
        self.api.feature_post("ops/config/save", {"op": "edit", "id": "halo-2", "machine": {**row, "http_address": "192.0.2.99"}})

    def test_the_control_machine_is_in_use_while_a_saved_launch_routes_there(self):
        self.api.orchestrate.statuses = {"qwen-replicas": {"profile": "qwen-replicas", "nodes": {}}}
        self.api.orchestrate._read = lambda path: {"router": {"machine": "local"}, "machines": {}}
        row = RAW["machines"][0]
        with self.assertRaises(Exception) as caught:
            self.api.feature_post("ops/config/save", {"op": "edit", "id": "workstation", "machine": {**row, "http_address": "127.0.0.2"}})
        self.assertIn("saved launch", str(caught.exception))

    def test_concurrent_saves_are_serialized(self):
        version = self.version()
        errors = []

        def save(address):
            try:
                self.api.feature_post("ops/config/save", {"op": "edit", "id": "workstation", "version": version,
                                                          "machine": {**RAW["machines"][0], "install_root": address}})
            except Exception as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=save, args=(f"C:/hl{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("changed since it was read", errors[0])

    def test_extras_and_host_key_scan(self):
        self.api.feature_post("ops/machine-extras", {"machine": "halo-1", "hide_disks": ["/boot/efi"], "hide_interfaces": ["wlan0"]})
        doc = self.api.read("ops/config", {})
        self.assertEqual(doc["machines"][1]["extras"], {"hide_disks": ["/boot/efi"], "hide_interfaces": ["wlan0"]})
        key = mock.Mock(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:abc")
        self.api.remote.keyscan.return_value = [key]
        _, scan = self.api.feature_post("ops/hostkey/scan", {"machine": "halo-1"})
        self.assertEqual(scan["candidates"], [{"algorithm": "ssh-ed25519", "fingerprint": "SHA256:abc"}])
        with self.assertRaises(Exception):
            self.api.feature_post("ops/hostkey/scan", {"machine": "workstation"})

    def test_machine_test_never_bootstraps(self):
        hosts = Path(self.dir.name) / "known_hosts"
        hosts.write_text("halo-1,192.0.2.10 ssh-ed25519 AAAA\n")
        self.api.orchestrate._known_hosts = lambda: hosts
        self.replies[("halo-1", "version")] = {"checks": {"tool": {"value": ops.expected_digest()}}}
        self.replies[("halo-1", "gpu")] = {"checks": {"amd": {"status": "ok", "value": [{"id": "card0"}]}}}
        self.replies[("halo-1", "doctor")] = {"checks": {"usb4": {"module": {"status": "ok", "value": True},
                                                                  "streams": {"status": "ok", "value": {"count": 4, "openable": ["a", "b"]}}}}}
        result = self.wait(self.api.mutate("ops/machine-test", {"machine": "halo-1"}))["result"]
        self.assertEqual({s["step"]: s["status"] for s in result["steps"]},
                         {"ssh": "pass", "node tool": "pass", "GPU": "pass", "USB4": "pass", "engines": "pass"})
        unpinned = self.wait(self.api.mutate("ops/machine-test", {"machine": "halo-2"}))["result"]
        self.assertEqual(unpinned["steps"][0]["status"], "skipped")
        self.assertNotIn(("halo-2", "version", {}), self.calls)
        self.assertFalse([c for c in self.calls if c[1] in ("bootstrap", "install")])


class CollectorTests(Base, unittest.TestCase):
    def sampler(self, collectors):
        registry = features.load(("turns", "logsearch", "ops"))
        registry.collectors.clear()
        for name, (fn, interval) in collectors.items():
            registry.collector(name, fn, interval=interval)
        features.reset(registry)
        sampler = Sampler(self.api, self.api.telemetry, registry.collectors)
        self.api.sampler = sampler
        return sampler

    def test_bounds_refresh_and_interval(self):
        sampler = self.sampler({"fast": (lambda api, now: [], 2.0), "slow": (lambda api, now: [], 60.0)})
        self.assertEqual(ops.bounds(2.0), (1.0, 60.0))
        self.assertEqual(ops.bounds(60.0), (30.0, 1800.0))
        for bad in ({"collector": "fast", "seconds": 0.5}, {"collector": "fast", "seconds": float("nan")},
                    {"collector": "nope", "seconds": 5}, {"collector": "fast", "seconds": "5"}):
            with self.assertRaises(Exception, msg=bad):
                self.api.feature_post("telemetry/interval", bad)
        self.api.feature_post("telemetry/interval", {"collector": "fast", "seconds": 5})
        self.assertEqual(sampler.collectors["fast"][1], 5.0)
        self.assertEqual(self.api.telemetry.get("ops", "intervals"), {"fast": 5.0})
        _, result = self.api.feature_post("telemetry/refresh", {"collector": "fast"})
        self.assertEqual(result["state"], "queued")
        doc = self.api.read("ops/collectors", {})
        self.assertEqual({c["collector"]: c["max_s"] for c in doc["collectors"]}, {"fast": 60.0, "slow": 1800.0})

    def test_refresh_without_samplers_is_refused(self):
        self.api.sampler = None
        with self.assertRaises(Exception):
            self.api.feature_post("telemetry/refresh", {"collector": "ops"})
        self.assertEqual(self.api.read("ops/collectors", {})["why"]["state"], "samplers are not running in this portal process")

    def test_persisted_intervals_are_applied_once_per_change(self):
        sampler = self.sampler({"fast": (lambda api, now: [], 2.0)})
        self.api.telemetry.put("ops", "intervals", {"fast": 4.0})
        with mock.patch.object(sampler, "set_interval", wraps=sampler.set_interval) as changed:
            ops.sample_control(self.api, 100.0)
            ops.sample_control(self.api, 102.0)
        self.assertEqual(changed.call_count, 1)

    def test_cli_requests_complete_from_a_collection_that_started_after_them(self):
        runs = []
        sampler = self.sampler({"fast": (lambda api, now: runs.append(now) or [], 2.0)})
        clock = [1000.0]
        sampler.clock = lambda: clock[0]
        rid = ops.queue_request(self.api, "refresh", {"collector": "fast"})
        self.assertEqual(ops.request_state(self.api, rid)["state"], "pending")
        sampler.run_once("fast")                     # an earlier collection: started before the request was handed over
        clock[0] = 1001.0
        ops.sample_control(self.api, clock[0])
        state = ops.request_state(self.api, rid)
        self.assertEqual((state["state"], state["result"]["requested_at"]), ("queued", 1001.0))
        sampler.run_once("fast")            # started at the same clock reading as the request: not counted
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "queued")
        clock[0] = 1002.0
        sampler.run_once("fast")
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "done")
        rid = ops.queue_request(self.api, "interval", {"collector": "fast", "seconds": 3})
        ops.sample_control(self.api, clock[0])
        self.assertEqual((ops.request_state(self.api, rid)["state"], sampler.collectors["fast"][1]), ("done", 3.0))
        with self.assertRaises(LookupError):
            ops.queue_request(self.api, "refresh", {"collector": "nope"})

    def test_a_running_collection_is_joined_and_confirmed_by_the_next(self):
        gate, started = threading.Event(), threading.Event()

        def slow(api, now):
            started.set()
            gate.wait(5)
            return []
        sampler = self.sampler({"slow": (slow, 2.0)})
        clock = [500.0]
        sampler.clock = lambda: clock[0]
        worker = threading.Thread(target=sampler.run_once, args=("slow",))
        worker.start()
        started.wait(5)
        rid = ops.queue_request(self.api, "refresh", {"collector": "slow"})
        clock[0] = 501.0
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "joined")
        gate.set()
        worker.join()
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "joined")      # that run began before the request
        clock[0] = 563.0                                                            # slower than a claim timeout
        sampler.run_once("slow")
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "done")

    def test_an_interrupted_handover_keeps_its_target(self):
        sampler = self.sampler({"fast": (lambda api, now: [], 2.0)})
        clock = [2000.0]
        sampler.clock = lambda: clock[0]
        rid = ops.queue_request(self.api, "refresh", {"collector": "fast"})
        # the claimant dies after recording its target and waking the collector, before the hand-off write
        with mock.patch.object(sampler, "refresh", side_effect=RuntimeError("portal stopped")):
            with self.assertRaises(RuntimeError):
                ops.sample_control(self.api, clock[0])
        state = ops.request_state(self.api, rid)
        self.assertEqual((state["state"], state["result"]["requested_at"]), ("claimed", 2000.0))
        clock[0] = 2070.0                                    # the claim expires; a new claimant reuses the target
        ops.sample_control(self.api, clock[0])
        state = ops.request_state(self.api, rid)
        self.assertEqual((state["state"], state["result"]["requested_at"]), ("queued", 2000.0))
        clock[0] = 2071.0
        sampler.run_once("fast")
        ops.sample_control(self.api, clock[0])
        self.assertEqual(ops.request_state(self.api, rid)["state"], "done")


    def test_recovery_after_ten_minutes_keeps_a_long_retention(self):
        sampler = self.sampler({"slow": (lambda api, now: [], 3600.0)})
        clock = [10_000.0]
        sampler.clock = lambda: clock[0]
        for interrupt_after_wake in (False, True):
            rid = ops.queue_request(self.api, "refresh", {"collector": "slow"})
            effect = None if interrupt_after_wake else RuntimeError("stopped before the wake")
            original = sampler.refresh

            def refresh(name, effect=effect):
                if effect is not None:
                    raise effect
                original(name)
                raise RuntimeError("stopped before the hand-off")
            with mock.patch.object(sampler, "refresh", side_effect=refresh):
                with self.assertRaises(RuntimeError):
                    ops.sample_control(self.api, clock[0])
            target = ops.request_state(self.api, rid)["result"]["requested_at"]
            self.assertGreater(ops.request_state(self.api, rid)["result"]["keep_s"], 7000)
            clock[0] += 700.0                                   # past ten minutes, within the request's retention
            ops.sample_control(self.api, clock[0])
            state = ops.request_state(self.api, rid)
            self.assertEqual((state["state"], state["result"]["requested_at"]), ("queued", target))
            clock[0] += 1.0
            sampler.run_once("slow")
            ops.sample_control(self.api, clock[0])
            self.assertEqual(ops.request_state(self.api, rid)["state"], "done")
            clock[0] += 1.0

    def test_unclaimed_requests_fail_explicitly(self):
        self.sampler({"fast": (lambda api, now: [], 2.0)})
        rid = ops.queue_request(self.api, "refresh", {"collector": "fast"})
        self.api.sampler = None
        self.assertEqual(ops.request_state(self.api, rid)["state"], "pending")
        sampler = self.sampler({"fast": (lambda api, now: [], 2.0)})
        store = self.api.telemetry
        store.execute("UPDATE ops_requests SET created=created-700 WHERE id=?", (rid,))
        with mock.patch.object(sampler, "refresh", return_value=True):
            ops.sample_control(self.api, time.time())
        state = ops.request_state(self.api, rid)
        self.assertEqual(state["state"], "failed")
        self.assertIn("no running portal", state["result"]["error"])


class CLITests(Base, unittest.TestCase):
    def test_cli_commands_reach_the_same_functions(self):
        from hyperloom import cli
        from hyperloom.cliext import ops as cliops
        with mock.patch.object(cliops, "cli_api", return_value=self.api), mock.patch("builtins.print") as printed:
            self.assertEqual(cli.main(["ops", "machine", "list", "--config", str(self.config)]), 0)
            self.assertEqual(json.loads(printed.call_args[0][0])["schema"], "hyperloom/ui-ops-config-v1")
            self.assertEqual(cli.main(["ops", "machine", "remove", "halo-2", "--config", str(self.config)]), 1)
            self.assertEqual(cli.main(["ops", "wake", "nope", "--config", str(self.config)]), 1)
            self.assertEqual(cli.main(["ops", "nodes", "--config", str(self.config)]), 0)
            self.assertEqual(cli.main(["ops", "refresh", "nope", "--config", str(self.config), "--wait", "0"]), 1)
        self.assertIn("halo-2", [m["id"] for m in json.loads(self.config.read_text())["machines"]])


if __name__ == "__main__":
    unittest.main()
