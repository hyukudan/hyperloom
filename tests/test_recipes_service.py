"""The recipe catalog and operations shared by the CLI and the portal (hyperloom/recipes/service.py): built-in
examples, ids and collisions, the user-directory writes, snapshots of launch inputs, plan / deploy, the launch record
and the route statistics. No machine is contacted: hardware facts are saved records."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from hyperloom import cli, orchestrate, render
from hyperloom.recipes import commands, launch, reader, service, session
from hyperloom.recipes.reader import RecipeError
from tests.recipe_fixtures import PRO, builtin_text, copy_of, isolated

QWEN = "qwen3.8-flash-next.lab"
FLOOR = session.Layers(sets=[("limits.host_free_ram_floor_gib", 8.0)])
# hl/recipes' DS fixture (its `up --recipe` tests deploy it on ds41-rtx), as a user recipe.
DS_FIXTURE = (Path(__file__).parent / "fixtures" / "recipes" / "ds41-lab.toml").read_text(encoding="utf-8")


def user_ds(name="ds-user"):
    service.user_dir().mkdir(parents=True, exist_ok=True)
    path = service.user_dir() / f"{name}.toml"
    path.write_text(DS_FIXTURE.replace('name = "ds41-lab"', f'name = "{name}"', 1), encoding="utf-8")
    return path
DS = "deepseek-v4.1-flash.lab"
MM = "minimax-m3.1-flash.lab"


def run_cli(*argv, stdin=None):
    """(exit code, stdout JSON or None, stderr) of `hyperloom ARGV` in this process."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), mock.patch("sys.stderr", err), mock.patch("sys.stdin", io.TextIOWrapper(
            io.BytesIO((stdin or "").encode("utf-8")), encoding="utf-8")):
        code = cli.main(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class BuiltinTests(unittest.TestCase):
    def test_vendored_examples_match_their_manifest(self):
        manifest = json.loads(service.BUILTIN_MANIFEST.read_text(encoding="utf-8"))
        files = {p.relative_to(service.BUILTIN_DIR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in service.BUILTIN_DIR.rglob("*.toml")}
        self.assertEqual(files, manifest["files"])
        self.assertTrue(all(b"\r\n" not in p.read_bytes() for p in service.BUILTIN_DIR.rglob("*.toml")))
        upstream = os.environ.get("HYPERLOOM_TIERSTREAM") or os.environ.get("HYPERLOOM_STRIX_STREAM")   # a checkout
        if upstream:
            for name in files:
                source = Path(upstream) / "recipes" / "examples" / name
                self.assertEqual(source.read_bytes().replace(b"\r\n", b"\n"),
                                 (service.BUILTIN_DIR / name).read_bytes(), name)

    def test_every_builtin_parses_and_pending_ones_name_their_blocker(self):
        with isolated():
            rows = {r["id"]: r for r in service.catalog()}
        self.assertEqual({r["status"] for r in rows.values()}, {"builtin", "pending"})
        self.assertTrue(all(r["error"] is None for r in rows.values()), [r["error"] for r in rows.values()])
        self.assertEqual(rows[QWEN]["model"], "qwen3.8-flash-next")
        pending = [r for r in rows.values() if r["status"] == "pending"]
        self.assertEqual(len(pending), 5)
        self.assertTrue(all(r["blocker"] for r in pending))

    def test_headline_only_from_enabled_routes(self):
        with isolated():
            rows = {r["id"]: r for r in service.catalog()}
        # The DS RTX route ships disabled: its 8K TTFT is not the headline; the pair's is.
        self.assertEqual(rows[DS]["headline"]["ttft_8k_s"]["route"], "prefill_pair")
        self.assertEqual(rows[QWEN]["headline"]["ttft_8k_s"], {"value": 2.94, "unit": "s", "basis": "measured",
                                                               "route": "prefill_rtx", "label": "TTFT",
                                                               "inherited": None})
        self.assertNotIn("memory_gib", rows[MM]["headline"])          # its VRAM row is the disabled RTX route's


class CatalogTests(unittest.TestCase):
    def test_sources_ids_and_collisions_keep_all_evidence(self):
        with isolated() as env:
            user = service.user_dir()
            user.mkdir(parents=True)
            configured = env.root / "operator"
            configured.mkdir()
            doc = json.loads(env.config.read_text())
            doc["pc"]["recipe_dirs"] = [str(configured)]
            env.config.write_text(json.dumps(doc))
            cluster = orchestrate.load_cluster(env.config)
            (user / "mine.toml").write_text(copy_of(QWEN, "mine"), encoding="utf-8")
            (configured / "MINE.toml").write_text(copy_of(QWEN, "other"), encoding="utf-8")   # same id, any case
            (user / "twin.toml").write_text(copy_of(QWEN, "same-name"), encoding="utf-8")
            (configured / "twin2.toml").write_text(copy_of(QWEN, "same-name"), encoding="utf-8")
            (user / "validate.toml").write_text(copy_of(QWEN, "reserved-id"), encoding="utf-8")
            rows = {(r["source"], r["id"]): r for r in service.catalog(cluster)}
            self.assertIn('duplicate recipe id "mine"', rows[("user", "mine")]["error"])
            self.assertIn('duplicate recipe id "mine"', rows[("configured", "MINE")]["error"])
            self.assertIn('duplicate recipe name "same-name"', rows[("user", "twin")]["error"])
            self.assertIn('duplicate recipe name "same-name"', rows[("configured", "twin2")]["error"])
            self.assertIn("reserved", rows[("user", "validate")]["error"])
            # pick (and so `up --recipe auto` and select) refuses the same files.
            results = {Path(r["path"]).name: r["error"] for r in session.candidates(commands.recipe_dirs(cluster))}
            self.assertIn("duplicate recipe id", results["mine.toml"])
            self.assertIn("duplicate recipe id", results["MINE.toml"])
            self.assertIn("duplicate recipe name", results["twin2.toml"])
            # A directory listed twice (the user dir also configured) is one source, never a self-collision.
            doc["pc"]["recipe_dirs"] = [str(user)]
            env.config.write_text(json.dumps(doc))
            again = service.catalog(orchestrate.load_cluster(env.config))
            self.assertEqual(sum(r["path"].endswith("mine.toml") for r in again), 1)

    def test_bounded_reads(self):
        with isolated():
            user = service.user_dir()
            user.mkdir(parents=True)
            (user / "big.toml").write_bytes(b"#" * (service.MAX_FILE_BYTES + 1))
            with mock.patch.object(service, "MAX_FILES", 2):
                for index in range(3):
                    (user / f"r{index}.toml").write_text(copy_of(QWEN, f"r{index}"), encoding="utf-8")
                rows = [r for r in service.catalog() if r["source"] == "user"]
            self.assertIn("not read", next(r for r in rows if r["id"] == "big")["error"])
            self.assertIn("more than 2 recipe files", rows[-1]["error"])

    def test_locate_ids_paths_and_pending(self):
        with isolated():
            self.assertEqual(service.locate(QWEN).name, QWEN + ".toml")
            path = service.BUILTIN_DIR / "pending" / "glm-5.3-flash.lab.toml"
            self.assertEqual(service.locate(str(path)), path)                          # a file is a file
            for ref in ("glm-5.3-flash.lab", str(path)):
                with self.subTest(ref=ref), self.assertRaisesRegex(RecipeError, "pending, it does not launch"):
                    service.locate(ref, for_up=True)
            with self.assertRaises(service.NotFound):
                service.locate("no-such.recipe")
            self.assertNotIn(service._key(service.PENDING_DIR), [service._key(d) for d in service.candidate_dirs()])

    def test_an_id_never_names_a_file(self):
        # A recipe id is a catalog lookup: a file of the working directory with that name is not consulted or read.
        with isolated() as env:
            here = env.root / "cwd"
            here.mkdir()
            (here / "evil").write_text(copy_of(QWEN, "evil"), encoding="utf-8")
            (here / "evil.toml").write_text(copy_of(QWEN, "evil"), encoding="utf-8")
            asked = []
            real = Path.is_file
            cwd = os.getcwd()
            os.chdir(here)
            try:
                with mock.patch.object(Path, "is_file", lambda p: asked.append(str(p)) or real(p)):
                    with self.assertRaises(service.NotFound):
                        service.locate("evil")
                    self.assertNotIn("evil", asked)
                    self.assertEqual(service.locate("evil.toml"), Path("evil.toml"))   # the CLI's FILE argument
                code, _, err = run_cli("recipe", "show", "evil", "--config", str(env.config))
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 3, err)
            # Not an id and not a FILE reference (no .toml/.json name, no directory part, or a name Windows would
            # shorten): refused without asking the file system.
            asked.clear()
            os.chdir(here)
            try:
                with mock.patch.object(Path, "is_file", lambda p: asked.append(str(p)) or real(p)):
                    for ref in ("evil ", "evil.", "evil.toml.", "evil.toml ", "e" * 70, "evil\0"):
                        with self.subTest(ref=ref), self.assertRaises(service.NotFound):
                            service.locate(ref)
                    self.assertEqual([p for p in asked if not Path(p).is_absolute() or Path(p).parent == here], [])
                    self.assertEqual(service.locate("./evil"), Path("evil"))          # a path is a FILE
                    (here / "UPPER.TOML").write_text(copy_of(QWEN, "upper"), encoding="utf-8")
                    self.assertEqual(service.locate("UPPER.TOML"), Path("UPPER.TOML"))   # any case of the suffix
            finally:
                os.chdir(cwd)
            for ref in ("x.toml", "x.JSON", "a/b", "a\\b", "C:x", "x:y", "..", "-x", "auto", "con"):
                with self.subTest(ref=ref), self.assertRaises(RecipeError):
                    service.check_id(ref)
            for ref in ("C:\\x.toml", "//host/share/x.toml", "x.toml", "x:y"):
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    service.argv("diff", {"a": QWEN, "b": ref}, config=env.config)
            self.assertIn("auto", service.argv("plan", {"recipe": "auto", "profile": "ds41-rtx"}, config=env.config))
            # Layer, facts and out names are files of hyperloom's directories: never a Windows device name, in any
            # case, with or without an extension.
            devices = ("CON", "con", "Nul", "nul.json", "PRN.toml", "aux.txt", "COM1", "com9.json", "LPT1",
                       "lpt9.toml", "con.x.json")
            for name in devices:
                for operation, arguments in (
                        ("plan", {"recipe": QWEN, "profile": "qwen-replicas", "overrides": [name]}),
                        ("plan", {"recipe": QWEN, "profile": "qwen-replicas", "tuning_profile": name}),
                        ("plan", {"recipe": QWEN, "profile": "qwen-replicas", "facts": name}),
                        ("probe", {"profile": "qwen-replicas", "out": name}),
                        ("probe", {"profile": "qwen-replicas", "evidence": ["halo-1=" + name]})):
                    with self.subTest(name=name, operation=operation, arguments=sorted(arguments)), \
                            self.assertRaisesRegex(ValueError, "device name"):
                        service.argv(operation, arguments, config=env.config)
            for name in ("console", "com10", "lpt", "nullx.json", "auxiliary.toml"):
                with self.subTest(name=name):
                    service.argv("probe", {"profile": "qwen-replicas", "out": name}, config=env.config)

    def test_storage_follows_the_config_in_use(self):
        with isolated() as env:
            other = env.root / "elsewhere" / "cluster.json"
            other.parent.mkdir()
            other.write_text(env.config.read_text())
            code, result, err = run_cli("recipe", "save", "mine", "--from", "-", "--config", str(other),
                                        stdin=copy_of(QWEN, "mine"))
            self.assertEqual(code, 0, err)
            self.assertEqual(Path(result["path"]).parent, other.parent / "recipes")
            self.assertFalse((env.config.parent / "recipes" / "mine.toml").exists())


class WriteTests(unittest.TestCase):
    def test_saved_name_is_checked(self):
        # The reader in use (the core when installed) resolved the text; the saved name is still checked here.
        with isolated():
            resolved = mock.Mock(recipe={"name": "../evil"})
            with mock.patch.object(service, "_resolve_text", return_value=resolved), \
                    self.assertRaisesRegex(RecipeError, "recipe name '../evil'"):
                service.save(None, "mine", text=copy_of(QWEN, "mine"))
            self.assertFalse((service.user_dir() / "mine.toml").exists())

    def test_save_replace_delete_with_sha(self):
        with isolated() as env:
            first = service.save(None, "qwen3.8-flash-next.mine", text=copy_of(QWEN, "mine"))
            self.assertEqual(Path(first["path"]).parent, service.user_dir().resolve())
            with self.assertRaisesRegex(service.Conflict, "pass its sha256"):
                service.save(None, "qwen3.8-flash-next.mine", text=copy_of(QWEN, "mine"))
            with self.assertRaisesRegex(service.Conflict, "changed since it was read"):
                service.save(None, "qwen3.8-flash-next.mine", text=copy_of(QWEN, "mine"), replace="0" * 64)
            second = service.save(None, "qwen3.8-flash-next.mine", text=copy_of(QWEN, "mine") + "\n# edited\n",
                                  replace=first["sha256"])
            self.assertTrue(second["replaced"])
            with self.assertRaisesRegex(service.Conflict, "does not exist"):
                service.save(None, "other.mine", text=copy_of(QWEN, "other"), replace=first["sha256"])
            with self.assertRaisesRegex(service.Conflict, "changed since it was read"):
                service.delete(None, "qwen3.8-flash-next.mine", first["sha256"])
            service.delete(None, "qwen3.8-flash-next.mine", second["sha256"])
            self.assertFalse(Path(second["path"]).exists())
            del env

    def test_never_over_another_source_and_names_are_unique(self):
        with isolated():
            with self.assertRaisesRegex(service.Conflict, "taken by builtin"):
                service.save(None, QWEN, text=copy_of(QWEN, "x"))
            with self.assertRaisesRegex(service.Conflict, 'recipe name "qwen3.8-flash-next-lab" is taken'):
                service.save(None, "copy", text=builtin_text(QWEN))
            with self.assertRaisesRegex(RecipeError, "only user recipes are deleted"):
                service.delete(None, QWEN, "0" * 64)
            for bad in ("../x", "a/b", ".hidden", "con", "NUL.lab", "x.", "validate", "-x", ""):
                with self.subTest(id=bad), self.assertRaises(RecipeError):
                    service.save(None, bad, text=copy_of(QWEN, "bad"))
            with self.assertRaisesRegex(RecipeError, "missing max_batch|max_batch|invalid|expected"):
                service.save(None, "broken", text=copy_of(QWEN, "broken").replace("max_batch = 8", ""))
            self.assertFalse((service.user_dir() / "broken.toml").exists())

    def test_sibling_suffixes_and_case_aliases(self):
        with isolated():
            service.user_dir().mkdir(parents=True)
            (service.user_dir() / "dup.toml").write_text(copy_of(QWEN, "a"), encoding="utf-8")
            (service.user_dir() / "dup.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(service.Conflict, "hold the same id"):
                service.delete(None, "dup", "0" * 64)
            saved = service.save(None, "Case.Id", text=copy_of(QWEN, "case"))
            with self.assertRaisesRegex(service.Conflict, "exists: pass its sha256"):
                service.save(None, "case.id", text=copy_of(QWEN, "case"))
            new_text = copy_of(QWEN, "case") + "\n# replaced through another case\n"
            replaced = service.save(None, "case.id", text=new_text, replace=saved["sha256"])
            self.assertTrue(replaced["replaced"])
            files = [p for p in service.user_dir().iterdir() if p.stem.casefold() == "case.id"]
            self.assertEqual(len(files), 1)                                    # one file, still there
            self.assertEqual(files[0].read_text(encoding="utf-8"), new_text)

    @unittest.skipIf(os.name == "nt" and not hasattr(os, "symlink"), "no symlinks")
    def test_link_and_overlapping_user_directories_are_refused(self):
        with isolated() as env:
            target = env.root / "elsewhere"
            target.mkdir()
            try:
                os.symlink(target, service.user_dir(), target_is_directory=True)
            except OSError:
                self.skipTest("symlinks need a privilege here")
            with self.assertRaisesRegex(RecipeError, "plain directory"):
                service.save(None, "x", text=copy_of(QWEN, "x"))
        with isolated() as env:
            doc = json.loads(env.config.read_text())
            doc["pc"]["recipe_dirs"] = [str(service.user_dir().parent)]
            env.config.write_text(json.dumps(doc))
            with self.assertRaisesRegex(RecipeError, "overlaps the configured directory"):
                service.save(orchestrate.load_cluster(env.config), "x", text=copy_of(QWEN, "x"))

    def test_concurrent_writers_one_name_one_winner(self):
        with isolated() as env:
            results = []

            def save(index):
                try:
                    service.save(None, f"t{index}", text=copy_of(QWEN, "contested"))
                    results.append("ok")
                except service.Conflict:
                    results.append("conflict")
            threads = [threading.Thread(target=save, args=(i,)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(results), ["conflict"] * 5 + ["ok"])
            # Another process (the CLI) competing with this one through the same file lock.
            procs = [subprocess.Popen([sys.executable, "-m", "hyperloom", "recipe", "save", f"p{i}", "--from", "-",
                                       "--config", str(env.config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) for i in range(3)]
            codes = []
            for proc in procs:
                proc.communicate(copy_of(QWEN, "contested-2").encode("utf-8"), timeout=120)
                codes.append(proc.returncode)
            self.assertEqual(sorted(codes), [0, 4, 4])

    def test_derived_copy_is_one_overrides_layer(self):
        mm_text = (Path(__file__).parent / "fixtures" / "recipes" / "minimax-lab.toml").read_text(encoding="utf-8")
        mm_text = mm_text.replace('name = "minimax-m3.1-flash-lab"', 'name = "mm-base"', 1)
        with isolated():
            service.user_dir().mkdir(parents=True)
            (service.user_dir() / "mm-base.toml").write_text(mm_text, encoding="utf-8")    # CRLF on Windows
            base_sha = hashlib.sha256((service.user_dir() / "mm-base.toml").read_bytes()).hexdigest()  # the raw file's
            group = "routes.prefill_long.placement.rtx.units.layers"
            pair = [(group + ".rank", "list"), (group + ".list", [3, 1, 2])]
            with self.assertRaisesRegex(RecipeError, "change together"):
                session.resolve(service.locate("mm-base"), session.Layers(sets=pair[1:]))      # a lone --set: refused
            copy = service.save(None, "mm-copy", base="mm-base", base_sha256=base_sha,
                                sets=pair + [("routes.prefill_long.when.prompt_tokens_min", 8192)])
            tree = json.loads(Path(copy["path"]).read_text(encoding="utf-8"))
            self.assertEqual(tree["name"], "mm-copy")
            self.assertEqual(tree["x-derived-from"]["sha256"], base_sha)
            self.assertNotIn("x-predicted", tree)
            self.assertIn("not re-measured", tree["x-derived-from"]["note"])
            resolved = session.resolve(Path(copy["path"]), session.Layers()).recipe
            self.assertEqual(resolved["routes"][0]["when"]["prompt_tokens_min"], 8192)
            with self.assertRaisesRegex(service.Conflict, "not at the version edited"):
                service.save(None, "mm-copy2", base="mm-base", base_sha256="0" * 64, sets=pair)
            with self.assertRaisesRegex(RecipeError, "not a settable path|is not settable"):
                service.save(None, "mm-copy3", base="mm-base", base_sha256=base_sha,
                             sets=[("routes.prefill_long.execute", "tp")])
            row = next(r for r in service.catalog() if r["id"] == "mm-copy")
            self.assertTrue(all(t.get("inherited") for t in row["tradeoffs"]))

    def test_layers_and_receipts(self):
        with isolated():
            saved = service.save_layer(None, "qwen-floor", QWEN, [("limits.host_free_ram_floor_gib", 6.0)])
            layer = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
            self.assertEqual((layer["schema"], layer["recipe"]), (service.OVERRIDES_SCHEMA, "qwen3.8-flash-next-lab"))
            with self.assertRaisesRegex(RecipeError, "not a settable path|is not settable"):
                service.save_layer(None, "bad", QWEN, [("routes.prefill_rtx.execute", "tp")])
            text = json.dumps(dict(layer, recipe="no-such-recipe"))
            with self.assertRaisesRegex(RecipeError, "no recipe named"):
                service.save_layer(None, "orphan", text=text)
            again = service.save_layer(None, "copy-floor", text=json.dumps(layer))
            self.assertEqual(again["recipe"], "qwen3.8-flash-next-lab")
            self.assertEqual({r["name"] for r in service.layer_files()}, {"qwen-floor.json", "copy-floor.json"})
            with self.assertRaisesRegex(service.Conflict, "changed"):
                service.delete_layer(None, "qwen-floor", "0" * 64)
            service.delete_layer(None, "qwen-floor", saved["sha256"])
            receipt = {"disk_read_gbytes_ps": 6.1, "method": "direct", "host": "halo-1", "path": "/m.gguf"}
            stored = service.save_receipt(None, "halo1-disk", json.dumps(receipt))
            self.assertEqual(Path(stored["path"]).parent, service.receipts_dir().resolve())
            with self.assertRaisesRegex(RecipeError, "needs"):
                service.save_receipt(None, "bad", json.dumps({"host": "x"}))
            for value in (float("nan"), float("inf"), 10 ** 400, 0, "6.1", True):
                with self.assertRaisesRegex(RecipeError, "is not a JSON number|finite number"):
                    service.save_receipt(None, "bad", json.dumps(dict(receipt, disk_read_gbytes_ps=value)))
            with self.assertRaisesRegex(RecipeError, "strings"):
                service.save_receipt(None, "bad", json.dumps(dict(receipt, host=["halo-1"])))
            # Files written by hand: bounded reads, each problem a row error, the whole file hashed.
            directory = service.receipts_dir()
            (directory / "huge.json").write_bytes(b" " * (service.MAX_RECEIPT_BYTES + 1) + b"{}")
            (directory / "nan.json").write_text(json.dumps(dict(receipt, disk_read_gbytes_ps=float("nan"))))
            rows = {r["name"]: r for r in service.receipt_files()}
            self.assertIn("larger than 64 KiB", rows["huge"]["error"])
            self.assertIn("NaN", rows["nan"]["error"])
            self.assertEqual(rows["halo1-disk"]["sha256"], hashlib.sha256(Path(stored["path"]).read_bytes()).hexdigest())
            self.assertIsNone(rows["halo1-disk"]["error"])

    def test_layers_read_both_schema_spellings_and_write_the_legacy_one(self):
        """tierstream/* layer ids are the same kinds as strix-stream/* (dispatch by kind, not by string); the layers
        the portal builds keep the legacy id and a given document is stored as it was given."""
        with isolated():
            saved = service.save_layer(None, "qwen-floor", QWEN, [("limits.host_free_ram_floor_gib", 6.0)])
            layer = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
            self.assertEqual(layer["schema"], "strix-stream/recipe-overrides-v1")
            renamed = json.dumps(dict(layer, schema="tierstream/recipe-overrides-v1"))
            again = service.save_layer(None, "ts-floor", text=renamed)
            self.assertEqual(again["recipe"], "qwen3.8-flash-next-lab")
            self.assertEqual(Path(again["path"]).read_bytes(), renamed.encode("utf-8"))
            # Checked like an overrides layer: resolved with its recipe.
            bad = json.dumps(dict(layer, schema="tierstream/recipe-overrides-v1",
                                  set={"routes.prefill_rtx.execute": "tp"}))
            with self.assertRaisesRegex(RecipeError, "not a settable path|is not settable"):
                service.save_layer(None, "ts-bad", text=bad)
            # A tuning profile in either spelling is checked as a profile: bound to the recipe file's sha256.
            for schema in ("strix-stream/recipe-profile-v1", "tierstream/recipe-profile-v1"):
                profile = {"schema": schema, "recipe": layer["recipe"], "recipe_sha256": "0" * 64, "source": "tune",
                           "set": {}}
                with self.assertRaisesRegex(reader.StaleProfile, "tuned on another version"):
                    service.save_layer(None, "ts-profile", text=json.dumps(profile))
            for schema in ("tierstream/recipe-v1", "tierstream/recipe-overrides-v2", "strix-stream/recipe-profile-v2"):
                with self.assertRaisesRegex(RecipeError, "schema must be strix-stream/recipe-profile-v1 or "
                                            "tierstream/recipe-profile-v1 or strix-stream/recipe-overrides-v1 or "
                                            "tierstream/recipe-overrides-v1"):
                    service.save_layer(None, "ts-unknown", text=json.dumps(dict(layer, schema=schema)))
            rows = {r["name"]: r["schema"] for r in service.layer_files()}
            self.assertEqual(rows, {"qwen-floor.json": "strix-stream/recipe-overrides-v1",
                                    "ts-floor.json": "tierstream/recipe-overrides-v1"})

    def test_receipts_never_write_into_a_read_only_source(self):
        with isolated() as env:
            doc = json.loads(env.config.read_text())
            service.receipts_dir().mkdir(parents=True)
            doc["pc"]["recipe_dirs"] = [str(service.receipts_dir())]
            env.config.write_text(json.dumps(doc))
            receipt = {"disk_read_gbytes_ps": 6.1, "method": "direct", "host": "halo-1", "path": "/m.gguf"}
            with self.assertRaisesRegex(RecipeError, "overlaps the configured directory"):
                service.save_receipt(orchestrate.load_cluster(env.config), "x", json.dumps(receipt))


class SharingTests(unittest.TestCase):
    """Windows: a reader holding a file makes os.replace/unlink fail for a moment (PermissionError); the recipe writes
    retry like the other state writers, and a failed snapshot publish says why instead of "no snapshot"."""

    def flaky(self, fail_first, code=13):
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(str(target))
            if len(calls) <= fail_first:
                raise OSError(code, "the process cannot access the file" if code == 13 else "no space left")
            return real(source, target)
        return mock.patch.object(os, "replace", side_effect=replace), calls

    def test_publish_and_unlink_retry_a_sharing_violation(self):
        with isolated():
            patch, calls = self.flaky(3)
            with patch:
                saved = service.save(None, "mine", text=copy_of(QWEN, "mine"))
            self.assertEqual(len(calls), 4)
            self.assertEqual(Path(saved["path"]).read_text(encoding="utf-8"), copy_of(QWEN, "mine"))
            self.assertEqual([p.name for p in service.user_dir().iterdir() if p.name.endswith(".part")], [])
            real, attempts = Path.unlink, []

            def unlink(path, *a, **kw):
                attempts.append(path.name)
                if path.name == "mine.toml" and len(attempts) <= 2:
                    raise PermissionError(13, "in use")
                return real(path, *a, **kw)
            with mock.patch.object(Path, "unlink", unlink):
                service.delete(None, "mine", saved["sha256"])
            self.assertFalse(Path(saved["path"]).exists())

    def test_a_jobs_cancel_marker_unlink_retries_and_never_replaces_the_result(self):
        with isolated():
            real, attempts = Path.unlink, []

            def unlink(fail):
                def call(path, *a, **kw):
                    if path.parent.name == "deploy-cancel" and path.suffix != ".json":
                        attempts.append(path.name)
                        if len(attempts) <= fail:
                            raise PermissionError(13, "in use")
                    return real(path, *a, **kw)
                return call
            for fail, gone in ((2, True), (10 ** 6, False)):
                with self.subTest(fail=fail):
                    attempts.clear()
                    marker = service.cancel_marker("ab12cd34")
                    with mock.patch.object(Path, "unlink", unlink(fail)), mock.patch("time.sleep"):
                        with service.launch_progress("ab12cd34", "up", "qwen-replicas") as progress:
                            marker.touch()                               # a cancel that came after the launch
                            result = "the launch's own answer"
                    self.assertEqual(result, "the launch's own answer")
                    self.assertEqual(progress.state["phase"], "done")
                    self.assertGreater(len(attempts), 1)
                    self.assertEqual(marker.exists(), not gone)
            # The launch's own error is not replaced by the unlink's either.
            with mock.patch.object(Path, "unlink", unlink(10 ** 6)), mock.patch("time.sleep"), \
                    self.assertRaisesRegex(RecipeError, "the launch failed"):
                with service.launch_progress("cd34ab12", "up", "qwen-replicas"):
                    service.cancel_marker("cd34ab12").touch()
                    raise RecipeError("the launch failed")

    def test_snapshot_publish_retries_and_reports_its_error(self):
        with isolated():
            patch, calls = self.flaky(2)
            with patch:
                digest = service.launch_inputs(None, service.locate(QWEN), session.Layers(),
                                               intent={"profile": "qwen-replicas"})
            self.assertEqual(len(calls), 3)
            service.load_inputs(digest)
            patch, calls = self.flaky(100, 28)                                  # not a sharing violation
            with patch, self.assertRaises(OSError) as raised:
                service.launch_inputs(None, service.locate(QWEN), session.Layers(),
                                      intent={"profile": "qwen-w5"})
            self.assertEqual((len(calls), raised.exception.errno), (1, 28))     # told at once, not retried
            self.assertNotIsInstance(raised.exception, service.NotFound)       # not "no snapshot"
            self.assertNotIn("no snapshot", str(raised.exception))
            leftovers = [p.name for p in (service.runtime_dir() / "recipe-inputs").iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [])


class SnapshotTests(unittest.TestCase):
    def test_layout_digest_and_immutability(self):
        with isolated() as env:
            first, second = env.root / "a", env.root / "b"
            first.mkdir(), second.mkdir()
            layer = {"schema": service.OVERRIDES_SCHEMA, "recipe": "qwen3.8-flash-next-lab", "set": {}}
            (first / "o.json").write_text(json.dumps(layer))
            (second / "o.json").write_text(json.dumps(dict(layer, source="other")))     # same basename
            layers = session.Layers(overrides=[first / "o.json", second / "o.json"], sets=[("limits.host_free_ram_floor_gib", 6.0)])
            digest = service.launch_inputs(None, service.locate(QWEN), layers, intent={"profile": "qwen-replicas"})
            root, manifest = service.load_inputs(digest)
            names = [e["name"] for e in manifest["entries"]]
            self.assertEqual(names, [f"recipe/{QWEN}.toml", "overrides/00/o.json", "overrides/01/o.json",
                                     "config/cluster.json"])
            self.assertEqual(manifest["intent"]["profile"], "qwen-replicas")
            self.assertEqual(service.launch_inputs(None, service.locate(QWEN), layers, intent={"profile": "qwen-replicas"}),
                             digest)
            for changed in (dict(intent={"profile": "qwen-w5"}), dict(intent={"profile": "qwen-replicas", "deadline_s": 600})):
                self.assertNotEqual(service.launch_inputs(None, service.locate(QWEN), layers, **changed), digest)
            other = session.Layers(overrides=layers.overrides, sets=[("limits.host_free_ram_floor_gib", 7.0)])
            self.assertNotEqual(service.launch_inputs(None, service.locate(QWEN), other, intent={"profile": "qwen-replicas"}),
                                digest)
            env.config.write_text(env.config.read_text().replace("18501", "18502"))
            self.assertNotEqual(service.launch_inputs(None, service.locate(QWEN), layers, intent={"profile": "qwen-replicas"}),
                                digest)
            (root / "overrides" / "00" / "o.json").write_text("{}")
            with self.assertRaisesRegex(RecipeError, "changed since the snapshot"):
                service.load_inputs(digest)

    def test_launch_reads_only_the_snapshot_and_its_config(self):
        with isolated() as env:
            service.user_dir().mkdir(parents=True)
            mine = service.user_dir() / "mine.toml"
            mine.write_text(copy_of(QWEN, "mine"), encoding="utf-8")
            digest = service.launch_inputs(None, mine, session.Layers(), intent={"profile": "qwen-replicas"})
            mine.write_text(copy_of(QWEN, "mine").replace("max_batch = 8", "max_batch = 4"), encoding="utf-8")
            env.config.write_text(env.config.read_text().replace('"qwen-replicas"', '"renamed"', 1))
            cluster, intent, options = service.options_from_inputs(digest)
            self.assertIn("qwen-replicas", cluster.profiles)                 # the snapshot's configuration
            self.assertIn("max_batch = 8", Path(options.recipe).read_text(encoding="utf-8"))
            self.assertEqual(intent["profile"], "qwen-replicas")
            with self.assertRaisesRegex(RecipeError, "planned for profile qwen-replicas"):
                service.options_from_inputs(digest, "ds41-pair")

    def test_retention_keeps_referenced_and_recent(self):
        with isolated():
            old = service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "a"})
            kept = service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "b"})
            stale = time.time() - service.KEEP_SNAPSHOT_S - 60
            for digest in (old, kept):
                os.utime(service.runtime_dir() / "recipe-inputs" / digest, (stale, stale))
            states = {"b": {"profile": "b", "recipe": {"inputs_digest": kept}}}
            with mock.patch.object(service, "_states", return_value=states):
                service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "c"})
            self.assertFalse((service.runtime_dir() / "recipe-inputs" / old).exists())
            self.assertTrue((service.runtime_dir() / "recipe-inputs" / kept).exists())
            # A launch in progress pins its snapshot (its state does not name it yet).
            busy = service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "d"})
            os.utime(service.runtime_dir() / "recipe-inputs" / busy, (stale, stale))
            with mock.patch.object(service, "MAX_SNAPSHOTS", 0):
                first, second = service.acquire_pin(busy), service.acquire_pin(busy)     # two overlapping launches
                service.release_pin(second)
                service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "e"})
                self.assertTrue((service.runtime_dir() / "recipe-inputs" / busy).exists())   # first still holds it
                service.release_pin(first)
                service.launch_inputs(None, service.locate(QWEN), session.Layers(), intent={"profile": "f"})
                self.assertFalse((service.runtime_dir() / "recipe-inputs" / busy).exists())
                with self.assertRaises(service.NotFound):
                    service.acquire_pin(busy)                     # a pruned snapshot cannot be pinned (plan again)
                # `up --recipe` holds its snapshot's pin from publication until it returns.
                _, _, options = service.up_options(None, QWEN, "qwen-replicas")
                self.assertTrue(Path(options.pin).exists())
                service.release_pin(options.pin)


class PlanDeployTests(unittest.TestCase):
    def facts_source(self, env, model, profile):
        return session.saved_source(env.save_facts("f", model, profile))

    def test_plan_is_conditional_preflight_with_a_preview(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            source = self.facts_source(env, "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            result = service.plan(cluster, "ds-user", "ds41-rtx", source=source)
            self.assertFalse(result["up"]["ok"])                         # the pinned router has no routes policy
            self.assertIn("prefill policy", result["up"]["refused"])
            self.assertIn("conditional preflight", result["up"]["conditions"])
            self.assertIn("prelaunch", " ".join(result["checked_at_start"]))
            self.assertEqual(result["preview"]["evaluation"]["recipe"], "ds-user")
            self.assertTrue(result["file"].endswith("ds-user.toml"))       # originals, not snapshot paths
            with mock.patch.object(render, "router_policies", return_value=("nodes", "routes")):
                ready = service.plan(cluster, "ds-user", "ds41-rtx", source=source)
            self.assertTrue(ready["up"]["ok"], ready["up"])
            self.assertEqual(ready["intent"]["profile"], "ds41-rtx")
            # The built-in lab recipe: refused with the reasons (as the CLI gives them), never a silent pass.
            builtin = service.plan(cluster, DS, "ds41-pair", source=self.facts_source(env, "deepseek-v4.1-flash", "ds41-pair"))
            self.assertFalse(builtin["up"]["ok"])
            mm = service.plan(cluster, MM, "mm", source=self.facts_source(env, "minimax-m3.1-flash", "mm"))
            self.assertIn("bind loopback", mm["up"]["refused"])
            pending = service.plan(cluster, "glm-5.3-flash.lab", "ds41-rtx", source=source)
            self.assertIn("pending", pending["up"]["refused"])

    def test_deploy_refuses_before_stopping_anything(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            source = self.facts_source(env, "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            with mock.patch.object(render, "router_policies", return_value=("nodes", "routes")):
                digest = service.plan(cluster, "ds-user", "ds41-rtx", source=source)["inputs"]
                fake = mock.Mock(wraps=orchestrate)
                fake.down.return_value = {"stopped": True}
                fake.up.return_value = {"state": "running"}
                running = {"qwen-replicas": {"profile": "qwen-replicas"}}
                with mock.patch.object(service, "_states", return_value=running):
                    with self.assertRaisesRegex(service.Conflict, "running profiles are qwen-replicas"):
                        service.deploy(digest, [], source=source, orchestrate=fake)
                    fake.down.assert_not_called()
                    with mock.patch.object(launch, "prepare", side_effect=RecipeError("planner refused")):
                        with self.assertRaisesRegex(RecipeError, "planner refused"):
                            service.deploy(digest, ["qwen-replicas"], source=source, orchestrate=fake)
                    fake.down.assert_not_called()
                    env.config.write_text(env.config.read_text().replace("18601", "18602"))   # config edited later
                    result = service.deploy(digest, ["qwen-replicas"], source=source, orchestrate=fake)
            fake.down.assert_called_once_with("qwen-replicas")
            self.assertIsNotNone(fake.up.call_args.kwargs["cancelled"])
            launched_cluster, profile = fake.up.call_args.args
            self.assertEqual(profile, "ds41-rtx")
            self.assertIn('"port": 18601', json.dumps(launched_cluster.raw))                    # the snapshot's config
            self.assertEqual(fake.up.call_args.kwargs["recipe"].digest, digest)
            self.assertEqual(result["inputs"], digest)

    def test_up_recipe_inputs_takes_everything_from_the_snapshot(self):
        with isolated() as env:
            digest = service.launch_inputs(None, service.locate(DS), session.Layers(), intent={"profile": "ds41-pair"})
            for extra in (["--recipe", DS], ["--node", "halo-1"], ["--router", "halo-1"], ["--recipe-set", "a=1"]):
                with self.subTest(extra=extra):
                    code, _, err = run_cli("up", "--recipe-inputs", digest, *extra, "--config", str(env.config))
                    self.assertEqual(code, 1)
                    self.assertIn("planned snapshot as it is", err)
            with mock.patch.object(orchestrate, "up", return_value={"state": "running"}) as up:
                code, _, err = run_cli("up", "--recipe-inputs", digest, "--config", str(env.config))
            self.assertEqual(code, 0, err)
            self.assertEqual(up.call_args.args[1], "ds41-pair")
            self.assertEqual(up.call_args.kwargs["recipe"].digest, digest)
            with mock.patch.object(orchestrate, "up", return_value={}) as up:
                code, _, err = run_cli("up", "--recipe", DS, "--config", str(env.config))   # profile from the model
            self.assertEqual(code, 1)
            self.assertIn("name the profile", err)                      # ds41-pair and ds41-rtx both serve it
            with mock.patch.object(orchestrate, "up", return_value={}) as up:
                code, _, err = run_cli("up", "--recipe", "glm-5.3-flash.lab", "--config", str(env.config))
            self.assertIn("pending", err)
            up.assert_not_called()


class DeployRefusalTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.env = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.source = session.saved_source(self.env.save_facts("f", "deepseek-v4.1-flash", "ds41-rtx"))
        user_ds()
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            self.digest = service.plan(orchestrate.load_cluster(self.env.config), "ds-user", "ds41-rtx",
                                       source=self.source)["inputs"]
        self.fake = mock.Mock(wraps=orchestrate)
        self.fake.down.return_value = {}
        self.fake.up.return_value = {}
        patcher = mock.patch.object(service, "_states", return_value={"qwen-replicas": {"profile": "qwen-replicas"}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def deploy(self, **kw):
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            return service.deploy(self.digest, ["qwen-replicas"], source=self.source, orchestrate=self.fake, **kw)

    def test_bad_approval_refuses_before_stopping(self):
        approval = {"ref": "x", "lease_s": 1, "end_epoch": time.time() + 600}
        with self.assertRaisesRegex(ValueError, "approval needs"):
            self.deploy(approval=approval)
        self.fake.down.assert_not_called()

    def test_a_target_state_of_another_configuration_refuses_before_stopping(self):
        state = orchestrate._state_path("ds41-rtx")
        state.parent.mkdir(parents=True, exist_ok=True)
        for owner, why in ((None, "unknown configuration .*stop it first"),
                           (orchestrate.config_record(self.env.root / "other" / "cluster.json"), "another configuration")):
            state.write_text(json.dumps({"profile": "ds41-rtx", "config": owner}), encoding="utf-8")
            with self.assertRaisesRegex(service.Conflict, why):
                self.deploy()
        state.unlink()
        self.fake.down.assert_not_called()
        self.fake.up.assert_not_called()

    def test_cancel_before_any_stop_and_before_the_launch(self):
        job = "ab12cd34"
        marker = service.cancel_marker(job)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with self.assertRaisesRegex(RecipeError, "deploy cancelled"):
            self.deploy(job=job)
        self.fake.down.assert_not_called()
        self.fake.down.side_effect = lambda name: marker.touch() or {}      # cancelled while stopping
        with self.assertRaisesRegex(RecipeError, "deploy cancelled"):
            self.deploy(job=job)
        self.fake.up.assert_not_called()
        self.assertFalse(marker.exists())
        # Cancelled during the hand-over: up gets the check and starts nothing.
        self.fake.down.side_effect = None
        self.fake.up.side_effect = lambda *a, **kw: kw["cancelled"]() and (_ for _ in ()).throw(
            orchestrate.OrchestrationError("profile stop requested"))
        self.fake.down.side_effect = lambda name: {}
        marker.touch()
        marker.unlink()
        self.deploy(job=job)                                                    # not cancelled: launches
        self.fake.up.assert_called_once()

    def test_up_honors_cancel_right_after_taking_the_profile(self):
        cluster = orchestrate.load_cluster(self.env.config)
        with mock.patch.object(orchestrate, "check_launch", side_effect=orchestrate.check_launch) as checks, \
                mock.patch.object(orchestrate, "_start_up_watch") as watch:
            with self.assertRaisesRegex(Exception, "stop requested"):
                orchestrate.up(cluster, "qwen-replicas", cancelled=lambda: True)
        watch.assert_not_called()                                               # nothing started
        self.assertFalse(orchestrate._state_path("qwen-replicas").exists())


class RunningTests(unittest.TestCase):
    def record(self, env):
        cluster = orchestrate.load_cluster(env.config)
        source = session.saved_source(env.save_facts("f", "deepseek-v4.1-flash", "ds41-rtx"))
        mine = user_ds("mine")
        _, _, options = service.up_options(cluster, str(mine), "ds41-rtx")
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            prepared = launch.prepare(cluster, "ds41-rtx", options, router="local", vram_cap_gib=None,
                                      state_dir=env.root, source=source)
        return mine, prepared.record()

    def test_active_is_the_launch_as_recorded(self):
        with isolated() as env:
            mine, record = self.record(env)
            states = {"ds41-rtx": {"profile": "ds41-rtx", "phase": "running", "recipe": record}}
            row = service.active(None, states=states)["deployments"][0]
            self.assertEqual(row["recipe"], "mine")
            self.assertEqual(row["effective"]["name"], "mine")
            self.assertEqual({i["now"] for i in row["inputs"]}, {"unchanged"})
            self.assertTrue(record["inputs_digest"])
            mine.write_text(copy_of(DS, "mine") + "\n# edited\n", encoding="utf-8")
            self.assertEqual(service.active(None, states=states)["deployments"][0]["inputs"][0]["now"],
                             "changed since the launch")
            mine.unlink()
            row = service.active(None, states=states)["deployments"][0]
            self.assertEqual(row["inputs"][0]["now"], "deleted since the launch")
            self.assertEqual(row["effective"]["name"], "mine")                    # still explained as launched

    def test_routes_split_rtx_and_nodes_from_the_record(self):
        with isolated() as env:
            _, record = self.record(env)
            record["route_table"]["state_file"] = str(env.root / "route-state.json")
            (env.root / "route-state.json").write_text(json.dumps({"at": time.time() - 2, "devices": {}}))
            places = {p["id"]: p["place"] for p in record["deploy"]["prefill"]}
            states = {"ds41-rtx": {"profile": "ds41-rtx", "recipe": record, "router": {"endpoint": "127.0.0.1:1"}}}
            status = {"prefill_routes": {"effective_sha256": record["route_table"]["effective_sha256"],
                                         "admitted": {r: 3 for r in places}, "refused": 1, "handoff_failed": 0,
                                         "recent": [{"id": "r1", "route": "prefill_pair", "outcome": "served"}]}}
            row = service.routes(None, states=states, router_status=lambda e: status)["deployments"][0]
            self.assertEqual(row["totals"]["nodes"] + row["totals"]["rtx"], 3 * len(places))
            self.assertEqual(row["totals"]["refused"], 1)
            self.assertEqual(row["recent"][0]["on"], "nodes")
            self.assertLess(row["state"]["age_s"], 10)
            self.assertNotIn("identity", row["why"])
            status["prefill_routes"].pop("recent")
            status["prefill_routes"]["effective_sha256"] = "0" * 64
            row = service.routes(None, states=states, router_status=lambda e: status)["deployments"][0]
            self.assertIn("another route table", row["why"]["identity"])
            self.assertIn("R7", row["why"]["recent"])
            row = service.routes(None, states=states, router_status=mock.Mock(side_effect=OSError("refused")))
            self.assertIn("router status unavailable", row["deployments"][0]["why"]["stats"])


class FactsTests(unittest.TestCase):
    def test_saved_facts_say_when_they_were_observed(self):
        with isolated() as env:
            path = env.save_facts("f", "qwen3.8-flash-next", "qwen-replicas")
            source = session.saved_source(path)
            ctx = mock.Mock(profile="qwen-replicas")
            notes = source(ctx)[3]
            self.assertRegex(notes[-1], r"observed \d{4}-\d\d-\d\d .*\(saved, not live\)")
            self.assertFalse(any(a.option_strings == ["--facts"] for a in cli.build_parser()._subparsers._group_actions[0]
                                 .choices["up"]._actions))                     # a launch always gathers fresh facts


if __name__ == "__main__":
    unittest.main()
