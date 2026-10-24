# Phase 3a: the RTX prefill service

Status: implemented on `hl/phase3a`, 2026-10-23 (lead review fixes included). The hardware acceptance (below) has not run yet.

## What it does

`hyperloom up <profile>` runs a resident prefill service on this machine's RTX PRO when the profile has a `prefill_service`:

1. The decode side starts as usual: pair lease, layout check, both ranks `READY`.
2. The service starts. It binds to 127.0.0.1 only, inside a Job Object, with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<PRO uuid>`. It is ready when it prints its `READY` event and `/health` answers `ok`.
3. The router starts with `prefill.policy auto`. Its offload target is the service, using the service's measured profile and the decode profile's measured curves. The handoff goes to rank 0's `/admin/import-state` with rank 0's key.

`hyperloom down` stops the router first, then the service, then the engines. A failure to stop the service is reported but never keeps the engines running.

**Lifecycle safety**
- The service record (its directory) is written to the profile's state file **before** the helper starts, so `down` and the controller's watchdog can always find it.
- The helper holds a lifetime lock and checks the stop file again right before starting the CUDA child. A stop that wins the race leaves nothing running.
- The helper stops the service by itself when the controller's state file or token disappears.
- The service binary's sha256 must match its release `MANIFEST.json`; a release is built into a staging directory and published once, never rebuilt in place.
- After `READY`, `/health` is checked every 10 s; three failures stop the service.
- The watchdog fails closed: any error inside it kills the service.

## Configuration

```jsonc
"machines": [{ "id": "workstation", "os": "windows", "access": "local", …,
               "devices": [{ "id": "rtx-pro", "type": "nvidia", "uuid": "GPU-…" }] }, …],
"assets": { "workstation": { "ds41_model": { "path": "…", "bytes": …, "sha256": "…" } } },
"profiles": { "ds41-rtx": { "preset": "ds41-pair-long", "rank0": "halo-1", "rank1": "halo-2",
                            "prefill_service": { "preset": "rtx-prefill-ds41", "machine": "workstation", "device": "rtx-pro" } } },
"pc": { "cuda": { "pro_uuid": "GPU-…",
                  "reservation": { "owner": "…", "approval": "<coordination line>", "expires_at": "2026-10-23T22:00:00Z",
                                   "vram_cap_gib": 90, "host_gib": 80, "free_commit_floor_gib": 4 } } }
```

- **Reservation (lab mode).** Without a current reservation, nothing heavy starts on the PC: no service and no CUDA build. The UI cannot grant one; it lives only in the config file.
- **What the reservation controls:**
  - the VRAM cap (`--vram-cap-gib`) and the watchdog's limit. `up --vram-cap-gib X` may lower it; then the
    service's placement is planned for X (docs/vramplan.md) and the engine receives the planned flags (for DeepSeek an
    engine `--vram-cap-gib` of X − margin − CUDA context, and a `--host-gib` no larger than what streams). Without
    the flag the launch line is the qualified one, unchanged;
  - `--host-gib` = min(80, `host_gib`, free physical memory − 20), the same rule the lab window used;
  - `--host-commit-floor-gib` = floor + 10;
  - the watchdog's limits.
- **Watchdog.** Memory every 0.5 s and the GPU every 2 s (`nvidia-smi` with a 5 s timeout), for the service's PID. It kills that process, and only that process, in four cases:
  - free commit falls below the floor;
  - its VRAM goes above the cap (under WDDM it measures device use above the starting baseline);
  - it becomes visible on any GPU other than the PRO;
  - the reservation ends.
- **Router.** The service is loopback-only, so the router must run locally (`--router local`).
- **Window.** A reservation lasts at most 20 minutes unless it carries an `extended_approval` (main's ACK line).
- **One heavy job per device.** The service helper and the CUDA build hold a per-UUID lock, so two profiles (or a build and a service) never share the PRO.
- **Context.** `ds41-pair` qualifies `--context 4096` only. Offloading an 8K prompt needs a context of at least prompt + output reserve + 1, and the router sends no reserve (TensorMeld R9), so the pair reserves its default max tokens (1024). The `ds41-pair-long` preset uses the configuration main qualified for the RTX profile, `--context 33024 --slots 2` (the one the W2 end-to-end runs used).
- **Longest offload.** The service takes up to 32768 positions; the pair imports up to 33024 − 1024 − 1 = 31999. The router has no per-service length cap, so a prompt of 32000–32768 tokens is prefilled, its import is refused (413) and the router falls back to a local prefill. `prefill check` reports this gap.

## Model-agnostic design

A service preset (`hyperloom/presets/rtx-prefill-<name>.json`, schema `hyperloom/prefill-service-v1`) is pure data. It holds:
- the binary, build recipe and argv template;
- which flags carry secrets;
- the decode preset families it can hand off to, with each family's context rule and measured router curves;
- the service's measured profile;
- a named fidelity oracle.

`hyperloom/rtx.py` names no model.

**Adding Qwen** takes a new preset with:
- `decode_presets: ["qwen-replicas"]`;
- the Qwen service's argv;
- its handoff path;
- its oracle.

The service passes one `--handoff-api-key`, so a family with several decode replicas needs either the same key on every replica or the router's per-replica decode keys (TensorMeld R9). `max_decode_replicas` enforces this.

## CLI

| Command | What it does |
|---|---|
| `hyperloom prefill check <profile>` | Passive: config, reservation, `nvidia-smi` identity of the UUID (it must be an RTX PRO 6000), memory, built release. |
| `hyperloom prefill build <profile> --source <engine checkout>` | `windows/build-cuda-front.ps1` in a Job Object, under the reservation's free-commit watchdog and deadline (at most 4 h). The checkout must be clean and at the preset's pinned commit. Runs vswhere, vcvars64, the CUDA 13.0 check, then Ninja at Idle priority. Writes `MANIFEST.json` with the binary sha256. |
| `hyperloom prefill gate <profile> --prompts <dir>` | The fidelity gate against the running service. For each oracle length it checks the state sha256, next token, logits FNV and state size, then that the process is visible only on the PRO. |
| `hyperloom prefill placement <profile>` | Acceptance 2–3 through the running router. An ~8K-token prompt must be placed `offload` (router header), served by the service, with no fallback in the router counters. A short prompt must stay `local`. |

## Acceptance (SPEC §6 Phase 3a)

1. **Only on the PRO.** The gate's `visibility`: `only_pro` is true or false from the driver's per-process rows. Under WDDM the driver may list none; then it is `null` (unverified) and the evidence is every GPU's memory use next to the service's PRO-pinned environment and the watchdog log.
2. **8K offload with no local fallback.** `prefill placement`.
3. **Short prompts local.** `prefill placement`.
4. **Exactness.**
   - Named gate `ds41-rtx-exact-v1`: the service's states equal the qualified exact arm's oracle at 512/2K/8K/32K.
   - Launch-equivalence: the same executable launched the lab way gives the same gate result.

**Composed identity (main 21:05).** The exact arm computes the base-model prefill, while the pair decodes with its trellis mix-B identity. An offloaded request therefore runs a composed identity. Its quality receipt is named `ds41-rtx-exact-v1` and uses the approved three-test criterion (noise-referenced, oracle-referenced with the absolute floor, paired aggregate) against the pair's own prefill at 2K and 8K, in one short pair turn plus a PC-BIG window. Until that receipt exists the preset records `quality_gate: null`. An RTX prefill service with the mix-B overlay identity (an engine request) would make offload bitwise with the pair.

## Known limits (component requests)

- **R12:** the service takes keys only as literal arguments. They are visible in the process command line on this machine. Mitigations:
  - loopback only;
  - keys are redacted in every state file, log and the UI;
  - the helper's spec file, the only copy on disk, is deleted once read.
- **R13:** the service has no `/metrics` and no `/api/status`. Stats come from `/health` and the gate replies.
- **R9:** the router forwards neither `reserve_tokens` nor per-replica decode keys.
- **TensorMeld (new):** a per-service maximum prompt length for offload.
- **Engine (new):** an RTX prefill service with the pair's mix-B overlay identity.
