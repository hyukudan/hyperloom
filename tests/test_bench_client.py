"""Bench client: SSE parsing, streaming replies, no redirects (and no key after one), caps, cancellation."""

from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))

from fake_server import FakeServer  # noqa: E402

from hyperloom.bench import client  # noqa: E402


BODY = {"model": "fixture-model", "messages": [{"role": "user", "content": "hello there"}], "max_tokens": 6, "stream": True,
        "stream_options": {"include_usage": True}, "return_token_ids": True}


class SSEParserTests(unittest.TestCase):
    def test_events_comments_and_multiline_data(self):
        parser = client.SSEParser()
        out = []
        for line in (b": comment\n", b"data: {\"a\":\n", b"data: 1}\n", b"\n", b"event: x\n", b"data:[DONE]\r\n", b"\r\n"):
            out += parser.line(line)
        self.assertEqual(out, ['{"a":\n1}', "[DONE]"])

    def test_flush_returns_a_trailing_event(self):
        parser = client.SSEParser()
        parser.line(b"data: tail")
        self.assertEqual(parser.flush(), ["tail"])
        self.assertEqual(parser.flush(), [])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer(token_delay=0.001)
        self.addCleanup(self.server.close)
        self.endpoint = client.Endpoint("http", "127.0.0.1", self.server.port, "k" * 64)

    def test_stream_measures_and_collects_usage_and_ids(self):
        reply = client.chat(self.endpoint, BODY, request_id="hlb-test-1")
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.completion_tokens, 6)
        self.assertEqual(len(reply.token_ids), 6)
        self.assertEqual(reply.cached_tokens, 0)
        self.assertEqual(reply.content_events, 6)
        self.assertTrue(0 < reply.ttft_s <= reply.last_output_s <= reply.end_s)
        self.assertEqual(reply.headers["x-tensormeld-replica"], "halo-1")
        sent = self.server.requests[-1]["headers"]
        self.assertEqual(sent["Authorization"], "Bearer " + "k" * 64)
        self.assertEqual(sent["X-Request-Id"], "hlb-test-1")

    def test_non_streamed_reply(self):
        reply = client.chat(self.endpoint, dict(BODY, stream=False))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.completion_tokens, 6)
        self.assertEqual(reply.text.split(), [f"w{i % 1000}" for i in reply.token_ids])

    def test_reply_without_usage_has_no_counts(self):
        self.server.config["no_usage"] = True
        reply = client.chat(self.endpoint, BODY)
        self.assertTrue(reply.ok)
        self.assertIsNone(reply.completion_tokens)
        self.assertIsNone(reply.token_ids)
        self.assertEqual(reply.content_events, 6)

    def test_http_error_is_reported(self):
        self.server.config["status"] = 503
        reply = client.chat(self.endpoint, BODY)
        self.assertFalse(reply.ok)
        self.assertIn("503", reply.error)
        self.assertIn("fixture failure", reply.error)

    def test_redirect_is_refused_and_no_key_reaches_the_other_location(self):
        with FakeServer() as elsewhere:
            self.server.config["redirect"] = f"http://127.0.0.1:{elsewhere.port}"
            reply = client.chat(self.endpoint, BODY)
            self.assertIn("redirect refused", reply.error)
            with self.assertRaises(client.ClientError) as caught:
                client.get_json(self.endpoint, "/v1/models")
            self.assertIn("redirect refused", str(caught.exception))
            self.assertEqual(elsewhere.requests, [])

    def test_cancel_closes_an_open_stream(self):
        self.server.config["token_delay"] = 0.2
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        started = time.monotonic()
        reply = client.chat(self.endpoint, dict(BODY, max_tokens=200), cancelled=stop.is_set)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(reply.cancelled)
        self.assertEqual(reply.error, "cancelled")

    def test_cancel_interrupts_a_stalled_body(self):
        self.server.config["stall_after"] = 1
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        started = time.monotonic()
        reply = client.chat(self.endpoint, BODY, cancelled=stop.is_set)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual((reply.error, reply.cancelled), ("cancelled", True))

    def test_truncated_stream_is_an_error(self):
        self.server.config["truncate_after"] = 2
        reply = client.chat(self.endpoint, BODY)
        self.assertFalse(reply.ok)
        self.assertIn("ended before completion", reply.error)

    def test_request_timeout_closes_the_stream(self):
        self.server.config["token_delay"] = 0.2
        started = time.monotonic()
        reply = client.chat(self.endpoint, dict(BODY, max_tokens=200), timeout=1.0)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(reply.error, "request timed out")

    def test_connection_failures_are_errors_not_exceptions(self):
        dead = client.Endpoint("http", "127.0.0.1", 1)
        reply = client.chat(dead, BODY, timeout=3)
        self.assertFalse(reply.ok)
        self.assertIn("connection", reply.error)

    def test_oversized_line_is_refused(self):
        original = client.LINE_CAP
        client.LINE_CAP = 64
        self.addCleanup(setattr, client, "LINE_CAP", original)
        reply = client.chat(self.endpoint, BODY)
        self.assertIn("size cap", reply.error)

    def test_models(self):
        self.server.config["context_tokens"] = 32768
        data = client.get_json(self.endpoint, "/v1/models")
        self.assertEqual(data["data"][0]["max_model_len"], 32768)

    def test_endpoint_checks_and_url(self):
        with self.assertRaises(ValueError):
            client.Endpoint("ftp", "x", 1)
        with self.assertRaises(ValueError):
            client.Endpoint("http", "x", 0)
        self.assertEqual(client.Endpoint("https", "::1", 443).url, "https://[::1]:443")
        self.assertNotIn("k" * 8, repr(self.endpoint))

    def test_tool_call_deltas_are_assembled(self):
        self.server.config["responder"] = lambda body: {"text": "", "tool_calls": [
            {"id": "call_0", "function": {"name": "get_weather", "arguments": '{"city": "Lisbon", "unit": "celsius"}'}}]}
        reply = client.chat(self.endpoint, BODY)
        self.assertEqual(reply.tool_calls, [{"id": "call_0", "name": "get_weather", "arguments": '{"city": "Lisbon", "unit": "celsius"}'}])
        self.assertEqual(reply.finish_reason, "tool_calls")


if __name__ == "__main__":
    unittest.main()
