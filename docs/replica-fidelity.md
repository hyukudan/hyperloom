# Placement replica fidelity

An owned placement service uses the native replica serve spool. The legacy prefill service keeps its preset's
HTTP `/v1/prefill` oracle (state SHA256, next token, logits FNV, state size and exact-mode checks). These are
different engine protocols and different references.

`hyperloom prefill gate PROFILE --prompts DIRECTORY` uses `DIRECTORY/replica-oracle.json` for a running placement
replica. This is a Hyperloom reference-bundle manifest, not a new replica request format. It pins a reference
captured independently of the candidate run. It must be supplied locally; Hyperloom never creates expected tokens
from the candidate or treats a successful ping as fidelity evidence. The adapter preset must publish
`fidelity.oracle_sha256`, the SHA256 of the bundle manifest's exact bytes. An arbitrary self-pinned bundle cannot
replace that release-owned oracle; a missing published oracle pin refuses before a request is queued.

```json
{
  "schema": "hyperloom/replica-oracle-v1",
  "engine_sha": "<published 40-digit release SHA>",
  "chat_prompt": 96,
  "chat_steps": 128,
  "chats": [0, 1],
  "files": {
    "state": {"file": "state.bin", "sha256": "<SHA256 of imported state>"},
    "sequences": {"file": "seqs.tsv", "sha256": "<SHA256 of native sequences>"},
    "expected": {"file": "expected.jsonl", "sha256": "<SHA256 of reference qualification output>"}
  }
}
```

All three files must be inside the bundle directory. `seqs.tsv` is the replica's native format: a sequence name,
a tab, then comma-separated token IDs per line. Each selected zero-based sequence must contain at least
`chat_prompt` tokens. The state is the native `--import-state` file. The expected JSONL is native `--qual` output
from a qualified `chats0` reference: exactly one `chat_end` for each selected chat, keyed `plain:0#0-INDEX`, with
`chat_steps + 1` tokens in `toks` (the prompt's greedy token followed by the committed decode tokens), plus the native
`position`, `next_token` and canonical-state `digest`. Other native
events can be retained in the file. The bundle SHA must equal both the published adapter pin and the running
service's recorded release SHA. The release owner must bind the reference to the qualified model/content and
numerical configuration; file hashes alone do not establish that provenance.

The gate checks ready PID/load identity and `--ping`, then submits one native request with `--import-state`,
`--seqs-file`, `--chats`, `--chat-prompt`, `--chat-steps`, `--phases`, `--qual` and a fresh `--json` output path.
The phases are `chats0` for plain workers and `chats0,schats0` when the launch includes `--draft`. In the latter
case the replica also applies its own canonical plain/speculative token comparison. Hyperloom requires every
plain arm and every `spec:schats0#0-INDEX` arm to match the pinned tokens and canonical end state in full. Missing, repeated, short,
foreign or mismatching arms fail. Request rejection, nonzero native failures, fatal replies, a changed load,
missing output, native failed-gate events or a failed final ping fail too. It does not retry an ambiguous request.
The unique native output file remains in the owned spool directory as local evidence: the worker keeps its stream
open until the next workload request, so immediate deletion is unsafe on Windows.

Before and after the request, the owned PID must have positive attributable process VRAM on every touched UUID,
with no activity on another device. Missing counters, zero usage on a touched device and attribution errors fail;
device totals cannot substitute. These checks use the placement observer alongside the existing lifetime watchdog.
They do not establish continuous visibility between samples, model quality qualification or the full §5 X1 campaign
(per-position logits, fixed replay, forced divergent controllers, every rows/kept pair and graph/eager coverage).

The shipped adapter still has a null release SHA and cannot launch. The inspected native implementation supplies
the spool, prompts and token output, but does not publish a release-bound reference bundle or a normative corpus
with expected tokens for this gate. The engine owner must provide the qualified native imported state,
sequence corpus, complete `chats0 --qual` reference tokens and their model/configuration provenance, bound to
the release SHA that the release owner publishes. The adapter must also publish the exact manifest's
`fidelity.oracle_sha256`. No new native HTTP endpoint or expected-token flag is needed.
Placement flags/components/receipts support and native placement qualification remain separate release blockers.
Until those inputs exist, the gate refuses a missing or invalid oracle; the CPU fake-protocol tests qualify only
Hyperloom's integration and failure handling.
