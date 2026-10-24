"""VRAM placement planner: what an engine keeps on the RTX PRO for a given --vram-cap-gib.

An engine family supplies a memory model (hyperloom/placement/<family>.py) built from its real model files:
- fixed device bytes (CUDA context, workspaces, embeddings/head, state per token x positions), always on the device;
- placeable units (a layer, or the hot experts of a layer) with their exact bytes and a benefit (seconds saved per
  request when the unit is on the device instead of streamed), and an ordering rule (``prefix``: only the first k
  units in model order, as a layer split needs; ``ranked``: any subset, best benefit per byte first);
- where a unit lives when it is not on the device (``pinned`` host RAM, counted against host_gib; ``disk``: free);
- how a set of chosen units becomes the engine's own flags.

The planner is deterministic: the same model files, inputs and cap give the same flags. It never changes what an
engine computes, only where weights live, so the exact paths stay exact (the validation compares tokens).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

GIB = 1 << 30


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Unit:
    key: str
    bytes: int
    benefit: float = 0.0
    host: str = "pinned"  # where it lives when not resident: pinned (host_gib), disk, or pair (another device)


@dataclass
class Model:
    engine: str
    fixed: list  # [(name, bytes)] on the device in every placement
    units: list  # [Unit] in model order
    order: str  # prefix | ranked
    flags: object  # callable(chosen keys in model order, plan) -> {flag: value}
    host_fixed: list = field(default_factory=list)  # [(name, bytes)] pinned host in every placement
    stream: object = None  # callable(streamed units) -> device bytes of the streaming slots (0 when none)
    min_units: int = 0
    notes: list = field(default_factory=list)
    source: dict = field(default_factory=dict)  # model files the sizes came from


@dataclass
class Inputs:
    vram_cap_gib: float
    host_gib: float | None = None  # pinned host the engine may lock; None = not limited here
    margin_gib: float = 2.0
    margin_fraction: float = 0.03


@dataclass
class Plan:
    engine: str
    cap_bytes: int
    margin_bytes: int
    fixed_bytes: int
    stream_bytes: int
    resident: list
    streamed: list
    resident_bytes: int
    pinned_bytes: int
    disk_bytes: int
    peak_bytes: int
    benefit: float
    flags: dict
    fixed: list
    notes: list
    source: dict

    def to_dict(self):
        return {"engine": self.engine, "vram_cap_gib": _gib(self.cap_bytes), "margin_gib": _gib(self.margin_bytes),
                "predicted_peak_gib": _gib(self.peak_bytes), "fixed_gib": _gib(self.fixed_bytes),
                "stream_slots_gib": _gib(self.stream_bytes),
                "resident_gib": _gib(self.resident_bytes), "pinned_host_gib": _gib(self.pinned_bytes),
                "disk_gib": _gib(self.disk_bytes), "resident_units": len(self.resident),
                "streamed_units": len(self.streamed), "flags": self.flags,
                "fixed": [[n, _gib(b)] for n, b in self.fixed], "notes": list(self.notes), "source": self.source}

    def argv(self):
        out = []
        for flag, value in self.flags.items():
            out += [flag, str(value)]
        return out


def _gib(n):
    return round(n / GIB, 3)


def margin_bytes(inputs):
    return int(max(inputs.margin_gib * GIB, inputs.margin_fraction * inputs.vram_cap_gib * GIB))


def plan(model, inputs):
    """Fill the device up to cap - margin with the model's units; the rest stays where each unit lives."""
    if not math.isfinite(inputs.vram_cap_gib) or inputs.vram_cap_gib <= 0:
        raise PlanError("vram cap must be a positive number of GiB")
    cap = int(inputs.vram_cap_gib * GIB)
    margin = margin_bytes(inputs)
    fixed = sum(b for _, b in model.fixed)
    room = cap - margin - fixed
    if room < 0:
        raise PlanError(f"{model.engine}: fixed device memory {_gib(fixed)} GiB + margin {_gib(margin)} GiB "
                        f"exceeds the {inputs.vram_cap_gib:g} GiB cap")
    chosen = _choose(model, room)
    if len(chosen) < model.min_units:
        raise PlanError(f"{model.engine}: only {len(chosen)} units fit under the cap; the engine needs "
                        f"{model.min_units}")
    keys = {u.key for u in chosen}
    resident = [u for u in model.units if u.key in keys]
    streamed = [u for u in model.units if u.key not in keys]
    slots = _stream(model, streamed)
    pinned = sum(u.bytes for u in streamed if u.host == "pinned") + sum(b for _, b in model.host_fixed)
    if inputs.host_gib is not None and pinned > inputs.host_gib * GIB:
        raise PlanError(f"{model.engine}: {_gib(pinned)} GiB must be pinned on the host at this cap, above "
                        f"host_gib {inputs.host_gib:g}; raise the cap or the host budget")
    used = sum(u.bytes for u in resident)
    result = Plan(model.engine, cap, margin, fixed, slots, [u.key for u in resident], [u.key for u in streamed], used,
                  pinned, sum(u.bytes for u in streamed if u.host == "disk"), fixed + slots + used,
                  sum(u.benefit for u in resident), {}, list(model.fixed), list(model.notes), dict(model.source))
    result.flags = dict(model.flags([u.key for u in resident], result))
    return result


def _stream(model, streamed):
    return int(model.stream(streamed)) if model.stream and streamed else 0


def _choose(model, room):
    units = model.units
    if model.order == "prefix":
        # The longest prefix whose bytes plus the slots for the remaining (streamed) units fit.
        total = sum(u.bytes for u in units)
        for k in range(len(units), -1, -1):
            if total + _stream(model, units[k:]) <= room:
                return units[:k]
            if k:
                total -= units[k - 1].bytes
        return []
    if model.order != "ranked":
        raise PlanError(f"unknown unit order {model.order}")
    if sum(u.bytes for u in units) <= room:
        return list(units)
    room -= _stream(model, units)  # something streams: reserve the slots (sized for the largest unit)
    position = {u.key: i for i, u in enumerate(units)}
    # Best benefit per byte first; ties keep model order (deterministic). Greedy is exact for equal-sized units,
    # which is every family's case within a kind (a layer, an expert of a layer).
    ranked = sorted(units, key=lambda u: (-(u.benefit / u.bytes if u.bytes else math.inf), position[u.key]))
    out, used = [], 0
    for unit in ranked:
        if used + unit.bytes <= room:
            out.append(unit)
            used += unit.bytes
    return out


def explain(result):
    """Plain text: what is on the device, why, and what is left where."""
    d = result.to_dict()
    lines = [f"{result.engine}: cap {d['vram_cap_gib']:g} GiB, margin {d['margin_gib']:g} GiB "
             f"-> predicted peak {d['predicted_peak_gib']:g} GiB",
             f"  fixed on device {d['fixed_gib']:g} GiB: " + ", ".join(f"{n} {g:g}" for n, g in d["fixed"]),
             f"  stream slots {d['stream_slots_gib']:g} GiB",
             f"  resident {len(result.resident)} units, {d['resident_gib']:g} GiB" + _span(result.resident),
             f"  not resident {len(result.streamed)} units: pinned host {d['pinned_host_gib']:g} GiB, "
             f"disk {d['disk_gib']:g} GiB" + _span(result.streamed),
             "  flags: " + (" ".join(result.argv()) or "(none)")]
    lines += ["  note: " + n for n in result.notes]
    return "\n".join(lines)


def _span(keys):
    if not keys:
        return ""
    if len(keys) <= 6:
        return " (" + ", ".join(keys) + ")"
    return f" ({keys[0]} .. {keys[-1]})"


def plan_first(models, inputs):
    """The first option (in the model's preference order) that fits; every refusal is reported if none does."""
    if not isinstance(models, (list, tuple)):
        return plan(models, inputs)
    reasons = []
    for option in models:
        try:
            return plan(option, inputs)
        except PlanError as exc:
            reasons.append(str(exc))
    raise PlanError("no placement fits: " + "; ".join(reasons))
