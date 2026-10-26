"""CPU-only residual brackets, including changing attribution and watchdog retries."""
from dataclasses import replace
import os
from unittest import mock

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import pc_big, placement_supervision, rtx
from hyperloom.recipes import preflight
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW
from tests.test_rtx import _binary_spec


@pytest.mark.parametrize("stage", ["pre-start", "handover", "watchdog"])
@pytest.mark.parametrize("case", ["growth", "release-foreign", "stable-clear", "stable-foreign", "ambiguous",
                                 "crossing-pids", "negative-total", "fractional-total", "boolean-total",
                                 "below-attributed", "rounded-total", "missing-before", "missing-after", "cap"])
def test_residual_brackets_bound_each_owned_pid(tmp_path, monkeypatch, stage, case):
    data = policy()
    keys = sorted(u.lower() for u in data["caps_gib"])
    pro = keys[0]
    data["pc_big_guard"] = dict(pro_uuids=[pro], preferences=str(tmp_path / "preferences.sqlite3"))
    owners = {pro: {os.getpid(), 42}}
    child = {"growth": [1, 4, 4, 4], "release-foreign": [4, 1, 1, 1], "stable-foreign": [1],
             "ambiguous": [4, 1], "crossing-pids": [1, 4], "cap": [9]}.get(case, [4])
    counts = {}
    def sample(pid):
        index = counts.get(pid, 0)
        counts[pid] = index + 1
        if pid == 42 and ((case == "missing-before" and index == 0) or
                          (case == "missing-after" and index == 1)):
            return {keys[1]: 0}
        if pid == os.getpid():
            value = [4, 1][index % 2] if case == "crossing-pids" else 0
        else:
            if case == "ambiguous" and stage == "watchdog":
                # Each check takes six bracket samples and one separate cap sample. Keep every bracket release-shaped.
                index %= 7
            value = child[index % len(child)] if case in ("ambiguous", "crossing-pids") else child[min(index, len(child) - 1)]
        result = dict.fromkeys(keys, 0)
        result[pro] = value * rtx.GIB + (1 if case == "rounded-total" and pid == 42 else 0)
        return result
    observer = mock.Mock()
    observer.sample.side_effect = sample
    total = {"negative-total": -1, "fractional-total": 4096.5, "boolean-total": True,
             "below-attributed": 1024, "rounded-total": 4096, "crossing-pids": 8192,
             "cap": 10240, "stable-clear": 6144, "stable-foreign": 4096}.get(case, 5120)
    query = mock.Mock(return_value=total)
    monkeypatch.setattr(rtx, "device_used_mib", query)
    monkeypatch.setattr(rtx, "compute_apps", lambda: [(pro, 777, None)])
    monkeypatch.setattr(rtx, "pro_busy", mock.Mock(side_effect=AssertionError("owned PRO idle query")))
    unavailable = case in ("ambiguous", "crossing-pids", "negative-total", "fractional-total",
                           "boolean-total", "below-attributed")
    foreign = case in ("release-foreign", "stable-foreign")
    missing = case.startswith("missing-")
    if stage == "handover":
        if unavailable or missing:
            error = pc_big.CounterUnavailable if unavailable else rtx.ServiceError
            with pytest.raises(error):
                preflight._handover_busy(pro, owners[pro], observer)
        else:
            assert bool(preflight._handover_busy(pro, owners[pro], observer)) == foreign
    elif stage == "pre-start":
        if unavailable or foreign or missing:
            error = pc_big.CounterUnavailable if unavailable else rtx.ServiceError
            with pytest.raises(error):
                placement_supervision.validate(data, now=NOW, starting=True, owners=owners, observer=observer)
        else:
            placement_supervision.validate(data, now=NOW, starting=True, owners=owners, observer=observer)
    else:
        now = [NOW]
        dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data, deadline=NOW + 600),
            42, lambda: None, lambda _: None, observer=observer, clock=lambda: now[0],
            pc_big_clock=lambda: now[0], memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
        if unavailable:
            for strike in range(3):
                assert (dog.check() is not None) == (strike == 2)
                assert dog.smi_failures == strike + 1
                now[0] += 5
            observer.sample.side_effect = lambda pid: dict.fromkeys(keys, 0)
            query.return_value = 0
            assert dog.check() is None
            assert dog.smi_failures == 0
        else:
            reason = dog.check()
            assert (reason is not None) == (foreign or missing or case == "cap")
            if case == "cap":
                assert "exceeds placement cap" in reason
            assert dog.smi_failures == 0
    if case in ("growth", "release-foreign"):
        assert query.call_count == (3 if stage == "watchdog" and case == "release-foreign" else 2)
    if case in ("ambiguous", "crossing-pids"):
        assert query.call_count == (10 if stage == "watchdog" else 3)
    observer.close.assert_not_called()
