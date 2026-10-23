"""Control-side manifests and starts use fake node clients only."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hyperloom import assets, cli, orchestrate as orch, secrets
from hyperloom.render import ProcessSpec
from tests.test_orchestrate import cluster_fixture, FakeRemote
from tests.test_node_assets import node


class AssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-manifest-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.cluster = cluster_fixture()
        self.calls, self.leases, self.fail = [], {}, set()
        self.files = {}
        for mid in self.cluster.machines:
            model = self.directory / mid / "model"
            model.parent.mkdir()
            model.write_bytes(b"model")
            self.files[mid] = model
            self.cluster.assets[mid] = {"model": {"path": str(model), "bytes": 5,
                                                "sha256": hashlib.sha256(b"model").hexdigest()}}
        patches = [
            patch.object(secrets, "_directory", return_value=self.directory / "config/secrets"),
            patch.object(secrets, "runtime_dir", return_value=self.directory / "run"),
            patch.object(orch, "_client", side_effect=self.client),
            patch.object(orch, "_wait_job", side_effect=self.wait_job),
            patch.object(orch, "_start_up_watch"),
            patch.object(orch, "_start_local_router", return_value={"machine": "local", "endpoint": {}}),
            patch.object(orch, "_stop_local_router"),
            patch.object(orch, "_http_json", return_value={}),
            patch.object(orch.render, "render_processes", side_effect=self.processes),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, machine, known_hosts=None):
        client = FakeRemote(machine, self.calls, self.leases, self.fail)
        call = client.call

        def invoke(verb, payload, timeout=60):
            if verb == "stat-assets":
                self.calls.append((machine.id, verb, deepcopy(payload)))
                return node.run(verb, payload)
            if verb == "verify-model":
                self.calls.append((machine.id, verb, deepcopy(payload)))
                return {"job": payload["path"]}
            return call(verb, payload, timeout)

        client.call = invoke
        return client

    def wait_job(self, client, root, job, timeout=3600):
        return {"state": "succeeded", **node.hash_asset(Path(job))}

    def processes(self, cluster, profile):
        _, kind, mids = orch._profile(cluster, profile)
        return [ProcessSpec(mid, "replica" if kind == "qwen-replicas" else f"rank{i}",
                            ["/opt/hyperloom/bin/engine", "--http-port", "18501"], {}, "qwen-stderr")
                for i, mid in enumerate(mids)]

    def entry(self, mid):
        return {**node.hash_asset(self.files[mid]), "verified_at": 123.0}

    def manifest(self):
        manifest = {mid: {str(path): self.entry(mid)} for mid, path in self.files.items()}
        assets.write_manifest(manifest)
        return manifest

    def assert_refused(self, pattern, profile="qwen-replicas"):
        with self.assertRaisesRegex(orch.OrchestrationError, pattern):
            orch.up(self.cluster, profile)
        self.assertNotIn("lease", [verb for _, verb, _ in self.calls])

    def test_manifest_missing_roundtrip_and_private_file(self):
        self.assertEqual(assets.read_manifest(), {})
        manifest = self.manifest()
        self.assertEqual(assets.read_manifest(), manifest)
        self.assertEqual(assets.manifest_path(), self.directory / "config/assets-manifest.json")
        if os.name == "posix":
            self.assertEqual(assets.manifest_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(assets.manifest_path().parent.iterdir()), [assets.manifest_path()])

    def test_corrupt_manifest_is_not_silently_size_only(self):
        self.manifest()
        assets.manifest_path().write_text('{"schema":"unknown"}')
        with self.assertRaisesRegex(ValueError, "invalid assets manifest"):
            assets.read_manifest()
        with self.assertRaisesRegex(ValueError, "invalid assets manifest"):
            orch.up(self.cluster, "qwen-replicas")
        self.assertNotIn("lease", [verb for _, verb, _ in self.calls])

    def test_verify_every_machine_and_read_back_identity(self):
        result = assets.verify(self.cluster, "qwen-replicas")
        self.assertEqual(len(result["verified"]), 2)
        manifest = assets.read_manifest()
        for mid, path in self.files.items():
            entry = manifest[mid][str(path)]
            for key, value in node.hash_asset(path).items():
                self.assertEqual(entry[key], value)
            self.assertGreater(entry["verified_at"], 0)
        self.assertEqual([mid for mid, verb, _ in self.calls if verb == "verify-model"], ["halo-1", "halo-2"])

    def test_large_boundary_and_machine_filter(self):
        self.cluster.assets["halo-1"]["model"]["bytes"] = assets.LARGE
        result = assets.verify(self.cluster, "qwen-replicas", machine="halo-1")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["verified"], [])
        self.assertEqual(self.calls, [])
        self.assertFalse(assets.manifest_path().exists())
        fake = {**self.entry("halo-1"), "bytes": assets.LARGE}
        with patch.object(orch, "_wait_job", return_value=fake):
            result = assets.verify(self.cluster, "qwen-replicas", include_large=True, machine="halo-1")
        self.assertEqual(len(result["verified"]), 1)
        self.assertEqual(set(assets.read_manifest()), {"halo-1"})
        with self.assertRaisesRegex(ValueError, "not part of the profile"):
            assets.verify(self.cluster, "qwen-replicas", machine="workstation")

    def test_unknown_config_digest_is_hashed(self):
        for entries in self.cluster.assets.values():
            entries["model"]["sha256"] = "0" * 64
        assets.verify(self.cluster, "qwen-replicas")
        for _, verb, payload in self.calls:
            if verb == "verify-model":
                self.assertIsNone(payload["sha256"])
        self.assertEqual(assets.read_manifest()["halo-1"][str(self.files["halo-1"])]["sha256"],
                         hashlib.sha256(b"model").hexdigest())

    def test_failed_hash_job_does_not_replace_trusted_entry(self):
        before = self.manifest()
        with patch.object(orch, "_wait_job", side_effect=orch.OrchestrationError("hash job failed")):
            with self.assertRaisesRegex(orch.OrchestrationError, "halo-1.*model.*hash job failed"):
                assets.verify(self.cluster, "qwen-replicas")
        self.assertEqual(assets.read_manifest(), before)

    def test_same_size_wrong_content_manifest_refused_before_any_lease(self):
        self.files["halo-1"].write_bytes(b"wrong")
        self.manifest()
        self.assert_refused("halo-1.*model.*sha256 differs from config")
        status = assets.status(self.cluster, "qwen-replicas")
        self.assertTrue(status["assets"][0]["stale"])

    def test_changed_mtime_refused_and_status_is_stale(self):
        self.manifest()
        path = self.files["halo-2"]
        stamp = path.stat().st_mtime_ns + 1000000000
        os.utime(path, ns=(stamp, stamp))
        self.assert_refused("halo-2.*model.*mtime_ns")
        status = assets.status(self.cluster, "qwen-replicas")
        self.assertTrue(status["assets"][1]["stale"])

    def test_same_size_content_changed_after_verification_is_stale(self):
        self.manifest()
        path = self.files["halo-1"]
        before = path.stat()
        path.write_bytes(b"wrong")
        stamp = before.st_mtime_ns + 1000000000
        os.utime(path, ns=(stamp, stamp))
        self.assert_refused("halo-1.*model.*stale manifest")

    def test_inode_and_dev_changes_refused(self):
        for field in ("inode", "dev"):
            with self.subTest(field=field):
                manifest = self.manifest()
                manifest["halo-1"][str(self.files["halo-1"])][field] += 1
                assets.write_manifest(manifest)
                self.assert_refused("halo-1.*model.*" + field)

    def test_unverified_size_mismatch_refused(self):
        for size in (4, 6):
            with self.subTest(size=size):
                self.cluster.assets["halo-2"]["model"]["bytes"] = size
                self.assert_refused("halo-2.*model.*size mismatch")

    def test_zero_bytes_means_size_not_pinned(self):
        # Directory assets (vocab, trellis packs) are configured with bytes 0.
        for mid in self.cluster.machines:
            pack = self.directory / mid / "pack"
            pack.mkdir()
            (pack / "a.bin").write_bytes(b"abc")
            self.cluster.assets[mid]["model"] = {"path": str(pack), "bytes": 0, "sha256": "0" * 64}
        result = orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([row["verification"] for row in result["assets"]], ["size only", "size only"])
        verified = assets.verify(self.cluster, "qwen-replicas")["verified"]
        self.assertEqual(len(verified), 2)
        self.assertEqual(verified[0]["bytes"], 3)
        orch.down("qwen-replicas")
        result = orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([row["verification"] for row in result["assets"]], ["sha256", "sha256"])

    def test_size_only_up_and_status_without_manifest(self):
        result = orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([row["verification"] for row in result["assets"]], ["size only", "size only"])
        self.assertEqual([row["verification"] for row in orch.status("qwen-replicas")["assets"]],
                         ["size only", "size only"])
        status = assets.status(self.cluster, "qwen-replicas")
        self.assertTrue(all(row["ok"] and not row["stale"] for row in status["assets"]))
        self.assertIn("not an integrity guarantee", status["cache_note"])
        self.assertNotIn("verify-model", [verb for _, verb, _ in self.calls])
        verbs = [verb for _, verb, _ in self.calls]
        self.assertLess(max(i for i, verb in enumerate(verbs[:verbs.index("lease")]) if verb == "stat-assets"),
                        verbs.index("lease"))

    def test_verified_up_uses_stat_without_hash_jobs(self):
        self.manifest()
        result = orch.up(self.cluster, "qwen-replicas")
        self.assertTrue(all(row["verification"] == "sha256" for row in result["assets"]))
        self.assertNotIn("verify-model", [verb for _, verb, _ in self.calls])

    def test_directory_child_change_refused_without_reading_contents(self):
        pack = self.directory / "vocab"
        pack.mkdir()
        child = pack / "words"
        child.write_bytes(b"old")
        self.cluster.assets["halo-1"]["vocab"] = {"path": str(pack), "bytes": 3, "sha256": "0" * 64}
        self.manifest()
        manifest = assets.read_manifest()
        manifest["halo-1"][str(pack)] = {**node.hash_asset(pack), "verified_at": 123.0}
        assets.write_manifest(manifest)
        stamp = child.stat().st_mtime_ns + 1000000000
        os.utime(child, ns=(stamp, stamp))
        with patch.object(node, "hash_file", side_effect=AssertionError("no content reads on up")):
            self.assert_refused("halo-1.*vocab.*tree_stat_sha256")

    def test_pair_cross_rank_mismatch_and_one_unknown_rank(self):
        self.cluster = cluster_fixture(pair=True)
        for mid, path in self.files.items():
            self.cluster.assets[mid] = {"ds41_model": {"path": str(path), "bytes": 5, "sha256": "0" * 64}}
        self.files["halo-2"].write_bytes(b"wrong")
        manifest = self.manifest()
        self.assert_refused("halo-1.*ds41_model.*between pair ranks", "ds41-pair")
        rows = assets.status(self.cluster, "ds41-pair")["assets"]
        self.assertTrue(all(row["stale"] for row in rows))
        del manifest["halo-2"]
        assets.write_manifest(manifest)
        result = orch.up(self.cluster, "ds41-pair")
        self.assertEqual([row["verification"] for row in result["assets"]], ["sha256", "size only"])

    def test_cli_verify_and_status(self):
        for args in (("verify", "--include-large", "--machine", "halo-2"), ("status",)):
            output, error = io.StringIO(), io.StringIO()
            with patch.object(cli, "_cluster", return_value=self.cluster), redirect_stdout(output), redirect_stderr(error):
                code = cli.main(["assets", args[0], "qwen-replicas", *args[1:]])
            self.assertEqual(code, 0, error.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["profile"], "qwen-replicas")
        self.assertEqual(set(assets.read_manifest()), {"halo-2"})
