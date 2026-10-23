import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hyperloom import remote


FIXTURE = Path(__file__).parent / "fixtures" / "ssh" / "fake_ssh.py"


def hostkey(fill=b"a"):
    algorithm = b"ssh-ed25519"
    blob = len(algorithm).to_bytes(4, "big") + algorithm + (32).to_bytes(4, "big") + fill * 32
    return remote.HostKey("ssh-ed25519", base64.b64encode(blob).decode(),
                          "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="))


class OpenSshToolTests(unittest.TestCase):
    def test_windows_prefers_native_openssh_else_path(self):
        with (patch.object(remote.shutil, "which", return_value="/x/ssh"),
              patch.object(remote.os, "name", "posix")):
            self.assertEqual(remote.openssh_tool("ssh"), "/x/ssh")
        with (patch.object(remote.shutil, "which", return_value="/x/msys/ssh"),
              patch.object(remote.os, "name", "nt"),
              patch.object(remote.os.path, "isfile", return_value=True),
              patch.dict(os.environ, {"SystemRoot": r"C:\Windows"})):
            self.assertTrue(remote.openssh_tool("ssh").lower().endswith(r"openssh\ssh.exe"))


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom ssh ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / "calls.jsonl"
        self.known = remote.KnownHosts(self.directory / "known hosts")
        self.key = hostkey()
        self.machine = SimpleNamespace(id="halo-1", http_address="192.0.2.1",
                                       ssh_target="operator@halo-1", ssh_port=2222)
        self.client = remote.Remote(self.machine, self.known)
        self.env = patch.dict(os.environ, {"HYPERLOOM_FAKE_KEY": self.key.key_b64,
                                          "HYPERLOOM_FAKE_LOG": str(self.log),
                                          "HYPERLOOM_FAKE_MODE": "ok"})
        self.env.start()
        self.addCleanup(self.env.stop)
        original = subprocess.Popen
        self.children = []

        def launch(argv, **kwargs):
            if argv[0] not in {"ssh", "scp", "ssh-keyscan"}:
                return original(argv, **kwargs)
            kwargs.pop("executable", None)  # the fake replaces the resolved OpenSSH binary
            child = original([sys.executable, str(FIXTURE), *argv], **kwargs)
            self.children.append(child)
            return child

        self.popen = patch.object(remote.subprocess, "Popen", side_effect=launch)
        self.mock_popen = self.popen.start()
        self.addCleanup(self.popen.stop)

    def pin(self):
        self.known.pin(self.machine.id, self.machine.http_address, self.machine.ssh_port, self.key)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_keyscan_candidates_and_fingerprint(self):
        self.assertEqual(remote.keyscan("192.0.2.1", 2222), [self.key])
        self.assertEqual(self.calls()[0]["argv"], ["ssh-keyscan", "-t", "ed25519,ecdsa,rsa",
                                                 "-p", "2222", "192.0.2.1"])
        self.assertFalse(self.known.path.exists())

    def test_keyscan_empty_and_malformed(self):
        for mode in ("empty", "malformed_scan"):
            with self.subTest(mode=mode), patch.dict(os.environ, HYPERLOOM_FAKE_MODE=mode):
                with self.assertRaises(remote.RemoteError):
                    remote.keyscan("192.0.2.1", 2222)

    def test_windows_keyscan_falls_back_to_path_tool(self):
        native, git = r"C:\Windows\System32\OpenSSH\ssh-keyscan.exe", r"C:\Git\usr\bin\ssh-keyscan.exe"
        line = f"[192.0.2.1]:2222 ssh-ed25519 {self.key.key_b64}\n".encode()
        with (patch.object(remote.os, "name", "nt"), patch.object(remote, "openssh_tool", return_value=native),
              patch.object(remote.shutil, "which", return_value=git), patch.object(remote.os.path, "isfile", return_value=True),
              patch.object(remote, "_run", side_effect=[remote.RemoteError("exit 1"), line]) as run):
            self.assertEqual(remote.keyscan("192.0.2.1", 2222), [self.key])
        self.assertEqual(run.call_args.kwargs["executable"], git)
        # Only Git\cmd on PATH: the tool beside it in usr\bin.
        which = {"ssh-keyscan": None, "git": r"C:\Git\cmd\git.exe"}.get
        with (patch.object(remote.os, "name", "nt"), patch.object(remote, "openssh_tool", return_value=native),
              patch.object(remote.shutil, "which", side_effect=which), patch.object(remote.os.path, "isfile", return_value=True),
              patch.object(remote, "_run", side_effect=[remote.RemoteError("exit 1"), line]) as run):
            self.assertEqual(remote.keyscan("192.0.2.1", 2222), [self.key])
        self.assertEqual(run.call_args.kwargs["executable"], git)
        # The same binary found on PATH is not retried.
        with (patch.object(remote.os, "name", "nt"), patch.object(remote, "openssh_tool", return_value=native),
              patch.object(remote.shutil, "which", side_effect={"ssh-keyscan": native.upper(), "git": None}.get),
              patch.object(remote.os.path, "isfile", return_value=True),
              patch.object(remote, "_run", side_effect=remote.RemoteError("exit 1")) as run):
            with self.assertRaises(remote.RemoteError):
                remote.keyscan("192.0.2.1", 2222)
        self.assertEqual(run.call_count, 1)

    def test_unknown_key_refused_before_connection(self):
        with self.assertRaisesRegex(remote.RemoteError, "unknown host key"):
            self.client.call("doctor", {}, 2)
        self.mock_popen.assert_not_called()

    def test_pin_is_exact_idempotent_and_rejects_replacement(self):
        self.pin()
        original = self.known.path.read_bytes()
        self.pin()
        self.assertEqual(self.known.path.read_bytes(), original)
        self.assertIn("[halo-1]:2222", original.decode())
        self.assertIn("[192.0.2.1]:2222", original.decode())
        with self.assertRaisesRegex(remote.RemoteError, "changed"):
            self.known.pin("halo-1", "192.0.2.1", 2222, hostkey(b"b"))
        self.assertEqual(self.known.path.read_bytes(), original)

    def test_wrong_fingerprint_is_not_pinned(self):
        with self.assertRaises(remote.RemoteError):
            self.known.pin("halo-1", "192.0.2.1", 22,
                           remote.HostKey(self.key.algorithm, self.key.key_b64, "SHA256:wrong"))
        self.assertFalse(self.known.path.exists())

    def test_json_roundtrip_quotes_spaces_unicode_and_eof(self):
        self.pin()
        payload = {"lab_dir": "/opt/operator space ' Ω", "nested": ["\"", "\\", "\n", "雪"]}
        reply = self.client.call("doctor", payload, 3)
        self.assertEqual(reply, {"verb": "doctor", "payload": payload})
        command = self.calls()[-1]["argv"][-1]
        self.assertEqual(shlex.split(command), [self.client.node_path, "doctor"])
        self.assertNotIn("雪", command)

    def test_every_call_pins_alias_address_and_nondefault_port(self):
        self.pin()
        self.client.call("gpu", {}, 3)
        for call in self.calls():
            argv = call["argv"]
            self.assertIn("HostKeyAlias=[halo-1]:2222", argv)
            self.assertIn("Hostname=192.0.2.1", argv)
            self.assertEqual(argv[argv.index("-p") + 1], "2222")
            self.assertEqual(argv[argv.index("-F") + 1], str(self.known.config_path))
            self.assertEqual(argv[argv.index("--") + 1], "operator@halo-1")
            for flag in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=none",
                         "BatchMode=yes", "ConnectTimeout=10", "UpdateHostKeys=no"):
                self.assertIn(flag, argv)

    def test_bootstrap_uses_pinned_scp_and_absolute_node_path(self):
        self.pin()
        source = self.directory / "node source"
        source.mkdir()
        for name in remote.NODE_FILES:
            (source / name).write_text("fixture", encoding="utf-8")
        self.client.bootstrap(source, timeout=3)
        calls = self.calls()
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0]["argv"][-1].startswith("umask 077; mkdir"))
        scp = calls[1]["argv"]
        self.assertEqual(scp[0], "scp")
        self.assertEqual(scp[scp.index("-P") + 1], "2222")
        self.assertIn("StrictHostKeyChecking=yes", scp)
        self.assertIn("HostKeyAlias=[halo-1]:2222", scp)
        self.assertEqual(scp[-5:-1], [str((source / name).absolute()) for name in remote.NODE_FILES])
        self.assertEqual(scp[-1], "operator@halo-1:.hyperloom-boot/")
        boot = remote.PurePosixPath(self.client.node_path).parent
        self.assertEqual(shlex.split(calls[2]["argv"][-1]),
                         ["chmod", "700", "--", *(str(boot / name) for name in remote.NODE_FILES)])

    def test_bootstrap_missing_extension_fails_before_connection(self):
        self.pin()
        source = self.directory / "incomplete"
        source.mkdir()
        (source / "hyperloom-node").write_text("fixture", encoding="utf-8")
        with self.assertRaisesRegex(remote.RemoteError, "bootstrap files"):
            self.client.bootstrap(source, timeout=3)
        self.mock_popen.assert_not_called()

    def test_key_changed_after_confirmation_blocks_bootstrap(self):
        self.pin()
        source = self.directory / "node files"
        source.mkdir()
        for name in remote.NODE_FILES:
            (source / name).write_text("fixture", encoding="utf-8")
        with patch.dict(os.environ, HYPERLOOM_FAKE_KEY=hostkey(b"b").key_b64):
            with self.assertRaises(remote.RemoteError):
                self.client.bootstrap(source, timeout=3)
        self.assertEqual(len(self.calls()), 1)

    def test_pairing_binding_cannot_move_address_or_port(self):
        self.pin()
        for field, value in (("http_address", "192.0.2.2"), ("ssh_port", 22)):
            machine = SimpleNamespace(**vars(self.machine))
            setattr(machine, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(remote.RemoteError, "unknown"):
                remote.Remote(machine, self.known).call("version", {}, 2)

    def test_payload_and_verb_validation_precedes_connection(self):
        for verb, payload in (("doctor; id", {}), ("install", {}), ("doctor", []),
                              ("doctor", {"x": "x" * remote.MAX_JSON}),
                              ("doctor", {"x": float("nan")}), ("doctor", {"x": object()})):
            with self.subTest(verb=verb), self.assertRaises(remote.RemoteError):
                self.client.call(verb, payload, 2)
        self.mock_popen.assert_not_called()

    def test_invalid_target_address_port_and_keys(self):
        for field, value in (("ssh_target", "-bad"), ("ssh_target", "halo-1\n"),
                             ("ssh_target", "halo-1 bad"), ("http_address", "-bad"),
                             ("ssh_port", 0), ("ssh_port", True)):
            machine = SimpleNamespace(**vars(self.machine))
            setattr(machine, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(remote.RemoteError):
                remote.Remote(machine, self.known)
        with self.assertRaises(remote.RemoteError):
            remote._hostkey("ssh-ed25519", "bad!")

    def test_bad_replies_and_connection_failure(self):
        self.pin()
        self.client.node_path = "/opt/hyperloom/hyperloom-node"
        for mode in ("eof", "malformed", "wrongtype", "duplicate", "nonutf8", "badexit"):
            with self.subTest(mode=mode), patch.dict(os.environ, HYPERLOOM_FAKE_MODE=mode):
                with self.assertRaises(remote.RemoteError):
                    self.client.call("gpu", {}, 3)

    def test_bounded_stdout_and_stderr(self):
        self.pin()
        for mode in ("huge", "stderr_huge"):
            with self.subTest(mode=mode), patch.dict(os.environ, HYPERLOOM_FAKE_MODE=mode):
                with self.assertRaisesRegex(remote.RemoteError, "size limit"):
                    self.client.call("gpu", {}, 3)

    def test_timeout_kills_child_and_closes_threads(self):
        self.pin()
        with patch.dict(os.environ, HYPERLOOM_FAKE_MODE="sleep"):
            with self.assertRaisesRegex(remote.RemoteError, "timed out"):
                self.client.call("gpu", {}, 0.2)
        self.assertFalse(any(t.name.startswith("Thread-") and t.is_alive()
                             for t in threading.enumerate()))
        self.assertTrue(self.children)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_timeout_kills_ssh_descendant_too(self):
        self.pin()
        child_log = self.directory / "descendant.json"
        with patch.dict(os.environ, HYPERLOOM_FAKE_MODE="descendant", HYPERLOOM_FAKE_CHILD_LOG=str(child_log)):
            with self.assertRaisesRegex(remote.RemoteError, "timed out"):
                self.client.call("doctor", {}, 1)
        self.assertTrue(child_log.exists(), "fixture did not start the descendant")
        child_pid = json.loads(child_log.read_text())["pid"]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, child_pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0,
                                 "ssh descendant survived timeout")
            finally:
                kernel.CloseHandle(handle)

    def test_cancellation_before_and_during_call(self):
        self.pin()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(remote.RemoteError, "cancelled"):
            self.client.call("doctor", {}, 2, cancel_event=cancelled)
        self.mock_popen.assert_not_called()
        cancelled.clear()
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
        try:
            with patch.dict(os.environ, HYPERLOOM_FAKE_MODE="sleep"):
                with self.assertRaisesRegex(remote.RemoteError, "cancelled"):
                    self.client.call("doctor", {}, 3, cancel_event=cancelled)
        finally:
            timer.join()
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_keyboard_interrupt_reaps_child(self):
        self.pin()
        original_sleep = time.sleep

        def interrupt(seconds):
            original_sleep(seconds)
            raise KeyboardInterrupt

        with patch.dict(os.environ, HYPERLOOM_FAKE_MODE="sleep"):
            with patch.object(remote.time, "sleep", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.client.call("gpu", {}, 3)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed")
    def test_real_openssh_configuration_with_spaces_without_connecting(self):
        self.pin()
        self.popen.stop()
        options = self.known.options("halo-1", "192.0.2.1", 2222)
        result = subprocess.run(["ssh", "-G", *options, "-p", "2222", "--", "halo-1"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.decode("utf-8").splitlines()
        settings = dict(line.split(" ", 1) for line in lines if " " in line)
        self.assertEqual(settings["hostname"], "192.0.2.1")
        self.assertEqual(settings["hostkeyalias"], "[halo-1]:2222")
        self.assertEqual(settings["port"], "2222")
        self.assertEqual(settings["stricthostkeychecking"], "true")
        self.assertEqual(settings["batchmode"], "yes")
        self.assertEqual(settings["userknownhostsfile"], str(self.known.path.as_posix()))

    def test_timeout_must_be_finite_positive(self):
        self.pin()
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(remote.RemoteError):
                self.client.call("gpu", {}, timeout)
        self.mock_popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
