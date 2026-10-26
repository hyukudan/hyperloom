"""Controller recovery uses CPU fakes; no engine, device query or detached process runs."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import hashlib
import json
import threading

import pytest

from hyperloom import orchestrate, procs, rtx, placement_supervision
from hyperloom.recipes import launch, commands, content_cache, core, runtime, session

from tests import test_rtx, test_recipes
from tests.placement_fixtures import grant, identity, files
from tests.replica_fake import startup_args
from tests.test_recipes_grants import NOW, window


@pytest.mark.parametrize("fault", ["controller-death", "cancelled", "launch", "existing", "ready"])
def test_placement_start_owns_handlers_watcher_and_rollback(tmp_path, monkeypatch, request, fault):
    path = tmp_path / "profile.json"
    spec = replace(test_rtx._binary_spec(), placement_supervision={"caps_bytes": {"synthetic": 1}})
    placement_request = SimpleNamespace(files=mock.Mock())
    prepared = SimpleNamespace(placement_request=placement_request, production=False, record=lambda: {})
    monkeypatch.setattr(launch, "render_placement", lambda *a, **kw: spec)
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: path)
    monkeypatch.setattr(orchestrate, "_known_hosts", lambda: tmp_path / "known-hosts")
    calls, locks = [], []
    real_lock = procs.InstanceLock
    def lock(where):
        item = real_lock(where)
        locks.append(item)
        return item
    monkeypatch.setattr(procs, "InstanceLock", lock)

    @contextmanager
    def handlers(where):
        assert where == path
        calls.append("handlers")
        try:
            yield
        finally:
            calls.append("handlers-closed")
    monkeypatch.setattr(orchestrate, "_up_handlers", handlers)
    def detached(argv, log):
        state = orchestrate._read(path)
        assert state["phase"] == "starting"
        assert argv[-2:] == [str(path), state["token"]]
        assert calls == ["handlers"]
        calls.append("watcher")
        orchestrate._write(path.with_suffix(".watch-ready"), {"token": state["token"]})
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(orchestrate, "_detached", detached)
    helper = {"running": False}
    record = {"directory": str(tmp_path / "helper")}
    monkeypatch.setattr(rtx, "prepare_service", lambda _: record)
    threads = []
    def stop_helpers():
        directory = Path(record["directory"])
        if directory.exists():
            (directory / "stop").touch()
        for thread in threads:
            thread.join(5)
    request.addfinalizer(stop_helpers)
    cancelled = [False]
    def start(saved, rendered, *, detached, owner, check):
        assert calls[:2] == ["handlers", "watcher"]
        assert orchestrate._read(path)["prefill_service"] == saved
        assert rtx._owner_alive(owner)
        directory = Path(saved["directory"])
        directory.mkdir()
        ready = threading.Event()
        def process():
            # Fake only the helper process: real stop_service must wait for its lifetime lock.
            lifetime = real_lock(directory / "helper.lock").acquire()
            try:
                helper["running"] = True
                ready.set()
                while not (directory / "stop").exists():
                    threading.Event().wait(.01)
                helper["running"] = False
            finally:
                lifetime.close()
        thread = threading.Thread(target=process)
        threads.append(thread)
        thread.start()
        assert ready.wait(5)
        if fault == "controller-death":
            # An uncatchable controller death releases its instance lock. Run the real recovery loop then.
            locks[0].close()
            orchestrate.up_watch(path, owner["token"])
            assert (Path(record["directory"]) / "stop").exists()
            assert rtx._read(Path(record["directory"]) / "state.json")["state"] == "stopped"
            assert not helper["running"]
            assert not path.exists()
            assert not rtx._owner_alive(owner)
            raise RuntimeError("controller died")
        if fault == "launch":
            raise RuntimeError("launch failed")
        if fault == "cancelled":
            cancelled[0] = True
            check()
    monkeypatch.setattr(rtx, "launch_service", start)
    if fault == "existing":
        path.write_bytes(b'{"existing":true}')
    if fault == "ready":
        assert launch.up_placement(None, "profile", prepared, deadline_s=600)["phase"] == "running"
        assert helper["running"]
        orchestrate._down_state(orchestrate._read(path))
    else:
        message = {"existing": "saved running state", "cancelled": "stop requested",
                   "launch": "launch failed", "controller-death": "controller died"}[fault]
        with pytest.raises(RuntimeError, match=message):
            launch.up_placement(None, "profile", prepared, deadline_s=600, cancelled=lambda: cancelled[0])
        assert not helper["running"]
        if fault == "existing":
            assert path.read_bytes() == b'{"existing":true}'
            placement_request.files.publish.assert_not_called()
            assert "watcher" not in calls
        else:
            assert not path.exists()
    assert all(item.file is None for item in locks)
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    if fault != "existing":
        placement_request.files.publish.assert_called_once_with()
        assert calls[-1] == "handlers-closed"


def test_01_evaluate_prepare_up_and_real_watchdog_with_fake_child(tmp_path, files, identity, monkeypatch):
    from hyperloom import admission, cli, gpu_counters, orchestrate, secrets
    from hyperloom.recipes import service
    from tests.replica_fake import FakeReplica as FakeChild

    artifacts, document = files
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION, capabilities=["placement-v1"])
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
    source = lambda _, **kw: ({"cuda": [row]}, {"model": "synthetic", "devices": {identity: row}, "links": []}, raw, [])
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
    inputs = {"routes": {"decode": dict(components=ref, selected_content=selected, receipts=str(artifacts.receipts),
                                      argv=startup_args(model)[1:])}}
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
    gates, locks, closes, threads, watchers = [], [], [], [], []
    def device_lock(key):
        locks.append(key)
        item = mock.Mock()
        item.acquire.return_value = item
        item.close.side_effect = lambda: closes.append(key)
        return item
    FakeChild.instances = []
    def detached(argv, logfile):
        if "_up-watch" in argv:
            thread = threading.Thread(target=orchestrate.up_watch, args=argv[-2:])
            watchers.append(thread)
            thread.start()
            return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)
        thread = threading.Thread(target=rtx.service_run, args=(argv[-1],), kwargs=dict(
            child_factory=FakeChild, verify=lambda u: {"uuid": u}, lock_factory=device_lock,
            gate_runner=lambda args: gates.append(args), observer_factory=lambda: observer,
            health=lambda port: {"status": "ok"}, memory=lambda: dict(free_physical_gib=100, free_commit_gib=120)))
        threads.append(thread)
        thread.start()
        return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)
    monkeypatch.setattr(orchestrate, "_detached", detached)
    # The CLI must hold the PC for the real selected placement Request, even without a prefill-service profile.
    content_cache._write(tmp_path / "placement-inputs" / ("d" * 64 + ".json"), inputs)
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "locate", lambda *a, **kw: path)
    monkeypatch.setattr(service, "deployment_profile", lambda *a: "decode")
    database = admission.Admission(":memory:")
    monkeypatch.setattr(admission, "for_config", lambda _: database)
    args = cli.build_parser().parse_args(["up", "decode", "--recipe", str(path),
                                         "--config", str(tmp_path / "config.json")])
    try:
        release = cli._admission_hold(args)
        try:
            assert database.holds()[0]["resources"] == ["local"]
        finally:
            release()
        database.acquire("bench", {"local"}, label="PC benchmark")
        with pytest.raises(RuntimeError, match="benchmark"):
            cli._admission_hold(args)
    finally:
        database.close()
    prepared_calls = len(calls)
    try:
        state = orchestrate.up(cluster, "decode", recipe=options,
                               admitted_selection=launch.selection(cluster, "decode", prepared))
        assert state["phase"] == "running"
        assert gates == [entry["gate"]] and locks == [identity.lower()]
        assert orchestrate.endpoint("decode")["protocol"] == "ds41-replica-spool-v1"
        assert len(calls) == prepared_calls + 1
        values[identity.lower()] = 90 * rtx.GIB + 1
        threads[0].join(5)
        assert not threads[0].is_alive()
        stopped = content_cache._read(Path(state["prefill_service"]["directory"]) / "state.json", "PL_E_STALE")
        assert "placement cap" in stopped["reason"]
        FakeChild.instances[0].process.kill.assert_called_once()
        assert closes == locks
    finally:
        for thread in threads + watchers:
            for directory in tmp_path.glob("prefill-*"):
                (directory / "stop").touch()
            thread.join(5)
