"""CPU-only decision regressions."""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import json

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import procs
from hyperloom.recipes import commands, content_cache, core, preflight, reader, runtime, session
from hyperloom.recipes.reader import RecipeError

from tests import test_recipes, test_placement_render
from tests.placement_fixtures import identity, files


def test_01_published_evaluation_keeps_admitted_launch_decision(tmp_path, identity):
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(test_recipes.PlacementSchemaTests.document()), encoding="utf-8")
    bound = {"anchor": [{"id": identity, "host": "local"}], "aux": []}
    ctx = SimpleNamespace(pc="local", nodes=[], service=None, profile="decode")
    resolved = core.Resolved(recipe, "a" * 64, {"fields": []}, False, [])
    backend = core.CoreBackend(core.Core("fake"))
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])
    decision = {"placement": True, "service": {"start": True, "why": None}}
    with mock.patch.object(session.facts, "context", return_value=ctx), \
            mock.patch.object(session, "resolve", return_value=resolved), \
            mock.patch.object(backend, "match", return_value=([], bound, "a" * 64)), \
            mock.patch.object(session.apply, "probe_report", return_value={}), \
            mock.patch.object(session.apply, "descriptors", return_value={}), \
            mock.patch.object(session.facts, "availability", return_value={}), \
            mock.patch.object(preflight, "check", return_value={}), \
            mock.patch.object(runtime, "preset", return_value=data), \
            mock.patch.object(runtime, "decision", return_value=decision, create=True):
        result = session.evaluate(SimpleNamespace(pc={}), "decode", path, session.Layers(),
                                  lambda _, **kw: ({}, {}, {}, []), backend)
    assert result.refusal is None
    assert result.decision == decision


@pytest.mark.parametrize("flags,phase,rows,valid", [
    ({"--predict": 8}, "chats9", 1, False), ({"--predict": 2}, "chats8", 1, True),
    ({"--predict": 9}, "chats9", 1, True), ({"--predict": 33}, "chats0", 2, False),
    ({"--predict": 32}, "chats32", 2, True), ({}, "chats9", 1, False)])
def test_3_cached_predict_limit_survives_decision_serialization(monkeypatch, flags, phase, rows, valid):
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        fixture.document["config"] = dict(max_rows=rows)
        fixture.write()
        files = dict(placement=str(fixture.files.placement), manifest=str(fixture.files.manifest),
                     receipts=str(fixture.files.receipts), manifest_sha256=None, document=fixture.document,
                     allocation_flags=flags)
        decision = json.loads(json.dumps(dict(machine="local", files=files, supervision={}, argv=[
            *fixture.argv, "--phases", phase])))
        monkeypatch.setattr(runtime, "preset", lambda: fixture.data)
        request = runtime.request(decision)
        if valid:
            runtime.render(request.files, binary="fake", argv=request.argv)
        else:
            with pytest.raises(RecipeError, match="PL_E_SCHEMA.*(?:phases|predict)"):
                runtime.render(request.files, binary="fake", argv=request.argv)
    finally:
        fixture.doCleanups()


def test_3_decision_uses_the_admitted_allocation_reference(monkeypatch):
    from hyperloom.recipes import commands, content_cache

    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        rid, sha, build = "decode", "a" * 64, "b" * 64
        plan = fixture.document
        plan.update(route=rid, effective_sha256=sha, build_sha256=build, config=dict(max_rows=1))
        for device in plan["devices"]:
            device["host"] = "local"
        manifest_sha = content_cache._sha(fixture.manifest)
        (fixture.root / (manifest_sha + ".json")).write_bytes(fixture.files.manifest.read_bytes())
        reference = dict(build_sha256=build, receipts_sha256=plan["receipts_sha256"], flags={"--predict": 8})
        record = dict(reference=reference, manifest_sha256=manifest_sha, selected=dict(model=fixture.argv[0]))
        cache = mock.Mock(manifests=SimpleNamespace(directory=fixture.root))
        cache.record.return_value = record
        argv = [*fixture.argv[1:], "--phases", "chats9"]
        admitted = dict(inputs=dict(routes={rid: dict(components=reference, receipts=str(fixture.files.receipts),
                                                    selected_content=record["selected"], argv=argv)}))
        recipe = dict(routes=[dict(id=rid, phase="decode", components=dict(arena=dict(units=1)))])
        monkeypatch.setattr(runtime, "preset", lambda: fixture.data)
        monkeypatch.setattr(commands, "components_store", lambda: cache)
        monkeypatch.setattr(commands, "runtime_dir", lambda: fixture.root / "unpublished")
        monkeypatch.setattr(runtime, "executable", lambda *a: Path("fake"))
        before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        with mock.patch.object(runtime.Files, "publish", side_effect=AssertionError("publish")), \
                mock.patch.object(procs, "LocalChild", side_effect=AssertionError("child")), \
                pytest.raises(RecipeError, match="PL_E_SCHEMA.*cached --predict"):
            runtime.decision(None, recipe, SimpleNamespace(pc="local"), None, sha, {rid: plan}, admitted)
        cache.record.assert_called_once_with(reference, selected=record["selected"])
        assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        assert not (fixture.root / "unpublished").exists()
    finally:
        fixture.doCleanups()
