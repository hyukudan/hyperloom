"""Pure lease validation on all platforms; fake process groups on POSIX."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "node_lease"
loader = importlib.util.spec_from_file_location("lease_under_test", REPO / "node" / "hyperloom_lease.py")
lease = importlib.util.module_from_spec(loader)
loader.loader.exec_module(lease)


class LeaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / ".hyperloom-root").write_text("synthetic-root-id")
        self.lab = self.base / "lab"
        self.lab.mkdir()
        (self.lab / ".gpu-turn").mkdir()
        self.spec = {"root": str(self.root), "lab_dir": str(self.lab), "label": "hl-qwen-test",
                     "kind": "single", "processes": [{"name": "engine", "machine": "local",
                     "argv": [str(Path(sys.executable).resolve())], "env": {},
                     "ready": "qwen-stderr", "health_url": "http://127.0.0.1:18501/health"}]}


class LeaseLogicTests(LeaseFixture):
    def test_default_deadline(self):
        self.assertEqual(lease.validate_spec(self.spec)["deadline_s"], 1140)

    def test_valid_labels(self):
        for label in ("hl-a", "hl-qwen-halo-1-1234", "hl-" + "a" * 40):
            self.spec["label"] = label
            self.assertEqual(lease.validate_spec(self.spec)["label"], label)

    def test_invalid_labels(self):
        for label in ("qwen-test", "hl-", "hl-UPPER", "hl-a_b", "hl-" + "a" * 41, "hl-a\n"):
            self.spec["label"] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                lease.validate_spec(self.spec)

    def test_deadline_requires_approval_for_single_and_pair(self):
        for kind in ("single", "pair"):
            self.spec.update(kind=kind, deadline_s=1141)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                lease.validate_spec(self.spec)

    def test_approved_deadline(self):
        self.spec.update(deadline_s=1500, approval={"lease_s": 1560, "end_epoch": 9999, "ref": "coord synthetic ACK"})
        self.assertEqual(lease.validate_spec(self.spec, now=100)["deadline_s"], 1500)

    def test_invalid_approvals(self):
        for changes in ({"lease_s": 1199}, {"lease_s": True}, {"lease_s": 1200.0},
                        {"end_epoch": 100}, {"ref": ""}, {"ref": " "}, {"end_epoch": float("inf")}):
            approval = {"lease_s": 1200, "end_epoch": 1000, "ref": "synthetic approval"}
            approval.update(changes)
            self.spec["approval"] = approval
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                lease.validate_spec(self.spec, now=100)

    def test_bad_deadlines(self):
        for deadline in (0, -1, True, "1140", float("nan"), float("inf")):
            self.spec["deadline_s"] = deadline
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                lease.validate_spec(self.spec)

    def test_gpu_lease_requires_lab(self):
        self.spec["lab_dir"] = None
        with self.assertRaises(ValueError):
            lease.validate_spec(self.spec)
        self.spec["kind"] = "none"
        self.assertEqual(lease.validate_spec(self.spec)["kind"], "none")

    def test_environment_is_clean_and_release_loader_path_allowed(self):
        with patch.dict(os.environ, {"AMBIENT_SECRET": "should-not-propagate", "LD_PRELOAD": "bad"}):
            env = lease.environment({"STRIX_TEST": "1"}, {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"})
        self.assertEqual(set(env), {"PATH", "HOME", "STRIX_TEST", "LD_LIBRARY_PATH"})
        self.assertEqual(env["PATH"], lease.BASE_PATH)

    def test_environment_rejections(self):
        for name in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "PYTHON",
                     "BASH_ENV", "ENV", "PATH", "HOME", "GPU_TURN_MAX", "INVALID=NAME"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                lease.environment({name: "unsafe"})
        with self.assertRaises(ValueError):
            lease.environment({}, {"PYTHONPATH": "/tmp"})

    def test_environment_rejected_before_launch(self):
        self.spec["processes"][0]["env"] = {"LD_PRELOAD": "bad"}
        with patch.object(lease.subprocess, "Popen") as start:
            self.assertIn("error", lease.run("lease", self.spec))
        start.assert_not_called()

    def test_argv_preserves_code_arguments_without_a_shell(self):
        proc = self.spec["processes"][0]
        proc["argv"].extend(["-c", "import sys\nprint(sys.version)"])
        self.assertEqual(lease.validate_process(proc)["argv"], proc["argv"])
        proc["argv"].append("bad\0argument")
        with self.assertRaises(ValueError):
            lease.validate_process(proc)

    def test_release_runtime_environment(self):
        release_dir = self.root / "releases" / "aabbccddeeff"
        (release_dir / "bin").mkdir(parents=True)
        binary = release_dir / "bin" / "engine"
        binary.touch()
        (release_dir / "MANIFEST.json").write_text(json.dumps({"runtime_env": {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"}}))
        self.assertEqual(lease.runtime_env(self.root, [str(binary)]), {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"})

    def test_paths_and_names(self):
        for value in ("relative", str(self.root / ".." / "other"), str(self.root) + "\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lease.path(value)
        for value in ("../bad", "UPPER", "a/b", "a" * 41):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lease.identifier(value)

    def test_marked_root_and_boundary_checks(self):
        self.assertEqual(lease.marked_root(str(self.root)), self.root)
        with self.assertRaises(ValueError):
            lease.marked_root(str(self.lab))
        with self.assertRaises(ValueError):
            lease.marked_root(str(self.root), str(self.root / "lab"))
        with patch.object(lease.Path, "home", return_value=self.root / "home"):
            with self.assertRaises(ValueError):
                lease.marked_root(str(self.root))
        with self.assertRaises(ValueError):
            lease.marked_root(str(REPO))

    def test_duplicate_process_names(self):
        self.spec["processes"] *= 2
        with self.assertRaises(ValueError):
            lease.validate_spec(self.spec)

    def test_peer_validation(self):
        self.spec["processes"][0]["machine"] = "peer"
        with self.assertRaises(ValueError):
            lease.validate_spec(self.spec)
        self.spec.update(kind="pair", peer_ssh="halo-2", peer_root=str(self.root))
        lease.validate_spec(self.spec)
        for target in ("-halo-2", "halo-2 command", "halo-2;command"):
            self.spec["peer_ssh"] = target
            with self.assertRaises(ValueError):
                lease.validate_spec(self.spec)

    def test_health_url_validation(self):
        for url in ("http://halo-1:18501/health", "https://127.0.0.1:18501/health",
                    "http://127.0.0.1/health", "http://127.0.0.1:18501/admin/drain",
                    "http://operator@127.0.0.1:18501/health"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                lease.health_url(url)

    def test_readiness_adapters_require_correct_stream(self):
        qwen = "strix serve: listening on 127.0.0.1:18501 model synthetic"
        ds41 = 'log prefix\n{"event":"READY", "rank":0}\n'
        self.assertTrue(lease.adapter_ready("qwen-stderr", "", qwen))
        self.assertFalse(lease.adapter_ready("qwen-stderr", qwen, ""))
        self.assertTrue(lease.adapter_ready("ds41-json", ds41, ""))
        self.assertFalse(lease.adapter_ready("ds41-json", "", ds41))
        self.assertFalse(lease.adapter_ready("ds41-json", '{"event":"POISONED"}', ""))

    def test_readiness_requires_both_event_and_health(self):
        child = lease.Child(self.root, "abc", self.spec["processes"][0])
        qwen = "strix serve: listening on 127.0.0.1:18501 model synthetic"
        with patch.object(child, "logs", return_value=("", qwen)), patch.object(lease, "http", return_value=False):
            self.assertFalse(child.check_ready())
        with patch.object(child, "logs", return_value=("", "")), patch.object(lease, "http", return_value=True):
            self.assertFalse(child.check_ready())

    def test_secret_atomic_replace_and_no_echo(self):
        value = b"synthetic-test-value"
        payload = {"root": str(self.root), "name": "engine-key", "value_b64": base64.b64encode(value).decode()}
        result = lease.run("put-secret", payload)
        self.assertNotIn("error", result)
        self.assertNotIn(value.decode(), json.dumps(result))
        key = self.root / "etc" / "keys" / "engine-key"
        self.assertEqual(key.read_bytes(), value)
        payload["value_b64"] = base64.b64encode(b"replacement").decode()
        self.assertNotIn("error", lease.run("put-secret", payload))
        self.assertEqual(key.read_bytes(), b"replacement")
        self.assertEqual(list(key.parent.glob("*.tmp")), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    def test_secret_rejects_bad_name_and_base64(self):
        for name, value in (("../escape", "eA=="), ("safe", "???"), ("safe", "")):
            reply = lease.run("put-secret", {"root": str(self.root), "name": name, "value_b64": value})
            self.assertIn("error", reply)

    def test_logs_are_bounded_across_streams(self):
        lease.marked_root(str(self.root))
        for stream in ("stdout", "stderr"):
            lease.log_path(self.root, "abc", "engine", stream).write_bytes(b"x" * 100000)
        reply = lease.run("logs", {"root": str(self.root), "lease": "abc", "name": "engine", "tail": 1000000})
        self.assertEqual(len(reply["stdout"].encode()) + len(reply["stderr"].encode()), 65536)
        lease.log_path(self.root, "abc", "engine", "stdout").write_bytes("".join(str(i) + "\n" for i in range(20)).encode())
        lease.log_path(self.root, "abc", "engine", "stderr").write_text("")
        reply = lease.run("logs", {"root": str(self.root), "lease": "abc", "name": "engine", "tail": 9})
        self.assertEqual(reply["stdout"], "".join(str(i) + "\n" for i in range(11, 20)))

    def test_logs_utf8_byte_limit(self):
        lease.marked_root(str(self.root))
        for stream in ("stdout", "stderr"):
            lease.log_path(self.root, "abc", "engine", stream).write_bytes(("€" * 30000).encode())
        reply = lease.run("logs", {"root": str(self.root), "lease": "abc", "name": "engine"})
        self.assertLessEqual(sum(len(reply[key].encode()) for key in ("stdout", "stderr")), 65536)

    def test_status_exposes_install_jobs(self):
        lease.marked_root(str(self.root))
        job = {"job": "abc", "state": "succeeded", "release_id": "aabbccddeeff"}
        lease.write_json(self.root / "run" / "job-abc.json", job)
        lease.write_json(self.root / "run" / "job-abc.spec.json", {"recipe": {}})
        reply = lease.run("status", {"root": str(self.root), "job": "abc"})
        self.assertEqual(reply["job"], job)
        self.assertEqual(reply["jobs"], [job])

    def test_launch_command_and_turn_cap(self):
        # Check POSIX launch construction on Windows without starting any child.
        fake_os = SimpleNamespace(**dict(vars(os), name="posix"))
        for kind in ("single", "pair", "none"):
            for approved in (False, True):
                self.spec.update(kind=kind, approval={"lease_s": 1200, "ref": "synthetic ACK",
                                 "end_epoch": time.time() + 3600} if approved else None)
                process = Mock(pid=123, poll=Mock(return_value=0))
                with patch.dict(os.environ, {"GPU_TURN_MAX": "99999"}), \
                        patch.object(lease, "os", fake_os), patch.object(lease.subprocess, "Popen", return_value=process) as start:
                    reply = lease.run("lease", self.spec)
                self.assertNotIn("error", reply)
                argv = start.call_args.args[0]
                env = start.call_args.kwargs["env"]
                if kind == "none":
                    self.assertEqual(Path(argv[0]).name, "hyperloom-node")
                else:
                    self.assertEqual(Path(argv[0]).name, "gpu-turn-pair.sh" if kind == "pair" else "gpu-turn.sh")
                    self.assertEqual(argv[1], self.spec["label"])
                    self.assertEqual(Path(argv[2]).name, "hyperloom-node")
                    self.assertEqual(argv[3], "supervise-internal")
                self.assertEqual(env.get("GPU_TURN_MAX"), "1200" if kind == "single" and approved else None)
                self.assertTrue(start.call_args.kwargs["start_new_session"])
                lease._launches.remove(process)

    def test_stop_order_and_group_escalation(self):
        events = []
        processes = []
        for name, adapter in (("qwen", "qwen-stderr"), ("ds41", "ds41-json")):
            proc = dict(self.spec["processes"][0], name=name, ready=adapter)
            child = lease.Child(self.root, "abc", proc)
            child.process = Mock(pid=100 + len(processes), poll=Mock(return_value=None))
            processes.append(child)
        with patch.object(lease, "http", side_effect=lambda *a, **kw: events.append("drain")), \
                patch.object(lease, "group_signal", side_effect=lambda proc, sig: events.append((proc.pid, sig))), \
                patch.object(lease, "signal", SimpleNamespace(**dict(vars(signal), SIGKILL=9))), \
                patch.object(lease.time, "monotonic", side_effect=[0, 31]):
            lease.stop_children(processes)
        self.assertEqual(events[0], "drain")
        self.assertEqual(events[1], (101, signal.SIGTERM))
        self.assertEqual(events[2], (100, signal.SIGTERM))
        # Both groups are killed even if their leaders have already left children.
        self.assertEqual([event[0] for event in events[3:]], [100, 101])

    def test_ds41_follower_stops_without_an_http_drain(self):
        proc = dict(self.spec["processes"][0], ready="ds41-json",
                    argv=[str(Path(sys.executable).resolve()), "--rank", "1"])
        child = lease.Child(self.root, "abc", proc)
        child.process = Mock(pid=123, poll=Mock(return_value=None))
        with patch.object(lease, "http") as http, patch.object(lease, "group_signal") as stop:
            child.terminate()
        http.assert_not_called()
        stop.assert_called_once_with(child.process, signal.SIGTERM)

    def test_install_policy_protects_assets(self):
        (self.root / "etc").mkdir()
        (self.root / "etc" / "install-policy.json").write_text(json.dumps({"asset_paths": [str(self.root / "asset")]}))
        with self.assertRaisesRegex(ValueError, "asset"):
            lease.marked_root(str(self.root))

    def test_marked_root_refuses_nested_repository(self):
        (self.root / "unrelated" / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "repository"):
            lease.marked_root(str(self.root))

    def test_status_and_idempotent_stop(self):
        lease.marked_root(str(self.root))
        state = {"lease": "abc", "state": "ready", "processes": [{"name": "engine", "pid": 123}],
                 "start_time": 100, "deadline": 1240, "reason": None}
        lease.write_json(lease.record_path(self.root, "abc"), state)
        reply = lease.run("status", {"root": str(self.root)})
        self.assertEqual(reply["leases"], [state])
        for _ in range(2):
            self.assertNotIn("error", lease.run("stop", {"root": str(self.root), "lease": "abc"}))
        self.assertTrue(lease.stop_path(self.root, "abc").exists())

    def test_layout_checks_after_holder(self):
        turn = self.lab / ".gpu-turn"
        (turn / "holder").write_text("hl-qwen-test since synthetic")
        (turn / "layout-state").write_text("1 -")
        usb = self.base / "usb.json"
        usb.write_text('{"layout":"1"}')
        self.spec["layout_required"] = "1"
        with patch.object(lease, "USB_LAYOUT", usb):
            lease.layout_check(self.spec)
            usb.write_text('{"layout":"2b"}')
            with self.assertRaisesRegex(ValueError, "USB4"):
                lease.layout_check(self.spec)
            usb.write_text('{"layout":"1"}')
            (turn / "layout-state").write_text("2b -")
            with self.assertRaisesRegex(ValueError, "lab layout"):
                lease.layout_check(self.spec)

    def test_pair_layout_checks_peer(self):
        turn = self.lab / ".gpu-turn"
        (turn / "holder").write_text("PAIR hl-qwen-test (local) since synthetic")
        (turn / "layout-state").write_text("1 -")
        usb = self.base / "usb.json"
        usb.write_text('{"layout":1}')
        self.spec.update(kind="pair", layout_required="1", peer_ssh="halo-2")
        self.spec["peer_lab_dir"] = str(self.base / "peer lab")
        with patch.object(lease, "USB_LAYOUT", usb), patch.object(lease, "peer_call", return_value={"layout_ok": False}) as peer:
            with self.assertRaisesRegex(ValueError, "peer layout"):
                lease.layout_check(self.spec)
        self.assertEqual(peer.call_args.args[1], "supervise-internal")
        self.assertEqual(peer.call_args.args[2]["lab_dir"], self.spec["peer_lab_dir"])

    def test_json_rejects_duplicates_and_oversize(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"[]", b" " * (lease.MAX_JSON + 1)):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                lease.decode(raw)


@unittest.skipUnless(os.name == "posix", "POSIX node process groups and executable fixtures")
class LeaseProcessTests(LeaseFixture):
    def setUp(self):
        super().setUp()
        self.boot = self.base / "boot"
        self.boot.mkdir()
        for name in ("hyperloom-node", "hyperloom_lease.py"):
            shutil.copyfile(REPO / "node" / name, self.boot / name)
            (self.boot / name).chmod(0o700)
        for name in ("gpu-turn.sh", "gpu-turn-pair.sh"):
            shutil.copyfile(FIXTURES / "gpu-turn.sh", self.lab / name)
            (self.lab / name).chmod(0o700)
        self.ids = []
        self.addCleanup(self.cleanup_leases)

    def node(self, verb, payload):
        result = subprocess.run([sys.executable, str(self.boot / "hyperloom-node"), verb],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=15)
        reply = json.loads(result.stdout)
        self.assertNotIn("error", reply, reply)
        return reply

    def cleanup_leases(self):
        for lease_id in self.ids:
            try:
                self.node("cancel", {"root": str(self.root), "lease": lease_id})
                self.wait_state(lease_id, lease.TERMINAL, timeout=35)
            except (AssertionError, OSError, subprocess.SubprocessError):
                pass

    def state(self, lease_id):
        return lease.read_json(lease.record_path(self.root, lease_id))

    def wait_state(self, lease_id, states, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.state(lease_id)
            if state["state"] in states:
                return state
            time.sleep(0.05)
        self.fail("lease did not reach " + repr(states) + ": " + repr(self.state(lease_id)))

    def engine(self, name="engine", adapter="qwen", **extra):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        argv = [str(Path(sys.executable).resolve()), str(FIXTURES / "fake_engine.py"),
                "--port", str(port), "--adapter", adapter, "--events", str(self.base / (name + ".events")),
                "--pid-file", str(self.base / (name + ".pid"))]
        for key, value in extra.items():
            argv.append("--" + key.replace("_", "-"))
            if value is not True:
                argv.append(str(value))
        return {"name": name, "machine": "local", "argv": argv, "env": {"STRIX_TEST": "1"},
                "ready": "qwen-stderr" if adapter in {"qwen", "silent"} else "ds41-json",
                "health_url": f"http://127.0.0.1:{port}/health", "ready_timeout_s": 2}

    def start(self, processes=None, **changes):
        spec = dict(self.spec, processes=processes or [self.engine()], deadline_s=10)
        spec.update(changes)
        started = time.monotonic()
        lease_id = self.node("lease", spec)["lease"]
        self.assertLess(time.monotonic() - started, 2)
        self.ids.append(lease_id)
        return lease_id

    def assert_gone(self, pid):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("process remains alive: " + str(pid))

    def test_turn_args_env_and_qwen_readiness(self):
        lease_id = self.start()
        state = self.wait_state(lease_id, {"ready"})
        invocation = lease.read_json(self.lab / ".gpu-turn" / "invocation.json")
        self.assertEqual(invocation["argv"][0], "hl-qwen-test")
        self.assertEqual(invocation["argv"][2], "supervise-internal")
        self.assertNotIn("GPU_TURN_MAX", invocation["env"])
        spec_file = Path(invocation["argv"][3])
        self.assertEqual(stat.S_IMODE(spec_file.stat().st_mode), 0o600)
        self.node("stop", {"root": str(self.root), "lease": lease_id})
        final = self.wait_state(lease_id, {"stopped"})
        self.assertEqual(final["reason"], "requested")
        self.assert_gone(state["processes"][0]["pid"])
        env = lease.read_json(self.base / "engine.pid")["env"]
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["STRIX_TEST"], "1")

    def test_approval_sets_single_turn_cap(self):
        lease_id = self.start(approval={"lease_s": 1200, "ref": "synthetic ACK", "end_epoch": time.time() + 3600})
        self.wait_state(lease_id, {"ready"})
        invocation = lease.read_json(self.lab / ".gpu-turn" / "invocation.json")
        self.assertEqual(invocation["env"]["GPU_TURN_MAX"], "1200")

    def test_approval_end_expires_while_queued_without_start(self):
        block = self.lab / ".gpu-turn" / "block"
        block.touch()
        lease_id = self.start(approval={"lease_s": 1200, "ref": "synthetic ACK", "end_epoch": time.time() + 0.4})
        time.sleep(0.5)
        block.unlink()
        final = self.wait_state(lease_id, {"stopped"})
        self.assertEqual(final["reason"], "deadline")
        self.assertEqual(final["processes"], [])
        self.assertFalse((self.base / "engine.pid").exists())

    def test_pair_deadline(self):
        lease_id = self.start(kind="pair", deadline_s=1.5)
        state = self.wait_state(lease_id, {"stopped"})
        self.assertEqual(state["reason"], "deadline")
        self.assertLess(state["end_time"] - state["start_time"], 4)
        self.assert_gone(state["processes"][0]["pid"])

    def test_ds41_readiness_and_authenticated_drain_before_term(self):
        key = self.base / "key"
        key.write_text("synthetic-test-value")
        lease_id = self.start([self.engine(adapter="ds41", http_api_key_file=key)])
        self.wait_state(lease_id, {"ready"})
        self.node("stop", {"root": str(self.root), "lease": lease_id})
        self.wait_state(lease_id, {"stopped"})
        events = [json.loads(line)["event"] for line in (self.base / "engine.events").read_text().splitlines()]
        self.assertEqual(events, ["start", "drain", "term", "exit"])

    def test_partial_start_failure_cleans_first_process(self):
        first = self.engine(grandchild=True)
        second = self.engine("broken")
        second["argv"] = [str(self.base / "missing-executable")]
        lease_id = self.start([first, second])
        final = self.wait_state(lease_id, {"failed"})
        self.assertEqual(len(final["processes"]), 1)
        self.assert_gone(final["processes"][0]["pid"])

    def test_readiness_timeout_cleans_all(self):
        lease_id = self.start([self.engine("engine"), self.engine("silent", "silent")])
        final = self.wait_state(lease_id, {"failed"})
        self.assertEqual(final["reason"], "readiness-timeout:silent")
        for proc in final["processes"]:
            self.assert_gone(proc["pid"])

    def test_process_exit_stops_siblings_and_descendants(self):
        lease_id = self.start([self.engine("engine", exit_after=1, grandchild=True), self.engine("sibling")])
        self.wait_state(lease_id, {"ready"})
        final = self.wait_state(lease_id, {"stopped"})
        self.assertEqual(final["reason"], "process-exit:engine")
        for proc in final["processes"]:
            self.assert_gone(proc["pid"])
        self.assert_gone(lease.read_json(self.base / "engine.pid")["child"])

    def test_cancel_queued_turn(self):
        (self.lab / ".gpu-turn" / "block").touch()
        lease_id = self.start()
        self.assertEqual(self.state(lease_id)["state"], "queued")
        owner = lease.read_json(self.root / "run" / ("owner-" + lease_id + ".json"))["pid"]
        self.node("cancel", {"root": str(self.root), "lease": lease_id})
        self.assertEqual(self.state(lease_id)["state"], "cancelled")
        self.assertFalse((self.base / "engine.pid").exists())
        self.assert_gone(owner)

    def test_supervisor_signals_cleanup(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            lease_id = self.start()
            state = self.wait_state(lease_id, {"ready"})
            os.kill(state["pid"], sig)
            final = self.wait_state(lease_id, {"stopped"})
            self.assertEqual(final["reason"], "requested")
            self.assert_gone(final["processes"][0]["pid"])

    def test_layout_abort_starts_nothing(self):
        (self.lab / ".gpu-turn" / "layout-state").write_text("2b -")
        lease_id = self.start(layout_required="1")
        state = self.wait_state(lease_id, {"aborted"})
        self.assertEqual(state["processes"], [])
        self.assertFalse((self.base / "engine.pid").exists())

    def test_router_without_turn_script(self):
        lease_id = self.start(kind="none", lab_dir=None)
        self.wait_state(lease_id, {"ready"})
        self.assertFalse((self.lab / ".gpu-turn" / "invocation.json").exists())

    def test_peer_dead_man_stdin_eof(self):
        proc = self.engine(adapter="ds41", grandchild=True)
        payload = {"root": str(self.root), "lease": "peer-test", "process": proc}
        helper = subprocess.Popen([sys.executable, str(self.boot / "hyperloom-node"), "peer-run"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            helper.stdin.write(json.dumps(payload).encode() + b"\n")
            helper.stdin.flush()
            end = time.monotonic() + 5
            while not (self.base / "engine.pid").exists() and time.monotonic() < end:
                time.sleep(0.05)
            pids = lease.read_json(self.base / "engine.pid")
            self.assertIsNone(helper.poll())
            self.node("logs", {"root": str(self.root), "lease": "peer-test", "name": "engine"})
            helper.stdin.close()
            helper.wait(timeout=5)
            reply = json.loads(helper.stdout.read())
            self.assertNotIn("error", reply)
            self.assert_gone(pids["pid"])
            self.assert_gone(pids["child"])
            events = [json.loads(line)["event"] for line in (self.base / "engine.events").read_text().splitlines()]
            self.assertLess(events.index("drain"), events.index("term"))
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
            for stream in (helper.stdin, helper.stdout, helper.stderr):
                stream.close()

    def test_peer_deadline_stops_child_with_channel_still_open(self):
        proc = self.engine(adapter="ds41", grandchild=True)
        payload = {"root": str(self.root), "lease": "peer-deadline", "process": proc,
                   "deadline": time.time() + 2}
        helper = subprocess.Popen([sys.executable, str(self.boot / "hyperloom-node"), "peer-run"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            helper.stdin.write(json.dumps(payload).encode() + b"\n")
            helper.stdin.flush()
            helper.wait(timeout=8)
            reply = json.loads(helper.stdout.read())
            self.assertNotIn("error", reply)
            self.assertFalse(helper.stdin.closed)
            pids = lease.read_json(self.base / "engine.pid")
            self.assert_gone(pids["pid"])
            self.assert_gone(pids["child"])
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
            for stream in (helper.stdin, helper.stdout, helper.stderr):
                stream.close()

    def test_peer_process_readiness_and_cleanup_through_fake_ssh(self):
        peer_boot = self.base / "peer-boot"
        shutil.copytree(self.boot, peer_boot)
        fake_ssh = self.base / "fake_ssh.py"
        shutil.copyfile(FIXTURES / "fake_ssh.py", fake_ssh)
        peer_root = self.base / "peer-root"
        peer_root.mkdir()
        (peer_root / ".hyperloom-root").write_text("synthetic-root-id")
        spec = dict(self.spec, peer_root=str(peer_root), peer_ssh="halo-2")
        rank0 = self.engine(name="rank0", adapter="ds41")
        local_lease = self.start(processes=[rank0], kind="none", lab_dir=None)
        self.wait_state(local_lease, {"ready"})
        proc = self.engine(adapter="ds41", grandchild=True, rank=1)
        proc["health_url"] = rank0["health_url"]
        proc["machine"] = "peer"
        child = lease.Child(self.root, "peer-local", proc, spec)
        lease.marked_root(str(self.root))

        def ssh(target, command):
            return [sys.executable, str(fake_ssh), "--", target, command]

        with patch.object(lease, "ssh_command", side_effect=ssh):
            try:
                child.start()
                end = time.monotonic() + 5
                while time.monotonic() < end and not child.check_ready():
                    time.sleep(0.05)
                self.assertTrue(child.check_ready())
                self.assertIsNotNone(child.peer_pid)
                self.assertEqual(lease.read_json(lease.record_path(peer_root, "peer-local"))["state"], "acquired")
            finally:
                lease.stop_children([child])
        final = lease.read_json(lease.record_path(peer_root, "peer-local"))
        self.assertEqual(final["state"], "stopped")
        pids = lease.read_json(self.base / "engine.pid")
        self.assert_gone(pids["pid"])
        self.assert_gone(pids["child"])
        # Peer-run sends one final status document; engine output stays in peer logs.
        final_reply = json.loads(lease.log_path(self.root, "peer-local", "engine", "stdout").read_text())
        self.assertEqual(final_reply["pid"], pids["pid"])
        events = [json.loads(line)["event"] for line in (self.base / "engine.events").read_text().splitlines()]
        self.assertNotIn("drain", events)  # The follower has no HTTP listener.


if __name__ == "__main__":
    unittest.main()


class Usb4LayoutIdTests(unittest.TestCase):
    """The lab's /run/usb4stream-layout.json holds per-stream records, not a layout name."""

    @staticmethod
    def streams(direct):
        keys = ["0-2.0/data0", "0-2.0/data1", "1-2.0/data0", "1-2.0/data1"]
        doc = {k: {"device": "/dev/tbstream%d" % i, "config": {"gpu_direct": d}} for i, (k, d) in enumerate(zip(keys, direct))}
        doc.update(version=2, topology={"cables_up": 2})
        return doc

    def test_derives_production_layout(self):
        self.assertEqual(lease.usb4_layout_id(self.streams([0, 0, 0, 0])), "1")
        self.assertEqual(lease.usb4_layout_id(self.streams([0, 1, 0, 1])), "gpu-direct")
        self.assertEqual(lease.usb4_layout_id({"layout": "1"}), "1")
        partial = self.streams([0, 0, 0, 0])
        del partial["1-2.0/data1"]
        self.assertEqual(lease.usb4_layout_id(partial), "unknown")
