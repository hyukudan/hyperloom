"""MQTT 3.1.1 client and publisher against an in-test broker on a socket pair."""

import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hyperloom import mqtt, secrets


def read_packet(sock):
    def exact(size):
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data
    first = exact(1)[0]
    size = mqtt.decode_length(exact)
    return first >> 4, first & 0x0F, exact(size) if size else b""


def parse_string(body, offset):
    size = struct.unpack("!H", body[offset:offset + 2])[0]
    return body[offset + 2:offset + 2 + size], offset + 2 + size


def parse_connect(body):
    name, offset = parse_string(body, 0)
    level, flags = body[offset], body[offset + 1]
    keepalive = struct.unpack("!H", body[offset + 2:offset + 4])[0]
    offset += 4
    client_id, offset = parse_string(body, offset)
    result = {"name": name, "level": level, "flags": flags, "keepalive": keepalive, "client_id": client_id.decode()}
    if flags & 0x04:
        result["will_topic"], offset = parse_string(body, offset)
        result["will_message"], offset = parse_string(body, offset)
    if flags & 0x80:
        result["username"], offset = parse_string(body, offset)
    if flags & 0x40:
        result["password"], offset = parse_string(body, offset)
    return result


def parse_publish(flags, body):
    topic, offset = parse_string(body, 0)
    qos = (flags >> 1) & 3
    packet_id = None
    if qos:
        packet_id = struct.unpack("!H", body[offset:offset + 2])[0]
        offset += 2
    return {"topic": topic.decode(), "qos": qos, "retain": bool(flags & 1), "id": packet_id, "payload": body[offset:]}


class Broker:
    """Scripted broker: CONNACK with ``code``, PUBACK for QoS 1 (``ack`` decides the id or None to stay silent),
    PINGRESP for PINGREQ. Every packet is recorded."""

    def __init__(self, code=0, ack=lambda packet_id: packet_id, extra=None):
        self.client_side, self.sock = socket.socketpair()
        self.code, self.ack, self.extra = code, ack, extra
        self.packets = []
        self.connected = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                kind, flags, body = read_packet(self.sock)
                self.packets.append((kind, flags, body))
                if kind == 1:
                    if self.extra:
                        self.sock.sendall(self.extra)
                    self.sock.sendall(bytes((0x20, 2, 0, self.code)))
                    self.connected.set()
                elif kind == 3:
                    publish = parse_publish(flags, body)
                    if publish["qos"]:
                        packet_id = self.ack(publish["id"])
                        if packet_id is not None:
                            self.sock.sendall(b"\x40\x02" + struct.pack("!H", packet_id))
                elif kind == 12:
                    self.sock.sendall(b"\xd0\x00")
                elif kind == 14:
                    return
        except (EOFError, OSError):
            return

    def connect(self, address, timeout):
        self.address = address
        return self.client_side

    def publishes(self):
        return [parse_publish(flags, body) for kind, flags, body in self.packets if kind == 3]

    def close(self):
        for sock in (self.sock, self.client_side):
            try:
                sock.close()
            except OSError:
                pass


class CodecTests(unittest.TestCase):
    def test_remaining_length_round_trip_and_bounds(self):
        for value in (0, 1, 127, 128, 16383, 16384, 2097151, 2097152, mqtt.MAX_REMAINING):
            encoded = mqtt.encode_length(value)
            self.assertLessEqual(len(encoded), 4)
            stream = iter(encoded)
            self.assertEqual(mqtt.decode_length(lambda n: bytes((next(stream),))), value)
        with self.assertRaises(mqtt.MQTTError):
            mqtt.encode_length(mqtt.MAX_REMAINING + 1)
        stream = iter(b"\xff\xff\xff\xff\x01")
        with self.assertRaises(mqtt.MQTTError):
            mqtt.decode_length(lambda n: bytes((next(stream),)))

    def test_connect_flags_will_and_credentials(self):
        packet = mqtt.connect_packet("cid", keepalive=30, username="user", password="pw",
                                     will=("lab/portal/status", b"offline", 1, True))
        self.assertEqual(packet[0], 0x10)
        stream = iter(packet[1:])
        size = mqtt.decode_length(lambda n: bytes((next(stream),)))
        body = bytes(stream)
        self.assertEqual(len(body), size)
        parsed = parse_connect(body)
        self.assertEqual((parsed["name"], parsed["level"], parsed["keepalive"]), (b"MQTT", 4, 30))
        # username, password, will retain, will QoS 1, will flag, clean session
        self.assertEqual(parsed["flags"], 0x80 | 0x40 | 0x20 | 0x08 | 0x04 | 0x02)
        self.assertEqual((parsed["will_topic"], parsed["will_message"]), (b"lab/portal/status", b"offline"))
        self.assertEqual((parsed["username"], parsed["password"]), (b"user", b"pw"))
        with self.assertRaises(ValueError):
            mqtt.connect_packet("cid", password="pw")
        with self.assertRaises(ValueError):
            mqtt.connect_packet("cid", will=("t", b"x", 2, False))

    def test_topics_and_sizes(self):
        for topic in ("", "a/+/b", "a/#", "a\x00b", "$SYS/x", "x" * 1025):
            with self.assertRaises(ValueError):
                mqtt.check_topic(topic)
        self.assertEqual(mqtt.check_topic("lab/status/halo-1"), "lab/status/halo-1")
        with self.assertRaises(ValueError):
            mqtt.publish_packet("t", b"x" * (mqtt.MAX_PACKET + 1))
        with self.assertRaises(ValueError):
            mqtt.publish_packet("t", b"x", 1, packet_id=0)
        packet = mqtt.publish_packet("t", b"hi", 1, True, 7)
        self.assertEqual(packet[0], 0x33)


class ClientTests(unittest.TestCase):
    def client(self, broker, **kw):
        return mqtt.Client("workstation", 1883, client_id="cid", connect=broker.connect, timeout=1.0, **kw)

    def test_connect_publish_qos0_qos1_ping_disconnect(self):
        broker = Broker()
        self.addCleanup(broker.close)
        client = self.client(broker, username="user", password="pw", will=("p/portal/status", b"offline", 1, True))
        client.connect()
        self.assertEqual(broker.address, ("workstation", 1883))
        client.publish("p/a", "zero", 0, True)
        client.publish("p/b", b"one", 1, False)
        client.publish("p/c", b"two", 1, False)
        client.ping()
        client.disconnect()
        broker.thread.join(2)
        connect = parse_connect(broker.packets[0][2])
        self.assertEqual((connect["client_id"], connect["username"], connect["password"]), ("cid", b"user", b"pw"))
        self.assertEqual([(p["topic"], p["qos"], p["retain"], p["id"], p["payload"]) for p in broker.publishes()],
                         [("p/a", 0, True, None, b"zero"), ("p/b", 1, False, 1, b"one"), ("p/c", 1, False, 2, b"two")])
        self.assertEqual([kind for kind, _, _ in broker.packets], [1, 3, 3, 3, 12, 14])

    def test_connack_refusal_is_named_without_credentials(self):
        broker = Broker(code=4)
        self.addCleanup(broker.close)
        with self.assertRaises(mqtt.MQTTError) as caught:
            self.client(broker, username="user", password="hunter2").connect()
        self.assertIn("user name or password", str(caught.exception))
        self.assertNotIn("hunter2", str(caught.exception))

    def test_first_packet_must_be_connack(self):
        broker = Broker(extra=b"\xd0\x00")
        self.addCleanup(broker.close)
        with self.assertRaises(mqtt.MQTTError):
            self.client(broker).connect()

    def test_puback_with_another_id_closes_and_silence_times_out(self):
        broker = Broker(ack=lambda packet_id: packet_id + 5)
        self.addCleanup(broker.close)
        client = self.client(broker).connect()
        with self.assertRaises(mqtt.MQTTError):
            client.publish("p/x", b"1", 1)
        broker = Broker(ack=lambda packet_id: None)
        self.addCleanup(broker.close)
        client = self.client(broker).connect()
        started = time.monotonic()
        with self.assertRaises(mqtt.MQTTError) as caught:
            client.publish("p/x", b"1", 1)
        self.assertIn("timed out", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3)

    def test_fragmented_reads(self):
        class Slow:
            def __init__(self, data):
                self.data = data

            def recv(self, size):
                chunk, self.data = self.data[:1], self.data[1:]
                return chunk

            def settimeout(self, value):
                pass
        client = mqtt.Client("h", 1, client_id="c")
        client.sock = Slow(b"\x40\x02\x00\x01")
        self.assertEqual(client._expect(mqtt.PUBACK_TYPE, time.monotonic() + 1, 1), b"\x00\x01")
        client.sock = Slow(b"\x40\x03\x00\x01\x00")
        with self.assertRaises(mqtt.MQTTError):
            client._expect(mqtt.PUBACK_TYPE, time.monotonic() + 1, 1)

    def test_tls_verifies_certificate_and_host_name(self):
        seen = {}

        class Context:
            check_hostname = False
            verify_mode = ssl.CERT_NONE

            def wrap_socket(self, sock, server_hostname):
                seen.update(host=server_hostname, check=self.check_hostname, mode=self.verify_mode)
                return sock
        broker = Broker()
        self.addCleanup(broker.close)
        with mock.patch.object(mqtt.ssl, "create_default_context", return_value=Context()):
            self.client(broker, tls=True).connect()
        self.assertEqual(seen, {"host": "workstation", "check": True, "mode": ssl.CERT_REQUIRED})

    def test_connection_failure_message_is_fixed(self):
        def refuse(address, timeout):
            raise ConnectionRefusedError("refused by the peer at " + "x" * 8)
        with self.assertRaises(mqtt.MQTTError) as caught:
            mqtt.Client("h", 1, client_id="c", connect=refuse).connect()
        self.assertEqual(str(caught.exception), "connection to the broker failed (ConnectionRefusedError)")


class PublisherTests(unittest.TestCase):
    settings = {"host": "workstation", "port": 1883, "client_id": "cid", "topic_prefix": "lab", "username": None}

    def wait(self, check, timeout=5):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                self.fail("timed out")
            time.sleep(0.02)

    def test_queue_drops_oldest_and_coalesces_retained(self):
        outcomes = []
        publisher = mqtt.Publisher(self.settings, limit=3)
        for index in range(4):
            publisher.put("lab/events", str(index), 1, done=lambda outcome, i=index: outcomes.append((i, outcome)))
        self.assertEqual((publisher.state()["queued"], publisher.state()["dropped"]), (3, 1))
        self.assertEqual(outcomes, [(0, "dropped")])
        publisher.put("lab/status/a", "1", 0, True, done=lambda outcome: outcomes.append(("a1", outcome)))
        publisher.put("lab/status/a", "0", 0, True)
        self.assertIn(("a1", "replaced"), outcomes)
        self.assertEqual(sum(1 for m in publisher._queue if m.topic == "lab/status/a"), 1)

    def test_online_status_will_publish_expiry_and_clean_stop(self):
        broker = Broker()
        self.addCleanup(broker.close)
        sent = []
        publisher = mqtt.Publisher(self.settings, connect=broker.connect)
        publisher.put("lab/old", "x", 0, expires=time.monotonic() - 1, done=sent.append)
        publisher.put("lab/events", "e1", 1, done=sent.append)
        publisher.start()
        self.wait(lambda: publisher.state()["published"] >= 1)
        publisher.stop()
        broker.thread.join(2)
        connect = parse_connect(broker.packets[0][2])
        self.assertEqual((connect["will_topic"], connect["will_message"], (connect["flags"] >> 3) & 3,
                          bool(connect["flags"] & 0x20)), (b"lab/portal/status", b"offline", 1, True))
        topics = [(p["topic"], p["payload"]) for p in broker.publishes()]
        self.assertEqual(topics, [("lab/portal/status", b"online"), ("lab/events", b"e1"),
                                  ("lab/portal/status", b"offline")])
        self.assertEqual(sent, ["expired", "sent"])
        self.assertEqual(publisher.state()["expired"], 1)
        self.assertFalse(publisher.state()["connected"])

    def test_not_alive_ends_publishing_and_reports_exit(self):
        broker = Broker()
        self.addCleanup(broker.close)
        alive = threading.Event()
        alive.set()
        exited = threading.Event()
        publisher = mqtt.Publisher(self.settings, connect=broker.connect, alive=alive.is_set,
                                   on_exit=lambda p: exited.set())
        publisher.start()
        self.wait(lambda: publisher.state()["connected"])
        alive.clear()
        self.assertTrue(exited.wait(3))
        self.assertEqual(broker.publishes()[-1]["payload"], b"offline")
        self.wait(lambda: not publisher.state()["running"])

    def test_reconnects_after_failure_with_backoff(self):
        brokers = []
        attempts = []

        def connect(address, timeout):
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                raise ConnectionRefusedError()
            broker = Broker()
            brokers.append(broker)
            return broker.connect(address, timeout)
        publisher = mqtt.Publisher(self.settings, connect=connect, backoff=(0.05, 0.1))
        publisher.start()
        self.wait(lambda: publisher.state()["connected"])
        self.assertEqual(publisher.state()["failures"], 1)
        publisher.stop()
        for broker in brokers:
            broker.close()

    def test_a_session_the_broker_drops_at_once_backs_off_too(self):
        attempts, brokers = [], []

        def connect(address, timeout):
            attempts.append(time.monotonic())
            broker = Broker()
            brokers.append(broker)

            def drop():                   # the session starts (online status acknowledged), then the broker drops it
                deadline = time.monotonic() + 2
                while not broker.publishes() and time.monotonic() < deadline:
                    time.sleep(0.005)
                try:
                    broker.sock.shutdown(socket.SHUT_RDWR)      # the client sees the end at once (Linux too)
                except OSError:
                    pass
                broker.sock.close()
            threading.Thread(target=drop, daemon=True).start()
            return broker.connect(address, timeout)
        publisher = mqtt.Publisher(self.settings, connect=connect, backoff=(0.2, 0.4))
        stop = threading.Event()

        def feed():                       # a steady stream of messages, so every session has something to send
            index = 0
            while not stop.wait(0.01):
                publisher.put("lab/events", str(index), 1)
                index += 1
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        publisher.start()
        time.sleep(1.2)
        stop.set()
        feeder.join(2)
        publisher.stop()
        for broker in brokers:
            broker.close()
        self.assertGreaterEqual(len(attempts), 2)
        self.assertLessEqual(len(attempts), 5)                       # 0, 0.2, 0.6, 1.0 s: never a tight loop
        self.assertTrue(all(b - a >= 0.18 for a, b in zip(attempts, attempts[1:])))


class PasswordTests(unittest.TestCase):
    def test_password_round_trip_in_the_secret_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(secrets, "_directory", return_value=Path(directory)):
            name, other = "integrations-mqtt-0a1b2c3d", "integrations-mqtt-ffffffff"
            self.assertIsNone(mqtt.load_password(name))
            self.assertFalse(mqtt.password_saved(name))
            mqtt.save_password(name, "p@ss word")
            self.assertTrue(mqtt.password_saved(name))
            self.assertEqual(mqtt.load_password(name), "p@ss word")
            self.assertIsNone(mqtt.load_password(other))          # each configuration has its own secret
            stored = (Path(directory) / name).read_bytes()
            if secrets.os.name == "nt":
                self.assertNotIn(b"p@ss word", stored)       # DPAPI protected at rest
            mqtt.clear_password(name)
            self.assertIsNone(mqtt.load_password(name))
            for bad in ("", "x" * 513):
                with self.assertRaises(ValueError):
                    mqtt.save_password(name, bad)
            for reference in ("router-key", "../integrations-mqtt-0a1b2c3d", None):
                with self.assertRaises(ValueError):
                    mqtt.load_password(reference)


class StrictnessTests(unittest.TestCase):
    def client(self, broker):
        return mqtt.Client("workstation", 1883, client_id="cid", connect=broker.connect, timeout=1.0)

    def test_connack_flags_and_pingresp_flags_are_checked(self):
        class BadConnack(Broker):
            def run(self):
                read_packet(self.sock)
                self.sock.sendall(bytes((0x20, 2, 1, 0)))           # session present on a clean session
        broker = BadConnack()
        self.addCleanup(broker.close)
        client = self.client(broker)
        with self.assertRaises(mqtt.MQTTError):
            client.connect()
        self.assertIsNone(client.sock)                              # closed on the protocol error

        class BadPing(Broker):
            def run(self):
                read_packet(self.sock)
                self.sock.sendall(bytes((0x20, 2, 0, 0)))
                read_packet(self.sock)
                self.sock.sendall(b"\xd1\x00")                      # PINGRESP with reserved flags set
        broker = BadPing()
        self.addCleanup(broker.close)
        client = self.client(broker).connect()
        with self.assertRaises(mqtt.MQTTError):
            client.ping()
        self.assertIsNone(client.sock)

    def test_one_deadline_covers_the_whole_operation(self):
        class Trickle(Broker):
            def run(self):
                read_packet(self.sock)
                for byte in bytes((0x20, 2, 0, 0)):
                    time.sleep(0.4)
                    try:
                        self.sock.sendall(bytes((byte,)))
                    except OSError:
                        return
        broker = Trickle()
        self.addCleanup(broker.close)
        started = time.monotonic()
        with self.assertRaises(mqtt.MQTTError):
            self.client(broker).connect()                           # 1 s budget, the answer needs 1.6 s
        self.assertLess(time.monotonic() - started, 1.5)

    def test_name_resolution_respects_the_deadline(self):
        def slow(*args, **kw):
            time.sleep(2)
            return []
        with mock.patch.object(mqtt.socket, "getaddrinfo", side_effect=slow):
            started = time.monotonic()
            with self.assertRaises(mqtt.MQTTError) as caught:
                mqtt.open_connection(("workstation", 1883), 0.3)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("resolving", str(caught.exception))


class ResolverTests(unittest.TestCase):
    def test_a_hung_resolution_is_never_started_twice(self):
        calls, release = [], threading.Event()

        def hung(*args, **kw):
            calls.append(args)
            release.wait(5)
            raise OSError("no answer")
        self.addCleanup(release.set)
        with mock.patch.object(mqtt.socket, "getaddrinfo", side_effect=hung):
            for _ in range(4):
                with self.assertRaises(mqtt.MQTTError):
                    mqtt.open_connection(("resolver-test.invalid", 1883), 0.05)
            self.assertEqual(len(calls), 1)                          # one resolver thread, not one per attempt
            release.set()
            time.sleep(0.1)
            with self.assertRaises(mqtt.MQTTError):
                mqtt.open_connection(("resolver-test.invalid", 1883), 0.5)
        self.assertEqual(len(calls), 2)                              # once it ended, a new attempt resolves again


class LifecycleTests(unittest.TestCase):
    settings = {"host": "workstation", "port": 1883, "client_id": "cid", "topic_prefix": "lab", "username": None}

    def wait(self, check, timeout=5):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                self.fail("timed out")
            time.sleep(0.02)

    def test_stop_interrupts_a_blocked_publish_and_confirms_the_end(self):
        broker = Broker(ack=lambda packet_id: None)                 # never acknowledges
        self.addCleanup(broker.close)
        publisher = mqtt.Publisher(self.settings, connect=broker.connect)
        publisher.client = lambda: mqtt.Client("workstation", 1883, client_id="cid", connect=broker.connect, timeout=30,
                                               will=(publisher.status_topic, b"offline", 1, True))
        broker.ack = lambda packet_id: packet_id if packet_id == 1 else None   # only the online status is acked
        publisher.put("lab/events", "e", 1)
        publisher.start()
        self.wait(lambda: len(broker.publishes()) >= 2)             # online + the event, now waiting for its ack
        started = time.monotonic()
        self.assertTrue(publisher.stop(timeout=8))
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(publisher.state()["running"])

    def test_liveness_is_checked_before_each_send(self):
        broker = Broker()
        self.addCleanup(broker.close)
        alive = threading.Event()
        alive.set()
        publisher = mqtt.Publisher(self.settings, connect=broker.connect, alive=alive.is_set)
        publisher.start()
        self.wait(lambda: publisher.state()["connected"])
        alive.clear()
        publisher.put("lab/events", "late", 1)
        self.assertTrue(publisher.stop())
        self.assertNotIn("lab/events", [p["topic"] for p in broker.publishes()])

    def test_a_failed_online_publish_closes_the_new_connection(self):
        broker = Broker(ack=lambda packet_id: packet_id + 1)        # wrong id: the online status fails
        self.addCleanup(broker.close)
        publisher = mqtt.Publisher(self.settings, connect=broker.connect, backoff=(5, 5))
        publisher.client = lambda: mqtt.Client("workstation", 1883, client_id="cid", connect=broker.connect, timeout=1,
                                               will=(publisher.status_topic, b"offline", 1, True))
        publisher.start()
        self.wait(lambda: publisher.state()["failures"] == 1)
        self.assertEqual(publisher.state()["last_error"], "PUBACK for a packet that is not in flight")
        self.assertEqual(broker.client_side.fileno(), -1)           # the client socket was closed
        self.assertTrue(publisher.stop())

    def test_no_offline_status_once_ownership_lapsed(self):
        broker = Broker()
        self.addCleanup(broker.close)
        owner = threading.Event()
        owner.set()
        publisher = mqtt.Publisher(self.settings, connect=broker.connect, owner=owner.is_set)
        publisher.start()
        self.wait(lambda: publisher.state()["connected"])
        owner.clear()
        self.assertTrue(publisher.stop())
        broker.thread.join(2)
        self.assertEqual([p["payload"] for p in broker.publishes()], [b"online"])
        self.assertEqual(broker.packets[-1][0], 14)                 # a clean DISCONNECT: the will is discarded

    def test_oversized_messages_are_refused_when_queued(self):
        publisher = mqtt.Publisher(self.settings)
        with self.assertRaises(ValueError):
            publisher.put("lab/events", b"x" * (mqtt.MAX_PACKET + 1), 1)
        self.assertEqual(publisher.state()["queued"], 0)

    def test_errors_kept_for_documents_are_fixed_texts(self):
        def refuse(address, timeout):
            raise ConnectionRefusedError("peer said: password=hunter2")
        publisher = mqtt.Publisher(self.settings, connect=refuse, backoff=(5, 5))
        publisher.start()
        self.wait(lambda: publisher.state()["failures"] == 1)
        self.assertEqual(publisher.state()["last_error"], "connection to the broker failed (ConnectionRefusedError)")
        self.assertTrue(publisher.stop())


if __name__ == "__main__":
    unittest.main()
