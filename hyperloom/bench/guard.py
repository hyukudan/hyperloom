"""Benchmark guard (F30): the admission rules of contract C11 made visible, read-only.

The admission database is read without writing (a read-only connection, or a plain SELECT on the instance the
portal already holds); expired holds and holds whose owner process is gone are filtered out, never deleted here.
A missing database is "no holds recorded"; an unreadable one is unavailable with the reason.
"""

import json
import os
from pathlib import Path
import sqlite3
import time

from hyperloom import admission

from . import targets


RULES = [
    "A benchmark or demo holds the physical resources of its target: a profile's machines and the router; an ad-hoc "
    "endpoint's declared machines (all of them when it declares none).",
    f"At most one benchmark or demo per resource set and {admission.BENCH_CAP} lab-wide.",
    "No benchmark starts while a deploy, start, install or stop is running; no deploy starts while a benchmark holds "
    "its resources (it is refused with the benchmark's id); a stop cancels the benchmarks on its resources.",
    "Every run has a generated-token budget, a deadline and cancellation; a run started from a page is cancelled "
    "about two minutes after the page stops renewing it.",
    "Benchmarks never take a GPU turn, never deploy and never switch a router policy.",
]
SELECT = "SELECT id, kind, resources, label, pid, started, expires, cancelled FROM holds ORDER BY started"


def _rows(api):
    """(holds, why): live holds read without writing."""
    db = getattr(api, "admission_db", None)
    try:
        if db is not None:
            with db._lock:
                rows = db._db.execute(SELECT).fetchall()
        else:
            path = os.environ.get("HYPERLOOM_ADMISSION_DB") or str(Path(api.config_path).parent / "run" / "admission.sqlite3")
            if path == ":memory:":
                return None, "admission database not shared with this process"
            if not Path(path).exists():
                return [], None
            connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                rows = connection.execute(SELECT).fetchall()
            finally:
                connection.close()
    except sqlite3.Error as exc:
        return None, f"admission database unreadable: {exc}"
    now = time.time()
    holds = []
    for hid, kind, resources, label, pid, started, expires, cancelled in rows:
        if expires < now or not admission._alive(pid):
            continue
        holds.append({"id": hid[:12], "kind": kind, "resources": json.loads(resources), "label": label,
                      "started": started, "expires": expires, "cancelled": cancelled})
    return holds, None


def refusal(holds, resources):
    """Why a benchmark on ``resources`` would be refused now (None: it would be admitted)."""
    blockers = [h for h in holds if h["kind"] in ("core", "stop")]
    if blockers:
        what = "stop" if blockers[0]["kind"] == "stop" else "deploy, start or install"
        return f"a {what} is running ({blockers[0]['label'] or blockers[0]['id']})"
    benches = [h for h in holds if h["kind"] == "bench" and not h["cancelled"]]
    busy = [h for h in benches if set(h["resources"]) & set(resources) or "*" in h["resources"] or "*" in resources]
    if busy:
        return "the target's resources are held by " + (busy[0]["label"] or busy[0]["id"])
    if len(benches) >= admission.BENCH_CAP:
        return f"at most {admission.BENCH_CAP} benchmark or demo runs at a time"
    return None


def document(api, query):
    unknown = set(query) - {"target"}
    if unknown:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(unknown)))
    holds, why_holds = _rows(api)
    jobs = api.read("jobs").get("jobs") or []
    running = [{"job": j.get("id"), "action": j.get("action"), "progress": j.get("progress"),
                "resources": j.get("resources"), "started_at": j.get("started_at"),
                "run_id": (j.get("arguments") or {}).get("run_id")}
               for j in jobs if j.get("state") == "running" and str(j.get("action", "")).startswith("bench/")]
    core = [j for j in jobs if j.get("state") == "running" and not str(j.get("action", "")).startswith("bench/")
            and j.get("action") in ("up", "down", "install", "recipes/deploy", "recipes/probe")]
    why = {}
    if why_holds:
        why["holds"] = why_holds
    preview = None
    if "target" in query:
        kind, _, name = query["target"].partition(":")
        if kind not in ("profile", "target") or not name:
            raise ValueError("target must be profile:<name> or target:<id>")
        parsed = targets.parse({kind: name})
        try:
            resources = sorted(targets.resources(api, parsed, readonly=True))
        except LookupError as exc:
            resources, why["preview"] = None, str(exc).strip("'\"")
        if resources is not None:
            reason = None
            if core:
                reason = f"a deploy, stop or install job is running ({core[0].get('action')})"
            elif holds is None:
                reason = None
                why["preview"] = "admission state unavailable: " + (why_holds or "")
            else:
                reason = refusal(holds, resources)
            preview = {"target": query["target"], "resources": resources, "admitted": reason is None and holds is not None,
                       "refusal": reason}
    return api.document("bench-guard", rules=RULES, cap=admission.BENCH_CAP, holds=holds, running=running,
                        core_jobs=[{"job": j.get("id"), "action": j.get("action")} for j in core], preview=preview,
                        why=why)
