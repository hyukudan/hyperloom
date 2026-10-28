"""strix-stream desc-v1 descriptors -> vramplan models, and vramplan plans -> strix-stream plan-v1 tiers.

A new model is a descriptor, not planner code (main 18:33): any engine whose adapter emits `strix-stream/desc-v1`
(strix-stream docs/design.md section 2) is planned here, and the engine runs exactly that placement through
`ss_plan_from_json`. The rule is vramplan's; strix-stream's C++ `ss_plan_make` implements the same rule, and the
golden tests check that both give the same tiers and totals on recorded cases.

Descriptor -> Model (design.md "Field mapping"):
- fixed = fixed_device + every layer's fixed_bytes + non-streamable units at the pool stride + workspace + dynamic
  slots + state per position x positions;
- units = every (layer, unit) of streamable layers, key `L<l>.U<u>`; device bytes = unit_bytes rounded up to
  unit_align (the pool stride), pinned bytes = unit_bytes rounded up to 4 KiB (strix-stream's pinned billing since
  8a4ee8a), file bytes = unit_bytes;
- rings = one class per unit stride; slots = the budget's, else the descriptor's ring_slots, else 2 x the most units
  of any streamable layer with that stride.

Validation follows strix-stream's desc_validate: the same keys (plus `x-*`), types, bounds (<= 4096 layers, <= 65536
units, ring_slots <= 2^20, power-of-two alignments), part formats and sources, finite benefits. Known difference:
dynamic slots are billed at their upper bound (min(slots, units) per layer, before residents are chosen), where
strix-stream bills min(slots, non-resident units) after the choice; the tiers are the same, vramplan's reported
peak can only be higher (conservative).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..vramplan import Model, PlanError, Unit

SCHEMA = "strix-stream/desc-v1"
SCHEMAS = (SCHEMA, "tierstream/desc-v1")  # the project is renamed tierstream; both spellings are read, the old one written
PLAN_SCHEMA = "strix-stream/plan-v1"
TIERS = ("pinned", "disk", "pinned|disk", "pair")
LETTER = {"pinned": "P", "disk": "D", "pair": "X"}
HOST_STRIDE = 4096  # strix-stream's pinned tier packs units at 4 KiB strides (plan.cpp kHostStride)
MAX_BYTES = 64 << 20  # descriptor file size read at most
MAX_LAYERS, MAX_UNITS, MAX_RING, MAX_ALIGN = 4096, 1 << 16, 1 << 20, 1 << 20
FORMATS = {"F32", "F16", "BF16", "FP8_E4M3", "FP8_E5M2", "EXL3", "MXFP4", "NVFP4", "Q2_K", "Q3_K", "Q4_K", "Q5_K",
           "Q6_K", "Q8_0", "Q4_0", "Q4_1", "Q5_0", "Q5_1", "IQ1_S", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ3_XXS",
           "IQ3_S", "IQ4_NL", "IQ4_XS", "TQ1_0", "TQ2_0", "I8", "U8", "mixed"}
TOP_KEYS = {"schema", "model", "engine", "fixed_device", "host_fixed", "state_bytes_per_position",
            "workspace_bytes_per_row", "unit_align", "sources", "order", "tiers", "ring_slots", "routes", "layers",
            "lookup_tables"}
LAYER_KEYS = {"index", "kind", "fixed_bytes", "n_units", "top_k", "n_shared", "unit_bytes", "parts", "router", "hash",
              "benefit", "streamable"}


def _keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise PlanError(f"descriptor: {where}: expected an object")
    extra = [k for k in obj if k not in allowed and not str(k).startswith("x-")]
    if extra:
        raise PlanError(f"descriptor: {where}: unknown keys {sorted(map(str, extra))}")


def _u64(obj, key, where, default=0, high=(1 << 64) - 1):
    value = obj.get(key, default)
    if type(value) is not int or not 0 <= value <= high:
        raise PlanError(f"descriptor: {where}.{key}: expected an integer in 0..{high}")
    return value


def _text(obj, key, where):
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise PlanError(f"descriptor: {where}.{key}: expected a string")
    return value


def _list(obj, key, where):
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise PlanError(f"descriptor: {where}.{key}: expected an array")
    return value


def _pow2(value, where, high):
    if not 1 <= value <= high or value & (value - 1):
        raise PlanError(f"descriptor: {where}: expected a power of two in 1..{high}")


def _named(obj, key, where):
    for i, n in enumerate(_list(obj, key, where)):
        _keys(n, {"name", "bytes"}, f"{where}.{key}[{i}]")
        _text(n, "name", f"{where}.{key}[{i}]")
        _u64(n, "bytes", f"{where}.{key}[{i}]")


def load(source):
    """A desc-v1 descriptor from a path (at most 64 MiB) or a parsed object, validated like strix-stream's
    desc_validate. Every problem is a PlanError."""
    if isinstance(source, (str, Path)):
        try:
            with open(source, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except OSError as exc:
            raise PlanError(f"descriptor: {exc}") from exc
        if len(raw) > MAX_BYTES:
            raise PlanError("descriptor: larger than 64 MiB")
        try:
            desc = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise PlanError(f"descriptor: not JSON: {type(exc).__name__}") from exc
    else:
        desc = source
    try:
        _validate(desc)
    except PlanError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, RecursionError) as exc:
        raise PlanError(f"descriptor: malformed: {type(exc).__name__}: {exc}") from exc
    return desc


def load_inputs(source, *, components, store, selected):
    """A legacy desc-v1 plus its separately cached components-v1. The reference is never a descriptor field.

    Return the two documents separately, preserving every desc-v1 byte/hash and legacy plan identity. Cache
    resolution performs stat-only content admission and never runs the engine to obtain a missing descriptor.
    """
    return load(source), store.load(components, selected=selected)


def _validate(desc):
    _keys(desc, TOP_KEYS, "desc")
    if desc.get("schema") not in SCHEMAS:
        raise PlanError(f"descriptor: expected schema {' or '.join(SCHEMAS)}")
    for key in ("model", "engine", "routes"):
        _text(desc, key, "desc")
    if desc.get("order", "ranked") not in ("prefix", "ranked") or desc.get("tiers", "pinned|disk") not in TIERS:
        raise PlanError("descriptor: order must be prefix | ranked and tiers pinned | disk | pinned|disk | pair")
    _named(desc, "fixed_device", "desc")
    _named(desc, "host_fixed", "desc")
    _u64(desc, "state_bytes_per_position", "desc")
    _u64(desc, "workspace_bytes_per_row", "desc")
    _pow2(_u64(desc, "unit_align", "desc", 256), "desc.unit_align", 1 << 62)
    _u64(desc, "ring_slots", "desc", 0, MAX_RING)
    sources = _list(desc, "sources", "desc")
    for i, s in enumerate(sources):
        _keys(s, {"name", "path", "align"}, f"desc.sources[{i}]")
        _text(s, "name", f"desc.sources[{i}]")
        if not isinstance(s.get("path"), str):
            raise PlanError(f"descriptor: desc.sources[{i}].path: expected a string")
        _pow2(_u64(s, "align", f"desc.sources[{i}]", 4096), f"desc.sources[{i}].align", MAX_ALIGN)
    layers = _list(desc, "layers", "desc")
    if not 1 <= len(layers) <= MAX_LAYERS:
        raise PlanError(f"descriptor: layers: 1..{MAX_LAYERS} layers")
    for i, layer in enumerate(layers):
        _layer(layer, i, len(sources))
    for i, t in enumerate(_list(desc, "lookup_tables", "desc")):
        where = f"desc.lookup_tables[{i}]"
        _keys(t, {"name", "source", "offset", "rows", "row_bytes", "placement"}, where)
        if _u64(t, "source", where) >= len(sources) or t.get("placement", "file") not in ("file", "pinned", "device"):
            raise PlanError(f"descriptor: {where}: source out of range or placement not file | pinned | device")
        for key in ("offset", "rows", "row_bytes"):
            _u64(t, key, where)


def _layer(layer, i, n_sources):
    where = f"desc.layers[{i}]"
    _keys(layer, LAYER_KEYS, where)
    if type(layer.get("index")) is not int or layer["index"] != i:
        raise PlanError(f"descriptor: {where}.index: layers must be listed in order 0..n-1")
    kind = layer.get("kind")
    if kind not in ("dense", "moe", "blob"):
        raise PlanError(f"descriptor: {where}.kind: expected dense | moe | blob")
    _u64(layer, "fixed_bytes", where)
    n = _u64(layer, "n_units", where, 1 if kind == "blob" else 0, MAX_UNITS)
    top_k = _u64(layer, "top_k", where, 0, MAX_UNITS)
    _u64(layer, "n_shared", where, 0, MAX_UNITS)
    if "streamable" in layer and type(layer["streamable"]) is not bool:
        raise PlanError(f"descriptor: {where}.streamable: expected true or false")
    parts = _list(layer, "parts", where)
    if (kind == "dense" and (n or parts)) or (kind == "blob" and n != 1) or (kind == "moe" and n < 1):
        raise PlanError(f"descriptor: {where}: dense has no units, blob exactly one, moe at least one")
    if kind == "moe" and not 1 <= top_k <= n:
        raise PlanError(f"descriptor: {where}.top_k: 1..n_units")
    total = 0
    for p_i, part in enumerate(parts):
        pw = f"{where}.parts[{p_i}]"
        _keys(part, {"name", "format", "source", "offset", "stride", "bytes"}, pw)
        _text(part, "name", pw)
        fmt = part.get("format")
        if not isinstance(fmt, str) or not (fmt in FORMATS or fmt.startswith("x-")):
            raise PlanError(f"descriptor: {pw}.format: unknown format {fmt!r}")
        if _u64(part, "source", pw) >= n_sources:
            raise PlanError(f"descriptor: {pw}.source: no such source")
        size = _u64(part, "bytes", pw)
        if size <= 0:
            raise PlanError(f"descriptor: {pw}.bytes: > 0")
        if n > 1 and _u64(part, "stride", pw, size) < size:
            raise PlanError(f"descriptor: {pw}.stride: >= bytes (units overlap)")
        _u64(part, "offset", pw)
        total += size
    if n and not parts:
        raise PlanError(f"descriptor: {where}.parts: a layer with units needs parts")
    if "unit_bytes" in layer and _u64(layer, "unit_bytes", where) != total:
        raise PlanError(f"descriptor: {where}.unit_bytes differs from the sum of its parts")
    benefit = layer.get("benefit")
    if benefit is not None and (not isinstance(benefit, list) or len(benefit) != n or any(
            type(b) not in (int, float) or not math.isfinite(b) or b < 0 for b in benefit)):
        raise PlanError(f"descriptor: {where}.benefit: {n} finite values >= 0")
    hashes = layer.get("hash")
    if hashes is not None and (not isinstance(hashes, list) or len(hashes) != n or any(
            not isinstance(h, str) or len(h) != 16 or any(c not in "0123456789abcdef" for c in h) for h in hashes)):
        raise PlanError(f"descriptor: {where}.hash: one 16-hex-digit xxh64 per unit")
    router = layer.get("router")
    if router is not None:
        _keys(router, {"tensor", "bias", "format", "score", "rows", "cols"}, f"{where}.router")
        if not isinstance(router.get("tensor"), str):
            raise PlanError(f"descriptor: {where}.router.tensor: expected a string")
        rows = _u64(router, "rows", f"{where}.router", 0, MAX_UNITS)
        if rows not in (0, n):
            raise PlanError(f"descriptor: {where}.router.rows: 0 or n_units")
        _u64(router, "cols", f"{where}.router")


def _unit_bytes(layer):
    return sum(p["bytes"] for p in layer.get("parts", []))


def _n_units(layer):
    return layer.get("n_units", 1 if layer["kind"] == "blob" else 0)


def _stride(layer, align):
    return -(-_unit_bytes(layer) // align) * align


def model(source, *, workspace_bytes=0, dynamic_slots_per_layer=0, ring_slots=0, benefit=None, positions=None):
    """The vramplan Model of a descriptor. `benefit`: one value per unit in global order (e.g. route counts), else
    each layer's `benefit`, else 1. `positions` adds state_bytes_per_position x positions to the fixed bytes."""
    desc = load(source)
    for name, value, high in (("workspace_bytes", workspace_bytes, (1 << 64) - 1),
                              ("dynamic_slots_per_layer", dynamic_slots_per_layer, MAX_UNITS),
                              ("ring_slots", ring_slots, MAX_RING), ("positions", positions or 0, 1 << 32)):
        if type(value) is not int or not 0 <= value <= high:
            raise PlanError(f"descriptor plan: {name} must be an integer in 0..{high}")
    align = desc.get("unit_align", 256)
    layers = desc["layers"]
    streamable = {L for L, layer in enumerate(layers) if layer.get("streamable", True) and _n_units(layer)}
    fixed = [(f"fixed_device:{n.get('name', i)}", n["bytes"]) for i, n in enumerate(desc.get("fixed_device", []))]
    fixed.append(("layers fixed_bytes", sum(layer.get("fixed_bytes", 0) for layer in layers)))
    kept = sum(_n_units(layer) * _stride(layer, align) for layer in layers if not layer.get("streamable", True))
    if kept:
        fixed.append(("non-streamable units (pool stride)", kept))
    if workspace_bytes:
        fixed.append(("workspace", workspace_bytes))
    if dynamic_slots_per_layer:
        fixed.append(("dynamic slots (upper bound)", sum(
            min(dynamic_slots_per_layer, _n_units(layers[L])) * _stride(layers[L], align) for L in sorted(streamable))))
    if positions:
        fixed.append(("state", desc.get("state_bytes_per_position", 0) * positions))
    tiers = desc.get("tiers", "pinned|disk")
    total = sum(_n_units(layer) for layer in layers)
    if benefit is not None and (len(benefit) != total or any(
            type(b) not in (int, float) or not math.isfinite(b) or b < 0 for b in benefit)):
        raise PlanError(f"descriptor plan: benefit needs {total} finite values >= 0")
    units, g = [], 0
    for L, layer in enumerate(layers):
        for u in range(_n_units(layer)):
            if L in streamable:
                b = benefit[g] if benefit is not None else (layer["benefit"][u] if "benefit" in layer else 1.0)
                units.append(Unit(f"L{L}.U{u}", _stride(layer, align), float(b), host=tiers,
                                  host_bytes=_stride(layer, HOST_STRIDE), file_bytes=_unit_bytes(layer)))
            g += 1
    slots_for = {}
    for L in streamable:
        stride = _stride(layers[L], align)
        slots_for[stride] = max(slots_for.get(stride, 0), 2 * _n_units(layers[L]))

    def stream(streamed):
        if tiers == "pair":
            return 0
        strides = {u.bytes for u in streamed if u.bytes}
        return sum(s * (ring_slots or desc.get("ring_slots", 0) or slots_for[s]) for s in strides)

    notes = ["planned from a strix-stream desc-v1 descriptor; the engine runs it via ss_plan_from_json"]
    if not positions and desc.get("state_bytes_per_position"):
        notes.append("no positions given: the descriptor's per-position state is not counted")
    if not workspace_bytes and desc.get("workspace_bytes_per_row"):
        notes.append("no workspace given: the engine's workspace is not counted")
    return Model(engine=desc.get("engine") or desc.get("model") or "desc-v1", fixed=fixed, units=units,
                 order=desc.get("order", "ranked"), flags=lambda resident, plan: {}, stream=stream,
                 host_fixed=[(f"host_fixed:{n.get('name', i)}", n["bytes"])
                             for i, n in enumerate(desc.get("host_fixed", []))],
                 notes=notes, source={"descriptor": desc.get("model"), "units": total})


def plan_v1(source, plan):
    """The plan as strix-stream plan-v1: per layer one letter per unit (R resident, P pinned, D disk, X pair), the
    input of ss_plan_from_json, plus vramplan's totals for the record."""
    desc = load(source)
    resident = set(plan.resident)
    rows = []
    for L, layer in enumerate(desc["layers"]):
        letters = []
        for u in range(_n_units(layer)):
            key = f"L{L}.U{u}"
            streamed = layer.get("streamable", True) and key not in resident
            letters.append(LETTER[plan.tiers[key]] if streamed else "R")
        rows.append("".join(letters))
    return {"schema": PLAN_SCHEMA, "engine": plan.engine, "cap_bytes": plan.cap_bytes,
            "margin_bytes": plan.margin_bytes, "peak_bytes": plan.peak_bytes, "pinned_bytes": plan.pinned_bytes,
            "disk_bytes": plan.disk_bytes, "tiers": rows}
