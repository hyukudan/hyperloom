"""Passive topology and DRS fixtures. Machine identifiers and paths are synthesized at runtime."""
from copy import deepcopy
import ctypes as C
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom.recipes import probe, sysmem, topology
from hyperloom.recipes.reader import RecipeError


class LinuxTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # BDFs are generated at runtime, not copied from an operational machine.
        bus = uuid.uuid4().int % 200 + 1
        self.ids = [f"0000:{bus:02x}:{i:02x}.0" for i in range(3)]
        self.nodes = []
        class Node:
            def __init__(self, name, path, parents):
                self.name, self.path, self.parents = name, path, parents

            def __truediv__(self, name):
                return self.path / name

        parents = []
        for ident, speed, width in zip(self.ids, ("32.0", "8.0", "16.0"), (16, 4, 16)):
            directory = self.root / f"hop-{len(self.nodes)}"
            directory.mkdir()
            node = Node(ident, directory, list(reversed(parents)))
            parents.append(node)
            for name, value in {"max_link_speed": speed + " GT/s PCIe", "max_link_width": str(width),
                                "current_link_speed": "2.5 GT/s PCIe", "current_link_width": "1",
                                "numa_node": "0", "vendor": "0xffff", "device": "0xffff"}.items():
                (node / name).write_text(value, encoding="ascii")
            self.nodes.append(node)
        self.lookup = mock.MagicMock()
        self.lookup.__truediv__.return_value.resolve.return_value = node

    def test_full_chain_uses_narrowest_capabilities_not_idle_link(self):
        result = topology.linux(self.ids[-1], root=self.lookup)
        self.assertEqual(result["pci_path"], self.ids)
        self.assertEqual(result["link"], {"gen": 3, "width": 4})
        self.assertEqual(result["numa_node"], 0)
        self.assertEqual(result["via"], "unknown")

    def test_partial_unreadable_and_negative_numa_stay_unknown(self):
        (self.nodes[1] / "max_link_speed").unlink()
        (self.nodes[-1] / "numa_node").write_text("-1")
        result = topology.linux(self.ids[-1], root=self.lookup)
        self.assertEqual(result["link"], {"gen": "unknown", "width": 4})
        self.assertEqual(result["numa_node"], "unknown")
        self.assertEqual(topology.linux(self.ids[-1], root=self.root)["pci_path"], "unknown")

    def test_via_classification_requires_evidence_and_pci_is_normalized(self):
        rows = [{"id": "root", "gen": 4, "width": 16, "via": "direct"},
                {"id": "bridge", "gen": 3, "width": 4, "via": "chipset"},
                {"id": "endpoint", "gen": 4, "width": 16}]
        self.assertEqual(topology.summarize(rows)["via"], "chipset")
        rows[1]["via"] = "direct"
        self.assertEqual(topology.summarize(rows)["via"], "direct")
        self.assertEqual(topology.pci(self.ids[-1].replace("0000:", "00000000:")), self.ids[-1])


class WindowsTopologyTests(unittest.TestCase):
    def test_cfgmgr_properties_parent_chain_missing_and_ambiguous(self):
        bus = uuid.uuid4().int % 200 + 1
        ident = f"0000:{bus:02x}:01.0"
        api = mock.Mock()
        api.nodes.return_value = [2]
        api.parent.side_effect = lambda n: {2: 1, 1: 0, 0: None}[n]
        api.identity.side_effect = lambda n: "PCI\\VEN_FFFF&DEV_FFFF" if n else "ACPI\\root"
        properties = {(2, topology._DEVICE, 23): bus, (2, topology._DEVICE, 30): 1 << 16,
                      (1, topology._DEVICE, 23): bus - 1, (1, topology._DEVICE, 30): 2 << 16,
                      (2, topology._DEVICE, 37): ["root#port#endpoint"],
                      (1, topology._DEVICE, 37): ["root#port"],
                      (2, topology._PCI_DEVICE, 11): 4, (2, topology._PCI_DEVICE, 12): 16,
                      (1, topology._PCI_DEVICE, 11): 3, (1, topology._PCI_DEVICE, 12): 4,
                      (2, topology._NUMA, 3): 1}
        api.prop.side_effect = lambda *args: properties.get(args)
        result = topology.windows(ident, api=api)
        self.assertEqual(result["pci_path"], [f"0000:{bus - 1:02x}:02.0", ident])
        self.assertEqual(result["link"], {"gen": 3, "width": 4})
        self.assertEqual(result["numa_node"], 1)
        properties.pop((1, topology._PCI_DEVICE, 12))
        self.assertEqual(topology.windows(ident, api=api)["link"]["width"], "unknown")
        api.nodes.return_value = [2, 2]
        self.assertEqual(topology.windows(ident, api=api)["pci_path"], "unknown")
        api.nodes.side_effect = PermissionError()
        self.assertEqual(topology.windows(ident, api=api)["pci_path"], "unknown")


class SysmemTests(unittest.TestCase):
    def setUp(self):
        root = PureWindowsPath(tempfile.gettempdir())
        if not root.is_absolute():
            root = PureWindowsPath(chr(67) + ":/") / uuid.uuid4().hex
        self.exe = str(root / (uuid.uuid4().hex + ".exe"))
        self.api = mock.Mock(spec=["create", "load", "find", "settings", "destroy"])
        self.api.find.return_value = ("profile", {"name": self.exe})
        self.api.settings.return_value = [("CUDA - Sysmem Fallback Policy", 0, 2)]

    def test_read_only_profile_match_and_always_destroy_session(self):
        self.assertEqual(sysmem.read_policy(self.exe, self.api), sysmem.NO_FALLBACK)
        self.assertEqual([c[0] for c in self.api.mock_calls], ["create", "load", "find", "settings", "destroy"])
        self.api.find.return_value = ("profile", {"name": PureWindowsPath(self.exe).name})
        self.assertEqual(sysmem.read_policy(self.exe, self.api), "unknown")
        self.assertTrue(sysmem.profile_matches(self.exe, {"name": PureWindowsPath(self.exe).name,
                                                         "folder": str(PureWindowsPath(self.exe).parent)}))
        self.api.load.side_effect = OSError()
        with self.assertRaises(OSError):
            sysmem.read_policy(self.exe, self.api)
        self.assertEqual(self.api.destroy.call_count, 3)

    def test_timeout_unknown_refused_only_in_placement_and_no_legacy_query(self):
        run = mock.Mock(side_effect=subprocess.TimeoutExpired("DRS", 5))
        self.assertEqual(sysmem.probe(self.exe, run=run, platform="nt"), "unknown")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.args[0][-1], self.exe)
        with self.assertRaisesRegex(RecipeError, "PL_E_SYSMEM"):
            sysmem.require("unknown", placement=True, platform="windows-wddm")
        sysmem.require("unknown", placement=False, platform="windows-wddm")
        sysmem.require("unknown", placement=True, platform="linux")
        sysmem.require(sysmem.NO_FALLBACK, placement=True, platform="windows-wddm")
        self.assertEqual(sysmem.probe(self.exe, run=run, platform="posix"), "not_applicable")
        self.assertEqual(run.call_count, 1)

    def test_default_fallback_missing_and_malformed_values(self):
        for values, expected in (([("CUDA - Sysmem Fallback Policy", 0, 0)], "fallback_allowed"),
                                 ([("CUDA - Sysmem Fallback Policy", 0, 99)], "unknown"), ([], "unknown")):
            self.api.settings.return_value = values
            self.assertEqual(sysmem.read_policy(self.exe, self.api), expected)
        for value in ("{}", '"unexpected"', "not json"):
            run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=value))
            self.assertEqual(sysmem.probe(self.exe, run=run, platform="nt"), "unknown")


class PassiveInventoryTests(unittest.TestCase):
    def test_unavailable_extended_fields_preserve_inventory_as_unknown(self):
        ident = "GPU-" + str(uuid.uuid4())
        run = mock.Mock(side_effect=[SimpleNamespace(returncode=1, stdout="", stderr="unsupported field"),
            SimpleNamespace(returncode=1, stdout="", stderr="unsupported field"),
            SimpleNamespace(returncode=0, stdout=f"{ident}, test class, 24576, 0\n")])
        rows = probe.nvidia_gpus(run, topology_reader=lambda pci: topology.summarize([], complete=False))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["arch"], "unknown")
        self.assertEqual(rows[0]["pci_path"], "unknown")
        self.assertEqual(run.call_count, 3)

    def test_identity_arch_platform_and_probe_keys_without_measurements(self):
        ident = "GPU-" + str(uuid.uuid4())
        pci_id = f"{0:08x}:{uuid.uuid4().int % 200:02x}:{1:02x}.0"
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=
            f"{ident},  Test GPU  , 24576, 100, {pci_id}, 600.12, 12.0, WDDM\n"))
        topology_reader = mock.Mock(return_value={"pci_path": ["root", "port"], "link": {"gen": 4, "width": 4},
                                                  "via": "chipset", "numa_node": 0})
        with mock.patch.object(probe.os, "name", "nt"):
            rows = probe.nvidia_gpus(run, topology_reader=topology_reader)
        self.assertEqual(rows[0]["name"], "Test GPU")
        self.assertEqual(rows[0]["arch"], "sm_120")
        self.assertEqual(rows[0]["platform"], "windows-wddm")
        self.assertEqual(rows[0]["driver"], "600.12")
        self.assertEqual(rows[0]["link"], {"gen": 4, "width": 4})
        self.assertEqual(rows[0]["memory_bytes"], 24576 * probe.MIB)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--query-gpu=", run.call_args.args[0][1])

    def test_fact_and_candidate_bridge_preserves_topology_and_store_provenance(self):
        from hyperloom.recipes import apply, devices, facts
        ident = "GPU-" + str(uuid.uuid4())
        ctx = apply.Context("synthetic", "synthetic", "single", [], "local")
        cluster = SimpleNamespace(machines={"local": SimpleNamespace(devices=[{
            "id": "anchor", "type": "nvidia", "uuid": ident}])}, assets={}, pc={})
        topology_facts = {"pci_path": ["root", "endpoint"], "link": {"gen": 3, "width": 4},
                          "via": "chipset", "numa_node": 0, "platform": "windows-wddm"}
        gpu = {"uuid": ident, "name": "test class", "memory_gib": 96, **topology_facts}
        memory = {"total_gib": 128, "free_gib": 90, "free_commit_gib": 100}
        reader = mock.Mock(return_value=sysmem.NO_FALLBACK)
        stores = {"xfer_host": "host-class", "file_store": "store-class", "engram_store": "engram-class"}
        with mock.patch.object(probe, "read_bandwidth", side_effect=AssertionError("measurement")):
            inventory, gathered, raw, notes = facts.gather(cluster, ctx, call=mock.Mock(), gpus=lambda: [gpu],
                memory=lambda: memory, transfer_identity=stores)
        reader.assert_not_called()
        report = devices.candidates(cluster, ctx, inventory, gathered)
        self.assertEqual({k: report[k] for k in stores}, stores)
        self.assertEqual({k: report["devices"][0][k] for k in topology_facts}, topology_facts)
        self.assertNotIn("sysmem_policy", raw)
        facts.gather(cluster, ctx, call=mock.Mock(), gpus=lambda: [gpu], memory=lambda: memory,
                     release_executable="release", sysmem_reader=reader)
        reader.assert_called_once_with("release")
