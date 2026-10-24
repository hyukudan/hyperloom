"""Pinned model sources and user-confirmed downloads (SPEC §5.8).

Nothing downloads without a confirmation: ``plan`` asks the node for the exact commands
and a confirmation code; ``start`` runs only when the caller passes that code and the node
recomputes the same plan. Conversions and merges are documented, never run.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from . import orchestrate
from .config import ConfigError, PRESETS, preset, read_json, record
from .paths import repo_root


STATUS = ("download", "unavailable")


def catalog():
    data = read_json(PRESETS / "sources.json")
    record(data, "sources", ("schema", "engine_sha", "note", "profiles"))
    if data["schema"] != "hyperloom/sources-v1":
        raise ConfigError("sources: unknown schema")
    # Sources come from the ledgers at the pins: a pin move needs the catalog re-checked.
    if data["engine_sha"] != read_json(repo_root() / "versions.lock")["components"]["engine"]["commit"]:
        raise ConfigError("sources: catalog is not checked at the pinned engine commit")
    for profile, assets in data["profiles"].items():
        for name, row in assets.items():
            where = f"sources.{profile}.{name}"
            if row.get("status") not in STATUS:
                raise ConfigError(f"{where}: unknown status")
            if row["status"] == "unavailable":
                record(row, where, ("status", "why"))
                continue
            record(row, where, ("status", "repo", "revision", "files", "ledger", "license", "metadata"), ("after",))
            if not re.fullmatch(r"[0-9a-f]{40}", row["revision"]):
                raise ConfigError(f"{where}: revision must be an immutable commit")
            for item in row["files"]:
                record(item, where + ".files", ("file", "bytes", "sha256"))
    return data


def sources(cluster, profile):
    """Every asset of a profile with its pinned source or the reason it has none (read-only)."""
    _, kind, _ = orchestrate._profile(cluster, profile)
    rows = catalog()["profiles"].get(kind, {})
    return {name: rows.get(name, {"status": "unavailable", "why": "no pinned source"})
            for name in preset(kind)["assets"]}


def _request(cluster, profile, asset, machine_id):
    _, _, mids = orchestrate._profile(cluster, profile)
    if machine_id not in mids:
        raise ValueError(f"{machine_id} is not a machine of {profile}")
    row = sources(cluster, profile).get(asset)
    if row is None:
        raise ValueError(f"{profile} has no asset {asset}")
    if row["status"] != "download":
        raise ValueError(f"{asset} is not downloadable: {row['why']}")
    manifest = cluster.assets.get(machine_id, {}).get(asset)
    if manifest is None:
        raise ValueError(f"{machine_id}: register {asset} (path, bytes, sha256) in the asset manifest first")
    if not manifest["bytes"] or set(manifest["sha256"]) == {"0"}:
        raise ValueError(f"{machine_id}: {asset} needs its real bytes and sha256 in the manifest before a download")
    directory = PurePosixPath(manifest["path"]).parent
    machine = cluster.machines[machine_id]
    files = [{"repo": row["repo"], "revision": row["revision"], "file": item["file"], "bytes": item["bytes"],
              "sha256": item["sha256"], "directory": str(directory),
              "destination": str(directory / PurePosixPath(item["file"]).name)} for item in row["files"]]
    return machine, {"root": machine.install_root, "lab_dir": orchestrate._lab(cluster, machine_id), "files": files}, row


def plan(cluster, profile, asset, machine_id):
    machine, payload, row = _request(cluster, profile, asset, machine_id)
    result = orchestrate._call(orchestrate._client(machine), "download-plan", payload)
    return {"machine": machine.id, "asset": asset, "profile": profile, "files": result["files"],
            "disk": result["disk"], "after": row.get("after"), "confirm": result["confirm"],
            "next": f"hyperloom assets download {profile} {asset} --machine {machine.id} --confirm {result['confirm']}"}


def start(cluster, profile, asset, machine_id, confirm):
    if not isinstance(confirm, str) or not re.fullmatch(r"[0-9a-f]{12}", confirm):
        raise ValueError("--confirm takes the 12-character code printed by the plan")
    machine, payload, row = _request(cluster, profile, asset, machine_id)
    result = orchestrate._call(orchestrate._client(machine), "download", dict(payload, confirm=confirm))
    return {"machine": machine.id, "job": result["job"], "log": result["log"], "after": row.get("after")}


def job(cluster, machine_id, job_id, cancel=False):
    machine = cluster.machines[machine_id]
    payload = {"root": machine.install_root, "job": job_id}
    return orchestrate._call(orchestrate._client(machine), "download-cancel" if cancel else "job", payload)
