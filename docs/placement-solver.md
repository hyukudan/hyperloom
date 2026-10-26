# Operator-selected placement rows

`hyperloom solve RECIPE --route ROUTE --bundle NAME` runs the packaged `ts-recipe solve` on cached CPU inputs.
`NAME` names a local `run/solver-inputs/NAME.json` containing `probe`, `inputs`, and optional `desc`, `context`, and
`identity`. The inputs use Tierstream's solver input schema; each configuration supplies its exact cached components.
JSON document references are relative to the bundle. This command never acquires descriptors or measurements.

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
signed operational document is rewritten for publication. Web device exclusion follows configuration `excluded`,
with no GPU model-name exclusion.

`tests/test_placement_privacy.py` scans tracked and newly added recipe/profile/receipt/fixture data for UUIDs, PCI,
host/user fields, local host/user names and absolute paths. There are 22 historical synthetic fixtures that already
contain such values, including the frozen eb9ae96 G6 oracle. `tests/fixtures/privacy-legacy.json` pins their exact
existing bytes; changed and new fixtures receive no exemption. Therefore this gate proves no new privacy-bearing
artifacts, not a repository-wide zero-identifiers claim. Removing the historical exceptions conflicts with keeping
the legacy render goldens unchanged and remains a separate migration.
