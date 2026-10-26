# Placement rendering

The opt-in `rtx-decode-ds41-placement` adapter is for the RTX-alone DS decode replica. Its `engine_sha` is deliberately
null until claudeinfo-release publishes the pinned release. Rendering also requires an explicit `placement-v1`
capability for that release. The selected replica at a919c257 does not parse the placement flags, so the shipped
capability list is empty. A published SHA alone does not enable launch.
The legacy prefill service and its immutable G6 oracle are unchanged. The new preset has a distinct schema and is
not included in the list of qualified legacy presets.

`recipes.runtime.Request` supplies the local machine, cached `Files`, and the already admitted per-device supervision
policy to `recipes.launch.render_placement` / `rtx.render_service(placement_request=...)`. Rendering reads cached JSON,
stats content files and opens native input files to check readability without reading their contents. With a
placement-capable release it emits one `--placement`, `--content-manifest`, and `--receipts`; allocation flags and sugar
are refused. It never expands placement into legacy capacity flags. Only touched UUIDs are visible, in full PCI order,
with `CUDA_DEVICE_ORDER=PCI_BUS_ID`. Legacy rendering keeps its exact environment.

The core engine identifier is `strix-cuda-ds41-replica`. Its placement-v1 document is kept intact; it does not carry
a manifest hash. The components-cache sidecar binds and validates the manifest independently. Device byte budgets
are carried from the core into rendering, physical-capacity checks and watchdog comparisons.

Evaluation returns an admitted decode decision containing the plan, artifact references and supervision policy.
`up --recipe` launches that local decode replica through the existing owned service helper, locks, per-device
gates and watchdog. It starts no remote decode engines or parked-import prefill service for this RTX-alone route.
The native spool is the profile endpoint (`protocol=ds41-replica-spool-v1`, `directory`); this route starts no HTTP
server or TensorMeld router. The local per-effective-recipe `placement-inputs` cache supplies each
route's `components` reference, `selected_content`, `receipts` file and optional non-sizing `argv`; the selected
`model` (or `target`) is the model argument. Artifact publication happens after pure admission and rendering.
The installed CUDA release manifest must name the published commit and the descriptor's executable hash.

Pure rendering validates the native startup argv against the parser at a919c257. It requires the positional model,
`--import-state` and the full-model `--receipt`. The default phases also require `--ref`, `--chats` and
`--seqs-file`; explicit phases retain their native input requirements. Every input occurrence must name a readable
regular file, including `--routes`, `--residency-seed`, `--winpf-prior` and `--controller-prior FILE[:CAP]`.
The controller's `none` and `builtin[:CAP]` values need no file. Duplicate inputs cannot hide a missing earlier file.
Missing values, unknown options and native-unsupported `--option=value` spellings are synchronous schema refusals.
Output options name future files; rendering does not create them. File formats, state capacity, reference coverage,
receipt contents and request shape bounds remain native validation obligations.

The allocation and qualification controls found in that parser are placement-owned and conflict even when spelled
with `=` or repeated with an apparently matching value:

- Capacity and memory policy: `--arena-gib`, `--spares`, `--host-spares`, `--pressure-spares`, `--predict`,
  `--layers`, `--max-rows`, `--max-positions`, `--max-pinned-gib`, `--host-margin-gib`, `--cache`, `--engram`,
  `--engram-cache-mb`.
- Drafter allocations and graph materialisation: `--draft`, `--draft-device`, `--draft-vocab`, `--draft-keep`,
  `--draft-keep-from`, `--warmup-graphs`, `--test-hooks`, `--timing`, `--stream-hashes`, `--chat-logits`,
  `--records-inline`, `--route-stats`.
- Kernel dispatch and the qualified graph/stream envelope: `--fuse-hc`, `--fuse-shared`, `--router-v2`,
  `--router-pf`, `--router-pf0`, `--pdl`, `--pdl-np`, `--pdl-regs`, `--shared-fork`, `--qakv-s1`, `--qakv-auto`,
  `--attn-fork`, `--ob-expand`, `--moe-expand`, `--ob-one`, `--attn1`, `--attn-h1`, `--attn-pf`, `--hc-cluster`,
  `--hc-partial5`, `--moe-v1b3`, `--dsrtxk-off`, `--fuse-off`.

The existing sizing aliases and placement sugar remain refused. Request lengths, phase choices and scheduling
controls do not replace the placement's fixed capacities; the native placement implementation must bound them by
the admitted shape set. In particular, `--draft` must not conditionally allocate an unplanned drafter: the native
placement loader must select/load the admitted drafter from cached content, rather than accept this argv override.
Until that exists, speculative startup phases cannot satisfy their legacy `--draft` requirement through extra argv.

The native command is `replica MODEL [options] --serve DIR [placement files when capable]`. MODEL is always argv[1].
The helper substitutes a fresh directory under its own runtime directory for DIR. No HTTP, port or authentication
options are sent to the replica. The existing request port (8096 by default, optionally `placement-inputs.port`)
still undergoes pure admission and helper race checks; this adapter opens no listener on it.

`hyperloom.replica_spool.Client(directory, alive=...)` forwards native runtime arguments as one UTF-8 argument per
LF line: write `queue/NAME.tmp`, rename to `queue/NAME.req`, then await `done/NAME.json`. The worker owns the move
through `run/NAME.req` into `done/NAME.req`. Responses, including rc 2 request rejections, are returned intact;
request outputs are at the paths supplied with native options such as `--json`. Requests are never retried after
timeout. Readiness comes from `ready.json` with lowercase `event=ready` and a worker-owned `lock` directory.
The helper checks `--ping` completion while idle and sends `--quit` before its bounded process-stop fallback.
Passive status reads the spool and helper state without enqueuing work. There is no HTTP `/health` check or
uppercase READY requirement on this route. Native argument forwarding does not provide text tokenization or an
OpenAI HTTP API. Clients needing those APIs require a separately specified bridge; the current launch path does
not route HTTP requests to this engine. The legacy HTTP prefill adapter keeps its existing protocol.

Replica prerequisites at a919c257: implement the three placement-file options as load-time options, validate and
apply their documents before allocation, and derive the placement-owned capacity (including max_rows and
max_positions), drafter selection and qualified dispatch/graph settings from the admitted plan. Its current serve
parser requires `--max-positions`, `--max-rows`, and `--import-state`; the cached
non-sizing argv must supply required native runtime inputs, while placement-owned sizing must come from the new
placement loader. Preserve the native request allowlist and spool protocol. Publish/qualify that implementation
before adding the capability and release pin here. This change supplies neither native placement support nor GPU
qualification.

The launch owner can ingest realised checkpoint-1 events with `recipes.launch.ingest_context`. The JSONL event shape
is `event=context`, `checkpoint=1`, the six context identity fields (`arch`, `device_name`, `driver`, `platform`,
`build_sha256`, `signature`), `ctx_observed` in bytes, and an attributable `method` (`pdh-process-dedicated`, `nvml-process`, or
`amdsmi-process`). Checkpoint 2 and other events do not enter the context allowance. Samples remain oldest first;
Tierstream owns their aggregation. Input logs and context documents remain local.

The single-/two-device G6 fixture contains placeholders only. Tests generate physical identities and absolute paths
at runtime and use a test-only release SHA; these do not qualify a production release.
