# VRAM placement: `--vram-cap-gib` plans the RTX placement

Status: `hl/vramplan`, 2026-10-24. Unit-tested; the PC-BIG validation (predicted vs measured peak VRAM, identical
outputs) is in `wave4/vramplan/report.md` of the lab notes.

## What it does

`hyperloom up <profile> --vram-cap-gib X` (or the reservation's `vram_cap_gib` when the flag is absent) now does two
things for the RTX prefill service:

1. **Plans** what the engine keeps on the RTX PRO for that cap and turns the plan into the engine's own flags.
2. **Guards** it: the watchdog still kills the service above X (the hard stop is unchanged).

The plan is recomputed on every start, so a different cap on the next `up` gives a different placement. `--vram-cap-gib`
can lower the operator's reservation, never raise it. A cap that cannot hold the engine is refused before anything
starts. The plan (flags, predicted peak, pinned host, per-layer residency and an `explain` text) is recorded on the
service record in the profile's state.

```text
hyperloom prefill plan <profile> [--vram-cap-gib X]     # passive: the flags `up` would use
hyperloom vramplan <engine> --model PATH --vram-cap-gib X [--positions N] [--host-gib H] [...]
```

`vramplan` plans any registered engine from its model files alone (headers only, no weights read), including the
engines that have no hyperloom service preset yet.

## How a plan is made

- **Sizes come from the model files.** `hyperloom/modelfiles.py` reads GGUF and safetensors headers and returns every
  tensor's exact byte size. Each family's memory model (`hyperloom/placement/<family>.py`) maps tensors to what the
  engine allocates, following the engine's own code at its pinned commit.
- **Fixed device memory** (CUDA context, workspaces, state per token × positions) is reserved first, plus a safety
  margin of max(2 GiB, 3 % of the cap).
- **Placeable units** (a layer, or one expert of one layer) fill the rest:
  - `prefix`: only the first k units in model order (a layer split, `--resident N`);
  - `ranked`: best benefit per byte first, ties in model order.
- **Streaming slots** (the device buffers a streamed unit is copied into) are reserved whenever anything streams.
- **Pinned host** for non-resident units is checked against `host_gib`; a plan that needs more is refused.
- The planner is deterministic and never changes what the engine computes, only where weights live.

## Engines

| engine | binary | knob | rule |
|---|---|---|---|
| `minimax-rtx-prefill` | `mm-rtx-prefill` (minimax/rtx-prefill 736d1361) | `--resident N` | layers [0, N) on the device (the 3 dense layers count); the rest pinned host, 2 device slots. Blob bytes replay `build_layer` exactly (237,834,240 / 2,864,462,336 B). No disk path: every streamed layer is pinned. |
| `qwen-rtx-prefill` | `strix-cuda-qwen-prefill-service` (qwen/rtx-prefill-coop 70b93af3) | `--blocks`, `--max-positions` | no partial residency: every tensor but the head and the PLE table (pinned, mapped) is on the device. The cap picks the largest block that fits, or refuses. |
| `ds41-rtx-prefill` | `strix-cuda-ds41-prefill-service` (9211b07e) | `--vram-cap-gib`, `--host-gib` | the service plans its own per-expert residency from its cap. hyperloom replays that plan (rows-weighted `plan_streamed`, cone rows) to predict residency, peak and pinned host, passes an engine cap of guard cap − margin − context (the engine's cap excludes its CUDA context; the watchdog's does not), and a `--host-gib` no larger than what streams. |
| `ds41-rtx-front` | `strix-cuda-ds41-pipeline-front` (D-064) | `--cuts K --layers K --tiers D…` | the longest prefix of whole layers; the pair tail must use `--first-layer K`. Base calibrated from the measured K=22 device use. |
| `ds41-rtx-hot` | `strix-cuda-ds41-hot-decode` | placement JSON | experts ranked by measured routing frequency (a routes file); writes `tensormeld/hot-expert-placement-v1`. |

Only `rtx-prefill-ds41` is a hyperloom service preset today; it carries
`"placement": {"engine": "ds41-rtx-prefill", "model_asset": "ds41_model"}` and the argv token `{placement}`, which
expands to the plan's flags. A future MiniMax or Qwen service preset adds the same block with its engine name.

## Measured inputs (not derivable from the files)

- DS prefill: device bytes before the experts by positions (512/2K/8K/32K: 11.10/11.46/12.89/18.36 GiB) and the free
  memory at start (93.09 GiB), from the exact-arm runs. With them the replay reproduces the measured 32K plan
  (layers 0-19 stream, 20-39 keep 334..383 experts, slots 7.64 GB, experts 71.5 GB).
- DS front: 96.0 GB at K=22. DS hot: 9.55 GiB reserve (W1), which reproduces W1's 5,442 / 6,521 / 7,600 / 8,678
  experts at 60 / 70 / 80 / 90 GiB.
- CUDA context: 1.0 GiB (DS) / 0.6 GiB (MiniMax, Qwen) until the PC-BIG measures it.
