"""Heterogeneous DS decode placement across the RTX PRO and the Strix pair: candidate plans and predicted tok/s.

main 20:06: every unit gets a cost on each device and the plan chooses where it lives, minimising link crossings
per token. This module compares the plan families the engines can run today, each priced by a time model calibrated
on the measured points of hetero/hdec-report.md (2026-10-21/22, 1 user, R1, greedy, exact, 1 GbE):

- pair only (P15): 27.2 tok/s, every unit on the pair;
- pipeline split: RTX layers [0, K) with every expert resident, pair [K, 40), one crossing per token; per layer
  ~0.30 ms on the RTX and ~0.87 ms on the pair, crossing p50 0.78 ms; calibrated at K=22 = 38.4 tok/s;
- dense + hot experts: attention, dense, shared expert and head of all 40 layers on the RTX (9.55 GiB reserve, W1),
  the hottest experts resident, the rest computed by the pair's expert servers; ms/token = 21 + 0.37 x remote
  layers/token (hdec fit; static 80 GiB: 24.2 remote layers -> 33.6 tok/s; + 16 dynamic copies: 11.0 -> 40.1).
  Remote layers/token = 40 x (1 - layer_all_local), from the held-out hit-rate curve of the routes
  (wave4/vramplan/curves: replica mode = experts chosen from all 40 x 384 by frequency).

Every prediction carries its calibration source; families with no measured point are not ranked. The pair's
weights never move (Strix weights come only from local NVMe, main 18:01): only activations cross the link.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ..vramplan import GIB, PlanError

LAYERS = 40
PAIR_ONLY_MS = 1000 / 27.2
PIPE = {"rtx_layer_ms": 0.30, "pair_layer_ms": 0.87, "crossing_ms": 0.78, "k": 22, "measured_tok_s": 38.4}
HOT = {"base_ms": 21.0, "per_remote_layer_ms": 0.37, "reserve_gib": 9.55, "measured": [(24.2, 33.6), (11.0, 40.1)]}
CROSSING_SAVING_25G_MS = 0.42  # hdec: a 2.5 GbE switch, per pipeline crossing (user decision; not measured on hot)


@dataclass
class Candidate:
    family: str
    flags: dict
    ms_per_token: float
    calibration: str
    detail: dict

    @property
    def tok_s(self):
        return 1000 / self.ms_per_token

    def to_dict(self):
        return {"family": self.family, "flags": self.flags, "ms_per_token": round(self.ms_per_token, 2),
                "tok_s": round(self.tok_s, 1), "calibration": self.calibration, "detail": self.detail}


def _pipe_constant():
    """The per-token time outside the layers, so that K=22 reproduces the measured 38.4 tok/s."""
    k = PIPE["k"]
    layers = PIPE["rtx_layer_ms"] * k + PIPE["pair_layer_ms"] * (LAYERS - k) + PIPE["crossing_ms"]
    return 1000 / PIPE["measured_tok_s"] - layers


def pipeline(k, link="1g"):
    if not 1 <= k < LAYERS:
        raise PlanError(f"pipeline split: K must be in 1..{LAYERS - 1}")
    crossing = PIPE["crossing_ms"] - (CROSSING_SAVING_25G_MS if link == "2.5g" else 0.0)
    ms = _pipe_constant() + PIPE["rtx_layer_ms"] * k + PIPE["pair_layer_ms"] * (LAYERS - k) + crossing
    return Candidate("pipeline", {"--cuts": k, "--layers": k, "--tiers": "D" * k, "tail --first-layer": k}, ms,
                     "hdec K=22 38.4 tok/s; per-layer RTX 0.30 / pair 0.87 ms, crossing 0.78 ms"
                     + (" minus 0.42 ms for 2.5 GbE (not measured)" if link == "2.5g" else ""),
                     {"rtx_layers": k, "pair_layers": LAYERS - k, "crossings_per_token": 1})


def _interp(curve, x):
    pts = sorted(curve)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def hot(expert_gib, curve, dynamic_note=False):
    """Dense + hot experts with `expert_gib` of statically resident experts; curve = [(expert GiB, layer_all_local)]
    held out from the routes (replica mode)."""
    if expert_gib < 0:
        raise PlanError("hot placement: no room for experts after the 9.55 GiB dense reserve")
    local = _interp(curve, expert_gib)
    remote = LAYERS * (1 - local)
    ms = HOT["base_ms"] + HOT["per_remote_layer_ms"] * remote
    return Candidate("dense+hot", {"expert_gib": round(expert_gib, 2)}, ms,
                     "hdec fit 21 + 0.37 ms x remote layers (static 80 GiB 33.6 tok/s, dynamic 40.1 tok/s)",
                     {"remote_layers_per_token": round(remote, 1), "layer_all_local": round(local, 3),
                      "dynamic_copies": "not modelled (measured +16 copies/token: 40.1 tok/s at 80 GiB)"
                      if dynamic_note else None})


def candidates(vram_cap_gib, *, curve, front_k, margin_gib=None, link="1g"):
    """Every family at this cap, best first. front_k: the pipeline K that fits the cap (vramplan ds41-rtx-front)."""
    if not math.isfinite(vram_cap_gib) or vram_cap_gib <= 0:
        raise PlanError("vram cap must be a positive number of GiB")
    margin = margin_gib if margin_gib is not None else max(2.0, 0.03 * vram_cap_gib)
    out = [Candidate("pair-only", {}, PAIR_ONLY_MS, "measured P15 27.2 tok/s", {"rtx": "unused"})]
    if front_k >= 1:
        out.append(pipeline(front_k, link))
    expert_gib = vram_cap_gib - margin - HOT["reserve_gib"]
    if expert_gib >= 0:
        out.append(hot(expert_gib, curve, dynamic_note=True))
    return sorted(out, key=lambda c: c.ms_per_token)


def curve_from_csv(path, mode="replica"):
    """(expert GiB, layer_all_local) points from wave4/vramplan/curves/ds41-hitrate.csv."""
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return [(float(r["expert_gib"]), float(r["layer_all_local"])) for r in csv.DictReader(f) if r["mode"] == mode]
