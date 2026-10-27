"""Lead-review regressions. All hardware, CUDA and process boundaries are CPU fakes."""
from contextlib import ExitStack
from dataclasses import replace
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom import device_policy, gpu_counters, placement_supervision, procs, rtx
from hyperloom.recipes import grants, measurements, preflight, reader, service, solver
from hyperloom.web.api import API, APIError
from tests.test_recipes_grants import NOW, window


class LeadReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.uuid, self.pro = ("GPU-" + str(uuid.uuid4()) for _ in range(2))
        self.grant = dict(uuid=self.uuid, vram_cap_gib=4, approval="test", window=window(),
                          gate=[sys.executable, "fake-gate"])
        self.cluster = SimpleNamespace(pc={"cuda": {"pro_uuid": self.pro, "grants": [self.grant]}},
            machines={"local": SimpleNamespace(access="local", devices=[
                dict(uuid=self.uuid, pc_big_conflict=True)])})

    def test_m1_each_live_signal_refuses_probe_before_cuda(self):
        from tests.test_recipes_grants import GrantTests
        from tests.test_placement_supervision import policy
        helper = GrantTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.cluster.pc["cuda"]["pro_uuid"] = self.pro
        data = policy()
        data["pc_big_guard"] = dict(pro_uuid=self.pro, turn_ledger=str(self.root / "ledger.md"))
        ledger = self.root / "ledger.md"
        ledger.write_text("", encoding="utf-8")
        with service.configured(self.root / "config.json"), ExitStack() as stack:
            stack.enter_context(mock.patch.object(rtx, "device_lock_path", return_value=self.root / "pro.lock"))
            busy = stack.enter_context(mock.patch.object(rtx, "pro_busy", return_value=None))
            cuda = stack.enter_context(mock.patch.object(gpu_counters.C, "WinDLL", create=True,
                                                        side_effect=AssertionError("CUDA before refusal")))
            for signal in ("lock", "busy", "ledger", "ledger-markdown", "partial", "unreadable", "busy-unreadable"):
                with self.subTest(signal=signal), ExitStack() as case:
                    ledger.write_text("", encoding="utf-8")
                    self.cluster.pc["turn_ledger"] = str(ledger)
                    helper.cluster.pc["turn_ledger"] = str(ledger)
                    data["pc_big_guard"]["turn_ledger"] = str(ledger)
                    if signal == "lock":
                        lock = procs.InstanceLock(self.root / "pro.lock").acquire()
                        case.callback(lock.close)
                    elif signal == "busy":
                        busy.return_value = "compute processes [42]"
                    elif signal == "busy-unreadable":
                        busy.side_effect = rtx.ServiceError("counter unavailable")
                    else:
                        if signal == "ledger":
                            ledger.write_text("- 12:00 operator: PC-BIG start\n", encoding="utf-8")
                        elif signal == "ledger-markdown":
                            ledger.write_text("12:00 operator: **PC-BIG start**\n", encoding="utf-8")
                        elif signal == "partial":
                            ledger.write_text("- 12:00 operator: PC-BIG sta", encoding="utf-8")
                        configured = str(self.root / "missing" if signal == "unreadable" else ledger)
                        self.cluster.pc["turn_ledger"] = configured
                        helper.cluster.pc["turn_ledger"] = configured
                        data["pc_big_guard"]["turn_ledger"] = configured
                    with self.assertRaisesRegex(reader.RecipeError, "PC-BIG"):
                        measurements.acquire(self.cluster, "xfer", runtime=self.root, uuid=self.uuid,
                            xfer_sha256="a" * 64, store_id="test", clock=lambda: NOW,
                            inventory=lambda: [dict(uuid=self.uuid, driver="test", name="renamed GPU")],
                            gpu_runner=lambda *a: self.fail("CUDA work admitted"))
                    with self.assertRaisesRegex(reader.RecipeError, "PC-BIG"):
                        helper.check()
                    with self.assertRaisesRegex(rtx.ServiceError, "PC-BIG"):
                        placement_supervision.validate(data, now=NOW, starting=True)
                    busy.return_value = None
                    busy.side_effect = None
            cuda.assert_not_called()

    def test_m1_watchdog_stops_when_live_window_opens(self):
        from tests.test_placement_supervision import policy
        from tests.test_rtx import _binary_spec
        data = policy()
        data["pc_big_guard"] = dict(pro_uuid=self.pro, turn_ledger=str(self.root / "ledger.md"))
        killed = []
        observer = mock.Mock()
        observer.sample.return_value = dict.fromkeys((u.lower() for u in data["caps_gib"]), 0)
        with mock.patch.object(rtx, "device_lock_path", return_value=self.root / "pro.lock"), \
                mock.patch.object(rtx, "pro_busy", return_value=None) as busy:
            for signal in ("lock", "busy", "ledger", "unreadable"):
                with self.subTest(signal=signal), ExitStack() as case:
                    killed.clear()
                    busy.return_value = None
                    ledger = self.root / "ledger.md"
                    ledger.write_text("", encoding="utf-8")
                    dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data,
                        deadline=NOW + 600), 42, lambda: killed.append(1), lambda _: None, observer=observer,
                        clock=lambda: NOW, memory=lambda: dict(free_physical_gib=100, free_commit_gib=120),
                        interval=.01, memory_interval=.01).start()
                    case.callback(dog.stop)
                    if signal == "lock":
                        lock = procs.InstanceLock(self.root / "pro.lock").acquire()
                        case.callback(lock.close)
                    elif signal == "busy":
                        busy.return_value = "compute processes [42]"
                    elif signal == "ledger":
                        ledger.write_text("- 12:00 operator: PC-BIG start\n", encoding="utf-8")
                    else:
                        ledger.unlink()
                    dog._pc_big_next = 0
                    dog._thread.join(2)
                    self.assertEqual(killed, [1])
                    self.assertIn("PC-BIG", dog.reason)

    def test_m1_configured_ledger_is_required_without_a_telemetry_database(self):
        from hyperloom import pc_big
        ledger = self.root / "ledger.md"
        ledger.write_text("", encoding="utf-8")
        self.cluster.pc["turn_ledger"] = str(ledger)
        guard = pc_big.context(self.cluster, [dict(uuid=self.uuid)])
        with mock.patch.object(rtx, "pro_busy", return_value=None), \
                mock.patch.object(rtx, "device_lock_path", return_value=self.root / "pro.lock"):
            self.assertFalse(pc_big.active(guard))
            ledger.write_text("- 12:00 operator: PC-BIG start\n", encoding="utf-8")
            self.assertTrue(pc_big.active(guard))
            ledger.unlink()
            with self.assertRaisesRegex(rtx.ServiceError, "PC-BIG"):
                pc_big.active(guard)
            guard["turn_ledger"] = None
            with self.assertRaisesRegex(rtx.ServiceError, "PC-BIG"):
                pc_big.active(guard)
        self.assertFalse((self.root / "telemetry.sqlite3").exists())

    def test_m2_measurement_observer_follows_lock_and_gate(self):
        events = []
        lock, observer = mock.Mock(), mock.Mock()
        lock.acquire.side_effect = lambda: events.append("lock") or lock
        def gate(_):
            events.append("gate")
        def nvcuda(_):
            self.assertEqual(events[:2], ["lock", "gate"])
            raise gpu_counters.CounterError("test ends before real CUDA")
        def observe():
            return gpu_counters.C.WinDLL("nvcuda.dll")
        with mock.patch.object(gpu_counters.C, "WinDLL", side_effect=nvcuda, create=True), \
                mock.patch.object(rtx, "memory_status", return_value=dict(free_physical_gib=100, free_commit_gib=120)), \
                mock.patch.object(rtx, "pro_busy", return_value=None):
            with self.assertRaisesRegex(gpu_counters.CounterError, "test ends"):
                measurements._gpu(dict(uuid=self.uuid), self.grant, clock=lambda: NOW,
                    lock_factory=lambda _: lock, gate_runner=gate, observer_factory=observe)
        lock.close.assert_called_once()

    def test_m2_passive_luid_mapping_never_loads_cuda(self):
        import ctypes as C
        nvml = gpu_counters.Nvml.__new__(gpu_counters.Nvml)
        nvml.dll = mock.Mock()
        nvml.devices = lambda: [(self.uuid.lower(), None)]
        def handle(pci, out):
            out._obj.value = 1
            return 0
        def identity(handle, out, size):
            out.value = self.uuid.lower().encode()
            return 0
        nvml.dll.nvmlDeviceGetHandleByPciBusId_v2.side_effect = handle
        nvml.dll.nvmlDeviceGetUUID.side_effect = identity
        with mock.patch.object(gpu_counters, "dxgi_adapters", return_value=[(b"0000:01:00.0", (9, 10))], create=True), \
                mock.patch.object(C, "WinDLL", side_effect=AssertionError("preflight loaded CUDA"), create=True):
            self.assertEqual(nvml.luids(), {(9, 10): self.uuid.lower()})

    def test_m2_service_observer_follows_lock_and_gate(self):
        from tests.test_rtx import HelperTests, _binary_spec
        from tests.test_placement_supervision import policy
        helper = HelperTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        data, events = policy(), []
        keys = sorted(g["uuid"].lower() for g in data["grants"])
        def lock(uuid):
            item = mock.Mock()
            item.acquire.side_effect = lambda: events.append("lock") or item
            return item
        def observe():
            self.assertEqual(events, ["lock", "lock", "gate", "gate"])
            raise gpu_counters.CounterError("test ends before real CUDA")
        spec = replace(_binary_spec(), placement_supervision=data, pro_uuid=keys[0])
        with mock.patch.object(placement_supervision, "validate", return_value=(
                {g["uuid"].lower(): g for g in data["grants"]}, {})):
            rtx.service_run(helper.write(spec), lock_factory=lock, gate_runner=lambda _: events.append("gate"),
                verify=lambda _: {}, observer_factory=observe)
        self.assertIn("test ends", helper.state()["reason"])

    def test_preflight_lock_probe_never_acquires(self):
        from tests.test_placement_preflight import PreflightTests
        helper = PreflightTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        path = self.root / "device.lock"
        with mock.patch.object(rtx, "device_lock_path", return_value=path), \
                mock.patch.object(rtx, "device_lock", side_effect=AssertionError("preflight took a lock")):
            lock = procs.InstanceLock(path).acquire()
            try:
                with self.assertRaisesRegex(reader.RecipeError, "PL_E_BUSY"):
                    helper.check()
            finally:
                lock.close()
            helper.check()  # An existing, unlocked file is free too.

    def test_reviewed_helper_lock_probe_never_acquires(self):
        directory = self.root / "helper"
        directory.mkdir()
        path = directory / "helper.lock"
        record = dict(directory=str(directory), pid=os.getpid(), device=self.uuid, port=8096)
        reviewed = {"old": dict(token="reviewed-controller", prefill_service=record)}
        (directory / "state.json").write_text(json.dumps(dict(
            state="ready", pid=os.getpid(), device=[dict(uuid=self.uuid)])), encoding="utf-8")
        lock = procs.InstanceLock(path).acquire()
        self.addCleanup(lock.close)
        with mock.patch.object(procs.InstanceLock, "acquire", side_effect=AssertionError("preflight took a lock")):
            owners, ports, credit = preflight._reviewed_resources(reviewed, self.cluster)
            self.assertEqual(owners, {self.uuid.lower(): {os.getpid()}})
            self.assertEqual(ports, {8096: {os.getpid()}})
            self.assertEqual(credit, {})
            lock.close()
            before = path.stat()
            self.assertEqual(preflight._reviewed_resources(reviewed, self.cluster), ({}, {}, {}))
            self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
            path.unlink()
            self.assertEqual(preflight._reviewed_resources(reviewed, self.cluster), ({}, {}, {}))
            self.assertFalse(path.exists())
            for error in (procs.ProcessError("unreadable"), OSError("unreadable")):
                with self.subTest(error=error), mock.patch.object(procs, "lock_held", side_effect=error):
                    self.assertEqual(preflight._reviewed_resources(reviewed, self.cluster), ({}, {}, {}))

    def test_portal_busy_precedes_preflight(self):
        api = API(self.root / "config.json")
        api._active = "existing"
        with mock.patch.object(api, "_arguments", return_value={"profile": "test", "recipe": "test"}), \
                mock.patch.object(api, "_audit"), \
                mock.patch.object(preflight, "cli", side_effect=AssertionError("busy portal probed")):
            with self.assertRaises(APIError) as caught:
                api.mutate("up", {})
        self.assertEqual(caught.exception.status, 409)

    def test_portal_400_audits_full_reason_and_redacts_response(self):
        api = API(self.root / "config.json")
        full = "PL_E_BUSY " + str(self.root / "private.lock") + " processes [12345]"
        with mock.patch.object(api, "_arguments", return_value={"profile": "test", "recipe": "test"}), \
                mock.patch.object(api, "_recipe_up_argv", return_value=["up", "test", "--recipe", "test"]), \
                mock.patch.object(preflight, "cli", side_effect=reader.RecipeError(full)), \
                mock.patch.object(api, "_audit") as audit:
            with self.assertRaises(APIError) as caught:
                api.mutate("up", {})
        self.assertEqual(caught.exception.status, 400)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn("12345", str(caught.exception))
        self.assertEqual(audit.call_args.args[2]["error"], full)
        self.assertIsNone(api._active)

    def test_planner_actions_are_audited(self):
        from hyperloom.web.features import planner
        registered = []
        reg = SimpleNamespace(get=lambda *a: None, post=lambda name, fn, **kw: registered.append((name, kw)),
                              capability=lambda *a: None)
        planner.register(reg)
        self.assertTrue(all(kw.get("audited") for name, kw in registered))
        self.assertEqual(len(registered), 4)

    def test_solver_cache_evicts_oldest_by_count_and_bytes(self):
        from tests.test_placement_solver import SolverTests
        helper = SolverTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        directory = helper.root / "solver-plans"
        directory.mkdir()
        for index in range(3):
            path = directory / (str(index) + ".json")
            path.write_bytes(b"x" * 100)
            os.utime(path, (index + 1, index + 1))
        with mock.patch.object(solver, "MAX_PLAN_FILES", 3, create=True), \
                mock.patch.object(solver, "MAX_PLAN_BYTES", 250, create=True):
            with self.assertRaisesRegex(reader.RecipeError, "cache"):
                helper.solve()  # A single oversized result must be refused, without growing the cache.
        self.assertLessEqual(sum(p.stat().st_size for p in directory.glob("*.json")), 300)
        with mock.patch.object(solver, "MAX_PLAN_FILES", 3, create=True):
            helper.solve()
        self.assertFalse((directory / "0.json").exists())
        self.assertLessEqual(len(list(directory.glob("*.json"))), 3)

    def test_solver_cache_total_byte_limit_evicts_oldest(self):
        directory = self.root / "solver-plans"
        directory.mkdir()
        for index in range(3):
            path = directory / (str(index) + ".json")
            path.write_bytes(b"x" * 100)
            os.utime(path, (index + 1, index + 1))
        with mock.patch.object(solver.commands, "runtime_dir", return_value=self.root), \
                mock.patch.object(solver.service, "runtime_lock", return_value=ExitStack()), \
                mock.patch.object(solver, "MAX_PLAN_FILES", 100), mock.patch.object(solver, "MAX_PLAN_BYTES", 250):
            solver._save_plan("new", {"small": True})
        self.assertFalse((directory / "0.json").exists())
        self.assertTrue((directory / "1.json").exists())
        self.assertLessEqual(sum(p.stat().st_size for p in directory.glob("*.json")), 250)

    def test_device_policy_ambiguous_identity_is_excluded(self):
        card = dict(uuid=self.uuid, name="generic GPU")
        self.assertTrue(device_policy.resolve(card, [dict(uuid=self.uuid, excluded=False)] * 2)["excluded"])

    def test_policy_uses_configured_conflict_flag_with_arbitrary_names(self):
        self.cluster.machines["local"].devices[0]["pc_big_conflict"] = False
        self.cluster.pc["cuda"]["grants"].append(dict(self.grant, uuid=self.pro, window=window("PC-BIG")))
        self.assertIsNone(grants.pc_big_deadline(self.cluster, [dict(uuid=self.uuid, name="RTX 4090")], now=NOW))
        self.cluster.machines["local"].devices[0]["pc_big_conflict"] = True
        with service.configured(self.root / "config.json"), mock.patch.object(rtx, "pro_busy", return_value=None):
            self.assertEqual(grants.pc_big_deadline(self.cluster, [dict(uuid=self.uuid, name="generic GPU")], now=NOW), NOW)

    def test_docs_and_source_literals_have_no_local_identity(self):
        from tests.test_placement_privacy import findings
        self.assertEqual(findings(), {})

    def test_privacy_patterns_cover_docs_and_decoded_source_strings(self):
        from tests.test_placement_privacy import LOCAL_IDENTITY
        for value in ("C:" + "/Users/example/config", "D:" + "\\tmp\\scratch", "C:" + "/Python313/python.exe",
                      "claude" + "info-release", "claude" + "soporte-wdsrtx"):
            self.assertIsNotNone(LOCAL_IDENTITY.search(value))
        self.assertIsNone(LOCAL_IDENTITY.search("<runtime>/config; the release owner"))

    def test_test_directories_and_runner_are_portable(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / "tests").glob("test*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                        "TemporaryDirectory", "mkdtemp"):
                    self.assertFalse(any(k.arg == "dir" and "cwd" in ast.unparse(k.value) for k in node.keywords), path.name)
        ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        if "pytest" in ci:
            import tomllib
            config = tomllib.loads((root / "pyproject.toml").read_text())
            self.assertTrue(any("pytest" in item for item in config["project"]["optional-dependencies"]["test"]))
            self.assertEqual(config["tool"]["pytest"]["ini_options"]["testpaths"], ["tests"])
            self.assertIn("third_party", config["tool"]["pytest"]["ini_options"]["norecursedirs"])
        else:
            self.fail("fixture-based placement tests must run in CI")
