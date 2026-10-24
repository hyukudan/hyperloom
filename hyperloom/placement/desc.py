"""strix-stream desc-v1 descriptors -> vramplan models, and vramplan plans -> strix-stream plan-v1 tiers.

A new model is a descriptor, not planner code (main 18:33): any engine whose adapter emits `strix-stream/desc-v1`
(strix-stream docs/design.md section 2) is planned here, and the engine runs exactly that placement through
`ss_plan_from_json`. The rule is vramplan's; strix-stream's C++ `ss_plan_make` implements the same rule, and the
golden tests check that both give the same tiers and totals.

Descriptor -> Model (design.md "Field mapping"):
- fixed = fixed_device + every layer's fixed_bytes + non-streamable units (unit_bytes) + workspace + dynamic slots;
- units = every (layer, unit) of streamable layers, key `L<l>.U<u>`, device bytes = unit_bytes rounded up to
  unit_align, host bytes = unit_bytes;
- rings = one class per unit stride; slots = the budget's, else the descriptor's ring_slots, else 2 x the most units
  of any streamable layer with that stride.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..vramplan import Model, PlanError, Unit

SCHEMA = "strix-stream/desc-v1"
PLAN_SCHEMA = "strix-stream/plan-v1"
TIERS = ("pinned", "disk", "pinned|disk", "pair")
LETTER = {"pinned": "P", "disk": "D", "pair": "X"}


def load(source):
    """A desc-v1 descriptor from a path or a parsed object, with the checks the planner relies on."""
    desc = json.loads(Path(source).read_text(encoding="utf-8")) if isinstance(source, (str, Path)) else source
    if not isinstance(desc, dict) or desc.get("schema") != SCHEMA:
        raise PlanError(f"descriptor: expected schema {SCHEMA}")
    if desc.get("order", "ranked") not in ("prefix", "ranked") or desc.get("tiers", "pinned|disk") not in TIERS:
        raise PlanError("descriptor: order must be prefix | ranked and tiers pinned | disk | pinned|disk | pair")
    layers = desc.get("layers")
    if not isinstance(layers, list) or not layers:
        raise PlanError("descriptor: layers must be a nonempty array")
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict) or layer.get("index") != i or layer.get("kind") not in ("dense", "moe", "blob"):
            raise PlanError(f"descriptor: layers[{i}] needs index {i} and kind dense | moe | blob")
        n = _n_units(layer)
        kind = layer["kind"]
        # The same structural rules as strix-stream's desc_validate, so both accept and refuse the same documents.
        if (kind == "dense" and (n or layer.get("parts"))) or (kind == "blob" and n != 1) or (kind == "moe" and n < 1):
            raise PlanError(f"descriptor: layers[{i}]: dense has no units, blob exactly one, moe at least one")
        if kind == "moe" and not 1 <= int(layer.get("top_k", 0)) <= n:
            raise PlanError(f"descriptor: layers[{i}].top_k: 1..n_units")
        if n:
            parts = layer.get("parts") or []
            if not parts or any(int(p.get("bytes", 0)) <= 0 for p in parts):
                raise PlanError(f"descriptor: layers[{i}].parts: a layer with units needs parts of > 0 bytes")
            if "unit_bytes" in layer and int(layer["unit_bytes"]) != sum(int(p["bytes"]) for p in parts):
                raise PlanError(f"descriptor: layers[{i}].unit_bytes differs from the sum of its parts")
        benefit = layer.get("benefit")
        if benefit is not None and (len(benefit) != n or any(not b >= 0 for b in benefit)):
            raise PlanError(f"descriptor: layers[{i}].benefit needs {n} values >= 0")
    return desc


def _unit_bytes(layer):
    if "unit_bytes" in layer:
        return int(layer["unit_bytes"])
    return sum(int(p["bytes"]) for p in layer.get("parts", []))


def _n_units(layer):
    return int(layer.get("n_units", 1 if layer["kind"] == "blob" else 0))


def _stride(layer, align):
    return -(-_unit_bytes(layer) // align) * align


def model(source, *, workspace_bytes=0, dynamic_slots_per_layer=0, ring_slots=0, benefit=None, positions=None):
    """The vramplan Model of a descriptor. `benefit`: one value per unit in global order (e.g. route counts), else
    each layer's `benefit`, else 1. `positions` adds state_bytes_per_position x positions to the fixed bytes."""
    desc = load(source)
    align = int(desc.get("unit_align", 256))
    layers = desc["layers"]
    streamable = {L for L, layer in enumerate(layers) if layer.get("streamable", True) and _n_units(layer)}
    fixed = [(f"fixed_device:{n['name']}", int(n["bytes"])) for n in desc.get("fixed_device", [])]
    fixed.append(("layers fixed_bytes", sum(int(layer.get("fixed_bytes", 0)) for layer in layers)))
    kept = sum(_n_units(layer) * _unit_bytes(layer) for layer in layers if not layer.get("streamable", True))
    if kept:
        fixed.append(("non-streamable units", kept))
    if workspace_bytes:
        fixed.append(("workspace", int(workspace_bytes)))
    if dynamic_slots_per_layer:
        fixed.append(("dynamic slots", sum(min(dynamic_slots_per_layer, _n_units(layers[L])) * _stride(layers[L], align)
                                           for L in sorted(streamable))))
    if positions:
        fixed.append(("state", int(desc.get("state_bytes_per_position", 0)) * positions))
    tiers = desc.get("tiers", "pinned|disk")
    total = sum(_n_units(layer) for layer in layers)
    if benefit is not None and len(benefit) != total:
        raise PlanError(f"descriptor: benefit needs one value per unit ({total})")
    units, g = [], 0
    for L, layer in enumerate(layers):
        for u in range(_n_units(layer)):
            if L in streamable:
                b = benefit[g] if benefit is not None else (layer["benefit"][u] if "benefit" in layer else 1.0)
                units.append(Unit(f"L{L}.U{u}", _stride(layer, align), float(b), host=tiers,
                                  host_bytes=_unit_bytes(layer)))
            g += 1
    slots_for = {}
    for L in streamable:
        stride = _stride(layers[L], align)
        slots_for[stride] = max(slots_for.get(stride, 0), 2 * _n_units(layers[L]))

    def stream(streamed):
        if tiers == "pair":
            return 0
        strides = {u.bytes for u in streamed if u.bytes}
        return sum(s * (ring_slots or int(desc.get("ring_slots", 0)) or slots_for[s]) for s in strides)

    return Model(engine=desc.get("engine") or desc.get("model") or "desc-v1", fixed=fixed, units=units,
                 order=desc.get("order", "ranked"), flags=lambda resident, plan: {}, stream=stream,
                 host_fixed=[(f"host_fixed:{n['name']}", int(n["bytes"])) for n in desc.get("host_fixed", [])],
                 notes=["planned from a strix-stream desc-v1 descriptor; the engine runs it via ss_plan_from_json"],
                 source={"descriptor": desc.get("model"), "units": total})


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
