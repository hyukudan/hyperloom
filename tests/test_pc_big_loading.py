"""CPU-only loading, transient allocation and native counter regressions."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ctypes as C

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import gpu_counters, pc_big, placement_supervision, rtx
from hyperloom.recipes import preflight
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW
from tests.test_rtx import _binary_spec


def test_owned_bracket_uses_native_total_and_one_pdh_snapshot_per_side(monkeypatch):
    import uuid
    pro = "GPU-" + str(uuid.uuid4())
    key = pro.lower()
    own = 4 * rtx.GIB + 17
    dll = mock.Mock()
    dll.nvmlInit_v2.return_value = dll.nvmlShutdown.return_value = 0
    def handle(identity, out):
        assert identity == ("GPU-" + pro[4:].lower()).encode("ascii")
        out._obj.value = 0x123456789
        return 0
    def memory(handle, out):
        assert handle.value == 0x123456789
        out._obj.total = 96 * rtx.GIB
        out._obj.free = 96 * rtx.GIB - own - 800 * (1 << 20)
        out._obj.used = own + 800 * (1 << 20)
        return 0
    dll.nvmlDeviceGetHandleByUUID.side_effect = handle
    dll.nvmlDeviceGetMemoryInfo_v2.side_effect = memory
    monkeypatch.setattr(C, "CDLL", lambda _: dll)
    monkeypatch.setattr(C, "WinDLL", mock.Mock(side_effect=AssertionError("CUDA loaded")), raising=False)
    observer = gpu_counters.Observer.__new__(gpu_counters.Observer)
    observer.nvml = gpu_counters.Nvml()
    observer.luids = {(9, 10): key}
    observer.pdh = mock.Mock()
    observer.pdh.rows.return_value = [
        (f"pid_{pid}_luid_0x9_0xa_phys_0", used, True)
        for pid, used in ((1, rtx.GIB), (2, own - rtx.GIB))]
    monkeypatch.setattr(rtx, "device_used_mib", mock.Mock(side_effect=AssertionError("subprocess total")))
    try:
        assert not pc_big.owned_busy(pro, {1, 2}, observer)
        assert observer.pdh.rows.call_count == 2
        dll.nvmlDeviceGetMemoryInfo_v2.assert_called_once()
        assert dll.nvmlDeviceGetMemoryInfo_v2.argtypes[0] is C.c_void_p
        assert observer.device_used_bytes(pro.lower()) == own + 800 * (1 << 20)
        dll.nvmlDeviceGetHandleByUUID.assert_called_once()
        C.WinDLL.assert_not_called()
    finally:
        observer.close()
    dll.nvmlShutdown.assert_called_once()


@pytest.mark.parametrize("native", [False, True])
def test_total_falls_back_only_when_native_counter_is_unavailable(monkeypatch, native):
    class Observer:
        def sample(self, pid):
            return {"synthetic": 4 * rtx.GIB}
    if native:
        def unavailable(self, uuid):
            raise gpu_counters.CounterError("native total unavailable")
        Observer.device_used_bytes = unavailable
    total = mock.Mock(return_value=4 * 1024 + 800)
    monkeypatch.setattr(rtx, "device_used_mib", total)
    assert not pc_big.owned_busy("synthetic", {42}, Observer())
    total.assert_called_once_with("synthetic")


def loading_watchdog(tmp_path, monkeypatch, shape="growth", ready_timeout=120):
    data = policy()
    keys = sorted(u.lower() for u in data["caps_gib"])
    pro = keys[0]
    data["pc_big_guard"] = dict(pro_uuids=[pro], turn_ledger=None)
    counts = {}
    def sample(pid):
        n = counts.get(pid, 0)
        counts[pid] = n + 1
        # Three brackets then the independent cap sample; allocations can change between brackets.
        side = n % 7
        child = ([1, 4] if shape != "release" else [4, 1])[side % 2] if side < 6 else 1
        if shape == "growth":
            child += n // 7
        parent = [4, 1][n % 2] if shape == "mixed" else 0
        return dict.fromkeys(keys, 0) | {pro: (child if pid == 42 else parent) * rtx.GIB}
    observer = mock.Mock()
    observer.sample.side_effect = sample
    monkeypatch.setattr(rtx, "compute_apps", lambda: [(pro, 777, None)])
    def used(_):
        return (3 + (counts.get(42, 0) - 1) // 7) * 1024 + 800
    total = mock.Mock(side_effect=used if shape == "growth" else None,
                      return_value={"growth": 3 * 1024 + 800, "release": 5 * 1024 + 800,
                                    "mixed": 8 * 1024}[shape])
    monkeypatch.setattr(rtx, "device_used_mib", total)
    now = [NOW]
    lines = []
    dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data, deadline=NOW + 600,
        ready_timeout_s=ready_timeout), 42, lambda: None, lines.append, observer=observer,
        clock=lambda: now[0], pc_big_clock=lambda: now[0],
        memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    return dog, observer, now, lines, total, keys


def test_sustained_own_growth_does_not_kill_or_reset_existing_strikes(tmp_path, monkeypatch):
    dog, observer, now, lines, total, keys = loading_watchdog(tmp_path, monkeypatch)
    assert dog.check() is None  # No previous high bound: establish history with one conservative strike.
    assert dog.smi_failures == 1
    now[0] += 5
    for _ in range(4):
        assert dog.check() is None
        assert dog.smi_failures == 1
        assert "load in progress" in lines[-1]["smi"]
        now[0] += 5
    observer.sample.side_effect = lambda pid: dict.fromkeys(keys, 0)
    total.side_effect = None
    total.return_value = 0
    assert dog.check() is None
    assert dog.smi_failures == 0


@pytest.mark.parametrize("other", ["owned", "unowned", "cap"])
def test_load_allowance_does_not_hide_other_device_activity_or_caps(tmp_path, monkeypatch, other):
    dog, observer, _, _, total, keys = loading_watchdog(tmp_path, monkeypatch)
    if other == "cap":
        sample = observer.sample.side_effect
        def over_cap(pid):
            result = sample(pid)
            result[keys[1]] = 9 * rtx.GIB if pid == 42 else 0
            return result
        observer.sample.side_effect = over_cap
        assert "placement cap" in dog.check()
    else:
        uuid = keys[1] if other == "owned" else "synthetic"
        dog.policy["pc_big_guard"]["pro_uuids"].append(uuid)
        total.side_effect = lambda u: 4096 if u == uuid else 3 * 1024 + 800
        monkeypatch.setattr(rtx, "pro_busy", lambda _: "foreign activity")
        assert "window is active" in dog.check()


@pytest.mark.parametrize("stage", ["admission", "handover"])
def test_growth_allowance_is_never_used_for_admission(tmp_path, monkeypatch, stage):
    dog, observer, _, _, _, keys = loading_watchdog(tmp_path, monkeypatch)
    with pytest.raises(pc_big.CounterUnavailable):
        if stage == "admission":
            placement_supervision.validate(dog.policy, now=NOW, starting=True, owners=dog.owners, observer=observer)
        else:
            preflight._handover_busy(keys[0], dog.owners[keys[0]], observer)


@pytest.mark.parametrize("ready_timeout, grace", [(120, 120), (10, 10)])
def test_growth_that_never_settles_eventually_strikes(tmp_path, monkeypatch, ready_timeout, grace):
    dog, _, now, _, _, _ = loading_watchdog(tmp_path, monkeypatch, ready_timeout=ready_timeout)
    dog.policy["caps_gib"] = dict.fromkeys(dog.policy["caps_gib"], 90)
    dog.policy["caps_bytes"] = dict.fromkeys(dog.policy["caps_bytes"], 90 * rtx.GIB)
    for grant in dog.policy["grants"]:
        grant["vram_cap_gib"] = 90
    assert dog.check() is None
    for offset in range(5, grace, 5):
        now[0] = NOW + offset
        assert dog.check() is None
        assert dog.smi_failures == 1
    for strike in range(2, rtx.SMI_FAILURES_MAX + 1):
        now[0] = NOW + grace + (strike - 2) * 5
        assert (dog.check() is not None) == (strike == rtx.SMI_FAILURES_MAX)
        assert dog.smi_failures == strike


@pytest.mark.parametrize("shape", ["release", "mixed"])
def test_release_and_mixed_ambiguity_receive_no_load_allowance(tmp_path, monkeypatch, shape):
    dog, _, now, _, _, _ = loading_watchdog(tmp_path, monkeypatch, shape)
    for strike in range(1, rtx.SMI_FAILURES_MAX + 1):
        assert (dog.check() is not None) == (strike == rtx.SMI_FAILURES_MAX)
        assert dog.smi_failures == strike
        now[0] += 5


@pytest.mark.parametrize("stage", ["watchdog", "admission", "handover"])
@pytest.mark.parametrize("persistent", [False, True])
def test_own_spike_only_in_total_needs_two_busy_watchdog_brackets(tmp_path, monkeypatch, stage, persistent):
    dog, observer, _, _, total, keys = loading_watchdog(tmp_path, monkeypatch)
    pro = keys[0]
    observer.sample.side_effect = lambda pid: dict.fromkeys(keys, 0) | {pro: 4 * rtx.GIB if pid == 42 else 0}
    total.side_effect = [7 * 1024 + 800, (7 if persistent else 4) * 1024 + 800]
    if stage == "watchdog":
        assert (dog.check() is not None) == persistent
        assert total.call_count == 2
        assert dog.smi_failures == 0
    elif stage == "admission":
        with pytest.raises(rtx.ServiceError, match="window is active"):
            placement_supervision.validate(dog.policy, now=NOW, starting=True, owners=dog.owners, observer=observer)
        assert total.call_count == 1
    else:
        assert preflight._handover_busy(pro, dog.owners[pro], observer)
        assert total.call_count == 1


def test_owned_busy_normalizes_uuid_like_handover_filter(monkeypatch):
    observer = SimpleNamespace(sample=lambda pid: {"synthetic": 4 * rtx.GIB})
    monkeypatch.setattr(rtx, "compute_apps", lambda: [])
    monkeypatch.setattr(rtx, "device_used_mib", lambda _: 4 * 1024 + 800)
    assert preflight._handover_busy("  SYNTHETIC  ", {42}, observer) is None


def test_live_qualification_documents_bracket_assumption_and_total_accounting():
    root = Path(__file__).resolve().parents[1]
    supervision = (root / "docs/placement-supervision.md").read_text(encoding="utf-8")
    resolution = (root / "docs/placement-review-resolution.md").read_text(encoding="utf-8")
    for document in (supervision, resolution):
        assert "monotonic per PID within each bracket" in document
        assert "total below attribution" in document
        assert "under load" in document
