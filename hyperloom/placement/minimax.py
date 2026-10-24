"""MiniMax M3.1 RTX prefill (mm-rtx-prefill, minimax/rtx-prefill): --resident N.

Layers L < N live in one device buffer each; the others are pinned host blobs copied into two device slots (each the
size of the largest streamed blob). Every blob holds both TP halves. Sizes replay the engine's blob builder
(src/minimax/rtxpf/cuda/mm_rtx_weights.cpp build_layer at 736d1361) over the safetensors header shapes: 256-byte
aligned pieces, EXL3 trellis split per rank (slice_out: full suh + half svh; slice_in: half suh + full svh), mul1 not
uploaded, F16/BF16/F32 small tensors as FP32. Runtime buffers follow mm_rtx_prefill_main.cu (Runner::alloc, the
per-prompt K/V/index keys of all 60 layers, the RoPE rows); timing mode runs one prompt at a time.
"""
from __future__ import annotations

import math
import re

from .. import modelfiles
from ..vramplan import GIB, Model, PlanError, Unit

P = "language_model.model."
LAYERS, FIRST_MOE, EXPERTS, TOP_E = 60, 3, 128, 4
HIDDEN, Q, KV, Q_LOCAL, KV_LOCAL, IDX_LOCAL, DENSE_LOCAL, EXPERT_LOCAL = 6144, 8192, 512, 4096, 256, 256, 6144, 1536
CUDA_GRANULE = 2 << 20  # cudaMalloc rounds each allocation up to 2 MiB pages
DEFAULTS = {"context_gib": 0.6, "pcie_gbps": 25.0}


def _align(n):
    return (n + 255) & ~255


def _elements(t):
    return math.prod(t.shape)


class _Blob:
    def __init__(self, index):
        self.index, self.size = index, 0

    def put(self, n):
        self.size = _align(self.size) + n

    def floats(self, name):
        self.put(_elements(self.index[name]) * 4)

    def mat(self, prefix, kind, n):
        tr, suh, svh = (self.index[f"{prefix}.{part}"] for part in ("trellis", "suh", "svh"))
        if kind == "out":  # output slice: in_tiles x (n/16) tiles, full suh, n of svh
            self.put(tr.bytes * n // svh.shape[0])
            self.put(suh.shape[0] * 2)
            self.put(n * 2)
        else:  # input slice: n/16 tile rows, n of suh, full svh
            self.put(tr.bytes * n // suh.shape[0])
            self.put(n * 2)
            self.put(svh.shape[0] * 2)

    def mlp(self, prefix, names, inter):
        self.mat(prefix + names[0], "out", inter)
        self.mat(prefix + names[1], "out", inter)
        self.mat(prefix + names[2], "in", inter)


def layer_bytes(index, layer):
    """Bytes of one layer's blob, as build_layer lays it out."""
    lp = f"{P}layers.{layer}."
    a, b = lp + "self_attn.", lp + "block_sparse_moe."
    blob = _Blob(index)
    for _ in range(2):
        blob.mat(a + "q_proj", "out", Q_LOCAL)
        blob.mat(a + "k_proj", "out", KV_LOCAL)
        blob.mat(a + "v_proj", "out", KV_LOCAL)
        blob.mat(a + "o_proj", "in", Q_LOCAL)
        blob.put(HIDDEN * IDX_LOCAL * 4)  # this rank's index_q heads, transposed
    blob.put(HIDDEN * 128 * 4)  # index_k, transposed
    for name in ("q_norm", "k_norm", "index_q_norm", "index_k_norm"):
        blob.floats(a + name + ".weight")
    blob.floats(lp + "post_attention_layernorm.weight")
    blob.floats(f"{P}layers.{layer + 1}.input_layernorm.weight" if layer + 1 < LAYERS else P + "norm.weight")
    if layer < FIRST_MOE:
        for _ in range(2):
            blob.mlp(lp + "mlp.", ("gate_proj", "up_proj", "down_proj"), DENSE_LOCAL)
    else:
        for e in range(EXPERTS):
            for _ in range(2):
                blob.mlp(f"{b}experts.{e}.", ("w1", "w3", "w2"), EXPERT_LOCAL)
        for _ in range(2):
            blob.mlp(b + "shared_experts.", ("gate_proj", "up_proj", "down_proj"), EXPERT_LOCAL)
        blob.floats(b + "gate.weight")
        blob.floats(b + "e_score_correction_bias")
    return blob.size


def _granule(n):
    return -(-n // CUDA_GRANULE) * CUDA_GRANULE


STAGE_BUFFERS, FILE_ALIGN = 4, 4096


def _three_tier(model_dir, *, positions, layers, context_gib, blob_cache):
    meta, tensors = modelfiles.read(model_dir)
    index = {t.name: t for t in tensors if "vision" not in t.file.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]}
    sizes = [layer_bytes(index, L) for L in range(layers)]
    dense = min(FIRST_MOE, layers)
    run = runtime_bytes(positions)
    rows = positions - 1
    fixed = [("cuda_context", int(context_gib * GIB)), ("kv_state", run["kv_state"]),
             ("activations", run["activations"]), ("rope_and_norm", run["rope"] + CUDA_GRANULE),
             ("dense layers (always resident)", sum(_granule(n) for n in sizes[:dense]))]
    padded = [-(-n // FILE_ALIGN) * FILE_ALIGN for n in sizes]
    units = [Unit(f"layer:{L}", _granule(sizes[L]), benefit=1.0, host="pinned|disk", host_bytes=sizes[L],
                  file_bytes=padded[L]) for L in range(dense, layers)]
    slot = _granule(max(sizes[dense:])) if layers > dense else 0

    def stream(streamed):
        return 2 * slot if streamed else 0

    def flags(resident, plan):
        on_file = sum(1 for t in plan.tiers.values() if t == "disk")
        pinned = sum(1 for t in plan.tiers.values() if t == "pinned")
        if on_file and pinned < 2:
            raise PlanError("minimax three-tier: the engine keeps the first two streamed layers off the file tier, "
                            "so a plan with file layers needs at least 2 pinned layers; raise the host budget")
        out = {"--resident": dense + len(resident), "--pinned": pinned, "--blob-cache": str(blob_cache)}
        if layers != LAYERS:
            out["--layers"] = layers
        return out

    return Model(
        engine="minimax-rtx-prefill", fixed=fixed, units=units, order="prefix", flags=flags, stream=stream,
        host_fixed=[("state_blobs", 2 * rows * LAYERS * 1280), ("embed_staging", rows * HIDDEN * 4),
                    ("file staging ring", STAGE_BUFFERS * max(padded[dense:], default=0))],
        notes=["three-tier mode: --resident counts the dense layers; the engine spreads the MoE device layers and "
               "the file layers over the depth (byte counts do not depend on which)",
               "the 4-buffer file staging ring is counted even when no layer streams from the file (conservative)",
               f"device slots 2 x {slot} B; file blobs padded to {FILE_ALIGN} B"],
        source={"model_dir": str(model_dir), "positions": positions, "layer_bytes": sizes, "mode": "three-tier",
                "files": modelfiles.fingerprint(model_dir)})


def runtime_bytes(positions):
    """Device buffers of one prompt of `positions` tokens (rows = positions - 1), each rounded to the CUDA granule."""
    rows = positions - 1
    pairs, blocks = rows * TOP_E, -(-rows // 128)
    floats = [rows * HIDDEN] * 2 + [rows * Q, rows * KV, rows * KV, rows * 512, rows * 128, rows * Q,
              rows * HIDDEN, rows * HIDDEN] + [max(pairs * EXPERT_LOCAL, rows * DENSE_LOCAL)] * 2 + \
             [pairs * HIDDEN, rows * HIDDEN, pairs * HIDDEN, pairs * HIDDEN, rows * EXPERTS * 2, rows * 4 * blocks]
    words = [rows * 4 * 16, rows * 4, rows * TOP_E * 2, 3 * pairs, rows]
    activations = sum(_granule(n * 4) for n in floats + words)
    kv = 2 * _granule(LAYERS * rows * 4 * 128 * 2) + _granule(LAYERS * rows * 128 * 2)
    rope = _granule(rows * 256)
    return {"kv_state": kv, "activations": activations, "rope": rope}


def model(model_dir, *, positions, layers=LAYERS, context_gib=DEFAULTS["context_gib"], pcie_gbps=DEFAULTS["pcie_gbps"],
          blob_cache=None):
    """`layers` < 60 is the engine's --layers profiling subset (validation only).

    blob_cache: the three-tier mode of mm-rtx-prefill (minimax/engine 48ab2724: --resident N --pinned H --blob-cache
    DIR, spec-minimax 3.1). The dense layers always stay on the device and N - 3 MoE layers are spread over the depth
    (which ones does not change any byte count: the MoE blobs are equal); of the other layers H are pinned host blobs
    and the rest are read per prompt from DIR (4 KiB-padded files) through a 4-buffer pinned staging ring of the
    largest padded blob. The engine never puts the first two streamed layers on the file tier, so a plan needs at
    least two pinned layers whenever any layer streams from the file."""
    if blob_cache is not None:
        return _three_tier(model_dir, positions=positions, layers=layers, context_gib=context_gib,
                           blob_cache=blob_cache)
    meta, tensors = modelfiles.read(model_dir)
    index = {t.name: t for t in tensors if "vision" not in t.file.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]}
    found = sorted({int(m.group(1)) for n in index if (m := re.match(re.escape(P) + r"layers\.(\d+)\.", n))})
    if found != list(range(LAYERS)):
        raise modelfiles.ModelFileError(f"{model_dir}: expected MiniMax layers 0..{LAYERS - 1}")
    sizes = [layer_bytes(index, L) for L in range(layers)]
    units = [Unit(f"layer:{L}", _granule(n), benefit=n / (pcie_gbps * 1e9)) for L, n in enumerate(sizes)]
    run = runtime_bytes(positions)  # K/V/index keys are allocated for all 60 layers even under --layers
    rows = positions - 1
    fixed = [("cuda_context", int(context_gib * GIB)), ("kv_state", run["kv_state"]),
             ("activations", run["activations"]), ("rope_and_norm", run["rope"] + CUDA_GRANULE)]
    slot = _granule(max(sizes))

    def stream(streamed):
        return 2 * slot if streamed else 0

    def flags(resident, plan):
        out = {"--resident": len(resident)}
        if layers != LAYERS:
            out["--layers"] = layers
        return out

    return Model(
        engine="minimax-rtx-prefill", fixed=fixed, units=units, order="prefix", flags=flags, stream=stream,
        host_fixed=[("state_blobs", 2 * rows * LAYERS * 1280), ("embed_staging", rows * HIDDEN * 4)],
        notes=[f"--resident N keeps layers 0..N-1 on the device (blob {sizes[0]} B dense, {sizes[FIRST_MOE]} B MoE); "
               f"streamed layers use 2 device slots of {slot} B",
               f"benefit = streamed bytes / {pcie_gbps:g} GB/s per prompt (estimate until the PC-BIG measures it)",
               "host also keeps the BF16 embedding table pageable (not pinned)"],
        source={"model_dir": str(model_dir), "positions": positions, "layer_bytes": sizes,
                "files": modelfiles.fingerprint(model_dir)})
