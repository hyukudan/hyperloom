"""Node verb usb4: read-only USB4 link telemetry from a recorded node tree (sysfs, /proc, layout, journal)."""

import importlib.util
import io
import json
import os
import shutil
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/usb4/tree-halo-2.json").read_text(encoding="utf-8"))
spec = importlib.util.spec_from_file_location("hyperloom_usb4_under_test", ROOT / "node" / "hyperloom_usb4.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
BOOT = "000000000000000000000000000000b1"


class Child:
    """Stands in for the journalctl process: merged output, an exit code, optional stall."""

    def __init__(self, lines, code=0, stall=0.0):
        self.data = b"".join((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n" for line in lines)
        self.code, self.stall, self.killed = code, stall, False
        self.stdout = self

    def read(self, size):
        if self.stall:
            end = time.monotonic() + self.stall
            while time.monotonic() < end and not self.killed:
                time.sleep(0.01)
            self.stall = 0.0
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def close(self):
        pass

    def wait(self, timeout=None):
        return -9 if self.killed else self.code

    def kill(self):
        self.killed = True


class Runner:
    def __init__(self, *children):
        self.children, self.calls = list(children), []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.STDOUT
        return self.children.pop(0)


class TreeReader(node.Reader):
    """The recorded tree under a temporary root; links, modes and character devices come from the fixture so the
    tests do not depend on symlink privileges of the host."""

    def __init__(self, root, tree, **kwargs):
        super().__init__(root, **kwargs)
        self.tree = tree

    def parent_name(self, logical):
        target = self.tree["links"].get(logical)
        if target:
            return PurePosixPath(target).parent.name
        return super().parent_name(logical)

    def link(self, logical):
        target = self.tree["links"].get(logical)
        if target:
            return "link", PurePosixPath(target).name
        state, value = self.probe(logical)
        return ("absent", None) if state == "absent" else super().link(logical)

    def device(self, logical):
        if logical in self.tree["char_devices"]:
            return "char", os.stat_result((stat.S_IFCHR | 0o660, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return super().device(logical)

    def probe(self, logical):
        state, result = super().probe(logical)
        if state == "present" and logical in self.tree["modes"]:
            values = list(result)
            values[0] = stat.S_IFREG | int(self.tree["modes"][logical], 8)
            return state, os.stat_result(values)
        return state, result


def build(directory, tree):
    for logical, content in tree["files"].items():
        if os.name == "nt" and ":" in logical:
            continue                        # retimer entries (N-0:P.I) cannot be file names on Windows
        path = Path(directory).joinpath(*PurePosixPath(logical).parts[1:])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8", newline="\n")
    for logical in tree["links"]:
        Path(directory).joinpath(*PurePosixPath(logical).parts[1:]).mkdir(parents=True, exist_ok=True)


def boot_entries(first_mono_s=2):
    """The recorded journal entries with the first one moved to the start of the boot."""
    entries = [dict(e) for e in FIXTURE["journal"]]
    entries[0]["__MONOTONIC_TIMESTAMP"] = str(first_mono_s * 1000000)
    entries[0]["_SOURCE_MONOTONIC_TIMESTAMP"] = "0"
    return entries


class NodeVerbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        build(self.dir.name, FIXTURE)

    def collect(self, payload=None, runner=None, deadline_s=5.0, tree=FIXTURE):
        reader = TreeReader(self.dir.name, tree, runner=runner or Runner(), clock=lambda: 1_800_000_000.0)
        return node.Collector(reader, deadline_s=deadline_s).collect(node.validate(payload or {}))

    def test_links_layout_parameters_and_interrupts_from_the_recorded_node(self):
        guarded = []
        real_open = Path.open

        def watch(path, *args, **kwargs):
            text = str(path).replace("\\", "/")
            guarded.append(text)
            if "/dev/tbstream" in text or "/sys/kernel/config" in text or text.endswith("gpu_reap_orphans"):
                raise AssertionError("opened a forbidden path: " + text)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", watch):
            report = self.collect()
        self.assertEqual((report["schema"], report["verb"], report["boot_id"]), ("hyperloom/node-v1", "usb4", BOOT))
        self.assertEqual(report["domains"], {"0": "0000:c7:00.5", "1": "0000:c7:00.6"})
        links = {link["router"]: link for link in report["links"]}
        self.assertEqual(sorted(links), ["0-2", "1-2"])
        self.assertEqual((links["0-2"]["rx"]["gbps"], links["0-2"]["full_rate"], links["0-2"]["below_full_rate"]),
                         (20.0, False, ["rx", "tx"]))
        self.assertEqual((links["1-2"]["tx"]["gbps"], links["1-2"]["full_rate"], links["1-2"]["peer"]), (40.0, True, "halo-1"))
        layout = report["layout"]
        self.assertEqual(layout["status"], "ok")
        stream = {row["id"]: row for row in layout["streams"]}["0-2.0/data0"]
        self.assertEqual(stream["device"], "/dev/tbstream0")
        self.assertEqual(stream["published"]["openable"], True)
        self.assertEqual(stream["live"], {"device_present": True, "driver": "thunderbolt_stream", "attached_now": True})
        self.assertNotIn("gpu_timing", stream["config"])                # whitelisted scalars only
        self.assertEqual(stream["config"]["ring_size"], 1024)
        params = report["parameters"]["modules"]["thunderbolt_stream"]
        self.assertEqual(params["write_only"], ["gpu_reap_orphans"])
        self.assertEqual(params["values"]["gpu_poll"], "N")
        vectors = report["interrupts"]["vectors"]
        self.assertEqual([(v["vector"], v["ring"]) for v in vectors if v["nhi"] == "0000:c7:00.5"],
                         [(0, "control"), (1, "control"), (2, "data"), (3, "data"), (4, "data"), (5, "data")])
        self.assertTrue(all(v["count"] > 0 and v["domain"] in (0, 1) for v in vectors))
        self.assertEqual(report["journal"]["status"], "skipped")
        self.assertFalse(any("/dev/" in path for path in guarded))

    def test_initial_scan_is_bound_to_the_boot_and_states_its_coverage(self):
        runner = Runner(Child(FIXTURE["journal"]))
        journal = self.collect({"journal": {}}, runner)["journal"]
        argv = runner.calls[0]
        self.assertIn("--boot=" + BOOT, argv)
        self.assertEqual(argv[-2:], ["-n", str(node.JOURNAL_INITIAL)])
        self.assertEqual((journal["status"], journal["mode"], journal["complete"]), ("ok", "initial", False))
        self.assertIn("not the first message", journal["incomplete_why"])   # the recorded window starts late
        self.assertEqual(journal["cursor"], FIXTURE["journal"][-1]["__CURSOR"])
        self.assertEqual(sum(journal["counts"].values()), journal["matched"])
        self.assertEqual(set(journal["counts"]), set(node.KINDS))
        complete = self.collect({"journal": {}}, Runner(Child(boot_entries())))["journal"]
        self.assertTrue(complete["complete"])
        self.assertIsNone(complete["incomplete_why"])

    def test_follow_continues_only_from_the_exact_stored_entry(self):
        entries = boot_entries()
        cursor = entries[10]["__CURSOR"]
        runner = Runner(Child(entries[10:]))
        journal = self.collect({"journal": {"boot_id": BOOT, "cursor": cursor}}, runner)["journal"]
        self.assertIn("--cursor=" + cursor, runner.calls[0])
        self.assertEqual((journal["mode"], journal["entries"], journal["complete"]), ("follow", len(entries) - 11, True))
        expected = sum(1 for e in entries[11:] if node.classify(e["MESSAGE"]))
        self.assertEqual(journal["matched"], expected)
        # A silently repositioned cursor (the stored entry was vacuumed) is a rescan, never a continuation.
        runner = Runner(Child(entries[11:]), Child(entries))
        journal = self.collect({"journal": {"boot_id": BOOT, "cursor": cursor}}, runner)["journal"]
        self.assertEqual((journal["mode"], journal["entries"]), ("cursor_lost", len(entries)))
        self.assertNotIn("--cursor=" + cursor, runner.calls[1])
        # A rejected cursor as well.
        runner = Runner(Child([b"Failed to seek to cursor: Invalid argument"], code=1), Child(entries))
        self.assertEqual(self.collect({"journal": {"boot_id": BOOT, "cursor": cursor}}, runner)["journal"]["mode"], "cursor_lost")
        # A cursor of another boot starts this boot from scratch.
        other = cursor.replace("b=" + BOOT, "b=" + "c" * 32)
        runner = Runner(Child(entries))
        self.assertEqual(self.collect({"journal": {"boot_id": "c" * 32, "cursor": other}}, runner)["journal"]["mode"], "initial")
        self.assertFalse(any(arg.startswith("--cursor") for arg in runner.calls[0]))

    def test_unreadable_foreign_and_malformed_journals(self):
        entries = boot_entries()
        hint = b"Hint: You are currently not seeing messages from other users and the system."
        journal = self.collect({"journal": {}}, Runner(Child([hint])))["journal"]
        self.assertEqual(journal["status"], "unavailable")
        self.assertIn("not readable", journal["why"])
        journal = self.collect({"journal": {}}, Runner(Child([])))["journal"]
        self.assertIn("no kernel entries", journal["why"])
        foreign = dict(entries[5], __CURSOR=entries[5]["__CURSOR"].replace("b=" + BOOT, "b=" + "d" * 32))
        journal = self.collect({"journal": {}}, Runner(Child(entries[:5] + [foreign])))["journal"]
        self.assertIn("another boot", journal["why"])
        journal = self.collect({"journal": {}}, Runner(Child(entries + [b"{not json"])))["journal"]
        self.assertEqual((journal["status"], journal["malformed"], journal["complete"]), ("ok", 1, False))
        self.assertIn("malformed", journal["incomplete_why"])
        with mock.patch.object(node, "JOURNAL_TIMEOUT_S", 0.3):
            stalled = Child(entries, stall=5)
            journal = self.collect({"journal": {}}, Runner(stalled))["journal"]
        self.assertIn("did not answer", journal["why"])
        self.assertTrue(stalled.killed)

    def test_journal_diagnostics_and_entries_without_a_message_make_totals_incomplete(self):
        entries = boot_entries()
        no_message = {k: v for k, v in entries[3].items() if k != "MESSAGE"}
        journal = self.collect({"journal": {}}, Runner(Child(entries[:3] + [no_message] + entries[4:])))["journal"]
        self.assertEqual((journal["malformed"], journal["complete"]), (1, False))
        warning = b"Journal file /var/log/journal/x/system@1.journal is truncated, ignoring file."
        journal = self.collect({"journal": {}}, Runner(Child([warning] + entries)))["journal"]
        self.assertEqual((journal["status"], journal["complete"]), ("ok", False))
        self.assertIn("journalctl reported: Journal file", journal["incomplete_why"])

    def test_live_checks_tell_absence_from_failed_inspection(self):
        tree = json.loads(json.dumps(FIXTURE))
        tree["char_devices"] = tree["char_devices"][1:]                  # tbstream0 gone
        del tree["links"]["/sys/bus/thunderbolt/devices/1-2.0/driver"]   # 1-2.0 has no driver bound
        (Path(self.dir.name) / "sys/bus/thunderbolt/devices/1-2.0/driver").rmdir()
        reader = TreeReader(self.dir.name, tree, runner=Runner())
        failing = reader.probe

        def probe(logical):
            if logical == "/sys/bus/thunderbolt/devices/0-2.0":
                return "error", "Permission denied"
            return failing(logical)
        reader.probe = probe
        report = node.Collector(reader).collect({})
        live = {row["id"]: row["live"] for row in report["layout"]["streams"]}
        self.assertEqual(live["0-2.0/data0"]["device_present"], False)                 # observed absence
        self.assertIsNone(live["0-2.0/data0"]["attached_now"])                        # failed inspection
        self.assertIn("Permission denied", live["0-2.0/data0"]["attached_why"])
        self.assertEqual((live["1-2.0/data0"]["attached_now"], live["1-2.0/data0"]["attached_why"]),
                         (False, "no driver bound to the service"))
        self.assertEqual(report["layout"]["live"]["status"], "unavailable")
        self.assertIn("2 live checks failed", report["layout"]["live"]["why"])

    def test_a_failed_listing_is_unavailable_not_an_empty_observation(self):
        reader = TreeReader(self.dir.name, FIXTURE, runner=Runner())
        original = reader.listdir
        reader.listdir = lambda logical: None if logical == "/sys/bus/thunderbolt/devices" else original(logical)
        report = node.Collector(reader).collect({})
        self.assertEqual(report["links"]["status"], "unavailable")
        self.assertIn("cannot list", report["links"]["why"])
        reader = TreeReader(self.dir.name, FIXTURE, runner=Runner())
        reader.listdir = lambda logical: None if logical.endswith("/parameters") else original(logical)
        params = node.Collector(reader).collect({})["parameters"]
        self.assertEqual(params["status"], "unavailable")
        self.assertIn("cannot list its parameters", params["why"])
        reader = TreeReader(self.dir.name, FIXTURE, runner=Runner())
        reading = reader.read
        reader.read = lambda logical, limit=node.SMALL: None if logical.endswith("0-2/rx_speed") or logical.endswith("0-2/tx_speed") else reading(logical, limit)
        report = node.Collector(reader).collect({})
        self.assertIn("0-2: unreadable rx_speed, tx_speed", report["why"]["links_failed"])
        self.assertIsNone({l["router"]: l for l in report["links"]}["0-2"]["full_rate"])
        missing = node.Reader(self.dir.name).listdir("/sys/bus/thunderbolt/nothing-here")
        self.assertEqual(missing, [])

    def test_stream_devices_are_only_ever_lstat(self):
        reader = node.Reader(self.dir.name)
        self.assertEqual(reader.device("/dev/tbstream0")[0], "absent")
        self.assertEqual(reader.device("/dev/sda")[0], "error")
        self.assertEqual(reader.device("/dev/tbstream0/../sda")[0], "error")
        self.assertIsNone(reader.read("/dev/tbstream0"))
        self.assertEqual(reader.probe("/dev/tbstream0")[0], "error")
        device = Path(self.dir.name) / "dev"
        device.mkdir()
        (device / "tbstream0").write_text("not a device", encoding="utf-8")
        self.assertEqual(reader.device("/dev/tbstream0")[0], "other")
        with mock.patch.object(os, "open", side_effect=AssertionError("a device was opened")):
            reader.device("/dev/tbstream0")

    def test_overall_deadline_returns_what_finished(self):
        slow = TreeReader(self.dir.name, FIXTURE, runner=Runner())
        original = slow.read

        def read(logical, limit=node.SMALL):
            if logical == "/proc/interrupts":
                time.sleep(3)
            return original(logical, limit)
        slow.read = read
        started = time.monotonic()
        report = node.Collector(slow, deadline_s=0.5).collect({})
        self.assertLess(time.monotonic() - started, 2.8)
        self.assertIn("deadline", report["interrupts"]["why"] + " ".join(report["why"]))
        self.assertEqual(report["interrupts"]["status"], "unavailable")
        self.assertIsInstance(report["links"], list)

    def test_payload_validation_and_output_cap(self):
        for bad in ({"root": "/"}, {"journal": {"cursor": "x\ny"}}, {"journal": {"boot_id": "nothex"}},
                    {"journal": {"cursor": 5}}, {"journal": {"other": 1}}, {"journal": []}):
            with self.assertRaises(ValueError, msg=bad):
                node.validate(bad)
        self.assertEqual(node.validate({"journal": {}}), {"journal": {"boot_id": None, "cursor": None}})
        big = {"schema": "hyperloom/node-v1", "verb": "usb4", "links": [],
               "journal": {"recent": [{"message": "x" * 300}] * 4000}}
        capped = node.cap(big)
        self.assertTrue(capped["truncated"])
        self.assertLessEqual(len(json.dumps(capped)), node.MAX_OUTPUT)

    def test_paths_outside_the_sources_are_refused(self):
        reader = node.Reader(self.dir.name)
        for bad in ("/sys/kernel/config/thunderbolt/stream/0-2.0/data0/gpu_timing", "/etc/passwd",
                    "/sys/bus/thunderbolt/../../etc/passwd", "relative/path", "/sys/kernel/debug/thunderbolt"):
            self.assertIsNone(reader.read(bad), bad)
        self.assertIsNotNone(reader.read("/proc/interrupts", limit=node.MAX_FILE))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "symlinks need a POSIX host")
    def test_symlinks_escaping_the_sources_are_refused(self):
        secret = Path(self.dir.name) / "elsewhere"
        secret.write_text("secret\n", encoding="utf-8")
        layout = Path(self.dir.name) / "run" / "usb4stream-layout.json"
        layout.unlink()
        layout.symlink_to(secret)
        domain = Path(self.dir.name) / "sys/bus/thunderbolt/devices/domain0"
        shutil.rmtree(domain)
        target = Path(self.dir.name) / "sys/devices/pci0000:00/0000:c7:00.5/domain0"
        target.mkdir(parents=True)
        domain.symlink_to(target)
        reader = node.Reader(self.dir.name)
        self.assertIsNone(reader.read("/run/usb4stream-layout.json"))
        attribute = Path(self.dir.name) / "sys/bus/thunderbolt/devices/0-2/rx_speed"
        attribute.unlink()
        (Path(self.dir.name) / "dev").mkdir()
        (Path(self.dir.name) / "dev/tbstream0").write_text("20.0 Gb/s", encoding="utf-8")
        attribute.symlink_to(Path(self.dir.name) / "dev/tbstream0")
        self.assertIsNone(reader.read("/sys/bus/thunderbolt/devices/0-2/rx_speed"))
        self.assertEqual(reader.parent_name("/sys/bus/thunderbolt/devices/domain0"), "0000:c7:00.5")
        report = node.Collector(reader).collect({})
        self.assertEqual(report["layout"]["status"], "unavailable")
        self.assertEqual(report["domains"]["0"], "0000:c7:00.5")

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "symlinks need a POSIX host")
    def test_a_symlinked_device_directory_is_refused(self):
        outside = Path(self.dir.name).parent / (Path(self.dir.name).name + "-outside")
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "tbstream0").write_text("x", encoding="utf-8")
        (Path(self.dir.name) / "dev").symlink_to(outside)
        state, why = node.Reader(self.dir.name).device("/dev/tbstream0")
        self.assertEqual(state, "error")
        self.assertIn("outside /dev", why)

    def test_classification_of_kernel_lines(self):
        cases = {
            "thunderbolt domain0: host interface reset after DMA teardown (attempt 1)": "host_reset",
            "thunderbolt domain1: host interface reset after DMA teardown pending: NHI in use": "reset_pending",
            "tbstream: tbstream0: CLOSE not sent (-105): the peer sees no EOF": "close_not_sent",
            "thunderbolt 0000:c7:00.6: 0:1: hop deactivation failed for hop 0, index 2": "hop_deactivation_failed",
            "tbstream: GPU context still quarantined after rmdir": "quarantine",
            "thunderbolt 0-2: new host found, vendor=0x1d6b device=0x4": None,
            "nvme0: host interface reset": None,
        }
        for message, kind in cases.items():
            self.assertEqual(node.classify(message), kind, message)
        self.assertEqual(node.normal_boot("488B1F63-40c1-414e-96c2-a56c417a7397"), "488b1f6340c1414e96c2a56c417a7397")
        self.assertTrue(node.same_entry("s=a;i=1;b=" + BOOT + ";m=1", "s=a;i=1;b=" + BOOT + ";x=2"))
        self.assertFalse(node.same_entry("s=a;i=1;b=" + BOOT, "s=a;i=2;b=" + BOOT))

    def test_node_tool_dispatches_the_verb_and_refuses_bad_payloads(self):
        result = subprocess.run([sys.executable, str(ROOT / "node" / "hyperloom-node"), "usb4"],
                                input=json.dumps({"bogus": 1}), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("payload accepts only journal", json.loads(result.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
