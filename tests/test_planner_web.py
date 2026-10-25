"""Portal side of the recipe planner (web/features/planner.py): argument checks, the command line it builds, the
answer contract, the page document, and one end-to-end request through the real server and recipe runner (a CLI
child process on the stub reader; no machine is contacted)."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from hyperloom.web import features, recipes as recipe_web, server
from hyperloom.web.api import API, APIError
from hyperloom.web.features import planner as feature
from tests.recipe_fixtures import isolated

FLOOR = "limits.host_free_ram_floor_gib"


class FakeAPI:
    def __init__(self, config_path="cfg.json"):
        self.config_path = Path(config_path)
        self.recipes = mock.Mock()

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", **fields}


def outcome(code, stdout=b"", stderr=b"", why=None):
    return recipe_web.Result(code, stdout, stderr, why)


class ArgvTests(unittest.TestCase):
    def test_whatif_argv(self):
        argv, stdin = feature.whatif_argv(FakeAPI(), {"recipe": "ds-user", "profile": "ds41-rtx", "facts": "ds-now",
                                                      "vary": {FLOOR: [8, 16.5], "routes.r.enabled": [True, False],
                                                               "routes.r.fallback": ["nodes"]},
                                                      "prompt_tokens": 9000})
        self.assertEqual(argv[:3], ["whatif", "ds-user", "--catalog-only"])
        self.assertIn(f"{FLOOR}=8,16.5", argv)
        self.assertIn("routes.r.enabled=true,false", argv)
        self.assertIn('routes.r.fallback="nodes"', argv)
        self.assertEqual(argv[-2:], ["--config", "cfg.json"])
        self.assertIsNone(stdin)
        argv, stdin = feature.whatif_argv(FakeAPI(), {"recipe": "ds-user",
                                                      "variants": [{"name": "x", "set": {FLOOR: 4}}]})
        self.assertIn("--variants", argv)
        self.assertEqual(json.loads(stdin), {"variants": [{"name": "x", "set": {FLOOR: 4}}]})

    def test_whatif_refusals(self):
        for arguments, message in (({}, "recipe is required"), ({"recipe": "../x"}, "must be an id"),
                                   ({"recipe": "a", "facts": "C:/x.json"}, "facts must be an id"),
                                   ({"recipe": "a", "bogus": 1}, "unknown arguments"),
                                   ({"recipe": "a", "vary": {"a b": [1]}}, "not a settable path"),
                                   ({"recipe": "a", "vary": {FLOOR: []}}, "1..12 values"),
                                   ({"recipe": "a", "vary": {FLOOR: [None]}}, "null"),
                                   ({"recipe": "a", "vary": {FLOOR: [{"x": 1}]}}, "values are"),
                                   ({"recipe": "a", "vary": {FLOOR: ["a\nb"]}}, "one line"),
                                   ({"recipe": "a", "vary": {"a.x": [1, 2, 3, 4], "b.y": [1, 2, 3]}}, "13 variants"),
                                   ({"recipe": "a", "prompt_tokens": 0}, "prompt_tokens"),
                                   ({"recipe": "a", "prompt_tokens": True}, "prompt_tokens"),
                                   ({"recipe": "a", "variants": [{"set": {}}]}, "1..256 settable paths"),
                                   ({"recipe": "a", "variants": [{"set": {FLOOR: None}}]}, "null"),
                                   ({"recipe": "a", "variants": [{"set": {FLOOR: {"x": 1}}}]}, "values are"),
                                   ({"recipe": "a", "variants": [{"set": {"a b": 1}}]}, "not a settable path"),
                                   ({"recipe": "a", "variants": [{"name": "", "set": {FLOOR: 1}}]}, "name"),
                                   ({"recipe": "a", "variants": ["x"]}, "expected"),
                                   ([], "JSON object")):
            with self.assertRaisesRegex(ValueError, message):
                feature.whatif_argv(FakeAPI(), arguments)

    def test_placement_argv(self):
        argv, stdin = feature.placement_argv(FakeAPI(), {"model": "qwen3.8-flash-next", "facts": "lab", "prompt": 4096,
                                                         "output": 64, "slots": 2, "objective": "decode", "top_k": 3})
        self.assertEqual(argv, ["placement", "qwen3.8-flash-next", "--catalog-only", "--facts", "lab", "--prompt", "4096", "--output", "64",
                                "--slots", "2", "--top-k", "3", "--objective", "decode", "--config", "cfg.json"])
        self.assertIsNone(stdin)
        for arguments, message in (({"model": "m", "slots": 65}, "slots"), ({"model": "m", "objective": "x"}, "objective"),
                                   ({"model": "m", "path": "x"}, "unknown"), ({"model": "/etc"}, "must be an id")):
            with self.assertRaisesRegex(ValueError, message):
                feature.placement_argv(FakeAPI(), arguments)


class AnswerTests(unittest.TestCase):
    argv = ["whatif", "ds-user", "--catalog-only", "--config", "cfg.json"]

    def answer(self, result):
        return feature.answer(FakeAPI(), "whatif", self.argv, result)

    def test_completed_commands(self):
        doc = self.answer(outcome(0, b'{"schema": "hyperloom/planner-whatif-v1"}'))
        self.assertEqual((doc["ok"], doc["exit_code"], doc["result"]["schema"]), (True, 0, "hyperloom/planner-whatif-v1"))
        self.assertEqual(doc["cli"], "hyperloom whatif ds-user --config cfg.json")      # the hidden flag is not shown
        refusal = self.answer(outcome(1, b"", b"hyperloom: 13 variants (the base included); at most 12\n"))
        self.assertEqual((refusal["ok"], refusal["result"], refusal["error"]),
                         (False, None, "13 variants (the base included); at most 12"))
        with_json = self.answer(outcome(1, b'{"scenarios": []}'))
        self.assertEqual(with_json["result"], {"scenarios": []})

    def test_errors_carry_the_reason_and_the_command_line(self):
        for result, status, text in ((outcome(None, why="deadline"), 504, "stopped: deadline"),
                                     (outcome(None, why="output overflow"), 502, "stopped: output overflow"),
                                     (outcome(0, b""), 502, "printed no JSON"),
                                     (outcome(0, b"not json"), 502, "printed no JSON"),
                                     (outcome(1, b"not json", b"x"), 502, "printed no JSON"),
                                     (outcome(1), 502, "without a message"),
                                     (outcome(7, b"", b"boom"), 502, "exit 7")):
            with self.assertRaises(APIError) as caught:
                self.answer(result)
            self.assertEqual(caught.exception.status, status)
            self.assertIn(text, str(caught.exception))
            self.assertIn("CLI: hyperloom whatif ds-user", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "bad"):
            self.answer(outcome(2, b"", b"hyperloom: bad"))
        with self.assertRaisesRegex(LookupError, "unknown recipe"):
            self.answer(outcome(3, b"", b"hyperloom: unknown recipe \"x\""))

    def test_a_busy_runner_is_503(self):
        api = FakeAPI()
        api.recipes.run.side_effect = recipe_web.Busy("recipe requests are busy: try again shortly")
        with self.assertRaises(APIError) as caught:
            feature.run(api, ["whatif"], None)
        self.assertEqual(caught.exception.status, 503)
        api.recipes.run.side_effect = None
        api.recipes.run.return_value = outcome(0, b"{}")
        feature.run(api, ["whatif", "x"], b"in")
        api.recipes.run.assert_called_with(["whatif", "x"], stdin=b"in", deadline_s=feature.DEADLINE_S)


class PortalTests(unittest.TestCase):
    """The real server, API and recipe runner (the planner CLI runs as the runner's child)."""

    def setUp(self):
        self.context = isolated()
        self.env = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.env.save_facts("ds-now", "deepseek-v4.1-flash", "ds41-rtx")
        registry = features.load(("planner",))
        self.assertEqual(registry.failed, {})
        features.reset(registry)
        self.addCleanup(features.reset)
        self.api = API(self.env.config, audit_path=self.env.root / "audit.jsonl")
        self.server = server.create_server(0, api=self.api)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def request(self, method, path, body=None):
        host = self.server.server_address[0]
        connection = http.client.HTTPConnection(host, self.port, timeout=170)
        headers = {"Host": f"{host}:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Origin": self.server.origin, "X-Hyperloom-CSRF": self.server.csrf_token,
                            "Content-Type": "application/json", "Content-Length": str(len(data))})
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_page_document_and_capabilities(self):
        layers = self.env.root / "appdata" / "hyperloom" / "run" / "planner"
        status, doc = self.request("GET", "/hyperloom/api/planner")
        self.assertEqual(status, 200)
        self.assertEqual(doc["schema"], "hyperloom/ui-planner-v1")
        self.assertIn({"id": "ds41-rtx", "model": "deepseek-v4.1-flash"}, doc["profiles"])
        self.assertEqual([f["name"] for f in doc["facts"]], ["ds-now"])
        self.assertTrue(doc["placement"]["models"]["deepseek-v4.1-flash"]["available"])
        self.assertFalse(doc["placement"]["models"]["minimax-m3.1-flash"]["available"])
        self.assertEqual(doc["limits"]["variants"], 12)
        self.assertFalse(layers.exists())                                 # a GET writes nothing
        status, _ = self.request("GET", "/hyperloom/api/planner?x=1")
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/hyperloom/api/planner/whatif")
        self.assertEqual(status, 405)
        capabilities = features.registry().capabilities
        self.assertTrue(capabilities["planner-whatif"]["enabled"])
        self.assertTrue(capabilities["placement-preview"]["enabled"])

    def test_whatif_end_to_end(self):
        status, doc = self.request("POST", "/hyperloom/api/planner/whatif",
                                   {"recipe": "deepseek-v4.1-flash.lab", "profile": "ds41-rtx",
                                    "vary": {FLOOR: [8]}})
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc["schema"], "hyperloom/ui-planner-whatif-v1")
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["cli"].startswith("hyperloom whatif deepseek-v4.1-flash.lab --profile ds41-rtx --vary"))
        result = doc["result"]
        self.assertEqual([v["id"] for v in result["variants"]], ["base", "v1"])
        self.assertEqual(result["variants"][1]["fields"][FLOOR]["value"], 8.0)
        status, doc = self.request("POST", "/hyperloom/api/planner/whatif", {"recipe": "no-such", "profile": "ds41-rtx"})
        self.assertEqual(status, 404)
        status, doc = self.request("POST", "/hyperloom/api/planner/whatif",
                                   {"recipe": "deepseek-v4.1-flash.lab", "profile": "ds41-pair"})
        self.assertEqual(status, 200)
        self.assertFalse(doc["ok"])
        self.assertIn("no saved hardware facts for profile ds41-pair", doc["error"])


if __name__ == "__main__":
    unittest.main()
