"""Loopback-only HTTP transport for the portal."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os
import secrets
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import select
import socket

from .api import API, APIError, MUTATIONS, redact
from . import recipes as recipe_web


MAX_BODY = 64 * 1024
STATIC_DIR = Path(__file__).with_name("static")


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=18700, *, api=None, static_dir=None):
        self.api = api if api is not None else API()
        self.static_dir = Path(static_dir) if static_dir else STATIC_DIR
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


    def server_close(self):
        # Recipe children (and their whole process trees) never outlive the portal.
        runner = getattr(self.api, "recipes", None)
        if runner is not None:
            runner.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "Hyperloom"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        # Request targets, bodies and headers may contain secrets.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", head=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if not head:
            self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": redact(message)})

    def _host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._error(403, "Host is not allowed")
            return False
        # Only origin-form requests; never accept a proxy request target.
        if not self.path.startswith("/") or self.path.startswith("//"):
            self._error(400, "invalid request target")
            return False
        return True

    def do_HEAD(self):
        self._get(head=True)

    def do_GET(self):
        self._get()

    def _get(self, head=False):
        if not self._host():
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon", head=head)
                return
            if parsed.path.startswith("/hyperloom/api/"):
                endpoint = parsed.path[len("/hyperloom/api/"):]
                if endpoint == "recipes" or endpoint.startswith("recipes/"):
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
                    if any(len(values) != 1 for values in query.values()):
                        raise APIError("duplicate query argument")
                    if endpoint in MUTATIONS:
                        raise APIError("mutation requires POST", 405)
                    status, document = self.server.api.recipe_get(endpoint, {k: v[0] for k, v in query.items()})
                    self._send(status, document, head=head)
                    return
                if endpoint in MUTATIONS:
                    self._error(405, "mutation requires POST")
                    return
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
                if any(len(values) != 1 for values in query.values()):
                    raise APIError("duplicate query argument")
                self._send(200, self.server.api.read(endpoint, {key: values[0] for key, values in query.items()}), head=head)
                return
            # An explicit file allow-list prevents directory listings, symlink escape,
            # alternate separators, encoded traversal and accidental source serving.
            path = unquote(parsed.path)
            files = {"/": "index.html", "/index.html": "index.html",
                     "/hyperloom/": "index.html", "/hyperloom/index.html": "index.html",
                     "/app.css": "app.css", "/app.js": "app.js", "/recipes.js": "recipes.js",
                     "/hyperloom/app.css": "app.css", "/hyperloom/app.js": "app.js",
                     "/hyperloom/recipes.js": "recipes.js"}
            name = files.get(path)
            if name is None:
                self._error(404, "not found")
                return
            root = self.server.static_dir.resolve()
            source = (root / name).resolve()
            if source.parent != root or not source.is_file():
                self._error(404, "not found")
                return
            body = source.read_bytes()
            content_type = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[source.suffix] + "; charset=utf-8"
            if name == "index.html":
                token = self.server.csrf_token
                html = body.decode("utf-8").replace("__HYPERLOOM_CSRF__", token)
                # The meta works even if a static page has no placeholder.
                meta = '<meta name="hyperloom-csrf" content="' + token + '">'
                html = html.replace("</head>", meta + "</head>") if "</head>" in html else meta + html
                body = html.encode("utf-8")
            self._send(200, body, content_type, head=head)
        except APIError as exc:
            self._error(exc.status, str(exc))
        except (ValueError, UnicodeError) as exc:
            self._error(400, str(exc))
        except (OSError, RuntimeError) as exc:
            self._error(503, str(exc))
        except Exception as exc:          # an answer, never a dropped connection
            self._error(500, f"internal error ({type(exc).__name__})")

    def do_POST(self):
        if not self._host():
            return
        try:
            host = self.headers["Host"]
            origins = self.headers.get_all("Origin", [])
            if origins != ["http://" + host]:
                raise APIError("Origin must equal the UI origin", 403)
            tokens = self.headers.get_all("X-Hyperloom-CSRF", [])
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), self.server.csrf_token.encode("ascii")):
                raise APIError("invalid CSRF token", 403)
            types = self.headers.get_all("Content-Type", [])
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise APIError("Content-Type must be application/json", 415)
            if self.headers.get_all("Transfer-Encoding"):
                raise APIError("Transfer-Encoding is not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise APIError("one Content-Length is required", 411)
            size = int(lengths[0])
            target = urlsplit(self.path).path[len("/hyperloom/api/"):]
            limit = recipe_web.LARGE_BODY_BYTES if target in recipe_web.LARGE_BODY else MAX_BODY
            if size > limit:
                raise APIError(f"body exceeds {limit // 1024} KiB", 413)
            if size == 0:
                raise APIError("JSON object required")
            parsed = urlsplit(self.path)
            if parsed.query or not parsed.path.startswith("/hyperloom/api/"):
                raise APIError("unknown mutation", 404)
            action = parsed.path[len("/hyperloom/api/"):]
            if action not in MUTATIONS and action not in recipe_web.POSTS:
                raise APIError("unknown mutation", 404)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise APIError("incomplete body")
            def invalid_constant(_):
                raise ValueError("nonfinite JSON number")
            arguments = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid_constant)
            if not isinstance(arguments, dict):
                raise APIError("JSON object required")
            if action in recipe_web.POSTS:
                status, document = self.server.api.recipe_post(action, arguments, cancelled=self._client_gone)
                self._send(status, document)
                return
            self._send(202, self.server.api.mutate(action, arguments))
        except APIError as exc:
            self._error(exc.status, str(exc))
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            self._error(400, "invalid JSON or arguments: " + str(exc))
        except (OSError, RuntimeError) as exc:
            self._error(503, str(exc))
        except Exception as exc:          # an answer, never a dropped connection
            self._error(500, f"internal error ({type(exc).__name__})")

    def _client_gone(self):
        """True once the client closed its connection (a synchronous recipe request is then cancelled)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _method(self):
        if self._host():
            self._error(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = _method


def create_server(port=18700, *, api=None, static_dir=None):
    """Create a loopback server; port=0 is useful for isolated tests."""
    return PortalServer(port, api=api, static_dir=static_dir)


def serve(port=18700, no_browser=False):
    with create_server(port) as server:
        # Mutations survive the portal process; test fixtures with patched backends stay in-process.
        server.api.detached = os.environ.get("HYPERLOOM_UI_INPROCESS") != "1"
        print("Hyperloom portal: " + server.origin, flush=True)
        if not no_browser:
            webbrowser.open(server.origin + "/")
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
