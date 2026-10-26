"""Operator picks use the core's emitted layer and semantic re-plan; no Hyperloom sizing fallback."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom import cli
from hyperloom.recipes import commands, core, reader, service, session, solver
from hyperloom.web.features import planner as web_planner
from tests import test_recipes


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recipe = self.root / "recipe.json"
        self.recipe.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        self.parsed = reader.parse(self.recipe.read_text())
        self.rid = self.parsed["routes"][0]["id"]
        self.bundle = self.root / "solver-inputs" / "cached.json"
        self.bundle.parent.mkdir()
        self.bundle.write_bytes(json.dumps({"probe": {"devices": []}, "inputs": {"choices": [
            {"id": "plain", "components": {"schema": "tierstream/components-v1"}}]},
            "desc": {"cached": True}, "context": {"schema": "tierstream/context-v1", "entries": []}}).encode())
        self.doc = {"schema": "tierstream/solver-v1", "solver_sha256": "a" * 64, "rows": [
            {"id": "plain", "choice": {"mode": "plain", "R": 1, "ctx": 8192, "batch": 1},
             "placement": {"arena_bytes": 100, "file_units": 0}, "requires": [],
             "prediction": {"tag": "unpredicted", "estimates": ["allhit_us"]}},
            {"id": "file", "choice": {}, "placement": {"file_units": 50},
             "prediction": {"tag": "knot", "milli_tok_s": 123456}, "requires": ["file_expert_tier"]},
            {"id": "refused", "choice": {}, "placement": {"refusal": {"code": "PL_E_BUDGET"}}}]}
        sets = {f"routes.{self.rid}.max_rows": 1, f"routes.{self.rid}.max_sequences": 1,
                f"routes.{self.rid}.placement.anchor.max_context_tokens": 8192,
                f"routes.{self.rid}.components.arena.units": 10,
                f"routes.{self.rid}.components.expert_file.units": 0}
        layer = json.loads(service._overrides_layer(self.parsed["name"], self.recipe.read_text(), list(sets.items()),
                                                   "Tierstream pick/re-plan"))
        self.emitted = ("# Tierstream emitted bytes; preserve this comment and field order.\n" +
                        "\n".join(f"{k} = {json.dumps(v)}" for k, v in layer.items() if k != "set") +
                        "\n[set]\n" + "\n".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sets.items()) + "\n").encode()
        self.calls = []
        self.error = None

        def run(argv, **kwargs):
            self.calls.append(argv)
            self.assertEqual(argv[1], "solve")
            self.assertIn("--desc", argv)
            self.assertIn("--context", argv)
            self.assertEqual(json.loads(Path(argv[argv.index("--inputs") + 1]).read_text())["choices"][0]["id"], "plain")
            if self.error:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.error)
            Path(argv[argv.index("--json-out") + 1]).write_bytes(json.dumps(self.doc).encode())
            if "--pick" in argv:
                Path(argv[argv.index("--emit-overrides") + 1]).write_bytes(self.emitted)
            return SimpleNamespace(returncode=0, stdout="rows, refusals, estimates, unpredicted\n", stderr="")

        self.backend = core.CoreBackend(core.Core("test-ts-recipe", run=run))
        # Resolution is a schema-only fake. Every place/solve/pick still goes through the actual CLI bridge above.
        self.backend.resolve = core.StubBackend().resolve
        for target, name, value in ((service, "locate", self.recipe), (commands, "runtime_dir", self.root),
                                    (service, "config_sha256", "b" * 64)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.configured = service.configured(self.root / "cluster.json")
        self.configured.__enter__()
        self.addCleanup(lambda: self.configured.__exit__(None, None, None))
        self.cluster = SimpleNamespace(raw={"pc": {}})

    def solve(self):
        return solver.solve(self.cluster, "recipe", self.rid, "cached", backend=self.backend)

    def test_rows_refusals_estimates_and_unpredicted_are_preserved_without_a_pick(self):
        result = self.solve()
        self.assertEqual([r["id"] for r in result["rows"]], ["plain", "file", "refused"])
        self.assertEqual(result["rows"][0]["tag"], "unpredicted")
        self.assertEqual(result["rows"][0]["estimates"], ["allhit_us"])
        self.assertTrue(result["rows"][0]["saveable"])
        self.assertFalse(result["rows"][1]["saveable"])
        self.assertEqual(result["rows"][2]["refusal"]["code"], "PL_E_BUDGET")
        self.assertNotIn("--pick", self.calls[0])
        self.assertFalse((self.root / "recipes/layers").exists())

    def test_pick_saves_exact_emitted_bytes_and_reload_parity(self):
        result = self.solve()
        with mock.patch.object(service, "catalog", return_value=[
                {"name": self.parsed["name"], "error": None, "path": str(self.recipe)}]):
            saved = solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
        self.assertEqual(Path(saved["path"]).read_bytes(), self.emitted)
        self.assertEqual(self.calls[-1][self.calls[-1].index("--pick") + 1], "plain")
        self.assertIn("--emit-overrides", self.calls[-1])
        resolved = session.resolve(self.recipe, session.Layers(overrides=[Path(saved["path"])]))
        route = resolved.recipe["routes"][0]
        self.assertEqual(route["max_rows"], 1)
        self.assertEqual(route["max_sequences"], 1)
        self.assertEqual(route["components"]["arena"]["units"], 10)
        self.assertEqual(route["components"]["expert_file"]["units"], 0)
        self.assertEqual(route.get("assist"), self.parsed["routes"][0].get("assist"))
        self.assertTrue(saved["replan_checked"])

    def test_hypothetical_refused_unknown_and_duplicate_ids_cannot_be_saved(self):
        result = self.solve()
        for row in ("file", "refused", "missing"):
            with self.subTest(row=row), mock.patch.object(service, "save_layer") as save:
                with self.assertRaisesRegex(reader.RecipeError, "PL_E_CAPABILITY"):
                    solver.pick(self.cluster, result["selection"], row, "picked", backend=self.backend)
                save.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.doc["rows"].append(deepcopy(self.doc["rows"][0]))
        result = self.solve()
        with self.assertRaisesRegex(reader.RecipeError, "PL_E_CAPABILITY"):
            solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)

    def test_stale_recipe_bundle_and_config_refuse_before_pick_or_save(self):
        for path in (self.recipe, self.bundle):
            result = self.solve()
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(path=path.name), mock.patch.object(service, "save_layer") as save:
                with self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
                    solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
                save.assert_not_called()
            path.write_bytes(original)
        result = self.solve()
        with mock.patch.object(service, "config_sha256", return_value="c" * 64):
            with self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
                solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
        self.assertTrue(all("--pick" not in call for call in self.calls))

    def test_core_replan_refusal_and_changed_result_never_save(self):
        result = self.solve()
        self.error = "PL_E_STALE picked row does not re-plan identically"
        with mock.patch.object(service, "save_layer") as save:
            with self.assertRaisesRegex(reader.RecipeError, "does not re-plan identically"):
                solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
            save.assert_not_called()
        self.error = None
        self.doc["rows"][0]["placement"]["arena_bytes"] += 1
        with mock.patch.object(service, "save_layer") as save:
            with self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
                solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
            save.assert_not_called()

    def test_referenced_components_costs_and_language_usage_are_frozen_and_checked(self):
        directory = self.bundle.parent / "documents"
        directory.mkdir()
        for name in ("components", "costs", "language"):
            (directory / (name + ".json")).write_bytes(json.dumps({"schema": name, "value": 1}).encode())
        inputs = directory / "inputs.json"
        inputs.write_bytes(json.dumps({"choices": [{"id": "plain", "components": "components.json"}],
                                       "costs": "costs.json", "usage_lang": {"en": "language.json"}}).encode())
        self.bundle.write_bytes(json.dumps({"probe": {"devices": []}, "inputs": "documents/inputs.json",
                                            "desc": {}, "context": {}}).encode())
        result = self.solve()
        plan = json.loads((self.root / "solver-plans" / (result["selection"] + ".json")).read_bytes())
        frozen = plan["snapshot"]["request"]["inputs"]
        self.assertEqual(frozen["costs"]["value"], 1)
        self.assertEqual(frozen["usage_lang"]["en"]["value"], 1)
        self.assertEqual(frozen["choices"][0]["components"]["value"], 1)
        for path in (directory / "components.json", directory / "costs.json", directory / "language.json"):
            raw = path.read_bytes()
            path.write_bytes(raw + b" ")
            with self.subTest(input=path.name), self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
                solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
            path.write_bytes(raw)

    def test_cached_configuration_revalidates_manifest_at_pick_time(self):
        request = json.loads(self.bundle.read_bytes())
        request["inputs"]["choices"][0]["components"] = {
            "schema": "hyperloom/components-ref-v1", "build_sha256": "a" * 64,
            "receipts_sha256": "b" * 64, "flags": {}}
        request["inputs"]["choices"][0]["selected_content"] = {"target": "local-only"}
        self.bundle.write_bytes(json.dumps(request).encode())
        store = mock.Mock()
        store.load.return_value = {"schema": "tierstream/components-v1"}
        with mock.patch.object(commands, "components_store", return_value=store):
            result = self.solve()
            store.load.side_effect = reader.RecipeError("PL_E_STALE manifest changed")
            with self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE manifest changed"):
                solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
    def test_incompatible_writer_cannot_enable_assists_or_change_budgets_or_destinations(self):
        result = self.solve()
        for path, value in ((f"routes.{self.rid}.assist.aux.device_gib", 20),
                            (f"routes.{self.rid}.placement.anchor.device_gib", 80),
                            (f"routes.{self.rid}.components.drafter.device", "aux")):
            self.emitted = service._overrides_layer(self.parsed["name"], self.recipe.read_text(), [(path, value)],
                                                    "incompatible writer").encode()
            with self.subTest(path=path), mock.patch.object(service, "save_layer") as save:
                with self.assertRaisesRegex(reader.RecipeError, "PL_E_CONFLICT"):
                    solver.pick(self.cluster, result["selection"], "plain", "picked", backend=self.backend)
                save.assert_not_called()

    def test_cli_and_web_use_the_same_pick_arguments(self):
        api = SimpleNamespace(config_path=self.root / "cluster.json")
        argv = web_planner.solve_argv(api, {"selection": "a" * 64, "row": "plain", "name": "picked"}, picking=True)
        args = cli.build_parser().parse_args(argv)
        self.assertEqual((args.selection, args.pick, args.save_layer), ("a" * 64, "plain", "picked"))
        for values in ({"recipe": "recipe", "route": "decode", "bundle": "../private"},
                       {"recipe": "recipe", "route": "decode", "bundle": "cached", "pick": "plain"}):
            with self.assertRaises(ValueError):
                web_planner.solve_argv(api, values)

    def test_no_stub_solver_fallback(self):
        with self.assertRaisesRegex(reader.RecipeError, "compatible ts-recipe"):
            solver.solve(self.cluster, "recipe", self.rid, "cached", backend=core.StubBackend())
