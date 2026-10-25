"""Benchmark runs in the telemetry store (F29).

One feature-owned table, ``bench_runs``: a small JSON ``summary`` (lists, comparisons, share cards) and a
gzip-compressed JSON ``detail`` (per level / item / case data, the A/B receipt, debug traces). A running job
rewrites its row with its live state. Retention: quality 30 runs per target, demo 20 sessions per target, every
other kind 200 runs.
"""

import gzip
import json
import os
import re

from hyperloom import admission, telemetry


KINDS = ("decode", "prefill", "ab", "quality", "tools", "demo")
RUN_ID = re.compile(r"[0-9a-f]{16}")
KEEP_PER_TARGET = {"quality": 30, "demo": 20}
KEEP_PER_KIND = 200
DETAIL_CAP = 8 * 1024 * 1024
DDL = """
CREATE TABLE IF NOT EXISTS bench_runs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_key TEXT NOT NULL, target_label TEXT, state TEXT NOT NULL,
    started REAL NOT NULL, finished REAL, model TEXT, recipe_digest TEXT, summary TEXT NOT NULL, detail BLOB,
    pid INTEGER);
CREATE INDEX IF NOT EXISTS bench_runs_kind ON bench_runs(kind, started);
CREATE INDEX IF NOT EXISTS bench_runs_target ON bench_runs(kind, target_key, started);
"""


def _table(store):
    if not getattr(store, "_bench_runs_ready", False):
        store.ensure_table(DDL)
        store._bench_runs_ready = True
    return store


def _has_table(store):
    if getattr(store, "_bench_runs_ready", False):
        return True
    return bool(store.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bench_runs'"))


def pack(detail):
    data = gzip.compress(json.dumps(detail, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii"), 6)
    if len(data) > DETAIL_CAP:
        raise ValueError("run detail exceeds its size cap")
    return data


def unpack(blob):
    return json.loads(gzip.decompress(blob)) if blob else None


def write(store, run):
    """Insert or replace a run row; run = {id, kind, target_key, target_label, state, started, finished, model,
    recipe_digest, summary, detail}."""
    _table(store)
    from hyperloom.web.api import redact      # idempotent: what is stored equals what any GET returns
    run = {**run, "summary": redact(run.get("summary") or {}), "detail": redact(run.get("detail"))}
    detail = run.get("detail")
    try:
        blob = pack(detail) if detail is not None else None
    except ValueError:
        detail = {"truncated": "the run detail exceeded 8 MiB compressed and was not kept"}
        blob = pack(detail)
    store.execute("INSERT INTO bench_runs(id, kind, target_key, target_label, state, started, finished, model, recipe_digest,"
                  " summary, detail, pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
                  " finished=excluded.finished, model=excluded.model, recipe_digest=excluded.recipe_digest,"
                  " summary=excluded.summary, detail=excluded.detail",
                  (run["id"], run["kind"], run["target_key"], run.get("target_label"), run["state"], run["started"],
                   run.get("finished"), run.get("model"), run.get("recipe_digest"),
                   json.dumps(run.get("summary") or {}, allow_nan=False, sort_keys=True), blob, os.getpid()))


def prune(store, kind, target_key):
    _table(store)
    keep = KEEP_PER_TARGET.get(kind)
    if keep:
        store.execute("DELETE FROM bench_runs WHERE kind=? AND target_key=? AND state!='running' AND id NOT IN"
                      " (SELECT id FROM bench_runs WHERE kind=? AND target_key=? ORDER BY started DESC LIMIT ?)",
                      (kind, target_key, kind, target_key, keep))
    store.execute("DELETE FROM bench_runs WHERE kind=? AND state!='running' AND id NOT IN"
                  " (SELECT id FROM bench_runs WHERE kind=? ORDER BY started DESC LIMIT ?)", (kind, kind, KEEP_PER_KIND))


COLUMNS = "id, kind, target_key, target_label, state, started, finished, model, recipe_digest, summary, pid"


def _row(values, detail=None):
    keys = COLUMNS.split(", ")
    row = dict(zip(keys, values))
    row["summary"] = json.loads(row["summary"])
    pid = row.pop("pid")
    if row["state"] == "running" and (pid is None or not admission._alive(pid)):
        row["state"] = "interrupted"      # its process is gone (shown; the row is rewritten at the next run start)
    if detail is not None:
        row["detail"] = detail
    return row


def runs(store, kind=None, target_key=None, limit=50):
    if store is None or not _has_table(store):
        return []
    sql, args = f"SELECT {COLUMNS} FROM bench_runs", []
    where = []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if target_key:
        where.append("target_key=?")
        args.append(target_key)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started DESC LIMIT ?"
    args.append(int(limit))
    return [_row(values) for values in store.execute(sql, args)]


def get(store, run_id, detail=True):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    if store is None or not _has_table(store):
        raise LookupError("unknown run " + run_id)
    rows = store.execute(f"SELECT {COLUMNS}, detail FROM bench_runs WHERE id=?", (run_id,))
    if not rows:
        raise LookupError("unknown run " + run_id)
    values = rows[0]
    return _row(values[:-1], unpack(values[-1]) if detail else None)


def clear(store, kind=None, run_id=None):
    _table(store)
    if run_id:
        return store.execute("DELETE FROM bench_runs WHERE id=? AND state!='running' RETURNING id", (run_id,))
    if kind:
        return store.execute("DELETE FROM bench_runs WHERE kind=? AND state!='running' RETURNING id", (kind,))
    return store.execute("DELETE FROM bench_runs WHERE state!='running' RETURNING id")


def mark_stale(store):
    """Rows left 'running' by a portal or CLI process that is gone become 'interrupted' (called at run start)."""
    _table(store)
    for run_id, pid in store.execute("SELECT id, pid FROM bench_runs WHERE state='running'"):
        if pid is None or not admission._alive(pid):
            store.execute("UPDATE bench_runs SET state='interrupted' WHERE id=? AND state='running'", (run_id,))


def opened(api):
    """The store for jobs and POSTs (opened when needed)."""
    return _table(telemetry.open_store(api))
