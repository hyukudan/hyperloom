"""Portal summaries against sanitized captured field shapes; no hardware access."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from hyperloom import config
from hyperloom.web.api import API, _accelerator, _nic_name, _nics, _os_name, _usb4

FIXTURE = Path(__file__).parent / "fixtures" / "web" / "inspection.json"


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.inspection = json.loads(FIXTURE.read_text(encoding="utf-8"))
        machines = {mid: config.Machine(mid, "windows" if mid == "workstation" else "linux",
                    "local" if mid == "workstation" else "ssh", "192.0.2." + str(i + 10), mid, 22,
                    "/opt/hyperloom/" + mid, True, [{"id": "gpu"}])
                    for i, mid in enumerate(("workstation", "halo-1", "halo-2"))}
        self.cluster = config.Cluster(machines, {}, {"qwen-replicas": {"nodes": ["halo-1", "halo-2"]}}, {}, {}, {})
        self.orch = Mock()
        self.orch._read.side_effect = FileNotFoundError
        self.api = API(orchestrator=self.orch)
        self.api._node = lambda m, verb: deepcopy(self.inspection[m.id][verb])
        self.api._probe = lambda m: deepcopy(self.inspection[m.id].get("probe"))
        self.api._action_log = lambda: []

    def inventory(self):
        return self.api._inventory(self.cluster, {})

    def test_os_ram_nics_and_rocm_are_short_summaries(self):
        pc, first, second = self.inventory()
        self.assertEqual(pc["os"], "Windows 11")
        self.assertEqual(first["os"], "Ubuntu 26.04.1 LTS")
        self.assertEqual(first["ram_summary"], "80.0 GB free of 131.0 GB")
        self.assertEqual(first["cpu_model"], "Ryzen AI Max+ 395")
        self.assertEqual(pc["nics_summary"], ["Ethernet · 1 Gb/s"])
        self.assertEqual(first["rocm"], "ROCm 10.1.0")
        self.assertEqual(len(first["rocm_packages"]), 2)
        self.assertEqual(first["kernel_summary"], "7.3.0-rc5 · USB4 stream driver loaded")

    def test_flat_usb4_records_override_incorrect_zero_count(self):
        pc, first, second = self.inventory()
        self.assertIsNone(pc["usb4"])
        self.assertEqual(first["usb4"]["summary"], "4/4 streams openable · layout 1 (host)")
        self.assertEqual(first["usb4"]["cables"][0]["summary"], "Cable A · 40 Gb/s")
        self.assertEqual(first["usb4"]["cables"][1]["summary"], "Cable B · 40 Gb/s")
        self.assertEqual(second["usb4"]["cables"][1]["health"], "warning")
        self.assertNotIn("/data", json.dumps(first["usb4"]))

    def test_degraded_requires_a_real_problem(self):
        pc, first, second = self.inventory()
        self.assertEqual([m["health"] for m in (pc, first, second)], ["online", "online", "degraded"])
        self.assertEqual(second["warnings"], ["Cable B trained below Gen3 x2"])
        self.inspection["halo-1"]["doctor"]["checks"]["usb4"]["describe"]["value"]["0-2.0/data0"]["openable"] = False
        first = self.inventory()[1]
        self.assertEqual(first["health"], "degraded")
        self.assertIn("USB4 missing stream · 3/4 streams openable", first["warnings"])

    def test_unknown_openability_is_not_a_missing_stream(self):
        usb = self.inspection["halo-1"]["doctor"]["checks"]["usb4"]
        for key, stream in usb["describe"]["value"].items():
            if "/data" in key:
                stream.pop("openable")
        for cable in usb["describe"]["value"]["topology"]["cables"]:
            cable.pop("streams_openable")
        first = self.inventory()[1]
        self.assertIsNone(first["usb4"]["openable"])
        self.assertEqual(first["health"], "online")

    def test_powershell_interface_shape_and_names_are_normalized(self):
        links = _nics([{"Name": "Conexión", "Status": "Up", "LinkSpeed": "1 Gbps"}])
        self.assertEqual(links[0]["summary"], "Conexión · 1 Gb/s")

    def test_legacy_launch_uses_the_cli_renderer_without_claiming_saved_inputs(self):
        from hyperloom.render import ProcessSpec
        spec = ProcessSpec("halo-1", "replica", ["/opt/engine", "--http-port", "18501"], {}, "qwen-stderr")
        with patch("hyperloom.web.api.render.render_processes", return_value=[spec]) as renderer:
            profile = self.api._profiles(self.cluster, {})[0]
        renderer.assert_called_once()
        self.assertEqual(profile["effective_configuration"][0]["source"], "configured profile render")
        self.assertEqual(profile["effective_configuration"][0]["value"], spec.argv)

    def test_unified_memory_sums_vram_and_gtt_for_headroom(self):
        gpu = self.inventory()[1]["accelerators"][0]
        self.assertEqual(gpu["name"], "Radeon 8060S · gfx1151")
        self.assertEqual(gpu["memory_kind"], "unified · GPU-visible")
        self.assertEqual(gpu["memory_total_bytes"], 125536870912)
        self.assertEqual(gpu["memory_used_bytes"], 40200000000)
        self.assertEqual(gpu["memory_free_bytes"], 85336870912)

    def test_missing_gtt_usage_never_reports_carveout_headroom(self):
        raw = self.inspection["halo-1"]["gpu"]["checks"]["amd"]["value"][0]
        raw["gtt_used_bytes"] = {"status": "unavailable", "value": None}
        gpu = self.inventory()[1]["accelerators"][0]
        self.assertIsNone(gpu["memory_used_bytes"])
        self.assertIsNone(gpu["memory_free_bytes"])

    def test_discrete_gpu_keeps_vram_accounting(self):
        raw = {"vram_total_bytes": {"status": "ok", "value": 24 * 1024 ** 3},
               "gtt_total_bytes": {"status": "ok", "value": 32 * 1024 ** 3},
               "vram_used_bytes": {"status": "ok", "value": 1024 ** 3}}
        gpu = _accelerator(raw, {}, {}, [])
        self.assertEqual(gpu["memory_kind"], "VRAM")
        self.assertEqual(gpu["memory_free_bytes"], 23 * 1024 ** 3)

    def test_windows_unicode_decode_and_mojibake_repair(self):
        for name in ("Conexión", "Conexión".encode("utf-8").decode("cp1252")):
            self.assertEqual(_nic_name(name), "Conexión")
        self.assertEqual(_nic_name("Ethernet Ω"), "Ethernet Ω")
        api = API()
        data = {"schema_version": 1, "interfaces": [{"name": "Conexión", "state": "up", "negotiated_mbps": 1000}]}
        def run(argv, **kwargs):
            if "probe" in argv:
                self.assertEqual(kwargs["encoding"], "utf-8")
                self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps(data, ensure_ascii=False))
            return SimpleNamespace(returncode=1, stdout="")
        with patch("hyperloom.web.api.subprocess.run", side_effect=run):
            self.assertEqual(api._probe(SimpleNamespace(access="local"))["interfaces"][0]["name"], "Conexión")

    def test_nvidia_utilization_comes_from_smi(self):
        data = {"schema_version": 1, "devices": [{"vendor": "nvidia", "name": "Fixture NVIDIA"}]}
        def run(argv, **kwargs):
            if "probe" in argv:
                return SimpleNamespace(returncode=0, stdout=json.dumps(data))
            if argv[0] == "nvidia-smi":
                self.assertIn("utilization.gpu", argv[1])
                return SimpleNamespace(returncode=0, stdout="GPU-fixture, Fixture NVIDIA, 37, 96000, 40000, 54, 211\n")
            return SimpleNamespace(returncode=1, stdout="")
        with patch("hyperloom.web.api.subprocess.run", side_effect=run):
            device = self.api.__class__()._probe(SimpleNamespace(access="local"))["devices"][0]
        gpu = _accelerator(device, {}, {}, [])
        self.assertEqual(gpu["utilization_pct"], 37)
        self.assertEqual(gpu["temperature_c"], 54)
        self.assertEqual(gpu["power_w"], 211)

    def test_preset_parameters_resolve_port_and_engine_defaults(self):
        self.cluster.profiles["qwen-replicas"]["port"] = 18509
        profile = self.api._profiles(self.cluster, {})[0]
        parameters = {v["id"]: v for v in profile["parameters"]}
        self.assertEqual(parameters["--http-port"]["production"], 18509)
        self.assertEqual(parameters["--http-max-queue"]["production"], "engine default")
        self.assertNotIn("{port}", json.dumps(profile))
        self.assertEqual(profile["router"], "local")
        self.assertEqual(profile["devices"], ["halo-1:gpu", "halo-2:gpu"])
        self.assertEqual(profile["identity"]["receipt"], "qwen-rep-rfix")

    def test_launch_metadata_and_effective_argv_env(self):
        owned = {"release_id": "a" * 12, "nodes": ["halo-2"], "machines": {"halo-2": {}},
                 "devices": ["halo-2:gpu"], "started_at": 900, "router": {"machine": "halo-2"},
                 "process_specs": [{"machine_id": "halo-2", "role": "replica", "argv": ["/opt/engine", "--http-port", "18501"], "env": {"STRIX_COHORT_ROWS": "32"}}]}
        self.orch._read.side_effect = None
        self.orch._read.return_value = owned
        profile = self.api._profiles(self.cluster, {})[0]
        self.assertEqual((profile["release"], profile["router"], profile["nodes"], profile["devices"], profile["started_at"]),
                         ("a" * 12, "halo-2", ["halo-2"], ["halo-2:gpu"], 900))
        self.assertEqual(profile["effective_configuration"][0]["value"], owned["process_specs"][0]["argv"])
        self.assertEqual(profile["effective_configuration"][1]["value"], owned["process_specs"][0]["env"])
        self.assertEqual(profile["effective_configuration"][0]["source"], "saved launch argv/env")

    def test_snapshot_headroom_and_topology_use_summaries(self):
        self.orch.load_cluster.return_value = self.cluster
        self.orch.status.return_value = {}
        self.orch._known_hosts.return_value = FIXTURE.parent / "absent-known-hosts"
        data = self.api._snapshot()
        self.assertEqual(data["devices"]["summary"]["minimum_headroom_bytes"], 60000000000)
        self.assertEqual(data["devices"]["summary"]["memory_free_bytes"], 230673741824)
        self.assertEqual(len(data["topology"]["usb4"]), 2)
        self.assertEqual(len(data["topology"]["links"]), 7)
        self.assertEqual(sum(link["kind"] == "USB4" for link in data["topology"]["links"]), 4)
        self.assertEqual(sum(link["kind"] == "Ethernet" for link in data["topology"]["links"]), 3)

    def test_engine_status_adds_trellis_identity(self):
        self.orch.load_cluster.return_value = self.cluster
        self.orch.status.return_value = {}
        self.orch._known_hosts.return_value = FIXTURE.parent / "absent-known-hosts"
        self.api._statistics = lambda statuses, cluster=None: {"engines": [{"profile": "qwen-replicas", "status": {"memory": {"trellis_identity": "fixture-identity"}}}]}
        profile = self.api._snapshot()["profiles"]["profiles"][0]
        self.assertEqual(profile["identity"]["trellis"], ["fixture-identity"])


if __name__ == "__main__":
    unittest.main()
