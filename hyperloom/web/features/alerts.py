"""Alert notifications (F21): findings delivered to JSON webhooks and to push topics.

A channel is {kind: webhook|push, a URL, min_severity, reminders}. The URL is kept in the user's secret directory
(encrypted for the current user on Windows, a private file elsewhere) and is never returned, logged or written to an
event: documents show it masked. Only https URLs are accepted, or http to a numeric loopback address.

Delivery follows the findings' transitions: each health evaluation queues, per channel, ONE message with that
evaluation's transitions in order (firing at or above the channel's minimum severity, escalated, resolved for
findings the channel was told about) and due reminders, in the same transaction as the finding states, so a restart
never re-notifies and a crash loses nothing. A new channel first learns the findings already firing. Messages of a
channel are sent oldest first (a later message waits for an earlier one), so a resolve never overtakes its firing. The
``alerts`` collector sends them: one POST with a 5 s total deadline, a direct connection (no proxy), redirects
refused; retries after 5 s, 20 s and 60 s, then every 5 min with the same message id until delivered or the channel
is removed; the outbox keeps at most 100 messages (the oldest undelivered are dropped, with an event). Delivery is at least once (a crash after the receiver accepted a message resends it); every payload carries a stable
``message_id`` for de-duplication. Every send and failure is an activity-log event (category ``alerts``).
"""

import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit
import uuid

from hyperloom import secrets, telemetry
from hyperloom.web.features import health


CHANNELS, TRACK = "alerts/channels", "alerts/track"
KINDS = ("webhook", "push")
REMINDERS = {"none": None, "1h": 3600, "4h": 4 * 3600, "12h": 12 * 3600, "24h": 24 * 3600}
CHANNEL_ID = re.compile(r"ch-[0-9a-f]{8}")
MAX_CHANNELS, MAX_OUTBOX, SAVE_TRIES = 20, 100, 5
BACKOFF, COOLDOWN_S = (5.0, 20.0, 60.0), 300.0
SEND_TIMEOUT, RUN_BUDGET_S, RUN_SENDS, CLAIM_S = 5.0, 12.0, 3, 30.0
RESPONSE_CAP = 4096
SEVERITIES = health.SEVERITIES
_TABLE = """
CREATE TABLE IF NOT EXISTS alerts_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
                                          channel TEXT NOT NULL, created REAL NOT NULL, next_at REAL NOT NULL,
                                          attempts INTEGER NOT NULL DEFAULT 0, claimed_until REAL, items TEXT NOT NULL);
"""
BOOK_LOCK = threading.RLock()     # planning + its commit vs delivery acknowledgements (this process)


def prepare(store):
    health.prepare(store)
    if not getattr(store, "_hyperloom_alert_tables", False):
        store.ensure_table(_TABLE)
        store._hyperloom_alert_tables = True


# -- URLs -----------------------------------------------------------------------------------------------------------

def check_url(url):
    """The URL if a channel may use it; ValueError (whose text never contains the URL) otherwise."""
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError("the channel URL must be 1..2048 characters")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("the channel URL must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("the channel URL is malformed") from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError("the channel URL must use https (or http to a loopback address)")
    if "@" in parts.netloc:
        raise ValueError("the channel URL must not carry user credentials; put the topic secret in the path")
    if parts.fragment or "#" in url:
        raise ValueError("the channel URL must not have a fragment")
    if not parts.hostname:
        raise ValueError("the channel URL needs a host")
    if port == 0:
        raise ValueError("the channel URL port is invalid")
    if scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("http is allowed only to a numeric loopback address; use https")
    return url


def mask(url):
    parts = urlsplit(url)
    host = parts.hostname or "?"
    host = "[" + host + "]" if ":" in host else host
    return f"{parts.scheme.lower()}://{host}{':' + str(parts.port) if parts.port else ''}/…"


VERSION = re.compile(r"[0-9a-f]{16}")


def _url_path(channel, version):
    if not isinstance(channel, str) or not CHANNEL_ID.fullmatch(channel):
        raise ValueError("invalid channel id")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("invalid channel URL version")
    return secrets._directory() / "alert-urls" / f"{channel}.{version}"


def store_url(channel, url):
    """Write the URL as a new immutable version file; returns the version the channel record then publishes."""
    data = check_url(url).encode("utf-8")
    version = uuid.uuid4().hex[:16]
    secrets._write(_url_path(channel, version), secrets._dpapi(data) if os.name == "nt" else data)
    return version


def load_url(channel, version):
    data = _url_path(channel, version).read_bytes()
    if os.name == "nt":
        data = secrets._dpapi(data, decrypt=True)
    return check_url(data.decode("utf-8"))


def retire_url(channel, version):
    """Remove one URL version that a committed record no longer names (explicit retirement only)."""
    if version is None:
        return
    try:
        _url_path(channel, version).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def drop_urls(channel):
    """Remove every URL version of a deleted channel."""
    if not isinstance(channel, str) or not CHANNEL_ID.fullmatch(channel):
        raise ValueError("invalid channel id")
    directory = secrets._directory() / "alert-urls"
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return
    for path in entries:
        stem, _, version = path.name.partition(".")
        if stem == channel and VERSION.fullmatch(version) and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass


# -- channels -------------------------------------------------------------------------------------------------------

def _public(record):
    return {key: record.get(key) for key in ("id", "name", "kind", "min_severity", "reminders", "url_masked", "created", "updated")}


def channels(store):
    return sorted((value for _, _, value in store.items(CHANNELS) if isinstance(value, dict)), key=lambda c: (c.get("created") or 0, c["id"]))


def _validated(arguments, existing):
    unknown = set(arguments) - {"id", "name", "kind", "url", "min_severity", "reminders"}
    if unknown:
        raise ValueError("unknown arguments (allowed: id, name, kind, url, min_severity, reminders)")
    record = dict(existing or {})
    name = arguments.get("name", record.get("name"))
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or not name.isprintable():
        raise ValueError("name must be 1..64 printable characters")
    kind = arguments.get("kind", record.get("kind"))
    if not isinstance(kind, str) or kind not in KINDS or existing is not None and kind != existing.get("kind"):
        raise ValueError("kind must be webhook or push (and cannot change)")
    severity = arguments.get("min_severity", record.get("min_severity", "warning"))
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise ValueError("min_severity must be info, warning or critical")
    reminders = arguments.get("reminders", record.get("reminders", "none"))
    if not isinstance(reminders, str) or reminders not in REMINDERS:
        raise ValueError("reminders must be none, 1h, 4h, 12h or 24h")
    url = arguments.get("url")
    if url is None and existing is None:
        raise ValueError("a new channel needs its URL")
    if url is not None:
        check_url(url)
    record.update(name=name.strip(), kind=kind, min_severity=severity, reminders=reminders)
    return record, url


def save_channel(store, arguments, now):
    """Create or update a channel (the portal POST and the CLI); returns the public record.

    A new URL is written as a new version file first; the channel record that names it, the catch-up message and its
    bookkeeping then commit in ONE transaction guarded by the channel record and the finding states this save read
    (another save or a finding that changed meanwhile, in any process, aborts it and the save reads again). The
    version file of a save that did not commit is removed; superseded versions are removed after the commit."""
    cid = arguments.get("id")
    if cid is not None and (not isinstance(cid, str) or not CHANNEL_ID.fullmatch(cid)):
        raise ValueError("invalid channel id")
    existing = store.get(CHANNELS, cid) if cid else None
    if cid is not None and existing is None:
        raise LookupError("unknown channel")
    record, url = _validated(arguments, existing)
    if cid is None and len(channels(store)) >= MAX_CHANNELS:
        raise PermissionError(f"at most {MAX_CHANNELS} channels")
    cid = cid or "ch-" + uuid.uuid4().hex[:8]
    prepare(store)
    version = store_url(cid, url) if url is not None else None
    try:
        for _ in range(SAVE_TRIES):
            with BOOK_LOCK:
                text = health.stored_text(store, CHANNELS, cid)
                current = json.loads(text) if text else None
                if existing is not None and current is None:
                    raise LookupError("unknown channel")
                record, _ = _validated(arguments, current)
                record.update(id=cid, created=(current or {}).get("created", now), updated=now)
                if version is not None:
                    record.update(url_version=version, url_masked=mask(url))
                lowered = current is None or health.RANK[record["min_severity"]] < health.RANK.get(current.get("min_severity"), 1)
                revision = health.version(store, health.STATE)
                states = {key: value for key, _, value in store.items(health.STATE)}
                if current is None and len(channels(store)) >= MAX_CHANNELS:
                    raise PermissionError(f"at most {MAX_CHANNELS} channels")
                statements = [health.unchanged(CHANNELS, cid, text), health.same_version(health.STATE, revision),
                              # the channel cap, checked inside the transaction (concurrent creates cannot pass it)
                              ("INSERT INTO health_guard(x) SELECT 1 WHERE ? AND (SELECT COUNT(*) FROM docs WHERE "
                               "namespace=?) >= ?", (current is None, CHANNELS, MAX_CHANNELS)),
                              health.upsert_doc(CHANNELS, cid, record, now)]
                if lowered:
                    statements += catch_up(store, record, states, now)
                try:
                    health.commit(store, statements)
                except sqlite3.IntegrityError:
                    continue
                if version is not None and (current or {}).get("url_version") != version:
                    retire_url(cid, (current or {}).get("url_version"))     # the version this commit replaced
                version = None                      # published: never removed below
                return _public(record)
        raise PermissionError("the channel or the findings kept changing while saving; try again")
    finally:
        if version is not None:
            try:
                _url_path(cid, version).unlink(missing_ok=True)
            except OSError:
                pass


def delete_channel(store, cid):
    if not isinstance(cid, str) or not CHANNEL_ID.fullmatch(cid):
        raise ValueError("invalid channel id")
    if store.get(CHANNELS, cid) is None:
        raise LookupError("unknown channel")
    prepare(store)
    with BOOK_LOCK:
        health.commit(store, [health.delete_doc(CHANNELS, cid),
                              ("DELETE FROM docs WHERE namespace=? AND substr(key, 1, ?)=?", (TRACK, len(cid) + 1, cid + "|")),
                              ("DELETE FROM alerts_outbox WHERE channel=?", (cid,))])
    drop_urls(cid)
    return {"deleted": cid}


# -- planning -------------------------------------------------------------------------------------------------------

def _item(finding, state):
    return {"id": finding["id"], "rule": finding.get("rule"), "state": state, "severity": finding.get("severity"),
            "title": finding.get("title"), "detail": finding.get("detail"), "hint": finding.get("hint"),
            "machine": finding.get("machine"), "since": finding.get("since"), "occurrence": finding.get("occurrence")}


CHANNELS_VERSION = ("(SELECT group_concat(entry, char(10)) FROM (SELECT key || char(9) || value AS entry FROM docs "
                    "WHERE namespace='" + CHANNELS + "' ORDER BY key))")      # the complete records, not update times


def channels_version(store):
    return store.execute("SELECT " + CHANNELS_VERSION)[0][0]


def channels_unchanged(version):
    """Abort the transaction when a channel was added, changed or removed since the planner read them."""
    return ("INSERT INTO health_guard(x) SELECT 1 WHERE " + CHANNELS_VERSION + " IS NOT ?", (version,))


def _queue(cid, items, now, message=None):
    message = message or uuid.uuid4().hex
    return message, ("INSERT INTO alerts_outbox(message_id, channel, created, next_at, attempts, items) "
                     "VALUES (?, ?, ?, ?, 0, ?)", (message, cid, now, now, json.dumps(items, ensure_ascii=True)))


RESYNC, ATTEMPTED = "alerts/resync", "alerts/attempted"


def track_key(cid, finding_id, occurrence):
    """Delivery bookkeeping is kept per occurrence of a finding, so every occurrence a channel was told about keeps
    its own record until its resolve is delivered."""
    return f"{cid}|{finding_id}|{occurrence}"


def _mark(finding, now, message="pending"):
    summary = {key: finding.get(key) for key in ("id", "rule", "title", "severity", "machine", "since", "occurrence")}
    return {"occurrence": finding.get("occurrence"), "severity": finding.get("severity"), "finding": summary,
            "queued_at": now, "message": message, "delivered_at": None, "delivered_severity": None, "escalating": None,
            "reminded_at": None, "reminder_queued": None, "resolving": None}


def bound(now):
    """Statements, run last in the committing transaction, that keep the outbox at MAX_OUTBOX rows. They count the
    table inside the transaction, so writers on different connections can never exceed the bound. When it is
    exceeded, the channel owning the oldest unclaimed message loses its whole unclaimed backlog (a later message can
    then never arrive without an earlier one) and is marked for a resync; its bookkeeping is kept, so the next
    evaluation reconciles it: every occurrence it was told about that is no longer active gets its resolve, every
    active one whose firing was dropped is sent again. One event per dropped backlog."""
    over = f"(SELECT COUNT(*) FROM alerts_outbox) > {MAX_OUTBOX}"
    victim = "(SELECT channel FROM alerts_outbox WHERE claimed_until IS NULL OR claimed_until < ? ORDER BY id LIMIT 1)"
    once = [("INSERT INTO events(ts, category, severity, machine, message, data) SELECT ?, 'alerts', 'warning', NULL, "
             "'alert outbox full: dropped the undelivered backlog of channel ' || " + victim + " || "
             "'; it is brought up to date at the next evaluation', NULL WHERE " + over, (now, now)),
            ("INSERT INTO docs(namespace, key, ts, value) SELECT ?, " + victim + ", ?, '{}' WHERE " + over +
             " AND " + victim + " IS NOT NULL ON CONFLICT(namespace, key) DO NOTHING", (RESYNC, now, now, now)),
            # messages already attempted (a receiver may have accepted them) are remembered for the reconciliation
            ("INSERT INTO docs(namespace, key, ts, value) SELECT ?, message_id, ?, '{\"channel\": \"' || channel || '\"}' "
             "FROM alerts_outbox WHERE " + over + " AND channel = " + victim + " AND attempts > 0 AND "
             "(claimed_until IS NULL OR claimed_until < ?) ON CONFLICT(namespace, key) DO NOTHING",
             (ATTEMPTED, now, now, now)),
            ("DELETE FROM alerts_outbox WHERE " + over + " AND channel = " + victim +
             " AND (claimed_until IS NULL OR claimed_until < ?)", (now, now))]
    # One pass drops one channel's backlog; claimed rows (at most one per channel) are never dropped, so passes
    # for every possible channel always restore the bound.
    return once * (MAX_CHANNELS + 2) + [health.RETENTION]


def plan(store, transitions, states, now):
    """Statements queuing this evaluation's deliveries: per channel ONE message with this evaluation's transitions
    (in order), a reconciliation after a dropped backlog, and due reminders. The caller commits them with the finding
    states while holding BOOK_LOCK; the statements abort the commit if the channels or the bookkeeping changed
    meanwhile (the evaluation is then simply redone)."""
    prepare(store)
    with BOOK_LOCK:
        statements = [channels_unchanged(channels_version(store))]     # captured before the reads it protects
        configured = channels(store)
        texts = {key: text for key, text in store.execute("SELECT key, value FROM docs WHERE namespace=?", (TRACK,))}
        track = {key: json.loads(text) for key, text in texts.items()}
        queued = {row[0] for row in store.execute("SELECT message_id FROM alerts_outbox")}
        rows = 0
        active = {fid: f for fid, f in states.items() if f.get("state") in ("firing", "clearing")}
        known = {c["id"] for c in configured}
        for key in [k for k in track if k.split("|", 1)[0] not in known]:
            statements.append(health.delete_doc(TRACK, key))
            del track[key]
        for key, _ in store.execute("SELECT key, value FROM docs WHERE namespace=?", (RESYNC,)):
            if key not in known:
                statements.append(health.delete_doc(RESYNC, key))
        for key, text in store.execute("SELECT key, value FROM docs WHERE namespace=?", (ATTEMPTED,)):
            if json.loads(text).get("channel") not in known:
                statements.append(health.delete_doc(ATTEMPTED, key))
        resync = {key: text for key, text in store.execute("SELECT key, value FROM docs WHERE namespace=?", (RESYNC,))}
        for channel in configured:
            cid, floor = channel["id"], health.RANK.get(channel.get("min_severity"), 1)
            period = REMINDERS.get(channel.get("reminders"))
            items, touched = [], {}
            current = lambda key: touched.get(key, track.get(key))
            if cid in resync:
                # Its backlog was dropped (outbox full): reconcile from the bookkeeping, before this round's changes.
                # An occurrence the channel may have seen (delivered, attempted, or its firing still queued) gets its
                # resolve once it is no longer active; one it never saw is forgotten; dropped firings and escalations
                # of active occurrences are sent again (at least once).
                statements.append(health.unchanged(RESYNC, cid, resync[cid]))
                statements.append(health.delete_doc(RESYNC, cid))
                tried = {key for key, text in store.execute("SELECT key, value FROM docs WHERE namespace=?", (ATTEMPTED,))
                         if json.loads(text).get("channel") == cid}
                for message_id in sorted(tried):
                    statements.append(health.delete_doc(ATTEMPTED, message_id))
                for key, mark in sorted(track.items()):
                    if not key.startswith(cid + "|") or not isinstance(mark, dict):
                        continue
                    fid = key.split("|")[1]
                    finding = active.get(fid)
                    seen = bool(mark.get("delivered_at")) or mark.get("message") in tried or mark.get("message") in queued
                    ended = mark.get("resolving") or finding is None or finding.get("occurrence") != mark.get("occurrence")
                    if ended:
                        if mark.get("resolving") and mark["resolving"] in queued:
                            continue                                        # its resolve is still on the way
                        if seen:
                            items.append({**(mark.get("finding") or {"id": fid}), "state": "resolved"})
                            touched[key] = {**mark, "resolving": "pending"}
                        else:
                            touched[key] = None                             # never seen: nothing to resolve
                        continue
                    if not mark.get("delivered_at") and mark.get("message") not in queued:
                        items.append(_item(finding, "firing"))              # its firing was dropped: again
                        touched[key] = {**mark, "message": "pending", "escalating": None,
                                        "severity": finding.get("severity")}
                    elif mark.get("escalating") and mark["escalating"] not in queued:
                        items.append(_item(finding, "escalated"))           # its escalation was dropped: again
                        touched[key] = {**mark, "escalating": "pending", "severity": finding.get("severity")}
                for fid, finding in sorted(active.items()):
                    key = track_key(cid, fid, finding.get("occurrence"))
                    if current(key) is None and key not in touched and health.RANK.get(finding.get("severity"), 1) >= floor:
                        items.append(_item(finding, "firing"))
                        touched[key] = _mark(finding, now)
            for change, finding in transitions:
                key = track_key(cid, finding["id"], finding.get("occurrence"))
                mark = current(key)
                live = isinstance(mark, dict) and not mark.get("resolving")
                rank = health.RANK.get(finding.get("severity"), 1)
                if change == "cleared":
                    if live:
                        items.append(_item(finding, "resolved"))
                        touched[key] = {**mark, "resolving": "pending"}
                elif change == "escalated" and live:
                    if rank > health.RANK.get(mark.get("severity"), 0):
                        items.append(_item(finding, "escalated"))
                        touched[key] = {**mark, "severity": finding.get("severity"), "escalating": "pending"}
                elif change in ("appeared", "escalated") and mark is None and rank >= floor:
                    items.append(_item(finding, "firing"))
                    touched[key] = _mark(finding, now)
            if period:
                for fid, finding in sorted(active.items()):
                    key = track_key(cid, fid, finding.get("occurrence"))
                    mark = current(key)
                    if not isinstance(mark, dict) or mark.get("resolving"):
                        continue
                    pending = mark.get("reminder_queued") in queued or mark.get("reminder_queued") == "pending"
                    if mark.get("delivered_at") and not pending and \
                            now - max(mark["delivered_at"], mark.get("reminded_at") or 0) >= period:
                        items.append(_item(finding, "reminder"))
                        touched[key] = {**mark, "reminder_queued": "pending"}
            message = uuid.uuid4().hex if items else None
            for key, mark in touched.items():
                statements.append(health.unchanged(TRACK, key, texts.get(key)))     # bookkeeping read = committed
                if mark is None:
                    statements.append(health.delete_doc(TRACK, key))
                    track.pop(key, None)
                    continue
                for field in ("message", "resolving", "reminder_queued", "escalating"):
                    if mark.get(field) == "pending":
                        mark[field] = message
                statements.append(health.upsert_doc(TRACK, key, mark, now))
                track[key] = mark
            if items:
                _, row = _queue(cid, items, now, message)
                statements.append(row)
                rows += 1
        return statements + (bound(now) if rows else [])


def catch_up(store, channel, states, now):
    """Statements telling a new channel (or one with a lower minimum severity) about the findings already firing at
    or above its minimum; committed with the channel record by ``save_channel``."""
    cid, floor = channel["id"], health.RANK.get(channel.get("min_severity"), 1)
    items, statements, message = [], [], uuid.uuid4().hex
    for fid, finding in sorted(states.items()):
        key = track_key(cid, fid, finding.get("occurrence"))
        if finding.get("state") not in ("firing", "clearing") or health.RANK.get(finding.get("severity"), 1) < floor:
            continue
        text = health.stored_text(store, TRACK, key)
        if text is not None:
            continue
        items.append(_item(finding, "firing"))
        statements.append(health.unchanged(TRACK, key, text))
        statements.append(health.upsert_doc(TRACK, key, _mark(finding, now, message), now))
    if not items:
        return []
    _, row = _queue(cid, items, now, message)
    return statements + [row] + bound(now)


# -- sending --------------------------------------------------------------------------------------------------------

_SENDS = threading.BoundedSemaphore(2)      # sends in flight in this process (collector + test POSTs)
_RESOLVERS = threading.BoundedSemaphore(4)  # name lookups still running after their send gave up (they never send)


def _when(ts):
    return time.strftime("%H:%M UTC", time.gmtime(ts)) if isinstance(ts, (int, float)) else "?"


def text(items, message_id=None):
    lines = [f"hyperloom alert {message_id}"] if message_id else []
    for item in items:
        lines.append(" · ".join(str(part) for part in (item.get("state", "").upper(), item.get("severity"), item.get("title"),
                                                      item.get("detail"), "since " + _when(item.get("since"))) if part))
    return "\n".join(lines)


def payload(kind, channel_kind, message_id, items, now):
    if channel_kind == "webhook":
        body = {"schema": "hyperloom/alert-v1", "message_id": message_id, "kind": kind, "sent_at": now, "items": items}
        return json.dumps(body, ensure_ascii=True, allow_nan=False, default=str).encode("utf-8"), "application/json"
    return text(items, message_id).encode("utf-8"), "text/plain; charset=utf-8"


class Expired(Exception):
    pass


class _Attempt:
    """One send's deadline. Every socket is registered before it blocks; at the deadline a watchdog shuts the
    registered socket down, and every step checks the deadline before it starts, so nothing is transmitted after
    it. Name resolution (which transmits nothing to the receiver) runs on a bounded helper thread joined with the
    time left."""

    def __init__(self, timeout, clock=time.monotonic):
        self.clock, self.deadline = clock, clock() + timeout
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.sock = None
        self.timer = threading.Timer(timeout, self.abort)
        self.timer.daemon = True
        self.timer.start()

    def left(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0 or self.expired.is_set():
            raise Expired()
        return remaining

    def register(self, sock):
        with self.lock:
            self.sock = sock
            if self.expired.is_set():
                raise Expired()

    def abort(self):
        with self.lock:
            self.expired.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.sock.close()
                except OSError:
                    pass

    def close(self):
        self.timer.cancel()
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass

    def resolve(self, host, port):
        try:
            ipaddress.ip_address(host)
            return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
        except ValueError:
            pass
        if not _RESOLVERS.acquire(blocking=False):
            raise ConnectionError("too many name lookups pending")
        result = {}

        def lookup():
            try:
                result["addresses"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                result["error"] = exc
            finally:
                _RESOLVERS.release()

        worker = threading.Thread(target=lookup, name="hyperloom-alert-resolve", daemon=True)
        worker.start()
        worker.join(self.left())
        if "addresses" not in result:
            if "error" in result:
                raise result["error"]
            raise Expired()
        return result["addresses"]

    def connect(self, scheme, host, port):
        port = port or (443 if scheme == "https" else 80)
        last = None
        for family, kind, proto, _, address in self.resolve(host, port):
            sock = socket.socket(family, kind, proto)
            self.register(sock)
            try:
                sock.settimeout(self.left())
                sock.connect(address)
                break
            except Expired:
                raise
            except OSError as exc:
                last = exc
                sock.close()
        else:
            raise last or ConnectionError("no address")
        if scheme == "https":
            context = ssl.create_default_context()
            sock.settimeout(self.left())
            wrapped = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
            self.register(wrapped)
            wrapped.do_handshake()
            sock = wrapped
        return sock


class _Connected(http.client.HTTPConnection):
    """An HTTP connection over a socket the attempt already opened (and registered with its watchdog)."""

    def __init__(self, host, sock, attempt):
        super().__init__(host)
        self.sock, self._attempt = sock, attempt

    def connect(self):
        raise ConnectionError("the attempt's socket is closed")

    def send(self, data):
        self.sock.settimeout(self._attempt.left())      # never transmit after the deadline
        super().send(data)


def send(url, channel_kind, kind, message_id, items, now, timeout=SEND_TIMEOUT):
    """One POST with a total deadline, in the calling thread; {"ok", "status"?, "error"?, "busy"?}. A direct
    connection (never a proxy), https verified, redirects never followed; the error text never contains the URL."""
    data, content_type = payload(kind, channel_kind, message_id, items, now)
    parts = urlsplit(check_url(url))
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    if not _SENDS.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "another alert send is in progress"}
    attempt = _Attempt(timeout)
    try:
        sock = attempt.connect(parts.scheme.lower(), parts.hostname, parts.port)
        host = parts.hostname if ":" not in parts.hostname else "[" + parts.hostname + "]"
        conn = _Connected(host + (f":{parts.port}" if parts.port else ""), sock, attempt)
        conn.request("POST", target, body=data, headers={"Content-Type": content_type, "User-Agent": "hyperloom-alerts",
                                                          "Connection": "close"})
        sock.settimeout(attempt.left())
        response = conn.getresponse()
        response.read(RESPONSE_CAP)
        status = response.status
        attempt.left()
    except Expired:
        return {"ok": False, "error": f"no answer within {timeout:g} s"}
    except Exception as exc:
        if attempt.expired.is_set() or isinstance(exc, TimeoutError) or attempt.clock() >= attempt.deadline:
            return {"ok": False, "error": f"no answer within {timeout:g} s"}
        return {"ok": False, "error": f"connection failed ({type(exc).__name__})"}
    finally:
        attempt.close()
        _SENDS.release()
    if 300 <= status < 400:
        return {"ok": False, "status": status, "error": "redirect refused"}
    if not 200 <= status < 300:
        return {"ok": False, "status": status, "error": f"HTTP {status}"}
    return {"ok": True, "status": status}


def _ack(store, owner, generation, row, channel, outcome, now):
    oid, message, cid, attempts, items = row
    items = json.loads(items)
    name = f"{channel.get('name', cid)} ({channel.get('url_masked', '?')})" if channel else cid
    statements = health.guard(owner, generation, now)
    with BOOK_LOCK:
        counts = {}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        summary = ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))
        data = {"channel": cid, "message_id": message, "items": len(items)}
        if outcome["ok"] or channel is None:
            statements.append(("DELETE FROM alerts_outbox WHERE id=?", (oid,)))
        if outcome["ok"]:
            marks = {}
            for item in items:
                key = track_key(cid, item["id"], item.get("occurrence"))
                if key not in marks:
                    text = health.stored_text(store, TRACK, key)
                    marks[key] = (text, json.loads(text) if text else None)
                text, mark = marks[key]
                if not isinstance(mark, dict):
                    continue
                if item["state"] == "resolved":
                    mark = None if mark.get("resolving") == message else mark      # delivered: the record is done
                else:
                    mark = {**mark, "delivered_at": mark.get("delivered_at") or now}
                    if item["state"] in ("firing", "escalated"):
                        mark["delivered_severity"] = item.get("severity")
                        if mark.get("escalating") == message:
                            mark["escalating"] = None
                    if item["state"] == "reminder":
                        mark.update(reminded_at=now, reminder_queued=None)
                marks[key] = (text, mark)
            for key, (text, mark) in marks.items():
                if text is None:
                    continue
                statements.append(health.unchanged(TRACK, key, text))
                statements.append(health.delete_doc(TRACK, key) if mark is None else health.upsert_doc(TRACK, key, mark, now))
            statements.append(health.insert_event(now, "alerts", "info", f"alert sent to {name}: {summary}",
                                                  data={**data, "status": outcome.get("status")}))
        elif channel is not None:
            wait = BACKOFF[attempts] if attempts < len(BACKOFF) else COOLDOWN_S
            statements.append(("UPDATE alerts_outbox SET attempts=?, next_at=?, claimed_until=NULL WHERE id=?",
                               (attempts + 1, now + wait, oid)))
            statements.append(health.insert_event(now, "alerts", "warning",
                                                  f"alert to {name} failed ({outcome['error']}); attempt {attempts + 1}, "
                                                  f"next in {wait:g} s", data={**data, "attempt": attempts + 1}))
        statements.append(health.RETENTION)
        health.commit(store, statements)


def deliver(api, now, store=None, clock=time.monotonic, sender=None):
    """The ``alerts`` collector body: send due messages (lease holder only), oldest first per channel, within the
    run budget. Returns no points."""
    store = store or telemetry.open_store(api)
    prepare(store)
    owner = health.owner_of(api)
    generation = health.held(store, owner, now)
    if generation is None:
        return []
    started, sends = clock(), 0
    heads = store.execute("SELECT id, message_id, channel, attempts, items, next_at, claimed_until FROM alerts_outbox "
                          "WHERE id IN (SELECT MIN(id) FROM alerts_outbox GROUP BY channel) ORDER BY id")
    for oid, message, cid, attempts, items, next_at, claimed in heads:
        left = RUN_BUDGET_S - (clock() - started)
        if left < 1.0 or sends >= RUN_SENDS:
            break
        if next_at > now or (claimed is not None and claimed >= now):
            continue
        claim = now + CLAIM_S
        health.commit(store, health.guard(owner, generation, now) + [
            ("UPDATE alerts_outbox SET claimed_until=? WHERE id=? AND (claimed_until IS NULL OR claimed_until < ?)",
             (claim, oid, now))])
        check = store.execute("SELECT claimed_until FROM alerts_outbox WHERE id=?", (oid,))
        if not check or check[0][0] != claim:
            continue
        channel = store.get(CHANNELS, cid)
        row = (oid, message, cid, attempts, items)
        if channel is None:
            _ack(store, owner, generation, row, None, {"ok": False, "error": "channel removed"}, now)
            continue
        try:
            url = load_url(cid, channel.get("url_version"))
        except Exception as exc:
            outcome = {"ok": False, "error": "channel URL unavailable (" + type(exc).__name__ + ")"}
        else:
            outcome = (sender or send)(url, channel["kind"], "alert", message, json.loads(items), now,
                                       timeout=min(SEND_TIMEOUT, left))
        if outcome.get("busy"):         # a test send holds the slots: not an attempt, retried on the next run
            health.commit(store, health.guard(owner, generation, now) + [
                ("UPDATE alerts_outbox SET claimed_until=NULL WHERE id=?", (oid,))])
            break
        sends += 1
        _ack(store, owner, generation, row, channel, outcome, now)
    return []


def test_channel(api, store, cid, now, sender=None):
    """Send a test message now (one attempt); the result and an activity-log event."""
    if not isinstance(cid, str) or not CHANNEL_ID.fullmatch(cid):
        raise ValueError("invalid channel id")
    channel = store.get(CHANNELS, cid)
    if channel is None:
        raise LookupError("unknown channel")
    item = {"id": "test", "rule": "test", "state": "test", "severity": "info", "title": "Test notification",
            "detail": "hyperloom alert channel test", "hint": None, "machine": None, "since": now}
    try:
        url = load_url(cid, channel.get("url_version"))
    except Exception as exc:
        outcome = {"ok": False, "error": "channel URL unavailable (" + type(exc).__name__ + ")"}
    else:
        outcome = (sender or send)(url, channel["kind"], "test", uuid.uuid4().hex, [item], now)
    if outcome.get("busy"):
        raise PermissionError("another alert send is in progress; try again in a few seconds")
    name = f"{channel['name']} ({channel.get('url_masked', '?')})"
    store.event("alerts", "info" if outcome["ok"] else "warning",
                f"test alert to {name} " + ("delivered" if outcome["ok"] else f"failed ({outcome['error']})"),
                data={"channel": cid, "test": True, "status": outcome.get("status")}, ts=now)
    return {"channel": cid, **outcome}


# -- portal endpoints -----------------------------------------------------------------------------------------------

def _audit_arguments(arguments):
    """Validated values only (never the URL, never an arbitrary client string)."""
    safe = {}
    cid, name = arguments.get("id"), arguments.get("name")
    if isinstance(cid, str) and CHANNEL_ID.fullmatch(cid):
        safe["id"] = cid
    if isinstance(name, str) and 1 <= len(name.strip()) <= 64 and name.isprintable():
        safe["name"] = name.strip()
    for key, allowed in (("kind", KINDS), ("min_severity", SEVERITIES), ("reminders", tuple(REMINDERS))):
        if isinstance(arguments.get(key), str) and arguments[key] in allowed:
            safe[key] = arguments[key]
    if "url" in arguments:
        try:
            safe["url_masked"] = mask(check_url(arguments["url"]))
        except Exception:
            safe["url_masked"] = "[not shown]"
    return safe


def _audited(api, action, arguments, work):
    """Run a channel operation and audit it; refusals carry this module's own fixed texts, anything else only its
    exception class."""
    try:
        result = work()
    except (ValueError, LookupError, PermissionError) as exc:
        api.audit(action, _audit_arguments(arguments), {"state": "refused", "error": str(exc).strip("'\"")[:300]})
        raise
    except Exception as exc:
        api.audit(action, _audit_arguments(arguments), {"state": "failed", "error": type(exc).__name__})
        raise
    api.audit(action, _audit_arguments(arguments), {"state": "succeeded", "result": result})
    return result


def channels_post(api, arguments):
    store = telemetry.open_store(api)
    return _audited(api, "alerts/channels", arguments, lambda: save_channel(store, arguments, api.clock()))


def delete_post(api, arguments):
    store = telemetry.open_store(api)

    def work():
        if set(arguments) - {"id"}:
            raise ValueError("delete takes only the channel id")
        return delete_channel(store, arguments.get("id"))
    return _audited(api, "alerts/channels/delete", arguments, work)


def test_post(api, arguments):
    store = telemetry.open_store(api)

    def work():
        if set(arguments) - {"id"}:
            raise ValueError("test takes only the channel id")
        return test_channel(api, store, arguments.get("id"), api.clock())
    return _audited(api, "alerts/test", arguments, work)


def document(api, query):
    if query:
        raise ValueError("alerts takes no query arguments")
    store = telemetry.existing_store(api)
    lists = {"kinds": list(KINDS), "severities": list(SEVERITIES), "reminders": list(REMINDERS)}
    if store is None:
        why = "alert channels unavailable: the telemetry store is not open in this portal process"
        return api.document("alerts", channels=None, outbox=None, recent=None, **lists,
                            why={"channels": why, "outbox": why, "recent": why})
    prepare_read = getattr(store, "_hyperloom_alert_tables", False)
    outbox = {"length": 0, "oldest_age_s": None, "per_channel": {}}
    if prepare_read or store.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts_outbox'"):
        rows = store.execute("SELECT channel, COUNT(*), MIN(created) FROM alerts_outbox GROUP BY channel")
        oldest = [row[2] for row in rows]
        outbox = {"length": sum(row[1] for row in rows), "per_channel": {row[0]: row[1] for row in rows},
                  "oldest_age_s": max(0.0, api.clock() - min(oldest)) if oldest else None}
    return api.document("alerts", channels=[_public(c) for c in channels(store)], outbox=outbox,
                        recent=store.events(category="alerts", limit=20), **lists)


def register(reg):
    reg.get("alerts", document)
    reg.post("alerts/channels", channels_post)          # audited by the handler with the URL masked
    reg.post("alerts/channels/delete", delete_post)
    reg.post("alerts/test", test_post)
    reg.collector("alerts", deliver, interval=2)
    reg.capability("alerts", True, "webhook and push channels · https or loopback http · no redirects")
