"""Model library, control side: roots, the RTX big-job ledger guard and the held verification."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hyperloom import downloads
from hyperloom.config import parse
from hyperloom.telemetry.store import Store
from tests.test_config import example


models = downloads.models_module()
install = models.install
POSIX = os.name == "posix"


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RootsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.cluster = parse(example())

    def test_config_and_prefs_roots(self):
        roots = downloads.library_roots(self.cluster)
        self.assertEqual(roots["halo-1"], [{"path": "/opt/assets", "source": "config"}])
        self.assertEqual(roots["workstation"], [{"path": "C:\\hyperloom\\assets", "source": "config"}])
        self.store.put("prefs", "library/roots", {"halo-1": ["/srv/models", "relative", "/opt/assets"],
                                                  "nobody": ["/x"], "halo-2": "not a list"})
        roots = downloads.library_roots(self.cluster, self.store)
        self.assertEqual([r["path"] for r in roots["halo-1"]], ["/opt/assets", "/srv/models"])
        self.assertEqual(roots["halo-1"][1]["source"], "prefs")

    def test_set_roots_validates_per_os(self):
        self.assertEqual(downloads.set_roots(self.cluster, self.store, "halo-1", ["/srv/models", "/srv/models"]),
                         {"machine": "halo-1", "roots": ["/srv/models"]})
        for machine, value in (("halo-1", "C:\\models"), ("halo-1", "/srv/../etc"), ("workstation", "/srv/models"),
                               ("workstation", "D:relative"), ("halo-1", "relative")):
            with self.subTest(value), self.assertRaises(ValueError):
                downloads.set_roots(self.cluster, self.store, machine, [value])
        with self.assertRaises(LookupError):
            downloads.set_roots(self.cluster, self.store, "nobody", [])
        downloads.set_roots(self.cluster, self.store, "workstation", ["D:\\models"])
        downloads.set_roots(self.cluster, self.store, "halo-1", [])
        self.assertEqual(self.store.get("prefs", "library/roots"), {"workstation": ["D:\\models"]})


class RecordedReplyTests(unittest.TestCase):
    def test_a_recorded_node_reply_passes_through_with_its_root_source(self):
        reply = json.loads((Path(__file__).resolve().parent / "fixtures" / "library" / "models-reply.json")
                           .read_text(encoding="utf-8"))
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.put("prefs", "library/roots", {"halo-2": ["/opt/lab/models"]})
        with patch.object(downloads, "_node", return_value=reply) as node:
            obj = example()
            obj["machines"][2]["lab_dir"] = "/srv/lab"
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
