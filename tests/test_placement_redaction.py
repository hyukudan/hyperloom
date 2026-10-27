"""Placement free-text rules and the legacy log boundary, without hardware or node calls."""
import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from hyperloom.web import api as portal


TEXT = "HF_TOKEN hf_probe_secret; PASS=pass_probe_secret; mysql -pmysql_probe_secret; sshpass -p ssh_probe_secret"


class PlacementRedactionTests(unittest.TestCase):
    def test_node_rules_do_not_outgrow_packaged_placement_rules(self):
        from hyperloom.placement_redaction import redact
        from tests.test_ops_logsearch import node
        corpus = [TEXT, "512 tokens in 3.1 s", "READY plain output", "password [redacted]"]
        corpus.extend(scheme + " scheme_secret" for scheme in ("Bearer", "Basic", "Digest", "Token", "Negotiate"))
        for name in ("AWS_SECRET_ACCESS_KEY", "db.password", "PASS", "pwd", "credential", "auth", "api-key"):
            for value in ("literal_secret", '"quoted secret"', "'quoted secret'"):
                corpus.extend((name + "=" + value, '"' + name + '": ' + value))
        corpus.extend("--" + name + separator + "option_secret" for name in (
            "api-key", "secret", "token", "password", "passwd", "credential") for separator in ("=", " "))
        corpus.extend(name + " word_secret" for name in ("password", "passwd", "passphrase", "pwd"))
        corpus.extend(name + " env_secret" for name in ("HF_TOKEN", "DB_PASS", "db_pass", "secret_value", "AUTH"))
        corpus.extend(("https://user:url_secret@example.invalid/path", "mysql -u user -pglued_secret",
                       "mariadb-admin -pglued_secret", "sshpass -pglued_secret", "sshpass -p spaced_secret"))
        for line in corpus + ["\n".join(corpus)]:
            with self.subTest(line=line):
                self.assertEqual(redact(line), node.redact(line))

    def test_placement_text_needs_no_checkout(self):
        with mock.patch.object(portal, "repo_root", side_effect=AssertionError("checkout lookup")):
            self.assertEqual(portal._placement_text(TEXT).count("[redacted]"), 4)

    def test_saved_placement_profile_masks_both_log_paths_and_legacy_matches_base(self):
        source = subprocess.check_output(["git", "show", "2a7c803:hyperloom/web/api.py"],
                                         text=True, encoding="utf-8")
        namespace = dict(vars(portal))
        for name in ("_logs", "_process_logs"):
            definition = next(node for node in ast.walk(ast.parse(source))
                              if isinstance(node, ast.FunctionDef) and node.name == name)
            exec(compile(ast.Module(body=[definition], type_ignores=[]), "base:" + name, "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            for record in (None, {}, {"placement_supervision": None}, {"placement_supervision": {}}):
                with self.subTest(record=record):
                    api = portal.API(Path(directory) / "config.json", clock=lambda: 123)
                    state = {} if record is None else {"prefill_service": record}
                    api._cached, api._expires = {"logs": {}}, float("inf")
                    with mock.patch.object(api.orchestrate, "_read", return_value=state), \
                            mock.patch.object(api.orchestrate, "log_machine", return_value="local"), \
                            mock.patch.object(api.orchestrate, "logs", return_value={"stdout": TEXT, "stderr": TEXT}), \
                            mock.patch.object(api, "_action_log", return_value=[]):
                        statuses = {"arbitrary-profile": {"nodes": {"local": {}}, "router": True,
                                                           "router_machine": "local"}}
                        for name, argument in (("_logs", statuses), ("_process_logs", ("arbitrary-profile", "prefill", 100)),
                                               ("_process_logs", ("arbitrary-profile", "router", 100))):
                            actual = getattr(api, name)(argument)
                            if record is not None and record.get("placement_supervision") is not None:
                                self.assertNotIn("hf_probe_secret", json.dumps(actual))
                                self.assertNotIn("pass_probe_secret", json.dumps(actual))
                                self.assertNotIn("mysql_probe_secret", json.dumps(actual))
                                self.assertNotIn("ssh_probe_secret", json.dumps(actual))
                            else:
                                expected = namespace[name](api, argument)
                                self.assertEqual(json.dumps(actual).encode(), json.dumps(expected).encode())
                        # Exercise the query cache as well as the direct process path.
                        for _ in range(2):
                            result = api.read("logs", {"profile": "arbitrary-profile", "process": "prefill"})
                            self.assertEqual(result, portal.redact(api._process_logs(("arbitrary-profile", "prefill", 100))))
