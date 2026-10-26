"""Placement refusals are read-only, including the CLI/portal admission and snapshot boundaries."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom import admission, cli, orchestrate, procs, rtx
from hyperloom.recipes import apply, commands, core, launch, measurements, preflight, reader, service, session
from hyperloom.recipes.reader import RecipeError
from hyperloom.web.api import API, APIError
from tests import test_recipes
from tests.test_recipes_grants import NOW, window


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.uuid = "GPU-" + str(uuid.uuid4())
        self.recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
        route = self.recipe["routes"][0]
        route["assist"]["aux"]["device_gib"] = 0
        self.rid = route["id"]
        self.grant = {"uuid": self.uuid, "vram_cap_gib": 90, "approval": "test operator", "window": window(),
                      "gate": [sys.executable, "fake-gate"]}
        self.cluster = SimpleNamespace(pc={"cuda": {"grants": [self.grant], "host_grant": {
            "host_gib": 74, "approval": "test operator", "window": window()}}})
        self.bound = {"binding": {"anchor": [{"id": self.uuid, "host": "local"}], "aux": []}}
        self.report = {"devices": [{"id": self.uuid, "uuid": self.uuid, "kind": "cuda", "name": "test class",
                                    "arch": "sm_120", "driver": "600.1", "platform": "windows-wddm"}]}
        self.raw = {"hosts": {"local": {"free_ram_gib": 100, "free_commit_gib": 120}},
                    "sysmem_policy": "prefer_no_sysmem_fallback"}
        self.inputs = {"routes": {self.rid: {"components": {"fake": "cached reference"},
                                             "selected_content": {"target": "fake-model"}}}}
        self.cache = mock.Mock()
        self.cache.load.return_value = {"schema": "tierstream/components-v1"}
        self.backend = mock.Mock()
        self.backend.place.return_value = core.PlacementOutput({"authoritative": True,
            "devices": [{"uuid": self.uuid, "set": "anchor", "budget_bytes": 90 * rtx.GIB}],
            "host": {"pinned_budget_bytes": 68 * rtx.GIB, "pageable_budget_bytes": 2 * rtx.GIB}}, "")
        self.observer = mock.Mock()
        self.observer.sample.return_value = {self.uuid.lower(): 0}

    def check(self):
        return preflight.check(self.cluster, "fake-recipe", session.Layers(), self.backend, self.recipe,
                               self.bound, self.report, self.raw, "a" * 64, inputs=self.inputs, cache=self.cache,
                               now=NOW, busy=lambda _: None, observer_factory=lambda: self.observer)

    def forbidden(self, stack):
        paths = [(orchestrate, "_write"), (orchestrate, "_call"), (orchestrate, "_detached"),
                 (procs, "LocalChild"), (procs, "InstanceLock"), (rtx, "prepare_service"),
                 (rtx, "launch_service"), (rtx, "run_gate"), (measurements, "acquire")]
        return [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                for module, name in paths]

    def test_every_refusal_precedes_state_lease_helper_child_and_gate(self):
        def missing():
            self.cluster.pc["cuda"]["grants"] = []
        def expired():
            self.grant["window"]["expires_at"] = "2029-12-31T23:59:59Z"
        def overgrant():
            self.grant["vram_cap_gib"] = 89
        def unknown_identity():
            self.report["devices"][0]["driver"] = "unknown"
        def sysmem():
            self.raw["sysmem_policy"] = "fallback_allowed"
        def host():
            self.raw["hosts"]["local"]["free_ram_gib"] = 1
        def commit():
            self.raw["hosts"]["local"]["free_commit_gib"] = 1
        def missing_descriptor():
            self.inputs["routes"].clear()
        def stale_manifest():
            self.cache.load.side_effect = RecipeError("PL_E_STALE run hyperloom probe --manifest")
        def budget():
            self.backend.place.side_effect = RecipeError("PL_E_BUDGET authoritative core refusal")
        def capability():
            self.backend.place.side_effect = RecipeError("PL_E_CAPABILITY authoritative core refusal")
        def observer():
            self.observer.sample.return_value = {}
        changes = [(missing, "PL_E_GRANT"), (expired, "PL_E_GRANT"), (overgrant, "PL_E_GRANT"),
                   (unknown_identity, "PL_E_STALE"), (sysmem, "PL_E_SYSMEM"), (host, "PL_E_GRANT"),
                   (commit, "PL_E_FREE"), (missing_descriptor, "PL_E_MISSING_DESCRIPTOR"),
                   (stale_manifest, "PL_E_STALE"), (budget, "PL_E_BUDGET"), (capability, "PL_E_CAPABILITY"),
                   (observer, "PL_E_CAPABILITY")]
        originals = deepcopy((self.cluster, self.report, self.raw, self.inputs))
        for change, code in changes:
            self.cluster, self.report, self.raw, self.inputs = deepcopy(originals)
            self.grant = self.cluster.pc["cuda"]["grants"][0]
            self.cache.load.side_effect = self.backend.place.side_effect = None
            self.observer.sample.return_value = {self.uuid.lower(): 0}
            change()
            with self.subTest(refusal=change.__name__), ExitStack() as stack:
                forbidden = self.forbidden(stack)
                with self.assertRaisesRegex(RecipeError, code):
                    self.check()
                for call in forbidden:
                    call.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_core_receives_exact_cached_document_and_idle_observer_fail_closed(self):
        plans = self.check()
        self.assertEqual(plans[self.rid], self.backend.place.return_value.document)
        self.assertEqual(self.backend.place.call_args.kwargs["components"], self.cache.load.return_value)
        self.observer.close.assert_called_once()
        for busy in (lambda _: "busy", mock.Mock(side_effect=rtx.ServiceError("N/A"))):
            with ExitStack() as stack:
                self.forbidden(stack)
                with self.assertRaisesRegex(RecipeError, "PL_E_BUSY"):
                    preflight.check(self.cluster, "fake", session.Layers(), self.backend, self.recipe,
                                    self.bound, self.report, self.raw, "a" * 64, inputs=self.inputs,
                                    cache=self.cache, now=NOW, busy=busy,
                                    observer_factory=lambda: self.observer)

    def test_layer_detection_and_legacy_do_not_enter_placement_admission(self):
        path = self.root / "recipe.json"
        from tests.test_recipes import DS
        path.write_bytes(DS.encode())
        options = launch.Options(str(path))
        self.assertFalse(preflight.includes(options))
        with mock.patch.object(launch, "prepare", side_effect=AssertionError("legacy admission")), \
                mock.patch.object(orchestrate, "check_launch") as check:
            preflight.before_options(None, "legacy", options)
        check.assert_called_once()
        options.layers.sets = [("routes.decode.components.drafter.device", "anchor")]
        self.assertTrue(preflight.includes(options))

    def test_prepare_refusal_is_before_router_dependency_and_all_mutation(self):
        path = self.root / "recipe.json"
        path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        evaluation = SimpleNamespace(problems=[], refusal="PL_E_STALE fake content identity")
        with ExitStack() as stack:
            self.forbidden(stack)
            stack.enter_context(mock.patch.object(launch, "_backend", return_value=(self.backend, True)))
            stack.enter_context(mock.patch.object(session, "evaluate", return_value=evaluation))
            policies = stack.enter_context(mock.patch.object(launch.render, "router_policies",
                                                             side_effect=AssertionError("late router check")))
            with self.assertRaisesRegex(RecipeError, "PL_E_STALE"):
                launch.prepare(None, "test", launch.Options(str(path)), router="local", vram_cap_gib=None,
                               state_dir=self.root, source=lambda _: None)
            policies.assert_not_called()
        self.assertEqual([p.name for p in self.root.iterdir()], ["recipe.json"])

    def test_real_up_and_session_complete_preflight_then_refuse_unqualified_adapter(self):
        path = self.root / "recipe.json"
        path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        self.cluster.machines = {"local": SimpleNamespace(devices=[{
            "id": "test-gpu", "type": "nvidia", "uuid": self.uuid}])}
        ctx = apply.Context("test", "synthetic", "single", [], "local")
        raw = {**self.raw, "devices": {self.uuid: {"available": True}}, "lan": {}, "usb4": {}}
        inventory = {"cuda": [{**self.report["devices"][0], "memory_gib": 96}]}
        gathered = {"model": "synthetic", "devices": {self.uuid: inventory["cuda"][0]}, "links": []}
        backend = core.CoreBackend(core.Core("fake-core"))
        backend.resolve = mock.Mock(return_value=core.Resolved(self.recipe, "a" * 64, {}))
        backend.match = mock.Mock(return_value=([], self.bound["binding"], "a" * 64))
        backend.place = self.backend.place
        with ExitStack() as stack:
            forbidden = self.forbidden(stack)
            stack.enter_context(mock.patch.object(orchestrate, "check_launch", return_value=(
                {}, "synthetic", [], None, "synthetic", False, None, [])))
            stack.enter_context(mock.patch.object(orchestrate, "_state_path", return_value=self.root / "up-test.json"))
            stack.enter_context(mock.patch.object(launch, "_backend", return_value=(backend, True)))
            stack.enter_context(mock.patch.object(session.facts, "context", return_value=ctx))
            stack.enter_context(mock.patch.object(commands, "live_source", return_value=lambda _, **kw: (
                inventory, gathered, raw, [])))
            stack.enter_context(mock.patch.object(preflight.content_cache, "_read", return_value=self.inputs))
            stack.enter_context(mock.patch.object(commands, "components_store", return_value=self.cache))
            stack.enter_context(mock.patch.object(preflight.time, "time", return_value=NOW))
            stack.enter_context(mock.patch.object(preflight.gpu_counters, "Observer", return_value=self.observer))
            stack.enter_context(mock.patch.object(rtx, "pro_busy", return_value=None))
            with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY.*schema only"):
                orchestrate.up(self.cluster, "test", recipe=launch.Options(str(path)))
            self.backend.place.assert_called_once()
            self.observer.sample.assert_called_once()
            for call in forbidden:
                call.assert_not_called()
        self.assertEqual([p.name for p in self.root.iterdir()], ["recipe.json"])

    def test_cli_hold_and_direct_up_refuse_before_database_pin_snapshot_or_progress(self):
        args = cli.build_parser().parse_args(["up", "test", "--recipe", "fake.json"])
        for operation in (lambda: cli._admission_hold(args),
                          lambda: cli.up_command(args, load_cluster=lambda _: self.cluster)):
            with ExitStack() as stack:
                self.forbidden(stack)
                stack.enter_context(mock.patch.object(preflight, "cli", side_effect=RecipeError("PL_E_BUDGET")))
                database = stack.enter_context(mock.patch.object(admission, "for_config"))
                pin = stack.enter_context(mock.patch.object(service, "acquire_pin"))
                snapshot = stack.enter_context(mock.patch.object(service, "launch_inputs"))
                progress = stack.enter_context(mock.patch.object(service, "launch_progress"))
                with self.assertRaisesRegex(RecipeError, "PL_E_BUDGET"):
                    operation()
                for call in (database, pin, snapshot, progress):
                    call.assert_not_called()

    def test_snapshot_pin_and_deploy_progress_follow_preflight(self):
        path = self.root / "recipe.json"
        path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
        with mock.patch.object(service, "locate", return_value=path), \
                mock.patch.object(service, "deployment_profile", return_value="test"), \
                mock.patch.object(preflight, "before_options", side_effect=RecipeError("PL_E_GRANT")), \
                mock.patch.object(service, "launch_inputs") as snapshot:
            with self.assertRaisesRegex(RecipeError, "PL_E_GRANT"):
                service.up_options(self.cluster, str(path), "test")
            snapshot.assert_not_called()
        with mock.patch.object(preflight, "snapshot", side_effect=RecipeError("PL_E_STALE")), \
                mock.patch.object(service, "launch_progress") as progress, \
                mock.patch.object(service, "acquire_pin") as pin:
            with self.assertRaisesRegex(RecipeError, "PL_E_STALE"):
                service.deploy("a" * 64)
            progress.assert_not_called()
            pin.assert_not_called()

    def test_portal_refusal_is_before_hold_job_and_child(self):
        api = API(self.root / "config.json")
        for action in ("up", "recipes/deploy"):
            args = {"profile": "test", "recipe": "test"} if action == "up" else {"inputs": "a" * 64}
            argv = ["up", "test", "--recipe", "test"] if action == "up" else ["recipe", "deploy", "a" * 64]
            with mock.patch.object(api, "_arguments", return_value=args), \
                    mock.patch.object(api, "_recipe_up_argv", return_value=argv), \
                    mock.patch.object(api, "_deploy_argv", return_value=argv), \
                    mock.patch.object(preflight, "cli", side_effect=RecipeError("PL_E_CAPABILITY")), \
                    mock.patch.object(api, "_hold") as hold, mock.patch.object(api, "_start_worker") as child:
                with self.assertRaisesRegex(APIError, "PL_E_CAPABILITY"):
                    api.mutate(action, args)
                hold.assert_not_called()
                child.assert_not_called()
                self.assertEqual(api._jobs, {})
                self.assertIsNone(api._active)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
