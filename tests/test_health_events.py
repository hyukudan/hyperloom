"""Activity log (F23): filters and paging, clearing with shared confirmation codes, the audit-log mirror."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hyperloom.telemetry.store import Store
from hyperloom.web.features import events, health
from tests.test_health_engine import Clock, FakeAPI, T0


def audit_line(action, when, arguments=None, **result):
    return json.dumps({"when": datetime.fromtimestamp(when, timezone.utc).isoformat(), "action": action,
                       "arguments": arguments or {}, "result": result}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.api = FakeAPI(self.store, self.clock, self.dir.name)
        self.addCleanup(self.api.admission_db.close)
        health.prepare(self.store)
        self.owner = health.owner_of(self.api)
        self.generation = health.acquire(self.store, self.owner, T0)

    def collect(self):
        health.acquire(self.store, self.owner, self.clock.now)
        return events.collect(self.api, self.clock.now)

    def append(self, text, mode="a"):
        with open(self.api.audit_path, mode + "b") as stream:
            stream.write(text.encode("utf-8") if isinstance(text, str) else text)

    def mirrored(self):
        return [e["message"] for e in reversed(self.store.events(limit=500))
                if e["category"] in ("jobs", "config") and not e["message"].startswith("activity log cleared")]


class DocumentTests(Base):
    def test_filters_paging_and_validation(self):
        for i in range(7):
            self.store.event("health" if i % 2 else "alerts", "warning" if i % 3 == 0 else "info", f"e{i}",
                             machine="halo-1" if i < 4 else None, ts=T0 + i)
        page = events.document(self.api, {"limit": "3"})
        self.assertEqual([e["message"] for e in page["events"]], ["e6", "e5", "e4"])
        nxt = events.document(self.api, {"limit": "3", "before": str(page["next_before"])})
        self.assertEqual([e["message"] for e in nxt["events"]], ["e3", "e2", "e1"])
        self.assertEqual(sorted(page["categories"]), ["alerts", "health"])
        self.assertEqual(page["machines"], ["halo-1"])
        self.assertEqual([e["message"] for e in events.document(self.api, {"severity": "warning"})["events"]], ["e6", "e3", "e0"])
        self.assertEqual(len(events.document(self.api, {"category": "health", "machine": "halo-1"})["events"]), 2)
        for bad in ({"path": "x"}, {"limit": "0"}, {"limit": "201"}, {"before": "-1"}, {"before": "x"},
                    {"severity": "fatal"}, {"category": "a b"}, {"machine": "x" * 65}):
            with self.assertRaises(ValueError, msg=bad):
                events.document(self.api, bad)
        self.assertIsNone(page["clear"]["confirm"])         # codes come from the collector, never from a GET
        self.assertEqual(self.store.get(events.CONFIRM, "codes"), None)

    def test_no_store(self):
        self.api.telemetry = None
        document = events.document(self.api, {})
        self.assertIsNone(document["events"])
        self.assertIn("events", document["why"])


class ClearTests(Base):
    def test_clear_needs_the_current_code_and_rotates_it(self):
        self.store.event("health", "info", "old", ts=T0 - 8 * 86400)
        self.store.event("health", "info", "recent", ts=T0 - 3600)
        self.collect()
        codes = events.document(self.api, {})["clear"]["confirm"]
        self.assertEqual(sorted(codes), ["24h", "7d", "all"])
        for bad in ({"older_than": "7d", "confirm": "nope"}, {"older_than": "1y", "confirm": codes["7d"]},
                    {"older_than": "7d", "confirm": codes["24h"]}, {"older_than": "7d", "confirm": codes["7d"], "x": 1}):
            with self.assertRaises(ValueError, msg=bad):
                events.clear_post(self.api, bad)
        result = events.clear_post(self.api, {"older_than": "7d", "confirm": codes["7d"]})
        self.assertEqual(result, {"older_than": "7d", "removed": 1})
        self.assertEqual([e["message"] for e in self.store.events()], ["activity log cleared (older than 7 d)", "recent"])
        with self.assertRaises(ValueError):                        # a code works once
            events.clear_post(self.api, {"older_than": "7d", "confirm": codes["7d"]})
        self.assertNotEqual(events.document(self.api, {})["clear"]["confirm"]["7d"], codes["7d"])
        self.assertEqual([a[1] for a in self.api.audits], [{"older_than": "7d"}, {"older_than": "[invalid]"},
                                                           {"older_than": "7d"}, {"older_than": "7d"},
                                                           {"older_than": "7d"}, {"older_than": "7d"}])
        self.assertEqual([a[2]["state"] for a in self.api.audits], ["refused"] * 4 + ["succeeded", "refused"])

    def test_codes_expire_and_work_from_another_process(self):
        path = Path(self.dir.name) / "telemetry.sqlite3"
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        holder, other = FakeAPI(first, self.clock, self.dir.name), FakeAPI(second, self.clock, self.dir.name)
        self.addCleanup(holder.admission_db.close)
        self.addCleanup(other.admission_db.close)
        health.acquire(first, health.owner_of(holder), T0)
        events.collect(holder, T0)
        code = events.document(other, {})["clear"]["confirm"]["all"]
        self.assertEqual(events.clear_post(other, {"older_than": "all", "confirm": code})["older_than"], "all")
        self.clock.now = T0 + events.CODE_TTL_S + 1
        code = events.document(other, {})["clear"]["confirm"]
        self.assertIsNone(code)                                   # expired and not yet renewed

    def test_the_boundary_only_moves_forward(self):
        stale = events.clear_statements(self.store, "7d", T0)          # read before a newer clear commits
        events.clear(self.store, "all", T0 + 10)
        with self.assertRaises(sqlite3.IntegrityError):
            health.commit(self.store, stale)
        events.clear(self.store, "7d", T0 + 20)                        # a later, older-cutoff clear keeps the newest
        self.assertEqual(self.store.get(events.BOUNDARY, "cutoff")["cutoff"], T0 + 10)

    def test_malformed_options_are_refused_and_audited(self):
        for bad in ({}, [], 7):
            with self.assertRaises(ValueError):
                events.clear_post(self.api, {"older_than": bad, "confirm": "x"})
        self.assertEqual([a[1] for a in self.api.audits], [{"older_than": "[invalid]"}] * 3)

    def test_cli_clear_has_no_code(self):
        self.store.event("health", "info", "x", ts=T0 - 10)
        self.assertEqual(events.clear(self.store, "all", T0), 1)
        with self.assertRaises(ValueError):
            events.clear(self.store, "forever", T0)


class MirrorTests(Base):
    def test_incremental_and_idempotent(self):
        profile = {"profile": "qwen-replicas", "token": "[redacted]"}
        self.append(audit_line("up", T0 - 5, profile, job="a" * 32, state="accepted")
                    + audit_line("up", T0 - 4, profile, job="a" * 32, state="failed", error="Bearer secret-token at /x")
                    + audit_line("recipes/save", T0 - 3, profile, state="succeeded"))
        self.collect()
        self.assertEqual(self.mirrored(), [f"up profile qwen-replicas accepted (job {'a' * 8})",
                                           f"up profile qwen-replicas failed (job {'a' * 8})",
                                           "recipes/save profile qwen-replicas succeeded"])
        rows = self.store.events(limit=10)
        self.assertNotIn("secret-token", json.dumps(rows))
        self.assertEqual([r["severity"] for r in rows if r["category"] == "jobs"], ["warning", "info"])
        self.assertEqual(rows[-1]["ts"], T0 - 5)                    # the audit entry's own time
        self.collect()
        self.assertEqual(len(self.mirrored()), 3)
        self.append(audit_line("down", T0, job="b" * 32, state="succeeded"))
        self.collect()
        self.assertEqual(len(self.mirrored()), 4)

    def test_a_short_file_growing_is_not_a_rotation_and_truncation_restarts(self):
        self.append('{"when": "x", "action": "a"}\n')              # 30 bytes: shorter than the fingerprint length
        self.collect()
        self.append(audit_line("b", T0))
        self.collect()
        self.assertEqual(self.mirrored(), ["a", "b"])
        self.append(audit_line("c", T0), mode="w")                 # rotated: a new, shorter file
        self.collect()
        self.assertEqual(self.mirrored(), ["a", "b", "c"])

    def test_malformed_and_oversized_lines_are_skipped(self):
        self.append(audit_line("first", T0))
        self.collect()
        self.append("not json\n" + "{" + "x" * (events.LINE_CAP + 10) + "\n" + audit_line("ok", T0))
        self.collect()
        self.assertEqual(self.mirrored(), ["first", "ok"])
        self.append("y" * (events.READ_CAP + 1000) + "\n" + audit_line("after", T0))    # spans several reads
        for _ in range(3):
            self.collect()
        self.assertEqual(self.mirrored(), ["first", "ok", "after"])
        self.assertGreaterEqual(self.store.get(events.CURSOR, "audit")["skipped"], 2)

    def test_first_run_reads_only_the_recent_tail(self):
        self.append("".join(audit_line(f"old{i}", T0 - 100) for i in range(800)) + audit_line("newest", T0))
        self.collect()
        self.collect()                                              # at most 500 entries per run
        mirrored = self.mirrored()
        self.assertEqual(mirrored[-1], "newest")
        self.assertLess(len(mirrored), 800)

    def test_cleared_history_never_returns(self):
        self.append(audit_line("before", T0 - 100))
        self.clock.now = T0
        cutoff_events = events.clear(self.store, "all", T0)
        self.assertEqual(cutoff_events, 0)
        self.collect()                                              # the entry is older than the clear: skipped
        self.assertEqual(self.mirrored(), [])
        self.append(audit_line("after", T0 + 1))
        self.clock.now = T0 + 2
        self.collect()
        self.assertEqual(self.mirrored()[-1], "after")
        self.assertNotIn("before", self.mirrored())

    def test_a_batch_read_before_a_clear_is_refused(self):
        self.append(audit_line("x", T0))
        text = health.stored_text(self.store, events.BOUNDARY, "cutoff")
        events.clear(self.store, "all", T0 + 1)
        with self.assertRaises(sqlite3.IntegrityError):
            health.commit(self.store, [health.unchanged(events.BOUNDARY, "cutoff", text)])

    def test_only_the_lease_holder_mirrors(self):
        self.append(audit_line("x", T0))
        self.store.execute("UPDATE health_lease SET owner='other', until=?", (T0 + 1000,))
        events.collect(self.api, T0)
        self.assertEqual(self.mirrored(), [])

    def test_registration(self):
        from hyperloom.web import features
        reg = features.Registry()
        events.register(reg)
        self.assertEqual((list(reg.gets), list(reg.posts), reg.audited), (["events"], ["events/clear"], set()))


if __name__ == "__main__":
    unittest.main()
