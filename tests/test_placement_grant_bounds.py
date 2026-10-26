"""Byte/GiB supervision bounds and the PC's admission hold, with CPU fakes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import mock
import json

import pytest

from hyperloom import admission, cli, orchestrate, placement_supervision, rtx
from hyperloom.config import parse
from hyperloom.recipes import launch, service, grants, reader, runtime

from tests import test_recipes, test_rtx
from tests.placement_fixtures import grant, identity, files
from tests.test_placement_supervision import policy
from tests.test_recipes_grants import NOW, window


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
    monkeypatch.setattr(launch, "prepare", mock.Mock())
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
        with pytest.raises(rtx.ServiceError, match="byte cap exceeds"):
            rtx.render_placement_service(cluster, runtime.Request("local", artifacts, policy), api_key="0" * 64)
        policy["caps_gib"][identity.lower()] = budget / rtx.GIB
        policy["grants"][0]["vram_cap_gib"] = budget / rtx.GIB
        spec = rtx.render_placement_service(cluster, runtime.Request("local", artifacts, policy), api_key="0" * 64)
    assert spec.placement_supervision["caps_bytes"][identity.lower()] == budget
    observed = {identity.lower(): budget}
    observer = mock.Mock()
    observer.sample.side_effect = lambda pid: observed
    dog = placement_supervision.Watchdog(spec, 42, lambda: None, lambda _: None, observer=observer,
        clock=lambda: NOW, memory=lambda: dict(free_physical_gib=100, free_commit_gib=120))
    assert dog.check() is None
    observed[identity.lower()] += 1
    assert "placement cap" in dog.check()


def test_03_admission_refuses_future_big_overlap(identity):
    from tests.test_recipes_grants import GrantTests
    helper = GrantTests()
    helper.setUp()
    helper.report["devices"][0]["name"] = "RTX " + str(4090)
    future = grant(identity)
    future["window"] = dict(window("PC-BIG"), starts_at="2030-01-01T00:02:00Z")
    helper.cluster.pc["cuda"]["grants"].append(future)
    with pytest.raises(reader.RecipeError, match="PC-BIG"):
        helper.check()
