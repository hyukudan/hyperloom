"""Asset hashing uses temporary files, without GPUs or remote machines."""

import ctypes
import errno
import hashlib
import importlib.util
import mmap
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "node_assets_under_test", Path(__file__).resolve().parents[1] / "node/hyperloom_install.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class NodeAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-assets-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.model = self.directory / "model"
        self.model.write_bytes(b"model")

    @unittest.skipUnless(hasattr(os, "O_DIRECT") and hasattr(os, "readv"), "Linux direct I/O")
    def test_direct_io_real_aligned_multiblock_and_tail(self):
        data = b"d" * node.HASH_BLOCK + b"tail" * 31
        self.model.write_bytes(data)
        # Some filesystems cannot do direct I/O; exercise fallback in a separate test.
        try:
            fd = os.open(self.model, os.O_RDONLY | os.O_DIRECT)
            try:
                with mmap.mmap(-1, node.HASH_BLOCK) as buffer:
                    os.readv(fd, [buffer])
            finally:
                os.close(fd)
        except OSError as exc:
            if exc.errno in node.DIRECT_UNSUPPORTED:
                self.skipTest("temporary filesystem does not support O_DIRECT")
            raise
        real_readv = os.readv
        calls = []

        def readv(fd, buffers):
            self.assertEqual(len(buffers[0]), 8 * 1024 ** 2)
            self.assertEqual(ctypes.addressof(ctypes.c_char.from_buffer(buffers[0])) % mmap.PAGESIZE, 0)
            calls.append(os.lseek(fd, 0, os.SEEK_CUR))
            return real_readv(fd, buffers)

        with patch.object(node.os, "readv", side_effect=readv), patch.object(
                node.os, "posix_fadvise") as advise:
            self.assertEqual(node.hash_file(self.model), hashlib.sha256(data).hexdigest())
        self.assertEqual(calls, [0, node.HASH_BLOCK])
        advise.assert_not_called()

    def test_unsupported_direct_open_uses_noreuse(self):
        real_open = os.open
        direct = getattr(os, "O_DIRECT", 0x40000000)
        calls = []

        def open_file(path, flags):
            calls.append(flags)
            if flags & direct:
                raise OSError(errno.EOPNOTSUPP, "synthetic unsupported direct I/O")
            return real_open(path, flags)

        with patch.object(node.os, "O_DIRECT", direct, create=True), patch.object(
                node.os, "readv", create=True), patch.object(node.os, "open", side_effect=open_file), patch.object(
                node.os, "POSIX_FADV_NOREUSE", 5, create=True), patch.object(
                node.os, "posix_fadvise", create=True) as advise:
            self.assertEqual(node.hash_file(self.model), hashlib.sha256(b"model").hexdigest())
        self.assertEqual(calls, [os.O_RDONLY | direct, os.O_RDONLY])
        self.assertEqual(advise.call_args.args[1:], (0, 0, 5))

    @unittest.skipUnless(hasattr(os, "O_DIRECT") and hasattr(os, "readv"), "Linux direct I/O")
    def test_unsupported_direct_read_restarts_hash_from_zero(self):
        with patch.object(node.os, "readv", side_effect=OSError(errno.EINVAL, "unsupported")), patch.object(
                node.os, "posix_fadvise") as advise:
            self.assertEqual(node.hash_file(self.model), hashlib.sha256(b"model").hexdigest())
        advise.assert_called_once()

    @unittest.skipUnless(hasattr(os, "O_DIRECT"), "Linux direct I/O")
    def test_direct_io_errors_do_not_fall_back(self):
        for error in (errno.EIO, errno.EACCES):
            with self.subTest(error=error), patch.object(node.os, "open", side_effect=OSError(error, "failed")), patch.object(
                    node.os, "posix_fadvise") as advise:
                with self.assertRaises(OSError):
                    node.hash_file(self.model)
                advise.assert_not_called()

    def test_directory_digest_is_canonical_and_stable(self):
        pack = self.directory / "pack"
        (pack / "sub").mkdir(parents=True)
        (pack / "z").write_bytes(b"last")
        (pack / "sub/a").write_bytes(b"first")
        expected = hashlib.sha256(node.canonical([
            ("sub/a", 5, hashlib.sha256(b"first").hexdigest()),
            ("z", 4, hashlib.sha256(b"last").hexdigest())])).hexdigest()
        first = node.hash_asset(pack)
        self.assertEqual(first["sha256"], expected)
        self.assertEqual(first["bytes"], 9)
        # Creation order, file identity and mtime do not enter the content digest.
        (pack / "z").unlink()
        (pack / "z").write_bytes(b"last")
        self.assertEqual(node.hash_asset(pack)["sha256"], expected)
        self.assertNotEqual(first["tree_stat_sha256"], node.stat_asset(pack)["tree_stat_sha256"])

    def test_directory_child_stat_changes_without_hashing(self):
        pack = self.directory / "pack"
        pack.mkdir()
        child = pack / "child"
        child.write_bytes(b"old")
        before = node.stat_asset(pack)
        child.write_bytes(b"new")
        stamp = child.stat().st_mtime_ns + 1000000000
        os.utime(child, ns=(stamp, stamp))
        with patch.object(node, "hash_file", side_effect=AssertionError("stat must not read content")):
            after = node.stat_asset(pack)
        self.assertEqual(before["mtime_ns"], after["mtime_ns"])
        self.assertNotEqual(before["tree_stat_sha256"], after["tree_stat_sha256"])

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest("symlinks unavailable: " + str(exc))

    def test_symlink_escape_refused_for_stat_and_hash(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                pack = self.directory / ("dir-pack" if directory else "file-pack")
                pack.mkdir()
                self.symlink(pack / "escape", self.directory if directory else self.model, directory)
                for operation in (node.hash_asset, node.stat_asset):
                    with self.assertRaisesRegex(ValueError, "symlink leaves directory"):
                        operation(pack)

    def test_internal_symlink_and_cycle(self):
        pack = self.directory / "pack"
        pack.mkdir()
        (pack / "file").write_bytes(b"data")
        self.symlink(pack / "alias", pack / "file")
        self.assertEqual(node.hash_asset(pack)["bytes"], 8)
        self.symlink(pack / "cycle", pack, True)
        with self.assertRaisesRegex(ValueError, "symlink cycle"):
            node.stat_asset(pack)

    def test_file_symlink_cycle_is_a_structured_error(self):
        pack = self.directory / "pack"
        pack.mkdir()
        self.symlink(pack / "cycle", pack / "cycle")
        result = node.run("stat-assets", {"paths": [str(pack)]})
        self.assertIn("error", result["assets"][str(pack)])

    def test_stat_assets_reports_identity_and_missing_path(self):
        missing = str(self.directory / "absent")
        result = node.run("stat-assets", {"paths": [str(self.model), missing]})
        self.assertEqual(result["assets"][str(self.model)], node.file_identity(self.model.stat()))
        self.assertIn("error", result["assets"][missing])
        self.assertIn("error", node.run("stat-assets", {"paths": "invalid"}))

    def test_background_unknown_digest_and_directory_worker(self):
        root = node.checked_root({"root": str(self.directory / "install")}, create=True)
        pack = self.directory / "pack"
        pack.mkdir()
        (pack / "model").write_bytes(b"model")
        for asset in (self.model, pack):
            with patch.object(node, "launch_job", return_value={"job": "synthetic"}) as launch:
                result = node.run("verify-model", {"root": str(root), "path": str(asset), "bytes": 5, "sha256": None})
            self.assertEqual(result["job"], "synthetic")
            payload = launch.call_args.args[2]
            spec_path = root / "run/job-synthetic.spec.json"
            node.atomic_json(spec_path, {"root": str(root), "job": "synthetic", "limited": False,
                                        "kind": "verify-model", "payload": payload})
            self.assertEqual(node.job_main(str(spec_path)), 0)
            state = node.read_json(root / "run/job-synthetic.json")
            for key, value in node.hash_asset(asset).items():
                self.assertEqual(state[key], value)

    def test_hash_refuses_changes_during_read(self):
        real_hash = node.hash_file

        def changing_hash(path):
            result = real_hash(path)
            stamp = path.stat().st_mtime_ns + 1000000000
            os.utime(path, ns=(stamp, stamp))
            return result

        with patch.object(node, "hash_file", side_effect=changing_hash):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                node.hash_asset(self.model)

    @unittest.skipUnless(os.name == "posix", "node launch requires POSIX")
    def test_hash_job_is_nice_19_and_idle_io(self):
        root = node.checked_root({"root": str(self.directory / "install")}, create=True)
        with patch.object(node.shutil, "which", return_value="/usr/bin/ionice"), patch.object(
                node.subprocess, "Popen", return_value=Mock(pid=123)) as launch:
            node.launch_job(root, "verify-model", {})
        self.assertEqual(launch.call_args.args[0][:6], ["nohup", "nice", "-n", "19", "ionice", "-c3"])
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

    @unittest.skipUnless(os.name == "posix", "node launch requires POSIX")
    def test_missing_ionice_refuses_without_a_queued_job(self):
        root = node.checked_root({"root": str(self.directory / "install")}, create=True)
        with patch.object(node.shutil, "which", return_value=None), patch.object(node.subprocess, "Popen") as launch:
            result = node.run("verify-model", {"root": str(root), "path": str(self.model), "bytes": 5})
        self.assertIn("requires ionice", result["error"])
        launch.assert_not_called()
        self.assertEqual(list((root / "run").iterdir()), [])
