Placement editing and the CPU core bridge
========================================

Placement recipe leaves can be edited through the existing overrides, `--set`, save-layer and derived-copy
operations. A layer can create an absent assist or component table; the effective recipe must still validate.
`components.<id>.units` preserves an exact solver quota. A later `units` replaces `gib` (and conversely); both
in one layer are refused. Profile guards protect a tuned route's layout, including assists, components, row and
sequence counts, and host budgets. Optional devices use `{"unbound": true}` profile identity markers when absent.

`hyperloom.recipes.placement.place` and `.solve` accept cached input documents or paths and return
`PlacementOutput(document, text)`. They require the authoritative core. Discovery uses `pc.recipe_core`, then
`HYPERLOOM_TS_RECIPE`, then `ts-recipe` on PATH, followed by legacy `HYPERLOOM_SS_RECIPE` / `ss-recipe` discovery.
A missing explicit executable is an error for placement. An older executable without placement/JSON-output
support is also refused; there is no Python placement planner or diagnostic-stub fallback.

Every place/solve invocation uses a private `--json-out` file. `--explain` (place) and `--table` (solve) provide
display text from that same invocation. JSON is required even when stdout contains a display or happens to be
valid JSON. Core placement errors, including `PL_E_*` prefixes and detail lines, pass through unchanged.
This bridge does not run an engine, refresh caches, measure hardware, or enable placement launches. The legacy
adapter's placement refusal remains until the later admission/rendering work is complete.

CPU validation with an existing compatible Tierstream build:

```powershell
$env:HYPERLOOM_TS_RECIPE = '<tierstream build>/ts-recipe.exe'
$env:HYPERLOOM_PLACEMENT_GOLDEN = '<spec>/wave4/placement/golden'
python -m pytest -q tests/test_recipes_placement_edit.py tests/test_recipes_placement_core.py tests/test_vramplan.py
```

The bridge tests compare literal G4 J/J2 results and JSON/text output, and exercise SV1 costs/curves through solve.
When the sibling CPU `ts-test-place` binary is available, they also run its G4, SV1, solver and profile-guard groups
against private copies of the contract's frozen vectors. Tierstream performs all arithmetic and comparisons;
Hyperloom imports no reference planner. Without the environment variables these external integration checks
skip; invocation/error tests and editor tests still run. Existing desc-v1/vramplan parity tests are unchanged.
