"""Authoritative CPU placement bridge; optional parity with a packaged Tierstream build and contract goldens."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom.recipes import core, placement, reader, session


class DiscoveryTests(unittest.TestCase):
    def test_tierstream_precedence_and_legacy_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured, ts, ss = [Path(tmp) / name for name in ("configured.exe", "ts-recipe.exe", "ss-recipe.exe")]
            for path in (configured, ts, ss):
                path.touch()
                path.chmod(0o755)
            cluster = SimpleNamespace(raw={"pc": {"recipe_core": str(configured)}})
            with mock.patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": str(ts), "HYPERLOOM_SS_RECIPE": str(ss),
                                             "PATH": tmp}), mock.patch.object(core, "_names", return_value=[ts.name, ss.name]):
                self.assertEqual(core.locate(cluster), str(configured))
                self.assertEqual(core.locate(), str(ts))
                os.environ["HYPERLOOM_TS_RECIPE"] = ""
                self.assertEqual(core.locate(), str(ss))    # explicit legacy env keeps the base's priority over PATH
                os.environ["HYPERLOOM_SS_RECIPE"] = ""
                self.assertEqual(core.locate(), str(ts))
                ts.unlink()
                self.assertEqual(core.locate(), str(ss))
                cluster.raw["pc"]["recipe_core"] = str(configured) + ".missing"
                self.assertEqual(core.locate(cluster), str(ss))    # renamed sibling, as on the base
                os.environ["HYPERLOOM_TS_RECIPE"] = str(ts) + ".missing"
                self.assertIsNone(core.locate())
                ss.unlink()
                self.assertIsNone(core.locate(cluster))


class InvocationTests(unittest.TestCase):
    def test_place_solve_json_and_text_and_layer_inputs(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(kwargs["timeout"], core.TIMEOUT_S)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            for flag in ("--probe", "--identity"):
                self.assertEqual(json.loads(Path(argv[argv.index(flag) + 1]).read_text()), {"devices": []})
            Path(argv[argv.index("--json-out") + 1]).write_text('{"authoritative": true}', encoding="utf-8")
            kwargs["stdout"].write(b"core explanation\n")
            return SimpleNamespace(returncode=0)

        backend = core.CoreBackend(core.Core("ts-recipe", run=run))
        layers = session.Layers(profile=Path("tune.toml"), overrides=["one.toml", "two.toml"],
                                sets=[("routes.r.max_rows", 6)])
        for command in ("place", "solve"):
            kwargs = {"components": {"schema": "tierstream/components-v1"}} if command == "place" else {"inputs": {"choices": []}}
            result = getattr(placement, command)("recipe.toml", route="r", probe={"devices": []},
                       layers=layers, backend=backend, identity={"devices": []}, desc="cached.desc", context={"entries": []},
                       **kwargs)
            self.assertEqual(result.document, {"authoritative": True})
            self.assertEqual(result.text, "core explanation\n")
            argv = calls[-1]
            self.assertEqual(argv[:3], ["ts-recipe", command, "recipe.toml"])
            self.assertIn("--explain" if command == "place" else "--table", argv)
            self.assertEqual(argv[argv.index("--profile") + 1], "tune.toml")
            self.assertEqual([argv[i + 1] for i, arg in enumerate(argv) if arg == "--overrides"], ["one.toml", "two.toml"])
            self.assertIn("routes.r.max_rows=6", argv)
            self.assertIn("cached.desc", argv)
        self.assertFalse(Path(calls[0][calls[0].index("--json-out") + 1]).exists())

    def test_refusals_are_verbatim_and_never_fall_back(self):
        for rc in (1, 2, 3):
            error = "ts-recipe: place: PL_E_CAPABILITY drafter transport refused\n  details preserved\n"
            def refused(argv, **kwargs):
                kwargs["stderr"].write(error.encode())
                return SimpleNamespace(returncode=rc)
            run = mock.Mock(side_effect=refused)
            backend = core.CoreBackend(core.Core("ts-recipe", run=run))
            with self.subTest(rc=rc), mock.patch.object(reader, "resolve", side_effect=AssertionError("stub")):
                with self.assertRaises(reader.RecipeError) as caught:
                    placement.place("r.toml", route="r", probe={}, components={}, backend=backend)
                self.assertEqual(str(caught.exception), error.rstrip("\n"))
                self.assertEqual(run.call_count, 1)

    def test_missing_malformed_and_stdout_only_json_are_errors(self):
        for contents in (None, "text", "[]"):
            def run(argv, **kwargs):
                if contents is not None:
                    Path(argv[argv.index("--json-out") + 1]).write_text(contents, encoding="utf-8")
                kwargs["stdout"].write(b'{"not": "a fallback"}')
                return SimpleNamespace(returncode=0)
            with self.subTest(contents=contents), self.assertRaisesRegex(reader.RecipeError, "JSON"):
                placement.place("r.toml", route="r", probe={}, components={},
                                backend=core.CoreBackend(core.Core("ts-recipe", run=run)))

    def test_unavailable_timeout_and_stub_are_refused(self):
        for exc in (FileNotFoundError("missing"), subprocess.TimeoutExpired("ts-recipe", 60)):
            with self.subTest(exc=exc), self.assertRaisesRegex(reader.RecipeError, "unavailable"):
                placement.solve("r.toml", route="r", probe={}, inputs={},
                                backend=core.CoreBackend(core.Core("ts-recipe", run=mock.Mock(side_effect=exc))))
        with mock.patch.object(core, "locate", return_value=None):
            with self.assertRaisesRegex(reader.RecipeError, "requires.*ts-recipe"):
                placement.solve("r.toml", route="r", probe={}, inputs={})
        with self.assertRaisesRegex(reader.RecipeError, "requires.*ts-recipe"):
            placement.place("r.toml", route="r", probe={}, components={}, backend=core.StubBackend())

    def test_legacy_json_stdout_and_errors_stay_compatible(self):
        def legacy(argv, **kwargs):
            kwargs["stdout"].write(b'{"legacy": true}')
            return SimpleNamespace(returncode=0)
        run = mock.Mock(side_effect=legacy)
        self.assertEqual(core.Core("ss-recipe", run=run).call(["validate", "r.toml"]), {"legacy": True})
        self.assertNotIn("--json-out", run.call_args.args[0])
        def refused(argv, **kwargs):
            kwargs["stderr"].write(b"ss-recipe: refused\n")
            return SimpleNamespace(returncode=1)
        run.side_effect = refused
        with self.assertRaisesRegex(reader.RecipeError, "^refused$"):
            core.Core("ss-recipe", run=run).call(["validate", "r.toml"])


class TierstreamParityTests(unittest.TestCase):
    def setUp(self):
        executable = os.environ.get("HYPERLOOM_TS_RECIPE") or shutil.which("ts-recipe")
        golden = os.environ.get("HYPERLOOM_PLACEMENT_GOLDEN")
        if not executable or not golden:
            self.skipTest("set HYPERLOOM_TS_RECIPE and HYPERLOOM_PLACEMENT_GOLDEN for CPU golden parity")
        self.executable, self.golden = Path(executable).resolve(), Path(golden).resolve()
        self.backend = core.CoreBackend(core.Core(str(self.executable)))

    def test_g4_literal_bridge_json_explain_and_refusal(self):
        for case, expected_name in (("J", "J-literal-recipe"), ("J2", "J2-literal-single-gpu")):
            recipe = self.golden / "g4" / f"{case}-recipe.toml"
            probe = json.loads((self.golden / "g4" / f"{case}-probe.json").read_text())
            probe.update(model="DeepSeek-V4.1-Flash", engine="strix-cuda-ds41-replica")
            components = self.golden / "g4" / f"{case}-components.json"
            result = placement.place(recipe, route="decode_rtx", probe=probe, components=components, backend=self.backend)
            direct = self.backend.place(recipe, session.Layers(), route="decode_rtx", probe=probe,
                                        components=components, explain=False)
            self.assertEqual(result.document, direct.document)
            self.assertIn("rtx", result.text)
            want = json.loads((self.golden / "g4" / f"{expected_name}.expected.json").read_text())
            for set_id, device in want["devices"].items():
                got = next(d for d in result.document["devices"] if d["set"] == set_id)
                for key in ("budget_bytes", "margin_bytes", "context_bytes", "phase_peak_bytes", "signature"):
                    if key in device:
                        self.assertEqual(got[key], device[key], (case, set_id, key))
                arena = next((line for line in got["lines"] if line["component"] == "arena"), {})
                self.assertEqual(arena.get("slots", 0), device["active_slots"])
            self.assertEqual(result.document["host"]["pinned_total_bytes"], want["host"]["pinned_total_bytes"])
        bad = deepcopy(probe)
        bad["devices"][0]["name"] = "unknown GPU"
        with self.assertRaisesRegex(reader.RecipeError, "PL_E_UNKNOWN_DEVICE"):
            placement.place(recipe, route="decode_rtx", probe=bad, components=components, backend=self.backend)

    def test_sv1_solver_bridge_preserves_core_rows_and_table(self):
        recipe = self.golden / "g4/J2-recipe.toml"
        probe = json.loads((self.golden / "g4/J2-probe.json").read_text())
        probe.update(model="DeepSeek-V4.1-Flash", engine="strix-cuda-ds41-replica")
        # SV1's costs/curves are passed unchanged. Their synthetic calibration differs from J2: the core must
        # retain the placements and mark predictions unpredicted instead of estimating in Python.
        fixture = json.loads((self.golden / "sv1/SV1-solver.json").read_text())
        inputs = {key: fixture[key] for key in ("costs", "curves", "curves_applicability")}
        inputs["choices"] = [{key: choice[key] for key in ("id", "mode", "R", "ctx", "lang")}
                             for choice in fixture["choices"][:2]]
        for choice in inputs["choices"]:
            choice["components"] = str(self.golden / "g4/J2-components.json")
        result = placement.solve(recipe, route="decode_rtx", probe=probe, inputs=inputs, backend=self.backend)
        direct = self.backend.solve(recipe, session.Layers(), route="decode_rtx", probe=probe, inputs=inputs, table=False)
        self.assertEqual(result.document, direct.document)
        for row in result.document["rows"]:
            self.assertEqual(row["prediction"]["tag"], "unpredicted")
            self.assertIn(row["id"], result.text)

    def test_tierstream_g4_sv1_frozen_vectors(self):
        executable = self.executable.with_name("ts-test-place" + self.executable.suffix)
        if not executable.is_file():
            self.skipTest("build Tierstream's CPU ts-test-place target for all G4/SV1 vectors")
        # The upstream CPU harness constructs components/recipes from the abstract vectors and compares every
        # allocation and prediction with the frozen expected files. Run it on the contract's own goldens;
        # no reference arithmetic is imported into Hyperloom, and the external worktree remains read-only.
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "tests/data/placement"
            data.mkdir(parents=True)
            for directory in ("g4", "sv1", "calib"):
                for source in (self.golden / directory).iterdir():
                    if source.is_file():
                        shutil.copyfile(source, data / source.name)
            for group in ("g4", "sv1", "solver", "placement_profile_guard"):
                result = subprocess.run([str(executable), group], cwd=tmp, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS " + group, result.stdout)
                self.assertIn("0 failure(s)", result.stdout)
