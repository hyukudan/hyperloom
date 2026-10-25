"""Alert notifications (F21): channel URLs in the secret store, transition planning, ordered delivery with retries,
no redirects, total deadlines, and URLs that never reach documents, audits or events."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import secrets
from hyperloom.telemetry.store import Store
from hyperloom.web.features import alerts, health
from tests.test_health_engine import Clock, FakeAPI, T0

LOOPBACK = str(ipaddress.IPv4Address(0x7F000001))      # built at run time
LOOPBACK6 = str(ipaddress.IPv6Address(1))
HOST = ".".join(("hooks", "example", "test"))           # a reserved test name, built at run time
LOCAL_NAME = "local" + "host"
USERINFO = ":".join(("someone", "pw"))
TOKEN = "k" + "9" * 20                                   # a stand-in for a topic secret in a URL path


class Receiver:
    """A loopback HTTP receiver: records bodies; behaviour per path (status, delay, redirect)."""

    def __init__(self):
        received = self.received = []
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.path, self.headers.get("Content-Type"), body))
                if outer.delay:
                    time.sleep(outer.delay)
                if self.path.startswith("/moved"):
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                elif self.path.startswith("/fail"):
                    self.send_response(500)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                try:
                    self.wfile.write(b"ok")
                except OSError:              # the sender gave up (deadline): expected in those tests
                    pass

        self.server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://{LOOPBACK}:{self.server.server_address[1]}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(secrets, "_directory", lambda: Path(self.dir.name) / "secrets")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.api = FakeAPI(self.store, self.clock, self.dir.name)
        self.addCleanup(self.api.admission_db.close)
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)

    def channel(self, path="/hook/" + TOKEN, **fields):
        return alerts.save_channel(self.store, {"name": "lab", "kind": "webhook", "url": self.receiver.url(path),
                                                "min_severity": "warning", **fields}, self.clock.now)

    def finding(self, fid="gpu-hot:halo-1/card0", severity="critical", occurrence="o1", state="firing"):
        return {"id": fid, "rule": fid.split(":")[0], "title": "GPU hot", "severity": severity, "state": state,
                "detail": "halo-1 card0 at 92 °C", "hint": "h", "machine": "halo-1", "since": T0, "occurrence": occurrence}

    def plan(self, transitions, states):
        with alerts.BOOK_LOCK:
            health.commit(self.store, alerts.plan(self.store, transitions, states, self.clock.now))

    def outbox(self):
        return [(c, [(i["state"], i["id"]) for i in json.loads(items)])
                for c, items in self.store.execute("SELECT channel, items FROM alerts_outbox ORDER BY id")]

    def deliver(self, **kw):
        health.acquire(self.store, health.owner_of(self.api), self.clock.now)
        return alerts.deliver(self.api, self.clock.now, self.store, **kw)


class URLTests(Base):
    def test_rules(self):
        for good in (f"https://{HOST}/a?b=1", f"http://{LOOPBACK}:9/x", f"http://[{LOOPBACK6}]:8080/t"):
            self.assertEqual(alerts.check_url(good), good)
        for bad in (f"http://{HOST}/a", f"ftp://{HOST}/y", f"https://{USERINFO}@{HOST}/", f"https://{HOST}/a#f",
                    f"https://{HOST}/a b", f"https://{HOST}/\n", "https://" + "a" * 2050, "https:///path", "", None,
                    f"http://{LOCAL_NAME}/x", f"https://{HOST}:0/"):
            with self.assertRaises(ValueError, msg=bad) as caught:
                alerts.check_url(bad)
            if isinstance(bad, str) and bad:
                self.assertNotIn(bad, str(caught.exception))
        self.assertEqual(alerts.mask(f"https://{HOST}:8443/" + TOKEN + "?k=" + TOKEN), f"https://{HOST}:8443/…")

    def test_url_lives_only_in_the_secret_file(self):
        record = self.channel()
        stored = self.store.get(alerts.CHANNELS, record["id"])
        path = Path(self.dir.name) / "secrets" / "alert-urls" / f"{record['id']}.{stored['url_version']}"
        self.assertTrue(path.is_file())
        self.assertEqual(alerts.load_url(record["id"], stored["url_version"]), self.receiver.url("/hook/" + TOKEN))
        self.assertNotIn(TOKEN, json.dumps(self.store.items(alerts.CHANNELS)))
        document = alerts.document(self.api, {})
        self.assertNotIn(TOKEN, json.dumps(document))
        self.assertEqual(document["channels"][0]["url_masked"], f"http://{LOOPBACK}:{self.receiver.server.server_address[1]}/…")
        for bad in (("../x", "0" * 16), (record["id"], "../../x")):
            with self.assertRaises(ValueError):
                alerts.load_url(*bad)
        alerts.delete_channel(self.store, record["id"])
        self.assertFalse(path.exists())

    def test_channel_validation_and_audits_never_echo_the_url(self):
        result = alerts.channels_post(self.api, {"name": "lab", "kind": "push", "url": self.receiver.url("/t/" + TOKEN)})
        self.assertEqual(result["min_severity"], "warning")
        bad = [{"name": "x", "kind": "push", "url": f"http://{HOST}/" + TOKEN}, {"name": "", "kind": "push", "url": self.receiver.url("/")},
               {"name": "x", "kind": "sms", "url": self.receiver.url("/")}, {"name": "x", "kind": "push"},
               {"id": "ch-00000000", "name": "x"}, {"name": "x", "kind": "push", "url": self.receiver.url("/"), "extra": 1},
               {"name": "x", "kind": "push", "url": self.receiver.url("/"), "reminders": "2h"}]
        for arguments in bad:
            with self.assertRaises((ValueError, LookupError), msg=arguments):
                alerts.channels_post(self.api, arguments)
        text = json.dumps(self.api.audits)
        self.assertNotIn(TOKEN, text)
        self.assertIn("[not shown]", text)
        self.assertEqual([a[2]["state"] for a in self.api.audits], ["succeeded"] + ["refused"] * len(bad))
        updated = alerts.channels_post(self.api, {"id": result["id"], "min_severity": "critical"})
        self.assertEqual((updated["min_severity"], updated["url_masked"]), ("critical", result["url_masked"]))
        for odd in ({"reminders": {}}, {"reminders": []}, {"min_severity": {}}, {"kind": []}):
            with self.assertRaises(ValueError):
                alerts.channels_post(self.api, {"name": "x", "kind": "push", "url": self.receiver.url("/"), **odd})
        self.assertEqual(self.api.audits[-1][2]["state"], "refused")
        with self.assertRaises(ValueError) as caught:
            alerts.channels_post(self.api, {"name": "x", "kind": "push", "url": self.receiver.url("/"), "password=" + TOKEN: 1})
        self.assertNotIn(TOKEN, str(caught.exception) + json.dumps(self.api.audits))

    def test_a_save_that_does_not_commit_leaves_the_published_url_alone(self):
        record = self.channel()
        before = self.store.get(alerts.CHANNELS, record["id"])
        directory = Path(self.dir.name) / "secrets" / "alert-urls"
        always_changed = lambda namespace, value: ("INSERT INTO health_guard(x) VALUES (1)", ())
        with mock.patch.object(alerts.health, "same_version", always_changed):
            with self.assertRaises(PermissionError):
                alerts.save_channel(self.store, {"id": record["id"], "url": self.receiver.url("/other")}, self.clock.now)
        self.assertEqual(self.store.get(alerts.CHANNELS, record["id"]), before)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [f"{record['id']}.{before['url_version']}"])
        self.assertEqual(alerts.load_url(record["id"], before["url_version"]), self.receiver.url("/hook/" + TOKEN))
        self.clock.now += 1
        alerts.save_channel(self.store, {"id": record["id"], "url": self.receiver.url("/new")}, self.clock.now)
        after = self.store.get(alerts.CHANNELS, record["id"])
        self.assertNotEqual(after["url_version"], before["url_version"])
        self.assertEqual(alerts.load_url(record["id"], after["url_version"]), self.receiver.url("/new"))

    def test_concurrent_saves_through_separate_connections_commit_one_at_a_time(self):
        path = Path(self.dir.name) / "telemetry.sqlite3"
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        record = alerts.save_channel(first, {"name": "lab", "kind": "webhook", "url": self.receiver.url("/a")}, self.clock.now)
        text = alerts.health.stored_text(second, alerts.CHANNELS, record["id"])
        self.clock.now += 1
        alerts.save_channel(first, {"id": record["id"], "name": "renamed"}, self.clock.now)
        alerts.prepare(second)
        with self.assertRaises(sqlite3.IntegrityError):        # a commit based on the older read is refused
            alerts.health.commit(second, [alerts.health.unchanged(alerts.CHANNELS, record["id"], text)])
        self.assertEqual(second.get(alerts.CHANNELS, record["id"])["name"], "renamed")


class PlanTests(Base):
    def test_transitions_become_one_ordered_message_per_channel(self):
        warn = self.channel()
        crit = self.channel(min_severity="critical")
        a = self.finding(severity="warning")
        self.plan([("appeared", a)], {a["id"]: a})
        self.assertEqual(self.outbox(), [(warn["id"], [("firing", a["id"])])])       # below the critical channel
        b = {**a, "severity": "critical"}
        self.plan([("escalated", b)], {a["id"]: b})
        self.assertEqual(sorted(self.outbox()[1:]), sorted([(warn["id"], [("escalated", a["id"])]), (crit["id"], [("firing", a["id"])])]))
        self.plan([("cleared", b)], {})
        self.assertEqual(sorted(self.outbox()[3:]), sorted([(warn["id"], [("resolved", a["id"])]), (crit["id"], [("resolved", a["id"])])]))
        self.assertTrue(all(mark["resolving"] for _, _, mark in self.store.items(alerts.TRACK)))   # kept until delivered
        for _ in range(4):
            self.deliver()
        self.assertEqual(self.store.items(alerts.TRACK), [])

    def test_refire_while_outstanding_keeps_every_transition_in_order(self):
        channel = self.channel()
        a1, a2 = self.finding(occurrence="o1"), self.finding(occurrence="o2")
        self.plan([("appeared", a1)], {a1["id"]: a1})
        self.plan([("cleared", a1)], {})
        self.plan([("appeared", a2)], {a2["id"]: a2})
        self.plan([("cleared", a2)], {})
        self.assertEqual([items for _, items in self.outbox()], [[("firing", a1["id"])], [("resolved", a1["id"])],
                                                                 [("firing", a1["id"])], [("resolved", a1["id"])]])
        for _ in range(4):
            self.deliver()
        self.assertEqual([json.loads(body)["items"][0]["state"] for _, _, body in self.receiver.received],
                         ["firing", "resolved", "firing", "resolved"])
        self.assertEqual(channel["kind"], "webhook")

    def test_reminders_count_from_delivery(self):
        self.channel(reminders="1h")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.clock.now += 7200
        self.plan([], {a["id"]: a})                       # never delivered yet: no reminder
        self.assertEqual(len(self.outbox()), 1)
        self.deliver()
        self.clock.now += 3000
        self.plan([], {a["id"]: a})
        self.assertEqual(self.outbox(), [])
        self.clock.now += 700
        self.plan([], {a["id"]: a})
        self.plan([], {a["id"]: a})                       # one reminder queued at a time
        self.assertEqual([items for _, items in self.outbox()], [[("reminder", a["id"])]])

    def test_new_channels_catch_up_and_removed_channels_lose_their_bookkeeping(self):
        a = self.finding()
        self.store.put(health.STATE, a["id"], a)
        channel = self.channel()
        self.assertEqual(self.outbox(), [(channel["id"], [("firing", a["id"])])])
        alerts.delete_channel(self.store, channel["id"])
        self.assertEqual((self.outbox(), self.store.items(alerts.TRACK)), ([], []))

    def test_a_channel_change_during_planning_aborts_the_commit(self):
        self.channel()
        a = self.finding()
        statements = alerts.plan(self.store, [("appeared", a)], {a["id"]: a}, self.clock.now)
        self.clock.now += 1
        self.channel(path="/other")
        with self.assertRaises(sqlite3.IntegrityError):
            health.commit(self.store, statements)

    def test_a_same_clock_settings_change_also_aborts_the_commit(self):
        record = self.channel()
        a = self.finding(severity="warning")
        statements = alerts.plan(self.store, [("appeared", a)], {a["id"]: a}, self.clock.now)
        alerts.save_channel(self.store, {"id": record["id"], "min_severity": "critical"}, self.clock.now)   # same clock
        with self.assertRaises(sqlite3.IntegrityError):
            health.commit(self.store, statements)

    def test_overflow_drops_a_whole_channel_backlog_and_resyncs_it(self):
        busy, quiet = self.channel(path="/busy"), self.channel(path="/quiet")
        a, b = self.finding(occurrence="o1"), self.finding("disk-full:halo-2/disk:/", "warning", "o2")
        self.plan([("appeared", a)], {a["id"]: a})                  # [firing a] for both channels
        self.plan([("cleared", a)], {})                              # [resolved a] for both
        filler = [("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) VALUES (?, ?, ?, ?, 0, '[]')",
                   (f"x{i}", quiet["id"], T0, T0)) for i in range(alerts.MAX_OUTBOX - 4)]
        health.commit(self.store, filler)
        self.plan([("appeared", b)], {b["id"]: b})                  # 102 rows: the oldest owner's backlog goes
        rows = self.outbox()
        self.assertLessEqual(len(rows), alerts.MAX_OUTBOX)
        victim = busy["id"] if rows[0][0] != busy["id"] else quiet["id"]
        remaining = [items for channel, items in rows if channel == victim]
        self.assertEqual(remaining, [])                              # no orphan resolve without its firing
        self.assertIn("dropped the undelivered backlog of channel " + victim, self.store.events(category="alerts")[0]["message"])
        self.plan([], {b["id"]: b})                                  # the resync reconciles from the bookkeeping
        # a was never sent to it (firing and resolve both dropped): forgotten; b is active: sent again
        self.assertEqual([items for channel, items in self.outbox() if channel == victim], [[("firing", b["id"])]])
        self.assertEqual(self.store.items(alerts.RESYNC), [])

    def test_a_resolve_dropped_while_its_firing_is_in_flight_is_sent_after_it(self):
        channel = self.channel()
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        health.acquire(self.store, health.owner_of(self.api), self.clock.now)
        claimed = {}

        def sender(*args, **kwargs):                  # while the firing is on the wire, the finding clears and the
            self.plan([("cleared", a)], {})           # outbox overflows: the unclaimed resolve is dropped
            filler = [("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) VALUES "
                       "(?, ?, ?, ?, 0, '[]')", (f"f{i}", "ch-ffffffff", T0 + 1, T0 + 1)) for i in range(alerts.MAX_OUTBOX)]
            health.commit(self.store, filler + alerts.bound(self.clock.now))
            claimed["left"] = [items for c, items in self.outbox() if c == channel["id"]]
            return {"ok": True, "status": 200}
        alerts.deliver(self.api, self.clock.now, self.store, sender=sender)
        self.assertEqual(claimed["left"], [[("firing", a["id"])]])     # only the claimed firing survived
        self.store.execute("DELETE FROM alerts_outbox WHERE channel='ch-ffffffff'")
        self.plan([], {})                                      # resync: the delivered occurrence gets its resolve
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [[("resolved", a["id"])]])

    def test_overflow_with_many_channels_restores_the_bound(self):
        made = [self.channel(path=f"/c{i}") for i in range(alerts.MAX_CHANNELS)]
        filler = [("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) VALUES (?, ?, ?, ?, 0, '[]')",
                   (f"x{i}", made[i % len(made)]["id"], T0, T0)) for i in range(alerts.MAX_OUTBOX)]
        health.commit(self.store, filler)
        findings = {f"disk-full:m{i}/disk:/": self.finding(f"disk-full:m{i}/disk:/", "warning", f"o{i}") for i in range(3)}
        self.plan([("appeared", f) for f in findings.values()], findings)
        self.assertLessEqual(self.store.execute("SELECT COUNT(*) FROM alerts_outbox")[0][0], alerts.MAX_OUTBOX)

    def test_concurrent_creates_respect_the_channel_cap(self):
        path = Path(self.dir.name) / "telemetry.sqlite3"
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        for i in range(alerts.MAX_CHANNELS - 1):
            alerts.save_channel(first, {"name": f"c{i}", "kind": "push", "url": self.receiver.url("/x")}, self.clock.now)
        original, raced = alerts.catch_up, []

        def racing(store, record, states, now):
            if not raced:
                raced.append(1)
                alerts.save_channel(second, {"name": "late", "kind": "push", "url": self.receiver.url("/y")}, now)
            return original(store, record, states, now)
        with mock.patch.object(alerts, "catch_up", racing), self.assertRaises(PermissionError):
            alerts.save_channel(first, {"name": "early", "kind": "push", "url": self.receiver.url("/z")}, self.clock.now)
        self.assertEqual(len(alerts.channels(first)), alerts.MAX_CHANNELS)

    def fill_and_overflow(self, count=alerts.MAX_OUTBOX):
        filler = [("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) VALUES "
                   "(?, ?, ?, ?, 0, '[]')", (f"f{i}", "ch-ffffffff", T0 + 1, T0 + 1)) for i in range(count)]
        health.commit(self.store, filler + alerts.bound(self.clock.now))
        self.store.execute("DELETE FROM alerts_outbox WHERE channel='ch-ffffffff'")

    def test_a_dropped_escalation_is_sent_again(self):
        channel = self.channel(reminders="none")
        a = self.finding(severity="warning")
        self.plan([("appeared", a)], {a["id"]: a})
        self.deliver()                                                # warning delivered
        b = {**a, "severity": "critical"}
        self.plan([("escalated", b)], {a["id"]: b})
        self.fill_and_overflow()                                      # the escalation is dropped
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [])
        self.plan([], {a["id"]: b})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [[("escalated", a["id"])]])
        self.deliver()
        self.plan([], {a["id"]: b})                                   # recovered once: nothing more
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [])

    def test_an_occurrence_the_channel_never_saw_is_forgotten_not_resolved(self):
        channel = self.channel()
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.plan([("cleared", a)], {})
        self.fill_and_overflow()                                      # firing and resolve both dropped, never sent
        self.plan([], {})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [])
        self.assertEqual(self.store.items(alerts.TRACK), [])

    def test_an_attempted_firing_gets_its_resolve_after_a_drop(self):
        channel = self.channel(path="/fail/x")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.deliver()                                                # attempted (HTTP 500): the receiver may have it
        self.plan([("cleared", a)], {})
        self.fill_and_overflow()
        self.plan([], {})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [[("resolved", a["id"])]])

    def test_two_successive_drops_still_deliver_the_resolve(self):
        channel = self.channel(path="/fail/x")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.deliver()                                                # attempted: the receiver may have the firing
        self.plan([("cleared", a)], {})
        self.fill_and_overflow()
        self.plan([], {})                                             # compensating resolve queued
        self.fill_and_overflow()                                      # and dropped again
        self.plan([], {})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [[("resolved", a["id"])]])

    def test_a_crash_after_sending_before_the_acknowledgement_keeps_the_resolve(self):
        channel = self.channel()
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})

        def crash(*args, **kwargs):
            raise KeyboardInterrupt            # the receiver got the firing; this process dies before acknowledging
        health.acquire(self.store, health.owner_of(self.api), self.clock.now)
        with self.assertRaises(KeyboardInterrupt):
            alerts.deliver(self.api, self.clock.now, self.store, sender=crash)
        self.clock.now += alerts.CLAIM_S + 1                       # the claim expires; a new evaluator runs
        self.plan([("cleared", a)], {})
        self.fill_and_overflow()                                   # firing (expired claim) and resolve dropped
        self.plan([], {})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [[("resolved", a["id"])]])

    def test_a_catch_up_firing_dropped_unsent_leaves_no_orphan_resolve(self):
        channel = self.channel(min_severity="critical")
        w = self.finding(severity="warning")
        self.store.put(health.STATE, w["id"], w)
        self.fill_and_overflow(1)                                   # nothing to drop yet
        alerts.save_channel(self.store, {"id": channel["id"], "min_severity": "warning"}, self.clock.now)   # catch-up
        self.plan([("cleared", w)], {})                              # resolve queued behind the unsent firing
        self.fill_and_overflow()                                     # both dropped, never attempted
        self.plan([], {})
        self.assertEqual([items for c, items in self.outbox() if c == channel["id"]], [])
        self.assertEqual(self.store.items(alerts.TRACK), [])

    def test_the_bound_holds_for_writers_on_separate_connections(self):
        path = Path(self.dir.name) / "telemetry.sqlite3"
        first, second = Store(path, clock=self.clock), Store(path, clock=self.clock)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        alerts.prepare(first)
        alerts.prepare(second)
        a = self.finding()
        first.put(health.STATE, a["id"], a)
        alerts.health.commit(first, [("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) "
                                      "VALUES (?, 'ch-00000000', ?, ?, 0, '[]')", (str(i), T0, T0)) for i in range(99)])
        alerts.save_channel(first, {"name": "a", "kind": "push", "url": self.receiver.url("/a")}, self.clock.now)
        alerts.save_channel(second, {"name": "b", "kind": "push", "url": self.receiver.url("/b")}, self.clock.now)
        self.assertLessEqual(first.execute("SELECT COUNT(*) FROM alerts_outbox")[0][0], alerts.MAX_OUTBOX)
        self.assertEqual(first.execute("SELECT COUNT(*) FROM alerts_outbox WHERE channel='ch-00000000'")[0][0], 0)

    def test_a_dropped_reminder_is_re_armed(self):
        channel = self.channel(reminders="1h")
        a = self.finding()
        key = alerts.track_key(channel["id"], a["id"], a["occurrence"])
        self.store.put(alerts.TRACK, key, {"occurrence": a["occurrence"], "severity": "critical", "delivered_at": T0 - 7200,
                                           "reminded_at": None, "reminder_queued": "gone-message", "message": "m"})
        self.plan([], {a["id"]: a})
        self.assertEqual([items for _, items in self.outbox()], [[("reminder", a["id"])]])


class DeliveryTests(Base):
    def test_success_retries_and_redirects_are_logged_without_the_url(self):
        ok, moved, failing = self.channel(), self.channel(path="/moved/" + TOKEN), self.channel(path="/fail/" + TOKEN)
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.deliver()
        paths = [path for path, _, _ in self.receiver.received]
        self.assertEqual(sorted(p.split("/")[1] for p in paths), ["fail", "hook", "moved"])      # nothing followed the redirect
        body = json.loads(next(b for p, _, b in self.receiver.received if p.startswith("/hook")))
        self.assertEqual((body["schema"], body["kind"], body["items"][0]["state"]), ("hyperloom/alert-v1", "alert", "firing"))
        self.assertTrue(body["message_id"])
        events = self.store.events(category="alerts")
        self.assertNotIn(TOKEN, json.dumps(events))
        messages = " | ".join(e["message"] for e in events)
        self.assertIn("redirect refused", messages)
        self.assertIn("HTTP 500", messages)
        self.assertIn("next in 5 s", messages)
        rows = dict(self.store.execute("SELECT channel, attempts FROM alerts_outbox"))
        self.assertEqual(rows, {moved["id"]: 1, failing["id"]: 1})
        mark = self.store.get(alerts.TRACK, alerts.track_key(ok["id"], a["id"], a["occurrence"]))
        self.assertEqual(mark["delivered_at"], self.clock.now)
        self.receiver.received.clear()
        self.deliver()                                     # not due yet
        self.assertEqual(self.receiver.received, [])
        for wait, attempt in ((5, 2), (20, 3), (60, 4), (300, 5), (300, 6)):
            self.clock.now += wait
            self.deliver()
            self.assertEqual(dict(self.store.execute("SELECT channel, attempts FROM alerts_outbox"))[failing["id"]], attempt)
        message_ids = {json.loads(b)["message_id"] for p, _, b in self.receiver.received if p.startswith("/fail")}
        self.assertEqual(len(message_ids), 1)             # the same message id on every retry

    def test_at_most_three_sends_per_run(self):
        for i in range(5):
            self.channel(path=f"/s{i}")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        sent = []
        self.deliver(sender=lambda *args, **kw: sent.append(1) or {"ok": True, "status": 200})
        self.assertEqual(len(sent), alerts.RUN_SENDS)

    def test_a_later_message_waits_for_the_earlier_one(self):
        self.channel(path="/fail/x")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.plan([("cleared", a)], {})
        for _ in range(3):
            self.deliver()
            self.clock.now += 30
        self.assertEqual({json.loads(b)["items"][0]["state"] for _, _, b in self.receiver.received}, {"firing"})

    def test_push_channels_post_text(self):
        self.channel(kind="push")
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        self.deliver()
        _, content_type, body = self.receiver.received[0]
        self.assertTrue(content_type.startswith("text/plain"))
        first, second = body.decode().split("\n")[:2]
        self.assertTrue(first.startswith("hyperloom alert ") and len(first.split()[-1]) == 32)    # the stable message id
        self.assertTrue(second.startswith("FIRING · critical · GPU hot"))

    def test_total_deadline_against_a_slow_receiver(self):
        self.receiver.delay = 3.0
        started = time.monotonic()
        outcome = alerts.send(self.receiver.url("/hook"), "webhook", "alert", "m", [], T0, timeout=0.8)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(outcome, {"ok": False, "error": "no answer within 0.8 s"})

    def test_deadline_before_the_connection_exists_sends_nothing(self):
        connects = []

        def slow_lookup(*args, **kwargs):
            time.sleep(2.0)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (LOOPBACK, self.receiver.server.server_address[1]))]

        real_connect = socket.socket.connect
        with mock.patch.object(socket, "getaddrinfo", slow_lookup), \
                mock.patch.object(socket.socket, "connect", lambda s, a: connects.append(a) or real_connect(s, a)):
            started = time.monotonic()
            outcome = alerts.send(f"https://{HOST}/x", "webhook", "alert", "m", [], T0, timeout=0.5)
            self.assertLess(time.monotonic() - started, 1.5)
            time.sleep(2.0)                                # the lookup finishes later: still nothing is sent
        self.assertEqual(outcome["error"], "no answer within 0.5 s")
        self.assertEqual((connects, self.receiver.received), ([], []))

    def test_refused_delete_and_test_are_audited_without_client_text(self):
        record = self.channel()
        for post in (alerts.delete_post, alerts.test_post):
            with self.assertRaises(ValueError):
                post(self.api, {"id": record["id"], "password=" + TOKEN: 1})
        self.assertEqual([(a[0], a[2]["state"]) for a in self.api.audits],
                         [("alerts/channels/delete", "refused"), ("alerts/test", "refused")])
        self.assertNotIn(TOKEN, json.dumps(self.api.audits))

    def test_sends_are_bounded_and_a_busy_test_is_refused(self):
        record = self.channel()
        self.assertTrue(alerts._SENDS.acquire(blocking=False))
        self.assertTrue(alerts._SENDS.acquire(blocking=False))
        try:
            with self.assertRaises(PermissionError):
                alerts.test_channel(self.api, self.store, record["id"], self.clock.now)
        finally:
            alerts._SENDS.release()
            alerts._SENDS.release()
        result = alerts.test_post(self.api, {"id": record["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(self.receiver.received[-1][2])["kind"], "test")
        self.assertIn("test alert", self.store.events(category="alerts")[0]["message"])

    def test_only_the_lease_holder_delivers(self):
        self.channel()
        a = self.finding()
        self.plan([("appeared", a)], {a["id"]: a})
        health.prepare(self.store)
        health.acquire(self.store, "another-process", self.clock.now)
        alerts.deliver(self.api, self.clock.now, self.store)
        self.assertEqual(self.receiver.received, [])

    def test_end_to_end_from_a_finding(self):
        self.channel()
        self.store.describe("node_gpu_temp_celsius", "gauge", interval=5)
        self.store.put(health.CONFIG, "gpu-hot", {"hold_s": 0, "clear_s": 0})
        self.store.record([("node_gpu_temp_celsius", {"machine": "halo-1", "device": "card0"}, 95)], ts=self.clock.now)
        health.evaluate(self.api, self.clock.now, self.store)
        alerts.deliver(self.api, self.clock.now, self.store)
        self.clock.now += 10
        self.store.record([("node_gpu_temp_celsius", {"machine": "halo-1", "device": "card0"}, 60)], ts=self.clock.now)
        health.evaluate(self.api, self.clock.now, self.store)
        alerts.deliver(self.api, self.clock.now, self.store)
        states = [json.loads(b)["items"][0]["state"] for _, _, b in self.receiver.received]
        self.assertEqual(states, ["firing", "resolved"])

    def test_registration(self):
        from hyperloom.web import features
        reg = features.Registry()
        alerts.register(reg)
        self.assertEqual(sorted(reg.posts), ["alerts/channels", "alerts/channels/delete", "alerts/test"])
        self.assertFalse(reg.audited)            # the handlers audit with the URL masked


if __name__ == "__main__":
    unittest.main()
