from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyperloom import cli


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Spec:
    machine_id: str = "halo-1"
    role: str = "replica"
    argv: tuple = ("binary",)
    env: dict = None
    ready: str = "qwen-stderr"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyperloom cli tests ")
        self.directory = Path(self.temporary.name)
        self.cluster = SimpleNamespace(profiles={"qwen-replicas": {}}, machines={})
        self.config = SimpleNamespace(load=Mock(return_value=self.cluster))
        self.render = SimpleNamespace(render_router=Mock(return_value={"schema": "test"}),
                                      validate_router=Mock(),
                                      render_processes=Mock(return_value=[Spec(env={})]))
        self.real_module = importlib.import_module
        self.modules = {"hyperloom.config": self.config, "hyperloom.render": self.render}
        self.module_patch = patch.object(cli, "_module", side_effect=lambda name:
                                         self.modules.get(name) or self.real_module(name))
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.temporary.cleanup()

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = cli.main(list(args))
        return status, out.getvalue(), err.getvalue()

    def test_config_validate_and_failure(self):
        path = self.directory / "cluster example.json"
        status, out, err = self.invoke("config", "validate", str(path))
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out), {"valid": True})
        self.config.load.assert_called_once_with(path)
        self.config.load.side_effect = ValueError("bad schema")
        status, _, err = self.invoke("config", "validate", str(path))
        self.assertEqual(status, 1)
        self.assertIn("bad schema", err)

    def test_render_serializes_router_and_processes(self):
        status, out, err = self.invoke("render", "qwen-replicas", "--config", "example.json")
        self.assertEqual(status, 0, err)
        rendered = json.loads(out)
        self.assertEqual(rendered["processes"][0]["machine_id"], "halo-1")
        self.assertEqual(rendered["router"], {"schema": "test"})
        self.render.validate_router.assert_called_once()

    def test_missing_module_error_is_clear(self):
        self.module_patch.stop()
        with patch.object(cli.importlib, "import_module", side_effect=ModuleNotFoundError("missing")):
            status, _, err = self.invoke("config", "validate", "example.json")
        self.module_patch.start()
        self.assertEqual(status, 1)
        self.assertIn("hyperloom.config is unavailable", err)
        self.assertNotIn("Traceback", err)

    def test_phase_placeholders(self):
        for command in ("up", "down"):
            self.assertEqual(self.invoke(command), (0, "Phase 1a\n", ""))

    def test_platform_default_paths(self):
        with patch.object(cli, "os", SimpleNamespace(name="nt", environ={"APPDATA": str(self.directory)})):
            self.assertEqual(cli.config_path(), self.directory / "hyperloom" / "cluster.json")
        with patch.object(cli, "os", SimpleNamespace(name="posix")), patch.object(
                cli.Path, "home", return_value=self.directory):
            self.assertEqual(cli.config_path(), self.directory / ".config" / "hyperloom" / "cluster.json")

    def test_local_doctor_direct_subprocess(self):
        completed = subprocess.CompletedProcess([], 0, '{"commands":[]}', "")
        with patch.object(cli.subprocess, "run", return_value=completed) as run, patch.object(
                cli.Path, "is_file", return_value=True):
            status, out, err = self.invoke("doctor")
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out), {"commands": []})
        self.assertEqual(run.call_args.args[0][2], "doctor")
        self.assertTrue(Path(run.call_args.args[0][0]).is_absolute())
        self.assertEqual(run.call_args.kwargs["input"], "{}")

    def test_remote_doctor_uses_fixed_verb(self):
        machine = SimpleNamespace(access="ssh")
        self.cluster.machines["halo-1"] = machine
        remote = SimpleNamespace(Remote=Mock(), tool_digest=Mock(return_value="d" * 64))
        client = remote.Remote.return_value
        version = {"checks": {"tool": {"status": "ok", "value": "d" * 64}}}
        self.modules["hyperloom.remote"] = remote
        # Fresh machine: no node tool yet, so doctor bootstraps it once, then runs the read-only checks.
        client.has_tool.return_value = False
        client.call.side_effect = [version, {"commands": []}]
        status, out, err = self.invoke("doctor", "halo-1")
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out), {"commands": [], "node_tool": {
            "expected": "d" * 64, "deployed": "d" * 64, "bootstrapped": True, "status": "current"}})
        client.bootstrap.assert_called_once_with(timeout=60)
        self.assertEqual(client.call.call_args_list[0].args[0], "version")
        client.call.assert_called_with("doctor", {}, timeout=60)

    def test_remote_doctor_reports_stale_tool_without_replacing_it(self):
        self.cluster.machines["halo-1"] = SimpleNamespace(access="ssh")
        remote = SimpleNamespace(Remote=Mock(), tool_digest=Mock(return_value="d" * 64))
        client = remote.Remote.return_value
        client.has_tool.return_value = True
        client.call.side_effect = [{"checks": {"tool": {"status": "ok", "value": "e" * 64}}}, {"commands": []}]
        self.modules["hyperloom.remote"] = remote
        status, out, err = self.invoke("doctor", "halo-1")
        self.assertEqual(status, 0, err)
        tool = json.loads(out)["node_tool"]
        self.assertEqual((tool["status"], tool["bootstrapped"], tool["deployed"]), ("stale", False, "e" * 64))
        self.assertIn("hyperloom nodes update", tool["hint"])
        # The live-lease guard of `nodes update` is never bypassed: doctor does not redeploy.
        client.bootstrap.assert_not_called()

    def test_unknown_machine_fails_clearly(self):
        status, _, err = self.invoke("doctor", "halo-2")
        self.assertEqual(status, 1)
        self.assertIn("unknown machine", err)

    def test_stopped_router_status_and_stop(self):
        with patch.object(cli, "_runtime", return_value=self.directory):
            for operation in ("status", "stop"):
                status, out, err = self.invoke("router", operation)
                self.assertEqual(status, 0, err)
                self.assertEqual(json.loads(out)["status"], "stopped")

    def test_ambiguous_profile_requires_choice(self):
        self.cluster.profiles["ds41-pair"] = {}
        status, _, err = self.invoke("router", "start")
        self.assertEqual(status, 1)
        self.assertIn("--profile", err)

    def test_router_controller_lifecycle_with_real_router(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.render.render_router.return_value = {
            "schema": "tensormeld/replica-router-v1",
            "listen": {"address": "127.0.0.1", "port": port},
            "replicas": [{"id": "halo-1", "node": "halo-1", "address": "127.0.0.1",
                          "port": 1, "max_inflight": 1}],
        }
        with patch.object(cli, "_runtime", return_value=self.directory):
            try:
                for _ in range(2):
                    status, out, err = self.invoke("router", "start", "--profile", "qwen-replicas")
                    if status:
                        log = (self.directory / "controller.log").read_text(errors="replace")
                        self.fail(err + log)
                    self.assertEqual(json.loads(out)["status"], "running")
                    self.assertEqual(json.loads(self.invoke("router", "status")[1])["status"], "running")
                    self.assertEqual(self.invoke("router", "start")[0], 1)
                    status, out, err = self.invoke("router", "stop")
                    self.assertEqual(status, 0, err)
                    self.assertEqual(json.loads(out)["status"], "stopped")
                    final = json.loads((self.directory / "router-final.json").read_text())
                    self.assertEqual(final["schema"], "tensormeld/replica-router-status-v1")
                with socket.socket() as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))
            finally:
                self.invoke("router", "stop")
                for helper in cli._controllers[:]:
                    helper.wait(timeout=10)
                    cli._controllers.remove(helper)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer")
    def test_installer_whatif_with_spaces_and_offline_wheels(self):
        checkout = self.directory / "checkout with spaces"
        windows = checkout / "windows"
        windows.mkdir(parents=True)
        shutil.copyfile(ROOT / "windows" / "install.ps1", windows / "install.ps1")
        (checkout / "wheels").mkdir()
        local = self.directory / "local app data"
        roaming = self.directory / "roaming app data"
        env = dict(os.environ, LOCALAPPDATA=str(local), APPDATA=str(roaming))
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(windows / "install.ps1"), "-WhatIf"],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(local / "hyperloom" / "venv"), result.stdout)
        self.assertIn("--no-index", result.stdout)
        self.assertIn("--find-links", result.stdout)
        self.assertIn('"-e"', result.stdout)
        self.assertIn("checkout with spaces", result.stdout)
        self.assertIn("-m hyperloom ui", result.stdout)
        self.assertFalse(local.exists())
        self.assertFalse(roaming.exists())

    def test_module_entry_point(self):
        result = subprocess.run([sys.executable, "-m", "hyperloom", "up"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Phase 1a")


if __name__ == "__main__":
    unittest.main()
