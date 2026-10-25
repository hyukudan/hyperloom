"""GPU turn queue (F35): the read-only `turns` node verb against recorded lab files and a fake /proc, the portal's
cross-node composition and the big-job ledger."""

import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import config
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API
from hyperloom.web.features import ops, turns

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "ops"
spec = importlib.util.spec_from_file_location("hyperloom_turns_under_test", ROOT / "node" / "hyperloom_turns.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)

HZ = 100


def local_epoch(y, mo, d, h, mi, s):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


class FakeProc:
    """A /proc tree: boot at `boot`, processes with a start time and a command line."""

    def __init__(self, root, boot, now):
        self.root = Path(root)
        self.boot = boot
        (self.root / "sys/kernel/random").mkdir(parents=True)
        (self.root / "stat").write_text(f"cpu 1 2 3\nbtime {int(boot)}\n")
        (self.root / "uptime").write_text(f"{now - boot:.2f} 1.00\n")
        (self.root / "sys/kernel/random/boot_id").write_text("00000000-0000-4000-8000-000000000001\n")

    def add(self, pid, started, argv, state="S"):
        directory = self.root / str(pid)
        directory.mkdir()
        ticks = int((started - self.boot) * HZ)
        fields = [state] + ["0"] * 18 + [str(ticks), "0", "0"]
        (directory / "stat").write_text(f"{pid} (bash) " + " ".join(fields) + "\n")
        (directory / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def turn_argv(label, pair=False):
    return ["/bin/bash", "/opt/lab/gpu-turn" + ("-pair" if pair else "") + ".sh", label, "./run.sh"]


class TreeMixin:
    def build(self, node_name="halo-1"):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.lab = base / "lab"
        state = self.lab / ".gpu-turn"
        state.mkdir(parents=True)
        source = FIXTURES / node_name
        (state / "holder").write_bytes((source / "holder").read_bytes())
        (state / "log").write_bytes((source / "log").read_bytes())
        self.tickets = json.loads((source / "tickets.json").read_text())
        for queue, names in self.tickets.items():
            if names or queue == "single-q" or node_name == "halo-1":
                (state / queue).mkdir(exist_ok=True)
            for name in names:
                (state / queue / name).write_bytes(b"")
        # the recorded log ends at 02:09:06 local time; read it a little later the same night
        self.now = local_epoch(2026, 10, 25, 2, 9, 30)
        os.utime(state / "log", (self.now - 20, self.now - 20))
        self.proc = FakeProc(base / "proc", self.now - 200000, self.now)
        self.uid = os.stat(base).st_uid
        return state

    def collect(self):
        return node.collect(self.lab, proc=self.proc.root, now=self.now, uid=self.uid)


class TurnsNodeTests(TreeMixin, unittest.TestCase):
    def setUp(self):
        self.state = self.build("halo-1")

    def ticket(self, queue, index):
        name = self.tickets[queue][index]
        ns, pid = name.rsplit("-", 1)
        return int(ns.split("-")[-1]) / 1e9, int(pid)

    def test_holder_and_ticket_liveness(self):
        holder = (self.state / "holder").read_text()
        pid = int(holder.split()[-1])
        self.proc.add(pid, self.now - 3600, turn_argv(holder.split()[0]))
        live_ts, live_pid = self.ticket("single-q", 0)
        self.proc.add(live_pid, live_ts - 0.5, turn_argv("job-x"))                    # started before its ticket
        reused_ts, reused_pid = self.ticket("single-q", 1)
        self.proc.add(reused_pid, reused_ts + 5, turn_argv("job-y"))                  # started after: reused pid
        other_ts, other_pid = self.ticket("single-q", 2)
        self.proc.add(other_pid, other_ts - 10, ["/usr/bin/sleep", "100"])            # another program
        close_ts, close_pid = self.ticket("single-q", 3)
        same_tick = self.proc.boot + int((close_ts - self.proc.boot) * HZ) / HZ + 1e-6
        self.proc.add(close_pid, same_tick, turn_argv("job-z"))                       # within the clock resolution
        zombie_ts, zombie_pid = self.ticket("single-q", 4)
        self.proc.add(zombie_pid, zombie_ts - 1, turn_argv("job-w"), state="Z")
        result = self.collect()
        self.assertTrue(result["holder"]["alive"] and result["holder"]["verified"])
        self.assertEqual(result["holder"]["kind"], "single")
        queue = result["queues"]["single-q"]
        self.assertEqual([(t["pid"], t["label"]) for t in queue], [(live_pid, "job-x")])
        self.assertEqual([t["pid"] for t in result["unverified"]["single-q"]], [close_pid])
        self.assertEqual(result["stale_tickets"]["single-q"], len(self.tickets["single-q"]) - 2)
        self.assertGreater(queue[0]["waited_s"], 0)

    def test_holder_with_reused_pid_or_other_label_is_stale(self):
        holder = (self.state / "holder").read_text()
        pid = int(holder.split()[-1])
        self.proc.add(pid, self.now - 3600, turn_argv("another-label"))
        result = self.collect()
        self.assertFalse(result["holder"]["alive"])
        self.assertIn("stale holder file", result["holder"]["why_alive"])

    def test_previous_boot_tickets_are_not_live(self):
        ts, pid = self.ticket("pair-q", 0)
        self.proc.boot = ts + 100      # the node rebooted after the ticket was written
        proc = FakeProc(Path(self.tmp.name) / "proc2", ts + 100, self.now)
        proc.add(pid, ts + 200, turn_argv("job-a", pair=True))
        result = node.collect(self.lab, proc=proc.root, now=self.now, uid=self.uid)
        self.assertEqual(result["queues"]["pair-q"], [])

    def test_gate_and_priority_tickets(self):
        gate = self.state / "pair-q" / "0-0-single-halo-2-4242"
        gate.write_bytes(b"")
        os.utime(gate, (self.now - 30, self.now - 30))
        priority = f"0-{int((self.now - 40) * 1e9)}-4343"
        (self.state / "pair-q" / priority).write_bytes(b"")
        self.proc.add(4242, self.now - 60, ["bash", "-c", "gate"])
        self.proc.add(4343, self.now - 60, turn_argv("job-p", pair=True))
        result = self.collect()
        kinds = [(t["kind"], t["pid"]) for t in result["queues"]["pair-q"]]
        self.assertIn(("priority", 4343), kinds)
        self.assertNotIn(("gate", 4242), kinds)      # a gate's process is not a turn script: not its writer
        self.assertTrue(result["coordinator"])

    def test_log_turns_and_rollover(self):
        result = self.collect()
        turns_ = result["turns"]
        self.assertTrue(turns_)
        ended = [t for t in turns_ if t["outcome"].startswith("ended")]
        self.assertTrue(ended and all(t["duration_s"] >= 0 for t in ended))
        self.assertEqual(turns_[-1]["outcome"], "running")
        # the recorded log starts the evening before: the first events fall on the previous day
        first = datetime.datetime.fromtimestamp(result["events"][0]["ts"])
        self.assertEqual((first.day, first.hour >= 12), (24, True))
        self.assertTrue(all(a["ts"] <= b["ts"] + 300 for a, b in zip(result["events"], result["events"][1:])))

    def test_cap_reap_and_clock_crossing_with_a_long_gap(self):
        log = ("18:00:00 wait  job-a pid 11\n18:00:01 admit job-a pid 11 (no PAIR waiting)\n18:00:01 start job-a pid 11\n"
               "18:08:01 CAP   job-a hit 480s (rc 124)\n18:08:02 reap  job-a pid 11: WARN still alive [9]; reaped 2 pids "
               "[9:x:turn 10:y:gpu]; vram 158->4000 MiB, gtt 22->22 MiB; 61.0 s\n18:08:03 end   job-a pid 11\n"
               "14:00:00 start job-b pid 12\n14:01:00 end   job-b pid 12\n")
        (self.state / "log").write_text(log)
        anchor = local_epoch(2026, 10, 26, 14, 1, 5)
        os.utime(self.state / "log", (anchor, anchor))
        result = node.collect(self.lab, proc=self.proc.root, now=anchor + 10, uid=self.uid)
        first, second = result["turns"]
        self.assertEqual((first["cap_hit_s"], first["rc"]), (480, 124))
        self.assertIn("cap hit", first["outcome"])
        self.assertIn("reap warning", first["outcome"])
        self.assertEqual(first["reap"]["local"]["reaped"], 2)
        self.assertEqual(datetime.datetime.fromtimestamp(first["start"]).day, 25)   # 18:00 belongs to the day before
        self.assertEqual(datetime.datetime.fromtimestamp(second["start"]).day, 26)

    def test_old_cap_line_form_and_flags(self):
        (self.state / "log").write_text("10:00:00 start job-c pid 5\n10:20:00 CAP   job-c hit GPU_TURN_MAX=1200s\n"
                                        "10:20:01 end   job-c pid 5\n")
        (self.state / "maintenance").write_text("window:kernel\n")
        (self.state / "long-ok").write_text("job-c 1200\njob-d 99999\nbad line\n")
        result = self.collect()
        self.assertEqual(result["turns"][0]["cap_hit_s"], 1200)
        self.assertEqual(result["flags"]["maintenance"], "window:kernel")
        self.assertEqual(result["long_ok"], {"job-c": 1200})

    def test_links_and_bad_payloads_are_refused(self):
        self.assertIn("error", node.run("turns", {}))
        self.assertIn("error", node.run("turns", {"lab_dir": "relative/dir"}))
        self.assertIn("error", node.run("turns", {"lab_dir": "/x/../etc"}))
        self.assertIn("error", node.run("turns", {"lab_dir": "/x", "extra": 1}))
        if hasattr(os, "symlink"):
            target = Path(self.tmp.name) / "elsewhere"
            target.mkdir()
            (target / "secret").write_text("x")
            try:
                os.unlink(self.state / "holder")
                os.symlink(target / "secret", self.state / "holder")
            except OSError:
                self.skipTest("symlinks unavailable")
            result = self.collect()
            self.assertTrue(result["holder"]["unreadable"])
            self.assertIn("unreadable", result["why"]["holder"])

    def test_unreadable_holder_is_not_a_free_gpu(self):
        os.unlink(self.state / "holder")
        (self.state / "holder").mkdir()                  # not a plain file
        result = self.collect()
        self.assertTrue(result["holder"]["unreadable"])
        self.assertIn("unreadable", result["why"]["holder"])

    def test_linked_ancestor_is_refused(self):
        if not node.FD_WALK:
            self.skipTest("descriptor walk is POSIX only")
        link = Path(self.tmp.name) / "lab-link"
        os.symlink(self.lab, link)
        result = node.collect(link, proc=self.proc.root, now=self.now, uid=self.uid)
        self.assertFalse(result["present"])

    def test_full_queues_stay_within_the_output_limit(self):
        for queue in ("single-q", "pair-q"):
            for index in range(300):
                ts = self.now - 600 + index * 0.5
                pid = 200000 + index + (0 if queue == "single-q" else 5000)
                (self.state / queue / f"{int(ts * 1e9)}-{pid}").write_bytes(b"")
                self.proc.add(pid, ts - 1, turn_argv("l" * 120 + str(index)))
        result = self.collect()
        self.assertLessEqual(len(json.dumps(result)), node.OUTPUT_BUDGET)
        examined = len(result["queues"]["single-q"]) + len(result["queues"]["pair-q"])
        self.assertLessEqual(examined, node.MAX_TICKETS)
        self.assertIn("enumeration_truncated", result["why"])

    def test_long_ok_is_validated_and_bounded(self):
        lines = "".join(f"job-{'x' * 100}{i}\x7f 600\n" for i in range(400)) + "job-ok 900\n"
        (self.state / "long-ok").write_text(lines, encoding="utf-8")
        result = self.collect()
        self.assertEqual(result["long_ok"], {"job-ok": 900})
        (self.state / "long-ok").write_text("".join(f"job-{'y' * 120}{i} 600\n" for i in range(400)), encoding="utf-8")
        result = self.collect()
        self.assertLessEqual(len(result["long_ok"]), node.MAX_LONG_OK)
        self.assertLessEqual(len(json.dumps(result)), node.OUTPUT_BUDGET)

    def test_output_is_bounded(self):
        lines = "".join(f"00:{i // 60 % 60:02d}:{i % 60:02d} wait  job-{i} pid {i + 10} {'x' * 300}\n" for i in range(4000))
        (self.state / "log").write_text(lines)
        result = self.collect()
        self.assertLessEqual(len(json.dumps(result)), node.OUTPUT_BUDGET)
        self.assertLessEqual(len(result["events"]), node.MAX_EVENTS)

    def test_fixtures_hold_only_the_fixture_vocabulary(self):
        allowed_labels = re.compile(r"job-\d+")
        for path in FIXTURES.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", path)
            self.assertNotRegex(text, r"/home/|C:\\\\Users|@", path)
            for label in re.findall(r"(?:wait|start|end|reap|admit|CAP|REFUSE)\s+(?:PAIR )?(\S+)", text):
                self.assertRegex(label, allowed_labels, path)
            for node_name in re.findall(r"\| (\S+): reaped|0-0-single-(\S+)-\d+|LAUNCHER .* by (\S+)", text):
                self.assertIn("".join(node_name), ("halo-1", "halo-2", "job-a"), path)


class FakeOrchestrator:
    def __init__(self):
        self.statuses = {}

    def status(self, profile=None):
        return self.statuses

    def _profile(self, cluster, profile):
        settings = cluster.profiles[profile]
        return settings, "qwen-replicas", settings.get("nodes") or [settings["rank0"], settings["rank1"]]

    def _read(self, path):
        raise FileNotFoundError(path)

    def _state_path(self, profile):
        return Path("/nonexistent/up-" + profile + ".json")

    def _known_hosts(self):
        return Path("/nonexistent/known_hosts")


def cluster_of(lab_dir="/opt/lab"):
    machines = {"workstation": config.Machine("workstation", "windows", "local", "127.0.0.1", None, 22, "C:/hl", True, []),
                "halo-1": config.Machine("halo-1", "linux", "ssh", "192.0.2.10", "halo-1", 22, "/opt/hl", True,
                                         [{"id": "gpu"}], lab_dir),
                "halo-2": config.Machine("halo-2", "linux", "ssh", "192.0.2.11", "halo-2", 22, "/opt/hl", True,
                                         [{"id": "gpu"}], lab_dir)}
    profiles = {"qwen-replicas": {"nodes": ["halo-1", "halo-2"]}}
    raw = {"schema": "hyperloom/cluster-v1", "machines": [], "assets": {}, "profiles": profiles}
    return config.Cluster(machines, {}, profiles, {}, {}, raw)


class APIFixture:
    def make_api(self, cluster=None):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        api = API(Path(self.dir.name) / "cluster.json", orchestrator=FakeOrchestrator(), remote_module=mock.Mock(),
                  audit_path=Path(self.dir.name) / "audit.jsonl")
        api.telemetry = Store(":memory:")
        self.addCleanup(api.telemetry.close)
        cluster = cluster or cluster_of()
        api._cluster = lambda: cluster
        api.snapshot = lambda: {"profiles": {"profiles": []}}
        features.reset(features.load(("turns", "logsearch", "ops")))
        self.addCleanup(features.reset)
        return api


class TurnsPortalTests(APIFixture, TreeMixin, unittest.TestCase):
    def setUp(self):
        self.api = self.make_api()
        turns._replies.clear()

    def reply(self, holder, turn_list, coordinator, now, pair_q=None, single_q=None, first_ts=None):
        return {"schema": "hyperloom/node-v1", "verb": "turns", "now": now, "present": True, "holder": holder,
                "coordinator": coordinator, "flags": {}, "long_ok": {}, "turns": turn_list, "events": [],
                "queues": {"single-q": single_q or [], "pair-q": pair_q if coordinator else None},
                "unverified": {"single-q": [], "pair-q": []}, "stale_tickets": {"single-q": 0, "pair-q": 0},
                "log": {"first_ts": first_ts}, "why": {}}

    @staticmethod
    def turn(label, kind, side, start, duration):
        return {"label": label, "kind": kind, "side": side, "start": start, "start_clock": "x", "end": start + duration,
                "duration_s": duration, "outcome": "ended (rc not logged)", "pid": 1 if side == "local" else None}

    def test_remote_pair_attempts_are_confirmed_one_to_one(self):
        now = 10_000.0
        c = [self.turn("job-p", "pair", "local", 5000, 300), self.turn("job-s", "single", "local", 4000, 100),
             self.turn("job-s", "single", "local", 4200, 120), self.turn("job-s", "single", "local", 4400, 90)]
        s = [self.turn("job-p", "pair", "remote", 4880, 60), self.turn("job-p", "pair", "remote", 4941, 60),
             self.turn("job-p", "pair", "remote", 4998, 302), self.turn("job-q", "pair", "remote", 7000, 60)]
        replies = {"halo-1": {"reply": self.reply(None, c, True, now, pair_q=[], first_ts=3000), "fetched_at": now,
                              "error": None, "offset_s": 0.0, "offset_uncertainty_s": 0.1},
                   "halo-2": {"reply": self.reply(None, s, False, now), "fetched_at": now, "error": None,
                              "offset_s": 0.0, "offset_uncertainty_s": 0.1}}
        doc = turns.compose(self.api, replies, now=now)
        second = next(n for n in doc["nodes"] if n["machine"] == "halo-2")
        pairs = [t for t in second["recent"] if t["kind"] == "pair"]
        self.assertEqual(len(pairs), 1)                         # one run, from the first node, counted once
        self.assertEqual((pairs[0]["side"], pairs[0]["duration_s"]), ("both nodes", 300))
        self.assertEqual(second["unconfirmed_attempts"], 3)

    def test_competing_coordinator_starts_leave_attempts_unconfirmed(self):
        now = 10_000.0
        c = [self.turn("job-p", "pair", "local", 5000, 10), self.turn("job-p", "pair", "local", 5030, 10)]
        s = [self.turn("job-p", "pair", "remote", 4990, 60)]
        replies = {"halo-1": {"reply": self.reply(None, c, True, now, pair_q=[], first_ts=3000), "fetched_at": now, "error": None,
                              "offset_s": 0.0, "offset_uncertainty_s": 0.0},
                   "halo-2": {"reply": self.reply(None, s, False, now), "fetched_at": now, "error": None,
                              "offset_s": 0.0, "offset_uncertainty_s": 0.0}}
        doc = turns.compose(self.api, replies, now=now)
        second = next(n for n in doc["nodes"] if n["machine"] == "halo-2")
        self.assertFalse([t for t in second["recent"] if t["kind"] == "pair"])
        self.assertEqual(second["unconfirmed_attempts"], 1)

    def test_remote_holder_needs_a_confirmed_running_pair(self):
        now = 10_000.0
        main = {"parsed": True, "kind": "pair", "label": "job-p", "side": "local", "since": 9900.0, "pid": 7,
                "alive": True, "verified": True, "since_clock": "02:00:00"}
        remote_holder = {"parsed": True, "kind": "pair", "label": "job-p", "side": "remote", "since": 9898.0, "pid": None,
                         "alive": None, "verified": None, "since_clock": "01:59:58"}
        running = {**self.turn("job-p", "pair", "local", 9900, 0), "end": None, "duration_s": None, "outcome": "running",
                   "pid": 7}
        attempt = {**self.turn("job-p", "pair", "remote", 9898, 0), "end": None, "duration_s": None,
                   "outcome": "running", "start_clock": "01:59:58"}
        replies = {"halo-1": {"reply": self.reply(main, [running], True, now, pair_q=[], first_ts=9000), "fetched_at": now,
                              "error": None, "offset_s": 0.0, "offset_uncertainty_s": 0.0},
                   "halo-2": {"reply": self.reply(remote_holder, [attempt], False, now), "fetched_at": now,
                              "error": None, "offset_s": 0.0, "offset_uncertainty_s": 0.0}}
        second = next(n for n in turns.compose(self.api, replies, now=now)["nodes"] if n["machine"] == "halo-2")
        self.assertTrue(second["holder"]["pair_state"].startswith("running"))
        # a first-node log that starts inside the confirmation window does not cover it: unconfirmed
        replies["halo-1"]["reply"]["log"]["first_ts"] = 9850
        second = next(n for n in turns.compose(self.api, replies, now=now)["nodes"] if n["machine"] == "halo-2")
        self.assertIn("unconfirmed", second["holder"]["pair_state"])
        replies["halo-1"]["reply"]["log"]["first_ts"] = 9000
        # two attempts with the same time are ambiguous: nothing is confirmed
        replies["halo-2"]["reply"]["turns"] = [attempt, dict(attempt)]
        second = next(n for n in turns.compose(self.api, replies, now=now)["nodes"] if n["machine"] == "halo-2")
        self.assertIn("unconfirmed", second["holder"]["pair_state"])
        replies["halo-2"]["reply"]["turns"] = [attempt]
        # without the coordinator's run (e.g. its holder belongs to another label) the remote holder is unconfirmed
        replies["halo-1"]["reply"]["holder"] = {**main, "label": "job-other"}
        second = next(n for n in turns.compose(self.api, replies, now=now)["nodes"] if n["machine"] == "halo-2")
        self.assertIn("unconfirmed", second["holder"]["pair_state"])

    def test_remote_holder_without_log_records_is_unconfirmed(self):
        now = 10_000.0
        main = {"parsed": True, "kind": "pair", "label": "job-p", "side": "local", "since": 9900.0, "pid": 7,
                "alive": True, "verified": True}
        remote_holder = {"parsed": True, "kind": "pair", "label": "job-p", "side": "remote", "since": 9898.0, "pid": None}
        replies = {"halo-1": {"reply": self.reply(main, [], True, now, pair_q=[]), "fetched_at": now, "error": None,
                              "offset_s": 0.0, "offset_uncertainty_s": 0.0},
                   "halo-2": {"reply": self.reply(remote_holder, [], False, now), "fetched_at": now, "error": None,
                              "offset_s": 0.0, "offset_uncertainty_s": 0.0}}
        second = next(n for n in turns.compose(self.api, replies, now=now)["nodes"] if n["machine"] == "halo-2")
        self.assertIn("unconfirmed", second["holder"]["pair_state"])

    def test_holder_cap_and_estimates(self):
        now = 10_000.0
        holder = {"parsed": True, "kind": "single", "label": "job-h", "side": "local", "since": now - 500, "pid": 3,
                  "alive": True, "verified": True}
        recent = [self.turn("job-s", "single", "local", 1000 + i * 1000, 100 + i) for i in range(5)]
        tickets = [{"name": "1-2", "kind": "single", "pid": 9, "ts": now - 60, "label": "job-w", "verified": True,
                    "waited_s": 60}, {"name": "1-3", "kind": "single", "pid": 10, "ts": now - 30, "label": "job-v",
                                      "verified": True, "waited_s": 30}]
        replies = {"halo-1": {"reply": self.reply(holder, recent, True, now, pair_q=[], single_q=tickets),
                              "fetched_at": now, "error": None, "offset_s": 0.0, "offset_uncertainty_s": 0.0}}
        doc = turns.compose(self.api, replies, now=now)
        first = doc["nodes"][0]
        self.assertTrue(first["holder"]["past_cap"])
        self.assertEqual(first["holder"]["cap_s"], 480)
        self.assertEqual([w["position"] for w in first["waiting"]], [1, 2])
        self.assertEqual(first["waiting"][1]["estimated_start_s"] - first["waiting"][0]["estimated_start_s"], 102)
        self.assertIn("estimate", first["waiting"][0]["estimate"])

    def test_document_reads_the_collector_cache_only(self):
        calls = []
        with mock.patch.object(ops, "_node_call", lambda api, m, verb, payload: calls.append((m.id, verb)) or
                               {"schema": "hyperloom/node-v1", "verb": "turns", "now": 1.0, "present": False, "why": {}}):
            doc = self.api.read("turns", {})
            self.assertEqual(calls, [])
            self.assertIsNone(doc["ledger"]["state"])
            turns.sample(self.api, 0)
            self.assertEqual(sorted(calls), [("halo-1", "turns"), ("halo-2", "turns")])
            doc = self.api.read("turns", {})
        self.assertEqual([n["state"] for n in doc["nodes"]], ["unavailable", "unavailable"])
        self.assertEqual([s["machine"] for s in doc["skipped"]], ["workstation"])
        with self.assertRaises(Exception):
            self.api.read("turns", {"path": "/etc/passwd"})


LEDGER = """# coordination
- 21:30 job-a -> all: rule: before anything big, post "PC-BIG start <what>" and "PC-BIG end"
- 22:10 job-b: PC-BIG start (run 1, ~12 min; tope 900 s)
- 22:15 job-c: looked at the PRO, nothing (PC-BIG pending)
- 22:31 job-b: PC-BIG end (run 1: rc 0)
- 23:50 job-d: **PC-BIG start** long sweep (cap 30 min)
- 00:20 job-d: PC-BIG end
- 00:40 job-e: PC-BIG end (nothing started)
- 01:05 job-f: PC-BIG start (sweep 2)
"""


class LedgerTests(APIFixture, unittest.TestCase):
    def setUp(self):
        self.api = self.make_api()
        self.path = Path(self.dir.name) / "coordination.md"
        self.path.write_text(LEDGER, encoding="utf-8")
        self.anchor = local_epoch(2026, 10, 25, 1, 10, 0)
        os.utime(self.path, (self.anchor, self.anchor))
        turns._ledger_cache.update(key=None, records=None)

    def test_parse_pair_and_rollover(self):
        records = turns.ledger_records(str(self.path))
        self.assertEqual([(r["owner"], r["action"]) for r in records],
                         [("job-b", "start"), ("job-b", "end"), ("job-d", "start"), ("job-d", "end"), ("job-e", "end"),
                          ("job-f", "start")])
        self.assertEqual(records[0]["cap_s"], 900)
        self.assertEqual(records[2]["cap_s"], 1800)
        self.assertEqual(datetime.datetime.fromtimestamp(records[0]["ts"]).day, 24)
        self.assertEqual(datetime.datetime.fromtimestamp(records[-1]["ts"]).day, 25)
        state = turns.ledger_state(records, self.anchor + 60, 1800)
        self.assertEqual(state["state"], "held")
        self.assertEqual(state["holders"][0]["owner"], "job-f")
        self.assertEqual([r["owner"] for r in state["recent"]], ["job-d", "job-b"])
        self.assertEqual(state["recent"][1]["duration_s"], 21 * 60)
        self.assertEqual([a["why"] for a in state["anomalies"]], ["an end without a start"])
        later = turns.ledger_state(records, self.anchor + 3 * 3600, 1800)    # recomputed at read time: now stale
        self.assertEqual((later["state"], [r["owner"] for r in later["stale"]]), ("free", ["job-f"]))

    def test_settings_and_document(self):
        self.assertEqual(self.api.read("turns", {})["ledger"]["why"], "ledger path not configured (turns settings)")
        with self.assertRaises(Exception):
            self.api.feature_post("turns/settings", {"ledger_path": "relative.md"})
        with self.assertRaises(Exception):
            self.api.feature_post("turns/settings", {"ledger_cap_s": 5})
        self.api.feature_post("turns/settings", {"ledger_path": str(self.path), "ledger_cap_s": 1200})
        ledger = self.api.read("turns", {})["ledger"]
        self.assertIn(ledger["state"], ("held", "free"))
        audit = [json.loads(line) for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(audit[-1]["action"], "turns/settings")

    def test_ledger_through_a_junction_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows junctions")
        import _winapi
        junction = Path(self.dir.name) / "via-junction"
        _winapi.CreateJunction(self.dir.name, str(junction))
        with self.assertRaises(OSError):
            turns.read_ledger(str(junction / "coordination.md"))
        self.assertTrue(turns.read_ledger(str(self.path))[0])

    def test_conflicting_opens(self):
        records = turns.parse_ledger("- 10:00 job-a: PC-BIG start x\n- 10:05 job-b: PC-BIG start y\n", self.anchor)
        state = turns.ledger_state(records, records[-1]["ts"] + 60, 1800)
        self.assertEqual(state["state"], "conflict")
        self.assertEqual(len(state["holders"]), 2)

    def test_links_are_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        link = Path(self.dir.name) / "link.md"
        try:
            os.symlink(self.path, link)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(OSError):
            turns.read_ledger(str(link))


if __name__ == "__main__":
    unittest.main()
