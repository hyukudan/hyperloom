"""A fake chat-completions server for the bench tests (loopback only, deterministic).

Replies are a pure function of the request: the reply token ids are derived from the prompt text, the content is
one word per token, usage counts prompt characters / 4, and the final stream chunk carries ``usage`` and a
``strix`` object (queue/prefill/decode ms, token ids when requested). Knobs on ``FakeServer.config``:

    responder(body) -> {"text": str, "tool_calls": [...], "reasoning": str} | None   (default: words)
    length: int (reply tokens, default max_tokens)      token_delay: seconds between stream events
    status: int (answer every completion with it)        redirect: "http://127.0.0.1:<port>" (302 to there)
    no_ids: bool (never return token ids)                 no_usage: bool (stream without usage/strix)
    diverge_at: int (change the token id at this index; only for prompts containing diverge_match when given)
    prefix_cache: bool (cached tokens for a repeated prefix)   prefill_rate: tokens/s for the reported prefill_ms
    context_tokens: int (reported by /v1/models)          model: str
    stall_after: int (stop sending after this many content events, keep the connection open)
    truncate_after: int (close the connection after this many content events, without a finish)
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def words_for(ids):
    return [f"w{i % 1000}" for i in ids]


def reply_ids(prompt, count):
    ids, seed = [], hashlib.sha256(prompt.encode("utf-8")).digest()
    while len(ids) < count:
        seed = hashlib.sha256(seed).digest()
        ids.extend(int.from_bytes(seed[i:i + 2], "big") for i in range(0, 32, 2))
    return ids[:count]


def prompt_text(body):
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


class FakeServer:
    def __init__(self, **config):
        self.config = {"model": "fixture-model", **config}
        self.requests = []
        self.lock = threading.Lock()
        self.seen_prefixes = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def log_message(self, *_):
                pass

            def _json(self, status, value, headers=None):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, item in (headers or {}).items():
                    self.send_header(key, item)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with fake.lock:
                    fake.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                if fake.config.get("redirect"):
                    self.send_response(302)
                    self.send_header("Location", fake.config["redirect"] + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/v1/models":
                    model = {"id": fake.config["model"], "object": "model"}
                    if fake.config.get("context_tokens"):
                        model["max_model_len"] = fake.config["context_tokens"]
                    self._json(200, {"object": "list", "data": [model]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with fake.lock:
                    fake.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
                if fake.config.get("redirect"):
                    self.send_response(307)
                    self.send_header("Location", fake.config["redirect"] + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fake.config.get("status"):
                    self._json(fake.config["status"], {"error": {"message": "fixture failure"}})
                    return
                fake.answer(self, body)

        class Server(ThreadingHTTPServer):
            request_queue_size = 128        # up to 32 concurrent streams connect at once
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def answer(self, handler, body):
        prompt = prompt_text(body)
        max_tokens = int(body.get("max_tokens") or 16)
        custom = self.config.get("responder", lambda body: None)(body)
        prompt_tokens = max(1, len(prompt) // 4)
        cached = 0
        if self.config.get("prefix_cache"):
            prefix = prompt[:64]
            with self.lock:
                cached = prompt_tokens // 2 if prefix in self.seen_prefixes else 0
                self.seen_prefixes.add(prefix)
        if custom is not None:
            text = custom.get("text", "")
            pieces = [p + " " for p in text.split(" ")] if text else []
            if pieces:
                pieces[-1] = pieces[-1][:-1]
            ids = reply_ids(prompt + text, max(1, len(pieces)))
        else:
            count = min(max_tokens, int(self.config.get("length", max_tokens)))
            ids = reply_ids(prompt, count)
            pieces = [w + " " for w in words_for(ids)]
            custom = {}
        if (self.config.get("diverge_at") is not None and self.config["diverge_at"] < len(ids)
                and self.config.get("diverge_match", "") in prompt):
            ids[self.config["diverge_at"]] += 1
        completion = len(ids)
        rate = float(self.config.get("prefill_rate", 10000.0))
        strix = {"queue_ms": 1.0, "prefill_ms": round(prompt_tokens / rate * 1000.0, 3),
                 "decode_ms": float(completion * self.config.get("decode_ms_per_token", 2.0))}
        if body.get("return_token_ids") and not self.config.get("no_ids"):
            strix["token_ids"] = ids
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": prompt_tokens + completion,
                 "prompt_tokens_details": {"cached_tokens": cached}}
        finish = "tool_calls" if custom.get("tool_calls") else ("length" if completion >= max_tokens else "stop")
        headers = {"X-TensorMeld-Replica": "halo-1", "X-TensorMeld-Prefill": "local"}
        if not body.get("stream"):
            message = {"role": "assistant", "content": "".join(pieces)}
            if custom.get("tool_calls"):
                message["tool_calls"] = custom["tool_calls"]
            result = {"id": "chatcmpl-fixture", "object": "chat.completion", "model": self.config["model"],
                      "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
            if not self.config.get("no_usage"):
                result.update(usage=usage, strix=strix)
            handler._json(200, result, headers)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        for key, item in headers.items():
            handler.send_header(key, item)
        handler.send_header("Connection", "close")
        handler.end_headers()
        delay = float(self.config.get("token_delay", 0))

        def send(value):
            handler.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
            handler.wfile.flush()
        try:
            handler.wfile.write(b": fixture stream\n\n")
            send({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            if custom.get("reasoning"):
                send({"choices": [{"index": 0, "delta": {"reasoning_content": custom["reasoning"]}}]})
            for index, piece in enumerate(pieces):
                if delay:
                    time.sleep(delay)
                if index == self.config.get("stall_after"):
                    time.sleep(30)
                    return
                if index == self.config.get("truncate_after"):
                    handler.close_connection = True
                    return
                send({"choices": [{"index": 0, "delta": {"content": piece}}]})
            for index, call in enumerate(custom.get("tool_calls") or []):
                send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": call.get("id", f"call_{index}"),
                      "type": "function", "function": {"name": call["function"]["name"], "arguments": ""}}]}}]})
                arguments = call["function"]["arguments"]
                for start in range(0, len(arguments), 7):
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "function": {"arguments": arguments[start:start + 7]}}]}}]})
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            if not self.config.get("no_usage") and (body.get("stream_options") or {}).get("include_usage"):
                send({"choices": [], "usage": usage, "strix": strix})
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except OSError:
            pass
        handler.close_connection = True
