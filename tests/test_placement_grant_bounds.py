"""Byte/GiB supervision bounds and the PC's admission hold, with CPU fakes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import mock
import json
import math

import pytest

from hyperloom import admission, cli, orchestrate, placement_supervision, rtx
from hyperloom.config import parse
from hyperloom.recipes import launch, service, grants, reader, runtime, core

from tests import test_recipes, test_rtx
from tests.placement_fixtures import grant, identity, files
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW, window
from tests.test_placement_legacy_equality import baseline, outcome


@pytest.mark.parametrize("selection", ["mixed-auto", "stale-profile"])
def test_admission_resources_follow_selected_launch(tmp_path, monkeypatch, selection):
    cluster = parse(test_rtx.rtx_example())
    del cluster.profiles["ds41-rtx"]["prefill_service"]
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    path = recipes / "legacy.toml"
    path.write_text(test_recipes.DS, encoding="utf-8")
    doc = test_recipes.PlacementSchemaTests.document()
    (recipes / "placement.json").write_text(json.dumps(doc), encoding="utf-8")
    args = ["up", "ds41-rtx", "--recipe", "auto", "--recipe-dir", str(recipes),
            "--config", str(tmp_path / "config.json")]
    if selection == "stale-profile":
        profile = tmp_path / "profile.toml"
        profile.write_text(test_recipes.profile_text(recipe_sha="0" * 64).replace(
            'recipe = "minimax-m3.1-flash-lab"', 'recipe = "ds41-lab"').replace(
            '"limits.host_free_ram_floor_gib" = 4',
            '"routes.decode_pair.components.drafter.device" = "pair"'), encoding="utf-8")
        args += ["--recipe-profile", str(profile)]
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "candidate_dirs", lambda *a: [recipes])
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: tmp_path / "state.json")
    monkeypatch.setattr(launch, "_backend", lambda _: (core.StubBackend(), False))
    monkeypatch.setattr(launch.render, "router_policies", lambda: ("routes",))
    source = lambda ctx, **kw: test_recipes.UpWithRecipeTests.source(SimpleNamespace(avail={}), ctx)
    monkeypatch.setattr(launch.commands, "live_source", lambda *a, **kw: source)
    prepared = []
    prepare = launch.prepare
    def selected(*a, **kw):
        result = prepare(*a, **kw)
        assert result.placement_request is None and result.evaluation.path == str(path)
        if selection == "stale-profile":
            assert result.evaluation.effective.untuned
        prepared.append(result)
        return result
    monkeypatch.setattr(launch, "prepare", selected)
    database = admission.Admission(":memory:")
    monkeypatch.setattr(admission, "for_config", lambda _: database)
    args = cli.build_parser().parse_args(args)
    try:
        database.acquire("bench", {"halo-1"}, label="node benchmark")
        expected = outcome(lambda: baseline(cli, "_admission_hold")(args))
        assert expected[0] is RuntimeError and "benchmark" in expected[1]
        actual = outcome(lambda: cli._admission_hold(args))
        if callable(actual):
            actual()
        assert actual == expected
        assert prepared
    finally:
        database.close()


@pytest.mark.parametrize("tenths", range(1, 1000))
def test_decimal_caps_match_tierstream_round_half_up(tenths):
    data = policy()
    cap = tenths / 10
    budget = math.floor(cap * 1073741824.0 + 0.5)  # recipe.cpp gib_bytes; place.cpp d.budget.
    for entry in data["grants"]:
        entry["vram_cap_gib"] = cap
    data["caps_gib"] = {u: cap for u in data["caps_gib"]}
    data["caps_bytes"] = {u.lower(): budget for u in data["caps_gib"]}
    original = deepcopy(data)
    placement_supervision.validate(data, now=NOW)
    assert data == original
    for uuid in data["caps_bytes"]:
        data["caps_bytes"][uuid] += 1
        overrun = deepcopy(data)
        with pytest.raises(rtx.ServiceError, match="cap.*exceeds"):
            placement_supervision.validate(data, now=NOW)
        assert data == overrun
        data["caps_bytes"][uuid] -= 1


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("index", [0, 1])
def test_integer_caps_are_bounded_by_each_gib_cap_and_grant(extra, index):
    data = policy()
    data["caps_bytes"] = {u.lower(): int(v * rtx.GIB) for u, v in data["caps_gib"].items()}
    uuid = list(data["caps_bytes"])[index]
    data["caps_bytes"][uuid] += extra
    original = deepcopy(data)
    if extra:
        with pytest.raises(rtx.ServiceError, match="cap.*exceeds"):
            placement_supervision.validate(data, now=NOW)
    else:
        placement_supervision.validate(data, now=NOW)
    assert data == original  # A refusal must never clamp a budget.


@pytest.mark.parametrize("invalid", [None, {}, {"unknown": 1}])
def test_missing_or_incomplete_byte_caps_refuse(invalid):
    data = policy()
    data["caps_bytes"] = invalid
    with pytest.raises(rtx.ServiceError, match="integer byte caps"):
        placement_supervision.validate(data, now=NOW)


@pytest.mark.parametrize("placement", [False, True])
def test_placement_hold_includes_control_machine_without_prefill(tmp_path, monkeypatch, placement):
    cluster = parse(test_rtx.rtx_example())
    del cluster.profiles["ds41-rtx"]["prefill_service"]
    path = tmp_path / "recipe.json"
    doc = test_recipes.PlacementSchemaTests.document()
    if not placement:
        doc["routes"][0].pop("components", None)
        doc["routes"][0].pop("assist", None)
        doc["routes"][0].pop("max_rows", None)
        doc["routes"][0].pop("max_sequences", None)
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "locate", lambda *a, **kw: path)
    monkeypatch.setattr(service, "deployment_profile", lambda *a: "ds41-rtx")
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: tmp_path / "state.json")
    prepared = SimpleNamespace(placement_request=object() if placement else None, inputs=[],
                               evaluation=SimpleNamespace(path=str(path), effective_sha256="d" * 64,
                                                          context=SimpleNamespace(pc="workstation")))
    monkeypatch.setattr(launch, "prepare", mock.Mock(return_value=prepared))
    database = mock.Mock()
    monkeypatch.setattr(admission, "for_config", lambda _: database)
    args = cli.build_parser().parse_args(["up", "ds41-rtx", "--recipe", str(path),
                                          "--config", str(tmp_path / "config.json")])
    with service.configured(args.config):
        release = cli._admission_hold(args)
        release()
    resources = database.acquire.call_args.args[1]
    assert resources == ({"workstation"} if placement else {"halo-1", "halo-2", "router"})


def test_03_future_big_window_bounds_4090_turn(identity):
    entry = grant(identity)
    policy = dict(grants=[entry], caps_gib={identity.lower(): 90}, caps_bytes={identity.lower(): 90 * rtx.GIB},
                  host_pinned_gib=0, host_pageable_gib=0,
                  free_commit_floor_gib=4, pc_big_deadline=NOW + 120)
    with pytest.raises(rtx.ServiceError, match="PC-BIG"):
        placement_supervision.validate(policy, now=NOW + 120)


def test_07_absent_positive_optional_assist_consumes_no_grant(identity):
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    recipe["routes"][0]["components"]["drafter"]["device"] = "auto"
    cluster = SimpleNamespace(pc={"cuda": {"grants": [grant(identity)], "host_grant": dict(
        host_gib=74, approval="test", window=window())}})
    binding = {"anchor": [dict(id=identity, host="local")], "aux": []}
    report = {"devices": [dict(id=identity, uuid=identity, name="anchor", kind="cuda")]}
    used = grants.check(cluster, recipe, binding, report, {"local": {"free_ram_gib": 100}}, now=NOW)
    assert set(used) == {identity.lower()}


def test_14_core_half_byte_budget_is_carried_exactly(tmp_path, files, identity):
    artifacts, document = files
    cap = 10 + .5 / rtx.GIB
    budget = 10 * rtx.GIB + 1  # Tierstream floor(x + .5), unlike Python round.
    document["devices"][0]["budget_bytes"] = budget
    artifacts.placement.write_bytes(json.dumps(document).encode())
    policy = dict(grants=[grant(identity, cap)], caps_gib={identity.lower(): cap},
                  caps_bytes={identity.lower(): budget}, host_pinned_gib=0, host_pageable_gib=0,
                  free_commit_floor_gib=4)
    cluster = SimpleNamespace(machines={"local": SimpleNamespace(access="local", os="windows", install_root=str(tmp_path))})
    with mock.patch.object(runtime, "preset", return_value=dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])), \
            mock.patch.object(runtime, "render", return_value=(["fake"], {}, document)), \
            mock.patch.object(rtx.time, "time", return_value=NOW):
        spec = rtx.render_placement_service(cluster, runtime.Request("local", artifacts, policy), api_key="0" * 64)
        policy["caps_bytes"][identity.lower()] += 1
        with pytest.raises(rtx.ServiceError, match="byte cap exceeds"):
            rtx.render_placement_service(cluster, runtime.Request("local", artifacts, policy), api_key="0" * 64)
        policy["caps_bytes"][identity.lower()] -= 1
    assert spec.placement_supervision["caps_bytes"][identity.lower()] == budget
    observed = {identity.lower(): budget}
    observer = mock.Mock()
    observer.sample.side_effect = lambda pid: observed
    dog = placement_supervision.Watchdog(spec, 42, lambda: None, lambda _: None, observer=observer,
        clock=lambda: NOW, memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    assert dog.check() is None
    observed[identity.lower()] += 1
    assert "placement cap" in dog.check()


def test_03_admission_refuses_future_big_overlap(identity, request):
    from tests.test_recipes_grants import GrantTests
    helper = GrantTests()
    helper.setUp()
    request.addfinalizer(helper.doCleanups)
    helper.cluster.machines = {"local": SimpleNamespace(access="local", devices=[
        dict(uuid=helper.uuid, pc_big_conflict=True)])}
    helper.report["devices"][0]["name"] = "RTX " + str(4090)
    future = grant(identity)
    future["window"] = dict(window("PC-BIG"), starts_at="2030-01-01T00:02:00Z")
    helper.cluster.pc["cuda"]["grants"].append(future)
    with pytest.raises(reader.RecipeError, match="PC-BIG"):
        helper.check()
