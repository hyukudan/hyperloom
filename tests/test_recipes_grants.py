"""Passive placement binding and operator grants, with synthetic identities only."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom import config
from hyperloom.recipes import apply, core, devices, grants, reader, session
from tests import test_recipes
from tests.test_config import example

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()


def window(kind="device"):
    return {"starts_at": "2029-12-31T23:55:00Z", "expires_at": "2030-01-01T00:10:00Z", "kind": kind}


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.uuid = "GPU-" + str(uuid.uuid4())
        self.aux = "GPU-" + str(uuid.uuid4())
        self.recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
        self.recipe["routes"][0]["assist"]["aux"]["device_gib"] = 0
        self.grant = {"uuid": self.uuid, "vram_cap_gib": 90, "approval": "operator decision",
                      "window": window(), "gate": [sys.executable, "operator-gate.py"]}
        self.cluster = SimpleNamespace(pc={"cuda": {"grants": [self.grant], "host_grant": {
            "host_gib": 74, "approval": "operator decision", "window": window()}}})
        self.report = {"devices": [{"id": self.uuid, "uuid": self.uuid, "name": "anchor class", "kind": "cuda"},
                                    {"id": self.aux, "uuid": self.aux, "name": "assist class", "kind": "cuda"}]}
        self.binding = {"anchor": [{"id": self.uuid, "host": "local"}], "aux": []}
        self.hosts = {"local": {"free_ram_gib": grants.HOST_START_BYTES / (1 << 30)}}

    def check(self):
        return grants.check(self.cluster, self.recipe, self.binding, self.report, self.hosts, now=NOW)

    def test_exact_cap_floor_and_disabled_assist(self):
        before = deepcopy(self.recipe)
        with mock.patch.object(core.subprocess, "run", side_effect=AssertionError("no process")):
            self.assertEqual(len(self.check()), 1)
        self.assertEqual(self.recipe, before)
        self.grant["vram_cap_gib"] = 89.999
        with self.assertRaisesRegex(reader.RecipeError, "above grant"):
            self.check()
        self.assertEqual(self.recipe, before)

    def test_missing_expired_future_and_separate_assist_approval(self):
        for changes in ({"approval": ""}, {"window": {**window(), "expires_at": "2029-12-31T23:59:59Z"}},
                        {"window": {**window(), "starts_at": "2030-01-01T00:01:00Z"}}):
            saved = deepcopy(self.grant)
            self.grant.update(changes)
            with self.subTest(changes=changes), self.assertRaises((config.ConfigError, reader.RecipeError)):
                self.check()
            self.grant.clear()
            self.grant.update(saved)
        self.recipe["routes"][0]["assist"]["aux"]["device_gib"] = 8
        self.binding["aux"] = [{"id": self.aux, "host": "local"}]
        with self.assertRaisesRegex(reader.RecipeError, "no operator grant"):
            self.check()
        self.cluster.pc["cuda"]["grants"].append({**deepcopy(self.grant), "uuid": self.aux})
        self.assertEqual(len(self.check()), 2)
        self.report["devices"][1]["name"] = "RTX " + str(4000 + 90)
        self.grant["window"]["kind"] = "PC-BIG"
        with self.assertRaisesRegex(reader.RecipeError, "PC-BIG"):
            self.check()

    def test_host_grant_and_one_byte_below_memavailable(self):
        self.hosts["local"]["free_ram_gib"] -= 1 / (1 << 30)
        with self.assertRaisesRegex(reader.RecipeError, "82 GB"):
            self.check()
        self.hosts["local"]["free_ram_gib"] += 1 / (1 << 30)
        self.cluster.pc["cuda"]["host_grant"]["host_gib"] = 69
        with self.assertRaisesRegex(reader.RecipeError, "host grant"):
            self.check()
        self.cluster.pc["cuda"]["host_grant"]["host_gib"] = 75
        with self.assertRaises(config.ConfigError):
            self.check()

    def test_config_validates_grants_without_changing_legacy_reservation(self):
        obj = example()
        obj["pc"]["cuda"] = deepcopy(self.cluster.pc["cuda"])
        self.assertEqual(config.parse(obj).pc["cuda"], self.cluster.pc["cuda"])
        obj["pc"]["cuda"]["grants"].append({**deepcopy(self.grant), "uuid": self.uuid.upper()})
        with self.assertRaisesRegex(config.ConfigError, "duplicate"):
            config.parse(obj)
        for key in ("gate", "window", "approval"):
            bad = deepcopy(self.grant)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(config.ConfigError):
                grants.parse_grants([bad])


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.ids = ["GPU-" + str(uuid.uuid4()) for _ in range(4)]
        self.ctx = apply.Context("synthetic", "synthetic", "single", [], "local")
        self.declared = [{"id": f"device-{i}", "type": "nvidia", "uuid": ident,
                          "name": f"class-{i}", "memory_gib": 96 if i == 0 else 24}
                         for i, ident in enumerate(self.ids[:3])]
        self.declared[2]["excluded"] = True
        self.cluster = SimpleNamespace(machines={"local": SimpleNamespace(devices=self.declared)}, pc={})
        self.inventory = {"cuda": [{"uuid": self.ids[0], "name": "class-0", "memory_gib": 96},
                                    {"uuid": self.ids[3], "name": "unconfigured", "memory_gib": 24}]}
        self.facts = {"model": "synthetic", "devices": {}, "links": []}

    def report(self):
        return devices.candidates(self.cluster, self.ctx, self.inventory, self.facts)

    def test_all_configured_nonexcluded_candidates_present_and_absent(self):
        report = self.report()
        self.assertEqual([r["id"] for r in report["devices"]], self.ids[:2])
        self.assertEqual([r["present"] for r in report["devices"]], [True, False])
        self.declared[1]["name"] = "RTX " + str(4090)
        self.assertEqual(len(self.report()["devices"]), 2)

    def test_session_uses_core_assignment_and_never_legacy_preselection(self):
        recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
        binding = {"anchor": [{"id": self.ids[0], "host": "local"}], "aux": []}
        backend = core.CoreBackend(core.Core("cpu-core"))
        resolved = core.Resolved(recipe, "0" * 64, {"fields": []}, False, [])
        with mock.patch.object(session.facts, "context", return_value=self.ctx), \
                mock.patch.object(session, "resolve", return_value=resolved), \
                mock.patch.object(backend, "match", return_value=([], binding, "0" * 64)) as match, \
                mock.patch.object(apply, "bind", side_effect=AssertionError("legacy binding")):
            evaluation = session.evaluate(self.cluster, "synthetic", "recipe.toml", session.Layers(),
                lambda ctx: (self.inventory, self.facts, {"devices": {}, "hosts": {}, "lan": {}, "usb4": {}}, []), backend)
        self.assertEqual(evaluation.bound["binding"], binding)
        self.assertIn("no operator grant", evaluation.refusal)
        self.assertEqual(len(match.call_args.args[2]["devices"]), 2)


class StrictCoreTests(unittest.TestCase):
    report = CandidateTests.report

    def setUp(self):
        CandidateTests.setUp(self)
        executable = os.environ.get("HYPERLOOM_TS_RECIPE")
        if not executable:
            self.skipTest("set HYPERLOOM_TS_RECIPE for strict CPU binding parity")
        self.backend = core.CoreBackend(core.Core(executable))
        self.recipe = test_recipes.PlacementSchemaTests.document()
        self.recipe["match"]["devices"][0]["name"] = "class-0"
        self.recipe["match"]["devices"][1].update(name="class-1", uuid=self.ids[1])

    def bind(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            path = Path(tmp) / "recipe.json"
            path.write_text(json.dumps(self.recipe), encoding="utf-8")
            return self.backend.match(path, session.Layers(), self.report(), None)

    def test_strict_unknown_excluded_ambiguous_and_configured_absence(self):
        problems, binding, _ = self.bind()
        self.assertEqual(problems, [])
        self.assertEqual(binding["aux"], [])
        self.recipe["match"]["devices"][1]["uuid"] = self.ids[2]
        self.assertIn("PL_E_UNKNOWN_DEVICE", " ".join(self.bind()[0]))
        self.recipe["match"]["devices"][1]["uuid"] = "GPU-" + str(uuid.uuid4())
        self.assertIn("PL_E_UNKNOWN_DEVICE", " ".join(self.bind()[0]))
        self.recipe["match"]["devices"][1].pop("uuid")
        self.recipe["match"]["devices"][1]["name"] = "class-0"
        self.inventory["cuda"].append({"uuid": self.ids[1], "name": "class-0", "memory_gib": 96})
        self.assertIn("PL_E_UNKNOWN_DEVICE", " ".join(self.bind()[0]))

    def test_aliases_and_pci_order_are_core_owned(self):
        self.recipe["match"]["devices"][1].pop("uuid")
        self.recipe["match"]["devices"][1].pop("name")
        self.recipe["match"]["devices"][0].pop("name")
        self.recipe["match"]["devices"][0]["index"] = 1
        for i, device in enumerate(self.declared[:2]):
            device["pci"] = f"{0:04x}:{i + 1:02x}:{0:02x}.{0}"
        self.inventory["cuda"].append({"uuid": self.ids[1], "name": "class-1", "memory_gib": 96})
        problems, binding, _ = self.bind()
        self.assertEqual(problems, [])
        self.assertEqual(binding["anchor"][0]["id"], self.ids[1])
        self.assertEqual(binding["aux"][0]["id"], self.ids[0])
        self.declared.append({**self.declared[0], "id": "alias"})
        self.assertEqual(self.bind()[0], [])
        self.declared[-1]["pci"] = f"{0:04x}:{9:02x}:{0:02x}.{0}"
        self.assertIn("PL_E_DUPLICATE_DEVICE", " ".join(self.bind()[0]))
