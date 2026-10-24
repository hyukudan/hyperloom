# Phase 2 acceptance on real hardware (2026-10-23/24)

**Setup**
- A Windows control machine and two Strix Halo nodes, `halo-1` (rank 0) and `halo-2` (rank 1), joined by two USB4 cables, layout 1 (host path, `gpu_direct` 0 on all four streams).
- Engine pin `9211b07e` (engine `main`, code identical to the earlier pin `70ec42b9`), release `b3da5cbcd3b2` on both nodes, binary sha256 identical.
- Every GPU process ran inside a lab PAIR turn with an `hl-` label. No root, and no kernel, module, configfs or layout changes.
- Turns queued under the lab rules of the day: no queueing while a campaign PAIR waited, and one turn holding all the arms of a measurement.

## Results (DeepSeek pair, preset `ds41-pair`, receipt "P17.5 production defaults")

**1. Golden render equals the pin's production line.**
`tests/test_render.py` checks the rendered rank 0 and rank 1 lines against `tests/fixtures/render/ds41-pair.golden.json`, which is taken from the pin's production-defaults contract. The pin bump to `9211b07e` touched no engine source and no part of that contract.

**2. Both ranks READY, production identity.**
Every launch below reached `READY` on both ranks (51-74 s with a warm page cache) with `trellis ds41-trellis-mix-v1{head=K5,shexp_w13=K4,shexp_w2=K4}:shexp,head (= ds41-trellis-mixB-v1)`, the production identity.

**3. Token regression gate** (first pass, 2026-10-23, same binary in both arms): 8 of 8 greedy prompts, 290 tokens, identical between the hyperloom launch and the lab launch. See `acceptance-1a.md`.

**4. Negative tests**
- **Mismatched releases.**
  - `hyperloom`: with `halo-1` on the new release and `halo-2` on the previous one, `up` refused before any GPU turn: `profile nodes must run the same release id`.
  - Engine hello, when forced (rank 1 on a copy of the binary with one appended byte): rank 0 emitted `{"event":"POISONED","reason":"the engine executables differ (SHA-256)"}`; neither rank became READY.
- **A same-size wrong asset**, using sparse decoys of the overlay's size inside the install root (lab files untouched):
  - both decoys verified with `hyperloom assets verify` (same digest on both ranks);
  - one byte written into the `halo-2` decoy (same size and inode): `up` refused before any turn with `stale manifest: mtime_ns changed`;
  - with the mtime restored, the metadata cache passed (the documented limit), and after `assets verify --machine halo-2` the next `up` refused with `manifest sha256 mismatch between pair ranks`.
- **A wrong layout, aborted after acquire.** The real layout cannot be changed (no configfs changes), so an operator fault file `<install root>/fault/usb4-layout.json` naming layout `gpu-direct` was placed on `halo-1`. `up ds41-pair` queued, acquired its PAIR turn, and the supervisor aborted one second later, before starting any engine: lease state `aborted`, reason `USB4 layout mismatch: gpu-direct (fault file)`, no processes. The fault file was then removed.

**5. Interleaved serve C1/C4 against the lab launch** (investigation threshold 3 %)

One PAIR turn, three arms in order A B A, release `b3da5cbcd3b2`:
- A: the lab-style launch (inherited user environment);
- B: the line `hyperloom` renders for the profile, with the supervisor's clean environment (`PATH`, `HOME`, the release's loader path and the six identity variables).

Bench: 8 fixed prompts, greedy, 192 tokens each, streamed straight to rank 0; C1 = 8 sequential requests, C4 = 2 rounds of 4 concurrent requests. Every request in every arm produced 192 tokens.

| tok/s | A1 | B | A2 | B vs mean(A) |
|---|---|---|---|---|
| C1 aggregate | 33.0 | 34.7 | 36.1 | +0.3 % |
| C1 decode per request | 37.8 | 39.1 | 40.4 | 0.0 % |
| C4 aggregate | 56.9 | 57.8 | 57.5 | +1.0 % |
| C4 decode per request | 21.6 | 21.7 | 21.0 | +2.2 % |

The hyperloom line is not slower in any cell. The A1-to-A2 spread on C1 (9 %) is larger than any B difference.

**Full path.** A separate turn ran `hyperloom up ds41-pair --router local` (supervisor, layout check, readiness, owner watch) and the same bench: C1 36.5 tok/s aggregate (40.8 per request), C4 55.9 (21.7). `down` ended the turn cleanly.

## Product changes made during the acceptance
- `hyperloom assets verify|status`, the trusted asset manifest and the pre-lease asset checks (`docs/assets.md`).
- `up --queue-timeout`: a busy lab FIFO held a PAIR waiter for more than an hour.
- `<install root>/fault/usb4-layout.json`: an operator fault file checked in addition to the real layout record, used for the wrong-layout test; it can only make a start fail.
- The release-mismatch refusal names each machine's release.
- `up` refuses, before any lease, a node whose deployed node tool differs from the control machine's checkout (the first wrong-layout run tested nothing: the nodes still ran the tool installed before the fault-file check). `hyperloom nodes update PROFILE` redeploys it, staged and renamed into place, and is refused while a lease is live.
