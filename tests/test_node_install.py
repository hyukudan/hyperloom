"""Node install tests use temp roots and synthetic tools only."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


NODE = Path(__file__).resolve().parents[1] / "node"
spec = importlib.util.spec_from_file_location("node_install_under_test", NODE / "hyperloom_install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom-install-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "install space"
        self.payload = {"root": str(self.root), "lab_dir": None,
                        "components": {"engine": {"repo": "https://example.invalid/engine", "commit": "a" * 40}},
                        "recipe": {"configure": ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"],
                                   "targets": ["strix-server"], "ctest_regex": "^strix-host-tests$", "trellis": False}}

    def mark(self):
        return install.checked_root({"root": str(self.root)}, create=True)

    def make_release(self):
        self.mark()
        manifest = {"binaries": {"strix-server": hashlib.sha256(b"binary").hexdigest()},
                    "capabilities": {"strix-server": {"ok": True}}, "recipe": {"configure": []}}
        rid = install.release_id(manifest)
        directory = self.root / "releases" / rid
        (directory / "bin").mkdir(parents=True, exist_ok=True)
        (directory / "bin" / "strix-server").write_bytes(b"binary")
        install.atomic_json(directory / "MANIFEST.json", manifest)
        return rid, manifest

    def call(self, verb, **kwargs):
        return install.run(verb, {"root": str(self.root), **kwargs})


class InstallTests(InstallCase):
    def test_verbs(self):
        self.assertEqual(install.VERBS, {"install", "job", "releases", "activate", "uninstall", "verify-model", "push-release"})

    def test_marker_created_once(self):
        self.mark()
        marker = (self.root / ".hyperloom-root").read_text()
        self.mark()
        self.assertEqual(marker, (self.root / ".hyperloom-root").read_text())
        self.assertRegex(marker.strip(), r"^[a-f0-9]{32}$")

    def test_job_queries_state_and_bounded_log_tail(self):
        self.mark()
        state = {"job": "abc", "kind": "verify-model", "state": "running"}
        install.atomic_json(self.root / "run" / "job-abc.json", state)
        log = self.root / "log" / "job-abc.log"
        log.write_bytes(b"old\n" + ("€" * 30000).encode() + b"\nlast\n")
        reply = self.call("job", job="abc")
        self.assertEqual(reply["state"], "running")
        self.assertEqual(reply["kind"], "verify-model")
        self.assertLessEqual(len(reply["tail"].encode()), 65536)
        self.assertTrue(reply["tail"].endswith("last\n"))
        self.assertNotIn("old", reply["tail"])
        self.assertIn("error", self.call("job", job="../escape"))
        self.assertIn("error", self.call("job", job="missing"))
        install.atomic_json(self.root / "run" / "job-abc.json",
                            {**state, "state": "succeeded", "verified": True})
        self.assertTrue(self.call("job", job="abc")["verified"])

    def test_failed_job_is_queryable_through_the_node_dispatcher(self):
        self.mark()
        install.atomic_json(self.root / "run" / "job-abc.json",
                            {"job": "abc", "state": "failed", "error": "synthetic build failure"})
        (self.root / "log" / "job-abc.log").write_bytes(b"compiler diagnostic\n")
        result = subprocess.run([sys.executable, str(NODE / "hyperloom-node"), "job"],
                                input=json.dumps({"root": str(self.root), "job": "abc"}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertNotIn("error", reply)
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["reason"], "synthetic build failure")
        self.assertEqual(reply["tail"], "compiler diagnostic\n")

    def test_marker_refuses_home_and_ancestor(self):
        with patch.object(install.Path, "home", return_value=self.base / "operator"):
            for root in (self.base, self.base / "operator"):
                with self.subTest(root=root), self.assertRaises(ValueError):
                    install.checked_root({"root": str(root)}, create=True)

    def test_marker_refuses_lab_and_assets_in_both_directions(self):
        for key in ("lab_dir", "asset_paths"):
            for protected in (self.root, self.root / "asset", self.base):
                payload = {"root": str(self.root), key: [str(protected)] if key == "asset_paths" else str(protected)}
                with self.subTest(key=key, path=protected), self.assertRaises(ValueError):
                    install.checked_root(payload, create=True)

    def test_marker_refuses_repository_and_parent_repository(self):
        self.root.mkdir()
        nested = self.root / "checkout"
        (nested / ".git").mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.mark()
        shutil.rmtree(nested)
        (self.base / ".git").write_text("gitdir: synthetic")
        with self.assertRaises(ValueError):
            self.mark()

    def test_marker_refuses_nonempty_unmarked(self):
        self.root.mkdir()
        (self.root / "keep").write_text("untouched")
        with self.assertRaises(ValueError):
            self.mark()
        self.assertEqual((self.root / "keep").read_text(), "untouched")

    def test_uninstall_requires_valid_marker(self):
        self.root.mkdir()
        self.assertIn("error", self.call("uninstall"))
        (self.root / ".hyperloom-root").write_text("wrong")
        self.assertIn("error", self.call("uninstall"))

    def test_path_rejections(self):
        for value in ("relative", str(self.base / ".." / "other"), str(self.root) + "\n", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install.absolute(value)

    def test_release_id_stable_and_twelve_characters(self):
        self.assertEqual(install.release_id({"b": 2, "a": 1}), install.release_id({"a": 1, "b": 2}))
        self.assertEqual(len(install.release_id({})), 12)
        self.assertNotEqual(install.release_id({"a": 1}), install.release_id({"a": 2}))

    def test_manifest_and_binary_integrity(self):
        rid, _ = self.make_release()
        install.release(self.root, rid)
        (self.root / "releases" / rid / "bin" / "strix-server").write_bytes(b"tampered")
        self.assertIn("error", self.call("activate", profile="qwen-replicas", release_id=rid))

    def test_capability_required_for_activation(self):
        rid, manifest = self.make_release()
        manifest["capabilities"]["strix-server"]["ok"] = False
        new_id = install.release_id(manifest)
        directory = self.root / "releases" / rid
        install.atomic_json(directory / "MANIFEST.json", manifest)
        directory.rename(directory.with_name(new_id))
        self.assertIn("error", self.call("activate", profile="qwen-replicas", release_id=new_id))

    def test_activation_and_listing(self):
        rid, manifest = self.make_release()
        self.assertNotIn("error", self.call("activate", profile="qwen-replicas", release_id=rid))
        state = install.read_json(self.root / "etc" / "active-qwen-replicas.json")
        self.assertEqual(state["release_id"], rid)
        rows = self.call("releases")
        self.assertEqual(rows["active"], {"qwen-replicas": rid})
        self.assertEqual(rows["releases"][0]["manifest"], manifest)
        self.assertEqual(rows["releases"][0]["active_profiles"], ["qwen-replicas"])
        self.assertFalse(list((self.root / "etc").glob("tmp*")))

    def test_activation_refused_for_running_and_queued_leases(self):
        rid, _ = self.make_release()
        for state in ("queued", "acquired", "ready", "running", "draining"):
            install.atomic_json(self.root / "run" / "lease-one.json", {"state": state, "profile": "qwen-replicas"})
            self.assertIn("error", self.call("activate", profile="qwen-replicas", release_id=rid))

    def test_legacy_lease_without_profile_fails_closed(self):
        rid, _ = self.make_release()
        install.atomic_json(self.root / "run" / "lease-one.json", {"state": "ready"})
        self.assertIn("error", self.call("activate", profile="qwen-replicas", release_id=rid))

    def test_other_profile_and_stopped_lease_allow_activation(self):
        rid, _ = self.make_release()
        for state in ({"state": "ready", "profile": "other"}, {"state": "stopped", "profile": "qwen-replicas"}):
            install.atomic_json(self.root / "run" / "lease-one.json", state)
            self.assertNotIn("error", self.call("activate", profile="qwen-replicas", release_id=rid))

    def test_uninstall_refused_while_lease_or_job_runs(self):
        rid, _ = self.make_release()
        path = self.root / "run" / "lease-one.json"
        install.atomic_json(path, {"state": "running"})
        self.assertIn("error", self.call("uninstall"))
        self.assertTrue((self.root / "releases" / rid).exists())
        path.unlink()
        install.atomic_json(self.root / "run" / "job-one.json", {"state": "queued"})
        self.assertIn("error", self.call("uninstall"))

    def test_uninstall_boundaries(self):
        self.mark()
        outside = self.base / "asset"
        outside.write_bytes(b"keep")
        self.assertNotIn("error", self.call("uninstall"))
        self.assertFalse(self.root.exists())
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_uninstall_keep_logs(self):
        self.mark()
        (self.root / "log" / "build.log").write_text("log")
        self.assertNotIn("error", self.call("uninstall", keep_logs=True))
        self.assertEqual((self.root / "log" / "build.log").read_text(), "log")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"log", ".hyperloom-root"})

    def test_lab_pair_refused_before_install(self):
        lab = self.base / "lab"
        (lab / ".gpu-turn").mkdir(parents=True)
        (lab / ".gpu-turn" / "holder").write_text("PAIR synthetic since 00:00:00")
        payload = {**self.payload, "lab_dir": str(lab)}
        self.assertIn("error", install.run("install", payload))
        self.assertFalse(self.root.exists())

    def test_invalid_install_payloads(self):
        mutations = [lambda p: p["components"]["engine"].update(commit="main"),
                     lambda p: p["components"]["engine"].update(repo="--upload-pack=bad"),
                     lambda p: p["recipe"].update(targets=["../bad"]),
                     lambda p: p["recipe"].update(configure=["x\ny"]),
                     lambda p: p["recipe"].update(trellis=True)]
        for mutate in mutations:
            payload = copy.deepcopy(self.payload)
            mutate(payload)
            self.assertIn("error", install.run("install", payload))

    def test_verify_model_size_only_and_failures(self):
        model = self.base / "model.gguf"
        model.write_bytes(b"model")
        self.assertTrue(install.run("verify-model", {"path": str(model), "bytes": 5, "sha256": None})["verified"])
        self.assertIn("error", install.run("verify-model", {"path": str(model), "bytes": 4}))
        self.assertIn("error", install.run("verify-model", {"path": str(model), "bytes": 5, "sha256": "bad"}))

    def test_verify_model_hash_uses_jobs(self):
        self.mark()
        model = self.base / "model.gguf"
        model.write_bytes(b"model")
        with patch.object(install, "launch_job", return_value={"job": "synthetic"}) as launch:
            result = self.call("verify-model", path=str(model), bytes=5, sha256=hashlib.sha256(b"model").hexdigest())
        self.assertEqual(result["job"], "synthetic")
        self.assertEqual(launch.call_args.args[1], "verify-model")

    def test_hash_worker_records_success_and_failure(self):
        self.mark()
        model = self.base / "model.gguf"
        model.write_bytes(b"model")
        for digest, expected in ((hashlib.sha256(b"model").hexdigest(), "succeeded"), ("0" * 64, "failed")):
            spec_path = self.root / "run" / "job-synthetic.spec.json"
            install.atomic_json(spec_path, {"root": str(self.root), "job": "synthetic", "limited": False,
                "kind": "verify-model", "payload": {"path": str(model), "bytes": 5, "sha256": digest}})
            install.job_main(str(spec_path))
            state = install.read_json(self.root / "run" / "job-synthetic.json")
            self.assertEqual(state["state"], expected)
            self.assertFalse(spec_path.exists())

    def test_worker_records_build_exception(self):
        self.mark()
        spec_path = self.root / "run" / "job-synthetic.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "synthetic", "limited": False,
                                       "kind": "install", "payload": self.payload})
        with patch.object(install, "build", side_effect=ValueError("synthetic build failure")):
            self.assertEqual(install.job_main(str(spec_path)), 1)
        state = install.read_json(self.root / "run" / "job-synthetic.json")
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error"], "synthetic build failure")

    def test_hash_worker_rechecks_lab_holder(self):
        self.mark()
        lab = self.base / "lab"
        (lab / ".gpu-turn").mkdir(parents=True)
        (lab / ".gpu-turn" / "holder").write_text("PAIR synthetic")
        spec_path = self.root / "run" / "job-synthetic.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "synthetic", "limited": False,
            "kind": "verify-model", "payload": {"lab_dir": str(lab), "path": str(self.base / "absent")}})
        self.assertEqual(install.job_main(str(spec_path)), 1)
        state = install.read_json(self.root / "run" / "job-synthetic.json")
        self.assertIn("PAIR", state["error"])

    def test_systemd_envelope_and_fallback(self):
        with patch.object(install.shutil, "which", return_value="synthetic"), patch.object(install, "command"):
            self.assertEqual(install.envelope(), ["systemd-run", "--user", "--scope", "-p", "MemoryMax=16G", "nice", "-n", "19"])
        with patch.object(install.shutil, "which", return_value="synthetic"), patch.object(install, "command", side_effect=OSError):
            self.assertEqual(install.envelope(), ["nice", "-n", "19"])

    def test_push_release_bad_peer_and_bad_hash_never_connect(self):
        rid, _ = self.make_release()
        with patch.object(install, "command") as command:
            for peer in ("-option", "halo-2;bad", "halo-2\n"):
                self.assertIn("error", self.call("push-release", release_id=rid, peer_ssh=peer, peer_root="/tmp/hyperloom"))
            (self.root / "releases" / rid / "bin" / "strix-server").write_bytes(b"bad")
            self.assertIn("error", self.call("push-release", release_id=rid, peer_ssh="halo-2", peer_root="/tmp/hyperloom"))
            command.assert_not_called()

    def test_push_release_rsync_verifies_peer_before_publish(self):
        rid, manifest = self.make_release()
        digest = manifest["binaries"]["strix-server"]
        manifest_digest = install.sha256(self.root / "releases" / rid / "MANIFEST.json")
        def fake(argv, **kwargs):
            output = ""
            if argv[-1].startswith("sha256sum"):
                output = (manifest_digest if "MANIFEST.json" in argv[-1] else digest) + "  synthetic\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        with patch.object(install.shutil, "which", return_value="rsync"), patch.object(install, "command", side_effect=fake) as command:
            result = self.call("push-release", release_id=rid, peer_ssh="halo-2", peer_root="/tmp/install space")
        self.assertTrue(result["verified"], result)
        calls = [call.args[0] for call in command.call_args_list]
        rsync = next(i for i, argv in enumerate(calls) if argv[0] == "rsync")
        hashes = [i for i, argv in enumerate(calls) if argv[-1].startswith("sha256sum")]
        publish = next(i for i, argv in enumerate(calls) if " && mv -- " in argv[-1])
        self.assertLess(rsync, min(hashes))
        self.assertLess(max(hashes), publish)
        self.assertIn("StrictHostKeyChecking=yes", calls[0])

    def test_push_release_mismatch_never_publishes(self):
        rid, _ = self.make_release()
        with patch.object(install.shutil, "which", return_value="rsync"), patch.object(install, "command", return_value=subprocess.CompletedProcess([], 0, stdout="bad  synthetic\n")) as command:
            result = self.call("push-release", release_id=rid, peer_ssh="halo-2", peer_root="/tmp/hyperloom")
        self.assertIn("sha256 mismatch", result["error"])
        self.assertFalse(any(" && mv -- " in call.args[0][-1] for call in command.call_args_list))

    def test_push_release_existing_is_verified_without_copy(self):
        rid, manifest = self.make_release()
        def fake(argv, **kwargs):
            output = "existing"
            if argv[-1].startswith("sha256sum"):
                digest = (install.sha256(self.root / "releases" / rid / "MANIFEST.json")
                          if "MANIFEST.json" in argv[-1] else manifest["binaries"]["strix-server"])
                output = digest + "  synthetic\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        with patch.object(install, "command", side_effect=fake) as command:
            result = self.call("push-release", release_id=rid, peer_ssh="halo-2", peer_root="/tmp/hyperloom")
        self.assertTrue(result["verified"], result)
        self.assertTrue(all(call.args[0][0] == "ssh" for call in command.call_args_list))

    def test_push_release_tar_fallback_uses_a_pipe(self):
        rid, manifest = self.make_release()
        def fake(argv, **kwargs):
            output = ""
            if argv[-1].startswith("sha256sum"):
                digest = (install.sha256(self.root / "releases" / rid / "MANIFEST.json")
                          if "MANIFEST.json" in argv[-1] else manifest["binaries"]["strix-server"])
                output = digest + "  synthetic\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        from unittest.mock import MagicMock
        archive = MagicMock()
        archive.__enter__.return_value = archive
        archive.stdout = io.BytesIO(b"synthetic tar")
        archive.returncode = 0
        archive.poll.return_value = 0
        with patch.object(install.shutil, "which", return_value=None), patch.object(install, "command", side_effect=fake), \
                patch.object(install.subprocess, "Popen", return_value=archive) as popen, \
                patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as transfer:
            result = self.call("push-release", release_id=rid, peer_ssh="halo-2", peer_root="/tmp/hyperloom")
        self.assertTrue(result["verified"], result)
        self.assertEqual(popen.call_args.args[0][0], "tar")
        self.assertEqual(transfer.call_args.args[0][0], "ssh")
        self.assertIs(transfer.call_args.kwargs["stdin"], archive.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink boundary checks")
    def test_symlink_protection(self):
        self.mark()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "asset").write_text("keep")
        shutil.rmtree(self.root / "etc")
        (self.root / "etc").symlink_to(outside, target_is_directory=True)
        self.assertIn("error", self.call("activate", profile="qwen-replicas", release_id="a" * 12))
        self.assertIn("error", self.call("uninstall"))
        self.assertEqual((outside / "asset").read_text(), "keep")


@unittest.skipUnless(os.name == "posix", "fake POSIX build tools and detached process groups")
class PosixBuildTests(InstallCase):
    def setUp(self):
        super().setUp()
        tools = self.base / "tools"
        tools.mkdir()
        fixture = Path(__file__).with_name("fixtures") / "node_install" / "fake_tool.py"
        for name in ("git", "cmake", "ninja", "ctest", "systemd-run", "amdclang++", "dpkg-query"):
            path = tools / name
            shutil.copyfile(fixture, path)
            path.chmod(0o755)
        self.trace = self.base / "trace.jsonl"
        self.environment = patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                                 "FAKE_TRACE": str(self.trace), "FAKE_SYSTEMD": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def wait_job(self, result):
        self.assertNotIn("error", result)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.call("job", job=result["job"])
            if state["state"] in {"succeeded", "failed"}:
                return state
            time.sleep(0.05)
        self.fail("background job did not finish")

    def test_build_end_to_end_and_release_stability(self):
        self.payload["components"]["tensormeld"] = {"repo": "https://example.invalid/tensormeld", "commit": "c" * 40}
        first = self.wait_job(install.run("install", self.payload))
        self.assertEqual(first["state"], "succeeded", first)
        second = self.wait_job(install.run("install", self.payload))
        self.assertEqual(second["release_id"], first["release_id"])
        _, manifest = install.release(self.root, first["release_id"])
        self.assertEqual(manifest["runtime_env"], install.RUNTIME_ENV)
        self.assertEqual(manifest["components"], self.payload["components"])
        self.assertTrue(manifest["capabilities"]["strix-server"]["ok"])
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertTrue(any(row[0] == "git" and "--depth" in row and "1" in row for row in trace))
        self.assertTrue(any(row[0] == "ninja" and "-j6" in row and "strix-host-tests" in row for row in trace))
        source = self.root / "src" / ("tensormeld-" + "c" * 40)
        self.assertTrue((source / ".git").is_dir())
        self.assertFalse(any(row[0] == "cmake" and str(source) in row for row in trace))

    def test_trellis_and_systemd_build(self):
        self.payload["recipe"]["trellis"] = True
        self.payload["components"]["strix-trellis"] = {"repo": "https://example.invalid/trellis", "commit": "b" * 40}
        with patch.dict(os.environ, {"FAKE_SYSTEMD": "1"}):
            result = self.wait_job(install.run("install", self.payload))
        self.assertEqual(result["state"], "succeeded", result)
        trace = self.trace.read_text()
        for flag in ("-DSTRIX_TRELLIS_ENABLE_HIP=ON", "-DSTRIX_DS41_TRELLIS_API=v12", "MemoryMax=16G"):
            self.assertIn(flag, trace)

    def test_fetch_recovers_from_interrupted_empty_source_directory(self):
        self.mark()
        source = self.root / "src" / ("engine-" + "a" * 40)
        source.mkdir()
        sources = install.fetch_sources(self.root, self.payload["components"])
        self.assertEqual(sources["engine"], source)
        self.assertEqual((source / ".fake-commit").read_text(), "a" * 40)

    def test_push_release_fetches_router_source_on_peer_without_building(self):
        # Exercise real tar/sha256sum and shell quoting using a local ssh stand-in.
        self.payload["components"]["tensormeld"] = {"repo": "https://example.invalid/tensormeld", "commit": "c" * 40}
        state = self.wait_job(install.run("install", self.payload))
        self.assertEqual(state["state"], "succeeded", state)
        peer_home = self.base / "peer home"
        peer_boot = peer_home / ".hyperloom-boot"
        peer_boot.mkdir(parents=True)
        shutil.copyfile(NODE / "hyperloom_install.py", peer_boot / "hyperloom_install.py")
        tools = self.base / "tools"
        shutil.copyfile(Path(__file__).with_name("fixtures") / "node_install" / "fake_ssh.py", tools / "ssh")
        (tools / "ssh").chmod(0o755)
        peer_root = self.base / "peer install 'quoted'"
        which = shutil.which
        with patch.dict(os.environ, {"HOME": str(peer_home)}), patch.object(
                install.shutil, "which", side_effect=lambda name: None if name == "rsync" else which(name)):
            reply = self.call("push-release", release_id=state["release_id"], peer_ssh="halo-2",
                              peer_root=str(peer_root))
        self.assertTrue(reply.get("verified"), reply)
        install.release(peer_root, state["release_id"])
        self.assertEqual((peer_root / "src" / ("tensormeld-" + "c" * 40) / ".fake-commit").read_text(), "c" * 40)
        self.assertEqual(list((peer_root / "run").glob("job-*.json")), [])
        self.assertFalse((peer_root / "src" / ("engine-" + "a" * 40)).exists())

    def test_build_test_failure_has_no_release(self):
        with patch.dict(os.environ, {"FAKE_CTEST_FAIL": "1"}):
            state = self.wait_job(install.run("install", self.payload))
        self.assertEqual(state["state"], "failed")
        self.assertEqual(list((self.root / "releases").iterdir()), [])

    def test_gpu_test_subset_refused(self):
        with patch.dict(os.environ, {"FAKE_TEST_TARGET": "strix-hip-tests"}):
            state = self.wait_job(install.run("install", self.payload))
        self.assertEqual(state["state"], "failed")

    def test_help_usage_nonzero_is_accepted(self):
        with patch.dict(os.environ, {"FAKE_HELP_EXIT": "2"}):
            state = self.wait_job(install.run("install", self.payload))
        self.assertEqual(state["state"], "succeeded", state)

    def test_hash_job_good_and_bad(self):
        self.mark()
        model = self.base / "model.gguf"
        model.write_bytes(b"model")
        for digest, expected in ((hashlib.sha256(b"model").hexdigest(), "succeeded"), ("0" * 64, "failed")):
            state = self.wait_job(self.call("verify-model", path=str(model), bytes=5, sha256=digest))
            self.assertEqual(state["state"], expected)


if __name__ == "__main__":
    unittest.main()
