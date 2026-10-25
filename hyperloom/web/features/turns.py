"""GPU turn queue (F35): who holds each node's GPU, the waiting tickets, recent turns, and the RTX PRO big-job
ledger of the control machine. Read-only.

GET  turns                         per node: holder (cap, time left), verified waiting tickets with estimates, recent
                                   turns; the pair queue of the coordinating node; the big-job ledger
POST turns/settings {ledger_path?, ledger_cap_s?, pair_cap_s?}   (audited) preferences (namespace ``turns``)

The collector ``turns`` (every 5 s) calls the read-only ``turns`` node verb on every lab-mode machine with a lab
directory (one call in flight per machine) and keeps each reply with its fetch time; the GET only composes the
document from those replies. Times on the nodes are clock-only log lines whose dates are inferred; estimates are
labelled as estimates.
"""

from datetime import date, timedelta
import os
import re
import statistics
import threading
import time

from hyperloom.web.features import ops

NAMESPACE = "turns"
SINGLE_CAP_S = 480
PAIR_CAP_DEFAULT_S = 480
LEDGER_CAP_DEFAULT_S = 1800
LEDGER_TAIL = 2 << 20
CONFIRM_BEFORE_S, CONFIRM_AFTER_S = 75.0, 5.0
INTERVAL = 5.0
STALE_AFTER_S = 30.0

_state_lock = threading.Lock()
_replies = {}           # id(api) -> {machine: {"reply", "error", "fetched_at", "offset_s", "offset_uncertainty_s"}}
_running = {}           # (id(api), machine) -> True while a call is in flight


def eligible(cluster):
    """(machines to read, {machine: why not})."""
    chosen, skipped = [], {}
    for mid, machine in cluster.machines.items():
        if machine.os != "linux":
            skipped[mid] = "not a Linux node"
        elif not machine.lab_mode or not machine.lab_dir:
            skipped[mid] = "no lab directory configured (lab_mode with lab_dir)"
        elif machine.access not in ("ssh", "local"):
            skipped[mid] = "agent access is not supported"
        else:
            chosen.append(machine)
    return chosen, skipped


def fetch(api, machines=None, clock=time.time):
    """Read every eligible node once (in parallel, one call in flight per machine); returns {machine: entry}."""
    cluster = api.cluster()
    chosen, _ = eligible(cluster)
    if machines is not None:
        chosen = [m for m in chosen if m.id in machines]
    key = id(api)
    results = {}

    def one(machine):
        sent = clock()
        entry = {"fetched_at": None, "reply": None, "error": None, "offset_s": None, "offset_uncertainty_s": None}
        try:
            reply = ops._node_call(api, machine, "turns", {"lab_dir": machine.lab_dir})
            received = clock()
            if not isinstance(reply, dict) or reply.get("schema") != "hyperloom/node-v1":
                raise ValueError("unexpected reply from the node tool")
            if reply.get("error"):
                raise ValueError(reply["error"])
            entry.update(reply=reply, fetched_at=received)
            if isinstance(reply.get("now"), (int, float)):
                entry["offset_s"] = reply["now"] - (sent + received) / 2
                entry["offset_uncertainty_s"] = (received - sent) / 2
        except Exception as exc:
            entry.update(error=str(exc)[:300], fetched_at=clock())
        results[machine.id] = entry
        with _state_lock:
            _replies.setdefault(key, {})[machine.id] = entry
            _running.pop((key, machine.id), None)

    threads = []
    for machine in chosen:
        with _state_lock:
            if _running.get((key, machine.id)):
                continue            # the previous call to this machine is still running: coalesced
            _running[(key, machine.id)] = True
        thread = threading.Thread(target=one, args=(machine,), name="hyperloom-turns-" + machine.id, daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(15)
    return results


def sample(api, now):
    fetch(api)
    return []


def _median(values):
    values = [v for v in values if isinstance(v, (int, float)) and v >= 0]
    return statistics.median(values) if len(values) >= 3 else None


def holder_view(holder, reply, settings, now_node):
    if not holder:
        return None
    row = dict(holder)
    if not holder.get("parsed"):
        return row
    if holder["kind"] == "single":
        long_ok = (reply.get("long_ok") or {}).get(holder["label"])
        row["cap_s"], row["cap_source"] = (long_ok, "long-ok entry for this label") if long_ok else (
            SINGLE_CAP_S, "single-turn cap (GPU_TURN_MAX can only lower it)")
    else:
        row["cap_s"], row["cap_source"] = settings["pair_cap_s"], "lab rule for pair turns (not enforced by the turn tool)"
    elapsed = max(0.0, now_node - holder["since"])
    row["elapsed_s"] = elapsed
    row["remaining_s"] = row["cap_s"] - elapsed
    row["past_cap"] = elapsed > row["cap_s"]
    return row


def confirm_pairs(coordinator, second, used=None):
    """One-to-one matching of the second node's remote pair attempts to the coordinator's pair starts.

    Times are corrected by the nodes' clock offsets; ambiguous candidates stay unconfirmed. Returns
    (confirmed [(attempt, coordinator turn)], unconfirmed attempts)."""
    attempts = [t for t in second["turns"] if t["kind"] == "pair" and t["side"] == "remote"]
    starts = [t for t in coordinator["turns"] if t["kind"] == "pair" and t["side"] == "local"]
    shift = (coordinator["offset"] or 0.0) - (second["offset"] or 0.0)
    slack = (coordinator["uncertainty"] or 0.0) + (second["uncertainty"] or 0.0) + 1.0
    candidates = {}
    for index, start in enumerate(starts):
        options = [a for a in attempts if a["label"] == start["label"]
                   and start["start"] - CONFIRM_BEFORE_S - slack <= a["start"] + shift <= start["start"] + CONFIRM_AFTER_S + slack]
        if options:
            candidates[index] = max(options, key=lambda a: a["start"])
    claimed = {}
    for index, attempt in candidates.items():
        claimed.setdefault(id(attempt), []).append(index)
    confirmed, taken = [], set()
    for index, attempt in candidates.items():
        same_label = [s for s in starts if s["label"] == starts[index]["label"] and s is not starts[index]
                      and abs(s["start"] - starts[index]["start"]) <= CONFIRM_BEFORE_S + CONFIRM_AFTER_S + 2 * slack]
        if len(claimed[id(attempt)]) == 1 and not same_label:
            confirmed.append((attempt, starts[index]))
            taken.add(id(attempt))
    earliest = coordinator["first_ts"]
    unconfirmed = []
    for attempt in attempts:
        if id(attempt) in taken:
            continue
        row = dict(attempt)
        row["why"] = ("older than the first node's log tail" if earliest is not None and attempt["start"] + shift < earliest
                      else "no matching pair start on the first node (a lock attempt)")
        unconfirmed.append(row)
    return confirmed, unconfirmed


def remote_holder_state(holder, coordinator, confirmed):
    """The second node's `PAIR X (remote)` holder is running only with the whole evidence: the coordinator's holder
    is `PAIR X (local)` and verified, its open run (same pid) is one-to-one confirmed by a remote attempt, and that
    attempt's own clock is the remote holder's (they are written together); otherwise it is a lock attempt."""
    main = coordinator["node"]["holder"] or {}
    if not (main.get("parsed") and main.get("kind") == "pair" and main.get("side") == "local"
            and main.get("label") == holder.get("label") and main.get("alive") and main.get("verified")):
        return "lock attempt (unconfirmed: the first node holds no verified run of this pair)"
    runs = [t for t in coordinator["turns"] if t["kind"] == "pair" and t["side"] == "local" and t["outcome"] == "running"
            and t["label"] == main["label"] and t.get("pid") == main.get("pid")]
    if len(runs) != 1:
        return "lock attempt (unconfirmed: the first node's log does not show this run)"
    matches = [attempt for attempt, start in confirmed if start is runs[0]]
    if len(matches) != 1 or holder.get("since") is None or abs(matches[0]["start"] - holder["since"]) > 1.5:
        return "lock attempt (unconfirmed: no attempt of this node matches the first node's run)"
    return "running (confirmed by the first node)"


def _settings(api):
    stored = ops.pref(api, NAMESPACE, "settings", {}) or {}
    return {"ledger_path": stored.get("ledger_path"), "ledger_cap_s": stored.get("ledger_cap_s", LEDGER_CAP_DEFAULT_S),
            "pair_cap_s": stored.get("pair_cap_s", PAIR_CAP_DEFAULT_S)}


def compose(api, replies, now=None):
    """The turns document from node replies ({machine: entry})."""
    now = api.clock() if now is None else now
    cluster = api.cluster()
    chosen, skipped = eligible(cluster)
    settings = _settings(api)
    nodes, views = [], {}
    for machine in chosen:
        entry = replies.get(machine.id)
        node = {"machine": machine.id, "state": None, "fetched_at": None, "age_s": None, "holder": None,
                "flags": None, "waiting": None, "unverified": None, "recent": None, "stale_tickets": None,
                "unconfirmed_attempts": None, "anomalies": [], "coordinator": None, "boot_id": None, "why": {}}
        nodes.append(node)
        if entry is None:
            node["state"] = "unavailable"
            node["why"]["state"] = "not read yet (the turn collector runs every 5 s)"
            continue
        node["fetched_at"] = entry["fetched_at"]
        node["age_s"] = now - entry["fetched_at"] if entry["fetched_at"] else None
        if entry.get("error"):
            node["state"] = "unavailable"
            node["why"]["state"] = entry["error"]
            continue
        reply = entry["reply"]
        node["state"] = "stale" if node["age_s"] is not None and node["age_s"] > STALE_AFTER_S else "ok"
        if not reply.get("present"):
            node["state"] = "unavailable"
            node["why"].update(reply.get("why") or {})
            continue
        offset = entry.get("offset_s") or 0.0
        now_node = now + offset
        turns = []
        for turn in reply.get("turns") or []:
            if turn.get("duration_s") is not None and not 0 <= turn["duration_s"] <= 86400:
                node["anomalies"].append({**turn, "why": "impossible duration (dates are inferred from clock times)"})
            else:
                turns.append(turn)
        views[machine.id] = {"turns": turns, "offset": entry.get("offset_s"), "uncertainty": entry.get("offset_uncertainty_s"),
                             "first_ts": (reply.get("log") or {}).get("first_ts"), "reply": reply, "node": node,
                             "now_node": now_node}
        node.update(coordinator=bool(reply.get("coordinator")), boot_id=reply.get("boot_id"), flags=reply.get("flags"),
                    stale_tickets=reply.get("stale_tickets"))
        node["holder"] = holder_view(reply.get("holder"), reply, settings, now_node)
        for key, why in (reply.get("why") or {}).items():
            node["why"].setdefault(key, why)
    coordinators = [v for v in views.values() if v["node"]["coordinator"]]
    coordinator = coordinators[0] if len(coordinators) == 1 else None
    pair_queue, pair_why = None, None
    if coordinator is not None:
        pair_queue = (coordinator["reply"].get("queues") or {}).get("pair-q")
    elif len(coordinators) > 1:
        pair_why = "more than one node keeps a pair queue"
    else:
        pair_why = "the coordinating node (the one keeping the pair queue) is not readable"
    pair_durations = [t["duration_s"] for t in (coordinator or {"turns": []})["turns"]
                      if t["kind"] == "pair" and t["side"] == "local" and t["duration_s"] is not None][-20:]
    for mid, view in views.items():
        node, reply = view["node"], view["reply"]
        own = [t for t in view["turns"] if not (t["kind"] == "pair" and t["side"] == "remote")]
        if not node["coordinator"]:
            if coordinator is not None:
                confirmed, unconfirmed = confirm_pairs(coordinator, view)
                own += [{**start, "side": "both nodes", "from": coordinator["node"]["machine"],
                         "confirmed_by": attempt["start_clock"]} for attempt, start in confirmed]
                node["unconfirmed_attempts"] = len(unconfirmed)
                holder = node["holder"]
                if holder and holder.get("kind") == "pair":
                    holder["pair_state"] = remote_holder_state(holder, coordinator, confirmed)
            else:
                node["unconfirmed_attempts"] = len([t for t in view["turns"] if t["kind"] == "pair" and t["side"] == "remote"])
                node["why"]["unconfirmed_attempts"] = "the first node is not readable: pair attempts are not confirmed"
                if node["holder"] and node["holder"].get("kind") == "pair":
                    node["holder"]["pair_state"] = "unconfirmed (the first node is not readable)"
        own.sort(key=lambda t: t["start"])
        node["recent"] = list(reversed(own[-20:]))
        singles = [t["duration_s"] for t in own if t["kind"] == "single" and t["duration_s"] is not None][-20:]
        median_single = _median(singles)
        holder = node["holder"]
        if holder and holder.get("parsed"):
            typical = median_single if holder["kind"] == "single" else _median(pair_durations)
            remaining = None
            if typical is not None:
                remaining = max(0.0, min(typical - holder["elapsed_s"], max(holder["remaining_s"], 0.0)))
        else:
            remaining = 0.0
        waiting = []
        for position, ticket in enumerate((reply.get("queues") or {}).get("single-q") or []):
            row = {**ticket, "position": position + 1, "estimated_start_s": None,
                   "estimate": "estimate: the holder's typical remaining time + the queue position x the median of the "
                               "last turns; pair alternation is not modelled"}
            if remaining is not None and median_single is not None:
                row["estimated_start_s"] = remaining + position * median_single
            waiting.append(row)
        node["waiting"] = waiting
        if median_single is None:
            node["why"]["estimated_start_s"] = "fewer than 3 recent single turns to estimate from"
        node["unverified"] = [t for queue in (reply.get("unverified") or {}).values() for t in queue or []]
    if pair_queue is not None:
        median_pair = _median(pair_durations)
        base = None
        main = coordinator["node"]["holder"]
        if not main:
            base = 0.0
        elif main.get("parsed") and main.get("verified"):
            typical = median_pair if main["kind"] == "pair" else _median(
                [t["duration_s"] for t in coordinator["turns"] if t["kind"] == "single" and t["duration_s"] is not None][-20:])
            if typical is not None:
                base = max(0.0, min(typical - main["elapsed_s"], max(main.get("remaining_s", 0.0), 0.0)))
        position = 0
        rows = []
        for t in pair_queue:
            estimate = None
            if t["kind"] != "gate" and base is not None and median_pair is not None:
                estimate = base + position * median_pair
            if t["kind"] != "gate":
                position += 1
            rows.append({**t, "position": len(rows) + 1, "estimated_start_s": estimate,
                         "estimate": "estimate: the first node's holder's typical remaining time + the pair position x "
                                     "the median of the last pair turns; single turns owed in between are not modelled"})
        pair_queue = rows
    ledger = ledger_view(api, settings, now)
    why = {}
    if not chosen:
        why["nodes"] = "no lab-mode machine with a lab directory is configured"
    if pair_why:
        why["pair_queue"] = pair_why
    return api.document("turns", nodes=nodes, skipped=[{"machine": m, "why": w} for m, w in skipped.items()],
                        pair_queue=pair_queue, ledger=ledger, settings=settings, why=why,
                        rules={"single_cap_s": SINGLE_CAP_S, "pair_cap_s": settings["pair_cap_s"],
                               "confirm_window_s": [-CONFIRM_BEFORE_S, CONFIRM_AFTER_S]},
                        notes=["dates of turn log lines are inferred from clock times",
                               "a waiting ticket is listed when its live process started before the ticket and is a turn "
                               "script; unverified ones (command line unreadable) are listed apart",
                               "estimated starts are estimates"])


def document(api, query):
    if query:
        raise ValueError("turns takes no parameters")
    with _state_lock:
        replies = dict(_replies.get(id(api), {}))
    doc = compose(api, replies)
    if not replies and getattr(api, "sampler", None) is None:
        doc["why"]["nodes"] = "unavailable · the turn collector runs with the portal's samplers (not running here)"
    return doc


# -- RTX PRO big-job ledger --------------------------------------------------------------------------------------
LEDGER_LINE = re.compile(r"\s*[-*]?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.{1,80}?):\s*\**\s*PC-BIG\s+(start|end)\b\**\s*[:.-]?\s*(.*)")
CAP_TEXT = re.compile(r"(?i)\b(?:tope|cap|max|limit|deadline)\s*[:=]?\s*~?\s*(\d{1,5})\s*(s|sec|secs|seconds|min|mins|minutes)\b")
_ledger_cache = {"key": None, "records": None}


def _local_epochs(day, seconds):
    hour, rest = divmod(int(seconds), 3600)
    minute, second = divmod(rest, 60)
    found = set()
    for dst in (0, 1, -1):
        try:
            epoch = time.mktime((day.year, day.month, day.day, hour, minute, second, 0, 0, dst))
        except (OverflowError, ValueError):
            continue
        local = time.localtime(epoch)
        if (local.tm_year, local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec) == (
                day.year, day.month, day.day, hour, minute, second):
            found.add(epoch)
    return found


def _clock_days(values, anchor, tolerance):
    """Inferred epochs for clock seconds in file order, the newest at or before `anchor` (as the turns node verb:
    each value takes the latest reading, that day or the day before, not later than the next-newer one + tolerance)."""
    out, ref, day = [None] * len(values), anchor + tolerance, date.fromtimestamp(anchor)
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        candidates = _local_epochs(day, value) | _local_epochs(day - timedelta(days=1), value)
        if not candidates:
            candidates = {time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)) + value}
        earlier = [c for c in candidates if c <= ref]
        out[index] = chosen = max(earlier) if earlier else min(candidates)
        ref, day = chosen + tolerance, date.fromtimestamp(chosen)
    return out


def parse_ledger(text, anchor):
    """Big-job records [{owner, action, ts, clock, cap_s, cap_source, detail}] in file order."""
    rows = []
    for line in text.splitlines():
        match = LEDGER_LINE.fullmatch(line[:2000])
        if match:
            rows.append(match)
    times = _clock_days([int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0) for m in rows], anchor, 1800)
    records = []
    for match, ts in zip(rows, times):
        detail = match[6].strip()[:300]
        cap = CAP_TEXT.search(detail)
        cap_s = None
        if cap:
            cap_s = int(cap[1]) * (60 if cap[2].lower().startswith("min") else 1)
        records.append({"owner": match[4].strip()[:80], "action": match[5], "ts": ts,
                        "clock": f"{int(match[1]):02d}:{match[2]}" + (f":{match[3]}" if match[3] else ""),
                        "cap_s": cap_s, "detail": detail})
    return records


def read_ledger(path):
    """(text of the last 2 MiB, mtime, size): the file must be a regular file reached without a link."""
    stream = ops.safe_open(path)
    with stream:
        info = os.fstat(stream.fileno())
        start = max(0, info.st_size - LEDGER_TAIL)
        stream.seek(start)
        data = stream.read(LEDGER_TAIL)
    if start:
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return data.decode("utf-8", "replace"), info.st_mtime, info.st_size, info.st_mtime_ns


def ledger_records(path):
    try:
        info = os.stat(path)
        key = (path, info.st_size, info.st_mtime_ns)
    except OSError as exc:
        raise OSError(exc.strerror or str(exc)) from None
    if _ledger_cache["key"] != key:
        text, mtime, _, _ = read_ledger(path)
        _ledger_cache.update(key=key, records=parse_ledger(text, mtime))
    return _ledger_cache["records"]


def ledger_state(records, now, default_cap):
    """Open (holders), stale opens, recent closed and anomalies, computed at `now`."""
    open_by_owner, closed, anomalies = {}, [], []
    for record in records:
        if record["action"] == "start":
            if record["owner"] in open_by_owner:
                anomalies.append({**open_by_owner[record["owner"]], "why": "a second start before an end"})
            open_by_owner[record["owner"]] = record
        else:
            start = open_by_owner.pop(record["owner"], None)
            if start is None:
                anomalies.append({**record, "why": "an end without a start"})
            else:
                closed.append({"owner": record["owner"], "start": start["ts"], "end": record["ts"],
                               "start_clock": start["clock"], "end_clock": record["clock"],
                               "duration_s": record["ts"] - start["ts"], "detail": start["detail"]})
    holders, stale = [], []
    for record in open_by_owner.values():
        cap = record["cap_s"] or default_cap
        row = {**record, "cap_s": cap, "cap_source": "stated in the line" if record["cap_s"] else "default cap",
               "elapsed_s": max(0.0, now - record["ts"])}
        (stale if row["elapsed_s"] > cap else holders).append(row)
    return {"state": "conflict" if len(holders) > 1 else "held" if holders else "free",
            "holders": holders, "stale": stale, "recent": list(reversed(closed[-20:])), "anomalies": anomalies[-20:]}


def ledger_view(api, settings, now):
    path = settings["ledger_path"]
    if not path:
        return {"state": None, "why": "ledger path not configured (turns settings)"}
    try:
        records = ledger_records(path)
    except OSError as exc:
        return {"state": None, "why": "ledger unreadable: " + str(exc)}
    view = ledger_state(records, now, settings["ledger_cap_s"])
    view["note"] = "times are the lines' HH:MM in the control machine's time zone; dates are inferred"
    return view


def save_settings(api, arguments):
    ops.strict(arguments, ("ledger_path", "ledger_cap_s", "pair_cap_s"))
    current = dict(ops.pref(api, NAMESPACE, "settings", {}, store=ops.store_for_write(api)) or {})
    if "ledger_path" in arguments:
        value = arguments["ledger_path"]
        if value in (None, ""):
            current.pop("ledger_path", None)
        else:
            ops.check_path(value, "ledger_path", windows=os.name == "nt")
            current["ledger_path"] = value
    for key in ("ledger_cap_s", "pair_cap_s"):
        if key in arguments:
            value = arguments[key]
            if type(value) is not int or not 60 <= value <= 7200:
                raise ValueError(key + " must be an integer number of seconds in 60..7200")
            current[key] = value
    ops.set_pref(api, NAMESPACE, "settings", current)
    return {"settings": _settings_from(current)}


def _settings_from(stored):
    return {"ledger_path": stored.get("ledger_path"), "ledger_cap_s": stored.get("ledger_cap_s", LEDGER_CAP_DEFAULT_S),
            "pair_cap_s": stored.get("pair_cap_s", PAIR_CAP_DEFAULT_S)}


def register(reg):
    reg.get("turns", document)
    reg.post("turns/settings", save_settings, audited=True)
    reg.collector("turns", sample, interval=INTERVAL)
