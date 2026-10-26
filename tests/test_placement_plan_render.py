"""CPU-only plan render regressions."""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom.recipes import commands, content_cache, core, reader, runtime, session

from tests import test_recipes
from tests.placement_fixtures import identity, files
from tests.replica_fake import startup_args


def test_02_actual_core_schema_renders_without_injected_manifest_hash(files):
    artifacts, document = files
    before = artifacts.placement.read_bytes()
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])
    assert runtime.render(artifacts, binary="fake", argv=startup_args(artifacts.placement.parent / "model"),
                          data=data)[2] == document
    assert artifacts.placement.read_bytes() == before
    assert data["engine"] == document["engine"]


def test_02_genuine_core_output_renders_without_modifying_hashed_document(tmp_path, files, identity):
    executable = os.environ.get("HYPERLOOM_TS_RECIPE")
    golden = os.environ.get("HYPERLOOM_PLACEMENT_GOLDEN")
    if not executable or not golden:
        pytest.skip("set HYPERLOOM_TS_RECIPE and HYPERLOOM_PLACEMENT_GOLDEN for genuine CPU output")
    artifacts, _ = files
    golden = Path(golden)
    probe = json.loads((golden / "g4/J2-probe.json").read_bytes())
    probe.update(model="DeepSeek-V4.1-Flash", engine="strix-cuda-ds41-replica")
    probe["devices"][0].update(id=identity, uuid=identity, pci=f"{0:04x}:{1:02x}:{0:02x}.{0}")
    components = json.loads((golden / "g4/J2-components.json").read_bytes())
    components["receipts_sha256"] = hashlib.sha256(artifacts.receipts.read_bytes()).hexdigest()
    backend = core.CoreBackend(core.Core(executable))
    result = backend.place(golden / "g4/J2-recipe.toml", session.Layers(), route="decode_rtx",
                           probe=probe, components=components)
    assert "manifest_sha256" not in result.document
    artifacts.placement.write_bytes(json.dumps(result.document).encode())
    original = artifacts.placement.read_bytes()
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])
    assert runtime.render(artifacts, binary="fake", argv=startup_args(tmp_path / "model"), data=data)[2] == result.document
    assert artifacts.placement.read_bytes() == original


def test_17_corrupt_executable_refuses_in_pure_decision(tmp_path, monkeypatch):
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION)
    binary = tmp_path / "bin" / (data["binary"] + ".exe")
    binary.parent.mkdir()
    good = b"CPU-only engine fixture"
    digest = hashlib.sha256(good).hexdigest()
    binary.write_bytes(good)
    content_cache._write(binary.parent.parent / "MANIFEST.json", {
        "engine_commit": data["engine_sha"], "binaries": {binary.name: digest}})
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    plan = dict(engine=data["engine"], effective_sha256="a" * 64, route="decode", devices=[],
                build_sha256=digest, receipts_sha256="b" * 64)
    cache = SimpleNamespace(record=lambda *a, **kw: dict(reference=dict(build_sha256=digest,
        receipts_sha256="b" * 64), manifest_sha256="c" * 64, selected={"model": "model"}),
        manifests=SimpleNamespace(directory=tmp_path))
    monkeypatch.setattr(runtime, "preset", lambda: data)
    monkeypatch.setattr(runtime, "executable", lambda *args: binary)
    monkeypatch.setattr(runtime, "render", lambda *args, **kw: None)
    monkeypatch.setattr(commands, "components_store", lambda: cache)
    monkeypatch.setattr(commands, "runtime_dir", lambda: tmp_path)
    admitted = dict(inputs={"routes": {"decode": dict(components={}, selected_content={}, receipts="receipts")}},
                    policies={"decode": {}})
    def decide():
        return runtime.decision(None, recipe, SimpleNamespace(pc="local"), {}, "a" * 64,
                                {"decode": plan}, admitted)
    assert decide()["placement"]
    before = set(tmp_path.rglob("*"))
    binary.write_bytes(b"X" * len(good))
    with pytest.raises(reader.RecipeError, match="PL_E_STALE.*binary"):
        decide()
    assert set(tmp_path.rglob("*")) == before
