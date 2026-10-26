"""Round 6: passive command refusals and fault-tolerant helper teardown, with no GPU or engine work."""
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hyperloom import admission, cli, gpu_counters, orchestrate, placement_supervision, procs, rtx
from hyperloom.config import parse
from hyperloom.recipes import launch, service
from tests import test_recipes, test_rtx
from tests.test_placement_supervision import policy


SNAPSHOT_FLAGS = [["--recipe", "other"], ["--recipe-profile", "profile.json"],
                  ["--recipe-overrides", "overrides.json"], ["--recipe-set", "a=1"],
                  ["--recipe-dir", "recipes"], ["--recipe-workload", "default"], ["--recipe-workload", ""],
                  ["--node", "halo-1"], ["--device", "workstation:rtx-pro"],
                  ["--diagnostic", "unknown"], ["--vram-cap-gib", "0"], ["--router", "halo-1"]]
COMMAND_FLAGS = [(["--node", "unknown"], "unknown machine"),
                 (["--node", "halo-1", "halo-1"], "distinct machines"),
                 (["--node", "halo-2", "halo-1"], "rank order"),
                 (["--diagnostic", "unknown"], "unknown diagnostic"),
                 (["--device", "bad"], "unknown machine or device"),
                 (["--device", "workstation:rtx-4090"], "excluded"),
                 (["--device", "halo-1:missing"], "unsupported profile device"),
                 (["--vram-cap-gib", "90"], "both set the RTX cap"),
                 (["--router", "unknown"], "router locally"),
                 (["--job", "not-hex"], "expected hex")]
COMMAND_CASES = [(entry, flags, "planned snapshot") for entry in ("hold-inputs", "inputs")
                 for flags in SNAPSHOT_FLAGS]
COMMAND_CASES += [(entry, flags, message) for entry in ("hold-up", "up") for flags, message in COMMAND_FLAGS]
COMMAND_CASES += [(entry, ["--job", "not-hex"], "expected hex")
                  for entry in ("hold-deploy", "deploy")]
COMMAND_CASES += [(entry, [], "not the reviewed") for entry in ("hold-deploy", "deploy")]
COMMAND_CASES += [(entry, [], "no snapshot") for entry in ("hold-deploy", "deploy", "hold-inputs", "inputs")]
COMMAND_CASES += [(entry, [], "expected a 64-hex digest") for entry in ("hold-inputs", "inputs")]
COMMAND_CASES += [(entry, [], "no such recipe") for entry in ("hold-up", "up")]
COMMAND_CASES += [(entry, [], "saved running state")
                  for entry in ("hold-up", "up", "hold-inputs", "inputs", "hold-plain", "plain")]
PLAIN_FLAGS = [(flags, "loopback-only" if flags[0] == "--router" else message)
               for flags, message in COMMAND_FLAGS if flags[0] != "--vram-cap-gib"]
PLAIN_FLAGS += [(["--vram-cap-gib", value], "vram-cap-gib") for value in ("nan", "0", "91")]
PLAIN_FLAGS += [(["--recipe-profile", "profile.json"], "need --recipe")]
PLAIN_FLAGS += [(["--recipe-workload", ""], "need --recipe")]
COMMAND_CASES += [(entry, flags, message) for entry in ("hold-plain", "plain") for flags, message in PLAIN_FLAGS]


@pytest.mark.parametrize("entry,flags,message", COMMAND_CASES)
def test_2_option_refusals_precede_holds_publication_pins_and_progress(tmp_path, monkeypatch, entry, flags, message):
    cluster = parse(test_rtx.rtx_example())
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(test_recipes.PlacementSchemaTests.document()), encoding="utf-8")
    options = launch.Options(str(path))
    intent = dict(service.DEFAULT_INTENT, profile="ds41-rtx")
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    def from_inputs(*args):
        if message == "no snapshot":
            raise service.NotFound("no snapshot (it may have expired: plan again)")
        if not args[0]:
            raise service.RecipeError("expected a 64-hex digest")
        return cluster, intent, options
    monkeypatch.setattr(service, "options_from_inputs", from_inputs)
    monkeypatch.setattr(service, "deployment_profile", lambda *a: "ds41-rtx")
    def locate(ref, *args, **kwargs):
        if not ref:
            raise service.NotFound("no such recipe")
        return path
    monkeypatch.setattr(service, "locate", locate)
    monkeypatch.setattr(service, "_states", lambda: {"old": {}} if message == "not the reviewed" else {})
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: tmp_path / "up.json")
    if message == "saved running state":
        (tmp_path / "up.json").write_text("{}", encoding="utf-8")
    prepare = mock.Mock(return_value=SimpleNamespace(placement_request=object()))
    monkeypatch.setattr(launch, "prepare", prepare)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    fake = mock.Mock()
    with service.configured(tmp_path / "config.json"), ExitStack() as stack:
        writes = [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                  for module, name in [(admission, "for_config"), (service, "launch_inputs"),
                                       (service, "acquire_pin"), (service, "pinned"), (service, "launch_progress")]]
        if entry == "deploy":
            operation = lambda: service.deploy("a" * 64, job="not-hex" if flags else None, orchestrate=fake)
        else:
            argv = (["recipe", "deploy", "a" * 64] if entry == "hold-deploy" else
                    ["up", "ds41-rtx"] if "plain" in entry else
                    ["up", "ds41-rtx", "--recipe-inputs" if "inputs" in entry else "--recipe",
                     "" if message in ("expected a 64-hex digest", "no such recipe") else
                     "a" * 64 if "inputs" in entry else str(path)])
            args = cli.build_parser().parse_args([*argv, *flags, "--config", str(tmp_path / "config.json")])
            operation = (lambda: cli._admission_hold(args)) if entry.startswith("hold-") else (
                lambda: cli.up_command(args, orchestrate=fake, load_cluster=lambda _: cluster))
        with pytest.raises((ValueError, RuntimeError), match=message):
            operation()
        for write in writes:
            write.assert_not_called()
        fake.up.assert_not_called()
        fake.down.assert_not_called()
        prepare.assert_not_called()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


CLEANUP_STEPS = ["spec", "watchdog", "pdh", "nvml", "spool", "child", "device-0", "device-1",
                 "state", "events", "lifetime", "log", "multiple"]
CLEANUP_CASES = [(stage, step) for stage in ("loading", "ready") for step in CLEANUP_STEPS]
CLEANUP_CASES += [("stale-ready", step) for step in ("pdh", "nvml", "multiple")]
CLEANUP_CASES += [("watchdog-start", step) for step in ("pdh", "nvml", "multiple")]
CLEANUP_CASES += [("legacy", "device-0")]


@pytest.mark.parametrize("stage,fault", CLEANUP_CASES)
def test_3_cleanup_faults_cannot_skip_child_locks_or_terminal_state(tmp_path, monkeypatch, capsys, stage, fault):
    calls, failures, children, locks = [], [], [], []
    broken = {"watchdog", "pdh", "nvml", "child", "device-0", "device-1"} if fault == "multiple" else {fault}
    if fault == "log":
        broken.add("pdh")

    def step(name, action=lambda: None):
        calls.append(name)
        result = action()
        if name in broken:
            failures.append(name)
            raise gpu_counters.CounterError("injected " + name)
        return result

    real_lock = procs.InstanceLock
    def lock(path, name):
        item = real_lock(path)
        close = item.close
        item.close = lambda: step(name, close)
        locks.append(item)
        return item
    monkeypatch.setattr(procs, "InstanceLock", lambda path: lock(path, "lifetime"))
    def device_lock(uuid):
        index = len([c for c in calls if c.startswith("acquire-")])
        calls.append("acquire-" + str(index))
        return lock(tmp_path / ("device-" + str(index) + ".lock"), "device-" + str(index))

    real_open, real_unlink, real_write = Path.open, Path.unlink, rtx._write
    class Events:
        def __init__(self, stream):
            self.stream = stream
        def write(self, line):
            if fault == "log" and '"cleanup"' in line:
                raise OSError("injected log")
            return self.stream.write(line)
        def flush(self):
            self.stream.flush()
        def close(self):
            step("events", self.stream.close)
    def opening(path, *args, **kw):
        stream = real_open(path, *args, **kw)
        mode = args[0] if args else kw.get("mode", "r")
        return Events(stream) if path.name == "watchdog.jsonl" and mode == "a" else stream
    def unlink(path, *args, **kw):
        if path.name == "spec.json" and kw.get("missing_ok"):
            return step("spec", lambda: real_unlink(path, *args, **kw))
        return real_unlink(path, *args, **kw)
    def write(path, value):
        if value.get("state") == "stopped":
            calls.append("state")
            if "state" in broken:
                failures.append("state")
                raise gpu_counters.CounterError("injected state")
        return real_write(path, value)
    monkeypatch.setattr(Path, "open", opening)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(rtx, "_write", write)

    observer = gpu_counters.Observer.__new__(gpu_counters.Observer)
    observer.pdh = mock.Mock(close=lambda: step("pdh"))
    observer.nvml = mock.Mock(close=lambda: step("nvml"))
    data = policy()
    values = {u.lower(): 0 for u in data["caps_gib"]}
    def sample(pid):
        if stage == "stale-ready" or stage == "watchdog-start" and pid == 4242:
            raise gpu_counters.CounterError("injected sample before child")
        return values
    observer.sample = sample
    monkeypatch.setattr(placement_supervision, "validate", lambda *a, **kw: (
        {g["uuid"].lower(): g for g in data["grants"]}, {u.lower(): v for u, v in data["caps_gib"].items()}))
    class Dog(placement_supervision.Watchdog):
        def start(self):
            if stage == "watchdog-start":
                return super().start()
            self.observer.sample(self.pid)
            self.reason = "test stop" if stage == "loading" else None
            return self
        def alive(self):
            return True
    monkeypatch.setattr(placement_supervision, "Watchdog", Dog)
    monkeypatch.setattr(rtx.Watchdog, "stop", lambda _: step("watchdog"))
    class Child(test_rtx.FakeChild):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            children.append(self)
        def stop(self, grace_s=5):
            step("child", lambda: super(Child, self).stop(grace_s))
    spool = mock.Mock(ready=lambda: {"event": "READY"}, health=lambda: True,
                      shutdown=lambda **kw: step("spool"))
    monkeypatch.setattr("hyperloom.replica_spool.Client", lambda *a, **kw: spool)
    def sleep(_):
        assert json.loads((tmp_path / "state.json").read_bytes())["state"] == "ready"
        (tmp_path / "stop").touch()
    monkeypatch.setattr(rtx, "time", SimpleNamespace(time=rtx.time.time, monotonic=rtx.time.monotonic, sleep=sleep))
    spec = replace(test_rtx._binary_spec(), pro_uuid=data["grants"][0]["uuid"], replica_spool=True,
                   placement_supervision=None if stage == "legacy" else data)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"spec": spec.__dict__}), encoding="utf-8")
    if stage == "stale-ready":
        real_write(tmp_path / "state.json", {"state": "ready", "pid": 999})
    with pytest.raises(BaseException) as raised:
        rtx.service_run(path, child_factory=Child, watchdog_factory=test_rtx.FakeDog,
                        verify=lambda u: {"uuid": u}, lock_factory=device_lock, gate_runner=lambda _: None,
                        observer_factory=lambda: observer, memory=lambda: {})
    expected = ["spec"]
    if stage != "stale-ready":
        expected += ([] if stage == "legacy" else ["watchdog", "pdh", "nvml"])
        expected += ["spool", "child", "device-0"] + ([] if stage == "legacy" else ["device-1"])
    else:
        expected += ["pdh", "nvml"]
    expected += ["state", "events", "lifetime"]
    assert set(expected) <= set(calls), (calls, raised.value)
    assert calls[-1] == "lifetime"
    if "child" in calls:
        assert calls.index("child") < calls.index("device-0") < calls.index("state")
    assert all(child.stopped for child in children)
    assert not path.exists()
    if fault != "state":
        assert json.loads((tmp_path / "state.json").read_bytes())["state"] == "stopped"
    for item in locks:
        assert item.file is None
        released = real_lock(item.path).acquire()
        released.close()
    logs = (tmp_path / "watchdog.jsonl").read_text(encoding="utf-8") + capsys.readouterr().err
    assert failures
    for name in failures:
        label = ("watchdog" if name in ("pdh", "nvml") and stage != "stale-ready" else
                 "observer" if name in ("pdh", "nvml") else
                 "device" if name.startswith("device-") else name)
        assert label in logs
    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return [leaf for nested in error.exceptions for leaf in leaves(nested)]
        return [str(error)]
    errors = leaves(raised.value)
    for name in failures:
        assert "injected " + name in errors
