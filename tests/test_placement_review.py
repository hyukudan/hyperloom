"""CPU-only regressions for the fourteen placement review findings."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import time
from unittest import mock

import pytest

from hyperloom import placement_supervision, rtx
from hyperloom.recipes import commands, content_cache, core, descriptors, grants, launch, measurements
from hyperloom.recipes import preflight, reader, runtime, session, solver
from tests import test_recipes
from tests.test_recipes_grants import NOW, window


def grant(identity, cap=90):
    return dict(uuid=identity, vram_cap_gib=cap, approval="test", window=window(), gate=[sys.executable, "fake"])


@pytest.fixture
def identity():
    import uuid
    return "GPU-" + str(uuid.uuid4())


def test_01_published_evaluation_keeps_admitted_launch_decision(tmp_path, identity):
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    bound = {"anchor": [{"id": identity, "host": "local"}], "aux": []}
    ctx = SimpleNamespace(pc="local", nodes=[], service=None, profile="decode")
    resolved = core.Resolved(recipe, "a" * 64, {"fields": []}, False, [])
    backend = core.CoreBackend(core.Core("fake"))
    data = dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])
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
        result = session.evaluate(SimpleNamespace(pc={}), "decode", "fake", session.Layers(),
                                  lambda _: ({}, {}, {}, []), backend)
    assert result.refusal is None
    assert result.decision == decision


@pytest.fixture
def files(tmp_path, identity):
    model = tmp_path / "model"
    model.write_bytes(b"model")
    stat = model.stat()
    manifest = {"schema": content_cache.MANIFEST_SCHEMA, "files": [dict(path=str(model), size=stat.st_size,
        mtime_ns=stat.st_mtime_ns, sha256="a" * 64, header_sha256="b" * 64)]}
    receipts = b'{"schema":"tierstream/receipts-v1"}\n'
    document = dict(schema="tierstream/placement-v1", engine="strix-cuda-ds41-replica",
                    receipts_sha256=hashlib.sha256(receipts).hexdigest(),
                    devices=[dict(kind="cuda", uuid=identity, pci=f"{0:04x}:{1:02x}:{0:02x}.{0}",
                                  role="anchor", budget_bytes=10 * rtx.GIB)])
    result = runtime.Files(*(tmp_path / n for n in ("placement.json", "manifest.json", "receipts.json")))
    result.placement.write_bytes(json.dumps(document).encode())
    result.manifest.write_bytes(json.dumps(manifest).encode())
    result.receipts.write_bytes(receipts)
    return result, document


def test_02_actual_core_schema_renders_without_injected_manifest_hash(files):
    artifacts, document = files
    before = artifacts.placement.read_bytes()
    data = dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])
    assert runtime.render(artifacts, binary="fake", argv=["model"], data=data)[2] == document
    assert artifacts.placement.read_bytes() == before
    assert data["engine"] == document["engine"]


def test_03_future_big_window_bounds_4090_turn(identity):
    entry = grant(identity)
    policy = dict(grants=[entry], caps_gib={identity.lower(): 90}, host_pinned_gib=0, host_pageable_gib=0,
                  free_commit_floor_gib=4, pc_big_deadline=NOW + 120)
    with pytest.raises(rtx.ServiceError, match="PC-BIG"):
        placement_supervision.validate(policy, now=NOW + 120)


def test_04_legacy_mtime_cache_loads_unchanged(tmp_path):
    import subprocess
    # Execute the actual merge-base writer, including its JSON formatting and integer-second stat identity.
    namespace = dict(__name__="hyperloom.recipes.baseline_descriptors", __package__="hyperloom.recipes")
    baseline = subprocess.run(["git", "show", "ddc5640:hyperloom/recipes/descriptors.py"],
                              check=True, capture_output=True, text=True).stdout
    exec(compile(baseline, "ddc5640:descriptors.py", "exec"), namespace)
    model = tmp_path / "model"
    model.write_bytes(b"model")
    os.utime(model, ns=(1700000000250000000, 1700000000250000000))
    legacy = namespace["Store"](tmp_path / "cache")
    store = descriptors.Store(tmp_path / "cache")
    with mock.patch.object(descriptors.modelfiles, "read", return_value=({"legacy": True}, [])):
        legacy.save(model)
        original = store._path(model).read_bytes()
        assert store.load(model) == ({"legacy": True}, [])
        store.save(model)
        assert store._path(model).read_bytes() == original
    # Baseline deliberately treats same-second changes as unchanged; the next second invalidates it.
    os.utime(model, ns=(1700000000750000000, 1700000000750000000))
    assert store.load(model) == ({"legacy": True}, [])
    os.utime(model, ns=(1700000001250000000, 1700000001250000000))
    with pytest.raises(descriptors.DescriptorError, match="changed"):
        store.load(model)


def test_04_strict_placement_headers_have_a_separate_versioned_namespace(tmp_path):
    model = tmp_path / "model"
    model.write_bytes(b"model")
    legacy = descriptors.Store(tmp_path / "cache")
    strict = descriptors.PlacementStore(tmp_path / "cache")
    with mock.patch.object(descriptors.modelfiles, "read", return_value=({}, [])):
        legacy.save(model)
        strict.save(model)
    assert legacy._path(model) != strict._path(model)
    old = content_cache._read(legacy._path(model), "PL_E_STALE")
    precise = content_cache._read(strict._path(model), "PL_E_STALE")
    assert "mtime" in old["files"][0] and "schema" not in old
    assert precise["schema"] == strict.schema and "mtime_ns" in precise["files"][0]
    stat = model.stat()
    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100))
    with pytest.raises(descriptors.DescriptorError, match="changed"):
        strict.load(model)


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


def test_06_unmatched_4090_and_matched_policy_agree_across_views(identity):
    from hyperloom.web import api
    from hyperloom.web.features import machines
    raw = dict(uuid=identity, id=identity, name="NVIDIA RTX 4090")
    declared = [dict(uuid="other", name=raw["name"], excluded=False)]
    assert api._accelerator(raw, {}, {}, declared)["excluded"]
    declared[0]["uuid"] = identity
    assert not machines.excluded_card(raw, declared)
    for card, configured in ((dict(raw, device=identity[:12]), {"id": identity, "excluded": False}),
                             (dict(name=raw["name"], device=identity), {"uuid": identity, "excluded": False}),
                             (dict(name=raw["name"], pci="bus-test"), {"pci": "bus-test", "excluded": False})):
        assert not machines.excluded_card(card, [configured])
        assert not api._accelerator(card, {}, {}, [configured])["excluded"]


def test_07_absent_positive_optional_assist_consumes_no_grant(identity):
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    recipe["routes"][0]["components"]["drafter"]["device"] = "auto"
    cluster = SimpleNamespace(pc={"cuda": {"grants": [grant(identity)], "host_grant": dict(
        host_gib=74, approval="test", window=window())}})
    binding = {"anchor": [dict(id=identity, host="local")], "aux": []}
    report = {"devices": [dict(id=identity, uuid=identity, name="anchor", kind="cuda")]}
    used = grants.check(cluster, recipe, binding, report, {"local": {"free_ram_gib": 100}}, now=NOW)
    assert set(used) == {identity.lower()}


def test_08_manifest_command_connects_core_runner(tmp_path):
    args = SimpleNamespace(xfer=False, store=False, uuid=None, xfer_sha256=None, store_id=None,
                           file=["model"], window={})
    with mock.patch.object(commands, "runtime_dir", return_value=tmp_path), \
            mock.patch.object(measurements, "acquire", return_value={}) as acquire:
        commands.measure_command(SimpleNamespace(), args)
    assert callable(acquire.call_args.kwargs.get("manifest_runner"))


def test_09_live_placement_source_resolves_release_executable(tmp_path):
    data = dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])
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
    source.placement = False
    ctx = SimpleNamespace(model="synthetic")
    def evaluate(cluster, profile, path, layers, once, backend):
        once(ctx)
        return SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=reader.parse(Path(path).read_text())))
    with mock.patch.object(session.facts, "context", return_value=ctx), \
            mock.patch.object(session, "evaluate", side_effect=evaluate):
        session.pick(None, "decode", [tmp_path], session.Layers(), source)
    assert source.placement is True
    source.assert_called_once()


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


def test_11_all_committed_artifacts_pass_privacy_without_exceptions():
    from tests.test_placement_privacy import findings
    assert findings() == {}


def test_12_solver_snapshots_matching_measurements(tmp_path, identity):
    from tests.test_placement_solver import SolverTests
    helper = SolverTests()
    helper.setUp()
    try:
        bundle = json.loads(helper.bundle.read_bytes())
        key = measurements.key(identity, "600", "a" * 64, "store")
        bundle["measurement_keys"] = {"anchor": key}
        bundle["probe"]["devices"] = [dict(id=identity, uuid=identity, driver="600")]
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        measured = measurements.median([dict(h2d_mbps=1, d2h_mbps=2, h2d_lat_ns=3)] * 9,
                                        measurements.XFER_METHOD)
        measurements.Cache(helper.root).save(key, "xfer", measured)
        result = helper.solve()
        record = content_cache._read(helper.root / "solver-plans" / (result["selection"] + ".json"), "PL_E_STALE")
        assert record["snapshot"]["request"]["probe"]["probe_xfer"]["anchor"]["h2d_mbps"] == 1
    finally:
        helper.doCleanups()


def test_13_concurrent_cache_publication_is_atomic_and_cleans_temps(tmp_path):
    path = tmp_path / "cache.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: content_cache._write(path, {"value": i}), range(64)))
    assert type(json.loads(path.read_bytes())["value"]) is int
    assert list(tmp_path.iterdir()) == [path]


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
    with mock.patch.object(runtime, "preset", return_value=dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])), \
            mock.patch.object(runtime, "render", return_value=(["fake"], {}, document)), \
            mock.patch.object(rtx.time, "time", return_value=NOW):
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


def test_01_evaluate_prepare_up_and_real_watchdog_with_fake_child(tmp_path, files, identity, monkeypatch):
    from hyperloom import gpu_counters, orchestrate, secrets
    from tests.replica_fake import FakeReplica as FakeChild

    artifacts, document = files
    data = dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])
    recipe_doc = test_recipes.PlacementSchemaTests.document()
    recipe_doc["routes"][0]["components"]["drafter"]["device"] = "auto"
    path = tmp_path / "recipe.json"
    path.write_bytes(json.dumps(recipe_doc).encode())
    model = tmp_path / "model"
    receipts_sha = hashlib.sha256(artifacts.receipts.read_bytes()).hexdigest()
    build_sha = hashlib.sha256(b"fake CPU-only engine").hexdigest()
    ref = content_cache.reference(build_sha, {}, receipts_sha)
    component = dict(schema=content_cache.COMPONENT_SCHEMA, build_sha256=build_sha, receipts_sha256=receipts_sha)
    cache = content_cache.ComponentsStore(tmp_path)
    manifest_sha = cache.manifests.save(json.loads(artifacts.manifest.read_bytes()))
    selected = {"model": str(model)}
    cache.save(ref, component, manifest=manifest_sha, selected=selected)
    document.update(build_sha256=build_sha, effective_sha256="d" * 64, route="decode")
    document["devices"][0].update(set="anchor", host="local", name="test device", budget_bytes=90 * rtx.GIB)
    document["host"] = dict(pinned_budget_bytes=68 * rtx.GIB, pageable_budget_bytes=2 * rtx.GIB)
    binary = Path(tmp_path / "cuda" / data["engine_sha"][:12] / "bin" / (data["binary"] + ".exe"))
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake CPU-only engine")
    content_cache._write(binary.parent.parent / "MANIFEST.json", dict(engine_commit=data["engine_sha"],
        binaries={binary.name: hashlib.sha256(binary.read_bytes()).hexdigest()}))
    entry = grant(identity)
    cluster = SimpleNamespace(pc={"cuda": {"grants": [entry], "host_grant": dict(
        host_gib=74, approval="test", window=window())}}, machines={"local": SimpleNamespace(
        id="local", os="windows", access="local", install_root=str(tmp_path), devices=[dict(
            id="anchor", uuid=identity, type="nvidia", excluded=False)])})
    ctx = session.apply.Context("decode", "synthetic", "replicas", [], "local", limits={})
    row = dict(uuid=identity, kind="cuda", name="test device", memory_gib=96, arch="sm_120",
               driver="600", platform="windows-wddm", pci=document["devices"][0]["pci"])
    raw = dict(hosts={"local": dict(free_ram_gib=100, free_commit_gib=120)}, devices={identity: {}},
               lan={}, usb4={}, sysmem_policy="prefer_no_sysmem_fallback")
    source = lambda _: ({"cuda": [row]}, {"model": "synthetic", "devices": {identity: row}, "links": []}, raw, [])
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1] == "place"
        assert json.loads(Path(argv[argv.index("--components") + 1]).read_bytes()) == component
        Path(argv[argv.index("--json-out") + 1]).write_bytes(json.dumps(document).encode())
        return SimpleNamespace(returncode=0, stdout="admitted", stderr="")
    backend = core.CoreBackend(core.Core("fake CPU core", run=run))
    backend.resolve = core.StubBackend().resolve
    binding = {"anchor": [dict(id=identity, host="local")], "aux": []}
    monkeypatch.setattr(backend, "match", lambda *args: ([], binding, "d" * 64))
    monkeypatch.setattr(session.facts, "context", lambda *args: ctx)
    monkeypatch.setattr(runtime, "preset", lambda: data)
    monkeypatch.setattr(commands, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(commands, "live_source", lambda *args, **kw: source)
    monkeypatch.setattr(commands, "components_store", lambda: cache)
    monkeypatch.setattr(launch, "_backend", lambda _: (backend, True))
    monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
    monkeypatch.setattr(rtx.time, "time", lambda: NOW)
    values = {identity.lower(): 0}
    observer = mock.Mock()
    observer.sample.side_effect = lambda pid: dict(values)
    monkeypatch.setattr(gpu_counters, "Observer", lambda: observer)
    dog = placement_supervision.Watchdog
    monkeypatch.setattr(placement_supervision, "Watchdog", lambda *a, **kw: dog(*a, clock=lambda: NOW, **kw))
    inputs = {"routes": {"decode": dict(components=ref, selected_content=selected, receipts=str(artifacts.receipts))}}
    options = launch.Options(str(path), placement_inputs=inputs)
    prepared = launch.prepare(cluster, "decode", options, router="local", vram_cap_gib=None, state_dir=tmp_path)
    assert prepared.evaluation.ok and prepared.evaluation.decision["placement"]
    assert prepared.placement_request.supervision["caps_bytes"] == {identity.lower(): 90 * rtx.GIB}
    assert not prepared.placement_request.files.placement.exists()  # pure admission never publishes
    state_path = tmp_path / "up.json"
    monkeypatch.setattr(orchestrate, "check_launch", lambda *a, **k: ({}, "fake", [], None, "fake", False, [], []))
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: state_path)
    monkeypatch.setattr(secrets, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(secrets, "ensure", lambda _: "a" * 64)
    gates, locks, closes, threads = [], [], [], []
    def device_lock(key):
        locks.append(key)
        item = mock.Mock()
        item.acquire.return_value = item
        item.close.side_effect = lambda: closes.append(key)
        return item
    FakeChild.instances = []
    def detached(argv, logfile):
        thread = threading.Thread(target=rtx.service_run, args=(argv[-1],), kwargs=dict(
            child_factory=FakeChild, verify=lambda u: {"uuid": u}, lock_factory=device_lock,
            gate_runner=lambda args: gates.append(args), observer_factory=lambda: observer,
            health=lambda port: {"status": "ok"}, memory=lambda: dict(free_physical_gib=100, free_commit_gib=120)))
        threads.append(thread)
        thread.start()
        return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)
    monkeypatch.setattr(orchestrate, "_detached", detached)
    try:
        state = orchestrate.up(cluster, "decode", recipe=options)
        assert state["phase"] == "running"
        assert gates == [entry["gate"]] and locks == [identity.lower()]
        assert orchestrate.endpoint("decode")["protocol"] == "ds41-replica-spool-v1"
        assert len(calls) == 2
        values[identity.lower()] = 90 * rtx.GIB + 1
        threads[0].join(5)
        assert not threads[0].is_alive()
        stopped = content_cache._read(Path(state["prefill_service"]["directory"]) / "state.json", "PL_E_STALE")
        assert "placement cap" in stopped["reason"]
        FakeChild.instances[0].process.kill.assert_called_once()
        assert closes == locks
    finally:
        for thread in threads:
            for directory in tmp_path.glob("prefill-*"):
                (directory / "stop").touch()
            thread.join(5)


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


def test_08_manifest_full_read_deadline_and_persistence(tmp_path, files, monkeypatch):
    artifacts, _ = files
    approval = {k: v for k, v in window().items() if k != "kind"}
    approval["approval"] = "test"
    document = json.loads(artifacts.manifest.read_bytes())
    monkeypatch.setattr(core, "locate", lambda _: "fake ts-recipe")
    run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr=""))
    def runner(paths, permitted):
        return measurements.manifest(None, paths, permitted, clock=lambda: NOW, run=run)
    result = measurements.acquire(None, "manifest", runtime=tmp_path, files=[tmp_path / "model"], window=approval,
                                  clock=lambda: NOW, manifest_runner=runner)
    assert run.call_args.args[0][1:3] == ["manifest", "--full"]
    assert run.call_args.kwargs["timeout"] == 600
    assert (tmp_path / "content-manifests" / (result["manifest_sha256"] + ".json")).exists()
    import subprocess
    run.side_effect = subprocess.TimeoutExpired("manifest", 600)
    with pytest.raises(reader.RecipeError, match="deadline"):
        measurements.manifest(None, [tmp_path / "model"], approval, clock=lambda: NOW, run=run)


def test_11_portable_g6_reconstructs_original_frozen_bytes():
    from tests.portable_fixtures import read_fixture
    path = Path(__file__).parent / "fixtures/render/legacy-eb9ae96.json"
    assert hashlib.sha256(read_fixture(path).encode()).hexdigest() == "40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7"


def test_13_failed_publication_cleans_unique_temporary_file(tmp_path):
    path = tmp_path / "cache.json"
    content_cache._write(path, {"old": True})
    with mock.patch.object(Path, "replace", side_effect=OSError("publication failed")), pytest.raises(OSError):
        content_cache._write(path, {"new": True})
    assert json.loads(path.read_bytes()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


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
    data = dict(runtime.preset(), engine_sha="1" * 40, capabilities=["placement-v1"])
    assert runtime.render(artifacts, binary="fake", argv=["model"], data=data)[2] == result.document
    assert artifacts.placement.read_bytes() == original


def test_12_production_solver_only_forwards_current_cache_hits_and_freezes_them(identity):
    from tests.test_placement_solver import SolverTests
    helper = SolverTests()
    helper.setUp()
    try:
        identity_key = measurements.key(identity, "600", "a" * 64, "store")
        measured = measurements.median([dict(h2d_mbps=10, d2h_mbps=20, h2d_lat_ns=30)] * 9,
                                        measurements.XFER_METHOD)
        cache = measurements.Cache(helper.root)
        cache.save(identity_key, "xfer", measured)
        bundle = json.loads(helper.bundle.read_bytes())
        bundle.update(measurement_keys={"anchor": identity_key})
        bundle["probe"].update(devices=[dict(uuid=identity, driver="600")], file_store="store",
                               xfer_sha256={"anchor": "a" * 64}, probe_xfer={"anchor": {"stale": 1}})
        captured = []
        original_run = helper.backend.core.run
        def run(argv, **kwargs):
            captured.append((json.loads(Path(argv[argv.index("--probe") + 1]).read_bytes()),
                             json.loads(Path(argv[argv.index("--inputs") + 1]).read_bytes())))
            return original_run(argv, **kwargs)
        helper.backend.core.run = run
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        with mock.patch.object(measurements, "acquire", side_effect=AssertionError("no measurement during solve")):
            result = helper.solve()
            assert captured[-1][0]["probe_xfer"]["anchor"]["h2d_mbps"] == 10
            assert captured[-1][1]["choices"][0]["probe_xfer"] == captured[-1][0]["probe_xfer"]
            for field in ("uuid", "driver", "xfer", "store"):
                changed = deepcopy(bundle)
                if field in ("uuid", "driver"):
                    changed["probe"]["devices"][0][field] = "changed"
                elif field == "xfer":
                    changed["probe"]["xfer_sha256"]["anchor"] = "b" * 64
                else:
                    changed["probe"]["file_store"] = "new-store"
                helper.bundle.write_bytes(json.dumps(changed).encode())
                helper.solve()
                assert captured[-1][0]["probe_xfer"] == {}
                assert captured[-1][1]["choices"][0]["probe_xfer"] == {}
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        cache.save(identity_key, "xfer", measurements.median([dict(h2d_mbps=11, d2h_mbps=22, h2d_lat_ns=33)] * 9,
                                                             measurements.XFER_METHOD))
        with pytest.raises(reader.RecipeError, match="measurements changed"):
            solver.pick(helper.cluster, result["selection"], "plain", "picked", backend=helper.backend)
    finally:
        helper.doCleanups()
