"""Read-only launch evidence and portal-observed time to READY."""

import hashlib
import json
import math
import os
import re
import statistics
import threading
import tempfile
from pathlib import Path

from hyperloom.winproc import retry_sharing

from hyperloom.recipes import service


ACTIONS = ("up", "recipes/deploy")
LABELS = (("checks", "Admission / preflight"), ("stopping", "Stop previous profiles"),
          ("lease", "Node leases / GPU turn"), ("processes", "Start processes"),
          ("loading", "Load models"), ("ready", "All READY"))
_history_lock = threading.Lock()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def history_path(api):
    with service.configured(api.config_path):
        return service.runtime_dir() / "launch-history.json"


def config_key(api):
    """Rows are kept per portal configuration: two configurations in one directory share the history file."""
    from hyperloom import orchestrate
    path = orchestrate._config_key(api.config_path)
    if path is None:
        path = os.path.normcase(os.path.abspath(os.fspath(api.config_path)))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def read_history(api):
    try:
        rows = json.loads(retry_sharing(lambda: history_path(api).read_text(encoding="utf-8")))
        if not isinstance(rows, list):
            raise ValueError("history must be a list")
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("profile"), str)
                and finite(r.get("seconds")) and r["seconds"] >= 0], None
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeError, ValueError, TypeError):
        return [], "launch history is unreadable; previous observations preserved"


def history(api):
    return read_history(api)[0]


def estimate(api, profile, retained=None):
    rows, why = read_history(api) if retained is None else retained
    key = config_key(api)
    values = [r["seconds"] for r in rows if r["profile"] == profile and r.get("config") == key][-20:]
    n = len(values)
    return {"seconds": statistics.median(values) if n else None, "n": n,
            "min": min(values) if n else None, "max": max(values) if n else None,
            "label": f"estimate from {n} previous starts on this portal",
            **({"why": why or "no previous successful starts observed on this portal"} if not n else {})}


def completed(result):
    result = result if isinstance(result, dict) else {}
    up = result.get("up", result)
    return up if isinstance(up, dict) else {}


def record_ready(api, job, result, finished):
    """Called only by the launch job worker. Orchestrate returns phase=running after all READY waits."""
    up = completed(result)
    if job.get("action") not in ACTIONS or not isinstance(up, dict) or up.get("phase") != "running":
        return
    profile = up.get("profile") or (result or {}).get("profile") or job["arguments"].get("profile")
    start = job.get("started_at")
    if not profile or not finite(start) or not finite(finished) or finished < start:
        return
    from hyperloom.web.api import redact
    row = redact({"profile": profile, "config": config_key(api), "seconds": finished - start})
    path = history_path(api)
    path.parent.mkdir(parents=True, exist_ok=True)
    with service._file_lock(path.with_suffix(".lock"), thread_lock=_history_lock):
        rows, why = read_history(api)
        if why:
            return {"why": why}
        rows += [row]
        kept, counts = [], {}
        for item in reversed(rows):
            name = (item.get("config"), item["profile"])
            counts[name] = counts.get(name, 0) + 1
            if counts[name] <= 20:
                kept.append({k: item[k] for k in ("profile", "config", "seconds") if k in item})
            if len(kept) >= 500:
                break
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix="launch-history-", suffix=".tmp", delete=False, newline="\n") as stream:
                temporary = Path(stream.name)
                json.dump(list(reversed(kept)), stream)
            retry_sharing(lambda: temporary.replace(path))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def derive(job, progress=None, profile=None, events=(), now=0):
    """Only evidence belonging to this launch is accepted; missing timestamps stay null."""
    progress, profile = progress or {}, profile or {}
    start, finish = job.get("started_at"), job.get("finished_at")
    cutoff = finish if finite(finish) else now
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    up = completed(result)
    name = progress.get("profile") or job.get("arguments", {}).get("profile") or up.get("profile")
    phases = {key: {"id": key, "label": label, "state": "unknown", "started_at": None,
                    "finished_at": None, "detail": None, "why": "phase not observable in recorded sources"}
              for key, label in LABELS}

    def mark(key, state, detail=None, began=None, ended=None):
        phases[key].update(state=state, detail=detail, started_at=began, finished_at=ended)
        phases[key].pop("why", None)

    phase = progress.get("phase")
    launched = service.launch_began(progress)
    success = job.get("state") == "succeeded" and up.get("phase") == "running"
    ended = job.get("state") != "running"
    current = (profile.get("id") == name and finite(profile.get("started_at")) and finite(start)
               and start <= profile["started_at"] <= cutoff)
    processes = (profile.get("processes") or []) if current else []
    leases = (profile.get("leases") or []) if current else []
    result_stops = result.get("stopped") or []
    stopped = progress.get("stopped") or [p.get("profile") if isinstance(p, dict) else p for p in result_stops]
    stop_states = {p.get("profile"): service.stop_outcome(p.get("down"))
                   for p in result_stops if isinstance(p, dict)}
    stop_states.update(progress.get("stop_states") or {})
    confirmed = [p for p in stopped if stop_states.get(p) == "stopped"]
    requested = [p for p in stopped if stop_states.get(p) in ("stop requested", "stop-requested")]
    stopping = progress.get("stopping")
    expected_stops = set(stopped) | set(job.get("arguments", {}).get("stop") or [])
    if phase == "checks":
        mark("checks", "running")
    if launched or phase == "stopping" or success:
        mark("checks", "done")
    if phase in ("checks", "stopping") and not ended:
        for key in ("lease", "processes", "loading", "ready"):
            mark(key, "pending")
    if job["action"] == "up":
        mark("stopping", "skipped", "Plain up does not stop other profiles")
    elif expected_stops or stopping:
        mark("stopping", "done" if not stopping and expected_stops <= set(confirmed) else "running" if not ended else "unknown",
             {"stopped": confirmed, "requested": requested, "stopping": stopping, "stop_states": stop_states})
        if phases["stopping"]["state"] == "unknown":
            phases["stopping"]["why"] = "stops recorded; completion of the stop phase is unavailable"
    elif (launched or success) and (progress.get("stopped") == [] or result.get("stopped") == []
                                  or job.get("arguments", {}).get("stop") == []):
        mark("stopping", "skipped", "No profiles to stop")
    elif phase == "checks" and not ended:
        mark("stopping", "pending")
    if leases and all(l.get("state") in ("acquired", "starting", "ready") for l in leases):
        mark("lease", "done", [{"machine": l.get("machine"), "state": l.get("state")} for l in leases])
    elif leases and any(l.get("state") == "queued" for l in leases):
        mark("lease", "running", "Waiting for GPU turn")

    # Capture time is not an event timestamp. Only timed supervise events can be attributed to this job.
    relevant, untimed = [], False
    for entry in events:
        if entry.get("profile") != name:
            continue
        event = dict(entry)
        try:
            message = json.loads(entry.get("message") or "null")
        except (ValueError, TypeError):
            message = None
        if isinstance(message, dict) and message.get("event") in ("READY", "POISONED", "STOPPED"):
            event.update(event=message["event"], when=message.get("when", message.get("time")),
                         reason=message.get("reason"), role=message.get("process"))
        when = event.get("when")
        if not finite(when):
            untimed = untimed or event.get("event") in ("READY", "POISONED", "STOPPED")
        elif event.get("event") in ("READY", "POISONED", "STOPPED") and finite(start) and start <= when <= cutoff:
            relevant.append(event)
    relevant.sort(key=lambda e: e["when"])
    details, starts, ready_times = [], [], []
    for process in processes:
        p = dict(process)
        began = p.get("start_time")
        if finite(began) and finite(start) and start <= began <= cutoff:
            starts.append(began)
        else:
            began = None
        matches = [e for e in relevant if (e.get("process") == p.get("machine")
                   and sum(q.get("machine") == p.get("machine") for q in processes) == 1)
                   or e.get("role") == p.get("name") and e.get("process") == p.get("machine")]
        last = matches[-1] if matches else {}
        if last.get("event") == "READY":
            p["state"] = "ready"
            ready_times.append(last["when"])
        elif last.get("event") in ("STOPPED", "POISONED"):
            p["state"] = last["event"].lower()
        elif (p.get("state") == "ready" and began is not None and finite(p.get("ready_after_s"))
              and 0 <= p["ready_after_s"] <= cutoff - began):
            ready_times.append(began + p["ready_after_s"])
        details.append({"machine": p.get("machine"), "role": p.get("role") or p.get("name"),
                        "state": p.get("state"), "started_at": began, "ready_after_s": p.get("ready_after_s")})
    # Visible engines alone are not the expected roster: router/service start later.
    declared = profile.get("configuration") or {}
    nodes = declared.get("nodes") or [declared[k] for k in ("rank0", "rank1") if k in declared]
    roles = lambda p: str(p.get("role") or "").lower()
    engines = {p["machine"] for p in details if roles(p) not in ("router", "prefill", "prefill_service", "prefill-service")}
    service_config = declared.get("prefill_service")
    roster = (bool(nodes) and set(nodes) <= engines and any(roles(p) == "router" for p in details)
              and (not service_config or any(roles(p) in ("prefill", "prefill_service", "prefill-service") for p in details)))
    ready = roster and bool(details) and all(p["state"] == "ready" for p in details)
    if starts:
        mark("processes", "done" if len(starts) == len(processes) else "unknown", details, min(starts))
        if phases["processes"]["state"] == "unknown":
            phases["processes"]["why"] = "some process start times are unavailable"
    if details and all(p["state"] in ("starting", "ready") for p in details):
        mark("loading", "done" if ready or success else "running", details, min(starts) if starts else None)
        if not ready and not success:
            mark("ready", "pending")
    if ready or success:
        mark("ready", "done", "All processes reached READY", ended=finish if success else None)
        mark("loading", "done", details or "Launch completed after all READY waits",
             min(starts) if starts else None,
             max(ready_times) if len(ready_times) == len(processes) and processes else finish if success else None)
    if untimed:
        for key in ("processes", "loading", "ready"):
            if phases[key]["state"] == "unknown":
                phases[key]["why"] += "; captured supervise events have no attributable timestamp"
    poisoned = next((p for p in processes if str(p.get("state", "")).lower() == "poisoned"), None)
    poison_event = next((e for e in relevant if e.get("event") == "POISONED"), None)
    failure = None
    if poisoned or poison_event or job.get("state") in ("failed", "cancelled", "canceled"):
        key = "loading" if poisoned or poison_event else next((k for k, p in phases.items() if p["state"] == "running"), None)
        reason = ((poisoned or {}).get("reason") or (poison_event or {}).get("reason")
                  or (poison_event or {}).get("message") or job.get("error")
                  or ("Launch cancelled" if job.get("state") in ("cancelled", "canceled") else "Launch failed; no reason recorded"))
        failure = {"phase": key, "reason": reason}
        if key:
            mark(key, "failed", reason, phases[key]["started_at"], finish)
        else:
            failure["why"] = "failure phase not observable in recorded sources"
        if phase == "failed" and progress.get("launched") is False:
            for key in ("lease", "processes", "loading", "ready"):
                if phases[key]["state"] == "unknown":
                    mark(key, "skipped", "Launch did not pass the hand-over")
    if ended:
        for item in phases.values():
            if item["state"] in ("running", "pending"):
                item.update(state="unknown", why="job ended without phase completion evidence")
    return {"job": job["job"], "profile": name, "action": job["action"], "job_state": job["state"],
            "elapsed_s": max(0, (finish if finite(finish) else now) - start) if finite(start) else None,
            "phases": list(phases.values()), "failure": failure}


def jobs(api):
    with api._job_lock:
        return [dict(j) for j in api._jobs.values() if j.get("action") in ACTIONS]


def launch(api, jid, query, *, retained=None):
    if query or not re.fullmatch(r"[0-9a-fA-F]+", jid):
        raise ValueError("launch accepts a hex job id and no query parameters")
    job = next((j for j in jobs(api) if j["job"] == jid), None)
    if job is None:
        raise LookupError("unknown launch job")
    why = {}
    with service.configured(api.config_path):
        try:
            progress = service.read_progress(jid)
        except (OSError, ValueError):
            progress = None
            why["progress"] = "launch progress is unavailable or unreadable"
    snapshot = api.cached_snapshot() or {}
    name = ((progress or {}).get("profile") or job.get("arguments", {}).get("profile")
            or completed(job.get("result")).get("profile"))
    for source, field in (("profiles", "profiles"), ("logs", "entries")):
        document = snapshot.get(source) or {}
        if document.get(field) is None:
            why[source] = document.get("why") or "snapshot source unavailable"
    profile = next((p for p in (snapshot.get("profiles") or {}).get("profiles") or [] if p.get("id") == name), None)
    result = derive(job, progress, profile, (snapshot.get("logs") or {}).get("entries") or [], api.clock())
    from hyperloom.web.api import redact
    return redact(api.document("launch", **result, eta=estimate(api, result["profile"], retained), why=why))


def latest(api, query):
    if query:
        raise ValueError("launch accepts no query parameters")
    rows = {}
    retained = read_history(api)
    seen = set()
    for job in sorted(jobs(api), key=lambda j: j.get("started_at") or 0, reverse=True):
        name = (job.get("arguments", {}).get("profile") or completed(job.get("result")).get("profile"))
        if job.get("state") != "running" and name and name in seen:
            continue
        try:
            document = launch(api, job["job"], {}, retained=retained)
        except LookupError:  # the bounded job roster can evict a job between reads
            continue
        if document["profile"]:
            seen.add(document["profile"])
            rows.setdefault(document["profile"], document)
    from hyperloom.web.api import redact
    return redact(api.document("launch", launches=list(rows.values())))


def register(reg):
    reg.get("launch", latest)
    reg.get_prefix("launch/", launch)
