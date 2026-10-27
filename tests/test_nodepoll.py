"""The node poll: one combined read-only session per node and cycle, nothing without an open page but the turn
check, the turn check with or without the history store, and the node tool's ``poll`` verb."""

import contextlib
import copy
import dataclasses
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom import nodepoll, pollbudget
from hyperloom.web.features import machines, turns

from tests.test_fleet_support import Clock, FakeAPI, host, lab


NODE = Path(__file__).resolve().parents[1] / "node"


def node_tool():
    loader = importlib.machinery.SourceFileLoader("hyperloom_node_poll_test", str(NODE / "hyperloom-node"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def turn_reply(label=None):
    holder = {"parsed": True, "alive": True, "label": label, "kind": "single"} if label else None
    return {"schema": "hyperloom/node-v1", "verb": "turns", "holder": holder, "now": 0}


class Node:
    """A fake node: answers every part of a poll; records each session (one per call)."""

    def __init__(self):
        self.sessions, self.holder = [], {}

    def __call__(self, machine, payload):
        self.sessions.append((machine.id, [part["key"] for part in payload["parts"]]))
        parts = {}
        for part in payload["parts"]:
            if part["verb"] == "turns":
                parts[part["key"]] = turn_reply(self.holder.get(machine.id))
            else:
                parts[part["key"]] = {"schema": "hyperloom/node-v1", "verb": part["verb"], "checks": {},
                                      "payload": part["payload"]}
        return {"schema": "hyperloom/node-v1", "verb": "poll", "parts": parts}


class PollTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cluster = lab()
        cluster.machines["halo-1"] = dataclasses.replace(cluster.machines["halo-1"], lab_dir="/opt/lab")
        self.clock = Clock(1000.0)
        self.api = FakeAPI(directory.name, cluster, self.clock)
        self.addCleanup(self.api.telemetry.close)
        self.node = Node()
        self.poll = nodepoll.NodePoll(self.api, clock=self.clock, call=self.node)
        self.api.node_poll = self.poll

    def run_for(self, seconds, page=False, want=()):
        """Tick once a second (each poll finishes before the next tick); readers ask every 2 s."""
        for _ in range(seconds):
            if page and int(self.clock.now) % 2 == 0:
                self.poll.page_seen()
            for mid, key, verb in want:
                self.poll.read(mid, key, verb, {})
            for worker in self.poll.tick():
                worker.join(5)
            self.clock.now += 1

    def per_minute(self, mid):
        return [keys for machine_id, keys in self.node.sessions if machine_id == mid]

    def test_with_a_page_open_every_reader_shares_one_session_per_node_and_cycle(self):
        """Fleet, USB4, the tool check, the inventory's doctor and gpu, a launch status and a log: one session per
        node per 30 s (two a minute), the lab node's turn check inside it."""
        want = [(mid, key, verb) for mid in ("halo-1", "halo-2")
                for key, verb in (("host", "host"), ("usb4", "usb4"), ("version", "version"), ("doctor", "doctor"),
                                  ("gpu", "gpu"), ("status:x", "status"), ("logs:x", "logs"))]
        self.run_for(600, page=True, want=want)
        for mid in ("halo-1", "halo-2"):
            sessions = self.per_minute(mid)
            self.assertLessEqual(len(sessions), 20)                   # ten minutes: at most two a minute
            self.assertGreaterEqual(len(sessions), 19)
            full = [keys for keys in sessions if "host" in keys]
            self.assertGreaterEqual(len(full), 18)
            self.assertEqual(set(full[-1]) - {"turns"}, {"host", "usb4", "version", "doctor", "gpu", "status:x",
                                                          "logs:x"})
        self.assertTrue(all("turns" in keys for keys in self.per_minute("halo-1")))
        self.assertFalse(any("turns" in keys for keys in self.per_minute("halo-2")))     # no lab directory
        starts = [at for mid, at, _ in self.poll.sessions if mid == "halo-1"]
        self.assertTrue(all(b - a >= nodepoll.CYCLE_S for a, b in zip(starts, starts[1:])))
        answer, error, _ = self.poll.read("halo-2", "host", "host", {})
        self.assertEqual((answer["verb"], error), ("host", None))

    def test_without_a_page_only_the_turn_check_runs(self):
        want = [("halo-1", "host", "host"), ("halo-2", "host", "host")]
        self.run_for(300, page=False, want=want)        # the collectors still ask; nobody looks
        self.assertEqual(self.per_minute("halo-2"), [])
        self.assertEqual({tuple(keys) for keys in self.per_minute("halo-1")}, {("turns",)})
        self.assertLessEqual(len(self.per_minute("halo-1")), 10)
        # A page opens: the next due poll carries what is wanted; it closes: back to the turn check alone.
        self.run_for(60, page=True, want=want)
        self.assertIn("host", self.per_minute("halo-2")[-1])
        self.clock.now += nodepoll.PAGE_S
        self.node.sessions.clear()
        self.run_for(120, page=False, want=want)
        self.assertEqual(self.per_minute("halo-2"), [])
        self.assertEqual({tuple(keys) for keys in self.per_minute("halo-1")}, {("turns",)})

    def test_a_held_turn_leaves_only_the_turn_check_once_a_minute(self):
        self.node.holder["halo-1"] = "someone-bench"
        want = [("halo-1", "host", "host"), ("halo-1", "gpu", "gpu")]
        self.run_for(240, page=True, want=want)
        sessions = self.per_minute("halo-1")
        self.assertIn("turns", sessions[0])                     # the first reading finds the holder
        self.assertTrue(all(keys == ["turns"] for keys in sessions[1:]))
        self.assertLessEqual(len(sessions), 5)
        self.assertEqual(self.poll.read("halo-1", "gpu", "gpu", {})[1][:6], "paused")
        self.assertEqual(turns._replies[id(self.api)]["halo-1"]["reply"]["holder"]["label"], "someone-bench")
        self.node.holder.clear()                                 # the turn ends: the next turn check frees the node
        self.run_for(150, page=True, want=want)
        self.assertIsNone(pollbudget.for_api(self.api).turn("halo-1"))
        self.assertIn("gpu", self.per_minute("halo-1")[-1])

    def test_one_poll_never_asks_for_more_parts_than_the_node_takes(self):
        self.assertEqual(nodepoll.MAX_PARTS, node_tool().POLL_MAX_PARTS)
        self.poll.page_seen()
        for index in range(50):
            self.clock.now += 0.01
            self.poll.read("halo-1", f"logs:{index}", "logs", {"tail": index})
        for worker in self.poll.tick():
            worker.join(5)
        keys = self.per_minute("halo-1")[0]
        self.assertEqual(len(keys), nodepoll.MAX_PARTS)
        self.assertIn("turns", keys)
        self.assertIn("logs:49", keys)                          # the most recently asked are kept
        self.assertNotIn("logs:0", keys)

    def test_a_long_payload_keeps_a_short_key(self):
        read = nodepoll.reader(self.api)
        self.poll.page_seen()
        with self.assertRaises(nodepoll.NotRead):
            read("halo-2", "logs", {"root": "/opt/" + "r" * 300, "lease": "abc", "name": "n", "tail": 100})
        key = next(k for k in self.poll._wants["halo-2"] if k.startswith("logs:"))
        self.assertLessEqual(len(key), 64)
        for worker in self.poll.tick():
            worker.join(5)
        self.assertEqual(read("halo-2", "logs", {"root": "/opt/" + "r" * 300, "lease": "abc", "name": "n",
                                                 "tail": 100})["payload"]["tail"], 100)

    def test_a_local_lab_machine_gets_its_turn_check_too(self):
        cluster = self.api.cluster()
        cluster.machines["bench"] = dataclasses.replace(cluster.machines["halo-1"], id="bench", access="local",
                                                        ssh_target=None)
        fetched = []
        with mock.patch.object(turns, "fetch", side_effect=lambda api, machines=None, **kw: fetched.append(machines)):
            self.run_for(120)
        self.assertEqual(fetched, [["bench"]] * 4)               # every 30 s, without a page or a store
        self.assertEqual(turns.sample(self.api, 0), [])          # the collector adds no second check
        self.assertEqual(len(fetched), 4)

    def test_a_local_turn_worker_that_cannot_start_is_retried(self):
        cluster = self.api.cluster()
        cluster.machines["bench"] = dataclasses.replace(cluster.machines["halo-1"], id="bench", access="local",
                                                        ssh_target=None)
        real = threading.Thread.start

        def start(thread):
            if thread.name == "hyperloom-node-poll-local":
                raise RuntimeError("can't start new thread")
            return real(thread)
        with mock.patch.object(turns, "fetch") as fetch:
            with mock.patch.object(threading.Thread, "start", start):
                self.run_for(2)
            self.run_for(2)
        fetch.assert_called_once()

    def test_a_failed_poll_is_each_part_s_error_and_a_busy_node_waits(self):
        self.poll._call = mock.Mock(side_effect=OSError("ssh refused"))
        self.poll.page_seen()
        self.poll.read("halo-2", "host", "host", {})
        for worker in self.poll.tick():
            worker.join(5)
        answer, error, _ = self.poll.read("halo-2", "host", "host", {})
        self.assertIsNone(answer)
        self.assertIn("ssh refused", error)
        from hyperloom import nodegate
        release = nodegate.try_hold(self.api.config_path, "halo-1")
        try:
            self.clock.now += 60
            self.assertNotIn("halo-1", [w.name.rsplit("-", 1)[-1] for w in self.poll.tick()])
        finally:
            release()

    def test_collectors_take_the_poll_s_answer_and_open_no_session(self):
        answer = host("halo-1")
        self.poll._answers["halo-1"] = {"host": (answer, None, 990.0)}
        self.poll._answers["halo-2"] = {"host": (None, "OSError: unreachable", 990.0)}
        round_ = machines.collect_nodes(self.api, 1000.0)
        self.assertEqual(self.api.calls, [])                    # api.node never called: no session of its own
        rows = {row["machine"]: row for row in round_["machines"]}
        self.assertTrue(rows["halo-1"]["doc"]["ok"])
        self.assertIn("unreachable", rows["halo-2"]["doc"]["error"])
        again = {row["machine"]: row for row in machines.collect_nodes(self.api, 1030.0)["machines"]}
        self.assertIn("no new node reading", again["halo-1"]["skipped"])       # one answer is taken once
        self.assertEqual(self.poll._wants["halo-1"]["host"][0], "host")


class StartTests(unittest.TestCase):
    def serve(self, environment, telemetry_fails=False):
        from hyperloom.web import server
        api = SimpleNamespace(config_path=Path(tempfile.gettempdir()) / "unused" / "cluster.json",
                              _cluster=lambda: lab(), telemetry=None)
        fake = mock.MagicMock()
        fake.__enter__.return_value = SimpleNamespace(api=api, origin="http://x", serve_forever=mock.Mock(
            side_effect=KeyboardInterrupt))
        started = []
        with mock.patch.object(server, "create_server", return_value=fake), \
                mock.patch.object(nodepoll, "start", side_effect=lambda a: started.append(a)), \
                mock.patch.object(server, "start_telemetry", side_effect=RuntimeError("store failed")
                                  if telemetry_fails else None) as telemetry, \
                mock.patch("hyperloom.nodegate.portal"), mock.patch.dict(os.environ, environment), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                server.serve(no_browser=True)
            except RuntimeError:
                pass
        return started, telemetry, api

    def test_the_turn_check_runs_with_telemetry_off_or_a_failed_store(self):
        started, telemetry, api = self.serve({"HYPERLOOM_TELEMETRY": "0"})
        self.assertEqual(started, [api])
        telemetry.assert_not_called()
        started, telemetry, api = self.serve({"HYPERLOOM_TELEMETRY": "1"}, telemetry_fails=True)
        self.assertEqual(started, [api])                        # started before the store is even tried

    def test_turns_are_recorded_without_any_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cluster = lab()
        cluster.machines["halo-1"] = dataclasses.replace(cluster.machines["halo-1"], lab_dir="/opt/lab")
        api = SimpleNamespace(config_path=Path(directory.name) / "cluster.json", cluster=lambda: cluster,
                              telemetry=None, orchestrate=SimpleNamespace(_read=lambda path: {}))
        node = Node()
        node.holder["halo-1"] = "someone-bench"
        poll = nodepoll.NodePoll(api, clock=Clock(1000.0), call=node)
        for worker in poll.tick():
            worker.join(5)
        self.assertEqual(pollbudget.for_api(api).turn("halo-1"), ("someone-bench", False))


class NodeVerbTests(unittest.TestCase):
    def test_poll_runs_read_only_parts_in_one_process(self):
        tool = node_tool()
        seen = []

        class Extension:
            @staticmethod
            def run(verb, payload):
                seen.append((verb, payload))
                return {"schema": "hyperloom/node-v1", "verb": verb, "body": "x" * 10}
        with mock.patch.object(tool, "extension_for", return_value=Extension):
            reply = tool.poll({"parts": [{"key": "v", "verb": "version", "payload": {}},
                                         {"key": "s", "verb": "status", "payload": {"root": "/opt/x"}},
                                         {"key": "h", "verb": "host", "payload": {}},
                                         {"key": "bad", "verb": "doctor", "payload": {"extra": 1}}]})
        self.assertEqual(reply["parts"]["v"]["checks"]["version"]["value"], tool.VERSION)
        self.assertEqual(seen, [("status", {"root": "/opt/x", "observe": True}), ("host", {})])
        self.assertIn("only root and lab_dir", reply["parts"]["bad"]["error"])
        for verb in ("lease", "put-secret", "model-verify", "copy-job", "stop", "logs-search", "poll"):
            with self.subTest(verb=verb), self.assertRaises(ValueError):
                tool.poll({"parts": [{"key": "k", "verb": verb, "payload": {}}]})
        for bad in ({}, {"parts": []}, {"parts": [{"key": "k", "verb": "host", "payload": {}}] * 2},
                    {"parts": [{"key": "k", "verb": "host", "payload": {}}], "other": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tool.poll(bad)

    def test_poll_is_bounded_by_its_deadline_and_reply_size(self):
        tool = node_tool()
        times = iter([0.0, 0.0, tool.POLL_DEADLINE_S + 1])

        class Big:
            @staticmethod
            def run(verb, payload):
                return {"schema": "hyperloom/node-v1", "verb": verb, "body": "x" * (tool.POLL_REPLY // 2 + 10)}
        with mock.patch.object(tool, "extension_for", return_value=Big):
            reply = tool.poll({"parts": [{"key": str(i), "verb": "host", "payload": {}} for i in range(2)]},
                              clock=lambda: next(times))
            self.assertNotIn("error", reply["parts"]["0"])
            self.assertIn("deadline", reply["parts"]["1"]["error"])
            reply = tool.poll({"parts": [{"key": str(i), "verb": "host", "payload": {}} for i in range(3)]},
                              clock=lambda: 0.0)
        self.assertEqual([("error" in reply["parts"][str(i)]) for i in range(3)], [False, True, True])
        self.assertIn("reply is full", reply["parts"]["1"]["error"])

    def test_the_transport_takes_a_poll_reply_up_to_its_own_limit(self):
        from hyperloom import remote
        self.assertIn("poll", remote.VERBS)
        client = remote.Remote(SimpleNamespace(id="halo-1", http_address="192.0.2.1", ssh_target="op@halo-1",
                                               ssh_port=22), mock.Mock(spec=remote.KnownHosts, **{"options.return_value": []}))
        client.node_path = "/opt/boot/hyperloom-node"
        limits = []
        with mock.patch.object(remote, "_run", side_effect=lambda *a, **kw: limits.append(a[4]) or b"{}"):
            client.call("poll", {"parts": []})
            client.call("host", {})
        self.assertEqual(limits, [remote.POLL_REPLY, remote.MAX_JSON])
        self.assertGreater(remote.POLL_REPLY, node_tool().POLL_REPLY)

    def test_status_and_logs_parts_create_nothing_on_the_node(self):
        """The lease verbs make run/, log/ and etc/ in an install root; as poll parts they only read."""
        tool = node_tool()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install"
            root.mkdir()
            (root / ".hyperloom-root").write_text("marked\n", encoding="utf-8")
            reply = tool.poll({"parts": [
                {"key": "s", "verb": "status", "payload": {"root": root.as_posix()}},
                {"key": "l", "verb": "logs", "payload": {"root": root.as_posix(), "lease": "abc", "name": "n",
                                                       "tail": 5}}]})
            self.assertEqual(sorted(p.name for p in root.iterdir()), [".hyperloom-root"])
            self.assertEqual(reply["parts"]["s"].get("leases"), [], reply["parts"]["s"])

    def test_a_home_is_never_a_model_root_on_the_node(self):
        import importlib
        import sys
        sys.path.insert(0, str(NODE))
        self.addCleanup(sys.path.remove, str(NODE))
        models = importlib.import_module("hyperloom_models")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "accounts" / "researcher"
            home.mkdir(parents=True)
            with mock.patch.object(models.Path, "home", return_value=home):
                for value in (home, home.parent):
                    with self.subTest(value=value), self.assertRaisesRegex(ValueError, "home directory"):
                        models.scan_root(str(value), None)

    def test_a_dead_waiter_is_reported_not_settled_by_the_poll(self):
        import importlib
        import sys
        sys.path.insert(0, str(NODE))
        self.addCleanup(sys.path.remove, str(NODE))
        lease = importlib.import_module("hyperloom_lease")
        state = {"lease": "abc", "state": "queued", "turn_pid": 1, "label": "hl-x"}
        with mock.patch.object(lease, "waiter_alive", return_value=False), \
                mock.patch.object(lease, "tail_file", return_value="refused\n"), \
                mock.patch.object(lease, "inside", return_value=Path("unused")), \
                mock.patch.object(lease, "write_json") as write, mock.patch.object(lease, "state_lock") as lock:
            settled = lease.settle_dead_waiter(Path("unused"), copy.deepcopy(state), write=False)
        self.assertEqual((settled["state"], settled["reason"]), ("failed", "turn ended before acquire: refused"))
        write.assert_not_called()
        lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
