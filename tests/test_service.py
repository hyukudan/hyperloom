"""Non-lab systemd --user mode: refusals, unit content, fake systemctl, supervise exit codes, lease exclusion."""
import contextlib
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyperloom import cli, orchestrate, secrets, service
from hyperloom.orchestrate import OrchestrationError
from hyperloom.render import ProcessSpec
from tests.test_orchestrate import RELEASE, FakeRemote, cluster_fixture


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "node"))
loader = importlib.util.spec_from_file_location("service_under_test", REPO / "node" / "hyperloom_service.py")
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
install_loader = importlib.util.spec_from_file_location("install_for_service_test", REPO / "node" / "hyperloom_install.py")
node_install = importlib.util.module_from_spec(install_loader)
install_loader.loader.exec_module(node_install)
SECRET = "f" * 64
OTHER_BOOT = "0" * 32


class FakeSystemctl:
    def __init__(self, active="inactive", fail=(), enabled="disabled"):
        self.calls, self.active, self.fail, self.enabled = [], active, set(fail), enabled

    def __call__(self, argv, **_):
        self.calls.append(argv[2:])
        code = 1 if argv[2] in self.fail else 0
        if argv[2] == "is-active":
            out = self.active + "\n"
        elif argv[2] == "is-enabled":
            out = self.enabled + "\n"
        else:
            out = "ActiveState=" + self.active + "\nNRestarts=0\n"
        return subprocess.CompletedProcess(argv, code, out, "Failed to connect to bus" if code else "")


class NodeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "root"
        self.home = self.base / "home"
        self.home.mkdir()
        for name in ("etc", "run", "log", "releases/" + RELEASE + "/bin"):
            (self.root / name).mkdir(parents=True)
        (self.root / ".hyperloom-root").write_text("a" * 32)
        self.policy({"lab_dir": None, "asset_paths": []})
        self.binary = self.root / "releases" / RELEASE / "bin" / "strix-hip-decode-token"
        self.binary.write_bytes(b"")
        self.units = self.base / "units"
        self.proc = {"name": "halo-1", "machine": "local", "argv": [self.binary.as_posix(), "--http-port", "18501"],
                     "env": {"STRIX_COHORT_ROWS": "32"}, "ready": "qwen-stderr",
                     "health_url": "http://192.0.2.10:18501/health",
                     "key_file": (self.root / "etc" / "keys" / "key-a").as_posix(), "ready_timeout_s": 600}
        self.payload = {"root": self.root.as_posix(), "profile": "qwen-replicas", "processes": [self.proc]}
        self.systemctl = FakeSystemctl()
        self.path_dir = self.base / "path-bin"
        self.path_dir.mkdir()
        for patcher in (patch.object(node, "unit_dir", side_effect=lambda: self.units),
                        patch.object(node.subprocess, "run", side_effect=self.systemctl),
                        patch.object(node.shutil, "which", return_value="/usr/bin/systemctl"),
                        patch.object(node.sys, "executable", "/usr/bin/python3"),
                        patch.object(node.Path, "home", return_value=self.home),
                        patch.dict(os.environ, {"PATH": str(self.path_dir)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def policy(self, value):
        filename = self.root / "etc" / "install-policy.json"
        if value is None:
            filename.unlink(missing_ok=True)
        else:
            filename.write_text(json.dumps(value))

    def record(self, label, state="acquired", **extra):
        lid = os.urandom(16).hex()
        value = {"lease": lid, "label": label, "kind": "single", "state": state, "queued_at": time.time(), **extra}
        node.lease.write_json(node.lease.record_path(self.root, lid), value)
        return lid

    def write_spec(self):
        _, spec = node.build_spec(self.root, self.payload)
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        node.lease.write_json(spec_file, spec)
        return spec_file

    def plain(self):
        if sys.platform == "win32" and not self.root.as_posix().startswith("/"):
            # Unit paths are POSIX; exercise content with the plain-path check bypassed.
            patcher = patch.object(node, "safe", side_effect=lambda value: value)
            patcher.start()
            self.addCleanup(patcher.stop)


class NodeServiceTests(NodeCase):
    def test_lab_root_and_lab_payload_are_refused(self):
        self.assertIn("refused", node.run("service-install", dict(self.payload, lab_dir="/opt/lab"))["error"])
        self.policy({"lab_dir": (self.base / "lab").as_posix()})
        for verb in ("service-install", "service-start"):
            self.assertIn("lab mode", node.run(verb, self.payload)["error"])
        self.assertEqual(self.systemctl.calls, [])

    def test_guard_fails_closed_without_explicit_null_lab_dir(self):
        for policy, words in ((None, "install-policy.json is missing"), ({}, "lab mode"),
                              ({"asset_paths": []}, "lab mode"), ([], "JSON object")):
            self.policy(policy)
            for verb in ("service-install", "service-start"):
                with self.subTest(policy=policy, verb=verb):
                    self.assertIn(words, node.run(verb, self.payload)["error"])
        self.assertEqual(self.systemctl.calls, [])

    def test_turn_files_on_the_machine_are_refused(self):
        self.plain()
        places = (self.home, self.home / "lab", self.base, self.base / "other-lab", self.path_dir)
        for place in places:
            for name in node.LAB_FILES:
                place.mkdir(exist_ok=True)
                marker = place / name
                marker.mkdir() if name == ".gpu-turn" else marker.write_text("#!/bin/sh\n")
                try:
                    for verb in ("service-install", "service-start"):
                        with self.subTest(place=place.name, name=name, verb=verb):
                            self.assertIn("lab turn files", node.run(verb, self.payload)["error"])
                finally:
                    marker.rmdir() if name == ".gpu-turn" else marker.unlink()
        self.assertEqual(self.systemctl.calls, [])
        self.assertNotIn("error", node.run("service-install", self.payload))

    def test_stop_status_remove_stay_available_on_lab_machines(self):
        (self.home / "gpu-turn.sh").write_text("")
        self.assertEqual(node.run("service-stop", self.payload)["requested"], "stop")
        self.assertNotIn("error", node.run("service-status", self.payload))
        self.assertNotIn("error", node.run("service-remove", self.payload))

    def test_spec_keeps_lease_validation_and_release_executables(self):
        cases = [dict(self.proc, env={"LD_PRELOAD": "/x"}), dict(self.proc, machine="peer"),
                 dict(self.proc, argv=["/usr/bin/python3"])]
        for proc in cases:
            with self.subTest(proc=proc), self.assertRaises(ValueError):
                node.build_spec(self.root, dict(self.payload, processes=[proc]))
        profile, spec = node.build_spec(self.root, self.payload)
        self.assertEqual((spec["kind"], spec["label"], spec["lab_dir"], spec["service"]),
                         ("none", "hl-svc-qwen-replicas", None, True))

    def test_install_writes_fixed_unit_without_secrets(self):
        self.plain()
        reply = node.run("service-install", self.payload)
        self.assertNotIn("error", reply)
        unit = (self.units / "hl-qwen-replicas.service").read_text()
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        self.assertEqual(unit, node.UNIT.format(profile="qwen-replicas", root=self.root,
                                                python="/usr/bin/python3", spec=spec_file))
        for line in ("KillMode=mixed", "Restart=on-failure", "StartLimitBurst=3", "TimeoutStopSec=90",
                     "RestartPreventExitStatus=3 4", "ExecStart=/usr/bin/python3 "):
            self.assertIn(line, unit)
        self.assertNotIn(SECRET, unit + spec_file.read_text())
        self.assertNotIn("Environment", unit)
        self.assertEqual(self.systemctl.calls, [["show-environment"], ["is-active", "hl-qwen-replicas.service"],
                                                ["daemon-reload"], ["is-enabled", "hl-qwen-replicas.service"]])
        self.assertEqual((reply["enabled"], reply["unit_file_state"]), (False, "disabled"))
        for name in node.COPIED:
            self.assertTrue((self.root / "bin" / name).is_file())

    def test_start_limit_trips_before_three_readiness_timeouts_end(self):
        interval = int(re.search(r"StartLimitIntervalSec=(\d+)", node.UNIT).group(1))
        burst = int(re.search(r"StartLimitBurst=(\d+)", node.UNIT).group(1))
        restart = int(re.search(r"RestartSec=(\d+)", node.UNIT).group(1))
        self.assertGreaterEqual(interval, 2 * burst * (self.proc["ready_timeout_s"] + restart))
        prevented = re.search(r"RestartPreventExitStatus=(.*)", node.UNIT).group(1).split()
        self.assertEqual(sorted(prevented), sorted([str(node.EXIT_READY_TIMEOUT), str(node.EXIT_REFUSED)]))

    def test_reinstall_reports_the_actual_enablement(self):
        self.plain()
        self.systemctl.enabled = "enabled"
        reply = node.run("service-install", self.payload)
        self.assertEqual((reply["enabled"], reply["unit_file_state"]), (True, "enabled"))
        self.assertNotIn(["enable", "hl-qwen-replicas.service"], self.systemctl.calls)
        node.run("service-install", dict(self.payload, enable=True))
        self.assertIn(["enable", "hl-qwen-replicas.service"], self.systemctl.calls)

    def test_interpreter_is_not_resolved(self):
        with patch.object(node.sys, "executable", "/usr/local/../bin/python3"):
            self.assertEqual(node.interpreter(), "/usr/bin/python3")
        for value in ("", "python3", "/opt/py thon"):
            with patch.object(node.sys, "executable", value), self.assertRaises(ValueError):
                node.interpreter()
        if os.name == "posix":
            link = self.base / "python-link"
            link.symlink_to(os.path.realpath(sys.executable))
            with patch.object(node.sys, "executable", link.as_posix()):
                self.assertEqual(node.interpreter(), link.as_posix())

    def test_unit_paths_must_be_plain(self):
        for value in ("/opt/hyper loom", "/opt/%h", "relative", "/opt/../etc", '/opt/"x"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                node.safe(value)
        self.assertEqual(node.safe("/opt/hyperloom/halo-1"), "/opt/hyperloom/halo-1")

    def test_running_unit_is_not_replaced_or_removed(self):
        self.systemctl.active = "active"
        with patch.object(node, "safe", side_effect=lambda value: value):
            self.assertIn("stop it", node.run("service-install", self.payload)["error"])
        self.assertIn("stop it", node.run("service-remove", self.payload)["error"])

    def test_missing_user_manager_is_reported(self):
        self.systemctl.fail = {"show-environment"}
        with patch.object(node, "safe", side_effect=lambda value: value):
            error = node.run("service-install", self.payload)["error"]
        self.assertIn("user manager", error)
        self.assertIn("lingering is the user's own step", error)
        self.assertFalse(self.units.exists())

    def test_remove_checks_the_user_manager_first_and_resets_failed(self):
        self.units.mkdir()
        unit = self.units / "hl-qwen-replicas.service"
        unit.write_text("x")
        spec_file = self.write_spec()
        self.systemctl.fail = {"show-environment"}
        self.assertIn("user manager", node.run("service-remove", self.payload)["error"])
        self.assertTrue(unit.exists() and spec_file.exists())
        self.assertEqual(self.systemctl.calls, [["show-environment"]])
        self.systemctl.fail, self.systemctl.calls = set(), []
        lid = self.record("hl-svc-qwen-replicas", state="ready")
        self.assertTrue(node.run("service-remove", self.payload)["removed"])
        self.assertFalse(unit.exists() or spec_file.exists())
        self.assertEqual(self.systemctl.calls, [["show-environment"], ["is-active", "hl-qwen-replicas.service"],
                                                ["disable", "hl-qwen-replicas.service"], ["daemon-reload"],
                                                ["reset-failed", "hl-qwen-replicas.service"]])
        self.assertEqual(node.lease.read_json(node.lease.record_path(self.root, lid))["state"], "failed")

    def test_start_stop_go_through_systemctl_user_without_blocking(self):
        for verb, word in (("service-start", "start"), ("service-stop", "stop")):
            self.assertEqual(node.run(verb, self.payload)["requested"], word)
        self.assertEqual(self.systemctl.calls, [["start", "--no-block", "hl-qwen-replicas.service"],
                                                ["stop", "--no-block", "hl-qwen-replicas.service"]])

    def test_start_is_refused_while_a_lease_is_live(self):
        self.record("hl-qwen-halo-1-abcd")
        self.assertIn("lease is live", node.run("service-start", self.payload)["error"])
        self.assertEqual(self.systemctl.calls, [])


class ServiceRunTests(NodeCase):
    def run_with(self, state, reason):
        spec_file = self.write_spec()

        def supervise(filename):
            lid = node.lease.read_json(filename)["lease"]
            record = node.lease.read_json(node.lease.record_path(self.root, lid))
            self.assertEqual((record["label"], record["kind"], record["pid"]),
                             ("hl-svc-qwen-replicas", "service", os.getpid()))
            record.update(state=state, reason=reason)
            node.lease.write_json(node.lease.record_path(self.root, lid), record)
        with patch.object(node.lease, "supervise", side_effect=supervise):
            return node.service_run(spec_file.as_posix())

    def test_service_run_exit_code_follows_supervise_reason(self):
        for state, reason, code in (("stopped", "requested", 0), ("failed", "process-exit:halo-1", 1),
                                    ("stopped", "deadline", 1), ("failed", "readiness-timeout:halo-1", 3)):
            with self.subTest(reason=reason):
                self.assertEqual(self.run_with(state, reason), code)
        status = node.run("service-status", self.payload)
        self.assertEqual(status["lease"]["label"], "hl-svc-qwen-replicas")

    def test_service_run_rejects_lease_specs(self):
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        node.lease.write_json(spec_file, {"root": self.root.as_posix(), "kind": "single", "lab_dir": "/opt/lab"})
        with self.assertRaises(ValueError):
            node.service_run(spec_file.as_posix())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(node.main(["x", "service-run", spec_file.as_posix()]), node.EXIT_REFUSED)

    def test_service_run_refuses_live_leases_and_turn_files(self):
        spec_file = self.write_spec()
        lid = self.record("hl-qwen-halo-1-abcd")
        with patch.object(node.lease, "supervise") as supervise, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(node.main(["x", "service-run", spec_file.as_posix()]), node.EXIT_REFUSED)
            node.lease.write_json(node.lease.record_path(self.root, lid), {"lease": lid, "state": "stopped"})
            (self.home / ".gpu-turn").mkdir()
            self.assertEqual(node.main(["x", "service-run", spec_file.as_posix()]), node.EXIT_REFUSED)
        supervise.assert_not_called()
        self.assertEqual([p.name for p in (self.root / "run").glob("spec-*.json")], [])

    def test_stale_records_are_settled_and_pruned(self):
        stale = [self.record("hl-svc-qwen-replicas", state="ready", pid=os.getpid(), boot_id=OTHER_BOOT),
                 self.record("hl-svc-qwen-replicas", state="queued", boot_id=OTHER_BOOT)]
        old = [self.record("hl-svc-qwen-replicas", state="stopped", queued_at=index) for index in range(8)]
        self.assertEqual(self.run_with("stopped", "requested"), 0)
        for lid in stale:
            record = node.lease.read_json(node.lease.record_path(self.root, lid))
            self.assertEqual(record["state"], "failed")
            self.assertIn("superseded", record["reason"])
        remaining = [state for _, state in node.service_records(self.root, "qwen-replicas")]
        self.assertEqual(len(remaining), node.KEEP_RECORDS + 1)
        self.assertFalse(any(node.lease.record_path(self.root, lid).exists() for lid in old[:5]))

    def test_a_live_run_of_the_same_unit_refuses_a_second_start(self):
        # A hand-run copy beside the active unit: its record's process is alive on this boot, so it is not stale.
        lid = self.record("hl-svc-qwen-replicas", state="ready", pid=os.getpid())
        with self.assertRaisesRegex(ValueError, "lease is live"):
            self.run_with("stopped", "requested")
        self.assertEqual(node.lease.read_json(node.lease.record_path(self.root, lid))["state"], "ready")

    def test_status_settles_records_of_an_inactive_unit(self):
        lid = self.record("hl-svc-qwen-replicas", state="ready", pid=os.getpid())
        self.assertEqual(node.run("service-status", self.payload)["lease"]["state"], "failed")
        self.assertEqual(node.lease.read_json(node.lease.record_path(self.root, lid))["state"], "failed")


class LeaseExclusionTests(NodeCase):
    def setUp(self):
        super().setUp()
        self.lab = self.base / "lab"
        (self.lab / ".gpu-turn").mkdir(parents=True)
        self.spec = {"root": self.root.as_posix(), "lab_dir": self.lab.as_posix(), "label": "hl-qwen-test",
                     "kind": "single", "processes": [{"name": "engine", "machine": "local",
                     "argv": [str(Path(sys.executable).resolve())], "env": {},
                     "ready": "qwen-stderr", "health_url": "http://127.0.0.1:18501/health"}]}
        self.fake_os = SimpleNamespace(**dict(vars(os), name="posix"))

    def lease(self, **changes):
        process = Mock(pid=123, poll=Mock(return_value=0))
        with patch.object(node.lease, "os", self.fake_os), \
                patch.object(node.lease.subprocess, "Popen", return_value=process) as start:
            reply = node.lease.run("lease", dict(self.spec, **changes))
        if start.called:
            node.lease._launches.remove(process)
        return reply, start.called

    def test_gpu_leases_refuse_while_a_unit_is_installed(self):
        (self.root / "etc" / "service-qwen-replicas.json").write_text("{}")
        for kind in ("single", "pair"):
            reply, started = self.lease(kind=kind)
            self.assertIn("installed on this root", reply["error"])
            self.assertFalse(started)
        reply, started = self.lease(kind="none", lab_dir=None, label="hl-router-abcd")
        self.assertNotIn("error", reply)

    def test_leases_refuse_while_a_unit_runs_and_stale_records_do_not_block(self):
        lid = self.record("hl-svc-qwen-replicas", state="ready")
        for changes in ({}, {"kind": "none", "lab_dir": None, "label": "hl-router-abcd"}):
            reply, started = self.lease(**changes)
            self.assertIn("unit is running", reply["error"])
            self.assertFalse(started)
        node.lease.write_json(node.lease.record_path(self.root, lid),
                              {"lease": lid, "label": "hl-svc-qwen-replicas", "state": "ready", "boot_id": OTHER_BOOT})
        reply, started = self.lease()
        self.assertNotIn("error", reply)
        self.assertTrue(started)
        self.assertEqual(node.lease.read_json(node.lease.record_path(self.root, lid))["state"], "failed")

    def test_service_labels_are_reserved(self):
        reply, started = self.lease(kind="none", lab_dir=None, label="hl-svc-qwen-replicas")
        self.assertIn("reserved", reply["error"])
        self.assertFalse(started)

    def test_pair_peer_refuses_on_a_service_root(self):
        (self.root / "etc" / "service-qwen-replicas.json").write_text("{}")
        payload = {"root": self.root.as_posix(), "lease": "a" * 32, "process": self.spec["processes"][0]}
        with patch.object(node.lease, "os", self.fake_os), patch.object(node.lease, "Child") as child:
            with self.assertRaisesRegex(ValueError, "installed on this root"):
                node.lease.peer_run(payload, stream=io.BytesIO())
        child.assert_not_called()

    def test_lab_install_and_uninstall_refuse_while_a_unit_is_installed(self):
        (self.root / ".hyperloom-root").write_text("b" * 32)
        (self.root / "src").mkdir()
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        spec_file.write_text("{}")
        with self.assertRaisesRegex(ValueError, "service remove"):
            node_install.checked_root({"root": self.root.as_posix(), "lab_dir": self.lab.as_posix()}, create=True)
        self.assertIsNone(json.loads((self.root / "etc" / "install-policy.json").read_text())["lab_dir"])
        node_install.checked_root({"root": self.root.as_posix(), "lab_dir": None}, create=True)
        self.assertIn("service remove", node_install.run("uninstall", {"root": self.root.as_posix()})["error"])
        self.assertTrue(self.root.exists())
        spec_file.unlink()
        self.assertNotIn("error", node_install.run("uninstall", {"root": self.root.as_posix()}))

    def test_install_ignores_service_records_of_a_previous_boot(self):
        (self.root / ".hyperloom-root").write_text("b" * 32)
        lid = self.record("hl-svc-qwen-replicas", state="ready", boot_id=OTHER_BOOT, profile="qwen-replicas")
        self.assertEqual(node_install.active_leases(self.root), [])
        node.lease.write_json(node.lease.record_path(self.root, lid),
                              {"lease": lid, "label": "hl-svc-qwen-replicas", "state": "ready", "pid": os.getpid()})
        self.assertEqual(len(node_install.active_leases(self.root)), 1)


class StatusRemote(FakeRemote):
    states = {}
    tools = {}
    release_ids = {}

    def call(self, verb, payload, timeout=60):
        if verb == "service-status":
            self.calls.append((self.machine.id, verb, payload))
            sequence = self.states.get(self.machine.id, ["inactive"])
            return {"systemd": {"ActiveState": sequence.pop(0) if len(sequence) > 1 else sequence[0]}}
        if verb == "version" and self.machine.id in self.tools:
            self.tool = self.tools[self.machine.id]
        reply = super().call(verb, payload, timeout)
        if verb == "releases" and self.machine.id in self.release_ids:
            reply = {"active": {"qwen-replicas": self.release_ids[self.machine.id]},
                     "releases": [{"release_id": self.release_ids[self.machine.id], "manifest": {}}]}
        return reply


class ControlServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls, self.fail = [], set()
        StatusRemote.states, StatusRemote.tools, StatusRemote.release_ids = {}, {}, {}
        base = cluster_fixture()
        self.cluster = replace(base, machines={mid: replace(m, lab_mode=False) for mid, m in base.machines.items()})
        for patcher in (patch.object(secrets, "_directory", return_value=Path(temporary.name) / "stored"),
                        patch.object(secrets, "ensure", return_value=SECRET),
                        patch.object(orchestrate.remote, "Remote",
                                     side_effect=lambda machine, known: StatusRemote(machine, self.calls, {}, self.fail)),
                        patch.object(service.render, "render_processes", side_effect=self.processes)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def processes(self, cluster, profile):
        self.selected = cluster
        return [ProcessSpec(mid, "replica", ["/bin/engine", "--http-port", "18501"], {}, "qwen-stderr")
                for mid in cluster.profiles[profile]["nodes"]]

    def verbs(self, verb):
        return [(mid, payload) for mid, name, payload in self.calls if name == verb]

    def test_lab_mode_default_and_pair_are_refused_before_remote_work(self):
        with self.assertRaisesRegex(OrchestrationError, "lab_mode is true"):
            service.run(cluster_fixture(), "install", "qwen-replicas")
        with self.assertRaisesRegex(OrchestrationError, "qwen-replicas only"):
            service.run(cluster_fixture(pair=True), "install", "ds41-pair")
        self.assertEqual(self.calls, [])

    def test_install_pushes_keys_and_sends_rendered_processes(self):
        result = service.run(self.cluster, "install", "qwen-replicas", enable=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["release_id"], RELEASE)
        self.assertEqual(self.selected.profiles["qwen-replicas"]["release_id"], RELEASE)
        installs = self.verbs("service-install")
        self.assertEqual([mid for mid, _ in installs], ["halo-1", "halo-2"])
        for mid, payload in installs:
            self.assertTrue(payload["enable"])
            self.assertEqual(payload["processes"][0]["health_url"],
                             "http://" + self.cluster.machines[mid].http_address + ":18501/health")
            self.assertNotIn(SECRET, json.dumps(payload))
        self.assertEqual(len(self.verbs("put-secret")), 2)
        self.assertEqual([mid for mid, _ in self.verbs("version")], ["halo-1", "halo-2"])

    def test_install_and_up_share_one_prelaunch_helper(self):
        class Stop(Exception):
            pass
        with patch.object(service, "prelaunch", side_effect=Stop) as shared, self.assertRaises(Stop):
            service.run(self.cluster, "install", "qwen-replicas")
        self.assertIs(service.prelaunch, orchestrate.prelaunch)
        args = shared.call_args.args
        self.assertEqual((args[1], args[3], args[5]), ("qwen-replicas", "qwen-replicas", ["halo-1", "halo-2"]))
        self.assertIn("prelaunch(cluster, profile, settings, kind, clients, mids)",
                      Path(orchestrate.__file__).read_text(encoding="utf-8").split("\ndef up(", 1)[1])

    def test_install_runs_the_tool_and_asset_checks_before_any_key(self):
        StatusRemote.tools = {"halo-2": "0" * 64}
        with self.assertRaisesRegex(OrchestrationError, "node tool differs"):
            service.run(self.cluster, "install", "qwen-replicas")
        self.assertEqual(self.verbs("put-secret") + self.verbs("service-install"), [])
        StatusRemote.tools = {}
        with patch.object(orchestrate.assets, "inspect", side_effect=OrchestrationError("asset changed")) as inspect, \
                self.assertRaisesRegex(OrchestrationError, "asset changed"):
            service.run(self.cluster, "install", "qwen-replicas")
        self.assertTrue(inspect.call_args.kwargs["strict"])
        self.assertEqual(self.verbs("put-secret") + self.verbs("service-install"), [])

    def test_install_release_mismatch_names_each_node(self):
        StatusRemote.release_ids = {"halo-2": "b" * 12}
        with self.assertRaisesRegex(OrchestrationError, "same release id: halo-1 " + RELEASE + ", halo-2 " + "b" * 12):
            service.run(self.cluster, "install", "qwen-replicas")
        self.assertEqual(self.verbs("service-install"), [])

    def test_each_node_result_is_reported_when_one_fails(self):
        self.fail.add(("halo-1", "service-install"))
        result = service.run(self.cluster, "install", "qwen-replicas")
        self.assertFalse(result["ok"])
        self.assertIn("fixture failure", result["nodes"]["halo-1"]["error"])
        self.assertNotIn("error", result["nodes"]["halo-2"])
        self.assertEqual([mid for mid, _ in self.verbs("service-install")], ["halo-1", "halo-2"])
        self.fail.add(("halo-2", "service-start"))
        result = service.run(self.cluster, "start", "qwen-replicas")
        self.assertEqual((result["ok"], "error" in result["nodes"]["halo-1"], "error" in result["nodes"]["halo-2"]),
                         (False, False, True))

    def test_cli_exits_nonzero_when_a_node_failed(self):
        reply = {"profile": "qwen-replicas", "nodes": {"halo-1": {"error": "x"}}, "ok": False}
        with patch.object(service, "run", return_value=reply), patch.object(cli, "_cluster"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["service", "start", "qwen-replicas"]), 1)
        self.assertEqual(json.loads(out.getvalue())["nodes"]["halo-1"]["error"], "x")

    def test_stop_waits_beyond_the_unit_stop_timeout(self):
        unit = re.search(r"TimeoutStopSec=(\d+)", node.UNIT).group(1)
        self.assertGreater(service.STOP_WAIT_S, int(unit))
        StatusRemote.states = {"halo-1": ["deactivating", "deactivating", "inactive"], "halo-2": ["failed"]}
        result = service.run(self.cluster, "stop", "qwen-replicas", poll_s=0)
        self.assertTrue(result["ok"])
        self.assertEqual({mid: row["active_state"] for mid, row in result["nodes"].items()},
                         {"halo-1": "inactive", "halo-2": "failed"})
        self.assertEqual([payload for _, payload in self.verbs("service-stop")],
                         [{"root": m.install_root, "profile": "qwen-replicas"} for m in self.cluster.machines.values()])
        StatusRemote.states = {"halo-1": ["deactivating"]}
        result = service.run(self.cluster, "stop", "qwen-replicas", poll_s=0, stop_wait_s=0)
        self.assertFalse(result["ok"])
        self.assertIn("still deactivating", result["nodes"]["halo-1"]["error"])

    def test_other_operations_send_profile_only(self):
        service.run(self.cluster, "start", "qwen-replicas")
        self.assertEqual([(verb, payload) for _, verb, payload in self.calls],
                         [("service-start", {"root": m.install_root, "profile": "qwen-replicas"})
                          for m in self.cluster.machines.values()])
        with self.assertRaises(ValueError):
            service.run(self.cluster, "restart", "qwen-replicas")


if __name__ == "__main__":
    unittest.main()
