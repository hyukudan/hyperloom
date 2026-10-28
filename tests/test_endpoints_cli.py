"""`hyperloom endpoints ...`: the CLI calls the portal's functions, audits edits and redacts its output."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hyperloom import cli, config
from tests.test_endpoints_feature import KEY, MODELS, PUBLIC, FakeServer, json_route


class CLITests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.config = root / "cluster.json"
        machines = {"workstation": config.Machine("workstation", "windows", "local", "127.0.0.1", None, 22, "/opt", True, [])}
        cluster = config.Cluster(machines, {}, {}, {}, {}, {})
        for owner, name, value in ((cli, "config_path", lambda: self.config),
                                   (config, "load", lambda *a, **kw: cluster)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        from hyperloom import orchestrate
        patcher = mock.patch.object(orchestrate, "load_cluster", lambda *a, **kw: cluster)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["endpoints", *argv])
        return code, out.getvalue(), err.getvalue()

    def tearDown(self):
        from hyperloom import telemetry
        store = telemetry._stores.pop(str(Path(self.dir.name) / "telemetry.sqlite3"), None)
        if store is not None:
            store.close()

    def test_add_list_check_exposure_remove(self):
        server = FakeServer({"/v1/models": json_route({"object": "list", "data": [{"id": "fixture-model"}]})})
        self.addCleanup(server.close)
        code, out, _ = self.run_cli("add", "--label", "Box", "--host", "127.0.0.1", "--port", str(server.port))   # no --key-name: targets carry no key
        self.assertEqual((code, out.strip()), (0, f"saved box: http://127.0.0.1:{server.port}"))
        code, _, err = self.run_cli("add", "--label", "Public", "--host", PUBLIC, "--port", "80")
        self.assertEqual(code, 2)
        self.assertIn("private or loopback", err)
        with mock.patch("hyperloom.secrets.get", return_value=KEY) as lookup:
            code, out, _ = self.run_cli("check", "--json")
        self.assertEqual(code, 0)
        lookup.assert_not_called()                                     # no stored key is loaded for a target
        self.assertTrue(all("Authorization" not in headers for _, headers in server.seen))
        self.assertEqual(json.loads(out)["box"]["kind"], "generic")
        code, out, _ = self.run_cli("list")
        self.assertIn("box\thttp://127.0.0.1", out)
        self.assertIn("generic\tup", out)
        code, out, _ = self.run_cli("exposure")
        self.assertEqual(code, 0)
        self.assertIn("target/box", out)
        code, out, _ = self.run_cli("exposure", "--json")
        self.assertIn('"summary"', out)
        code, out, _ = self.run_cli("remove", "box")
        self.assertEqual((code, out.strip()), (0, "removed box"))
        code, _, err = self.run_cli("remove", "box")
        self.assertEqual(code, 2)
        audit = [json.loads(line) for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([(a["action"], a["result"]["state"]) for a in audit],
                         [("endpoints/save", "succeeded"), ("endpoints/save", "refused"),
                          ("endpoints/remove", "succeeded"), ("endpoints/remove", "refused")])

    def test_unavailable_exposure_fails_in_both_formats(self):
        from hyperloom.web.features import endpoints
        with mock.patch.object(endpoints, "_cluster", return_value=None):
            self.assertEqual(self.run_cli("exposure")[0], 1)
            self.assertEqual(self.run_cli("exposure", "--json")[0], 1)

    def test_exposure_exit_code_is_one_when_red(self):
        server = FakeServer({"/v1/models": MODELS})
        self.addCleanup(server.close)
        self.run_cli("add", "--label", "Open", "--host", "127.0.0.1", "--port", str(server.port))
        from hyperloom.web.features import endpoints
        # The loopback answer is red only with LAN listener evidence: give the control machine a fleet answer.
        with mock.patch.object(endpoints, "listener", return_value={"state": "any"}):
            code, out, _ = self.run_cli("exposure")
            self.assertEqual(code, 1)
            self.assertIn("RED", out)
            code, out, _ = self.run_cli("exposure", "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["summary"]["red"], 1)


if __name__ == "__main__":
    unittest.main()
