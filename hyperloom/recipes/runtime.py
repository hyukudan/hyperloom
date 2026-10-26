"""CPU-only placement rendering and local realised-context ingestion.

The opt-in decode adapter has no release pin until the release owner publishes one. Neither rendering nor
ingestion describes an engine, reads model data, acquires a grant or starts a process.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from . import content_cache, replica_args
from .reader import RecipeError

PRESET = Path(__file__).resolve().parents[1] / "presets" / "rtx-decode-ds41-placement.json"
CONTEXT_KEYS = ("arch", "device_name", "driver", "platform", "build_sha256", "signature")
CONTEXT_METHODS = frozenset(("nvml-process", "pdh-process-dedicated", "amdsmi-process"))
SUGAR = frozenset(("--anchor", "--gpu-budget-gib", "--device-map"))
SIZING = frozenset(("--arena-gib", "--spares", "--host-spares", "--max-rows", "--max-pinned-gib",
                    "--host-margin-gib", "--draft-device", "--draft-vocab", "--cache", "--layers", "--engram",
                    "--engram-cache-mb", "--chunk", "--chunk-size", "--positions", "--context", "--slots",
                    "--vram-cap-gib", "--host-gib", "--max-sequences", "--max-positions", "--predict",
                    "--pressure-spares", "--draft", "--draft-keep", "--draft-keep-from", "--warmup-graphs",
                    "--test-hooks", "--timing", "--stream-hashes", "--chat-logits", "--records-inline",
                    "--route-stats", "--fuse-hc", "--fuse-shared",
                    "--router-v2", "--router-pf", "--router-pf0", "--pdl", "--pdl-np", "--pdl-regs",
                    "--shared-fork", "--qakv-s1", "--qakv-auto", "--attn-fork", "--ob-expand", "--moe-expand",
                    "--ob-one", "--attn1", "--attn-h1", "--attn-pf", "--hc-cluster", "--hc-partial5",
                    "--moe-v1b3", "--dsrtxk-off", "--fuse-off"))


def preset():
    return content_cache._read(PRESET, "PL_E_CAPABILITY")


def qualified(data):
    if data.get("schema") != "hyperloom/placement-adapter-v1" or data.get("engine") != "strix-cuda-ds41-replica":
        raise RecipeError("PL_E_CAPABILITY unsupported placement engine (schema only for legacy adapters)")
    if not isinstance(data.get("engine_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", data["engine_sha"]):
        raise RecipeError("PL_E_CAPABILITY placement release is unpublished; waiting for a SHA pinned by the release owner")
    if data["engine_sha"] != replica_args.AUDITED_REVISION:
        raise RecipeError("PL_E_CAPABILITY placement release differs from the audited replica parser revision")


def executable(cluster, machine, data=None):
    data = preset() if data is None else data
    qualified(data)
    root = PureWindowsPath(cluster.machines[machine].install_root)
    return root / "cuda" / data["engine_sha"][:12] / "bin" / (data["binary"] + ".exe")


@dataclass(frozen=True)
class Files:
    placement: Path
    manifest: Path
    receipts: Path
    manifest_sha256: str | None = None
    document: dict | None = None
    allocation_flags: dict | None = None

    def read(self):
        document = deepcopy(self.document) if self.document is not None else content_cache._read(self.placement, "PL_E_STALE")
        manifest = content_cache._read(self.manifest, "PL_E_STALE")
        receipts = content_cache._read(self.receipts, "PL_E_STALE")
        if document.get("schema") != "tierstream/placement-v1":
            raise RecipeError("PL_E_SCHEMA expected placement-v1")
        if manifest.get("schema") != content_cache.MANIFEST_SCHEMA:
            raise RecipeError("PL_E_STALE expected cached content-manifest-v1")
        if receipts.get("schema") != "tierstream/receipts-v1":
            raise RecipeError("PL_E_STALE expected cached receipts-v1")
        if self.manifest_sha256 is not None and self.manifest_sha256 != content_cache._sha(manifest):
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

    def publish(self):
        """Publish only the already admitted core bytes after pure rendering has succeeded."""
        self.read()
        if self.document is not None:
            content_cache._write(self.placement, self.document)

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


def decision(cluster, recipe, ctx, bound, effective_sha, plans, admitted):
    """One opt-in RTX-alone decode process, using the admitted core plan and local cache sidecars."""
    from . import commands, reader
    data = preset()
    qualified(data)
    routes = [r for r in recipe["routes"] if r.get("enabled", True)]
    if len(routes) != 1 or routes[0]["phase"] != "decode" or not reader.placement_mode(routes[0]):
        raise RecipeError("PL_E_CAPABILITY the RTX-alone adapter requires one placement decode route")
    route = routes[0]
    plan = plans[route["id"]]
    if (plan.get("engine") != data["engine"] or plan.get("effective_sha256") != effective_sha
            or plan.get("route") != route["id"]):
        raise RecipeError("PL_E_STALE core placement does not identify this decode configuration")
    if any(d.get("host") != ctx.pc for d in plan["devices"]):
        raise RecipeError("PL_E_CAPABILITY RTX-alone placement requires local devices")
    saved = admitted["inputs"]["routes"][route["id"]]
    cache = commands.components_store()
    record = cache.record(saved["components"], selected=saved["selected_content"])
    if (plan.get("build_sha256") != record["reference"]["build_sha256"] or
            plan.get("receipts_sha256") != record["reference"]["receipts_sha256"]):
        raise RecipeError("PL_E_STALE placement differs from the admitted components descriptor")
    manifest = cache.manifests.directory / (record["manifest_sha256"] + ".json")
    receipts = saved.get("receipts")
    if not isinstance(receipts, str):
        raise RecipeError("PL_E_STALE placement requires its local receipts file")
    receipts = str(Path(receipts).resolve())
    files = Files(commands.runtime_dir() / "placements" / (content_cache._sha(plan) + ".json"), manifest,
                  Path(receipts), record["manifest_sha256"], plan, record["reference"].get("flags"))
    selected = record["selected"]
    target = selected.get("model", selected.get("target"))
    if target is None:
        raise RecipeError("PL_E_STALE placement requires selected model content")
    extra = saved.get("argv", [])
    if not isinstance(extra, list) or any(not isinstance(a, str) for a in extra):
        raise RecipeError("PL_E_SCHEMA placement argv must be an array of strings")
    argv = (str(target), *extra)
    binary = executable(cluster, ctx.pc, data)
    render(files, binary=binary, argv=argv, data=data)
    release = content_cache._read(Path(binary).parent.parent / "MANIFEST.json", "PL_E_STALE")
    if (release.get("engine_commit") != data["engine_sha"] or
            release.get("binaries", {}).get(binary.name) != plan["build_sha256"] or not Path(binary).is_file()):
        raise RecipeError("PL_E_STALE placement release binary or manifest is unavailable")
    try:
        with Path(binary).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise RecipeError("PL_E_STALE placement release binary is unreadable") from exc
    if digest != plan["build_sha256"]:
        raise RecipeError("PL_E_STALE placement release binary digest differs from the admitted build")
    return {"placement": True, "decode": {"id": route["id"], "flags": {}}, "prefill": [],
            "service": {"start": True, "why": None}, "running": [], "requirements": [],
            "effective_sha256": effective_sha, "machine": ctx.pc, "argv": list(argv),
            "files": {"placement": str(files.placement), "manifest": str(manifest), "receipts": receipts,
                      "manifest_sha256": files.manifest_sha256, "document": plan,
                      "allocation_flags": files.allocation_flags},
            "supervision": admitted["policies"][route["id"]], "port": admitted["inputs"].get("port", 8096)}


def request(decision):
    files = decision["files"]
    return Request(decision["machine"], Files(Path(files["placement"]), Path(files["manifest"]),
                   Path(files["receipts"]), files["manifest_sha256"], files["document"], files.get("allocation_flags")),
                   decision["supervision"], port=decision.get("port", 8096), argv=tuple(decision["argv"]))


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
    if "placement-v1" not in data.get("capabilities", []):
        raise RecipeError("PL_E_CAPABILITY replica release does not parse placement-v1 flags")
    document = files.read()
    if document.get("engine") != data["engine"]:
        raise RecipeError("PL_E_CAPABILITY placement engine differs from the decode adapter")
    if any(not isinstance(token, str) for token in argv):
        raise RecipeError("PL_E_SCHEMA placement argv must contain only strings")
    conflicts = sorted({token.split("=", 1)[0] for token in argv}
                       & (SUGAR | SIZING | {"--placement", "--content-manifest", "--receipts"}))
    if conflicts:
        raise RecipeError("PL_E_CONFLICT placement owns allocation flags: " + ", ".join(conflicts))
    replica_args.validate(argv, config=document.get("config"), allocation_flags=files.allocation_flags)
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
