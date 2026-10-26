"""Launch states belong to the configuration they were launched from: a portal or a command on another configuration
never lists or polls them (orchestrate.launch_states and its readers)."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom import orchestrate, secrets
from hyperloom.web.features import ledger, logsearch, machines, observe


class LaunchStatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-launch-states-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.run = self.directory / "run"
        self.run.mkdir()
        self.config_a = self.directory / "a" / "cluster.json"
        self.config_b = self.directory / "b" / "cluster.json"
        for path, text in ((self.config_a, '{"a": 1}'), (self.config_b, '{"b": 2}')):
            path.parent.mkdir()
            path.write_text(text, encoding="utf-8")
        patcher = mock.patch.object(secrets, "runtime_dir", lambda: self.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, profile, config=None, port=18600, **extra):
        state = {"profile": profile, "machines": {}, "known_hosts": str(self.directory / "known_hosts"),
                 "router": {"machine": "local", "directory": str(self.directory / ("router-" + profile)),
                            "endpoint": {"address": "127.0.0.1", "port": port}},
                 "rendered_router": {"replicas": [{"id": profile + "-r", "node": "node-" + profile,
                                                   "address": "127.0.0.1", "port": port + 1}]},
                 "launch": {"family": "qwen-replicas"}, **extra}
        if config is not None:
            state["config"] = orchestrate.config_record(config)
        (self.run / ("up-" + profile + ".json")).write_text(json.dumps(state), encoding="utf-8")
        return state

    def api(self, config):
        return SimpleNamespace(config_path=config, orchestrate=orchestrate)

    def test_config_record_is_the_resolved_path_and_the_bytes_at_launch(self):
        record = orchestrate.config_record(self.config_a.parent / "." / ".." / "a" / "cluster.json")
        self.assertEqual(record["path"], str(self.config_a.resolve()))
        self.assertEqual(len(record["sha256"]), 64)
        self.assertNotEqual(record["sha256"], orchestrate.config_record(self.config_b)["sha256"])
        self.assertIsNone(orchestrate.config_record(self.directory / "missing.json")["sha256"])

    def test_only_the_states_of_this_configuration(self):
        a = self.state("prof-a", self.config_a)
        b = self.state("prof-b", self.config_b)
        self.state("legacy")                                   # written before states recorded their configuration
        (self.run / "up-broken.json").write_text("{", encoding="utf-8")
        (self.run / "up-list.json").write_text("[]", encoding="utf-8")
        self.assertEqual(orchestrate.launch_states(self.config_a), {"prof-a": a})
        self.assertEqual(orchestrate.launch_states(self.config_b), {"prof-b": b})
        self.assertEqual(orchestrate.launch_states(self.directory / "c" / "cluster.json"), {})

    def test_the_same_file_by_another_spelling_and_after_an_edit(self):
        a = self.state("prof-a", self.config_a)
        spelled = self.config_a.parent / ".." / "a" / "cluster.json"
        self.assertEqual(orchestrate.launch_states(spelled), {"prof-a": a})
        if os.name == "nt":
            self.assertEqual(orchestrate.launch_states(str(self.config_a).upper()), {"prof-a": a})
        self.config_a.write_text('{"a": 3}', encoding="utf-8")   # an edit never hides the profiles it launched
        self.assertEqual(orchestrate.launch_states(self.config_a), {"prof-a": a})

    def test_a_record_without_a_path_is_not_shown(self):
        for index, record in enumerate(({}, {"sha256": "0" * 64}, {"path": 7}, "text", None)):
            state = dict(self.state(f"odd-{index}"), config=record)
            (self.run / f"up-odd-{index}.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(orchestrate.launch_states(self.config_a), {})

    def test_portal_readers_on_b_never_see_a(self):
        self.state("prof-a", self.config_a, port=18600)
        self.state("prof-b", self.config_b, port=18700)
        self.state("legacy", port=18800)
        api = self.api(self.config_b)
        rows, omitted = observe.launches(api)
        self.assertEqual([row["profile"] for row in rows], ["prof-b"])
        self.assertEqual(omitted["profiles"], 0)
        self.assertEqual(list(machines.launch_records(api)), ["prof-b"])
        self.assertEqual(list(machines.loopback_engines(machines.launch_records(api))), ["node-prof-b"])
        self.assertEqual([state["profile"] for state in ledger._states(api)], ["prof-b"])
        self.assertEqual([label for label, _ in logsearch.owned_directories(api)], ["prof-b router"])
        self.assertEqual([row["profile"] for row in observe.launches(self.api(self.config_a))[0]], ["prof-a"])

    def test_observe_counts_only_this_configurations_profiles_beyond_the_cap(self):
        for index in range(observe.MAX_PROFILES + 2):
            self.state(f"b-{index:02d}", self.config_b, port=18600 + 2 * index)
        for index in range(5):
            self.state(f"a-{index:02d}", self.config_a, port=19600 + 2 * index)
        rows, omitted = observe.launches(self.api(self.config_b))
        self.assertEqual(len(rows), observe.MAX_PROFILES)
        self.assertTrue(all(row["profile"].startswith("b-") for row in rows))
        self.assertEqual(omitted["profiles"], 2)

    def test_status_on_b_never_polls_the_profiles_of_a(self):
        self.state("prof-a", self.config_a, machines={"node-a": {"install_root": "/opt/a"}})
        self.state("prof-b", self.config_b, machines={"node-b": {"install_root": "/opt/b"}})
        self.state("legacy", machines={"node-legacy": {"install_root": "/opt/legacy"}})
        polled = []

        def call(client, verb, payload, timeout=60):
            polled.append(payload["root"])
            return {"leases": []}
        with mock.patch.object(orchestrate, "_client", lambda machine, known=None: machine), \
                mock.patch.object(orchestrate, "_call", call), \
                mock.patch.object(orchestrate, "_http_json", side_effect=OSError("no router here")):
            result = orchestrate.status(config_path=self.config_b)
            self.assertEqual(list(result), ["prof-b"])
            self.assertEqual(polled, ["/opt/b"])
            polled.clear()
            self.assertEqual(sorted(orchestrate.status()), ["legacy", "prof-a", "prof-b"])   # no configuration: all
            self.assertEqual(sorted(polled), ["/opt/a", "/opt/b", "/opt/legacy"])

    def test_status_by_name_on_b_never_polls_a_launch_of_a(self):
        self.state("prof-a", self.config_a, machines={"node-a": {"install_root": "/opt/a"}})
        self.state("legacy", machines={"node-legacy": {"install_root": "/opt/legacy"}})
        with mock.patch.object(orchestrate, "_call", side_effect=AssertionError("polled")), \
                mock.patch.object(orchestrate, "_http_json", side_effect=AssertionError("polled")):
            for name in ("prof-a", "legacy"):
                answer = orchestrate.status(name, config_path=self.config_b)
                self.assertEqual((answer["state"], answer["why"]), ("stopped", "launched from another configuration"))

    def test_cli_up_records_its_own_config_even_inside_another(self):
        from hyperloom import cli
        from hyperloom.recipes import service
        seen = []
        fake = SimpleNamespace(up=lambda *a, **kw: seen.append(service.configured_file()) or {"state": "running"})
        args = cli.build_parser().parse_args(["up", "prof-b", "--config", str(self.config_b)])
        cli.up_command(args, orchestrate=fake, load_cluster=lambda path: None)
        with service.configured(self.config_a):                 # an ambient configuration never wins over --config
            cli.up_command(args, orchestrate=fake, load_cluster=lambda path: None)
        self.assertEqual(seen, [self.config_b.absolute()] * 2)


if __name__ == "__main__":
    unittest.main()
