"""Recipes in the orchestrator: stub reader (schema draft 2), selection, deployment decision, probes, saved header
descriptors, the recipe CLI, `up --recipe`, and the router's selector against the reader (conformance)."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import dataclasses
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import cli, modelfiles
from hyperloom.paths import repo_root
from hyperloom.recipes import apply, core, descriptors, facts, launch, probe, reader, session
from hyperloom.recipes.reader import GIB_BYTES, RecipeError, StaleProfile
from hyperloom.recipes.service import BUILTIN_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
MM = (FIXTURES / "minimax-lab.toml").read_text(encoding="utf-8")
DS = (FIXTURES / "ds41-lab.toml").read_text(encoding="utf-8")
GPU = "GPU-00000000-0000-0000-0000-000000000001"


def mm(old=None, new=None):
    return MM if old is None else MM.replace(old, new)


LIMITS = {"node_context": 33024, "node_batch": 8, "node_flags": ("--context", "--slots"), "service_positions": 32768,
          "default_generation_tokens": 1024, "prefill_batch": (1, None), "prefill_chunk": 4096}


def ctx(service=True, capabilities=("import-state-parked",), model="minimax-m3.1-flash"):
    return apply.Context("mm", model, "pair", ["n1", "n2"], "pc", capabilities=capabilities,
                         service={"machine": "pc", "device": "pro", "uuid": GPU, "engine": "ds41-rtx-prefill",
                                  "host_overhead_gib": 4.0} if service else None, limits=dict(LIMITS))


INVENTORY = {"cuda": [{"uuid": GPU, "name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6}]}
FACTS = {"model": "minimax-m3.1-flash",
         "devices": {GPU: {"name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6, "host_ram_gib": 127.7},
                     "n1": {"name": "AMD Radeon 8060S", "memory_gib": 120.0, "host_ram_gib": 125.0},
                     "n2": {"name": "AMD Radeon 8060S", "memory_gib": 120.0, "host_ram_gib": 125.0}},
         "links": [{"kind": "ethernet", "hosts": ["pc", "n1"], "gbps": 2.5},
                   {"kind": "ethernet", "hosts": ["pc", "n2"], "gbps": 2.5},
                   {"kind": "usb4", "hosts": ["n1", "n2"], "gbps": 0.0}]}


def availability(**changes):
    data = {"devices": {GPU: {"available": True}, "n1": {"available": True}, "n2": {"available": True}},
            "links": {"pc-nodes": {"up": True}, "pair.internal": {"up": True}},
            "hosts": {"pc": {"free_ram_gib": 100.0, "free_commit_gib": 150.0},
                      "n1": {"free_ram_gib": 124.0}, "n2": {"free_ram_gib": 124.0}}}
    data.update(changes)
    return data


# The MiniMax fixture's units groups are a strix-stream placement; hyperloom's service mapping takes only the budget.
PLAIN = mm('chunk_tokens = 4096\n\n[[routes.placement.rtx.units]]\nid = "layers"\nclass = "blob"\nresident = 32\n'
           'pinned = 16\nrest = "disk"\n', '')


class ReaderTests(unittest.TestCase):
    def test_fixture_parses_with_defaults(self):
        recipe = reader.parse(MM, "mm.toml")
        self.assertEqual([r["id"] for r in recipe["routes"]], ["prefill_long", "prefill_nodes", "decode_pair"])
        placement = recipe["routes"][0]["placement"]["rtx"]
        self.assertEqual((placement["staging_buffers"], placement["workspace_gib"]), (4, None))
        self.assertEqual(recipe["routes"][0]["handoff_route"], "decode_pair")
        self.assertEqual(reader.parse(json.dumps(reader.load_text(MM)))["routes"], recipe["routes"])  # JSON == TOML

    def test_refusals_name_the_path(self):
        cases = {
            'resident = 32': ('residnet = 32', 'routes.prefill_long.placement.rtx.units.layers: unknown key "residnet"'),
            'name = "minimax-m3.1-flash-lab"': ('name = "minimax-m3.1-flash-lab"\nexact = true', "exactness is not a recipe option"),
            'when = { prompt_tokens_min = 4096 }': ('when = { prompt_tokens_min = 4096.0 }', "expected an integer"),
            'handoff_route = "decode_pair"\nlinks': ('handoff_route = "prefill_nodes"\nlinks', "is not a decode route"),
            'fallback = "prefill_nodes"': ('fallback = "prefill_long"', "expected another route"),
            'links = ["pc-nodes"]': ('links = []', "links: must connect rtx, pair"),
            'device_gib = 92.0': ('device_gib = 95.0', "exceeds match.devices.rtx.memory_min_gib"),
            'host_pinned_gib = 48.0': ('host_pinned_gib = 0.0', "must be > 0 when a unit group uses the pinned tier"),
            'rest = "disk"': ('rest = "peer"', 'required exactly when rest = "peer"'),
            'execute = "tp"\nhandoff_route': ('execute = "single"\nhandoff_route', "single does not fit"),
            'memory_min_gib = 94.0': ('memory_min_gib = inf', "inf and nan"),
        }
        for old, (new, message) in cases.items():
            with self.subTest(new=new), self.assertRaisesRegex(RecipeError, message.replace("(", r"\(").replace(")", r"\)")):
                self.assertIn(old, MM)
                reader.parse(mm(old, new))

    def test_catch_all_cycles_and_unknown_references(self):
        with self.assertRaisesRegex(RecipeError, "must have an empty `when`"):
            reader.parse(mm('execute = "tp"\nhandoff_route = "decode_pair"\n',
                            'execute = "tp"\nhandoff_route = "decode_pair"\nwhen = { prompt_tokens_max = 4095 }\n'))
        with self.assertRaisesRegex(RecipeError, "no decode route"):
            reader.parse(mm('phase = "decode"', 'phase = "prefill"'))
        with self.assertRaisesRegex(RecipeError, 'duplicate id "rtx"'):
            reader.parse(mm('id = "pair"\nkind = "hip"', 'id = "rtx"\nkind = "hip"'))
        with self.assertRaisesRegex(RecipeError, "not a settable path"):
            reader.parse(mm('pin = ["routes.prefill_long.when.prompt_tokens_min"]', 'pin = ["routes.prefill_long.phase"]'))

    def test_layers_precedence_pins_and_atomicity(self):
        overrides = ('schema = "strix-stream/recipe-overrides-v1"\nrecipe = "minimax-m3.1-flash-lab"\n[set]\n'
                     '"routes.prefill_long.placement.rtx.units.layers.resident" = 33\n')
        eff = reader.resolve(MM, "mm.toml", profile=(profile_text(set_lines=(
            '"routes.prefill_long.when.prompt_tokens_min" = 2048', '"routes.prefill_long.placement.rtx.units.layers.resident" = 31')),
            "p.toml"), overrides=[(overrides, "o.toml")], sets=[reader.parse_set("limits.host_free_ram_floor_gib=6")],
            probe=identity())
        self.assertFalse(eff.untuned, eff.refused)
        long = eff.recipe["routes"][0]
        self.assertEqual(long["when"]["prompt_tokens_min"], 4096)                          # pinned: profile ignored
        self.assertEqual(long["placement"]["rtx"]["units"][0]["resident"], 33)             # override beats profile
        rows = {r["path"]: r for r in reader.explain(eff)["fields"]}
        resident = rows["routes.prefill_long.placement.rtx.units.layers.resident"]
        self.assertEqual((resident["layer"], resident["origin"]), ("override", "o.toml"))
        self.assertEqual([s["layer"] for s in resident["shadowed"]], ["recipe", "profile"])
        pinned = rows["routes.prefill_long.when.prompt_tokens_min"]
        self.assertEqual(pinned["shadowed"][0]["why"], "pinned by recipe: routes.prefill_long.when.prompt_tokens_min")
        self.assertEqual(rows["limits.host_free_ram_floor_gib"]["origin"], "--set")
        # A budget the profile was tuned with cannot be overridden on top of it (draft 5 §6.1).
        with self.assertRaisesRegex(RecipeError, "drop the profile or retune"):
            reader.resolve(MM, profile=(profile_text(set_lines=(
                '"routes.prefill_long.placement.rtx.units.layers.resident" = 31',)), "p.toml"), probe=identity(),
                sets=[("routes.prefill_long.placement.rtx.host_pinned_gib", 40.0)])
        self.assertEqual(reader.resolve(MM, profile=(profile_text(), "p.toml"), probe=identity(),
                                        sets=[("routes.prefill_long.placement.rtx.host_pinned_gib", 40.0)])
                         .recipe["routes"][0]["placement"]["rtx"]["host_pinned_gib"], 40.0)   # placement not tuned
        self.assertEqual(reader.resolve(MM, sets=[("routes.prefill_long.placement.rtx.host_pinned_gib", 40.0)])
                         .recipe["routes"][0]["placement"]["rtx"]["host_pinned_gib"], 40.0)
        # A layer that would make the recipe invalid is refused whole.
        with self.assertRaisesRegex(RecipeError, "--set: refused"):
            reader.resolve(MM, sets=[("routes.prefill_long.when.prompt_tokens_min", 5000),
                                     ("routes.prefill_long.when.prompt_tokens_max", 4500)])
        with self.assertRaisesRegex(RecipeError, "not a settable path"):
            reader.resolve(MM, sets=[("routes.prefill_long.execute", "tp")])

    def test_stale_profiles_run_untuned(self):
        cases = [
            (profile_text(recipe_sha="0" * 64), identity(), "another version of the recipe file"),
            (profile_text(), None, "no identity"),
            (profile_text(), identity(workload="decode-chat"), "tuned for workload default"),
            (profile_text(), identity(drivers="581.02"), "key.drivers.rtx is 580.88, probed 581.02"),
            (profile_text(), identity(disk=5.0), "6.8 at tuning, 5 now"),
            (profile_text(), identity(descriptor="1" * 64), "key.descriptors"),
        ]
        for text, ident, why in cases:
            with self.subTest(why=why):
                eff = reader.resolve(MM, profile=(text, "p.toml"), probe=ident)
                self.assertTrue(eff.untuned)
                self.assertIn(why, eff.refused[0])
                self.assertEqual(eff.recipe["limits"]["host_free_ram_floor_gib"], 8.0)
        self.assertFalse(reader.resolve(MM, profile=(profile_text(), "p.toml"), probe=identity(disk=6.5)).untuned)
        with self.assertRaisesRegex(RecipeError, "workload: required"):
            reader.resolve(MM, profile=(profile_text().replace('workload = "default"\n', ""), "p.toml"), probe=identity())
        with self.assertRaisesRegex(RecipeError, r"key.drivers: one entry for every set"):
            reader.resolve(MM, profile=(profile_text().replace(', pair = "6.4.3"', ""), "p.toml"), probe=identity())
        with self.assertRaisesRegex(RecipeError, 'is for "other"'):
            reader.resolve(MM, overrides=[('schema = "strix-stream/recipe-overrides-v1"\nrecipe = "other"\n[set]\n', "o")])


class PlacementSchemaTests(unittest.TestCase):
    """Schema only: no binding, sizing arithmetic, probes or engine processes."""

    @staticmethod
    def document():
        return {"schema": reader.SCHEMA, "name": "placement-schema", "match": {"model": "synthetic", "devices": [
            {"id": "anchor", "kind": "cuda", "memory_min_gib": 94},
            {"id": "aux", "kind": "cuda", "memory_min_gib": 8, "optional": True}]}, "routes": [
                {"id": "decode", "phase": "decode", "devices": ["anchor"], "execute": "single",
                 "max_rows": 4, "max_sequences": 1, "assist": {"aux": {"device_gib": 8}},
                 "components": {"drafter": {"device": "aux"}, "arena": {"units": 8192}},
                 "placement": {"anchor": {"device_gib": 90, "max_context_tokens": 32768, "max_batch": 1,
                                          "host_pinned_gib": 68, "host_pageable_gib": 2,
                                          "host_available_floor_gib": 20}}}]}

    def parse(self, doc=None):
        return reader.parse(json.dumps(self.document() if doc is None else doc))

    def test_placement_defaults_and_optional_key_omission(self):
        recipe = self.parse()
        route = recipe["routes"][0]
        self.assertTrue(reader.placement_mode(route))
        self.assertEqual(route["assist"]["aux"], {"device_gib": 8.0, "margin_gib": 0.5, "margin_fraction": 0.03})
        self.assertEqual(route["components"]["arena"], {"device": "auto", "units": 8192})
        self.assertEqual(route["placement"]["anchor"]["margin_gib"], 2.0)
        self.assertNotIn("optional", recipe["match"]["devices"][0])
        doc = self.document()
        doc["routes"][0]["assist"]["aux"] = {}
        self.assertEqual(self.parse(doc)["routes"][0]["assist"]["aux"]["device_gib"], 0)

    def test_selectors_and_optional_binding_order(self):
        doc = self.document()
        doc["match"]["devices"].insert(0, {"id": "z_aux", "kind": "hip", "memory_min_gib": 8, "optional": True})
        aux = doc["match"]["devices"][-1]
        # Synthetic selector identities, never measurements from a local machine.
        aux.update(uuid=GPU, pci="0000:06:00.0", index=1)
        parsed = self.parse(doc)
        self.assertEqual([d["id"] for d in parsed["match"]["devices"]], ["anchor", "aux", "z_aux"])
        self.assertEqual({k: parsed["match"]["devices"][1][k] for k in ("uuid", "pci", "index")},
                         {"uuid": GPU, "pci": "0000:06:00.0", "index": 1})

    def test_activation_requires_a_non_empty_assist_or_components_table(self):
        # Contract rev 21c: empty tables (or ones holding only x- notes) carry no placement content and stay legacy.
        for tables in ({}, {"assist": {}, "components": {}}, {"components": {"x-note": {}}}):
            doc = self.document()
            route = doc["routes"][0]
            for key in ("assist", "components", "max_rows", "max_sequences"):
                route.pop(key)
            route.update(tables)
            self.assertFalse(reader.placement_mode(self.parse(doc)["routes"][0]))
            route["max_rows"] = 1
            with self.assertRaisesRegex(RecipeError, "PL_E_SCHEMA"):
                self.parse(doc)

    def test_legacy_canonical_bytes_and_alias_schemas(self):
        # Frozen with a8d112c reader on these portable fixtures; shipped recipe hashes stay unchanged.
        for text, sha in ((DS, "301a9b097f955aecd2c475f9f0d3008bac6a96ded86a5544db3067fbf9bd38eb"),
                          (MM, "fa68cc0060695ec0ed88a56cc16686a1767e30f27431a6791e4bf55f1bd33c29")):
            self.assertEqual(reader.resolve(text).canonical_sha256(), sha)
            self.assertEqual(reader.resolve(text.replace("strix-stream/", "tierstream/")).canonical_sha256(), sha)
        shipped = {
            "deepseek-v4.1-flash.lab-hetero.toml": "626cbf6149d6fa31c893e10b04dc74b8f15b8a3859ba51d8f7635534e5cc606a",
            "deepseek-v4.1-flash.lab.toml": "f67c89a1babda15c17a2bafc16e02d01da105c8f2e13d577491068cdb900ae8c",
            "minimax-m3.1-flash.lab.toml": "03aa4745b2a3d0580ab4e4bef37969c575d3c1a645c39b7c9b8f988d2a0fd8ec",
            "qwen3.8-flash-next.lab.toml": "3d77634953f4855e8523ccc50e20dae131b7f9eb4a17258e995bb12cfc183bc0",
        }
        for name, sha in shipped.items():
            text = (repo_root() / "hyperloom/recipes/examples" / name).read_text(encoding="utf-8")
            self.assertEqual(reader.resolve(text).canonical_sha256(), sha)
        # Renamed layer schemas are compatible too; the old output identity stays unchanged.
        layer = {"schema": "tierstream/recipe-overrides-v1", "recipe": reader.parse(MM)["name"],
                 "set": {"limits.host_free_ram_floor_gib": 9}}
        self.assertEqual(reader.resolve(MM, overrides=[(json.dumps(layer), "alias")]).recipe["limits"]
                         ["host_free_ram_floor_gib"], 9)
        tuned = reader.resolve(MM, profile=(profile_text().replace("strix-stream/", "tierstream/"), "alias"),
                               probe=identity())
        self.assertFalse(tuned.untuned, tuned.refused)

    def test_zero_extension_fields_and_legacy_reserved_ids(self):
        doc = self.document()
        route = doc["routes"][0]
        for key in ("assist", "components", "max_rows", "max_sequences"):
            route.pop(key)
        route["placement"]["anchor"]["host_pageable_gib"] = 0
        route["placement"]["anchor"]["host_available_floor_gib"] = 0
        doc["match"]["devices"][1]["optional"] = False
        doc["match"]["devices"].append({"id": "auto", "kind": "cuda", "memory_min_gib": 1})
        parsed = self.parse(doc)
        self.assertFalse(reader.placement_mode(parsed["routes"][0]))
        self.assertNotIn("optional", parsed["match"]["devices"][1])
        for key in reader.HOST:
            self.assertNotIn(key, parsed["routes"][0]["placement"]["anchor"])
        self.assertNotIn(reader.ABSENT, reader.placement_defaults().values())

    @staticmethod
    def rejection_cases():
        base = PlacementSchemaTests.document()
        changes = [
            (("match", "devices", 1, "uuid"), "GPU-BAD", "PL_E_SCHEMA"),
            (("match", "devices", 1, "pci"), "0000:AF:00.0", "PL_E_SCHEMA"),
            (("match", "devices", 1, "pci"), "0000:06:00.8", "PL_E_SCHEMA"),
            (("match", "devices", 1, "index"), -1, "index"),
            (("match", "devices", 1, "index"), True, "index"),
            (("match", "devices", 1, "optional"), 1, "optional"),
            (("match", "devices", 1, "kind"), "cpu", "PL_E_SCHEMA"),
            (("match", "devices", 1, "count"), 2, "PL_E_SCHEMA"),
            (("match", "devices", 0, "optional"), True, "PL_E_SCHEMA"),
            (("routes", 0, "assist"), {"missing": {}}, "PL_E_UNKNOWN_DEVICE"),
            (("routes", 0, "assist"), {"anchor": {}}, "PL_E_SCHEMA"),
            (("routes", 0, "assist", "aux", "device_gib"), -1, "device_gib"),
            (("routes", 0, "assist", "aux", "margin_gib"), 65, "margin_gib"),
            (("routes", 0, "assist", "aux", "margin_fraction"), 0.51, "margin_fraction"),
            (("routes", 0, "assist", "aux", "exact"), True, "exactness"),
            (("routes", 0, "components"), {"unknown": {}}, "PL_E_UNKNOWN_COMPONENT"),
            (("routes", 0, "components", "drafter", "device"), "missing", "PL_E_UNKNOWN_DEVICE"),
            (("routes", 0, "components", "drafter", "gib"), 1, "PL_E_SCHEMA"),
            (("routes", 0, "components", "drafter", "units"), 1, "PL_E_SCHEMA"),
            (("routes", 0, "components", "arena", "gib"), 1, "PL_E_SCHEMA"),
            (("routes", 0, "components", "arena", "units"), -1, "units"),
            (("routes", 0, "components", "arena", "units"), 2 ** 32 + 1, "units"),
            (("routes", 0, "max_rows"), 0, "max_rows"),
            (("routes", 0, "max_sequences"), 65537, "max_sequences"),
            (("routes", 0, "placement", "anchor", "host_pageable_gib"), -1, "host_pageable_gib"),
            (("routes", 0, "placement", "anchor", "host_available_floor_gib"), "20", "host_available_floor_gib"),
        ]
        for path, value, message in changes:
            doc = deepcopy(base)
            target = doc
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            yield ".".join(map(str, path)), doc, message
        doc = deepcopy(base)
        doc["match"]["devices"][0]["count"] = 2
        doc["routes"][0]["execute"] = "tp"
        yield "execute", doc, "PL_E_SCHEMA"
        doc = deepcopy(base)
        doc["routes"][0]["assist"] = {}
        yield "unlisted assist destination", doc, "PL_E_UNKNOWN_DEVICE"
        for word in reader.DESTINATIONS:
            doc = deepcopy(base)
            doc["match"]["devices"].append({"id": word, "kind": "cuda", "memory_min_gib": 1})
            yield "reserved " + word, doc, "PL_E_SCHEMA"

    def test_rejections(self):
        for name, doc, message in self.rejection_cases():
            with self.subTest(name=name), self.assertRaisesRegex(RecipeError, message):
                self.parse(doc)

    def test_layers_are_a_capability_refusal(self):
        doc = self.document()
        doc["routes"][0]["components"]["dense"] = {"layers": [[0, 2], 4]}
        with self.assertRaisesRegex(RecipeError, "layers: PL_E_CAPABILITY"):
            self.parse(doc)

    def test_schema_extensions_cannot_launch_through_the_legacy_adapter(self):
        doc = self.document()
        with mock.patch.object(apply, "requirements", side_effect=AssertionError("must refuse before planning")):
            with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY.*schema only"):
                apply.deploy(self.parse(doc), None, None, None, "0" * 64)
            doc["match"]["devices"][1].pop("optional")
            with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY.*schema only"):
                apply.deploy(self.parse(doc), None, None, None, "0" * 64)
            route = doc["routes"][0]
            for key in ("assist", "components", "max_rows", "max_sequences"):
                route.pop(key)
            with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY.*schema only"):
                apply.deploy(self.parse(doc), None, None, None, "0" * 64)
            for key in reader.HOST:
                route["placement"]["anchor"].pop(key)
            for key, value in (("index", 0), ("pci", "0000:06:00.0"), ("uuid", GPU)):
                doc["match"]["devices"][1][key] = value
                with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY.*schema only"):
                    apply.deploy(self.parse(doc), None, None, None, "0" * 64)
                doc["match"]["devices"][1].pop(key)
        apply.refuse_unsupported(self.parse(doc))

    def test_all_component_ids_destinations_and_elastic_quotas(self):
        for ident in reader.COMPONENT_IDS:
            for destination in sorted(reader.DESTINATIONS | {"anchor", "aux"}):
                doc = self.document()
                value = {"device": destination}
                if ident in reader.ELASTIC_IDS:
                    value["gib"] = 0.25
                doc["routes"][0]["components"] = {ident: value}
                with self.subTest(component=ident, destination=destination):
                    self.assertEqual(self.parse(doc)["routes"][0]["components"][ident], value)

    def test_tierstream_parser_and_canonical_parity(self):
        executable = os.environ.get("HYPERLOOM_TS_RECIPE") or shutil.which("ts-recipe")
        if not executable:
            self.skipTest("set HYPERLOOM_TS_RECIPE to a placement-capable Tierstream CLI for CPU parity")
        with tempfile.TemporaryDirectory(prefix="hl-schema-") as tmp:
            path = Path(tmp) / "recipe.json"

            def validate(doc):
                path.write_text(json.dumps(doc), encoding="utf-8")
                return subprocess.run([executable, "validate", str(path)], capture_output=True, text=True, timeout=30)

            docs = [self.document(), reader.load_text(DS), reader.load_text(MM)]
            selectors = self.document()
            selectors["match"]["devices"][1].update(uuid=GPU, pci="0000:06:00.0", index=1)
            docs.append(selectors)
            for ident in reader.COMPONENT_IDS:
                doc = self.document()
                value = {"device": "auto"}
                if ident in reader.ELASTIC_IDS:
                    value["gib"] = 0.25
                doc["routes"][0]["components"] = {ident: value}
                docs.append(doc)
            for doc in docs:
                result = validate(doc)
                self.assertEqual(result.returncode, 0, result.stderr)
                canonical = json.loads(result.stdout)
                # Re-read the authoritative canonical form into Hyperloom's established normalized shape.
                left, right = self.parse(doc), self.parse(canonical)
                left.pop("_written")
                right.pop("_written")
                self.assertEqual(left, right)
                again = validate(canonical)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(again.stdout, result.stdout)
            for name, doc, message in self.rejection_cases():
                with self.subTest(name=name):
                    result = validate(doc)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(message, result.stderr)


def profile_text(recipe_sha=None, set_lines=('"limits.host_free_ram_floor_gib" = 4',)):
    sha = recipe_sha or __import__("hashlib").sha256(MM.encode()).hexdigest()
    return ('schema = "strix-stream/recipe-profile-v1"\nrecipe = "minimax-m3.1-flash-lab"\n'
            f'recipe_sha256 = "{sha}"\nsource = "tune"\nworkload = "default"\n'
            f'[key]\nengine_sha256 = "{"a" * 64}"\nmodel_sha256 = "{"b" * 64}"\n'
            f'devices = {{ rtx = ["{GPU}"], pair = ["n1", "n2"] }}\n'
            'drivers = { rtx = "580.88", pair = "6.4.3" }\n'
            f'descriptors = {{ rtx = ["{"0" * 64}"], pair = ["{"0" * 64}", "{"0" * 64}"] }}\n'
            '[probe.rtx]\ndisk_read_gbytes_ps = 6.8\nh2d_gbytes_ps = 25.1\n[probe.pair]\ndisk_read_gbytes_ps = 5.9\n'
            '[set]\n' + "\n".join(set_lines) + "\n")


def identity(workload="default", drivers="580.88", disk=6.8, descriptor="0" * 64):
    return {"engine_sha256": "a" * 64, "model_sha256": "b" * 64, "workload": workload,
            "binding": {"rtx": [{"id": GPU, "host": "pc"}], "pair": [{"id": "n1", "host": "n1"}, {"id": "n2", "host": "n2"}]},
            "descriptors": {"rtx": [descriptor], "pair": ["0" * 64, "0" * 64]},
            "devices": [{"id": GPU, "driver": drivers, "disk_read_gbytes_ps": disk, "h2d_gbytes_ps": 25.0},
                        {"id": "n1", "driver": "6.4.3", "disk_read_gbytes_ps": 5.9},
                        {"id": "n2", "driver": "6.4.3", "disk_read_gbytes_ps": 6.0}]}


BINDING = {"rtx": [{"id": GPU, "host": "pc"}], "pair": [{"id": "n1", "host": "n1"}, {"id": "n2", "host": "n2"}]}
SHA = "e" * 64


def compiled(recipe, context=None):
    """Requirements and running credits as hyperloom compiles them (cap model), for the bound fixture devices."""
    bound = {"binding": BINDING, "roles": {"rtx": "service", "pair": "decode"}, "why": {}}
    reqs = apply.requirements(recipe, SHA, context or ctx(), bound)
    running = [apply.running_entry(r, SHA, reqs) for r in ("decode_pair", "prefill_nodes")]
    return reqs, running


def select_state(recipe, tokens=8192, **changes):
    reqs, running = compiled(recipe)
    data = {"phase": "prefill", "prompt_tokens": tokens, "context_tokens": tokens + 1024, "batch": 1,
            "binding": BINDING, "descriptors": {"rtx": ["0" * 64], "pair": ["0" * 64, "0" * 64]},
            "devices": {GPU: {"available": True}, "n1": {"available": True}, "n2": {"available": True}},
            "links": {"pc-nodes": {"up": True}, "pair.internal": {"up": True}},
            "hosts": {"pc": {"free_ram_gib": 70.0}, "n1": {"free_ram_gib": 20.0}, "n2": {"free_ram_gib": 20.0}},
            "running": running, "requirements": reqs}
    data.update(changes)
    return data


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.recipe = reader.parse(MM)

    def pick(self, **changes):
        tokens = changes.pop("tokens", 8192)
        return reader.select(self.recipe, select_state(self.recipe, tokens, **changes), SHA)

    def test_threshold_fallback_and_trace(self):
        self.assertEqual(self.pick(tokens=4095)["route"], "prefill_nodes")
        result = self.pick(tokens=4096)
        self.assertEqual((result["route"], result["handoff_route"], result["charge"]),
                         ("prefill_long", "decode_pair", {"pc": 53 * GIB_BYTES}))
        result = self.pick(devices={GPU: {"available": False, "why": "PRO busy"}, "n1": {"available": True},
                                    "n2": {"available": True}})
        self.assertEqual(result["route"], "prefill_nodes")
        self.assertEqual(result["trace"][0]["verdict"], "not admitted")
        self.assertIn("PRO busy", result["trace"][0]["why"])

    def test_ram_charge_running_credit_and_plan_binding(self):
        low = {"hosts": {"pc": {"free_ram_gib": 55.0}, "n1": {"free_ram_gib": 20.0}, "n2": {"free_ram_gib": 20.0}}}
        # 48 pinned + 1 staging + 4 overhead + floor 8 > 55; the nodes keep their floor too (running, charged 0).
        nodes_low = {"hosts": {"pc": {"free_ram_gib": 70.0}, "n1": {"free_ram_gib": 5.0}, "n2": {"free_ram_gib": 20.0}}}
        with self.assertRaisesRegex(RecipeError, "host n1: free RAM 5.0 GiB - charge 0.0 GiB < floor 8"):
            self.pick(**nodes_low)
        with self.assertRaisesRegex(RecipeError, "host n2: free RAM unknown"):
            self.pick(hosts={"pc": {"free_ram_gib": 70.0}, "n1": {"free_ram_gib": 20.0}})
        self.assertIn("charge 53.0 GiB", self.pick(**low)["trace"][0]["why"])
        reqs, running = compiled(self.recipe)
        credited = running + [apply.running_entry("prefill_long", SHA, reqs)]
        self.assertEqual(self.pick(running=credited, **low)["route"], "prefill_long")
        stale = [dict(entry, plan_ids=["0" * 64]) for entry in credited]       # credits for other plans count nothing
        with self.assertRaisesRegex(RecipeError, "no admissible prefill route"):
            self.pick(running=stale, **low)

    def test_requirements_must_match_recipe_device_and_descriptor(self):
        reqs, _ = compiled(self.recipe)
        for change in ({"effective_sha256": "0" * 64}, {"id": "GPU-other"}, {"descriptor_sha256": "1" * 64}):
            bad = [dict(r, **change) if r["route"] == "prefill_long" else r for r in reqs]
            result = self.pick(requirements=bad)
            self.assertEqual(result["route"], "prefill_nodes")
            self.assertIn("not compiled for this recipe", result["trace"][0]["why"])

    def test_bounds_internal_link_missing_state_session(self):
        with self.assertRaisesRegex(RecipeError, "context 40000 > max_context_tokens 32768 on rtx.*on pair"):
            self.pick(context_tokens=40000)
        with self.assertRaisesRegex(RecipeError, "pair.internal"):
            self.pick(links={"pc-nodes": {"up": True}})
        with self.assertRaisesRegex(RecipeError, "missing from the state"):
            self.pick(devices={})
        self.assertEqual(self.pick(tokens=4, session_route="prefill_long")["route"], "prefill_long")
        with self.assertRaisesRegex(RecipeError, "session route"):
            self.pick(session_route="prefill_long", links={})


class ApplyTests(unittest.TestCase):
    def deploy(self, text=PLAIN, context=None, avail=None, inventory=INVENTORY):
        recipe = reader.parse(text)
        context = context or ctx()
        bound = apply.bind(recipe, context, inventory)
        return recipe, bound, apply.deploy(recipe, context, bound, avail or availability(), SHA)

    def test_bind_report_and_match_messages(self):
        recipe = reader.parse(MM)
        bound = apply.bind(recipe, ctx(), INVENTORY)
        self.assertEqual(bound["binding"], BINDING)
        report = apply.probe_report(recipe, bound, FACTS)
        self.assertEqual([d["id"] for d in report["devices"]], [GPU, "n1", "n2"])
        self.assertEqual(apply.match(recipe, report, bound), [])
        small = deepcopy(FACTS)
        small["devices"][GPU].update(name="NVIDIA GeForce RTX 4090", memory_gib=23.6)
        small["links"] = small["links"][:1]
        problems = apply.match(recipe, apply.probe_report(recipe, bound, small), bound)
        self.assertTrue(any('name does not contain "RTX PRO 6000"' in p and "memory 23.60 GiB < 94" in p for p in problems))
        self.assertTrue(any("match.links.pc-nodes" in p and "n2" in p for p in problems))
        with self.assertRaisesRegex(RecipeError, "count 2, but profile mm runs 3"):
            apply.bind(recipe, apply.Context("mm", "m", "replicas", ["a", "b", "c"], "pc"), INVENTORY)

    def test_disk_minimum_without_measurement_says_how_to_measure(self):
        recipe = reader.parse(mm('host_ram_min_gib = 120.0', 'host_ram_min_gib = 120.0\ndisk_read_gbytes_ps_min = 5.0'))
        bound = apply.bind(recipe, ctx(), INVENTORY)
        problems = apply.match(recipe, apply.probe_report(recipe, bound, FACTS), bound)
        self.assertIn("recipe probe <profile> --disks", problems[0])

    def test_service_starts_with_mapped_flags_and_running_routes(self):
        _, _, decision = self.deploy()
        self.assertEqual(decision["service"], {"start": True, "why": None,
                                               "flags": {"vram_cap_gib": 92.0, "host_gib": 48.0}})
        self.assertEqual([r["route"] for r in decision["running"]], ["decode_pair", "prefill_nodes"])
        long = [r for r in decision["requirements"] if r["route"] == "prefill_long"]
        self.assertEqual([r["host_bytes"] for r in long], [53 * GIB_BYTES])              # 48 + 1 staging + 4 overhead
        self.assertEqual(len({r["plan_id"] for r in decision["requirements"]}), len(decision["requirements"]))

    def test_skips_never_start_the_service(self):
        cases = [
            (availability(devices={GPU: {"available": False, "why": "RTX PRO busy: 1 job"},
                                   "n1": {"available": True}, "n2": {"available": True}}), "RTX PRO busy"),
            (availability(links={"pc-nodes": {"up": False, "why": "n1: no physical interface up"},
                                 "pair.internal": {"up": True}}), "pc-nodes"),
            (availability(hosts={"pc": {"free_ram_gib": 55.0, "free_commit_gib": 150.0},
                                 "n1": {"free_ram_gib": 124.0}, "n2": {"free_ram_gib": 124.0}}), "< floor 8 GiB"),
        ]
        for avail, why in cases:
            with self.subTest(why=why):
                _, _, decision = self.deploy(avail=avail)
                self.assertFalse(decision["service"]["start"])
                self.assertIn(why, decision["service"]["why"])
                self.assertTrue(next(p for p in decision["prefill"] if p["id"] == "prefill_nodes")["admitted"])
        _, _, decision = self.deploy(context=ctx(service=False))
        self.assertEqual(decision["service"]["why"], "the profile has no prefill service")

    def test_refusals_before_launch(self):
        with self.assertRaisesRegex(RecipeError, "units cannot be applied to the ds41-rtx-prefill service"):
            self.deploy(MM.replace("chunk_tokens = 4096\n", "", 1))
        with self.assertRaisesRegex(RecipeError, "chunk_tokens, units cannot be applied"):
            self.deploy(MM)
        with self.assertRaisesRegex(RecipeError, "no parked import contract"):
            self.deploy(context=ctx(capabilities=()))
        with self.assertRaisesRegex(RecipeError, "batch predicates on prefill routes"):
            self.deploy(PLAIN.replace("when = { prompt_tokens_min = 4096 }", "when = { prompt_tokens_min = 4096, batch_max = 2 }"))
        two = PLAIN.replace('[[routes]]\nid = "decode_pair"', '[[routes]]\nid = "decode_big"\nphase = "decode"\n'
                            'devices = ["pair"]\nexecute = "tp"\nwhen = { batch_min = 4 }\n'
                            '[routes.placement.pair]\nmax_context_tokens = 32768\nmax_batch = 8\nstaging_buffers = 8\n'
                            '\n[[routes]]\nid = "decode_pair"')
        with self.assertRaisesRegex(RecipeError, "chooses decode by workload"):
            self.deploy(two)
        local = PLAIN.replace('max_batch = 1\nchunk_tokens = 4096\n', 'max_batch = 1\nchunk_tokens = 4096\nstaging_mib = 128\n')
        self.assertNotEqual(local, PLAIN)
        with self.assertRaisesRegex(RecipeError, "same \"pair\" processes with different placements \\(staging_mib\\)"):
            self.deploy(local)

    def test_local_prefill_field_rules(self):
        # R3 (design-r14 v3, Sol-10): one rule per field for a prefill route run by the decode processes.
        prefill = 'max_context_tokens = 32768\nmax_batch = 1\nchunk_tokens = 4096\n'
        self.assertIn(prefill, PLAIN)
        self.deploy(PLAIN)                                                                  # equal to the engine facts
        cases = [
            (prefill.replace("max_batch = 1", "max_batch = 2"), "max_batch: 2 != the 1 prompt"),
            (prefill.replace("max_context_tokens = 32768", "max_context_tokens = 33000"),
             "max_context_tokens: 33000 > the decode route's 32768"),
            (prefill.replace("chunk_tokens = 4096\n", ""), r"chunk_tokens: 0 \(whole prompt\) != the engine's prefill "
                                                           "pieces of 4096"),
            (prefill.replace("chunk_tokens = 4096", "chunk_tokens = 2048"), "chunk_tokens: 2048 != the engine's"),
        ]
        for text, why in cases:
            with self.subTest(why=why), self.assertRaisesRegex(RecipeError, why):
                self.deploy(PLAIN.replace(prefill, text))
        # An engine that enforces 8 prompts per prefill: a recipe bound of 1 is not enforced, so it refuses too.
        with self.assertRaisesRegex(RecipeError, "max_batch: 1 != the 8 prompt"):
            self.deploy(context=dataclasses.replace(ctx(), limits=dict(LIMITS, prefill_batch=(8, None))))
        for limits, why in ((dict(LIMITS, prefill_batch=(None, "known for engine 9211b07e13c6, the preset runs abc")),
                             "max_batch: known for engine 9211b07e13c6"),
                            (dict(LIMITS, prefill_chunk=None), "chunk_tokens: the engine's prefill piece size is unknown")):
            with self.subTest(why=why), self.assertRaisesRegex(RecipeError, why):
                self.deploy(context=dataclasses.replace(ctx(), limits=limits))

    def test_prefill_facts_are_bound_to_the_engine_build_and_mode(self):
        fact = facts.FAMILY_PREFILL_BATCH["ds41-pair"]
        data = {"engine_sha": fact["engine_sha"], "argv": ["{binary}", "--prefill-path", "device"]}
        self.assertEqual(facts._prefill_batch("ds41-pair", {}, data), (1, None))
        self.assertIn("holds for --prefill-path device only",
                      facts._prefill_batch("ds41-pair", {}, dict(data, argv=["--prefill-path", "rows"]))[1])
        self.assertIn("the preset runs", facts._prefill_batch("ds41-pair", {}, dict(data, engine_sha="0" * 40))[1])
        self.assertIn("no cited prefill concurrency", facts._prefill_batch("qwen-replicas", {}, data)[1])

    def test_node_workspace_is_checked_against_the_declaration(self):
        # R4: no launch flag sets it; it must equal the engine's declared runtime on every deployed member.
        text = PLAIN.replace('max_context_tokens = 32768\nmax_batch = 8\n',
                             'max_context_tokens = 32768\nmax_batch = 8\nworkspace_gib = 7.6\n')
        text = text.replace('max_batch = 1\nchunk_tokens = 4096\n', 'max_batch = 1\nchunk_tokens = 4096\nworkspace_gib = 7.6\n')
        self.assertEqual(text.count("workspace_gib = 7.6"), 2)
        cases = [({}, "n1: no engine memory declaration"),
                 ({"n1": {"gib": 7.6}, "n2": {"why": "no engine memory declaration for this exact launch"}},
                  "n2: no engine memory declaration for this exact launch"),
                 ({"n1": {"gib": 7.6}, "n2": {"gib": 7.7}}, r"7.6 GiB != the 7.70 GiB the engine declares on n2")]
        for runtime, why in cases:
            with self.subTest(why=why), self.assertRaisesRegex(RecipeError, why):
                self.deploy(text, context=dataclasses.replace(ctx(), limits=dict(LIMITS, node_runtime=runtime)))
        self.deploy(text, context=dataclasses.replace(ctx(), limits=dict(LIMITS, node_runtime={
            "n1": {"gib": 7.62}, "n2": {"gib": 7.58}})))                                  # within 0.05 GiB
        low = availability(hosts={"pc": {"free_ram_gib": 100.0}, "n1": {"free_ram_gib": 100.0},
                                  "n2": {"free_ram_gib": 124.0}})
        with self.assertRaisesRegex(RecipeError, r"no admissible decode route \(decode_pair: host n1"):
            self.deploy(avail=low)
        big = PLAIN.replace("max_context_tokens = 32768\nmax_batch = 1\ndevice_gib",
                            "max_context_tokens = 65536\nmax_batch = 1\ndevice_gib")
        with self.assertRaisesRegex(RecipeError, "max_context_tokens: 65536 > the service's 32768 positions"):
            self.deploy(big, context=dataclasses.replace(ctx(), limits=dict(LIMITS, service_positions=32768)))
        with self.assertRaisesRegex(RecipeError, "max_batch: 8 > the engine's --slots 4"):
            self.deploy(context=dataclasses.replace(ctx(), limits=dict(LIMITS, node_batch=4)))
        with self.assertRaisesRegex(RecipeError, "does not set --context and hyperloom does not know"):
            self.deploy(context=dataclasses.replace(ctx(), limits=dict(LIMITS, node_context=None)))
        with self.assertRaisesRegex(RecipeError, "runs one prompt at a time"):
            self.deploy(PLAIN.replace("max_context_tokens = 32768\nmax_batch = 1\ndevice_gib",
                                      "max_context_tokens = 32768\nmax_batch = 2\ndevice_gib"))

    def test_planned_disk_tier_reaches_the_table_and_admission(self):
        eff = reader.resolve(PLAIN)
        bound = apply.bind(eff.recipe, ctx(), INVENTORY)
        decision = apply.deploy(eff.recipe, ctx(), bound, availability(), SHA)
        decision["requirements"] = apply.with_disk(decision["requirements"], {"prefill_long"}, "rtx", 6 * GIB_BYTES, SHA)
        table = apply.route_table(eff.to_json(), eff.sha256, bound, decision, "s.json", ctx())
        self.assertEqual([r["disk_members"] for r in table["routes"]], [[GPU], []])
        long = [r for r in decision["requirements"] if r["route"] == "prefill_long"][0]
        self.assertEqual(long["disk_bytes"], 6 * GIB_BYTES)
        state = select_state(eff.recipe, requirements=decision["requirements"],
                             running=compiled(eff.recipe)[1] + [apply.running_entry("prefill_long", SHA, decision["requirements"])])
        result = reader.select(eff.recipe, state, SHA)
        self.assertEqual((result["route"], result["trace"][0]["why"]), ("prefill_nodes", 'set "rtx" member ' + GPU + ": disk unavailable"))
        state["devices"][GPU]["disk_available"] = True
        self.assertEqual(reader.select(eff.recipe, state, SHA)["route"], "prefill_long")

    def test_generation_default_must_be_known(self):
        with self.assertRaisesRegex(RecipeError, "default generation budget is unknown"):
            self.deploy(context=dataclasses.replace(ctx(), limits=dict(LIMITS, default_generation_tokens=None)))

    def test_route_table_is_the_effective_prefill_routes(self):
        eff = reader.resolve(PLAIN, sets=[("routes.prefill_long.when.prompt_tokens_min", 3072)])
        bound = apply.bind(eff.recipe, ctx(), INVENTORY)
        decision = apply.deploy(eff.recipe, ctx(), bound, availability(), eff.canonical_sha256())
        table = apply.route_table(eff.to_json(), eff.sha256, bound, decision, "state.json", ctx())
        self.assertEqual(table["schema"], "tensormeld/prefill-route-table-v1")
        self.assertEqual([(r["id"], r["place"], r["when"]["prompt_tokens_min"]) for r in table["routes"]],
                         [("prefill_long", "offload", 3072), ("prefill_nodes", "local", 0)])
        self.assertEqual(table["routes"][0]["sets"], ["rtx", "pair"])
        self.assertEqual(table["routes"][0]["links"], ["pc-nodes", "pair.internal"])
        self.assertEqual(table["routes"][0]["max_context_tokens"], 32768)
        self.assertEqual(table["routes"][0]["charges"], {"pc": 53.0})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def test_sequential_offsets(self):
        mib = probe.MIB
        self.assertEqual(probe._offsets(100 * mib, 8 * mib, 4), [48 * mib, 56 * mib, 64 * mib, 72 * mib])
        self.assertEqual(len(probe._offsets(20 * mib, 8 * mib, 32)), 2)
        self.assertEqual(probe._offsets(4 * mib, 8 * mib, 4), [])

    def test_disk_probe_reads_bounded_and_reports_the_method(self):
        path = self.dir / "model.bin"
        path.write_bytes(os.urandom(24 * probe.MIB))
        result = probe.read_bandwidth(path, 16 * probe.MIB)
        self.assertEqual(result["bytes"], 16 * probe.MIB)
        self.assertIn(result["method"], ("direct", "buffered"))
        self.assertGreater(result["gbytes_ps"], 0)
        with self.assertRaises(probe.ProbeError):
            probe.read_bandwidth(path, 2 * probe.DISK_PROBE_MAX_BYTES)

    def test_disk_fact_cache_and_buffered_is_no_evidence(self):
        path = self.dir / "model.bin"
        path.write_bytes(b"\0" * 16)
        cache = facts.Cache(self.dir / "cache.json")
        stat = probe.file_stat(path)
        missing = probe.disk_fact(cache, "pc", str(path), stat, False, None)
        self.assertIn("--disks", missing["why"])
        measured = probe.disk_fact(cache, "pc", str(path), stat, True,
                                   lambda: {"gbytes_ps": 6.5, "bytes": 1, "seconds": 1, "method": "direct"})
        self.assertEqual((measured["value"], measured["source"]), (6.5, "probed"))
        self.assertEqual(probe.disk_fact(cache, "pc", str(path), stat, False, None)["value"], 6.5)
        os.utime(path, (1, 1))
        self.assertIsNone(probe.disk_fact(cache, "pc", str(path), probe.file_stat(path), False, None)["value"])
        buffered = probe.disk_fact(cache, "pc", str(path), stat, True,
                                   lambda: {"gbytes_ps": 9.0, "bytes": 1, "seconds": 1, "method": "buffered"})
        self.assertIsNone(buffered["value"])
        self.assertIn("unverified", buffered["why"])

    def test_node_facts_and_links(self):
        gpu = {"checks": {"amd": {"status": "ok", "value": [{
            "product_name": {"status": "ok", "value": "AMD Radeon 8060S"},
            "vram_total_bytes": {"status": "ok", "value": 512 * probe.MIB},
            "gtt_total_bytes": {"status": "ok", "value": 120 * probe.GIB}}]},
            "ram": {"status": "ok", "value": {"total_bytes": 125 * probe.GIB, "available_bytes": 110 * probe.GIB}}}}
        node = probe.node_device(gpu)
        self.assertEqual((node["name"]["value"], node["host_free_ram_gib"]["value"]), ("AMD Radeon 8060S", 110.0))
        self.assertEqual(facts._gtt(gpu), 120.0)
        doctor = {"checks": {"interfaces": {"status": "ok", "value": [
            {"name": "eno1", "state": "up", "negotiated_mbps": 2500}, {"name": "eno2", "state": "down"}]},
            "usb4": {"streams": {"status": "ok", "value": {"count": 4, "openable": ["s0", "s1"]}},
                     "layout": {"status": "ok", "value": {}}, "devices": {"status": "ok", "value": ["/dev/tbstream0"]}}}}
        lan, usb4 = probe.node_links(doctor)
        self.assertEqual((lan["value"], lan["up"], usb4["up"], usb4["streams"]), (2.5, True, True, 2))
        lan, usb4 = probe.node_links({"checks": {"usb4": {"layout": {"status": "unavailable"}}}})
        self.assertFalse(lan["up"] or usb4["up"])

    def test_node_evidence_must_name_host_and_path(self):
        cache = facts.Cache(self.dir / "cache.json")
        receipt = self.dir / "r.json"
        receipt.write_text(json.dumps({"disk_read_gbytes_ps": 6.1, "method": "direct", "host": "n1", "path": "/m.gguf"}))
        self.assertEqual(cache.put_evidence("n1", "/m.gguf", receipt)["method"], "receipt")
        self.assertEqual(cache.evidence("n1", "/m.gguf")["disk_read_gbytes_ps"], 6.1)
        self.assertIsNone(cache.evidence("n1", "/other.gguf"))                       # bound to the model path
        self.assertIsNone(cache.evidence("n1", "/m.gguf", now=time.time() + probe.CACHE_TTL_S + 1))   # and its age
        for bad in ({"host": "n2"}, {"method": "buffered"}, {"disk_read_gbytes_ps": 0}):
            receipt.write_text(json.dumps(dict({"disk_read_gbytes_ps": 6.1, "method": "direct", "host": "n1",
                                                "path": "/m.gguf"}, **bad)))
            with self.subTest(bad=bad), self.assertRaises(probe.ProbeError):
                cache.put_evidence("n1", "/m.gguf", receipt)


class GatherTests(unittest.TestCase):
    def test_unreachable_node_and_busy_pro_are_unavailable_never_invented(self):
        cluster = fake_cluster()
        context = ctx()

        def call(mid, verb):
            if mid == "n2":
                raise OSError("ssh: connection refused")
            if verb == "gpu":
                return {"checks": {"amd": {"status": "ok", "value": [{
                    "product_name": {"status": "ok", "value": "AMD Radeon 8060S"},
                    "gtt_total_bytes": {"status": "ok", "value": 120 * probe.GIB}}]},
                    "ram": {"status": "ok", "value": {"total_bytes": 125 * probe.GIB, "available_bytes": 100 * probe.GIB}}}}
            return {"checks": {"interfaces": {"status": "ok", "value": [{"name": "e", "state": "up", "negotiated_mbps": 1000}]}}}

        inventory, gathered, raw, notes = facts.gather(
            cluster, context, call=call, gpus=lambda: INVENTORY["cuda"],
            memory=lambda: {"total_gib": 127.7, "free_gib": 90.0, "free_commit_gib": 120.0},
            pro_busy=lambda uuid: "compute processes [4]", now=time.time())
        self.assertEqual(raw["devices"][GPU]["why"], "RTX PRO busy: compute processes [4]")
        self.assertEqual(gathered["devices"]["n1"]["memory_gib"], 120.0)
        self.assertIn("connection refused", raw["devices"]["n2"]["why"])
        self.assertIsNone(raw["hosts"]["n2"]["free_ram_gib"])
        self.assertEqual(raw["hosts"]["pc"]["free_ram_gib"], 90.0)
        recipe = reader.parse(PLAIN)
        bound = apply.bind(recipe, context, inventory)
        links = facts.link_state(recipe, bound, raw)
        self.assertFalse(links["pc-nodes"]["up"])


class DescriptorTests(unittest.TestCase):
    def test_saved_only_never_opens_the_model(self):
        with tempfile.TemporaryDirectory() as d:
            model = Path(d) / "m.gguf"
            model.write_bytes(b"x")
            store = descriptors.Store(Path(d) / "desc")
            with mock.patch.object(modelfiles, "read", return_value=({"a": 1}, [modelfiles.Tensor("t", 8, str(model), "F16", (4,))])):
                store.save(model)
            with descriptors.saved_only(store), mock.patch("builtins.open", side_effect=AssertionError("opened")):
                meta, tensors = modelfiles.read(model)
            self.assertEqual((meta, tensors[0].bytes), ({"a": 1}, 8))
            os.utime(model, (5, 5))
            with descriptors.saved_only(store), self.assertRaisesRegex(descriptors.DescriptorError, "changed"):
                modelfiles.read(model)
            with descriptors.saved_only(store), self.assertRaisesRegex(descriptors.DescriptorError, "no saved header"):
                modelfiles.read(Path(d) / "other.gguf")


def fake_cluster():
    from hyperloom.config import Cluster, Machine
    machines = {"pc": Machine("pc", "windows", "local", "127.0.0.1", None, 22, "C:/hl", True,
                              [{"id": "rtx-pro", "type": "nvidia", "uuid": GPU}]),
                "n1": Machine("n1", "linux", "ssh", "192.0.2.10", "n1", 22, "/opt/hl", True, [{"id": "gpu"}]),
                "n2": Machine("n2", "linux", "ssh", "192.0.2.11", "n2", 22, "/opt/hl", True, [{"id": "gpu"}])}
    profiles = {"mm": {"preset": "minimax-pair", "rank0": "n1", "rank1": "n2", "overrides": {"--capacity": 33024}}}
    return Cluster(machines, {}, profiles, {}, {}, {"pc": {}})


def saved_facts(path, profile="mm", model="minimax-m3.1-flash", **avail):
    gathered = dict(deepcopy(FACTS), model=model)
    raw = {"devices": availability()["devices"], "hosts": availability()["hosts"],
           "lan": {"n1": {"up": True}, "n2": {"up": True}}, "usb4": {"n1": {"up": True}, "n2": {"up": True}}}
    raw.update(avail)
    Path(path).write_text(json.dumps(session.facts_record(profile, INVENTORY, gathered, raw, [])))
    return path


MM_PREFILL_FACT = {"batch": 1, "engine_sha": "862a2f0d668a07660392f6d3008a750068eeece3", "mode": ("--capacity", "33024"),
                   "source": "synthetic (session plumbing tests only)"}


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.cluster = fake_cluster()
        # These tests exercise the session plumbing on a MiniMax-shaped profile; hyperloom has no cited MiniMax prefill
        # facts (test_minimax_local_prefill_is_refused_without_engine_facts), so a synthetic one stands in here.
        for patcher in (mock.patch.dict(facts.FAMILY_PREFILL_BATCH, {"minimax-pair": MM_PREFILL_FACT}),
                        mock.patch.dict(facts.FAMILY_PREFILL_CHUNK, {"minimax-pair": ("--prefill-chunk", 4096)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recipe = self.dir / "mm.toml"
        self.recipe.write_text(PLAIN, encoding="utf-8")

    def test_minimax_local_prefill_is_refused_without_engine_facts(self):
        with mock.patch.dict(facts.FAMILY_PREFILL_BATCH, clear=True):
            evaluation = session.evaluate(self.cluster, "mm", self.recipe, session.Layers(),
                                          session.saved_source(saved_facts(self.dir / "f.json")))
        self.assertIsNone(evaluation.decision)
        self.assertIn("max_batch: hyperloom has no cited prefill concurrency for minimax-pair", evaluation.refusal)

    def test_evaluate_from_saved_facts_without_a_service(self):
        evaluation = session.evaluate(self.cluster, "mm", self.recipe, session.Layers(),
                                      session.saved_source(saved_facts(self.dir / "f.json")))
        self.assertEqual(evaluation.problems, [])
        self.assertEqual(evaluation.bound["roles"], {"rtx": "idle", "pair": "decode"})
        self.assertFalse(evaluation.decision["service"]["start"])
        self.assertEqual(evaluation.summary()["deploy"]["prefill"][0]["why"], "profile mm has no prefill service")
        with self.assertRaisesRegex(RecipeError, "facts of profile other"):
            session.evaluate(self.cluster, "mm", self.recipe, session.Layers(),
                             session.saved_source(saved_facts(self.dir / "g.json", profile="other")))

    def test_diff_is_semantic(self):
        other = self.dir / "b.toml"
        other.write_text("# reordered comment\n" + PLAIN.replace("device_gib = 92.0", "device_gib = 90.0"), encoding="utf-8")
        result = session.diff(session.resolve(self.recipe, session.Layers()), session.resolve(other, session.Layers()))
        self.assertEqual([r["path"] for r in result["fields"]], ["routes.prefill_long.placement.rtx.device_gib"])
        same = session.diff(session.resolve(self.recipe, session.Layers()), session.resolve(self.recipe, session.Layers()))
        self.assertTrue(same["same"])

    def test_pick_most_specific_and_reports_every_candidate(self):
        recipes = self.dir / "recipes"
        recipes.mkdir()
        (recipes / "a.toml").write_text(PLAIN, encoding="utf-8")
        (recipes / "b.toml").write_text(PLAIN.replace('name = "minimax-m3.1-flash-lab"', 'name = "loose"')
                                        .replace('name = "RTX PRO 6000"\n', ''), encoding="utf-8")
        (recipes / "c.toml").write_text("schema = 1", encoding="utf-8")
        (recipes / "d.toml").write_text(DS, encoding="utf-8")
        source = session.saved_source(saved_facts(self.dir / "f.json"))
        chosen, rows = session.pick(self.cluster, "mm", [recipes], session.Layers(), source)
        self.assertEqual(Path(chosen.path).name, "a.toml")
        results = {Path(r["path"]).name: r["result"] for r in rows}
        self.assertEqual(results["b.toml"], "passes")
        self.assertTrue(results["c.toml"].startswith("invalid"))
        self.assertEqual(results["d.toml"], 'other model ("deepseek-v4.1-flash")')
        (recipes / "e.toml").write_text(PLAIN, encoding="utf-8")                      # same name as a.toml
        chosen, rows = session.pick(self.cluster, "mm", [recipes], session.Layers(), source)
        self.assertEqual(Path(chosen.path).name, "b.toml")
        self.assertIn("duplicate recipe name", {Path(r["path"]).name: r["result"] for r in rows}["e.toml"])

    def test_cli_explain_validate_diff(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["recipe", "explain", str(self.recipe), "--recipe-set",
                             "routes.prefill_long.when.prompt_tokens_min=3072"])
        self.assertEqual(code, 0)
        fields = {f["path"]: f for f in json.loads(out.getvalue())["fields"]}
        self.assertEqual(fields["routes.prefill_long.when.prompt_tokens_min"]["layer"], "override")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("hyperloom.cli._cluster", return_value=self.cluster):
            code = cli.main(["recipe", "explain", str(self.recipe), "mm", "--facts", str(saved_facts(self.dir / "f.json")),
                             "--prompt-tokens", "9000", "--link-down", "pc-nodes"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["simulated_request"]["route"], "prefill_nodes")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", err):
            code = cli.main(["recipe", "validate", str(self.recipe), "--recipe-set", "routes.prefill_long.execute=tp"])
        self.assertEqual(code, 1)
        self.assertRegex(err.getvalue(), "not a settable path|is not settable")           # stub or core wording
        bad = deepcopy(FACTS)
        bad["devices"]["n1"]["memory_gib"] = 64.0
        record = json.loads(Path(saved_facts(self.dir / "f.json")).read_text())
        record["facts"] = dict(bad, model="minimax-m3.1-flash")
        (self.dir / "bad.json").write_text(json.dumps(record))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("hyperloom.cli._cluster", return_value=self.cluster):
            code = cli.main(["recipe", "validate", str(self.recipe), "mm", "--facts", str(self.dir / "bad.json")])
        self.assertEqual(code, 1)
        self.assertRegex(json.loads(out.getvalue())["match"][0], r"memory 64(\.00)? GiB.*|.*110 GiB.*memory 64 GiB")


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def test_up_needs_the_core_or_the_explicit_stub(self):
        with mock.patch.dict(os.environ, {launch.STUB_ENV: ""}), mock.patch.object(launch.core, "locate", return_value=None):
            with self.assertRaisesRegex(RecipeError, "needs the strix-stream recipe core"):
                launch.prepare(fake_cluster(), "mm", launch.Options(str(FIXTURES / "minimax-lab.toml")),
                               router="local", vram_cap_gib=None, state_dir=self.dir)

    def test_core_output_is_bounded_utf8_json_objects(self):
        def runner(out=b"", err=b"", code=0):
            def run(argv, **kw):
                kw["stdout"].write(out)
                kw["stderr"].write(err)
                return mock.Mock(returncode=code)
            return run
        exe = str(self.dir / "ss-recipe.exe")
        self.assertEqual(core.Core(exe, run=runner(b'{"a": 1}')).call(["x"]), {"a": 1})
        cases = [(runner(b"[1]"), "not a JSON object"), (runner(b'"x"'), "not a JSON object"),
                 (runner(b"\xff\xfe"), "not UTF-8"), (runner(b"x" * (core.MAX_OUTPUT + 1)), "exceeds"),
                 (runner(err="ss-recipe: refusé".encode(), code=1), "refusé"),
                 (runner(err=b"\xff", code=1), "not UTF-8"), (runner(b"{", code=0), "no JSON")]
        for run, why in cases:
            with self.assertRaisesRegex(RecipeError, why):
                core.Core(exe, run=run).call(["x"])

    def test_core_executable_must_be_an_absolute_program(self):
        exe = self.dir / ("ss-recipe.exe" if os.name == "nt" else "ss-recipe")
        exe.write_bytes(b"")
        exe.chmod(0o755)
        bat = self.dir / "ss-recipe.bat"
        bat.write_bytes(b"")
        with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": str(exe)}):
            self.assertEqual(core.locate(), str(exe))
        for bad in ("ss-recipe.exe", str(self.dir / "missing.exe")) + ((str(bat),) if os.name == "nt" else ()):
            with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": bad}):
                self.assertIsNone(core.locate(), bad)
        # PATH: relative entries (the current directory) are never searched.
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": "",
                                              "PATH": os.pathsep.join([".", ""])}):
                self.assertIsNone(core.locate())
            with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": "",
                                              "PATH": str(self.dir)}):
                self.assertEqual(core.locate(), str(exe))
        finally:
            os.chdir(cwd)

    def test_core_is_found_under_both_names(self):
        # The project is renamed tierstream: ts-recipe first, ss-recipe still found; both env names; a configured
        # path that no longer exists is looked up as its sibling.
        suffix = ".exe" if os.name == "nt" else ""
        old, new = self.dir / "old", self.dir / "new"
        for directory, name in ((old, "ss-recipe"), (new, "ts-recipe"), (new, "ss-recipe")):
            directory.mkdir(exist_ok=True)
            (directory / (name + suffix)).write_bytes(b"")
            (directory / (name + suffix)).chmod(0o755)
        ts, ss = str(new / ("ts-recipe" + suffix)), str(old / ("ss-recipe" + suffix))
        clear = {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": ""}
        with mock.patch.dict(os.environ, dict(clear, PATH=os.pathsep.join([str(old), str(new)]))):
            self.assertEqual(core.locate(), ts)                                  # the new name wins on PATH
        with mock.patch.dict(os.environ, dict(clear, PATH=str(old))):
            self.assertEqual(core.locate(), ss)
        with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": ts, "HYPERLOOM_SS_RECIPE": ss}):
            self.assertEqual(core.locate(), ts)
        with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": "", "HYPERLOOM_SS_RECIPE": ss}):
            self.assertEqual(core.locate(), ss)
        cluster = lambda path: mock.Mock(raw={"pc": {"recipe_core": str(path)}})
        with mock.patch.dict(os.environ, dict(clear, PATH="")):
            self.assertEqual(core.locate(cluster(new / ("ss-recipe" + suffix))), str(new / ("ss-recipe" + suffix)))
            self.assertEqual(core.locate(cluster(new / ("strix-old" + suffix))), ts)      # missing: the sibling
            self.assertIsNone(core.locate(cluster(self.dir / "none" / ("ss-recipe" + suffix))))
            if os.name == "nt":
                bat = new / "ss-recipe.bat"
                bat.write_bytes(b"")
                self.assertIsNone(core.locate(cluster(bat)))                    # present but unusable: no fallback
        # Both stderr prefixes are stripped from the core's refusal.
        def refusal(err):
            def run(argv, **kw):
                kw["stderr"].write(err)
                return mock.Mock(returncode=1)
            return run
        for prefix in (b"ts-recipe: ", b"ss-recipe: "):
            with self.assertRaises(RecipeError) as caught:
                core.Core(ts, run=refusal(prefix + b"no route fits")).call(["x"])
            self.assertEqual(str(caught.exception), "no route fits")

    def test_renamed_schema_ids_are_read_and_written_legacy(self):
        renamed = MM.replace('schema = "strix-stream/recipe-v1"', 'schema = "tierstream/recipe-v1"', 1)
        self.assertNotEqual(renamed, MM)
        old, new = reader.resolve(MM, "mm.toml"), reader.resolve(renamed, "mm.toml")
        self.assertEqual(new.recipe["schema"], "strix-stream/recipe-v1")
        self.assertEqual(new.to_json(), old.to_json())
        self.assertEqual(new.canonical_sha256(), old.canonical_sha256())
        self.assertEqual(reader.explain(new)["schema"], "strix-stream/recipe-explain-v1")
        # One hash rule: the stub backend's effective sha256 is the reader's, for either spelling.
        resolved = core.Resolved(new.recipe, new.sha256, {})
        self.assertEqual(core.StubBackend().effective_sha256(None, None, resolved, None), old.canonical_sha256())
        self.assertEqual(reader.canonical_sha256(dict(old.to_json(), schema="tierstream/recipe-v1")),
                         old.canonical_sha256())
        select = reader.select(new.recipe, select_state(new.recipe), SHA)
        self.assertEqual(select, reader.select(old.recipe, select_state(old.recipe), SHA))
        self.assertEqual(select["schema"], "strix-stream/recipe-select-v1")
        # Profile and overrides layers in either spelling (dispatched by kind), bound to the file they were made for.
        overrides = ('schema = "tierstream/recipe-overrides-v1"\nrecipe = "minimax-m3.1-flash-lab"\n[set]\n'
                     '"limits.host_free_ram_floor_gib" = 6\n')
        self.assertEqual(reader.resolve(MM, overrides=[(overrides, "o")]).recipe["limits"]["host_free_ram_floor_gib"], 6)
        profile = profile_text().replace("strix-stream/recipe-profile-v1", "tierstream/recipe-profile-v1")
        eff = reader.resolve(MM, profile=(profile, "p.toml"), probe=identity())
        self.assertFalse(eff.untuned, eff.refused)
        self.assertEqual(eff.trace["limits.host_free_ram_floor_gib"]["layer"], "profile")
        for bad in ("tierstream/recipe-v2", "strix-stream/recipe-v2", "tierstream/recipe-profile-v1"):
            with self.subTest(bad=bad), self.assertRaisesRegex(RecipeError, "or tierstream/recipe-v1"):
                reader.parse(MM.replace('schema = "strix-stream/recipe-v1"', f'schema = "{bad}"', 1))
        with self.assertRaisesRegex(RecipeError, "or tierstream/recipe-overrides-v1"):
            reader.resolve(MM, overrides=[(overrides.replace("recipe-overrides-v1", "recipe-v1"), "o")])

    def test_crlf_recipes_bind_layers_to_their_raw_bytes(self):
        import hashlib
        lf = MM.replace("\r\n", "\n")
        for name, body in (("lf", lf.encode()), ("crlf", lf.replace("\n", "\r\n").encode())):
            with self.subTest(newlines=name):
                path = self.dir / f"{name}.toml"
                path.write_bytes(body)
                raw_sha = hashlib.sha256(body).hexdigest()
                layer = self.dir / f"{name}-o.toml"
                layer.write_bytes(('schema = "strix-stream/recipe-overrides-v1"\nrecipe = "minimax-m3.1-flash-lab"\n'
                                   f'recipe_sha256 = "{raw_sha}"\n[set]\n"limits.host_free_ram_floor_gib" = 6\n')
                                  .replace("\n", "\r\n" if name == "crlf" else "\n").encode())
                resolved = session.resolve(path, session.Layers(overrides=[layer]))
                self.assertEqual(resolved.file_sha256, raw_sha)
                self.assertEqual(resolved.recipe["limits"]["host_free_ram_floor_gib"], 6)
                # The core backend hashes the same raw bytes (its effective document may carry the renamed id).
                effective = dict(__import__("tomllib").loads(lf), schema="tierstream/recipe-v1")
                fake = mock.Mock(executable="core")
                fake.call.side_effect = lambda args, inputs=None: effective if args[0] == "validate" else {"fields": []}
                by_core = session.resolve(path, session.Layers(), backend=core.CoreBackend(fake))
                self.assertEqual((by_core.file_sha256, by_core.recipe["schema"]), (raw_sha, reader.SCHEMA))
        other = hashlib.sha256(lf.encode()).hexdigest()
        stale = self.dir / "stale-o.toml"
        stale.write_text('schema = "strix-stream/recipe-overrides-v1"\nrecipe = "minimax-m3.1-flash-lab"\n'
                         f'recipe_sha256 = "{other}"\n[set]\n', encoding="utf-8")
        with self.assertRaisesRegex(RecipeError, "written for another version of the recipe file"):
            session.resolve(self.dir / "crlf.toml", session.Layers(overrides=[stale]))

    def test_layer_ranges_and_lists_are_bounded(self):
        for value in ("[[0, 1000000000000]]", "[5000]", "[" + ", ".join(map(str, range(5000))) + "]"):
            text = PLAIN.replace('host_pinned_gib = 48.0\n', 'host_pinned_gib = 48.0\n\n[[routes.placement.rtx.units]]\n'
                                 f'id = "layers"\nclass = "blob"\nlayers = {value}\nresident = 1\n', 1)
            self.assertNotEqual(text, PLAIN)
            started = time.monotonic()
            with self.assertRaises(RecipeError):
                reader.resolve(text)
            self.assertLess(time.monotonic() - started, 2.0)

    def test_receipts_must_be_finite(self):
        receipt = self.dir / "r.json"
        receipt.write_text('{"host": "n1", "path": "/m", "method": "direct", "disk_read_gbytes_ps": Infinity}')
        with self.assertRaisesRegex(probe.ProbeError, "finite"):
            facts.Cache(self.dir / "facts.json").put_evidence("n1", "/m", receipt)
        receipt.write_text('{"host": "n1", "path": "/m", "method": "direct", "disk_read_gbytes_ps": 1' + "0" * 400 + "}")
        with self.assertRaisesRegex(probe.ProbeError, "finite"):
            facts.Cache(self.dir / "facts.json").put_evidence("n1", "/m", receipt)

    def test_node_poller_survives_any_bad_answer(self):
        from hyperloom import orchestrate
        state = {"machines": {"n1": {}, "n2": {}}, "known_hosts": None}
        polled = {"n1": {"free_ram_gib": 20.0, "at": 1.0}}
        answers = iter([{"checks": []}, TypeError("bad")])           # parsing raises; the call raises

        def call(*args, **kwargs):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(orchestrate, "_client"), mock.patch.object(orchestrate, "_call", side_effect=call):
            orchestrate.poll_nodes_once(state, polled)
        self.assertEqual(polled, {"n1": {"free_ram_gib": 20.0, "at": 1.0}})     # unchanged: both nodes age out

    def test_engine_declaration_receipts(self):
        launch_sha = "c" * 64
        output = {"schema": "strix/engine-capabilities-v1", "executable_sha256": "e" * 64,
                  "modes": [{"name": "ep2-tp", "memory": [
                      {"rank": 0, "workspace_bytes": 3 << 30, "state_bytes_per_slot": 1 << 30, "kv_pool_bytes": 0},
                      {"rank": 1, "workspace_bytes": 2 << 30, "state_bytes_per_slot": 1 << 30, "kv_pool_bytes": 0}]}]}
        good = {"schema": facts.DECLARATION_SCHEMA, "machine": "n1", "rank": 0, "launch_sha256": launch_sha,
                "output": output}
        receipt = self.dir / "d.json"
        cache = facts.Cache(self.dir / "facts.json")
        receipt.write_text(json.dumps(good))
        entry = cache.put_declaration("n1", 0, launch_sha, receipt)
        self.assertEqual((entry["workspace_bytes"], entry["state_bytes_per_slot"]), (3 << 30, 1 << 30))
        self.assertEqual(cache.declaration("n1", launch_sha)["rank"], 0)
        self.assertIsNone(cache.declaration("n1", "d" * 64))                            # another launch: nothing
        bad = [(dict(good, machine="n2"), "names n2"), (dict(good, rank=1), "rank 1"),
               (dict(good, launch_sha256="d" * 64), "the launch changed"),
               (dict(good, schema="x"), "expected hyperloom/engine-describe-v1"),
               (dict(good, output=dict(output, schema="x")), "one strix/engine-capabilities-v1 mode"),
               (dict(good, output=dict(output, modes=[output["modes"][0], output["modes"][0]])), "one strix"),
               (dict(good, output={**output, "modes": [{"memory": [{"rank": 1}]}]}), "no memory entry for rank 0"),
               (dict(good, rank=False), "rank must be an integer"), (dict(good, rank=0.0), "rank must be an integer"),
               (dict(good, output={**output, "modes": [{"memory": [dict(output["modes"][0]["memory"][0], rank=False)]}]}),
                "no memory entry for rank 0"),
               (dict(good, output={k: v for k, v in output.items() if k != "executable_sha256"}), "executable_sha256"),
               (dict(good, output=dict(output, executable_sha256="E" * 64)), "executable_sha256")]
        for value, key in (("1" + "0" * 30, "workspace_bytes"), ("-1", "kv_pool_bytes"), ("1.5", "state_bytes_per_slot")):
            text = json.dumps(good).replace(f'"{key}": {good["output"]["modes"][0]["memory"][0][key]}', f'"{key}": {value}', 1)
            bad.append((json.loads(text), f"{key} must be an integer"))
        for data, why in bad:
            receipt.write_text(json.dumps(data))
            with self.subTest(why=why), self.assertRaisesRegex(probe.ProbeError, why):
                cache.put_declaration("n1", 0, launch_sha, receipt)
        receipt.write_bytes(json.dumps(good).encode() + b" " * facts.MAX_RECEIPT_BYTES)    # bounded read
        with self.assertRaisesRegex(probe.ProbeError, "larger than 4 MiB"):
            cache.put_declaration("n1", 0, launch_sha, receipt)
        with self.assertRaisesRegex(probe.ProbeError, "larger than 4 MiB"):
            cache.put_evidence("n1", "/m", receipt)
        # The runtime the check compares: workspace + state per slot x launched slots + KV pool.
        context = dataclasses.replace(ctx(), limits=dict(LIMITS, node_batch=2))
        rows = [{"machine": "n1", "rank": 0, "launch_sha256": launch_sha}, {"machine": "n2", "rank": 1,
                                                                            "launch_sha256": "f" * 64}]
        with mock.patch.object(facts, "describe_commands", return_value=rows):
            runtime = facts.node_runtime(fake_cluster(), dataclasses.replace(context, nodes=["n1", "n2"]), cache)
        self.assertEqual((runtime["n1"]["gib"], runtime["n1"]["executable_sha256"]), (5.0, "e" * 64))
        self.assertIn("no engine memory declaration for this exact launch", runtime["n2"]["why"])

    def test_describe_commands_are_the_exact_launch_plus_the_declaration_flag(self):
        from tests import test_rtx
        from hyperloom import render
        from hyperloom.config import parse
        obj = test_rtx.rtx_example()
        cluster = parse(obj)
        rows = facts.describe_commands(cluster, "ds41-rtx")
        self.assertEqual([r["rank"] for r in rows], [0, 1])
        self.assertTrue(all(r["argv"][-1] == "--describe-capabilities-json" and facts.IDENTITY_KEY in r["argv"]
                            for r in rows))
        # Sol-11 #1: `up` sets another release id and its own key paths; the final render has the same identity.
        final = deepcopy(cluster)
        final.profiles["ds41-rtx"]["release_id"] = "abcdef012345"
        final.profiles["ds41-rtx"]["api_key_files"] = {m: f"/opt/hl/secrets/{m}-new.key" for m in ("halo-1", "halo-2")}
        processes = render.render_processes(final, "ds41-rtx")
        identities = facts.launch_identities(final, "ds41-rtx", processes)
        self.assertEqual([identities[r["machine"]][1] for r in rows], [r["launch_sha256"] for r in rows])
        # Diagnostics (extra env) on the final render are another launch.
        changed = [type(p)(p.machine_id, p.role, p.argv, dict(p.env, STRIX_X="1"), p.ready) for p in processes]
        self.assertNotEqual(facts.launch_identities(final, "ds41-rtx", changed)[rows[0]["machine"]][1],
                            rows[0]["launch_sha256"])
        obj["profiles"]["ds41-rtx"].setdefault("overrides", {})["--default-max-tokens"] = 2048
        changed = facts.describe_commands(parse(obj), "ds41-rtx")
        self.assertNotEqual(rows[0]["launch_sha256"], changed[0]["launch_sha256"])  # a flag change = another launch
        obj = test_rtx.rtx_example()
        machine = rows[0]["machine"]
        obj["assets"][machine]["ds41_model"]["sha256"] = "9" * 64                  # same path, another file
        replaced = facts.describe_commands(parse(obj), "ds41-rtx")
        self.assertNotEqual(rows[0]["launch_sha256"], replaced[0]["launch_sha256"])

    def test_router_follows_the_release_auth(self):
        # Sol-11 #2: a key-enabled MiniMax release is reached over the LAN with its key, from either router placement.
        from tests import test_rtx
        from hyperloom import engines, render
        from hyperloom.config import parse
        obj = test_rtx.rtx_example()
        obj["profiles"]["mm"] = {"preset": "minimax-pair", "rank0": "halo-1", "rank1": "halo-2", "port": 18601,
                                 "release_id": "0" * 12}
        real = engines.load_preset
        keyed = lambda name: (dict(real(name), serve_auth="key-file",
                                   argv=real(name)["argv"] + ["--api-key-file", "{key_file}"])
                              if name == "minimax-pair" else real(name))
        for keyed_release in (False, True):
            with mock.patch.object(engines, "load_preset", side_effect=keyed if keyed_release else real):
                for host in ("local", "halo-1"):
                    cluster = parse(deepcopy(obj))
                    cluster.router.update(machine=host, api_key_file="/k/router.key",
                                          replica_api_key_files={"halo-1": "/k/halo-1.key"})
                    if host == "local":
                        cluster.router["listen"] = "127.0.0.1:18600"
                    with self.subTest(keyed=keyed_release, router=host):
                        if not keyed_release and host == "local":
                            with self.assertRaisesRegex(Exception, "bind loopback only"):
                                render.render_router(cluster, "mm")
                            continue
                        replica = render.render_router(cluster, "mm")["replicas"][0]
                        if keyed_release:
                            self.assertEqual((replica["address"], replica["api_key_file"]),
                                             (cluster.machines["halo-1"].http_address, "/k/halo-1.key"))
                        else:
                            self.assertEqual(replica["address"], "127.0.0.1")
                            self.assertNotIn("api_key_file", replica)

    def test_saved_declarations_and_the_final_launch_are_rechecked(self):
        # Sol-11 #3: a saved runtime row for another launch is a refusal reason, not a pass.
        from tests import test_rtx
        from hyperloom import render
        from hyperloom.config import parse
        cluster = parse(test_rtx.rtx_example())
        context = dataclasses.replace(ctx(), profile="ds41-rtx", nodes=["halo-1", "halo-2"])
        current = {r["machine"]: r["launch_sha256"] for r in facts.describe_commands(cluster, "ds41-rtx")}
        rows = {"halo-1": {"gib": 7.6, "launch_sha256": current["halo-1"]},
                "halo-2": {"gib": 7.6, "launch_sha256": "0" * 64}}
        out = facts.current_runtime(cluster, context, rows)
        self.assertEqual(out["halo-1"]["gib"], 7.6)
        self.assertIn("for another launch", out["halo-2"]["why"])
        # Sol-11 #1: up refuses a final render that is not the checked launch (before any lease).
        launch_obj = mock.Mock()
        launch_obj.evaluation.context.limits = {"node_runtime": {m: {"gib": 7.6, "launch_sha256": s,
                                                                     "executable_sha256": "e" * 64}
                                                                 for m, s in current.items()}}
        launch_obj.evaluation.decision = {"decode": {"id": "decode", "runtime_checked": True}}
        binary = render.preset(cluster.profiles["ds41-rtx"]["preset"])["binary"]
        releases = {m: {"manifest": {"binaries": {binary: "e" * 64}}} for m in current}
        processes = render.render_processes(cluster, "ds41-rtx")
        launch.verify_launch(launch_obj, cluster, "ds41-rtx", processes, releases)        # the checked launch: fine
        altered = [type(p)(p.machine_id, p.role, p.argv + ["--extra"], p.env, p.ready) for p in processes]
        with self.assertRaisesRegex(RecipeError, "differs from the one its engine declarations were checked"):
            launch.verify_launch(launch_obj, cluster, "ds41-rtx", altered, releases)
        # The declaration must come from the executable of the release being started.
        for rebuilt in ({**releases, "halo-2": {"manifest": {"binaries": {binary: "f" * 64}}}},
                        {**releases, "halo-2": {"manifest": {}}}, {"halo-1": releases["halo-1"]}):
            with self.subTest(releases=sorted(rebuilt)), self.assertRaisesRegex(
                    RecipeError, "halo-2: the engine declaration was made by executable eeeeeeeeeeee"):
                launch.verify_launch(launch_obj, cluster, "ds41-rtx", processes, rebuilt)
        # workspace_gib at its default: admission checked no declaration, so none is re-checked here either.
        launch_obj.evaluation.decision = {"decode": {"id": "decode", "runtime_checked": False}}
        launch.verify_launch(launch_obj, cluster, "ds41-rtx", altered, {})

    def test_release_auth_binds_the_lan_only_for_that_release(self):
        from hyperloom import engines
        from hyperloom.config import ConfigError
        keyed = ["{binary}", "--bind", "{address}", "--api-key-file", "{key_file}"]
        presets = {"mm-old": {"family": "minimax-pair"},
                   "mm-new": {"family": "minimax-pair", "serve_auth": "key-file", "argv": keyed},
                   "ds-bad": {"family": "ds41-pair", "serve_auth": "key-file", "argv": keyed},
                   "mm-odd": {"family": "minimax-pair", "serve_auth": "token", "argv": keyed}}
        with mock.patch.object(engines, "load_preset", side_effect=presets.__getitem__):
            old, new = engines.release_adapter("mm-old"), engines.release_adapter("mm-new")
            self.assertEqual((old.bind, old.key_file), ("loopback", False))
            self.assertEqual((new.bind, new.key_file), ("lan", True))
            self.assertIn("key-file", new.capabilities)
            self.assertEqual(engines.adapter("minimax-pair").bind, "loopback")          # the family row is unchanged
            for name in ("ds-bad", "mm-odd"):
                with self.subTest(name=name), self.assertRaisesRegex(ConfigError, "serve_auth"):
                    engines.release_adapter(name)

    def test_release_auth_needs_the_key_on_the_command_line(self):
        # A keyed release whose argv does not pass {key_file} after a key flag would serve the LAN unauthenticated.
        from hyperloom import engines
        from hyperloom.config import ConfigError
        argvs = {"none": ["{binary}", "--bind", "{address}"],
                 "no-flag": ["{binary}", "--bind", "{address}", "{key_file}"],
                 "other-flag": ["{binary}", "--model", "{key_file}"],
                 "first": ["{key_file}", "--bind", "{address}"]}
        presets = {name: {"family": "minimax-pair", "serve_auth": "key-file", "argv": argv}
                   for name, argv in argvs.items()}
        with mock.patch.object(engines, "load_preset", side_effect=presets.__getitem__):
            for name in argvs:
                with self.subTest(name=name), self.assertRaisesRegex(ConfigError, "needs the argv to pass the key"):
                    engines.release_adapter(name)
        for flag in engines.KEY_FILE_FLAGS:
            self.assertTrue(engines.passes_key_file(["{binary}", flag, "{key_file}"]))

    def test_idle_cuda_members_are_never_available(self):
        spec = dict(self.spec(None), idle=["GPU-4090"])
        out = launch.write_route_state(spec, memory=lambda: {"free_physical_gib": 60.0, "free_commit_gib": 80.0})
        self.assertFalse(out["devices"]["GPU-4090"]["available"])

    def spec(self, service_dir):
        return {"file": str(self.dir / "route-state.json"), "service_dir": service_dir, "uuids": [GPU],
                "nodes": ["n1", "n2"], "pc": "pc", "links": ["pc-nodes"], "internal": ["pair.internal"],
                "reservation_end": time.time() + 600, "max_age_s": 10, "machines": {}, "known_hosts": None}

    def test_write_route_state_from_measurements_only(self):
        service = self.dir / "svc"
        service.mkdir()
        (service / "state.json").write_text(json.dumps({"state": "ready"}))
        spec = self.spec(str(service))
        memory = lambda: {"free_physical_gib": 60.0, "free_commit_gib": 80.0}
        now = time.time()
        fresh = {"n1": {"free_ram_gib": 20.0, "at": now}, "n2": {"free_ram_gib": 21.0, "at": now}}
        data = launch.write_route_state(spec, memory=memory, nodes=fresh, replicas={"n1": "up"}, now=now)
        self.assertEqual(json.loads(Path(spec["file"]).read_text()), data)
        self.assertTrue(data["devices"][GPU]["available"] and data["devices"]["n2"]["available"])
        self.assertEqual(data["hosts"]["n2"], {"free_ram_gib": 21.0})
        self.assertEqual(data["links"], {"pc-nodes": {"up": True}, "pair.internal": {"up": True}})
        # A node without a recent answer, or a replica the router reports down: unavailable, links down, no RAM.
        stale = dict(fresh, n2={"free_ram_gib": 21.0, "at": now - 30})
        data = launch.write_route_state(spec, memory=memory, nodes=stale, replicas={"n1": "up"}, now=now)
        self.assertFalse(data["devices"]["n2"]["available"])
        self.assertNotIn("n2", data["hosts"])
        data = launch.write_route_state(spec, memory=memory, nodes=fresh, replicas={"n1": "down"}, now=now)
        self.assertFalse(data["devices"]["n1"]["available"] or data["links"]["pair.internal"]["up"])
        data = launch.write_route_state(spec, memory=memory, nodes=fresh, replicas={}, now=now)
        self.assertFalse(data["links"]["pc-nodes"]["up"])                            # no router answer yet
        (service / "state.json").write_text(json.dumps({"state": "stopped"}))
        self.assertEqual(launch.write_route_state(spec, memory=memory)["devices"][GPU],
                         {"available": False, "why": "prefill service stopped"})
        (service / "state.json").write_text(json.dumps({"state": "ready"}))
        spec["reservation_end"] = time.time() + 30
        self.assertIn("reservation", launch.write_route_state(spec, memory=memory)["devices"][GPU]["why"])

    def test_plan_service_refuses_instead_of_clamping(self):
        from tests import test_rtx
        from hyperloom.config import parse
        cluster = parse(test_rtx.rtx_example())
        with mock.patch.object(launch.rtx, "plan_placement", return_value=None):
            self.assertEqual(launch.plan_service(cluster, "ds41-rtx", 88.0, 48.0, free_physical_gib=100.0), (None, None))
            plan, why = launch.plan_service(cluster, "ds41-rtx", 95.0, 48.0, free_physical_gib=100.0)
            self.assertIn("above the reservation", why)
            plan, why = launch.plan_service(cluster, "ds41-rtx", 88.0, 80.0, free_physical_gib=100.0)
            self.assertIn("host_pinned_gib 80 is above what this start allows", why)
        with mock.patch.object(launch.rtx, "plan_placement", side_effect=launch.descriptors.DescriptorError("no saved header")):
            self.assertEqual(launch.plan_service(cluster, "ds41-rtx", 88.0, 48.0, free_physical_gib=100.0),
                             (None, "no saved header"))

    def test_exact_host_and_whole_gib(self):
        from tests import test_rtx
        from hyperloom.config import parse
        cluster = parse(test_rtx.rtx_example())
        plan = mock.Mock(flags={"--vram-cap-gib": "80", "--host-gib": "31"})
        plan.to_dict.return_value = {"flags": plan.flags, "disk_gib": 0}
        with mock.patch.object(launch.rtx, "plan_placement", return_value=plan):
            result, why = launch.plan_service(cluster, "ds41-rtx", 88.0, 48.0, free_physical_gib=100.0)
        self.assertIsNone(why)
        self.assertEqual(result["flags"]["--host-gib"], "48")                     # the budget, not the plan's estimate
        result, why = launch.plan_service(cluster, "ds41-rtx", 88.0, 48.5, free_physical_gib=100.0)
        self.assertIn("whole GiB", why)

    def test_generation_default_follows_the_launch(self):
        from tests import test_rtx
        from hyperloom.config import parse
        obj = test_rtx.rtx_example()
        obj["profiles"]["ds41-rtx"].setdefault("overrides", {})["--default-max-tokens"] = 4096
        cluster = parse(obj)
        self.assertEqual(facts.context(cluster, "ds41-rtx").limits["default_generation_tokens"], 4096)
        del obj["profiles"]["ds41-rtx"]["overrides"]["--default-max-tokens"]
        self.assertEqual(facts.context(parse(obj), "ds41-rtx").limits["default_generation_tokens"], 1024)

    def test_route_state_disk_availability(self):
        model = self.dir / "model.gguf"
        spec = dict(self.spec(None), disk={GPU: str(model)})
        memory = lambda: {"free_physical_gib": 60.0, "free_commit_gib": 80.0}
        self.assertFalse(launch.write_route_state(spec, memory=memory)["devices"][GPU]["disk_available"])
        model.write_bytes(b"x")
        self.assertTrue(launch.write_route_state(spec, memory=memory)["devices"][GPU]["disk_available"])

    def test_node_floors(self):
        evaluation = mock.Mock()
        evaluation.effective.recipe = {"limits": {"host_free_ram_floor_gib": 8.0}}
        evaluation.context.nodes = ["n1", "n2"]
        item = launch.Launch(evaluation, True, False, None, None, None, {})
        self.assertIsNone(launch.node_floors(item, lambda mid: 20.0))
        self.assertIn("n2: free RAM 6.0", launch.node_floors(item, lambda mid: 20.0 if mid == "n1" else 6.0))
        self.assertIn("not reported", launch.node_floors(item, lambda mid: None))


class UpWithRecipeTests(unittest.TestCase):
    """`up --recipe` on the DS RTX profile (the existing up harness): the service starts or is skipped as decided."""

    def setUp(self):
        from tests import test_rtx
        self.harness = test_rtx.UpDownTests("test_order_keys_ownership_and_router_services")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.calls = self.harness.calls
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        recipe = self.dir / "ds.toml"
        recipe.write_text(DS, encoding="utf-8")
        self.options = launch.Options(str(recipe))
        patches = [mock.patch.dict(os.environ, {launch.STUB_ENV: "1"}),
                   mock.patch.object(launch.core, "locate", return_value=None),
                   mock.patch.object(launch.render, "router_policies", return_value=("nodes", "rtx", "auto", "routes")),
                   mock.patch("hyperloom.render.validate_router"),
                   mock.patch.object(launch.render, "check_route_table", side_effect=_check_table),
                   mock.patch.object(launch.rtx, "pro_busy", return_value=None),
                   mock.patch.object(launch, "plan_service", side_effect=self.plan)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.avail = {}
        self.node_free = 100.0
        self.planned = []
        self.plan_why = None
        from tests import test_orchestrate
        original = test_orchestrate.FakeRemote.call
        harness = self

        def call(remote_self, verb, payload, timeout=60):
            if verb == "gpu":
                remote_self.calls.append((remote_self.machine.id, verb, payload))
                return {"checks": {"ram": {"status": "ok", "value": {"total_bytes": 128 * probe.GIB,
                                                                       "available_bytes": int(harness.node_free * probe.GIB)}}}}
            return original(remote_self, verb, payload, timeout)
        patcher = mock.patch.object(test_orchestrate.FakeRemote, "call", call)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, cluster, profile, cap, host, **kw):
        self.planned.append((cap, host))
        return ({"flags": {"--vram-cap-gib": "80"}}, None) if self.plan_why is None else (None, self.plan_why)

    def source(self, ctx_):
        gathered = deepcopy(FACTS)
        gathered["model"] = "deepseek-v4.1-flash"
        gathered["devices"]["halo-1"] = gathered["devices"].pop("n1")
        gathered["devices"]["halo-2"] = gathered["devices"].pop("n2")
        gathered["links"] = [{"kind": "ethernet", "hosts": ["workstation", "halo-1"], "gbps": 2.5},
                             {"kind": "ethernet", "hosts": ["workstation", "halo-2"], "gbps": 2.5},
                             {"kind": "usb4", "hosts": ["halo-1", "halo-2"], "gbps": 0.0}]
        devices = {GPU: {"available": True}, "halo-1": {"available": True}, "halo-2": {"available": True}}
        devices.update(self.avail)
        raw = {"devices": devices, "hosts": {"workstation": {"free_ram_gib": 100.0, "free_commit_gib": 150.0},
                                             "halo-1": {"free_ram_gib": 124.0}, "halo-2": {"free_ram_gib": 124.0}},
               "lan": {"halo-1": {"up": True}, "halo-2": {"up": True}}, "usb4": {"halo-1": {"up": True}, "halo-2": {"up": True}}}
        return INVENTORY, gathered, raw, []

    def up(self):
        from tests import test_rtx
        orch = self.harness.modules()[0]
        cluster = self.harness.rtx_cluster()
        cluster.pc["cuda"] = deepcopy(test_rtx.rtx_example()["pc"]["cuda"])     # the operator's PC-BIG grant
        with mock.patch("hyperloom.recipes.commands.live_source", return_value=self.source):
            return orch.up(cluster, "ds41-rtx", recipe=self.options)

    def test_route_table_refused_by_the_pinned_router_stops_before_any_lease(self):
        with mock.patch.object(launch.render, "check_route_table", return_value="routes[0].hosts: expected bound hosts"):
            with self.assertRaisesRegex(RecipeError, "pinned TensorMeld router refuses the route table: routes"):
                self.up()
        self.assertEqual([verb for _, verb, _ in self.calls if verb in ("lease", "service-render", "router-start")], [])

    def test_service_starts_with_the_recipe_budget_and_the_routes_policy(self):
        state = self.up()
        self.assertEqual(self.planned, [(88.0, 48.0)])                            # planned before any lease
        render_kw = next(p for _, verb, p in self.calls if verb == "service-render")
        self.assertEqual((render_kw["vram_cap_gib"], render_kw["host_gib_exact"]), (88.0, 48.0))
        rendered = next(p for _, verb, p in self.calls if verb == "router-start")["rendered"]
        self.assertEqual(rendered["prefill"]["policy"], "routes")
        self.assertEqual([r["id"] for r in rendered["prefill"]["routes"]["routes"]], ["prefill_long", "prefill_nodes"])
        self.assertFalse(state["production"])                                    # stub reader
        self.assertTrue(state["recipe"]["service"]["start"])
        orch = self.harness.modules()[0]
        with mock.patch.object(orch, "_call", return_value={}), mock.patch.object(orch, "_http_json", return_value={}):
            self.assertEqual(orch.status("ds41-rtx")["recipe"]["recipe"], "ds41-lab")

    def test_pro_busy_skips_the_service_entirely(self):
        self.avail = {GPU: {"available": False, "why": "RTX PRO busy: compute processes [7]"}}
        state = self.up()
        verbs = [verb for _, verb, _ in self.calls]
        self.assertNotIn("service-render", verbs)
        self.assertNotIn("service-start", verbs)
        rendered = next(p for _, verb, p in self.calls if verb == "router-start")["rendered"]
        # Node-only runs keep the recipe's admission per request; the offload route is compiled disabled.
        self.assertEqual(rendered["prefill"]["policy"], "routes")
        self.assertNotIn("services", rendered["prefill"])
        self.assertEqual([(r["id"], r["enabled"]) for r in rendered["prefill"]["routes"]["routes"]],
                         [("prefill_long", False), ("prefill_nodes", True)])
        self.assertIn("RTX PRO busy", state["recipe"]["service"]["why"])
        self.assertEqual(self.planned, [])

    def test_planner_refusal_skips_the_service_before_any_lease(self):
        self.plan_why = "no saved header descriptor; run `hyperloom recipe probe <profile> --descriptors`"
        state = self.up()
        verbs = [verb for _, verb, _ in self.calls]
        self.assertNotIn("service-render", verbs)
        self.assertIn("planner: no saved header descriptor", state["recipe"]["service"]["why"])

    def test_low_node_ram_after_load_stops_up(self):
        orch = self.harness.modules()[0]
        self.node_free = 6.0
        with self.assertRaisesRegex(orch.OrchestrationError, "free RAM 6.0 GiB after the engine loaded < floor 8"):
            self.up()
        self.assertNotIn("service-start", [verb for _, verb, _ in self.calls])
        self.assertTrue(all(row["state"] == "stopped" for row in self.harness.leases.values()))

    def test_recheck_at_launch_and_after_ready(self):
        with mock.patch.object(launch.rtx, "pro_busy", return_value="compute processes [9]"):
            state = self.up()
        self.assertNotIn("service-start", [verb for _, verb, _ in self.calls])
        self.assertIn("at launch: RTX PRO busy", state["recipe"]["service"]["why"])
        orch = self.harness.modules()[0]
        orch.down("ds41-rtx")
        self.calls.clear()
        def memory():
            started = any(verb == "service-start" for _, verb, _ in self.calls)
            return {"free_physical_gib": 6.0 if started else 100.0, "free_commit_gib": 150.0}
        with mock.patch.object(launch.rtx, "memory_status", side_effect=memory):
            state = self.up()
        verbs = [verb for _, verb, _ in self.calls]
        self.assertLess(verbs.index("service-start"), verbs.index("service-stop"))
        self.assertIn("after READY: free RAM 6.0", state["recipe"]["service"]["why"])


def _tensormeld_routes():
    """The router's selector from the pinned TensorMeld, or from HYPERLOOM_TM_SRC (a TensorMeld src dir)."""
    for root in (os.environ.get("HYPERLOOM_TM_SRC"), str(repo_root() / "third_party" / "tensormeld" / "src")):
        if root and (Path(root) / "tensormeld" / "prefill_routes.py").is_file():
            # Other tests may have imported the pinned tensormeld package already: import this one in isolation and
            # put theirs back.
            saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "tensormeld"}
            for name in saved:
                del sys.modules[name]
            sys.path.insert(0, root)
            try:
                return importlib.import_module("tensormeld.prefill_routes")
            finally:
                sys.path.remove(root)
                for name in [n for n in sys.modules if n.split(".")[0] == "tensormeld"]:
                    del sys.modules[name]
                sys.modules.update(saved)
    return None




def _check_table(table):
    """The router's own table parser when a TensorMeld with routes is reachable (HYPERLOOM_TM_SRC), else accept."""
    routes = _tensormeld_routes()
    if routes is None:
        return None
    try:
        routes.RouteTable.parse(table)
    except Exception as exc:  # noqa: BLE001 - its ValidationError comes from the isolated import
        return str(exc)
    return None


def _core():
    """The real recipe core (ts-recipe or ss-recipe) named by HYPERLOOM_TS_RECIPE or HYPERLOOM_SS_RECIPE, when it can
    run here."""
    executable = os.environ.get("HYPERLOOM_TS_RECIPE") or os.environ.get("HYPERLOOM_SS_RECIPE")
    if not executable or not Path(executable).is_file() or (executable.endswith(".exe") and os.name != "nt"):
        return None
    return core.CoreBackend(core.Core(executable))


EXAMPLES = BUILTIN_DIR   # strix-stream 4f95e1d recipes/examples, the deployable files, unchanged (portal catalog)
# What hyperloom knows of each family's engine today (facts.FAMILY_PREFILL_*): only DS has cited prefill facts.
LAB = {"deepseek-v4.1-flash": dict(topology="pair", caps=("import-state-parked",), prefill_batch=(1, None),
                                   prefill_chunk=4096),
       "minimax-m3.1-flash": dict(topology="pair", caps=(), prefill_chunk=None,
                                  prefill_batch=(None, "hyperloom has no cited prefill concurrency for minimax-pair")),
       "qwen3.8-flash-next": dict(topology="replicas", caps=(), prefill_chunk=1024,
                                  prefill_batch=(None, "hyperloom has no cited prefill concurrency for qwen-replicas"))}


class LabExampleTests(unittest.TestCase):
    """The deployable lab examples stay refused until their contracts and measurements exist (design-r14 v3, Sol-10):
    each refusal is pinned here; no fixture is edited to reach "admitted"."""

    def deploy(self, name, nodes_free=124.0):
        recipe = reader.parse((EXAMPLES / f"{name}.toml").read_text(encoding="utf-8"), name)
        lab = LAB[recipe["match"]["model"]]
        context = dataclasses.replace(ctx(capabilities=lab["caps"], model=recipe["match"]["model"]),
                                      topology=lab["topology"],
                                      limits=dict(LIMITS, prefill_batch=lab["prefill_batch"],
                                                  prefill_chunk=lab["prefill_chunk"]))
        avail = availability(hosts={"pc": {"free_ram_gib": 100.0, "free_commit_gib": 150.0},
                                    "n1": {"free_ram_gib": nodes_free, "free_commit_gib": nodes_free},
                                    "n2": {"free_ram_gib": nodes_free, "free_commit_gib": nodes_free}})
        bound = apply.bind(recipe, context, INVENTORY)
        return apply.deploy(recipe, context, bound, avail, SHA)

    def test_every_deployable_example_is_refused_with_its_reason(self):
        self.assertEqual(sorted(p.stem for p in EXAMPLES.glob("*.toml")),
                         ["deepseek-v4.1-flash.lab", "deepseek-v4.1-flash.lab-hetero", "minimax-m3.1-flash.lab",
                          "qwen3.8-flash-next.lab"])
        cases = [
            # Node charge = device budget (memory_min_gib) + overhead: the conservative model (R1/R2 await main).
            ("deepseek-v4.1-flash.lab", 124.0, r"decode_pair: host n1: free RAM 124.0 GiB - charge 105.0 GiB < floor 20"),
            ("qwen3.8-flash-next.lab", 124.0, r"decode_replicas: host n1: free RAM 124.0 GiB - charge 121.0 GiB < floor 8"),
            # With synthetic free RAM the next refusal is the recipe's own field (R3), still a refusal.
            ("deepseek-v4.1-flash.lab", 300.0, r"prefill_pair.placement.pair.chunk_tokens: 0 \(whole prompt\) != the "
                                               "engine's prefill pieces of 4096"),
            ("qwen3.8-flash-next.lab", 300.0, r"prefill_replicas.placement.nodes.max_batch: hyperloom has no cited "
                                              "prefill concurrency for qwen-replicas"),
            ("minimax-m3.1-flash.lab", 124.0, r"prefill_pair.placement.pair.max_batch: hyperloom has no cited prefill "
                                              "concurrency for minimax-pair"),
            ("deepseek-v4.1-flash.lab-hetero", 124.0, r"execute = hetero decode needs a profile whose engine runs it"),
        ]
        for name, free, why in cases:
            with self.subTest(name=name, free=free), self.assertRaisesRegex(RecipeError, why):
                self.deploy(name, nodes_free=free)


GRID_FULL = dict(tokens=(0, 4095, 4096, 4097, 30000), context=(None, 32769, 40000), gpu=(True, False),
                 n1=(True, False), link=(True, False), service=(True, False), stale=(False, True),
                 pc=(7.0, 40.0, 70.0), nodes=(None, 5.0, 20.0), disk=(None, True, False))
GRID_CORE = dict(tokens=(4095, 4096), context=(None, 40000), gpu=(True, False), n1=(True,), link=(True, False),
                 service=(True, False), stale=(False, True), pc=(7.0, 70.0), nodes=(None, 5.0, 20.0), disk=(None,))


class ConformanceTests(unittest.TestCase):
    """One admission rule: the router's per-request selector == the stub's §7 == the core's ss-recipe select
    (design §9.1). The stub's document and decision come from the core when it is present."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "mm.toml"
        self.path.write_text(PLAIN, encoding="utf-8")

    def states(self, bound, decision, sha, grid):
        for v in itertools.product(*grid.values()):
            case = dict(zip(grid, v))
            tokens = case["tokens"]
            context = tokens + 1024 if case["context"] is None else case["context"]
            devices = {GPU: {"available": case["gpu"]}, "n1": {"available": case["n1"]}, "n2": {"available": True}}
            if case["disk"] is not None:
                devices[GPU]["disk_available"] = case["disk"]
            links = {"pc-nodes": {"up": case["link"]}, "pair.internal": {"up": True}}
            hosts = {"pc": {"free_ram_gib": case["pc"]}}
            if case["nodes"] is not None:
                hosts.update(n1={"free_ram_gib": case["nodes"]}, n2={"free_ram_gib": 20.0})
            router_state = None if case["stale"] else {"devices": devices, "links": links, "hosts": hosts}
            # The core/stub see the router's own health as availability (the PRO set needs the service up) and the
            # service's routes as running while it is up.
            running = decision["running"] + ([apply.running_entry("prefill_long", sha, decision["requirements"])]
                                             if case["service"] else [])
            state = apply.admission_state(
                bound, {"devices": {} if case["stale"] else dict(devices, **{GPU: dict(devices[GPU], available=case["gpu"] and case["service"])}),
                        "links": {} if case["stale"] else links, "hosts": {} if case["stale"] else hosts},
                ctx(), running=running, requirements=decision["requirements"], tokens=tokens, context=context)
            yield case, tokens, context, router_state, state

    def prepare(self, backend):
        resolved = session.resolve(self.path, session.Layers(), backend=backend)
        bound = apply.bind(resolved.recipe, ctx(), INVENTORY)
        sha = SHA
        if isinstance(backend, core.CoreBackend):
            problems, binding, sha = backend.match(self.path, session.Layers(), apply.probe_report(resolved.recipe, bound, FACTS), None)
            self.assertEqual((problems, binding), ([], bound["binding"]))
        decision = apply.deploy(resolved.recipe, ctx(), bound, availability(), sha)
        return resolved, bound, decision, sha

    def test_router_equals_stub(self):
        routes = _tensormeld_routes()
        if routes is None:
            self.skipTest("TensorMeld without prefill_routes (set HYPERLOOM_TM_SRC to a src dir that has it)")
        resolved, bound, decision, sha = self.prepare(core.StubBackend())
        # The PRO route plans a file tier: its member needs disk_available too (Sol-6 #2).
        decision["requirements"] = apply.with_disk(decision["requirements"], {"prefill_long"}, "rtx", GIB_BYTES, sha)
        table = routes.RouteTable.parse(apply.route_table(resolved.to_json(), resolved.file_sha256, bound, decision,
                                                          "s.json", ctx()))
        checked = 0
        for case, tokens, context, router_state, state in self.states(bound, decision, sha, GRID_FULL):
            try:
                router = routes.select(table, tokens, context, router_state, "stale",
                                       routes.Health(service_up=case["service"], replica_up=True))[0].id
            except routes.NoRoute:
                router = None
            try:
                expected = reader.select(resolved.recipe, state, sha)["route"]
            except RecipeError:
                expected = None
            self.assertEqual(router, expected, case)
            checked += 1
        self.assertEqual(checked, 5 * 3 * 2 ** 5 * 3 * 3 * 3)

    def test_stub_equals_core(self):
        backend = _core()
        if backend is None:
            self.skipTest("no recipe core here (set HYPERLOOM_TS_RECIPE or HYPERLOOM_SS_RECIPE to its executable)")
        resolved, bound, decision, sha = self.prepare(backend)
        stub = reader.parse(PLAIN)
        self.assertEqual(resolved.recipe["routes"], stub["routes"])          # the core's document reads back the same
        for case, tokens, context, _, state in self.states(bound, decision, sha, GRID_CORE):
            try:
                expected = reader.select(resolved.recipe, state, sha)["route"]
            except RecipeError:
                expected = None
            try:
                got = backend.select(self.path, session.Layers(), resolved, state, sha)["route"]
            except RecipeError:
                got = None
            self.assertEqual(got, expected, case)


if __name__ == "__main__":
    unittest.main()
