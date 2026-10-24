"""DeepSeek V4.1 Flash on the RTX PRO: three placements, all sized from the GGUF header.

prefill (strix-cuda-ds41-prefill-service, engine 9211b07e): the service plans its own expert residency from
  --vram-cap-gib (src/cuda/ds41_prefill_driver.cu:1491-1606): budget = cap - used_before_experts - reserve, streamed
  experts per layer by rows at --positions (the cone: decoder layers 20-39 compute only their suffix rows), the rest
  resident; streamed experts pinned on the host in ascending layer order while --host-gib lasts, then read from the
  file. Its cap excludes the CUDA context, while hyperloom's watchdog measures the whole process, so the plan passes
  the engine a cap below the guard's. `predict_prefill` replays the engine's plan to predict per-layer residency,
  peak VRAM and pinned host, and picks --host-gib no larger than what is streamed.
front (strix-cuda-ds41-pipeline-front, D-064): RTX layers [0, K) with every expert on the device (--tiers D..D);
  the pair runs [K, 40) (strix-deepseek41-rank --role pipeline-stage --first-layer K). K = the longest layer prefix
  that fits.
hot (strix-cuda-ds41-hot-decode MODEL PLACEMENT.json): resident experts chosen by measured routing frequency (a
  routes file: per token, "layer:id,id,..."); output = a tensormeld/hot-expert-placement-v1 file.
Placement never changes bits (per-expert residency == whole layers, cuda_ds41_prefill.hpp:184-186; the split is
bitwise without trellis, DECISIONS D-064; hot mode 256/256).
"""
from __future__ import annotations

from collections import Counter
import json
import math
import re

from .. import modelfiles
from ..vramplan import GIB, Model, Plan, PlanError, Unit, check_peak

LAYERS, EXPERTS, FIRST_DECODER, WINDOW, WINDOW_ROWS = 40, 384, 20, 128, 64
# Measured (hetero/w2/e2e/alias-*.jsonl, exact profile): device bytes in use before the routed experts (dense
# weights, row buffers, workspaces, attention state; CUDA context excluded) by --positions, GiB.
USED_BEFORE_EXPERTS = [(512, 11.10), (2048, 11.46), (8192, 12.89), (32768, 18.36)]
# free_at_start_gib: cudaMemGetInfo free after the service's own context, measured 93.09 of 95.57 GiB (TTFT exact run);
# context_gib: the share of that the process itself holds (what the watchdog sees). NOT measured yet: ~2.5 GiB is gone
# before planning (context + display + driver); the margin (2.7 GiB at 90) covers the difference today. Re-check
# against the on-device measurement before shrinking any margin.
DEFAULTS = {"context_gib": 1.0, "reserve_gib": 1.0, "slots": 2, "free_at_start_gib": 93.09}
# Measured: the decode FRONT at K=22 (positions 2600) uses 96.0 GB of the PRO (hetero/hdec-report.md section 6.3b).
FRONT_MEASURED = {"k": 22, "device_bytes": 96.0e9, "positions": 2600}
HOT_RESERVE_GIB = 9.55  # measured W1 plan: dense + state + buffers of the hot decoder (hetero/w1-report.md)


def sizes(model_gguf):
    """Per layer: routed expert bytes (all experts) and per-expert bytes, dense+shared bytes; globals."""
    _, tensors = modelfiles.read(model_gguf)
    exps, dense, engram, glob = Counter(), Counter(), 0, 0
    for t in tensors:
        m = re.match(r"blk\.(\d+)\.(.*)", t.name)
        if not m:
            glob += t.bytes
        elif "_exps" in m.group(2):
            exps[int(m.group(1))] += t.bytes
        elif "engram_embd" in m.group(2):
            engram += t.bytes  # stays in the file (NVMe), read per row
        else:
            dense[int(m.group(1))] += t.bytes
    if sorted(exps) != list(range(LAYERS)) or any(v % EXPERTS for v in exps.values()):
        raise modelfiles.ModelFileError(f"{model_gguf}: expected {LAYERS} layers of {EXPERTS} routed experts")
    per_expert = {exps[L] // EXPERTS for L in exps}
    if len(per_expert) != 1:
        raise modelfiles.ModelFileError(f"{model_gguf}: routed experts differ in size across layers")
    return {"expert_bytes": per_expert.pop(), "layer_experts": [exps[L] for L in range(LAYERS)],
            "layer_dense": [dense[L] for L in range(LAYERS)], "globals": glob, "engram_file": engram}


def _interp(points, x):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    (x0, y0), (x1, y1) = points[-2:]
    return y1 + (y1 - y0) * (x - x1) / (x1 - x0)


def cone_rows(positions, draft_rows=0, draft_layers=3):
    """Rows each layer computes at max positions (plan_suffix_replay, cached 0), rounded as the driver does."""
    t, out_begin, begins = positions, positions - 1, [0] * LAYERS
    draft_begin = t - draft_rows if draft_rows else t
    for layer in range(LAYERS - 1, FIRST_DECODER - 1, -1):
        kv_begin = min(out_begin - (WINDOW - 1), t - WINDOW)
        begins[layer] = max(out_begin, 0) // WINDOW_ROWS * WINDOW_ROWS
        input_begin = min(out_begin, kv_begin)
        if layer >= LAYERS - draft_layers:
            input_begin = min(input_begin, draft_begin)
        out_begin = input_begin
    return [float(positions - b) for b in begins]


def plan_streamed(budget, slots, w, E=EXPERTS):
    """ds41_prefill_driver.cu plan_streamed: s_l = min(E, floor(X w_l / W)) for the smallest X that fits."""
    n, W = len(w), sum(w)

    def streamed(X):
        return [int(min(E, math.floor(X * v / W))) for v in w]

    def fits(s):
        mx = max(s)
        return sum(E - v for v in s) + (0 if mx == 0 else min(slots, n)) * mx <= budget

    if fits([0] * n):
        return [0] * n
    lo, hi = 0, n * E * 4
    if not fits(streamed(hi)):
        raise PlanError(f"device expert budget of {budget} experts below {min(slots, n)} slots of a whole layer")
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(streamed(mid)):
            hi = mid
        else:
            lo = mid
    return streamed(hi)


def predict_prefill(model_gguf, *, vram_cap_gib, positions, host_gib, margin_gib=2.0, margin_fraction=0.03,
                    context_gib=DEFAULTS["context_gib"], reserve_gib=DEFAULTS["reserve_gib"], slots=DEFAULTS["slots"],
                    free_at_start_gib=DEFAULTS["free_at_start_gib"], sz=None):
    """The service's own residency plan at the engine cap hyperloom passes (guard cap - margin - context)."""
    sz = sz or sizes(model_gguf)
    eb = sz["expert_bytes"]
    margin = max(margin_gib, margin_fraction * vram_cap_gib)
    engine_cap = math.floor((vram_cap_gib - margin - context_gib) * 100) / 100
    used = _interp(USED_BEFORE_EXPERTS, positions)
    free_at_plan = free_at_start_gib - used
    avail = min(free_at_plan - reserve_gib, engine_cap - used - reserve_gib)
    if avail <= 0:
        raise PlanError(f"ds41 prefill: {used:.2f} GiB before experts at {positions} positions leaves nothing "
                        f"under the {vram_cap_gib:g} GiB cap")
    budget = int(avail * GIB / eb)
    w, note, n_slots = cone_rows(positions), "rows", slots
    try:
        s = plan_streamed(budget, slots, w)
    except PlanError:
        try:
            s, note = plan_streamed(budget, slots, [1.0] * LAYERS), "even"
        except PlanError:
            s, note, n_slots = plan_streamed(budget, 1, [1.0] * LAYERS), "even-1slot", 1
    host_left, tiers, pinned, disk = host_gib * GIB, [], 0, 0
    for v in s:
        need = v * eb
        if v == 0:
            tiers.append("device")
        elif host_left >= need:
            tiers.append("host")
            host_left -= need
            pinned += need
        else:
            tiers.append("file")
            disk += need
    resident = [EXPERTS - v for v in s]
    slot_bytes = (0 if max(s) == 0 else min(n_slots, LAYERS)) * max(s) * eb
    experts_dev = sum(resident) * eb
    peak = int((context_gib + used) * GIB) + experts_dev + slot_bytes
    check_peak("ds41-rtx-prefill", peak, int(vram_cap_gib * GIB), int(margin * GIB))
    # --host-gib: what the streamed layers need (rounded up), never more than the reservation's rule allows.
    want_host = min(host_gib, math.ceil(sum(v * eb for v in s) / GIB)) if max(s) else 0
    flags = {"--vram-cap-gib": f"{engine_cap:g}", "--host-gib": str(int(want_host))}
    fixed = [("cuda_context", int(context_gib * GIB)), ("dense_buffers_state (measured curve)", int(used * GIB))]
    return Plan(engine="ds41-rtx-prefill", cap_bytes=int(vram_cap_gib * GIB), margin_bytes=int(margin * GIB),
                fixed_bytes=int((context_gib + used) * GIB), stream_bytes=slot_bytes,
                resident=[f"layer:{L}:{r}" for L, r in enumerate(resident) if r],
                streamed=[f"layer:{L}:{v}" for L, v in enumerate(s) if v], resident_bytes=experts_dev,
                pinned_bytes=pinned, disk_bytes=disk, peak_bytes=peak, benefit=0.0, flags=flags, fixed=fixed,
                notes=[f"engine plan '{note}' at --positions {positions}: resident experts per layer "
                       f"{_runs(resident)}; streamed tiers {_runs(tiers)}",
                       f"engine cap {engine_cap:g} GiB = guard cap - margin - CUDA context (the engine's cap "
                       "excludes its context; the watchdog's does not)",
                       f"dense from the GGUF {(sum(sz['layer_dense']) + sz['globals']) / 1e9:.2f} GB; "
                       f"expert {eb} B; Engram {sz['engram_file'] / 1e9:.1f} GB stays in the file"],
                source={"model": str(model_gguf), "positions": positions, "engine_budget_experts": budget,
                        "resident_per_layer": resident, "tiers": tiers, "plan": note},
                tiers={f"layer:{L}:{v}": {"host": "pinned", "file": "disk"}[tier]
                       for L, (v, tier) in enumerate(zip(s, tiers)) if v})


def _runs(values):
    out, start = [], 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            out.append(f"{start}-{i - 1}:{values[start]}" if i - 1 > start else f"{start}:{values[start]}")
            start = i
    return " ".join(out)


def front_model(model_gguf, *, sz=None):
    """Decode split: the longest prefix [0, K) of whole layers (dense + shared + every expert) on the PRO."""
    sz = sz or sizes(model_gguf)
    per = [sz["layer_dense"][L] + sz["layer_experts"][L] for L in range(LAYERS)]
    k = FRONT_MEASURED["k"]
    base = int(FRONT_MEASURED["device_bytes"] - sum(per[:k]))

    def flags(resident, plan):
        n = len(resident)
        return {"--cuts": n, "--layers": n, "--tiers": "D" * n}

    return Model(engine="ds41-rtx-front", fixed=[("front_base (measured at K=22, incl. context)", base)],
                 units=[Unit(f"layer:{L}", per[L], host="pair") for L in range(LAYERS)], order="prefix",
                 flags=flags, min_units=1,
                 notes=["the pair tail must start at the same layer: strix-deepseek41-rank --role pipeline-stage "
                        "--first-layer K",
                        f"base calibrated from the measured K={k} device use (96.0 GB); "
                        "the K=23 reading (97.84 GB) is the cross-check"],
                 source={"model": str(model_gguf), "layer_bytes": per})


def route_counts(routes_path):
    """Selections per (layer, expert) from a routes file ("layer:id,id,..." per token; '#' lines are headers)."""
    counts = Counter()
    with open(routes_path, encoding="ascii") as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            for word in line.split():
                layer, _, ids = word.partition(":")
                if not ids:
                    continue
                L = int(layer)
                for e in ids.split(","):
                    if e:
                        counts[(L, int(e))] += 1
    return counts


def hot_model(model_gguf, *, routes, reserve_gib=HOT_RESERVE_GIB, sz=None):
    """Hot decode: experts ranked by measured selections (ties: layer, then id), each 1 expert_bytes."""
    sz = sz or sizes(model_gguf)
    eb = sz["expert_bytes"]
    counts = route_counts(routes) if isinstance(routes, str) else Counter(routes)
    units = [Unit(f"{L}:{e}", eb, benefit=float(counts.get((L, e), 0)), host="pair")
             for L in range(LAYERS) for e in range(EXPERTS)]
    total = sum(counts.values())

    def flags(resident, plan):
        hits = sum(counts.get(tuple(map(int, k.split(":"))), 0) for k in resident)
        plan.notes.append(f"resident experts take {hits / total:.1%} of the measured selections" if total else
                          "no routes: ties by layer and id")
        return {"placement": "<written by hyperloom vramplan --placement-out>"}

    return Model(engine="ds41-rtx-hot", fixed=[("hot_reserve (measured W1: dense + state + buffers)",
                                                int(reserve_gib * GIB))],
                 units=units, order="ranked", flags=flags,
                 notes=["experts not resident are served by the pair's expert servers"],
                 source={"model": str(model_gguf), "expert_bytes": eb, "routes_selections": total})


def placement_json(plan, *, model_file, budget_gib, servers, reserve_gib=HOT_RESERVE_GIB):
    """tensormeld/hot-expert-placement-v1 for a hot plan: rtx-pro experts; every other expert on the two AMD expert
    servers (machine ids from the cluster, rank 0 holds experts [0, 192), rank 1 [192, 384))."""
    if len(servers) != 2 or not all(isinstance(x, str) and x for x in servers):
        raise PlanError("hot placement needs the two expert-server machine ids")
    expert_bytes = plan.source["expert_bytes"]
    per = {L: [] for L in range(LAYERS)}
    for key in plan.resident:
        L, e = map(int, key.split(":"))
        per[L].append(e)
    return {
        "format": "tensormeld/hot-expert-placement-v1",
        "model": {"family": "deepseek-v4.1-flash", "file": model_file, "model_identity": None, "layers": LAYERS,
                  "experts": EXPERTS, "top_k": 6, "expert_bytes": expert_bytes, "canonical_split": 192},
        "dense": {"device": "rtx-pro", "layers": "all"},
        "devices": [{"id": "rtx-pro", "kind": "cuda", "role": "parts-host", "budget_gib": budget_gib,
                     "reserve_gib": reserve_gib, "expert_gib": round(budget_gib - reserve_gib, 6),
                     "experts": len(plan.resident)},
                    {"id": "rtx-4090", "kind": "cuda", "role": "expert-tier", "budget_gib": 0, "reserve_gib": 0.0,
                     "expert_gib": 0, "experts": 0},
                    {"id": "pc-ram", "kind": "host", "role": "expert-cache", "budget_gib": 0, "expert_gib": 0,
                     "experts": 0},
                    {"id": "amd", "kind": "hip", "role": "expert-server",
                     "servers": [{"node": servers[0], "experts": [0, 192]}, {"node": servers[1], "experts": [192, 384]}]}],
        "layers": [{"layer": L, "rtx-pro": sorted(per[L]), "rtx-4090": [], "pc-ram": []} for L in range(LAYERS)],
        "combine": {"identity": "ds41-canonical-halves-v1",
                    "remote_return": "raw-per-expert; a remote canonical prefix of a half may return one folded partial"},
        "fit": {"objective": "routing-frequency (hyperloom vramplan)", "budget_gib": budget_gib},
        "expected": {},
    }


def write_placement(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=1)
