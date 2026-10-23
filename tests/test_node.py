import hashlib
from importlib.machinery import SourceFileLoader
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "node" / "hyperloom-node"
loader = SourceFileLoader("hyperloom_node_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
node = importlib.util.module_from_spec(spec)
loader.exec_module(node)


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom node ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.invocations = []
        self.outputs = {}
        self.inspector = node.Inspector(self.root, "/opt/lab space Ω", self.runner)

    def write(self, path, value):
        destination = self.root / path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode("utf-8"))
        return destination

    def runner(self, argv, **kwargs):
        self.invocations.append((argv, kwargs))
        result = self.outputs.get(Path(argv[0]).name, "synthetic version")
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(returncode=0, stdout=result.encode("utf-8"), stderr=b"")

    def command(self, name, output="synthetic version"):
        self.write("/usr/bin/" + name, "fixture executable placeholder")
        self.outputs[name] = output

    def run_cli(self, verb="version", payload=b"", *options):
        return subprocess.run([sys.executable, str(SCRIPT), verb, "--root", str(self.root), *options],
                              input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10)

    def test_version_is_one_json_document_and_no_commands(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        obj = json.loads(result.stdout)
        self.assertEqual(obj["schema"], "hyperloom/node-v1")
        self.assertEqual(obj["verb"], "version")
        self.assertEqual(obj["commands"], [])
        self.assertEqual(obj["checks"]["version"]["value"], "0.1.0")

    def test_empty_tree_doctor_reports_unavailable(self):
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        obj = json.loads(result.stdout)
        checks = obj["checks"]
        for name in ("os", "cmake", "ninja", "git", "compiler", "rocm_root",
                     "rocm_version", "rocm_package_version", "gfx_targets", "lab", "secure_boot"):
            self.assertEqual(checks[name]["status"], "unavailable", name)
            self.assertTrue(checks[name]["why"])
        self.assertEqual(checks["disk"]["status"], "ok")
        self.assertEqual(obj["commands"], [])

    def test_doctor_versions_passive_gfx_and_exact_commands(self):
        self.write("/etc/os-release", 'NAME="Synthetic Linux"\nVERSION_ID="1"\n')
        self.write("/opt/rocm/core-10.1/.info/version", "10.1.0\n")
        for name in ("cmake", "ninja", "git", "amdclang++", "dpkg-query", "mokutil"):
            self.command(name)
        self.write("/sys/class/kfd/kfd/topology/nodes/0/properties", "gfx_target_version 0\n")
        self.write("/sys/class/kfd/kfd/topology/nodes/1/properties", "gfx_target_version 110501\n")
        result = self.inspector.inspect("doctor")
        checks = result["checks"]
        self.assertEqual(checks["gfx_targets"]["value"], [{
            "path": "/sys/class/kfd/kfd/topology/nodes/1/properties", "target": "gfx1151"}])
        self.assertEqual(checks["rocm_version"]["value"], "10.1.0")
        self.assertEqual(checks["rocm_package_version"]["status"], "ok")
        self.assertEqual(checks["rocm_root"]["value"], True)
        self.assertEqual(result["commands"], [argv for argv, kwargs in self.invocations])
        commands = [[Path(argv[0]).name, *argv[1:]] for argv in result["commands"]]
        self.assertEqual(commands, [["cmake", "--version"], ["ninja", "--version"],
                                    ["git", "--version"], ["amdclang++", "--version"],
                                    ["dpkg-query", "-W", "-f=${Version}", "rocm-core"],
                                    ["mokutil", "--sb-state"]])
        self.assertTrue(checks["secure_boot"]["informational"])
        for argv, kwargs in self.invocations:
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(kwargs["timeout"], 5)

    def test_compiler_and_package_fallback(self):
        self.command("c++", "Synthetic compiler 1")
        self.command("rpm", "10.1")
        checks = self.inspector.doctor()
        self.assertEqual(checks["compiler"]["value"], "Synthetic compiler 1")
        self.assertEqual(checks["rocm_package_version"]["value"], "10.1")

    def test_usb4_json_no_arguments_module_devices_layout(self):
        describe = {"version": 1, "topology": {"warnings": ["synthetic cable absent"]}}
        self.command("usb4stream-describe", json.dumps(describe))
        self.write("/sys/module/thunderbolt_stream/fixture", "")
        self.write("/dev/tbstream0", "")
        self.write("/run/usb4stream-layout.json", '{"layout":1}')
        usb = self.inspector.doctor()["usb4"]
        self.assertEqual(usb["describe"]["value"], describe)
        self.assertEqual(usb["devices"]["value"], ["/dev/tbstream0"])
        self.assertEqual(usb["module"]["value"], True)
        self.assertEqual(usb["layout"]["value"], {"layout": 1})
        argv = next(argv for argv in self.inspector.commands if Path(argv[0]).name == "usb4stream-describe")
        self.assertEqual(len(argv), 1)

    def test_usb4_malformed_json_and_missing_contract(self):
        for output in ("not JSON", "[]", '{"version":1}', '{"version":1,"version":2}',
                       '{"version":1,"topology":{"load":1e999}}'):
            with self.subTest(output=output):
                self.command("usb4stream-describe", output)
                result = self.inspector.doctor()["usb4"]["describe"]
                self.assertEqual(result["status"], "unavailable")

    def test_lab_script_sha_and_state_are_read_only(self):
        script = "#!/bin/sh\n# synthetic lab script\n"
        for name in ("gpu-turn.sh", "gpu-turn-pair.sh", "layout-watch2.sh"):
            self.write("/opt/lab space Ω/" + name, script)
        for name, value in (("holder", "PAIR hl-ds41-pair-test"),
                            ("maintenance", "planned"), ("layout-state", "1")):
            self.write("/opt/lab space Ω/.gpu-turn/" + name, value)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.inspector.lab()["value"]
        self.assertEqual(result["holder"]["value"], "PAIR hl-ds41-pair-test")
        digest = hashlib.sha256(script.encode()).hexdigest()
        for check in result["script_sha256"].values():
            self.assertEqual(check, {"status": "ok", "value": digest})
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.inspector.commands, [])

    def test_missing_lab_files_do_not_crash(self):
        result = self.inspector.lab()["value"]
        for check in result["script_sha256"].values():
            self.assertEqual(check["status"], "unavailable")
        for field in ("holder", "maintenance", "layout-state"):
            self.assertEqual(result[field]["status"], "unavailable")

    def gpu_tree(self):
        device = "/sys/class/drm/card0/device/"
        self.write(device + "vendor", "0x1002")
        for name, value in (("mem_info_vram_used", 4096), ("mem_info_vram_total", 8192),
                            ("mem_info_gtt_used", 1024), ("mem_info_gtt_total", 16384),
                            ("gpu_busy_percent", 7)):
            self.write(device + name, str(value))
        self.write(device + "hwmon/hwmon0/temp1_input", "42500")
        self.write(device + "hwmon/hwmon0/power1_average", "15500000")
        self.write("/proc/meminfo", "MemTotal: 10000 kB\nMemAvailable: 6000 kB\nMemFree: 2000 kB\n")
        self.write("/proc/loadavg", "0.25 0.50 1.00 1/100 42")

    def test_gpu_passive_memory_hwmon_ram_load_and_kernel(self):
        self.gpu_tree()
        self.command("uname", "synthetic-kernel")
        result = self.inspector.inspect("gpu")
        checks = result["checks"]
        card = checks["amd"]["value"][0]
        self.assertEqual(card["id"], "card0")
        for name, value in (("vram_used_bytes", 4096), ("vram_total_bytes", 8192),
                            ("gtt_used_bytes", 1024), ("gtt_total_bytes", 16384), ("busy_percent", 7)):
            self.assertEqual(card[name]["value"], value)
        self.assertEqual(card["temperature_c"]["value"]["hwmon0/temp1_input"]["value"], 42.5)
        self.assertEqual(card["power_w"]["value"]["hwmon0/power1_average"]["value"], 15.5)
        self.assertEqual(checks["ram"]["value"], {"total_bytes": 10240000,
                         "available_bytes": 6144000, "free_bytes": 2048000, "used_bytes": 4096000})
        self.assertEqual(checks["load"]["value"], [0.25, 0.5, 1.0])
        self.assertEqual(checks["kernel"]["value"], "synthetic-kernel")
        self.assertEqual([[Path(argv[0]).name, *argv[1:]] for argv in result["commands"]], [["uname", "-r"]])

    def test_non_amd_and_connectors_are_skipped(self):
        self.gpu_tree()
        self.write("/sys/class/drm/card1/device/vendor", "0x1234")
        self.write("/sys/class/drm/card0-DP-1/device/vendor", "0x1002")
        self.assertEqual(len(self.inspector.gpu()["amd"]["value"]), 1)

    def test_power_input_fallback(self):
        self.write("/sys/class/drm/card0/device/vendor", "0x1002")
        self.write("/sys/class/drm/card0/device/hwmon/hwmon0/power1_input", "1000000")
        card = self.inspector.gpu()["amd"]["value"][0]
        self.assertEqual(card["power_w"]["value"]["hwmon0/power1_input"]["value"], 1)
        self.assertEqual(card["vram_used_bytes"]["status"], "unavailable")

    def test_missing_gpu_and_ram_are_unavailable(self):
        result = self.run_cli("gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        for check in json.loads(result.stdout)["checks"].values():
            self.assertEqual(check["status"], "unavailable")

    def test_malformed_values_and_partial_ram(self):
        self.write("/sys/class/drm/card0/device/vendor", "0x1002")
        self.write("/sys/class/drm/card0/device/gpu_busy_percent", "bad")
        self.write("/proc/meminfo", "MemTotal: 10000 kB\n")
        self.write("/proc/loadavg", "bad")
        checks = self.inspector.gpu()
        self.assertIsNone(checks["ram"]["value"]["used_bytes"])
        self.assertEqual(checks["load"]["status"], "unavailable")
        self.assertEqual(checks["amd"]["value"][0]["busy_percent"]["status"], "unavailable")
        self.write("/proc/meminfo", "bad")
        self.assertEqual(self.inspector.ram()["status"], "unavailable")

    def test_command_timeout_and_oserror_report_unavailable(self):
        self.command("cmake")
        for error in (subprocess.TimeoutExpired(["cmake"], 5), OSError("unavailable")):
            self.outputs["cmake"] = error
            self.assertEqual(self.inspector.doctor()["cmake"]["status"], "unavailable")
            self.assertTrue(self.inspector.commands)

    def test_payload_validation(self):
        for payload in (b"[]", b"{} {}", b'{"x":1}', b'{"root":1}', b'{"root":null,"root":null}',
                        b'{"lab_dir":"relative"}', b'{"lab_dir":"/opt/../lab"}',
                        b'{"root":NaN}', b'{"root":1e999}', b"\xff", b"x" * (node.MAX_JSON + 1)):
            with self.subTest(payload=payload[:40]):
                result = self.run_cli("version", payload)
                self.assertEqual(result.returncode, 2, result.stderr)
                obj = json.loads(result.stdout)
                self.assertIn("error", obj)
                self.assertEqual(obj["commands"], [])

    def test_payload_paths_and_options_on_either_side_of_verb(self):
        payload = json.dumps({"lab_dir": "/opt/lab space Ω"}, ensure_ascii=False).encode("utf-8")
        result = self.run_cli("doctor", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"]["lab"]["status"], "ok")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "gpu"],
                                input=b"{}", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_mutating_verbs(self):
        for verb in ("install", "lease", "supervise"):
            result = self.run_cli(verb)
            self.assertEqual(result.returncode, 2)

    def test_file_limit_and_fixture_escape(self):
        self.write("/proc/meminfo", "x" * (node.MAX_FILE + 1))
        self.assertEqual(self.inspector.ram()["status"], "unavailable")
        with self.assertRaises(ValueError):
            self.inspector.path("/../outside")


if __name__ == "__main__":
    unittest.main()
