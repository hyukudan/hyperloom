"""Inference observability: request timeline (F8), latency per model/route/caller (F9), engine panel (F10),
speculative decoding (F15) and observed conversation affinity (F16).

Every network read happens in the two collectors (``observe-router``, ``observe-engines``, every 5 s). They find
the running profiles in hyperloom's own launch records (local files: router endpoint, rendered replicas, engine
family, prefill service port; no ssh), read the router's request log, histograms and sessions with the router key,
read LAN-bound engines directly (``/api/status?detail=1``, ``/metrics``; both unauthenticated on the engines) and
take loopback-bound engines from the fleet package's node sampler (``fleet/engine`` documents, C4), never with a
node call of their own. They write the shared series, two bounded tables and the latest parsed snapshots into the
telemetry store. GET handlers read only the store: a GET never opens a socket, never arms a component's
instrumentation and never creates a table. The CLI (hyperloom/cliext/observe.py) runs the same collector functions
once against an in-memory store and prints the same documents.

GET observe/requests?profile=&model=&route=&outcome=&caller=&state=&limit=&before=   recent and in-flight requests
GET observe/requests/<id>                                                          one request, every attempt
GET observe/latency?range=&by=model|route|caller&profile=                          percentiles and histograms
GET observe/engines?profile=                                                       engine panel
GET observe/speculative?profile=                                                   speculative decoding
GET observe/sessions?profile=                                                      observed conversation affinity
GET observe/settings; POST observe/settings/save {affinity_idle_s, retention_days}     (audited)
"""

from dataclasses import asdict
import ipaddress
import json
import math
import re
import threading
import time
import socket
from urllib.parse import urlencode, urlsplit

from hyperloom import secrets, telemetry
from hyperloom.adapters import AdapterError, engine, minimax, prom, router


INTERVAL = 5.0
HTTP_DEADLINE_S, HTTP_CAP = 2.0, 1024 * 1024
RUN_BUDGET_S = 4.0
MAX_PROFILES, MAX_REPLICAS, MAX_PAGES, PAGE_LIMIT = 8, 8, 8, 128
MAX_ROWS, MAX_RECORD_BYTES, MAX_ENGINE_RECORD_BYTES = 20000, 8192, 4096
MAX_CALLERS = 32
WINDOW_S = 60.0
STALE_S = 3 * INTERVAL
ABANDONED_S = 3600.0
CONTINUITY_S = 0.5
YOUNG_S = 30.0
PAGES_PER_RUN = 3
DEFAULTS = {"affinity_idle_s": 1800, "retention_days": 7}
BOUNDS = {"affinity_idle_s": (60, 86400), "retention_days": (1, 30)}
REQUEST_ID = re.compile(r"[0-9a-f]{16}")
JOINED_ID = re.compile(r"([0-9a-f]{16})\.([0-9]{1,3})")
FILTER = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")
CALLER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

NO_STORE = "metrics store not open in this portal process (samplers disabled)"
NOT_COLLECTED = "not collected yet (collectors run every 5 s while a profile runs)"
NEEDS_LOG = "needs router request log (TensorMeld request timeline)"
NEEDS_HIST = "needs armed router histograms (TensorMeld request timeline)"
NEEDS_SESSIONS = "needs router session keys (TensorMeld request timeline)"
NEEDS_NODE = "needs node host engine-status (fleet package)"
NEEDS_SERVICE = "needs prefill service request ids"
NEEDS_ENGINE_IDS = "needs engine request ids (instrumented engine build)"
AFFINITY_LABEL = "observed affinity · routing seen by the router, not confirmed cache residency"
GAP_LABEL = ("gap between delivered chunks, router-observed; speculative steps publish several tokens at once, "
             "so this is a delivery gap, not a per-token latency")

DDL = """
CREATE TABLE IF NOT EXISTS observe_requests (profile TEXT NOT NULL, instance TEXT NOT NULL, id TEXT NOT NULL,
    seq INTEGER NOT NULL, started_at REAL, ended_at REAL, updated_at REAL NOT NULL, state TEXT, outcome TEXT,
    model TEXT, route TEXT, caller TEXT, session TEXT, record TEXT NOT NULL, PRIMARY KEY(profile, instance, id));
CREATE INDEX IF NOT EXISTS observe_requests_started ON observe_requests(profile, started_at);
CREATE INDEX IF NOT EXISTS observe_requests_id ON observe_requests(id);
CREATE TABLE IF NOT EXISTS observe_engine_requests (profile TEXT NOT NULL, replica TEXT NOT NULL,
    instance TEXT NOT NULL, id TEXT NOT NULL, rid TEXT, attempt INTEGER, time REAL, ended REAL, seen_at REAL NOT NULL,
    record TEXT NOT NULL, PRIMARY KEY(profile, replica, instance, id));
CREATE INDEX IF NOT EXISTS observe_engine_rid ON observe_engine_requests(profile, rid);
CREATE INDEX IF NOT EXISTS observe_engine_seen ON observe_engine_requests(profile, seen_at);
"""

SERIES = {
    "engine_up": ("state", None, {"0": "unreachable", "1": "up"}),
    "engine_generated_tokens": ("delta", "tokens", None),
    "engine_prompt_tokens": ("delta", "tokens", None),
    "engine_cached_tokens": ("delta", "tokens", None),
    "engine_imported_tokens": ("delta", "tokens", None),
    "engine_completed_requests": ("delta", None, None),
    "engine_queue_waiting": ("gauge", None, None),
    "engine_queue_running": ("gauge", None, None),
    "engine_slots_busy": ("gauge", None, None),
    "engine_ttft_seconds_p50": ("gauge", "s", None),
    "engine_ttft_seconds_p95": ("gauge", "s", None),
    "engine_decode_tok_s_p50": ("gauge", "tok/s", None),
    "engine_draft_acceptance": ("gauge", None, None),
    "engine_draft_accepted_per_cycle": ("gauge", "tokens", None),
    "router_requests": ("delta", None, None),
    "usb4_exchange_seconds_per_step": ("gauge", "s", None),
}
COUNTERS = {"generated_tokens": "engine_generated_tokens", "prompt_tokens": "engine_prompt_tokens",
            "cached_tokens": "engine_cached_tokens", "imported_tokens": "engine_imported_tokens",
            "completed": "engine_completed_requests"}
BASELINES = "telemetry/baseline"


class Unavailable(Exception):
    """The source answered that it does not serve this endpoint; the text says why."""


HEADER_CAP = 64 * 1024
CRLF = b"\r\n"


def _parse_url(url):
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname or parts.port is None:
        raise ValueError("only http://host:port URLs from the launch records are read")
    return parts.hostname, parts.port, (parts.path or "/") + ("?" + parts.query if parts.query else "")


def _dechunk(body):
    out, pos = bytearray(), 0
    while True:
        end = body.find(CRLF, pos)
        if end < 0:
            raise ValueError("truncated chunked body")
        size = int(body[pos:end].split(b";", 1)[0], 16)
        if size == 0:
            return bytes(out)
        out += body[end + 2:end + 2 + size]
        pos = end + 2 + size + 2


def http_get(url, headers=None, text=False, deadline=None):
    """One HTTP/1.1 GET on a plain socket: every connect, send and receive gets the time left of one absolute
    deadline (at most 2 s), so a slow or trickling peer is cut at the deadline; headers <= 64 KiB, body <= 1 MiB;
    no proxy is involved and redirects are never followed. 404/405/501 -> Unavailable."""
    limit = time.monotonic() + HTTP_DEADLINE_S
    deadline = limit if deadline is None else min(deadline, limit)
    host, port, target = _parse_url(url)

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("response not complete within the deadline")
        return remaining
    lines = [f"GET {target} HTTP/1.1", f"Host: {_host(host)}:{port}", "Connection: close", "Accept-Encoding: identity",
             *(f"{k}: {v}" for k, v in (headers or {}).items())]
    request = CRLF.join(line.encode("latin-1") for line in lines) + CRLF + CRLF
    with socket.create_connection((host, port), timeout=left()) as sock:
        sock.settimeout(left())
        sock.sendall(request)
        data = bytearray()
        while True:
            sock.settimeout(left())
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > HEADER_CAP + HTTP_CAP + 4096:
                raise ValueError("response exceeds 1 MiB")
    head, sep, body = bytes(data).partition(CRLF + CRLF)
    if not sep or len(head) > HEADER_CAP:
        raise ValueError("malformed or oversized response head")
    status_line, *header_lines = head.decode("latin-1").split(CRLF.decode())
    try:
        code = int(status_line.split()[1])
    except (IndexError, ValueError):
        raise ValueError("malformed status line") from None
    fields = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        fields[name.strip().lower()] = value.strip()
    if "chunked" in fields.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    elif fields.get("content-length", "").isdigit():
        length = int(fields["content-length"])
        if len(body) < length:
            raise ValueError("response ended before its Content-Length")
        body = body[:length]
    if len(body) > HTTP_CAP:
        raise ValueError("response exceeds 1 MiB")
    if code in (404, 405, 501):
        raise Unavailable(f"HTTP {code}")
    if 300 <= code < 400:
        raise OSError(f"HTTP {code}: redirects are not followed")
    if code != 200:
        raise OSError(f"HTTP {code}")
    return body.decode("utf-8") if text else json.loads(body)


# Tests and the browser fixture replace this one function.
_http = http_get


# ------------------------------------------------------------------------------------------------- discovery

def _store(api):
    return _prepare(telemetry.open_store(api))


def _prepare(store):
    """The collectors' tables and series descriptors (created once per store; collectors only, never a GET)."""
    if not getattr(store, "_observe_ready", False):
        store.ensure_table(DDL)
        for name, (kind, unit, legend) in SERIES.items():
            store.describe(name, kind, unit=unit, interval=INTERVAL, legend=legend)
        store._observe_ready = True
    return store


def _host(address):
    return f"[{address}]" if ":" in address else address


def _loopback(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address == "localhost"


def launches(api):
    """(running profiles, omitted) from hyperloom's launch records (local files, no ssh): [{profile, router (url),
    key_name, service_key, replicas [{id, node, address, port, loopback}], family, service_port}]. At most 8
    profiles x 8 replicas; what is left out is counted, never silently dropped."""
    rows, omitted = [], {"profiles": 0, "replicas": 0}
    paths = sorted(secrets.runtime_dir().glob("up-*.json"))
    omitted["profiles"] = max(0, len(paths) - MAX_PROFILES)
    for path in paths[:MAX_PROFILES]:
        profile = path.stem[3:]
        try:
            state = api.orchestrate._read(path)
        except (OSError, ValueError):
            continue
        endpoint = (state.get("router") or {}).get("endpoint") if isinstance(state, dict) else None
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("address"), str) or type(endpoint.get("port")) is not int:
            continue
        rendered = [item for item in (state.get("rendered_router") or {}).get("replicas") or []
                    if isinstance(item, dict) and isinstance(item.get("address"), str)
                    and type(item.get("port")) is int and 1024 <= item["port"] <= 65535]
        omitted["replicas"] += max(0, len(rendered) - MAX_REPLICAS)
        replicas = [{"id": str(item.get("id") or item.get("node"))[:64], "node": str(item.get("node") or item.get("id"))[:64],
                     "address": item["address"], "port": item["port"], "loopback": _loopback(item["address"])}
                    for item in rendered[:MAX_REPLICAS]]
        service = state.get("prefill_service") or {}
        port = service.get("port") if isinstance(service, dict) else None
        rows.append({"profile": profile, "router": f"http://{_host(endpoint['address'])}:{endpoint['port']}",
                     "key_name": api.orchestrate._secret_name(profile, "router"),
                     "service_key": api.orchestrate._secret_name(profile, "prefill"), "replicas": replicas,
                     "family": (state.get("launch") or {}).get("family"),
                     "service_port": port if type(port) is int and 1024 <= port <= 65535 else None})
    return rows, omitted


_rotation_lock = threading.Lock()


def _rotated(api, name, items):
    """Start each run one task later, so a spent time budget never starves the same tasks (C5)."""
    with _rotation_lock:
        offsets = api.__dict__.setdefault("_observe_rotation", {})
        offset = offsets.get(name, 0)
        offsets[name] = offset + 1
    if not items:
        return items
    offset %= len(items)
    return items[offset:] + items[:offset]


def _bearer(name):
    key = secrets.get(name)
    return {"Authorization": "Bearer " + key} if key else {}


def _reconcile(store, namespace, keep):
    """Documents of sources that no longer run (not in the current launch records) are removed: a stopped engine
    or router is never shown as current."""
    for key, _, _ in store.items(namespace):
        if key not in keep:
            store.delete(namespace, key)


# ------------------------------------------------------------------------------------------------- the router

def _caller(value, seen):
    """C7: a key label as the router reports it, at most 32 labels per profile; anything else is anonymous."""
    if not isinstance(value, str) or not CALLER.fullmatch(value) or value == "other":
        return "anonymous"
    if value in seen or value == "anonymous":
        return value
    if len(seen) >= MAX_CALLERS:
        return "anonymous"
    seen.append(value)
    return value


def _key_attempts(attempts):
    """The attempts a join needs, whatever the bounds: the served (else last) decode attempt and the last prefill
    attempt."""
    decode = [a for a in attempts if a.get("kind") == "decode"]
    served = [a for a in decode if a.get("outcome") == "served"]
    prefill = [a for a in attempts if a.get("kind") == "prefill"]
    return [a for a in ((served or decode or [None])[-1], (prefill or [None])[-1]) if a is not None]


def _encode(record):
    """JSON of a record within 8 KiB: decisions and the route trace dropped first, then the attempts are cut to
    the first two plus the ones the join needs; the cut is named. Always valid JSON, or None."""
    text = json.dumps(record, separators=(",", ":"), allow_nan=False)
    if len(text) <= MAX_RECORD_BYTES:
        return text
    why = dict(record.get("parse_why") or {})
    why.update(decisions="dropped: record over 8 KiB", route_trace="dropped: record over 8 KiB")
    trimmed = {**record, "decisions": [], "route_trace": [], "parse_why": why}
    text = json.dumps(trimmed, separators=(",", ":"), allow_nan=False)
    if len(text) <= MAX_RECORD_BYTES:
        return text
    kept = record["attempts"][:2]
    kept += [a for a in _key_attempts(record["attempts"]) if a not in kept]
    trimmed["attempts"] = kept
    why["attempts"] = f"{len(record['attempts']) - len(kept)} attempts dropped: record over 8 KiB"
    text = json.dumps(trimmed, separators=(",", ":"), allow_nan=False)
    return text if len(text) <= MAX_RECORD_BYTES else None


def _ttft_ms(record):
    """Router-observed TTFT: arrival to the first delivered data event of a stream (None otherwise)."""
    if record.get("stream") is not True:
        return None
    return (record.get("stamps") or {}).get("first_data_event")


def _ended(record):
    last = (record.get("stamps") or {}).get("last_byte")
    return None if record.get("at") is None or last is None else record["at"] + last / 1000.0


def _quantiles(values, qs=(0.5, 0.95, 0.99)):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    result = {"n": len(values)}
    for q in qs:
        result[f"p{round(q * 100)}"] = values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))] if values else None
    return result


def _final_points(profile, finals):
    counts = {}
    for record in finals:
        key = (record.get("route") or "direct", record.get("outcome") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return [("router_requests", {"profile": profile, "route": route, "outcome": outcome}, count, "delta")
            for (route, outcome), count in sorted(counts.items())]


def _ingest(store, profile, instance, page, now, ts, seen_callers):
    """Apply one page in ONE transaction: record upserts, the final-once request counts and the cursor (the cursor
    is the baseline of the router's update sequence, so a crash between steps neither loses nor repeats a count).
    The baseline of an instance exists before its first page (``_pull``), so ``apply`` always runs here."""
    records = sorted(page.records, key=lambda r: r["seq"])
    known = {}
    ids = [r["id"] for r in records]
    for chunk in range(0, len(ids), 400):
        part = ids[chunk:chunk + 400]
        rows = store.execute(f"SELECT id, seq, state FROM observe_requests WHERE profile=? AND instance=? AND id IN "
                             f"({','.join('?' * len(part))})", (profile, instance, *part))
        known.update({row[0]: (row[1], row[2]) for row in rows})
    statements, finals, skipped = [], [], 0
    for record in records:
        old = known.get(record["id"])
        if old is not None and old[0] >= record["seq"]:
            continue
        record["caller"] = _caller(record.get("caller"), seen_callers)
        text = _encode(record)
        if text is None:
            skipped += 1
            continue
        if record.get("state") == "done" and (old is None or old[1] != "done"):
            finals.append(record)
        statements.append((
            "INSERT INTO observe_requests(profile, instance, id, seq, started_at, ended_at, updated_at, state, outcome, "
            "model, route, caller, session, record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile, instance, id) "
            "DO UPDATE SET seq=excluded.seq, started_at=excluded.started_at, ended_at=excluded.ended_at, "
            "updated_at=excluded.updated_at, state=excluded.state, outcome=excluded.outcome, model=excluded.model, "
            "route=excluded.route, caller=excluded.caller, session=excluded.session, record=excluded.record",
            (profile, instance, record["id"], record["seq"], record.get("at"), _ended(record), now, record.get("state"),
             record.get("outcome"), record.get("model"), record.get("route"), record["caller"], record.get("session"), text)))
        known[record["id"]] = (record["seq"], record.get("state"))
    cursor = max([page.next or 0] + [r["seq"] for r in records])
    points = _final_points(profile, finals)
    applied = []

    def apply(delta, info):
        applied.append(True)
        return {"points": points, "sql": statements}
    store.counter_delta(f"observe/{profile}/router-log", instance, float(cursor), ts, apply=apply)
    return (len(records), finals, skipped) if applied else (0, [], 0)


def _cursor(store, profile):
    return store.get(BASELINES, f"observe/{profile}/router-log")


def _gap(store, profile, now, text):
    gaps = store.get("observe/gaps", profile) or {"count": 0}
    store.put("observe/gaps", profile, {"count": gaps.get("count", 0) + 1, "last": now, "why": text})


def _abandon(store, profile, now, instance=None):
    """In-flight records whose final update can no longer arrive: their router restarted, or nothing changed for an
    hour (the router finishes records outside its ring). Shown as 'final state not observed'."""
    statements = [("UPDATE observe_requests SET state='unknown' WHERE profile=? AND state='in_flight' AND updated_at < ?",
                   (profile, now - ABANDONED_S))]
    if instance is not None:
        statements.append(("UPDATE observe_requests SET state='unknown' WHERE profile=? AND state='in_flight' "
                           "AND instance != ?", (profile, instance)))
    store.transaction(statements)


def _baseline(store, profile, instance, ts):
    """The zero baseline of a router instance, set before its first page so the page's apply runs (C3 bootstrap)."""
    store.counter_delta(f"observe/{profile}/router-log", instance, 0.0, ts)


def _pull(store, launch, headers, now, deadline):
    """Read the request log from the stored cursor, at most 3 pages (a bounded share of the run). Returns
    (records read, final records)."""
    profile = launch["profile"]
    seen = store.get("observe/callers", profile) or []
    count, finals, limit = 0, [], PAGE_LIMIT
    cursor = _cursor(store, profile)
    after = int(cursor["value"]) if cursor else 0
    instance = cursor["instance"] if cursor else None
    ts = max(now, (cursor or {}).get("ts", 0) + 1e-3)
    complete, pages = False, 0
    while pages < PAGES_PER_RUN and time.monotonic() < deadline:
        url = f"{launch['router']}/api/requests?" + urlencode({"after": after, "limit": limit})
        try:
            page = router.parse_requests(_http(url, headers, deadline=deadline))
        except ValueError as exc:
            if "exceeds 1 MiB" in str(exc) and limit > 1:
                limit = max(1, limit // 4)
                continue
            if "exceeds 1 MiB" in str(exc):
                _gap(store, profile, now, "a single router record exceeds 1 MiB: the log cannot be read past it")
            raise
        pages += 1
        current = page.instance or instance or "router"
        if instance is not None and (current != instance or (page.seq is not None and page.seq < after)):
            # A new router process: its sequence starts again; read it from the start.
            current = current if current != instance else f"{current}-{int(now)}"
            _abandon(store, profile, now, current)
            _gap(store, profile, now, "router restarted; updates of the previous router not read yet are lost")
            _baseline(store, profile, current, ts)
            ts += 1e-3
            instance, after = current, 0
            continue
        if instance is None:
            _baseline(store, profile, current, ts)
            ts += 1e-3
            instance = current
        if page.seq is not None and page.capacity and page.seq - after > page.capacity:
            _gap(store, profile, now, f"{page.seq - after} router updates since the last read exceed its ring of "
                                      f"{page.capacity}: some requests may be missing or show an earlier state")
        read, final, skipped = _ingest(store, profile, current, page, now, ts, seen)
        if skipped:
            _gap(store, profile, now, f"{skipped} records over 8 KiB even after trimming were not kept")
        ts += 1e-3
        count += read
        finals += final
        after = max([after, page.next or 0] + [r["seq"] for r in page.records])
        if not page.more:
            complete = True
            break
    if not complete:
        _gap(store, profile, now, "collector behind the router log (page or time share used up); continuing next run")
    store.put("observe/callers", profile, seen)
    _abandon(store, profile, now)
    return count, finals


def _router_gauges(store, profile, now):
    rows = store.execute("SELECT model, record FROM observe_requests WHERE profile=? AND ended_at >= ? AND "
                         "outcome='served'", (profile, now - WINDOW_S))
    by_model = {}
    for model, text in rows:
        value = _ttft_ms(json.loads(text))
        if value is not None:
            by_model.setdefault(model or "unknown", []).append(value / 1000.0)
    points = []
    for model, values in sorted(by_model.items()):
        q = _quantiles(values, (0.5, 0.95))
        labels = {"kind": "router", "profile": profile, "model": model}
        points += [("engine_ttft_seconds_p50", labels, q["p50"]), ("engine_ttft_seconds_p95", labels, q["p95"])]
    return points


def _bound_families(families):
    """At most 32 families x 64 label sets; the +Inf bound is stored as null (JSON has no infinity)."""
    return {name: [{**line, "buckets": [[None if b == math.inf else b, c] for b, c in line["buckets"]]}
                   for line in lines[:64]] for name, lines in list(families.items())[:32]}


def _router_instance(samples):
    return next((dict(labels)["instance"][:80] for (name, labels), _ in samples.items()
                 if name == "tensormeld_router_instance_info" and dict(labels).get("instance")), None)


def _sessions(launch, headers, deadline):
    """The router's session view, reduced from 256 entries when an answer is over the cap; coverage disclosed."""
    limit = 256
    while True:
        try:
            head, sessions, why = router.parse_sessions(
                _http(f"{launch['router']}/api/sessions?limit={limit}", headers, deadline=deadline))
            break
        except ValueError as exc:
            if "exceeds 1 MiB" not in str(exc) or limit <= 8:
                raise
            limit //= 4
    if head.get("count") is not None and head["count"] > len(sessions):
        why["coverage"] = f"{len(sessions)} newest of {head['count']} sessions read"
    return {**head, "limit": limit, "returned": len(sessions), "sessions": sessions, "why": why}


ROUTER_PARTS = ("requests", "metrics", "sessions")


def collect_router(api, now, store=None):
    """One collection of every running profile's router: request log, histograms and sessions as separate tasks,
    rotated across runs. Returns (gauge points, attempted, succeeded)."""
    store = _prepare(store) if store is not None else _store(api)
    points, attempted, succeeded = [], 0, 0
    deadline = time.monotonic() + RUN_BUDGET_S
    retention = settings(store)["retention_days"] * 86400
    found, omitted = launches(api)
    store.put("observe/inventory", "router", {"ts": now, "profiles": [l["profile"] for l in found], "omitted": omitted})
    _reconcile(store, "observe/source", {launch["profile"] for launch in found})
    sources = {}
    for launch in found:
        previous = store.get("observe/source", launch["profile"]) or {}
        sources[launch["profile"]] = {"ts": now, "why": {}, "last_ok": dict(previous.get("last_ok") or {}),
                                      "failing_since": dict(previous.get("failing_since") or {}),
                                      "requests": previous.get("requests")}
    tasks = [(launch, part) for launch in found for part in ROUTER_PARTS]
    for launch, part in _rotated(api, "router", tasks):
        source = sources[launch["profile"]]
        if time.monotonic() >= deadline:
            source["why"][part] = "collector time budget spent; read next run"
            continue
        attempted += 1
        try:
            headers = _bearer(launch["key_name"]) if part != "metrics" else {}
            if part == "requests":
                share = min(deadline, time.monotonic() + RUN_BUDGET_S / 2)
                count, finals = _pull(store, launch, headers, now, share)
                source["requests"] = {"read": count, "finished": len(finals)}
            elif part == "metrics":
                samples = prom.parse(_http(launch["router"] + "/metrics", text=True, deadline=deadline))
                families = router.histograms(samples, "tensormeld_router_")
                if not families:
                    raise Unavailable("no histogram families")
                store.put("observe/router-metrics", launch["profile"], {"ts": now, "instance": _router_instance(samples),
                                                                       "families": _bound_families(families)})
            else:
                store.put("observe/sessions", launch["profile"], {"ts": now, **_sessions(launch, headers, deadline)})
            source["last_ok"][part] = now
            source["failing_since"].pop(part, None)
            succeeded += 1
        except Unavailable as exc:
            source["why"][part] = {"requests": NEEDS_LOG, "metrics": NEEDS_HIST, "sessions": NEEDS_SESSIONS}[part] + f" ({exc})"
            source["last_ok"][part] = now           # the router answered: the feature is absent, the source works
            source["failing_since"].pop(part, None)
            succeeded += 1
        except (OSError, ValueError, AdapterError, TimeoutError) as exc:
            source["why"][part] = f"router {part} unreadable: {exc}"
            source["failing_since"].setdefault(part, now)
    for launch in found:
        store.put("observe/source", launch["profile"], sources[launch["profile"]])
        points += _router_gauges(store, launch["profile"], now)
        _prune(store, launch["profile"], now - retention)
    for name in ("observe/router-metrics", "observe/sessions"):
        _reconcile(store, name, {launch["profile"] for launch in found})
    return points, attempted, succeeded


def _prune(store, profile, before):
    store.transaction([
        ("DELETE FROM observe_requests WHERE profile=? AND updated_at < ?", (profile, before)),
        ("DELETE FROM observe_requests WHERE profile=? AND rowid IN (SELECT rowid FROM observe_requests WHERE profile=? "
         "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (profile, profile, MAX_ROWS)),
        ("DELETE FROM observe_engine_requests WHERE profile=? AND seen_at < ?", (profile, before)),
        ("DELETE FROM observe_engine_requests WHERE profile=? AND rowid IN (SELECT rowid FROM observe_engine_requests "
         "WHERE profile=? ORDER BY seen_at DESC LIMIT -1 OFFSET ?)", (profile, profile, MAX_ROWS)),
    ])


# ------------------------------------------------------------------------------------------------- engines

def _read_direct(replica, now, deadline, clock=time.time):
    """One reading of a LAN-bound engine at the address of our own launch record: {status, metrics_text, health,
    why, observed_at}. ``/health`` is read first, so an unreachable engine is known as such before the budget goes to
    the larger answers; a read that was not attempted stays unknown (with why). The observation time is taken when
    the counter-bearing answers arrived."""
    base = f"http://{_host(replica['address'])}:{replica['port']}"
    reading = {"status": None, "metrics_text": None, "health": None, "why": {}, "observed_at": now, "via": "direct"}
    try:
        _http(base + "/health", deadline=deadline)
        reading["health"] = True
    except Unavailable:
        reading["health"] = True                 # it answered: up, without a health route
    except TimeoutError as exc:
        reading["why"]["health"] = "not read: " + str(exc)
        return reading
    except (OSError, ValueError) as exc:
        reading["health"] = False                # advisory: status and metrics still get their own bounded reads
        reading["why"]["health"] = "health check failed: " + str(exc)
    for part, path, text in (("status", "/api/status?detail=1", False), ("metrics", "/metrics", True)):
        try:
            reading["status" if part == "status" else "metrics_text"] = _http(base + path, text=text, deadline=deadline)
            reading[part + "_at"] = clock()
        except Unavailable as exc:
            reading["why"][part] = f"engine serves no {part} ({exc})"
        except (OSError, ValueError, TimeoutError) as exc:
            reading["why"][part] = f"engine {part} unreadable: {exc}"
    reading["observed_at"] = reading.get("status_at") or reading.get("metrics_at") or now
    return reading


def _read_via_fleet(store, profile, replica, now, stale=STALE_S):
    """One reading of a loopback-bound engine from the fleet sampler's hand-off document
    (``fleet/engine/<machine>:<profile>:<port>``), checked against our launch record; observed at the node sample's
    own time."""
    key = f"{replica['node']}:{profile}:{replica['port']}"
    found = store.get("fleet/engine", key)
    reading = {"status": None, "metrics_text": None, "health": None, "why": {}, "observed_at": None,
               "via": "node (fleet sampler)", "source_key": key}
    if not isinstance(found, dict):
        reading["why"]["status"] = NEEDS_NODE + " · no node sample for this engine yet"
        return reading
    mismatch = [name for name, want in (("machine", replica["node"]), ("profile", profile), ("port", replica["port"]))
                if found.get(name) != want]
    if found.get("replica") != replica["id"]:
        mismatch.append("replica")
    if mismatch or type(found.get("ts")) not in (int, float):
        reading["why"]["status"] = "node sample does not match the launch record (" + ", ".join(mismatch or ["ts"]) + ")"
        return reading
    reading["observed_at"] = float(found["ts"])
    instance = found.get("instance")
    reading["fleet_instance"] = instance[:80] if isinstance(instance, str) and instance else None
    reading["fleet_start"] = found.get("start") if type(found.get("start")) in (int, float) else None
    status = found.get("status")
    reading["status"] = status if isinstance(status, dict) else None
    reading["metrics_text"] = found.get("metrics_text") if isinstance(found.get("metrics_text"), str) else None
    outcomes = found.get("outcomes") if isinstance(found.get("outcomes"), dict) else {}
    if reading["status"] is None:
        reading["why"]["status"] = "engine status not in the node sample" + (
            f" ({outcomes.get('/api/status?detail=1') or outcomes.get('/api/status')})" if outcomes else "")
    if reading["metrics_text"] is None:
        reading["why"]["metrics"] = "engine metrics not in the node sample" + (
            f" ({outcomes.get('/metrics')})" if outcomes.get("/metrics") else "")
    health = found.get("health")
    reading["health"] = health is True or (isinstance(health, dict) and health.get("status") in ("ok", 200)) or (
        outcomes.get("/health") == "ok") if health is not None or "/health" in outcomes else None
    age = now - reading["observed_at"]
    if age > stale:
        reading["why"]["stale"] = f"node sample is {age:.0f} s old"
        reading["stale"] = True
    return reading


def _identity(store, key, observed, extra, reading, uptime):
    """(instance id, start time, basis, why, counter source) of one engine reading. Counters are accounted only
    under an identity carried by the answer that also carries the counters (or the fleet sampler's validated
    identity of the same node sample), so a restart between two answers can never put a new process's counters on
    the old instance:
    - the metrics answer with ``strix_engine_info{instance}`` or ``strix_engine_start_time_seconds`` -> metrics counters;
    - else the status answer with ``instance`` (id or start time) -> the status totals;
    - else the fleet sampler's ``instance`` for this node sample;
    - else (older builds) the status totals with a start derived from that same answer's uptime, kept for the stored
      id only while elapsed time and uptime advance together (within 0.5 s) and the engine is at least 30 s old;
    - else none: nothing is accounted.
    Two answers naming different ids or start times (more than 1 s apart) are a conflict: nothing is accounted."""
    extra = extra or {}
    status_id, status_start = observed.get("instance"), observed.get("started_at")
    metrics_id, metrics_start = extra.get("instance"), extra.get("start_time")
    fleet_id = reading.get("fleet_instance")
    ids = {value for value in (status_id, metrics_id, fleet_id) if value}
    if len(ids) > 1 or (status_start is not None and metrics_start is not None and abs(status_start - metrics_start) > 1):
        return None, None, "conflict", "the answers name different engine instances (a restart between them)", None
    has_metrics, has_status = reading.get("metrics_text") is not None, reading.get("status") is not None
    if has_metrics and (metrics_id or metrics_start is not None):
        return (metrics_id or f"start-{metrics_start:.3f}"), metrics_start or status_start, (
            "engine instance id" if metrics_id else "engine start time"), None, "metrics"
    if has_status and (status_id or status_start is not None):
        return (status_id or f"start-{status_start:.3f}"), status_start, (
            "engine instance id" if status_id else "engine start time"), None, "status"
    if fleet_id:
        return (fleet_id, reading.get("fleet_start"), "node sampler's engine identity", None,
                "status" if has_status else "metrics")
    observed_at = reading["observed_at"]
    if uptime is None or not has_status:
        return None, None, "unavailable", "no engine identity (build without instance id, start time or uptime)", None
    if uptime < YOUNG_S:
        store.delete("observe/instance", key)
        return None, None, "derived from uptime", f"engine younger than {YOUNG_S:.0f} s: its identity is not certain yet", None
    stored = store.get("observe/instance", key)
    if stored and stored.get("observed_at") is not None and uptime > stored["uptime"] and abs(
            (uptime - stored["uptime"]) - (observed_at - stored["observed_at"])) <= CONTINUITY_S:
        store.put("observe/instance", key, {**stored, "uptime": uptime, "observed_at": observed_at})
        return stored["instance"], stored["start"], "derived from uptime", None, "status"
    start = observed_at - uptime
    instance = f"start-{start:.3f}"
    store.put("observe/instance", key, {"instance": instance, "start": start, "uptime": uptime, "observed_at": observed_at})
    return instance, start, "derived from uptime", None, "status"


def _paired(store, key, instance, numerator, denominator, ts):
    """Deltas of two counters over the same interval: both baselines last advanced at the same sample and advance
    together now; a positive denominator. Otherwise None."""
    if numerator is None or denominator is None:
        return None
    before = [store.get(BASELINES, f"{key}/{part}") for part in ("num", "den")]
    a, _ = store.counter_delta(f"{key}/num", instance, numerator, ts)
    b, _ = store.counter_delta(f"{key}/den", instance, denominator, ts)
    if a is None or b is None or not b or None in before or before[0]["ts"] != before[1]["ts"]:
        return None
    return a, b


def collect_engines(api, now, store=None):
    """One collection of every running engine: the router members only (rank 0 of a distributed engine; followers
    never serve HTTP and are never read or counted), and every RTX prefill service. Engines and services are
    separate tasks rotated across runs. Returns (gauge points to record now, attempted, succeeded); readings taken
    from the fleet sampler are recorded at their own observation time by ``_engine_sample``."""
    store = _prepare(store) if store is not None else _store(api)
    points, attempted, succeeded = [], 0, 0
    deadline = time.monotonic() + RUN_BUDGET_S
    found, omitted = launches(api)
    keep = {f"{launch['profile']}/{replica['id']}" for launch in found for replica in launch["replicas"]}
    _reconcile(store, "observe/engine", keep)
    _reconcile(store, "observe/service", {launch["profile"] for launch in found if launch["service_port"]})
    store.put("observe/inventory", "engines", {"ts": now, "engines": sorted(keep), "omitted": omitted})
    tasks = [("engine", launch, replica) for launch in found for replica in launch["replicas"]]
    tasks += [("service", launch, None) for launch in found if launch["service_port"]]
    for kind, launch, replica in _rotated(api, "engines", tasks):
        if kind == "service":
            if time.monotonic() < deadline:
                attempted += 1
                succeeded += bool(_service_sample(store, launch, now, deadline))
            continue
        key = f"{launch['profile']}/{replica['id']}"
        if not replica["loopback"] and time.monotonic() >= deadline:
            previous = store.get("observe/engine", key)
            if previous:
                previous.setdefault("why", {})["stale"] = "collector time budget spent; snapshot from an earlier run"
                store.put("observe/engine", key, previous)
            continue
        attempted += 1
        reading = (_read_via_fleet(store, launch["profile"], replica, now, stale_after(api, "fleet-nodes"))
                   if replica["loopback"]
                   else _read_direct(replica, now, deadline, api.clock))
        if reading["status"] is not None or reading["metrics_text"] is not None or reading["health"]:
            succeeded += 1
        points += _engine_sample(store, launch, replica, reading, now)
    return points, attempted, succeeded


def _engine_sample(store, launch, replica, reading, now):
    profile, rid, family = launch["profile"], replica["id"], launch["family"]
    key = f"{profile}/{rid}"
    machine = replica["node"]
    previous = store.get("observe/engine", key) or {}
    observed_at = reading["observed_at"]
    snapshot = {"profile": profile, "replica": rid, "machine": machine, "family": family, "via": reading["via"],
                "ts": now, "observed_at": observed_at, "status": None, "metrics": None, "observed": None, "extra": None,
                "why": dict(reading["why"]), "last_ok": dict(previous.get("last_ok") or {}),
                "failing_since": dict(previous.get("failing_since") or {}),
                "counter_resets": list(previous.get("counter_resets") or [])[-20:]}
    labels = {"profile": profile, "replica": rid, "machine": machine}
    # A fleet document is one node sample: it is accounted once, at its own time, however often it is read.
    repeat = (observed_at is not None and previous.get("observed_at") is not None
              and observed_at <= previous["observed_at"] and reading["via"] != "direct")
    stale = bool(reading.get("stale"))
    if observed_at is None or repeat or stale:
        keep = {k: previous.get(k) for k in ("status", "metrics", "observed", "extra", "model", "instance",
                                             "instance_basis", "started_at", "last_served", "last_served_basis",
                                             "completed", "state", "observed_at")}
        snapshot.update({k: v for k, v in keep.items() if v is not None})
        if observed_at is None:
            snapshot["state"] = "unavailable"
        elif stale:
            snapshot["state"] = "stale"
        snapshot["ts"] = now
        store.put("observe/engine", key, snapshot)
        return []
    for part, ok in (("health", reading["health"]), ("status", reading["status"] is not None),
                     ("metrics", reading["metrics_text"] is not None)):
        if ok:
            snapshot["last_ok"][part] = observed_at
            snapshot["failing_since"].pop(part, None)
        elif part in reading["why"] and "not read" not in reading["why"][part]:
            snapshot["failing_since"].setdefault(part, now)
    parsed_status = observed = metrics = extra = None
    if reading["status"] is not None:
        try:
            parsed_status = engine.parse_status(reading["status"])
            observed = engine.parse_observed(reading["status"])
            snapshot["last_ok"]["status"] = observed_at
        except AdapterError as exc:
            snapshot["why"]["status"] = "engine status: " + str(exc)
    if reading["metrics_text"] is not None:
        try:
            if family == "minimax-pair":
                metrics, extra = minimax.parse_metrics(reading["metrics_text"])
            else:
                metrics = engine.parse_metrics(reading["metrics_text"])
                extra = engine.parse_metrics_extra(reading["metrics_text"])
            snapshot["last_ok"]["metrics"] = observed_at
        except AdapterError as exc:
            snapshot["why"]["metrics"] = "engine metrics: " + str(exc)
    up = parsed_status is not None or metrics is not None or bool(reading["health"])
    points = [("engine_up", labels, 1 if up else 0, "state")]
    if parsed_status is None and metrics is None:
        snapshot.update(state="up · no status or metrics" if up else "unreachable",
                        last_served=previous.get("last_served"), last_served_basis=previous.get("last_served_basis"),
                        instance=previous.get("instance"), started_at=previous.get("started_at"), model=previous.get("model"))
        store.put("observe/engine", key, snapshot)
        return _at(store, points, observed_at, now)
    observed = observed or {"instance": None, "started_at": None, "armed": None, "armed_since": None, "observed": None,
                            "totals": {}, "speculative": None, "prefix_cache": None, "histograms": None, "requests": [],
                            "why": {}}
    if family == "minimax-pair" and observed.get("speculative") is None and extra:
        observed["speculative"] = minimax.speculative(extra)
    model = (parsed_status.model if parsed_status else None) or family or "unknown"
    uptime = parsed_status.uptime_seconds if parsed_status else None
    instance, started, basis, conflict, source = _identity(store, key, observed, extra, reading, uptime)
    series_labels = {**labels, "model": model, "instance": instance or "unknown"}
    if conflict:
        snapshot["why"]["instance"] = conflict
    # Counters -> deltas through the persisted baselines (C3), only with an identity carried by the same answer as
    # the counters (see _identity).
    if source == "metrics" and metrics is not None:
        values = {field: getattr(metrics, field) for field in ("generated_tokens", "prompt_tokens", "cached_tokens", "completed")}
        values["imported_tokens"] = (extra or {}).get("imported_tokens")
    elif parsed_status is not None:
        totals = parsed_status.totals or {}
        values = {"generated_tokens": totals.get("generated_tokens"), "prompt_tokens": totals.get("prompt_tokens"),
                  "cached_tokens": totals.get("cached_tokens"), "completed": (parsed_status.queue or {}).get("completed"),
                  "imported_tokens": (observed.get("totals") or {}).get("imported_tokens")}
    else:
        values = {"completed": metrics.completed if metrics else None}
    counter_at = reading.get("metrics_at", observed_at) if source == "metrics" else observed_at
    if instance is not None and source is not None:
        for field, name in COUNTERS.items():
            value = values.get(field)
            if value is None:
                continue
            _, info = store.counter_delta(f"observe/{key}/{field}", instance, value, counter_at, max_gap=6 * INTERVAL,
                                          apply=lambda delta, info, name=name: {"points": [(name, series_labels, delta, "delta")]})
            if info.get("unobserved_s"):
                snapshot["counter_resets"] = (snapshot["counter_resets"] + [{"ts": counter_at, "counter": field,
                                              "unobserved_s": info["unobserved_s"], "instance": instance}])[-20:]
            if info.get("uncertain"):
                snapshot["why"][field] = "increase spread over a gap longer than 30 s (placement in time approximate)"
    else:
        snapshot["why"].setdefault("counters", "not accounted: " + (conflict or "no engine identity"))
    queue = (parsed_status.queue or {}) if parsed_status else {}
    waiting = metrics.waiting if metrics and metrics.waiting is not None else queue.get("queued")
    running = metrics.running if metrics and metrics.running is not None else queue.get("running")
    slots_busy = metrics.slots_busy if metrics and metrics.slots_busy is not None else (
        (parsed_status.slots or {}).get("busy") if parsed_status else None)
    points += [("engine_queue_waiting", series_labels, waiting), ("engine_queue_running", series_labels, running),
               ("engine_slots_busy", series_labels, slots_busy)]
    ring = ({request["id"]: request for request in (parsed_status.requests or []) if request and request.get("id")}
            if parsed_status else {})
    fields = {request["id"]: request for request in observed.get("requests") or []}
    new = _store_engine_requests(store, profile, rid, instance or "unknown", ring, fields, now) if instance else []
    # Window quantiles over successful requests (status "done") that ended in the last 60 s; the end is
    # reconstructed as arrival + duration.
    recent = [r for r in ring.values() if r.get("status") == "done" and _engine_end(r) is not None
              and observed_at - WINDOW_S <= _engine_end(r) <= observed_at + 5]
    measured = [fields[r["id"]]["first_token_ms"] / 1000.0 for r in recent
                if (fields.get(r["id"]) or {}).get("first_token_ms") is not None]
    approx = [r["engine_ttft_ms"] / 1000.0 for r in recent if r.get("engine_ttft_ms") is not None
              and (fields.get(r["id"]) or {}).get("first_token_ms") is None]
    for basis_name, window in (("first_token", measured), ("queue_prefill", approx)):
        if window:
            q = _quantiles(window, (0.5, 0.95))
            ttft_labels = {**series_labels, "kind": "engine", "basis": basis_name}
            points += [("engine_ttft_seconds_p50", ttft_labels, q["p50"]), ("engine_ttft_seconds_p95", ttft_labels, q["p95"])]
    points.append(("engine_decode_tok_s_p50", series_labels,
                   _quantiles([r.get("decode_tok_s") for r in recent if r.get("output_tokens")], (0.5,))["p50"]))
    # Speculative decoding: interval ratios of the cumulative counters, else of the newly completed requests.
    spec = observed.get("speculative") or {}
    method = spec.get("method")
    ratio = cycles_ratio = None
    if spec.get("accepted") is not None and spec.get("proposed") is not None:
        if instance is not None:
            pair = _paired(store, f"observe/{key}/spec", instance, spec["accepted"], spec["proposed"], observed_at)
            ratio = pair[0] / pair[1] if pair else None
            pair = _paired(store, f"observe/{key}/cycles", instance, spec["accepted"], spec.get("cycles"), observed_at)
            cycles_ratio = pair[0] / pair[1] if pair else None
    elif new:
        done = [fields.get(i) or {} for i in new if (ring.get(i) or {}).get("status") == "done"]
        known = [p for p in done if p.get("draft_accepted") is not None and p.get("draft_proposed") is not None]
        proposed = sum(p["draft_proposed"] for p in known)
        if known and proposed:
            method = method or "mtp"
            ratio = sum(p["draft_accepted"] for p in known) / proposed
            cycles = [p.get("draft_cycles") for p in known]
            if all(cycles):
                cycles_ratio = sum(p["draft_accepted"] for p in known) / sum(cycles)
    if method and ratio is not None:
        points.append(("engine_draft_acceptance", {**series_labels, "method": method}, ratio))
    if method and cycles_ratio is not None:
        points.append(("engine_draft_accepted_per_cycle", {**series_labels, "method": method}, cycles_ratio))
    group = (parsed_status.group or {}) if parsed_status else {}
    batch = (parsed_status.batch or {}) if parsed_status else {}
    if instance is not None:
        pair = _paired(store, f"observe/{key}/exchange", instance, group.get("exchange_seconds"), batch.get("steps"),
                       observed_at)
        if pair:
            points.append(("usb4_exchange_seconds_per_step", {**labels, "instance": instance}, pair[0] / pair[1]))
    # Last activity: the newest successful completion in the ring (reconstructed), or the sample interval in which
    # the completed counter moved (an interval, not an exact time).
    completed = values.get("completed")
    same = previous.get("instance") == instance and instance is not None
    ends = [_engine_end(r) for r in ring.values() if r.get("status") == "done" and _engine_end(r) is not None]
    last, last_basis = (max(ends), "newest completed request in the engine ring (arrival + duration)") if ends else (None, None)
    if same and previous.get("last_served") is not None and (last is None or previous["last_served"] > last):
        last, last_basis = previous["last_served"], previous.get("last_served_basis")
    if same and previous.get("completed") is not None and completed is not None and completed > previous["completed"]:
        interval = [previous.get("observed_at"), observed_at]
        if last is None or observed_at > last:
            last, last_basis = observed_at, f"completed counter moved between samples ({interval[0]:.0f}..{observed_at:.0f})"
    histograms = dict((extra or {}).get("histograms") or {})
    for name, lines in (observed.get("histograms") or {}).items():
        histograms.setdefault(name, lines)
    snapshot.update(status=_bounded_status(parsed_status), metrics=asdict(metrics) if metrics else None,
                    observed={k: v for k, v in observed.items() if k not in ("requests", "histograms")},
                    extra=_bounded_extra({**(extra or {}), "histograms": histograms}),
                    model=model, instance=instance, instance_basis=basis, started_at=started,
                    last_served=last, last_served_basis=last_basis, completed=completed,
                    state=(parsed_status.state if parsed_status else None) or "up")
    store.put("observe/engine", key, snapshot)
    return _at(store, points, observed_at, now)


def _at(store, points, observed_at, now):
    """Points observed now go back to the sampler; points of an earlier node sample are recorded at its own time."""
    if observed_at is None or abs(observed_at - now) < 1e-6:
        return points
    store.record(points, ts=observed_at, interval=INTERVAL)
    return []


def _engine_end(request):
    if request.get("time") is None:
        return None
    return request["time"] + (request.get("duration_ms") or 0.0) / 1000.0


def _bounded_status(parsed):
    if parsed is None:
        return None
    value = asdict(parsed)
    value.pop("requests", None)
    return value


def _bounded_extra(extra):
    if not extra:
        return None
    result = {k: v for k, v in extra.items() if k != "histograms"}
    result["histograms"] = _bound_families(extra.get("histograms") or {})
    return result


def _store_engine_requests(store, profile, replica, instance, ring, fields, now):
    """Insert ring records not seen before (a record in the ring is complete); returns their ids."""
    if not ring:
        return []
    ids = list(ring)[:512]
    rows = store.execute(f"SELECT id FROM observe_engine_requests WHERE profile=? AND replica=? AND instance=? AND id IN "
                         f"({','.join('?' * len(ids))})", (profile, replica, instance, *ids))
    seen = {row[0] for row in rows}
    statements, new = [], []
    for rid in ids:
        if rid in seen:
            continue
        request = {k: v for k, v in ring[rid].items() if k != "why"}
        request.update({k: v for k, v in (fields.get(rid) or {}).items() if k != "id" and v is not None})
        text = json.dumps(request, separators=(",", ":"), allow_nan=False)
        if len(text) > MAX_ENGINE_RECORD_BYTES:
            continue
        match = JOINED_ID.fullmatch(request.get("request_id") or rid)
        statements.append(("INSERT OR IGNORE INTO observe_engine_requests(profile, replica, instance, id, rid, attempt, "
                           "time, ended, seen_at, record) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (profile, replica, instance, rid, match[1] if match else None,
                            int(match[2]) if match else None, request.get("time"), _engine_end(request), now, text)))
        new.append(rid)
    store.transaction(statements)
    return new


SERVICE_SCHEMA = "strix-prefill-service-status-v1"
SERVICE_SECONDS = {"queue_ms": "queue", "prefill_ms": "prefill", "handoff_ms": "handoff", "duration_ms": "total"}


def _service_record(value):
    """One record of the service's ring -> the fields this package joins (times in ms)."""
    reader = engine.Reader(value)
    record = {"id": value["id"][:80], **{k: reader.get(k, kind) for k, kind in
                                         {"time": float, "computed_tokens": int, "tokens": int, "state_bytes": int,
                                          "outcome": str, "status": int}.items() if value.get(k) is not None}}
    seconds = value.get("seconds") if isinstance(value.get("seconds"), dict) else {}
    for name, key in SERVICE_SECONDS.items():
        number = seconds.get(key)
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            record[name] = number * 1000.0
    if record.get("computed_tokens") is None and record.get("tokens") is not None:
        record["computed_tokens"] = record["tokens"]
    return record


def _service_sample(store, launch, now, deadline):
    """The RTX prefill service's own records (joined by request id), read with the service key on its loopback
    port, when its status serves them. Returns True when the service answered."""
    profile = launch["profile"]
    loopback = str(ipaddress.ip_address(socket.INADDR_LOOPBACK))
    try:
        document = _http(f"http://{loopback}:{launch['service_port']}/api/status?detail=1",
                         _bearer(launch["service_key"]), deadline=deadline)
    except Unavailable as exc:
        store.put("observe/service", profile, {"ts": now, "why": f"{NEEDS_SERVICE} ({exc})"})
        return True
    except (OSError, ValueError, TimeoutError) as exc:
        store.put("observe/service", profile, {"ts": now, "why": "prefill service status unreadable: " + str(exc)})
        return False
    if not isinstance(document, dict) or document.get("schema") not in (None, SERVICE_SCHEMA) or not isinstance(
            document.get("requests"), list):
        store.put("observe/service", profile, {"ts": now, "why": NEEDS_SERVICE})
        return True
    ring = {}
    for value in document["requests"][:512]:
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            record = _service_record(value)
            ring[record["id"]] = record
    instance, _ = engine.instance_of(document)
    _store_engine_requests(store, profile, "prefill-service", instance or "service", ring, {}, now)
    store.put("observe/service", profile, {"ts": now, "why": None, "count": len(ring), "instance": instance})
    return True


def _sample(collect, api, now):
    store = _store(api)
    points, attempted, succeeded = collect(api, now, store)
    if attempted and not succeeded:
        # Keep what was learned (engine_up = 0), then report the failure to the sampler (C10: source stale).
        store.record(points, ts=now, interval=INTERVAL)
        raise RuntimeError(f"no source answered ({attempted} tried)")
    return points


def sample_router(api, now):
    return _sample(collect_router, api, now)


def sample_engines(api, now):
    return _sample(collect_engines, api, now)


# ------------------------------------------------------------------------------------------------- documents

def _readable(store):
    """True when the collectors created their tables in this store (a GET never creates them)."""
    rows = store.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
                         "('observe_requests', 'observe_engine_requests')")
    return rows[0][0] == 2


def settings(store):
    value = store.get("observe/settings", "settings") if store is not None else None
    return {**DEFAULTS, **(value or {})}


def _query(query, allowed):
    unknown = sorted(set(query) - set(allowed))
    if unknown:
        raise ValueError("unknown query arguments: " + ", ".join(unknown))
    for key in allowed:
        if key in query and key not in ("limit", "before", "range") and not FILTER.fullmatch(query[key]):
            raise ValueError("invalid " + key)
    return query


def _int(query, key, default, low, high):
    if key not in query:
        return default
    try:
        value = int(query[key])
    except ValueError:
        raise ValueError(key + " must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{key} must be in {low}..{high}")
    return value


def _number(query, key, default):
    if key not in query:
        return default
    try:
        value = float(query[key])
    except ValueError:
        raise ValueError(key + " must be a number") from None
    if not math.isfinite(value):
        raise ValueError(key + " must be finite")
    return value


def stale_after(api, collector="observe-router"):
    """3 intervals of a collector as configured in this portal (C10), else 3 x the default."""
    sampler = getattr(api, "sampler", None)
    interval = (getattr(sampler, "collectors", {}) or {}).get(collector, (None, INTERVAL))[1] if sampler else INTERVAL
    return 3 * float(interval or INTERVAL)


def _sources(store, profile=None, now=None, stale=STALE_S):
    """Router sources with, per part, whether it is stale (no answer within 3 collector intervals, C10)."""
    out = {}
    for key, _, value in store.items("observe/source"):
        if profile not in (None, key):
            continue
        last_ok = value.get("last_ok") or {}
        if now is not None:
            value["stale"] = {part: last_ok.get(part) is None or now - last_ok[part] > stale for part in ROUTER_PARTS}
        out[key] = value
    return out


def _reasons(sources, part):
    return sorted({(source.get("why") or {}).get(part) for source in sources.values()} - {None})


ROUTER_PHASES = (("read body", ("arrived",), "body_read"), ("tokenize", ("body_read",), "tokenized"),
                 ("slot wait", ("tokenized", "body_read"), "slot_acquired"),
                 ("route decision", ("slot_acquired", "tokenized", "body_read"), "route_decided"),
                 ("RTX hand-off", ("handoff_started",), "handoff_done"),
                 ("upstream wait", ("handoff_done", "route_decided", "slot_acquired"), "upstream_headers"),
                 ("first body byte", ("upstream_headers",), "first_body_byte"),
                 ("first data event", ("first_body_byte",), "first_data_event"),
                 ("delivery", ("first_data_event", "first_body_byte"), "last_byte"))


def _stamp(stamps, name):
    return stamps.get(name, 0.0 if name == "arrived" else None)


def _phases(stamps):
    """Router-observed phases (ms from arrival) between stamps the router recorded."""
    phases = []
    for name, starts, end in ROUTER_PHASES:
        begin = next((_stamp(stamps, s) for s in starts if _stamp(stamps, s) is not None), None)
        finish = stamps.get(end)
        if begin is not None and finish is not None and finish >= begin:
            phases.append({"phase": name, "start_ms": begin, "end_ms": finish})
    return phases


def _c8(prompt, cached, imported, reported=None):
    """(computed, why) per C8: prompt - cached - imported only from complete, consistent inputs; a count the engine
    reports itself is shown as reported (and a disagreement is named)."""
    if prompt is None or cached is None or imported is None:
        missing = [n for n, v in (("prompt", prompt), ("cached", cached), ("imported", imported)) if v is None]
        if reported is not None:
            return reported, None
        return None, "needs " + ", ".join(missing) + " token counts"
    if cached + imported > prompt:
        return None, "inconsistent counts: cached + imported exceed the prompt"
    computed = prompt - cached - imported
    if reported is not None and reported != computed:
        return reported, f"engine reports {reported} computed tokens; prompt - cached - imported = {computed}"
    return computed, None


def _engine_view(record, attempt):
    """The engine-observed block of a joined request: its own durations on its own clock (not placed on the
    router's axis: the two processes share no clock)."""
    if record is None:
        return None
    computed, c8_why = _c8(record.get("prompt_tokens"), record.get("cached_tokens"), record.get("imported_tokens"),
                           record.get("computed_tokens"))
    ttft = record.get("first_token_ms")
    phases, cursor = [], 0.0
    for name, key in (("queue", "queue_ms"), ("prefill", "prefill_ms"), ("decode", "decode_ms")):
        value = record.get(key)
        if value is None:
            break
        phases.append({"phase": name, "start_ms": cursor, "end_ms": cursor + value})
        cursor += value
    why = {}
    if ttft is None:
        why["ttft_ms"] = ("not reported (engine build without first_token_ms, or not armed yet); queue + "
                                 "prefill shown as an approximation") if record.get("engine_ttft_ms") is not None \
            else "not reported by the engine"
    if c8_why:
        why["computed_tokens"] = c8_why
    return {"observer": "engine", "id": record.get("id"), "attempt": attempt, "replica": record.get("replica"),
            "queue_ms": record.get("queue_ms"), "prefill_ms": record.get("prefill_ms"), "decode_ms": record.get("decode_ms"),
            "ttft_ms": ttft, "ttft_approx_ms": None if ttft is not None else record.get("engine_ttft_ms"),
            "prompt_tokens": record.get("prompt_tokens"), "cached_tokens": record.get("cached_tokens"),
            "imported_tokens": record.get("imported_tokens"), "computed_tokens": computed,
            "output_tokens": record.get("output_tokens"), "decode_tok_s": record.get("decode_tok_s"),
            "draft_accepted": record.get("draft_accepted"), "draft_proposed": record.get("draft_proposed"),
            "status": record.get("status"), "stop_reason": record.get("stop_reason"), "phases": phases,
            "clock": "engine's own clock: durations, not aligned to router time", "why": why}


def _reported(block):
    """The engine fields the router parsed from the answer, under output names (``ttft_ms`` = first_token_ms)."""
    if not block:
        return None
    return {("ttft_ms" if key == "first_token_ms" else key): value for key, value in block.items()}


def _engine_records(store, profile, rids):
    """{rid: {attempt: [records]}} of the engines and the prefill service for the given router ids."""
    found = {}
    rids = list(rids)
    for chunk in range(0, len(rids), 400):
        part = rids[chunk:chunk + 400]
        if not part:
            continue
        for replica, rid, attempt, text in store.execute(
                f"SELECT replica, rid, attempt, record FROM observe_engine_requests WHERE profile=? AND rid IN "
                f"({','.join('?' * len(part))})", (profile, *part)):
            record = json.loads(text)
            record["replica"] = replica
            found.setdefault(rid, {}).setdefault(attempt, []).append(record)
    return found


def _match(candidates, target, service):
    """The one record of an attempt, on the attempt's target when the router named it; several = ambiguous."""
    rows = [r for r in candidates or [] if (r["replica"] == "prefill-service") == service]
    if target and not service:
        rows = [r for r in rows if r["replica"] == target]
    if len(rows) > 1:
        return None, "ambiguous: several records carry this request id and attempt"
    return (rows[0], None) if rows else (None, None)


def _joined(profile, record, found, now, state=None):
    rid = record["id"]
    attempts = record.get("attempts") or []
    by_attempt = found.get(rid, {})
    decode = [a for a in attempts if a.get("kind") == "decode" and a.get("n") is not None]
    served = [a for a in decode if a.get("outcome") == "served"]
    chosen = (served or decode or [None])[-1]
    engine_block, why = None, {}
    if chosen is not None:
        match, ambiguity = _match(by_attempt.get(chosen["n"]), chosen.get("target"), False)
        engine_block = _engine_view(match, chosen["n"])
        if ambiguity:
            why["engine"] = ambiguity
    service_block = None
    # The hand-off that was used: the last prefill attempt that succeeded (failed hand-offs stay attempts and are
    # not counted), else the last one.
    prefill = [a for a in attempts if a.get("kind") == "prefill" and a.get("n") is not None]
    prefill = [a for a in prefill if a.get("outcome") == "ok"] or prefill
    if prefill:
        match, ambiguity = _match(by_attempt.get(prefill[-1]["n"]), prefill[-1].get("target"), True)
        if match:
            service_block = {"observer": "prefill service", "attempt": prefill[-1]["n"],
                             **{k: match.get(k) for k in ("computed_tokens", "prompt_tokens", "queue_ms", "prefill_ms",
                                                         "handoff_ms", "duration_ms")},
                             "basis": "the service's own record; its computed tokens count once, on the service (C8)"}
        else:
            why["service"] = ambiguity or f"no prefill service record for this hand-off attempt ({NEEDS_SERVICE})"
    stamps = record.get("stamps") or {}
    usage = record.get("usage") or {}
    state = state or record.get("state")
    row = {"profile": profile, "id": rid, "client_id": record.get("client_id"), "state": state,
           "outcome": record.get("outcome"), "status": record.get("status"), "started_at": record.get("at"),
           "path": record.get("path"), "model": record.get("model"), "route": record.get("route"),
           "policy": record.get("policy"), "caller": record.get("caller"), "replica": record.get("replica"),
           "prefill": record.get("prefill"), "stream": record.get("stream"), "data_events": record.get("data_events"),
           "bytes": record.get("bytes"), "reason": record.get("why"), "route_trace": record.get("route_trace") or [],
           "dropped": {k: record.get(k + "_dropped") for k in ("attempts", "decisions", "route_trace")
                       if record.get(k + "_dropped")},
           # "tokenized" is published as "prompt_encoded": the portal's redaction hides any other key naming tokens.
           "router": {"observer": "router", "stamps": {("prompt_encoded" if k == "tokenized" else k): v for k, v in stamps.items()},
                      "phases": _phases(stamps), "ttft_ms": _ttft_ms(record),
                      "queue_ms": record.get("queue_ms"), "total_ms": stamps.get("last_byte"), "usage": usage or None,
                      "usage_why": record.get("usage_why"), "engine_reported": _reported(record.get("engine")),
                      "route_uncached_tokens": record.get("route_uncached_tokens")},
           "attempts": attempts, "decisions": record.get("decisions") or [], "engine": engine_block,
           "service": service_block, "why": {**(record.get("parse_why") or {}), **why}}
    if state == "in_flight" and row["started_at"] is not None:
        row["elapsed_ms"] = max(0.0, (now - row["started_at"]) * 1000.0)
    if state == "unknown":
        row["why"]["state"] = "final state not observed (router restarted, or the record left its ring unfinished)"
    if engine_block is None and "engine" not in row["why"]:
        row["why"]["engine"] = ("no decode attempt" if chosen is None else
                                "no engine record yet" if state == "in_flight" else
                                f"no engine record with this request id ({NEEDS_ENGINE_IDS})")
    if row["router"]["ttft_ms"] is None:
        row["why"]["ttft_ms"] = ("non-stream answer: no data events, so no router TTFT" if record.get("stream") is False
                                 else "no data event delivered")
    return row


def _rows(store, sql, args):
    """(profile, record, stored state) rows."""
    return [(profile, json.loads(text), state) for profile, text, state in store.execute(sql, args)]


def requests_document(api, query, store=None):
    store = store if store is not None else telemetry.existing_store(api)
    query = _query(query, ("profile", "model", "route", "outcome", "caller", "state", "limit", "before"))
    limit = _int(query, "limit", 50, 1, 200)
    before = _number(query, "before", None)
    if store is None:
        return api.document("observe-requests", requests=None, unjoined=None, filters=None, sources=None, gaps=None,
                            why={"requests": NO_STORE})
    if not _readable(store):
        return api.document("observe-requests", requests=[], unjoined=[], filters={}, sources={}, gaps=None,
                            why={"requests": NOT_COLLECTED, "unjoined": NOT_COLLECTED})
    clauses, args = [], []
    for key in ("profile", "model", "route", "outcome", "caller", "state"):
        if key in query:
            clauses.append(f"{key}=?")
            args.append(query[key])
    if before is not None:
        clauses.append("started_at < ?")
        args.append(before)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _rows(store, f"SELECT profile, record, state FROM observe_requests{where} ORDER BY started_at DESC, seq DESC "
                        "LIMIT ?", (*args, limit))
    now = api.clock()
    result = []
    for profile in sorted({p for p, _, _ in rows}):
        found = _engine_records(store, profile, {r["id"] for p, r, _ in rows if p == profile})
        result += [_joined(profile, record, found, now, state) for p, record, state in rows if p == profile]
    result.sort(key=lambda row: row["started_at"] or 0, reverse=True)
    scope, scope_args = ("WHERE profile=?", (query["profile"],)) if "profile" in query else ("", ())
    filters = {key: [row[0] for row in store.execute(f"SELECT DISTINCT {key} FROM observe_requests {scope} ORDER BY "
                                                      f"{key} LIMIT 64", scope_args) if row[0] is not None]
               for key in ("profile", "model", "route", "outcome", "caller", "state")}
    unjoined = []
    for profile, replica, text in store.execute(
            "SELECT profile, replica, record FROM observe_engine_requests WHERE rid IS NULL AND replica != 'prefill-service' "
            + ("AND profile=? " if "profile" in query else "") + "ORDER BY time DESC LIMIT 20", scope_args):
        item = json.loads(text)
        unjoined.append({**_engine_view(dict(item, replica=replica), None), "profile": profile, "replica": replica,
                         "time": item.get("time"), "route": item.get("route")})
    sources = _sources(store, query.get("profile"), now, stale_after(api))
    why = {}
    if not result:
        reasons = _reasons(sources, "requests")
        why["requests"] = "; ".join(reasons) if reasons else (
            "no requests recorded" if sources else "no running profile with a router observed yet")
    if not unjoined:
        why["unjoined"] = "no engine-only records"
    return api.document("observe-requests", requests=result, unjoined=unjoined, filters=filters, sources=sources,
                        gaps={key: value for key, _, value in store.items("observe/gaps")} or None, why=why)


def request_document(api, rest, query, store=None):
    if not REQUEST_ID.fullmatch(rest or ""):
        raise ValueError("request id must be 16 hex characters")
    if query:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(query)))
    store = store if store is not None else telemetry.existing_store(api)
    if store is None:
        return api.document("observe-request", request=None, why={"request": NO_STORE})
    if not _readable(store):
        raise LookupError("unknown request " + rest)
    rows = _rows(store, "SELECT profile, record, state FROM observe_requests WHERE id=? ORDER BY updated_at DESC LIMIT 1",
                 (rest,))
    if not rows:
        raise LookupError("unknown request " + rest)
    profile, record, state = rows[0]
    found = _engine_records(store, profile, {rest})
    joined = _joined(profile, record, found, api.clock(), state)
    joined["observations"] = [dict(_engine_view(item, attempt),
                                   observer="prefill service" if item["replica"] == "prefill-service" else "engine")
                              for attempt, items in sorted(found.get(rest, {}).items(), key=lambda kv: kv[0] or 0)
                              for item in items]
    return api.document("observe-request", request=joined)


LATENCY_BINS = {"ms": [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 60000, math.inf],
                "tok/s": [1, 2, 5, 10, 15, 20, 30, 50, 75, 100, 150, 250, math.inf]}
LATENCY_METRICS = {"ttft_ms": "ms", "queue_ms": "ms", "total_ms": "ms", "decode_tok_s": "tok/s"}
DEFINITIONS = {
    "router": {"ttft_ms": "arrival to the first delivered data event (streams only)",
               "queue_ms": "slot wait at the router", "total_ms": "arrival to the last delivered byte",
               "decode_tok_s": "(completion tokens - 1) / (last byte - first data event); streams with >= 2 tokens"},
    "engine": {"ttft_ms": "submit to the first published token (first_token_ms; requests without it are left out)",
               "queue_ms": "engine queue (submit to take)", "total_ms": "queue + prefill + decode",
               "decode_tok_s": "output tokens / decode time, as the engine reports it"},
}


def _histogram(values, unit):
    bins = LATENCY_BINS[unit]
    counts = [0] * len(bins)
    for value in values:
        counts[next(i for i, bound in enumerate(bins) if value <= bound)] += 1
    return [{"le": None if bound == math.inf else bound, "count": count} for bound, count in zip(bins, counts)]


def _router_values(record):
    stamps = record.get("stamps") or {}
    tokens = (record.get("usage") or {}).get("completion_tokens")
    first, last = stamps.get("first_data_event"), stamps.get("last_byte")
    decode = ((tokens - 1) / ((last - first) / 1000.0) if record.get("stream") is True and tokens and tokens >= 2
              and first is not None and last is not None and last > first else None)
    return {"ttft_ms": _ttft_ms(record), "queue_ms": record.get("queue_ms"), "total_ms": stamps.get("last_byte"),
            "decode_tok_s": decode}


def _engine_values(view):
    if view is None:
        return {}
    total = sum(view[k] for k in ("queue_ms", "prefill_ms", "decode_ms")) if all(
        view.get(k) is not None for k in ("queue_ms", "prefill_ms", "decode_ms")) else None
    return {"ttft_ms": view.get("ttft_ms"), "queue_ms": view.get("queue_ms"), "total_ms": total,
            "decode_tok_s": view.get("decode_tok_s") if view.get("output_tokens") else None}


def latency_document(api, query, store=None):
    store = store if store is not None else telemetry.existing_store(api)
    query = _query(query, ("range", "by", "profile"))
    span = _int(query, "range", 3600, 60, 31 * 86400)
    by = query.get("by", "model")
    if by not in ("model", "route", "caller"):
        raise ValueError("by must be model, route or caller")
    common = dict(by=by, range=span, definitions=DEFINITIONS, labels={"delivery_gap": GAP_LABEL})
    if store is None:
        return api.document("observe-latency", **common, groups=None, histograms=None,
                            why={"groups": NO_STORE, "histograms": NO_STORE})
    if not _readable(store):
        return api.document("observe-latency", **common, groups=[], histograms=[],
                            why={"groups": NOT_COLLECTED, "histograms": NOT_COLLECTED})
    now = api.clock()
    args, scope = [now - span], ""
    if "profile" in query:
        scope, args = " AND profile=?", [now - span, query["profile"]]
    rows = _rows(store, f"SELECT profile, record, state FROM observe_requests WHERE state='done' AND ended_at >= ?{scope} "
                        "ORDER BY ended_at DESC LIMIT ?", (*args, MAX_ROWS))
    found = {profile: _engine_records(store, profile, {r["id"] for p, r, _ in rows if p == profile})
             for profile in {p for p, _, _ in rows}}
    groups = {}
    for profile, record, state in rows:
        if by == "caller":
            group = record.get("caller") or "anonymous"
        else:
            group = record.get(by) or ("direct" if by == "route" else "unknown")
        entry = groups.setdefault(group, {"router": {k: [] for k in LATENCY_METRICS},
                                          "engine": {k: [] for k in LATENCY_METRICS}, "outcomes": {}})
        outcome = record.get("outcome") or "unknown"
        entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
        if outcome != "served":
            continue
        for key, value in _router_values(record).items():
            if value is not None:
                entry["router"][key].append(value)
        joined = _joined(profile, record, found.get(profile, {}), now, state)
        for key, value in _engine_values(joined["engine"]).items():
            if value is not None:
                entry["engine"][key].append(value)
    result = []
    for group, entry in sorted(groups.items(), key=lambda item: -sum(item[1]["outcomes"].values())):
        metrics = {}
        for key, unit in LATENCY_METRICS.items():
            metrics[key] = {"unit": unit}
            for observer in ("router", "engine"):
                values = entry[observer][key]
                metrics[key][observer] = {**_quantiles(values), "histogram": _histogram(values, unit) if values else None,
                                          "basis": "measured per request", "definition": DEFINITIONS[observer][key]}
        result.append({"group": group, "requests": sum(entry["outcomes"].values()), "outcomes": entry["outcomes"],
                       "metrics": metrics})
    histograms = []
    for profile, _, value in store.items("observe/router-metrics"):
        if "profile" not in query or profile == query["profile"]:
            histograms += _estimated(value, "router", profile, None, value.get("instance"))
    for _, _, value in store.items("observe/engine"):
        if "profile" not in query or value.get("profile") == query["profile"]:
            histograms += _estimated({"ts": value.get("ts"), "families": (value.get("extra") or {}).get("histograms") or {}},
                                     "engine", value.get("profile"), value.get("replica"), value.get("instance"))
    sources = _sources(store, query.get("profile"))
    why = {}
    if not result:
        reasons = _reasons(sources, "requests")
        why["groups"] = "; ".join(reasons) if reasons else "no completed requests recorded in this range"
    if not histograms:
        reasons = _reasons(sources, "metrics")
        why["histograms"] = "; ".join(reasons) if reasons else NEEDS_HIST
    return api.document("observe-latency", **common, groups=result, histograms=histograms,
                        retention={"requests": MAX_ROWS, "days": settings(store)["retention_days"]}, why=why)


def _estimated(snapshot, observer, profile, replica, instance):
    out = []
    for family, lines in (snapshot.get("families") or {}).items():
        for line in lines:
            buckets = [[math.inf if b is None else b, c] for b, c in line.get("buckets") or []]
            out.append({"observer": observer, "profile": profile, "replica": replica, "instance": instance,
                        "family": family, "labels": line.get("labels"), "count": line.get("count"), "sum": line.get("sum"),
                        "p50": router.quantile(buckets, .5), "p95": router.quantile(buckets, .95),
                        "p99": router.quantile(buckets, .99),
                        "buckets": [[None if b == math.inf else b, c] for b, c in buckets],
                        "basis": "estimated from histogram buckets · cumulative since the component was armed "
                                 "(requests before arming are not in it)",
                        "label": GAP_LABEL if "gap" in family else None, "as_of": snapshot.get("ts")})
    return out


def _snapshots(store, profile=None):
    rows = [value for _, _, value in store.items("observe/engine")]
    return sorted([row for row in rows if profile in (None, row.get("profile"))],
                  key=lambda row: (row.get("profile") or "", row.get("replica") or ""))


def _cache_view(observed):
    cache = (observed or {}).get("prefix_cache")
    if cache is None:
        return None
    hits = cache.get("hits")
    if hits is None and cache.get("host_hits") is not None and cache.get("disk_hits") is not None:
        hits = cache["host_hits"] + cache["disk_hits"]
    return {"lookups": cache.get("lookups"), "hits": hits, "hit_tokens": cache.get("hit_tokens"),
            "disk_hits": cache.get("disk_hits", cache.get("disk.hits")), "bytes": cache.get("bytes", cache.get("host_bytes")),
            "raw": cache}


def _first(*values):
    return next((v for v in values if v is not None), None)


def _accounting(metrics, totals, observed, extra):
    """Prompt accounting of one engine instance, each split from one coherent scope:
    - lifetime (metrics answer, else status totals): prompt, cached and generated tokens; imported tokens are only
      counted since arming, so no lifetime computed split is made from them;
    - since arming (the engine's ``observed`` block, one copy taken together): prompt = computed + cached + imported
      (C8), checked.
    Both count engine work: every request this engine ran, so a request retried on another engine counts on each
    (requests are deduplicated by id in the request timeline and the ledger, not here)."""
    since = observed.get("observed") or {}
    life_totals = observed.get("totals") or {}
    life_computed, life_why = (None, "imported tokens are not reported for the instance lifetime")
    if life_totals.get("imported_tokens") is not None:
        # The status totals carry the whole split (one answer, one scope): prompt = computed + cached + imported.
        life_computed, life_why = _c8(totals.get("prompt_tokens"), totals.get("cached_tokens"),
                                      life_totals["imported_tokens"], life_totals.get("computed_tokens"))
    computed, why = _c8(since.get("prompt_tokens"), since.get("cached_tokens"), since.get("imported_tokens"),
                        since.get("computed_tokens"))
    if not since:
        why = "engine reports no C8 counts since arming (build without them, or not armed)"
    split = life_totals.get("imported_tokens") is not None
    return {"lifetime": {"prompt_tokens": totals.get("prompt_tokens") if split else _first(metrics.get("prompt_tokens"), totals.get("prompt_tokens")),
                         "cached_tokens": totals.get("cached_tokens") if split else _first(metrics.get("cached_tokens"), totals.get("cached_tokens")),
                         "imported_tokens": life_totals.get("imported_tokens"), "computed_tokens": life_computed,
                         "generated_tokens": _first(metrics.get("generated_tokens"), totals.get("generated_tokens")),
                         "basis": "lifetime of this engine instance" + (" (one status answer)" if split else ""),
                         "why": {"computed_tokens": life_why} if life_why else {}},
            "since_armed": {"requests": since.get("requests"), "prompt_tokens": since.get("prompt_tokens"),
                            "computed_tokens": computed, "cached_tokens": since.get("cached_tokens"),
                            "imported_tokens": since.get("imported_tokens"),
                            "basis": "requests finished since the engine was armed (C8); cache savings = cached only"},
            "scope": "engine work: every request this engine ran (a retried request counts on each engine)",
            "why": {"computed_tokens": why} if why else {}}


def engines_document(api, query, store=None):
    store = store if store is not None else telemetry.existing_store(api)
    query = _query(query, ("profile",))
    if store is None:
        return api.document("observe-engines", engines=None, why={"engines": NO_STORE})
    now = api.clock()
    rows = []
    for snap in _snapshots(store, query.get("profile")):
        status = snap.get("status") or {}
        metrics = snap.get("metrics") or {}
        observed = snap.get("observed") or {}
        extra = snap.get("extra") or {}
        totals = status.get("totals") or {}
        why = dict(snap.get("why") or {})
        accounting = _accounting(metrics, totals, observed, extra)
        observed_at = snap.get("observed_at")
        age = None if observed_at is None else now - observed_at
        stale = age is None or age > stale_after(api, "observe-engines")
        queue = status.get("queue") or {}
        busy = (_first(metrics.get("running"), queue.get("running")) or 0) + (_first(metrics.get("waiting"), queue.get("queued")) or 0)
        latest = store.latest("usb4_exchange_seconds_per_step", {"profile": snap.get("profile"),
                                                                 "replica": snap.get("replica")}, max_age=60)
        exchange = max(latest, key=lambda item: item["ts"])["value"] if latest else None
        last = snap.get("last_served")
        row = {"profile": snap.get("profile"), "replica": snap.get("replica"), "machine": snap.get("machine"),
               "family": snap.get("family"), "via": snap.get("via"), "as_of": observed_at, "age_s": age,
               "stale": stale, "instance": snap.get("instance"), "instance_basis": snap.get("instance_basis"),
               "started_at": snap.get("started_at"),
               "state": snap.get("state") if not stale or snap.get("state") in ("unreachable", "unavailable") else "stale",
               "model": status.get("model") or snap.get("model"), "engine": status.get("engine"),
               "armed": observed.get("armed"), "armed_since": observed.get("armed_since"),
               "context_tokens": status.get("context_tokens"),
               "uptime_seconds": status.get("uptime_seconds"), "slots": status.get("slots"),
               "queue": status.get("queue") or ({"queued": metrics.get("waiting"), "running": metrics.get("running")}
                                                if metrics else None),
               "memory": status.get("memory"), "batch": status.get("batch"), "group": status.get("group"),
               "prefix_cache": _cache_view(observed), "prompt_accounting": accounting,
               "speculative": observed.get("speculative"), "usb4_exchange_seconds_per_step": exchange,
               "last_served": last, "last_served_basis": snap.get("last_served_basis"),
               "idle_s": now - last if last is not None and not busy else None,
               "counter_resets": snap.get("counter_resets") or [], "last_ok": snap.get("last_ok") or {},
               "series": {name: f"history/{name}?" + urlencode({"range": 3600, "l.profile": snap.get("profile"),
                                                                 "l.replica": snap.get("replica")})
                          for name in ("engine_generated_tokens", "engine_queue_waiting", "engine_slots_busy",
                                       "usb4_exchange_seconds_per_step")},
               "why": {**why, **accounting["why"]}}
        for key in ("slots", "memory", "batch", "group", "context_tokens"):
            if row[key] is None:
                row["why"].setdefault(key, "not reported by this engine" if snap.get("status") else
                                      why.get("status", "engine status unavailable"))
        if row["prefix_cache"] is None:
            row["why"]["prefix_cache"] = "engine reports no prefix cache (disabled or not exposed)"
        if row["speculative"] is None:
            row["why"]["speculative"] = "engine reports no speculative decoding counters"
        if row["usb4_exchange_seconds_per_step"] is None:
            row["why"]["usb4_exchange_seconds_per_step"] = ("requires two status samples of a distributed engine"
                                                            if row["group"] else "not a distributed engine or not reported")
        if last is None:
            row["why"]["last_served"] = "no completed request seen"
        rows.append(row)
    return api.document("observe-engines", engines=rows,
                        why={} if rows else {"engines": "no engine observed yet (" + NOT_COLLECTED + ")"})


def _scope(spec):
    since = (spec.get("since") or "").lower()
    if since.startswith("armed"):
        return "cumulative since the engine was armed"
    if since.startswith("process"):
        return "cumulative since the engine process started"
    return "cumulative (scope not reported)"


def speculative_document(api, query, store=None):
    store = store if store is not None else telemetry.existing_store(api)
    query = _query(query, ("profile",))
    if store is None:
        return api.document("observe-speculative", engines=None, why={"engines": NO_STORE})
    rows = []
    for snap in _snapshots(store, query.get("profile")):
        spec = (snap.get("observed") or {}).get("speculative")
        batch = (snap.get("status") or {}).get("batch") or {}
        history = urlencode({"range": 3600, "l.profile": snap.get("profile"), "l.replica": snap.get("replica")})
        why = {}
        row = {"profile": snap.get("profile"), "replica": snap.get("replica"), "model": snap.get("model"),
               "instance": snap.get("instance"), "as_of": snap.get("observed_at"), "method": None, "scope": None,
               "accepted": None, "proposed": None, "cycles": None, "acceptance": None, "accepted_per_cycle": None,
               "depth": None, "window_rows": None, "window_label": None, "draft_seconds": None, "saved_seconds": None,
               "basis": None, "acceptance_series": "history/engine_draft_acceptance?" + history,
               "per_cycle_series": "history/engine_draft_accepted_per_cycle?" + history, "why": why}
        if spec:
            row.update(method=spec.get("method"), scope=spec.get("scope"), accepted=spec.get("accepted"),
                       proposed=spec.get("proposed"), cycles=spec.get("cycles"), draft_seconds=spec.get("draft_seconds"),
                       window_rows=spec.get("window_rows"), window_label=spec.get("window_label"), basis=_scope(spec))
            if spec.get("proposed") and spec.get("accepted") is not None:
                row["acceptance"] = spec["accepted"] / spec["proposed"]
            if spec.get("cycles") and spec.get("accepted") is not None:
                row["accepted_per_cycle"] = spec["accepted"] / spec["cycles"]
            depth = spec.get("depth") or []
            if depth:
                cycles = spec.get("cycles")
                row["depth"] = [{"depth": d["depth"], "reached": d.get("reached"), "hit": d.get("hit"),
                                 "reach_rate": d["reached"] / cycles if cycles and d.get("reached") is not None else None,
                                 "hit_rate": d["hit"] / d["reached"] if d.get("reached") and d.get("hit") is not None else None}
                                for d in depth]
            else:
                why["depth"] = "engine does not report per-depth reach/hit"
            if row["window_rows"] is None:
                why["window_rows"] = "engine does not report rows per window"
            step_ms = batch.get("mean_step_ms")
            if spec.get("draft_seconds") is not None and step_ms and spec.get("accepted") is not None:
                row["saved_seconds"] = spec["accepted"] * step_ms / 1000.0
                row["saved_basis"] = ("estimated: accepted draft tokens x mean step time (one target step per accepted "
                                      "token); the two counters may cover different spans")
            else:
                why["saved_seconds"] = ("engine does not report draft time" if spec.get("draft_seconds") is None
                                        else "needs the engine's mean step time")
            if spec.get("draft_seconds") is None:
                why["draft_seconds"] = "engine does not report draft time"
        else:
            records = [] if not _readable(store) else [json.loads(text) for (text,) in store.execute(
                "SELECT record FROM observe_engine_requests WHERE profile=? AND replica=? AND instance=? ORDER BY time DESC "
                "LIMIT 200", (snap.get("profile"), snap.get("replica"), snap.get("instance") or ""))]
            known = [r for r in records if r.get("status") == "done" and r.get("draft_accepted") is not None
                     and r.get("draft_proposed") is not None]
            proposed = sum(r["draft_proposed"] for r in known)
            if known and proposed:
                accepted = sum(r["draft_accepted"] for r in known)
                cycles = [r.get("draft_cycles") for r in known]
                row.update(method="mtp", accepted=accepted, proposed=proposed, acceptance=accepted / proposed,
                           cycles=sum(cycles) if all(cycles) else None,
                           accepted_per_cycle=accepted / sum(cycles) if all(cycles) else None,
                           basis=f"per-request draft counts of the last {len(known)} completed requests seen")
                why["depth"] = "engine does not report per-depth reach/hit"
                why["draft_seconds"] = why["saved_seconds"] = "engine does not report draft time"
            else:
                why["acceptance"] = "engine reports no speculative decoding counters"
        rows.append(row)
    return api.document("observe-speculative", engines=rows, why={} if rows else {"engines": "no engine observed yet"})


def sessions_document(api, query, store=None):
    store = store if store is not None else telemetry.existing_store(api)
    query = _query(query, ("profile",))
    if store is None:
        return api.document("observe-sessions", sessions=None, label=AFFINITY_LABEL, why={"sessions": NO_STORE})
    if not _readable(store):
        return api.document("observe-sessions", sessions=[], label=AFFINITY_LABEL, why={"sessions": NOT_COLLECTED})
    now = api.clock()
    idle = settings(store)["affinity_idle_s"]
    result, covered, coverage = [], set(), {}
    for profile, _, value in store.items("observe/sessions"):
        if "profile" in query and profile != query["profile"]:
            continue
        covered.add(profile)
        coverage[profile] = {"source": "router sessions", "router_idle_s": value.get("idle_s"),
                             "router_count": value.get("count"), "returned": value.get("returned"),
                             "as_of": value.get("ts"), "why": (value.get("why") or {}).get("coverage")}
        if value.get("idle_s") is not None and idle > value["idle_s"]:
            coverage[profile]["why"] = (f"the router forgets a conversation after {value['idle_s']:.0f} s idle; a longer "
                                        "expiry here cannot show older ones")
        for session in value.get("sessions") or []:
            if session.get("last_seen") is not None and now - session["last_seen"] > idle:
                continue
            turns = session.get("recent") or []
            result.append({"profile": profile, "key": (session.get("key") or "")[:12], "kind": session.get("kind"),
                           "endpoint": session.get("path"), "turns": session.get("turns"),
                           "replica": session.get("replica"), "replicas": session.get("replicas"),
                           "first_seen": session.get("first_seen"), "last_seen": session.get("last_seen"),
                           "reused_per_turn": [t.get("cached_tokens") for t in turns] if turns else None,
                           "uncached_per_turn": [t.get("uncached_tokens") for t in turns] if turns else None,
                           "expires_at": session["last_seen"] + idle if session.get("last_seen") is not None else None,
                           "source": "router sessions"})
    # Profiles without a sessions endpoint: grouped from the request records retained here that carry a session key.
    args, scope = [now - idle], ""
    if "profile" in query:
        scope, args = " AND profile=?", [now - idle, query["profile"]]
    grouped = {}
    for profile, session, started, text in store.execute(
            "SELECT profile, session, started_at, record FROM observe_requests WHERE session IS NOT NULL AND started_at >= ?"
            f"{scope} ORDER BY started_at", args):
        if profile in covered:
            continue
        coverage.setdefault(profile, {"source": "retained request records", "why": "grouped from the request records "
                                      "this portal kept; the router's own session view is not available"})
        record = json.loads(text)
        entry = grouped.setdefault((profile, session), {"profile": profile, "key": session[:12], "kind": None,
                                                        "endpoint": record.get("path"), "turns": 0, "replica": None,
                                                        "replicas": {}, "first_seen": started, "last_seen": None,
                                                        "reused_per_turn": [], "uncached_per_turn": [],
                                                        "source": "retained request records"})
        entry["turns"] += 1
        if record.get("replica"):
            entry["replica"] = record["replica"]
            entry["replicas"][record["replica"]] = entry["replicas"].get(record["replica"], 0) + 1
        entry["last_seen"] = started
        usage = record.get("usage") or {}
        entry["reused_per_turn"] = (entry["reused_per_turn"] + [usage.get("cached_tokens")])[-16:]
        entry["uncached_per_turn"] = (entry["uncached_per_turn"] + [usage.get("uncached_tokens")])[-16:]
    for entry in grouped.values():
        entry["expires_at"] = (entry["last_seen"] or now) + idle
        result.append(entry)
    result.sort(key=lambda row: row.get("last_seen") or 0, reverse=True)
    why = {}
    if not result:
        reasons = _reasons(_sources(store, query.get("profile")), "sessions")
        why["sessions"] = "; ".join(reasons) if reasons else "no conversations seen within the expiry"
    return api.document("observe-sessions", sessions=result[:500], label=AFFINITY_LABEL, idle_expiry_s=idle,
                        coverage=coverage, why=why)


def settings_document(api, query):
    if query:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(query)))
    store = telemetry.existing_store(api)
    return api.document("observe-settings", settings=settings(store), defaults=DEFAULTS,
                        bounds={k: list(v) for k, v in BOUNDS.items()},
                        why={} if store is not None else {"settings": NO_STORE + "; defaults shown"})


def save_settings(api, arguments, store=None):
    if not isinstance(arguments, dict) or not arguments or set(arguments) - set(DEFAULTS):
        raise ValueError("settings take affinity_idle_s and/or retention_days")
    for key, value in arguments.items():
        low, high = BOUNDS[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in {low}..{high}")
    store = store if store is not None else telemetry.open_store(api)
    value = {**settings(store), **arguments}
    store.put("observe/settings", "settings", value)
    return {"settings": value}


def register(reg):
    reg.get("observe/requests", requests_document)
    reg.get_prefix("observe/requests/", request_document)
    reg.get("observe/latency", latency_document)
    reg.get("observe/engines", engines_document)
    reg.get("observe/speculative", speculative_document)
    reg.get("observe/sessions", sessions_document)
    reg.get("observe/settings", settings_document)
    reg.post("observe/settings/save", save_settings, audited=True)
    reg.collector("observe-router", sample_router, interval=INTERVAL)
    reg.collector("observe-engines", sample_engines, interval=INTERVAL)
