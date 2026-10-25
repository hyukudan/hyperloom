"""`hyperloom whatif` (cliext/planner.py): variants of one recipe evaluated by the recipe command lines on saved
hardware facts. No machine is contacted: facts are saved records and the stub reader is used."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from hyperloom import cli, render
from hyperloom.cliext import planner
from hyperloom.recipes import service
from tests.recipe_fixtures import isolated

DS_FIXTURE = (Path(__file__).parent / "fixtures" / "recipes" / "ds41-lab.toml").read_text(encoding="utf-8")
FLOOR = "limits.host_free_ram_floor_gib"


def user_ds(name="ds-user"):
    service.user_dir().mkdir(parents=True, exist_ok=True)
    path = service.user_dir() / f"{name}.toml"
    path.write_text(DS_FIXTURE.replace('name = "ds41-lab"', f'name = "{name}"', 1), encoding="utf-8")
    return path


def run_cli(*argv, stdin=None):
    """(exit code, stdout JSON or None, stderr) of `hyperloom ARGV` in this process."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), mock.patch("sys.stderr", err), mock.patch("sys.stdin", io.StringIO(stdin or "")):
        code = cli.main(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class ValueTests(unittest.TestCase):
    def test_split_values_respects_brackets_and_quotes(self):
        self.assertEqual(planner.split_values("1, 2,3"), ["1", "2", "3"])
        self.assertEqual(planner.split_values('[1, 2], "a,b", x'), ["[1, 2]", '"a,b"', "x"])
        for bad in ("1,,2", "[1, 2", '"open', ""):
            with self.assertRaises(planner.Refused):
                planner.split_values(bad)

    def test_vary_values_read_like_recipe_set(self):
        vary = planner.parse_vary([f"{FLOOR}=8,16.5", "routes.r.enabled=true,false", "routes.r.fallback=nodes"])
        self.assertEqual(vary, [(FLOOR, [8, 16.5]), ("routes.r.enabled", [True, False]),
                                ("routes.r.fallback", ["nodes"])])
        with self.assertRaisesRegex(planner.Refused, "varied twice"):
            planner.parse_vary([f"{FLOOR}=1", f"{FLOOR}=2"])
        with self.assertRaisesRegex(planner.Refused, "PATH=V1"):
            planner.parse_vary(["no-equals"])
        with self.assertRaisesRegex(planner.Refused, "not a plain value"):
            planner.parse_vary(["x=1979-05-27T07:32:00Z"])          # a TOML datetime is not a JSON value

    def test_variants_are_the_base_and_every_combination_bounded(self):
        variants = planner.build_variants([("a.x", [1, 2]), ("b.y", [True, False])], [])
        self.assertEqual([v["id"] for v in variants], ["base", "v1", "v2", "v3", "v4"])
        self.assertEqual(variants[0]["set"], {})
        self.assertEqual(variants[4]["set"], {"a.x": 2, "b.y": False})
        self.assertEqual(len({v["name"] for v in variants}), 5)
        with self.assertRaisesRegex(planner.Refused, "13 variants"):
            planner.build_variants([("a.x", list(range(4))), ("b.y", [1, 2, 3])], [])
        explicit = planner.parse_variants(json.dumps({"variants": [{"name": "low floor", "set": {FLOOR: 4}}]}))
        both = planner.build_variants([("a.x", [1])], explicit)
        self.assertEqual([v["name"] for v in both], ["base (as written)", "a.x=1", "low floor"])
        self.assertEqual(planner.default_name({"routes.prefill_rtx.when.prompt_tokens_min": 2048}),
                         "prefill_rtx.prompt_tokens_min=2048")

    def test_explicit_variants_are_strict(self):
        for text, message in (('{"variants": [{"set": {"a": 1}, "set": {}}]}', "duplicate"),
                              ('{"variants": [{"set": {"a": NaN}}]}', "not allowed"),
                              ('{"variants": [{"set": {}}]}', "settable paths"),
                              ('{"variants": [{"set": {"a": 1}, "extra": 1}]}', "expected"),
                              (json.dumps({"variants": [{"set": {"a": 1}}] * 12}), "at most 11")):
            with self.assertRaisesRegex(planner.Refused, message):
                planner.parse_variants(text)


class FactsTests(unittest.TestCase):
    def test_last_saved_facts_of_the_profile(self):
        with isolated() as env:
            old = env.save_facts("old", "deepseek-v4.1-flash", "ds41-rtx")
            new = env.save_facts("new", "deepseek-v4.1-flash", "ds41-rtx")
            env.save_facts("other", "qwen3.8-flash-next", "qwen-replicas")
            for path, when in ((old, 1000.0), (new, 2000.0)):
                record = json.loads(path.read_text())
                record["observed_at"] = when
                path.write_text(json.dumps(record))
            name, _, record = planner.resolve_facts(None, ["ds41-rtx"])
            self.assertEqual((name, record["observed_at"]), ("new", 2000.0))
            self.assertEqual(planner.resolve_facts("old", ["ds41-rtx"])[0], "old")
            with self.assertRaisesRegex(planner.Refused, "facts of profile qwen-replicas"):
                planner.resolve_facts("other", ["ds41-rtx"])
            with self.assertRaisesRegex(planner.Refused, "recipe probe ds41-pair --out NAME"):
                planner.resolve_facts(None, ["ds41-pair"])
            with self.assertRaisesRegex(planner.Refused, "a saved facts name"):
                planner.resolve_facts("../old", ["ds41-rtx"])
            # The portal names facts only inside the facts directory, even when a same-named file is elsewhere.
            outside = Path(env.root) / "outside.json"
            outside.write_text(old.read_text())
            self.assertEqual(planner.resolve_facts(str(outside), ["ds41-rtx"])[1], outside)       # CLI: a file
            with self.assertRaises(planner.Refused):
                planner.resolve_facts(str(outside), ["ds41-rtx"], names_only=True)

    def test_unreadable_or_odd_records_are_skipped(self):
        with isolated() as env:
            good = env.save_facts("good", "deepseek-v4.1-flash", "ds41-rtx")
            directory = good.parent
            (directory / "broken.json").write_text("{", encoding="utf-8")
            (directory / "wrong.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
            record = json.loads(good.read_text())
            record["observed_at"] = float("nan")
            (directory / "nan.json").write_text(json.dumps(record), encoding="utf-8")
            (directory / "big.json").write_bytes(b" " * (planner.MAX_FACTS_BYTES + 1))
            try:
                os.symlink(good, directory / "link.json")
            except (OSError, NotImplementedError):
                pass
            self.assertEqual([n for n, _, _ in planner.saved_facts()], ["good"])


class LayerFileTests(unittest.TestCase):
    def test_layers_are_content_addressed_and_pruned(self):
        with isolated():
            text = planner.layer_text("ds-user", "0" * 64, {FLOOR: 8})
            first = planner.publish_layer(text)
            self.assertEqual(planner.publish_layer(text), first)
            self.assertEqual(json.loads(first.read_text())["schema"], "strix-stream/recipe-overrides-v1")
            stale = planner.layer_directory() / ("f" * 64 + ".json")
            stale.write_text("{}")
            os.utime(stale, (time.time() - 2 * 86400,) * 2)
            planner.prune_layers()
            self.assertTrue(first.exists())
            self.assertFalse(stale.exists())


class WhatifRunTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.env = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        user_ds()
        self.env.save_facts("ds-now", "deepseek-v4.1-flash", "ds41-rtx")

    def whatif(self, *extra, stdin=None):
        with mock.patch.object(render, "router_policies", return_value=("nodes", "routes")):
            return run_cli("whatif", "ds-user", "--profile", "ds41-rtx", *extra, "--config", str(self.env.config),
                           stdin=stdin)

    def test_variants_side_by_side(self):
        code, out, err = self.whatif("--vary", f"{FLOOR}=8,200", "--prompt-tokens", "9000")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["schema"], "hyperloom/planner-whatif-v1")
        self.assertEqual(out["facts"]["name"], "ds-now")
        self.assertEqual(out["facts"]["label"], "saved facts, not live")
        base, low, high = out["variants"]
        self.assertIsNone(base["layer"])
        self.assertEqual((base["fields"][FLOOR]["value"], base["fields"][FLOOR]["layer"]), (8.0, "default"))
        self.assertNotIn("--recipe-overrides", base["cli"]["plan"])
        self.assertEqual(base["predictions"]["label"], "predicted (recipe)")
        self.assertEqual(low["predictions"]["label"], "base recipe prediction, not re-measured for this variant")
        # The override took (layer + origin named by the variant id, never the private path).
        self.assertEqual(low["fields"][FLOOR]["value"], 8.0)
        self.assertEqual(low["fields"][FLOOR]["layer"], "override")
        self.assertIn("v1.json", low["fields"][FLOOR]["origin"])
        self.assertNotIn(str(planner.layer_directory()), json.dumps(out))
        self.assertIn("--recipe-overrides v1.json", low["cli"]["plan"])
        self.assertIn("--facts ds-now.json", low["cli"]["explain"])
        # The floor variant that fits: a decision, charges as up would admit them, up passes (conditional).
        self.assertTrue(low["fits"])
        self.assertIsNotNone(low["decision"])
        self.assertTrue(low["up"]["ok"], low["up"])
        self.assertTrue(low["resources"]["charges"])
        self.assertIn("as up would admit it", low["resources"]["charges_label"])
        self.assertIn("prefill_long", [r["id"] for r in low["routes"]])
        self.assertIn("route", low["simulated_request"])
        # The high floor: refused with the recipe's own reason; no decision, so charges say why.
        self.assertIsNone(high["decision"])
        self.assertIn("floor 200", high["refused"])
        self.assertIsNone(high["resources"]["charges"]["value"])
        self.assertIn("no decision", high["resources"]["charges"]["why"])
        self.assertFalse(high["up"]["ok"])
        self.assertIn("no decision", next(r for r in high["routes"] if r["id"] == "prefill_long")["decision"]["why"])
        self.assertEqual(high["layer"]["save_cli"].split(" --set ")[1], f"{FLOOR}=200")

    def test_each_variant_is_the_recipe_command_line(self):
        """One code path: a variant's answer equals `hyperloom recipe explain` run by hand with the same layer."""
        code, out, _ = self.whatif("--vary", f"{FLOOR}=200")
        variant = out["variants"][1]
        layer = planner.layer_directory() / (variant["layer"]["sha256"] + ".json")
        self.assertTrue(layer.is_file())
        facts = service.facts_dir() / "ds-now.json"
        with mock.patch.object(render, "router_policies", return_value=("nodes", "routes")):
            code, direct, _ = run_cli("recipe", "explain", "ds-user", "ds41-rtx", "--all", "--facts", str(facts),
                                      "--recipe-overrides", str(layer), "--config", str(self.env.config))
        self.assertEqual(code, 0)
        self.assertEqual(variant["refused"], direct["hardware"]["refused"])
        self.assertEqual(variant["match"], direct["hardware"]["match"])

    def test_a_refused_override_is_that_variant_answer(self):
        code, out, err = self.whatif("--vary", "routes.prefill_long.execute=tp")
        self.assertEqual(code, 0, err)
        bad = out["variants"][1]
        self.assertFalse(bad["explain"]["ok"])
        self.assertIn("not a settable path", bad["explain"]["error"])
        self.assertIsNone(bad["fields"]["routes.prefill_long.execute"]["value"])
        self.assertTrue(out["variants"][0]["explain"]["ok"])

    def test_explicit_variants_on_stdin(self):
        payload = json.dumps({"variants": [{"name": "no RTX", "set": {"routes.prefill_long.enabled": False}}]})
        code, out, err = self.whatif("--variants", "-", stdin=payload)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["variants"][1]["name"], "no RTX")
        self.assertFalse(out["variants"][1]["fields"]["routes.prefill_long.enabled"]["value"])

    def test_batch_refusals_and_codes(self):
        code, _, err = self.whatif("--vary", f"{FLOOR}=" + ",".join(str(i) for i in range(12)))
        self.assertEqual(code, 1)
        self.assertIn("13 variants", err)
        code, _, err = run_cli("whatif", "no-such-recipe", "--config", str(self.env.config))
        self.assertEqual(code, 3)
        self.assertIn("unknown recipe", err)
        code, _, err = run_cli("whatif", "ds-user", "--profile", "ds41-pair", "--config", str(self.env.config))
        self.assertEqual(code, 1)
        self.assertIn("no saved hardware facts for profile ds41-pair", err)

    def test_catalog_first_and_catalog_only(self):
        cluster = cli._cluster(self.env.config)
        path, ident = planner.resolve_recipe("ds-user", cluster, catalog_only=True)
        self.assertEqual((path.name, ident), ("ds-user.toml", "ds-user"))
        with self.assertRaises(service.NotFound):
            planner.resolve_recipe(str(path), cluster, catalog_only=True)
        self.assertEqual(planner.resolve_recipe(str(path), cluster)[1], "ds-user")

    def test_budget_stops_starting_variants(self):
        with mock.patch.object(planner.Budget, "left", side_effect=[1.0, 1.0, 0.0, 0.0, 0.0, 0.0]):
            code, out, err = self.whatif("--vary", f"{FLOOR}=8,9")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["variants"][0]["evaluated"])
        self.assertFalse(out["variants"][2]["evaluated"])
        self.assertIn("time budget", out["variants"][2]["why"])


if __name__ == "__main__":
    unittest.main()
