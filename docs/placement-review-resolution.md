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
| 4. Byte/GiB/grant bounds | `hyperloom/placement_supervision.py:21` requires exact integer byte-cap coverage; `:30` refuses byte caps above the GiB cap; the existing GiB/grant refusal remains. `hyperloom/recipes/measurements.py:107` supplies the explicit measurement's byte cap too. | `tests/test_placement_grant_bounds.py:22` checks equality and a one-byte overrun on either UUID without mutation; `:37` rejects missing/incomplete caps; `:92` now first refuses the core's half-byte rounding overrun, then retains exact watchdog checks under a valid cap. Existing grant-limit tests remain. | No clamping. Fractional caps that round above the admitted bound are deliberately refused. |
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
