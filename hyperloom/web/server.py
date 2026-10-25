"""Loopback-only HTTP transport for the portal."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os
import re
import secrets
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import select
import socket

from .api import API, APIError, MUTATIONS, redact
from . import recipes as recipe_web
from . import features as feature_registry


MAX_BODY = 64 * 1024
STATIC_DIR = Path(__file__).with_name("static")
# Feature pages and their assets: only the exact names listed in <dir>/manifest.json ({"files": [...]}) are
# served (no directory serving; names cannot contain separators, "..", ":" or stream suffixes).
ASSET_DIRS = ("pages", "assets")
ASSET_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*\.(?:js|css|svg|png|json|webmanifest)")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json; charset=utf-8", ".webmanifest": "application/manifest+json"}


def manifest(static_dir, directory):
    """The exact file names a static feature directory may serve, in manifest order. The directory and its
    manifest must be plain entries inside the static root (no link, junction or reparse point) or nothing is
    listed; both the shell's script tags and asset requests go through here."""
    root = Path(static_dir).resolve()
    folder = root / directory
    listing = folder / "manifest.json"
    try:
        if (folder.is_symlink() or folder.resolve() != folder or folder.parent != root
                or listing.is_symlink() or listing.resolve() != listing):
            return ()
        listed = json.loads(listing.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    if not isinstance(listed, list):
        return ()
    return tuple(name for name in listed if isinstance(name, str) and ASSET_NAME.fullmatch(name) and name != "manifest.json")


def page_tags(static_dir):
    """<link>/<script> tags for the listed feature page files (manifest order = load order)."""
    names = manifest(static_dir, "pages")
    return "".join(['<link rel="stylesheet" href="/pages/' + n + '">' for n in names if n.endswith(".css")] +
                   ['<script src="/pages/' + n + '" defer></script>' for n in names if n.endswith(".js")])


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
    request_queue_size = 64     # feature pages poll several documents at once; the default backlog of 5 refuses bursts

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
        if getattr(self.api, "sampler", None) is not None:
            from hyperloom import telemetry
            telemetry.stop(self.api)
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
            if parsed.path == "/hyperloom/api/stream":
                # Live snapshot stream (server-sent events); same Host and target checks as every GET.
                from .features import integrations
                integrations.serve_stream(self, head)
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
                registry = feature_registry.registry()
                if endpoint in MUTATIONS or endpoint in registry.posts or endpoint in registry.jobs:
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
            root = self.server.static_dir.resolve()
            parts = path[len("/hyperloom"):].split("/") if path.startswith("/hyperloom/") else path.split("/")
            if name is None and len(parts) == 3 and parts[0] == "" and parts[1] in ASSET_DIRS:
                # The feature directory itself must be a plain directory inside the static root (no link,
                # junction or reparse point leading elsewhere) before its manifest is even read.
                if parts[2] in manifest(root, parts[1]):
                    candidate = root / parts[1] / parts[2]
                    if not candidate.is_symlink():
                        root = root / parts[1]
                        name = parts[2]
            if name is None and path == "/sw.js" and "sw.js" in manifest(root, "assets"):
                # The installable shell's worker is served at the root so that its scope is the whole portal.
                if not (root / "assets" / "sw.js").is_symlink():
                    root, name = root / "assets", "sw.js"
            if name is None:
                self._error(404, "not found")
                return
            source = (root / name).resolve()
            if source.parent != root or not source.is_file():
                self._error(404, "not found")
                return
            body = source.read_bytes()
            content_type = CONTENT_TYPES.get(source.suffix)
            if content_type is None:
                self._error(404, "not found")
                return
            if name == "index.html" and source.parent == self.server.static_dir.resolve():
                token = self.server.csrf_token
                html = body.decode("utf-8").replace("__HYPERLOOM_CSRF__", token)
                # The meta works even if a static page has no placeholder.
                meta = '<meta name="hyperloom-csrf" content="' + token + '">'
                html = html.replace("</head>", meta + "</head>") if "</head>" in html else meta + html
                # Feature pages register themselves before app.js renders (deferred scripts run in order).
                tags = page_tags(self.server.static_dir)
                anchor = '<script src="/app.js" defer></script>'
                html = html.replace(anchor, tags + anchor) if anchor in html else html.replace("</head>", tags + "</head>")
                body = html.encode("utf-8")
            self._send(200, body, content_type, head=head)
        except APIError as exc:
            self._error(exc.status, str(exc))
        except (ValueError, UnicodeError) as exc:
            self._error(400, str(exc))
        except (OSError, RuntimeError) as exc:
            self._error(503, str(exc))

    def _drain(self):
        """Before an early POST refusal, read and discard a declared body (bounded, short timeout), so the client
        sees the error instead of a reset connection. The body is never parsed."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or int(lengths[0]) > recipe_web.LARGE_BODY_BYTES or self.headers.get_all("Transfer-Encoding")):
            return
        try:
            self.connection.settimeout(2)
            left = int(lengths[0])
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    def do_POST(self):
        self._body_read = False
        if not self._host():
            self._drain()
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
            registry = feature_registry.registry()
            if (action not in MUTATIONS and action not in recipe_web.POSTS and action not in registry.posts
                    and action not in registry.jobs and action != "jobs/renew"):
                raise APIError("unknown mutation", 404)
            self._body_read = True
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
            if action == "jobs/renew":
                status, document = self.server.api.renew_jobs(arguments)
                self._send(status, document)
                return
            if action in registry.posts and action not in MUTATIONS:
                status, document = self.server.api.feature_post(action, arguments)
                self._send(status, document)
                return
            self._send(202, self.server.api.mutate(action, arguments))
        except APIError as exc:
            self._drain()
            self._error(exc.status, str(exc))
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            self._drain()
            self._error(400, "invalid JSON or arguments: " + str(exc))
        except (OSError, RuntimeError) as exc:
            self._drain()
            self._error(503, str(exc))

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
        # Background samplers feed the history store (features' collectors); HYPERLOOM_TELEMETRY=0 disables them.
        if os.environ.get("HYPERLOOM_TELEMETRY", "1") != "0":
            from hyperloom import telemetry
            telemetry.start(server.api)
        print("Hyperloom portal: " + server.origin, flush=True)
        if not no_browser:
            webbrowser.open(server.origin + "/")
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
