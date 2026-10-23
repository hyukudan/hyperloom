# Phase 1a acceptance on real hardware (2026-10-23)

**Setup**
- Machines: a Windows control machine and two Strix Halo nodes, `halo-1` and `halo-2` (Ubuntu 26.04.1, Python 3.14, ROCm 10.1.0, gfx1151).
- Engine pin `70ec42b9`, TensorMeld `7165e98`, hyperloom at the commits listed below.
- Every GPU process ran inside a lab single GPU turn with an `hl-` label, at most 15 minutes. No root, and no kernel, module, configfs or layout changes.

## Results (Qwen3.8-Flash-Next replicas, preset `qwen-rep-rfix`)

**1. Build**
- `hyperloom install qwen-replicas --build-on halo-1` built `strix-hip-decode-token` and ran 4 host tests (all passed) in about 2.5 min.
- Two builds produced the same release id, `3533f2a1a303`.
- `push-release` copied it to halo-2, and the binary sha256 was identical on both nodes.

**2. Router on the control machine** (`up --router local`)
- Ready 2.5 min after `up` with a cold page cache.
- Router `/health`: 2 of 2 replicas up.
- Four parallel chat requests all completed, two on each replica.
- `tensormeld bench-replicas` (4 users × 2 turns, 128 tokens, 3000-character documents):

| Metric | Value |
|---|---|
| Requests OK | 8 of 8 |
| Aggregate throughput | 54.9 tok/s |
| Common-window throughput | 145.1 tok/s |
| MTP acceptance | 0.652 |

**3. Router on halo-1** (`up --router halo-1`)
- Ready in 64 s with a warm page cache.
- The router listened on halo-1's private address with a key.
- The same bench: 8 of 8 OK, 52.3 tok/s aggregate, 130.9 tok/s common window. This is the same order as run 2; the sample is small and was not an interleaved A/B.

**4. Failure handling**
- **`kill -9` of the halo-2 engine:**
  - its supervisor stopped with `process-exit:halo-2`;
  - the GPU turn was released;
  - the router reported 1 of 2 replicas up and kept serving halo-1.
- **Deadline 300 s:** the halo-1 turn ended after 302 s (the supervisor's stop path).
- **`down`:** every turn ended, no engine or router process was left, and the router port was free.

**5. Token regression gate** (same binary, two launchers)
- The lab launcher with the rfix environment, and hyperloom.
- 8 greedy prompts × 64 tokens with `return_token_ids`.
- **All 8 identical (478 tokens).**

## Bugs found and fixed during the acceptance

| Found | Fix | Commit |
|---|---|---|
| No CLI to pin host keys | `hosts scan\|pin` with the fingerprint confirmed on the machine | 0f0d52e |
| Git's MSYS ssh mangled quoted `-o` values from a Windows command line | prefer the native Windows OpenSSH; quote only when needed | 0f0d52e |
| Readiness counted queue time; poll every 0.2 s (one ssh per poll) | queue-aware wait, 600 s once acquired, 2 s poll | fe65854 |
| `down` could not cancel an `up` waiting for its turn; a killed `up` left a queued turn | stop marker, lease ids recorded at once, signal and console-close cleanup, `down --force` | a18e0e1 |
| `lab_dir` / `peer_ssh` accepted by one loader only | one schema | a18e0e1 |
| Local router had no deadline | it stops at the profile deadline or when all replicas stop | a18e0e1 |
| Clients had no way to get the router key | `keys export`, `endpoint` | a18e0e1 |
| Doctor: wrong ROCm package name; USB4 describe not validated | fixed for both describe formats | a18e0e1 |
| **The capability check ran `<binary> --help`: the DeepSeek serve binary treated it as a run and opened the USB4 streams outside a GPU turn (no turn was active, nothing was damaged, and the streams checked healthy afterwards)** | static flag check in the ELF binary; a test forbids executing a built target | 1b954a1 |
| A CRLF fixture broke a Linux shebang | LF everywhere (`.gitattributes`) | 1b954a1 |

## Open
- Rollback and uninstall have not been run on hardware yet (unit-tested only).
- A transient "expected one JSON object" reply during the first install poll did not reproduce. The error now shows the first 200 bytes of the reply.
- Interleaved A/B performance comparison of the two router placements (SPEC §6 1a.5).

## Phase 2 first acceptance: DeepSeek V4.1 Flash pair (preset P17.5 production defaults)

- **Build:** `hyperloom install ds41-pair --build-on halo-1` built strix-trellis and `strix-deepseek41-serve` and ran the host tests. Release `d2c8d71531bc` was pushed to halo-2 with identical shas. The capability check is static (nothing executed).
- **Start:** `up ds41-pair --router local` took one `gpu-turn-pair.sh` turn (`hl-ds41-*`).
  - The first attempt **refused to start** on the layout check, because the lab's layout file has per-stream records; this was fixed in c51a1b6.
  - After the fix, layout 1 was verified on both nodes after acquire, and both ranks were `READY` about 50 s after the turn began.
  - Both ranks connected over the 4 host streams (`usb4gpu` → host path, layout 1).
- **Identity:** `/api/status` `trellis_identity` = `ds41-trellis-mix-v1{head=K5,shexp_w13=K4,shexp_w2=K4}:shexp,head (= ds41-trellis-mixB-v1)`, which is the production identity.
- **Serve through the router on the control machine:**

| Run | Requests OK | Aggregate | Common window |
|---|---|---|---|
| bench C1 (2 requests) | 2 of 2 | 25.6 tok/s | — |
| bench C4 (8 requests) | 8 of 8 | 26.4 tok/s | 72.4 tok/s |

  The aggregate figures are wall-clock and include prefill of 3000-character documents; this is a smoke test, not a performance claim.
- **Token regression gate:** the same binary launched lab-style (inherited user environment, the same flags and identity) against hyperloom (clean environment), 8 greedy prompts: **all 8 identical (290 tokens)**.
- **Stop:** `down` → both ranks printed `STOPPED`; the pair turn was 2 min 44 s; no process was left on either node; `usb4stream-describe` shows 4 of 4 streams openable on both nodes afterwards.
- **Not yet run for the pair:** the router on a node (covered for Qwen), the negative tests (mismatched releases, same-size wrong asset), and interleaved C1/C4 against the lab launch.
