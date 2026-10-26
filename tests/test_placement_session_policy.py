"""Source policy is explicit and facts are sampled once per policy, including auto selection."""

from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock
import ast
import hashlib
import json
import os
import sys
import uuid

import pytest

from hyperloom import rtx, assets
from hyperloom.recipes import apply, facts, sysmem, probe, topology
from hyperloom.recipes import commands, reader, session, content_cache, core, descriptors, launch, preflight, runtime

from tests import test_recipes
from tests.placement_fixtures import identity
from tests.test_placement_legacy_equality import baseline_text, baseline
from tests.test_recipes_grants import NOW, window


@pytest.mark.parametrize("name", ["NVIDIA RTX PRO 6000 Blackwell", "NVIDIA RTX   PRO 6000 Blackwell"])
@pytest.mark.parametrize("caller", ["pick", "prepare"])
def test_mixed_selection_preserves_real_legacy_probe_and_match(tmp_path, monkeypatch, name, caller):
    cluster = test_recipes.fake_cluster()
    path = tmp_path / "legacy.toml"
    path.write_text(test_recipes.PLAIN, encoding="utf-8")
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        extra = ", 0000:01:00.0, 600, 12.0, WDDM" if "pci.bus_id" in argv[1] else ""
        return SimpleNamespace(returncode=0, stdout=f"{test_recipes.GPU}, {name}, 98304, 0{extra}\n")
    nvidia = probe.nvidia_gpus
    monkeypatch.setattr(probe, "nvidia_gpus", lambda **kw: nvidia(run, topology_reader=lambda _: {}, **kw))
    monkeypatch.setattr(probe, "host_memory", lambda: dict(total_gib=128, free_gib=100, free_commit_gib=120))
    monkeypatch.setattr(topology, "host", lambda: {})
    def node(mid, verb):
        if verb == "gpu":
            return {"checks": {"amd": {"status": "ok", "value": [{
                "product_name": {"status": "ok", "value": "AMD Radeon 8060S"},
                "gtt_total_bytes": {"status": "ok", "value": 120 * rtx.GIB}}]},
                "ram": {"status": "ok", "value": dict(total_bytes=128 * rtx.GIB, available_bytes=124 * rtx.GIB)}}}
        return {"checks": {"interfaces": {"status": "ok", "value": [dict(state="up", negotiated_mbps=2500)]},
                           "usb4": {"layout": {"status": "ok", "value": {}},
                                    "streams": {"status": "ok", "value": {"openable": ["test"]}}}}}
    monkeypatch.setattr(commands, "node_call", lambda _: node)
    monkeypatch.setattr(commands, "cache", lambda: None)
    monkeypatch.setattr(runtime, "preset", lambda: {"engine_sha": None})
    monkeypatch.setattr(launch.render, "router_policies", lambda: ("routes",))
    layers = session.Layers()
    source = commands.live_source(cluster)
    old = baseline(session, "evaluate")(cluster, "mm", path, layers, source)
    assert len(calls) == 1
    assert calls[0][1] == "--query-gpu=uuid,name,memory.total,memory.used"
    assert old.ok == ("   " not in name)
    expected, original_rows = session.pick(cluster, "mm", [tmp_path], layers, source)
    calls.clear()
    doc = test_recipes.PlacementSchemaTests.document()
    doc["match"]["model"] = old.context.model
    (tmp_path / "placement.json").write_text(json.dumps(doc), encoding="utf-8")
    if caller == "pick":
        chosen, rows = session.pick(cluster, "mm", [tmp_path], layers, source)
        assert rows[0] == original_rows[0]
        assert (chosen is None) == (expected is None)
        if chosen is not None:
            assert chosen.summary() == old.summary()
    else:
        monkeypatch.setattr(launch, "_backend", lambda _: (core.StubBackend(), False))
        options = launch.Options("auto", layers, dirs=[tmp_path], exact_dirs=True)
        if old.ok:
            prepared = launch.prepare(cluster, "mm", options, router="local", vram_cap_gib=None, state_dir=tmp_path)
            assert prepared.evaluation.summary() == old.summary()
        else:
            with pytest.raises(reader.RecipeError, match="no recipe fits"):
                launch.prepare(cluster, "mm", options, router="local", vram_cap_gib=None, state_dir=tmp_path)
    assert len(calls) == 2  # One legacy gather and one placement gather, with no policy override.
    assert sum(argv[1] == "--query-gpu=uuid,name,memory.total,memory.used" for argv in calls) == 1


def test_stale_placement_profile_gathers_separate_legacy_fallback(tmp_path, monkeypatch):
    path = tmp_path / "legacy.toml"
    path.write_text(test_recipes.PLAIN, encoding="utf-8")
    profile = tmp_path / "profile.toml"
    profile.write_text(test_recipes.profile_text(recipe_sha="0" * 64, set_lines=(
        '"routes.decode_pair.components.drafter.device" = "pair"',)), encoding="utf-8")
    cluster = test_recipes.fake_cluster()
    legacy_source = session.saved_source(test_recipes.saved_facts(tmp_path / "facts.json"))
    backend = core.CoreBackend(core.Core("fake"))
    backend.resolve = core.StubBackend().resolve
    backend.match = lambda *a: ([], test_recipes.BINDING, "a" * 64)
    backend.select = lambda path, layers, resolved, state, sha, identity: reader.select(resolved.recipe, state, sha)
    monkeypatch.setattr(session.devices, "candidates", lambda *a: test_recipes.FACTS)
    policies = []
    def source(ctx, *, policy=(False, False)):
        policies.append(policy)
        inventory, gathered, raw, notes = deepcopy(legacy_source(ctx))
        if any(policy):
            gathered["placement_sample"] = True
        return inventory, gathered, raw, notes
    original = session._legacy_evaluate
    samples = []
    def legacy(*args, **kw):
        sample = args[4](facts.context(cluster, "mm"))
        samples.append(sample)
        return original(*args, **kw)
    monkeypatch.setattr(session, "_legacy_evaluate", legacy)
    result = session.evaluate(cluster, "mm", path, session.Layers(profile=profile), source, backend)
    assert result.effective.untuned
    assert policies == [(False, True), (False, False)]
    assert "placement_sample" not in samples[0][1]


def test_auto_samples_once_for_explicit_policy_without_callable_attributes(tmp_path, monkeypatch):
    for name in ("a", "b"):
        document = test_recipes.PlacementSchemaTests.document()
        document["name"] = name
        (tmp_path / (name + ".json")).write_text(json.dumps(document), encoding="utf-8")
    ctx = SimpleNamespace(model="synthetic", profile="decode")
    monkeypatch.setattr(session.facts, "context", lambda *a: ctx)
    def evaluation(cluster, path, layers, backend, ctx, base, *args, **kwargs):
        return SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=base))
    monkeypatch.setattr(session, "_placement_evaluation", evaluation)

    class Source:
        __slots__ = ("policies",)
        def __init__(self):
            self.policies = []
        def __call__(self, ctx, *, policy=(False, False)):
            self.policies.append(policy)
            return {}, {}, {}, []
    source = Source()
    chosen, rows = session.pick(None, "decode", [tmp_path], session.Layers(), source)
    assert chosen is not None
    assert all(row["result"] == "passes" for row in rows)
    assert source.policies == [(True, False)]


def test_live_source_accepts_policy_without_mutation(monkeypatch):
    gathered = []
    monkeypatch.setattr(commands.facts, "gather", lambda *a, **kw: gathered.append(kw))
    monkeypatch.setattr(commands, "cache", lambda: None)
    source = commands.live_source(None)
    from hyperloom.recipes import runtime
    monkeypatch.setattr(runtime, "preset", lambda: {"engine_sha": None})
    source(SimpleNamespace(pc="local"), policy=(False, True))
    assert gathered[0]["placement"] is True
    assert not hasattr(source, "placement")
    assert not hasattr(source, "identity")


def test_legacy_evaluator_and_picker_are_ddc5640_code():
    original = ast.parse(baseline_text(session))
    import inspect
    for old_name, new_name in (("evaluate", "_legacy_evaluate"), ("pick", "_legacy_pick")):
        old = next(n for n in original.body if isinstance(n, ast.FunctionDef) and n.name == old_name)
        new = ast.parse(inspect.getsource(getattr(session, new_name))).body[0]
        new.name = old_name
        assert ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False)


def test_05_invalid_auto_candidate_does_not_abort_detection(tmp_path):
    (tmp_path / "broken.toml").write_bytes(b"broken [[[")
    (tmp_path / "valid.toml").write_bytes(test_recipes.DS.encode())
    assert preflight.includes(launch.Options("auto", dirs=[tmp_path])) is False
    parsed = reader.parse(test_recipes.DS)
    passing = SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=parsed))
    with mock.patch.object(session.facts, "context", return_value=SimpleNamespace(model=parsed["match"]["model"])), \
            mock.patch.object(session, "evaluate", return_value=passing):
        chosen, candidates = session.pick(None, "legacy", [tmp_path], session.Layers(), lambda _: None)
    assert chosen is passing
    assert candidates[0]["result"].startswith("invalid:")


def test_09_live_placement_source_resolves_release_executable(tmp_path):
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])
    cluster = SimpleNamespace(machines={"local": SimpleNamespace(install_root=str(tmp_path))})
    with mock.patch.object(runtime, "preset", return_value=data), \
            mock.patch.object(commands.facts, "gather", return_value=({}, {}, {}, [])) as gather:
        commands.live_source(cluster, placement=True)(SimpleNamespace(pc="local"))
    assert data["engine_sha"][:12] in str(gather.call_args.kwargs["release_executable"])


def test_09_auto_shared_source_collects_policy_for_placement_candidates(tmp_path):
    doc = test_recipes.PlacementSchemaTests.document()
    path = tmp_path / "placement.json"
    path.write_bytes(json.dumps(doc).encode())
    source = mock.Mock(return_value=({}, {}, {}, []))
    ctx = SimpleNamespace(model="synthetic")
    def evaluate(cluster, profile, path, layers, once, backend):
        once(ctx, policy=(True, False))
        return SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=reader.parse(Path(path).read_text())))
    with mock.patch.object(session.facts, "context", return_value=ctx), \
            mock.patch.object(session, "evaluate", side_effect=evaluate):
        session.pick(None, "decode", [tmp_path], session.Layers(), source)
    source.assert_called_once_with(ctx, policy=(True, False))


@pytest.mark.parametrize("layer_kind", ["sets", "overrides", "profile", "stale-profile", "legacy"])
@pytest.mark.parametrize("selection", ["direct", "auto"])
def test_16_effective_layer_selects_placement_and_policy(tmp_path, monkeypatch, layer_kind, selection):
    doc = test_recipes.PlacementSchemaTests.document()
    route = doc["routes"][0]
    for key in ("assist", "components", "max_rows", "max_sequences"):
        route.pop(key, None)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    sets = [("routes.decode.components.drafter.device", "anchor")]
    layers = session.Layers(sets=sets)
    if layer_kind == "overrides":
        override = tmp_path / "override.json"
        override.write_text(json.dumps({"schema": reader.OVERRIDES_SCHEMA, "recipe": doc["name"],
                                        "set": dict(sets)}), encoding="utf-8")
        layers = session.Layers(overrides=[override])
    binding = {"anchor": [dict(id="synthetic-anchor", host="local")], "aux": []}
    report = dict(engine_sha256="a" * 64, model_sha256="b" * 64, devices=[dict(
        id="synthetic-anchor", driver="test", disk_read_gbytes_ps=10, h2d_gbytes_ps=20)])
    descriptors = {"anchor": ["c" * 64]}
    if layer_kind in ("profile", "stale-profile"):
        profile = dict(schema=reader.PROFILE_SCHEMA, recipe=doc["name"], workload="default",
            recipe_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), key=dict(
                engine_sha256=report["engine_sha256"], model_sha256=report["model_sha256"],
                devices={"anchor": ["synthetic-anchor"], "aux": {"unbound": True}},
                drivers={"anchor": "test", "aux": {"unbound": True}}, descriptors=descriptors),
            probe={"anchor": dict(disk_read_gbytes_ps=10, h2d_gbytes_ps=20), "aux": {"unbound": True}},
            set=dict(sets))
        if layer_kind == "stale-profile":
            profile["key"]["engine_sha256"] = "d" * 64
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        layers = session.Layers(profile=profile_path)
    elif layer_kind == "legacy":
        layers = session.Layers(sets=[("routes.decode.placement.anchor.device_gib", 89)])
    backend = core.CoreBackend(core.Core("fake"))
    backend.resolve = core.StubBackend().resolve
    backend.match = mock.Mock(return_value=([], binding, "a" * 64))
    ctx = SimpleNamespace(pc="local", profile="decode", model="synthetic")
    monkeypatch.setattr(session.facts, "context", lambda *args: ctx)
    legacy_bind = mock.Mock(return_value={"binding": binding})
    if layer_kind == "profile":
        legacy_bind.side_effect = AssertionError("CUDA-only profile activation must not use legacy HIP binding")
    monkeypatch.setattr(session.apply, "bind", legacy_bind)
    monkeypatch.setattr(session.devices, "candidates", lambda *args: report)
    monkeypatch.setattr(session.apply, "probe_report", lambda *args, **kw: report)
    monkeypatch.setattr(session.apply, "descriptors", lambda *args: descriptors)
    monkeypatch.setattr(session.facts, "availability", lambda *args: {})
    monkeypatch.setattr(session.apply, "deploy", lambda *args, **kw: {})
    source = mock.Mock(return_value=({}, {}, {}, []))
    observed = []
    source.side_effect = lambda _, policy=(False, False): (observed.append(policy) or ({}, {}, {}, []))
    chosen = SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=reader.parse(json.dumps(doc))))
    placement = mock.Mock(return_value=chosen)
    monkeypatch.setattr(session, "_placement_evaluation", placement)
    if selection == "auto":
        result, _ = session.pick(None, "decode", [tmp_path], layers, source, backend)
    else:
        result = session.evaluate(None, "decode", path, layers, source, backend)
    if layer_kind in ("stale-profile", "legacy"):
        placement.assert_not_called()
        assert observed == ([(False, True), (False, False)] if layer_kind == "stale-profile" else [(False, False)])
        assert result.decision == {}
        assert result.effective.untuned == (layer_kind == "stale-profile")
        return
    assert result is chosen
    assert observed == ([(False, True), (True, True)] if layer_kind == "profile" else [(True, False)])
    assert reader.placement_mode(placement.call_args.args[5]["routes"][0])


def test_16_verified_live_identity_activates_profile_through_real_core(tmp_path, monkeypatch):
    executable = os.environ.get("HYPERLOOM_TS_RECIPE")
    golden = os.environ.get("HYPERLOOM_PLACEMENT_GOLDEN")
    if not executable or not golden:
        pytest.skip("set HYPERLOOM_TS_RECIPE and HYPERLOOM_PLACEMENT_GOLDEN for real CPU admission")
    components = json.loads((Path(golden) / "g4/J2-components.json").read_bytes())
    binary = tmp_path / "bin" / "replica.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"fake release executable")
    engine_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    content_cache._write(tmp_path / "MANIFEST.json", {
        "engine_commit": runtime.replica_args.AUDITED_REVISION, "binaries": {binary.name: engine_sha}})
    model = tmp_path / "model"
    model.write_bytes(b"fake model")
    model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
    stat = model.stat()
    entry = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino, dev=stat.st_dev,
                 sha256=model_sha, verified_at=1)
    monkeypatch.setattr(assets, "read_manifest", lambda: {"local": {"ds41_model": entry}})
    gpu_uuid = "GPU-" + str(uuid.uuid4())
    gpu = dict(uuid=gpu_uuid, name="NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
               memory_gib=128, driver="617.14", pci="0000:01:00.0",
               arch="sm_120", platform="windows-wddm", h2d_gbytes_ps=20, disk_read_gbytes_ps=10)
    cluster = SimpleNamespace(machines={"local": SimpleNamespace(devices=[dict(
        id="gpu", type="nvidia", uuid=gpu_uuid)])}, assets={"local": {"ds41_model": dict(
            path=str(model), bytes=stat.st_size, sha256=model_sha)}})
    ctx = apply.Context("test", "deepseek-v4.1-flash", "single", [], "local",
                        descriptors={gpu_uuid: "c" * 64})
    monkeypatch.setattr(facts, "context", lambda *a: ctx)
    doc = test_recipes.PlacementSchemaTests.document()
    doc["match"].update(model=ctx.model)
    doc["match"]["devices"][0]["memory_min_gib"] = 128
    doc["routes"][0]["placement"]["anchor"].update(
        device_gib=120, max_context_tokens=131072, margin_gib=0.6, margin_fraction=0)
    for key in ("assist", "components", "max_rows", "max_sequences"):
        doc["routes"][0].pop(key, None)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    profile = dict(schema=reader.PROFILE_SCHEMA, recipe=doc["name"], workload="default",
        recipe_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), key=dict(
            engine_sha256=engine_sha, model_sha256=model_sha,
            devices={"anchor": [gpu_uuid], "aux": {"unbound": True}},
            drivers={"anchor": gpu["driver"], "aux": {"unbound": True}},
            descriptors={"anchor": ["c" * 64]}),
        probe={"anchor": dict(disk_read_gbytes_ps=10, h2d_gbytes_ps=20), "aux": {"unbound": True}},
        set={"routes.decode.components.drafter.device": "anchor", "routes.decode.max_rows": 6})
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(facts.probe, "nvidia_gpus", lambda **kw: [gpu])
    monkeypatch.setattr(facts.probe, "host_memory", lambda: dict(total_gib=128, free_gib=100, free_commit_gib=120))
    monkeypatch.setattr(sysmem, "probe", lambda _: "prefer_no_sysmem_fallback")
    monkeypatch.setattr(commands, "cache", lambda: None)
    monkeypatch.setattr(runtime, "preset", lambda: dict(engine_sha=runtime.replica_args.AUDITED_REVISION))
    monkeypatch.setattr(runtime, "executable", lambda *a: binary)
    source = commands.live_source(cluster)
    backend = core.CoreBackend(core.Core(executable))
    from tests.test_recipes_grants import NOW, window
    cluster.pc = {"cuda": {"grants": [dict(uuid=gpu_uuid, vram_cap_gib=120, approval="test", window=window(),
                                         gate=[sys.executable, "fake-gate"])],
                            "host_grant": dict(host_gib=74, approval="test", window=window())}}
    cache = mock.Mock()
    cache.load.return_value = components
    monkeypatch.setattr(commands, "components_store", lambda: cache)
    monkeypatch.setattr(preflight.time, "time", lambda: NOW)
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    monkeypatch.setattr(rtx.secrets, "runtime_dir", lambda: tmp_path / "runtime")
    observer = mock.Mock()
    observer.sample.return_value = {gpu_uuid.lower(): 0}
    monkeypatch.setattr(preflight.gpu_counters, "Observer", lambda: observer)
    monkeypatch.setattr(preflight.procs, "check_port", lambda *a: None)
    place = mock.Mock(wraps=backend.place)
    monkeypatch.setattr(backend, "place", place)
    monkeypatch.setattr(apply, "refuse_unsupported", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "decision", lambda *a, **kw: {"placement": True})
    result = session.evaluate(cluster, "test", path, session.Layers(profile=profile_path), source, backend,
        placement_inputs={"routes": {"decode": {"components": {"cached": True}}}})
    assert result.refusal is None, result.refusal
    assert not result.effective.untuned, result.effective.refused
    assert result.decision == {"placement": True}
    place.assert_called_once()
    identity = place.call_args.kwargs["identity"]
    assert identity["workload"] == "default"
    assert identity["descriptors"] == {"anchor": ["c" * 64], "aux": []}
    observer.sample.assert_called_once()
    cache.load.assert_called_once()
    assert result.report["engine_sha256"] == engine_sha
    assert result.report["model_sha256"] == model_sha
    # Same-size model edits and changed executable bytes must not keep the verified identity.
    model.write_bytes(b"FAKE model")
    binary.write_bytes(b"corrupt release executable")
    _, gathered, _, _ = source(ctx)
    assert "engine_sha256" not in gathered
    assert "model_sha256" not in gathered
