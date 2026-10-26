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
from hyperloom.recipes.reader import RecipeError
from hyperloom.web import recipes as recipe_web
from hyperloom.web.api import API, APIError
from hyperloom.web.server import create_server
from tests.recipe_fixtures import builtin_text, copy_of, isolated
from tests.test_recipes_service import DS, DS_FIXTURE, QWEN, run_cli, user_ds

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


# This PC's free RAM, pinned for the portal's children and for the in-process CLI alike: plan answers (the RTX service
# plan, the free-RAM checks) depend on it, and a live value can change between the two runs.
PINNED_MEMORY = {"free_physical_gib": 61.25, "free_commit_gib": 127.75}
PINNED_CHILD = [sys.executable, "-c", "import sys; from hyperloom import rtx; "
                f"rtx.memory_status = lambda: dict({PINNED_MEMORY!r}); "
                "from hyperloom.cli import main; raise SystemExit(main(sys.argv[1:]))"]


def pinned_memory():
    """Pin rtx.memory_status here and in the portal's recipe children (a context manager)."""
    from contextlib import ExitStack
    from hyperloom import rtx
    stack = ExitStack()
    stack.enter_context(mock.patch.object(rtx, "memory_status", lambda: dict(PINNED_MEMORY)))
    stack.enter_context(mock.patch.object(recipe_web, "COMMAND", PINNED_CHILD))
    return stack


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
            with self.subTest(operation=operation, body=body), pinned_memory():
                status, document = self.portal.request("POST", "recipes/" + operation, body)
                self.assertIn(status, (200,), document)
                self.assertEqual(document["schema"], f"hyperloom/recipes-{operation}-v1")
                self.assertSameAsCli(document)
                if operation == "plan" and body["recipe"] == "ds-user":
                    # The child planned with the pinned free RAM (61.25 GiB free -> 41 GiB host pinned allowed).
                    self.assertIn("this start allows (41 GiB", json.dumps(document["result"]))
        status, refused = self.portal.request("POST", "recipes/validate", {"recipe": QWEN, "set": ["a.b=1"]})
        self.assertEqual((status, refused["exit_code"], refused["ok"]), (200, 1, False))
        self.assertRegex(refused["error"], "not a settable path|is not settable")

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
        # A receipt no answer could carry (NaN, an infinity) is refused when saved, and one written by hand is a row
        # error: the catalog still answers.
        for text in ('{"disk_read_gbytes_ps": NaN, "method": "direct", "host": "h", "path": "/m"}',
                     '{"disk_read_gbytes_ps": 1e999, "method": "direct", "host": "h", "path": "/m"}'):
            status, refused = self.portal.request("POST", "recipes/save-receipt", {"name": "bad", "text": text})
            self.assertEqual((status, refused["ok"]), (200, False), refused)
            self.assertIn("disk_read_gbytes_ps" if "1e999" in text else "NaN", json.dumps(refused))
        (Path(stored["result"]["path"]).parent / "by-hand.json").write_text(
            '{"disk_read_gbytes_ps": Infinity, "method": "direct", "host": "h", "path": "/m"}', encoding="utf-8")
        status, answer = self.portal.request("GET", "recipes")
        self.assertEqual(status, 200, answer)
        row = next(r for r in answer["result"]["receipts"] if r["name"] == "by-hand")
        self.assertIn("Infinity", row["error"])

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

    BAD_REFS = ("//pc/share/x.toml", "\\\\pc\\share\\x.toml", "\\\\?\\UNC\\pc\\s\\x",
                "C:x.toml", "C:\\x.toml", "/etc/x.toml", "x.toml:stream", "x::$DATA", "..", "../x", "a/b", "a\\b",
                "pyproject.toml", "x.JSON", "-h")

    def test_recipe_references_are_ids_never_paths(self):
        # A recipe file outside the catalog (a share, any local file): never read through a portal reference.
        outside = self.env.root / "share" / "x.toml"
        outside.parent.mkdir()
        outside.write_text(copy_of(QWEN, "outside"), encoding="utf-8")
        opened = []
        real_is_file, real_read = Path.is_file, Path.read_bytes

        def is_file(path):
            opened.append(str(path))
            return real_is_file(path)

        def read_bytes(path):
            opened.append(str(path))
            return real_read(path)
        for ref in self.BAD_REFS + (str(outside), outside.as_posix()):
            with self.subTest(ref=ref):
                opened.clear()
                with mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "read_bytes", read_bytes):
                    status, document = self.portal.request("GET", "recipes/" + ref)
                self.assertEqual(status, 400, document)
                self.assertFalse([p for p in opened if "share" in p or p.endswith(ref)], opened)
                for operation, body in (("validate", {"recipe": ref}), ("explain", {"recipe": ref}),
                                        ("plan", {"recipe": ref, "profile": "ds41-rtx"}),
                                        ("diff", {"a": QWEN, "b": ref}), ("diff", {"a": ref, "b": QWEN}),
                                        ("save", {"id": "copy", "base": ref, "base_sha256": "0" * 64}),
                                        ("save-layer", {"name": "layer", "recipe": ref, "set": ["limits.x=1"]})):
                    with self.assertRaises(ValueError):
                        recipe_web.Request("recipes/" + operation, body, config=self.env.config)
                    self.assertEqual(self.portal.request("POST", "recipes/" + operation, body)[0], 400)
        # "auto" picks a recipe only where the CLI takes it (plan, up); elsewhere it is an unknown (reserved) id.
        self.assertEqual(self.portal.request("GET", "recipes/auto")[0], 404)
        self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": "auto"})[0], 404)
        # An id-shaped reference is looked up in the catalog only, even when a file of that name exists where the
        # command runs (the children's working directory has NOTICE, LICENSE, README.md...).
        status, document = self.portal.request("POST", "recipes/validate", {"recipe": "NOTICE"})
        self.assertEqual((status, document["exit_code"]), (404, 3), document)
        status, document = self.portal.request("GET", "recipes/NOTICE")
        self.assertEqual((status, document["exit_code"]), (404, 3), document)

    def test_gets_have_no_side_effects(self):
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.env.root.rglob("*") if p.is_file())
        with mock.patch("tempfile.TemporaryDirectory", side_effect=AssertionError("a GET made a directory")), \
                mock.patch("tempfile.mkdtemp", side_effect=AssertionError("a GET made a directory")), \
                mock.patch("tempfile.mkstemp", side_effect=AssertionError("a GET made a file")):
            for endpoint in ("recipes", "recipes/" + QWEN, "recipes/ds-user", "recipes/active", "recipes/routes",
                             "recipes?model=deepseek-v4.1-flash", "recipes/active?profile=ds41-rtx"):
                status, document = self.portal.request("GET", endpoint)
                self.assertEqual((status, document["exit_code"]), (200, 0), document)
        after = sorted((str(p), p.stat().st_mtime_ns) for p in self.env.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertEqual(self.portal.request("GET", "recipes/active?profile=../x")[0], 400)

    def test_answers_are_redacted_and_internal_errors_answer(self):
        # A secret-named key of a launch record is redacted; the recipe's own numbers and prose are not.
        state = orchestrate._state_path("qwen-replicas")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"profile": "qwen-replicas", "phase": "running", "recipe": {
            "recipe": QWEN, "inputs": [], "router_api_key": "sekrit-value", "argv": ["--api-key", "sekrit-value"]},
            "config": orchestrate.config_record(self.env.config)}), encoding="utf-8")
        try:
            status, document = self.portal.request("GET", "recipes/active")
        finally:
            state.unlink()
        self.assertEqual(status, 200, document)
        self.assertNotIn("sekrit-value", json.dumps(document))
        self.assertEqual(document["result"]["deployments"][0]["record"]["router_api_key"], "[redacted]")
        status, document = self.portal.request("GET", "recipes/" + QWEN)
        when = next(r["when"] for r in document["result"]["structure"]["routes"] if r["id"] == "prefill_rtx")
        self.assertEqual(when["prompt_tokens_min"], 1024)                   # a measurement is not a secret
        from hyperloom.web.api import redact
        self.assertEqual(redact({"password": 123456, "api_key": 7, "token": 5, "secret": None, "prompt_tokens_min": 1,
                                 "prompt_tokens_max": None, "max_copies_per_token": 2, "api_key_per_token": 3,
                                 "password_tokens_min": 4, "secret_tokens": None, "private_key_tokens": 5}),
                         {"password": "[redacted]", "api_key": "[redacted]", "token": "[redacted]",
                          "secret": "[redacted]", "prompt_tokens_min": 1, "prompt_tokens_max": None,
                          "max_copies_per_token": 2, "api_key_per_token": "[redacted]",
                          "password_tokens_min": "[redacted]", "secret_tokens": "[redacted]",
                          "private_key_tokens": "[redacted]"})
        # The token-count exemption holds for numbers only: a string (or any structure) under such a key is redacted.
        self.assertEqual(redact({"service_tokens": "fixture-secret", "prompt_tokens": 7, "max_tokens": None,
                                 "access_tokens": ["fixture-secret"], "refresh_tokens": {"a": "fixture-secret"},
                                 "flag_tokens": True}),
                         {"service_tokens": "[redacted]", "prompt_tokens": 7, "max_tokens": None,
                          "access_tokens": "[redacted]", "refresh_tokens": "[redacted]", "flag_tokens": "[redacted]"})
        self.assertSameAsCli(document)
        with mock.patch.object(recipe_web, "COMMAND", [sys.executable, "-c", (
                "import sys; sys.stderr.write('hyperloom: refused: Authorization: Bearer abc.def\\n'); sys.exit(1)")]):
            status, document = self.portal.request("POST", "recipes/validate", {"recipe": QWEN})
        self.assertEqual((status, document["exit_code"]), (200, 1))
        self.assertNotIn("abc.def", document["error"])
        # An unexpected error in an in-process GET is an answer (the CLI's exit 1), never a dropped connection.
        with mock.patch.object(commands, "run", side_effect=TypeError("boom")):
            status, document = self.portal.request("GET", "recipes")
        self.assertEqual((status, document["exit_code"]), (200, 1))
        self.assertIn("internal error (TypeError): boom", document["error"])
        with mock.patch.object(self.portal.api, "recipe_get", side_effect=TypeError("boom")):
            self.assertEqual(self.portal.request("GET", "recipes")[0], 500)
        with mock.patch.object(self.portal.api, "recipe_post", side_effect=TypeError("boom")):
            self.assertEqual(self.portal.request("POST", "recipes/validate", {"recipe": QWEN})[0], 500)
        # The feature routes answer the same way (a GET document, a synchronous feature POST).
        with mock.patch.object(self.portal.api, "read", side_effect=TypeError("boom")):
            self.assertEqual(self.portal.request("GET", "planner")[0], 500)
        with mock.patch.object(self.portal.api, "feature_post", side_effect=TypeError("boom")):
            status, document = self.portal.request("POST", "planner/whatif", {"recipe": QWEN})
        self.assertEqual((status, document["error"]), (500, "internal error (TypeError)"))

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

    def test_stdin_is_supervised_too(self):
        # A child that never reads its stdin cannot hold the request past its deadline.
        with mock.patch.object(recipe_web, "COMMAND", self.script("import time; time.sleep(30)")):
            started = time.monotonic()
            result = recipe_web.Runner().run([], stdin=b"x" * (4 * 1024 * 1024), deadline_s=1)
        self.assertEqual(result.why, "deadline")
        self.assertLess(time.monotonic() - started, 15)

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
        # Recipe references and snapshot digests from the client are ids, never paths (nothing is opened).
        with mock.patch.object(service, "locate", side_effect=AssertionError("a client path reached locate")), \
                mock.patch.object(service, "load_inputs", side_effect=AssertionError("a client path was opened")):
            for ref in ContractTests.BAD_REFS:
                with self.subTest(ref=ref), self.assertRaises(APIError):
                    self.api.mutate("up", {"recipe": ref, "profile": "ds41-rtx"})
            for digest in ("../" + "0" * 61, "//pc/share/" + "0" * 42, "0" * 63 + "G", "x"):
                with self.subTest(digest=digest), self.assertRaises(APIError):
                    self.api.mutate("up", {"recipe_inputs": digest})
                with self.subTest(digest=digest), self.assertRaises(APIError):
                    self.api.mutate("recipes/deploy", {"inputs": digest})
            with self.assertRaises(APIError):
                self.api.mutate("recipes/deploy", {"inputs": "0" * 64, "stop": ["../x"]})
        self.assertIsNone(self.api._active)

    def test_config_in_use_is_per_call(self):
        # A call running on another configuration (a CLI up with --config, a portal on another file) does not move
        # the recipe directories of concurrent requests.
        other = self.env.root / "other" / "cluster.json"
        other.parent.mkdir()
        other.write_text(self.env.config.read_text(), encoding="utf-8")
        inside, release = threading.Event(), threading.Event()
        seen = {}

        def blocking_up(*a, **kw):
            seen["user_dir"] = service.user_dir()
            inside.set()
            release.wait(30)
            return {"state": "running"}
        self.orch.up.side_effect = blocking_up
        args = cli.build_parser().parse_args(["up", "ds41-rtx", "--recipe", DS, "--config", str(other)])
        worker = threading.Thread(target=cli.up_command, args=(args,), kwargs={"orchestrate": self.orch})
        worker.start()
        try:
            self.assertTrue(inside.wait(30))
            self.assertEqual(service.user_dir(), self.env.config.parent.absolute() / "recipes")
            status, listing = self.api.recipe_get("recipes", {})
            self.assertEqual(Path(listing["result"]["user_dir"]), self.env.config.parent.absolute() / "recipes")
        finally:
            release.set()
            worker.join(30)
        self.assertEqual(seen["user_dir"], other.parent.absolute() / "recipes")
        # The portal's recipe launches run on its own configuration: a client-named one is refused.
        with self.assertRaisesRegex(APIError, "portal's configuration"):
            self.api.mutate("up", {"recipe": DS, "profile": "ds41-rtx", "config": str(other)})
        with self.assertRaisesRegex(APIError, "portal's configuration"):
            self.api.mutate("up", {"recipe": DS, "profile": "ds41-rtx", "config": "//pc/share/cluster.json"})

    def test_no_option_injection_and_probe_names_in_the_portal_config(self):
        for body in ({"recipe": QWEN, "profile": "--recipe-dir=//pc/share"},
                     {"recipe": QWEN, "profile": "--recipe-profile=//pc/share/l.json"},
                     {"recipe": QWEN, "profile": "qwen-replicas", "recipe_workload": "--recipe-dir=//pc/share"},
                     {"profile": "--config=//pc/share/cluster.json"}, {"profile": "a/b"}):
            with self.subTest(body=body), self.assertRaises(APIError):
                self.api.mutate("up", body)
        for action, body in (("down", {"profile": "--force"}), ("cancel", {"profile": "-x"}),
                             ("install", {"profile": "qwen-replicas", "build_on": "--x"})):
            with self.subTest(action=action), self.assertRaises(APIError):
                self.api.mutate(action, body)
        # A probe job's file names resolve in the portal's configuration, not the process default.
        other = self.env.root / "other" / "cluster.json"
        other.parent.mkdir()
        other.write_text(self.env.config.read_text(), encoding="utf-8")
        api = API(other, orchestrator=self.orch, audit_path=self.env.root / "audit-other.jsonl")
        seen = []
        with mock.patch.object(api.recipes, "run", side_effect=lambda argv, **kw: seen.append(argv) or
                               recipe_web.Result(0, b"{}", b"")):
            job = api.mutate("recipes/probe", {"profile": "ds41-rtx", "out": "f1"})
            end = time.monotonic() + 30
            while api.read("jobs/" + job["job"])["state"] == "running" and time.monotonic() < end:
                time.sleep(0.02)
        out = Path(seen[0][seen[0].index("--out") + 1])
        self.assertEqual(out, other.parent.absolute() / "run" / "facts" / "f1.json")

    def test_cancel_an_up_with_a_recipe_and_no_profile(self):
        # The job names the profile it launches (bound when it is accepted), so its cancel stops that profile.
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            digest = service.plan(orchestrate.load_cluster(self.env.config), "ds-user", "ds41-rtx",
                                  source=self.source)["inputs"]
        for body, profile in (({"recipe": QWEN}, "qwen-replicas"), ({"recipe_inputs": digest}, "ds41-rtx")):
            with self.subTest(body=body):
                inside, release = threading.Event(), threading.Event()

                def launching(*a, **kw):
                    self.assertFalse(kw["cancelled"]())              # up owns the profile: the hand-over passed
                    inside.set()
                    release.wait(30)
                    return {"state": "running"}
                self.orch.up.side_effect = launching
                self.orch.down.reset_mock()
                with mock.patch.object(commands, "live_source", return_value=self.source):
                    job = self.api.mutate("up", body)
                    self.assertEqual(job["arguments"]["profile"], profile)
                    try:
                        self.assertTrue(inside.wait(30))
                        cancelled = self.wait(self.api.mutate("cancel", {"job": job["job"]}))
                    finally:
                        release.set()
                    self.wait(job)
                self.assertEqual(cancelled["state"], "succeeded", cancelled["error"])
                self.assertEqual(cancelled["result"]["phase"], "launching")
                self.orch.down.assert_called_once_with(profile)

    def test_cancel_an_up_with_a_recipe_before_its_hand_over(self):
        # Cancelled while it prepares (before up owns the profile): only the marker; up then starts nothing.
        cancelled, proceed = threading.Event(), threading.Event()
        answers = []

        def preparing(*a, **kw):
            proceed.wait(30)                                     # the cancel lands before the hand-over check
            if kw["cancelled"]():
                raise orchestrate.OrchestrationError("profile stop requested")
            return {"state": "running"}
        self.orch.up.side_effect = preparing
        with mock.patch.object(commands, "live_source", return_value=self.source):
            job = self.api.mutate("up", {"recipe": QWEN})
            answers.append(self.wait(self.api.mutate("cancel", {"job": job["job"]})))
            proceed.set()
            finished = self.wait(job)
        self.assertEqual(answers[0]["state"], "succeeded", answers[0]["error"])
        self.assertIn(answers[0]["result"]["phase"], ("not started", "checks"))
        self.assertIsNone(answers[0]["result"]["down"])
        self.assertIn("nothing was started", answers[0]["result"]["summary"])
        self.assertEqual(finished["state"], "failed")
        self.orch.down.assert_not_called()
        with self.assertRaises(APIError):
            self.api.mutate("up", {"recipe": DS})                       # two profiles serve it: name one

    def test_a_stop_of_its_profile_cancels_a_preparing_recipe_up(self):
        # `down P` (or a cancel by profile) while a recipe up prepares P: its marker is written too, so up, which
        # clears P's own stop marker when it takes P, starts nothing. A stop of another profile leaves it alone.
        for action, body, stops_it in (("down", {"profile": "qwen-replicas"}, True),
                                       ("cancel", {"profile": "qwen-replicas"}, True),
                                       ("down", {"profile": "mm"}, False)):
            with self.subTest(action=action, body=body):
                proceed = threading.Event()

                def preparing(*a, **kw):
                    proceed.wait(30)
                    if kw["cancelled"]():
                        raise orchestrate.OrchestrationError("profile stop requested")
                    return {"state": "running"}
                self.orch.up.side_effect = preparing
                self.orch.down.reset_mock()
                with mock.patch.object(commands, "live_source", return_value=self.source):
                    job = self.api.mutate("up", {"recipe": QWEN})
                    stopped = self.wait(self.api.mutate(action, body))
                    proceed.set()
                    finished = self.wait(job)
                self.assertEqual(stopped["state"], "succeeded", stopped["error"])
                self.assertEqual([c.args[0] for c in self.orch.down.call_args_list], [body["profile"]])
                if stops_it:
                    self.assertEqual(stopped["result"]["cancelled_switches"][0]["cancelled"], job["job"])
                    self.assertEqual(finished["state"], "failed")
                    self.assertRegex(finished["error"], "stop requested|up cancelled")   # nothing was started
                else:
                    self.assertNotIn("cancelled_switches", stopped["result"])
                    self.assertEqual(finished["state"], "succeeded", finished["error"])

    def test_cli_plain_up_takes_a_job(self):
        # `up PROFILE --job ID` (the portal's detached plain up): the job's marker is checked at the hand-over and
        # its progress is recorded in the configuration's directory, as for an up with a recipe.
        seen = []

        def up(*a, **kw):
            seen.append(kw["cancelled"]())
            if seen[-1]:
                raise orchestrate.OrchestrationError("profile stop requested")
            return {"state": "running"}
        self.orch.up.side_effect = up
        argv = ["up", "qwen-replicas", "--config", str(self.env.config), "--job"]
        self.assertEqual(cli.up_command(cli.build_parser().parse_args(argv + ["ab12cd34"]), orchestrate=self.orch),
                         {"state": "running"})
        with service.configured(self.env.config):
            self.assertEqual(service.read_progress("ab12cd34")["phase"], "done")
            self.assertEqual(service.read_progress("ab12cd34")["what"], "up")

        def cancel_at_hand_over(*a, **kw):
            with service.configured(self.env.config):
                service.cancel_marker("cd34ab12").touch()
            return up(*a, **kw)
        self.orch.up.side_effect = cancel_at_hand_over
        with self.assertRaisesRegex(orchestrate.OrchestrationError, "stop requested"):
            cli.up_command(cli.build_parser().parse_args(argv + ["cd34ab12"]), orchestrate=self.orch)
        self.assertEqual(seen, [False, True])
        with service.configured(self.env.config):
            self.assertEqual(service.read_progress("cd34ab12")["phase"], "failed")
            self.assertFalse(service.cancel_marker("cd34ab12").exists())

    def test_detached_stop_marks_a_recipe_up_of_that_profile(self):
        self.api.detached = True
        self.api._jobs["u1"] = {"job": "cd34ab12", "action": "up", "state": "running",
                                "arguments": {"recipe": QWEN, "profile": "qwen-replicas"}}
        with mock.patch.object(self.api, "_run_detached", return_value={"state": "stopped"}) as detached:
            result = self.api._execute("down", {"profile": "qwen-replicas"})
        detached.assert_called_once()
        self.assertEqual(result["cancelled_switches"][0]["cancelled"], "cd34ab12")
        with service.configured(self.env.config):
            self.assertTrue(service.cancel_marker("cd34ab12").exists())

    def test_detached_error_tail(self):
        # A recipe launch's refusal keeps the CLI's whole explanation; the other actions keep their short tail.
        child = mock.Mock(returncode=1)
        child.communicate.return_value = (b"", b"y" + b"x" * 20000)
        with mock.patch("hyperloom.web.api.subprocess.Popen", return_value=child):
            for action, args, size in (("down", {"profile": "qwen-replicas"}, 400),
                                       ("recipes/deploy", {"inputs": "0" * 64, "_job": "ab12cd34"}, 16384),
                                       ("up", {"recipe": QWEN, "profile": "qwen-replicas"}, 16384)):
                with self.subTest(action=action), self.assertRaises(APIError) as raised:
                    self.api._run_detached(action, args)
                self.assertEqual(len(str(raised.exception)), size)

    def test_detached_stop_cancels_a_running_switch(self):
        # The portal runs detached: `down` goes to its own CLI process, and still cancels the switch first.
        self.api.detached = True
        self.api._jobs["s1"] = {"job": "ab12cd34", "action": "recipes/deploy", "state": "running",
                                "arguments": {"inputs": "0" * 64}}
        with mock.patch.object(self.api, "_run_detached", return_value={"state": "stopped"}) as detached,                 mock.patch.object(self.api, "_cancel_deploy") as cancel:
            self.api._execute("down", {"profile": "qwen-replicas"})
        cancel.assert_called_once()
        detached.assert_called_once()

    def test_deploy_job_binds_the_planned_snapshot(self):
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            cluster = orchestrate.load_cluster(self.env.config)
            digest = service.plan(cluster, "ds-user", "ds41-rtx", source=self.source)["inputs"]
            with mock.patch.object(commands, "live_source", return_value=self.source):
                with mock.patch.object(self.api, "_audit", wraps=self.api._audit) as audit:
                    with self.assertRaisesRegex(APIError, "recipe preflight refused: .*running profiles are none"):
                        self.api.mutate("recipes/deploy", {"inputs": digest, "stop": ["qwen-replicas"]})
                    self.assertIn("running profiles are none", audit.call_args.args[2]["error"])
                self.orch.down.assert_not_called()
                job = self.wait(self.api.mutate("recipes/deploy", {"inputs": digest}))
        self.assertEqual(job["state"], "succeeded", job["error"])
        self.assertEqual(self.orch.up.call_args.kwargs["recipe"].digest, digest)
        # Cancel during the switch's checks: only its marker. Nothing is stopped (the running target is not touched)
        # and nothing is started; the answer says so.
        gate = threading.Event()
        original = launch.prepare

        def slow_prepare(*a, **kw):
            gate.wait(30)
            return original(*a, **kw)
        with mock.patch.object(render, "router_policies", return_value=("routes",)),                 mock.patch.object(commands, "live_source", return_value=self.source),                 mock.patch.object(launch, "prepare", side_effect=slow_prepare):
            self.orch.up.reset_mock()
            running = self.api.mutate("recipes/deploy", {"inputs": digest})
            cancelled = self.wait(self.api.mutate("cancel", {"job": running["job"]}))
            self.assertEqual(cancelled["state"], "succeeded", cancelled["error"])
            # (the deploy may not have recorded anything yet, or may have already met the marker and failed)
            self.assertIn(cancelled["result"]["phase"], ("not started", "checks", "failed"))
            self.assertIsNone(cancelled["result"]["down"])
            self.assertIn("; this deploy had stopped nothing", cancelled["result"]["summary"])
            self.orch.down.assert_not_called()
            gate.set()
            finished = self.wait(running)
        self.assertIn("deploy cancelled", finished["error"])
        self.orch.up.assert_not_called()
        # A plain stop (down of any profile) during a switch cancels the switch as well.
        gate.clear()
        with mock.patch.object(render, "router_policies", return_value=("routes",)), \
                mock.patch.object(commands, "live_source", return_value=self.source), \
                mock.patch.object(launch, "prepare", side_effect=slow_prepare):
            running = self.api.mutate("recipes/deploy", {"inputs": digest})
            stopped = self.wait(self.api.mutate("down", {"profile": "qwen-replicas"}))
            self.assertEqual(stopped["state"], "succeeded", stopped["error"])
            gate.set()
            finished = self.wait(running)
        self.assertIn("deploy cancelled", finished["error"])
        self.orch.up.assert_not_called()
        # The API forwards every up argument to the CLI, which refuses what a snapshot launch does not take.
        with self.assertRaisesRegex(APIError, "recipe preflight refused: .*--recipe-inputs"):
            self.api.mutate("up", {"recipe_inputs": digest, "node": ["halo-1"],
                                   "recipe_set": ["limits.host_free_ram_floor_gib=1"]})
        with mock.patch.object(self.api, "_audit", wraps=self.api._audit) as audit:
            with self.assertRaisesRegex(APIError, "recipe preflight refused: .*no snapshot"):
                self.api.mutate("recipes/deploy", {"inputs": "0" * 64})
            self.assertIn("no snapshot", audit.call_args.args[2]["error"])
        with self.assertRaises(APIError):
            self.api.mutate("recipes/deploy", {"inputs": digest, "stop": "qwen-replicas"})
        with self.assertRaises(APIError):
            self.api.mutate("recipes/probe", {"profile": "qwen-replicas", "out": "../x"})


class DeployCancelTests(unittest.TestCase):
    """Cancel at every step of a deploy (the switch): before the hand-over only the marker (nothing the deploy did not
    stop is stopped, nothing is started); after it, the target, the profile the deploy started. Each answer and the
    deploy's error say what was stopped."""

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
        with mock.patch.object(render, "router_policies", return_value=("routes",)):
            self.digest = service.plan(orchestrate.load_cluster(self.env.config), "ds-user", "ds41-rtx",
                                       source=self.source)["inputs"]
        self.answers = []

    def deploy(self, stop, *, prepare=None):
        """Run the deploy job with these profiles running (and stopped by it); returns the finished job."""
        running = {name: {"profile": name} for name in stop}
        original = launch.prepare
        with mock.patch.object(render, "router_policies", return_value=("routes",)), \
                mock.patch.object(commands, "live_source", return_value=self.source), \
                mock.patch.object(service, "_states", return_value=running), \
                mock.patch.object(launch, "prepare", side_effect=lambda *a, **kw: (prepare and prepare(),
                                                                                   original(*a, **kw))[1]):
            job = self.api.mutate("recipes/deploy", {"inputs": self.digest, "stop": list(stop)})
            end = time.monotonic() + 60
            while self.api.read("jobs/" + job["job"])["state"] == "running":
                self.assertLess(time.monotonic(), end)
                time.sleep(0.02)
            return self.api.read("jobs/" + job["job"])

    def cancel(self, action="cancel", **arguments):
        """What the portal's cancel (or a stop) does right now, from inside the deploy (the job is running)."""
        if action == "cancel":
            jid = next(j["job"] for j in self.api._jobs.values()
                       if j["action"] == "recipes/deploy" and j["state"] == "running")
            arguments = {"job": jid}
        self.answers.append(self.api._execute(action, arguments))
        return self.answers[-1]

    def downs(self):
        return [c.args[0] for c in self.orch.down.call_args_list]

    def test_before_the_deploy_records_anything(self):
        job = {"job": "ab12cd34", "action": "recipes/deploy", "state": "running", "arguments": {"inputs": self.digest}}
        answer = self.api._cancel_deploy(job)
        self.assertEqual((answer["phase"], answer["down"]), ("not started", None))
        with service.configured(self.env.config), mock.patch.object(service, "_states", return_value={}):
            with self.assertRaisesRegex(RecipeError, "deploy cancelled: cancelled before the launch: nothing was "
                                                     "started; this deploy had stopped nothing"):
                service.deploy(self.digest, [], orchestrate=self.orch, source=self.source, job="ab12cd34")
            self.assertEqual(service.read_progress("ab12cd34")["phase"], "failed")
            self.assertFalse(service.cancel_marker("ab12cd34").exists())
        self.orch.down.assert_not_called()
        self.orch.up.assert_not_called()

    def test_during_the_checks_the_running_target_is_not_stopped(self):
        # The review's case: ds41-rtx runs, a recipe is deployed on it, Cancel during the pre-stop checks.
        finished = self.deploy(["ds41-rtx"], prepare=self.cancel)
        self.assertEqual(finished["state"], "failed")
        self.assertIn("deploy cancelled: cancelled before the launch: nothing was started; this deploy had stopped "
                      "nothing", finished["error"])
        self.assertEqual((self.answers[0]["phase"], self.answers[0]["profile"], self.answers[0]["down"]),
                         ("checks", "ds41-rtx", None))
        self.assertEqual(self.downs(), [])
        self.orch.up.assert_not_called()

    def test_while_stopping_only_what_it_stopped_is_reported(self):
        def down(name, *a, **kw):
            if name == "mm":
                self.cancel()
            return {"state": "stopped"}
        self.orch.down.side_effect = down
        finished = self.deploy(["mm", "qwen-replicas"])
        self.assertEqual(self.downs(), ["mm"])                           # the second stop never happens
        self.orch.up.assert_not_called()
        answer = self.answers[0]
        self.assertEqual((answer["phase"], answer["stopping"], answer["stopped_by_deploy"], answer["down"]),
                         ("stopping", "mm", [], None))
        self.assertIn("it was stopping mm", answer["summary"])
        self.assertIn("nothing was started; this deploy had stopped mm (stopped): they stay stopped", finished["error"])

    def test_a_cancel_at_a_stop_never_answers_nothing_while_the_stop_runs(self):
        # The cancel lands right next to the record of "stopping qwen-replicas", on either side of it. Before it: the
        # cancel reads the checks and the deploy, meeting the marker, stops nothing. After it: the deploy still meets
        # the marker (recorded first, checked next) and leaves qwen-replicas running; the cancel's answer had named
        # that stop. Never "stopped nothing" while the deploy goes on to stop it.
        real = service._write_progress
        for before in (True, False):
            with self.subTest(before=before):
                self.orch.down.reset_mock()
                self.answers.clear()

                def write(path, state):
                    landing = state["phase"] == "stopping" and state["stopping"] == "qwen-replicas" \
                        and not self.answers
                    if landing and before:
                        self.cancel()
                    real(path, state)
                    if landing and not before:
                        self.cancel()
                with mock.patch.object(service, "_write_progress", side_effect=write):
                    finished = self.deploy(["qwen-replicas"])
                self.assertEqual(self.downs(), [])                       # nothing stopped, by the deploy or the cancel
                self.orch.up.assert_not_called()
                answer = self.answers[0]
                self.assertEqual((answer["phase"], answer["stopping"], answer["down"]),
                                 ("checks", None, None) if before else ("stopping", "qwen-replicas", None))
                if not before:
                    self.assertIn("it was stopping qwen-replicas (or, meeting this cancel first, left it running",
                                  answer["summary"])
                self.assertEqual(finished["state"], "failed")
                self.assertIn("deploy cancelled: cancelled before the launch: nothing was started; this deploy had "
                              "stopped nothing", finished["error"])
                self.assertNotIn("it was stopping", finished["error"])

    def test_at_the_hand_over_up_starts_nothing(self):
        def up(*a, **kw):
            self.cancel()                                                # before up's own check
            if kw["cancelled"]():
                raise orchestrate.OrchestrationError("profile stop requested")
            return {"state": "running"}
        self.orch.up.side_effect = up
        finished = self.deploy(["qwen-replicas"])
        self.assertEqual(self.downs(), ["qwen-replicas"])                # the deploy's own stop; never the target
        self.assertEqual((self.answers[0]["phase"], self.answers[0]["down"]), ("stopping", None))
        self.assertIn("deploy cancelled at the launch hand-over: nothing was started; this deploy had stopped "
                      "qwen-replicas", finished["error"])

    def test_at_the_hand_over_a_cancel_that_sees_launching_stops_the_target(self):
        # The cancel lands between up recording "launching" and checking the marker: up starts nothing and the
        # cancel sends the target its stop (harmless: nothing runs).
        real = service._write_progress

        def write(path, state):
            real(path, state)
            if state["phase"] == "launching":
                self.cancel()
        self.orch.up.side_effect = lambda *a, **kw: (_ for _ in ()).throw(
            orchestrate.OrchestrationError("profile stop requested")) if kw["cancelled"]() else {"state": "running"}
        with mock.patch.object(service, "_write_progress", side_effect=write):
            finished = self.deploy(["qwen-replicas"])
        self.assertEqual(self.downs(), ["qwen-replicas", "ds41-rtx"])
        self.assertEqual(self.answers[0]["phase"], "launching")
        self.assertIn("nothing was started", finished["error"])

    def test_after_the_launch_began_the_target_is_stopped(self):
        def up(*a, **kw):
            self.assertFalse(kw["cancelled"]())                         # up owns the profile, the launch goes on
            self.cancel()
            raise orchestrate.OrchestrationError("profile stop requested")   # its stop marker ends the launch
        self.orch.up.side_effect = up
        finished = self.deploy(["qwen-replicas"])
        self.assertEqual(self.downs(), ["qwen-replicas", "ds41-rtx"])
        answer = self.answers[0]
        self.assertEqual((answer["phase"], answer["profile"], answer["down"]), ("launching", "ds41-rtx",
                                                                                 {"state": "stopped"}))
        self.assertIn("ds41-rtx: stopped; this deploy had stopped qwen-replicas (stopped)", answer["summary"])
        self.assertIn("deploy cancelled after the launch began: the cancel stops ds41-rtx (its answer says how); this "
                      "deploy had stopped qwen-replicas (stopped)", finished["error"])

    def test_after_the_deploy_finished(self):
        finished = self.deploy([])
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        # A cancel still racing the job's end stops what the deploy started; a finished job cannot be cancelled.
        answer = self.api._cancel_deploy(dict(finished, state="running"))
        self.assertEqual((answer["phase"], answer["down"]), ("done", {"state": "stopped"}))
        self.assertEqual(self.downs(), ["ds41-rtx"])
        with self.assertRaises(APIError) as refused:
            self.api._execute("cancel", {"job": finished["job"]})
        self.assertEqual(refused.exception.status, 409)

    def test_unreadable_progress_is_never_nothing_to_undo(self):
        # The launch has begun ("launching"); the cancel cannot read the progress: it fails, saying so, and does not
        # claim the deploy had not started.
        real = Path.read_bytes

        def unreadable(path, *a, **kw):
            if path.parent.name == "deploy-cancel":
                raise PermissionError(13, "in use")
            return real(path, *a, **kw)
        failures = []

        def up(*a, **kw):
            self.assertFalse(kw["cancelled"]())
            with mock.patch.object(Path, "read_bytes", unreadable), mock.patch("time.sleep"):
                try:
                    self.cancel()
                except APIError as exc:
                    failures.append(exc)
            return {"state": "running"}
        self.orch.up.side_effect = up
        finished = self.deploy([])
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].status, 500)
        self.assertIn("progress cannot be read", str(failures[0]))
        self.assertIn("stop that profile with down", str(failures[0]))
        self.assertEqual(self.downs(), [])
        with service.configured(self.env.config):
            service.deploy_progress("ab12cd34").parent.mkdir(parents=True, exist_ok=True)
            for data in (b"{", b"\xff\xfe{}"):
                service.deploy_progress("ab12cd34").write_bytes(data)
                with self.assertRaisesRegex(RecipeError, "not valid JSON"):
                    service.read_progress("ab12cd34")
            self.assertIsNone(service.read_progress("cd34ab12"))

    def test_outcomes_are_reported_as_they_are(self):
        # A down that could only request the stop (the launch still runs) is reported as requested, not stopped.
        def down(name, *a, **kw):
            return {"state": "stop-requested"} if name in ("mm", "ds41-rtx") else {"state": "stopped"}
        self.orch.down.side_effect = down

        def up(*a, **kw):
            self.assertFalse(kw["cancelled"]())
            self.cancel()
            raise orchestrate.OrchestrationError("profile stop requested")
        self.orch.up.side_effect = up
        finished = self.deploy(["mm", "qwen-replicas"])
        answer = self.answers[0]
        self.assertIn("ds41-rtx: stop requested; this deploy had stopped mm (stop requested), qwen-replicas "
                      "(stopped)", answer["summary"])
        self.assertIn("mm (stop requested)", finished["error"])
        # A launch that began and then failed may keep its ownership (its cleanup can fail): the cancel stops it.
        failed = {"phase": "failed", "profile": "ds41-rtx", "launched": True, "stopped": []}
        self.assertTrue(service.launch_began(failed))
        self.assertIn("had begun (the launch had then failed): ds41-rtx: stopped",
                      service.cancel_summary(failed, down={"state": "stopped"}))
        self.assertFalse(service.launch_began(dict(failed, launched=False)))

    def test_a_finished_job_is_refused_over_http(self):
        finished = self.deploy([])
        server = create_server(0, api=self.api)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            host = f"127.0.0.1:{server.server_port}"
            body = json.dumps({"job": finished["job"]}).encode()
            connection = HTTPConnection(host, timeout=30)
            connection.request("POST", "/hyperloom/api/cancel", body=body, headers={
                "Host": host, "Origin": "http://" + host, "Content-Type": "application/json",
                "Content-Length": str(len(body)), "X-Hyperloom-CSRF": server.csrf_token})
            response = connection.getresponse()
            self.assertEqual(response.status, 409, response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(self.downs(), [])

    def test_a_stop_runs_even_when_the_cancel_files_fail(self):
        def stop_with(failure):
            def hook():
                with failure():
                    self.cancel("down", profile="mm")
            return hook
        real_touch = Path.touch

        def no_marker():
            def touch(path, *a, **kw):
                if path.parent.name == "deploy-cancel":
                    raise PermissionError(13, "denied")
                return real_touch(path, *a, **kw)
            return mock.patch.object(Path, "touch", touch)

        def garbled_progress():
            jid = next(j["job"] for j in self.api._jobs.values()
                       if j["action"] == "recipes/deploy" and j["state"] == "running")
            with service.configured(self.env.config):
                service.deploy_progress(jid).write_bytes(b"\xff\xfe")
            return mock.patch("time.sleep")
        for failure, expected in ((no_marker, "could not be written"), (garbled_progress, "not valid JSON")):
            with self.subTest(failure=failure.__name__):
                self.orch.down.reset_mock()
                self.answers.clear()
                self.deploy(["qwen-replicas"], prepare=stop_with(failure))
                self.assertEqual(self.downs()[0], "mm")                  # the stop asked for ran
                self.assertIn(expected, self.answers[0]["cancelled_switches"][0]["error"])

    def test_a_stop_of_another_profile_only_cancels(self):
        # Before the hand-over: the switch stops at its next step; only the named profile is stopped.
        finished = self.deploy(["qwen-replicas"], prepare=lambda: self.cancel("down", profile="mm"))
        self.assertEqual(self.downs(), ["mm"])
        self.assertEqual(self.answers[0]["cancelled_switches"][0]["phase"], "checks")
        self.assertIn("deploy cancelled", finished["error"])
        # After it: the launch the switch handed over goes on; the stop does not take its target down.
        self.orch.down.reset_mock()
        self.answers.clear()

        def up(*a, **kw):
            self.assertFalse(kw["cancelled"]())
            self.cancel("down", profile="mm")
            return {"state": "running"}
        self.orch.up.side_effect = up
        finished = self.deploy(["qwen-replicas"])
        self.assertEqual(finished["state"], "succeeded", finished["error"])
        self.assertEqual(self.downs(), ["qwen-replicas", "mm"])
        switch = self.answers[0]["cancelled_switches"][0]
        self.assertEqual((switch["phase"], switch["down"]), ("launching", None))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.environ.get("HYPERLOOM_TM_SRC"), "set HYPERLOOM_TM_SRC to a TensorMeld src/ with per-request "
                     "route records (branch router/recipe-routes-recent)")
class RouterRecordTests(unittest.TestCase):
    """`recipe routes` against the real TensorMeld router (fake engine and prefill service from its own tests)."""

    def test_routes_reads_the_routers_per_request_records(self):
        source = Path(os.environ["HYPERLOOM_TM_SRC"])
        # This TensorMeld, not the pinned one another test may already have imported.
        names = ("tensormeld", "test_prefill_routes", "test_replica_router")
        saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in names}
        for key in saved:
            del sys.modules[key]
        sys.path[:0] = [str(source), str(source.parent / "tests")]

        def restore():
            for p in (str(source), str(source.parent / "tests")):
                sys.path.remove(p)
            for key in [k for k in sys.modules if k.split(".")[0] in names]:
                del sys.modules[key]
            sys.modules.update(saved)
        self.addCleanup(restore)
        import test_prefill_routes as tm
        harness = tm.RoutesEndToEndTests("test_long_prompts_offload_short_ones_stay_local")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        router, port = harness.router(harness.service(), harness.engines(1))
        tm.call(port, "POST", "/v1/completions", {"prompt": list(range(5000))})
        tm.call(port, "POST", "/v1/completions", {"prompt": list(range(100))})
        record = {"recipe": "mm-example",
                  "deploy": {"prefill": [{"id": "prefill_long", "place": "offload"}, {"id": "prefill_nodes", "place": "local"}]},
                  "route_table": {"effective_sha256": "b" * 64, "state_file": harness.state_path,
                                  "routes": [{"id": "prefill_long", "enabled": True}, {"id": "prefill_nodes", "enabled": True}]}}
        states = {"mm": {"profile": "mm", "recipe": record, "router": {"endpoint": {"address": "127.0.0.1", "port": port}}}}
        row = service.routes(None, states=states)["deployments"][0]
        self.assertEqual(row["why"], {})
        self.assertEqual((row["totals"]["rtx"], row["totals"]["nodes"]), (1, 1))
        self.assertEqual([(r["route"], r["on"], r["outcome"], r["status"]) for r in row["recent"]],
                         [("prefill_long", "rtx", "served", 200), ("prefill_nodes", "nodes", "served", 200)])
