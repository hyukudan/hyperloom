"""Node verb `host` (fleet package): sections from a fixture tree, payload rules, bounds, loopback engine reads."""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


NODE = Path(__file__).resolve().parents[1] / "node"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fleet"


def load_module():
    spec = importlib.util.spec_from_file_location("fleet_node_observe", NODE / "hyperloom_observe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observe = load_module()

STAT_1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\ncpu1 50 0 50 350 50 0 0 0 0 0\nintr 1\n"
STAT_2 = "cpu  200 0 200 1300 100 0 0 0 0 0\ncpu0 100 0 100 650 50 0 0 0 0 0\ncpu1 100 0 100 650 50 0 0 0 0 0\nintr 2\n"


def write(root, path, text):
    target = Path(root) / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def build_tree(root):
    files = {
        "/proc/sys/kernel/random/boot_id": "0000000a-0000-4000-8000-00000000000a\n",
        "/proc/uptime": "1234.50 9000.00\n",
        "/proc/loadavg": "1.50 1.25 1.00 3/400 5000\n",
        "/proc/stat": STAT_1,
        "/proc/meminfo": "MemTotal:       131072000 kB\nMemFree:         1000 kB\nMemAvailable:    65536000 kB\n"
                         "Buffers: 10 kB\nCached: 20 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\nShmem: 5 kB\nHugePages_Total: 0\n",
        "/proc/self/mounts": "/dev/nvme0n1p2 / ext4 rw 0 0\n/dev/nvme0n1p1 /boot/efi vfat rw 0 0\n"
                             "tmpfs /run tmpfs rw 0 0\n/dev/nvme0n1p2 /var/again ext4 rw 0 0\nproc /proc proc rw 0 0\n",
        "/proc/diskstats": "259 0 nvme0n1 10 0 2000 0 20 0 4000 0 0 0 0\n259 1 nvme0n1p1 1 0 8 0 0 0 0 0 0 0 0\n",
        "/proc/net/dev": "Inter-|   Receive\n face |bytes packets\n    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
                         "  eno1: 5000 10 0 0 0 0 0 0 7000 20 0 0 0 0 0 0\n  veth9: 1 1 0 0 0 0 0 0 2 1 0 0 0 0 0 0\n",
        # LISTEN rows: any address on port 22, loopback on port 0x4A7D, a LAN bind; tcp6 any and loopback; one non-listen row
        "/proc/net/tcp": "  sl  local_address rem_address   st\n"
                         "   0: 00000000:0016 00000000:0000 0A 0\n   1: 0100007F:4A7D 00000000:0000 0A 0\n"
                         "   2: 0A01A8C0:1F90 00000000:0000 0A 0\n   3: 0100007F:4A7E 0100007F:9999 01 0\n",
        "/proc/net/tcp6": "  sl  local_address rem_address st\n"
                          "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A 0\n"
                          "   1: 00000000000000000000000001000000:4A7D 00000000000000000000000000000000:0000 0A 0\n",
        "/proc/4321/comm": "engine-a\n",
        "/proc/4322/comm": "other\n",
        "/sys/class/hwmon/hwmon0/name": "k10temp\n",
        "/sys/class/hwmon/hwmon0/temp1_input": "66750\n",
        "/sys/class/hwmon/hwmon0/temp1_label": "Tctl\n",
        "/sys/class/hwmon/hwmon1/name": "nvme\n",
        "/sys/class/hwmon/hwmon1/temp1_input": "58850\n",
        "/sys/class/hwmon/hwmon1/temp1_label": "Composite\n",
        "/sys/class/hwmon/hwmon1/temp2_input": "60000\n",
        "/sys/class/hwmon/hwmon1/temp2_label": "Composite\n",
        "/sys/class/hwmon/hwmon2/name": "nvme\n",
        "/sys/class/hwmon/hwmon2/temp1_input": "40000\n",
        "/sys/class/hwmon/hwmon3/name": "acpitz\n",
        "/sys/class/hwmon/hwmon3/temp1_input": "67000\n",
        "/sys/class/drm/card0/device/vendor": "0x1002\n",
        "/sys/class/drm/card0/device/gpu_busy_percent": "15\n",
        "/sys/class/drm/card0/device/mem_info_vram_used": "168546304\n",
        "/sys/class/drm/card0/device/mem_info_vram_total": "536870912\n",
        "/sys/class/drm/card0/device/mem_info_vis_vram_used": "168546304\n",
        "/sys/class/drm/card0/device/mem_info_vis_vram_total": "536870912\n",
        "/sys/class/drm/card0/device/mem_info_gtt_used": "64026701824\n",
        "/sys/class/drm/card0/device/mem_info_gtt_total": "125829120000\n",
        "/sys/class/drm/card0/device/pp_dpm_sclk": "0: 600Mhz \n1: 1814Mhz *\n2: 2900Mhz \n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/name": "amdgpu\n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/temp1_input": "50000\n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/temp1_label": "edge\n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/power1_average": "35467000\n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/power1_input": "36000000\n",
        "/sys/class/drm/card0/device/hwmon/hwmon5/power1_label": "PPT\n",
        "/sys/class/drm/card1/device/vendor": "0x10de\n",
        "/sys/class/net/eno1/address": "02:00:00:00:00:01\n",
        "/sys/class/net/eno1/operstate": "up\n",
        "/sys/class/net/eno1/ifindex": "2\n",
        "/sys/class/net/eno1/device/uevent": "\n",
        "/sys/class/net/veth9/address": "02:00:00:00:00:09\n",
        "/sys/class/kfd/kfd/proc/4321/vram_51102": "1048576\n",
        "/sys/class/kfd/kfd/proc/4322/vram_7": "2048\n",
    }
    for path, text in files.items():
        write(root, path, text)


class Statvfs:
    def __init__(self, blocks, free, avail):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, free, avail, 4096


def statvfs(path):
    return Statvfs(1000, 400, 300)


class Owners:
    """os.stat replacement: /proc/4321 belongs to us (uid 1000), everything else to someone else."""

    def __init__(self):
        self.real = os.stat

    def __call__(self, path, *a, **kw):
        result = self.real(path, *a, **kw)
        uid = 1000 if str(path).replace("\\", "/").endswith("/proc/4321") else 0
        return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink, uid, result.st_gid,
                               result.st_size, int(result.st_atime), int(result.st_mtime), int(result.st_ctime)))


class HostVerbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name
        build_tree(self.root)

    def collect(self, payload=None, **kw):
        with mock.patch.object(observe.os, "stat", Owners()):
            return observe.collect(payload or {}, root=self.root, statvfs=statvfs, uid=1000,
                                   sleep=kw.pop("sleep", lambda s: write(self.root, "/proc/stat", STAT_2)), **kw)

    def test_sections_from_a_fixture_tree(self):
        result = self.collect()
        self.assertEqual(result["schema"], "hyperloom/node-v1")
        checks = result["checks"]
        self.assertEqual({k for k, v in checks.items() if v["status"] != "ok"}, set())
        self.assertEqual(checks["boot_id"]["value"], "0000000a-0000-4000-8000-00000000000a")
        self.assertEqual(checks["uptime_s"]["value"], 1234.5)
        self.assertEqual(checks["load"]["value"], [1.5, 1.25, 1.0])
        cpu = checks["cpu"]["value"]
        # Between the two reads 800 ticks passed, 600 of them idle or waiting: 25 % busy.
        self.assertAlmostEqual(cpu["busy_percent"], 25.0)
        self.assertEqual((cpu["cores"], cpu["total"], cpu["idle"]), (2, 1800, 1400))
        self.assertEqual(checks["memory"]["value"]["MemAvailable"], 65536000 * 1024)
        self.assertNotIn("HugePages_Total", checks["memory"]["value"])
        card = checks["gpus"]["value"][0]
        self.assertEqual(len(checks["gpus"]["value"]), 1)          # the non-AMD card is ignored
        self.assertEqual((card["card"], card["busy_percent"], card["sclk_mhz"]), ("card0", 15, 1814))
        self.assertEqual((card["power_w"], card["power_sensor"], card["power_field"]), (35.467, "PPT", "average"))
        self.assertIsNone(card["power_cap_w"])
        self.assertIn("power_cap_w", card["why"])
        self.assertEqual(card["temps"], [{"sensor": "edge", "input": "temp1", "celsius": 50.0}])
        self.assertEqual(card["gtt_total_bytes"], 125829120000)
        chips = {chip["hwmon"]: chip for chip in checks["sensors"]["value"]}
        self.assertEqual(chips["hwmon0"]["temps"], [{"sensor": "Tctl", "input": "temp1", "celsius": 66.75}])
        self.assertEqual(chips["hwmon3"]["temps"][0]["sensor"], "temp1")      # no label: the input name
        disks = checks["disks"]["value"]
        self.assertEqual([d["mount"] for d in disks], ["/", "/boot/efi"])     # tmpfs, proc, the repeated device skipped
        self.assertEqual(disks[0]["total_bytes"], 1000 * 4096)
        self.assertEqual(disks[0]["used_bytes"], 600 * 4096)
        self.assertEqual(disks[0]["free_bytes"], 300 * 4096)
        self.assertEqual(checks["diskstats"]["value"], [{"device": "nvme0n1", "dev": "259:0", "read_bytes": 2000 * 512, "write_bytes": 4000 * 512}])
        net = {row["iface"]: row for row in checks["net"]["value"]}
        self.assertEqual(set(net), {"eno1", "veth9"})
        self.assertEqual((net["eno1"]["rx_bytes"], net["eno1"]["tx_bytes"], net["eno1"]["physical"]), (5000, 7000, True))
        self.assertFalse(net["veth9"]["physical"])
        self.assertEqual(checks["nics"]["value"], [{"iface": "eno1", "mac": "02:00:00:00:00:01"}])
        procs = {row["pid"]: row for row in checks["gpu_processes"]["value"]}
        self.assertEqual((procs[4321]["comm"], procs[4321]["own"], procs[4321]["vram_bytes"]), ("engine-a", True, 1048576))
        self.assertEqual((procs[4322]["comm"], procs[4322]["own"]), (None, False))
        self.assertIn("comm", procs[4322]["why"])
        self.assertEqual(checks["listeners"]["value"], [
            {"port": 22, "family": "ipv4", "scope": "any"}, {"port": 22, "family": "ipv6", "scope": "any"},
            {"port": 8080, "family": "ipv4", "scope": "lan"}, {"port": 19069, "family": "ipv4", "scope": "loopback"},
            {"port": 19069, "family": "ipv6", "scope": "loopback"}])
        self.assertNotIn("engine_status", checks)
        self.assertLessEqual(len(json.dumps(result)), observe.MAX_OUTPUT)

    def test_cpu_baseline_avoids_the_second_read(self):
        slept = []
        baseline = {"boot_id": "0000000a-0000-4000-8000-00000000000a", "total": 900, "idle": 750}
        cpu = self.collect({"cpu": baseline}, sleep=slept.append)["checks"]["cpu"]["value"]
        self.assertEqual(slept, [])
        self.assertAlmostEqual(cpu["busy_percent"], 50.0)        # 100 ticks since the baseline, 50 of them idle
        other_boot = dict(baseline, boot_id="0000000b-0000-4000-8000-00000000000b")
        self.collect({"cpu": other_boot}, sleep=slept.append)
        self.assertEqual(slept, [0.2])                                          # another boot: two reads

    def test_payload_is_strict(self):
        for payload in ({"root": "/"}, {"cpu": {"total": 1}}, {"cpu": {"boot_id": "x", "total": -1, "idle": 0}},
                        {"engine_status": {"port": 80}}, {"engine_status": {"port": 70000}},
                        {"engine_status": {"port": True}}, {"engine_status": {"port": 18501, "paths": ["/admin/drain"]}},
                        {"engine_status": {"port": 18501, "paths": ["/health", "/health"]}},
                        {"engine_status": {"port": 18501, "host": "x"}},
                        {"engine_status": [{"port": 18501 + i} for i in range(5)]},
                        {"engine_status": [{"port": 18501}, {"port": 18501}]}, {"engine_status": []}):
            with self.assertRaises(ValueError, msg=payload):
                observe.collect(payload, root=self.root, statvfs=statvfs)
        with self.assertRaises(ValueError):
            observe.run("gpu", {})

    def test_deadline_marks_the_remaining_sections(self):
        ticks = iter([0.0] + [0.0] * 6 + [10.0] * 500)
        result = observe.collect({}, root=self.root, statvfs=statvfs, uid=1000, clock=lambda: next(ticks),
                                 sleep=lambda s: None)
        late = [k for k, v in result["checks"].items() if v.get("why") == "node deadline reached"]
        self.assertIn("listeners", late)
        self.assertEqual(result["checks"]["boot_id"]["status"], "ok")

    def test_file_budget_is_enforced(self):
        with mock.patch.object(observe, "FILE_BUDGET", 5):
            result = observe.collect({}, root=self.root, statvfs=statvfs, uid=1000, sleep=lambda s: None)
        self.assertEqual(result["checks"]["boot_id"]["status"], "ok")
        self.assertNotEqual(result["checks"]["listeners"]["status"], "ok")

    @unittest.skipIf(os.name == "nt", "symbolic links need privileges on Windows")
    def test_links_out_of_the_readable_roots_are_not_followed(self):
        outside = Path(self.root).parent / (Path(self.root).name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: [p.unlink() for p in outside.iterdir()] and None or outside.rmdir())
        (outside / "name").write_text("secret-chip\n")
        (outside / "temp1_input").write_text("1000\n")
        os.symlink(outside, Path(self.root) / "sys/class/hwmon/hwmon9")
        chips = observe.collect({}, root=self.root, statvfs=statvfs, sleep=lambda s: None)["checks"]["sensors"]["value"]
        nine = next(chip for chip in chips if chip["hwmon"] == "hwmon9")
        self.assertIsNone(nine["name"])
        self.assertEqual(nine["temps"], [])

    def test_missing_sources_say_why(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        checks = observe.collect({}, root=empty.name, statvfs=statvfs, sleep=lambda s: None)["checks"]
        for name, value in checks.items():
            self.assertEqual(value["status"], "unavailable", name)
            self.assertTrue(value["why"], name)

    def test_recorded_node_answers_have_every_section(self):
        for name in ("host-halo-1.json", "host-halo-2.json"):
            answer = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            self.assertEqual(answer["verb"], "host")
            self.assertEqual({k for k, v in answer["checks"].items() if v["status"] == "ok"},
                             {"boot_id", "uptime_s", "load", "cpu", "memory", "gpus", "sensors", "disks", "diskstats",
                              "net", "nics", "gpu_processes", "listeners"})

    def test_node_cli_refuses_a_bad_payload(self):
        result = subprocess.run([sys.executable, str(NODE / "hyperloom-node"), "host"], input=b'{"root": "/etc"}',
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("host payload takes only", json.loads(result.stdout)["error"])


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/api/status?detail=1":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b'{"model": ', b'"m", "uptime_seconds": 5}'):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/metrics":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)

    def _send(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EngineStatusTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer((observe.LOOPBACK, 0), Engine)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def status(self, paths, **kw):
        result = observe.collect({"engine_status": {"port": self.port, "paths": paths}}, root=self.dir.name,
                                 statvfs=statvfs, sleep=lambda s: None, **kw)
        return result["checks"]["engine_status"]["value"][0]["paths"]

    def test_fixed_paths_on_the_loopback_only(self):
        connects = []

        def connect(address, timeout):
            connects.append(address)
            return socket.create_connection(address, timeout=timeout)

        paths = self.status(["/health", "/api/status?detail=1", "/metrics"], connect=connect)
        self.assertEqual(paths["/health"]["value"]["body"], "ok")
        self.assertEqual(json.loads(paths["/api/status?detail=1"]["value"]["body"]), {"model": "m", "uptime_seconds": 5})
        self.assertEqual(paths["/metrics"]["why"], "HTTP 302 (redirects are not followed)")
        self.assertEqual(connects, [(observe.LOOPBACK, self.port)] * 3)
        timings = [paths[p]["timing"] for p in ("/health", "/api/status?detail=1", "/metrics")]
        self.assertTrue(all(0 <= t["start_s"] <= t["end_s"] for t in timings))
        self.assertTrue(timings[0]["end_s"] <= timings[1]["start_s"] <= timings[1]["end_s"] <= timings[2]["start_s"])
        self.assertTrue(observe.LOOPBACK.startswith("127."))

    def test_a_trickling_response_stops_at_the_deadline(self):
        with mock.patch.object(observe, "ENGINE_TIMEOUT_S", 0.4):
            started = time.monotonic()
            paths = self.status(["/api/status"])
            elapsed = time.monotonic() - started
        self.assertEqual(paths["/api/status"]["why"], "engine response not complete within its deadline")
        self.assertLess(elapsed, 1.5)

    def test_size_and_output_budgets(self):
        with mock.patch.object(observe, "ENGINE_MAX_BYTES", 1):
            self.assertEqual(self.status(["/api/status?detail=1"])["/api/status?detail=1"]["why"], "response exceeds 1 MiB")
        payload = {"engine_status": {"port": self.port, "paths": ["/health", "/api/status?detail=1"]}}
        full = len(json.dumps(observe.collect(payload, root=self.dir.name, statvfs=statvfs, sleep=lambda s: None)))
        with mock.patch.object(observe, "MAX_OUTPUT", full - 5):
            result = observe.collect(payload, root=self.dir.name, statvfs=statvfs, sleep=lambda s: None)
        trimmed = result["checks"]["engine_status"]["value"][0]["paths"]
        self.assertEqual(trimmed["/api/status?detail=1"]["why"], "response exceeds the node output budget")

    def test_unreachable_port_is_an_outcome(self):
        with socket.socket() as probe:
            probe.bind((observe.LOOPBACK, 0))
            free = probe.getsockname()[1]
        result = observe.collect({"engine_status": {"port": free, "paths": ["/health"]}}, root=self.dir.name,
                                 statvfs=statvfs, sleep=lambda s: None)
        why = result["checks"]["engine_status"]["value"][0]["paths"]["/health"]["why"]
        # Refused at once on Linux; Windows retries a refused loopback connect until the path deadline.
        self.assertIn(why, ("engine not reachable on loopback: ConnectionRefusedError",
                            "engine response not complete within its deadline"))


if __name__ == "__main__":
    unittest.main()
