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

Run: python -m unittest tests.test_config tests.test_render -v

The qwen-replicas-w5, ds41-pair-w5 and minimax-pair goldens use the same abstraction (docs/adapters.md):
qwen-replicas-w5 equals the qwen-replicas line; ds41-pair-w5 is the ds41-pair argv with the w5 identity
environment; minimax-pair spells out the serve's defaults, binds 127.0.0.1 and takes no key file. Its model
directory asset (mm_model) is added by tests/test_engines.py with a synthetic path.
