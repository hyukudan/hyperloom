"""CPU-only placement rendering and local realised-context ingestion.

The opt-in decode adapter has no release pin until claudeinfo-release publishes one. Neither rendering nor
ingestion describes an engine, reads model data, acquires a grant or starts a process.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from . import content_cache
from .reader import RecipeError

PRESET = Path(__file__).resolve().parents[1] / "presets" / "rtx-decode-ds41-placement.json"
CONTEXT_KEYS = ("arch", "device_name", "driver", "platform", "build_sha256", "signature")
CONTEXT_METHODS = frozenset(("nvml-process", "pdh-process-dedicated", "amdsmi-process"))
SUGAR = frozenset(("--anchor", "--gpu-budget-gib", "--device-map"))
SIZING = frozenset(("--arena-gib", "--spares", "--host-spares", "--max-rows", "--max-pinned-gib",
                    "--host-margin-gib", "--draft-device", "--draft-vocab", "--cache", "--layers", "--engram",
                    "--engram-cache-mb", "--chunk", "--chunk-size", "--positions", "--context", "--slots",
                    "--vram-cap-gib", "--host-gib", "--max-sequences"))


def preset():
    return content_cache._read(PRESET, "PL_E_CAPABILITY")


def qualified(data):
    if data.get("schema") != "hyperloom/placement-adapter-v1" or data.get("engine") != "ds41-rtx-decode":
        raise RecipeError("PL_E_CAPABILITY unsupported placement engine (schema only for legacy adapters)")
    if not isinstance(data.get("engine_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", data["engine_sha"]):
        raise RecipeError("PL_E_CAPABILITY placement release is unpublished; waiting for claudeinfo-release pinned sha")


@dataclass(frozen=True)
class Files:
    placement: Path
    manifest: Path
    receipts: Path

    def read(self):
        document = content_cache._read(self.placement, "PL_E_STALE")
        manifest = content_cache._read(self.manifest, "PL_E_STALE")
        receipts = content_cache._read(self.receipts, "PL_E_STALE")
        if document.get("schema") != "tierstream/placement-v1":
            raise RecipeError("PL_E_SCHEMA expected placement-v1")
        if manifest.get("schema") != content_cache.MANIFEST_SCHEMA:
            raise RecipeError("PL_E_STALE expected cached content-manifest-v1")
        if receipts.get("schema") != "tierstream/receipts-v1":
            raise RecipeError("PL_E_STALE expected cached receipts-v1")
        if document.get("manifest_sha256") != content_cache._sha(manifest):
            raise RecipeError("PL_E_STALE placement content manifest identity changed")
        if document.get("receipts_sha256") != hashlib.sha256(Path(self.receipts).read_bytes()).hexdigest():
            raise RecipeError("PL_E_STALE placement receipts identity changed")
        entries = content_cache.ManifestStore._entries(manifest)
        for path, entry in entries.items():
            try:
                stat = Path(path).stat()
            except OSError as exc:
                raise RecipeError("PL_E_STALE selected content unavailable; run hyperloom probe --manifest") from exc
            if (stat.st_size, stat.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                raise RecipeError("PL_E_STALE selected content changed; run hyperloom probe --manifest")
        return document

    def argv(self):
        return ["--placement", str(self.placement), "--content-manifest", str(self.manifest),
                "--receipts", str(self.receipts)]


@dataclass(frozen=True)
class Request:
    machine: str
    files: Files
    supervision: dict
    port: int = 8096
    argv: tuple = ()


def visibility(document):
    """Every and only touched device, ordered by full PCI BDF then UUID (never a runtime ordinal)."""
    devices = document.get("devices")
    if not isinstance(devices, list) or not devices:
        raise RecipeError("PL_E_SCHEMA placement has no touched devices")
    seen, pcis = set(), set()
    for device in devices:
        if device.get("kind") != "cuda":
            raise RecipeError("PL_E_CAPABILITY placement decode adapter requires CUDA")
        uuid, pci = device.get("uuid", ""), device.get("pci", "")
        if (not re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid)
                or not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", pci)):
            raise RecipeError("PL_E_UNKNOWN_DEVICE touched CUDA device needs UUID and PCI identity")
        if uuid.lower() in seen or pci.lower() in pcis:
            raise RecipeError("PL_E_SCHEMA duplicate touched physical device")
        seen.add(uuid.lower())
        pcis.add(pci.lower())
    ordered = sorted(devices, key=lambda d: (d["pci"].lower(), d["uuid"].lower()))
    return {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": ",".join(d["uuid"] for d in ordered)}


def render(files, *, binary, argv=(), data=None):
    """One placement file, manifest and receipts, with no legacy sizing translation or inherited device order."""
    data = preset() if data is None else data
    qualified(data)
    document = files.read()
    if document.get("engine") != data["engine"]:
        raise RecipeError("PL_E_CAPABILITY placement engine differs from the decode adapter")
    conflicts = sorted({token.split("=", 1)[0] for token in argv}
                       & (SUGAR | SIZING | {"--placement", "--content-manifest", "--receipts"}))
    if conflicts:
        raise RecipeError("PL_E_CONFLICT placement owns allocation flags: " + ", ".join(conflicts))
    return [str(binary), *argv, *files.argv()], visibility(document), document


def context(document=None):
    document = deepcopy(document) if document is not None else {"schema": "tierstream/context-v1", "entries": []}
    if document.get("schema") != "tierstream/context-v1" or not isinstance(document.get("entries"), list):
        raise RecipeError("PL_E_SCHEMA expected context-v1")
    seen = set()
    for entry in document["entries"]:
        key = _context_key(entry)
        if key in seen:
            raise RecipeError("PL_E_SCHEMA duplicate context six-field key")
        seen.add(key)
        if not isinstance(entry.get("samples"), list):
            raise RecipeError("PL_E_SCHEMA context samples must be an array")
        for sample in entry["samples"]:
            _sample(sample)
    return document


def _context_key(entry):
    if any(not isinstance(entry.get(k), str) or not entry[k] for k in CONTEXT_KEYS):
        raise RecipeError("PL_E_SCHEMA context needs all six identity fields")
    if any(not content_cache._digest(entry[k]) for k in ("signature", "build_sha256")):
        raise RecipeError("PL_E_SCHEMA invalid context build or signature")
    return tuple(entry[k] for k in CONTEXT_KEYS)


def _sample(sample):
    if (not isinstance(sample, dict) or type(sample.get("bytes")) is not int or sample["bytes"] < 0
            or sample.get("method") not in CONTEXT_METHODS):
        raise RecipeError("PL_E_SCHEMA context requires attributable per-process bytes")


def ingest(realised, previous=None):
    """Read checkpoint-1 context observations from realised JSONL; keep chronological samples, never totals.

    Local event shape: {event: 'context', checkpoint: 1, <six identity fields>, ctx_observed, method}.
    Other engine events and warm-up/checkpoint-2 readings do not measure the ledger-zero context.
    """
    document = context(previous)
    entries = {_context_key(e): e for e in document["entries"]}
    try:
        with Path(realised).open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise RecipeError("PL_E_SCHEMA realised event must be an object")
                if event.get("event") != "context" or event.get("checkpoint") != 1:
                    continue
                key = _context_key(event)
                sample = {"bytes": event.get("ctx_observed"), "method": event.get("method")}
                _sample(sample)
                if key not in entries:
                    entries[key] = dict(zip(CONTEXT_KEYS, key), samples=[])
                    document["entries"].append(entries[key])
                entries[key]["samples"].append(sample)
    except (OSError, ValueError) as exc:
        raise RecipeError("PL_E_SCHEMA invalid realised JSONL") from exc
    return document
