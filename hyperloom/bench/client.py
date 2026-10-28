"""Streaming chat-completions client for benchmarks (stdlib only).

One request per call on its own connection (``http.client``; no proxies, no redirects, no retries). The key is
only ever placed in the ``Authorization`` header of the request to the configured address; a 3xx answer is a
failure ("redirect refused") and nothing is sent anywhere else. Responses are bounded: JSON bodies 4 MiB, one SSE
line 1 MiB, a whole stream 16 MiB. A watchdog thread shuts the socket down when the run is cancelled or the
request's own timeout passes, so a blocked read returns promptly and the stream is closed.

Client measurements use ``time.perf_counter()`` (``_clock``) relative to the moment the request was sent; not
``time.monotonic()``, which ticks every 15.6 ms on Windows before Python 3.13 and would read a fast TTFT as 0:
``headers_s`` (status line and headers received), ``first_event_s`` (first ``data:`` event), ``ttft_s``
(first event carrying content, reasoning or a tool-call delta: the client TTFT), ``last_output_s`` (the last such
event) and ``end_s`` (last byte).
Engine figures come only from the final usage / ``strix`` object of the response.
"""

from dataclasses import dataclass, field
import _socket
import http.client
import json
import socket
import ssl
import threading
import time


JSON_CAP = 4 * 1024 * 1024
LINE_CAP = 1024 * 1024
STREAM_CAP = 16 * 1024 * 1024
CONNECT_TIMEOUT_S = 10.0
KEPT_HEADERS = ("x-tensormeld-replica", "x-tensormeld-prefill", "x-tensormeld-route-id", "x-tensormeld-request-id",
                "content-type")
EVENT_TIMES = 32
_clock = time.perf_counter     # measurement clock: sub-microsecond everywhere (see the module docstring)


class ClientError(Exception):
    pass


@dataclass(frozen=True)
class Endpoint:
    """Where requests go. ``key`` comes from the secret store and never leaves this object except as a header."""
    scheme: str
    host: str
    port: int
    key: str = field(default=None, repr=False)

    def __post_init__(self):
        if self.scheme not in ("http", "https"):
            raise ValueError("scheme must be http or https")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535")

    @property
    def url(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def connection(self, timeout, cancelled):
        """An HTTP connection on an already connected socket (see ``connect``); it never reconnects by itself."""
        sock = connect(self, timeout, cancelled)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        connection.sock = sock
        connection.auto_open = 0
        return connection

    def headers(self, extra=None):
        headers = {"Accept": "application/json, text/event-stream", "User-Agent": "hyperloom-bench"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        headers.update(extra or {})
        return headers


_HELPERS = threading.BoundedSemaphore(64)


def _open_socket(host, port, timeout):
    error = None
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[:2]:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)    # timings must not wait on small-packet delays
            return sock
        except OSError as exc:
            sock.close()
            error = exc
    raise error or ClientError("no address for " + host)


def connect(endpoint, timeout, cancelled):
    """A connected (and for https, TLS-verified) socket. Name resolution, TCP connect and the TLS handshake run in
    one helper thread; the caller waits in 0.25 s slices and gives up on cancellation or after ``timeout``. An
    abandoned helper closes its socket when it finishes; at most 64 helpers exist at once."""
    if not _HELPERS.acquire(blocking=False):
        raise ClientError("too many pending connections")
    box, done, abandoned = {}, threading.Event(), threading.Event()

    def work():
        try:
            sock = _open_socket(endpoint.host, endpoint.port, timeout)
            if endpoint.scheme == "https":
                try:
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=endpoint.host)
                except BaseException:
                    sock.close()
                    raise
            box["sock"] = sock
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()
            if abandoned.is_set() and "sock" in box:
                box["sock"].close()
            _HELPERS.release()
    threading.Thread(target=work, name="hyperloom-bench-connect", daemon=True).start()
    end = time.monotonic() + timeout
    while not done.wait(0.25):
        if cancelled() or time.monotonic() >= end:
            abandoned.set()
            if done.is_set() and "sock" in box:
                box["sock"].close()
            raise ClientError("cancelled" if cancelled() else "connection timed out")
    if "error" in box:
        error = box["error"]
        raise ClientError(f"connection failed: {type(error).__name__}: {error}")
    return box["sock"]


class SSEParser:
    """Incremental server-sent-events parser: feed lines, get the data payloads of complete events."""

    def __init__(self):
        self._data = []

    def line(self, raw):
        """Feed one line (bytes, with or without its end of line); returns a list of complete event payloads."""
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if text == "":
            return self.flush()
        if text.startswith(":"):
            return []
        name, _, value = text.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        return []

    def flush(self):
        if not self._data:
            return []
        payload = "\n".join(self._data)
        self._data = []
        return [payload]


@dataclass
class Reply:
    status: int = None
    error: str = None
    headers: dict = field(default_factory=dict)
    headers_s: float = None
    first_event_s: float = None
    ttft_s: float = None
    last_output_s: float = None
    end_s: float = None
    events: int = 0
    content_events: int = 0
    text: str = ""
    reasoning: str = ""
    finish_reason: str = None
    usage: dict = None
    strix: dict = None
    tool_calls: list = None
    event_times: list = field(default_factory=list)
    cancelled: bool = False
    sent_at: float = None       # _clock() when the request was written (absolute, for windows across streams)

    @property
    def ok(self):
        return self.status == 200 and self.error is None

    @property
    def completion_tokens(self):
        value = (self.usage or {}).get("completion_tokens")
        return value if type(value) is int and value >= 0 else None

    @property
    def prompt_tokens(self):
        value = (self.usage or {}).get("prompt_tokens")
        return value if type(value) is int and value >= 0 else None

    @property
    def cached_tokens(self):
        details = (self.usage or {}).get("prompt_tokens_details")
        value = details.get("cached_tokens") if isinstance(details, dict) else None
        if value is None:
            value = (self.strix or {}).get("cached_tokens")
        return value if type(value) is int and value >= 0 else None

    @property
    def imported_tokens(self):
        value = (self.strix or {}).get("imported_tokens")
        if value is None:
            value = (self.usage or {}).get("imported_tokens")
        return value if type(value) is int and value >= 0 else None

    @property
    def token_ids(self):
        ids = (self.strix or {}).get("token_ids")
        if isinstance(ids, list) and all(type(v) is int and v >= 0 for v in ids):
            return ids
        return None

    def engine_ms(self, name):
        value = (self.strix or {}).get(name)
        return float(value) if type(value) in (int, float) and value >= 0 else None


class _Watchdog:
    """Shuts a connection's socket down on cancellation or when the request's time is up."""

    def __init__(self, connection, cancelled, deadline):
        # The socket itself is kept: http.client detaches connection.sock from a "Connection: close" response.
        self.sock, self.cancelled_fn, self.deadline = connection.sock, cancelled, deadline
        self.reason = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hyperloom-bench-watchdog", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._done.wait(0.25):
            try:
                cancelled = self.cancelled_fn()
            except Exception:       # a failing check never kills the watchdog; the deadline still applies
                cancelled = False
            if cancelled:
                self.reason = "cancelled"
            elif time.monotonic() >= self.deadline:
                self.reason = "request timed out"
            else:
                continue
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    # A blocked read on some platforms only returns once the descriptor itself is closed; the
                    # response's file object still references the socket, so close it at the descriptor level.
                    _socket.socket.close(self.sock)
                except OSError:
                    pass
            return

    def stop(self):
        self._done.set()
        self._thread.join(1.0)


def _read_capped(response, cap):
    data = response.read(cap + 1)
    if len(data) > cap:
        raise ClientError(f"response exceeds {cap // (1024 * 1024)} MiB")
    return data


def get_json(endpoint, path, *, timeout=10.0, cancelled=lambda: False):
    """GET a small JSON document (e.g. /v1/models). Raises ClientError."""
    started = time.monotonic()
    connection = endpoint.connection(min(CONNECT_TIMEOUT_S, timeout), cancelled)
    watchdog = None
    try:
        connection.sock.settimeout(timeout)
        watchdog = _Watchdog(connection, cancelled, started + timeout)
        connection.request("GET", path, headers=endpoint.headers())
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ClientError(f"redirect refused (HTTP {response.status})")
        data = _read_capped(response, JSON_CAP)
        if response.status != 200:
            raise ClientError(f"HTTP {response.status}" + _error_text(data))
        return json.loads(data)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        reason = watchdog.reason if watchdog and watchdog.reason else f"{type(exc).__name__}: {exc}"
        raise ClientError(reason) from None
    finally:
        if watchdog:
            watchdog.stop()
        connection.close()


def _error_text(data):
    try:
        body = json.loads(data)
        message = body.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        return ": " + str(message)[:200] if message else ""
    except (ValueError, AttributeError):
        return ""


def _merge_tool_calls(calls, deltas):
    for delta in deltas if isinstance(deltas, list) else []:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index", len(calls))
        if not isinstance(index, int) or not 0 <= index < 64:
            continue
        while len(calls) <= index:
            calls.append({"id": None, "name": "", "arguments": ""})
        call = calls[index]
        if isinstance(delta.get("id"), str):
            call["id"] = delta["id"]
        function = delta.get("function") if isinstance(delta.get("function"), dict) else {}
        if isinstance(function.get("name"), str):
            call["name"] += function["name"]
        if isinstance(function.get("arguments"), str):
            call["arguments"] += function["arguments"]


def _apply_event(reply, event, now, on_delta):
    """Fold one decoded stream event into the reply."""
    if not isinstance(event, dict):
        return
    if isinstance(event.get("usage"), dict):
        reply.usage = event["usage"]
    if isinstance(event.get("strix"), dict):
        reply.strix = event["strix"]
    choices = event.get("choices") if isinstance(event.get("choices"), list) else []
    for choice in choices[:1]:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        source = delta or message
        thought = source.get("reasoning_content") or source.get("reasoning") or ""
        piece = source.get("content") or choice.get("text") or ""
        thought = thought if isinstance(thought, str) else ""
        piece = piece if isinstance(piece, str) else ""
        tools = source.get("tool_calls")
        if thought or piece or tools:
            if reply.ttft_s is None:
                reply.ttft_s = now
            reply.last_output_s = now
            reply.content_events += 1
        if thought:
            reply.reasoning += thought
        if piece:
            reply.text += piece
        if tools:
            if reply.tool_calls is None:
                reply.tool_calls = []
            if delta:
                _merge_tool_calls(reply.tool_calls, tools)
            else:   # a complete (non-streamed) message
                for call in tools if isinstance(tools, list) else []:
                    function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
                    reply.tool_calls.append({"id": call.get("id") if isinstance(call, dict) else None,
                                             "name": str(function.get("name") or ""),
                                             "arguments": function.get("arguments") if isinstance(function.get("arguments"), str)
                                             else json.dumps(function.get("arguments"))})
        if isinstance(choice.get("finish_reason"), str):
            reply.finish_reason = choice["finish_reason"]
        if on_delta and (thought or piece):
            on_delta(piece, thought, now)


def chat(endpoint, body, *, timeout=600.0, cancelled=lambda: False, request_id=None, on_delta=None,
         path="/v1/chat/completions"):
    """POST a chat completion (streamed when body["stream"]); never raises for HTTP/transport failures: the Reply
    carries ``error`` and whatever was measured before it."""
    reply = Reply()
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = endpoint.headers({"Content-Type": "application/json"})
    if request_id:
        headers["X-Request-Id"] = request_id
    watchdog = None
    sent, deadline = _clock(), time.monotonic() + timeout     # the watchdog's deadline counts the connect too
    connection = None
    try:
        if cancelled():
            reply.error, reply.cancelled = "cancelled", True
            return reply
        try:
            connection = endpoint.connection(min(CONNECT_TIMEOUT_S, timeout), cancelled)
        except ClientError as exc:
            reply.error, reply.cancelled = str(exc), str(exc) == "cancelled"
            return reply
        connection.sock.settimeout(max(1.0, timeout))
        watchdog = _Watchdog(connection, cancelled, deadline)
        sent = _clock()
        reply.sent_at = sent
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        reply.headers_s = _clock() - sent
        reply.status = response.status
        reply.headers = {k.lower(): v for k, v in response.getheaders() if k.lower() in KEPT_HEADERS}
        if 300 <= response.status < 400:
            reply.error = f"redirect refused (HTTP {response.status})"
            return reply
        if response.status != 200:
            reply.error = f"HTTP {response.status}" + _error_text(_read_capped(response, JSON_CAP))
            return reply
        streaming = "text/event-stream" in reply.headers.get("content-type", "") or (
            body.get("stream") and "json" not in reply.headers.get("content-type", ""))
        if not streaming:
            data = _read_capped(response, JSON_CAP)
            now = _clock() - sent
            reply.first_event_s = now
            _apply_event(reply, json.loads(data), now, on_delta)
            reply.events = 1
            reply.end_s = _clock() - sent
            return reply
        parser, total, done = SSEParser(), 0, False
        while not done:
            line = response.readline(LINE_CAP + 1)
            if not line or watchdog.reason:     # closed, or cancelled / timed out (buffered data is not read on)
                break
            total += len(line)
            if len(line) > LINE_CAP or total > STREAM_CAP:
                raise ClientError("stream exceeds its size cap")
            payloads = parser.line(line) if line.endswith(b"\n") else parser.line(line) + parser.flush()
            for data in payloads:
                now = _clock() - sent
                if data.strip() == "[DONE]":
                    done = True
                    break
                reply.events += 1
                if reply.first_event_s is None:
                    reply.first_event_s = now
                if len(reply.event_times) < EVENT_TIMES:
                    reply.event_times.append(round(now, 4))
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("error"):
                    reply.error = "stream error" + _error_text(json.dumps(event).encode())
                _apply_event(reply, event, now, on_delta)
        if not done:
            for data in parser.flush():
                if data.strip() != "[DONE]":
                    try:
                        _apply_event(reply, json.loads(data), _clock() - sent, on_delta)
                    except ValueError:
                        pass
        reply.end_s = _clock() - sent
        if watchdog.reason and not done:
            reply.error, reply.cancelled = watchdog.reason, watchdog.reason == "cancelled"
        elif not done and reply.finish_reason is None and reply.error is None:
            reply.error = "stream ended before completion (no finish reason and no end marker)"
    except (OSError, http.client.HTTPException, ValueError, ClientError) as exc:
        reply.end_s = _clock() - sent
        if watchdog is not None and watchdog.reason:
            reply.error, reply.cancelled = watchdog.reason, watchdog.reason == "cancelled"
        else:
            reply.error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if watchdog is not None:
            watchdog.stop()
        if connection is not None:
            connection.close()
    return reply
