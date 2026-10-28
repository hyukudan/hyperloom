"""Polling budget: the portal's pollers read a node at a bounded cadence and pause while a GPU turn holds it."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hyperloom import pollbudget
from hyperloom.web.features import machines

from tests.test_fleet_support import FakeAPI, host


POLLERS = ("fleet-nodes", "turns", "usb4", "ops-nodes", "inventory", "observe")


class BudgetTests(unittest.TestCase):
    def test_load_per_node_is_bounded_whatever_the_pollers_ask(self):
        """Six pollers asking every second for ten minutes (the review measured ~64 ssh/min per node before):
        each gets at most one read per 30 s, all together at most one session per 5 s."""
        budget = pollbudget.Budget()
        granted = []
        for second in range(600):
            for poller in POLLERS:
                if budget.node("halo-1", poller, now=1000.0 + second) is None:
                    granted.append((second, poller))
        for minute in range(10):
            in_minute = [g for g in granted if minute * 60 <= g[0] < (minute + 1) * 60]
            self.assertLessEqual(len(in_minute), 14)                     # twelve spaced reads and two turn checks
            for poller in POLLERS:
                self.assertLessEqual(sum(1 for g in in_minute if g[1] == poller), 2)
        seconds = [g[0] for g in granted if g[1] != "turns"]
        self.assertTrue(all(b - a >= pollbudget.NODE_SPACING_S for a, b in zip(seconds, seconds[1:])))
        self.assertEqual({g[1] for g in granted}, set(POLLERS))       # nobody starves

    def test_a_held_turn_pauses_every_poll_but_the_turn_check(self):
        budget = pollbudget.Budget()
        budget.note_turn("halo-1", "someone-bench", own=False, now=1000.0)
        granted = []
        for second in range(300):
            for poller in POLLERS:
                why = budget.node("halo-1", poller, now=1000.0 + second)
                if why is None:
                    granted.append((second, poller))
                    if poller == "turns":                                  # each turn check reads it again
                        budget.note_turn("halo-1", "someone-bench", now=1000.0 + second)
                elif poller != "turns" and second == 0:
                    self.assertIn("paused: a GPU turn holds halo-1 (someone-bench)", why)
        self.assertEqual({p for _, p in granted}, {"turns"})
        self.assertLessEqual(len(granted), 300 / pollbudget.TURN_CHECK_S + 1)
        self.assertIsNone(budget.node("halo-2", "fleet-nodes", now=1000.0))     # other nodes are not paused
        self.assertIn("paused", budget.engine_paused("halo-1", now=1299.0))
        budget.note_turn("halo-1", None, now=1300.0)                           # the turn ended
        self.assertIsNone(budget.node("halo-1", "turns", now=1310.0))
        self.assertIsNone(budget.node("halo-1", "fleet-nodes", now=1316.0))
        self.assertIsNone(budget.engine_paused("halo-1", now=1310.0))

    def test_a_holder_never_expires_by_age_and_an_own_turn(self):
        budget = pollbudget.Budget()
        budget.note_turn("halo-1", "x", now=1000.0)
        # The turn check stopped answering: the node stays paused (only a reading of a free GPU lifts the pause).
        self.assertEqual(budget.turn("halo-1", now=1000.0 + 86400), ("x", False))
        self.assertIn("paused", budget.node("halo-1", "fleet-nodes", now=1000.0 + 86400))
        budget.note_turn("halo-1", "hl-lease", own=True, now=2000.0)
        self.assertIsNotNone(budget.node("halo-1", "usb4", now=2000.0))         # node polls pause in any turn
        self.assertIsNone(budget.engine_paused("halo-1", now=2000.0))           # our own engines stay readable
        self.assertIsNone(budget.engine("halo-1", "a:1", "observe", now=2000.0))
        self.assertIn("once per 30 s", budget.engine("halo-1", "a:1", "observe", now=2010.0))

    def test_turn_detection_never_starves_behind_the_other_pollers(self):
        """The fleet and turn collectors at their real 30 s cadence (and the others asking every second): the turn
        check is granted every cycle, so a turn that starts later is seen within about a minute."""
        budget = pollbudget.Budget()
        turns = []
        for second in range(600):
            now = 1000.0 + second
            for poller in ("inventory-doctor", "inventory-gpu", "usb4", "observe"):
                budget.node("halo-1", poller, now=now)
            if second % 30 == 0:
                budget.node("halo-1", "fleet-nodes", now=now)
            if second % 30 == 3 and budget.node("halo-1", "turns", now=now) is None:
                turns.append(second)
        self.assertGreaterEqual(len(turns), 18)                              # ~every 30 s over ten minutes
        self.assertTrue(all(b - a <= 61 for a, b in zip(turns, turns[1:])))

    def test_a_turn_check_that_found_the_gate_busy_keeps_its_priority(self):
        """A collector's call holds the node gate when the turn check comes: the check is not made, its grant is
        given back, and the next collector yields to it (no starvation at the gate)."""
        from types import SimpleNamespace
        from hyperloom.web.features import ops
        api = SimpleNamespace(config_path="unused/cluster.json")
        machine = SimpleNamespace(id="halo-1", access="ssh")
        budget = pollbudget.for_api(api)
        budget.node("halo-1", "turns", now=1000.0)                         # the previous check
        busy = [True]
        with mock.patch("hyperloom.nodegate.try_hold", lambda config, mid: None if busy[0] else (lambda: None)):
            self.assertIsNone(budget.node("halo-1", "fleet-nodes", now=1029.0))   # its call holds the gate
            self.assertFalse(ops.sampler_gate(api, machine, "turns", 1030.0)[0])   # busy: no check made
            busy[0] = False
            self.assertIn("goes first", budget.node("halo-1", "fleet-nodes", now=1059.0))
            self.assertTrue(ops.sampler_gate(api, machine, "turns", 1060.0)[0])

    def test_holder_lines(self):
        self.assertEqual(pollbudget.holder_label("PAIR qwen-bench since 01:02:03 pid 5"), "qwen-bench")
        self.assertEqual(pollbudget.holder_label("single-x (local) since 01:02:03"), "single-x")
        self.assertIsNone(pollbudget.holder_label("  "))


class CollectorTests(unittest.TestCase):
    def test_the_fleet_collector_pauses_a_node_a_turn_holds(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI(directory)
            self.addCleanup(api.telemetry.close)
            api.answers = {"halo-1": host("halo-1"), "halo-2": host("halo-2")}
            pollbudget.for_api(api).note_turn("halo-1", "someone-bench", now=1_800_000_000.0)
            round_ = machines.collect_nodes(api, 1_800_000_000.0)
            self.assertEqual([mid for mid, _, _ in api.calls], ["halo-2"])
            skipped = {m["machine"]: m.get("skipped") for m in round_["machines"]}
            self.assertIn("paused: a GPU turn holds halo-1", skipped["halo-1"])
            machines.collect_nodes(api, 1_800_000_005.0)                       # 5 s later: paced, nothing called
            self.assertEqual(len(api.calls), 1)

    def test_the_turns_collector_reading_pauses_the_others(self):
        from hyperloom.web.features import turns
        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI(directory)
            self.addCleanup(api.telemetry.close)
            machine = next(m for m in api.cluster().machines.values() if m.access == "ssh")
            reply = {"schema": "hyperloom/node-v1", "holder": {"parsed": True, "label": "someone-bench",
                                                                 "alive": True}}
            with mock.patch.object(turns.eligible.__globals__["ops"], "_node_call", lambda *a, **k: reply), \
                    mock.patch.object(turns, "eligible", lambda cluster: ([machine], {})):
                turns.fetch(api, skip_busy=True)
            self.assertIn("paused", pollbudget.for_api(api).node(machine.id, "fleet-nodes"))


if __name__ == "__main__":
    unittest.main()
