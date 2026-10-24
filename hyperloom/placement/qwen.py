"""Qwen3.8 Flash Next RTX prefill (strix-cuda-qwen-prefill-service, qwen/rtx-prefill-coop 70b93af3).

This engine has no partial residency: every GGUF tensor except output.weight (the head stays on the node) and
per_layer_token_embd (pinned, mapped host memory) is one cudaMalloc on the device, plus one packed HC copy per mixer.
The cap can only choose the row block (--blocks: its largest entry sizes every activation workspace) for the
--max-positions the profile needs, or refuse. Blocking does not change bits (cuda_qwen_prefill.hpp:8-9; release by
layer gate). `strix-memory-plan --resident-gib` is the node's host-RAM admission calculator, not an RTX knob.
Sizes follow src/cuda/qwen_prefill_driver.cu (upload_model, upload_ple, upload_mtp, allocate) at 70b93af3.
"""
from __future__ import annotations

from .. import modelfiles
from ..vramplan import GIB, Model, PlanError

LAYERS, QSA_LAYERS, GDN_LAYERS, HIDDEN, WIDE, TOP_K, EXPERTS = 48, 12, 36, 2560, 10240, 10, 512
HC_PACKED = 320 * WIDE + 320 * (WIDE // 32) * 4 + (320 // 4) * 4 * HIDDEN * 4 + (320 // 32) * 4 * HIDDEN * 4
GDN_MATRIX_FLOATS, GDN_CONV_HALFS, GDN_CONV_WIDTH = 48 * 128 * 128, 10240 * 4, 10240
QSA_Q_GATE, QSA_KV, QSA_ATTN, QSA_INDEX_Q, QSA_INDEX_DIM, QSA_HEADS, QSA_RATIO, QSA_MAX_SELECTED = \
    12288, 512, 6144, 512, 128, 24, 4, 2051
PLE_HEADS, PLE_WIDTH, PLE_EXPANDED = 16, 2560, 10240
MTP_RANK, MTP_INDEX, MTP_KV = 320, 128, 512
BLOCKS = (8192, 4096, 2048, 1024)  # largest block first: fewer, larger blocks are faster (report §2 block times)
CUDA_GRANULE = 2 << 20
DEFAULTS = {"context_gib": 0.6}


def _alloc(n):
    """cudaMalloc footprint: whole 2 MiB pages for large blocks, 512-byte steps for small ones."""
    if n >= CUDA_GRANULE:
        return -(-n // CUDA_GRANULE) * CUDA_GRANULE
    return -(-max(n, 1) // 512) * 512


def _a256(n):
    return (n + 255) // 256 * 256


def workspace_bytes(cap, positions, mtp):
    """allocate(): row workspaces sized by the block cap, attention state sized by --max-positions."""
    R = (cap + 15) // 16 * 16
    rows = [R * 4, R * HIDDEN * 2, R * WIDE * 2, R * WIDE * 2, R * WIDE * 2, R * HIDDEN * 2, R * HIDDEN * 2, R * 16,
            R * (WIDE + 320) * 4, R * 6144, R * (6144 // 32) * 4, R * (10240 + 6144 + 48 + 48 + 6144) * 4,
            _a256(-(-R // 32) * 3 * GDN_CONV_WIDTH * 2) + R * 48 * 4,
            R * QSA_Q_GATE * 4, R * QSA_KV * 4, R * QSA_KV * 4, R * QSA_INDEX_Q * 4, R * QSA_INDEX_DIM * 4,
            R * QSA_ATTN * 4, R * EXPERTS * 4, R * TOP_K * 4, R * TOP_K * 4, R * HIDDEN * 2, R * HIDDEN * 2,
            R * HIDDEN * 2, R * 1280 * 4, R * HIDDEN, R * (HIDDEN // 32) * 4, R * (HIDDEN // 32) * 4, R * 16,
            R * HIDDEN * 4, R * TOP_K * 640, R * TOP_K * 640 // 32 * 4, R * TOP_K * 640 // 32 * 4,
            R * TOP_K * HIDDEN * 4, R * TOP_K * 4, R * TOP_K * 4, (EXPERTS + R * TOP_K) * 4, (EXPERTS + R * TOP_K) * 4,
            R * PLE_HEADS * 8, R * PLE_WIDTH * 2, R * PLE_EXPANDED * 4, R * HIDDEN * 4, R * 16]
    chunk, blocks = min(R, 1024), -(-positions // QSA_RATIO)
    qsa_ws = [blocks * QSA_INDEX_DIM * 4, chunk * blocks * 4, chunk * QSA_MAX_SELECTED * 4,
              chunk * QSA_HEADS * QSA_MAX_SELECTED * 4]
    if mtp:
        r = R
        rows += [sum(_a256(x) for x in (r * HIDDEN * 2, r * (HIDDEN + PLE_EXPANDED) * 4, r * PLE_EXPANDED * 2,
                                        r * PLE_EXPANDED * 4, r * MTP_RANK * 4, r * HIDDEN * 2, r * MTP_INDEX * 4,
                                        r * MTP_KV * 4, r * MTP_KV * 4, 4)), R * 4]
    return sum(_alloc(n) for n in rows + qsa_ws)


def state_bytes(positions, mtp):
    per = [positions * 1024 * 2, positions * 128 * 2] * QSA_LAYERS
    per += [GDN_LAYERS * GDN_MATRIX_FLOATS * 4, GDN_LAYERS * GDN_CONV_HALFS * 2, 10240 * 9 * 2]
    if mtp:
        per += [positions * 1024 * 2, positions * 128 * 2]
    return sum(_alloc(n) for n in per)


def model(model_gguf, *, positions, mtp_gguf=None, context_gib=DEFAULTS["context_gib"]):
    meta, tensors = modelfiles.read(model_gguf)
    device = [t for t in tensors if t.name != "output.weight" and not t.name.startswith("per_layer_token_embd")]
    ple = sum(t.bytes for t in tensors if t.name.startswith("per_layer_token_embd"))
    layers = {int(t.name.split(".")[1]) for t in device if t.name.startswith("blk.")}
    if layers != set(range(LAYERS)) or not ple:
        raise modelfiles.ModelFileError(f"{model_gguf}: expected Qwen blk.0..{LAYERS - 1} and a PLE table")
    weights = sum(_alloc(t.bytes) for t in device) + 2 * LAYERS * _alloc(HC_PACKED)
    mtp_bytes = 0
    if mtp_gguf:
        _, side = modelfiles.read(mtp_gguf)
        wanted = {"blk.48.nextn.enorm.weight", "blk.48.nextn.hnorm.weight", "blk.48.nextn.eh_proj.weight",
                  "blk.48.hc_attn_norm.weight", "blk.48.hc_attn_down.weight", "blk.48.hc_attn_up.weight",
                  "blk.48.indexer.k_proj.weight", "blk.48.attn_k.weight", "blk.48.attn_v.weight",
                  "blk.48.attn_k_norm.weight"}
        mtp_bytes = sum(_alloc(t.bytes) for t in side if t.name in wanted) + _alloc(HIDDEN * 2 * HIDDEN // 32 * 18)
    fixed_common = [("cuda_context", int(context_gib * GIB)), ("weights", weights), ("mtp", mtp_bytes),
                    ("state", state_bytes(positions, bool(mtp_gguf))), ("rope", _alloc(positions * 32 * 8))]
    host = [("ple_table_mapped", ple),
            ("export_staging", positions * (12 * 2048 + 12 * 256 + 2048 + 256) + GDN_LAYERS * GDN_MATRIX_FLOATS * 4
             + GDN_LAYERS * GDN_CONV_HALFS * 2 + 10240 * 9 * 2 + 16 + WIDE * 2)]
    options = []
    for cap in BLOCKS:
        schedule = ",".join(str(b) for b in sorted({min(b, cap) for b in (1024, 2048, 4096, cap)}))

        def flags(resident, plan, schedule=schedule):
            return {"--max-positions": positions, "--blocks": schedule}

        options.append(Model(
            engine="qwen-rtx-prefill", fixed=fixed_common + [("workspaces", workspace_bytes(cap, positions,
                                                                                            bool(mtp_gguf)))],
            units=[], order="prefix", flags=flags, host_fixed=host,
            notes=[f"block cap {cap} rows (larger first; blocking is bit-invariant)",
                   "no partial residency in this engine: every weight but the head and the PLE table is on the device"],
            source={"model": str(model_gguf), "mtp": str(mtp_gguf) if mtp_gguf else None, "positions": positions,
                    "weights_bytes": weights, "files": modelfiles.fingerprint(model_gguf)}))
    return options


def check_positions(positions):
    if not 1024 <= positions <= 262144:
        raise PlanError("qwen: --max-positions out of range")
