"""`hyperloom recipe export` / `import` (hyperloom/recipes/bundle.py): a recipe and its overrides layers as one data-only
bundle, checked whole before anything is written into the user directories."""
import hashlib
import json
import unittest
from unittest import mock

from hyperloom import orchestrate
from hyperloom.recipes import bundle, service
from hyperloom.recipes.reader import RecipeError
from hyperloom.web import recipes as recipe_web
from tests.recipe_fixtures import isolated
from tests.test_recipes_service import QWEN, run_cli, user_ds

FLOOR = [("limits.host_free_ram_floor_gib", 8.0)]
FLOOR2 = [("limits.host_free_ram_floor_gib", 9.0)]


def data(document):
    return json.dumps(document).encode("utf-8")


def files(directory):
    return sorted(p.name for p in directory.iterdir() if p.suffix in (".json", ".toml")) if directory.is_dir() else []


class BundleTests(unittest.TestCase):
    def test_a_builtin_with_a_layer_round_trips_as_layers_only(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            layer = service.save_layer(cluster, "floor", QWEN, sets=FLOOR)
            exported = bundle.export(cluster, QWEN, [layer["name"]])
            self.assertEqual([entry["name"] for entry in exported["layers"]], ["floor"])
            text = json.dumps(exported)
            self.assertNotIn(str(env.root), text)                        # no local paths
            self.assertNotIn("path", {k for entry in exported["layers"] for k in entry})
            (service.layers_dir() / layer["name"]).unlink()
            result = bundle.import_(cluster, data(exported))
            self.assertEqual(result["present"], QWEN)                    # the built-in is not copied
            self.assertIsNone(result["recipe"])
            self.assertEqual([r["name"] for r in result["layers"]], [layer["name"]])

    def test_a_user_recipe_round_trips(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            exported = bundle.export(cluster, "ds-user")
            path.unlink()
            result = bundle.import_(cluster, data(exported), recipe_id="ds-shared")
            self.assertEqual((result["recipe"]["id"], result["recipe"]["name"]), ("ds-shared", "ds-user"))
            self.assertIn("ds-shared", {r["id"] for r in service.catalog(cluster)})

    def test_nothing_is_written_when_any_check_fails(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            exported = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.user_dir() / "ds-user.toml").unlink()
            service.save_layer(cluster, "floor", "deepseek-v4.1-flash.lab", sets=FLOOR, replace=hashlib.sha256(
                (service.layers_dir() / "floor.json").read_bytes()).hexdigest())   # the name now holds another layer
            before = sorted(p.name for p in service.user_dir().rglob("*"))
            with self.assertRaises(service.Conflict):
                bundle.import_(cluster, data(exported))
            tampered = json.loads(json.dumps(exported))
            tampered["recipe"]["text"] += "\n# changed\n"
            with self.assertRaisesRegex(RecipeError, "sha256"):
                bundle.import_(cluster, data(tampered))
            self.assertEqual(sorted(p.name for p in service.user_dir().rglob("*")), before)

    def test_tuning_profiles_are_not_bundled(self):
        # A tuning profile carries one machine's identity (engine, model, devices, drivers): neither exported nor
        # imported; nothing is written.
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            exported = bundle.export(cluster, "ds-user", ["floor.json"])
            profile = json.dumps({"schema": "strix-stream/recipe-profile-v1", "recipe": "ds-user", "set": {}})
            exported["layers"].append({"name": "tuned", "format": "json", "text": profile,
                                       "sha256": bundle._sha256(profile)})
            (service.user_dir() / "ds-user.toml").unlink()
            (service.layers_dir() / "floor.json").unlink()
            before = sorted(p.name for p in service.user_dir().rglob("*"))
            with self.assertRaisesRegex(RecipeError, "only overrides layers"):
                bundle.import_(cluster, data(exported))
            self.assertEqual(sorted(p.name for p in service.user_dir().rglob("*")), before)

    def test_a_name_taken_during_the_checks_writes_nothing(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            exported = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.user_dir() / "ds-user.toml").unlink()
            (service.layers_dir() / "floor.json").unlink()
            check = service.check_layer_document

            def racing(*args, **kwargs):          # another writer claims the layer name after the checks
                out = check(*args, **kwargs)
                (service.layers_dir() / "floor.json").write_text('{"another": "layer"}', encoding="utf-8")
                return out
            with mock.patch.object(service, "check_layer_document", side_effect=racing):
                with self.assertRaises(service.Conflict):
                    bundle.import_(cluster, data(exported))
            self.assertFalse((service.user_dir() / "ds-user.toml").exists())

    def test_running_an_interrupted_import_again_completes_it(self):
        # The second layer's write fails (a full disk, a stopped process): the recipe and the first layer are in
        # place and valid; the same import run again keeps them and writes the rest.
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            service.save_layer(cluster, "floor2", "ds-user", sets=FLOOR2)
            shared = bundle.export(cluster, "ds-user", ["floor.json", "floor2.json"])
            for name in ("floor.json", "floor2.json"):
                (service.layers_dir() / name).unlink()
            path.write_text(path.read_text(encoding="utf-8") + "\n# edited here since\n", encoding="utf-8")
            replace = hashlib.sha256(path.read_bytes()).hexdigest()
            publish = service._publish

            def failing(root, name, data_, current):
                if name.startswith("floor2"):
                    raise OSError(28, "No space left on device")
                return publish(root, name, data_, current)
            with mock.patch.object(service, "_publish", side_effect=failing):
                with self.assertRaises(OSError):
                    bundle.import_(cluster, data(shared), replace=replace)
            self.assertEqual(files(service.layers_dir()), ["floor.json"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), shared["recipe"]["sha256"])
            result = bundle.import_(cluster, data(shared), replace=replace)
            self.assertEqual(result["present"], "ds-user")                  # the recipe is already this text
            self.assertEqual(result["kept"], ["floor.json"])
            self.assertEqual([r["name"] for r in result["layers"]], ["floor2.json"])
            self.assertEqual(files(service.layers_dir()), ["floor.json", "floor2.json"])

    def test_a_replacement_never_changes_the_format(self):
        # TOML replaced by JSON (or back) would need two file operations; it is refused before any write.
        from hyperloom.recipes import reader, session
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            text = session.read_text(path)
            as_json = json.dumps(reader.load_text(text, path.name), indent=1) + "\n"
            shared = {"schema": bundle.SCHEMA, "layers": [],
                      "recipe": {"id": "ds-user", "name": "ds-user", "format": "json", "text": as_json,
                                 "sha256": bundle._sha256(as_json)}}
            with self.assertRaisesRegex(service.Conflict, "TOML and the bundle's is JSON"):
                bundle.import_(cluster, data(shared), replace=hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(files(service.user_dir()), ["ds-user.toml"])

    def test_export_checks_the_bytes_it_bundles(self):
        # The listing says overrides layer, but the file now holds a tuning profile: the bytes decide.
        from hyperloom.recipes import session
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            listing = service.layer_files()
            (service.layers_dir() / "floor.json").write_text(json.dumps(
                {"schema": "strix-stream/recipe-profile-v1", "recipe": "ds-user",
                 "recipe_sha256": hashlib.sha256(session.read_text(path).encode("utf-8")).hexdigest(), "set": {}}),
                encoding="utf-8")
            with mock.patch.object(service, "layer_files", return_value=listing):
                with self.assertRaisesRegex(RecipeError, "not bundled"):
                    bundle.export(cluster, "ds-user", ["floor.json"])

    def test_export_refuses_a_layer_of_another_version(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            path.write_text(path.read_text(encoding="utf-8") + "# a comment-only edit\n", encoding="utf-8")
            with self.assertRaisesRegex(RecipeError, "another version"):
                bundle.export(cluster, "ds-user", ["floor.json"])

    def test_export_refuses_a_name_that_is_not_an_id(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            outside = env.root / "my recipe.toml"
            outside.write_text(user_ds().read_text(encoding="utf-8"), encoding="utf-8")
            (service.user_dir() / "ds-user.toml").unlink()
            with self.assertRaisesRegex(RecipeError, "save the recipe under an id first"):
                bundle.export(cluster, str(outside))

    def test_export_refuses_ids_and_layer_names_import_refuses(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            text = user_ds().read_text(encoding="utf-8")
            for stem in ("list", "odd.", "con"):
                outside = env.root / f"{stem}.toml"
                outside.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(RecipeError, "save the recipe under an id first"):
                    bundle.export(cluster, str(outside))
            layer = service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            body = (service.layers_dir() / layer["name"]).read_text(encoding="utf-8")
            for name in ("my layer.json", "list.json"):
                (service.layers_dir() / name).write_text(body, encoding="utf-8")
                with self.assertRaisesRegex(RecipeError, "layer name"):
                    bundle.export(cluster, "ds-user", [name])
                (service.layers_dir() / name).unlink()
            (service.layers_dir() / "floor.toml").write_text(
                'schema = "strix-stream/recipe-overrides-v1"\nrecipe = "ds-user"\n[set]\n'
                '"limits.host_free_ram_floor_gib" = 8.0\n', encoding="utf-8")
            with self.assertRaisesRegex(RecipeError, "twice"):
                bundle.export(cluster, "ds-user", ["floor.json", "floor.toml"])

    def test_direct_reuse_still_refuses_a_duplicate_name(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            shared = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.layers_dir() / "floor.json").unlink()
            (service.user_dir() / "twin.toml").write_text(path.read_text(encoding="utf-8") + "# another text\n",
                                                          encoding="utf-8")
            with self.assertRaisesRegex(service.Conflict, 'name "ds-user" is taken by twin'):
                bundle.import_(cluster, data(shared))
            self.assertEqual(files(service.layers_dir()), [])

    def test_direct_reuse_checks_every_source_beyond_the_listing(self):
        # Another source holds the same id, or (beyond the bounded catalog listing) the same recipe name.
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            shared = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.layers_dir() / "floor.json").unlink()
            extra = env.root / "configured"
            extra.mkdir()
            text = path.read_text(encoding="utf-8")
            listed = service.directories

            def with_extra(cluster_=None, extra_=()):
                return listed(cluster_, extra_) + [("configured", extra)]
            with mock.patch.object(service, "directories", side_effect=with_extra):
                (extra / "ds-user.toml").write_text(text.replace('name = "ds-user"', 'name = "elsewhere"', 1),
                                                    encoding="utf-8")
                with self.assertRaisesRegex(service.Conflict, 'id "ds-user" is taken by configured'):
                    bundle.import_(cluster, data(shared))
                (extra / "ds-user.toml").unlink()
                (extra / "aa-first.toml").write_text(text.replace('name = "ds-user"', 'name = "aa-first"', 1),
                                                     encoding="utf-8")
                (extra / "zz-twin.toml").write_text(text + "# another text\n", encoding="utf-8")
                with mock.patch.object(service, "MAX_FILES", 1):          # the listing shows aa-first only
                    with self.assertRaisesRegex(service.Conflict, "taken by zz-twin"):
                        bundle.import_(cluster, data(shared))
            self.assertEqual(files(service.layers_dir()), [])

    def test_reuse_through_the_listing_is_checked_in_full(self):
        # The bundle's exact text is a configured recipe; beyond the bounded listing, another file has its name.
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            shared = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.layers_dir() / "floor.json").unlink()
            text = path.read_text(encoding="utf-8")
            path.unlink()
            first, second = env.root / "a", env.root / "b"
            first.mkdir()
            second.mkdir()
            (first / "aa-reuse.toml").write_text(text, encoding="utf-8")
            (second / "aa-other.toml").write_text(text.replace('name = "ds-user"', 'name = "aa-other"', 1),
                                                  encoding="utf-8")
            (second / "zz-twin.toml").write_text(text + "# another text\n", encoding="utf-8")
            listed = service.directories

            def with_extra(cluster_=None, extra_=()):
                return listed(cluster_, extra_) + [("configured", first), ("configured", second)]
            with mock.patch.object(service, "directories", side_effect=with_extra),                     mock.patch.object(service, "MAX_FILES", 1):
                with self.assertRaisesRegex(service.Conflict, "taken by zz-twin"):
                    bundle.import_(cluster, data(shared))
            self.assertEqual(files(service.layers_dir()), [])

    def test_a_full_user_directory_refuses_before_writing(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            shared = bundle.export(cluster, "ds-user")
            other = shared["recipe"]["text"].replace('name = "ds-user"', 'name = "ds-other"', 1)
            shared["recipe"].update(id="ds-other", name="ds-other", text=other, sha256=bundle._sha256(other))
            with mock.patch.object(service, "MAX_FILES", 1):
                with self.assertRaisesRegex(RecipeError, "the most the catalog lists"):
                    bundle.import_(cluster, data(shared), recipe_id="ds-other")
            self.assertEqual(files(service.user_dir()), ["ds-user.toml"])

    def test_reuse_is_decided_under_the_lock(self):
        # The identical user recipe disappears after the checks: the import writes it instead of reusing it.
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            service.save_layer(cluster, "floor", "ds-user", sets=FLOOR)
            exported = bundle.export(cluster, "ds-user", ["floor.json"])
            (service.layers_dir() / "floor.json").unlink()
            check = service.check_layer_document

            def deleting(*args, **kwargs):
                out = check(*args, **kwargs)
                path.unlink(missing_ok=True)
                return out
            with mock.patch.object(service, "check_layer_document", side_effect=deleting):
                result = bundle.import_(cluster, data(exported))
            self.assertIsNone(result["present"])
            self.assertEqual(result["recipe"]["id"], "ds-user")
            self.assertTrue(path.exists())

    def test_the_portal_takes_a_bundle_above_the_default_body_limit(self):
        from tests.test_web_recipes import Portal
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            padding = "".join(f"# padding line {i:05d} {'x' * 60}\n" for i in range(1000))
            path.write_text(path.read_text(encoding="utf-8") + padding, encoding="utf-8")
            exported = json.dumps(bundle.export(cluster, "ds-user"))
            self.assertGreater(len(exported), 64 * 1024)
            path.unlink()
            portal = Portal(env)
            try:
                status, doc = portal.request("POST", "recipes/import", {"text": exported})
            finally:
                portal.close()
            self.assertEqual((status, doc["exit_code"]), (200, 0), doc.get("error"))
            self.assertEqual(doc["result"]["recipe"]["id"], "ds-user")
        self.assertIn("recipes/import", recipe_web.LARGE_BODY)
        # A bundle at its limit, every byte escaped once inside the request's JSON, fits the request limit.
        self.assertLessEqual(2 * bundle.MAX_BUNDLE_BYTES + 1024, recipe_web.LARGE_BODY_BYTES)

    def test_an_export_import_would_refuse_is_refused(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = user_ds()
            path.write_text(path.read_text(encoding="utf-8") + "# " + "x" * (service.MAX_SAVE_BYTES + 10) + "\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(RecipeError, "import refuses"):
                bundle.export(cluster, "ds-user")
            # A bundle whose printed form fits only without Windows' newline expansion is refused too.
            path.write_text(path.read_text(encoding="utf-8").split("# xxx")[0], encoding="utf-8")
            printed = json.dumps(bundle.export(cluster, "ds-user"), indent=2)
            with mock.patch.object(bundle, "MAX_BUNDLE_BYTES", len(printed.encode("utf-8")) + 1):
                with self.assertRaisesRegex(RecipeError, "import refuses"):
                    bundle.export(cluster, "ds-user")

    def test_structure_and_limits(self):
        good = {"schema": bundle.SCHEMA, "recipe": {"id": "x", "name": "x", "format": "toml", "text": "a = 1\n",
                                                    "sha256": bundle._sha256("a = 1\n")}, "layers": []}
        bundle.read(data(good))
        for change, message in ((lambda b: b.update(extra=1), "exactly the keys"),
                                (lambda b: b.update(schema="other"), "schema"),
                                (lambda b: b["recipe"].update(format="json"), "format"),
                                (lambda b: b["recipe"].update(id="../x"), "id"),
                                (lambda b: b.update(layers=[dict(b["recipe"], name="l")] * 2), "exactly the keys"),
                                (lambda b: b.update(layers=[{"name": "l", "format": "toml", "text": "a = 1\n",
                                                             "sha256": bundle._sha256("a = 1\n")}] * 2), "twice")):
            bad = json.loads(json.dumps(good))
            change(bad)
            with self.assertRaisesRegex(RecipeError, message):
                bundle.read(data(bad))
        with self.assertRaisesRegex(RecipeError, "larger"):
            bundle.read(b" " * (bundle.MAX_BUNDLE_BYTES + 1))
        with self.assertRaisesRegex(RecipeError, "not JSON"):
            bundle.read(b'{"schema": NaN}')

    def test_a_layer_of_another_recipe_is_refused(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            layer = service.save_layer(cluster, "floor", QWEN, sets=FLOOR)
            with self.assertRaisesRegex(RecipeError, "applies to recipe"):
                bundle.export(cluster, "ds-user", [layer["name"]])
            with self.assertRaises(service.NotFound):
                bundle.export(cluster, "ds-user", ["missing.json"])

    def test_cli_and_portal(self):
        with isolated() as env:
            user_ds()
            out = env.root / "shared.json"
            code, result, err = run_cli("recipe", "export", "ds-user", "--out", str(out))
            self.assertEqual(code, 0, err)
            (service.user_dir() / "ds-user.toml").unlink()
            code, result, err = run_cli("recipe", "import", "-", stdin=out.read_text(encoding="utf-8"))
            self.assertEqual((code, result["recipe"]["id"]), (0, "ds-user"), err)
            request = recipe_web.Request("recipes/import", {"text": out.read_text(encoding="utf-8")}, config=env.config)
            self.assertEqual(request.argv[:3], ["recipe", "import", "-"])
            self.assertIsNotNone(request.stdin)
            with self.assertRaises(ValueError):                           # a path never comes from the client
                recipe_web.Request("recipes/import", {"bundle": str(out), "text": "{}"}, config=env.config)
            with self.assertRaises(ValueError):
                recipe_web.Request("recipes/import", {"bundle": str(out)}, config=env.config)
            self.assertIn("recipes/import", recipe_web.WRITES)
            self.assertIn("recipes/export", recipe_web.QUERIES)


if __name__ == "__main__":
    unittest.main()
