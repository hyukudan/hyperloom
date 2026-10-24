"""Saved model-header descriptors: recipe planning never opens a model file implicitly.

`hyperloom recipe probe <profile> --descriptors` reads the headers once (the operator's explicit action) and saves
them; `up --recipe`, `explain` and `validate` plan from the saved copy and refuse when it is missing or stale (the
file's size, mtime or volume changed). Without `--recipe` nothing here is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from .. import modelfiles


class DescriptorError(RuntimeError):
    pass


def _files(path):
    """The files a model path reads (same rule as modelfiles.read), with their identity."""
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix in (".safetensors", ".gguf"))
    else:
        files = [path]
        split = modelfiles.SPLIT.fullmatch(path.name)
        if split:
            files = sorted(p for p in path.parent.iterdir()
                           if p.name.startswith(split["prefix"] + "-") and p.name.endswith("-of-" + split["count"] + ".gguf"))
    out = []
    for f in files:
        st = os.stat(f)
        out.append({"file": str(f), "device": st.st_dev, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def _key(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:32]


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, model):
        return self.directory / (_key(model) + ".json")

    def save(self, model):
        """Read the headers of ``model`` now (explicit) and save them."""
        identity = _files(model)
        metadata, tensors = modelfiles.read(model)
        record = {"model": str(model), "files": identity, "metadata": metadata,
                  "tensors": [[t.name, t.bytes, t.file, t.dtype, list(t.shape)] for t in tensors]}
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self._path(model).with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(self._path(model))
        return {"model": str(model), "files": len(identity), "tensors": len(tensors)}

    def load(self, model):
        """(metadata, tensors) from the saved descriptor; refused when missing or stale. Only file identities are
        checked (stat), never the contents."""
        try:
            record = json.loads(self._path(model).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DescriptorError(f"{model}: no saved header descriptor; run `hyperloom recipe probe <profile> "
                                  "--descriptors`") from exc
        try:
            current = _files(model)
        except OSError as exc:
            raise DescriptorError(f"{model}: unavailable: {exc}") from exc
        if current != record["files"]:
            raise DescriptorError(f"{model}: the model files changed since their headers were saved; run "
                                  "`hyperloom recipe probe <profile> --descriptors`")
        tensors = [modelfiles.Tensor(n, b, f, d, tuple(s)) for n, b, f, d, s in record["tensors"]]
        return record["metadata"], tensors


@contextmanager
def saved_only(store):
    """Inside this block every modelfiles.read() is served from saved descriptors (and refused when there is none)."""
    original = modelfiles.read

    def read(path):
        return store.load(path)
    modelfiles.read = read
    try:
        yield
    finally:
        modelfiles.read = original
