"""The collectors' HTTP reader against a local test server: deadline, caps, chunked bodies, redirects, status codes."""

import ipaddress
import json
import socket
import threading
import time
import unittest

from hyperloom.web.features import observe


LOOPBACK = str(ipaddress.ip_address(socket.INADDR_LOOPBACK))


class Server:
    """A one-connection-at-a-time raw HTTP server; ``reply(request_bytes, connection)`` writes the answer."""

    def __init__(self, reply):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.bind((LOOPBACK, 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data)
                try:
                    self.reply(data, conn)
                except OSError:
                    pass

    def url(self, path="/x"):
        return f"http://{LOOPBACK}:{self.port}{path}"

    def close(self):
        self.sock.close()


def fixed(status, body, headers=()):
    def reply(request, conn):
        head = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", *headers]
        conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    return reply


class HttpTests(unittest.TestCase):
    def test_json_text_and_headers(self):
        server = Server(fixed(200, json.dumps({"a": 1}).encode()))
        try:
            self.assertEqual(observe.http_get(server.url("/api/status?detail=1"), {"Authorization": "Bearer k"}), {"a": 1})
            request = server.requests[0].decode()
            self.assertTrue(request.startswith("GET /api/status?detail=1 HTTP/1.1\r\n"))
            self.assertIn("Authorization: Bearer k\r\n", request)
            self.assertIn("Connection: close\r\n", request)
        finally:
            server.close()

    def test_chunked(self):
        def reply(request, conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nab\nc\r\n3\r\nd\ne\r\n0\r\n\r\n")
        server = Server(reply)
        try:
            self.assertEqual(observe.http_get(server.url(), text=True), "ab\ncd\ne")
        finally:
            server.close()

    def test_status_codes_and_redirects(self):
        for status, error in ((404, observe.Unavailable), (405, observe.Unavailable), (500, OSError), (302, OSError)):
            server = Server(fixed(status, b"{}", (f"Location: http://{ipaddress.IPv4Address(0xC0000209)}/",)))
            try:
                with self.assertRaises(error, msg=status):
                    observe.http_get(server.url())
                self.assertEqual(len(server.requests), 1)           # the redirect was not followed
            finally:
                server.close()

    def test_trickling_peer_is_cut_at_the_deadline(self):
        def reply(request, conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            for _ in range(100):
                conn.sendall(b"x")
                time.sleep(0.05)
        server = Server(reply)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                observe.http_get(server.url(), text=True, deadline=time.monotonic() + 0.3)
            self.assertLess(time.monotonic() - started, 0.6)
        finally:
            server.close()

    def test_size_cap_and_short_body(self):
        big = b"x" * (observe.HTTP_CAP + 10)
        server = Server(fixed(200, big))
        try:
            with self.assertRaises(ValueError):
                observe.http_get(server.url(), text=True)
        finally:
            server.close()

        def short(request, conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc")
        server = Server(short)
        try:
            with self.assertRaises(ValueError):
                observe.http_get(server.url(), text=True)
        finally:
            server.close()

    def test_only_plain_http_urls(self):
        for url in ("https://example.invalid:1/x", "http://nohostport/x", "file:///x"):
            with self.assertRaises(ValueError):
                observe.http_get(url)


if __name__ == "__main__":
    unittest.main()
