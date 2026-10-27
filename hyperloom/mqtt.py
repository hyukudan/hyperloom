"""Minimal MQTT 3.1.1 publisher (stdlib only).

``Client`` speaks the subset the portal needs: CONNECT (clean session, optional last will and credentials),
PUBLISH at QoS 0 or 1 (one QoS 1 message in flight; it waits for the PUBACK carrying its packet id), PINGREQ /
PINGRESP and DISCONNECT. Each operation runs against one deadline (name resolution, TCP, TLS and the broker's
answer share it). The first packet from the broker must be a valid CONNACK; later, while waiting for an
acknowledgement, a valid PINGRESP is accepted and anything else is a protocol error. A protocol error or a failed
operation closes the connection. TLS uses the system trust store with certificate and host name verification on.

``Publisher`` owns one client on a daemon thread: a bounded queue (1000, drop-oldest, counted), retained topics
coalesced (a newer value replaces a queued older one), per-message expiry checked at send time, reconnects with
backoff, and a retained ``online`` portal status whose last will (QoS 1, retained) is ``offline``. Liveness
(``alive()``) is checked before every connection and every send, and at least once a second while idle or
backing off; ``stop()`` interrupts a blocked operation by closing the socket and returns only when the thread has
ended (or reports that it has not). Each message can carry a callback told whether it was sent (QoS 1:
acknowledged), dropped, replaced or expired. Errors kept for documents are fixed texts.

The broker password lives in the hyperloom secret directory under a per-configuration secret name (DPAPI on
Windows, a 0600 file elsewhere) and never appears in a document, an error message or an audit line.
"""

from collections import deque
import os
import re
import socket
import ssl
import struct
import threading
import time

from . import secrets


PROTOCOL_LEVEL = 4
MAX_PACKET = 256 * 1024            # largest packet sent or accepted
MAX_REMAINING = 268_435_455        # four remaining-length bytes
QUEUE_LIMIT = 1000
OPERATION_TIMEOUT_S = 5.0
SECRET_NAME = re.compile(r"integrations-mqtt-[0-9a-f]{8}")
CONNACK = {1: "broker refused the protocol version", 2: "broker refused the client id",
           3: "broker unavailable", 4: "broker refused the user name or password", 5: "not authorized by the broker"}
TOPIC_FORBIDDEN = re.compile(r"[+#\x00]")
CONNACK_TYPE, PUBACK_TYPE, PINGRESP_TYPE = 2, 4, 13


class MQTTError(OSError):
    """A protocol, connection or broker refusal. Messages are fixed texts (never credentials or peer data)."""


def encode_length(value):
    if not 0 <= value <= MAX_REMAINING:
        raise MQTTError("MQTT packet too large")
    out = bytearray()
    while True:
        digit, value = value % 128, value // 128
        out.append(digit | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_length(read):
    """Decode a remaining length from read(1) calls (at most four bytes)."""
    value, multiplier = 0, 1
    for _ in range(4):
        byte = read(1)[0]
        value += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            return value
        multiplier *= 128
    raise MQTTError("malformed MQTT remaining length")


def _string(text):
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if len(data) > 65535:
        raise ValueError("MQTT string longer than 65535 bytes")
    return struct.pack("!H", len(data)) + data


def check_topic(topic):
    """A publish topic: 1..1024 UTF-8 bytes, no wildcards, no NUL, no leading $ (reserved by brokers)."""
    if (not isinstance(topic, str) or not topic or topic.startswith("$") or len(topic.encode("utf-8")) > 1024
            or TOPIC_FORBIDDEN.search(topic)):
        raise ValueError("invalid MQTT topic")
    return topic


def connect_packet(client_id, *, keepalive=30, username=None, password=None, will=None):
    """CONNECT bytes. ``will`` = (topic, payload bytes, qos 0|1, retain)."""
    if not 0 <= keepalive <= 65535:
        raise ValueError("keepalive must be 0..65535 s")
    flags = 0x02                                    # clean session
    payload = _string(client_id)
    if will is not None:
        topic, message, qos, retain = will
        if qos not in (0, 1):
            raise ValueError("will QoS must be 0 or 1")
        flags |= 0x04 | (qos << 3) | (0x20 if retain else 0)
        payload += _string(check_topic(topic)) + _string(message)
    if password is not None and username is None:
        raise ValueError("an MQTT password needs a user name")
    if username is not None:
        flags |= 0x80
        payload += _string(username)
    if password is not None:
        flags |= 0x40
        payload += _string(password)
    body = _string("MQTT") + bytes((PROTOCOL_LEVEL, flags)) + struct.pack("!H", keepalive) + payload
    return b"\x10" + encode_length(len(body)) + body


def publish_packet(topic, payload, qos=0, retain=False, packet_id=None, dup=False):
    if qos not in (0, 1):
        raise ValueError("QoS must be 0 or 1")
    body = _string(check_topic(topic))
    if qos:
        if not isinstance(packet_id, int) or not 1 <= packet_id <= 65535:
            raise ValueError("packet id must be 1..65535")
        body += struct.pack("!H", packet_id)
    body += payload
    if len(body) > MAX_PACKET:
        raise ValueError("MQTT message larger than 256 KiB")
    header = 0x30 | (0x08 if dup else 0) | (qos << 1) | (0x01 if retain else 0)
    return bytes((header,)) + encode_length(len(body)) + body


def _bytes(payload):
    return payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)


_resolving = {}                    # (host, port) -> the one resolver thread still running for it
_resolving_lock = threading.Lock()


def open_connection(address, timeout):
    """TCP connection whose name resolution also respects ``timeout`` (resolved on a helper thread). A resolution
    that outlives its attempt is not started again: later attempts wait for that one thread, so a hung resolver
    never piles up a thread per attempt."""
    host, port = address
    deadline = time.monotonic() + timeout
    key = (host, port)
    with _resolving_lock:
        worker = _resolving.get(key)
        if worker is None or not worker.is_alive():
            found = {}

            def resolve():
                try:
                    found["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    found["error"] = exc
            worker = threading.Thread(target=resolve, name="hyperloom-mqtt-resolve", daemon=True)
            worker.found = found
            _resolving[key] = worker
            worker.start()
    found = worker.found
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise MQTTError("timed out resolving the broker name")
    if "error" in found or not found.get("infos"):
        raise MQTTError("cannot resolve the broker name")
    last = None
    for family, kind, proto, _, sockaddr in found["infos"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise MQTTError(f"cannot connect to the broker ({type(last).__name__ if last else 'TimeoutError'})")


class Client:
    def __init__(self, host, port, *, client_id, username=None, password=None, tls=False, keepalive=30, will=None,
                 timeout=OPERATION_TIMEOUT_S, connect=open_connection):
        self.host, self.port, self.client_id = host, int(port), client_id
        self.username, self.password, self.tls = username, password, bool(tls)
        self.keepalive, self.will, self.timeout = int(keepalive), will, float(timeout)
        self._connect = connect
        self.sock = None
        self._next_id = 0
        self.last_sent = 0.0

    # -- transport ------------------------------------------------------------------------------------------------
    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MQTTError("timed out waiting for the broker")
        return remaining

    def _socket(self):
        sock = self.sock
        if sock is None:
            raise MQTTError("connection to the broker is closed")
        return sock

    def _recv_exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            sock = self._socket()
            sock.settimeout(self._remaining(deadline))
            try:
                chunk = sock.recv(size - len(data))
            except socket.timeout:
                raise MQTTError("timed out waiting for the broker") from None
            if not chunk:
                raise MQTTError("broker closed the connection")
            data += chunk
        return bytes(data)

    def _send(self, data, deadline):
        sock = self._socket()
        sock.settimeout(self._remaining(deadline))
        try:
            sock.sendall(data)
        except socket.timeout:
            raise MQTTError("timed out sending to the broker") from None
        self.last_sent = time.monotonic()

    def read_packet(self, deadline):
        """(type, flags, body) of the next packet; one larger than MAX_PACKET is a protocol error."""
        first = self._recv_exact(1, deadline)[0]
        size = decode_length(lambda n: self._recv_exact(n, deadline))
        if size > MAX_PACKET:
            raise MQTTError("MQTT packet from the broker exceeds 256 KiB")
        return first >> 4, first & 0x0F, self._recv_exact(size, deadline) if size else b""

    def _expect(self, kind, deadline, packet_id=None):
        """Wait for one packet of ``kind`` (a PUBACK with ``packet_id``); a valid PINGRESP is tolerated except
        before the CONNACK; anything else or a malformed packet is a protocol error."""
        while True:
            got, flags, body = self.read_packet(deadline)
            if got == PINGRESP_TYPE and kind != CONNACK_TYPE:
                if flags or body:
                    raise MQTTError("malformed PINGRESP from the broker")
                if kind == PINGRESP_TYPE:
                    return body
                continue
            if got != kind:
                raise MQTTError("unexpected packet from the broker")
            if flags != 0:
                raise MQTTError("malformed acknowledgement from the broker")
            if kind == CONNACK_TYPE and (len(body) != 2 or body[0] != 0):
                raise MQTTError("malformed CONNACK from the broker")
            if kind == PUBACK_TYPE:
                if len(body) != 2:
                    raise MQTTError("malformed PUBACK from the broker")
                if struct.unpack("!H", body)[0] != packet_id:
                    raise MQTTError("PUBACK for a packet that is not in flight")
            return body

    def _guarded(self, work):
        """Run a protocol step; any failure closes the connection and surfaces as a fixed-text MQTTError."""
        try:
            return work()
        except MQTTError:
            self.close()
            raise
        except ValueError:
            self.close()
            raise
        except (OSError, ssl.SSLError) as exc:
            self.close()
            raise MQTTError(f"connection to the broker failed ({type(exc).__name__})") from None

    # -- protocol -------------------------------------------------------------------------------------------------
    def connect(self):
        deadline = time.monotonic() + self.timeout

        def work():
            self.sock = self._connect((self.host, self.port), self._remaining(deadline))
            if self.tls:
                context = ssl.create_default_context()
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                self.sock.settimeout(self._remaining(deadline))
                raw, self.sock = self.sock, None
                try:
                    self.sock = context.wrap_socket(raw, server_hostname=self.host)
                except BaseException:
                    raw.close()
                    raise
            self._send(connect_packet(self.client_id, keepalive=self.keepalive, username=self.username,
                                      password=self.password, will=self.will), deadline)
            return self._expect(CONNACK_TYPE, deadline)
        body = self._guarded(work)
        if body[1] != 0:
            self.close()
            raise MQTTError(CONNACK.get(body[1], "broker refused the connection"))
        return self

    def publish(self, topic, payload, qos=0, retain=False):
        payload = _bytes(payload)
        deadline = time.monotonic() + self.timeout
        publish_packet(topic, payload, qos, retain, 1)       # an invalid message raises before anything is sent
        if qos == 0:
            packet = publish_packet(topic, payload, 0, retain)
            self._guarded(lambda: self._send(packet, deadline))
            return
        self._next_id = self._next_id % 65535 + 1      # one message in flight, so ids never overlap
        packet_id = self._next_id
        packet = publish_packet(topic, payload, 1, retain, packet_id)
        self._guarded(lambda: (self._send(packet, deadline), self._expect(PUBACK_TYPE, deadline, packet_id)))

    def ping(self):
        deadline = time.monotonic() + self.timeout
        self._guarded(lambda: (self._send(b"\xc0\x00", deadline), self._expect(PINGRESP_TYPE, deadline)))

    def disconnect(self):
        try:
            if self.sock is not None:
                self._send(b"\xe0\x00", time.monotonic() + 1.0)
        except (OSError, ValueError):
            pass
        self.close()

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)     # wakes a read or write blocked in another thread
            except (OSError, ValueError):
                pass
            try:
                sock.close()
            except OSError:
                pass


# -- broker password (secret store) ---------------------------------------------------------------------------------
# hyperloom.secrets.get() holds 64-hex API keys only; the broker password is an arbitrary string, stored with the
# same directory, private file mode / DPAPI protection and atomic replace, under its configuration's own name.
def _password_path(name):
    if not isinstance(name, str) or not SECRET_NAME.fullmatch(name):
        raise ValueError("invalid credential reference")
    return secrets._directory() / secrets._name(name)


def save_password(name, value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or "\x00" in value:
        raise ValueError("password must be 1..512 characters")
    data = value.encode("utf-8")
    secrets._write(_password_path(name), secrets._dpapi(data) if os.name == "nt" else data)


def clear_password(name):
    _password_path(name).unlink(missing_ok=True)


def load_password(name):
    try:
        data = _password_path(name).read_bytes()
    except FileNotFoundError:
        return None
    if os.name == "nt":
        data = secrets._dpapi(data, decrypt=True)
    return data.decode("utf-8")


def password_saved(name):
    try:
        return _password_path(name).is_file()
    except (OSError, ValueError):
        return False


# -- publisher ------------------------------------------------------------------------------------------------------
class Message:
    __slots__ = ("topic", "payload", "qos", "retain", "expires", "done")

    def __init__(self, topic, payload, qos, retain, expires, done):
        self.topic, self.payload, self.qos, self.retain = check_topic(topic), _bytes(payload), qos, retain
        self.expires, self.done = expires, done


def _reason(exc):
    """A fixed text for documents: our own protocol messages, else the exception class only."""
    return str(exc) if isinstance(exc, MQTTError) else f"publishing failed ({type(exc).__name__})"


class Publisher:
    """One broker connection on a daemon thread fed by a bounded queue (drop-oldest).

    ``alive()`` is checked before every connection and send and at least once a second; when it turns false the
    publisher publishes ``offline`` (retained), disconnects and ends, then calls ``on_exit``."""

    def __init__(self, settings, *, password=None, connect=open_connection, limit=QUEUE_LIMIT,
                 clock=time.time, backoff=(2.0, 60.0), alive=lambda: True, owner=lambda: True, on_exit=None,
                 monotonic=time.monotonic):
        self.settings = dict(settings)
        self.status_topic = check_topic(self.settings["topic_prefix"] + "/portal/status")
        self._password = password
        self._connect = connect
        self._clock = clock
        self._monotonic = monotonic
        self._backoff = backoff
        self._alive = alive
        self._owner = owner          # False once the publisher may no longer write (its lease is about to lapse)
        self._on_exit = on_exit
        self._queue = deque()
        self._limit = limit
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._client = None
        self.stats = {"connected": False, "connects": 0, "published": 0, "dropped": 0, "expired": 0, "failures": 0,
                      "last_error": None, "last_connect": None, "last_publish": None}

    def client(self):
        settings = self.settings
        username = settings.get("username")
        return Client(settings["host"], settings["port"], client_id=settings["client_id"], username=username,
                      password=self._password if username else None, tls=settings.get("tls", False),
                      keepalive=settings.get("keepalive", 30), will=(self.status_topic, b"offline", 1, True),
                      connect=self._connect)

    @staticmethod
    def _finish(message, outcome):
        if message.done is not None:
            try:
                message.done(outcome)
            except Exception:       # noqa: BLE001 - a callback never stops publishing
                pass

    def put(self, topic, payload, qos=0, retain=False, *, expires=None, done=None):
        """Queue a message. A retained message replaces a queued one of the same topic; a full queue drops its
        oldest message. ``expires`` is a monotonic deadline checked when the message is sent."""
        message = Message(topic, payload, qos, retain, expires, done)
        publish_packet(topic, message.payload, qos, retain, 1)     # raises ValueError for one that can never be sent
        finished = []
        with self._cv:
            if retain:
                for queued in [m for m in self._queue if m.retain and m.topic == topic]:
                    self._queue.remove(queued)
                    finished.append((queued, "replaced"))
            if len(self._queue) >= self._limit:
                finished.append((self._queue.popleft(), "dropped"))
                self.stats["dropped"] += 1
            self._queue.append(message)
            self._cv.notify()
        for old, outcome in finished:
            self._finish(old, outcome)

    def state(self):
        with self._cv:
            return {**self.stats, "queued": len(self._queue), "limit": self._limit,
                    "running": self._thread is not None and self._thread.is_alive()}

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hyperloom-mqtt", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=8.0):
        """Stop and wait for the thread to end; a blocked socket operation is interrupted by closing the socket.
        Returns True once the thread has ended."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return thread is None or not thread.is_alive()
        deadline = time.monotonic() + timeout
        thread.join(min(timeout, OPERATION_TIMEOUT_S / 2))
        while thread.is_alive() and time.monotonic() < deadline:
            client = self._client
            if client is not None:
                client.close()
            thread.join(0.2)
        return not thread.is_alive()

    def _fail(self, exc, client=None):
        with self._cv:
            self.stats.update(connected=False, last_error=_reason(exc), failures=self.stats["failures"] + 1)
        for owned in (client, self._client):
            if owned is not None:
                owned.close()
        self._client = None

    def _running(self):
        if self._stop.is_set():
            return False
        try:
            return bool(self._alive())
        except Exception:       # noqa: BLE001 - a failing liveness check ends publishing
            return False

    def _wait(self, seconds):
        """Back off, checking liveness at least once a second."""
        deadline = time.monotonic() + seconds
        while self._running():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(1.0, remaining))

    def _next(self):
        expired, message = [], None
        with self._cv:
            if not self._queue and not self._stop.is_set():
                self._cv.wait(1.0)
            while self._queue:
                candidate = self._queue.popleft()
                if candidate.expires is not None and self._monotonic() > candidate.expires:
                    self.stats["expired"] += 1
                    expired.append(candidate)
                    continue
                message = candidate
                break
        for old in expired:
            self._finish(old, "expired")
        return message

    def _requeue(self, message):
        with self._cv:          # best effort: back to the front unless the queue refilled meanwhile
            if len(self._queue) < self._limit:
                self._queue.appendleft(message)
                return
            self.stats["dropped"] += 1
        self._finish(message, "dropped")

    def _connect_session(self):
        client = self.client()
        try:
            client.connect()
            if not self._running():
                client.disconnect()
                return False
            client.publish(self.status_topic, b"online", 1, True)
        except (MQTTError, OSError, ValueError) as exc:
            self._fail(exc, client)
            return None
        self._client = client
        with self._cv:
            self.stats.update(connected=True, connects=self.stats["connects"] + 1, last_error=None,
                              last_connect=self._clock())
        return True

    def _run(self):
        delay = self._backoff[0]
        session_started = None
        try:
            while self._running():
                if self._client is None:
                    if session_started is not None:
                        # A session ended: a short one (the broker drops us at once) backs off like a failed connect,
                        # doubling up to the cap; only a session that lasted resets the delay.
                        if time.monotonic() - session_started >= self._backoff[1]:
                            delay = self._backoff[0]
                        session_started = None
                        self._wait(delay)
                        delay = min(self._backoff[1], delay * 2)
                        if not self._running():
                            break
                    connected = self._connect_session()
                    if connected is None:
                        self._wait(delay)
                        delay = min(self._backoff[1], delay * 2)
                        continue
                    if not connected:
                        break
                    session_started = time.monotonic()
                message = self._next()
                if not self._running():
                    if message is not None:
                        self._requeue(message)
                    break
                client = self._client
                try:
                    if message is not None:
                        client.publish(message.topic, message.payload, message.qos, message.retain)
                        with self._cv:
                            self.stats["published"] += 1
                            self.stats["last_publish"] = self._clock()
                        self._finish(message, "sent")
                    elif time.monotonic() - client.last_sent >= max(1.0, client.keepalive / 2):
                        client.ping()
                except ValueError:
                    if message is not None:     # the message itself is invalid: dropped, the session stays
                        with self._cv:
                            self.stats["dropped"] += 1
                        self._finish(message, "dropped")
                except (MQTTError, OSError) as exc:
                    if message is not None:
                        self._requeue(message)
                    self._fail(exc)
            if self._client is not None:
                # A clean stop publishes offline only while this publisher still owns the topics; otherwise it
                # disconnects cleanly (which also discards the will), leaving the status to the next owner.
                try:
                    if self._owner():
                        self._client.publish(self.status_topic, b"offline", 1, True)
                except (MQTTError, OSError, ValueError):
                    pass
                except Exception:   # noqa: BLE001 - a failing ownership check writes nothing
                    pass
                self._client.disconnect()
                self._client = None
        finally:
            with self._cv:
                self.stats["connected"] = False
                left = list(self._queue)
                self._queue.clear()
            for message in left:
                self._finish(message, "dropped")
            if self._on_exit is not None:
                try:
                    self._on_exit(self)
                except Exception:   # noqa: BLE001
                    pass
