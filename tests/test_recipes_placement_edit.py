"""Placement editing: layers, profile identity/guards, and saved copies (CPU only)."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from hyperloom.recipes import core, reader, service, session
from hyperloom.recipes.reader import RecipeError
from tests.recipe_fixtures import isolated


def document():
    return {"schema": "tierstream/recipe-v1", "name": "placement-edit",
            "match": {"model": "m", "engine": "e", "devices": [
                {"id": "anchor", "kind": "cuda", "memory_min_gib": 90},
                {"id": "aux", "kind": "cuda", "memory_min_gib": 12, "optional": True}]},
            "routes": [{"id": "decode", "phase": "decode", "devices": ["anchor"], "execute": "single",
                        "placement": {"anchor": {"max_context_tokens": 4096, "max_batch": 1}}}]}


def layer(text, sets, schema=reader.OVERRIDES_SCHEMA):
    return json.dumps({"schema": schema, "recipe": json.loads(text)["name"],
                       "recipe_sha256": hashlib.sha256(text.encode()).hexdigest(), "set": dict(sets)})


def tuning(text, sets, bound=False):
    marker = {"unbound": True}
    doc = json.loads(layer(text, sets, reader.PROFILE_SCHEMA))
    doc.update(workload="default", key={"engine_sha256": "a" * 64, "model_sha256": "b" * 64,
               "devices": {"anchor": ["gpu0"], "aux": ["gpu1"] if bound else marker},
               "drivers": {"anchor": "1", "aux": "1" if bound else marker},
               "descriptors": {"anchor": ["c" * 64], "aux": ["c" * 64] if bound else marker}},
               probe={"anchor": {"disk_read_gbytes_ps": 1, "h2d_gbytes_ps": 1},
                      "aux": {"disk_read_gbytes_ps": 1, "h2d_gbytes_ps": 1} if bound else marker})
    identity = {"workload": "default", "engine_sha256": "a" * 64, "model_sha256": "b" * 64,
                "binding": {"anchor": [{"id": "gpu0"}], "aux": [{"id": "gpu1"}] if bound else []},
                "descriptors": {"anchor": ["c" * 64], "aux": ["c" * 64] if bound else []},
                "devices": [{"id": ident, "driver": "1", "disk_read_gbytes_ps": 1, "h2d_gbytes_ps": 1}
                            for ident in ("gpu0", "gpu1")]}
    return doc, identity


EDITS = [("routes.decode.assist.aux.device_gib", 8), ("routes.decode.assist.aux.margin_gib", 0.25),
         ("routes.decode.assist.aux.margin_fraction", 0.01), ("routes.decode.components.drafter.device", "aux"),
         ("routes.decode.components.arena.units", 123), ("routes.decode.components.expert_file.units", 17),
         ("routes.decode.max_rows", 6), ("routes.decode.max_sequences", 2),
         ("routes.decode.placement.anchor.host_pageable_gib", 3),
         ("routes.decode.placement.anchor.host_available_floor_gib", 4)]


class PlacementEditTests(unittest.TestCase):
    def setUp(self):
        self.text = json.dumps(document())

    def test_paths_create_absent_tables_without_changing_the_base(self):
        base = reader.resolve(self.text)
        before = base.to_json()
        paths = reader.settable_paths(base.recipe)
        self.assertTrue(all(path in paths for path, _ in EDITS))
        service.fields(base.recipe)
        reader.explain(base)
        self.assertEqual(base.to_json(), before)
        self.assertNotIn("assist", before["routes"][0])
        edited = reader.resolve(self.text, overrides=[(layer(self.text, EDITS), "edit")])
        for path, value in EDITS:
            self.assertEqual(reader.value_of(edited.recipe, path), value, path)
        self.assertEqual(edited.recipe["routes"][0]["components"]["expert_file"]["device"], "auto")
        self.assertTrue(all(row["written"] is False for row in service.fields(base.recipe)
                            if row["path"] in dict(EDITS)))

    def test_structure_and_reserved_layers_are_not_editable(self):
        for path in ("routes.decode.assist.missing.device_gib", "routes.decode.assist.anchor.device_gib",
                     "routes.decode.components.unknown.units", "routes.decode.components.dense.layers",
                     "match.devices.aux.optional", "match.devices.aux.uuid", "routes.decode.execute"):
            with self.subTest(path=path), self.assertRaisesRegex(RecipeError, "not a settable"):
                reader.resolve(self.text, sets=[(path, 1)])

    def test_quota_replacement_and_same_layer_conflict(self):
        path = "routes.decode.components.arena."
        base = document()
        base["routes"][0]["components"] = {"arena": {"gib": 1}}
        text = json.dumps(base)
        result = reader.resolve(text, overrides=[(layer(text, [(path + "units", 123)]), "exact")])
        self.assertEqual(result.recipe["routes"][0]["components"]["arena"], {"device": "auto", "units": 123})
        result = reader.resolve(text, overrides=[(layer(text, [(path + "units", 123)]), "exact"),
                                                (layer(text, [(path + "gib", 2)]), "gib")])
        self.assertEqual(result.recipe["routes"][0]["components"]["arena"], {"device": "auto", "gib": 2})
        for entries in ([(path + "units", 123), (path + "gib", 2)],
                        [(path + "gib", 2), (path + "units", 123)]):
            with self.assertRaisesRegex(RecipeError, "PL_E_SCHEMA.*exclusive"):
                reader.resolve(text, overrides=[(layer(text, entries), "both")])
        with self.assertRaisesRegex(RecipeError, "PL_E_SCHEMA.*elastic"):
            reader.resolve(text, sets=[("routes.decode.components.dense.units", 1)])

    def test_unbound_profile_identity_and_transitions(self):
        for bound in (False, True):
            profile, identity = tuning(self.text, [("limits.host_free_ram_floor_gib", 9)], bound)
            result = reader.resolve(self.text, profile=(json.dumps(profile), "tune"), probe=identity)
            self.assertFalse(result.untuned)
            other = tuning(self.text, [], not bound)[1]
            result = reader.resolve(self.text, profile=(json.dumps(profile), "tune"), probe=other)
            self.assertTrue(result.untuned)
            self.assertIn("drop the profile or retune", result.refused[0])
        profile, identity = tuning(self.text, [])
        del profile["key"]["descriptors"]["aux"]
        self.assertFalse(reader.resolve(self.text, profile=(json.dumps(profile), "tune"), probe=identity).untuned)
        for scope in ("devices", "drivers"):
            bad = deepcopy(profile)
            bad["key"][scope]["aux"] = {"unbound": 1}
            with self.subTest(scope=scope), self.assertRaises(RecipeError):
                reader.resolve(self.text, profile=(json.dumps(bad), "tune"), probe=identity)

    def test_profile_guards_all_layout_scopes_in_both_orders(self):
        tuned_paths = ["routes.decode.components.arena.units", "routes.decode.assist.aux.device_gib",
                       "routes.decode.max_rows", "routes.decode.max_sequences",
                       "routes.decode.placement.anchor.reader_threads",
                       "routes.decode.placement.anchor.host_pageable_gib"]
        changed_paths = [path for path, _ in EDITS] + ["routes.decode.placement.anchor.device_gib",
                                                     "match.devices.anchor.name"]
        for tuned in tuned_paths:
            for changed in changed_paths:
                profile = {"kind": "profile", "origin": "tune", "set": [(tuned, 1)]}
                override = {"kind": "override", "origin": "edit", "set": [(changed, 1)]}
                for layers in ([profile, override], [override, profile]):
                    with self.subTest(tuned=tuned, changed=changed, first=layers[0]["kind"]):
                        with self.assertRaisesRegex(RecipeError, "drop the profile or retune"):
                            reader._check_profile_layers(layers)
        # An unrelated limit remains editable; an untuned route is not captured by another route's profile.
        profile["set"] = [("routes.other.components.arena.units", 1)]
        override["set"] = [("routes.decode.max_rows", 6)]
        reader._check_profile_layers([profile, override])
        profile, identity = tuning(self.text, [("routes.decode.components.arena.units", 123)])
        result = reader.resolve(self.text, profile=(json.dumps(profile), "tune"), probe=identity,
                                sets=[("limits.host_free_ram_floor_gib", 10)])
        self.assertFalse(result.untuned)

    def test_pinned_absent_table_does_not_activate_placement(self):
        doc = document()
        doc["pin"] = ["routes.decode.components"]
        text = json.dumps(doc)
        profile, identity = tuning(text, [("routes.decode.components.arena.units", 123)])
        result = reader.resolve(text, profile=(json.dumps(profile), "tune"), probe=identity)
        self.assertNotIn("components", result.recipe["routes"][0])
        row = next(r for r in reader.explain(result)["fields"] if r["path"].endswith("components.arena.units"))
        self.assertIn("pinned by recipe", row["shadowed"][0]["why"])

    def test_explain_layer_derive_reload_parity(self):
        with isolated():
            backend = core.StubBackend()
            service.save(None, "placement-base", text=self.text, backend=backend)
            saved = service.save_layer(None, "placement-edits", "placement-base", EDITS, backend=backend)
            effective = session.resolve(service.locate("placement-base"),
                                        session.Layers(overrides=[Path(saved["path"])]), backend=backend)
            rows = {r["path"]: r for r in effective.trace["fields"]}
            edits = [(path, rows[path]["value"]) for path, _ in EDITS]
            self.assertTrue(all(rows[path]["layer"] == "override" for path, _ in edits))
            copy = service.save(None, "placement-copy", base="placement-base",
                                base_sha256=hashlib.sha256(self.text.encode()).hexdigest(), sets=edits, backend=backend)
            reloaded = session.resolve(Path(copy["path"]), session.Layers(), backend=backend).to_json()
            expected = effective.to_json()
            expected["name"] = "placement-copy"
            self.assertEqual(reloaded, expected)
            alias = json.loads(Path(saved["path"]).read_text())
            alias["schema"] = "tierstream/recipe-overrides-v1"
            service.save_layer(None, "tierstream-layer", text=json.dumps(alias), backend=backend)

    def test_saved_profiles_check_paths_without_hardware(self):
        with isolated():
            backend = core.StubBackend()
            service.save(None, "placement-base", text=self.text, backend=backend)
            profile, _ = tuning(self.text, [("routes.decode.components.arena.units", 123)])
            profile["schema"] = "tierstream/recipe-profile-v1"
            service.save_layer(None, "placement-tune", text=json.dumps(profile), backend=backend)
            profile["set"] = {"routes.decode.components.arena.layers": [1]}
            with self.assertRaisesRegex(RecipeError, "not a settable"):
                service.save_layer(None, "bad-tune", text=json.dumps(profile), backend=backend)

    def test_derive_replaces_quota(self):
        doc = document()
        doc["routes"][0]["components"] = {"arena": {"gib": 1}}
        text, resolved = service.derive(json.dumps(doc), "base.json", "copy",
                                       [("routes.decode.components.arena.units", 123)], None, core.StubBackend())
        self.assertNotIn("gib", json.loads(text)["routes"][0]["components"]["arena"])
        self.assertEqual(resolved.recipe["routes"][0]["components"]["arena"]["units"], 123)

    def test_tierstream_layer_and_copy_parity(self):
        executable = os.environ.get("HYPERLOOM_TS_RECIPE")
        if not executable:
            self.skipTest("set HYPERLOOM_TS_RECIPE for CPU layer/copy parity")
        backend = core.CoreBackend(core.Core(executable))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "base.json"
            path.write_bytes(self.text.encode())
            override = Path(tmp) / "edits.json"
            override.write_bytes(layer(self.text, EDITS).encode())
            expected = session.resolve(path, session.Layers(overrides=[override]), backend=backend).to_json()
            local = reader.resolve(self.text, overrides=[(override.read_text(), "edits")]).to_json()
            self.assertEqual(local, expected)
            _, derived = service.derive(self.text, "base.json", "copy", EDITS, None, backend)
            expected["name"] = "copy"
            self.assertEqual(derived.to_json(), expected)
