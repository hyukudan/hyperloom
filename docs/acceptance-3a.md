# Phase 3a acceptance: results on real hardware

Branch `hl/phase3a`, 2026-10-23. Engine pin `9211b07e`. RTX PRO 6000 (WDDM), CUDA 13.0.88, MSVC 14.44.

## W1: CUDA build through hyperloom (CPU only)

`hyperloom prefill build ds41-rtx --source <clean 9211b07e checkout>`:
- runs in a Job Object under the reservation's free-commit watchdog, at Idle priority, with `-j8`;
- builds `strix-cuda-ds41-prefill-service.exe`, sha256 `93af46dd56dd96997b5f1b5c2565cae9647cf52514503982f6fda4a226d98788`;
- publishes the release once to `<install_root>\cuda\9211b07e13c6`.

The first real build found three Windows PowerShell 5.1 issues, all fixed in the script and the Python wrapper:
- `Get-FileHash` does not load with an inherited PowerShell 7 module path;
- `-Encoding utf8` writes a BOM;
- JSON arrays come back as one object.

A file committed with CRLF under `eol=lf` makes every clean engine checkout look modified; line-ending-only differences are now ignored.

## W2: the service on the PRO (PC-BIG 22:05–22:12)

| Check | Result |
|---|---|
| A. Service launched by hyperloom (render, helper, Job Object, watchdog, keys redacted), fidelity gate `ds41-rtx-exact-v1` | **PASS**: state sha256, next token, logits FNV and state size equal the exact-arm oracle at 512 / 2K / 8K / 32K; `fast` all false |
| 1. Visible only on the PRO | **PASS**: the driver's per-process rows show the service only on the PRO's UUID |
| 4. Launch-equivalence: the same executable with the lab window's own command line | **PASS**: the same four states |
| Engine branch `--overlay c-u6` (pair prefill identity) | 12 states captured (oracle 2K/8K + the 10 quality cells); repeatable byte for byte; the 8K logits FNV starts `50e7381b`, the pair's own 8K prefill FNV in the numbers ledger |

Timings in this window are not measurements: the PC was shared and the pinned tier was 66 GB.

## Pending

- W3a (pair turn): the pair's own prefill states for the bitwise comparison with the `--overlay` service.
- W3 (pair + PRO): `prefill placement` (8K offload with no fallback, short prompt local) through `hyperloom up ds41-rtx`.
