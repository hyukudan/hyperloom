"""`hyperloom recipe ready` and `hyperloom recipe matrix` (hyperloom/recipes/ready.py): the offline checklist before
`up --recipe`, with the same refusal texts as plan/up, and the model x hardware class table. No machine is contacted:
hardware facts are saved records."""
import json
import unittest
from unittest import mock

from hyperloom import orchestrate, render
from hyperloom.recipes import ready, reader, service, session
from hyperloom.web import recipes as recipe_web
from tests.recipe_fixtures import isolated
from tests.test_recipes_service import DS, MM, QWEN, run_cli, user_ds
from tests.test_web_recipes import pinned_memory

ROUTES = ("nodes", "routes")


def states(result):
    return {s["step"]: s["state"] for s in result["steps"]}


class ReadyTests(unittest.TestCase):
    def test_ready_when_every_step_passes(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            with mock.patch.object(render, "router_policies", return_value=ROUTES), pinned_memory():
                result = ready.check(cluster, "ds-user", "ds41-rtx")
            self.assertTrue(result["ready"], result["steps"])
            self.assertIn(states(result)["service"], ("ok", "warn"))         # warn: the fixture caps the host budget
            self.assertEqual(states(result)["hardware"], "ok")
            self.assertEqual(states(result)["core"], "warn")                # the stub reader: not production
            self.assertEqual(result["commands"]["up"], "hyperloom up ds41-rtx --recipe ds-user")
            self.assertEqual(result["next"], {"step": "plan", "fix": "hyperloom recipe plan ds-user ds41-rtx"})
            self.assertIn("prelaunch", " ".join(result["checked_at_start"]))

    def test_a_service_the_planner_declines_falls_back_to_the_nodes(self):
        from hyperloom import rtx
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            low = {"free_physical_gib": 20.0, "free_commit_gib": 127.75}
            with mock.patch.object(render, "router_policies", return_value=ROUTES),                     mock.patch.object(rtx, "memory_status", lambda: dict(low)):
                result = ready.check(cluster, "ds-user", "ds41-rtx")
            service_step = next(s for s in result["steps"] if s["step"] == "service")
            self.assertEqual(service_step["state"], "warn")
            self.assertIn("keeps every prefill on the nodes", service_step["detail"])
            self.assertTrue(result["ready"])

    def test_the_router_pin_blocks_with_ups_own_text(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            result = ready.check(cluster, "ds-user", "ds41-rtx")
            self.assertFalse(result["ready"])
            self.assertEqual(result["next"]["step"], "router")
            router = next(s for s in result["steps"] if s["step"] == "router")
            with self.assertRaises(reader.RecipeError) as refused:
                from hyperloom.recipes import launch
                launch.check_router()
            self.assertEqual(router["detail"], str(refused.exception))

    def test_pending_recipes_are_blocked_with_their_blocker(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            result = ready.check(cluster, "glm-5.3-flash.lab")
            self.assertEqual(states(result)["recipe"], "blocked")
            self.assertIn("pending", result["steps"][0]["detail"])
            self.assertEqual(states(result)["hardware"], "skipped")
            self.assertIsNone(result["commands"]["up"])

    def test_a_missing_probe_names_the_command_that_saves_one(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            user_ds()
            result = ready.check(cluster, "ds-user", "ds41-rtx")
            hardware = next(s for s in result["steps"] if s["step"] == "hardware")
            self.assertEqual(hardware["state"], "todo")
            self.assertTrue(hardware["fix"].startswith("hyperloom recipe probe ds41-rtx --out "))
            self.assertIn(str(service.facts_dir()), hardware["fix"])

    def test_a_refused_match_has_the_same_text_as_plan(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            path = env.save_facts("pair", "deepseek-v4.1-flash", "ds41-pair")
            with mock.patch.object(render, "router_policies", return_value=ROUTES):
                result = ready.check(cluster, DS, "ds41-pair")
                plan = service.plan(cluster, DS, "ds41-pair", source=session.saved_source(path))
            hardware = next(s for s in result["steps"] if s["step"] == "hardware")
            self.assertEqual(hardware["state"], "blocked")
            self.assertFalse(plan["up"]["ok"])
            self.assertTrue(hardware["detail"].startswith(plan["up"]["refused"]), (hardware, plan["up"]))

    def test_a_profile_that_cannot_run_a_recipe_is_blocked(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            env.save_facts("mm", "minimax-m3.1-flash", "mm")
            result = ready.check(cluster, MM, "mm")
            self.assertEqual(states(result)["launch"], "blocked")
            self.assertIn("bind loopback", next(s for s in result["steps"] if s["step"] == "launch")["detail"])
            self.assertEqual(states(result)["hardware"], "skipped")

    def test_several_serving_profiles_are_offered(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            serving = service.model_profiles(cluster, "qwen3.8-flash-next")
            result = ready.check(cluster, QWEN)
            profile = next(s for s in result["steps"] if s["step"] == "profile")
            if len(serving) > 1:
                self.assertEqual(profile["state"], "todo")
                for name in serving:
                    self.assertIn(f"hyperloom recipe ready {QWEN} {name}", profile["fix"])
            wrong = ready.check(cluster, QWEN, "ds41-rtx")
            self.assertIn('does not serve "qwen3.8-flash-next"',
                          next(s for s in wrong["steps"] if s["step"] == "profile")["detail"])

    def test_without_a_configuration(self):
        with isolated() as env:
            env.config.unlink()
            code, result, err = run_cli("recipe", "ready", QWEN)
            self.assertEqual(code, 1, err)
            self.assertEqual(states(result)["config"], "todo")
            self.assertIn("cluster.example.json", result["steps"][-2]["fix"] or result["steps"][-1]["fix"])

    def test_cli_text_and_exit_codes(self):
        with isolated() as env:
            env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx")
            user_ds()
            code, result, _ = run_cli("recipe", "ready", "ds-user", "ds41-rtx")
            self.assertEqual((code, result["ready"]), (1, False))
            from contextlib import redirect_stdout
            import io
            from hyperloom import cli
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["recipe", "ready", "ds-user", "ds41-rtx", "--format", "text"])
            self.assertEqual(code, 1)
            self.assertIn("NOT READY", out.getvalue())
            self.assertIn("[blocked] router applies recipe routes", out.getvalue())
            self.assertEqual(run_cli("recipe", "ready", "no.such")[0], 3)


class MatrixTests(unittest.TestCase):
    def test_classes_of_the_builtin_examples(self):
        with isolated() as env:
            cluster = orchestrate.load_cluster(env.config)
            result = ready.matrix(cluster)
            where = {r["id"]: c["class"] for c in result["cells"] for r in c["recipes"]}
            self.assertEqual(where[QWEN], "rtx-prefill")
            self.assertEqual(where[DS], "strix-pair")
            self.assertEqual(where["deepseek-v4.1-flash.lab-hetero"], "hetero")
            self.assertEqual(where["deepseek-v4.1-flash.lab-rtx-replica"], "rtx")
            self.assertEqual(where["glm-5.3-flash.lab"], "strix-pair")
            self.assertEqual({c["class"] for c in result["cells"]}, set(ready.HARDWARE))
            rows = {r["id"]: r for c in result["cells"] for r in c["recipes"]}
            self.assertIsNone(rows["glm-5.3-flash.lab"]["command"])                  # pending: never launched
            self.assertTrue(rows[QWEN]["command"].startswith("hyperloom up "))
            self.assertTrue(rows[QWEN]["command"].endswith(f"--recipe {QWEN}"))

    def test_a_figure_is_measured_only_with_resolved_sources(self):
        evidence = {"a": {"ref": "receipt.json"}}
        self.assertTrue(ready._figure("decode_tok_s", {"basis": "measured", "evidence": "a"}, evidence)["measured"])
        self.assertFalse(ready._figure("decode_tok_s", {"basis": "measured", "evidence": "b"}, evidence)["measured"])
        self.assertFalse(ready._figure("decode_tok_s", {"basis": "measured"}, evidence)["measured"])
        self.assertFalse(ready._figure("decode_tok_s", {"basis": "projected", "evidence": "a"}, evidence)["measured"])
        self.assertFalse(ready._figure("decode_tok_s", {"basis": "measured", "evidence": "a", "inherited": True},
                                       evidence)["measured"])

    def test_hardware_class_rules(self):
        def recipe(devices, routes):
            return {"match": {"devices": [{"id": i, "kind": k, "count": n} for i, k, n in devices]},
                    "routes": [{"phase": p, "devices": d, "execute": e, "enabled": on} for p, d, e, on in routes]}
        self.assertEqual(ready.hardware_class(recipe([("n", "hip", 1)], [("decode", ["n"], "single", True)])),
                         "strix-single")
        rtx_off = recipe([("p", "hip", 2), ("r", "cuda", 1)],
                         [("prefill", ["r"], "single", False), ("decode", ["p"], "tp", True)])
        self.assertEqual(ready.hardware_class(rtx_off), "strix-pair")           # a disabled route does not count
        rtx_off["routes"][0]["enabled"] = True
        self.assertEqual(ready.hardware_class(rtx_off), "rtx-prefill")

    def test_cli_and_portal(self):
        with isolated() as env:
            code, result, err = run_cli("recipe", "matrix")
            self.assertEqual(code, 0, err)
            self.assertIn("qwen3.8-flash-next", result["models"])
            operation, argv = recipe_web.read_request("recipes/matrix", {}, config=env.config)
            self.assertEqual((operation, argv[:2]), ("matrix", ["recipe", "matrix"]))
            operation, argv = recipe_web.read_request("recipes/ready", {"recipe": QWEN, "profile": "qwen-replicas"},
                                                      config=env.config)
            self.assertEqual(argv[:4], ["recipe", "ready", QWEN, "qwen-replicas"])
            with self.assertRaises(ValueError):
                recipe_web.read_request("recipes/ready", {"recipe": "../x.toml"}, config=env.config)
            with self.assertRaises(ValueError):
                recipe_web.read_request("recipes/matrix", {"format": "text"}, config=env.config)
            self.assertIn("ready", service.RESERVED)
            self.assertIn("matrix", service.RESERVED)
            json.dumps(result)


if __name__ == "__main__":
    unittest.main()
