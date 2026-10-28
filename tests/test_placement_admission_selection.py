"""Selected-launch admission across CLI and portal hand-offs, using CPU facts and gates."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import admission, cli, orchestrate
from hyperloom.config import parse
from hyperloom.recipes import core, grants, launch, preflight, reader, runtime, service, session
from hyperloom.web.api import API, APIError
from tests import test_recipes, test_rtx
from tests.test_recipes_grants import NOW, window


@pytest.fixture
def selection_env(tmp_path, monkeypatch):
    data = test_rtx.rtx_example()
    del data["profiles"]["ds41-rtx"]["prefill_service"]
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data), encoding="utf-8")
    cluster = parse(data)
    cluster.pc["cuda"]["grants"] = [dict(uuid=test_recipes.GPU, vram_cap_gib=90,
        approval="CPU test", window=window(), gate=[sys.executable, "fake-gate"])]
    cluster.pc["cuda"]["host_grant"] = dict(host_gib=74, approval="CPU test", window=window())
    data["pc"]["cuda"] = deepcopy(cluster.pc["cuda"])
    config.write_text(json.dumps(data), encoding="utf-8")
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    placement = test_recipes.PlacementSchemaTests.document()
    placement["match"]["model"] = "deepseek-v4.1-flash"
    placement["routes"][0]["components"]["drafter"]["device"] = "auto"
    (recipes / "placement.json").write_text(json.dumps(placement), encoding="utf-8")
    legacy = reader.load_text(test_recipes.DS)
    legacy["match"]["devices"] = [dict(id="pair", kind="hip", count=2, memory_min_gib=1)]
    legacy["match"].pop("links", None)
    legacy["routes"] = legacy["routes"][1:]
    (recipes / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
    backend = core.CoreBackend(core.Core("CPU fake core"))
    stub = core.StubBackend()
    backend.resolve = stub.resolve
    backend.select = lambda p, l, r, s, sha, identity=None: reader.select(r.recipe, s, sha)
    backend.match = lambda p, l, report, identity: ([], identity["binding"] if identity else {
        "anchor": [dict(id=test_recipes.GPU, host="workstation")], "aux": []}, "d" * 64)
    now = [NOW]
    checks = []
    def check(cluster, path, layers, backend, recipe, bound, report, raw, sha, **kw):
        checks.append(now[0])
        grants.check(cluster, recipe, bound["binding"], report, raw["hosts"], now=now[0])
        return {}
    monkeypatch.setattr(preflight, "check", check)  # Native adapter/render are fake; grant validation is real.
    monkeypatch.setattr(launch, "_backend", lambda _: (backend, False))
    monkeypatch.setattr(launch.render, "router_policies", lambda: ("routes",))
    monkeypatch.setattr(launch.render, "check_route_table", lambda _: None)  # Parser parity has separate tests.
    monkeypatch.setattr(session.apply, "refuse_unsupported", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "decision", lambda *a, **kw: {"placement": True})
    request = SimpleNamespace(files=mock.Mock())
    monkeypatch.setattr(runtime, "request", lambda _: request)
    monkeypatch.setattr(launch, "render_placement", lambda *a, **kw: SimpleNamespace(placement={}))
    source = lambda ctx, **kw: test_recipes.UpWithRecipeTests.source(SimpleNamespace(avail={}), ctx)
    monkeypatch.setattr(launch.commands, "live_source", lambda *a, **kw: source)
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "candidate_dirs", lambda *a: [recipes])
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: tmp_path / "launch.json")
    database = admission.Admission(":memory:")
    monkeypatch.setattr(admission, "for_config", lambda _: database)
    api = API(config, audit_path=tmp_path / "audit.jsonl")
    api.admission_db = database
    api._heartbeat = object()  # No background renewal needed for these synchronous CPU tests.
    monkeypatch.setattr(api, "_cluster", lambda: cluster)
    monkeypatch.setattr(api, "_recipe_loader", lambda _: cluster)
    monkeypatch.setattr(api, "_start_worker", lambda run, *a: run())
    try:
        yield SimpleNamespace(cluster=cluster, config=config, recipes=recipes, now=now, checks=checks,
                              database=database, api=api, request=request)
    finally:
        database.close()


@pytest.mark.parametrize("placement", [True, False])
def test_portal_holds_selected_resources_without_prefill(selection_env, monkeypatch, placement):
    env = selection_env
    recipe = "placement" if placement else "legacy"
    monkeypatch.setattr(service, "locate", lambda *a, **kw: env.recipes / (recipe + ".json"))
    monkeypatch.setattr(service, "deployment_profile", lambda *a: "ds41-rtx")
    held = []
    monkeypatch.setattr(env.api, "_execute", lambda *a: held.append(env.database.holds()[0]["resources"]))
    env.api.mutate("up", dict(profile="ds41-rtx", recipe=recipe))
    assert held == [["workstation"] if placement else ["halo-1", "halo-2", "router"]]
    env.database.acquire("bench", {"workstation"}, label="PC benchmark")
    if placement:
        with pytest.raises(APIError, match="benchmark") as refused:
            env.api.mutate("up", dict(profile="ds41-rtx", recipe=recipe))
        assert refused.value.status == 409
    else:
        env.api.mutate("up", dict(profile="ds41-rtx", recipe=recipe))
        assert len(held) == 2


@pytest.mark.parametrize("entry", ["cli", "portal", "detached"])
@pytest.mark.parametrize("changed", [True, False])
def test_admitted_selection_survives_grant_cutoff(selection_env, monkeypatch, entry, changed):
    env = selection_env
    # Isolate the reselection race from the portal resource bug covered by the preceding regression.
    monkeypatch.setattr(env.api, "_resources_of_profile", lambda *a: {"workstation"})
    env.database.acquire("bench", {"halo-1"}, label="node benchmark")
    writes = mock.Mock(side_effect=AssertionError("state published under the wrong admission"))
    monkeypatch.setattr(orchestrate, "_write", writes)
    started = mock.Mock(return_value={"phase": "running"})
    monkeypatch.setattr(launch, "up_placement", started)
    prepare = launch.prepare
    chosen = []
    def prepared(*a, **kw):
        result = prepare(*a, **kw)
        chosen.append(result.placement_request is not None)
        return result
    monkeypatch.setattr(launch, "prepare", prepared)
    def expire():
        if changed:
            env.now[0] = NOW + 541  # The real grant now expires in 59 s, below the startup bound.
    argv = ["up", "ds41-rtx", "--recipe", "auto", "--config", str(env.config)]
    release = lambda: None
    try:
        if entry == "cli":
            args = cli.build_parser().parse_args(argv)
            release = cli._admission_hold(args)
            assert env.database.holds()[-1]["resources"] == ["workstation"]
            expire()
            if changed:
                with pytest.raises(reader.RecipeError, match="selection changed after admission; retry"):
                    cli.up_command(args)
            else:
                assert cli.up_command(args)["phase"] == "running"
        else:
            execute = env.api._execute
            def after_hold(action, arguments):
                assert env.database.holds()[-1]["resources"] == ["workstation"]
                expire()
                if entry == "detached":
                    # Exercise the actual portal argv -> child parser -> child admission hand-off.
                    child = cli.build_parser().parse_args(env.api._cli_argv(action, arguments)[3:])
                    child_release = cli._admission_hold(child)
                    try:
                        return cli.up_command(child)
                    finally:
                        child_release()
                return execute(action, arguments)
            monkeypatch.setattr(env.api, "_execute", after_hold)
            job = env.api.mutate("up", dict(profile="ds41-rtx", recipe="auto"))
            result = env.api._jobs[job["job"]]
            if changed:
                assert result["state"] == "failed"
                assert "selection changed after admission; retry" in result["error"]
            else:
                assert result["state"] == "succeeded", result["error"]
        assert chosen[0] is True
        assert chosen[-1] is (not changed)
        assert env.checks[0] == NOW
        assert env.checks[-1] == (NOW + 541 if changed else NOW)
        writes.assert_not_called()
        env.request.files.publish.assert_not_called()
        assert started.call_count == (0 if changed else 1)
    finally:
        release()


def test_mixed_legacy_preflight_documents_live_probes_before_hold(selection_env, monkeypatch):
    env = selection_env
    docs = (Path(__file__).parents[1] / "docs" / "api.md").read_text(encoding="utf-8")
    assert "read-only live fact probes on the nodes before the admission hold" in docs
    env.now[0] = NOW + 541
    env.database.acquire("bench", {"halo-1"}, label="node benchmark")
    source = launch.commands.live_source(env.cluster)
    samples = []
    def observed(ctx, **kw):
        samples.append(env.database.holds())
        return source(ctx, **kw)
    monkeypatch.setattr(launch.commands, "live_source", lambda *a, **kw: observed)
    args = cli.build_parser().parse_args(["up", "ds41-rtx", "--recipe", "auto", "--config", str(env.config)])
    with pytest.raises(RuntimeError, match="node benchmark"):
        cli._admission_hold(args)
    assert samples and all([h["kind"] for h in sample] == ["bench"] for sample in samples)
