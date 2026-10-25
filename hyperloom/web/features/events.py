"""Activity log (F23): the persisted event log (2,000 events) with filters, paging and clearing.

GET ``events?category=&machine=&severity=&before=&limit=`` pages the log newest first (``before`` is an event id
from the previous page; ``severity`` is a minimum). POST ``events/clear {older_than: 24h|7d|all, confirm}`` clears
it from any portal process: ``confirm`` is the current code for that option from the events document; the codes are
created and renewed by the ``events`` collector (never by a GET) and replaced on every clear.

The ``events`` collector also mirrors the portal's audit log into the activity log (category ``jobs`` for job
records, ``config`` for other writes) from a persisted byte cursor, in the one process that holds the health
evaluator lease. Cleared history never returns: a clear records its cutoff and the mirror skips older audit entries
and refuses a batch read before a clear (compare-and-swap on the cursor and the cutoff).
"""

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets as random
import sqlite3

from hyperloom import telemetry
from hyperloom.telemetry.store import MAX_EVENTS, SEVERITIES
from hyperloom.web.features import health


CONFIRM, CURSOR, BOUNDARY = "events/confirm", "events/cursor", "events/boundary"
OPTIONS = {"24h": 86400.0, "7d": 7 * 86400.0, "all": None}
LABELS = {"24h": "older than 24 h", "7d": "older than 7 d", "all": "all events"}
CODE_TTL_S, CODE_RENEW_S = 600.0, 60.0
FILTER = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
ACTION = re.compile(r"[a-z][a-z0-9/_-]{0,63}")
JOB = re.compile(r"[0-9a-f][0-9a-f-]{7,63}")
STATES = frozenset(("accepted", "running", "succeeded", "completed", "failed", "refused", "cancelled", "stopped"))
READ_CAP, MAX_ENTRIES, LINE_CAP, FIRST_TAIL, FINGERPRINT = 256 * 1024, 500, 64 * 1024, 64 * 1024, 4096
INTERVAL = 10.0
UNAVAILABLE = "activity log unavailable: the telemetry store is not open in this portal process"


# -- the document ---------------------------------------------------------------------------------------------------

def _codes(store, now):
    stored = store.get(CONFIRM, "codes")
    if not isinstance(stored, dict):
        return None
    out = {option: value["code"] for option, value in stored.items()
           if option in OPTIONS and isinstance(value, dict) and isinstance(value.get("code"), str)
           and type(value.get("expires")) in (int, float) and value["expires"] > now}
    return out if len(out) == len(OPTIONS) else None


def document(api, query):
    unknown = set(query) - {"category", "machine", "severity", "before", "limit"}
    if unknown:
        raise ValueError("unknown query arguments (allowed: category, machine, severity, before, limit)")
    for key in ("category", "machine"):
        if key in query and not FILTER.fullmatch(query[key]):
            raise ValueError(key + " must be 1..64 characters of letters, digits and . _ : / -")
    severity = query.get("severity") or None
    if severity is not None and severity not in SEVERITIES:
        raise ValueError("severity must be info, warning or critical")
    try:
        before = int(query["before"]) if "before" in query else None
        limit = int(query.get("limit", 100))
    except ValueError:
        raise ValueError("before and limit must be integers") from None
    if before is not None and before < 1 or not 1 <= limit <= 200:
        raise ValueError("before must be a positive event id and limit 1..200")
    store = telemetry.existing_store(api)
    options = list(OPTIONS)
    if store is None:
        return api.document("events", events=None, next_before=None, categories=None, machines=None,
                            retention=MAX_EVENTS, clear={"options": options, "confirm": None},
                            why={"events": UNAVAILABLE, "confirm": UNAVAILABLE})
    rows = store.events(category=query.get("category"), machine=query.get("machine"), severity=severity, before=before,
                        limit=limit)
    categories = [row[0] for row in store.execute("SELECT DISTINCT category FROM events ORDER BY category LIMIT 64")]
    machines = [row[0] for row in store.execute("SELECT DISTINCT machine FROM events WHERE machine IS NOT NULL "
                                                "ORDER BY machine LIMIT 64")]
    total = store.execute("SELECT COUNT(*) FROM events")[0][0]
    codes = _codes(store, api.clock())
    why = {} if codes else {"confirm": "confirmation codes are being renewed; try again in a few seconds"}
    return api.document("events", events=rows, next_before=rows[-1]["id"] if len(rows) == limit else None,
                        categories=categories, machines=machines, total=total, retention=MAX_EVENTS,
                        clear={"options": options, "labels": LABELS, "confirm": codes}, why=why)


# -- clearing -------------------------------------------------------------------------------------------------------

def _fresh_codes(now):
    return {option: {"code": random.token_hex(8), "expires": now + CODE_TTL_S} for option in OPTIONS}


def clear_statements(store, older_than, now):
    """Delete, record the cutoff the audit mirror must respect, and log the clear (one transaction's statements)."""
    seconds = OPTIONS[older_than]
    cutoff = now if seconds is None else now - seconds
    text = health.stored_text(store, BOUNDARY, "cutoff")
    previous = json.loads(text) if text else {}
    newest = previous.get("cutoff") if type(previous.get("cutoff")) in (int, float) else cutoff
    boundary = {"cutoff": max(cutoff, newest), "at": now}
    delete = ("DELETE FROM events", ()) if seconds is None else ("DELETE FROM events WHERE ts < ?", (cutoff,))
    # The boundary only moves forward: a concurrent clear that moved it meanwhile aborts this one (callers retry).
    return [health.unchanged(BOUNDARY, "cutoff", text), delete, health.upsert_doc(BOUNDARY, "cutoff", boundary, now),
            health.insert_event(now, "config", "info", f"activity log cleared ({LABELS[older_than]})",
                                data={"older_than": older_than})]


def _check_option(older_than):
    if older_than not in OPTIONS:
        raise ValueError("older_than must be 24h, 7d or all")
    return older_than


def clear(store, older_than, now):
    """Clear without a code (the CLI, whose --yes is the confirmation); returns the number of events removed."""
    health.prepare(store)
    _check_option(older_than)
    seconds = OPTIONS[older_than]
    for _ in range(5):
        count = store.execute("SELECT COUNT(*) FROM events" + ("" if seconds is None else " WHERE ts < ?"),
                              () if seconds is None else (now - seconds,))[0][0]
        try:
            health.commit(store, clear_statements(store, older_than, now))
            return count
        except sqlite3.IntegrityError:
            continue
    raise PermissionError("the activity log kept changing while clearing; try again")


def clear_with_code(store, older_than, code, now):
    """The portal path: the code must be the current one for this option; the codes are replaced atomically."""
    for _ in range(5):
        try:
            return _clear_with_code(store, older_than, code, now)
        except _Retry:
            continue
    raise PermissionError("the activity log kept changing while clearing; try again")


class _Retry(Exception):
    pass


def _clear_with_code(store, older_than, code, now):
    health.prepare(store)
    _check_option(older_than)
    text = health.stored_text(store, CONFIRM, "codes")
    stored = json.loads(text) if text else {}
    expected = (stored.get(older_than) or {}) if isinstance(stored, dict) else {}
    if not isinstance(code, str) or not isinstance(expected.get("code"), str) or not hmac.compare_digest(
            code.encode("ascii", "replace"), expected["code"].encode("ascii")) or not expected.get("expires", 0) > now:
        raise ValueError("the confirmation code is wrong or expired; reload the activity log and confirm again")
    seconds = OPTIONS[older_than]
    count = store.execute("SELECT COUNT(*) FROM events" + ("" if seconds is None else " WHERE ts < ?"),
                          () if seconds is None else (now - seconds,))[0][0]
    try:
        health.commit(store, [health.unchanged(CONFIRM, "codes", text), *clear_statements(store, older_than, now),
                              health.upsert_doc(CONFIRM, "codes", _fresh_codes(now), now)])
    except sqlite3.IntegrityError:
        raise _Retry() from None             # the codes or the boundary changed: read again (a used code then fails)
    return count


def clear_post(api, arguments):
    store = telemetry.open_store(api)
    option = arguments.get("older_than") if arguments.get("older_than") in OPTIONS else "[invalid]"
    try:
        if set(arguments) - {"older_than", "confirm"}:
            raise ValueError("events/clear takes older_than and confirm")
        removed = clear_with_code(store, arguments.get("older_than"), arguments.get("confirm"), api.clock())
    except (ValueError, LookupError, PermissionError) as exc:
        api.audit("events/clear", {"older_than": option}, {"state": "refused", "error": str(exc)})
        raise
    result = {"older_than": option, "removed": removed}
    api.audit("events/clear", {"older_than": option}, {"state": "succeeded", "result": result})
    return result


# -- the collector: confirmation codes and the audit mirror ------------------------------------------------------------

def renew_codes(store, owner, generation, now):
    text = health.stored_text(store, CONFIRM, "codes")
    stored = json.loads(text) if text else None
    if isinstance(stored, dict) and all(isinstance(stored.get(o), dict) and stored[o].get("expires", 0) > now + CODE_RENEW_S
                                        for o in OPTIONS):
        return False
    health.commit(store, health.guard(owner, generation, now) + [health.unchanged(CONFIRM, "codes", text),
                                                                 health.upsert_doc(CONFIRM, "codes", _fresh_codes(now), now)])
    return True


def _fingerprint(stream, size):
    length = min(size, FINGERPRINT)
    stream.seek(0)
    return hashlib.sha256(stream.read(length)).hexdigest(), length


def _when(value, fallback):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return fallback


def entry_event(entry, now, cutoff):
    """(statement or None) for one audit entry: allow-listed fields only, never arguments or error text."""
    if not isinstance(entry, dict) or not isinstance(entry.get("action"), str) or not ACTION.fullmatch(entry["action"]):
        return None
    ts = _when(entry.get("when"), now)
    if cutoff is not None and ts < cutoff:
        return None
    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    arguments = entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}
    state = result.get("state") if result.get("state") in STATES else None
    job = result.get("job") if isinstance(result.get("job"), str) and JOB.fullmatch(result["job"]) else None
    profile = health._clean(arguments.get("profile"))
    machine = health._clean(arguments.get("machine"))
    message = " ".join(part for part in (entry["action"], profile and "profile " + profile, machine and "on " + machine,
                                         state, job and f"(job {job[:8]})") if part)
    return health.insert_event(ts, "jobs" if job else "config", "warning" if state == "failed" else "info", message,
                               machine, {"action": entry["action"], "state": state, "job": job})


def mirror(api, store, owner, generation, now):
    """Mirror new audit entries; returns the number of events written."""
    path = Path(api.audit_path)
    cursor_text = health.stored_text(store, CURSOR, "audit")
    boundary_text = health.stored_text(store, BOUNDARY, "cutoff")
    cursor = json.loads(cursor_text) if cursor_text else None
    boundary = json.loads(boundary_text) if boundary_text else {}
    cutoff = boundary.get("cutoff") if type(boundary.get("cutoff")) in (int, float) else None
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return 0
    identity = [info.st_dev, info.st_ino] if info.st_ino else None
    with path.open("rb") as stream:
        if isinstance(cursor, dict):
            same = cursor.get("identity") in (None, identity) or identity is None
            length = cursor.get("fingerprint_length", 0)
            if same and info.st_size >= length:
                stream.seek(0)
                same = hashlib.sha256(stream.read(length)).hexdigest() == cursor.get("fingerprint")
            else:
                same = False
            if same and info.st_size >= cursor.get("offset", 0):
                offset, discarding = cursor["offset"], bool(cursor.get("discarding"))
            else:
                offset, discarding = 0, False                   # rotated or truncated: the new file from its start
        else:
            offset = max(0, info.st_size - FIRST_TAIL)          # first run: recent entries only
            discarding = offset > 0                             # skip the partial line we land in
        stream.seek(offset)
        chunk = stream.read(READ_CAP)
        fingerprint, length = _fingerprint(stream, info.st_size)
    position, statements, skipped = 0, [], 0
    if discarding:
        newline = chunk.find(b"\n")
        if newline < 0:
            position = len(chunk)
        else:
            position, discarding = newline + 1, False
    entries = 0
    while not discarding and entries < MAX_ENTRIES:
        newline = chunk.find(b"\n", position)
        if newline < 0:
            if position == 0 and len(chunk) == READ_CAP:      # one line longer than a whole read: skip it
                position, discarding = len(chunk), True
            break
        line, position = chunk[position:newline], newline + 1
        entries += 1
        if len(line) > LINE_CAP:
            skipped += 1
            continue
        try:
            event = entry_event(json.loads(line.decode("utf-8")), now, cutoff)
        except (UnicodeError, ValueError):
            skipped += 1
            continue
        if event is not None:
            statements.append(event)
    new_cursor = {"offset": offset + position, "discarding": discarding, "identity": identity,
                  "fingerprint": fingerprint, "fingerprint_length": length, "skipped": (cursor or {}).get("skipped", 0) + skipped}
    if new_cursor == cursor:
        return 0
    health.commit(store, health.guard(owner, generation, now)
                  + [health.unchanged(CURSOR, "audit", cursor_text), health.unchanged(BOUNDARY, "cutoff", boundary_text)]
                  + statements + [health.upsert_doc(CURSOR, "audit", new_cursor, now), health.RETENTION])
    return len(statements)


def collect(api, now):
    store = telemetry.open_store(api)
    health.prepare(store)
    owner = health.owner_of(api)
    generation = health.held(store, owner, now)
    if generation is None:
        return []
    renew_codes(store, owner, generation, now)
    mirror(api, store, owner, generation, now)
    return []


def register(reg):
    reg.get("events", document)
    reg.post("events/clear", clear_post)              # audited by the handler (validated values only)
    reg.collector("events", collect, interval=INTERVAL)
    reg.capability("events", True, "activity log · findings, alerts, jobs, leases and configuration changes")
