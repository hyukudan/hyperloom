"""Prometheus re-export of router and engine telemetry (text format 0.0.4).

Only values the pinned adapters parse are exported; unavailable values are
omitted, never written as zero. Each source reports hyperloom_source_up and its
scrape duration. The listener is loopback-only: LAN exposure is a later phase.
"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import socket
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from . import __version__, secrets
from .adapters import engine, router
from .config import listen as parse_listen
from .paths import repo_root


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
MAX_RESPONSE = 1024 * 1024
TIMEOUT_S = 2
DEFAULT_LISTEN = "127.0.0.1:18701"

_ENGINE_COUNTERS = {"completed", "failed", "cancelled", "prompt_tokens", "cached_tokens",
                    "generated_tokens", "busy_seconds", "batch_steps", "batch_rows", "group_exchanges"}
_HELP = {
    "hyperloom_build_info": "hyperloom version and pinned component commits.",
    "hyperloom_source_up": "1 when the telemetry source answered with a supported document.",
    "hyperloom_source_scrape_seconds": "Time taken to fetch and parse the source.",
    "hyperloom_router_waiting": "Requests waiting in the router queue.",
    "hyperloom_router_rejected_total": "Requests rejected by the router.",
    "hyperloom_router_held": "1 when the router holds admission.",
    "hyperloom_router_replica_up": "1 when the router reports the replica state up.",
    "hyperloom_router_replica_admitting": "1 when the replica admits requests.",
    "hyperloom_router_replica_inflight": "Requests in flight on the replica.",
    "hyperloom_router_replica_max_inflight": "Configured in-flight limit of the replica.",
    "hyperloom_router_replica_assigned_total": "Requests assigned to the replica.",
    "hyperloom_router_replica_completed_total": "Requests the replica completed.",
    "hyperloom_router_replica_failed_total": "Requests the replica failed.",
    "hyperloom_router_replica_last_health_seconds": "Latency of the last replica health check.",
    "hyperloom_router_prefill_placements_total": "Prefill placement decisions by kind.",
    "hyperloom_router_prefill_offload_seconds_total": "Time spent in offloaded prefills.",
    "hyperloom_engine_info": "Engine model, state and trellis identity from /api/status.",
    "hyperloom_engine_uptime_seconds": "Engine uptime.",
    "hyperloom_engine_context_tokens": "Engine context length.",
    "hyperloom_engine_slots": "Engine slot and pool figures by kind.",
    "hyperloom_engine_memory_bytes": "Engine memory figures by kind.",
    "hyperloom_engine_group_bytes_sent_total": "Bytes sent by this rank in group exchanges.",
    "hyperloom_engine_group_exchange_seconds_total": "Time spent in group exchanges.",
}
for _field in engine.METRICS:
    _name = "hyperloom_engine_" + _field + ("_total" if _field in _ENGINE_COUNTERS else "")
    _HELP[_name] = "Engine " + engine.METRICS[_field] + " re-exported."


def _get(endpoint, suffix, text=False):
    address = endpoint["address"]
    host = f"[{address}]" if ":" in address else address
    with build_opener(ProxyHandler({})).open(f"http://{host}:{endpoint['port']}{suffix}", timeout=TIMEOUT_S) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("telemetry response exceeds 1 MiB")
    return data.decode("utf-8") if text else json.loads(data)


def running_routers():
    """Router endpoints of profiles with saved state, from orchestrate's state files."""
    result = {}
    for path in sorted(secrets.runtime_dir().glob("up-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        endpoint = (state.get("router") or {}).get("endpoint") if isinstance(state, dict) else None
        result[path.stem[3:]] = endpoint if isinstance(endpoint, dict) else None
    return result


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _value(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


class Exposition:
    def __init__(self):
        self.families = {}

    def add(self, name, kind, labels, value):
        value = _value(value)
        if value is None:
            return
        family = self.families.setdefault(name, (kind, {}))
        key = tuple(sorted((k, str(v)) for k, v in labels.items() if v is not None))
        family[1][key] = value

    def text(self):
        lines = []
        for name in sorted(self.families):
            kind, samples = self.families[name]
            lines += [f"# HELP {name} {_HELP.get(name, name)}", f"# TYPE {name} {kind}"]
            for labels, value in sorted(samples.items()):
                body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{body}}} {value!r}" if body else f"{name} {value!r}")
        return "\n".join(lines) + "\n"


def _replica_endpoint(raw):
    url = urlsplit(raw if "://" in raw else "http://" + raw)
    if not url.hostname or url.port is None:
        raise ValueError("replica endpoint requires host and port")
    return {"address": url.hostname, "port": url.port}


def _timed(fetch, endpoint, suffix, text=False):
    start = time.monotonic()
    try:
        return fetch(endpoint, suffix, text), None, time.monotonic() - start
    except (OSError, ValueError) as exc:
        return None, exc, time.monotonic() - start


def _source(out, labels, ok, elapsed):
    out.add("hyperloom_source_up", "gauge", labels, 1 if ok else 0)
    out.add("hyperloom_source_scrape_seconds", "gauge", labels, elapsed)


def _router(out, profile, raw):
    parsed = router.parse_status(raw)
    base = {"profile": profile}
    out.add("hyperloom_router_waiting", "gauge", base, parsed.waiting)
    out.add("hyperloom_router_rejected_total", "counter", base, parsed.rejected)
    out.add("hyperloom_router_held", "gauge", base, parsed.held)
    for replica in parsed.replicas or []:
        if not replica or replica["id"] is None:
            continue
        labels = {**base, "replica": replica["id"], "machine": replica["node"]}
        if replica["state"] is not None:
            out.add("hyperloom_router_replica_up", "gauge", labels, replica["state"] == "up")
        out.add("hyperloom_router_replica_admitting", "gauge", labels, replica["admitting"])
        for key in ("inflight", "max_inflight"):
            out.add("hyperloom_router_replica_" + key, "gauge", labels, replica[key])
        for key in ("assigned", "completed", "failed"):
            out.add(f"hyperloom_router_replica_{key}_total", "counter", labels, replica[key])
        if replica["last_health_ms"] is not None:
            out.add("hyperloom_router_replica_last_health_seconds", "gauge", labels, replica["last_health_ms"] / 1000)
    prefill = parsed.prefill or {}
    for kind, count in (prefill.get("placements") or {}).items():
        out.add("hyperloom_router_prefill_placements_total", "counter", {**base, "placement": kind}, count)
    if prefill.get("offload_ms_total") is not None:
        out.add("hyperloom_router_prefill_offload_seconds_total", "counter", base, prefill["offload_ms_total"] / 1000)
    return [(r["id"], r["node"], r["endpoint"]) for r in parsed.replicas or [] if r and r["id"] and r["endpoint"]]


def _engine_metrics(out, labels, text):
    parsed = engine.parse_metrics(text)
    for field in engine.METRICS:
        counter = field in _ENGINE_COUNTERS
        out.add("hyperloom_engine_" + field + ("_total" if counter else ""),
                "counter" if counter else "gauge", labels, getattr(parsed, field))


def _engine_status(out, labels, raw):
    parsed = engine.parse_status(raw)
    info = {"model": parsed.model, "state": parsed.state,
            "trellis_identity": (parsed.memory or {}).get("trellis_identity")}
    out.add("hyperloom_engine_info", "gauge", {**labels, **info}, 1)
    out.add("hyperloom_engine_uptime_seconds", "gauge", labels, parsed.uptime_seconds)
    out.add("hyperloom_engine_context_tokens", "gauge", labels, parsed.context_tokens)
    for kind, value in (parsed.slots or {}).items():
        out.add("hyperloom_engine_slots", "gauge", {**labels, "kind": kind}, value)
    for kind, value in (parsed.memory or {}).items():
        if kind != "trellis_identity":
            out.add("hyperloom_engine_memory_bytes", "gauge", {**labels, "kind": kind}, value)
    group = parsed.group or {}
    out.add("hyperloom_engine_group_bytes_sent_total", "counter", labels, group.get("bytes_sent"))
    out.add("hyperloom_engine_group_exchange_seconds_total", "counter", labels, group.get("exchange_seconds"))


def _build_info(out):
    labels = {"version": __version__}
    try:
        components = json.loads((repo_root() / "versions.lock").read_text(encoding="utf-8"))["components"]
        labels.update(engine_commit=components["engine"]["commit"], tensormeld_commit=components["tensormeld"]["commit"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass  # Unknown commits are left out rather than invented.
    out.add("hyperloom_build_info", "gauge", labels, 1)


def collect(routers=None, fetch=_get, profile=None):
    """Return the exposition text for {profile: router endpoint or None}."""
    routers = running_routers() if routers is None else routers
    if profile is not None:
        routers = {profile: routers.get(profile)}
    out = Exposition()
    _build_info(out)
    with ThreadPoolExecutor(max_workers=8) as pool:
        replicas = []
        for name, endpoint in sorted(routers.items()):
            labels = {"source": "router", "profile": name}
            if endpoint is None:
                _source(out, labels, False, None)
                continue
            raw, error, elapsed = _timed(fetch, endpoint, "/api/status")
            try:
                if error is None:
                    replicas += [(name, *row) for row in _router(out, name, raw)]
            except ValueError as exc:
                error = exc
            _source(out, labels, error is None, elapsed)
        jobs = []
        for name, rid, node, raw_endpoint in replicas:
            labels = {"profile": name, "replica": rid, "machine": node}
            try:
                endpoint = _replica_endpoint(raw_endpoint)
            except ValueError:
                endpoint = None
            for source, suffix, text, apply in (("engine_metrics", "/metrics", True, _engine_metrics),
                                                ("engine_status", "/api/status", False, _engine_status)):
                future = pool.submit(_timed, fetch, endpoint, suffix, text) if endpoint else None
                jobs.append((labels, source, future, apply))
        for labels, source, future, apply in jobs:
            raw, error, elapsed = future.result() if future else (None, ValueError("endpoint"), None)
            try:
                if error is None:
                    apply(out, labels, raw)
            except ValueError as exc:
                error = exc
            _source(out, {**labels, "source": source}, error is None, elapsed)
    return out.text()


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen=DEFAULT_LISTEN, *, collector=collect):
        endpoint = parse_listen(listen, "metrics listen")
        if not ipaddress.ip_address(endpoint["address"]).is_loopback:
            raise ValueError("metrics listen must be a loopback address; LAN exposure is a later phase")
        if ":" in endpoint["address"]:
            self.address_family = socket.AF_INET6
        self.collector = collector
        super().__init__((endpoint["address"], endpoint["port"]), Handler)
        host = f"[{endpoint['address']}]" if ":" in endpoint["address"] else endpoint["address"]
        self.allowed_hosts = {f"{host}:{self.server_port}", f"localhost:{self.server_port}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Hyperloom"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def _send(self, status, body, content_type="text/plain; charset=utf-8"):
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 405:
            self.send_header("Allow", "GET")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._send(403, "Host is not allowed\n")
        elif self.path != "/metrics":
            self._send(404, "not found\n")
        else:
            self._send(200, self.server.collector(), CONTENT_TYPE)

    def _method(self):
        self._send(405, "only GET /metrics\n")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_HEAD = _method


def serve(listen=DEFAULT_LISTEN, profile=None):
    with MetricsServer(listen, collector=lambda: collect(profile=profile)) as server:
        print(f"Hyperloom metrics: http://{sorted(server.allowed_hosts)[0]}/metrics", flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
