"""The shared node gate: one call per machine, reentrant for its holder, shared through an OS lock."""

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from hyperloom import nodegate


class NodeGateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = Path(self.dir.name) / "cluster.json"

    def test_reentrant_for_the_holder_and_exclusive_for_others(self):
        release = nodegate.try_hold(self.config, "halo-1")
        self.assertIsNotNone(release)
        with nodegate.hold(self.config, "halo-1", wait=0.2):      # same thread: reentrant
            pass
        seen = []
        other = threading.Thread(target=lambda: seen.append(nodegate.try_hold(self.config, "halo-1")))
        other.start()
        other.join(5)
        self.assertEqual(seen, [None])
        with self.assertRaises(TimeoutError):
            done = threading.Event()

            def blocked():
                try:
                    with nodegate.hold(self.config, "halo-1", wait=0.2):
                        pass
                except TimeoutError as exc:
                    seen.append(exc)
                done.set()
            threading.Thread(target=blocked).start()
            done.wait(5)
            raise seen[-1]
        release()
        with nodegate.hold(self.config, "halo-1", wait=0.2):
            pass
        other_release = nodegate.try_hold(self.config, "halo-2")
        self.assertIsNotNone(other_release)
        other_release()

    def test_another_process_holding_the_gate_is_respected(self):
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from hyperloom import nodegate; "
                "r = nodegate.try_hold(sys.argv[2], 'halo-1'); print('held' if r else 'busy', flush=True); time.sleep(3)")
        child = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(self.config)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.assertIsNone(nodegate.try_hold(self.config, "halo-1"))
        child.wait(10)
        for _ in range(50):
            release = nodegate.try_hold(self.config, "halo-1")
            if release:
                break
            time.sleep(0.1)
        self.assertIsNotNone(release)
        release()

    def test_the_portal_creates_no_lock_file_while_answering(self):
        from unittest import mock
        with mock.patch.object(nodegate, "_create", True):
            nodegate.portal(self.config, ["halo-1", "../bad"])          # startup: the configured machines' files
            locks = Path(self.dir.name) / "node-locks"
            self.assertEqual(sorted(p.name for p in locks.iterdir()), ["halo-1.lock"])
            before = (locks / "halo-1.lock").read_bytes()
            with nodegate.hold(self.config, "halo-1", wait=0.2):      # an existing file: opened, never written
                pass
            self.assertEqual((locks / "halo-1.lock").read_bytes(), before)
            # A machine without a file (added by hand meanwhile): gated in this process only, nothing created.
            release = nodegate.try_hold(self.config, "halo-2")
            self.assertIsNotNone(release)
            self.assertIsNone(nodegate.try_hold(self.config, "halo-2"))
            release()
            with nodegate.hold(self.config, "halo-2", wait=0.2):
                pass
            self.assertEqual(sorted(p.name for p in locks.iterdir()), ["halo-1.lock"])
            nodegate.prepare(self.config, ["halo-2"])                 # a configuration save makes it
            self.assertTrue((locks / "halo-2.lock").is_file())
        self.assertTrue(nodegate._create)

    def test_machine_ids_are_checked(self):
        with self.assertRaises(ValueError):
            nodegate.gate_path(self.config, "../x")


if __name__ == "__main__":
    unittest.main()
