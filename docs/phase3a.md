# Phase 3a: the RTX prefill service

Status: implemented on `hl/phase3a`, 2026-10-23. The hardware acceptance (below) has not run yet.

## What it does

`hyperloom up <profile>` runs a resident prefill service on this machine's RTX PRO when the profile has a `prefill_service`:

1. The decode side starts as usual: pair lease, layout check, both ranks `READY`.
2. The service starts. It binds to 127.0.0.1 only, inside a Job Object, with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<PRO uuid>`. It is ready when it prints its `READY` event and `/health` answers `ok`.
3. The router starts with `prefill.policy auto`. Its offload target is the service, using the service's measured profile and the decode profile's measured curves. The handoff goes to rank 0's `/admin/import-state` with rank 0's key.

`hyperloom down` stops the router first, then the service, then the engines.

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
  - `--vram-cap-gib`;
  - `--host-gib` = min(80, `host_gib`, free physical memory − 20), the same rule the lab window used;
  - `--host-commit-floor-gib` = floor + 10;
  - the watchdog's limits.
- **Watchdog.** Every 2 s it checks the service's PID. It kills that process, and only that process, in four cases:
  - free commit falls below the floor;
  - its VRAM goes above the cap (under WDDM it measures device use above the starting baseline);
  - it becomes visible on any GPU other than the PRO;
  - the reservation ends.
- **Router.** The service is loopback-only, so the router must run locally (`--router local`).
- **Context.** `ds41-pair` qualifies `--context 4096` only. Offloading an 8K prompt needs a context of at least prompt + output reserve + 1, and the router sends no reserve (TensorMeld R9), so the pair reserves its default max tokens (1024). The **unqualified** `ds41-pair-long` preset (`--context 10240 --slots 1`) exists for that. The Phase 3a hardware run qualifies it.

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
| `hyperloom prefill build <profile> --source <engine checkout>` | `windows/build-cuda-front.ps1`. The checkout must be clean and at the preset's pinned commit. Runs vswhere, vcvars64, the CUDA 13.0 check, then Ninja at Idle priority. Writes `MANIFEST.json` with the binary sha256. |
| `hyperloom prefill gate <profile> --prompts <dir>` | The fidelity gate against the running service. For each oracle length it checks the state sha256, next token, logits FNV and state size, then that the process is visible only on the PRO. |
| `hyperloom prefill placement <profile>` | Acceptance 2–3 through the running router. An ~8K-token prompt must be placed `offload` (router header), served by the service, with no fallback in the router counters. A short prompt must stay `local`. |

## Acceptance (SPEC §6 Phase 3a)

1. **Only on the PRO.** The gate's `visible_on` / `only_pro`, plus the watchdog log.
2. **8K offload with no local fallback.** `prefill placement`.
3. **Short prompts local.** `prefill placement`.
4. **Exactness.**
   - Named gate `ds41-rtx-exact-v1`: the service's states equal the qualified exact arm's oracle at 512/2K/8K/32K.
   - Launch-equivalence: the same executable launched the lab way gives the same gate result.

**Open: composed identity.** The exact arm computes the base-model prefill, while the pair decodes with its trellis mix-B identity. An offloaded request therefore runs a composed identity, and that identity needs its own named quality receipt (KL / top-1 of the decode after import vs after the pair's own prefill). The preset records it as `quality_gate: null`.

## Known limits (component requests)

- **R12:** the service takes keys only as literal arguments. They are visible in the process command line on this machine. Mitigations:
  - loopback only;
  - keys are redacted in every state file, log and the UI;
  - the helper's spec file, the only copy on disk, is deleted once read.
- **R13:** the service has no `/metrics` and no `/api/status`. Stats come from `/health` and the gate replies.
- **R9:** the router forwards neither `reserve_tokens` nor per-replica decode keys.
