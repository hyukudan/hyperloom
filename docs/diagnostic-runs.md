# Diagnostic runs (non-production)

A diagnostic run is an ordinary `up` with extra, **observation-only** engine switches. It is labelled
non-production everywhere it is recorded, and its output lands in the node's `<install root>/log/`.

```
hyperloom up qwen-replicas --diagnostic prefill-timing [--diagnostic cycle-timing ...]
hyperloom status qwen-replicas        # "production": false, "diagnostic": ["prefill-timing", ...]
hyperloom logs qwen-replicas halo-1   # stderr diagnostics are in the engine's lease log
```

## Rules
- The names come from the preset's `diagnostics` table (data per engine pin, `hyperloom/presets/*.json`). There is no
  free-form env: unknown names, or names of the other preset, are refused before any remote call.
- A diagnostic only **adds** env variables. It can never replace a production variable (rendering refuses), argv is
  unchanged, and the node's env rules still apply (reserved and loader names are rejected). Without `--diagnostic` the
  render is byte-for-byte the production render (golden tests).
- File outputs are rendered by code to `<install root>/log/diag-<run>-<role>-...` on the machine that runs the process
  (rank 1 writes on its own node). stderr outputs go to the lease's stderr log, also in `log/`.
- Marking: the PC run state and `status` carry `"production": false` and the list; the node lease record carries
  `"production": false, "diagnostic": [...]`. Lease labels keep the approved `hl-` form.
- **Never publish numbers from a diagnostic run as production measurements.** Timing switches add host clock reads and
  stderr writes. hyperloom has no measurement publisher; any bench or report that reads hyperloom state must refuse a
  run whose state says `"production": false`.

## Verified diagnostics (engine pin `70ec42b9`; unchanged in `9211b07e`)
Each entry was read in the pinned source and only reads clocks and prints or buffers records; none changes what is
enqueued or computed, and none gates a code path.

| Profile | Name | Env | Output | Source |
|---|---|---|---|---|
| qwen-replicas | `prefill-timing` | `STRIX_PREFILL_TIMING=1` | stderr: host ms per prefill phase, block download, scoring, spooling | `src/known_prefix_prefill_runtime.cpp:51-56`, `src/hip_known_prompt_backend.cpp:105-109`, `src/hip_ple_launcher.hip:2477-2483`, `apps/hip_decode_token_main.cpp:829-833` |
| qwen-replicas | `cycle-timing` | `STRIX_CYCLE_TIMING=1` | stderr at exit: host time between named cycle marks | `include/strix/host_phase_timing.hpp:12-27`, `src/hip_ple_launcher.hip:2575-2576` |
| qwen-replicas | `prompt-batch-log` | `STRIX_PROMPT_BATCH_LOG=1` | stderr: one line per grouped prompt batch | `apps/hip_mtp_decode_bridge.hpp:659-665` |
| ds41-pair | `rank1-prefill-log` | `STRIX_DS41_SERVE_PREFILL_LOG=1` | rank 1 stderr: prefill tokens, next token, logits FNV | `apps/deepseek41_serve_main.cpp:77-78, 815-819` |
| ds41-pair | `dist-latency-trace` | `STRIX_DIST_LATENCY_TRACE=<log>/diag-<run>-<role>-dist-latency` | `<prefix>.t<tid>.csv` per thread, written at thread exit (no file I/O inside an exchange) | `src/dist_latency_trace.hpp:1-2, 15-19`, `src/dist_communicator.cpp:159-167` |

## Considered and not included
| Variable | Why not |
|---|---|
| `STRIX_DS41_STEP_TRACE`, `STRIX_DS41_OP_TIMING` | Observation-only, but read only by `DeviceForward` (`src/hip_ds41_step_trace.hpp`, `src/hip_deepseek41_device_forward.hip:504, 914`); `strix-deepseek41-serve` runs `BatchForward`, so they would do nothing in this product. |
| `STRIX_DIST_TRACE` | Disables the staged exchange fast path (`src/dist_communicator.cpp:1797`): it changes the transport path. |
| `STRIX_MTP_BATCH_LOG` | Gates a code path (`src/hip_mtp_greedy_runtime.hip:530`, `if(joint \|\| (... && log))`). |
| `STRIX_DS41_SERVE_SCHED_LOG` | Only active with a non-default scheduler mode (`include/strix/deepseek41_serve_sched.hpp:96`). |

Adding a diagnostic is a reviewed preset change with the same check: read it at the pin, cite file:line, and add it
only if it cannot change numerics, scheduling or transport.

The portal does not offer diagnostics; they are CLI-only.
