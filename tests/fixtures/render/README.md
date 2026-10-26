The Qwen golden argv and its 13 explicit environment entries are sanitized from
the rfix r1-c1 receipt (same argv as r3; env adds the D-058/D-059 production switches). The preset retains its flag order, values and identity
pins, cross-checked with qwen_cmd.sh and qserve.py. Only paths, endpoints and the
elided calibration tokens have been substituted. The router curves and cache
block size come from that run's router-prod.json. No runtime environment is
inherited by the renderer; launchers must supply their sanitized base separately.

The DeepSeek golden is a synthetic two-rank launch using the pinned engine's
ds41-production-defaults.md contract: usb4gpu, device prefill, DSpark and the six
production identity environment entries. Other engine defaults remain unset.
The defaults document is not a full argv receipt. No wider context/slot
qualification is inferred: only context 4096 and slots 4 are accepted for this
preset until a named receipt qualifies other values. Qwen context stays 12288.

calibration.tokens contains three synthetic positional IDs. Its configured
control_path is the control machine's readable copy; path remains the node's
copy. Rendering checks its manifest size and sha256. Tests never use real model
files or GPUs. Example asset hashes and sizes are placeholders except for the
frozen Qwen sidecar/golden pins and the synthetic token file. Replace placeholders
with trusted asset manifests before use.
Each profile's release_id is the first 12 hex characters of its manifest SHA-256. The example
uses a synthetic all-zero ID; replace it with an installed release ID. Executable
paths always name releases/<release_id>/bin and never an activation symlink.

Asset names are model, sidecar, golden, vocab, calibration_tokens for Qwen and
ds41_model, dspark, ds41_vocab, overlay, trellis_k4, trellis_k5 for DeepSeek.
Profile api_key_files are node-local; router api_key_file and
replica_api_key_files are local to the selected router host. For remote router
placement, configure key paths on that machine. Rendering itself never reads
keys; validate_router invokes the real TensorMeld parser, which reads them.

Run: python -m pytest -q tests/test_config.py tests/test_render.py

G6 legacy oracle: `legacy-eb9ae96.json` freezes argv, explicit env, release id,
engine/binary pins, build component pins/options and expected refusals for every
shipped preset and the `ds41-rtx` service profile, with no cap, a 90 GiB cap and
an insufficient 8 GiB cap. Caps on profiles without a service exercise `up`'s
early refusal. Only pure renders run for service profiles; no engine, gate,
lease, network, model scan or GPU operation runs. Inputs are synthetic; the
clock, operator reservation, free RAM, process environment and cached model
header are fixed. RTX explicit env includes the legacy visibility variables,
as required by contract rev 21b section 7.1.7. Inherited env is cleared.

Generate independently using a separately prepared read-only checkout of the frozen revision
(PowerShell, repository root; replace `<baseline-checkout>` with that checkout):

```powershell
python tests/legacy_render.py --source "<baseline-checkout>" --output tests/fixtures/render/legacy-eb9ae96.json
python -m pytest -q tests/test_legacy_render.py
```

The generator imports Hyperloom only from `--source` in a fresh interpreter.
The stored fixture uses portable placeholders. `tests/portable_fixtures.py`
reconstructs its original values only in memory; the reconstructed bytes retain
SHA-256 `40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`.
The generator saves portable output by default; `--raw` is for local comparison
only. The test renders with `--raw` and compares LF JSON bytes against the
reconstructed frozen file; it never regenerates the oracle. Any difference needs reporting
and product review, not an automatic golden update. A second independent
generation at eb9ae96 was byte-identical; the integrated branch matched all
21 cases when this oracle was introduced. Paths and identities here are
portable fixture data. The privacy gate has no historical exemptions; all 22
previously exempt fixtures reconstruct their original bytes in memory.
IPv4 and IPv6 literals, including documentation and loopback addresses, also use portable placeholders. The
privacy gate validates address candidates in values and mapping keys with `ipaddress`; documentation ranges have
no exemption. Address substitutions expand only in memory before parser or byte comparisons.

The qwen-replicas-w5, ds41-pair-w5 and minimax-pair goldens use the same abstraction (docs/adapters.md):
qwen-replicas-w5 equals the qwen-replicas line; ds41-pair-w5 is the ds41-pair argv with the w5 identity
environment; minimax-pair spells out the serve's defaults, binds 127.0.0.1 and takes no key file. Its model
directory asset (mm_model) is added by tests/test_engines.py with a synthetic path.
