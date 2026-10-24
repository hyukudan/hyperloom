"""Confirmed pinned downloads: synthetic curl, temp roots, no network or ssh."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hyperloom import downloads
from hyperloom.config import parse
from tests.test_config import example


NODE = Path(__file__).resolve().parents[1] / "node"
FAKE_CURL = Path(__file__).resolve().parent / "fixtures" / "node_download" / "fake_curl.py"
sys.path.insert(0, str(NODE))
spec = importlib.util.spec_from_file_location("node_download_under_test", NODE / "hyperloom_download.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
sys.path.remove(str(NODE))
install = node.install
BODY = b"payload"


class NodeDownloadCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom-download-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "install root"
        install.checked_root({"root": str(self.root)}, create=True)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        (self.assets / node.ASSET_MARKER).write_text("", encoding="utf-8")
        self.lab = self.base / "lab"
        (self.lab / ".gpu-turn").mkdir(parents=True)
        which = patch.object(node.shutil, "which", side_effect=lambda name, path=None:
                             "/usr/bin/curl" if name == "curl" else None)
        which.start()
        self.addCleanup(which.stop)

    def item(self, **changes):
        item = {"repo": "unsloth/Qwen3.8-Flash-Next-GGUF", "revision": "a" * 40, "file": "UD/part-1.gguf",
                "bytes": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest(),
                "directory": str(self.assets), "destination": str(self.assets / "part-1.gguf")}
        item.update(changes)
        return item

    def payload(self, *items, **extra):
        return {"root": str(self.root), "lab_dir": str(self.lab), "files": list(items) or [self.item()], **extra}


class NodePlanTests(NodeDownloadCase):
    def test_plan_is_exact_and_deterministic(self):
        first = node.run("download-plan", self.payload())
        self.assertNotIn("error", first)
        self.assertEqual(first["confirm"], node.run("download-plan", self.payload())["confirm"])
        row = first["files"][0]
        url = "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/" + "a" * 40 + "/UD/part-1.gguf"
        self.assertEqual(row["argv"][:3], ["nice", "-n", "19"])
        self.assertEqual(row["argv"][-3:], ["--output", str(self.assets / ".part-1.gguf.hyperloom-part"), url])
        self.assertIn("--proto", row["argv"])
        self.assertTrue(first["disk"][0]["ok"])
        other = node.run("download-plan", self.payload(self.item(sha256="b" * 64)))
        self.assertNotEqual(first["confirm"], other["confirm"])

    def test_plan_refusals(self):
        cases = {
            "revision": self.item(revision="main"),
            "repo": self.item(repo="../x"),
            "file traversal": self.item(file="../secret"),
            "no sha": self.item(sha256=None),
            "zero bytes": self.item(bytes=0),
            "escape": self.item(destination=str(self.base / "elsewhere.gguf")),
            "dot name": self.item(destination=str(self.assets / "..")),
            "missing dir": self.item(directory=str(self.base / "none"), destination=str(self.base / "none" / "x")),
            "root overlap": self.item(directory=str(self.root), destination=str(self.root / "x.gguf")),
            "lab overlap": self.item(directory=str(self.lab), destination=str(self.lab / "x.gguf")),
        }
        for name, item in cases.items():
            with self.subTest(name):
                self.assertIn("error", node.run("download-plan", self.payload(item)))
        (self.assets / "part-1.gguf").write_bytes(b"existing")
        reply = node.run("download-plan", self.payload())
        self.assertIn("already exists", reply["error"])
        self.assertEqual((self.assets / "part-1.gguf").read_bytes(), b"existing")

    def test_only_a_marked_hyperloom_asset_directory(self):
        # A lab model directory (no marker) is never written into, even outside lab_dir.
        lab_models = self.base / "lab models"
        lab_models.mkdir()
        reply = node.run("download-plan", self.payload(self.item(directory=str(lab_models),
                                                                 destination=str(lab_models / "part-1.gguf"))))
        self.assertIn("not dedicated to hyperloom downloads", reply["error"])
        (lab_models / node.ASSET_MARKER).mkdir()
        reply = node.run("download-plan", self.payload(self.item(directory=str(lab_models),
                                                                 destination=str(lab_models / "part-1.gguf"))))
        self.assertIn("not dedicated to hyperloom downloads", reply["error"])

    def test_active_lease_refuses_download(self):
        code = node.run("download-plan", self.payload())["confirm"]
        install.atomic_json(self.root / "run" / "lease-l1.json", {"lease": "l1", "state": "acquired"})
        with patch.object(node.subprocess, "Popen") as popen:
            reply = node.run("download", self.payload(confirm=code))
        self.assertIn("lease is active: l1", reply["error"])
        popen.assert_not_called()

    def test_stale_running_job_is_settled(self):
        path = self.root / "run" / "job-abc.json"
        install.atomic_json(path, {"job": "abc", "kind": "download", "state": "running", "boot_id": "an-earlier-boot"})
        self.assertEqual(node.run("download-cancel", {"root": str(self.root), "job": "abc"})["state"], "failed")
        self.assertFalse((self.root / "run" / "job-abc.stop").exists())
        state = install.read_json(path)
        self.assertIn("rebooted", state["error"])
        reply = install.run("job", {"root": str(self.root), "job": "abc"})
        self.assertEqual(reply["state"], "failed")
        # Jobs without a boot record (builds, hashing) are reported as they are.
        install.atomic_json(path, {"job": "abc", "kind": "install", "state": "running"})
        self.assertEqual(install.run("job", {"root": str(self.root), "job": "abc"})["state"], "running")

    def test_curl_missing(self):
        with patch.object(node.shutil, "which", return_value=None):
            self.assertIn("curl", node.run("download-plan", self.payload())["error"])

    def test_confirm_mismatch_and_pair_turn_refused(self):
        code = node.run("download-plan", self.payload())["confirm"]
        with patch.object(node.subprocess, "Popen") as popen:
            reply = node.run("download", self.payload(confirm="0" * 12))
            self.assertIn("does not match", reply["error"])
            (self.lab / ".gpu-turn" / "holder").write_text("PAIR sx-1 123\n")
            reply = node.run("download", self.payload(confirm=code))
            self.assertIn("PAIR", reply["error"])
            (self.lab / ".gpu-turn" / "holder").write_text("SINGLE sx-2 456\n")
            reply = node.run("download", self.payload(confirm=code))
            self.assertIn("GPU turn is held: SINGLE", reply["error"])
            popen.assert_not_called()

    def test_cancel_only_download_jobs(self):
        install.atomic_json(self.root / "run" / "job-abc.json", {"job": "abc", "kind": "install", "state": "running"})
        self.assertIn("not a download", node.run("download-cancel", {"root": str(self.root), "job": "abc"})["error"])
        install.atomic_json(self.root / "run" / "job-abc.json", {"job": "abc", "kind": "download", "state": "running",
                                                                "boot_id": install.boot_id(), "pid": os.getpid()})
        self.assertEqual(node.run("download-cancel", {"root": str(self.root), "job": "abc"})["state"], "running")
        self.assertTrue((self.root / "run" / "job-abc.stop").exists())


class NodeJobTests(NodeDownloadCase):
    def job(self, body=BODY, **env):
        rows = node.plan(self.payload(), self.root)["files"]
        for row in rows:
            row["argv"] = [sys.executable, str(FAKE_CURL), *row["argv"][row["argv"].index("/usr/bin/curl") + 1:]]
        spec_path = self.root / "run" / "job-j1.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "j1", "lab_dir": str(self.lab), "files": rows})
        with patch.dict(os.environ, {"FAKE_BODY": body.decode(), **env}):
            code = node.job_main(str(spec_path))
        return code, install.read_json(self.root / "run" / "job-j1.json")

    def test_verified_file_is_renamed(self):
        code, state = self.job()
        self.assertEqual((code, state["state"]), (0, "succeeded"))
        self.assertEqual((self.assets / "part-1.gguf").read_bytes(), BODY)
        self.assertFalse((self.assets / ".part-1.gguf.hyperloom-part").exists())

    def test_verify_failure_leaves_no_final_file(self):
        code, state = self.job(body=b"tampered")
        self.assertEqual((code, state["state"]), (1, "failed"))
        self.assertIn("verification failed", state["error"])
        self.assertEqual(sorted(p.name for p in self.assets.iterdir()), [node.ASSET_MARKER])

    def test_failed_command_keeps_partial_for_resume(self):
        code, state = self.job(body=b"pay", FAKE_EXIT="22")
        self.assertIn("exited 22", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())
        self.assertEqual((self.assets / ".part-1.gguf.hyperloom-part").read_bytes(), b"pay")

    def test_hash_uses_the_direct_hasher_and_stops_for_a_turn(self):
        calls = []
        original = install.hash_file

        def hasher(path, check=None):
            calls.append(path)
            (self.lab / ".gpu-turn" / "holder").write_text("SINGLE sx-2 456\n")
            return original(path, check=check)

        with patch.object(install, "hash_file", side_effect=hasher):
            code, state = self.job()
        self.assertEqual(calls, [self.assets / ".part-1.gguf.hyperloom-part"])
        self.assertIn("GPU turn is held", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())
        self.assertEqual((self.assets / ".part-1.gguf.hyperloom-part").read_bytes(), BODY)

    @unittest.skipUnless(os.name == "posix", "flock")
    def test_partial_file_has_one_writer(self):
        part = self.assets / ".part-1.gguf.hyperloom-part"
        held = node.lock_part(part)
        try:
            code, state = self.job()
        finally:
            os.close(held)
        self.assertIn("another download job is writing", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_pair_turn_interrupts_running_download(self):
        (self.lab / ".gpu-turn" / "holder").write_text("PAIR sx-1 123\n")
        code, state = self.job(FAKE_SLEEP="30")
        self.assertIn("PAIR", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.obj = example()
        for machine in self.obj["machines"][1:]:
            machine["lab_dir"] = "/srv/lab"
        self.obj["assets"]["halo-1"]["model"].update(bytes=111, sha256="c" * 64)
        self.calls = []

        def call(client, verb, payload, timeout=60):
            self.calls.append((verb, payload))
            return {"files": [], "disk": [], "confirm": "0123456789ab", "job": "j", "log": "/l"}

        for name, value in (("_client", lambda machine: machine), ("_call", call)):
            patcher = patch.object(downloads.orchestrate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_catalog_is_checked_at_the_engine_pin(self):
        lock = json.loads((Path(__file__).resolve().parents[1] / "versions.lock").read_text(encoding="utf-8"))
        self.assertEqual(downloads.catalog()["engine_sha"], lock["components"]["engine"]["commit"])
        with patch.object(downloads, "read_json", side_effect=lambda path: (
                dict(json.loads(Path(path).read_text(encoding="utf-8")), engine_sha="0" * 40)
                if Path(path).name == "sources.json" else json.loads(Path(path).read_text(encoding="utf-8")))):
            with self.assertRaisesRegex(downloads.ConfigError, "pinned engine"):
                downloads.catalog()

    def test_catalog_pins_and_reasons(self):
        rows = downloads.sources(parse(self.obj), "qwen-replicas")
        self.assertEqual(rows["model"]["revision"], "38bb39ee97821de2c9009abb7e93950eec396e66")
        self.assertEqual(len(rows["model"]["files"]), 4)
        self.assertIn("documented only", rows["model"]["after"])
        self.assertEqual(rows["sidecar"]["status"], "unavailable")
        pair = downloads.sources(parse(self.obj), "ds41-pair")
        self.assertTrue(all(row["status"] == "unavailable" for row in pair.values()))

    def test_plan_targets_the_asset_directory(self):
        result = downloads.plan(parse(self.obj), "qwen-replicas", "model", "halo-1")
        verb, payload = self.calls[-1]
        self.assertEqual(verb, "download-plan")
        self.assertEqual({row["directory"] for row in payload["files"]}, {"/opt/assets"})
        self.assertTrue(all(row["destination"].startswith("/opt/assets/Qwen3.8") for row in payload["files"]))
        self.assertIn("--confirm 0123456789ab", result["next"])

    def test_refusals_before_any_node_call(self):
        cluster = parse(self.obj)
        for args, message in ((("qwen-replicas", "sidecar", "halo-1"), "not downloadable"),
                              (("qwen-replicas", "model", "halo-2"), "real bytes and sha256"),
                              (("qwen-replicas", "model", "workstation"), "not a machine"),
                              (("ds41-pair", "ds41_model", "halo-1"), "not downloadable")):
            with self.subTest(args), self.assertRaisesRegex(ValueError, message):
                downloads.plan(cluster, *args)
        with self.assertRaisesRegex(ValueError, "12-character"):
            downloads.start(cluster, "qwen-replicas", "model", "halo-1", "yes")
        self.assertEqual(self.calls, [])

    def test_start_sends_the_code(self):
        downloads.start(parse(self.obj), "qwen-replicas", "model", "halo-1", "0123456789ab")
        self.assertEqual(self.calls[-1][0], "download")
        self.assertEqual(self.calls[-1][1]["confirm"], "0123456789ab")


if __name__ == "__main__":
    unittest.main()
