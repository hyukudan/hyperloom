"""CPU-only supervision tests; identities and executable paths are created at runtime."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import sys
import ctypes as C
import time
import unittest
from unittest import mock
import uuid

from hyperloom import gpu_counters, placement_supervision as supervision, rtx
from tests.test_recipes_grants import NOW, window
from tests import test_rtx
from tests.test_rtx import _binary_spec


def policy():
    ids = sorted("GPU-" + str(uuid.uuid4()) for _ in range(2))
    return {"grants": [{"uuid": u, "vram_cap_gib": 12, "approval": "test approval", "window": window(),
                        "gate": [sys.executable, "operator-gate.py"]} for u in ids],
            "caps_gib": {u: 8 for u in ids}, "host_pinned_gib": 60, "host_pageable_gib": 2,
            "host_grant": {"host_gib": 74, "approval": "test approval", "window": window()},
            "free_commit_floor_gib": 4}


class PlacementSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.keys = [u.lower() for u in self.policy["caps_gib"]]
        self.spec = replace(_binary_spec(), placement_supervision=self.policy, deadline=NOW + 600)
        self.observer = mock.Mock()
        self.observer.sample.return_value = dict.fromkeys(self.keys, 8 * rtx.GIB)
        self.memory = {"free_physical_gib": 90, "free_commit_gib": 100}

    def dog(self, **kwargs):
        return supervision.Watchdog(self.spec, 42, lambda: None, lambda _: None, observer=self.observer,
                                    memory=lambda: self.memory, clock=lambda: NOW, **kwargs)

    def test_each_cap_exact_boundary_and_ungranted_activity(self):
        self.assertIsNone(self.dog().check())
        for key in self.keys:
            self.observer.sample.return_value[key] += 1
            self.assertIn("exceeds", self.dog().check())
            self.observer.sample.return_value[key] -= 1
        other = str(uuid.uuid4())
        self.observer.sample.return_value[other] = 0
        self.assertIsNone(self.dog().check())
        self.observer.sample.return_value[other] = 1
        self.assertIn("ungranted", self.dog().check())

    def test_missing_na_and_failed_attribution_never_use_device_totals(self):
        for sample in ({}, {self.keys[0]: None, self.keys[1]: 0}):
            self.observer.sample.return_value = sample
            self.assertIsNotNone(self.dog(used=mock.Mock(side_effect=AssertionError("device total"))).check())
        self.observer.sample.side_effect = gpu_counters.CounterError("N/A")
        self.assertEqual(self.dog().check(), "N/A")

    def test_deadlines_host_pressure_and_grant_shapes(self):
        original = deepcopy(self.policy)
        for field in ("grants", "host_grant"):
            grant = self.policy[field][1] if field == "grants" else self.policy[field]
            grant["window"]["expires_at"] = "2029-12-31T23:59:59Z"
            self.assertIn("expired", self.dog().check())
            self.policy.clear()
            self.policy.update(deepcopy(original))
        self.memory["free_commit_gib"] = 1
        self.assertIn("commit", self.dog().check())
        self.memory.update(free_commit_gib=100, free_physical_gib=1)
        dog = self.dog()
        for _ in range(rtx.PHYSICAL_STRIKES - 1):
            self.assertIsNone(dog.check())
        self.assertIn("available RAM", dog.check())
        self.memory.update(free_physical_gib=90)
        self.policy["caps_gib"].pop(next(iter(self.policy["caps_gib"])))
        with self.assertRaisesRegex(rtx.ServiceError, "one cap"):
            supervision.validate(self.policy, now=NOW)

    def test_prestart_host_charge_commit_and_memavailable(self):
        supervision.validate(self.policy, now=NOW, starting=True, memory=self.memory)
        for changes, message in (({"free_physical_gib": 70}, "82 GB"),
                                 ({"free_commit_gib": 65}, "commit")):
            with self.assertRaisesRegex(rtx.ServiceError, message):
                supervision.validate(self.policy, now=NOW, starting=True, memory={**self.memory, **changes})
        self.policy["host_pageable_gib"] = 15
        with self.assertRaisesRegex(rtx.ServiceError, "host grant"):
            supervision.validate(self.policy, now=NOW)

    def test_sorted_locks_rollback_and_all_gates(self):
        events = []
        def lock(key):
            item = mock.Mock()
            item.acquire.side_effect = lambda: events.append(("lock", key)) or item
            item.close.side_effect = lambda: events.append(("close", key))
            return item
        def gate(argv):
            events.append(("gate", argv))
            return "refused" if len([e for e in events if e[0] == "gate"]) == 2 else None
        with self.assertRaisesRegex(rtx.ServiceError, "refused"), ExitStack() as stack:
            supervision.acquire(self.policy, stack, lock_factory=lock, gate_runner=gate,
                                verify=lambda u: u, log=lambda _: None, now=NOW)
        self.assertEqual([e[1] for e in events if e[0] == "lock"], sorted(self.keys))
        self.assertEqual([e[1] for e in events if e[0] == "close"], sorted(self.keys, reverse=True))
        self.assertEqual(len([e for e in events if e[0] == "gate"]), 2)
        events.clear()
        def failing(key):
            if events:
                raise rtx.ServiceError("busy")
            return lock(key)
        with self.assertRaisesRegex(rtx.ServiceError, "busy"), ExitStack() as stack:
            supervision.acquire(self.policy, stack, lock_factory=failing, gate_runner=gate,
                                verify=lambda u: u, log=lambda _: None, now=NOW)
        self.assertEqual([e[0] for e in events], ["lock", "close"])

    def test_attribution_failure_kills_fake_process_once(self):
        kills = []
        self.observer.sample.side_effect = [dict.fromkeys(self.keys, 0), gpu_counters.CounterError("N/A")]
        dog = supervision.Watchdog(self.spec, 42, lambda: kills.append(1), lambda _: None,
                                   observer=self.observer, memory=lambda: self.memory, clock=lambda: NOW,
                                   interval=0.01, memory_interval=0.01).start()
        dog._thread.join(2)
        dog.stop()
        self.assertEqual(kills, [1])
        self.observer.close.assert_called_once()

    def test_placement_visibility_uses_only_attributable_counters(self):
        service = {"pid": 42, "placement_supervision": self.policy}
        forbidden = mock.Mock(side_effect=AssertionError("legacy GPU queries"))
        seen = rtx.visibility(service, forbidden, forbidden, observer=self.observer)
        self.assertTrue(seen["only_granted"])
        self.observer.sample.return_value[str(uuid.uuid4())] = 1
        self.assertFalse(rtx.visibility(service, forbidden, forbidden, observer=self.observer)["only_granted"])
        self.observer.close.assert_not_called()


class PdhTests(unittest.TestCase):
    def test_luid_attribution_and_fail_closed_instances(self):
        ident = str(uuid.uuid4())
        instance = "pid_42_luid_0x00000001_0x00000002_phys_0"
        mapping = {(1, 2): ident}
        self.assertEqual(gpu_counters.pdh_sample([(instance, 123, True),
                                                (instance.replace("42", "43"), 9999, True)], mapping, 42),
                         {ident: 123})
        for rows, luids in (([(instance, 123, False)], mapping), ([(instance, 123, True)], {}),
                            ([(instance, None, True)], mapping), ([(instance, 1, True)] * 2, mapping)):
            with self.assertRaises(gpu_counters.CounterError):
                gpu_counters.pdh_sample(rows, luids, 42)


class NvmlTests(unittest.TestCase):
    def test_native_process_abi_attribution_na_and_no_double_count(self):
        ids = ["GPU-" + str(uuid.uuid4()) for _ in range(2)]
        dll = mock.Mock()
        dll.nvmlInit_v2.return_value = dll.nvmlShutdown.return_value = 0
        def count(out):
            out._obj.value = 2
            return 0
        def handle(index, out):
            out._obj.value = index + 1
            return 0
        def identity(handle, out, length):
            out.value = ids[handle.value - 1].encode("ascii")
            return 0
        used = [123, 456]
        partitions = [False]
        def processes(handle, count, rows):
            count._obj.value = 2 if partitions[0] else 1
            if rows is None:
                return 7
            rows[0].pid, rows[0].used = 42, used[handle.value - 1]
            if partitions[0]:
                rows[1].pid, rows[1].used, rows[1].gpu_instance = 42, 7, 1
            return 0
        def luid(handle, out, mask):
            C.memmove(out, handle.value.to_bytes(4, "little") + (9).to_bytes(4, "little"), 8)
            mask._obj.value = 1
            return 0
        dll.nvmlDeviceGetCount_v2.side_effect = count
        dll.nvmlDeviceGetHandleByIndex_v2.side_effect = handle
        dll.nvmlDeviceGetUUID.side_effect = identity
        dll.nvmlDeviceGetLuid.side_effect = luid
        dll.nvmlDeviceGetComputeRunningProcesses_v2.side_effect = processes
        dll.nvmlDeviceGetGraphicsRunningProcesses_v2.side_effect = processes
        with mock.patch.object(gpu_counters.C, "CDLL", return_value=dll):
            nvml = gpu_counters.Nvml()
            self.assertEqual(nvml.sample(42), {ids[0].lower(): 123, ids[1].lower(): 456})
            self.assertEqual(nvml.sample(99), dict.fromkeys((u.lower() for u in ids), 0))
            self.assertEqual(nvml.luids(), {(9, 1): ids[0].lower(), (9, 2): ids[1].lower()})
            partitions[0] = True
            self.assertEqual(nvml.sample(42), {ids[0].lower(): 130, ids[1].lower(): 463})
            used[1] = (1 << 64) - 1
            with self.assertRaisesRegex(gpu_counters.CounterError, "N/A"):
                nvml.sample(42)
            nvml.close()
        dll.nvmlShutdown.assert_called_once()


class PlacementHelperTests(unittest.TestCase):
    def test_fake_child_is_supervised_on_every_granted_device(self):
        from tests.test_rtx import FakeChild
        helper = test_rtx.HelperTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        data = policy()
        active = {"starts_at": test_rtx._utc(-30), "expires_at": test_rtx._utc(900), "kind": "device"}
        for grant in data["grants"]:
            grant["window"] = dict(active)
        data["host_grant"]["window"] = dict(active)
        spec = replace(_binary_spec(), placement_supervision=data, pro_uuid=data["grants"][0]["uuid"],
                       deadline=time.time() + 600)
        observer = mock.Mock()
        values = {u.lower(): 0 for u in data["caps_gib"]}
        observer.sample.side_effect = lambda pid: dict(values)
        gates, locks, closes = [], [], []
        def lock(key):
            locks.append(key)
            item = mock.Mock()
            item.acquire.return_value = item
            item.close.side_effect = lambda: closes.append(key)
            return item
        thread = helper.run_helper(helper.write(spec), lock_factory=lock,
            gate_runner=lambda argv: gates.append(argv), observer_factory=lambda: observer,
            memory=lambda: {"free_physical_gib": 90, "free_commit_gib": 100})
        try:
            self.assertEqual(helper.wait_state("ready")["pid"], 4242)
            self.assertEqual(len(gates), 2)
            self.assertEqual(locks, sorted(values))
            # The assist, rather than the legacy pro_uuid, crosses its cap by exactly one byte.
            values[data["grants"][1]["uuid"].lower()] = 8 * rtx.GIB + 1
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIn("placement cap", helper.state()["reason"])
            FakeChild.instances[0].process.kill.assert_called_once()
            self.assertTrue(FakeChild.instances[0].stopped)
            self.assertEqual(closes, list(reversed(locks)))
            observer.close.assert_called_once()
        finally:
            (helper.directory / "stop").touch()
            thread.join(5)

    def test_gate_failure_starts_no_child_and_releases_all_locks(self):
        from tests.test_rtx import FakeChild
        helper = test_rtx.HelperTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        data = policy()
        closes, gates = [], []
        def lock(key):
            result = mock.Mock()
            result.acquire.return_value = result
            result.close.side_effect = lambda: closes.append(key)
            return result
        def gate(argv):
            gates.append(argv)
            return "operator refused" if len(gates) == 2 else None
        spec = replace(_binary_spec(), placement_supervision=data, pro_uuid=data["grants"][0]["uuid"])
        with mock.patch("hyperloom.placement_supervision.validate", return_value=(
                {g["uuid"].lower(): g for g in data["grants"]}, {u.lower(): v for u, v in data["caps_gib"].items()})):
            observer = mock.Mock()
            observer.sample.return_value = {u.lower(): 0 for u in data["caps_gib"]}
            helper.run_helper(helper.write(spec), lock_factory=lock, gate_runner=gate,
                              observer_factory=lambda: observer).join(5)
        self.assertEqual(FakeChild.instances, [])
        self.assertEqual(len(gates), 2)
        self.assertEqual(len(closes), 2)
        self.assertIn("operator refused", helper.state()["reason"])
