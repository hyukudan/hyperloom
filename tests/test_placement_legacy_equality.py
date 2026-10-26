"""Legacy-facing results and error precedence remain equal to the local ddc5640 source."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ast
import hashlib
import json
import subprocess

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import cli, device_policy, orchestrate, rtx
from hyperloom.recipes import apply, commands, facts, probe, reader, service, launch, session
from hyperloom.web import api

from tests import test_web_summaries, test_recipes


@lru_cache
def baseline_text(module):
    relative = Path(module.__file__).resolve().relative_to(Path(__file__).resolve().parents[1]).as_posix()
    return subprocess.check_output(["git", "show", "ddc5640:" + relative], text=True, encoding="utf-8")


def baseline(module, name):
    definition = next(node for node in ast.walk(ast.parse(baseline_text(module)))
                      if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "ddc5640:" + name, "exec"), namespace)
    return namespace[name]


def outcome(operation):
    try:
        return operation()
    except Exception as exc:
        return type(exc), str(exc)


def test_legacy_probe_report_ignores_cuda_driver_and_h2d():
    recipe = {"routes": [], "match": {"devices": [{"id": "cuda", "kind": "cuda"}]}}
    bound = {"binding": {"cuda": [{"id": "synthetic", "host": "local"}]}}
    gathered = {"model": "test", "devices": {"synthetic": {"name": "GPU", "driver": "600",
                "h2d_gbytes_ps": 30, "memory_gib": 96}}}
    old_facts = deepcopy(gathered)
    old_facts["devices"]["synthetic"].pop("driver")  # ddc5640's PC gather never supplied driver identity.
    old_facts["devices"]["synthetic"].pop("h2d_gbytes_ps")
    assert apply.probe_report(recipe, bound, gathered) == baseline(apply, "probe_report")(recipe, bound, old_facts)


@pytest.mark.parametrize("returncode", [0, 1])
def test_legacy_nvidia_query_and_name_match_baseline(returncode):
    result = SimpleNamespace(returncode=returncode, stdout="synthetic, Test   GPU, 24576, 1\n", stderr="failed")
    run = mock.Mock(return_value=result)
    old_run = mock.Mock(return_value=result)
    assert outcome(lambda: probe.nvidia_gpus(run)) == outcome(lambda: baseline(probe, "nvidia_gpus")(old_run))
    assert run.call_args_list == old_run.call_args_list


def test_legacy_facts_do_not_collect_host_topology():
    ctx = SimpleNamespace(pc="local", model="test", nodes=[], service=None)
    cluster = SimpleNamespace(assets={})
    with mock.patch("hyperloom.recipes.topology.host", side_effect=AssertionError("legacy topology walk")):
        result = facts.gather(cluster, ctx, call=mock.Mock(), gpus=lambda: [],
                              memory=lambda: {"total_gib": 128, "free_gib": 90, "free_commit_gib": 100})
    assert result == baseline(facts, "gather")(cluster, ctx, call=mock.Mock(), gpus=lambda: [],
        memory=lambda: {"total_gib": 128, "free_gib": 90, "free_commit_gib": 100})


@pytest.mark.parametrize("timeout", [None, False, 59, orchestrate.MAX_QUEUE_S + 1])
def test_deploy_queue_refusal_matches_baseline(monkeypatch, timeout):
    intent = dict(service.DEFAULT_INTENT, profile="test", queue_timeout_s=timeout)
    monkeypatch.setattr(service, "options_from_inputs", lambda _: (None, intent, None))
    progress = mock.Mock()
    expected = outcome(lambda: baseline(service, "_deploy_pinned")("id", [], None, None, None, progress))
    assert expected == (reader.RecipeError, "the snapshot's queue timeout is out of range")
    assert outcome(lambda: service._deploy_pinned("id", [], None, None, None, progress)) == expected


@pytest.mark.parametrize("flags", [["--deadline", "0"], ["--queue-timeout", "0"], ["--approval-ref", "ref"]])
def test_phase_1a_print_precedes_unused_launch_validation(capsys, flags):
    old = baseline(cli, "main")
    old.__globals__["_admission_hold"] = baseline(cli, "_admission_hold")
    expected = old(["up", *flags])
    expected_output = capsys.readouterr()
    assert expected == 0 and expected_output.out == "Phase 1a\n"
    assert cli.main(["up", *flags]) == expected
    assert capsys.readouterr() == expected_output


@pytest.mark.parametrize("preset_valid", [False, True])
def test_legacy_fidelity_error_precedence_matches_baseline(monkeypatch, preset_valid):
    def preset(*args):
        if preset_valid:
            return None, {}, None
        raise rtx.ServiceError("unknown preset")
    def running(*args):
        raise rtx.ServiceError("not running")
    monkeypatch.setattr(rtx, "_service", preset)
    monkeypatch.setattr(rtx, "_running", running)
    assert outcome(lambda: rtx.fidelity_gate(None, "unknown", None)) == outcome(
        lambda: baseline(rtx, "fidelity_gate")(None, "unknown", None))


@pytest.mark.parametrize("operation", ["evaluate", "pick"])
def test_legacy_context_error_precedes_reading_recipe_or_layers(tmp_path, monkeypatch, operation):
    def context(*args):
        raise rtx.ConfigError("unknown profile")
    monkeypatch.setattr(session.facts, "context", context)
    layers = session.Layers(profile=tmp_path / "missing-layer.json")
    path = tmp_path / "missing-recipe.json" if operation == "evaluate" else [tmp_path]
    args = (None, "unknown", path, layers, mock.Mock(side_effect=AssertionError("facts")))
    expected = outcome(lambda: baseline(session, operation)(*args))
    assert expected == (rtx.ConfigError, "unknown profile")
    assert outcome(lambda: getattr(session, operation)(*args)) == expected


@pytest.mark.parametrize("operation", ["evaluate", "pick"])
@pytest.mark.parametrize("input_kind", ["recipe", "profile", "override"])
@pytest.mark.parametrize("invalid", ["missing", "malformed"])
@pytest.mark.parametrize("backend_kind", ["stub", "core"])
def test_legacy_unreadable_input_matches_ddc5640(tmp_path, operation, input_kind, invalid, backend_kind):
    from hyperloom.recipes import core
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    path = recipes / "legacy.toml"
    path.write_text(test_recipes.PLAIN, encoding="utf-8")
    bad = path if input_kind == "recipe" else tmp_path / "layer.toml"
    if invalid == "malformed":
        bad.write_text("broken [[[", encoding="utf-8")
    else:
        bad.unlink(missing_ok=True)
    layers = session.Layers(**({"profile": bad} if input_kind == "profile" else
                              {"overrides": [bad]} if input_kind == "override" else {}))
    source = session.saved_source(test_recipes.saved_facts(tmp_path / "facts.json"))
    backend = core.StubBackend()
    if backend_kind == "core":
        def run(*a, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="authoritative core input error")
        backend = core.CoreBackend(core.Core("fake", run=run))
    old = baseline(session, operation)
    if operation == "pick":
        old.__globals__["evaluate"] = baseline(session, "evaluate")
    args = (test_recipes.fake_cluster(), "mm", path if operation == "evaluate" else [recipes], layers, source, backend)
    assert outcome(lambda: getattr(session, operation)(*args)) == outcome(lambda: old(*args))


@pytest.mark.parametrize("excluded", [False, True])
def test_devices_document_summary_and_rules_match_baseline(tmp_path, monkeypatch, excluded):
    fixture = test_web_summaries.SummaryTests()
    fixture.setUp()
    try:
        fixture.cluster.machines["workstation"].devices[-1]["excluded"] = excluded
        monkeypatch.setattr(fixture.api, "_cluster", lambda: fixture.cluster)
        fixture.orch.status.return_value = {}
        monkeypatch.setattr(api, "default_config_path", lambda: tmp_path / "config.json", raising=False)
        fixture.api.config_path = tmp_path / "config.json"
        fixture.orch._state_path.return_value = tmp_path / "state.json"
        fixture.orch._known_hosts.return_value = tmp_path / "known-hosts"
        old = baseline(api, "_snapshot")(fixture.api)
        new = fixture.api._snapshot()
        assert new["devices"]["summary"]["excluded_reason"] == old["devices"]["summary"]["excluded_reason"]
        assert new["settings"]["lab_rules"] == old["settings"]["lab_rules"]
    finally:
        fixture.doCleanups()


def test_configured_exclusion_reason_matches_legacy_default():
    card = {"id": "configured", "name": "GPU"}
    declared = [{"id": "configured", "excluded": True}]
    expected = baseline(api, "_accelerator")(card, {}, {}, declared)
    assert device_policy.resolve(card, declared)["policy_reason"] == expected["policy_reason"]


def test_engine_dependent_ingestion_has_no_dead_launch_wrappers():
    from hyperloom.recipes import launch
    assert not hasattr(launch, "ingest_context")
    assert not hasattr(commands, "manifest_store")
    document = (Path(__file__).resolve().parents[1] / "docs/placement-render.md").read_text(encoding="utf-8")
    assert "Engine-dependent open point: context-v1 ingestion" in document


def test_activation_document_matches_rev_21c():
    document = (Path(__file__).resolve().parents[1] / "docs/placement-core.md").read_text(encoding="utf-8")
    assert "Only non-empty `assist` or `components` tables activate placement" in document
    assert "Table presence activates placement" not in document


@pytest.mark.parametrize("table", ["assist", "components"])
def test_10_empty_table_stays_legacy_as_in_tierstream(table):
    # Contract rev 21c: only a non-empty table opts in; an empty one is normalised away (tierstream recipe.hpp:133).
    doc = test_recipes.PlacementSchemaTests.document()
    route = doc["routes"][0]
    for key in ("assist", "components", "max_rows", "max_sequences"):
        route.pop(key, None)
    legacy = reader.parse(json.dumps(doc))
    route[table] = {}
    parsed = reader.parse(json.dumps(doc))
    assert not reader.placement_mode(parsed["routes"][0])
    assert table not in parsed["routes"][0]
    assert parsed == legacy


def test_11_portable_g6_reconstructs_original_frozen_bytes():
    from tests.portable_fixtures import read_fixture
    path = Path(__file__).parent / "fixtures/render/legacy-eb9ae96.json"
    assert hashlib.sha256(read_fixture(path).encode()).hexdigest() == "40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7"
