"""Placement planner UI/CLI contracts and configuration-owned device exclusions."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom import cli
from hyperloom.cliext import planner
from hyperloom.web.api import _accelerator
from hyperloom.web.features import planner as web_planner


class PlannerPlacementUITests(unittest.TestCase):
    def test_gpu_name_does_not_exclude_and_configuration_uuid_does(self):
        ident = "GPU-" + str(uuid.uuid4())
        raw = {"id": ident, "uuid": ident, "name": "NVIDIA GeForce RTX 4090"}
        self.assertTrue(_accelerator(raw, {}, {}, [])["excluded"])  # unmentioned 4090: legacy rule
        declared = [{"id": "configured-assist", "uuid": ident, "excluded": True}]
        self.assertTrue(_accelerator(raw, {}, {}, declared)["excluded"])
        raw["name"] = "any other GPU model"
        self.assertTrue(_accelerator(raw, {}, {}, declared)["excluded"])
        declared[0]["excluded"] = False
        self.assertFalse(_accelerator(raw, {}, {}, declared)["excluded"])

    def test_explicit_config_name_policy_and_legacy_id_match(self):
        raw = {"id": "declared-id", "name": "portable class"}
        for declared in ([{"id": "declared-id", "excluded": True}],
                         [{"id": "other", "name": "portable class", "excluded": True}]):
            self.assertTrue(_accelerator(raw, {}, {}, declared)["excluded"])

    def test_planner_summary_keeps_solver_rows(self):
        solver = {"rows": [{"id": "plain", "tag": "unpredicted", "estimates": ["allhit_us"]}]}
        summary = planner.summarize({"set": {}}, {}, {"solver": solver})
        self.assertEqual(summary["solver"], solver)

    def test_portal_and_cli_share_solve_and_pick_commands(self):
        api = SimpleNamespace(config_path=Path("cluster.json"))
        for arguments, picking in (({"recipe": "recipe", "route": "decode", "bundle": "cached"}, False),
                                   ({"selection": "a" * 64, "row": "plain", "name": "picked"}, True)):
            argv = web_planner.solve_argv(api, arguments, picking=picking)
            args = cli.build_parser().parse_args(argv)
            self.assertEqual(args.command, "solve")
            self.assertTrue(args.catalog_only)
            if picking:
                self.assertEqual((args.pick, args.save_layer), ("plain", "picked"))
            else:
                self.assertEqual((args.recipe, args.route, args.bundle), ("recipe", "decode", "cached"))
        with mock.patch.object(planner, "_loader", return_value=None), \
                mock.patch("hyperloom.recipes.service.solve", return_value={"rows": []}) as solve, \
                mock.patch.object(planner, "_emit"):
            self.assertEqual(planner.run_solve(cli.build_parser().parse_args(
                web_planner.solve_argv(api, {"recipe": "recipe", "route": "decode", "bundle": "cached"}))), 0)
        solve.assert_called_once_with(None, "recipe", "decode", "cached")
