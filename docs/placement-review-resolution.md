# Placement review resolution

Review: `review-claude-hl-1.md`, against contract rev 21c. Worktree base remains `2337186` on `hl/placement`.
No commit or push was made. The production adapter still has `engine_sha: null` and no capabilities. All new
lifecycle, admission and native-interface coverage uses CPU fakes; this work supplies no engine/GPU qualification.

## Validation

- Authoritative full-suite interpreter: Python 3.13, pytest 8.4.2. Before: **46 failed, 2576 passed, 69 skipped**
  (`pytest-claude-before-py313.log`). Its 46 failing IDs exactly match the supplied `pytest-fix6.log`.
- Final full suite: **46 failed, 2618 passed, 69 skipped**, one warning, in 486.21 s
  (`pytest-claude-after-py313.log`). `pytest-claude-failure-comparison.log` proves both full runs have exactly the
  supplied 46 failing IDs, with no added or missing failures. There are 42 additional passing cases.
- G6: **21 rows byte-identical** to the frozen eb9ae96 oracle, regenerated in a fresh interpreter by
  `tests/test_legacy_render.py`. Restored oracle SHA-256:
  `40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`.
  `tests/fixtures/render/legacy-eb9ae96.json` is unchanged.
- Offline native parser audit: **97 options / 43 source files** agree with the a919c257 snapshot and documentation
  (`python tools/check_replica_args.py --source <replica>`). No source outside this worktree was modified.
- Reorganisation: **2726 collected before / 2726 immediately after the moves**. All **896 pre-move review cases**
  remain present after translating module names; all **45 moved test functions** had unchanged ASTs at the move.
  Later regression additions bring collection to **2733**. Relative to the original full suite's 2691 cases,
  that is 42 additional cases, with no original case removed.
- Focused checks include `pytest-claude-subjects-final.log` (303 passed, 2 skipped),
  `pytest-claude-final-refusals.log` (661 passed), `pytest-claude-measurements-final.log` (37 passed, 2 skipped),
  and `pytest-claude-final-regressions.log` (57 passed, 1 skipped). These overlap; they are not additive counts.
- An initial exploratory run with PATH's Python 3.11 and its different plugin set reported 48 failures,
  2577 passes and 70 skips. Python 3.13 reproduces the review's baseline and is used for the before/after comparison.
  TEMP was not changed and remains outside the repository.

## Findings

| Item | Change (file:line) | Regression coverage | Open points |
|---|---|---|---|
| 1. Controller/console recovery | `hyperloom/recipes/launch.py:300` enters `_up_handlers`; `:304` starts `_start_up_watch` immediately after the first state write. Handlers unwind with the instance lock. | `tests/test_placement_lifecycle.py:24`: real `up_watch` and instance locks with fake detached/service launch; controller death removes starting ownership and stops the fake helper, readiness cancellation rolls back, launch failure removes state, existing state preserves its bytes without publication/helper, and success becomes running. `:108` retains the integrated real helper/watchdog with fake child. | Closed with CPU lifecycle evidence. Native/GPU qualification remains external. |
| 2. Cleanup order/errors | `hyperloom/rtx.py:892` unwinds spec/watchdog/child/events/terminal state before device/lifetime locks. `:910` re-raises the first cleanup exception for legacy; placement retains grouped failures. | `tests/test_placement_cleanup.py:34`: ordering assertion for legacy and placement, including simultaneous legacy failures, terminal-write failure, observer/log failure and every lock release. | None. |
| 3. Published parser identity | `hyperloom/recipes/runtime.py:44` requires `engine_sha == replica_args.AUDITED_REVISION`; `hyperloom/recipes/replica_args.py:16` records that identity. `tools/check_replica_args.py:82` binds it to the audited snapshot revision. | `tests/test_placement_parser_release.py:13` accepts the audited SHA and rejects another published SHA with `PL_E_CAPABILITY`; `:23` checks runtime/snapshot drift; `:32` retains transitive native fingerprint checks. | A different published release needs an explicit parser re-audit and mirror/snapshot repin before qualification. The production release is still unpublished. |
| 4. Byte/GiB/grant bounds | `hyperloom/placement_supervision.py:21` requires exact integer byte-cap coverage; `:30` bounds byte caps by Tierstream's round-half-up GiB conversion and by the rounded grant; the existing GiB/grant refusal remains. `hyperloom/recipes/measurements.py:107` supplies the explicit measurement's byte cap too. | `tests/test_placement_grant_bounds.py:22` checks equality and a one-byte overrun on either UUID without mutation; `:37` rejects missing/incomplete caps; the half-byte render/watchdog case accepts the core's rounded budget and refuses a real one-byte overrun (corrected in the bbe9840 follow-up below). Existing grant-limit tests remain. | No clamping. Valid fractional caps use the same integer conversion as Tierstream (corrected in the bbe9840 follow-up). |
| 5. Legacy equality | Restorations are detailed below. Retained differences and reasons are listed in `docs/api.md:13` under **Legacy-visible changes**. | `tests/test_placement_legacy_equality.py` executes the local ddc5640 functions for comparison, alongside the retained editor/API/G6 tests. | Only the explicitly documented differences are retained. |
| 6. PC admission hold | `hyperloom/recipes/preflight.py:303` returns the control-machine resource for placement options; `hyperloom/cli.py:706` uses that verified resource before legacy profile-resource calculation. | `tests/test_placement_grant_bounds.py:45` checks a placement start without a prefill service holds the PC; its legacy counterpart keeps the original nodes/router resources. | Snapshots already hold `*`. No operator grant or window is created. |
| 7. Unwired ingestion | Removed `launch.ingest_context` and `commands.manifest_store` (the remaining adjacent APIs are `hyperloom/recipes/launch.py:326` and `hyperloom/recipes/commands.py:36`). `docs/placement-render.md:312` explicitly records the engine-dependent open point. | `test_engine_dependent_ingestion_has_no_dead_launch_wrappers` asserts both wrappers are absent and the deferral is documented. The existing context parser/history test and manifest-store persistence tests remain. | **Chose removal/deferment.** There is no agreed realised checkpoint-1 JSONL location/ownership contract. Guessing from request output risks stale-run/checkpoint-2 ingestion. The pure CPU `runtime.ingest` parser remains tested for future integration; start/stop currently writes no context-v1 document. |
| 8. Activation predicate | `docs/placement-core.md:10` states rev 21c: only non-empty assist/components activate; empty tables normalise away. | `test_activation_document_matches_rev_21c` plus both retained `test_10_empty_table_stays_legacy_as_in_tierstream` cases. | None. |
| 9. Explicit fact policies | `hyperloom/recipes/session.py:72` caches by explicit `(placement, identity)` policy; `hyperloom/recipes/commands.py:61` accepts that argument without callable mutation. `session.py:97` and `:315` retain ddc5640's evaluator/picker code; placement has its own branches. Profile errors still precede recipe/layer reads. | `tests/test_placement_session_policy.py:25` uses a callable with slots across auto candidates and checks one sample; `:50` checks explicit live policy; `:63` compares the legacy evaluator/picker ASTs with ddc5640. Existing sets/overrides/fresh/stale-profile cases remain. The legacy equality tests check profile/file error precedence for evaluate and pick. | None. |
| 10. Subject-based tests | Replaced all nine `test_placement_review*.py` modules with argv/preflight, cleanup/lifecycle, fidelity, grants/supervision, legacy equality, cache, identity, manifest, solver and decision modules. Synthetic fixtures live in `tests/placement_fixtures.py`. Documentation/import references follow the moves. | Collection/identity proof: `pytest-claude-rename-map.log`, `pytest-claude-collection-before-rename.log`, `pytest-claude-collection-after-rename.log`, `pytest-claude-collection-final.log`. 896 moved cases are retained; total counts are above. | None. |

## Legacy restorations (item 5)

| Restored behavior | Implementation | Regression against ddc5640 |
|---|---|---|
| Legacy CUDA reports omit driver/H2D identities and placement engine identity. | `hyperloom/recipes/apply.py:100`; `hyperloom/recipes/facts.py:113`. | `test_legacy_probe_report_ignores_cuda_driver_and_h2d` compares with the legacy PC gather's absent fields. |
| One passive nvidia-smi query, original GPU-name whitespace, no host/topology walk for legacy. | `hyperloom/recipes/probe.py:45` dispatches to the ddc5640 implementation at `:86`; `facts.py:195` gates host identity on placement. Explicit placement transfer acquisition requests extended inventory at `measurements.py:233`. | `test_legacy_nvidia_query_and_name_match_baseline` checks success and query failure; `test_legacy_facts_do_not_collect_host_topology` forbids the walk and compares full results. The explicit transfer acquisition has a CPU regression in `tests/test_placement_measurements.py`. |
| Deploy queue refusal is `RecipeError("the snapshot's queue timeout is out of range")`. | `hyperloom/recipes/service.py:1072`; snapshot preflight calls the same validator at `preflight.py:238`. | `test_deploy_queue_refusal_matches_baseline`: None, bool, below-minimum and above-maximum timeout. |
| Devices summary, lab-rule legend and unspecified exclusion reason keep their original text. | `hyperloom/web/api.py:898`, `:993`; `hyperloom/device_policy.py:14`. | `test_devices_document_summary_and_rules_match_baseline` with excluded/unexcluded flags; `test_configured_exclusion_reason_matches_legacy_default`. The original 2337186 snapshot was replayed separately to reproduce both summary failures after the fixture setup was corrected. |
| No-profile Phase 1a prints before unused launch-option validation. | `hyperloom/recipes/preflight.py:265`. | `test_phase_1a_print_precedes_unused_launch_validation` compares return/stdout/stderr with ddc5640's main and admission helper for invalid deadline, queue timeout and incomplete approval. |
| Legacy fidelity reports preset errors before missing/not-running state errors. | `hyperloom/rtx.py:1140`. | `test_legacy_fidelity_error_precedence_matches_baseline` checks both a bad preset and a valid preset with missing running state. Native placement dispatch remains covered separately. |

`save-layer`'s Tierstream schema aliases are retained for the namespace rename, with their reason in `docs/api.md`;
`tests/test_recipes_placement_edit.py` retains the alias regression. The placement rendering fixture now uses the
audited parser identity with synthetic binaries. This changes no legacy rendering fixture or production release pin.

## Reproduction evidence

The initial failures were captured before their fixes in `pytest-claude-red-core.log`,
`pytest-claude-red-cleanup.log`, `pytest-claude-red-legacy.log` and `pytest-claude-red-policy.log`.
`pytest-claude-red-devices-replay.log` replays the original devices snapshot with the corrected fixture and records
both intended failures. `pytest-claude-red-measurements.log` covers the affected explicit acquisition caller;
`pytest-claude-red-error-order.log` records the dispatcher error-order regressions before restoring legacy priority.
All logs are local, ignored artifacts in this worktree.


## Follow-up of bbe9840: Sol review 12 and Claude review 2

Base: `bbe984021cb2608fb6f47f526720ac89cd96b404`, contract rev 21c. This round addresses Sol 11/12 and dependent
NOT FIXED 5/9, plus Claude's three new findings. Earlier claims about mixed-directory admission/fact policies and
half-byte refusal are superseded by this section. All regression inputs and process boundaries remain CPU fakes.

| Item | Change (file:line) | Regression coverage | Open points |
|---|---|---|---|
| A: Sol 11, selected admission resources | `hyperloom/recipes/preflight.py:227` returns the prepared launch; `:300` uses its `placement_request` and `evaluation.context.pc`. A resolved legacy selection returns None to the existing CLI profile-resource calculation. | `tests/test_placement_grant_bounds.py:23`, `test_admission_resources_follow_selected_launch`: real prepare/selection and SQLite admission reject mixed-directory legacy selection and stale-profile fallback while a benchmark holds halo-1, with the same refusal as ddc5640. `tests/test_placement_lifecycle.py:135` also prepares a real placement Request through the CLI, asserts the PC hold and refuses an overlapping PC benchmark. | None for this finding. Snapshots retain the conservative `*` hold. |
| B: Sol 12 and dependent 5/9, separate fact samples | `hyperloom/recipes/session.py:72` caches each actual `(placement, identity)` policy without upgrading it; `:173` requests `(False, False)` on resolved legacy fallback; `:363` no longer forces placement across candidates. `hyperloom/recipes/launch.py:138` creates a legacy-default source, leaving policy selection to evaluation. | `tests/test_placement_session_policy.py:28`, `test_mixed_selection_preserves_real_legacy_probe_and_match`: real live_source/gather/probe/evaluate/pick and prepare paths compare legacy rows/selection with and without a placement candidate, including `NVIDIA RTX   PRO 6000 Blackwell`. The legacy query is exactly ddc5640's single query with original names, and mixed selection gathers each policy once. `:84`, `test_stale_placement_profile_gathers_separate_legacy_fallback`, checks separate identity/legacy samples; retained profile-policy cases assert complete tuples. | None for these findings; legacy evaluator/picker AST equality remains covered. |
| C: Claude new 1, rounded byte bounds | `hyperloom/placement_supervision.py:31` bounds integer budgets by both rounded GiB caps and rounded grants. The conversion is Tierstream `src/recipe.cpp:513` (`gib_bytes`), consumed by `src/place.cpp:239` and emitted as `budget_bytes` at `:983`. `docs/placement-render.md:25` corrects the previous unrounded bound. | `tests/test_placement_grant_bounds.py:75`, `test_decimal_caps_match_tierstream_round_half_up`: every 0.1..99.9 cap accepts the core's half-up bytes, checks both devices and refuses a one-byte overrun without mutation. `:169`, `test_14_core_half_byte_budget_is_carried_exactly`, accepts half-byte rounding at render, refuses one byte above it, and retains exact watchdog-boundary checks. | None; budgets are never clamped and GiB caps above grants remain refused. |
| D: Claude new 2, legacy input errors | `hyperloom/recipes/session.py:89` and `:313` catch only RecipeError from the routing check and dispatch to the legacy evaluator/picker, preserving backend errors and per-candidate refused rows. | `tests/test_placement_legacy_equality.py:125`, `test_legacy_unreadable_input_matches_ddc5640`: 24 comparisons execute ddc5640 functions for missing/malformed recipe, profile and override inputs, both evaluate/pick, and stub/core backends. The old pick comparison uses the old evaluate function too. | None for this finding. |
| E: Claude new 3, real controller recovery | `tests/test_placement_lifecycle.py:24` removes the fake `_down_state`. The fake helper process holds a real helper.lock until the real stop_service writes its stop marker. | `test_placement_start_owns_handlers_watcher_and_rollback[controller-death]` runs real up_watch, `_down_state`, stop_service and OS locks; asserts the stop marker, terminal stopped state, helper exit, ownership-file removal and dead owner. Other lifecycle faults retain their assertions. | CPU process-boundary evidence; native/GPU qualification remains external. |

Before production edits, `D:/tmp/hl-placement-round13-red.log` reproduced the decimal/half-byte, mixed-fact,
stale-profile policy, legacy error and mocked-recovery failures. After supplying the missing router capability
fixture, `D:/tmp/hl-placement-round13-red-admission.log` reproduced both admission failures and all four mixed
selection/prepare cases against the unchanged code. The controller-death regression first failed on the missing
helper stop marker with the old mocked recovery action, then passed with the real recovery functions.

The full Python 3.13 baseline completed before production edits: **46 failed, 2618 passed, 69 skipped**, one warning,
in 567.24 s (`D:/tmp/hl-placement-round13-before.log`). `D:/tmp/hl-placement-round13-failures.log` checks its exact
failure-ID equality with the supplied pytest-fix6.log. Focused validation including G6: **1090 passed, 1 skipped**
(`D:/tmp/hl-placement-round13-green.log`). The optional real-core test remains skipped without its configured binary
and golden directory. The offline a919c257 parser audit still passes: **97 options / 43 source files**
(`D:/tmp/hl-placement-round13-parser.log`).

Final full suite: **46 failed, 3648 passed, 69 skipped** in 482.15 s
(`D:/tmp/hl-placement-round13-after.log`). The exact failing-ID sets for fix6, before and after are equal, with no
added or missing failures (`D:/tmp/hl-placement-round13-failures.log`). The 1,030 added cases all pass. The lifecycle
module was also rerun after adding failure-path cleanup for its fake helper thread: **6 passed**
(`D:/tmp/hl-placement-round13-lifecycle.log`). G6's **21 rows remain byte-identical** in the fresh-interpreter test;
the unchanged restored oracle hashes to `40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`
(`D:/tmp/hl-placement-round13-artifacts.log`).

TEMP/TMP are unchanged at `C:/Users/Usuario/AppData/Local/Temp`. This round's logs live directly under `D:/tmp`,
outside git. All modified files use LF. No commit, push, engine/GPU work or network access was performed.

Engine-dependent open points remain as previously disclosed: no published placement-capable replica or GPU
qualification, and context-v1 persistence awaits the realised checkpoint-1 JSONL location/ownership hand-off.
