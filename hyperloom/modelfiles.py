"""Tensor byte sizes from model file headers (GGUF, safetensors), without reading any weight data.

The VRAM planner sizes what it places from these rows, never from guessed constants: one row per tensor with its
name, its exact byte size in the file and the file it lives in.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
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
MAX_HEADER = 256 << 20  # bytes of header (metadata + tensor infos) read from one file
MAX_DIMS = 8
SPLIT = re.compile(r"(?P<prefix>.+)-\d{5}-of-(?P<count>\d{5})\.gguf")


class _Reader:
    def __init__(self, stream):
        self.stream = stream

    def take(self, n):
        if self.stream.tell() + n > MAX_HEADER:
            raise ModelFileError("GGUF header larger than 256 MiB")
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
        """Scalars and strings; an array is skipped and recorded as {"array": length} (the planner needs none)."""
        if kind == 8:
            return self.string()
        if kind == 9:
            inner, n = self.scalar(4), self.scalar(10)
            if inner == 9:
                raise ModelFileError("nested GGUF arrays are not supported")
            if inner == 8:
                for _ in range(n):
                    self.take(self.scalar(10))
            elif inner in _SCALAR:
                self.take(n * struct.calcsize(_SCALAR[inner]))
            else:
                raise ModelFileError(f"unknown GGUF value type {inner}")
            return {"array": n}
        if kind not in _SCALAR:
            raise ModelFileError(f"unknown GGUF value type {kind}")
        return self.scalar(kind)


def gguf(path):
    """(metadata, tensors) of one GGUF file. Metadata arrays (vocabularies) are skipped; callers use scalars."""
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
            n_dims = r.scalar(4)
            if n_dims > MAX_DIMS:
                raise ModelFileError(f"{path}: tensor {name} has {n_dims} dimensions")
            dims = [r.scalar(10) for _ in range(n_dims)]
            infos.append((name, dims, r.scalar(4), r.scalar(10)))
        alignment = metadata.get("general.alignment", 32)
        if type(alignment) is not int or alignment <= 0 or alignment & (alignment - 1):
            raise ModelFileError(f"{path}: general.alignment must be a positive power of two")
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
        try:
            header = json.loads(stream.read(n))
        except ValueError as exc:
            raise ModelFileError(f"{path}: safetensors header is not JSON") from exc
    if not isinstance(header, dict):
        raise ModelFileError(f"{path}: safetensors header is not an object")
    data_bytes = path.stat().st_size - 8 - n
    metadata = header.pop("__metadata__", {}) or {}
    tensors = []
    for name in sorted(header):
        entry = header[name]
        offsets = entry.get("data_offsets") if isinstance(entry, dict) else None
        shape = entry.get("shape") if isinstance(entry, dict) else None
        if not (isinstance(offsets, list) and len(offsets) == 2 and all(type(x) is int for x in offsets)
                and 0 <= offsets[0] <= offsets[1] <= data_bytes and isinstance(entry.get("dtype"), str)
                and isinstance(shape, list) and all(type(x) is int and x >= 0 for x in shape)):
            raise ModelFileError(f"{path}: malformed safetensors entry {name}")
        tensors.append(Tensor(name, offsets[1] - offsets[0], str(path), entry["dtype"], tuple(shape)))
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
        # A split GGUF: name-00001-of-00003.gguf (every shard of the same name and count).
        split = SPLIT.fullmatch(path.name)
        if split:
            same = re.compile(re.escape(split["prefix"]) + r"-\d{5}-of-" + split["count"] + r"\.gguf")
            files = sorted(p for p in path.parent.iterdir() if same.fullmatch(p.name))
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
