"""Model library node module: inventory, guards, verification and its jobs (fixture trees)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


NODE = Path(__file__).resolve().parents[1] / "node"
sys.path.insert(0, str(NODE))
spec = importlib.util.spec_from_file_location("node_models_under_test", NODE / "hyperloom_models.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
sys.path.remove(str(NODE))
install = node.install
POSIX = os.name == "posix"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tree(path):
    """Every path below a directory with its size (directories as None): a snapshot to prove nothing changed."""
    result = {}
    for directory, dirs, files in os.walk(path):
        for name in dirs:
            result[os.path.relpath(os.path.join(directory, name), path)] = None
        for name in files:
            full = os.path.join(directory, name)
            result[os.path.relpath(full, path)] = os.lstat(full).st_size
    return result


class LibraryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom-library-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.lab = self.base / "lab"
        (self.lab / ".gpu-turn").mkdir(parents=True)
        self.peer_lab = self.base / "peer-lab"
        (self.peer_lab / ".gpu-turn").mkdir(parents=True)
        self.root = install.checked_root({"root": str(self.base / "install"), "lab_dir": str(self.lab)}, create=True)
        self.models = self.base / "models"
        self.models.mkdir()
        self.alpha = {"alpha/a.bin": b"A" * 5000, "alpha/sub/b.bin": b"B" * 70000, "alpha/c.json": b"{}"}
        for rel, data in self.alpha.items():
            (self.models / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.models / rel).write_bytes(data)
        (self.models / "alpha.sha256").write_text("".join(f"{digest(d)} {r}\n" for r, d in self.alpha.items()),
                                                  encoding="utf-8")
        (self.models / "beta.gguf").write_bytes(b"G" * 3000)
        (self.models / "beta.gguf.sha256").write_text(f"{digest(b'G' * 3000)}  beta.gguf\n", encoding="utf-8")
        (self.models / "gamma").mkdir()
        (self.models / "gamma" / "x.bin").write_bytes(b"x" * 10)
        (self.models / "gamma" / "files.sha256").write_text(f"{digest(b'x' * 10)} *x.bin\n", encoding="utf-8")
        (self.models / "delta").mkdir()
        (self.models / "delta" / "w.bin").write_bytes(b"w")
        (self.models / "notes.txt").write_text("loose", encoding="utf-8")

    def scan(self, **extra):
        reply = node.run("models", {"roots": [str(self.models)], **extra})
        self.assertNotIn("error", reply)
        return {unit["name"]: unit for unit in reply["roots"][0]["units"]}, reply

class InventoryTests(LibraryCase):
    def test_units_sizes_and_manifest_status(self):
        before = tree(self.base)
        units, reply = self.scan(root=str(self.root), lab_dir=str(self.lab))
        self.assertEqual(tree(self.base), before)                       # read-only: nothing created
        self.assertEqual(sorted(units), ["alpha", "beta.gguf", "delta", "gamma"])
        self.assertEqual(reply["roots"][0]["other_files"], 1)
        self.assertEqual((units["alpha"]["kind"], units["alpha"]["files"], units["alpha"]["bytes"]),
                         ("dir", 3, 75002))
        self.assertEqual(units["alpha"]["manifest"]["status"], "complete")
        self.assertEqual(units["alpha"]["manifest"]["manifests"], ["alpha.sha256"])
        self.assertEqual(units["beta.gguf"]["manifest"]["status"], "complete")
        self.assertEqual(units["gamma"]["manifest"]["status"], "complete")
        self.assertEqual(units["delta"]["manifest"]["status"], "none")
        self.assertIsNotNone(units["alpha"]["newest_mtime"])

    def test_partial_missing_conflict_invalid(self):
        (self.models / "alpha" / "extra.bin").write_bytes(b"e")
        self.assertEqual(self.scan()[0]["alpha"]["manifest"]["status"], "partial")
        (self.models / "alpha" / "extra.bin").unlink()
        (self.models / "alpha" / "c.json").unlink()
        units = self.scan()[0]
        self.assertEqual((units["alpha"]["manifest"]["status"], units["alpha"]["manifest"]["missing"]), ("missing", 1))
        (self.models / "gamma" / "more.sha256").write_text("0" * 64 + " x.bin\n", encoding="utf-8")
        self.assertEqual(self.scan()[0]["gamma"]["manifest"]["status"], "conflict")
        (self.models / "gamma" / "more.sha256").write_text("not a line\n../escape\n", encoding="utf-8")
        units = self.scan()[0]
        self.assertEqual((units["gamma"]["manifest"]["status"], units["gamma"]["manifest"]["invalid"]), ("partial", 2))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_links_are_counted_never_followed(self):
        outside = self.base / "outside.bin"
        outside.write_bytes(b"secret")
        os.symlink(outside, self.models / "alpha" / "link.bin")
        os.symlink(self.base, self.models / "linked-dir")
        units, reply = self.scan()
        self.assertEqual(units["alpha"]["links"], 1)
        self.assertNotIn("linked-dir", units)

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_windows_junctions_are_never_listed_or_entered(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.bin").write_bytes(b"s")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.models / "alpha" / "jump"), str(outside)],
                                capture_output=True)
        if result.returncode:
            self.skipTest("junctions cannot be created here")
        units, _ = self.scan()
        self.assertEqual(units["alpha"]["links"], 1)
        with self.assertRaisesRegex(ValueError, "link or junction"):
            node.windows_open_dir(Path(os.path.realpath(self.models)), ["alpha", "jump"])
        budget = node.Budget(time.monotonic() + 5)
        with node.Tree(str(self.models)) as tree, self.assertRaisesRegex(ValueError, "link or junction"):
            tree.list(["alpha", "jump"], budget)

    @unittest.skipUnless(os.name == "nt", "Windows directory handles")
    def test_windows_listing_stops_at_the_budget_and_the_root_stays_pinned(self):
        for index in range(50):
            (self.models / "gamma" / f"f{index:02d}.bin").write_bytes(b"x")
        consumed = []
        original = node.windows_list

        def counting(handle):
            for entry in original(handle):
                consumed.append(entry.name)
                yield entry
        budget = node.Budget(time.monotonic() + 5)
        with patch.object(node, "MAX_ENTRIES", 2), patch.object(node, "windows_list", counting), \
                node.Tree(str(self.models)) as tree, self.assertRaises(node.Bound):
            tree.list(["gamma"], budget)
        self.assertLessEqual(len(consumed), 3)                       # enumeration stopped, not only the result
        with node.Tree(str(self.models)) as tree:
            with self.assertRaises(OSError):                         # the open root cannot be renamed or replaced
                os.rename(self.models, self.base / "models-moved")
            self.assertEqual(tree.read("alpha.sha256", 4096), (self.models / "alpha.sha256").read_bytes())

    def test_bounds_are_reported_as_unknown(self):
        with patch.object(node, "MAX_ENTRIES", 9):
            units, reply = self.scan()
        self.assertIn("files", reply["roots"][0]["truncated"])
        self.assertTrue(any(u["manifest"]["status"] == "unknown" for u in units.values()))
        (self.models / "alpha" / "d1" / "d2" / "d3").mkdir(parents=True)
        units, reply = self.scan()
        self.assertEqual(units["alpha"]["manifest"]["status"], "unknown")
        self.assertIn("depth", reply["roots"][0]["truncated"])
        with patch.object(node, "MANIFEST_TOTAL_MAX", 10):
            units, reply = self.scan()
        self.assertIn("manifest bytes", reply["roots"][0]["truncated"])
        with patch.object(node.time, "monotonic", side_effect=[0.0] + [100.0] * 1000):
            reply = node.models({"roots": [str(self.models)]})       # the scan budget (run() adds its own)
        self.assertIn("deadline", reply["roots"][0]["truncated"])
        self.assertIn("error", node.run("models", {"roots": [str(self.models)], "deadline_s": 30}))

    def test_reply_is_trimmed_to_the_envelope_cap(self):
        for index in range(300):
            (self.models / f"unit-{index:03d}").mkdir()
        with patch.object(node, "MAX_REPLY", 20000):
            reply = node.run("models", {"roots": [str(self.models)]})
        self.assertLessEqual(len(json.dumps(reply)), 20000)
        self.assertIn("output", reply["roots"][0]["truncated"])

    def test_trimming_a_large_permitted_inventory_is_bounded(self):
        unit = {"name": "u" * 40, "kind": "dir", "bytes": 1, "files": 1, "newest_mtime": 1.0, "links": 0,
                "manifest": {"status": "none", "listed": 0, "missing": 0, "unlisted": 1, "invalid": 0, "conflicts": 0,
                             "manifests": ["m" * 60 + ".sha256"]}}
        big = {"roots": [{"path": f"/opt/r{i}", "units": [dict(unit) for _ in range(500)], "truncated": []}
                         for i in range(16)]}
        started = time.monotonic()
        with patch.object(node, "models", return_value=big):
            reply = node.run("models", {"roots": ["/opt/r"]})
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(len(json.dumps(reply)), node.MAX_REPLY)
        self.assertTrue(all("output" in root["truncated"] for root in reply["roots"] if len(root["units"]) < 500))

    def test_a_large_permitted_inventory_is_processed_within_the_deadline(self):
        # 500 units and 15 sibling manifests of almost 1 MiB each (within every bound), each listing every unit.
        for index in range(500):
            (self.models / f"u{index:03d}").mkdir()
            (self.models / f"u{index:03d}" / "f.bin").write_bytes(b"f")
        line = digest(b"f") + "  u{:03d}/part-{:05d}.bin\n"
        per_file = (node.MANIFEST_FILE_MAX - 1024) // len(line.format(0, 0))
        for sibling in range(15):                                  # within the 16 MiB total
            text = "".join(line.format(row % 500, row) for row in range(min(per_file, node.MANIFEST_LINES)))
            (self.models / f"set-{sibling:02d}.sha256").write_bytes(text.encode())
        started = time.monotonic()
        reply = node.run("models", {"roots": [str(self.models)]})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, node.DEADLINE_S + 1.5, elapsed)
        self.assertNotIn("error", reply)                                 # processed in full, well within 5 s
        self.assertFalse({"deadline", "manifest bytes", "files"} & set(reply["roots"][0]["truncated"]))
        units = {unit["name"]: unit for unit in reply["roots"][0]["units"]}
        self.assertEqual(units["u000"]["manifest"]["status"], "missing")
        self.assertEqual(len(units["u000"]["manifest"]["manifests"]), 15)

    def test_a_manifest_over_a_per_file_limit_is_a_bound_not_a_completeness_claim(self):
        with patch.object(node, "MANIFEST_FILE_MAX", 200):                # alpha.sha256 is larger than this
            units, reply = self.scan()
            self.assertIn("manifest size", reply["roots"][0]["truncated"])
            self.assertTrue(all(unit["manifest"]["status"] == "unknown" for unit in units.values()))
            with self.assertRaisesRegex(ValueError, "verification refused"):
                node.verify_unit(str(self.models), "alpha")
        with patch.object(node, "MANIFEST_LINES", 2):                     # alpha.sha256 has three lines
            units, reply = self.scan()
            self.assertIn("manifest lines", reply["roots"][0]["truncated"])
            self.assertEqual(units["alpha"]["manifest"]["status"], "unknown")
        padding = "# " + "x" * 1000 + "\n"
        manifest = self.models / "gamma" / "files.sha256"
        manifest.write_bytes((manifest.read_text(encoding="utf-8") + padding * 1100).encode())   # over 1 MiB
        units, reply = self.scan()
        self.assertEqual(units["gamma"]["manifest"]["status"], "unknown")
        self.assertEqual(units["alpha"]["manifest"]["status"], "complete")      # only the unit it belongs to
        self.assertIn("manifest size", reply["roots"][0]["truncated"])
        with self.assertRaisesRegex(ValueError, "verification refused"):
            node.verify_unit(str(self.models), "gamma")

    def test_a_manifest_that_grows_after_listing_is_a_bound(self):
        real = node.Tree.read
        target = self.models / "alpha.sha256"

        def growing(tree, path, limit):
            if path == "alpha.sha256":
                with target.open("ab") as stream:                    # grows between the listing and the read
                    stream.write(b"# " + b"x" * 4096 + b"\n")
            return real(tree, path, limit)
        with patch.object(node.Tree, "read", growing):
            units, reply = self.scan()
            self.assertIn("changed while scanned", reply["roots"][0]["truncated"])
            self.assertTrue(all(unit["manifest"]["status"] == "unknown" for unit in units.values()))
            with self.assertRaisesRegex(ValueError, "verification refused"):
                node.verify_unit(str(self.models), "alpha")
        # The aggregate allowance caps every read: seventeen manifests that each grow to 1 MiB are never all read.
        target.write_bytes(target.read_bytes().split(b"#")[0])
        budget, reads = node.Budget(time.monotonic() + 30), []

        def counting(tree, path, limit):
            reads.append(limit)
            return real(tree, path, limit)
        with node.Tree(str(self.models)) as tree, patch.object(node.Tree, "read", counting):
            budget.manifest_bytes = node.MANIFEST_TOTAL_MAX - 10
            with self.assertRaises(node.Bound):
                node.parse_manifest(tree, "alpha.sha256", target.stat().st_size, budget)
            budget.manifest_bytes = node.MANIFEST_TOTAL_MAX - target.stat().st_size
            node.parse_manifest(tree, "alpha.sha256", target.stat().st_size, budget)
        self.assertEqual(reads, [target.stat().st_size])                 # read limit = what is left
        self.assertEqual(budget.manifest_bytes, node.MANIFEST_TOTAL_MAX)

    def test_growing_internal_manifests_are_charged_and_bounded(self):
        for index in range(20):
            unit = self.models / f"grow-{index:02d}"
            unit.mkdir()
            (unit / "w.bin").write_bytes(b"w")
            (unit / "files.sha256").write_text(f"{digest(b'w')}  w.bin\n", encoding="utf-8")
        real, consumed = node.Tree.read, []

        def growing(tree, path, limit):
            if path.startswith("grow-") and path.endswith("files.sha256"):
                with (self.models / path).open("ab") as stream:      # grows to about 900 KiB after the listing
                    stream.write(b"# " + b"x" * (900 << 10) + b"\n")
            data = real(tree, path, limit)
            consumed.append(len(data))
            return data
        with patch.object(node.Tree, "read", growing):
            units, reply = self.scan()
        self.assertLessEqual(sum(consumed), node.MANIFEST_TOTAL_MAX)    # never more than the aggregate allowance
        self.assertTrue(all(units[f"grow-{i:02d}"]["manifest"]["status"] == "unknown" for i in range(20)))
        self.assertIn("manifest bytes", reply["roots"][0]["truncated"])

    def test_a_repeated_entry_or_an_invalid_only_sibling_is_partial(self):
        with (self.models / "alpha.sha256").open("a", encoding="utf-8") as stream:
            stream.write(f"{digest(self.alpha['alpha/a.bin'])} alpha/a.bin\n")       # listed twice, same digest
        (self.models / "delta.sha256").write_text("not a manifest line\n", encoding="utf-8")
        units, _ = self.scan()
        self.assertEqual((units["alpha"]["manifest"]["status"], units["alpha"]["manifest"]["repeated"]),
                         ("partial", 1))
        self.assertEqual((units["delta"]["manifest"]["status"], units["delta"]["manifest"]["invalid"]),
                         ("partial", 1))

    def test_the_copy_verbs_are_not_part_of_this_release(self):
        before = tree(self.root)
        for verb in ("copy-plan", "copy"):
            with self.subTest(verb):
                reply = node.run(verb, {"root": str(self.root), "name": "alpha"})
                self.assertEqual(reply["error"], "copies between machines are not available in this release")
        self.assertEqual(tree(self.root), before)
        self.assertFalse(any(hasattr(node, name) for name in ("plan", "start_copy", "publish", "receive_main",
                                                              "send_main", "Staging")))

    def test_slow_empty_manifests_stop_at_the_deadline_or_a_stop(self):
        for index in range(12):
            (self.models / f"empty-{index:02d}.sha256").write_bytes(b"")
        real = node.Tree.read

        def slow(tree, path, limit):
            time.sleep(0.2)                                              # a slow disk: 2.4 s for all of them
            return real(tree, path, limit)
        started = time.monotonic()
        with patch.object(node, "DEADLINE_S", 1.0), patch.object(node.Tree, "read", slow):
            reply = node.run("models", {"roots": [str(self.models)]})
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertTrue("error" in reply or "deadline" in reply["roots"][0]["truncated"], reply)
        due = time.monotonic() + 0.5

        def stop():
            if time.monotonic() > due:
                raise node.Stopped("cancelled")
        started = time.monotonic()
        with patch.object(node.Tree, "read", slow), self.assertRaises(node.Stopped):
            node.verify_unit(str(self.models), "alpha", stop)
        self.assertLess(time.monotonic() - started, 1.2)                 # stopped between the manifest reads

    def test_roots_are_checked(self):
        for bad in (["relative"], [str(self.models / "missing")], [], ["/"] if POSIX else ["C:\\"]):
            with self.subTest(bad):
                reply = node.run("models", {"roots": bad})
                self.assertTrue("error" in reply or "error" in reply["roots"][0], reply)
        self.assertIn("error", node.run("models", {"roots": [str(self.models)], "unknown": 1}))

class PolicyAndGuardTests(LibraryCase):
    def test_recorded_lab_wins_and_mismatch_refuses(self):
        policy = node.effective_policy({"root": str(self.root)})
        self.assertEqual(policy.lab, str(self.lab))
        with self.assertRaisesRegex(ValueError, "differs"):
            node.effective_policy({"root": str(self.root), "lab_dir": str(self.peer_lab)})
        bare = install.checked_root({"root": str(self.base / "bare")}, create=True)
        with self.assertRaisesRegex(ValueError, "no lab directory"):
            node.effective_policy({"root": str(bare), "lab_required": True})
        self.assertEqual(node.effective_policy({"root": str(bare), "lab_dir": str(self.lab)}).lab, str(self.lab))
        with self.assertRaisesRegex(ValueError, "unmarked"):
            node.effective_policy({"root": str(self.models)})

    @unittest.skipUnless(POSIX, "growth is observed through /proc")
    def test_lease_records_are_charged_as_read_and_growth_blocks(self):
        policy = node.effective_policy({"root": str(self.root)})
        for index in range(3):
            install.atomic_json(self.root / "run" / f"lease-l{index}.json", {"lease": f"l{index}", "state": "stopped"})
        size = (self.root / "run" / "lease-l0.json").stat().st_size
        with patch.object(node, "LEASE_BYTES", size * 2 + 1):           # the third record exceeds what is left
            self.assertIn("could not be checked", node.guard_reason(policy))
        real = os.fstat

        def grow(fd):
            info = real(fd)
            target = os.readlink(f"/proc/self/fd/{fd}")
            if target.endswith("lease-l1.json"):
                with open(target, "ab") as stream:                     # grows between fstat and the read
                    stream.write(b" " * 64)
            return info
        with patch.object(node.os, "fstat", grow):
            self.assertIn("changed while it was read", node.guard_reason(policy))

    def test_marked_root_creates_nothing(self):
        (self.root / "log").rmdir()
        before = tree(self.root)
        with self.assertRaisesRegex(ValueError, "no log directory"):
            node.marked_root(str(self.root))
        self.assertEqual(tree(self.root), before)

    def test_holder_and_leases(self):
        policy = node.effective_policy({"root": str(self.root)})
        self.assertIsNone(node.guard_reason(policy))
        (self.lab / ".gpu-turn" / "holder").write_text("PAIR halo-1 123\n", encoding="utf-8")
        self.assertIn("GPU turn is held: PAIR", node.guard_reason(policy))
        (self.lab / ".gpu-turn" / "holder").write_text("", encoding="utf-8")
        self.assertIsNone(node.guard_reason(policy))
        install.atomic_json(self.root / "run" / "lease-l1.json", {"lease": "l1", "state": "acquired"})
        self.assertIn("lease is active: l1", node.guard_reason(policy))
        install.atomic_json(self.root / "run" / "lease-l1.json", {"lease": "l1", "state": "stopped"})
        self.assertIsNone(node.guard_reason(policy))
        install.atomic_json(self.root / "run" / "lease-l2.json", {"lease": "l2", "state": "stopped"})
        with patch.object(node, "LEASE_RECORDS", 1):
            self.assertIn("could not be checked", node.guard_reason(policy))
        (self.root / "run" / "lease-l3.json").write_text("{broken", encoding="utf-8")
        self.assertIn("could not be checked", node.guard_reason(policy))


class SettlementTests(LibraryCase):
    def record(self, **fields):
        install.atomic_json(self.root / "run" / "job-j1.json", {"job": "j1", "kind": "model-verify",
                                                                "created": time.time(), **fields})

    def test_every_non_terminal_state_is_settled(self):
        if not POSIX:
            self.skipTest("job locks need POSIX")
        for state in ("queued", "running", "paused"):
            with self.subTest(state):
                self.record(state=state, pid=2 ** 22 + 12345, start_time=1, boot_id=install.boot_id())
                self.assertEqual(node.run("copy-job", {"root": str(self.root), "job": "j1"})["state"], "failed")
        self.record(state="running", pid=os.getpid(), start_time=node.start_time(os.getpid()) + 1,
                    boot_id=install.boot_id())
        self.assertIn("ended", node.run("copy-job", {"root": str(self.root), "job": "j1"})["reason"])
        self.record(state="running", pid=os.getpid(), start_time=node.start_time(os.getpid()), boot_id="earlier")
        self.assertIn("rebooted", node.run("copy-job", {"root": str(self.root), "job": "j1"})["reason"])
        self.record(state="queued", created=time.time() - 120)
        self.assertIn("never started", node.run("copy-job", {"root": str(self.root), "job": "j1"})["reason"])
        self.record(state="running", pid=os.getpid(), start_time=node.start_time(os.getpid()),
                    boot_id=install.boot_id())
        self.assertEqual(node.run("copy-job", {"root": str(self.root), "job": "j1"})["state"], "running")
        self.record(state="succeeded")
        self.assertEqual(node.run("copy-cancel", {"root": str(self.root), "job": "j1"})["state"], "succeeded")
        install.atomic_json(self.root / "run" / "job-j2.json", {"job": "j2", "kind": "install", "state": "running"})
        self.assertIn("not a model library job", node.run("copy-job", {"root": str(self.root), "job": "j2"})["error"])


class DeadlineTests(LibraryCase):
    @unittest.skipUnless(POSIX, "job locks")
    def test_a_held_job_lock_cannot_stretch_a_verb_past_its_deadline(self):
        import fcntl
        install.atomic_json(self.root / "run" / "job-j1.json", {"job": "j1", "kind": "model-verify",
                                                                "state": "running", "pid": os.getpid(),
                                                                "created": time.time()})
        fd = os.open(self.root / "run" / "job-j1.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with patch.object(node, "DEADLINE_S", 1.0):
                started = time.monotonic()
                reply = node.run("copy-job", {"root": str(self.root), "job": "j1"})
                elapsed = time.monotonic() - started
        finally:
            os.close(fd)
        self.assertIn("lock is held", reply["error"])
        self.assertLess(elapsed, 3)


class DetachedStartTests(LibraryCase):
    def test_no_worker_starts_after_the_deadline_and_a_settled_job_does_nothing(self):
        spawned = []
        with patch.object(node, "spawn", lambda *a, **k: spawned.append(a) or {"job": "x", "pid": 1}), \
                patch.object(node, "request_check", side_effect=ValueError("the request ran past its 5 s deadline")):
            reply = node.run("model-verify", {"root": str(self.root), "model_root": str(self.models), "name": "alpha"})
        self.assertIn("deadline", reply["error"])
        self.assertEqual(spawned, [])
        if not POSIX:
            return
        install.atomic_json(self.root / "run" / "job-j9.json", {"job": "j9", "kind": "model-verify", "state": "failed",
                                                                "reason": "the request ran past its deadline"})
        spec_path = self.root / "run" / "job-j9.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "j9", "kind": "model-verify", "spec": {}})
        with patch.object(node, "verify_job", side_effect=AssertionError("work ran for a settled job")):
            self.assertEqual(node.job_main(str(spec_path)), 1)
        self.assertEqual(install.read_json(self.root / "run" / "job-j9.json")["state"], "failed")


@unittest.skipUnless(POSIX, "node jobs need POSIX")
class StartupAfterTheWorkerExistsTests(LibraryCase):
    def test_a_late_failure_after_creating_the_worker_still_returns_its_handle(self):
        class Child:
            pid = 4242

            def wait(self):
                return 0
        real = node.transition

        def transition(root, job, update):
            if update({}).get("pid"):                                      # the post-creation pid update
                raise ValueError("the job lock is held by another process")
            return real(root, job, update)
        for verb, payload in (("model-verify", {"root": str(self.root), "model_root": str(self.models),
                                                "name": "alpha"}),):
            with self.subTest(verb):
                with patch.object(node.subprocess, "Popen", lambda *a, **k: Child()), \
                        patch.object(node, "transition", transition):
                    reply = node.run(verb, payload)
                self.assertNotIn("error", reply, reply.get("error"))
                self.assertEqual(reply["pid"], 4242)
                record = install.read_json(self.root / "run" / ("job-" + reply["job"] + ".json"))
                self.assertEqual(record["state"], "queued")                 # the worker records itself on start


@unittest.skipUnless(POSIX, "node jobs need POSIX")
class StartupDeadlineTests(LibraryCase):
    def test_no_worker_is_dispatched_once_the_deadline_passed_while_records_were_written(self):
        real = install.atomic_json

        def slow(path, value):
            real(path, value)
            node._request.deadline = time.monotonic() - 1               # the writes took the rest of the deadline
        with patch.object(install, "atomic_json", slow), \
                patch.object(node.subprocess, "Popen", side_effect=AssertionError("dispatched after the deadline")):
            reply = node.run("model-verify", {"root": str(self.root), "model_root": str(self.models), "name": "alpha"})
        self.assertIn("deadline", reply["error"])
        records = list((self.root / "run").glob("job-*.json"))
        self.assertEqual([install.read_json(path)["state"] for path in records if ".spec." not in path.name],
                         ["failed"])
        self.assertEqual(list((self.root / "run").glob("job-*.spec.json")), [])


class VerifyTests(LibraryCase):
    def test_counts_and_failures(self):
        result = node.verify_unit(str(self.models), "alpha")
        self.assertEqual((result["ok"], result["counts"]["ok"], result["entries"]), (True, 3, 3))
        (self.models / "alpha" / "a.bin").write_bytes(b"Q" * 5000)
        (self.models / "alpha" / "c.json").unlink()
        result = node.verify_unit(str(self.models), "alpha")
        self.assertEqual((result["counts"]["mismatch"], result["counts"]["missing"]), (1, 1))
        self.assertEqual(sorted(f["path"] for f in result["failures"]), ["alpha/a.bin", "alpha/c.json"])
        self.assertEqual(node.verify_unit(str(self.models), "beta.gguf")["counts"]["ok"], 1)
        with self.assertRaisesRegex(ValueError, "no manifest entries"):
            node.verify_unit(str(self.models), "delta")

    def test_verification_checks_before_reading_and_for_every_entry(self):
        real, reads = node.Tree.read, []

        def read(tree, path, limit):
            reads.append(path)
            return real(tree, path, limit)

        def stop():
            raise node.Stopped("cancelled")
        with patch.object(node.Tree, "read", read), self.assertRaises(node.Stopped):
            node.verify_unit(str(self.models), "alpha", stop)
        self.assertEqual(reads, [])                                      # cancelled before anything was read
        calls = []
        with patch.object(node.Tree, "read", read):
            def stop_while_scanning():
                calls.append(True)
                if reads:                                                # the first manifest was read: stop now
                    raise node.Stopped("cancelled")
            with self.assertRaises(node.Stopped):
                node.verify_unit(str(self.models), "alpha", stop_while_scanning)
        self.assertEqual(len(reads), 1)                                  # serviced while the manifests are parsed
        (self.models / "empty").mkdir()
        (self.models / "empty" / "zero.bin").write_bytes(b"")
        (self.models / "empty.sha256").write_text(f"{digest(b'')}  empty/zero.bin\nf{'0' * 63}  empty/gone.bin\n",
                                                  encoding="utf-8")
        calls.clear()
        result = node.verify_unit(str(self.models), "empty", lambda: calls.append(True))
        self.assertEqual((result["counts"]["ok"], result["counts"]["missing"]), (1, 1))
        self.assertGreaterEqual(len(calls), 1 + 2 + 1)                   # entry, both entries, the empty file's hash

    @unittest.skipUnless(POSIX, "node jobs")
    def test_verify_job_pauses_for_a_turn_and_aborts_after_the_limit(self):
        with patch.object(node, "spawn", lambda root, job, kind, spec, record=None: (
                install.atomic_json(Path(root) / "run" / f"job-{job}.spec.json",
                                    {"root": str(root), "job": job, "kind": kind, "spec": spec}),
                install.atomic_json(Path(root) / "run" / f"job-{job}.json",
                                    {"job": job, "kind": kind, "state": "queued", "created": time.time(), **record}),
                {"job": job})[-1]):
            reply = node.run("model-verify", {"root": str(self.root), "model_root": str(self.models), "name": "alpha"})
        job = reply["job"]
        holder = self.lab / ".gpu-turn" / "holder"
        holder.write_text("SINGLE y 1\n", encoding="utf-8")
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] > 12:
                holder.write_text("", encoding="utf-8")
        spec_path = self.root / "run" / f"job-{job}.spec.json"
        code = node.job_main(str(spec_path), clock=lambda: clock[0], sleep=sleep)
        state = node.run("copy-job", {"root": str(self.root), "job": job})
        self.assertEqual(state["state"], "succeeded", state)
        self.assertTrue(state["result"]["ok"])
        self.assertGreater(state["paused"]["total_s"], 0)
        holder.write_text("PAIR z 1\n", encoding="utf-8")
        self.assertIn("GPU turn", node.run("model-verify", {"root": str(self.root), "model_root": str(self.models),
                                                            "name": "alpha"})["error"])


class DurabilityTests(LibraryCase):
    def test_a_file_changed_after_it_was_hashed_is_reported(self):
        (self.models / "blob.bin").write_bytes(b"z" * 10000)
        reader = node.open_protected(self.models, "blob.bin", 10000)
        try:
            reader.identity = (0, 0, 0, 0)
            with self.assertRaisesRegex(ValueError, "changed while it was read"):
                node.hash_reader(reader)
        finally:
            reader.close()
        info = os.stat(self.models / "blob.bin")
        with self.assertRaisesRegex(ValueError, "not the file that was planned"):
            node.open_protected(self.models, "blob.bin", 10000, [info.st_dev, info.st_ino + 1])


class ReaderTests(LibraryCase):
    def test_cache_free_reads_match(self):
        data = os.urandom(3 * 4096 + 123)
        (self.models / "blob.bin").write_bytes(data)
        for offset in (0, 1, 4095, 4096, 5000, len(data) - 1):
            reader = node.open_protected(self.models, "blob.bin", len(data))
            try:
                self.assertEqual(reader.read_at(offset), data[offset:])
            finally:
                reader.close()
        reader = node.open_protected(self.models, "blob.bin")
        try:
            self.assertEqual(node.hash_reader(reader), digest(data))
        finally:
            reader.close()
        with self.assertRaisesRegex(ValueError, "differs from the planned"):
            node.open_protected(self.models, "blob.bin", 1)

    @unittest.skipUnless(POSIX, "posix cache advice")
    def test_advice_path_drops_every_chunk_and_refuses_without_advice(self):
        data = b"q" * 20000
        (self.models / "blob.bin").write_bytes(data)
        calls = []
        with patch.object(node.os, "O_DIRECT", 0, create=True), \
                patch.object(node, "drop_cache", lambda fd, offset=0, length=0: calls.append((offset, length))):
            with patch.object(node, "open_in", wraps=node.open_in):
                directory = os.open(self.models, os.O_RDONLY)
                try:
                    with patch.object(node.os, "preadv", side_effect=OSError(22, "unsupported")):
                        reader = node.open_in(directory, "blob.bin", len(data))
                finally:
                    os.close(directory)
                try:
                    self.assertFalse(reader.direct)
                    self.assertEqual(node.hash_reader(reader), digest(data))
                finally:
                    reader.close()
        self.assertEqual(calls, [(0, 20000)])
        directory = os.open(self.models, os.O_RDONLY)
        try:
            with patch.object(node.os, "preadv", side_effect=OSError(22, "unsupported")), \
                    patch.object(node.os, "posix_fadvise", None, create=True):
                delattr_fadvise = patch.dict(node.os.__dict__)
                with delattr_fadvise:
                    del node.os.__dict__["posix_fadvise"]
                    with self.assertRaisesRegex(ValueError, "page cache"):
                        node.open_in(directory, "blob.bin")
        finally:
            os.close(directory)

    @unittest.skipUnless(os.name == "nt", "Windows unbuffered reads")
    def test_windows_unbuffered_reads_at_unaligned_offsets(self):
        data = os.urandom(9 * 1024 * 1024 + 4321)
        (self.models / "blob.bin").write_bytes(data)
        reader = node.open_protected(self.models, "blob.bin", len(data))
        try:
            self.assertIsNotNone(reader.handle)
            for offset in (0, 1, 511, 4097, 8 * 1024 * 1024 + 3, len(data) - 7):
                chunk = reader.read_at(offset)
                self.assertEqual(chunk, data[offset:offset + len(chunk)])
                self.assertTrue(chunk)
            self.assertEqual(node.hash_reader(reader), digest(data))
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
