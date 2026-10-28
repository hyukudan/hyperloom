"""A1: collector-only, read-only session-cache queries; GET reads retained documents."""
import json
import re
import time

from hyperloom import nodepoll, pollbudget, telemetry
from hyperloom.adapters import sessions
from . import observe
from .launch import config_key

READ_ONLY_OPS = observe.SESSIONCACHE_READ_ONLY_OPS
FAMILIES = {"qwen-replicas": "qwen", "ds41-pair": "deepseek", "minimax-pair": "minimax"}
PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UNSUPPORTED = "engine build has no session cache"
NAMESPACE = "sessioncache/profiles"
LIMIT = 256
SOURCE_WHY_BYTES = 2048
INTERVAL = 30
UNSUPPORTED_BACKOFF = 600
PAGE_WHY = "collected only while a portal page is open"


def namespace(api):
    return NAMESPACE + "/" + config_key(api)


def _http(url, headers=None, deadline=None, *, method="GET", json_body=None):
    if method == "GET" and json_body is None:
        return observe._http(url, headers, deadline=deadline)
    # The sole JSON POST entry point: validate endpoint, exact body and read-only op
    # even when called directly, before the bounded transport can open a socket.
    from urllib.parse import urlsplit
    field = {"/admin/session-cache": "op", "/v1/session-cache": "operation"}.get(urlsplit(url).path)
    if (method != "POST" or not field or not isinstance(json_body, dict)
            or set(json_body) != {field} or not isinstance(json_body[field], str)
            or json_body[field] not in READ_ONLY_OPS):
        raise ValueError("only list/stats session cache queries are allowed")
    return observe._bounded_http(url, headers, deadline=deadline, method="POST", json_body=json_body)


def _query(base, kind, op, headers, deadline):
    # Validate before constructing a request: no caller can send a state-changing op.
    if op not in READ_ONLY_OPS:
        raise ValueError("only list/stats session cache queries are allowed")
    if kind == "qwen":
        path = "/v1/sessions" if op == "list" else "/v1/cache/stats"
        answer = _http(base + path, headers, deadline)
    elif kind in ("deepseek", "minimax"):
        path, field = (("/admin/session-cache", "op") if kind == "deepseek" else
                       ("/v1/session-cache", "operation"))
        answer = _http(base + path, headers, deadline, method="POST", json_body={field: op})
    else:
        raise ValueError("unsupported engine")
    _capability(answer)
    if not isinstance(answer, dict) or "error" in answer or answer.get("ok") is False:
        raise ValueError("invalid session cache response")
    return answer


def _capability(answer):
    # Inspect only fixed capability phrases; never retain arbitrary engine error text.
    if not isinstance(answer, dict):
        return
    error = answer.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    if isinstance(error, str) and any(phrase in error.lower() for phrase in
            ("unknown route", "unknown endpoint", "not found", "session cache is disabled",
             "session cache disabled", "session-cache disabled", "unknown session operation")):
        raise observe.Unavailable(UNSUPPORTED)


def _bounded_why(why):
    # Adapter diagnostics have a fixed schema; keep a second bound at retention.
    if len(why) > 8 or len(json.dumps(why).encode("utf-8")) > SOURCE_WHY_BYTES:
        return {"collection": "source diagnostics exceeded size limit; details omitted"}
    return why


def _source(api, cluster, launch, replica, kind, deadline, now, previous=None):
    row = {"replica": replica["id"], "supported": None, "why": {}, "stats": None, "sessions": None}
    machine = cluster.machines.get(replica.get("node"))
    address = replica.get("address")
    if machine is None or (address != machine.http_address and not
                           (replica.get("loopback") and observe._loopback(address))):
        row["why"] = {"collection": "replica address does not match configured machine"}
        return row
    if replica.get("loopback"):
        row["why"] = {"collection": "engine is bound to node loopback; no node calls in this collector"}
        return row
    base = f"http://{observe._host(replica['address'])}:{replica['port']}"
    why = pollbudget.for_api(api).engine(replica["node"],
        f"{replica['address']}:{replica['port']}", "sessioncache", now=now)
    if why is not None:
        row = dict(previous or row)
        row["why"] = {**row.get("why", {}), "stale": why}
        return row
    try:
        headers = observe._bearer(api.orchestrate._secret_name(launch["profile"], replica["node"]))
        if kind == "minimax":
            health = _http(base + "/health", headers, deadline)
            if not isinstance(health, dict) or health.get("mode") != "session-cache":
                raise observe.Unavailable(UNSUPPORTED)
        listed = _query(base, kind, "list", headers, deadline)
        stats = _query(base, kind, "stats", headers, deadline)
        parsed = sessions.parse_list(kind, listed)
        credential = headers.get("Authorization", "").removeprefix("Bearer ")
        if credential and parsed["sessions"] is not None:
            safe = [r for r in parsed["sessions"] if credential not in r["id"]]
            if len(safe) != len(parsed["sessions"]):
                parsed["why"]["sessions"] = "credential-like session id dropped"
            parsed["sessions"] = safe
        row.update(supported=True, sessions=parsed["sessions"], stats=sessions.parse_stats(kind, stats),
                   why=_bounded_why(parsed["why"]))
    except observe.Unavailable:
        row.update(supported=False, why={"supported": UNSUPPORTED})
    except TimeoutError:
        row["why"] = {"collection": "session cache query timed out"}
    except OSError as error:
        try:
            _capability(json.loads(getattr(error, "response_body", b"{}")))
        except observe.Unavailable:
            row.update(supported=False, why={"supported": UNSUPPORTED})
            return row
        except (ValueError, TypeError, UnicodeError):
            pass
        row["why"] = {"collection": "session cache query failed (transport or HTTP error)"}
    except (ValueError, TypeError):
        # No exceptions/response bodies/URLs/keys enter the store or logs.
        row["why"] = {"collection": "session cache query failed (transport, size cap or invalid response)"}
    return row


def collect(api, now, store=None):
    store = store if store is not None else telemetry.open_store(api)
    scope = namespace(api)
    poll = nodepoll.of(api)
    if poll is None or not poll.page_open():
        for key, _, previous in list(store.items(scope)):
            previous["why"] = {**previous.get("why", {}), "stale": PAGE_WHY}
            store.put(scope, key, previous)
        return []
    cluster = api.cluster()
    launches, omitted = observe.launches(api)
    store.put("sessioncache/discovery", config_key(api), {"configuration": config_key(api),
                                               "omitted": omitted, "why": "launch discovery includes only this portal's configuration"})
    keep = {p["profile"] for p in launches if PROFILE_ID.fullmatch(p["profile"])}
    observe._reconcile(store, scope, keep)
    deadline = time.monotonic() + observe.RUN_BUDGET_S
    for launch in observe._rotated(api, "sessioncache", launches):
        profile, kind = launch["profile"], FAMILIES.get(launch.get("family"))
        if profile not in keep:
            continue
        previous = store.get(scope, profile) or {}
        sources = []
        for replica in observe._rotated(api, "sessioncache/" + profile, launch["replicas"]):
            if not kind:
                break
            if time.monotonic() >= deadline:
                sources.append({"replica": replica["id"], "supported": None, "stats": None, "sessions": None,
                                "why": {"collection": "collector time budget exhausted"}})
                continue
            endpoint = [kind, replica.get("node"), replica.get("address"), replica.get("port")]
            old = next((dict(s) for s in previous.get("sources", [])
                        if s["replica"] == replica["id"] and s.get("_endpoint") == endpoint), None)
            if old:
                old["sessions"] = [{k: v for k, v in r.items() if k != "replica"}
                                   for r in previous.get("sessions", []) if r["replica"] == replica["id"]]
            if (old and old.get("supported") is False
                    and 0 <= now - old.get("as_of", 0) < UNSUPPORTED_BACKOFF):
                sources.append(old)
                continue
            source = _source(api, cluster, launch, replica, kind, deadline, now, old)
            source["_endpoint"] = endpoint
            if "stale" not in source["why"]:
                source["as_of"] = now
                # Kept with the source: a retained (paused) observation reports the count it was read with.
                source["_parsed"] = len(source.get("sessions") or [])
            sources.append(source)
        supported = (True if any(s["supported"] is True for s in sources) else
                     False if not kind or sources and all(s["supported"] is False for s in sources) else None)
        why = {"scope": "launch discovery includes only this portal's configuration"}
        if supported is False:
            why["supported"] = UNSUPPORTED
        elif supported is None:
            why["collection"] = "no successful session cache query"
        if any("stale" in s["why"] for s in sources):
            why["stale"] = "some endpoint observations retained; see sources"
        rows = [{**r, "replica": s["replica"]} for s in sources for r in s.get("sessions") or []]
        rows.sort(key=lambda r: (r.get("last_used") is not None, r.get("last_used") or 0), reverse=True)
        stats = {}
        stats_why = {}
        for field in sessions.STATS:
            values = [(s.get("stats") or {}).get(field) for s in sources]
            stats[field] = sum(values) if values and all(v is not None for v in values) else None
            if stats[field] is None:
                stats_why[field] = "not reported by every replica; see sources"
        stats["why"] = stats_why
        parsed = sum(s.get("_parsed", len(s.get("sessions") or [])) for s in sources)
        coverage = {"scope": "queried endpoints only; TP2 peer RAM is not reported", "limit": LIMIT, "returned": min(len(rows), LIMIT), "parsed": parsed,
                    "omitted": max(0, parsed - LIMIT), "discovery_omitted": omitted,
                    "order": "newest first where last_used is reported; otherwise engine order",
                    "complete": bool(sources) and all(s["supported"] is True and s["sessions"] is not None
                                                     and not s["why"] for s in sources) and parsed <= LIMIT
                                and not any(omitted.values())}
        # Keep only capped sessions once, rather than duplicating unbounded source lists.
        for source in sources:
            source.pop("sessions", None)
        store.put(scope, profile, {"_configuration": config_key(api), "profile": profile, "engine": kind or "unknown", "supported": supported,
                  "why": why, "as_of": min((s.get("as_of", now) for s in sources), default=now), "stats": stats, "sessions": rows[:LIMIT], "coverage": coverage,
                  "sources": sources})
    return []


def document(api, query, store=None):
    if set(query) - {"profile"} or ("profile" in query and
            (not isinstance(query["profile"], str) or not PROFILE_ID.fullmatch(query["profile"]))):
        raise ValueError("sessioncache accepts only a profile id")
    store = store if store is not None else telemetry.existing_store(api)
    why = {"profiles": observe.NO_STORE} if store is None else {}
    coverage = {"ignored_profiles": 0, "why": "launch discovery includes only this portal's configuration"}
    discovery = store.get("sessioncache/discovery", config_key(api)) if store is not None else None
    if discovery and discovery.get("configuration") == config_key(api):
        coverage["discovery_omitted"] = discovery["omitted"]
        coverage["why"] = discovery.get("why", "launch discovery includes only this portal's configuration")
    try:
        declared = api.cluster().profiles
    except (OSError, ValueError):
        profiles = [] if store is not None else None
        why["profiles"] = "current configuration is unavailable or invalid"
    else:
        retained = list(store.items(namespace(api))) if store is not None else []
        profiles = [{k: v for k, v in value.items() if k != "_configuration"} for key, _, value in retained
                    if key in declared and value.get("_configuration") == config_key(api) and
                    ("profile" not in query or key == query["profile"])] if store is not None else None
        coverage["ignored_profiles"] = sum(key not in declared or value.get("_configuration") != config_key(api)
                                           for key, _, value in retained)
    for profile in profiles or []:
        profile["sources"] = [{**{k: v for k, v in source.items() if not k.startswith("_")},
                               "why": _bounded_why(source.get("why", {}))}
                              for source in profile.get("sources", [])]
    return api.document("sessioncache", profiles=profiles,
                        why=why, coverage=coverage,
                        stale_after_s=3 * INTERVAL)


def register(reg):
    reg.get("sessioncache", document)
    reg.collector("sessioncache", collect, interval=INTERVAL)
