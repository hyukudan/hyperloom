"""F45 settings, publishing rounds and ownership; the integrations document and the CLI."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import time
import unittest
from unittest import mock

from hyperloom import cli, config, mqtt, secrets, telemetry
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API, redact
from hyperloom.web.features import integrations

from tests.test_mqtt import Broker, parse_connect


NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Sampler:
    def __init__(self):
        self._stop = threading.Event()

    def snapshot(self):
        return {}


class FakeAPI:
    def __init__(self, store, clock):
        self.telemetry, self.clock = store, clock
        self.sampler = Sampler()
        self.audits = []
        self.machines = [{"id": "halo-1", "health": "online"}, {"id": "halo-2", "health": "offline"},
                         {"id": "bad id", "health": "online"}]

    def cluster(self):
        # Configured machines; their state comes from node_up samples written here as the fleet collector would.
        self.telemetry.describe("node_up", "state", interval=5, legend={"0": "down", "1": "up"})
        codes = {"online": 1, "offline": 0}
        self.telemetry.record([("node_up", {"machine": m["id"]}, codes[m["health"]]) for m in self.machines
                               if m["health"] in codes], ts=self.clock())
        # The real contract: Cluster.machines maps machine id -> Machine.
        return config.Cluster(machines={m["id"]: types.SimpleNamespace(id=m["id"]) for m in self.machines},
                              assets={}, profiles={}, router={}, pc={}, raw={})

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", **fields}

    def audit(self, action, arguments, result):
        self.audits.append((action, arguments, result))


class FakePublisher:
    def __init__(self, settings, password, alive, on_exit):
        self.settings, self.password, self.alive, self.on_exit = settings, password, alive, on_exit
        self.messages, self.started, self.stopped = [], False, False
        self.connects = 1

    def put(self, topic, payload, qos=0, retain=False, *, expires=None, done=None):
        self.messages.append({"topic": topic, "payload": payload if isinstance(payload, bytes) else payload.encode(),
                              "qos": qos, "retain": retain, "expires": expires, "done": done})

    def state(self):
        return {"connected": True, "connects": self.connects, "queued": len(self.messages), "running": not self.stopped,
                "published": 0, "dropped": 0}

    def start(self):
        self.started = True

    def stop(self, timeout=5.0):
        self.stopped = True
        return True


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(secrets, "_directory", return_value=Path(directory.name) / "secrets")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.api = FakeAPI(self.store, self.clock)
        self.addCleanup(integrations.stop_publisher, self.api)
        self.publishers = []

    def factory(self, settings, password, alive, on_exit, owner):
        publisher = FakePublisher(settings, password, alive, on_exit)
        publisher.owner = owner
        self.publishers.append(publisher)
        return publisher

    def enable(self, **extra):
        return integrations.save_audited(self.api, {"enabled": True, "host": "workstation", "topic_prefix": "lab",
                                                    "interval_s": 10, **extra})

    def collect(self):
        return integrations.collect(self.api, self.clock.now, factory=self.factory)


class SettingsTests(Base):
    def test_validation(self):
        bad = [{"enabled": "yes"}, {"host": "a b"}, {"host": "-x"}, {"enabled": True}, {"port": 0}, {"port": "1883"},
               {"topic_prefix": "a/+"}, {"topic_prefix": "/a"}, {"topic_prefix": "a/b/c/d/e"}, {"client_id": "x" * 22},
               {"interval_s": 4}, {"password": "pw"}, {"username": "u", "password": "pw", "clear_password": True},
               {"unknown": 1}, {"username": ""}]
        for arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                integrations.save_audited(self.api, arguments)
        self.assertTrue(all(result["state"] == "refused" for _, _, result in self.api.audits))

    def test_save_keeps_the_password_out_of_documents_and_audit(self):
        result = self.enable(username="lab", password="hunter2-pw")
        self.assertEqual(result["credential"], "set")
        saved = self.store.get(integrations.NAMESPACE, integrations.MQTT_KEY)
        self.assertEqual(mqtt.load_password(saved["credential_ref"]), "hunter2-pw")
        text = json.dumps([self.api.audits, result, self.store.get(integrations.NAMESPACE, integrations.MQTT_KEY)])
        self.assertNotIn("hunter2-pw", text)
        self.assertEqual(self.api.audits[-1][1]["credential"], "set")
        saved = self.store.get(integrations.NAMESPACE, integrations.MQTT_KEY)
        self.assertRegex(saved["credential_ref"], mqtt.SECRET_NAME)
        self.assertEqual((saved["port"], saved["revision"]), (None, 1))
        public = redact(integrations.public_settings(saved))
        self.assertEqual((public["credential_saved"], public["port"]), (True, 1883))
        # A password-only change is a new revision (the publisher reconnects with it); clearing removes the file.
        self.assertEqual(integrations.save_audited(self.api, {"clear_password": True})["credential"], "cleared")
        self.assertIsNone(mqtt.load_password(saved["credential_ref"]))
        self.assertEqual(self.store.get(integrations.NAMESPACE, integrations.MQTT_KEY)["revision"], 2)

    def test_test_publish_uses_its_own_client_id_and_no_will(self):
        self.enable(username="lab", password="pw", client_id="hyperloom-ab12")
        broker = Broker()
        self.addCleanup(broker.close)
        result = integrations.test_publish(self.api, {}, connect=broker.connect)
        broker.thread.join(2)
        self.assertTrue(result["ok"])
        connect = parse_connect(broker.packets[0][2])
        self.assertEqual((connect["client_id"], connect["flags"] & 0x04, connect["password"]), ("hyperloom-ab12-t", 0, b"pw"))
        self.assertEqual(broker.publishes()[0]["topic"], "lab/portal/test")
        with self.assertRaises(ValueError):
            integrations.test_publish(self.api, {"host": "x"})

        def refuse(address, timeout):
            raise ConnectionRefusedError("details with password=pw")
        result = integrations.test_publish(self.api, {}, connect=refuse)
        self.assertFalse(result["ok"])
        self.assertNotIn("pw", result["detail"].replace("password", ""))


class PublishingTests(Base):
    def test_disabled_does_nothing(self):
        self.assertEqual(self.collect(), [])
        self.assertEqual(self.publishers, [])

    def test_round_statuses_metrics_events_and_cursor(self):
        self.store.event("health", "warning", "old event")
        self.enable()
        self.store.describe("node_gpu_busy_percent", "gauge", unit="%", interval=5)
        self.store.describe("node_net_rx_bytes", "delta", interval=5)
        self.store.record([("node_gpu_busy_percent", {"machine": "halo-1", "device": "card0"}, 55),
                           ("node_gpu_busy_percent", {"machine": "halo-1", "device": "card0,x=y"}, 56),
                           ("node_net_rx_bytes", {"machine": "halo-1", "iface": "eth0"}, 10)], ts=NOW - 1)
        points = self.collect()
        self.assertEqual(points, [("integrations_mqtt_connected", {}, 1, "state"), ("integrations_mqtt_queue", {}, 3, "gauge")])
        publisher = self.publishers[0]
        self.assertEqual((publisher.settings["port"], publisher.started), (1883, True))
        topics = [(m["topic"], m["payload"], m["qos"], m["retain"]) for m in publisher.messages]
        self.assertEqual(topics[:2], [("lab/status/halo-1", b"1", 0, True), ("lab/status/halo-2", b"0", 0, True)])
        metrics = json.loads(publisher.messages[2]["payload"])
        self.assertEqual(publisher.messages[2]["topic"], "lab/metrics/halo-1")
        # Labels stay structured (no two series can collapse into one key); each value keeps its own sample time;
        # counters are not published as values.
        self.assertEqual(sorted((v["labels"]["device"], v["value"], v["ts"]) for v in metrics["values"]),
                         [("card0", 55.0, NOW - 1), ("card0,x=y", 56.0, NOW - 1)])
        self.assertEqual({v["series"] for v in metrics["values"]}, {"node_gpu_busy_percent"})
        # The message expires in the queue when its samples go stale (15 s after they were taken).
        self.assertLessEqual(publisher.messages[2]["expires"] - time.monotonic(), 14.5)
        self.assertFalse(any(m["topic"] == "lab/events" for m in publisher.messages))     # history is not replayed
        # New events go out at QoS 1 in id order; the cursor moves only over acknowledged ones.
        first = self.store.event("health", "critical", "GPU hot", machine="halo-1")
        second = self.store.event("alerts", "info", "sent")
        publisher.messages.clear()
        self.clock.now += 5
        self.collect()
        events = [m for m in publisher.messages if m["topic"] == "lab/events"]
        self.assertEqual([json.loads(m["payload"])["id"] for m in events], [first, second])
        self.assertEqual({m["qos"] for m in events}, {1})
        self.assertFalse(any(m["topic"].startswith("lab/status") for m in publisher.messages))   # unchanged
        cursor = integrations._cursor(self.store)[0]
        events[1]["done"]("sent")
        self.clock.now += 5
        publisher.messages.clear()
        self.collect()
        self.assertEqual(integrations._cursor(self.store)[0], cursor)   # first not acked yet
        self.assertFalse(any(m["topic"] == "lab/events" for m in publisher.messages))      # still tracked, not requeued
        events[0]["done"]("dropped")
        self.collect()
        self.assertEqual(integrations._cursor(self.store), (second, 1))
        self.assertEqual(integrations.publisher_state(self.api)["events_lost"], 1)

    def test_events_removed_by_retention_are_counted_as_lost(self):
        self.enable()
        self.collect()
        publisher = self.publishers[0]
        cursor = integrations._cursor(self.store)[0]
        ids = [self.store.event("health", "info", f"e{i}") for i in range(5)]
        self.store.execute("DELETE FROM events WHERE id IN (?, ?)", (ids[0], ids[1]))     # as retention would
        publisher.messages.clear()
        self.collect()
        sent = [m for m in publisher.messages if m["topic"] == "lab/events"]
        self.assertEqual([json.loads(m["payload"])["id"] for m in sent], ids[2:])
        self.assertEqual(cursor, ids[0] - 1)
        # Not counted before the cursor moves past the gap; a restart in between does not count it twice.
        self.assertEqual(integrations._cursor(self.store), (cursor, 0))
        integrations.save_audited(self.api, {"interval_s": 11})        # new revision: a new publisher re-reads
        self.collect()
        republished = [m for m in self.publishers[1].messages if m["topic"] == "lab/events"]
        self.assertEqual([json.loads(m["payload"])["id"] for m in republished], ids[2:])
        for message in republished:
            message["done"]("sent")
        self.collect()
        self.assertEqual(integrations._cursor(self.store), (ids[-1], 2))
        self.assertEqual(integrations.publisher_state(self.api)["events_lost"], 2)

    def test_unknown_states_removed_machines_and_stale_metrics_clear_retained_messages(self):
        self.enable()
        self.store.describe("node_load1", "gauge", interval=5)
        self.store.record([("node_load1", {"machine": "halo-1"}, 1.5)], ts=NOW)
        self.collect()
        publisher = self.publishers[0]
        self.assertEqual(sorted(self.store.get(integrations.RETAINED_NAMESPACE, "topics")),
                         ["lab/metrics/halo-1", "lab/status/halo-1", "lab/status/halo-2"])
        publisher.messages.clear()
        self.api.machines = [{"id": "halo-1", "health": None}, {"id": "halo-2", "health": "offline"}]
        self.clock.now += 20                                # metrics are stale now (well before the interval)
        self.collect()
        cleared = {m["topic"]: (m["payload"], m["qos"]) for m in publisher.messages if m["retain"]}
        self.assertEqual(cleared, {"lab/status/halo-1": (b"", 1), "lab/metrics/halo-1": (b"", 1)})
        for message in publisher.messages:
            message["done"] and message["done"]("sent")
        # A reconnect republishes every retained status; a machine that left the configuration is cleared too.
        publisher.messages.clear()
        publisher.connects = 2
        self.api.machines = [{"id": "halo-1", "health": "online"}]
        self.clock.now += 5
        self.collect()
        self.assertEqual([(m["topic"], m["payload"]) for m in publisher.messages],
                         [("lab/status/halo-1", b"1"), ("lab/status/halo-2", b"")])
        publisher.messages[1]["done"]("sent")
        self.collect()
        self.assertEqual(self.store.get(integrations.RETAINED_NAMESPACE, "topics"), ["lab/status/halo-1"])

    def test_fresh_publications_then_machine_removal(self):
        self.enable()
        self.store.describe("node_load1", "gauge", interval=5)
        self.store.record([("node_load1", {"machine": "halo-1"}, 1.0)], ts=NOW)
        self.collect()
        publisher = self.publishers[0]
        sent = {m["topic"]: m["payload"] for m in publisher.messages}
        self.assertEqual(sent["lab/status/halo-1"], b"1")
        self.assertTrue(sent["lab/metrics/halo-1"])
        publisher.messages.clear()
        self.api.machines = [{"id": "halo-2", "health": "offline"}]       # halo-1 left the configuration
        self.clock.now += 5
        self.store.record([("node_load1", {"machine": "halo-1"}, 1.0)], ts=self.clock.now)   # still fresh
        self.collect()
        cleared = sorted(m["topic"] for m in publisher.messages if m["payload"] == b"")
        self.assertEqual(cleared, ["lab/metrics/halo-1", "lab/status/halo-1"])

    def test_a_new_publisher_clears_what_an_earlier_one_left(self):
        self.store.put(integrations.RETAINED_NAMESPACE, "topics", ["old/status/halo-9", "lab/metrics/halo-2"])
        self.enable()
        self.collect()
        cleared = [m["topic"] for m in self.publishers[0].messages if m["payload"] == b""]
        self.assertEqual(cleared, ["lab/metrics/halo-2", "old/status/halo-9"])

    def test_a_failed_snapshot_changes_and_clears_nothing(self):
        self.enable()
        self.collect()
        publisher = self.publishers[0]
        publisher.messages.clear()
        self.api.cluster = lambda: (_ for _ in ()).throw(RuntimeError("no configuration"))
        self.clock.now += 5
        self.collect()
        self.assertFalse(any(m["topic"].startswith("lab/status") for m in publisher.messages))

    def test_settings_change_restarts_and_disable_stops(self):
        self.enable()
        self.collect()
        self.collect()
        self.assertEqual(len(self.publishers), 1)
        self.enable(topic_prefix="lab2")
        self.collect()
        self.assertEqual(len(self.publishers), 2)
        self.assertTrue(self.publishers[0].stopped)
        self.assertEqual(self.publishers[1].settings["topic_prefix"], "lab2")
        integrations.save_audited(self.api, {"enabled": False})
        self.assertEqual(self.collect(), [])
        self.assertTrue(self.publishers[1].stopped)
        self.assertIsNone(self.store.get(integrations.OWNER_NAMESPACE, "lease"))

    def test_alive_follows_the_sampler_and_renewal(self):
        self.enable()
        self.collect()
        alive = self.publishers[0].alive
        self.assertTrue(alive())
        self.api.sampler._stop.set()
        self.assertFalse(alive())

    def test_lease_cutoffs_follow_the_stored_expiry(self):
        self.enable()
        self.collect()
        state = integrations._running[id(self.api)]
        expires = self.store.get(integrations.OWNER_NAMESPACE, "lease")["expires"]
        remaining = state["lease_until"] - time.monotonic()
        self.assertAlmostEqual(remaining, expires - self.clock(), delta=0.5)
        publisher = self.publishers[0]
        self.assertTrue(publisher.alive() and publisher.owner())
        # Ordinary sends stop first; the final offline status still fits; then nothing may be written.
        state["lease_until"] = time.monotonic() + integrations.WORK_MARGIN_S - 0.1
        self.assertFalse(publisher.alive())
        self.assertTrue(publisher.owner())
        state["lease_until"] = time.monotonic() + integrations.LEASE_MARGIN_S - 0.1
        self.assertFalse(publisher.owner())

    def test_metrics_follow_configured_machines_and_sample_sets(self):
        self.enable(interval_s=300)
        self.store.describe("node_load1", "gauge", interval=5)
        self.store.describe("node_disk_used_bytes", "gauge", interval=60)
        self.store.record([("node_load1", {"machine": "halo-1"}, 1.0), ("node_disk_used_bytes", {"machine": "halo-1"}, 7),
                           ("node_load1", {"machine": "halo-9"}, 2.0)], ts=NOW)
        self.collect()
        publisher = self.publishers[0]
        self.assertEqual([m["topic"] for m in publisher.messages if "/metrics/" in m["topic"]], ["lab/metrics/halo-1"])
        publisher.messages.clear()
        self.clock.now += 20            # the load sample is stale, the disk sample is not: replaced at once
        self.collect()
        metrics = [json.loads(m["payload"]) for m in publisher.messages if m["topic"] == "lab/metrics/halo-1"]
        self.assertEqual([v["series"] for v in metrics[0]["values"]], ["node_disk_used_bytes"])

    def test_one_publishing_portal_per_store(self):
        self.enable()
        other = FakeAPI(self.store, self.clock)
        self.addCleanup(integrations.stop_publisher, other)
        self.collect()
        self.assertEqual(integrations.collect(other, self.clock.now, factory=self.factory), [])
        self.assertEqual(len(self.publishers), 1)
        document = integrations.state(other, {})
        self.assertEqual(document["why"]["publisher"], "another portal process publishes")
        self.clock.now += integrations.LEASE_S + 1            # the first portal stopped renewing: taken over
        integrations.collect(other, self.clock.now, factory=self.factory)
        self.assertEqual(len(self.publishers), 2)
        self.collect()
        self.assertTrue(self.publishers[0].stopped)


class PayloadTests(unittest.TestCase):
    def test_metrics_payload_stays_within_the_message_limit(self):
        values = [{"series": "node_x", "labels": {"sensor": str(i) * 60}, "value": i, "ts": NOW} for i in range(6000)]
        payload = json.loads(integrations.metrics_payload("halo-1", NOW, values))
        self.assertLessEqual(len(json.dumps(payload)), integrations.METRICS_PAYLOAD_MAX)
        self.assertEqual(len(payload["values"]) + payload["omitted"], 6000)


class DocumentTests(Base):
    def test_state_document(self):
        reg = features.Registry()
        integrations.register(reg)
        features.reset(reg)
        self.addCleanup(features.reset)
        self.enable(username="lab", password="secret-pw")
        document = redact(integrations.state(self.api, {}))
        self.assertNotIn("secret-pw", json.dumps(document))
        self.assertEqual(document["mqtt"]["settings"]["credential_saved"], True)
        self.assertEqual(document["mqtt"]["topics"][0]["topic"], "lab/portal/status")
        self.assertEqual(document["stream"]["max_clients"], 4)
        self.assertEqual([t["document"] for t in document["export"]["targets"]], ["devices", "stats"])
        self.assertEqual(document["why"]["publisher"], "starting")
        self.api.telemetry = None
        self.assertIsNone(integrations.state(self.api, {})["mqtt"])
        with self.assertRaises(ValueError):
            integrations.state(self.api, {"x": "1"})

    def test_registration(self):
        reg = features.Registry()
        integrations.register(reg)
        self.assertEqual(sorted(reg.posts), ["integrations/mqtt/save", "integrations/mqtt/test"])
        self.assertFalse(reg.audited)                 # audited by the handlers, without the password
        self.assertIn("integrations/export/", reg.prefixes)
        self.assertEqual(reg.collectors["mqtt"][1], integrations.COLLECTOR_INTERVAL_S)


class CLITests(unittest.TestCase):
    def test_cli_calls_the_same_functions(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(telemetry, "_stores", {}) as opened:
            config = Path(directory) / "cluster.json"
            # Nothing may reach the machine's real configuration directory, audit log or secret files.
            with mock.patch.object(secrets, "_directory", return_value=Path(directory) / "secrets"), \
                    mock.patch.object(cli, "config_path", return_value=config):
                with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch("sys.stdin", io.StringIO("cli-pw\n")):
                    code = cli.main(["integrations", "mqtt-save", "--enable", "--host", "workstation", "--username",
                                     "u", "--password-stdin", "--prefix", "lab", "--config", str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue())["credential"], "set")
                saved = Store(Path(directory) / "telemetry.sqlite3")
                reference = saved.get(integrations.NAMESPACE, integrations.MQTT_KEY)["credential_ref"]
                saved.close()
                self.assertEqual(mqtt.load_password(reference), "cli-pw")
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main(["integrations", "mqtt-save", "--port", "0", "--config", str(config)]), 2)
                self.assertIn("port", err.getvalue())
            audit = [json.loads(line) for line in (Path(directory) / "audit.jsonl").read_text().splitlines()]
            self.assertEqual([entry["result"]["state"] for entry in audit], ["succeeded", "refused"])
            self.assertEqual(audit[0]["arguments"]["credential"], "set")
            self.assertNotIn("cli-pw", json.dumps(audit))
            for store in opened.values():
                self.assertEqual(store.get(integrations.NAMESPACE, integrations.MQTT_KEY)["host"], "workstation")
                store.close()


if __name__ == "__main__":
    unittest.main()
