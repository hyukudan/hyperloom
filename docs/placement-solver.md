# Operator-selected placement rows

`hyperloom solve RECIPE --route ROUTE --bundle NAME` runs the packaged `ts-recipe solve` on cached CPU inputs.
`NAME` names a local `run/solver-inputs/NAME.json` containing `probe`, `inputs`, and optional `desc`, `context`, and
`identity` and `measurement_keys`. The inputs use Tierstream's solver input schema; each configuration supplies its exact cached components.
JSON document references are relative to the bundle. This command never acquires descriptors or measurements.

`measurement_keys` maps executing set ids (plus `store` for host storage) to local
`{uuid, driver, xfer_sha256, store_id}` identities. The xfer SHA is Tierstream's xfer-v1 identity. Current probe
UUID/driver, `file_store`, and per-set `xfer_sha256` take precedence over old cache keys; unknown identities and
exact-key misses supply no figures. Solving replaces inline transfer figures with matching cache hits, freezes
those figures and provenance in its snapshot, and passes them to every core choice. Pick rechecks that snapshot;
changed cached measurements require another solve. Missing measurements remain `unpredicted`.

The CLI and web planner display the core's original row order, placement/refusal, predictions, estimates and
`unpredicted`. Solving saves a local selection snapshot, not a recipe. To save a reviewed row, use
`hyperloom solve --selection DIGEST --pick ROW --save-layer NAME`, or the planner's explicit Pick and save button.
The bridge calls `ts-recipe solve --pick ROW --emit-overrides FILE` (placement/v1-f1, fcdc54e API). Tierstream applies
the emitted overrides and verifies semantic placement equality before emitting them. Hyperloom checks that the
recipe, layers, configuration, JSON inputs, cached components and content metadata are still current and the fresh
solver document is unchanged, then saves those exact LF bytes. It does not recreate quotas or compare placement
hashes after quota materialization. Exact arena and FILE unit counts, context, sequences and plain/spec rows belong
to the core writer. Assists, budgets and component destinations cannot be changed by this writer.

Hypothetical FILE rows and refused rows have no save action. A lack of predictions alone does not hide a feasible
placement. Grants remain operator-owned local configuration; no planner action grants a turn or enables an assist.
The `recipes.service.plan` API also accepts `solver_bundle` and `solver_route` for an explicit solve preview.

## Artifact privacy

Snapshots, placement documents, content manifests and realised logs stay under the local runtime directory.
New placement fixtures use placeholders; tests construct UUIDs, PCI addresses and absolute paths at runtime. No
signed operational document is rewritten for publication. Both web device views share physical identity resolution
(UUID, PCI, configured id). A name fallback is retained for other device classes; an unmatched 4090 always retains
legacy exclusion and cannot inherit another 4090's flags through its name.

`tests/test_placement_privacy.py` scans tracked and newly added recipe/profile/receipt/fixture data for UUIDs, PCI,
host/user fields, local host/user names and absolute paths. No artifact is exempt. The 22 historical fixtures,
including the frozen eb9ae96 G6 oracle, use portable placeholders. Tests reconstruct their original values in
memory; all 22 retain their original byte hashes after reconstruction. The historical allowlist has been removed.
