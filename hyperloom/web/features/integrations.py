"""Integrations: live snapshot stream (F44), MQTT publishing (F45) and server-side CSV export (F46).

GET  integrations                        -> state of the exporter, the stream, MQTT and the export targets
GET  integrations/export/<document>.csv  -> {document, filename, content_type, columns, rows, share, csv}
     query: share=1 plus the arguments that target accepts (validated here, then again by the document itself)
POST integrations/mqtt/save              -> validated broker settings (audited without the password)
POST integrations/mqtt/test              -> connect with a separate client id and publish <prefix>/portal/test once
GET  /hyperloom/api/stream               -> text/event-stream of a projection of the overview snapshot

MQTT publishing runs only where the portal samplers run: the ``mqtt`` collector holds a publisher lease in the
store (one publishing portal process per configuration), starts, restarts or stops the publisher from the saved
settings, and renews it; a publisher whose collector stops renewing it (samplers stopped, lease lost) publishes
``offline`` and ends. CSV export never reads a file: it is built from a document the portal already serves, named
from a fixed list of targets with their own row, column, share and query rules.
"""

import atexit
import csv
import hashlib
import io
import json
import math
import re
import secrets as random
import threading
import time
from urllib.parse import urlsplit

from hyperloom import metrics_export, mqtt, telemetry


NAMESPACE = "prefs/integrations"
MQTT_KEY = "mqtt"
CURSOR_NAMESPACE = "integrations/mqtt-cursor"
OWNER_NAMESPACE = "integrations/mqtt-owner"
COLLECTOR_INTERVAL_S = 5
LEASE_S = 30.0
RENEW_GRACE_S = 20.0
RETAINED_NAMESPACE = "integrations/mqtt-retained"
# A publisher stops writing this long before its lease could be taken over: longer than one protocol operation,
# so a send started under the lease always ends before a successor can start.
LEASE_MARGIN_S = mqtt.OPERATION_TIMEOUT_S + 1.0
# Ordinary publishing stops one more operation earlier, so the final offline status always fits inside the lease.
WORK_MARGIN_S = LEASE_MARGIN_S + mqtt.OPERATION_TIMEOUT_S

STREAM_PATH = "/hyperloom/api/stream"
STREAM_MAX_CLIENTS = 4
STREAM_INTERVAL_S = 2.0
STREAM_MAX_S = 30 * 60
STREAM_HEARTBEAT_S = 15.0
STREAM_MAX_BYTES = 1024 * 1024

EXPORT_MAX_ROWS = 50_000
EXPORT_MAX_BYTES = 8 * 1024 * 1024
FORMULA = re.compile(r"^\s*[=+\-@\t\r]")

HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
                  r"|[0-9A-Fa-f:.]{2,45}")
PREFIX = re.compile(r"[A-Za-z0-9_-]{1,32}(?:/[A-Za-z0-9_-]{1,32}){0,3}")
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,21}")      # + "-t" for the test connection stays within 23 characters
TOPIC_PART = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SETTINGS = ("enabled", "host", "port", "tls", "topic_prefix", "username", "client_id", "interval_s")
DEFAULTS = {"enabled": False, "host": None, "port": None, "tls": False, "topic_prefix": "hyperloom", "username": None,
            "client_id": None, "interval_s": 10, "revision": 0, "credential_ref": None}
EVENTS_PER_RUN = 100
EVENTS_PENDING_MAX = 500


# -- MQTT settings ----------------------------------------------------------------------------------------------------
def _port(config):
    return config.get("port") or (8883 if config.get("tls") else 1883)


def mqtt_settings(store):
    """The saved settings over the defaults (never the password)."""
    saved = store.get(NAMESPACE, MQTT_KEY) if store is not None else None
    return {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}


def validate(arguments, current):
    """(settings, password action, password) from a save request; unknown fields are refused."""
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    if set(arguments) - set(SETTINGS) - {"password", "clear_password"}:
        raise ValueError("unknown settings field (allowed: " + ", ".join(SETTINGS + ("password", "clear_password")) + ")")
    config = {**current, **{key: arguments[key] for key in SETTINGS if key in arguments}}
    for key in ("enabled", "tls"):
        if not isinstance(config[key], bool):
            raise ValueError(key + " must be true or false")
    host = config["host"]
    if host is not None and (not isinstance(host, str) or not HOST.fullmatch(host)):
        raise ValueError("host must be a host name or an IP address")
    if config["enabled"] and not host:
        raise ValueError("a broker host is required to enable publishing")
    port = config["port"]
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("port must be 1..65535")
    if not isinstance(config["topic_prefix"], str) or not PREFIX.fullmatch(config["topic_prefix"]):
        raise ValueError("topic prefix must be 1-4 segments of letters, digits, _ or - separated by /")
    username = config["username"]
    if username is not None and (not isinstance(username, str) or not 1 <= len(username) <= 128 or "\x00" in username):
        raise ValueError("user name must be 1..128 characters")
    if config["client_id"] is None:
        config["client_id"] = "hyperloom-" + random.token_hex(4)
    if not isinstance(config["client_id"], str) or not CLIENT_ID.fullmatch(config["client_id"]):
        raise ValueError("client id must be 1..21 letters, digits, _ or -")
    if type(config["interval_s"]) is not int or not 5 <= config["interval_s"] <= 300:
        raise ValueError("interval must be 5..300 s")
    password = arguments.get("password")
    clear = arguments.get("clear_password", False)
    if not isinstance(clear, bool):
        raise ValueError("clear_password must be true or false")
    if password is not None and clear:
        raise ValueError("give a new password or clear it, not both")
    if password is not None:
        if not isinstance(password, str) or not 1 <= len(password) <= 512 or "\x00" in password:
            raise ValueError("password must be 1..512 characters")
        if username is None:
            raise ValueError("a password needs a user name")
    action = "set" if password is not None else "cleared" if clear else "unchanged"
    # Every save (also a password-only change) is a new revision, so a running publisher reconnects with it.
    config["revision"] = int(current.get("revision") or 0) + 1
    # The password of this configuration lives under its own secret name, chosen once.
    if not isinstance(config.get("credential_ref"), str) or not mqtt.SECRET_NAME.fullmatch(config["credential_ref"]):
        config["credential_ref"] = "integrations-mqtt-" + random.token_hex(4)
    return config, action, password


def public_settings(config):
    """Settings as documents show them: no password, only whether one is saved (a key name redaction keeps)."""
    return {**{key: config.get(key) for key in SETTINGS}, "port": _port(config),
            "credential_saved": mqtt.password_saved(config.get("credential_ref")), "revision": config.get("revision")}


def _password(config):
    return mqtt.load_password(config["credential_ref"]) if config.get("username") and config.get("credential_ref") else None


def save(api, arguments):
    store = telemetry.open_store(api)
    config, action, password = validate(arguments, mqtt_settings(store))
    if action == "set":
        mqtt.save_password(config["credential_ref"], password)
    elif action == "cleared":
        mqtt.clear_password(config["credential_ref"])
    store.put(NAMESPACE, MQTT_KEY, config)
    return {"saved": True, "credential": action, "mqtt": public_settings(config)}


def audit_arguments(arguments):
    """What a save changes, for the audit log: never the password, only what happened to it."""
    arguments = arguments if isinstance(arguments, dict) else {}
    clean = {key: arguments[key] for key in SETTINGS if key in arguments}
    clean["credential"] = ("set" if arguments.get("password") is not None else
                           "cleared" if arguments.get("clear_password") is True else "unchanged")
    return clean


def save_audited(api, arguments):
    try:
        result = save(api, arguments)
    except ValueError as exc:
        api.audit("integrations/mqtt/save", audit_arguments(arguments), {"state": "refused", "error": str(exc)})
        raise
    except OSError:
        api.audit("integrations/mqtt/save", audit_arguments(arguments), {"state": "failed", "error": "store unavailable"})
        raise ValueError("settings could not be saved") from None
    api.audit("integrations/mqtt/save", audit_arguments(arguments), {"state": "succeeded"})
    return result


def test_publish(api, arguments, connect=None):
    """One connection with a separate client id and no will (the publishing session is never displaced)."""
    if arguments:
        raise ValueError("test takes no arguments (it uses the saved settings)")
    config = mqtt_settings(telemetry.open_store(api))
    if not config.get("host"):
        raise PermissionError("no broker configured")
    started = time.monotonic()
    deadline = started + mqtt.OPERATION_TIMEOUT_S        # one budget for the whole action
    username = config.get("username")
    password = _password(config)
    client = mqtt.Client(config["host"], _port(config), client_id=config["client_id"] + "-t", username=username,
                         password=password, tls=config.get("tls", False), timeout=max(0.001, deadline - time.monotonic()),
                         **({"connect": connect} if connect else {}))
    try:
        if time.monotonic() >= deadline:
            raise mqtt.MQTTError("timed out waiting for the broker")
        client.connect()
        client.timeout = deadline - time.monotonic()
        if client.timeout <= 0:
            raise mqtt.MQTTError("timed out waiting for the broker")
        client.publish(config["topic_prefix"] + "/portal/test",
                       json.dumps({"ts": api.clock(), "message": "hyperloom test message"}), 1, False)
        client.close()          # no DISCONNECT round trip: the budget is spent on the test itself
        result = {"ok": True, "detail": "published to " + config["topic_prefix"] + "/portal/test"}
    except (mqtt.MQTTError, OSError, ValueError) as exc:
        client.close()
        result = {"ok": False, "detail": str(exc)[:200] if isinstance(exc, mqtt.MQTTError) else "connection failed"}
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    api.audit("integrations/mqtt/test", {}, {"state": "succeeded" if result["ok"] else "failed", "detail": result["detail"]})
    return result


# -- publisher ownership (one publishing portal process per configuration) -----------------------------------------
_lock = threading.Lock()
_running = {}       # id(api) -> publishing state
_tokens = {}        # id(api) -> lease owner token of that portal


def _token(api):
    with _lock:
        return _tokens.setdefault(id(api), random.token_hex(8))


def claim_lease(store, token, now):
    """Take or renew the publisher lease (compare-and-set in one statement); its stored expiry when this portal
    holds it, else None."""
    value = json.dumps({"owner": token, "expires": now + LEASE_S}, sort_keys=True)
    store.transaction([
        ("INSERT OR IGNORE INTO docs(namespace, key, ts, value) VALUES (?, 'lease', ?, ?)", (OWNER_NAMESPACE, now, value)),
        ("UPDATE docs SET ts=?, value=? WHERE namespace=? AND key='lease' AND (json_extract(value, '$.owner')=? "
         "OR json_extract(value, '$.expires')<?)", (now, value, OWNER_NAMESPACE, token, now))])
    current = store.get(OWNER_NAMESPACE, "lease") or {}
    return current.get("expires") if current.get("owner") == token else None


def release_lease(store, token):
    store.transaction([("DELETE FROM docs WHERE namespace=? AND key='lease' AND json_extract(value, '$.owner')=?",
                        (OWNER_NAMESPACE, token))])


def _sampler_running(api):
    sampler = getattr(api, "sampler", None)
    stopped = getattr(sampler, "_stop", None)       # the sampler's own stop flag (set by telemetry.stop)
    return sampler is not None and not (stopped is not None and stopped.is_set())


def _digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def stop_publisher(api, store=None):
    """Stop this portal's publisher; True when none is left running. The lease is given back only after the
    publisher thread has ended."""
    with _lock:
        state = _running.pop(id(api), None)
    if state is None:
        return True
    ended = state["publisher"].stop()
    if not ended:
        with _lock:
            _running.setdefault(id(api), state)     # still winding down: keep it (and the lease) until it ends
        return False
    if store is not None:
        try:
            release_lease(store, _token(api))
        except Exception:           # noqa: BLE001 - the lease expires on its own
            pass
    return True


@atexit.register
def _stop_all():
    with _lock:
        states = list(_running.values())
        _running.clear()
    for state in states:
        state["publisher"].stop(timeout=2.0)


def publisher_state(api):
    with _lock:
        state = _running.get(id(api))
    if state is None:
        return None
    return {**state["publisher"].state(), "events_lost": state["events_lost"]}


def _lease_cutoff(api, expires):
    """The local (monotonic) time at which the stored lease lapses: what is left of it now, not a fresh 30 s."""
    return time.monotonic() + (expires - api.clock())


def _ensure(api, config, factory, store, expires):
    """The running publisher for these settings (renewed), a new one, or None while a replaced one winds down."""
    digest = _digest(config)
    with _lock:
        state = _running.get(id(api))
        if state is not None and state["digest"] == digest and state["publisher"].state()["running"]:
            state["renewed"] = time.monotonic()
            state["lease_until"] = _lease_cutoff(api, expires)
            return state
    if state is not None and not stop_publisher(api):
        return None
    password = _password(config)
    state = {"digest": digest, "status": {}, "metrics": set(), "connects": 0, "metrics_at": None, "pending": {},
             "events_lost": 0, "renewed": time.monotonic(), "lock": threading.Lock(), "cleared": set(), "gaps": {}, "metric_keys": {},
             "lease_until": _lease_cutoff(api, expires)}

    def alive():
        # Checked before every connection and every send: renewed by the collector recently, the lease not about
        # to lapse (so a successor can never overlap), and the portal samplers still running.
        now = time.monotonic()
        return (now - state["renewed"] <= COLLECTOR_INTERVAL_S + RENEW_GRACE_S
                and now < state["lease_until"] - WORK_MARGIN_S and _sampler_running(api))

    def finished(publisher):
        # Only a publisher that ended on its own (not renewed, samplers stopped) gives the lease back; one replaced
        # by a new configuration leaves it to its successor.
        with _lock:
            current = _running.get(id(api)) is state
            if current:
                _running.pop(id(api), None)
        if current:
            try:
                release_lease(store, _token(api))
            except Exception:       # noqa: BLE001 - the lease expires on its own
                pass

    def owner():
        return time.monotonic() < state["lease_until"] - LEASE_MARGIN_S

    state["publisher"] = factory({**config, "port": _port(config)}, password, alive, finished, owner)
    with _lock:
        _running[id(api)] = state
    state["publisher"].start()
    return state


def machine_statuses(api, store):
    """{machine: "1"|"0"|None} for every configured machine from the fresh ``node_up`` state samples (None = state
    unknown). Reads the parsed configuration and the store only: publishing never triggers a node probe."""
    machines = api.cluster().machines
    descriptor = store.descriptor("node_up") or {}
    age = metrics_export.fresh_age(descriptor.get("interval_s"))
    latest = {row["labels"].get("machine"): row["value"] for row in store.latest("node_up", max_age=age)}
    result = {}
    for machine in machines:
        mid = getattr(machine, "id", None)
        if isinstance(mid, str) and TOPIC_PART.fullmatch(mid):
            value = latest.get(mid)
            result[mid] = None if value is None else "1" if value >= 1 else "0"
    return result


def machine_metrics(store, now):
    """{machine: (values, valid_until)}: the fresh latest node/USB4 gauge samples of each machine as
    [{series, labels, value, ts}] (labels kept as objects, so distinct series never merge), and the wall time at
    which the first of them goes stale."""
    result = {}
    for name in store.names():
        if not metrics_export.exported_series(name) or name in ("health_findings_active", "node_up"):
            continue
        descriptor = store.descriptor(name) or {}
        if descriptor.get("kind") not in ("gauge", "state"):
            continue
        age = metrics_export.fresh_age(descriptor.get("interval_s"))
        for row in store.latest(name, max_age=age):
            labels = dict(row["labels"])
            machine = labels.pop("machine", None)
            if not isinstance(machine, str) or not TOPIC_PART.fullmatch(machine) or not math.isfinite(row["value"]):
                continue
            values, valid_until = result.get(machine, ([], math.inf))
            values.append({"series": name, "labels": labels, "value": row["value"], "ts": row["ts"]})
            result[machine] = (values, min(valid_until, row["ts"] + age))
    return result


METRICS_PAYLOAD_MAX = 200 * 1024


def metrics_payload(machine, now, values):
    """The retained metrics JSON, within the message size limit: values beyond it are left out and counted."""
    values = sorted(values, key=lambda v: (v["series"], json.dumps(v["labels"], sort_keys=True)))
    kept = len(values)
    while True:
        payload = json.dumps({"machine": machine, "published_at": now, "values": values[:kept],
                              "omitted": len(values) - kept}, allow_nan=False, sort_keys=True)
        if len(payload.encode("utf-8")) <= METRICS_PAYLOAD_MAX or kept == 0:
            return payload
        kept //= 2


def _events_after(store, after, limit):
    rows = store.execute("SELECT id, ts, category, severity, machine, message FROM events WHERE id > ? ORDER BY id "
                         "LIMIT ?", (after, limit))
    return [dict(zip(("id", "ts", "category", "severity", "machine", "message"), row)) for row in rows]


def _cursor(store):
    """(last done event id or None, cumulative lost count) from one stored document."""
    value = store.get(CURSOR_NAMESPACE, "events")
    if isinstance(value, dict) and type(value.get("id")) is int:
        return value["id"], int(value.get("lost") or 0)
    return None, 0


def _advance_cursor(store, state):
    """Move the persisted cursor over the leading events that are done (acknowledged, or lost). The losses (dropped
    from the full queue, or ids removed by retention before they were read, counted on the event after them) are
    stored in the same document as the cursor, so a restart neither repeats nor forgets them."""
    pending, cursor, lost = state["pending"], None, 0
    with state["lock"]:
        for event_id in sorted(pending):
            if pending[event_id] == "queued":
                break
            lost += (pending.pop(event_id) == "lost") + state["gaps"].pop(event_id, 0)
            cursor = event_id
    if cursor is not None:
        _, total = _cursor(store)
        store.put(CURSOR_NAMESPACE, "events", {"id": cursor, "lost": total + lost})
        state["events_lost"] = total + lost


def _retained(store):
    value = store.get(RETAINED_NAMESPACE, "topics")
    return set(value) if isinstance(value, list) else set()


def _set_retained(store, topics):
    store.put(RETAINED_NAMESPACE, "topics", sorted(topics))


def publish_round(api, store, config, state, now):
    """Queue this round's messages; returns the number queued.

    Status and metrics topics are retained and coalesced. Every retained topic this configuration ever published
    is remembered in the store; one that has no current value (machine removed or state unknown, no fresh metrics,
    an old topic prefix) is cleared with an empty retained message (QoS 1, forgotten once acknowledged), so a
    restarted publisher also clears what an earlier one left. Metrics carry each sample's own time and expire in the
    queue when the first of them goes stale. Events go out at QoS 1 in id order; the persisted cursor moves only
    over acknowledged events, or ones dropped from the full queue or removed by event retention before they were
    read, which are counted as lost, so a restart may repeat events (``id`` for deduplication) but never skips
    one silently."""
    publisher, prefix = state["publisher"], config["topic_prefix"]
    queued = 0
    connects = publisher.state()["connects"]
    if connects != state["connects"]:
        state["connects"] = connects
        state["status"] = {}            # a new session: retained values are sent again
        state["metrics_at"] = None
    try:
        statuses = machine_statuses(api, store)
    except (OSError, ValueError, RuntimeError, LookupError, AttributeError):
        statuses = None                 # configuration unreadable: nothing is changed or cleared this round
    with state["lock"]:
        done, state["cleared"] = state["cleared"], set()
    stored = _retained(store)
    remembered = stored - done          # topics whose clearing the broker acknowledged are forgotten
    wanted = set()
    if statuses is not None:
        for machine, value in sorted(statuses.items()):
            if value is None:
                continue
            topic = f"{prefix}/status/{machine}"
            wanted.add(topic)
            if state["status"].get(machine) != value:
                publisher.put(topic, value, 0, True)
                state["status"][machine] = value
                queued += 1
        state["status"] = {machine: value for machine, value in state["status"].items()
                           if statuses.get(machine) is not None}
    # Freshness is evaluated every round (a machine whose samples went stale is cleared now); values are sent
    # every interval_s, or when a machine gains fresh values.
    # Only configured machines; when the sample set of a machine changes (a sample went stale, a new one arrived)
    # its retained message is replaced at once.
    metrics = {machine: value for machine, value in machine_metrics(store, now).items()
               if statuses is not None and machine in statuses}
    due = state["metrics_at"] is None or now - state["metrics_at"] >= config["interval_s"]
    keys = {}
    for machine, (values, valid_until) in sorted(metrics.items()):
        topic = f"{prefix}/metrics/{machine}"
        keys[topic] = sorted(json.dumps([v["series"], v["labels"]], sort_keys=True) for v in values)
        if due or keys[topic] != state["metric_keys"].get(topic):
            payload = metrics_payload(machine, now, values)
            publisher.put(topic, payload, 0, True, expires=time.monotonic() + max(0.0, valid_until - now))
            queued += 1
    if due:
        state["metrics_at"] = now
    state["metric_keys"] = keys
    state["metrics"] = set(keys)
    wanted |= state["metrics"]
    if statuses is not None:            # without the configuration nothing is cleared this round
        for topic in sorted(remembered - wanted):
            def cleared(outcome, topic=topic):
                if outcome == "sent":
                    with state["lock"]:
                        state["cleared"].add(topic)
            publisher.put(topic, b"", 1, True, done=cleared)
            queued += 1
    current = remembered | wanted
    if current != stored:
        _set_retained(store, current)
    _advance_cursor(store, state)
    room = EVENTS_PENDING_MAX - len(state["pending"])
    if room > 0:
        cursor, total = _cursor(store)
        state["events_lost"] = max(state["events_lost"], total)
        if cursor is None:              # first run: start at the newest event (history is not replayed)
            cursor = store.execute("SELECT MAX(id) FROM events")[0][0] or 0
            store.put(CURSOR_NAMESPACE, "events", {"id": cursor, "lost": total})
        with state["lock"]:
            after = max([cursor, *state["pending"]])
        previous = after
        for event in _events_after(store, after, min(EVENTS_PER_RUN, room)):
            event_id = event["id"]
            with state["lock"]:
                state["pending"][event_id] = "queued"
                if event_id - previous > 1:     # ids removed by retention or a clear before we read them
                    state["gaps"][event_id] = event_id - previous - 1
            previous = event_id

            def done(outcome, event_id=event_id):
                with state["lock"]:
                    if state["pending"].get(event_id) == "queued":
                        state["pending"][event_id] = "acked" if outcome == "sent" else "lost"
            try:
                publisher.put(prefix + "/events", json.dumps(event, allow_nan=False), 1, False, done=done)
            except ValueError:          # larger than a message may be: counted as lost, never retried
                done("dropped")
            queued += 1
    return queued


def _factory(settings, password, alive, on_exit, owner):
    return mqtt.Publisher(settings, password=password, alive=alive, on_exit=on_exit, owner=owner)


def collect(api, now, factory=None):
    """Collector ``mqtt``: hold the publisher lease, keep the publisher in line with the settings, queue a round."""
    store = telemetry.open_store(api)
    config = mqtt_settings(store)
    if not config.get("enabled") or not config.get("host"):
        stop_publisher(api, store)
        return []
    expires = claim_lease(store, _token(api), now)
    if expires is None:
        stop_publisher(api)         # never started, or our lease lapsed and another portal took it over
        return []
    state = _ensure(api, config, factory or _factory, store, expires)
    if state is None:
        return []
    publish_round(api, store, config, state, now)
    status = state["publisher"].state()
    store.describe("integrations_mqtt_connected", "state", interval=COLLECTOR_INTERVAL_S,
                   legend={"0": "disconnected", "1": "connected"})
    store.describe("integrations_mqtt_queue", "gauge", unit="messages", interval=COLLECTOR_INTERVAL_S)
    return [("integrations_mqtt_connected", {}, 1 if status["connected"] else 0, "state"),
            ("integrations_mqtt_queue", {}, status["queued"], "gauge")]


def topics(config):
    prefix = config["topic_prefix"]
    return [{"topic": prefix + "/portal/status", "qos": 1, "retained": True,
             "payload": "online; offline as the last will or on a clean stop"},
            {"topic": prefix + "/status/<machine>", "qos": 0, "retained": True,
             "payload": "1 up, 0 down (node_up); cleared when unknown"},
            {"topic": prefix + "/metrics/<machine>", "qos": 0, "retained": True,
             "payload": f"JSON of the fresh node and USB4 values every {config['interval_s']} s; cleared when none is fresh"},
            {"topic": prefix + "/events", "qos": 1, "retained": False,
             "payload": "JSON per new activity event (id for deduplication)"}]


# -- live stream (F44) ----------------------------------------------------------------------------------------------
SCALAR = True
SENSITIVE = re.compile(r"config|argv|args|env|command|path|address|endpoint|url|host|ssh|key|token|secret|password|"
                       r"credential|cookie|process|history|log|why|error|reason|detail|message|stderr|stdout|"
                       r"exception|trace", re.I)


def _scalar(value):
    if value is None or isinstance(value, bool) or type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= 512


def project(value, spec):
    """Keep only what ``spec`` names: SCALAR = a scalar or a list of scalars; {key: spec} = those keys of a dict;
    [spec] = each item of a list (at most 256); "why" = a dict of short strings; "values" = a dict of scalar values
    and one level of dicts of scalars, with no key that looks like configuration, an address, a credential or
    free-form error text."""
    if spec is SCALAR:
        if _scalar(value):
            return value
        if isinstance(value, list) and len(value) <= 64 and all(_scalar(item) for item in value):
            return list(value)
        return None
    if spec == "why":
        # Producer reasons can carry raw error text: only which fields are unavailable is streamed.
        return {k: "unavailable" for k in list(value)[:64] if isinstance(k, str) and len(k) <= 64} if isinstance(value, dict) else None
    if spec == "values":
        if not isinstance(value, dict):
            return project(value, SCALAR)
        out = {}
        for key, item in list(value.items())[:64]:
            if not isinstance(key, str) or SENSITIVE.search(key):
                continue
            if isinstance(item, dict):
                inner = {}
                for name, value in list(item.items())[:64]:
                    if isinstance(name, str) and not SENSITIVE.search(name) and _scalar(value):
                        inner[name] = value
                out[key] = inner
            else:
                out[key] = project(item, SCALAR)
        return out
    if isinstance(spec, list):
        return [project(item, spec[0]) for item in value[:256]] if isinstance(value, list) else None
    if isinstance(spec, dict):
        return {key: project(value.get(key), sub) for key, sub in spec.items()} if isinstance(value, dict) else None
    raise TypeError("bad projection")


ACCELERATOR = {key: SCALAR for key in ("name", "memory_kind", "utilization_pct", "memory_used_bytes",
                                       "memory_total_bytes", "memory_free_bytes", "temperature_c", "power_w", "excluded")}
STREAM_SPEC = {
    "devices": {"summary": "values", "machines": [{"id": SCALAR, "name": SCALAR, "os": SCALAR, "health": SCALAR,
                                                    "last_seen": SCALAR, "ram_summary": SCALAR, "disk_free_bytes": SCALAR,
                                                    "roles": [{"profile": SCALAR, "role": SCALAR}],
                                                    "accelerators": [ACCELERATOR], "why": "why"}], "why": "why"},
    "profiles": {"profiles": [{"id": SCALAR, "label": SCALAR, "family": SCALAR, "state": SCALAR, "nodes": SCALAR,
                               "router": SCALAR, "started_at": SCALAR, "ready_after_s": SCALAR, "why": "why"}], "why": "why"},
    "stats": {"engines": [{"id": SCALAR, "profile": SCALAR, "throughput": "values", "ttft_p50": "values",
                           "ttft_p95": "values", "draft_acceptance": "values", "usb4_exchange_mean_us": SCALAR,
                           "status": {"state": SCALAR, "model": SCALAR, "context_tokens": SCALAR, "uptime_seconds": SCALAR,
                                      "slots": "values"}, "why": "why"}],
              "routers": [{"profile": SCALAR, "waiting": SCALAR}], "why": "why"},
    "overview": {"kpis": "values", "machines": [{"id": SCALAR, "name": SCALAR, "status": "values", "roles": SCALAR,
                                                  "uptime_s": SCALAR, "headroom": "values", "gpu": ["values"],
                                                  "emitted_rate": "values", "turn_holder": "values", "why": "why"}],
                 "why": "why"},
}


def stream_content(api):
    """The streamed projection (without its event metadata)."""
    from hyperloom.web import features
    snapshot = api.snapshot()
    content = {"why": {}}
    for name in ("devices", "profiles", "stats"):
        content[name] = project(snapshot.get(name), STREAM_SPEC[name])
    if "overview" in features.registry().gets:
        try:
            content["overview"] = project(api.read("overview", {}), STREAM_SPEC["overview"])
        except Exception:       # noqa: BLE001 - one failing document never ends the stream
            content["overview"] = None
            content["why"]["overview"] = "overview unavailable"
    else:
        content["overview"] = None
        content["why"]["overview"] = "overview feature not loaded"
    from hyperloom.web.api import redact
    return redact(content)


_stream_lock = threading.Lock()
_stream_clients = {}        # (id(server), id(api)) -> clients now


def stream_clients(api):
    with _stream_lock:
        return sum(count for (_, owner), count in _stream_clients.items() if owner == id(api))


def _refuse(handler, status, message, extra=(), head=False):
    body = json.dumps({"error": message}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in extra:
        handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    if not head:
        handler.wfile.write(body)


def serve_stream(handler, head=False, *, interval=None, max_s=None, heartbeat=None, sleep=time.sleep):
    """GET /hyperloom/api/stream on the portal handler (Host and request target already checked by the server).
    At most STREAM_MAX_CLIENTS per portal; each connection ends after STREAM_MAX_S with an ``end`` event; an event
    is sent when the projection changes, a comment heartbeat otherwise; nothing is written anywhere."""
    interval = STREAM_INTERVAL_S if interval is None else interval
    max_s = STREAM_MAX_S if max_s is None else max_s
    heartbeat = STREAM_HEARTBEAT_S if heartbeat is None else heartbeat
    if head:
        _refuse(handler, 405, "the stream is GET only", (("Allow", "GET"),), head=True)
        return
    if urlsplit(handler.path).query:
        _refuse(handler, 400, "the stream takes no query arguments")
        return
    api = handler.server.api
    key = (id(handler.server), id(api))
    with _stream_lock:
        busy = _stream_clients.get(key, 0) >= STREAM_MAX_CLIENTS
        if not busy:
            _stream_clients[key] = _stream_clients.get(key, 0) + 1
    if busy:
        _refuse(handler, 503, f"at most {STREAM_MAX_CLIENTS} stream clients", (("Retry-After", "5"),))
        return
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True

        def write(text):
            handler.wfile.write(text.encode("utf-8"))
            handler.wfile.flush()

        write("retry: 5000\n\n")
        started = last_write = time.monotonic()
        last, seq = None, 0
        while True:
            now = time.monotonic()
            if now - started >= max_s:
                write('event: end\ndata: {"why": "maximum connection time reached; reconnect"}\n\n')
                return
            try:
                content = json.dumps(stream_content(api), ensure_ascii=True, allow_nan=False, sort_keys=True,
                                     separators=(",", ":"))
            except Exception:  # noqa: BLE001 - reported with a fixed message, the stream continues
                content = None
                write('event: error\ndata: {"why": "snapshot unavailable"}\n\n')
                last_write = now
            if content is not None and len(content) > STREAM_MAX_BYTES:
                write('event: error\ndata: {"why": "snapshot exceeds 1 MiB"}\n\n')
                last_write = now
            elif content is not None and content != last:
                seq += 1
                payload = '{"schema":"hyperloom/stream-v1","seq":%d,"server_time":%s,"content":%s}' % (
                    seq, json.dumps(api.clock()), content)
                write(f"id: {seq}\nevent: snapshot\ndata: {payload}\n\n")
                last, last_write = content, now
            elif now - last_write >= heartbeat:
                write(": keep-alive\n\n")
                last_write = now
            sleep(max(0.0, interval - (time.monotonic() - now)))
    except OSError:
        return          # the client went away (reset, broken pipe or a write timeout)
    finally:
        with _stream_lock:
            _stream_clients[key] -= 1
            if not _stream_clients[key]:
                del _stream_clients[key]


# -- CSV export (F46) -----------------------------------------------------------------------------------------------
def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if math.isfinite(value) else ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "'" + value if FORMULA.match(value) else value


class _Chunks:
    """Write target for csv.writer: keeps the written pieces and their UTF-8 size (no repeated buffer copies)."""

    def __init__(self, limit):
        self.parts, self.size, self.limit = [], 0, limit

    def write(self, text):
        self.size += len(text.encode("utf-8"))
        if self.size > self.limit:
            raise ValueError("export exceeds 8 MiB; narrow the range")
        self.parts.append(text)


def to_csv(columns, rows):
    """CSV text (CRLF) with a header row of labels; formula-leading strings (also in headers) are escaped."""
    out = _Chunks(EXPORT_MAX_BYTES)
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([cell(label) for _, label in columns])
    for row in rows:
        writer.writerow([cell(row.get(key)) for key, _ in columns])
    return "".join(out.parts)


def _utc(value):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value)) if type(value) in (int, float) and math.isfinite(value) else None


def _table(field, columns, share):
    """A target whose rows are the dicts of ``field``; ``share`` lists the columns share mode keeps."""
    def rows(document, shared):
        values = document.get(field)
        if values is None:
            raise PermissionError((document.get("why") or {}).get(field) or field + " unavailable")
        if not isinstance(values, list):
            raise LookupError(field + " is not a table")
        chosen = [column for column in columns if not shared or column[0] in share]
        return chosen, [row for row in values if isinstance(row, dict)]
    return rows


def _history(document, shared):
    if document.get("lines") is None:
        raise PermissionError((document.get("why") or {}).get("lines") or "history unavailable")
    labels = sorted({key for line in document["lines"] for key in line.get("labels") or {}})
    if shared:
        labels = [key for key in labels if key in ("machine", "device")]
    columns = [["time", "Time (epoch s, UTC)"], ["time_utc", "Time (UTC)"]] + [["l." + key, key] for key in labels] + [
        ["avg", "Average"], ["min", "Minimum"], ["max", "Maximum"], ["n", "Samples"], ["sum", "Sum"], ["last", "Last"],
        ["coverage", "Coverage"]]
    rows = []
    for line in document["lines"]:
        values = line.get("labels") or {}
        for point in line.get("points") or []:
            if not isinstance(point, list) or len(point) < 8:
                continue
            t, avg, high, low, n, total, last, coverage = point[:8]     # later fields (cadence) are not exported
            rows.append({"time": t, "time_utc": _utc(t), "avg": avg, "min": low, "max": high, "n": n, "sum": total,
                         "last": last, "coverage": coverage, **{"l." + key: values.get(key) for key in labels}})
    return columns, rows


def _energy(document, shared):
    buckets = document.get("buckets")
    if buckets is None:
        raise PermissionError((document.get("why") or {}).get("buckets") or "energy buckets unavailable")
    columns = [["start", "Start (epoch s, UTC)"], ["start_utc", "Start (UTC)"], ["label", "Period"],
               ["machine", "Machine"], ["wh", "Energy (Wh, estimated)"], ["coverage", "Coverage"]]
    if shared:
        columns = [column for column in columns if column[0] != "machine"]
    rows = []
    for bucket in buckets if isinstance(buckets, list) else []:
        if not isinstance(bucket, dict):
            continue
        base = {"start": bucket.get("start"), "start_utc": _utc(bucket.get("start")), "label": bucket.get("label"),
                "coverage": bucket.get("coverage")}
        machines = bucket.get("machines") if isinstance(bucket.get("machines"), dict) else {}
        if shared:
            known = [value for value in machines.values() if type(value) in (int, float)]
            rows.append({**base, "wh": sum(known) if known and len(known) == len(machines) else None})
        else:
            rows += [{**base, "machine": machine, "wh": wh} for machine, wh in sorted(machines.items())]
    return columns, rows


def _bench(document, shared):
    runs = document.get("runs")
    if runs is None:
        raise PermissionError((document.get("why") or {}).get("runs") or "benchmark runs unavailable")
    fixed = [["id", "Run"], ["kind", "Kind"], ["state", "State"], ["started", "Started (epoch s)"],
             ["finished", "Finished (epoch s)"], ["model", "Model"], ["recipe_digest", "Recipe digest"], ["target", "Target"]]
    share = ("kind", "state", "started", "finished")      # the benchmark share contract: no model, target or ids
    rows, numbers = [], set()
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        row = {key: run.get(key) for key, _ in fixed}
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        for key, value in summary.items():
            if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key) and type(value) in (int, float):
                row["summary." + key] = value
                numbers.add(key)
        rows.append(row)
    columns = [column for column in fixed if not shared or column[0] in share]
    columns += [["summary." + key, key] for key in sorted(numbers)[:48]]
    return columns, rows


RANGE = re.compile(r"[0-9]{1,8}|all|[0-9]{1,3}[hd]")
TARGETS = {
    "devices": {"label": "Machines", "query": {},
                "rows": _table("machines", [["id", "Machine"], ["name", "Name"], ["os", "OS"], ["health", "Health"],
                                            ["access", "Access"], ["cpu_model", "CPU"], ["ram_summary", "Host RAM"],
                                            ["disk_free_bytes", "Disk free (bytes)"], ["last_seen", "Last seen"]],
                               ("id", "os", "health"))},
    "stats": {"label": "Recent requests", "query": {},
              "rows": _table("recent_requests", [["time", "Time (epoch s)"], ["engine", "Engine"],
                                                 ["prompt_tokens", "Prompt tokens"], ["reused_tokens", "Reused tokens"],
                                                 ["engine_ttft_ms", "Engine TTFT (ms)"], ["decode_tok_s", "Decode (tok/s)"],
                                                 ["output_tokens", "Output tokens"], ["status", "Status"], ["route", "Route"]],
                             ("time", "prompt_tokens", "reused_tokens", "engine_ttft_ms", "decode_tok_s", "output_tokens",
                              "status"))},
    "ledger": {"label": "Token ledger by model", "query": {"range": RANGE}, "feature": "ledger",
               "rows": _table("models", [["model", "Model"], ["generated", "Generated tokens"],
                                         ["computed", "Computed prefill tokens"], ["cached", "Cache-restored tokens"],
                                         ["imported", "Imported tokens"], ["input", "Input tokens"], ["total", "Total tokens"],
                                         ["cached_share", "Cache-restored share"], ["output_input", "Output:input"],
                                         ["observed_s", "Observed (s)"]],
                              ("model", "generated", "computed", "cached", "imported", "input", "total", "cached_share",
                               "output_input"))},
    "energy": {"label": "Energy per period", "query": {"range": RANGE}, "feature": "energy", "rows": _energy},
    "bench/runs": {"label": "Benchmark runs", "feature": "bench/runs", "rows": _bench,
                   "query": {"kind": re.compile(r"[a-z]{1,16}"), "target": re.compile(r"[A-Za-z0-9._-]{1,64}"),
                             "limit": re.compile(r"[0-9]{1,3}")}},
}
HISTORY_QUERY = {"range": re.compile(r"[0-9.]{1,12}"), "start": re.compile(r"[0-9.]{1,16}"),
                 "end": re.compile(r"[0-9.]{1,16}"), "tier": re.compile(r"raw|m1|m15")}


def export_targets(api):
    """The exports this portal offers now (a target whose feature is not loaded is not offered)."""
    from hyperloom.web import features
    registry = features.registry()
    targets = [{"document": name, "label": spec["label"], "query": sorted(spec["query"])}
               for name, spec in TARGETS.items() if spec.get("feature") is None or spec["feature"] in registry.gets]
    store = telemetry.existing_store(api)
    if store is not None and "history/" in registry.prefixes:
        targets += [{"document": "history/" + name, "label": "History · " + name, "query": sorted(HISTORY_QUERY)}
                    for name in store.names()[:200]]
    return targets


def _arguments(query, rules, labels=False):
    forwarded = {}
    for key, value in query.items():
        if key == "share":
            continue
        rule = rules.get(key)
        if rule is None and labels and re.fullmatch(r"l\.[a-z][a-z0-9_]{0,31}", key) and len(value) <= 128:
            forwarded[key] = value
        elif rule is not None and rule.fullmatch(value):
            forwarded[key] = value
        else:
            raise ValueError("unsupported export argument " + key)
    return forwarded


def export(api, rest, query):
    if not rest.endswith(".csv"):
        raise LookupError("unknown export (expected <document>.csv)")
    name = rest[:-4]
    share = query.get("share", "0")
    if share not in ("0", "1"):
        raise ValueError("share must be 0 or 1")
    shared = share == "1"
    from hyperloom.web import features
    registry = features.registry()
    if name.startswith("history/") and "history/" in registry.prefixes:
        document = api.read(name, _arguments(query, HISTORY_QUERY, labels=True))
        columns, rows = _history(document, shared)
    elif name in TARGETS:
        spec = TARGETS[name]
        if spec.get("feature") is not None and spec["feature"] not in registry.gets:
            raise LookupError(name + " is not available in this portal")
        document = api.read(name, _arguments(query, spec["query"]))
        columns, rows = spec["rows"](document, shared)
    else:
        raise LookupError("not an exportable document: " + name)
    if len(rows) > EXPORT_MAX_ROWS:
        raise ValueError(f"export has more than {EXPORT_MAX_ROWS} rows; narrow the range")
    text = to_csv(columns, rows)
    filename = "hyperloom-" + re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") + ("-share" if shared else "") + ".csv"
    return api.document("integrations-export", document=name, filename=filename, content_type="text/csv",
                        columns=[key for key, _ in columns], rows=len(rows), share=shared, csv=text)


# -- state document ---------------------------------------------------------------------------------------------------
def state(api, query):
    if query:
        raise ValueError("integrations takes no query arguments")
    store = telemetry.existing_store(api)
    why = {}
    sampling = telemetry.status(api) is not None
    if store is None:
        why["mqtt"] = "settings store not open in this portal process (samplers disabled)"
        mqtt_doc = None
    else:
        config = mqtt_settings(store)
        lease = store.get(OWNER_NAMESPACE, "lease") or {}
        mqtt_doc = {"settings": public_settings(config), "publisher": publisher_state(api), "topics": topics(config),
                    "queue_limit": mqtt.QUEUE_LIMIT,
                    "publishing_here": lease.get("owner") == _token(api) and lease.get("expires", 0) >= api.clock()}
        if mqtt_doc["publisher"] is None:
            why["publisher"] = ("publishing disabled" if not config.get("enabled") else
                                "samplers not running in this portal process" if not sampling else
                                "another portal process publishes" if lease.get("owner") not in (None, _token(api))
                                and lease.get("expires", 0) >= api.clock() else "starting")
    exporter = {"command": "hyperloom metrics serve", "listen": metrics_export.DEFAULT_LISTEN,
                "store_series": "node_* and usb4_* gauges and states (latest sample within 3 intervals); counters are "
                                "not exported (a stored delta does not record its span)",
                "ledger": "hyperloom_ledger_tokens_total from the token ledger when present",
                "findings": "hyperloom_finding{rule,severity,machine,subject} while health evaluations are fresh",
                "passthrough": "complete router and engine histograms and router per-caller series, when exported"}
    stream = {"endpoint": STREAM_PATH, "clients": stream_clients(api), "max_clients": STREAM_MAX_CLIENTS,
              "interval_s": STREAM_INTERVAL_S, "max_connection_s": STREAM_MAX_S,
              "documents": ["devices", "profiles", "stats", "overview"]}
    return api.document("integrations", mqtt=mqtt_doc, sampling=sampling, exporter=exporter, stream=stream,
                        export={"targets": export_targets(api), "formats": ["csv", "json"]}, why=why)


def register(reg):
    reg.get("integrations", state)
    reg.get_prefix("integrations/export/", export)
    reg.post("integrations/mqtt/save", save_audited)
    reg.post("integrations/mqtt/test", test_publish)
    reg.collector("mqtt", collect, interval=COLLECTOR_INTERVAL_S)
    reg.capability("mqtt", True, "optional publisher; off until a broker is saved and enabled")
