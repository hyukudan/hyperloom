"""CPU-only regressions for round 3's identity, native interface and pure-resource refusals."""
from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest import mock
import uuid

import pytest

from hyperloom import assets, cli, orchestrate, procs, rtx
from hyperloom.recipes import apply, commands, content_cache, core, facts, launch, preflight, reader, runtime, service
from hyperloom.recipes import session, sysmem
from tests import test_recipes
from tests import test_placement_preflight


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
        "engine_commit": "1" * 40, "binaries": {binary.name: engine_sha}})
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
    monkeypatch.setattr(facts.probe, "nvidia_gpus", lambda: [gpu])
    monkeypatch.setattr(facts.probe, "host_memory", lambda: dict(total_gib=128, free_gib=100, free_commit_gib=120))
    monkeypatch.setattr(sysmem, "probe", lambda _: "prefer_no_sysmem_fallback")
    monkeypatch.setattr(commands, "cache", lambda: None)
    monkeypatch.setattr(runtime, "preset", lambda: dict(engine_sha="1" * 40))
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


def test_5_native_replica_spool_round_trip(tmp_path):
    from hyperloom import replica_spool
    from tests import test_placement_render, test_rtx
    from tests.replica_fake import FakeReplica
    data = dict(runtime.preset(), engine_sha="1" * 40)
    files = SimpleNamespace(read=lambda: {"engine": data["engine"], "devices": []},
                            argv=lambda: ["--placement", "plan"])
    with pytest.raises(reader.RecipeError, match="PL_E_CAPABILITY.*placement"):
        runtime.render(files, binary="replica", argv=["model"], data=data)
    spool = replica_spool.Client(tmp_path, alive=lambda: True)
    for name in ("queue", "run", "done", "lock"):
        (tmp_path / name).mkdir()
    (tmp_path / "ready.json").write_text('{"event":"ready","load_id":7}', encoding="utf-8")
    assert spool.ready()["load_id"] == 7
    def worker():
        request = next((tmp_path / "queue").glob("*.req"))
        args = request.read_text().splitlines()
        request.rename(tmp_path / "run" / request.name)
        content_cache._write(tmp_path / "done" / (request.stem + ".json"),
                             dict(name=request.stem, rc=0, args=args))
        (tmp_path / "run" / request.name).rename(tmp_path / "done" / request.name)
    with mock.patch("hyperloom.replica_spool.time.sleep", side_effect=lambda _: worker()):
        assert spool.request(["--ping"])["args"] == ["--ping"]
        assert spool.request(["--phases", "chats0", "--chat-steps", "2"])["rc"] == 0
        assert spool.request(["--quit"])["args"] == ["--quit"]
    with pytest.raises(ValueError):
        spool.request(["--phases\n--quit"])
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    helper = test_rtx.HelperTests()
    helper.setUp()
    try:
        fixture.document["devices"][0].update(role="anchor", budget_bytes=10 * rtx.GIB)
        fixture.document["devices"] = fixture.document["devices"][:1]
        fixture.write()
        from tests.test_recipes_grants import NOW, window
        policy = dict(grants=[dict(uuid=fixture.uuids[0], vram_cap_gib=10, approval="test", window=window(),
                                  gate=[sys.executable, "fake-gate"])],
                      caps_gib={fixture.uuids[0].lower(): 10}, host_pinned_gib=0, host_pageable_gib=0,
                      free_commit_floor_gib=4)
        cluster = SimpleNamespace(machines={"local": SimpleNamespace(access="local", os="windows",
                                                                       install_root=str(tmp_path))})
        request = runtime.Request("local", fixture.files, policy, argv=tuple(fixture.argv))
        with mock.patch.object(runtime, "preset", return_value=fixture.data), mock.patch.object(rtx.time, "time", return_value=NOW):
            spec = rtx.render_placement_service(cluster, request, api_key="0" * 64)
        assert spec.argv[1] == str(fixture.root / "model") and spec.replica_spool
        assert not {"--host", "--port", "--api-key"} & set(spec.argv)
        assert spec.argv.count("--placement") == spec.argv.count("--content-manifest") == spec.argv.count("--receipts") == 1
        # Exercise the real helper protocol; only process/GPU supervision is faked.
        spec = test_rtx._binary_spec(argv=[sys.executable, *spec.argv[1:]], replica_spool=True)
        FakeReplica.instances = []
        thread = helper.run_helper(helper.write(spec), child_factory=FakeReplica,
            health=mock.Mock(side_effect=AssertionError("native replica has no HTTP health")))
        try:
            assert helper.wait_state("ready")["ready"]["event"] == "ready"
            client = replica_spool.Client(helper.directory / "spool", alive=FakeReplica.instances[0].alive)
            assert client.request(["--not-runtime"])["rc"] == 2
            assert client.request(["--phases", "chats0"])["rc"] == 0
        finally:
            (helper.directory / "stop").touch()
            thread.join(5)
        assert not thread.is_alive()
        assert ["--ping"] in FakeReplica.instances[0].requests
        assert ["--quit"] in FakeReplica.instances[0].requests
    finally:
        fixture.doCleanups()
        helper.doCleanups()


@pytest.mark.parametrize("conflict", ["lock", "port"])
def test_6_known_resources_refuse_without_creating_state(tmp_path, monkeypatch, conflict):
    fixture = test_placement_preflight.PreflightTests()
    fixture.setUp()
    try:
        monkeypatch.setattr(rtx.secrets, "runtime_dir", lambda: tmp_path / "runtime")
        with ExitStack() as stack:
            port = stack.enter_context(socket.socket())
            port.bind(("127.0.0.1", 0))
            selected = port.getsockname()[1] if conflict == "port" else 8096
            if conflict == "lock":
                lock = rtx.device_lock(fixture.uuid).acquire()
                stack.callback(lock.close)
            fixture.inputs["port"] = selected
            before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")}
            with pytest.raises(reader.RecipeError, match="PL_E_BUSY.*" + conflict):
                fixture.check()
            assert {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")} == before
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize("case", ["reviewed", "foreign-lock", "foreign-port", "foreign-owner", "foreign-compute",
                                 "unattributed-memory", "stale-helper", "after-stop-lock", "after-stop-port"])
def test_5_reviewed_handover_admits_owned_resources_and_rechecks_after_stop(tmp_path, monkeypatch, case):
    fixture = test_placement_preflight.PreflightTests()
    fixture.setUp()
    try:
        from tests.test_recipes_grants import NOW
        directory = tmp_path / "old-helper"
        directory.mkdir()
        monkeypatch.setattr(rtx.secrets, "runtime_dir", lambda: tmp_path / "runtime")
        path = tmp_path / "recipe.json"
        path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        options = launch.Options(str(path), placement_inputs=fixture.inputs)
        intent = dict(service.DEFAULT_INTENT, profile="new")
        monkeypatch.setattr(service, "options_from_inputs", lambda *a: (fixture.cluster, intent, options))
        monkeypatch.setattr(orchestrate, "check_launch", lambda *a, **kw: None)
        monkeypatch.setattr(launch, "_backend", lambda _: (backend, True))
        ctx = apply.Context("new", "synthetic", "single", [], "local")
        monkeypatch.setattr(facts, "context", lambda *a: ctx)
        fixture.cluster.machines = {"local": SimpleNamespace(devices=[dict(
            id="gpu", type="nvidia", uuid=fixture.uuid)])}
        inventory = {"cuda": [{**fixture.report["devices"][0], "memory_gib": 96}]}
        gathered = dict(model="synthetic", devices={fixture.uuid: inventory["cuda"][0]}, links=[])
        raw = {**fixture.raw, "devices": {}, "lan": {}, "usb4": {}}
        source = lambda _: (inventory, gathered, raw, [])
        monkeypatch.setattr(commands, "live_source", lambda *a, **kw: source)
        backend = core.CoreBackend(core.Core("fake-core"))
        backend.resolve = mock.Mock(return_value=core.Resolved(fixture.recipe, "a" * 64, {}))
        backend.match = mock.Mock(return_value=([], fixture.bound["binding"], "a" * 64))
        backend.place = fixture.backend.place
        monkeypatch.setattr(commands, "components_store", lambda: fixture.cache)
        monkeypatch.setattr(preflight.time, "time", lambda: NOW)
        monkeypatch.setattr(preflight.gpu_counters, "Observer", lambda: fixture.observer)
        fixture.observer.sample.return_value = {fixture.uuid.lower(): 4 * rtx.GIB}
        monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
        pid = os.getpid() + 1000 if case == "foreign-owner" else os.getpid()
        apps = [(fixture.uuid, pid, 4096)]
        if case == "foreign-compute":
            apps.append((fixture.uuid, os.getpid() + 1, 4096))
        monkeypatch.setattr(rtx, "compute_apps", lambda: apps)
        monkeypatch.setattr(rtx, "device_used_mib", lambda _: 8192 if case == "unattributed-memory" else 4096)
        # Adapter qualification/rendering is faked; admission, ownership and OS resource checks run normally.
        monkeypatch.setattr(apply, "refuse_unsupported", lambda *a, **kw: None)
        monkeypatch.setattr(runtime, "decision", lambda *a, **kw: {"placement": True})
        monkeypatch.setattr(runtime, "request", lambda _: None)
        monkeypatch.setattr(launch, "render_placement", lambda *a, **kw: SimpleNamespace(placement={}))
        with ExitStack() as stack:
            port = stack.enter_context(socket.socket())
            port.bind(("127.0.0.1", 0))
            port.listen()
            selected = port.getsockname()[1]
            fixture.inputs["port"] = selected
            lock = rtx.device_lock(fixture.uuid).acquire()
            stack.callback(lock.close)
            helper = procs.InstanceLock(directory / "helper.lock").acquire()
            stack.callback(helper.close)
            if case == "stale-helper":
                helper.close()
            owned = "GPU-" + str(uuid.uuid4()) if case == "foreign-lock" else fixture.uuid
            record = dict(directory=str(directory), device=owned, pid=pid,
                          port=selected + 1 if case == "foreign-port" else selected,
                          placement_supervision={"caps_bytes": {owned.lower(): 90 * rtx.GIB}})
            content_cache._write(directory / "state.json", dict(state="ready", pid=pid,
                                                               device=[dict(uuid=owned)]))
            running = {"old": dict(profile="old", token="reviewed-controller", prefill_service=record)}
            monkeypatch.setattr(service, "_states", lambda: running)
            progress = stack.enter_context(mock.patch.object(service, "launch_progress", wraps=service.launch_progress))
            pin = stack.enter_context(mock.patch.object(service, "pinned", side_effect=lambda _: nullcontext()))
            fake = mock.Mock()
            def down(name):
                assert name == "old"
                if case != "after-stop-lock":
                    lock.close()
                if case != "after-stop-port":
                    port.close()
                helper.close()
                running.clear()
                return {"state": "stopped"}
            fake.down.side_effect = down
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".lock"}
            if case == "reviewed":
                # CLI/portal admission must carry --stop before it can create an admission hold.
                args = cli.build_parser().parse_args(["recipe", "deploy", "a" * 64, "--stop", "old",
                                                      "--config", str(tmp_path / "config.json")])
                preflight.cli(args)
                assert backend.place.call_count == 1
                progress.assert_not_called()
                pin.assert_not_called()
                result = service.deploy("a" * 64, stop=["old"], source=source, orchestrate=fake)
                assert result["stopped"] == [{"profile": "old", "down": {"state": "stopped"}}]
                fake.down.assert_called_once_with("old")
                fake.up.assert_called_once()
                # CLI + both deploy checks before stopping, then admission without ownership exemptions.
                assert backend.place.call_count == 4
                assert procs.port_owned("127.0.0.1", selected, [os.getpid()]) is False
            else:
                with pytest.raises(reader.RecipeError, match="PL_E_BUSY"):
                    service.deploy("a" * 64, stop=["old"], source=source, orchestrate=fake)
                fake.up.assert_not_called()
                if case.startswith("after-stop"):
                    fake.down.assert_called_once_with("old")
                    assert backend.place.call_count == 3
                else:
                    fake.down.assert_not_called()
                    progress.assert_not_called()
                    pin.assert_not_called()
            assert {p: p.read_bytes() for p in before} == before
    finally:
        fixture.doCleanups()
