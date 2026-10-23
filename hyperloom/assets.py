"""Scheduled asset hashing and the user-scoped integrity cache.

Metadata matches are a convenience cache, not an integrity guarantee. Re-run
``assets verify`` to check content, including changes that preserve metadata.
"""

import json
import re
import tempfile
import time
from pathlib import Path

from . import secrets
from .config import preset


FIELDS = ("bytes", "mtime_ns", "inode", "dev")
LARGE = 64 * 1024 ** 3
SCHEMA = "hyperloom/assets-manifest-v1"
CACHE_NOTE = "Metadata cache is a convenience, not an integrity guarantee; use assets verify to re-hash."


def manifest_path():
    return secrets._directory().parent / "assets-manifest.json"


def read_manifest():
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("assets"), dict):
        raise ValueError("invalid assets manifest; restore or remove assets-manifest.json, then run assets verify")
    for entries in data["assets"].values():
        if not isinstance(entries, dict):
            raise ValueError("invalid assets manifest entries")
        for entry in entries.values():
            if (not isinstance(entry, dict) or any(type(entry.get(f)) is not int for f in FIELDS)
                    or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or not isinstance(entry.get("verified_at"), (int, float))):
                raise ValueError("invalid assets manifest entry; restore or remove assets-manifest.json, then run assets verify")
    return data["assets"]


def write_manifest(entries):
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            secrets._private(temporary)
            stream.write((json.dumps({"schema": SCHEMA, "assets": entries}, indent=2) + "\n").encode("utf-8"))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def profile_assets(cluster, profile, mids=None):
    from . import orchestrate as orch
    _, kind, defaults = orch._profile(cluster, profile)
    names = preset(kind)["assets"]
    return {mid: {name: asset for name, asset in cluster.assets.get(mid, {}).items() if name in names}
            for mid in (defaults if mids is None else mids)}


def _fresh(client, entries):
    from . import orchestrate as orch
    paths = list(dict.fromkeys(asset["path"] for asset in entries.values()))
    if not paths:
        return {}
    reply = orch._call(client, "stat-assets", {"paths": paths})
    return reply.get("assets", {})


def _reason(asset, fresh, entry):
    if fresh.get("error"):
        return fresh["error"]
    if any(type(fresh.get(f)) is not int for f in FIELDS):
        return "asset stat unavailable"
    # bytes 0 = size not pinned in the config (directory assets: vocab, trellis packs).
    if asset["bytes"] and fresh["bytes"] != asset["bytes"]:
        return f"size mismatch (expected {asset['bytes']}, found {fresh['bytes']})"
    if entry is not None:
        changed = [f for f in (*FIELDS, "tree_stat_sha256") if entry.get(f) != fresh.get(f)]
        if changed:
            return "stale manifest: " + ", ".join(changed) + " changed; run assets verify"
        expected = asset.get("sha256", "0" * 64)
        if expected != "0" * 64 and entry["sha256"] != expected:
            return "manifest sha256 differs from config; run assets verify"
    return None


def inspect(cluster, profile, clients, mids=None, *, strict=False):
    from . import orchestrate as orch
    manifest, rows = read_manifest(), []
    selected = profile_assets(cluster, profile, mids)
    for mid, entries in selected.items():
        try:
            fresh = _fresh(clients[mid], entries)
        except (RuntimeError, OSError, ValueError) as exc:
            fresh = {a["path"]: {"error": str(exc)} for a in entries.values()}
        for name, asset in entries.items():
            entry = manifest.get(mid, {}).get(asset["path"])
            current = fresh.get(asset["path"], {})
            reason = _reason(asset, current, entry)
            rows.append({"machine": mid, "asset": name, "path": asset["path"],
                         "verification": "sha256" if entry else "size only",
                         "stale": entry is not None and reason is not None,
                         "ok": reason is None, "reason": reason,
                         "manifest": entry, "stat": current})
    _, kind, _ = orch._profile(cluster, profile)
    if kind == "ds41-pair":
        for name in {row["asset"] for row in rows}:
            ranks = [row for row in rows if row["asset"] == name and row["manifest"] is not None]
            if len({row["manifest"]["sha256"] for row in ranks}) > 1:
                for row in ranks:
                    row.update(ok=False, stale=True, reason="manifest sha256 mismatch between pair ranks")
    if strict:
        bad = next((row for row in rows if not row["ok"]), None)
        if bad:
            raise orch.OrchestrationError(f"{bad['machine']}: asset {bad['asset']} ({bad['path']}): {bad['reason']}")
    return rows


def status(cluster, profile):
    from . import orchestrate as orch
    selected = profile_assets(cluster, profile)
    clients = {mid: orch._client(cluster.machines[mid]) for mid in selected}
    return {"profile": profile, "assets": inspect(cluster, profile, clients), "cache_note": CACHE_NOTE}


def verify(cluster, profile, *, include_large=False, machine=None):
    from . import orchestrate as orch
    selected = profile_assets(cluster, profile)
    if machine is not None:
        if machine not in selected:
            raise ValueError("machine is not part of the profile: " + machine)
        selected = {machine: selected[machine]}
    jobs, skipped, verified = [], [], []
    for mid, entries in selected.items():
        client = orch._client(cluster.machines[mid])
        for name, asset in entries.items():
            if asset["bytes"] >= LARGE and not include_large:
                skipped.append({"machine": mid, "asset": name, "reason": "requires --include-large"})
                continue
            expected = asset["sha256"] if asset["sha256"] != "0" * 64 else None
            payload = {"root": cluster.machines[mid].install_root, "lab_dir": orch._lab(cluster, mid),
                       "path": asset["path"], "bytes": asset["bytes"], "sha256": expected}
            try:
                job = orch._call(client, "verify-model", payload)
                jobs.append((mid, name, asset, client, job["job"]))
            except (RuntimeError, OSError, ValueError, KeyError) as exc:
                raise orch.OrchestrationError(f"{mid}: asset {name} ({asset['path']}): {exc}") from exc
    for mid, name, asset, client, job in jobs:
        try:
            # Large models on idle-priority storage may take several hours.
            result = orch._wait_job(client, cluster.machines[mid].install_root, job, timeout=7 * 86400)
            entry = {f: result[f] for f in (*FIELDS, "sha256")}
            if "tree_stat_sha256" in result:
                entry["tree_stat_sha256"] = result["tree_stat_sha256"]
            entry["verified_at"] = time.time()
            reason = _reason(asset, result, entry)
            if reason or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                raise ValueError(reason or "invalid hash job result")
            # Re-read under a short lock so concurrent verification cannot lose entries.
            from .procs import InstanceLock
            lock = InstanceLock(manifest_path().with_suffix(".lock")).acquire()
            try:
                manifest = read_manifest()
                manifest.setdefault(mid, {})[asset["path"]] = entry
                write_manifest(manifest)
            finally:
                lock.close()
            verified.append({"machine": mid, "asset": name, "path": asset["path"], **entry})
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            raise orch.OrchestrationError(f"{mid}: asset {name} ({asset['path']}): {exc}") from exc
    return {"profile": profile, "verified": verified, "skipped": skipped}
