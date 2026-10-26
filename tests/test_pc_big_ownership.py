"""PC-BIG ownership and admission regressions with real locks and CPU process fakes."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import tomllib
from unittest import mock
import uuid

import pytest

from hyperloom import device_policy, gpu_counters, pc_big, placement_supervision, procs, rtx
from hyperloom.recipes import grants, measurements, preflight, reader, service
from hyperloom.telemetry.store import Store
from hyperloom.web.api import API, APIError
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW, window
from tests import test_rtx
from tests.test_rtx import FakeChild, _binary_spec


@pytest.fixture
def topology(tmp_path, monkeypatch):
    pro, assist = ("GPU-" + str(uuid.uuid4()) for _ in range(2))
    grant = dict(uuid=assist, vram_cap_gib=4, approval="CPU test", window=window(),
                 gate=[sys.executable, "fake-gate"])
    cluster = SimpleNamespace(pc={"cuda": {"grants": [grant]}}, profiles={
        "service": {"prefill_service": {"machine": "local", "device": "anchor"}}},
        machines={"local": SimpleNamespace(access="local", devices=[
            dict(id="anchor", uuid=pro), dict(id="assist", uuid=assist, pc_big_conflict=True)])})
    monkeypatch.setattr(service, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(rtx, "device_lock_path", lambda u: tmp_path / ("pcbig-" + u.lower() + ".lock"))
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    return cluster, pro, assist


def test_two_device_helper_and_watchdog_do_not_self_lock(tmp_path, monkeypatch):
    helper = test_rtx.HelperTests()
    helper.setUp()
    data = policy()
    keys = sorted(u.lower() for u in data["caps_gib"])
    pro = next(g["uuid"] for g in data["grants"] if g["uuid"].lower() == keys[0])
    data["pc_big_guard"] = dict(pro_uuid=pro, preferences=str(tmp_path / "telemetry.sqlite3"))
    assist = keys[1]
    dogs, apps = [], [(pro, 99999, None), (assist, 99999, None)]
    real_dog = placement_supervision.Watchdog
    class Captured(real_dog):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw, clock=lambda: NOW)
            dogs.append(self)
    observer = mock.Mock()
    observer.sample.side_effect = lambda pid: {u: 4000 * (1 << 20) if u == pro.lower() and pid == os.getpid()
                                               else 0 for u in keys}
    monkeypatch.setattr(placement_supervision, "Watchdog", Captured)
    # Encode spelling so Windows also exercises Linux's case-sensitive legacy lock filenames.
    monkeypatch.setattr(rtx, "device_lock_path", lambda u: tmp_path / ("pcbig-" + u.encode().hex() + ".lock"))
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    monkeypatch.setattr(rtx, "compute_apps", lambda: list(apps))
    total = [4000]
    monkeypatch.setattr(rtx, "device_used_mib", lambda u: total[0] if u.lower() == pro.lower() else 0)
    spec = replace(_binary_spec(), placement_supervision=data, pro_uuid=pro, deadline=NOW + 600)
    monkeypatch.setattr(time, "time", lambda: NOW)
    thread = helper.run_helper(helper.write(spec), lock_factory=rtx.device_lock,
        gate_runner=lambda _: None, observer_factory=lambda: observer,
        memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    try:
        state = helper.wait_state("ready", "stopped")
        assert state["state"] == "ready", state
        assert procs.lock_held(rtx.device_lock_path(pro))
        with pytest.raises(procs.ProcessError):
            rtx.device_lock(pro).acquire()
        apps.extend([(pro, os.getpid(), 4000), (pro, 4242, None)])
        dog = dogs[0]
        dog._pc_big_next = 0
        assert dog.check() is None
        FakeChild.instances[0].process.kill.assert_not_called()
        # The PRO guard never classifies an assist's foreign numeric row as a PC-BIG window.
        apps.append((assist, 777, rtx.PRO_IDLE_MIB + 1))
        dog._pc_big_next = 0
        assert dog.check() is None
        apps.pop()
        apps.append((pro, 777, rtx.PRO_IDLE_MIB + 1))
        dog._pc_big_next = 0
        assert "PC-BIG" in dog.check()
        apps.pop()
        total[0] = 4000 + rtx.PRO_IDLE_MIB
        dog._pc_big_next = 0
        assert dog.check() is None
        total[0] += 1
        dog._pc_big_next = 0
        assert "PC-BIG" in dog.check()
        total[0] = 4000
        ledger = tmp_path / "ledger.md"
        ledger.write_text("12:00 operator: **PC-BIG start**\n", encoding="utf-8")
        store = Store(tmp_path / "telemetry.sqlite3")
        store.put("prefs", "turns/ledger_path", str(ledger))
        store.close()
        dog._pc_big_next = 0
        assert "PC-BIG" in dog.check()
    finally:
        (helper.directory / "stop").touch()
        thread.join(5)
        helper.doCleanups()
    assert not thread.is_alive()
    assert not procs.lock_held(rtx.device_lock_path(pro))


def test_missing_pro_uuid_cannot_grant_with_zero_live_probes(topology, monkeypatch):
    cluster, pro, assist = topology
    with mock.patch.object(procs, "lock_held", return_value=True) as locks, \
            mock.patch.object(rtx, "pro_busy", return_value="busy") as busy:
        with pytest.raises(reader.RecipeError, match="PL_E_GRANT.*PC-BIG"):
            measurements.gpu_grant(cluster, dict(uuid=assist), now=NOW)
        assert locks.called or busy.called
        assert locks.call_args.args[0] == rtx.device_lock_path(pro)


def test_conflicting_probe_without_reliable_pro_identity_refuses(topology):
    cluster, _, assist = topology
    cluster.profiles.clear()
    with pytest.raises(reader.RecipeError, match="PL_E_GRANT.*PC-BIG"):
        measurements.gpu_grant(cluster, dict(uuid=assist), now=NOW)


def test_reviewed_handover_guard_uses_only_proven_live_owners(tmp_path, monkeypatch):
    from tests.test_placement_preflight import PreflightTests
    h = PreflightTests()
    h.setUp()
    try:
        h.cluster.pc["cuda"]["pro_uuid"] = h.uuid
        aux = "GPU-" + str(uuid.uuid4())
        h.cluster.pc["cuda"]["grants"].append(dict(h.grant, uuid=aux))
        h.recipe["routes"][0]["assist"]["aux"]["device_gib"] = 8
        h.bound["binding"]["aux"] = [dict(id=aux, host="local")]
        h.report["devices"].append(dict(h.report["devices"][0], id=aux, uuid=aux))
        h.backend.place.return_value.document["devices"].append(dict(uuid=aux, set="aux", budget_bytes=8 * rtx.GIB))
        h.observer.sample.return_value[aux.lower()] = 0
        monkeypatch.setattr(rtx, "device_lock_path", lambda u: tmp_path / (u.lower() + ".lock"))
        directory = tmp_path / "old-service"
        directory.mkdir()
        live = dict(state="ready", pid=42, device=[dict(uuid=h.uuid), dict(uuid=aux)])
        (directory / "state.json").write_text(json.dumps(live), encoding="utf-8")
        reviewed = {"old": dict(token="reviewed controller", prefill_service=dict(
            directory=str(directory), pid=42, device=h.uuid, port=8096,
            placement_supervision=dict(caps_bytes={h.uuid: 90 * rtx.GIB, aux: 8 * rtx.GIB})))}
        apps = [(h.uuid, 42, 4000), (h.uuid, 777, None), (aux, 777, None)]
        monkeypatch.setattr(rtx, "compute_apps", lambda: list(apps))
        monkeypatch.setattr(rtx, "pro_busy", lambda _: "reviewed child")
        total = [4000]
        monkeypatch.setattr(rtx, "device_used_mib", lambda u: total[0] if u.lower() == h.uuid.lower() else 0)
        def observer():
            result = mock.Mock()
            result.sample.side_effect = lambda pid: {h.uuid.lower(): 4000 * (1 << 20) if pid == 42 else 0,
                                                     aux.lower(): 0}
            return result
        def check():
            return preflight.check(h.cluster, "fake-recipe", preflight.session.Layers(), h.backend, h.recipe,
                h.bound, h.report, h.raw, "a" * 64, inputs=h.inputs, cache=h.cache, now=NOW,
                busy=lambda _: None, observer_factory=observer, reviewed=reviewed)
        with service.configured(tmp_path / "config.json"), ExitStack() as stack:
            lock = rtx.device_lock(h.uuid).acquire()
            stack.callback(lock.close)
            lifetime = procs.InstanceLock(directory / "helper.lock").acquire()
            stack.callback(lifetime.close)
            check()
            apps.append((h.uuid, 777, rtx.PRO_IDLE_MIB + 1))
            with pytest.raises(reader.RecipeError, match="PC-BIG"):
                check()
            apps.pop()
            total[0] += rtx.PRO_IDLE_MIB + 1
            with pytest.raises(reader.RecipeError, match="PC-BIG"):
                check()
            total[0] = 4000
            live["pid"] = 43
            (directory / "state.json").write_text(json.dumps(live), encoding="utf-8")
            with pytest.raises(reader.RecipeError, match="PC-BIG"):
                check()
    finally:
        h.doCleanups()


def test_watchdog_rate_limits_pc_big_and_tolerates_only_transient_counters(tmp_path, monkeypatch):
    data = policy()
    pro = "GPU-" + str(uuid.uuid4())
    data["pc_big_guard"] = dict(pro_uuid=pro, preferences=str(tmp_path / "telemetry.sqlite3"))
    observer = mock.Mock()
    observer.sample.return_value = dict.fromkeys((u.lower() for u in data["caps_gib"]), 0)
    now = [NOW]
    dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data, deadline=NOW + 600),
        42, lambda: None, lambda _: None, observer=observer, clock=lambda: now[0],
        pc_big_clock=lambda: now[0],
        memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    monkeypatch.setattr(procs, "lock_held", lambda _: False)
    with mock.patch.object(rtx, "pro_busy", return_value=None) as busy:
        assert dog.check() is None
        for _ in range(8):
            now[0] += .5
            assert dog.check_memory()[0] is None
            assert dog.check() is None
        assert busy.call_count == 1
        busy.side_effect = rtx.ServiceError("counter unavailable")
        for strike in range(3):
            now[0] += 5
            reason = dog.check()
            assert (reason is not None) == (strike == 2)
        busy.side_effect = None
        now[0] += 5
        assert dog.check() is None
        assert dog.smi_failures == 0
        monkeypatch.setattr(procs, "lock_held", mock.Mock(side_effect=OSError("unreadable")))
        now[0] += 5
        assert "PC-BIG" in dog.check()


def test_known_store_survives_policy_and_measurement_handoff(topology, tmp_path, monkeypatch, request):
    from tests.test_recipes_grants import GrantTests
    cluster, pro, assist = topology
    preferences = tmp_path / "telemetry.sqlite3"
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    def create():
        store = Store(preferences)
        store.put("prefs", "turns/ledger_path", str(ledger))
        store.close()
    create()
    h = GrantTests()
    h.setUp()
    request.addfinalizer(h.doCleanups)
    h.cluster.pc["cuda"]["pro_uuid"] = pro
    h.recipe["routes"][0]["assist"]["aux"]["device_gib"] = 8
    h.binding["aux"] = [dict(id=h.aux, host="local")]
    h.cluster.pc["cuda"]["grants"].append(dict(h.grant, uuid=h.aux))
    plan = dict(devices=[dict(uuid=h.uuid, set="anchor", budget_bytes=90 * rtx.GIB),
                        dict(uuid=h.aux, set="aux", budget_bytes=8 * rtx.GIB)],
                host=dict(pinned_budget_bytes=68 * rtx.GIB, pageable_budget_bytes=0))
    data = grants.policy(h.cluster, h.recipe, h.recipe["routes"][0], h.binding, h.report, plan, h.hosts, now=NOW)
    assert data["pc_big_guard"]["preferences_required"]
    assert data["pc_big_guard"]["ledger_required"]
    preferences.unlink()
    with pytest.raises(rtx.ServiceError, match="PC-BIG"):
        placement_supervision.validate(data, now=NOW)
    create()
    def inventory():
        preferences.unlink()
        return [dict(uuid=assist, driver="test")]
    def runner(row, grant):
        pc_big.require_clear(row["_pc_big_guard"])
        pytest.fail("deleted admission store was forgotten")
    with pytest.raises(reader.RecipeError, match="PC-BIG"):
        measurements.acquire(cluster, "xfer", runtime=tmp_path, uuid=assist, xfer_sha256="a" * 64,
                             store_id="test", clock=lambda: NOW, inventory=inventory, gpu_runner=runner)


def test_root_pytest_excludes_submodule_collection():
    root = Path(__file__).resolve().parents[1]
    options = tomllib.loads((root / "pyproject.toml").read_text())["tool"].get("pytest", {}).get("ini_options", {})
    assert options.get("testpaths") == ["tests"]
    assert "third_party" in options.get("norecursedirs", [])


@pytest.mark.parametrize("kind", ["apostrophe-path", "parenthesized-pid", "bare-uuid", "mig-uuid", "unknown-code"])
def test_legacy_recipe_portal_error_uses_public_envelope(tmp_path, monkeypatch, kind):
    api = API(tmp_path / "config.json")
    monkeypatch.setattr(api, "_arguments", lambda *a: dict(profile="test", recipe="legacy"))
    monkeypatch.setattr(api, "_recipe_up_argv", lambda _: ["up", "test", "--recipe", "legacy"])
    audit = mock.Mock()
    monkeypatch.setattr(api, "_audit", audit)
    errors = {"apostrophe-path": FileNotFoundError(2, "missing", str(tmp_path / "O'Brien" / "snapshot.json")),
              "parenthesized-pid": reader.RecipeError("approval missing for pid (12345)"),
              "bare-uuid": reader.RecipeError(str(uuid.uuid4())),
              "mig-uuid": reader.RecipeError("MIG-" + str(uuid.uuid4())),
              "unknown-code": reader.RecipeError("PL_E_PRIVATE arbitrary private details")}
    error = errors[kind]
    monkeypatch.setattr(preflight, "cli", mock.Mock(side_effect=error))
    with pytest.raises(APIError) as caught:
        api.mutate("up", {})
    assert caught.value.status == 400
    assert str(caught.value) == "recipe preflight refused: preflight checks failed; see local audit"
    assert audit.call_args.args[2]["error"] == str(error)
    assert api._active is None


@pytest.mark.parametrize("signal", ["held", "unreadable"])
def test_unowned_pro_lock_refuses_on_next_full_tick(tmp_path, monkeypatch, signal):
    data = policy()
    pro = "GPU-" + str(uuid.uuid4())
    data["pc_big_guard"] = dict(pro_uuids=[pro], preferences=str(tmp_path / "telemetry.sqlite3"))
    observer = mock.Mock()
    observer.sample.return_value = {u.lower(): 0 for u in data["caps_gib"]}
    now = [NOW]
    dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data, deadline=NOW + 600),
        42, lambda: None, lambda _: None, observer=observer, clock=lambda: now[0],
        pc_big_clock=lambda: now[0], memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    locks, busy, ledger = mock.Mock(return_value=False), mock.Mock(return_value=None), mock.Mock(return_value=False)
    monkeypatch.setattr(procs, "lock_held", locks)
    monkeypatch.setattr(rtx, "pro_busy", busy)
    monkeypatch.setattr(pc_big, "_ledger_active", ledger)
    assert dog.check() is None
    if signal == "held":
        locks.return_value = True
    else:
        locks.side_effect = OSError("unreadable lock")
    now[0] += rtx.WATCH_S
    assert "PC-BIG" in dog.check()
    assert busy.call_count == ledger.call_count == 1
    assert dog.smi_failures == 0


def test_legacy_visible_changes_document_early_busy_order():
    document = (Path(__file__).resolve().parents[1] / "docs/api.md").read_text(encoding="utf-8")
    legacy = document.split("## Legacy-visible changes", 1)[1].split("## Access and authentication", 1)[0]
    assert "HTTP 409" in legacy
    assert "before preflight" in legacy


@pytest.mark.parametrize("stage", ["pre-start", "watchdog"])
@pytest.mark.parametrize("signal", ["desktop-pro", "desktop-assist", "numeric-assist", "numeric-pro",
                                   "residual-boundary", "residual-over", "query-failure", "observer-failure",
                                   "missing-attribution", "negative-attribution"])
def test_owned_pro_activity_uses_attributable_residual(tmp_path, monkeypatch, stage, signal):
    data = policy()
    pro, assist = sorted(u.lower() for u in data["caps_gib"])
    data["pc_big_guard"] = dict(pro_uuids=[pro.upper()], preferences=str(tmp_path / "telemetry.sqlite3"))
    owners = {pro.upper(): {os.getpid(), 42}, assist: {42}}
    observer = mock.Mock()
    observer.sample.side_effect = lambda pid: {pro: (3000 if pid == os.getpid() else 1000) * (1 << 20), assist: 0}
    monkeypatch.setattr(gpu_counters, "Observer", lambda: observer)
    monkeypatch.setattr(procs, "lock_held", mock.Mock(side_effect=AssertionError("owned PRO lock checked")))
    monkeypatch.setattr(rtx, "pro_busy", mock.Mock(side_effect=AssertionError("owned PRO busy checked")))
    apps = []
    if signal == "desktop-pro":
        apps.append((pro, 777, None))
    if signal == "desktop-assist":
        apps.append((assist, 777, None))
    if signal.startswith("numeric"):
        apps.append((pro if signal == "numeric-pro" else assist, 777, rtx.PRO_IDLE_MIB + 1))
    monkeypatch.setattr(rtx, "compute_apps", lambda: apps)
    total = mock.Mock(return_value=4000 + rtx.PRO_IDLE_MIB + (signal == "residual-over"))
    monkeypatch.setattr(rtx, "device_used_mib", total)
    if signal == "query-failure":
        total.side_effect = rtx.ServiceError("total unavailable")
    if signal == "observer-failure":
        observer.sample.side_effect = gpu_counters.CounterError("attribution unavailable")
    if signal in ("missing-attribution", "negative-attribution"):
        observer.sample.side_effect = None
        observer.sample.return_value = {} if signal == "missing-attribution" else {pro: -1, assist: 0}
    refused = signal in ("numeric-pro", "residual-over", "query-failure", "observer-failure",
                         "missing-attribution", "negative-attribution")
    if stage == "pre-start":
        if refused:
            error = pc_big.CounterUnavailable if signal == "query-failure" else rtx.ServiceError
            with pytest.raises(error):
                placement_supervision.validate(data, now=NOW, starting=True, owners=owners)
        else:
            placement_supervision.validate(data, now=NOW, starting=True, owners=owners)
    else:
        now = [NOW]
        dog = placement_supervision.Watchdog(replace(_binary_spec(), placement_supervision=data, deadline=NOW + 600),
            42, lambda: None, lambda _: None, observer=observer, clock=lambda: now[0],
            pc_big_clock=lambda: now[0], memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
        if signal == "query-failure":
            for strike in range(3):
                assert (dog.check() is not None) == (strike == 2)
                now[0] += 5
            total.side_effect = None
            assert dog.check() is None
            assert dog.smi_failures == 0
        else:
            assert (dog.check() is not None) == refused
            assert dog.smi_failures == 0


def test_privacy_scan_catches_nested_docs_and_shipped_recipe_references():
    from tests.test_placement_privacy import LOCAL_IDENTITY, findings
    for text in ("/mnt/" + "d/tmp/project", "/home/" + "operator/file", "review-" + "claude-hl-5.md",
                 "pytest-" + "codex-red.log"):
        assert LOCAL_IDENTITY.search(text), text
    assert findings() == {}


def test_configured_device_policy_does_not_inherit_model_name_default(topology):
    _, _, assist = topology
    card = dict(uuid=assist, name="RTX 4090")
    assert not device_policy.resolve(card, [dict(uuid=assist, pc_big_conflict=True)], placement=True)["excluded"]
    assert device_policy.resolve(card, [])["excluded"]
    assert device_policy.resolve(card, [dict(uuid=assist, excluded=True)], placement=True)["excluded"]
    assert device_policy.resolve(dict(uuid=assist, name="generic"), [dict(name="generic")], placement=True)["excluded"]
