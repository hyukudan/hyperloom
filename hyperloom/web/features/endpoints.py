"""Other chat-completions-compatible endpoints (F17) and the endpoint exposure check (F40).

Targets are an allow-list kept in the telemetry store (document namespace ``prefs``, key ``endpoints/targets``):
scheme, host (a numeric private or loopback IP, or a configured machine id), port, an optional key name in the
hyperloom secret store and a label. Nothing else is ever contacted, and only with GETs on a fixed set of paths.

Identification uses positive signatures of a server's own endpoints, strongest first:
    strix           our engines: /api/status with schema strix-serve-status-v1, or /metrics with strix_* / mm_* samples
    props-server    a server exposing /props with a default_generation_settings object
    version-server  a server exposing /version and max_model_len on its /v1/models entries
    generic         a generic chat-completions-compatible server: only the /v1/models list shape
A random path is requested first; a server that answers it with 2xx is a catch-all, and since every signature
needs a body shape, a catch-all 200 never identifies anything nor overrides a positive identification.

Transport: one connection per request to the resolved IP literal, no proxies, no redirects followed (a 3xx is
reported and its body dropped, so credentials never reach a redirect target), 3 s per request including connect,
TLS, headers and body (a watchdog shuts the socket down), responses capped at 256 KiB, at most 6 requests and 12 s
per target per round, one short-lived worker per target, late results discarded.

    GET  endpoints              -> targets with their latest probe (read from the store; a GET never probes)
    GET  endpoints/exposure     -> exposure rows (F40) of the configured engines, the router and every target
    POST endpoints/save         -> add or update a target (audited)
    POST endpoints/remove       -> remove a target (audited)
    POST endpoints/check        -> a forced identification round and an exposure round, now (synchronous)
Collectors: ``endpoints`` (15 s: endpoint_up, endpoint_models, token deltas where /metrics is ours) and
``endpoints-exposure`` (60 s: listener evidence from the fleet package's stored host answers plus an
unauthenticated models-list request, which never generates anything).
"""

from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
import hashlib
import http.client
import ipaddress
import json
import re
import secrets as random
import socket
import ssl
import threading
import time
import weakref

from hyperloom import config, engines, render, secrets, telemetry
from hyperloom.adapters import AdapterError, engine as engine_adapter, prom


PREFS, TARGETS_KEY = "prefs", "endpoints/targets"
NAMESPACE, EXPOSURE_KEY = "endpoints", "exposure"
FLEET_HOST = "fleet/host"
MAX_TARGETS = 32
TIMEOUT_S = 3.0
CAP_BYTES = 256 * 1024
ROUND_DEADLINE_S = 12.0
ROUND_WAIT_S = 13.0
EXPOSURE_DEADLINE_S = 8.0
EXPOSURE_WORKERS = 32
MAX_REQUESTS = 6
IDENTIFY_EVERY_S = 600
MAX_MODELS = 64
INTERVAL_S = 15
EXPOSURE_INTERVAL_S = 60
STALE_EXPOSURE_S = 3 * EXPOSURE_INTERVAL_S
LISTENER_FRESH_S = 30
CLAIM_KEEP_S = 86400
CONTINUITY_SLACK_S = 4.0
UI_DEFAULT_PORT = 18700
ROUTER_DEFAULT_PORT = 18600
LOCK_WAIT_S = 30
NO_STORE = "endpoint store not open in this portal process (samplers disabled)"
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REV = re.compile(r"[0-9a-f]{16}")
KEY_NAME = re.compile(r"[a-z0-9-]{1,40}")
LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
FIELDS = frozenset(("id", "label", "scheme", "host", "port", "key_name"))
KIND_TEXT = {
    "strix": "our engine (strix-serve-status-v1 status or strix_* / mm_* metrics)",
    "props-server": "server exposing /props with default_generation_settings",
    "version-server": "server exposing /version and max_model_len in /v1/models",
    "generic": "generic chat-completions-compatible server (/v1/models list only)",
    "unknown": "no positive signature",
}
SERIES = (("endpoint_up", "state", None, {0: "down", 1: "up"}),
          ("endpoint_models", "gauge", "models", None),
          ("endpoint_generated_tokens", "delta", "tokens", None),
          ("endpoint_prompt_tokens", "delta", "tokens", None))
COUNTERS = (("generated_tokens", "endpoint_generated_tokens"), ("prompt_tokens", "endpoint_prompt_tokens"))
SCOPE_RANK = {"any": 3, "lan": 2, "loopback": 1}
VERDICTS = ("red", "amber", "green", "unknown")

_edit_lock = threading.Lock()
_round_lock = threading.Lock()       # identification rounds (collector, POST check, CLI) never overlap in a process
_exposure_lock = threading.Lock()
_exposure_cursor = [0]                # rotates where each exposure round starts, so no row is always last
_described = weakref.WeakSet()    # stores whose series are described


# -- targets ----------------------------------------------------------------------------------------------------
def _cluster(api):
    try:
        return api.cluster()
    except (OSError, ValueError, RuntimeError):
        return None


def _ui_port(cluster):
    try:
        listen = (cluster.pc or {}).get("ui_listen") if cluster is not None else None
        return config.listen(listen, "pc.ui_listen")["port"] if listen else UI_DEFAULT_PORT
    except ValueError:
        return UI_DEFAULT_PORT


def resolve(host, cluster):
    """The IP literal a target host stands for: a configured machine's address or the host itself."""
    if cluster is not None and host in cluster.machines:
        return cluster.machines[host].http_address
    return config.address(host, "host")


def _slug(label):
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].strip("-")
    return slug or "endpoint"


def validate(arguments, cluster, targets):
    """A target from POST/CLI arguments (strict); ``targets`` are the current ones (ids, duplicates, limit)."""
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = sorted(set(arguments) - FIELDS)
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(unknown))
    label = arguments.get("label")
    if not isinstance(label, str) or not LABEL.fullmatch(label.strip()):
        raise ValueError("label: 1..64 printable characters")
    label = label.strip()
    scheme = arguments.get("scheme", "http")
    if scheme not in ("http", "https"):
        raise ValueError("scheme: http or https")
    host = arguments.get("host")
    if not isinstance(host, str) or not host.strip() or len(host) > 64:
        raise ValueError("host: a private or loopback IP address or a configured machine id")
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        address = resolve(host, cluster)
    except ValueError:
        raise ValueError("host: a private or loopback IP address or a configured machine id "
                         "(names are not resolved; public addresses are not lab endpoints)") from None
    if cluster is None or host not in cluster.machines:
        host = address                       # the canonical spelling of the IP
    port = arguments.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port: an integer in 1..65535")
    if ipaddress.ip_address(address).is_loopback and port == _ui_port(cluster):
        raise ValueError("port: that is this portal's own port")
    key_name = arguments.get("key_name")
    if key_name in ("", None):
        key_name = None
    elif not isinstance(key_name, str) or not KEY_NAME.fullmatch(key_name):
        raise ValueError("key_name: a secret name ([a-z0-9-], at most 40 characters)")
    ids = {t["id"] for t in targets}
    tid = arguments.get("id")
    if tid is not None:
        if not isinstance(tid, str) or not ID.fullmatch(tid):
            raise ValueError("id: [a-z0-9-], at most 40 characters, starting with a letter or digit")
    else:
        base = tid = _slug(label)
        number = 2
        while tid in ids:
            suffix = "-" + str(number)
            tid = base[:40 - len(suffix)] + suffix
            number += 1
    for other in targets:
        if other["id"] == tid:
            continue
        try:
            same = (other["scheme"], resolve(other["host"], cluster), other["port"]) == (scheme, address, port)
        except ValueError:
            same = False
        if same:
            raise ValueError(f"{scheme}://{host}:{port} is already the target {other['id']}")
    if tid not in ids and len(targets) >= MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} targets")
    return {"id": tid, "label": label, "scheme": scheme, "host": host, "port": port, "key_name": key_name}


def _parse_targets(text):
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        doc = None
    rows = doc.get("targets") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    keep = []
    for row in rows[:MAX_TARGETS]:
        if (isinstance(row, dict) and isinstance(row.get("id"), str) and ID.fullmatch(row["id"])
                and isinstance(row.get("rev"), str) and REV.fullmatch(row["rev"])
                and row.get("scheme") in ("http", "https") and isinstance(row.get("host"), str)
                and type(row.get("port")) is int and 1 <= row["port"] <= 65535):
            keep.append(row)
    return keep


def _encode(value):
    # Default separators: the rev needle in _publish relies on '"rev": "<hex>"'.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


def _raw(store):
    rows = store.execute("SELECT value FROM docs WHERE namespace=? AND key=?", (PREFS, TARGETS_KEY))
    return rows[0][0] if rows else None


def load(store):
    """The stored targets ([] when none)."""
    return _parse_targets(_raw(store))


def _edit(store, change, now):
    """Read-modify-write of the targets with a one-statement compare-and-swap on the stored text, so the portal and
    a CLI run never lose each other's edits. ``change(targets) -> (targets, result)``."""
    with _edit_lock:
        for _ in range(5):
            old = _raw(store)
            targets, result = change(deepcopy(_parse_targets(old)))
            text = _encode({"version": 1, "targets": targets})
            if old is None:
                applied = store.execute("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) "
                                        "ON CONFLICT(namespace, key) DO NOTHING RETURNING 1",
                                        (PREFS, TARGETS_KEY, now, text))
            else:
                applied = store.execute("UPDATE docs SET ts=?, value=? WHERE namespace=? AND key=? AND value=? RETURNING 1",
                                        (now, text, PREFS, TARGETS_KEY, old))
            if applied:
                return result
        raise RuntimeError("the endpoint targets kept changing; try again")


def save(api, arguments):
    store = telemetry.open_store(api)
    cluster = _cluster(api)
    now = api.clock()

    def change(targets):
        target = validate(arguments, cluster, targets)
        target["rev"] = random.token_hex(8)       # everything derived from the old destination is dropped
        for index, row in enumerate(targets):
            if row["id"] == target["id"]:
                target["added"] = row.get("added", now)
                targets[index] = target
                return targets, (target, row["rev"])
        target["added"] = now
        return targets + [target], (target, None)
    target, replaced = _edit(store, change, now)
    if replaced:
        _drop(store, target["id"], replaced)
    return {"target": public(target, cluster)}


def remove(api, arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"id"} or not isinstance(arguments["id"], str):
        raise ValueError("expected {id}")
    store = telemetry.open_store(api)
    tid = arguments["id"]

    def change(targets):
        found = [t for t in targets if t["id"] == tid]
        if not found:
            raise LookupError("unknown endpoint target " + tid)
        return [t for t in targets if t["id"] != tid], found[0]["rev"]
    rev = _edit(store, change, api.clock())
    _drop(store, tid, rev)
    return {"removed": tid}


def public(target, cluster=None):
    row = {key: target.get(key) for key in ("id", "label", "scheme", "host", "port", "key_name", "added")}
    try:
        row["address"] = resolve(target["host"], cluster)
    except ValueError:
        row["address"] = None
    return row


# -- transport --------------------------------------------------------------------------------------------------
class Reply:
    __slots__ = ("status", "body", "ms", "error")

    def __init__(self, status=None, body=None, ms=None, error=None):
        self.status, self.body, self.ms, self.error = status, body, ms, error

    def ok(self):
        return self.error is None and self.body is not None and self.status is not None and 200 <= self.status < 300

    def json(self):
        if not self.ok():
            return None
        try:
            return json.loads(self.body.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, UnicodeError, RecursionError):
            return None

    def text(self):
        if not self.ok():
            return None
        try:
            return self.body.decode("utf-8")
        except UnicodeError:
            return None

    def describe(self):
        return self.error or (f"HTTP {self.status}" if self.status is not None else "no reply")


def _no_constant(_):
    raise ValueError("non-finite JSON number")


def _context():
    return ssl.create_default_context()


def _failure(exc, fired):
    if fired or isinstance(exc, (socket.timeout, TimeoutError)):
        return f"timed out ({TIMEOUT_S:g} s)"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS certificate not verified"
    if isinstance(exc, ssl.SSLError):
        return "TLS error"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
        return "connection closed by the server"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"network error (errno {exc.errno})"
    return "request failed (" + type(exc).__name__ + ")"


class Session:
    """GETs to one endpoint, bounded in count, elapsed time and bytes; credentials only when a key is given."""

    def __init__(self, scheme, address, port, key=None, *, host=None, deadline_s=ROUND_DEADLINE_S,
                 max_requests=MAX_REQUESTS, timeout_s=TIMEOUT_S):
        self.scheme, self.address, self.port, self.key = scheme, address, port, key
        authority = host or address              # the configured host (a machine id stays as written)
        self.host_header = (f"[{authority}]" if ":" in authority else authority) + f":{port}"
        self.deadline = time.monotonic() + deadline_s
        self.left = max_requests
        self.timeout = timeout_s
        self.requests = []

    def get(self, path, auth=True):
        if self.left <= 0:
            return Reply(error="request budget for this round used up")
        budget = min(self.timeout, self.deadline - time.monotonic())
        if budget <= 0:
            return Reply(error="round deadline reached")
        self.left -= 1
        self.requests.append(path)
        started = time.monotonic()
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.address, self.port, timeout=budget, context=_context())
        else:
            conn = http.client.HTTPConnection(self.address, self.port, timeout=budget)
        fired, connected = [], []

        def abort():
            # Elapsed-time bound: a server trickling its headers or body is cut off at the budget. The connected
            # socket is kept here because http.client drops conn.sock once a response owns the connection.
            fired.append(True)
            sock = connected[0] if connected else conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(budget, abort)
        timer.daemon = True
        timer.start()
        ms = lambda: round((time.monotonic() - started) * 1000, 1)
        try:
            headers = {"Host": self.host_header, "Accept": "application/json, text/plain;q=0.9",
                       "User-Agent": "hyperloom-endpoints", "Connection": "close"}
            if auth and self.key:
                headers["Authorization"] = "Bearer " + self.key
            conn.connect()
            connected.append(conn.sock)
            if fired:
                raise TimeoutError()
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            if fired:
                raise TimeoutError()
            if 300 <= response.status < 400:
                return Reply(response.status, None, ms(), f"redirect refused (HTTP {response.status}; not followed)")
            data = bytearray()
            while True:
                chunk = response.read1(65536)
                if fired:
                    raise TimeoutError()
                if not chunk:
                    break
                data += chunk
                if len(data) > CAP_BYTES:
                    return Reply(response.status, None, ms(), "response exceeds 256 KiB")
            return Reply(response.status, bytes(data), ms())
        except (OSError, http.client.HTTPException, ValueError) as exc:
            return Reply(None, None, ms(), _failure(exc, fired))
        finally:
            timer.cancel()
            conn.close()


# -- identification ---------------------------------------------------------------------------------------------
def _models(obj):
    """[{id, max_model_len?}] from a /v1/models list shape, else None."""
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), list) or obj.get("object", "list") != "list":
        return None
    models = []
    for item in obj["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 256:
            return None
        model = {"id": item["id"]}
        if type(item.get("max_model_len")) is int and item["max_model_len"] > 0:
            model["max_model_len"] = item["max_model_len"]
        models.append(model)
    return models


def _ours_metrics(text):
    if text is None:
        return False
    try:
        samples = prom.parse(text)
    except AdapterError:
        return False
    return any(name.startswith(("strix_", "mm_")) for name, _ in samples)


def _panel(status_obj, metrics_text):
    panel, why = {}, {}
    if status_obj is not None:
        try:
            parsed = engine_adapter.parse_status(status_obj)
            panel.update(model=parsed.model, engine=parsed.engine, state=parsed.state,
                         context_tokens=parsed.context_tokens, uptime_seconds=parsed.uptime_seconds,
                         queue=parsed.queue, totals=parsed.totals, slots=parsed.slots)
        except AdapterError:
            why["status"] = "status document did not parse"
    else:
        why["status"] = "no /api/status this round"
    if metrics_text is not None:
        try:
            metrics = engine_adapter.parse_metrics(metrics_text)
            panel.update(waiting=metrics.waiting, running=metrics.running, slots_busy=metrics.slots_busy,
                         generated_tokens_total=metrics.generated_tokens, prompt_tokens_total=metrics.prompt_tokens)
        except AdapterError:
            why["metrics"] = "metrics did not parse"
    else:
        why["metrics"] = "no recognised /metrics this round"
    panel["why"] = why
    return panel


def _key(target, lookup=None):
    """(key or None, problem or None) for a target, from the secret store only."""
    name = target.get("key_name")
    if not name:
        return None, None
    try:
        value = (lookup or secrets.get)(name)
    except (OSError, ValueError):
        return None, f"secret {name} unreadable or not a usable key"
    if value is None:
        return None, f"secret {name} is not in the secret store"
    return value, None


def _scrub(value, key):
    """Remove an echoed credential from everything taken from a response."""
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, list):
        return [_scrub(item, key) for item in value]
    if isinstance(value, dict):
        return {_scrub(k, key): _scrub(v, key) for k, v in value.items()}
    return value


def source(target, address):
    """The identity of what a target's results describe: its revision and the destination it resolved to (a
    machine alias can move without the target being saved)."""
    return hashlib.sha256(_encode([target["rev"], target["scheme"], address, target["port"]]).encode("ascii")).hexdigest()[:16]


def current_sources(targets, cluster):
    """{target id: source or None (does not resolve)} for the stored targets now."""
    result = {}
    for target in targets:
        try:
            result[target["id"]] = source(target, resolve(target["host"], cluster))
        except ValueError:
            result[target["id"]] = None
    return result


def probe(target, cluster, previous=None, *, now, identify=False, session_factory=None, key_lookup=None):
    """One round for one target -> its probe document (never contains the key)."""
    result = {"rev": target["rev"], "source": None, "checked_at": now, "kind": None, "kind_text": None, "catch_all": None,
              "signatures": [], "identified_at": None, "up": False, "latency_ms": None, "models": None,
              "models_total": None, "panel": None, "counters": None, "error": None, "why": {}, "instance": None,
              "instance_uptime": None, "instance_seen_at": None, "requests": []}
    started = time.monotonic()
    status_seen = None
    try:
        address = resolve(target["host"], cluster)
    except ValueError:
        result["error"] = "host no longer resolves to a private or loopback address (machine removed?)"
        return result
    result["source"] = source(target, address)
    key, problem = _key(target, key_lookup)
    if problem:
        result["why"]["key"] = problem + "; requests sent without credentials"
    session = (session_factory or Session)(target["scheme"], address, target["port"], key, host=target["host"])
    if not previous or previous.get("source") != result["source"]:
        previous = {}                       # another revision or destination: nothing carries over
    again = (identify or previous.get("kind") in (None, "unknown") or previous.get("error")
             or not isinstance(previous.get("identified_at"), (int, float))
             or now - previous["identified_at"] >= IDENTIFY_EVERY_S)
    kind, signatures = None, []
    status_obj = metrics_text = None
    if again:
        stray = session.get("/hl-probe-" + random.token_hex(8))
        result["catch_all"] = stray.status is not None and 200 <= stray.status < 300
        candidate = session.get("/api/status").json()
        if isinstance(candidate, dict) and candidate.get("schema") == engine_adapter.SCHEMA:
            kind, status_obj = "strix", candidate
            status_seen = now + _since(started)
            signatures.append("/api/status schema " + engine_adapter.SCHEMA)
        text = session.get("/metrics").text()
        if _ours_metrics(text):
            metrics_text = text
            kind = kind or "strix"
            signatures.append("/metrics with strix_* or mm_* samples")
        if kind is None:
            props = session.get("/props").json()
            if isinstance(props, dict) and isinstance(props.get("default_generation_settings"), dict):
                kind = "props-server"
                signatures.append("/props with default_generation_settings")
        models_reply = session.get("/v1/models")
        models = _models(models_reply.json())
        if kind is None and models and any("max_model_len" in m for m in models):
            version = session.get("/version").json()
            if isinstance(version, dict) and isinstance(version.get("version"), str):
                kind = "version-server"
                signatures.append("/version and max_model_len in /v1/models")
        if kind is None and models is not None:
            kind = "generic"
            signatures.append("/v1/models list shape")
        result["identified_at"] = now
    else:
        kind, signatures = previous["kind"], list(previous.get("signatures") or [])
        result["catch_all"] = previous.get("catch_all")
        result["identified_at"] = previous["identified_at"]
        models_reply = session.get("/v1/models")
        models = _models(models_reply.json())
        if kind == "strix":
            candidate = session.get("/api/status").json()
            if isinstance(candidate, dict) and candidate.get("schema") == engine_adapter.SCHEMA:
                status_obj = candidate
                status_seen = now + _since(started)
            text = session.get("/metrics").text()
            if _ours_metrics(text):
                metrics_text = text
    confirm = confirm_seen = None
    if status_obj is not None and metrics_text is not None:
        candidate = session.get("/api/status").json()
        if isinstance(candidate, dict) and candidate.get("schema") == engine_adapter.SCHEMA:
            confirm, confirm_seen = candidate, now + _since(started)
    result["kind"] = kind or "unknown"
    result["kind_text"] = KIND_TEXT[result["kind"]]
    result["signatures"] = signatures
    result["requests"] = list(session.requests)
    if models is not None:
        result["latency_ms"] = models_reply.ms
        result["models_total"] = len(models)
        result["models"] = models[:MAX_MODELS]
    else:
        result["why"]["models"] = "GET /v1/models: " + (models_reply.describe() if not models_reply.ok() else "not a models list")
    result["up"] = models is not None or status_obj is not None
    if kind is None:
        reasons = []
        if result["catch_all"]:
            reasons.append("answers 2xx on any path (catch-all), which proves nothing by itself")
        if models_reply.status in (401, 403):
            reasons.append("models list refused without a usable key")
        reasons.append("GET /v1/models: " + models_reply.describe())
        result["why"]["kind"] = "; ".join(reasons)
    if not result["up"]:
        result["error"] = "GET /v1/models: " + models_reply.describe()
    if kind == "strix":
        result["panel"] = panel = _panel(status_obj, metrics_text)
        uptime = panel.get("uptime_seconds")
        reported = _reported_instance(status_obj)
        if status_obj is not None and metrics_text is not None and not _same_lifetime(status_obj, status_seen, confirm, confirm_seen):
            # The counters were read between two status replies that do not describe one engine lifetime (or the
            # second one is missing): the sample cannot be attributed, so it is not accounted.
            result["why"]["counters"] = "engine status changed or did not answer around the metrics read; not accounted"
        elif reported is not None:
            # C3: the engine's own instance id or start time identifies its lifetime.
            result["instance"] = reported
        elif isinstance(uptime, (int, float)) and status_seen is not None:
            old_uptime, old_seen = previous.get("instance_uptime"), previous.get("instance_seen_at")
            # Continuity (C3): one engine lifetime is a run of replies whose uptime advanced like the wall clock
            # between them; anything else (less, backwards, a jump) is a new instance with a new baseline.
            continuous = (previous.get("instance") and isinstance(old_uptime, (int, float))
                          and isinstance(old_seen, (int, float)) and status_seen > old_seen
                          and abs((uptime - old_uptime) - (status_seen - old_seen)) <= CONTINUITY_SLACK_S)
            result["instance"] = previous["instance"] if continuous else "start-" + str(int(status_seen - uptime))
            result["instance_uptime"], result["instance_seen_at"] = uptime, status_seen
        else:
            result["why"]["counters"] = "no engine uptime reported, so no instance identity: token deltas not recorded"
    return _scrub(result, key)


def _since(started):
    """Seconds since a monotonic start (the time a status reply arrived within its round)."""
    return time.monotonic() - started


def _same_lifetime(first, first_seen, second, second_seen):
    """True when two status replies describe one engine lifetime: the same reported identity, or (without one) an
    uptime that advanced like the clock between them."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    a, b = _reported_instance(first), _reported_instance(second)
    if a is not None or b is not None:
        return a == b
    u1, u2 = first.get("uptime_seconds"), second.get("uptime_seconds")
    if type(u1) not in (int, float) or type(u2) not in (int, float) or first_seen is None or second_seen is None:
        return False
    return u2 >= u1 and abs((u2 - u1) - (second_seen - first_seen)) <= CONTINUITY_SLACK_S


def _reported_instance(status_obj):
    """The instance identity an engine reports itself (an id, or its start time), else None."""
    if not isinstance(status_obj, dict):
        return None
    for key in ("instance_id", "instance"):
        value = status_obj.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128:
            return "id-" + value
    for key in ("start_time", "started_at"):
        value = status_obj.get(key)
        if type(value) in (int, float) and value > 0:
            return "start-" + repr(float(value))
    return None


def _publish(store, tid, result, now):
    """Write a probe document only while its target revision is current, and never over a newer observation."""
    needle = '"rev": "' + result["rev"] + '"'
    return bool(store.execute(
        "INSERT INTO docs(namespace, key, ts, value) SELECT ?, ?, ?, ? WHERE EXISTS "
        "(SELECT 1 FROM docs WHERE namespace=? AND key=? AND instr(value, ?) > 0) "
        "ON CONFLICT(namespace, key) DO UPDATE SET ts=excluded.ts, value=excluded.value WHERE excluded.ts > docs.ts "
        "RETURNING 1", (NAMESPACE, "probe/" + tid, now, _encode(result), PREFS, TARGETS_KEY, needle)))


def _claim(store, tid, src, now):
    """Claim this endpoint's accounting slot: at most one accounting per sampling interval for a source, decided in
    one statement across every portal process, so each delta spans at least the interval its points are recorded
    with (coverage is never overstated)."""
    return bool(store.execute(
        "INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, '{}') ON CONFLICT(namespace, key) "
        "DO UPDATE SET ts=excluded.ts WHERE excluded.ts - docs.ts >= ? RETURNING 1",
        (NAMESPACE, f"account/{tid}/{src}", now, float(INTERVAL_S))))


def _drop(store, tid, rev):
    """Delete the probe document of target ``tid`` only if it holds revision ``rev`` and that revision is no longer
    current (one statement: a re-created id's newer document is never erased)."""
    needle = '"rev": "' + rev + '"'
    store.execute("DELETE FROM docs WHERE namespace=? AND key=? AND instr(value, ?) > 0 AND NOT EXISTS "
                  "(SELECT 1 FROM docs WHERE namespace=? AND key=? AND instr(value, ?) > 0)",
                  (NAMESPACE, "probe/" + tid, needle, PREFS, TARGETS_KEY, needle))


def _describe(store):
    if store in _described:
        return
    for name, kind, unit, legend in SERIES:
        store.describe(name, kind, unit=unit, interval=INTERVAL_S, legend=legend)
    _described.add(store)


def run_round(api, now, *, identify=False, session_factory=None, key_lookup=None, account=False):
    """Probe every target once (one worker each, one deadline) and publish the probe documents; with ``account``
    (the sampler's own rounds only, so each delta stands for one sampling interval) also account the token
    counters. Returns {target id: result} for the results that finished in time and were accepted."""
    store = telemetry.open_store(api)
    _describe(store)
    if not _round_lock.acquire(timeout=LOCK_WAIT_S):
        raise PermissionError("another endpoint probe round is still running")
    try:
        cluster = _cluster(api)
        targets = load(store)
        previous = {t["id"]: store.get(NAMESPACE, "probe/" + t["id"]) for t in targets}
        results = {}
        if targets:
            pool = ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="hyperloom-endpoints")
            try:
                futures = {pool.submit(probe, t, cluster, previous[t["id"]], now=now, identify=identify,
                                       session_factory=session_factory, key_lookup=key_lookup): t for t in targets}
                done, _ = wait(futures, timeout=ROUND_WAIT_S)
                for future in done:
                    if future.exception() is None:
                        results[futures[future]["id"]] = future.result()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)   # a late worker ends by its own request watchdog
        stored = load(store)
        current = {t["id"]: t["rev"] for t in stored}
        sources = current_sources(stored, _cluster(api))
        published = {}
        for tid, result in results.items():
            if current.get(tid) != result["rev"] or sources.get(tid) != result.get("source"):
                continue                                    # removed, re-saved or re-pointed meanwhile
            if not account and result.get("instance"):
                result["why"]["counters"] = "token deltas are accounted only by the sampler's rounds"
            if not _publish(store, tid, result, now):
                continue                                    # superseded at the moment of writing
            panel = result.get("panel") or {}
            if account and result.get("instance") and not _claim(store, tid, result["source"], now):
                result["why"]["counters"] = (f"accounted at most once per {INTERVAL_S} s (another sampler round "
                                             "accounted this endpoint less than an interval ago)")
            elif account and result.get("instance"):
                counters = {}
                for field, series in COUNTERS:
                    value = panel.get(field + "_total")
                    if isinstance(value, (int, float)):
                        labels = {"endpoint": tid}
                        delta, info = store.counter_delta(
                            f"endpoints/{tid}/{result['source']}/{field}", result["instance"], value, now,
                            max_gap=4 * INTERVAL_S,
                            apply=lambda d, i, series=series, labels=labels: {"points": [(series, labels, d, "delta")]})
                        counters[field] = {"delta": delta, "unobserved_s": info.get("unobserved_s"),
                                           "uncertain": info.get("uncertain"), "stale": info.get("stale")}
                if counters:
                    result["counters"] = counters
            if result.get("counters") or result["why"].get("counters"):
                _publish(store, tid, result, now + 1e-6)         # the same observation with its accounting
            published[tid] = result
        for key, ts, value in store.items(NAMESPACE):
            if key.startswith("account/"):
                # A claim is dropped only with its target, or once far older than any interval: a configuration
                # that fails to load for a moment must never reopen an accounting slot early.
                if key.split("/")[1] not in current or ts < now - CLAIM_KEEP_S:
                    store.execute("DELETE FROM docs WHERE namespace=? AND key=? AND ts=?", (NAMESPACE, key, ts))
                continue
            if not key.startswith("probe/") or not isinstance(value, dict):
                continue
            rev, src = value.get("rev"), value.get("source")
            if isinstance(rev, str) and current.get(key[6:]) != rev:
                _drop(store, key[6:], rev)
            elif isinstance(src, str) and sources.get(key[6:]) != src:
                # Same revision, other destination (a machine address moved): drop exactly that document.
                store.execute("DELETE FROM docs WHERE namespace=? AND key=? AND instr(value, ?) > 0",
                              (NAMESPACE, key, '"source": "' + src + '"'))
        return published
    finally:
        _round_lock.release()


def collect(api, now):
    """The ``endpoints`` collector: one round, then the up/models points (token deltas are written by the round)."""
    points = []
    for tid, result in run_round(api, now, account=True).items():
        labels = {"endpoint": tid}
        points.append(("endpoint_up", labels, 1 if result["up"] else 0, "state"))
        if result["models_total"] is not None:
            points.append(("endpoint_models", labels, result["models_total"], "gauge"))
    return points


# -- exposure (F40) ---------------------------------------------------------------------------------------------
def _scope(address):
    return "loopback" if ipaddress.ip_address(address).is_loopback else "lan"


def _local_machine(cluster):
    return next((m for m in cluster.machines.values() if m.access == "local"), None)


def router_listen(cluster):
    """(machine id or None, listen {address, port}) exactly as render_router binds it."""
    settings = cluster.router or {}
    host = settings.get("machine", "local")
    default = (cluster.pc or {}).get("router_listen", f"127.0.0.1:{ROUTER_DEFAULT_PORT}")
    if host != "local":
        address = cluster.machines[host].http_address
        default = f"[{address}]:{ROUTER_DEFAULT_PORT}" if ":" in address else f"{address}:{ROUTER_DEFAULT_PORT}"
        machine = host
    else:
        local = _local_machine(cluster)
        machine = local.id if local else None
    return machine, config.listen(settings.get("listen", default), "router.listen")


BASIS_FIELDS = ("kind", "machine", "scheme", "probe_address", "listen_address", "port", "configured_scope",
                "key_configured", "rev", "error")


def _basis(row):
    """Digest of a row's configured inputs: a stored verdict is shown only while it still describes them."""
    return hashlib.sha256(_encode({key: row.get(key) for key in BASIS_FIELDS}).encode("ascii")).hexdigest()[:16]


def configured(cluster, targets):
    """The endpoints the configuration and the allow-list declare, in the fixed check order: the router, the
    engines by profile and machine, then the targets by id (no I/O)."""
    rows = []
    try:
        machine, listen = router_listen(cluster)
        probe_address = listen["address"]
        remote = (cluster.router or {}).get("machine", "local") != "local"
        if remote and _scope(probe_address) == "loopback":
            # Its loopback is not ours: ask at that machine's address, where a loopback listener must not answer.
            probe_address = cluster.machines[machine].http_address
        rows.append({"id": "router", "kind": "router", "label": "router", "profile": None, "family": None,
                     "machine": machine, "port": listen["port"], "scheme": "http", "listen_address": listen["address"],
                     "configured_scope": _scope(listen["address"]), "key_configured": "api_key_file" in (cluster.router or {}),
                     "key_supported": True, "probe_address": probe_address})
    except (KeyError, ValueError):
        rows.append({"id": "router", "kind": "router", "label": "router", "machine": None, "port": None,
                     "error": "router listen address does not resolve"})
    for name, declared in (cluster.profiles or {}).items():
        try:
            preset_name = declared.get("preset", name)
            data = config.preset(preset_name)
            family = config.family(preset_name)
            adapter = engines.adapter(family)
            members = engines.router_members(family, engines.members(declared, family))
            port = declared.get("overrides", {}).get(render.port_flag(data), declared.get("port", data["port"]))
        except (OSError, ValueError, KeyError):
            rows.append({"id": f"engine/{name}", "kind": "engine", "label": name, "profile": name, "machine": None,
                         "port": None, "error": "profile does not render (preset or nodes invalid)"})
            continue
        for mid in members:
            machine = cluster.machines.get(mid)
            if machine is None:
                continue
            keyed = adapter.key_file and bool((declared.get("api_key_files") or {}).get(mid))
            rows.append({"id": f"engine/{name}/{mid}", "kind": "engine", "label": f"{name} on {mid}", "profile": name,
                         "family": family, "machine": mid, "port": port, "scheme": "http",
                         "configured_scope": adapter.bind, "key_configured": keyed, "key_supported": adapter.key_file,
                         "probe_address": machine.http_address})
    for target in sorted(targets, key=lambda t: t["id"]):
        try:
            address = resolve(target["host"], cluster)
        except ValueError:
            rows.append({"id": "target/" + target["id"], "kind": "target", "label": target.get("label"),
                         "machine": None, "port": target["port"], "rev": target["rev"], "error": "host does not resolve"})
            continue
        machine = target["host"] if target["host"] in cluster.machines else next(
            (m.id for m in cluster.machines.values() if m.http_address == address), None)
        if machine is None and ipaddress.ip_address(address).is_loopback:
            local = _local_machine(cluster)
            machine = local.id if local else None
        rows.append({"id": "target/" + target["id"], "kind": "target", "label": target.get("label"), "profile": None,
                     "family": None, "machine": machine, "port": target["port"], "scheme": target["scheme"],
                     "configured_scope": _scope(address), "key_configured": bool(target.get("key_name")),
                     "key_supported": True, "probe_address": address, "rev": target["rev"]})
    for row in rows:
        row["basis"] = _basis(row)
    return rows


def listener(store, cluster, machine_id, port, now):
    """Listener evidence for a port from the fleet package's stored host answer of that machine:
    {state: any|lan|loopback|not-listening|unavailable, why?}."""
    machine = cluster.machines.get(machine_id) if machine_id else None
    if machine is None:
        return {"state": "unavailable", "why": "no configured machine for this address"}
    doc = store.get(FLEET_HOST, machine_id)
    if not isinstance(doc, dict):
        if machine.os != "linux":
            return {"state": "unavailable", "why": f"listener table needs /proc/net/tcp; {machine_id} runs {machine.os}"}
        return {"state": "unavailable", "why": "no host answer from the fleet collector"}
    ts = doc.get("ts")
    if not isinstance(ts, (int, float)) or now - ts > LISTENER_FRESH_S:
        age = f" (last {int(now - ts)} s ago)" if isinstance(ts, (int, float)) else ""
        return {"state": "unavailable", "why": "no fresh host answer from the fleet collector" + age}
    if not doc.get("ok"):
        return {"state": "unavailable", "why": "the last host answer failed"}
    section = (((doc.get("host") or {}).get("checks") or {}).get("listeners")
               if isinstance(doc.get("host"), dict) else None)
    if not isinstance(section, dict) or section.get("status") != "ok" or not isinstance(section.get("value"), list):
        why = section.get("why") if isinstance(section, dict) and isinstance(section.get("why"), str) else None
        return {"state": "unavailable", "why": "listener table: " + (why or "unavailable")}
    widest = None
    for row in section["value"]:
        if isinstance(row, dict) and row.get("port") == port and row.get("scope") in SCOPE_RANK:
            if widest is None or SCOPE_RANK[row["scope"]] > SCOPE_RANK[widest]:
                widest = row["scope"]
    if widest is None:
        return {"state": "not-listening", "why": f"nothing listens on port {port}"}
    return {"state": widest}


def unauthenticated(scheme, address, port, session_factory=None):
    """GET /v1/models without credentials (never a generation) -> {outcome, status, detail}."""
    session = (session_factory or Session)(scheme, address, port, None, max_requests=1, deadline_s=TIMEOUT_S)
    reply = session.get("/v1/models", auth=False)
    if reply.status in (401, 403):
        return {"outcome": "refused", "status": reply.status, "detail": f"HTTP {reply.status} without credentials"}
    if reply.ok() and _models(reply.json()) is not None:
        return {"outcome": "answered", "status": reply.status, "detail": "models list served without credentials"}
    if reply.status is None and (reply.error == "connection refused" or (reply.error or "").startswith("timed out")):
        return {"outcome": "not_reachable", "status": None, "detail": reply.error}
    detail = reply.describe() if not reply.ok() else f"HTTP {reply.status}, not a models list"
    return {"outcome": "other", "status": reply.status, "detail": detail}


def verdict(row):
    """(verdict, reason): the ordered decision table of the design (first match wins, catch-all last)."""
    if row.get("error"):
        return "unknown", row["error"]
    probe_row = row.get("unauthenticated")
    if not probe_row or not probe_row.get("outcome"):
        return "unknown", (probe_row or {}).get("detail") or "not checked yet"
    P, S, C = probe_row["outcome"], row.get("probe_scope"), row.get("configured_scope")
    K = bool(row.get("key_configured"))
    L = (row.get("listener") or {}).get("state", "unavailable")
    where = f"{row.get('probe_address')}:{row.get('port')}"
    detail = probe_row.get("detail")
    if P == "answered" and (S == "lan" or L in ("any", "lan")):
        return "red", f"answers GET /v1/models without credentials on the LAN ({where})"
    if P == "answered" and S == "loopback":
        if L == "loopback":
            return "green", "answers without credentials on loopback only (listener bound to loopback)"
        if L == "not-listening":
            return "amber", "answers on loopback but the listener table shows nothing on this port (evidence inconsistent)"
        return "amber", "answers without credentials on loopback; LAN exposure not disproven (listener evidence unavailable)"
    if C == "loopback" and L in ("any", "lan"):
        return "amber", f"configured for loopback but bound to {L} addresses"
    if C == "loopback" and S == "lan" and P in ("refused", "other"):
        return "amber", f"configured for loopback but {where} answers on the LAN ({detail})"
    if C == "lan" and not K:
        return "amber", "LAN scope without a configured key" + ("" if row.get("key_supported", True)
                                                               else " (the engine has no key support)")
    if S == "lan" and P == "other":
        return "amber", f"{where} answers without credentials with {detail} (not refused)"
    if P == "refused":
        return "green", f"refuses the models list without credentials ({detail})"
    if P == "not_reachable":
        if L == "not-listening":
            return "green", "not listening (not running)"
        if S == "lan" and L == "loopback":
            return "green", f"bound to loopback; not reachable from the LAN at {where}"
        if S == "lan" and C == "loopback" and L == "unavailable":
            return "amber", (f"not reachable from the control machine at {where}, but without listener evidence a wider "
                             "bind behind a filter is not excluded")
        if L in ("any", "lan"):
            return "amber", f"listening on {L} addresses but not reachable from the control machine ({detail})"
        if C == "lan" and L == "unavailable":
            return "amber", f"{where} not reachable and no listener evidence"
        if S == "loopback":
            return "unknown", f"nothing answers at {where}; nothing to assess"
    if P == "other" and S == "loopback":
        if L == "loopback":
            return "green", f"loopback only ({detail})"
        return "amber", f"answers on loopback ({detail}); listener evidence does not prove loopback only"
    return "unknown", f"no rule for outcome {P}, probe scope {S}, configured scope {C}, listener {L}"


def _summary(rows):
    summary = dict.fromkeys(VERDICTS, 0)
    for row in rows:
        summary[row["verdict"]] += 1
    return summary


def _publish_exposure(store, document, ts):
    """Publish an exposure round unless a round that started later is already stored."""
    store.execute("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
                  "DO UPDATE SET ts=excluded.ts, value=excluded.value WHERE excluded.ts > docs.ts",
                  (NAMESPACE, EXPOSURE_KEY, ts, _encode(document)))


def exposure_round(api, now, *, session_factory=None, deadline_s=None):
    """One exposure round: listener evidence and an unauthenticated models-list request for every configured row,
    all within one deadline; returns the document it published."""
    store = telemetry.open_store(api)
    if not _exposure_lock.acquire(timeout=LOCK_WAIT_S):
        raise PermissionError("another exposure round is still running")
    try:
        deadline = time.monotonic() + (EXPOSURE_DEADLINE_S if deadline_s is None else deadline_s)
        cluster = _cluster(api)
        if cluster is None:
            document = {"checked_at": now, "rows": None, "summary": None,
                        "why": {"rows": "cluster configuration unavailable"}}
            _publish_exposure(store, document, now)
            return document
        rows = configured(cluster, load(store))
        pending = []
        for row in rows:
            if row.get("error"):
                continue
            row["listener"] = listener(store, cluster, row.get("machine"), row["port"], now)
            row["probe_scope"] = _scope(row["probe_address"])
            pending.append(row)
        late = {"outcome": None, "status": None, "detail": "exposure deadline reached before this row was checked"}

        def check_row(row):
            left = deadline - time.monotonic()
            if left <= 0:
                return dict(late)

            def factory(*args, **kwargs):
                kwargs["deadline_s"] = min(kwargs.get("deadline_s", TIMEOUT_S), left)
                return (session_factory or Session)(*args, **kwargs)
            return unauthenticated(row["scheme"], row["probe_address"], row["port"], factory)
        if pending:
            pool = ThreadPoolExecutor(max_workers=min(len(pending), EXPOSURE_WORKERS), thread_name_prefix="hyperloom-exposure")
            try:
                start = _exposure_cursor[0] % len(pending)
                _exposure_cursor[0] += EXPOSURE_WORKERS
                ordered = pending[start:] + pending[:start]
                futures = [(pool.submit(check_row, row), row) for row in ordered]
                done, _ = wait([f for f, _ in futures], timeout=max(0.0, deadline - time.monotonic()) + 0.5)
                for future, row in futures:
                    row["unauthenticated"] = future.result() if future in done and future.exception() is None else dict(late)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        for row in rows:
            row["verdict"], row["reason"] = verdict(row)
        document = {"checked_at": now, "rows": rows, "summary": _summary(rows), "why": {}}
        _publish_exposure(store, document, now)
        return document
    finally:
        _exposure_lock.release()


def collect_exposure(api, now):
    exposure_round(api, now)
    return []


# -- documents and posts ----------------------------------------------------------------------------------------
def _collectors(api):
    status = telemetry.status(api)
    if status is None:
        return None
    return {name: status.get(name) for name in ("endpoints", "endpoints-exposure")}


def index(api, query):
    if query:
        raise ValueError("endpoints takes no query arguments")
    store = telemetry.existing_store(api)
    collectors = _collectors(api)
    why = {}
    if collectors is None:
        why["collectors"] = "samplers not running in this portal process; probes run only on Check now or the CLI"
    if store is None:
        why["targets"] = NO_STORE
        return api.document("endpoints", targets=None, limits={"max_targets": MAX_TARGETS}, collectors=collectors,
                            kinds=KIND_TEXT, why=why)
    cluster = _cluster(api)
    rows = []
    targets = load(store)
    sources = current_sources(targets, cluster)
    for target in targets:
        row = public(target, cluster)
        found = store.get(NAMESPACE, "probe/" + target["id"])
        if (not isinstance(found, dict) or found.get("rev") != target["rev"]
                or found.get("source") != sources.get(target["id"])):
            row["probe"] = None
            row["why"] = {"probe": f"not probed yet (every {INTERVAL_S} s while the samplers run)"}
        else:
            for private in ("instance", "instance_uptime", "instance_seen_at", "rev", "source"):
                found.pop(private, None)
            row["probe"] = found
            row["why"] = {}
        rows.append(row)
    return api.document("endpoints", targets=rows, limits={"max_targets": MAX_TARGETS}, collectors=collectors,
                        kinds=KIND_TEXT, why=why)


def exposure(api, query):
    if query:
        raise ValueError("endpoints/exposure takes no query arguments")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("endpoints-exposure", rows=None, summary=None, checked_at=None, why={"rows": NO_STORE})
    found = store.get(NAMESPACE, EXPOSURE_KEY)
    if not isinstance(found, dict) or not isinstance(found.get("rows"), list):
        why = found.get("why") if isinstance(found, dict) else None
        checked = found.get("checked_at") if isinstance(found, dict) else None
        return api.document("endpoints-exposure", rows=None, summary=None, checked_at=checked,
                            why=why or {"rows": f"not checked yet (every {EXPOSURE_INTERVAL_S} s while the samplers run)"})
    checked = found.get("checked_at")
    age = api.clock() - checked if isinstance(checked, (int, float)) else None
    stored = {row.get("id"): row for row in found["rows"] if isinstance(row, dict)}
    why = dict(found.get("why") or {})
    cluster = _cluster(api)
    if cluster is None:
        rows = list(stored.values())
        for row in rows:
            row["verdict"], row["reason"] = "unknown", "cluster configuration unavailable: the last result cannot be verified"
        why["configuration"] = "cluster configuration unavailable; last results cannot be verified"
    else:
        rows = []
        for current in configured(cluster, load(store)):
            row = stored.get(current["id"])
            if row is None:
                current["verdict"], current["reason"] = "unknown", "not checked yet"
                rows.append(current)
            elif row.get("basis") != current["basis"]:
                current["verdict"], current["reason"] = "unknown", "configuration changed since the last check"
                rows.append(current)
            else:
                rows.append(row)
    if age is None or age > STALE_EXPOSURE_S:
        for row in rows:
            row["verdict"], row["reason"] = "unknown", f"result stale: last check {int(age or 0)} s ago"
        why["stale"] = f"the exposure check last ran {int(age or 0)} s ago (samplers stopped?)"
    order = {"red": 0, "amber": 1, "unknown": 2, "green": 3}
    rows.sort(key=lambda r: (order.get(r.get("verdict"), 2), r.get("id") or ""))
    return api.document("endpoints-exposure", rows=rows, summary=_summary(rows), checked_at=checked, age_s=age, why=why)


def check(api, arguments):
    """POST endpoints/check: a forced identification round and an exposure round, now (synchronous)."""
    if arguments:
        raise ValueError("endpoints/check takes no arguments")
    now = api.clock()
    results = run_round(api, now, identify=True)
    document = exposure_round(api, api.clock())
    return {"probed": sorted(results), "exposure": document.get("summary")}


def register(reg):
    reg.get("endpoints", index)
    reg.get("endpoints/exposure", exposure)
    reg.post("endpoints/save", save, audited=True)
    reg.post("endpoints/remove", remove, audited=True)
    reg.post("endpoints/check", check)
    reg.collector("endpoints", collect, interval=INTERVAL_S)
    reg.collector("endpoints-exposure", collect_exposure, interval=EXPOSURE_INTERVAL_S)
