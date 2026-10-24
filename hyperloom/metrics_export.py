"""Prometheus re-export of router and engine telemetry (text format 0.0.4).

Only values the pinned adapters parse are exported; unavailable values are
omitted, never written as zero. Each source reports hyperloom_source_up and its
scrape duration. The listener is loopback-only: LAN exposure is a later phase.
"""

from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import socket
import threading
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
# Whole-scrape budget, below Prometheus' default 10 s scrape timeout. A source still running at the
# deadline is reported down; its worker thread is abandoned (daemon) and ends at its own socket timeout.
SCRAPE_DEADLINE_S = 8
WORKERS = 8
# Concurrent scrapes served at once; further requests get 503 instead of piling up worker threads.
MAX_CONCURRENT_SCRAPES = 2
DEFAULT_LISTEN = "127.0.0.1:18701"
# Everything a fetch or a parse may raise for a bad upstream: refused/reset sockets and timeouts (OSError),
# bad JSON/UTF-8/schema (ValueError, AdapterError), malformed HTTP (HTTPException: BadStatusLine,
# IncompleteRead, LineTooLong) and endpoints without address/port (KeyError, TypeError).
SOURCE_ERRORS = (OSError, ValueError, HTTPException, KeyError, TypeError)

_ENGINE_COUNTERS = {"completed", "failed", "cancelled", "prompt_tokens", "cached_tokens",
                    "generated_tokens", "busy_seconds", "batch_steps", "batch_rows", "group_exchanges"}
# Engine status slots.* split by unit so one family never mixes counts, tokens and bytes.
_SLOT_COUNTS = ("total", "busy", "free")
_SLOT_POSITIONS = ("context_tokens", "pool_positions", "pool_largest_free")
_HELP = {
    "hyperloom_build_info": "hyperloom version and component commits (a running launch's release, else versions.lock).",
    "hyperloom_source_up": "1 when the telemetry source answered with a supported document.",
    "hyperloom_source_scrape_seconds": "Time taken to fetch and parse the source.",
    "hyperloom_router_waiting": "Requests waiting in the router queue.",
    "hyperloom_router_rejected_total": "Requests rejected by the router.",
    "hyperloom_router_held": "1 when the router holds admission.",
    "hyperloom_router_duplicate_ids": "Replica or prefill service ids the router reported more than once (only the first is exported).",
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
    "hyperloom_router_prefill_service_up": "1 when the router reports the prefill service state up.",
    "hyperloom_router_prefill_service_inflight": "Offloaded prefills in flight on the service, as the router counts them.",
    "hyperloom_router_prefill_service_max_inflight": "Configured in-flight limit of the prefill service.",
    "hyperloom_router_prefill_service_assigned_total": "Prefills the router assigned to the service.",
    "hyperloom_router_prefill_service_completed_total": "Prefills the service completed, as the router counts them.",
    "hyperloom_router_prefill_service_failed_total": "Prefills the service failed, as the router counts them.",
    "hyperloom_engine_info": "Engine model and trellis identity from /api/status.",
    "hyperloom_engine_state": "1 for the engine's current state from /api/status.",
    "hyperloom_engine_uptime_seconds": "Engine uptime.",
    "hyperloom_engine_context_tokens": "Engine context length.",
    "hyperloom_engine_slots": "Engine slot counts by kind (total, busy, free).",
    "hyperloom_engine_slot_positions": "Engine slot context and pool sizes in token positions by kind.",
    "hyperloom_engine_slot_pool_bytes": "Engine slot pool size in bytes.",
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


class _Fetches:
    """Runs fetches on daemon threads, at most WORKERS at a time, against one scrape deadline.

    result() returns (value, error, elapsed). A fetch not finished by the deadline is an error; its
    thread is left to end at its own socket timeout and its late result is discarded."""

    def __init__(self, fetch, deadline):
        self.fetch = fetch
        self.deadline = deadline
        self.slots = threading.BoundedSemaphore(WORKERS)

    def submit(self, endpoint, suffix, text=False):
        job = {"done": threading.Event(), "start": time.monotonic(), "result": None}

        def run():
            started = time.monotonic()
            try:
                if not self.slots.acquire(timeout=max(0.0, self.deadline - started)):
                    raise TimeoutError("scrape deadline reached before the fetch started")
                try:
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("scrape deadline reached before the fetch started")
                    job["result"] = (self.fetch(endpoint, suffix, text), None)
                finally:
                    self.slots.release()
            except SOURCE_ERRORS as exc:
                job["result"] = (None, exc)
            except Exception as exc:  # noqa: BLE001 - a fetch bug marks one source down, never the scrape.
                job["result"] = (None, RuntimeError(f"fetch failed: {type(exc).__name__}"))
            job["elapsed"] = time.monotonic() - job["start"]
            job["done"].set()

        threading.Thread(target=run, name="hyperloom-metrics-fetch", daemon=True).start()
        return job

    def result(self, job):
        if not job["done"].wait(max(0.0, self.deadline - time.monotonic())):
            return None, TimeoutError("scrape deadline exceeded"), time.monotonic() - job["start"]
        value, error = job["result"]
        return value, error, job["elapsed"]


def _source(out, labels, ok, elapsed):
    out.add("hyperloom_source_up", "gauge", labels, 1 if ok else 0)
    out.add("hyperloom_source_scrape_seconds", "gauge", labels, elapsed)


def _unique(rows, out, base, kind):
    """First record per id; later records with an already seen id are counted, not exported."""
    seen, duplicates, result = set(), 0, []
    for row in rows or []:
        if not row or row["id"] is None:
            continue
        if row["id"] in seen:
            duplicates += 1
            continue
        seen.add(row["id"])
        result.append(row)
    if rows is not None:
        out.add("hyperloom_router_duplicate_ids", "gauge", {**base, "kind": kind}, duplicates)
    return result


def _router(out, profile, raw):
    parsed = router.parse_status(raw)
    base = {"profile": profile}
    out.add("hyperloom_router_waiting", "gauge", base, parsed.waiting)
    out.add("hyperloom_router_rejected_total", "counter", base, parsed.rejected)
    out.add("hyperloom_router_held", "gauge", base, parsed.held)
    replicas = _unique(parsed.replicas, out, base, "replica")
    for replica in replicas:
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
    # Prefill services (Phase 3a) are exported from the router's view only: the service itself has no
    # /metrics or /api/status yet (SPEC R13), and its /v1/describe needs the service key.
    for service in _unique(prefill.get("services"), out, base, "prefill_service"):
        labels = {**base, "service": service["id"], "machine": service["node"]}
        if service["state"] is not None:
            out.add("hyperloom_router_prefill_service_up", "gauge", labels, service["state"] == "up")
        for key in ("inflight", "max_inflight"):
            out.add("hyperloom_router_prefill_service_" + key, "gauge", labels, service[key])
        for key in ("assigned", "completed", "failed"):
            out.add(f"hyperloom_router_prefill_service_{key}_total", "counter", labels, service[key])
    return [(r["id"], r["node"], r["endpoint"]) for r in replicas if r["endpoint"]]


def _engine_metrics(out, labels, text):
    parsed = engine.parse_metrics(text)
    for field in engine.METRICS:
        counter = field in _ENGINE_COUNTERS
        out.add("hyperloom_engine_" + field + ("_total" if counter else ""),
                "counter" if counter else "gauge", labels, getattr(parsed, field))


def _engine_status(out, labels, raw):
    parsed = engine.parse_status(raw)
    info = {"model": parsed.model, "trellis_identity": (parsed.memory or {}).get("trellis_identity")}
    out.add("hyperloom_engine_info", "gauge", {**labels, **info}, 1)
    if parsed.state is not None:
        out.add("hyperloom_engine_state", "gauge", {**labels, "state": parsed.state}, 1)
    out.add("hyperloom_engine_uptime_seconds", "gauge", labels, parsed.uptime_seconds)
    out.add("hyperloom_engine_context_tokens", "gauge", labels, parsed.context_tokens)
    slots = parsed.slots or {}
    for kind in _SLOT_COUNTS:
        out.add("hyperloom_engine_slots", "gauge", {**labels, "kind": kind}, slots.get(kind))
    for kind in _SLOT_POSITIONS:
        out.add("hyperloom_engine_slot_positions", "gauge", {**labels, "kind": kind}, slots.get(kind))
    out.add("hyperloom_engine_slot_pool_bytes", "gauge", labels, slots.get("pool_bytes"))
    for kind, value in (parsed.memory or {}).items():
        if kind != "trellis_identity":
            out.add("hyperloom_engine_memory_bytes", "gauge", {**labels, "kind": kind}, value)
    group = parsed.group or {}
    out.add("hyperloom_engine_group_bytes_sent_total", "counter", labels, group.get("bytes_sent"))
    out.add("hyperloom_engine_group_exchange_seconds_total", "counter", labels, group.get("exchange_seconds"))


def _lock_commits():
    try:
        components = json.loads((repo_root() / "versions.lock").read_text(encoding="utf-8"))["components"]
        return {"engine_commit": components["engine"]["commit"], "tensormeld_commit": components["tensormeld"]["commit"]}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return {}  # Unknown commits are left out rather than invented.


def _launches():
    """{profile: launch record} of each saved launch state."""
    result = {}
    for path in sorted(secrets.runtime_dir().glob("up-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        launch = state.get("launch") if isinstance(state, dict) else None
        if isinstance(launch, dict):
            result[path.stem[3:]] = launch
    return result


def launch_families(launches=None):
    """{profile: engine family} of each saved launch (its recorded family, else its preset's)."""
    from .config import family as preset_family
    result = {}
    for name, launch in (_launches() if launches is None else launches).items():
        family = launch.get("family")
        if not isinstance(family, str):
            try:
                family = preset_family(launch.get("preset") or name)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        result[name] = family
    return result


def _loopback(family):
    from . import engines
    try:
        return engines.adapter(family).bind == "loopback"
    except (ValueError, KeyError):
        return False


def launch_commits():
    """{profile: {engine_commit, tensormeld_commit}} of each saved launch: the running release's manifest components,
    else the launched preset's engine pin. Missing values are left out."""
    result = {}
    for name, launch in _launches().items():
        components = launch.get("components") if isinstance(launch.get("components"), dict) else {}
        row = {}
        engine_commit = components.get("engine") or launch.get("engine_sha")
        if isinstance(engine_commit, str):
            row["engine_commit"] = engine_commit
        if isinstance(components.get("tensormeld"), str):
            row["tensormeld_commit"] = components["tensormeld"]
        if row:
            result[name] = row
    return result


def _build_info(out, routers=None, launches=None):
    """One sample per running profile (its router is up) with its launch's commits, versions.lock for what the
    launch does not record; with no running profile, one sample from versions.lock."""
    lock = _lock_commits()
    profiles = sorted(name for name, endpoint in (routers or {}).items() if isinstance(endpoint, dict))
    if not profiles:
        out.add("hyperloom_build_info", "gauge", {"version": __version__, **lock}, 1)
        return
    if launches is None:
        try:
            launches = launch_commits()
        except (OSError, RuntimeError):
            launches = {}
    for name in profiles:
        out.add("hyperloom_build_info", "gauge", {"version": __version__, "profile": name,
                                                  **lock, **launches.get(name, {})}, 1)


def _apply(out, apply, labels, outcome):
    raw, error, elapsed = outcome
    result = None
    if error is None:
        try:
            result = apply(out, labels, raw)
        except Exception as exc:  # noqa: BLE001 - one bad source is that source down, never a failed scrape
            error = exc
    return result, error, elapsed


LOOPBACK_UNAVAILABLE = "unavailable: engine bound to loopback on its node"


def collect(routers=None, fetch=_get, profile=None, deadline_s=SCRAPE_DEADLINE_S, launches=None, families=None):
    """Return the exposition text for {profile: router endpoint or None}.

    Routers, then every replica's engine sources, are fetched in parallel against one overall deadline. ``launches``
    ({profile: commits}) and ``families`` ({profile: engine family}) default to the saved launch states. Replicas of
    a loopback-bound family are never probed: their address is 127.0.0.1 on their node, not on this machine."""
    routers = running_routers() if routers is None else routers
    if families is None:
        try:
            families = launch_families()
        except (OSError, RuntimeError):
            families = {}
    if profile is not None:
        routers = {profile: routers.get(profile)}
    out = Exposition()
    _build_info(out, routers, launches)
    fetches = _Fetches(fetch, time.monotonic() + deadline_s)
    jobs = []
    for name, endpoint in sorted(routers.items()):
        labels = {"source": "router", "profile": name}
        if not isinstance(endpoint, dict):
            _source(out, labels, False, None)
        else:
            jobs.append((name, labels, fetches.submit(endpoint, "/api/status")))
    replicas = []
    for name, labels, job in jobs:
        rows, error, elapsed = _apply(out, lambda o, _labels, raw: _router(o, name, raw), labels, fetches.result(job))
        replicas += [(name, *row) for row in rows or []]
        _source(out, labels, error is None, elapsed)
    jobs = []
    for name, rid, node, raw_endpoint in replicas:
        labels = {"profile": name, "replica": rid, "machine": node}
        if _loopback(families.get(name)):
            for source in ("engine_metrics", "engine_status"):
                _source(out, {**labels, "source": source, "reason": LOOPBACK_UNAVAILABLE}, False, None)
            continue
        try:
            endpoint = _replica_endpoint(raw_endpoint)
        except SOURCE_ERRORS:
            endpoint = None
        for source, suffix, text, apply in (("engine_metrics", "/metrics", True, _engine_metrics),
                                            ("engine_status", "/api/status", False, _engine_status)):
            job = fetches.submit(endpoint, suffix, text) if endpoint else None
            jobs.append((labels, source, job, apply))
    for labels, source, job, apply in jobs:
        outcome = fetches.result(job) if job else (None, ValueError("replica endpoint"), None)
        _, error, elapsed = _apply(out, apply, labels, outcome)
        _source(out, {**labels, "source": source}, error is None, elapsed)
    return out.text()


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen=DEFAULT_LISTEN, *, collector=collect, max_concurrent=MAX_CONCURRENT_SCRAPES):
        endpoint = parse_listen(listen, "metrics listen")
        if not ipaddress.ip_address(endpoint["address"]).is_loopback:
            raise ValueError("metrics listen must be a loopback address; LAN exposure is a later phase")
        if ":" in endpoint["address"]:
            self.address_family = socket.AF_INET6
        self.collector = collector
        self.scrapes = threading.BoundedSemaphore(max_concurrent)
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
        if status == 503:
            self.send_header("Retry-After", "5")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._send(403, "Host is not allowed\n")
        elif urlsplit(self.path).path != "/metrics":
            self._send(404, "not found\n")
        elif not self.server.scrapes.acquire(blocking=False):
            self._send(503, "too many concurrent scrapes\n")
        else:
            try:
                body = self.server.collector()
            except Exception as exc:  # noqa: BLE001 - answer 500 rather than drop the connection.
                self._send(500, f"collector failed: {type(exc).__name__}\n")
            else:
                self._send(200, body, CONTENT_TYPE)
            finally:
                self.server.scrapes.release()

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
