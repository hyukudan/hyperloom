"""Model library, control side: roots, the RTX big-job ledger guard and the held verification."""
from tests.portable_fixtures import read_fixture
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import dataclasses
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hyperloom import downloads
from hyperloom.config import parse
from hyperloom.telemetry.store import Store
from tests.test_config import example


models = downloads.models_module()
install = models.install
POSIX = os.name == "posix"


def known_homes(case, cluster, home="/home/op"):
    """The nodes' homes as the portal's first node read learns them (no ssh in tests)."""
    from hyperloom import remote
    paths = {(m.id, m.ssh_target, m.ssh_port): home + "/.hyperloom-boot/hyperloom-node"
             for m in cluster.machines.values() if m.access == "ssh"}
    patcher = patch.dict(remote._NODE_PATHS, paths)
    patcher.start()
    case.addCleanup(patcher.stop)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RootsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.cluster = parse(example())
        known_homes(self, self.cluster)

    def test_a_save_learns_the_node_home_first_and_refuses_when_it_cannot(self):
        """An asset kept directly in a home anywhere (here /srv/accounts/researcher, not named after the ssh
        user) never authorizes that home: the save learns the node's home first, or is refused."""
        from hyperloom import remote
        halo = self.cluster.machines["halo-1"]
        self.cluster.assets["halo-1"]["odd"] = {"path": "/srv/accounts/researcher/odd.gguf", "bytes": 1}
        client = SimpleNamespace(_tool_path=lambda timeout, cancel: remote._NODE_PATHS.__setitem__(
            (halo.id, halo.ssh_target, halo.ssh_port), "/srv/accounts/researcher/.hyperloom-boot/hyperloom-node"))
        with patch.dict(remote._NODE_PATHS, clear=True),                 patch("hyperloom.orchestrate._client", return_value=client) as made:
            with self.assertRaises(ValueError):
                downloads.set_roots(self.cluster, self.store, "halo-1", ["/srv/accounts/researcher/runs"])
            made.assert_called_once_with(halo)
            downloads.set_roots(self.cluster, self.store, "halo-1", ["/opt/assets/x"])     # learned once
            self.assertEqual(made.call_count, 1)
        with patch.dict(remote._NODE_PATHS, clear=True),                 patch("hyperloom.orchestrate._client", side_effect=remote.RemoteError("unknown host key")):
            with self.assertRaisesRegex(ValueError, "home could not be checked"):
                downloads.set_roots(self.cluster, self.store, "halo-1", ["/opt/assets/x"])
            # Before the home is known no asset directory is a base: a saved root inside one is not used.
            self.store.put("prefs", "library/roots", {"halo-1": ["/srv/accounts/researcher/private"]})
            self.assertEqual(downloads.configured_bases(self.cluster, halo), [halo.install_root])
            self.assertNotIn("/srv/accounts/researcher/private",
                             [r["path"] for r in downloads.library_roots(self.cluster, self.store)["halo-1"]])
        # Another ssh account under the same machine id is another identity: its home is learned again.
        moved = dataclasses.replace(halo, ssh_target="other@halo-1")
        with patch.dict(remote._NODE_PATHS, {(halo.id, halo.ssh_target, halo.ssh_port): "/opt/x/.hyperloom-boot/n"}):
            self.assertIsNone(remote.known_home(moved))
            self.assertEqual(remote.known_home(halo), "/opt/x")

    def test_config_and_prefs_roots(self):
        roots = downloads.library_roots(self.cluster)
        self.assertEqual(roots["halo-1"], [{"path": "/opt/assets", "source": "config"}])
        self.assertEqual(roots["workstation"], [{"path": "C:\\hyperloom\\assets", "source": "config"}])
        self.store.put("prefs", "library/roots", {"halo-1": ["/opt/assets/extra", "relative", "/opt/assets",
                                                             "/srv/models", "/opt/assets/.cache"],
                                                  "nobody": ["/x"], "halo-2": "not a list"})
        roots = downloads.library_roots(self.cluster, self.store)
        # A root saved elsewhere (an earlier release) or in a hidden directory is never used.
        self.assertEqual([r["path"] for r in roots["halo-1"]], ["/opt/assets", "/opt/assets/extra"])
        self.assertEqual(roots["halo-1"][1]["source"], "prefs")

    def test_an_asset_directly_in_a_home_never_opens_the_home(self):
        # The parent of an asset kept directly in a home directory is that home: never a root nor a base for one.
        self.cluster.assets["halo-1"]["loose"] = {"path": "/home/someone/loose.gguf", "bytes": 1,
                                                  "control_path": "C:\\Users\\someone\\loose.gguf"}
        self.cluster.assets["halo-1"]["top"] = {"path": "/top.gguf", "bytes": 1}
        roots = downloads.library_roots(self.cluster)
        self.assertEqual(roots["halo-1"], [{"path": "/opt/assets", "source": "config"}])
        self.assertEqual(roots["workstation"], [{"path": "C:\\hyperloom\\assets", "source": "config"}])
        for machine, value in (("halo-1", "/home/someone"), ("halo-1", "/home/someone/models"), ("halo-1", "/top"),
                               ("workstation", "C:\\Users\\someone\\Documents")):
            with self.subTest(value), self.assertRaises(ValueError):
                downloads.set_roots(self.cluster, self.store, machine, [value])
        # A node's home elsewhere: named after its ssh account, or learned from the node tool's path.
        from hyperloom import remote
        import dataclasses
        halo = dataclasses.replace(self.cluster.machines["halo-1"], ssh_target="op@halo-1")
        self.assertTrue(downloads.too_wide(halo, "/srv/accounts/op"))
        self.assertFalse(downloads.too_wide(halo, "/srv/accounts/op/models"))
        halo = self.cluster.machines["halo-1"]
        with patch.dict(remote._NODE_PATHS, {(halo.id, halo.ssh_target, halo.ssh_port):
                                             "/data/people/op/.hyperloom-boot/hyperloom-node"}):
            for value in ("/data/people/op", "/data/people"):
                self.assertTrue(downloads.too_wide(halo, value), value)
            self.assertFalse(downloads.too_wide(halo, "/data/people/op/models"))
            self.cluster.assets["halo-1"]["odd"] = {"path": "/data/people/op/odd.gguf", "bytes": 1}
            self.assertNotIn("/data/people/op", [r["path"] for r in downloads.library_roots(self.cluster)["halo-1"]])
        # A model directory inside a home is still a root.
        self.cluster.assets["halo-1"]["kept"] = {"path": "/home/someone/models/kept.gguf", "bytes": 1}
        self.assertIn({"path": "/home/someone/models", "source": "config"},
                      downloads.library_roots(self.cluster)["halo-1"])
        for machine, value, wide in (("halo-1", "/", True), ("halo-1", "/home", True), ("halo-1", "/root", True),
                                     ("halo-1", "/root/models", False), ("halo-1", "/var/home/someone", True),
                                     ("workstation", "D:\\", True), ("workstation", "C:\\Users", True),
                                     ("workstation", "C:\\Users\\someone\\models", False)):
            with self.subTest(value=value):
                self.assertEqual(downloads.too_wide(self.cluster.machines[machine], value), wide)

    def test_set_roots_validates_per_os(self):
        self.assertEqual(downloads.set_roots(self.cluster, self.store, "halo-1", ["/opt/assets/a", "/opt/assets/a"]),
                         {"machine": "halo-1", "roots": ["/opt/assets/a"]})
        for machine, value in (("halo-1", "C:\\models"), ("halo-1", "/srv/../etc"), ("workstation", "/srv/models"),
                               ("workstation", "D:relative"), ("halo-1", "relative"),
                               ("halo-1", "/srv/models"), ("halo-1", "/home/someone"), ("halo-1", "/opt/assets/.ssh"),
                               ("workstation", "D:\\models")):     # only inside a configured root, never hidden
            with self.subTest(value), self.assertRaises(ValueError):
                downloads.set_roots(self.cluster, self.store, machine, [value])
        with self.assertRaises(LookupError):
            downloads.set_roots(self.cluster, self.store, "nobody", [])
        downloads.set_roots(self.cluster, self.store, "workstation", ["C:\\hyperloom\\assets\\more"])
        downloads.set_roots(self.cluster, self.store, "halo-1", [])
        self.assertEqual(self.store.get("prefs", "library/roots"), {"workstation": ["C:\\hyperloom\\assets\\more"]})


class RecordedReplyTests(unittest.TestCase):
    def test_a_recorded_node_reply_passes_through_with_its_root_source(self):
        reply = json.loads(read_fixture(Path(__file__).resolve().parent / "fixtures" / "library" / "models-reply.json"))
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.put("prefs", "library/roots", {"halo-2": ["/opt/lab/models"]})
        with patch.object(downloads, "_node", return_value=reply) as node:
            obj = example()
            obj["machines"][2]["lab_dir"] = "/srv/lab"
            obj["machines"][2]["install_root"] = "/opt/lab"          # the extra root lies inside it
            inventory = downloads.inventory(parse(obj), "halo-2", store, now=10)
        self.assertEqual(node.call_args.args[2]["roots"], ["/opt/assets", "/opt/lab/models"])
        root = inventory["roots"][0]
        self.assertEqual((root["source"], root["truncated"], inventory["scanned_at"]), ("prefs", ["depth"], 10))
        statuses = {unit["manifest"]["status"] for unit in root["units"]}
        self.assertEqual(statuses, {"unknown", "none", "complete"})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.ledger = self.dir / "coordination.md"
        self.ledger.write_text("- 01:00 someone: PC-BIG start (a job)\n- 01:10 someone: PC-BIG end (rc 0)\n",
                               encoding="utf-8")
        self.config = self.dir / "config"
        self.config.mkdir()

    def guard(self):
        return downloads.LedgerGuard(self.config, self.ledger)

    def append(self, text):
        with self.ledger.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)

    def test_not_configured_and_no_baseline(self):
        self.assertIsNone(downloads.LedgerGuard(self.config, None).check())
        before = sorted(os.listdir(self.config))
        self.assertIn("no accepted baseline", self.guard().check())
        self.assertEqual(sorted(os.listdir(self.config)), before)          # a first-use check creates nothing

    def test_open_start_blocks_and_end_clears(self):
        self.guard().accept()
        self.assertIsNone(self.guard().check())
        self.append("- 02:00 other: PC-BIG start (big run, tope 1500 s)\n")
        self.assertIn("open in the ledger (other)", self.guard().check())
        self.append("- 02:20 other: PC-BIG end (rc 0)\n")
        self.assertIsNone(self.guard().check())
        self.append("- 02:30 other: PC-BIG sta")
        self.assertIn("line being written", self.guard().check())

    def test_observed_start_survives_truncation_and_rotation(self):
        self.guard().accept()
        accepted = self.ledger.read_bytes()
        self.append("- 03:00 other: PC-BIG start (big run)\n")
        running = self.guard()
        self.assertIn("open in the ledger", running.check())               # observed: the checkpoint advanced
        self.ledger.write_bytes(accepted)                                  # truncated back to the accepted prefix
        self.assertIn("rewritten", running.check())
        self.assertIn("rewritten", self.guard().check())                   # a fresh process too
        os.replace(self.ledger, self.dir / "old.md")
        self.ledger.write_bytes(accepted)                                  # rotated to a new file
        self.assertIn("rewritten", self.guard().check())
        self.guard().accept()                                              # the operator reviews and accepts
        self.assertIsNone(self.guard().check())

    def test_a_rotation_between_lookup_and_open_is_seen(self):
        self.guard().accept()
        old = self.dir / "old.md"
        os.replace(self.ledger, old)
        self.ledger.write_bytes(old.read_bytes())                         # an idle replacement, new identity
        stale = os.stat(old)
        with patch.object(downloads._os, "stat", lambda path, *a, **k: stale):   # a lookup made before the rotation
            self.assertIn("rewritten", self.guard().check())

    def test_an_observed_incomplete_line_survives_truncation(self):
        self.guard().accept()
        accepted = self.ledger.read_bytes()
        self.append("- 12:00 active: PC-BIG start")                    # no newline yet
        running = self.guard()
        self.assertIn("line being written", running.check())
        self.ledger.write_bytes(accepted)
        self.assertIn("rewritten", running.check())
        self.assertIn("rewritten", self.guard().check())
        self.guard().accept()
        self.append("- 12:00 active: PC-BIG start")
        self.assertIn("line being written", self.guard().check())
        self.append(" (run)\n")                                         # completing the line keeps the prefix
        self.assertIn("open in the ledger (active)", self.guard().check())

    def test_interior_and_same_size_rewrites_block(self):
        self.guard().accept()
        self.append("- 04:00 note: nothing\n" * 50)
        self.assertIsNone(self.guard().check())
        data = bytearray(self.ledger.read_bytes())
        data[30:35] = b"XXXXX"
        self.ledger.write_bytes(bytes(data) + b"- 05:00 note: appended\n")
        self.assertIn("rewritten", self.guard().check())
        self.guard().accept()
        data = bytearray(self.ledger.read_bytes())
        data[100] = ord("Y")
        self.ledger.write_bytes(bytes(data))
        self.assertIn("rewritten", self.guard().check())

    def test_checkpoint_is_synced_before_a_durable_replace(self):
        calls = []
        real_fsync = os.fsync

        def fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)
        if os.name == "nt":
            real = models.windows_replace
            replace = patch.object(models, "windows_replace", lambda a, b: (calls.append("replace"), real(a, b)))
        else:
            real = os.replace
            replace = patch.object(downloads._os, "replace", lambda a, b: (calls.append("replace"), real(a, b)))
        with patch.object(downloads._os, "fsync", fsync), replace:
            self.guard().accept()
        self.assertEqual(calls[:2], ["fsync", "replace"])
        if os.name == "posix":
            self.assertEqual(calls, ["fsync", "replace", "fsync"])            # the directory after the rename
        self.assertIsNone(self.guard().check())

    def test_concurrent_guards_never_move_the_checkpoint_back(self):
        self.guard().accept()
        first, second = self.guard(), self.guard()
        self.append("- 06:00 note: one\n")
        self.assertIsNone(first.check())
        size = json.loads((self.config / "library-ledger.json").read_text())["size"]
        self.assertIsNone(second.check())
        self.assertEqual(json.loads((self.config / "library-ledger.json").read_text())["size"], size)

    def test_lock_held_elsewhere_is_bounded(self):
        self.guard().accept()
        lock = self.config / "library-ledger.lock"
        holder = subprocess.Popen([sys.executable, "-c", (
            "import os, sys, time\n"
            "f = open(sys.argv[1], 'r+b')\n"
            "if os.name == 'nt':\n"
            "    import msvcrt; msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "else:\n"
            "    import fcntl; fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n"), str(lock)], stdout=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"held")
            started = time.monotonic()
            self.assertIn("in time", self.guard().check())
            self.assertLess(time.monotonic() - started, 5)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertIsNone(self.guard().check())

class HeldVerificationTests(unittest.TestCase):
    def test_verification_is_held_on_every_machine(self):
        """Nothing is started, read or called: no node verb, no control-machine hash (held like the copies)."""
        self.assertFalse(any(hasattr(downloads, name) for name in ("verify_start", "watch_job", "library_job",
                                                                   "LedgerMonitor")))
        self.assertEqual(downloads.VERIFY_HELD, "model verification is not available in this release")


if __name__ == "__main__":
    unittest.main()
