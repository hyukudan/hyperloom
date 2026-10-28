"""`hyperloom usb4`: the same node verb and summary as the portal page, printed as text or JSON."""

import contextlib
import copy
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from hyperloom import cli, config
from hyperloom.cliext import usb4 as command
from hyperloom.web.features import usb4

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {name: json.loads((ROOT / f"tests/fixtures/usb4/node-{name}.json").read_text(encoding="utf-8"))
           for name in ("halo-1", "halo-2")}


class FakeAPI:
    def __init__(self):
        machines = {mid: config.Machine(mid, system, "ssh" if system == "linux" else "local", "192.0.2.1", mid, 22,
                                        "/opt/hyperloom/" + mid, True, [])
                    for mid, system in (("workstation", "windows"), ("halo-1", "linux"), ("halo-2", "linux"))}
        self._cluster = config.Cluster(machines, {}, {}, {}, {}, {})

    def cluster(self):
        return self._cluster


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.counts = {}

        def fetch(api, machine, payload):
            self.calls.append((machine.id, payload))
            report = copy.deepcopy(REPORTS[machine.id])
            for vector in report["interrupts"]["vectors"]:
                vector["count"] += self.counts.get(machine.id, 0)
            if "journal" not in payload:
                report["journal"] = {"status": "skipped", "why": "not requested in this call"}
            return report
        for patcher in (mock.patch.object(usb4, "fetch", fetch), mock.patch.object(command, "_api", FakeAPI)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["usb4", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_text_report_of_every_linux_node(self):
        code, out, err = self.main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([m for m, _ in self.calls], ["halo-1", "halo-2"])
        self.assertEqual(self.calls[0][1], {"journal": {}})
        self.assertIn("link 0-2 -> halo-2: rx 20 Gb/s x2 = 40 Gb/s, tx 20 Gb/s x2 = 40 Gb/s · full rate", out)
        self.assertIn("link 0-2 -> halo-1: rx 10 Gb/s x2 = 20 Gb/s, tx 10 Gb/s x2 = 20 Gb/s · BELOW FULL RATE (rx, tx)", out)
        self.assertIn("stream 0-2.0/data0 /dev/tbstream0: published openable=True", out)
        self.assertIn("pass --rate SECONDS", out)
        self.assertIn("kernel evidence this boot: close_not_sent 1,", out)

    def test_json_one_machine_and_errors(self):
        code, out, _ = self.main("halo-2", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(list(data), ["halo-2"])
        self.assertEqual(data["halo-2"]["journal"]["status"], "ok")
        self.assertEqual(self.main("workstation")[0], 2)
        self.assertEqual(self.main("--rate", "500")[0], 2)
        with mock.patch.object(usb4, "fetch", side_effect=OSError("ssh: connection refused")):
            code, out, _ = self.main("halo-1")
        self.assertEqual(code, 1)
        self.assertIn("halo-1 · unavailable · OSError: ssh: connection refused", out)

    def test_rate_from_two_samples(self):
        api = FakeAPI()
        machine = api.cluster().machines["halo-1"]
        clock = iter([100.0, 110.0])

        def sleep(seconds):
            self.counts["halo-1"] = 500
        summary = command.collect(api, machine, rate=10, sleep=sleep, clock=lambda: next(clock))
        rates = {(row["nhi"], row["ring"]): row["rate_per_s"] for row in summary["interrupts"]["nhis"]}
        self.assertEqual(rates[("0000:c7:00.5", "control")], 100.0)        # 2 vectors x 500 over 10 s
        self.assertEqual(self.calls[1][1], {})                               # the second sample skips the journal
        self.assertEqual(summary["journal"]["status"], "ok")

    def test_samples_across_a_reboot_or_without_boot_ids_are_not_combined(self):
        api = FakeAPI()
        machine = api.cluster().machines["halo-1"]
        boots = {"value": None}
        original = usb4.fetch

        def fetch(api_, machine_, payload):
            report = original(api_, machine_, payload)
            if boots["value"] is not None:
                report["boot_id"] = boots["value"]
            return report

        def reboot(seconds):
            boots["value"] = "c" * 32
            self.counts["halo-1"] = 500
        with mock.patch.object(usb4, "fetch", fetch):
            summary = command.collect(api, machine, rate=5, sleep=reboot, clock=iter([0.0, 5.0]).__next__)
        self.assertEqual(summary["journal"]["status"], "unavailable")
        self.assertTrue(all(row["rate_per_s"] is None and "rebooted" in row["why"] for row in summary["interrupts"]["nhis"]))

        def no_boot(seconds):
            boots["value"] = ""
        with mock.patch.object(usb4, "fetch", fetch):
            boots["value"] = ""
            summary = command.collect(api, machine, rate=5, sleep=no_boot, clock=iter([0.0, 5.0]).__next__)
        self.assertEqual(summary["journal"]["status"], "unavailable")
        self.assertTrue(all("no boot id" in row["why"] for row in summary["interrupts"]["nhis"]))


if __name__ == "__main__":
    unittest.main()
