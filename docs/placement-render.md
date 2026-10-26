# Placement rendering

The opt-in `rtx-decode-ds41-placement` adapter is for the RTX-alone DS decode replica. Its `engine_sha` is deliberately
null until claudeinfo-release publishes the pinned release. Rendering refuses while that prerequisite is missing.
The legacy prefill service and its immutable G6 oracle are unchanged. The new preset has a distinct schema and is
not included in the list of qualified legacy presets.

`recipes.runtime.Request` supplies the local machine, cached `Files`, and the already admitted per-device supervision
policy to `recipes.launch.render_placement` / `rtx.render_service(placement_request=...)`. Rendering reads cached JSON
and stats content files. It emits one `--placement`, `--content-manifest`, and `--receipts`; allocation flags and sugar
are refused. It never expands placement into legacy capacity flags. Only touched UUIDs are visible, in full PCI order,
with `CUDA_DEVICE_ORDER=PCI_BUS_ID`. Legacy rendering keeps its exact environment.

The core engine identifier is `strix-cuda-ds41-replica`. Its placement-v1 document is kept intact; it does not carry
a manifest hash. The components-cache sidecar binds and validates the manifest independently. Device byte budgets
are carried from the core into rendering, physical-capacity checks and watchdog comparisons.

Evaluation returns an admitted decode decision containing the plan, artifact references and supervision policy.
`up --recipe` launches that local decode replica through the existing owned service helper, locks, per-device
gates and watchdog. It starts no remote decode engines or parked-import prefill service for this RTX-alone route.
The loopback replica is the profile endpoint. The local per-effective-recipe `placement-inputs` cache supplies each
route's `components` reference, `selected_content`, `receipts` file and optional non-sizing `argv`; the selected
`model` (or `target`) is the model argument. Artifact publication happens after pure admission and rendering.
The installed CUDA release manifest must name the published commit and the descriptor's executable hash.

The launch owner can ingest realised checkpoint-1 events with `recipes.launch.ingest_context`. The JSONL event shape
is `event=context`, `checkpoint=1`, the six context identity fields (`arch`, `device_name`, `driver`, `platform`,
`build_sha256`, `signature`), `ctx_observed` in bytes, and an attributable `method` (`pdh-process-dedicated`, `nvml-process`, or
`amdsmi-process`). Checkpoint 2 and other events do not enter the context allowance. Samples remain oldest first;
Tierstream owns their aggregation. Input logs and context documents remain local.

The single-/two-device G6 fixture contains placeholders only. Tests generate physical identities and absolute paths
at runtime and use a test-only release SHA; these do not qualify a production release.
