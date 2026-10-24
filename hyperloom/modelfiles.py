"""Tensor byte sizes from model file headers (GGUF, safetensors), without reading any weight data.

The VRAM planner sizes what it places from these rows, never from guessed constants: one row per tensor with its
name, its exact byte size in the file and the file it lives in.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import struct


class ModelFileError(ValueError):
    pass


@dataclass(frozen=True)
class Tensor:
    name: str
    bytes: int
    file: str
    dtype: str
    shape: tuple = ()  # safetensors order (outermost first); GGUF order (innermost first), as each file stores it


# ggml type id -> (name, elements per block, bytes per block). Types missing here are sized by the offset of the
# next tensor (exact up to the file's alignment padding).
GGML_TYPES = {
    0: ("F32", 1, 4), 1: ("F16", 1, 2), 2: ("Q4_0", 32, 18), 3: ("Q4_1", 32, 20), 6: ("Q5_0", 32, 22),
    7: ("Q5_1", 32, 24), 8: ("Q8_0", 32, 34), 9: ("Q8_1", 32, 36), 10: ("Q2_K", 256, 84), 11: ("Q3_K", 256, 110),
    12: ("Q4_K", 256, 144), 13: ("Q5_K", 256, 176), 14: ("Q6_K", 256, 210), 15: ("Q8_K", 256, 292),
    16: ("IQ2_XXS", 256, 66), 17: ("IQ2_XS", 256, 74), 18: ("IQ3_XXS", 256, 98), 19: ("IQ1_S", 256, 50),
    20: ("IQ4_NL", 32, 18), 21: ("IQ3_S", 256, 110), 22: ("IQ2_S", 256, 82), 23: ("IQ4_XS", 256, 136),
    24: ("I8", 1, 1), 25: ("I16", 1, 2), 26: ("I32", 1, 4), 27: ("I64", 1, 8), 28: ("F64", 1, 8),
    29: ("IQ1_M", 256, 56), 30: ("BF16", 1, 2), 34: ("TQ1_0", 256, 54), 35: ("TQ2_0", 256, 66),
    39: ("MXFP4", 32, 17),
}
_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
MAX_HEADER = 256 << 20


class _Reader:
    def __init__(self, stream):
        self.stream = stream

    def take(self, n):
        data = self.stream.read(n)
        if len(data) != n:
            raise ModelFileError("truncated GGUF header")
        return data

    def scalar(self, kind):
        fmt = _SCALAR[kind]
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self):
        n = self.scalar(10)
        if n > MAX_HEADER:
            raise ModelFileError("GGUF string too long")
        return self.take(n).decode("utf-8", "replace")

    def value(self, kind):
        if kind == 8:
            return self.string()
        if kind == 9:
            inner, n = self.scalar(4), self.scalar(10)
            if n > MAX_HEADER:
                raise ModelFileError("GGUF array too long")
            return [self.value(inner) for _ in range(n)]
        if kind not in _SCALAR:
            raise ModelFileError(f"unknown GGUF value type {kind}")
        return self.scalar(kind)


def gguf(path):
    """(metadata, tensors) of one GGUF file. Large metadata arrays (vocabularies) are kept; callers use scalars."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as stream:
        r = _Reader(stream)
        if r.take(4) != b"GGUF":
            raise ModelFileError(f"{path}: not a GGUF file")
        version = r.scalar(4)
        if version not in (2, 3):
            raise ModelFileError(f"{path}: GGUF version {version}")
        count, kv_count = r.scalar(10), r.scalar(10)
        metadata = {}
        for _ in range(kv_count):
            key = r.string()
            metadata[key] = r.value(r.scalar(4))
        infos = []
        for _ in range(count):
            name = r.string()
            dims = [r.scalar(10) for _ in range(r.scalar(4))]
            infos.append((name, dims, r.scalar(4), r.scalar(10)))
        alignment = int(metadata.get("general.alignment", 32))
        start = -(-stream.tell() // alignment) * alignment
    ends = sorted(offset for *_, offset in infos) + [size - start]
    tensors = []
    for name, dims, kind, offset in infos:
        elements = 1
        for d in dims:
            elements *= d
        following = ends[ends.index(offset) + 1:]
        room = next((e for e in following if e > offset), size - start) - offset
        if kind in GGML_TYPES:
            dtype, block, per = GGML_TYPES[kind]
            nbytes = elements // block * per
            if nbytes > room:
                raise ModelFileError(f"{path}: tensor {name} overruns the file")
        else:
            dtype, nbytes = f"ggml{kind}", room
        tensors.append(Tensor(name, nbytes, str(path), dtype, tuple(dims)))
    return metadata, tensors


def safetensors(path):
    """(metadata, tensors) of one safetensors file: the 8-byte header length and its JSON only."""
    path = Path(path)
    with open(path, "rb") as stream:
        head = stream.read(8)
        if len(head) != 8:
            raise ModelFileError(f"{path}: truncated safetensors header")
        n = struct.unpack("<Q", head)[0]
        if n > MAX_HEADER:
            raise ModelFileError(f"{path}: safetensors header too large")
        header = json.loads(stream.read(n))
    metadata = header.pop("__metadata__", {}) or {}
    tensors = []
    for name in sorted(header):
        begin, end = header[name]["data_offsets"]
        tensors.append(Tensor(name, end - begin, str(path), header[name]["dtype"], tuple(header[name]["shape"])))
    return metadata, tensors


def read(path):
    """A model path: one GGUF file, a split GGUF (any shard), or a directory of safetensors / GGUF shards.
    Returns (metadata of the first file, every tensor)."""
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix in (".safetensors", ".gguf"))
        if not files:
            raise ModelFileError(f"{path}: no .safetensors or .gguf files")
    else:
        files = [path]
        stem = path.name
        # llama.cpp splits: name-00001-of-00003.gguf
        if "-of-" in stem and stem.endswith(".gguf"):
            prefix = stem[:stem.rindex("-", 0, stem.index("-of-"))]
            files = sorted(p for p in path.parent.iterdir() if p.name.startswith(prefix + "-") and p.suffix == ".gguf")
    metadata, tensors = {}, []
    for i, f in enumerate(files):
        meta, rows = (gguf if f.suffix == ".gguf" else safetensors)(f)
        if i == 0:
            metadata = meta
        tensors.extend(rows)
    return metadata, tensors


def total(tensors):
    return sum(t.bytes for t in tensors)


def fingerprint(path):
    """Names and sizes of the model files: a plan is valid for these exact files."""
    path = Path(path)
    files = sorted(path.iterdir()) if path.is_dir() else [path]
    return [(f.name, os.stat(f).st_size) for f in files if f.is_file()]
