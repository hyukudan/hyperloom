"""Non-lab systemd --user mode: refusals, unit content, fake systemctl, supervise exit codes."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hyperloom import secrets, service
from hyperloom.orchestrate import OrchestrationError
from hyperloom.render import ProcessSpec
from tests.test_orchestrate import RELEASE, FakeRemote, cluster_fixture


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "node"))
loader = importlib.util.spec_from_file_location("service_under_test", REPO / "node" / "hyperloom_service.py")
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
SECRET = "f" * 64


class FakeSystemctl:
    def __init__(self, active="inactive", fail=()):
        self.calls, self.active, self.fail = [], active, set(fail)

    def __call__(self, argv, **_):
        self.calls.append(argv[2:])
        code = 1 if argv[2] in self.fail else 0
        out = self.active + "\n" if argv[2] == "is-active" else "ActiveState=active\nNRestarts=0\n"
        return subprocess.CompletedProcess(argv, code, out, "Failed to connect to bus" if code else "")


class NodeServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "root"
        for name in ("etc", "run", "log", "releases/" + RELEASE + "/bin"):
            (self.root / name).mkdir(parents=True)
        (self.root / ".hyperloom-root").write_text("a" * 32)
        (self.root / "etc" / "install-policy.json").write_text(json.dumps({"lab_dir": None, "asset_paths": []}))
        self.binary = self.root / "releases" / RELEASE / "bin" / "strix-hip-decode-token"
        self.binary.write_bytes(b"")
        self.units = self.base / "units"
        self.proc = {"name": "halo-1", "machine": "local", "argv": [self.binary.as_posix(), "--http-port", "18501"],
                     "env": {"STRIX_COHORT_ROWS": "32"}, "ready": "qwen-stderr",
                     "health_url": "http://192.0.2.10:18501/health",
                     "key_file": (self.root / "etc" / "keys" / "key-a").as_posix(), "ready_timeout_s": 600}
        self.payload = {"root": self.root.as_posix(), "profile": "qwen-replicas", "processes": [self.proc]}
        self.systemctl = FakeSystemctl()
        for target, value in (("unit_dir", lambda: self.units),):
            patcher = patch.object(node, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(node.subprocess, "run", side_effect=self.systemctl),
                        patch.object(node.shutil, "which", return_value="/usr/bin/systemctl"),
                        patch.object(node.sys, "executable", "/usr/bin/python3")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_lab_root_and_lab_payload_are_refused(self):
        self.assertIn("refused", node.run("service-install", dict(self.payload, lab_dir="/opt/lab"))["error"])
        (self.root / "etc" / "install-policy.json").write_text(json.dumps({"lab_dir": (self.base / "lab").as_posix()}))
        for verb in ("service-install", "service-start"):
            self.assertIn("lab mode", node.run(verb, self.payload)["error"])
        self.assertEqual(self.systemctl.calls, [])

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
        if sys.platform == "win32" and not self.root.as_posix().startswith("/"):
            # Unit paths are POSIX; exercise content with the plain-path check bypassed.
            patcher = patch.object(node, "safe", side_effect=lambda value: value)
            patcher.start()
            self.addCleanup(patcher.stop)
        reply = node.run("service-install", self.payload)
        self.assertNotIn("error", reply)
        unit = (self.units / "hl-qwen-replicas.service").read_text()
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        self.assertEqual(unit, node.UNIT.format(profile="qwen-replicas", root=self.root,
                                                python=Path("/usr/bin/python3").resolve(), spec=spec_file))
        for line in ("KillMode=mixed", "Restart=on-failure", "StartLimitBurst=3", "TimeoutStopSec=90"):
            self.assertIn(line, unit)
        self.assertNotIn(SECRET, unit + spec_file.read_text())
        self.assertNotIn("Environment", unit)
        self.assertEqual(self.systemctl.calls, [["show-environment"], ["is-active", "hl-qwen-replicas.service"],
                                                ["daemon-reload"]])
        for name in node.COPIED:
            self.assertTrue((self.root / "bin" / name).is_file())

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

    def test_start_stop_go_through_systemctl_user(self):
        for verb, word in (("service-start", "start"), ("service-stop", "stop")):
            self.assertEqual(node.run(verb, self.payload)["requested"], word)
        self.assertEqual(self.systemctl.calls, [["start", "hl-qwen-replicas.service"],
                                                ["stop", "hl-qwen-replicas.service"]])

    def test_service_run_exit_code_follows_supervise_reason(self):
        _, spec = node.build_spec(self.root, self.payload)
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        node.lease.write_json(spec_file, spec)
        for state, reason, code in (("stopped", "requested", 0), ("failed", "process-exit:halo-1", 1),
                                    ("stopped", "deadline", 1)):
            def supervise(filename):
                lid = node.lease.read_json(filename)["lease"]
                record = node.lease.read_json(node.lease.record_path(self.root, lid))
                self.assertEqual((record["label"], record["kind"]), ("hl-svc-qwen-replicas", "service"))
                record.update(state=state, reason=reason)
                node.lease.write_json(node.lease.record_path(self.root, lid), record)
            with self.subTest(reason=reason), patch.object(node.lease, "supervise", side_effect=supervise):
                self.assertEqual(node.service_run(spec_file.as_posix()), code)
        status = node.run("service-status", self.payload)
        self.assertEqual(status["lease"]["label"], "hl-svc-qwen-replicas")
        self.assertEqual(status["systemd"]["ActiveState"], "active")

    def test_service_run_rejects_lease_specs(self):
        spec_file = self.root / "etc" / "service-qwen-replicas.json"
        node.lease.write_json(spec_file, {"root": self.root.as_posix(), "kind": "single", "lab_dir": "/opt/lab"})
        with self.assertRaises(ValueError):
            node.service_run(spec_file.as_posix())


class ControlServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls = []
        base = cluster_fixture()
        self.cluster = replace(base, machines={mid: replace(m, lab_mode=False) for mid, m in base.machines.items()})
        for patcher in (patch.object(secrets, "_directory", return_value=Path(temporary.name) / "stored"),
                        patch.object(secrets, "ensure", return_value=SECRET),
                        patch.object(service.orchestrate.remote, "Remote",
                                     side_effect=lambda machine, known: FakeRemote(machine, self.calls, {}, set())),
                        patch.object(service.render, "render_processes", side_effect=self.processes)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def processes(self, cluster, profile):
        self.selected = cluster
        return [ProcessSpec(mid, "replica", ["/bin/engine", "--http-port", "18501"], {}, "qwen-stderr")
                for mid in cluster.profiles[profile]["nodes"]]

    def test_lab_mode_default_and_pair_are_refused_before_remote_work(self):
        with self.assertRaisesRegex(OrchestrationError, "lab_mode is true"):
            service.run(cluster_fixture(), "install", "qwen-replicas")
        with self.assertRaisesRegex(OrchestrationError, "qwen-replicas only"):
            service.run(cluster_fixture(pair=True), "install", "ds41-pair")
        self.assertEqual(self.calls, [])

    def test_install_pushes_keys_and_sends_rendered_processes(self):
        result = service.run(self.cluster, "install", "qwen-replicas", enable=True)
        self.assertEqual(result["release_id"], RELEASE)
        self.assertEqual(self.selected.profiles["qwen-replicas"]["release_id"], RELEASE)
        installs = [(mid, payload) for mid, verb, payload in self.calls if verb == "service-install"]
        self.assertEqual([mid for mid, _ in installs], ["halo-1", "halo-2"])
        for mid, payload in installs:
            self.assertTrue(payload["enable"])
            self.assertEqual(payload["processes"][0]["health_url"],
                             "http://" + self.cluster.machines[mid].http_address + ":18501/health")
            self.assertNotIn(SECRET, json.dumps(payload))
        puts = [verb for _, verb, _ in self.calls if verb == "put-secret"]
        self.assertEqual(len(puts), 2)

    def test_other_operations_send_profile_only(self):
        service.run(self.cluster, "stop", "qwen-replicas")
        self.assertEqual([(verb, payload) for _, verb, payload in self.calls],
                         [("service-stop", {"root": m.install_root, "profile": "qwen-replicas"})
                          for m in self.cluster.machines.values()])
        with self.assertRaises(ValueError):
            service.run(self.cluster, "restart", "qwen-replicas")


if __name__ == "__main__":
    unittest.main()
