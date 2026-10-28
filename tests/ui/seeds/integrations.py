"""Fixture integrations for the browser smoke: MQTT settings pointing at an in-process loopback test broker (so the
page shows a live publisher), a saved user name, and a fresh node series for the metrics topic and the export list."""

import socket
import struct
import threading


def _serve(listener):
    def client(sock):
        def exact(size):
            data = b""
            while len(data) < size:
                chunk = sock.recv(size - len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return data
        try:
            while True:
                first = exact(1)[0]
                size, multiplier = 0, 1
                while True:
                    byte = exact(1)[0]
                    size += (byte & 0x7F) * multiplier
                    multiplier *= 128
                    if not byte & 0x80:
                        break
                body = exact(size) if size else b""
                kind, flags = first >> 4, first & 0x0F
                if kind == 1:
                    sock.sendall(b"\x20\x02\x00\x00")
                elif kind == 3 and (flags >> 1) & 3:
                    topic = struct.unpack("!H", body[:2])[0]
                    sock.sendall(b"\x40\x02" + body[2 + topic:4 + topic])
                elif kind == 12:
                    sock.sendall(b"\xd0\x00")
                elif kind == 14:
                    return
        except (EOFError, OSError):
            return
        finally:
            sock.close()

    while True:
        try:
            sock, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=client, args=(sock,), daemon=True).start()


LOOPBACK = socket.inet_ntoa(bytes((127, 0, 0, 1)))


def seed(store, now):
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    listener.listen(4)
    threading.Thread(target=_serve, args=(listener,), daemon=True).start()
    store.put("prefs/integrations", "mqtt", {
        "enabled": True, "host": LOOPBACK, "port": listener.getsockname()[1], "tls": False, "topic_prefix": "lab",
        "username": "lab-portal", "client_id": "hyperloom-fixture", "interval_s": 10, "revision": 1,
        "credential_ref": "integrations-mqtt-5eed0001"})
    store.describe("node_load1", "gauge", interval=5)
    for offset in range(0, 600, 5):
        store.record([("node_load1", {"machine": "halo-1"}, 1.0 + offset / 600)], ts=now - 600 + offset)
