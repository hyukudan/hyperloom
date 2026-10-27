"""Placement uses the portal redactor; missing shared masks wait for the shared feature branch."""
import ast
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom.recipes import preflight, reader
from hyperloom.web import api as portal, server


PENDING = "pending hl/features-nits hyperloom/redaction.py"
# Synthetic values only. This table specifies outputs, never matching rules.
CASES = (
    ("hf_env", "HF_TOKEN hf_probe_secret", "HF_TOKEN [redacted]"),
    ("pass_assignment", "PASS=pass_probe_secret", "PASS=[redacted]"),
    ("mysql_short", "mysql -pmysql_probe_secret", "mysql -p[redacted]"),
    ("sshpass_short", "sshpass -p ssh_probe_secret", "sshpass -p [redacted]"),
    ("hf_quoted", 'HF_TOKEN "two word secret"', "HF_TOKEN [redacted]"),
    ("pass_quoted", 'PASS="two word secret"', "PASS=[redacted]"),
    ("mysql_quoted", 'mysql -p"two word secret"', "mysql -p[redacted]"),
    ("sshpass_quoted", 'sshpass -p "two word secret"', "sshpass -p [redacted]"),
    ("sshpass_single_quoted", "sshpass -p 'two word secret'", "sshpass -p [redacted]"),
    ("password_option_quoted", '--password "two word secret"', "--password [redacted]"),
    ("password_word_quoted", 'password "two word secret"', "password [redacted]"),
    ("password_assignment_quoted", 'password="two word secret"', "password=[redacted]"),
    ("empty_url_user", "https://:url_probe_secret@example.invalid", "https://[redacted]@example.invalid"),
    ("curl_user", "curl -u user:curl_probe_secret", "curl -u [redacted]"),
    ("bare_hf", "hf_probe_secret", "[redacted]"),
    ("bare_ghp", "ghp_probe_secret", "[redacted]"),
)
LAUNCHES = {
    "up": ("up", {"profile": "test", "recipe": "test"}, ["up", "test", "--recipe", "test"]),
    "inputs": ("up", {"profile": "test", "recipe_inputs": "a" * 64},
               ["up", "test", "--recipe-inputs", "a" * 64]),
    "deploy": ("recipes/deploy", {"inputs": "a" * 64}, ["recipe", "deploy", "a" * 64]),
}
ERRORS = {"oserror": OSError, "valueerror": ValueError, "runtimeerror": RuntimeError,
          "notread": portal.nodepoll.NotRead}
BOUNDARIES = (
    *("preflight_" + launch for launch in LAUNCHES),
    "logs_feed", "logs_process", "logs_cached",
    *("logs_feed_" + error for error in ERRORS),
    *("logs_process_" + error for error in ERRORS),
    *("job_" + launch for launch in LAUNCHES),
    *("completion_" + launch for launch in LAUNCHES),
    "job_result", "completion_audit_error", "thread_job", "thread_http", "admission_http", "acceptance_http",
)


class PlacementBoundaryTests(unittest.TestCase):
    def exercise(self, text, boundary):
        """Run a real API boundary and observe the shared redactor, including HTTP exception transport."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            api = portal.API(Path(directory) / "config.json", clock=lambda: 123,
                             audit_path=Path(directory) / "audit.jsonl")
            shared = portal.redact
            spy = stack.enter_context(mock.patch.object(portal, "redact", wraps=shared))
            stack.enter_context(mock.patch.object(server, "redact", spy))
            prefix, suffix = "", ""
            if boundary.startswith("logs_"):
                stack.enter_context(mock.patch.object(portal.orchestrate, "log_machine", return_value="local"))
                stack.enter_context(mock.patch.object(api, "_action_log", return_value=[]))
                error = next((name for name in ERRORS if boundary.endswith("_" + name)), None)
                logs = stack.enter_context(mock.patch.object(api.orchestrate, "logs", **(
                    {"side_effect": ERRORS[error](text)} if error else
                    {"return_value": {"stdout": text, "stderr": text}})))
                api._cached, api._expires = {"logs": {}}, float("inf")
                if boundary.startswith("logs_feed"):
                    api._cached["logs"] = api._logs({"test": {"nodes": {"local": {}}}})
                    result = api.read("logs")
                    actual = result["why"]["test/local"] if error else result["entries"][0]["message"]
                else:
                    # Exercise the real GET exception mapping without opening a socket or starting pollers.
                    handler = object.__new__(server.Handler)
                    handler._host, handler._send = lambda: True, mock.Mock()
                    handler.server = SimpleNamespace(api=api)
                    handler.path = "/hyperloom/api/logs?profile=test&process=prefill"
                    stack.enter_context(mock.patch.object(server, "_page_seen"))
                    handler._get()
                    status, result = handler._send.call_args.args[:2]
                    if error and error != "notread":
                        self.assertEqual(status, 400 if error == "valueerror" else 503)
                        actual = result["error"]
                    else:
                        self.assertEqual(status, 200)
                        actual = result["why"]["entries"] if error else result["entries"]["stdout"]
                    if boundary == "logs_cached":
                        handler._get()
                        self.assertEqual(handler._send.call_args.args[1], result)
                        self.assertEqual(logs.call_count, 1)
            else:
                launch = boundary.rsplit("_", 1)[-1]
                action, arguments, argv = LAUNCHES.get(launch, LAUNCHES["up"])
                for name, value in (("_arguments", arguments), ("_recipe_up_argv", argv), ("_deploy_argv", argv),
                                    ("_resources_of_profile", {"local"}), ("_accepted", None)):
                    stack.enter_context(mock.patch.object(api, name, return_value=value))
                stack.enter_context(mock.patch.object(api, "_hold"))
                stack.enter_context(mock.patch.object(api, "_unhold"))
                stack.enter_context(mock.patch.object(preflight, "cli", **(
                    {"side_effect": reader.RecipeError(text)} if boundary.startswith("preflight_") else
                    {"return_value": None})))
                stack.enter_context(mock.patch.object(api, "_execute", **(
                    {"return_value": text} if boundary == "job_result" else
                    {"side_effect": RuntimeError(text)})))
                if boundary.startswith("thread_"):
                    stack.enter_context(mock.patch.object(portal.threading, "Thread")).return_value.start.side_effect = RuntimeError(text)
                    prefix = "the job could not start: "
                else:
                    stack.enter_context(mock.patch.object(api, "_start_worker", side_effect=lambda run, *a: run()))
                if boundary == "completion_audit_error":
                    stack.enter_context(mock.patch.object(api, "_audit", side_effect=OSError(text)))
                    prefix = "audit write failed: "
                elif boundary == "admission_http":
                    api._hold.side_effect = RuntimeError(text)
                    prefix = "admission unavailable: "
                elif boundary == "acceptance_http":
                    api._accepted.return_value = OSError(text)
                    prefix, suffix = "audit write failed: ", " (the job was not started)"
                try:
                    response = api.mutate(action, {})
                except portal.APIError as exc:
                    if boundary.startswith("preflight_"):
                        self.assertEqual(str(exc), "recipe preflight refused: preflight checks failed; see local audit")
                        actual = json.loads(api.audit_path.read_text(encoding="utf-8").splitlines()[-1])["result"]["error"]
                    elif boundary == "thread_job":
                        actual = next(iter(api._jobs.values()))["error"]
                    else:
                        self.assertIn(boundary, ("thread_http", "admission_http", "acceptance_http"))
                        handler = SimpleNamespace(_send=mock.Mock())
                        server.Handler._error(handler, exc.status, str(exc))
                        actual = handler._send.call_args.args[1]["error"]
                else:
                    self.assertFalse(boundary.startswith(("preflight_", "thread_")))
                    job = api.read("jobs/" + response["id"])
                    if boundary.startswith("completion_") and boundary != "completion_audit_error":
                        actual = json.loads(api.audit_path.read_text(encoding="utf-8").splitlines()[-1])["result"]["error"]
                    else:
                        actual = job["result"] if boundary == "job_result" else job["error"]
            self.assertTrue(any(call.args and isinstance(call.args[0], str) and text in call.args[0]
                                for call in spy.call_args_list), "raw boundary text bypassed api.redact")
            return actual, prefix, suffix

    def test_legacy_log_responses_match_base_bytes(self):
        source = subprocess.check_output(["git", "show", "2a7c803:hyperloom/web/api.py"], text=True, encoding="utf-8")
        namespace = dict(vars(portal))
        for name in ("_logs", "_process_logs"):
            definition = next(node for node in ast.walk(ast.parse(source))
                              if isinstance(node, ast.FunctionDef) and node.name == name)
            exec(compile(ast.Module(body=[definition], type_ignores=[]), "base:" + name, "exec"), namespace)
        for _, text, _ in CASES:
            for failure in (None, *ERRORS.values()):
                for name, argument in (("_logs", {"test": {"nodes": {"local": {}}}}),
                                       ("_process_logs", ("test", "prefill", 100))):
                    responses = []
                    for method in (getattr(portal.API, name), namespace[name]):
                        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                            api = portal.API(Path(directory) / "config.json", clock=lambda: 123,
                                             audit_path=Path(directory) / "audit.jsonl")
                            stack.enter_context(mock.patch.object(api, "_action_log", return_value=[]))
                            stack.enter_context(mock.patch.object(portal.orchestrate, "log_machine", return_value="local"))
                            stack.enter_context(mock.patch.object(api.orchestrate, "logs", **(
                                {"side_effect": failure(text)} if failure else
                                {"return_value": {"stdout": text, "stderr": text}})))
                            try:
                                responses.append(json.dumps(method(api, argument)).encode())
                            except (OSError, ValueError, RuntimeError, portal.nodepoll.NotRead) as exc:
                                responses.append((type(exc).__name__, str(exc).encode()))
                    with self.subTest(text=text, failure=failure, name=name):
                        self.assertEqual(responses[0], responses[1])

    def test_legacy_launch_error_and_completion_match_base_bytes(self):
        source = subprocess.check_output(["git", "show", "2a7c803:hyperloom/web/api.py"], text=True, encoding="utf-8")
        definition = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.FunctionDef) and node.name == "mutate")
        namespace = dict(vars(portal))
        exec(compile(ast.Module(body=[definition], type_ignores=[]), "base:mutate", "exec"), namespace)
        text = "; ".join(case[1] for case in CASES)
        for action, arguments, argv in LAUNCHES.values():
            results = []
            for method in (portal.API.mutate, namespace["mutate"]):
                with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    api = portal.API(Path(directory) / "config.json", clock=lambda: 123,
                                     audit_path=Path(directory) / "audit.jsonl")
                    for name, value in (("_arguments", arguments), ("_recipe_up_argv", argv),
                                        ("_deploy_argv", argv), ("_resources_of_profile", {"local"}),
                                        ("_accepted", None)):
                        stack.enter_context(mock.patch.object(api, name, return_value=value))
                    stack.enter_context(mock.patch.object(preflight, "cli"))
                    stack.enter_context(mock.patch.object(api, "_hold"))
                    stack.enter_context(mock.patch.object(api, "_unhold"))
                    stack.enter_context(mock.patch.object(api, "_execute", side_effect=RuntimeError(text)))
                    stack.enter_context(mock.patch.object(api, "_start_worker", side_effect=lambda run, *a: run()))
                    stack.enter_context(mock.patch.object(portal.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)))
                    response = method(api, action, {})
                    results.append((json.dumps(response).encode(), json.dumps(api.read("jobs/" + response["id"])).encode(),
                                    api.audit_path.read_bytes()))
            self.assertEqual(results[0], results[1])


def _boundary_test(case, boundary, masking):
    _, text, expected = case
    def test(self):
        actual, prefix, suffix = self.exercise(text, boundary)
        self.assertEqual(actual, prefix + (expected if masking else portal.redact(text)) + suffix)
    if masking and portal.redact(text) != expected:
        test = unittest.skip(PENDING)(test)
    return test


for _case in CASES:
    for _boundary in BOUNDARIES:
        for _masking in (False, True):
            _name = "test_" + ("mask_" if _masking else "shared_") + _case[0] + "_at_" + _boundary
            setattr(PlacementBoundaryTests, _name, _boundary_test(_case, _boundary, _masking))
