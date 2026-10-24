"""Portal recipe endpoints (hyperloom/web/recipes.py): every answer is the `hyperloom recipe ...` command line's own
(same exit code, stdout JSON and refusal text), the transport rules of the other mutations, bounded children whose
whole process tree is removed, the field editor's one-layer edits, `up` with a recipe and the server-side deploy job.
No machine is contacted: hardware facts are saved records."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import cli, config, orchestrate, render
from hyperloom.recipes import commands, launch, service, session
from hyperloom.web import recipes as recipe_web
from hyperloom.web.api import API, APIError
from hyperloom.web.server import create_server
from tests.recipe_fixtures import builtin_text, copy_of, isolated
from tests.test_recipes_service import DS_FIXTURE, QWEN, run_cli, user_ds

MM_TEXT = (Path(__file__).parent / "fixtures" / "recipes" / "minimax-lab.toml").read_text(encoding="utf-8").replace(
    'name = "minimax-m3.1-flash-lab"', 'name = "mm-base"', 1)


class Portal:
    """The real server and API on an isolated configuration (children inherit it)."""

    def __init__(self, env, orchestrator=None):
        self.env = env
        self.api = API(env.config, orchestrator=orchestrator, audit_path=env.root / "audit.jsonl")
        self.server = create_server(0, api=self.api)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def request(self, method, endpoint, body=None, *, csrf=True, raw=None):
        connection = HTTPConnection(self.host, timeout=200)
        headers = {"Host": self.host}
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if method == "POST":
            headers.update({"Origin": "http://" + self.host, "Content-Type": "application/json",
                            "Content-Length": str(len(payload or b""))})
            if csrf:
                headers["X-Hyperloom-CSRF"] = self.server.csrf_token
        connection.request(method, "/hyperloom/api/" + endpoint, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if data else None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def cli_of(document):
    """The CLI argv a portal answer names (its `cli` line)."""
    return shlex.split(document["cli"])[1:]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.env_context = isolated()
        self.env = self.env_context.__enter__()
        self.addCleanup(self.env_context.__exit__, None, None, None)
        self.portal = Portal(self.env)
        self.addCleanup(self.portal.close)
        self.env.save_facts("qwen", "qwen3.8-flash-next", "qwen-replicas")
        self.env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx")
        user_ds()

    def assertSameAsCli(self, document, stdin=None):
        code, result, err = run_cli(*cli_of(document), stdin=stdin)
        self.assertEqual(document["exit_code"], code, (document["error"], err))
        if result is not None:
            self.assertEqual(json.loads(json.dumps(document["result"])), result)
        if code:
            self.assertEqual(document["error"] or "", err.strip().removeprefix("hyperloom: "))

    def test_get_documents(self):
        status, listing = self.portal.request("GET", "recipes")
        self.assertEqual((status, listing["schema"], listing["exit_code"]), (200, "hyperloom/recipes-list-v1", 0))
        self.assertIn(QWEN, {r["id"] for r in listing["result"]["recipes"]})
        self.assertTrue(listing["cli"].startswith("hyperloom recipe list --config "))
        self.assertSameAsCli(listing)
        status, one = self.portal.request("GET", "recipes/" + QWEN)
        self.assertEqual(status, 200)
        self.assertEqual(one["result"]["id"], QWEN)
        self.assertTrue(any(f["path"] == "routes.prefill_rtx.when.prompt_tokens_min" for f in one["result"]["fields"]))
        status, missing = self.portal.request("GET", "recipes/no.such")
        self.assertEqual((status, missing["exit_code"]), (404, 3))
        self.assertIn("unknown recipe", missing["error"])
        for endpoint in ("recipes/active", "recipes/routes"):
            status, document = self.portal.request("GET", endpoint)
            self.assertEqual((status, document["result"]), (200, {"deployments": []}))
        for endpoint in ("recipes/validate", "recipes/save", "recipes/deploy", "recipes/probe"):
            self.assertEqual(self.portal.request("GET", endpoint)[0], 405)
        self.assertEqual(self.portal.request("GET", "recipes?unknown=1")[0], 400)

    def test_answers_equal_the_cli(self):
        facts = {"facts": "ds"}
        cases = [("validate", {"recipe": QWEN}), ("validate", {"recipe": "ds-user", "profile": "ds41-rtx", **facts}),
                 ("validate", {"recipe": QWEN, "set": ["routes.prefill_rtx.execute=tp"]}),
                 ("explain", {"recipe": "ds-user", "profile": "ds41-rtx", "prompt_tokens": 9000,
                              "link_down": ["pc-nodes"], **facts}),
                 ("diff", {"a": QWEN, "b": "deepseek-v4.1-flash.lab"}),
                 ("select", {"model": "deepseek-v4.1-flash", "profile": "ds41-rtx", **facts}),
                 ("pick", {"profile": "ds41-rtx", **facts}),
                 ("plan", {"recipe": "ds-user", "profile": "ds41-rtx", **facts}),
                 ("plan", {"recipe": "deepseek-v4.1-flash.lab", "profile": "ds41-pair", "facts": "ds"})]
        for operation, body in cases:
            with self.subTest(operation=operation, body=body):
                status, document = self.portal.request("POST", "recipes/" + operation, body)
                self.assertIn(status, (200,), document)
                self.assertEqual(document["schema"], f"hyperloom/recipes-{operation}-v1")
                self.assertSameAsCli(document)
        status, refused = self.portal.request("POST", "recipes/validate", {"recipe": QWEN, "set": ["a.b=1"]})
        self.assertEqual((status, refused["exit_code"], refused["ok"]), (200, 1, False))
        self.assertIn("not a settable path", refused["error"])

    def test_drafts_saves_and_audit(self):
        draft = copy_of(QWEN, "draft-recipe")
        status, checked = self.portal.request("POST", "recipes/check", {"text": draft})
        self.assertEqual((status, checked["result"]["valid"]), (200, True))
        self.assertSameAsCli(checked, stdin=draft)
        status, broken = self.portal.request("POST", "recipes/check", {"text": draft.replace("max_batch = 8", "")})
        self.assertEqual(broken["exit_code"], 1)
        big = draft + "#" * (200 * 1024) + "\n"
        status, saved = self.portal.request("POST", "recipes/save", {"id": "qwen.big", "text": big})
        self.assertEqual((status, saved["exit_code"]), (200, 0), saved)
        self.assertEqual(Path(saved["result"]["path"]).parent, service.user_dir().resolve())
        status, again = self.portal.request("POST", "recipes/save", {"id": "qwen.big", "text": big})
        self.assertEqual((status, again["exit_code"]), (409, 4))
        status, builtin = self.portal.request("POST", "recipes/save", {"id": QWEN, "text": draft})
        self.assertEqual(status, 409)
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN, "set": ["x" * 70000]})[0], 413)
        audit = [json.loads(line) for line in (self.env.root / "audit.jsonl").read_text().splitlines()]
        save = next(e for e in audit if e["action"] == "recipes/save")
        self.assertEqual(save["arguments"]["text"]["bytes"], len(big.encode()))
        self.assertNotIn("max_batch", json.dumps(audit))                       # the text itself is not copied
        status, deleted = self.portal.request("POST", "recipes/delete", {"id": "qwen.big",
                                                                         "sha256": saved["result"]["sha256"]})
        self.assertEqual((status, deleted["ok"]), (200, True))
        receipt = {"disk_read_gbytes_ps": 6.1, "method": "direct", "host": "halo-1", "path": "/m.gguf"}
        status, stored = self.portal.request("POST", "recipes/save-receipt", {"name": "halo1", "text": json.dumps(receipt)})
        self.assertEqual(status, 200, stored)
        listing = self.portal.request("GET", "recipes")[1]["result"]
        self.assertEqual(listing["receipts"][0]["host"], "halo-1")

    def test_transport_rules(self):
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN}, csrf=False)[0], 403)
        self.assertEqual(self.portal.request("POST", "recipes/validate", raw=b"[1]")[0], 400)
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN, "bogus": 1})[0], 400)
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": "-h"})[0], 400)
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN,
                                                                         "overrides": ["../../x.json"]})[0], 400)
        # Synchronous answers are not blocked by a running job.
        self.portal.api._active = "job"
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN})[0], 200)

    def test_edits_are_one_overrides_layer(self):
        service.user_dir().mkdir(parents=True, exist_ok=True)
        (service.user_dir() / "mm-base.toml").write_text(MM_TEXT, encoding="utf-8")
        group = "routes.prefill_long.placement.rtx.units.layers"
        status, document = self.portal.request("POST", "recipes/explain", {
            "recipe": "mm-base", "edits": {group + ".rank": "list", group + ".list": [3, 1, 2]}})
        self.assertEqual((status, document["exit_code"]), (200, 0), document)
        self.assertIn("--recipe-overrides edits.json", document["cli"])
        self.assertEqual(json.loads(document["cli_files"]["edits.json"])["set"][group + ".list"], [3, 1, 2])
        rows = {f["path"]: f for f in document["result"]["fields"]}
        self.assertEqual(rows[group + ".list"]["value"], [3, 1, 2])
        status, lone = self.portal.request("POST", "recipes/validate", {"recipe": "mm-base",
                                                                       "set": [group + ".list=[3, 1, 2]"]})
        self.assertEqual(lone["exit_code"], 1)

    def test_custom_config_keeps_its_own_directories(self):
        other = self.env.root / "custom" / "cluster.json"
        other.parent.mkdir()
        other.write_text(self.env.config.read_text())
        portal = Portal(type("E", (), {"config": other, "root": other.parent})())
        try:
            status, saved = portal.request("POST", "recipes/save", {"id": "mine", "text": copy_of(QWEN, "mine")})
            self.assertEqual(status, 200, saved)
            self.assertEqual(Path(saved["result"]["path"]).parent, (other.parent / "recipes").resolve())
        finally:
            portal.close()
            service.use_config(self.env.config)


class RunnerTests(unittest.TestCase):
    def script(self, body):
        return [sys.executable, "-c", body]

    def test_admission_deadline_overflow_cancel_and_unicode(self):
        runner = recipe_web.Runner(workers=1, waiting=0)
        with mock.patch.object(recipe_web, "COMMAND", self.script("import time; time.sleep(30)")):
            started = time.monotonic()
            holder = threading.Thread(target=runner.run, args=([],), kwargs={"deadline_s": 3})
            holder.start()
            time.sleep(0.5)
            with self.assertRaises(recipe_web.Busy):
                runner.run([], deadline_s=3)
            holder.join()
            self.assertLess(time.monotonic() - started, 20)
            result = runner.run([], deadline_s=1)
            self.assertEqual(result.why, "deadline")
            status, document = recipe_web.document("validate", ["recipe", "validate"], result, now=0)
            self.assertEqual((status, document["ok"]), (504, False))
            gone = time.monotonic() + 0.5
            self.assertEqual(runner.run([], deadline_s=30, cancelled=lambda: time.monotonic() > gone).why, "client gone")
        with mock.patch.object(recipe_web, "COMMAND", self.script(
                "import sys; sys.stdout.write('x' * (9 * 1024 * 1024)); sys.stdout.flush()")):
            result = runner.run([], deadline_s=30)
            self.assertEqual(result.why, "output overflow")
            self.assertEqual(recipe_web.document("list", [], result, now=0)[0], 502)
        with mock.patch.object(recipe_web, "COMMAND", self.script(
                "import sys; sys.stderr.write('hyperloom: refusé ✓\\n'); sys.exit(1)")):
            result = runner.run([], deadline_s=30)
            status, document = recipe_web.document("validate", [], result, now=0)
            self.assertEqual((status, document["error"]), (200, "refusé ✓"))

    def test_whole_tree_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            beat = Path(directory) / "beat"
            grandchild = ("import pathlib, time\n"
                          f"p = pathlib.Path({str(beat)!r})\n"
                          "while True:\n    p.write_text(str(time.time())); time.sleep(0.05)\n")
            # The grandchild leaves the worker's process group / session (as ssh does in remote.py).
            spawn = ("import subprocess, sys, time\n"
                     f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], start_new_session=True, "
                     "creationflags=0x08000000 if sys.platform == 'win32' else 0)\n"
                     "time.sleep(60)\n")
            with mock.patch.object(recipe_web, "COMMAND", self.script(spawn)):
                result = recipe_web.Runner().run([], deadline_s=3)
            self.assertEqual(result.why, "deadline")
            time.sleep(1.0)
            last = beat.read_text()
            time.sleep(1.0)
            self.assertEqual(beat.read_text(), last, "a descendant survived the cleanup")

    def test_shutdown_kills_live_children(self):
        runner = recipe_web.Runner()
        with mock.patch.object(recipe_web, "COMMAND", self.script("import time; time.sleep(60)")):
            box = {}
            thread = threading.Thread(target=lambda: box.update(r=runner.run([], deadline_s=60)))
            thread.start()
            time.sleep(1.0)
            started = time.monotonic()
            runner.close()
            thread.join(30)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNotNone(box["r"].code)
        with self.assertRaises(recipe_web.Busy):
            runner.run([], deadline_s=1)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.env_context = isolated()
        self.env = self.env_context.__enter__()
        self.addCleanup(self.env_context.__exit__, None, None, None)
        self.orch = mock.Mock(wraps=orchestrate)
        self.orch.up.return_value = {"state": "running"}
        self.orch.down.return_value = {"state": "stopped"}
        self.api = API(self.env.config, orchestrator=self.orch, audit_path=self.env.root / "audit.jsonl")
        user_ds()
        self.source = session.saved_source(self.env.save_facts("ds", "deepseek-v4.1-flash", "ds41-rtx"))

    def wait(self, job):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            value = self.api.read("jobs/" + job["job"])
            if value["state"] != "running":
                return value
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def test_up_with_a_recipe_runs_the_cli_up(self):
        with mock.patch.object(commands, "live_source", return_value=self.source):
            job = self.wait(self.api.mutate("up", {"recipe": "ds-user", "profile": "ds41-rtx"}))
        self.assertEqual(job["state"], "succeeded", job["error"])
        cluster, profile = self.orch.up.call_args.args
        self.assertEqual(profile, "ds41-rtx")
        options = self.orch.up.call_args.kwargs["recipe"]
        self.assertTrue(options.digest)
        self.assertIn("recipe-inputs", options.recipe)                       # the snapshot, not the user file
        plain = self.wait(self.api.mutate("up", {"profile": "ds41-pair"}))
        self.assertIn("later phase", plain["error"])                         # the replica gate stays for plain up
        with self.assertRaises(APIError):
            self.api.mutate("up", {"recipe": "ds-user", "recipe_overrides": ["../x.json"]})

    def test_deploy_job_binds_the_planned_snapshot(self):
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            cluster = orchestrate.load_cluster(self.env.config)
            digest = service.plan(cluster, "ds-user", "ds41-rtx", source=self.source)["inputs"]
            with mock.patch.object(commands, "live_source", return_value=self.source):
                refused = self.wait(self.api.mutate("recipes/deploy", {"inputs": digest, "stop": ["qwen-replicas"]}))
                self.assertIn("running profiles are none", refused["error"])
                self.orch.down.assert_not_called()
                job = self.wait(self.api.mutate("recipes/deploy", {"inputs": digest}))
        self.assertEqual(job["state"], "succeeded", job["error"])
        self.assertEqual(self.orch.up.call_args.kwargs["recipe"].digest, digest)
        missing = self.wait(self.api.mutate("recipes/deploy", {"inputs": "0" * 64}))
        self.assertIn("no snapshot", missing["error"])
        with self.assertRaises(APIError):
            self.api.mutate("recipes/deploy", {"inputs": digest, "stop": "qwen-replicas"})
        with self.assertRaises(APIError):
            self.api.mutate("recipes/probe", {"profile": "qwen-replicas", "out": "../x"})


if __name__ == "__main__":
    unittest.main()
