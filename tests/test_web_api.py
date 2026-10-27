"""API contract and transport checks with no real workers or GPU activity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyperloom import cli, config
from hyperloom.web.api import API, APIError, SCREENS, redact
from hyperloom.web.server import create_server


def fixture():
    machine = config.Machine("halo-1", "linux", "ssh", "192.0.2.10", "halo-1", 22,
                             "/opt/hyperloom/halo-1", True, [{"id": "gpu"}])
    profiles = {"qwen-replicas": {"nodes": ["halo-1"]}}
    return config.Cluster({machine.id: machine}, {"halo-1": {"model": {
        "path": "/opt/assets/model.gguf", "bytes": 64, "sha256": "a" * 64}}},
        profiles, {}, {}, {"schema": "hyperloom/cluster-v1", "profiles": profiles})


def wait_job(api, job):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        value = api.read("jobs/" + job["job"])
        if value["state"] != "running":
            return value
        time.sleep(.01)
    raise AssertionError("job did not finish")


class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-web-api-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cluster = fixture()
        self.now = 1000.
        self.orch = Mock()
        self.orch.load_cluster.return_value = self.cluster
        self.orch._known_hosts.return_value = self.directory / "known_hosts"
        self.orch._read.side_effect = FileNotFoundError
        self.orch.status.return_value = {"qwen-replicas": {"state": "stopped"}}
        self.orch._profile.return_value = ({}, "qwen-replicas", ["halo-1"])
        self.orch.up.return_value = {"state": "running"}
        self.orch.down.return_value = {"state": "stopped"}
        self.orch.install.return_value = "a" * 12
        self.orch.logs.return_value = {"stdout": "READY\napi_key=hidden", "stderr": ""}
        self.remote = Mock()
        self.calls = []

        self._inventory_times = []

        def node(verb, payload, timeout):
            self.calls.append(verb)
            self._inventory_times.append(self.now)
            checks = {"os": {"status": "ok", "value": "Linux"},
                      "disk": {"status": "ok", "value": {"free_bytes": 1234}}} if verb == "doctor" else {
                          "ram": {"status": "ok", "value": {"total_bytes": 4096}},
                          "amd": {"status": "ok", "value": []}}
            return {"schema": "hyperloom/node-v1", "verb": verb, "checks": checks}
        self.remote.Remote.return_value.call.side_effect = node
        # The inventory's doctor and gpu reads are separate pollers that the budget spaces 5 s apart; these tests
        # read both at once (the pacing itself: test_concurrent_tabs... and tests/test_pollbudget.py).
        spacing = patch("hyperloom.pollbudget.NODE_SPACING_S", 0.0)
        spacing.start()
        self.addCleanup(spacing.stop)
        self.api = API(self.directory / "cluster.json", orchestrator=self.orch,
                       remote_module=self.remote, clock=lambda: self.now,
                       audit_path=self.directory / "audit.jsonl")

    def test_every_screen_has_version_time_and_missing_source_reasons(self):
        for screen in SCREENS:
            with self.subTest(screen=screen):
                document = self.api.read(screen)
                self.assertEqual(document["schema"], "hyperloom/ui-" + screen + "-v1")
                self.assertEqual(document["server_time"], self.now)
                self.assertEqual(document["poll_interval_s"], 2)
                self.assertIsInstance(document["why"], dict)
                json.dumps(document, allow_nan=False)
        self.assertIsNone(self.api.read("stats")["step_breakdown"])
        self.assertIn("R16", self.api.read("stats")["why"]["step_breakdown"])
        self.assertIsNone(self.api.read("models")["models"][0]["presence"])
        self.assertIsNone(self.api.read("devices")["machines"][0]["cpu_model"])
        self.orch.up.assert_not_called()
        self.orch.install.assert_not_called()
        self.orch.down.assert_not_called()

    def test_concurrent_tabs_share_one_poll_across_every_screen(self):
        with ThreadPoolExecutor(max_workers=14) as pool:
            documents = list(pool.map(self.api.read, list(SCREENS) * 2))
        self.assertEqual(len(documents), 14)
        self.assertEqual(self.calls, ["doctor", "gpu"])
        self.assertEqual(self.orch.status.call_count, 1)
        self.assertEqual(self.orch.status.call_args.kwargs["config_path"], self.api.config_path)
        # The snapshot is rebuilt every 2 s, but node reads are paced: each verb once per 30 s cycle (one ssh each),
        # the launch status once per cycle.
        for step in range(14):
            self.now += 2
            self.api.read("devices")
        self.assertEqual(self.calls, ["doctor", "gpu"])
        self.assertEqual(self.orch.status.call_count, 1)
        with patch("hyperloom.pollbudget.NODE_SPACING_S", 5.0):           # the real spacing: never a burst
            self.now += 2
            self.api.read("devices")
            self.assertEqual(self.calls, ["doctor", "gpu", "doctor"])
            self.assertEqual(self.orch.status.call_count, 2)
            self.now += 2
            self.api.read("devices")
            self.assertEqual(self.calls, ["doctor", "gpu", "doctor"])     # gpu waits for the 5 s spacing
            self.now += 4
            self.api.read("devices")
            self.assertEqual(self.calls, ["doctor", "gpu", "doctor", "gpu"])

    def test_status_and_node_logs_skip_a_node_a_gpu_turn_holds(self):
        from hyperloom import pollbudget
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}
        pollbudget.for_api(self.api).note_turn("halo-1", "someone-bench", now=self.now)
        document = self.api.read("logs")
        skip = self.orch.status.call_args.kwargs["skip"]
        self.assertIn("paused", skip("halo-1"))
        self.assertIsNone(skip("halo-2"))
        self.orch.logs.assert_not_called()                                # no node log read during the turn
        self.assertIn("paused", document["why"]["qwen-replicas/halo-1"])
        pollbudget.for_api(self.api).note_turn("halo-1", None, now=self.now)
        self.now += 2
        self.api.read("logs")
        self.orch.logs.assert_called_once()                               # the GPU is free: read again
        self.now += 2
        self.api.read("logs")
        self.orch.logs.assert_called_once()                               # and then once per 30 s only

    def test_every_core_node_session_is_paced_by_the_budget(self):
        """Inventory, launch status and node logs together, snapshots every 2 s for five minutes (and a job finishing
        in between): every ssh session to the node is a budget grant, at least 5 s apart, at most 12 a minute."""
        from hyperloom import pollbudget
        sessions = []

        def status(config_path=None, skip=None):
            if skip("halo-1", "node") is None:
                sessions.append(("status", self.now))
            return {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}

        def logs(profile, process, tail, config_path=None):
            sessions.append(("logs", self.now))
            return {"stdout": "READY", "stderr": ""}
        self.orch.status.side_effect = status
        self.orch.logs.side_effect = logs
        start = self.now
        with patch.object(pollbudget, "NODE_SPACING_S", 5.0):
            for step in range(150):
                if step == 40:
                    self.api._status_stale = True                         # a job of this portal just finished
                self.api.read("logs")
                self.now += 2
        sessions += [("inventory", at) for at in self._inventory_times]
        times = sorted(at for _, at in sessions)
        self.assertTrue(all(b - a >= 5 for a, b in zip(times, times[1:])), times)
        for minute in range(5):
            self.assertLessEqual(sum(1 for at in times if start + 60 * minute <= at < start + 60 * (minute + 1)), 12)
        self.assertEqual({kind for kind, _ in sessions}, {"status", "logs", "inventory"})

    def test_a_router_on_a_held_node_is_neither_read_nor_its_log_fetched(self):
        from hyperloom import pollbudget
        self.orch.status.side_effect = lambda config_path=None, skip=None: {"qwen-replicas": {
            "nodes": {}, "router": {"error": skip("halo-1", "router")}, "router_machine": "halo-1"}}
        pollbudget.for_api(self.api).note_turn("halo-1", "someone-bench", now=self.now)
        document = self.api.read("logs")
        self.orch.logs.assert_not_called()
        self.assertIn("paused", document["why"]["qwen-replicas/router"])

    def test_the_logs_page_refresh_of_one_process_is_paced_and_paused(self):
        """The Logs page asks for one process's log every 2 s: a node's log (and a router hosted on a node) is read
        through the budget: nothing while a turn holds the node, then at most once per 30 s (the last answer
        between reads)."""
        from hyperloom import orchestrate, pollbudget
        fetched = []
        self.orch.logs.side_effect = lambda profile, process, tail, config_path=None: (
            fetched.append((process, self.now)) or {"stdout": f"line {len(fetched)}", "stderr": ""})
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}
        hosts = {"halo-1": "halo-1", "router": "halo-1", "prefill": "local"}
        with patch.object(orchestrate, "log_machine", lambda profile, process, read=None: hosts[process]), \
                patch.object(pollbudget, "NODE_SPACING_S", 5.0):
            pollbudget.for_api(self.api).note_turn("halo-1", "someone-bench", now=self.now)
            for step in range(30):                                        # one minute of refreshes in a turn
                for process in ("halo-1", "router"):
                    document = self.api.read("logs", {"profile": "qwen-replicas", "process": process, "tail": "5"})
                    self.assertIn("paused", document["why"]["entries"])
                self.now += 2
            self.assertEqual(fetched, [])
            pollbudget.for_api(self.api).note_turn("halo-1", None, now=self.now)
            for step in range(30):                                        # one minute with the GPU free
                self.api.read("logs", {"profile": "qwen-replicas", "process": "halo-1", "tail": "5"})
                self.now += 2
            reads = [at for process, at in fetched if process == "halo-1"]     # the page's and the snapshot's
            self.assertLessEqual(len(reads), 4)
            self.assertTrue(all(b - a >= 5 for a, b in zip(reads, reads[1:])))
            last = self.api.read("logs", {"profile": "qwen-replicas", "process": "halo-1", "tail": "5"})
            self.assertTrue(last["entries"], last)                        # between reads: the last answer
            self.api.read("logs", {"profile": "qwen-replicas", "process": "prefill", "tail": "5"})
            self.assertEqual(fetched[-1][0], "prefill")                   # a local file: no node, no budget

    def test_a_status_read_the_spacing_refused_is_tried_again(self):
        """Another poller (the turn check) takes the node just before each status read: the read is retried at the
        next snapshot instead of waiting a cycle, so the status keeps following the node."""
        from hyperloom import pollbudget
        budget = pollbudget.for_api(self.api)
        served = {"value": 0}
        reads = []

        def status(config_path=None, skip=None):
            if skip("halo-1", "node") is None:
                served["value"] += 1
                reads.append(self.now)
            return {"qwen-replicas": {"nodes": {"halo-1": {"leases": [], "generation": served["value"]}}}}
        self.orch.status.side_effect = status
        with patch.object(pollbudget, "NODE_SPACING_S", 5.0):
            for step in range(150):                                       # five minutes of 2 s snapshots
                if step % 15 == 0 and step:
                    budget.node("halo-1", "turns", now=self.now - 1)     # the turn check, 1 s before each cycle
                    budget.note_turn("halo-1", None, now=self.now - 1)
                self.api.read("profiles")
                self.now += 2
        self.assertGreaterEqual(len(reads), 9)                            # about once per 30 s, never stuck
        self.assertTrue(all(b - a <= 40 for a, b in zip(reads, reads[1:])), reads)
        status = self.api._status_cache[1]["qwen-replicas"]["nodes"]["halo-1"]
        self.assertEqual(status["generation"], served["value"])

    def test_the_inventory_pauses_while_a_gpu_turn_holds_the_node(self):
        from hyperloom import pollbudget
        self.api.read("devices")
        self.assertEqual(self.calls, ["doctor", "gpu"])
        pollbudget.for_api(self.api).note_turn("halo-1", "someone-bench", now=self.now + 30)
        self.now += 30
        row = self.api.read("devices")["machines"][0]
        self.assertEqual(self.calls, ["doctor", "gpu"])                  # no node call during the turn
        self.assertIn("paused: a GPU turn holds halo-1", row["why"]["inspection"])
        self.assertIsNotNone(row["gpu"])                                  # the last answer stays shown

    def test_a_machine_first_seen_in_a_gpu_turn_is_online_not_offline(self):
        from hyperloom import pollbudget
        pollbudget.for_api(self.api).note_turn("halo-1", "someone-bench", now=self.now)
        row = self.api.read("devices")["machines"][0]
        self.assertEqual(self.calls, [])                                  # no node read during the turn
        self.assertEqual(row["health"], "online")                         # the turn check answered: busy, not down
        self.assertIn("paused", row["why"]["inspection"])

    def test_offline_keeps_last_seen_without_fake_values(self):
        first = self.api.read("devices")["machines"][0]
        self.remote.Remote.return_value.call.side_effect = RuntimeError("machine offline")
        self.now += 30                                  # the next paced read
        row = self.api.read("devices")["machines"][0]
        self.assertEqual(row["health"], "offline")
        self.assertEqual(row["last_seen"], first["last_seen"])
        self.assertIsNone(row["accelerators"])
        self.assertEqual(row["why"]["accelerators"], "machine offline")

    def test_unpinned_machine_is_unpaired_and_trusted_alias_is_not_an_address(self):
        self.remote.Remote.return_value.call.side_effect = RuntimeError("unknown host key; confirm a fingerprint before connecting")
        self.assertEqual(self.api.read("devices")["machines"][0]["health"], "unpaired")
        (self.directory / "known_hosts").write_text("192.0.2.10,halo-1 ssh-ed25519 fixture\n", encoding="utf-8", newline="\n")
        self.remote._hostkey.return_value = SimpleNamespace(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:fixture")
        self.now += 2
        trusted = self.api.read("settings")["trusted_machines"][0]
        self.assertEqual(trusted["machine"], "halo-1")
        self.assertEqual(trusted["fingerprint"], "SHA256:fixture")

    def test_missing_config_is_a_document_with_null_and_why(self):
        self.orch.load_cluster.side_effect = ValueError("not configured")
        for screen in SCREENS:
            document = self.api.read(screen)
            self.assertTrue(document["why"])
            for field in document["why"]:
                self.assertIsNone(document[field])

    def test_failed_status_is_unavailable_never_stopped_or_zero(self):
        self.orch.status.side_effect = RuntimeError("status unavailable")
        profiles = self.api.read("profiles")["profiles"]
        self.assertIsNone(profiles[0]["state"])
        self.assertEqual(profiles[0]["why"]["state"], "status unavailable")
        self.assertIsNone(self.api.read("stats")["engines"])

    def test_inventory_preserves_passive_measurements_and_lease_deadline(self):
        def node(verb, payload, timeout):
            checks = {"cpu_model": {"status": "ok", "value": "Fixture CPU"},
                      "usb4": {"module": {"status": "ok", "value": True}}} if verb == "doctor" else {
                "amd": {"status": "ok", "value": [{"id": "card0", "vram_total_bytes": {"status": "ok", "value": 100},
                    "vram_used_bytes": {"status": "ok", "value": 40}, "busy_percent": {"status": "ok", "value": 75}}]}}
            return {"schema": "hyperloom/node-v1", "checks": checks}
        self.remote.Remote.return_value.call.side_effect = node
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": [
            {"lease": "fixture", "deadline": 1100., "state": "ready", "processes": [
                {"name": "replica", "ready": True, "exe_sha256": "b" * 64}]}]}}}}
        with patch.object(self.api, "_probe", return_value={"devices": [{"vendor": "amd", "name": "Presence only"}]}):
            machine = self.api.read("devices")["machines"][0]
        self.assertEqual(machine["cpu"], "Fixture CPU")
        self.assertEqual(len(machine["devices"]), 1)
        self.assertEqual(machine["devices"][0]["memory_free_bytes"], 60)
        self.assertEqual(machine["devices"][0]["utilization_pct"], 75)
        self.assertEqual(machine["lease"]["remaining_s"], 100)
        process = self.api.read("profiles")["profiles"][0]["processes"][0]
        self.assertEqual(process["state"], "ready")
        self.assertEqual(process["binary_sha256"], "b" * 64)

    def test_default_logs_merge_captured_streams_and_portal_failures(self):
        self.orch.status.return_value = {"qwen-replicas": {"nodes": {"halo-1": {"leases": []}}}}
        self.api._audit("up", {"profile": "qwen-replicas"}, {"state": "failed", "error": "fixture failure"})
        document = self.api.read("logs")
        self.assertTrue(any(row.get("event") == "READY" for row in document["entries"]))
        self.assertEqual(document["failures"][0]["reason"], "fixture failure")
        self.assertNotIn("hidden", json.dumps(document))

    def test_local_doctor_on_first_run_does_not_require_configuration(self):
        self.orch.load_cluster.side_effect = FileNotFoundError
        with patch.object(self.api, "_node", return_value={"schema": "hyperloom/node-v1"}) as node:
            self.assertEqual(wait_job(self.api, self.api.mutate("doctor", {}))["state"], "succeeded")
            self.assertEqual(node.call_args.args[0].access, "local")
            self.orch.load_cluster.assert_not_called()

    def test_up_uses_cli_arguments_and_audits_without_secrets(self):
        arguments = {"profile": "qwen-replicas", "router": "halo-1", "node": ["halo-1"],
                     "device": ["halo-1:gpu"], "deadline": 1140}
        result = wait_job(self.api, self.api.mutate("up", arguments))
        self.assertEqual(result["state"], "succeeded")
        self.orch.up.assert_called_once()
        kwargs = dict(self.orch.up.call_args.kwargs)
        cancelled = kwargs.pop("cancelled")                         # the job's hand-over check (its cancel marker)
        self.assertEqual((self.orch.up.call_args.args, kwargs), ((self.cluster, "qwen-replicas"), dict(
            router="halo-1", nodes=["halo-1"], devices=["halo-1:gpu"], deadline_s=1140, approval=None)))
        self.assertFalse(cancelled())
        entries = [json.loads(line) for line in self.api.audit_path.read_text().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["action"], "up")
        self.assertEqual(entries[-1]["result"]["state"], "succeeded")

    def test_up_runs_on_the_portals_configuration_and_lists_only_its_launches(self):
        from hyperloom.recipes import service
        seen = []
        self.orch.up.side_effect = lambda *a, **kw: seen.append(service.configured_file()) or {"state": "running"}
        self.assertEqual(wait_job(self.api, self.api.mutate("up", {"profile": "qwen-replicas"}))["state"], "succeeded")
        self.api._execute_action("up", {"profile": "qwen-replicas"})      # in process, without a job
        self.assertEqual(seen, [Path(self.api.config_path).absolute()] * 2)
        self.api.read("profiles")
        self.assertEqual(self.orch.status.call_args.kwargs["config_path"], self.api.config_path)
        self.assertEqual(set(self.orch.status.call_args.kwargs), {"config_path", "skip"})

    def test_profiles_show_only_this_configurations_launch_of_a_shared_name(self):
        from hyperloom import orchestrate
        owned = {"profile": "qwen-replicas", "router": {"machine": "halo-1"}, "release_id": "r" * 12,
                 "process_specs": [{"machine_id": "halo-1", "role": "replica", "argv": ["/opt/foreign-engine"],
                                    "env": {"FOREIGN_SETTING": "1"}}],
                 "rendered_router": {"replicas": [{"id": "foreign-replica", "address": "127.0.0.1", "port": 18999}]}}
        status = {"qwen-replicas": {"nodes": {}}}
        self.orch._read.side_effect = lambda path: dict(owned, config=orchestrate.config_record(
            self.directory / "other" / "cluster.json"))
        row = self.api._profiles(self.cluster, status)[0]
        self.assertIsNone(row["launch"])
        self.assertEqual(row["why"]["launch"], "no owned launch")
        self.assertNotIn("foreign", json.dumps(row).lower())              # nothing of the other launch shows
        # A launch written before states recorded their configuration is shown, marked unknown (and stoppable).
        self.orch._read.side_effect = lambda path: dict(owned)
        row = self.api._profiles(self.cluster, status)[0]
        self.assertEqual((row["router"], row["config_identity"]), ("halo-1", "unknown"))
        self.assertIn("unknown configuration", row["why"]["configuration_identity"])
        self.orch._read.side_effect = lambda path: dict(owned, config=orchestrate.config_record(self.api.config_path))
        row = self.api._profiles(self.cluster, status)[0]
        self.assertEqual(row["router"], "halo-1")
        self.assertEqual(row["release"], "r" * 12)

    def test_core_node_calls_never_wait_behind_a_collector(self):
        """A collector's hung ssh call holds the machine's node gate: the core's doctor and gpu reads still answer
        at once (no TimeoutError after the gate's wait), while a feature call keeps waiting for the gate."""
        from hyperloom import nodegate
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def collector():
            with nodegate.hold(self.api.config_path, "halo-1", wait=1.0):
                held.set()
                release.wait(10)
        thread = threading.Thread(target=collector, daemon=True)
        thread.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        document = self.api.read("devices")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("doctor", self.calls)
        self.assertIn("gpu", self.calls)
        row = next(m for m in document["machines"] if m["id"] == "halo-1")
        self.assertNotIn("busy", json.dumps(row["why"]))
        with patch.object(nodegate, "hold", side_effect=TimeoutError("node halo-1 is busy with another call")):
            with self.assertRaises(TimeoutError):
                self.api.node("halo-1", "host", {})            # a feature call is gated (and skipped when busy)
        release.set()
        thread.join(5)

    def test_a_launch_without_a_configuration_record_can_be_stopped_here(self):
        from hyperloom import orchestrate
        legacy = {"profile": "qwen-replicas", "router": {"machine": "halo-1"}, "machines": {}}
        self.orch._read.side_effect = lambda path: dict(legacy)
        self.assertEqual(orchestrate.ownership(legacy, self.api.config_path), "unknown")
        job = wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))
        self.assertEqual(job["state"], "succeeded")
        self.orch.down.assert_called_once_with("qwen-replicas", force=False)

    def test_install_down_doctor_rotate_and_host_pin_endpoints(self):
        for action, arguments in (("install", {"profile": "qwen-replicas"}),
                                  ("down", {"profile": "qwen-replicas", "force": True}),
                                  ("doctor", {"machine": "halo-1"})):
            with self.subTest(action=action):
                self.assertEqual(wait_job(self.api, self.api.mutate(action, arguments))["state"], "succeeded")
        self.orch.install.assert_called_once_with(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        self.orch.down.assert_called_once_with("qwen-replicas", force=True)
        with patch("hyperloom.web.api.secrets.rotate", return_value="do-not-expose") as rotate:
            job = wait_job(self.api, self.api.mutate("keys/rotate", {"name": "engine"}))
            self.assertEqual(job["state"], "succeeded")
            rotate.assert_called_once_with("engine")
            self.assertNotIn("do-not-expose", self.api.audit_path.read_text())
        self.remote.keyscan.return_value = [SimpleNamespace(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:fixture")]
        job = wait_job(self.api, self.api.mutate("hosts/pin", {"machine": "halo-1", "fingerprint": "SHA256:fixture"}))
        self.assertEqual(job["state"], "succeeded")
        self.remote.KnownHosts.return_value.pin.assert_called_once()

    def test_host_pin_never_pins_a_different_fingerprint(self):
        self.remote.keyscan.return_value = []
        job = wait_job(self.api, self.api.mutate("hosts/pin", {"machine": "halo-1", "fingerprint": "SHA256:fixture"}))
        self.assertEqual(job["state"], "failed")
        self.remote.KnownHosts.return_value.pin.assert_not_called()

    def test_a_stop_goes_ahead_when_the_admission_database_is_unavailable(self):
        import sqlite3
        broken = Mock()
        broken.acquire.side_effect = sqlite3.DatabaseError("file is not a database")
        self.api.admission_db = broken
        self.api._resources_of_profile = lambda profile, router=None: {"halo-1", "router"}
        bench = Mock()
        with self.api._job_lock:                  # a benchmark of this process that holds an overlapping machine
            self.api._features_running["bench-1"] = bench
            self.api._hold_resources["bench-1"] = {"halo-1"}
            self.api._features_running["bench-2"] = Mock()
            self.api._hold_resources["bench-2"] = {"halo-9"}
        self.addCleanup(lambda: [self.api._features_running.pop(j, None) for j in ("bench-1", "bench-2")])
        job = wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))
        self.assertEqual(job["state"], "succeeded")
        self.assertIn("admission unavailable", job["warning"])
        self.orch.down.assert_called_once_with("qwen-replicas", force=False)
        bench.cancel.assert_called_once()
        self.api._features_running["bench-2"].cancel.assert_not_called()
        audit = [json.loads(line) for line in self.api.audit_path.read_text().splitlines()]
        self.assertIn("admission unavailable", audit[-1]["result"]["warning"])
        with self.assertRaises(APIError) as caught:            # a deploy is still refused without it
            self.api.mutate("up", {"profile": "qwen-replicas"})
        self.assertEqual(caught.exception.status, 503)
        self.orch.up.assert_not_called()

    def test_failure_audited_and_next_job_allowed(self):
        self.orch.up.side_effect = RuntimeError("api_key=hidden failed")
        job = wait_job(self.api, self.api.mutate("up", {"profile": "qwen-replicas"}))
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("hidden", json.dumps(job))
        self.assertNotIn("hidden", self.api.audit_path.read_text())
        self.assertEqual(wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))["state"], "succeeded")

    def test_running_job_refuses_mutation_but_stop_and_cancel_run(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def up(*args, **kwargs):
            started.set()
            release.wait(3)
            return {"state": "stopped"}
        self.orch.up.side_effect = up
        job = self.api.mutate("up", {"profile": "qwen-replicas"})
        self.assertTrue(started.wait(1))
        with self.assertRaises(APIError) as refused:
            self.api.mutate("install", {"profile": "qwen-replicas"})
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(wait_job(self.api, self.api.mutate("down", {"profile": "qwen-replicas"}))["state"], "succeeded")
        self.assertEqual(wait_job(self.api, self.api.mutate("cancel", {"job": job["job"]}))["state"], "succeeded")
        release.set()
        wait_job(self.api, job)

    def plain_up(self, up, stop):
        """Run a plain up job whose orchestrate.up is ``up(cancelled, cue)``; ``stop()`` runs when up sets ``cue``.
        Returns (the stop's finished job, the up's finished job)."""
        cue, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        self.orch.up.side_effect = lambda *a, **kw: up(kw["cancelled"], cue, resume)
        job = self.api.mutate("up", {"profile": "qwen-replicas"})
        self.assertTrue(cue.wait(3))
        stopped = wait_job(self.api, stop(job))
        resume.set()
        return stopped, wait_job(self.api, job)

    def test_cancel_of_a_plain_up_before_its_hand_over_starts_nothing_and_stops_nothing(self):
        # Cancelled while up prepares (before it owns the profile): only the job's marker. The cancel never answers
        # "stopped" while up goes on: up meets the marker at the hand-over and starts nothing.
        def up(cancelled, cue, resume):
            cue.set()
            resume.wait(3)
            if cancelled():
                raise RuntimeError("profile stop requested")
            return {"state": "running"}
        cancel, finished = self.plain_up(up, lambda job: self.api.mutate("cancel", {"job": job["job"]}))
        self.assertEqual(cancel["state"], "succeeded", cancel["error"])
        self.assertIn(cancel["result"]["phase"], ("not started", "checks"))
        self.assertIsNone(cancel["result"]["down"])
        self.assertIn("nothing was started", cancel["result"]["summary"])
        self.assertEqual((finished["state"], finished["error"]), ("failed", "profile stop requested"))
        self.orch.down.assert_not_called()

    def test_cancel_of_a_plain_up_never_stops_a_profile_that_was_already_running(self):
        # The profile ran before this job: up refuses it (before the hand-over). A cancel meanwhile stops nothing.
        def up(cancelled, cue, resume):
            cue.set()
            resume.wait(3)
            raise RuntimeError("profile has saved running state; run down before up")
        cancel, finished = self.plain_up(up, lambda job: self.api.mutate("cancel", {"job": job["job"]}))
        self.assertEqual(cancel["state"], "succeeded", cancel["error"])
        self.assertIsNone(cancel["result"]["down"])
        self.assertEqual(finished["state"], "failed")
        self.orch.down.assert_not_called()

    def test_cancel_of_a_plain_up_after_its_hand_over_stops_the_profile(self):
        def up(cancelled, cue, resume):
            self.assertFalse(cancelled())                           # up owns the profile: the launch goes on
            cue.set()
            resume.wait(3)
            return {"state": "running"}
        cancel, finished = self.plain_up(up, lambda job: self.api.mutate("cancel", {"job": job["job"]}))
        self.assertEqual(cancel["state"], "succeeded", cancel["error"])
        self.assertEqual((cancel["result"]["phase"], cancel["result"]["down"]), ("launching", {"state": "stopped"}))
        self.orch.down.assert_called_once_with("qwen-replicas")
        self.assertEqual(finished["state"], "succeeded", finished["error"])

    def test_a_stop_of_its_profile_cancels_a_preparing_plain_up(self):
        # `down P` while a plain up prepares P: up would clear P's stop marker when it takes P and start; the job's
        # marker is written too, so it starts nothing. A stop of another profile leaves it alone.
        for profile, stops_it in (("qwen-replicas", True), ("other", False)):
            with self.subTest(profile=profile):
                self.orch.down.reset_mock()

                def up(cancelled, cue, resume):
                    cue.set()
                    resume.wait(3)
                    if cancelled():
                        raise RuntimeError("profile stop requested")
                    return {"state": "running"}
                stopped, finished = self.plain_up(up, lambda job: self.api.mutate("down", {"profile": profile}))
                self.assertEqual(stopped["state"], "succeeded", stopped["error"])
                self.orch.down.assert_called_once_with(profile, force=False)
                self.assertEqual(finished["state"], "failed" if stops_it else "succeeded")
                self.assertEqual("cancelled_switches" in stopped["result"], stops_it)

    def test_every_mutation_runs_on_the_portals_configuration(self):
        # A configuration file named by the client is refused on every mutation that took one (nothing runs); the
        # portal's own path is accepted and is what runs.
        bodies = {"up": {"profile": "qwen-replicas"}, "install": {"profile": "qwen-replicas"},
                  "doctor": {"machine": "halo-1"}, "hosts/pin": {"machine": "halo-1", "fingerprint": "SHA256:fixture"}}
        for action, body in bodies.items():
            for other in ("//pc/share/cluster.json", str(self.directory / "other" / "cluster.json"), "cluster.json"):
                with self.subTest(action=action, config=other):
                    with self.assertRaisesRegex(APIError, "portal's configuration: drop config"):
                        self.api.mutate(action, dict(body, config=other))
        self.orch.load_cluster.assert_not_called()
        self.orch.up.assert_not_called()
        self.orch.install.assert_not_called()
        self.remote.keyscan.assert_not_called()
        self.remote.keyscan.return_value = [SimpleNamespace(algorithm="ssh-ed25519", sha256_fingerprint="SHA256:fixture")]
        for action, body in bodies.items():
            with self.subTest(action=action, config="the portal's"):
                job = wait_job(self.api, self.api.mutate(action, dict(body, config=str(self.directory / "cluster.json"))))
                self.assertEqual(job["state"], "succeeded", job["error"])
                self.assertNotIn("config", job["arguments"])
        self.assertEqual({c.args[0] for c in self.orch.load_cluster.call_args_list}, {self.directory / "cluster.json"})
        # The detached CLI is always given the portal's configuration.
        for action in ("up", "install", "down"):         # a detached down holds this configuration's admission too
            argv = self.api._cli_argv(action, {"profile": "qwen-replicas"})
            self.assertEqual(argv[argv.index("--config") + 1], str(self.directory / "cluster.json"))

    def test_detached_plain_up_carries_its_job(self):
        argv = self.api._cli_argv("up", {"profile": "qwen-replicas", "_job": "ab12cd34"})
        self.assertEqual(argv[argv.index("--job") + 1], "ab12cd34")
        self.assertNotIn("--job", self.api._cli_argv("up", {"profile": "qwen-replicas"}))

    def test_logs_query_is_read_only_cached_and_redacted(self):
        query = {"profile": "qwen-replicas", "process": "halo-1", "tail": "5"}
        first = self.api.read("logs", query)
        self.assertEqual(first, self.api.read("logs", query))
        self.orch.logs.assert_called_once_with("qwen-replicas", "halo-1", 5, config_path=self.api.config_path)
        self.assertNotIn("hidden", json.dumps(first))

    def test_unknown_arguments_and_bad_types_refused(self):
        for action, args in (("up", {}), ("up", {"profile": "qwen-replicas", "env": {}}),
                             ("up", {"profile": "qwen-replicas", "deadline": True}),
                             ("up", {"profile": "qwen-replicas", "node": "halo-1"}),
                             ("down", {"profile": "qwen-replicas", "force": "yes"}),
                             ("hosts/pin", {"machine": "halo-1"}), ("cancel", {})):
            with self.subTest(action=action, arguments=args), self.assertRaises(APIError):
                self.api.mutate(action, args)

    def test_engine_metrics_quantiles_counter_resets_and_route_blocker(self):
        raw = {"schema": "tensormeld/replica-router-status-v1", "status": "ok", "replicas": [
            {"id": "replica", "endpoint": "http://127.0.0.1:18501"}]}
        self.orch.status.return_value = {"qwen-replicas": {"router": raw}}
        count = [100]
        def fetch(endpoint, suffix, text=False):
            if text:
                return f"strix_generated_tokens_total {count[0]}\nstrix_queue_waiting 2\nstrix_queue_running 1\n"
            return {"schema": "strix-serve-status-v1", "requests": [
                {"id": "request", "time": 1000, "queue_ms": 10., "prefill_ms": 20., "route": "fake"}]}
        with patch("hyperloom.web.api._http", side_effect=fetch):
            first = self.api.read("stats")
            self.assertIsNone(first["engines"][0]["throughput"]["value"])
            self.assertEqual(first["engines"][0]["ttft_p50"]["value"], 30)
            self.assertIsNone(first["recent_requests"][0]["route"])
            count[0] = 200
            self.now += 30                              # engine reads follow the paced launch status
            second = self.api.read("stats")
            self.assertAlmostEqual(second["engines"][0]["throughput"]["value"], 100 / 30)
            self.assertEqual(second["engines"][0]["ttft_p50"]["n"], 1)
            count[0] = 1
            self.now += 30
            reset = self.api.read("stats")["engines"][0]["throughput"]
            self.assertIsNone(reset["value"])
            self.assertTrue(reset["reset"])

    def test_profile_rows_expose_the_engine_family(self):
        cluster = deepcopy(self.cluster)
        cluster.profiles["w5"] = {"preset": "qwen-replicas-w5", "nodes": ["halo-1"]}
        cluster.profiles["mm"] = {"preset": "minimax-pair", "rank0": "halo-1", "rank1": "halo-1"}
        self.orch.load_cluster.return_value = cluster
        rows = {row["id"]: row for row in self.api.read("profiles")["profiles"]}
        self.assertEqual({name: (row["preset"], row["family"]) for name, row in rows.items()},
                         {"qwen-replicas": ("qwen-replicas", "qwen-replicas"),
                          "w5": ("qwen-replicas-w5", "qwen-replicas"), "mm": ("minimax-pair", "minimax-pair")})
        script = (Path(__file__).resolve().parents[1] / "hyperloom" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        # The UI gates on the family the API reports, never on a preset or profile name.
        self.assertIn('p.family !== "qwen-replicas"', script)
        self.assertIn('p.family === "qwen-replicas"', script)
        self.assertNotIn('profileId(p)) !== "qwen-replicas"', script)
        self.assertNotIn('p.profile) === "qwen-replicas"', script)

    def test_loopback_engines_are_not_polled_from_the_control_machine(self):
        raw = {"schema": "tensormeld/replica-router-status-v1", "status": "ok", "replicas": [
            {"id": "rank0", "endpoint": "http://127.0.0.1:18441"}]}
        cluster = deepcopy(self.cluster)
        cluster.profiles["mm"] = {"preset": "minimax-pair", "rank0": "halo-1", "rank1": "halo-1"}
        self.orch.load_cluster.return_value = cluster
        self.orch.status.return_value = {"mm": {"router": raw}, "mm-launch": {"router": raw, "launch": {"family": "minimax-pair"}},
                                         "qwen-replicas": {"router": raw}}
        fetched = []
        def fetch(endpoint, suffix, text=False):
            fetched.append(suffix)
            raise OSError("connection refused")
        with patch("hyperloom.web.api._http", side_effect=fetch):
            stats = self.api.read("stats")
        rows = {row["profile"]: row for row in stats["engines"]}
        for name in ("mm", "mm-launch"):
            self.assertIsNone(rows[name]["status"])
            self.assertEqual(rows[name]["why"]["status"], "unavailable: engine bound to loopback on its node")
            self.assertEqual(rows[name]["why"]["metrics"], "unavailable: engine bound to loopback on its node")
        # Only the LAN-bound qwen engine is probed.
        self.assertEqual(fetched, ["/metrics"])
        self.assertEqual(rows["qwen-replicas"]["why"]["status"], "connection refused")

    def test_a_key_file_release_binds_the_lan(self):
        # The launched release decides the bind, not the family row (a key-file MiniMax release serves the LAN).
        from hyperloom import engines
        from hyperloom.web.api import _binds_loopback
        real = engines.load_preset
        keyed = dict(real("minimax-pair"), serve_auth="key-file",
                     argv=real("minimax-pair")["argv"] + ["--api-key-file", "{key_file}"])
        cluster = deepcopy(self.cluster)
        cluster.profiles["mm"] = {"preset": "mm-keyed", "rank0": "halo-1", "rank1": "halo-1"}
        with patch.object(engines, "load_preset", side_effect=lambda n: keyed if n == "mm-keyed" else real(n)):
            self.assertFalse(_binds_loopback("mm", {"launch": {"family": "minimax-pair", "preset": "mm-keyed"}}, None))
            self.assertFalse(_binds_loopback("mm", {}, cluster))
            self.assertTrue(_binds_loopback("mm", {"launch": {"family": "minimax-pair", "preset": "minimax-pair"}},
                                            None))

    def test_redaction_keeps_measurements_and_key_file_paths(self):
        value = redact({"api_key": "hidden", "generated_tokens": 123, "api_key_file": "/opt/key", "argv": ["--api-key", "hidden"]})
        self.assertEqual(value["generated_tokens"], 123)
        self.assertEqual(value["api_key_file"], "/opt/key")
        self.assertNotIn("hidden", json.dumps(value))
        self.assertIsInstance(self.api.read("settings")["secrets"], dict)
        self.assertEqual(redact({"secrets": {"engine": "hidden"}}), {"secrets": "[redacted]"})


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom-web-http-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "index.html").write_text('<html><head></head><body></body></html>', encoding="utf-8", newline="\n")
        self.api = Mock()
        self.api.read.return_value = {"why": {}}
        self.api.mutate.return_value = {"job": "fixture", "state": "running"}
        self.server = create_server(0, api=self.api, static_dir=directory)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(2)))
        self.headers = {"Origin": self.server.origin, "Content-Type": "application/json", "X-Hyperloom-CSRF": self.server.csrf_token}

    def request(self, method, path="/hyperloom/api/up", body=b'{}', headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=self.headers if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_server_loopback_static_injection_and_no_cors(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, data = self.request("GET", "/", body=None)
        self.assertEqual(status, 200)
        self.assertIn(self.server.csrf_token.encode(), data)
        self.assertIn('name="hyperloom-csrf"', data.decode())
        self.assertFalse(any(name.lower().startswith("access-control-") for name in headers))
        for path in ("/../api.py", "/%2e%2e/api.py", "/server.py", "/static/", "/index.html/.."):
            self.assertEqual(self.request("GET", path, body=None)[0], 404)

    def test_security_negatives_never_reach_backend(self):
        for headers, expected in (({**self.headers, "Origin": "http://192.0.2.10"}, 403),
                                  ({**self.headers, "Host": "192.0.2.10"}, 403),
                                  ({**self.headers, "X-Hyperloom-CSRF": "wrong"}, 403),
                                  ({key: value for key, value in self.headers.items() if key != "X-Hyperloom-CSRF"}, 403),
                                  ({**self.headers, "Content-Type": "text/plain"}, 415)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", headers=headers)[0], expected)
        self.assertEqual(self.request("POST", body=b"x" * 65537)[0], 413)
        for body in (b"{", b"[]", b'{"x":NaN}', b'{"x":1,"x":2}'):
            self.assertEqual(self.request("POST", body=body)[0], 400)
        self.assertEqual(self.request("GET", body=None)[0], 405)
        self.api.mutate.assert_not_called()

    def test_every_endpoint_dispatches_and_mutation_returns_job(self):
        for screen in SCREENS:
            self.assertEqual(self.request("GET", "/hyperloom/api/" + screen, body=None)[0], 200)
            self.api.read.assert_called_with(screen, {})
        for action in ("up", "down", "install", "doctor", "hosts/pin", "keys/rotate", "cancel"):
            self.assertEqual(self.request("POST", "/hyperloom/api/" + action)[0], 202)
            self.api.mutate.assert_called_with(action, {})

    def test_cli_ui_only_calls_server(self):
        with patch("hyperloom.web.server.serve") as serve, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["ui", "--port", "18701", "--no-browser"]), 0)
            serve.assert_called_once_with(18701, no_browser=True)


if __name__ == "__main__":
    unittest.main()


class DetachedCommandLineTests(unittest.TestCase):
    def test_up_and_down_argv(self):
        from hyperloom.web.api import API
        api = API(config_path="C:/cfg/cluster.json", orchestrator=object(), remote_module=object(),
                  audit_path=tempfile.mkdtemp() + "/audit.jsonl")
        up = api._cli_argv("up", {"profile": "qwen-replicas", "node": ["halo-1"], "router": "halo-1", "deadline": 600})
        self.assertEqual(up[1:5], ["-m", "hyperloom", "up", "qwen-replicas"])
        self.assertIn("--node", up)
        self.assertEqual(up[up.index("--router") + 1], "halo-1")
        self.assertEqual(up[up.index("--deadline") + 1], "600")
        down = api._cli_argv("down", {"profile": "qwen-replicas", "force": True})
        self.assertEqual(down[3:], ["down", "qwen-replicas", "--config", str(Path("C:/cfg/cluster.json")), "--force"])
        self.assertFalse(api.detached)
