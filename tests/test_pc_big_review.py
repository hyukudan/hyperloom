"""Review regressions using CPU counters, fake clocks and synchronous scheduling."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ctypes as C
import json
import sys
import uuid

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import gpu_counters, pc_big, placement_supervision, rtx
from hyperloom.recipes import launch, measurements, preflight, runtime, session
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW, window
from tests.test_rtx import _binary_spec


@pytest.mark.parametrize("ambiguous", [False, True])
def test_measurement_uses_real_watchdog_with_zero_load_grace(monkeypatch, tmp_path, ambiguous):
    identity = "GPU-" + str(uuid.uuid4())
    key = identity.lower()
    now, loading, sides, dogs = [NOW], [False], [0], []
    class Observer:
        def __init__(self):
            self.closed = 0
        def sample(self, pid):
            if not loading[0] or pid != 42:
                return {key: 0}
            side = sides[0] % 7
            sides[0] += 1
            return {key: (4 if side < 6 and side % 2 else 1) * rtx.GIB}
        def close(self):
            self.closed += 1
    observer = Observer()
    child = mock.Mock(pid=42, returncode=0)
    child.poll.return_value = 0
    samples = [{"h2d_mbps": n + 1} for n in range(9)]
    def communicate(*args, **kwargs):
        if args:
            now[0] += 5
            loading[0] = ambiguous
        return json.dumps(samples), ""
    child.communicate.side_effect = communicate
    memory = lambda: dict(free_physical_gib=100, free_commit_gib=120)
    def watchdog(*args, **kwargs):
        dog = placement_supervision.Watchdog(*args, **kwargs, memory=memory, pc_big_clock=lambda: now[0])
        # Execute the actual inherited fail-closed loop once, without starting a thread or waiting.
        monkeypatch.setattr(dog._thread, "start", dog._run)
        monkeypatch.setattr(dog._stop, "wait", lambda _: dog._stop.set())
        dogs.append(dog)
        return dog
    monkeypatch.setattr(rtx, "memory_status", memory)
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    monkeypatch.setattr(rtx, "compute_apps", lambda: [(identity, 777, None)])
    monkeypatch.setattr(rtx, "device_used_mib", lambda _: 3 * 1024 + 800 if loading[0] else 0)
    lock = mock.Mock()
    lock.acquire.return_value = lock
    grant = dict(uuid=identity, vram_cap_gib=4, approval="test", window=window(),
                 gate=[sys.executable, "fake-gate"])
    row = dict(uuid=identity, _pc_big_guard=dict(pro_uuids=[identity],
                                               preferences=str(tmp_path / "preferences.sqlite3")))
    result = measurements._gpu(row, grant, clock=lambda: now[0], lock_factory=lambda _: lock,
        gate_runner=lambda _: None, observer_factory=lambda: observer, watchdog_factory=watchdog,
        popen=lambda *a, **kw: child, job_factory=mock.Mock)
    assert result["values"] == {"h2d_mbps": 5}
    assert dogs[0].spec.ready_timeout_s == 0
    assert dogs[0].reason is None
    assert dogs[0].smi_failures == int(ambiguous)
    child.kill.assert_not_called()
    assert not dogs[0]._thread.is_alive()
    assert observer.closed == 1
    lock.close.assert_called_once()


@pytest.mark.parametrize("version", ["v1-only", "v2", "unsupported", "version-mismatch"])
def test_native_total_matches_allocated_memory_or_falls_back(monkeypatch, version):
    identity = "GPU-" + str(uuid.uuid4())
    exports = ["nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetHandleByUUID", "nvmlDeviceGetMemoryInfo"]
    if version != "v1-only":
        exports.append("nvmlDeviceGetMemoryInfo_v2")
    dll = mock.Mock(spec_set=exports)
    dll.nvmlInit_v2.return_value = dll.nvmlShutdown.return_value = 0
    def lookup(value, out):
        assert value == ("GPU-" + identity[4:].lower()).encode("ascii")
        out._obj.value = 0x123456789
        return 0
    dll.nvmlDeviceGetHandleByUUID.side_effect = lookup
    used, reserved = 4 * rtx.GIB + 800 * (1 << 20), 3 * rtx.GIB
    def v1(handle, out):
        out._obj.used = used + reserved
        return 0
    dll.nvmlDeviceGetMemoryInfo.side_effect = v1
    def v2(handle, out):
        assert handle.value == 0x123456789
        assert out._obj.version == C.sizeof(out._obj) | (2 << 24)
        assert type(out._obj).used.offset == 32
        out._obj.total, out._obj.reserved, out._obj.used = 96 * rtx.GIB, reserved, used
        return {"unsupported": 3, "version-mismatch": 25}.get(version, 0)
    if version != "v1-only":
        dll.nvmlDeviceGetMemoryInfo_v2.side_effect = v2
    monkeypatch.setattr(C, "CDLL", lambda _: dll)
    fallback = mock.Mock(return_value=used // (1 << 20))
    monkeypatch.setattr(rtx, "device_used_mib", fallback)
    nvml = gpu_counters.Nvml()
    try:
        assert pc_big._device_total(identity, nvml) == used
        if version == "v2":
            signature = dll.nvmlDeviceGetMemoryInfo_v2.argtypes
            assert nvml.device_used_bytes("  " + identity.upper() + "  ") == used
            assert nvml.device_used_bytes(identity.lower()) == used
            assert dll.nvmlDeviceGetMemoryInfo_v2.argtypes is signature
            dll.nvmlDeviceGetHandleByUUID.assert_called_once()
            fallback.assert_not_called()
        else:
            fallback.assert_called_once()
        dll.nvmlDeviceGetMemoryInfo.assert_not_called()
    finally:
        nvml.close()
    dll.nvmlShutdown.assert_called_once()


def scripted_watchdog(tmp_path, monkeypatch, *, ready_timeout=120):
    data = policy()
    keys = sorted(u.lower() for u in data["caps_gib"])
    pro = keys[0]
    for grant in data["grants"]:
        grant["vram_cap_gib"] = 90
    data["caps_gib"] = dict.fromkeys(keys, 90)
    data["caps_bytes"] = dict.fromkeys(keys, 90 * rtx.GIB)
    data["pc_big_guard"] = dict(pro_uuids=[pro], preferences=str(tmp_path / "preferences.sqlite3"))
    now, hi, low, foreign = [0.0], [0], [0], [0]
    class Observer:
        def sample_many(self, pids):
            value = hi[0] if self.after else low[0]
            self.after = not self.after
            return {pid: dict.fromkeys(keys, 0) | {pro: value if pid == 42 else 0} for pid in pids}
        def sample(self, pid):
            return dict.fromkeys(keys, 0) | {pro: hi[0] if pid == 42 else 0}
        after = False
        def close(self):
            pass
    observer = Observer()
    total = mock.Mock(side_effect=lambda _: (low[0] + (hi[0] - low[0]) // 2 + foreign[0] +
                                             800 * (1 << 20)) // (1 << 20))
    monkeypatch.setattr(rtx, "compute_apps", lambda: [(pro, 777, None)])
    monkeypatch.setattr(rtx, "device_used_mib", total)
    lines = []
    dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data,
        ready_timeout_s=ready_timeout, deadline=NOW + 600), 42, lambda: None, lines.append,
        observer=observer, clock=lambda: NOW + now[0], pc_big_clock=lambda: now[0],
        memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    return dog, now, hi, low, foreign, total, keys, lines


@pytest.mark.parametrize("case", ["stall", "sub-gib", "hi-growth", "ready", "timeout", "foreign", "large-foreign"])
def test_growth_ambiguity_requires_hi_progress_and_pending_readiness(tmp_path, monkeypatch, case):
    timeout = 120 if case == "hi-growth" else 20
    dog, now, hi, low, foreign, _, _, lines = scripted_watchdog(tmp_path, monkeypatch, ready_timeout=timeout)
    # Establish a successful full-check baseline; only the bracket hi changes in subsequent checks.
    assert dog.check() is None
    hi[0] = 4 * rtx.GIB
    low[0] = rtx.GIB
    if case in ("foreign", "large-foreign"):
        low[0], hi[0] = 2 * rtx.GIB, int(4.6 * rtx.GIB)
        foreign[0] = int((2.4 if case == "foreign" else 3) * rtx.GIB)
    if case == "ready":
        dog.mark_ready()
    if case == "large-foreign":
        now[0] = 5
        assert "window is active" in dog.check()
        return
    dog.smi_failures = 2  # Qualifying progress must preserve previous strikes.
    now[0] = 5
    reason = dog.check()
    if case == "ready":
        assert "counters failed persistently" in reason
        assert dog.smi_failures == 3
        return
    assert reason is None and dog.smi_failures == 2
    for step in range(2, 15 if case == "hi-growth" else 6):
        now[0] = step * 5
        if case == "sub-gib":
            hi[0] += rtx.GIB - 1
        elif case not in ("stall",):
            hi[0] += rtx.GIB
            if case in ("foreign",):
                low[0] += rtx.GIB
        reason = dog.check()
        should_strike = case in ("stall", "sub-gib") or step * 5 >= timeout
        if should_strike:
            assert "counters failed persistently" in reason
            assert dog.smi_failures == 3
            break
        assert reason is None and dog.smi_failures == 2
    assert "load in progress" in lines[-1]["smi"]


def test_fallback_load_of_90_gib_at_1_2_gib_per_second_survives(tmp_path, monkeypatch):
    dog, now, _, _, _, total, keys, _ = scripted_watchdog(tmp_path, monkeypatch)
    pro = keys[0]
    def own():
        return int(min(90, now[0] * 1.2) * rtx.GIB)
    class Observer:
        def sample_many(self, pids):
            now[0] += .05
            return {pid: dict.fromkeys(keys, 0) | {pro: own() if pid == 42 else 0} for pid in pids}
        def sample(self, pid):
            return dict.fromkeys(keys, 0) | {pro: own() if pid == 42 else 0}
    dog.observer = Observer()  # No native total: the real nvidia-smi fallback widens the brackets.
    def smi(_):
        now[0] += 1.05
        return (own() + 800 * (1 << 20)) // (1 << 20)
    total.side_effect = smi
    for check in range(18):
        now[0] = check * 5
        assert dog.check() is None
        assert dog.smi_failures <= 1
    assert own() == 90 * rtx.GIB
    assert dog.smi_failures == 0
    assert total.call_count > 18


@pytest.mark.parametrize("owned", [False, True])
def test_prepare_reuses_one_reviewed_resource_snapshot(monkeypatch, tmp_path, owned):
    owners = {"synthetic": {42}} if owned else {}
    reviewed = mock.Mock(side_effect=[(owners, {}, {}), ({"changed": {43}}, {}, {})])
    monkeypatch.setattr(preflight, "_reviewed_resources", reviewed)
    monkeypatch.setattr(preflight, "includes", lambda _: True)
    observer = mock.Mock()
    monkeypatch.setattr(gpu_counters, "Observer", lambda: observer)
    monkeypatch.setattr(launch, "_backend", lambda _: (None, False))
    evaluation = SimpleNamespace(problems=[], refusal=None, decision={"placement": True},
                                 effective_sha256="a" * 64, effective=SimpleNamespace(untuned=False))
    monkeypatch.setattr(session, "evaluate", lambda *a, **kw: evaluation)
    request = object()
    monkeypatch.setattr(runtime, "request", lambda _: request)
    render = mock.Mock(return_value=SimpleNamespace(placement={}))
    monkeypatch.setattr(launch, "render_placement", render)
    launch.prepare(None, "test", launch.Options("fake-recipe"), router="local", vram_cap_gib=None,
                   state_dir=tmp_path, source=lambda: None, reviewed={"old": {}})
    reviewed.assert_called_once()
    assert render.call_args.kwargs["owners"] is owners
    assert render.call_args.kwargs["observer"] is (observer if owned else None)
    assert observer.close.call_count == int(owned)


def test_loading_documentation_covers_retained_strikes_and_delayed_foreign_detection():
    document = (Path(__file__).resolve().parents[1] / "docs/placement-supervision.md").read_text(encoding="utf-8")
    assert "first later strike can stop" in document
    assert "idle threshold plus own growth within a bracket" in document
    assert "counters failed persistently" in document
    assert "NVML v2 used == nvidia-smi memory.used at idle and under load" in document


def test_helper_readiness_ends_load_allowance_without_real_threads(tmp_path, monkeypatch):
    from tests.test_rtx import FakeChild
    dog, now, hi, low, _, _, keys, _ = scripted_watchdog(tmp_path, monkeypatch)
    data = dog.policy
    observer = dog.observer
    spec = replace(_binary_spec(), placement_supervision=data, pro_uuid=keys[0], deadline=NOW + 600)
    dogs = []
    class ScheduledThread:
        def __init__(self, *, target, daemon):
            self.dog, self.running = target.__self__, False
            dogs.append(self.dog)
        def start(self):
            self.running = True
            self.dog.clock = lambda: NOW + now[0]
            self.dog.pc_big_clock = lambda: now[0]
            assert self.dog.check() is None
        def is_alive(self):
            return self.running
        def join(self, timeout=None):
            self.running = False
    class Child(FakeChild):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.process.pid = 42
    monkeypatch.setattr(rtx.threading, "Thread", ScheduledThread)
    monkeypatch.setattr(rtx.time, "time", lambda: NOW + now[0])
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    def tick(_):
        assert json.loads((tmp_path / "state.json").read_text())["state"] == "ready"
        now[0], hi[0], low[0] = 5, 4 * rtx.GIB, rtx.GIB
        assert dogs[0].check() is None
        assert dogs[0].smi_failures == 1
        (tmp_path / "stop").touch()
    monkeypatch.setattr(rtx.time, "sleep", tick)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(dict(spec=spec.__dict__)), encoding="utf-8")
    lock = mock.Mock()
    lock.acquire.return_value = lock
    rtx.service_run(path, child_factory=Child, observer_factory=lambda: observer,
        verify=lambda identity: dict(uuid=identity), gate_runner=lambda _: None,
        lock_factory=lambda _: lock, health=lambda _: dict(status="ok"),
        memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["reason"] == "stopped"
    assert "cleanup_errors" not in state
    assert len(dogs) == 1 and not dogs[0]._thread.is_alive()

