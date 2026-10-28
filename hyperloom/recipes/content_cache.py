"""Runtime-only component descriptors and content manifests. Reads here are JSON and stat, never model data.

The explicit, granted probe acquires documents elsewhere. Cache misses never describe an engine or refresh a
manifest. References are sidecars, and are never added to desc-v1 or its identity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .reader import RecipeError

REFERENCE_SCHEMA = "hyperloom/components-ref-v1"
COMPONENT_SCHEMA = "tierstream/components-v1"
MANIFEST_SCHEMA = "tierstream/content-manifest-v1"
SHA = re.compile(r"[0-9a-f]{64}")
MAX_JSON_BYTES = 64 << 20


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecipeError("PL_E_MISSING_DESCRIPTOR invalid cache identity or document") from exc


def _sha(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _digest(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def reference(build_sha256, flags, receipts_sha256):
    """Flags must contain every allocation-affecting option of this engine configuration."""
    if not _digest(build_sha256) or not _digest(receipts_sha256) or not isinstance(flags, dict):
        raise RecipeError("PL_E_MISSING_DESCRIPTOR expected build SHA, allocation flags and receipts SHA")
    return json.loads(_json({"schema": REFERENCE_SCHEMA, "build_sha256": build_sha256,
                            "flags": flags, "receipts_sha256": receipts_sha256}))


def is_reference(value):
    return isinstance(value, dict) and value.get("schema") == REFERENCE_SCHEMA


def _reference(value):
    if not is_reference(value) or set(value) != {"schema", "build_sha256", "flags", "receipts_sha256"}:
        raise RecipeError("PL_E_MISSING_DESCRIPTOR invalid components-v1 cache reference")
    return reference(value["build_sha256"], value["flags"], value["receipts_sha256"])


def _read(path, code):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("cache document too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as exc:
        refresh = "; run `hyperloom probe --manifest`" if code == "PL_E_STALE" else ""
        raise RecipeError(f"{code} cached document missing or invalid{refresh}") from exc


def _write(path, value):
    data = _json(value)
    if len(data) > MAX_JSON_BYTES:
        raise RecipeError("PL_E_MISSING_DESCRIPTOR cache document too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        for attempt in range(10):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                # Windows can briefly retain the replaced destination's handle during concurrent publication.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                    raise
                time.sleep(min(.01 * 2 ** attempt, .1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _selected(paths):
    if (not isinstance(paths, dict) or not paths or any(not isinstance(k, str) or not k for k in paths)
            or any(not isinstance(p, (str, Path)) or not str(p) for p in paths.values())):
        raise RecipeError("PL_E_STALE selected content paths are required")
    return {role: os.path.normcase(str(Path(path).resolve())) for role, path in paths.items()}


class ManifestStore:
    def __init__(self, runtime):
        self.directory = Path(runtime) / "content-manifests"

    def save(self, document):
        """Persist an already acquired manifest. This method does not acquire or refresh content hashes."""
        self._entries(document)
        digest = _sha(document)
        _write(self.directory / (digest + ".json"), document)
        return digest

    @staticmethod
    def _entries(document):
        if not isinstance(document, dict) or document.get("schema") != MANIFEST_SCHEMA:
            raise RecipeError("PL_E_STALE expected a cached content-manifest-v1")
        files = document.get("files")
        if not isinstance(files, list) or not files:
            raise RecipeError("PL_E_STALE content manifest has no files")
        entries = {}
        for entry in files:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or not Path(entry["path"]).is_absolute()
                    or any(type(entry.get(k)) is not int or entry[k] < 0 for k in ("size", "mtime_ns"))
                    or any(not _digest(entry.get(k)) for k in ("sha256", "header_sha256"))):
                raise RecipeError("PL_E_STALE malformed content manifest entry")
            key = os.path.normcase(str(Path(entry["path"]).resolve()))
            if key in entries:
                raise RecipeError("PL_E_STALE duplicate content manifest path")
            entries[key] = entry
        return entries

    def validate(self, digest, selected):
        """Check only selected files against cached size and nanosecond mtime. Never read their contents."""
        if not _digest(digest):
            raise RecipeError("PL_E_STALE missing content manifest; run `hyperloom probe --manifest`")
        document = _read(self.directory / (digest + ".json"), "PL_E_STALE")
        if _sha(document) != digest:
            raise RecipeError("PL_E_STALE cached manifest identity changed")
        entries = self._entries(document)
        for path in _selected(selected).values():
            entry = entries.get(path)
            try:
                stat = os.stat(path)
            except OSError as exc:
                raise RecipeError("PL_E_STALE selected content unavailable; run `hyperloom probe --manifest`") from exc
            if entry is None or (stat.st_size, stat.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                raise RecipeError("PL_E_STALE selected content changed; run `hyperloom probe --manifest`")
        return document


class ComponentsStore:
    def __init__(self, runtime):
        self.directory = Path(runtime) / "components-v1"
        self.manifests = ManifestStore(runtime)

    def save(self, ref, document, *, manifest, selected):
        """Save the output of an explicit granted acquisition, associated with its exact selected content."""
        ref = _reference(ref)
        self._document(ref, document)
        self.manifests.validate(manifest, selected)
        record = {"reference": ref, "document": document, "document_sha256": _sha(document),
                  "manifest_sha256": manifest, "selected": _selected(selected)}
        _write(self.directory / (_sha(ref) + ".json"), record)
        return deepcopy(ref)

    @staticmethod
    def _document(ref, document):
        if (not isinstance(document, dict) or document.get("schema") != COMPONENT_SCHEMA
                or document.get("build_sha256") != ref["build_sha256"]
                or document.get("receipts_sha256") != ref["receipts_sha256"]):
            raise RecipeError("PL_E_MISSING_DESCRIPTOR components build or receipts do not match the cache key")

    def record(self, ref, *, selected):
        ref = _reference(ref)
        record = _read(self.directory / (_sha(ref) + ".json"), "PL_E_MISSING_DESCRIPTOR")
        document = record.get("document")
        self._document(ref, document)
        if record.get("reference") != ref or record.get("document_sha256") != _sha(document):
            raise RecipeError("PL_E_MISSING_DESCRIPTOR cached components identity changed")
        if record.get("selected") != _selected(selected):
            raise RecipeError("PL_E_STALE selected content differs from the cached configuration")
        self.manifests.validate(record.get("manifest_sha256"), selected)
        return deepcopy(record)

    def load(self, ref, *, selected):
        return self.record(ref, selected=selected)["document"]
