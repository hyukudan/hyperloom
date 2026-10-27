"""Fixture model library for the browser smoke: scans of the three fixture machines, a verification, four jobs and
the RTX big-job guard with an accepted baseline (no node is contacted)."""

import hashlib
import json
from pathlib import Path


def _unit(name, kind, size, files, status, newest, **manifest):
    return {"name": name, "kind": kind, "bytes": size, "files": files, "newest_mtime": newest, "links": 0,
            "manifest": {"status": status, "listed": files if status == "complete" else max(0, files - 1),
                         "missing": 0, "unlisted": 0 if status == "complete" else 1, "invalid": 0, "conflicts": 0,
                         "manifests": [name + ".sha256"] if status != "none" else [], **manifest}}


def seed(store, now):
    store.put("prefs", "library/roots", {"halo-2": ["/opt/hyperloom/halo-2/models"]})
    store.put("library/inventory", "halo-1", {"machine": "halo-1", "scanned_at": now - 420, "cache": "direct", "roots": [
        {"path": "/opt/models", "source": "config", "free_bytes": 612_000_000_000, "other_files": 2, "truncated": [],
         "units": [_unit("qwen-flash", "dir", 52_400_000_000, 5, "complete", now - 86400 * 3),
                   _unit("ds-trellis-k4", "dir", 118_900_000_000, 43, "complete", now - 86400 * 9),
                   _unit("draft-small.gguf", "file", 1_250_000_000, 1, "partial", now - 86400),
                   _unit("scratch-exports", "dir", 4_100_000_000, 12, "none", now - 3600)]}]})
    store.put("library/inventory", "halo-2", {"machine": "halo-2", "scanned_at": now - 400, "cache": "direct", "roots": [
        {"path": "/opt/models", "source": "config", "free_bytes": 701_000_000_000, "other_files": 0, "truncated": [],
         "units": [_unit("qwen-flash", "dir", 52_400_000_000, 5, "complete", now - 86400 * 3)]},
        {"path": "/opt/hyperloom/halo-2/models", "source": "prefs", "free_bytes": 1_400_000_000_000, "other_files": 0,
         "truncated": ["files"], "units": [
             {"name": "large-archive", "kind": "dir", "bytes": None, "files": None, "newest_mtime": None, "links": None,
              "manifest": {"status": "unknown", "why": "scan bound reached: files"}}]}]})
    store.put("library/inventory", "workstation", {"machine": "workstation", "scanned_at": now - 380, "cache": "unbuffered",
                                                   "roots": [
        {"path": "D:\\models", "source": "prefs", "free_bytes": 2_900_000_000_000, "other_files": 1, "truncated": [],
         "units": [_unit("ds-trellis-k5", "dir", 131_000_000_000, 43, "complete", now - 86400 * 2)]}]})
    store.put("library/verify", "halo-1|/opt/models|qwen-flash", {"at": now - 7200, "state": "succeeded", "result": {
        "unit": "qwen-flash", "entries": 5, "ok": True, "counts": {"ok": 5, "mismatch": 0, "missing": 0, "changed": 0,
                                                                   "conflict": 0}}})
    store.put("library/jobs", "fixture-running", {"id": "fixture-running", "kind": "verify", "state": "running",
                                                  "started": now - 300, "machine": "halo-1", "name": "ds-trellis-k4",
                                                  "node_job": "a" * 32, "progress": "hashed 41 GB",
                                                  "node": {"state": "running", "progress": {"bytes_done": 41 << 30}}})
    store.put("library/jobs", "fixture-failed", {"id": "fixture-failed", "kind": "verify", "state": "failed",
                                                 "started": now - 5400, "finished": now - 4700, "machine": "halo-2",
                                                 "name": "ds-trellis-k5", "node_job": "b" * 32,
                                                 "reason": "paused for more than 10 minutes (a GPU turn is held: "
                                                           "SINGLE)",
                                                 "node": {"state": "paused", "paused": {"reason": "a GPU turn is held: "
                                                                                                  "SINGLE"}}})
    store.put("library/jobs", "fixture-unresolved", {"id": "fixture-unresolved", "kind": "verify",
                                                     "state": "unresolved", "started": now - 3000,
                                                     "finished": now - 2400, "machine": "halo-1",
                                                     "name": "draft-small.gguf",
                                                     "node_job": "c" * 32,
                                                     "reason": "the node job could not be read for 10 minutes; it may "
                                                               "still be running: follow it with hyperloom models job",
                                                     "node": {"state": "paused", "paused": {"reason": "a hyperloom lease "
                                                                                                      "is active"}}})
    store.put("library/jobs", "fixture-verify", {"id": "fixture-verify", "kind": "verify", "state": "succeeded",
                                                 "started": now - 7300, "finished": now - 7200, "machine": "halo-1",
                                                 "name": "qwen-flash", "node": {"state": "succeeded"}})


def patches(directory, now):
    from hyperloom.telemetry.store import Store
    ledger = Path(directory) / "coordination.md"
    ledger.write_text("- 01:00 fixture: PC-BIG start (sample)\n- 01:20 fixture: PC-BIG end (rc 0)\n", encoding="utf-8")
    store = Store(str(Path(directory) / "hyperloom" / "telemetry.sqlite3"))
    try:
        store.put("prefs", "turns/ledger_path", str(ledger))
    finally:
        store.close()
    data = ledger.read_bytes()
    state = Path(directory) / "hyperloom" / "library-ledger.json"
    state.write_text(json.dumps({"identity": [0, 0], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                 "accepted_at": now - 1800}), encoding="utf-8")
    return []
