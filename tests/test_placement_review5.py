"""Round 5: reviewed host reclamation and passive launch-command refusals (no engine or GPU work)."""
from contextlib import ExitStack, nullcontext
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from hyperloom import admission, cli, orchestrate, procs, rtx
from hyperloom.recipes import content_cache, launch, preflight, reader, service
from tests import test_placement_preflight, test_recipes


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


@pytest.mark.parametrize("entry", ["hold-up", "up", "hold-inputs", "hold-deploy", "deploy"])
@pytest.mark.parametrize("invalid", ["incomplete", "timezone", "expired", "short-lease", "blank-ref",
                                    "deadline", "long-deadline", "queue"])
def test_4_command_refusals_precede_admission_progress_snapshot_and_pins(tmp_path, monkeypatch, entry, invalid):
    path = tmp_path / "recipe.json"
    path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
    options = launch.Options(str(path))
    intent = dict(service.DEFAULT_INTENT, profile="new")
    approval = None
    flags = []
    if invalid in ("deadline", "long-deadline", "queue"):
        key = "queue_timeout_s" if invalid == "queue" else "deadline_s"
        intent[key] = 1 if invalid == "queue" else (0 if invalid == "deadline" else 1200)
        flags = ["--queue-timeout" if invalid == "queue" else "--deadline", str(intent[key])]
    elif invalid == "incomplete":
        flags = ["--approval-ref", "review"]
        approval = {"ref": "review"}
    else:
        ref = " " if invalid == "blank-ref" else "review"
        lease = 1 if invalid == "short-lease" else 1300
        end = "2000-01-01T00:00:00Z" if invalid == "expired" else "2099-01-01T00:00:00Z"
        if invalid == "timezone":
            end = end[:-1]
        flags = ["--approval-ref", ref, "--approval-lease", str(lease), "--approval-end", end]
        approval = dict(ref=ref, lease_s=lease, end_epoch=0 if invalid == "expired" else 4070908800)
        if invalid == "timezone":
            approval["end_epoch"] = float("nan")  # Direct API equivalent: an invalid end.
    monkeypatch.setattr(service, "options_from_inputs", lambda *a: (None, intent, options))
    monkeypatch.setattr(preflight, "before_options", lambda *a, **kw: None)
    monkeypatch.setattr(service, "_states", lambda: {})
    monkeypatch.setattr(cli, "_cluster", lambda _: None)
    fake = mock.Mock()
    with ExitStack() as stack:
        writes = [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                  for module, name in [(admission, "for_config"), (service, "launch_progress"),
                                       (service, "acquire_pin"), (service, "pinned"), (service, "launch_inputs")]]
        if entry == "deploy":
            operation = lambda: service.deploy("a" * 64, orchestrate=fake, approval=approval)
        else:
            if entry == "hold-deploy":
                argv = ["recipe", "deploy", "a" * 64]
                flags = [] if invalid in ("deadline", "long-deadline", "queue") else flags
            elif entry == "hold-inputs":
                argv = ["up", "new", "--recipe-inputs", "a" * 64]
            else:
                argv = ["up", "new", "--recipe", str(path)]
            args = cli.build_parser().parse_args([*argv, *flags, "--config", str(tmp_path / "config.json")])
            operation = (lambda: cli.up_command(args, orchestrate=fake, load_cluster=lambda _: None)) if entry == "up" \
                else (lambda: cli._admission_hold(args))
        message = "queue timeout" if invalid == "queue" else ("deadline" if "deadline" in invalid else "approval")
        with pytest.raises(ValueError, match=message):
            operation()
        for write in writes:
            write.assert_not_called()
        fake.down.assert_not_called()
        fake.up.assert_not_called()
