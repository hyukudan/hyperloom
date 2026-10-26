"""CPU-only grant admission regressions."""

from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest import mock
import json
import os
import socket
import uuid

import pytest

from hyperloom import cli, orchestrate, procs, rtx
from hyperloom.recipes import apply, commands, content_cache, core, facts, launch, preflight, reader, runtime, service

from tests import test_recipes, test_placement_preflight


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
        source = lambda _, **kw: (inventory, gathered, raw, [])
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


@pytest.mark.parametrize("case", ["owned", "owned-approved", "foreign-physical", "foreign-floor", "foreign-commit",
                                 "unknown", "stale", "duplicate", "after-stop-physical", "after-stop-floor",
                                 "after-stop-commit"])
def test_3_reviewed_private_host_memory_is_reclaimed_only_until_shutdown(tmp_path, monkeypatch, case):
    fixture = test_placement_preflight.PreflightTests()
    fixture.setUp()
    try:
        directory = tmp_path / "old-helper"
        directory.mkdir()
        path = tmp_path / "recipe.json"
        path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        options = launch.Options(str(path), placement_inputs=fixture.inputs)
        intent = dict(service.DEFAULT_INTENT, profile="new")
        approval = None
        if case == "owned-approved":
            intent["deadline_s"] = 1200
            approval = dict(ref="review", lease_s=1300, end_epoch=4070908800)
        monkeypatch.setattr(service, "options_from_inputs", lambda *a: (fixture.cluster, intent, options))
        fixture.cluster.machines = {"local": SimpleNamespace(access="local")}
        monkeypatch.setattr(orchestrate, "check_launch", lambda *a, **kw: None)
        monkeypatch.setattr(rtx.secrets, "runtime_dir", lambda: tmp_path / "runtime")
        monkeypatch.setattr(rtx, "compute_apps", lambda: [])
        monkeypatch.setattr(rtx, "device_used_mib", lambda _: 0)
        monkeypatch.setattr(rtx, "pro_busy", lambda _: None)
        monkeypatch.setattr(preflight.procs, "check_port", lambda *a: None)
        counter = mock.Mock(return_value=0 if case == "unknown" else 70 * rtx.GIB)
        monkeypatch.setattr(procs, "private_resident_bytes", counter, raising=False)
        memory = fixture.raw["hosts"]["local"]
        memory.update(free_ram_gib=1 if case == "foreign-physical" else 60,
                      free_commit_gib=1 if case == "foreign-commit" else 60)
        if case in ("foreign-floor", "after-stop-floor"):
            fixture.recipe["limits"]["host_free_ram_floor_gib"] = 16
        if case == "foreign-floor":
            memory["free_ram_gib"] = 10  # 80 GiB after credit meets 82 GB, but leaves too little after the charge.
        original = dict(memory)
        from tests.test_recipes_grants import NOW
        def prepare(*args, reviewed=None, **kwargs):
            return preflight.check(fixture.cluster, path, options.layers, fixture.backend, fixture.recipe,
                                   fixture.bound, fixture.report, fixture.raw, "a" * 64, inputs=fixture.inputs,
                                   cache=fixture.cache, now=NOW, busy=lambda _: None,
                                   observer_factory=lambda: fixture.observer, reviewed=reviewed)
        monkeypatch.setattr(launch, "prepare", prepare)
        with ExitStack() as stack:
            helper = procs.InstanceLock(directory / "helper.lock").acquire()
            stack.callback(helper.close)
            if case == "stale":
                helper.close()
            record = dict(directory=str(directory), machine="local", device=fixture.uuid, pid=os.getpid(), port=8096,
                          placement_supervision={"caps_bytes": {fixture.uuid.lower(): 90 * rtx.GIB}})
            content_cache._write(directory / "state.json", dict(state="ready", pid=os.getpid(),
                                                               device=[dict(uuid=fixture.uuid)]))
            state = dict(token="reviewed-controller", prefill_service=record)
            running = {"old": state}
            if case == "duplicate":
                running["alias"] = state
                memory.update(free_ram_gib=1, free_commit_gib=1)  # Counting this PID twice would wrongly admit.
                original = dict(memory)
            stops = sorted(running)
            monkeypatch.setattr(service, "_states", lambda: running)
            events = []
            # Keep the real progress object, but observe context entry without writing a job file.
            actual_progress = service.launch_progress
            def progress(*args):
                events.append("progress")
                return actual_progress(*args)
            monkeypatch.setattr(service, "launch_progress", progress)
            pin = stack.enter_context(mock.patch.object(service, "pinned", side_effect=lambda _: nullcontext()))
            fake = mock.Mock()
            def down(name):
                events.append("down")
                assert memory == original  # Hypothetical credit must never contaminate the physical sample.
                memory.update(free_ram_gib=60 if case == "after-stop-physical" else 130,
                              free_commit_gib=60 if case == "after-stop-commit" else 130)
                if case == "after-stop-floor":
                    memory["free_ram_gib"] = 80
                helper.close()
                running.clear()
                return {"state": "stopped"}
            fake.down.side_effect = down
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".lock"}
            if case.startswith("owned"):
                service.deploy("a" * 64, stop=stops, source=lambda _: None, orchestrate=fake, approval=approval)
                fake.down.assert_called_once_with("old")
                fake.up.assert_called_once()
                assert counter.call_count == 2  # Both reviewed checks; no credit after down.
            else:
                with pytest.raises(reader.RecipeError, match="PL_E_(GRANT|FREE)"):
                    service.deploy("a" * 64, stop=stops, source=lambda _: None, orchestrate=fake)
                fake.up.assert_not_called()
                if case.startswith("after-stop"):
                    fake.down.assert_called_once_with("old")
                else:
                    fake.down.assert_not_called()
                    assert events == []
                    pin.assert_not_called()
            assert {p: p.read_bytes() for p in before} == before
    finally:
        fixture.doCleanups()
