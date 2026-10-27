# Placement review resolution

Review: the initial independent review, against contract rev 21c. Worktree base remains `2337186` on `hl/placement`.
No commit or push was made. The production adapter still has `engine_sha: null` and no capabilities. All new
lifecycle, admission and native-interface coverage uses CPU fakes; this work supplies no engine/GPU qualification.

## Validation

- Authoritative full-suite interpreter: Python 3.13, pytest 8.4.2. Before: **46 failed, 2576 passed, 69 skipped**
  (`<historical-validation: before-py313>`). Its 46 failing IDs exactly match the supplied `pytest-fix6.log`.
- Final full suite: **46 failed, 2618 passed, 69 skipped**, one warning, in 486.21 s
  (`<historical-validation: after-py313>`). `<historical-validation: failure-comparison>` proves both full runs have exactly the
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
- Focused checks include `<historical-validation: subjects-final>` (303 passed, 2 skipped),
  `<historical-validation: final-refusals>` (661 passed), `<historical-validation: measurements-final>` (37 passed, 2 skipped),
  and `<historical-validation: final-regressions>` (57 passed, 1 skipped). These overlap; they are not additive counts.
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
| 10. Subject-based tests | Replaced all nine `test_placement_review*.py` modules with argv/preflight, cleanup/lifecycle, fidelity, grants/supervision, legacy equality, cache, identity, manifest, solver and decision modules. Synthetic fixtures live in `tests/placement_fixtures.py`. Documentation/import references follow the moves. | Collection/identity proof: `<historical-validation: rename-map>`, `<historical-validation: collection-before-rename>`, `<historical-validation: collection-after-rename>`, `<historical-validation: collection-final>`. 896 moved cases are retained; total counts are above. | None. |

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

The initial failures were captured before their fixes in `<historical-validation: red-core>`,
`<historical-validation: red-cleanup>`, `<historical-validation: red-legacy>` and `<historical-validation: red-policy>`.
`<historical-validation: red-devices-replay>` replays the original devices snapshot with the corrected fixture and records
both intended failures. `<historical-validation: red-measurements>` covers the affected explicit acquisition caller;
`<historical-validation: red-error-order>` records the dispatcher error-order regressions before restoring legacy priority.
All logs are local, ignored artifacts in this worktree.


## Follow-up of bbe9840: implementation review 12 and independent review 2

Base: `bbe984021cb2608fb6f47f526720ac89cd96b404`, contract rev 21c. This round addresses implementation 11/12 and dependent
NOT FIXED 5/9, plus independent three new findings. Earlier claims about mixed-directory admission/fact policies and
half-byte refusal are superseded by this section. All regression inputs and process boundaries remain CPU fakes.

| Item | Change (file:line) | Regression coverage | Open points |
|---|---|---|---|
| A: implementation 11, selected admission resources | `hyperloom/recipes/preflight.py:227` returns the prepared launch; `:300` uses its `placement_request` and `evaluation.context.pc`. A resolved legacy selection returns None to the existing CLI profile-resource calculation. | `tests/test_placement_grant_bounds.py:23`, `test_admission_resources_follow_selected_launch`: real prepare/selection and SQLite admission reject mixed-directory legacy selection and stale-profile fallback while a benchmark holds halo-1, with the same refusal as ddc5640. `tests/test_placement_lifecycle.py:135` also prepares a real placement Request through the CLI, asserts the PC hold and refuses an overlapping PC benchmark. | None for this finding. Snapshots retain the conservative `*` hold. |
| B: implementation 12 and dependent 5/9, separate fact samples | `hyperloom/recipes/session.py:72` caches each actual `(placement, identity)` policy without upgrading it; `:173` requests `(False, False)` on resolved legacy fallback; `:363` no longer forces placement across candidates. `hyperloom/recipes/launch.py:138` creates a legacy-default source, leaving policy selection to evaluation. | `tests/test_placement_session_policy.py:28`, `test_mixed_selection_preserves_real_legacy_probe_and_match`: real live_source/gather/probe/evaluate/pick and prepare paths compare legacy rows/selection with and without a placement candidate, including `NVIDIA RTX   PRO 6000 Blackwell`. The legacy query is exactly ddc5640's single query with original names, and mixed selection gathers each policy once. `:84`, `test_stale_placement_profile_gathers_separate_legacy_fallback`, checks separate identity/legacy samples; retained profile-policy cases assert complete tuples. | None for these findings; legacy evaluator/picker AST equality remains covered. |
| C: independent new 1, rounded byte bounds | `hyperloom/placement_supervision.py:31` bounds integer budgets by both rounded GiB caps and rounded grants. The conversion is Tierstream `src/recipe.cpp:513` (`gib_bytes`), consumed by `src/place.cpp:239` and emitted as `budget_bytes` at `:983`. `docs/placement-render.md:25` corrects the previous unrounded bound. | `tests/test_placement_grant_bounds.py:75`, `test_decimal_caps_match_tierstream_round_half_up`: every 0.1..99.9 cap accepts the core's half-up bytes, checks both devices and refuses a one-byte overrun without mutation. `:169`, `test_14_core_half_byte_budget_is_carried_exactly`, accepts half-byte rounding at render, refuses one byte above it, and retains exact watchdog-boundary checks. | None; budgets are never clamped and GiB caps above grants remain refused. |
| D: independent new 2, legacy input errors | `hyperloom/recipes/session.py:89` and `:313` catch only RecipeError from the routing check and dispatch to the legacy evaluator/picker, preserving backend errors and per-candidate refused rows. | `tests/test_placement_legacy_equality.py:125`, `test_legacy_unreadable_input_matches_ddc5640`: 24 comparisons execute ddc5640 functions for missing/malformed recipe, profile and override inputs, both evaluate/pick, and stub/core backends. The old pick comparison uses the old evaluate function too. | None for this finding. |
| E: independent new 3, real controller recovery | `tests/test_placement_lifecycle.py:24` removes the fake `_down_state`. The fake helper process holds a real helper.lock until the real stop_service writes its stop marker. | `test_placement_start_owns_handlers_watcher_and_rollback[controller-death]` runs real up_watch, `_down_state`, stop_service and OS locks; asserts the stop marker, terminal stopped state, helper exit, ownership-file removal and dead owner. Other lifecycle faults retain their assertions. | CPU process-boundary evidence; native/GPU qualification remains external. |

Before production edits, `<external>/hl-placement-round13-red.log` reproduced the decimal/half-byte, mixed-fact,
stale-profile policy, legacy error and mocked-recovery failures. After supplying the missing router capability
fixture, `<external>/hl-placement-round13-red-admission.log` reproduced both admission failures and all four mixed
selection/prepare cases against the unchanged code. The controller-death regression first failed on the missing
helper stop marker with the old mocked recovery action, then passed with the real recovery functions.

The full Python 3.13 baseline completed before production edits: **46 failed, 2618 passed, 69 skipped**, one warning,
in 567.24 s (`<external>/hl-placement-round13-before.log`). `<external>/hl-placement-round13-failures.log` checks its exact
failure-ID equality with the supplied pytest-fix6.log. Focused validation including G6: **1090 passed, 1 skipped**
(`<external>/hl-placement-round13-green.log`). The optional real-core test remains skipped without its configured binary
and golden directory. The offline a919c257 parser audit still passes: **97 options / 43 source files**
(`<external>/hl-placement-round13-parser.log`).

Final full suite: **46 failed, 3648 passed, 69 skipped** in 482.15 s
(`<external>/hl-placement-round13-after.log`). The exact failing-ID sets for fix6, before and after are equal, with no
added or missing failures (`<external>/hl-placement-round13-failures.log`). The 1,030 added cases all pass. The lifecycle
module was also rerun after adding failure-path cleanup for its fake helper thread: **6 passed**
(`<external>/hl-placement-round13-lifecycle.log`). G6's **21 rows remain byte-identical** in the fresh-interpreter test;
the unchanged restored oracle hashes to `40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`
(`<external>/hl-placement-round13-artifacts.log`).

TEMP/TMP are unchanged at `<system-temp>`. This round's logs live directly under `<external>`,
outside git. All modified files use LF. No commit, push, engine/GPU work or network access was performed.

Engine-dependent open points remain as previously disclosed: no published placement-capable replica or GPU
qualification, and context-v1 persistence awaits the realised checkpoint-1 JSONL location/ownership hand-off.

## Follow-up of cf14941: implementation review 13 item 8 and independent review 3

Base: `cf14941c51eec51d8acfe6e76292d2bfd1ac385b`, contract rev 21c. This round addresses the portal hold,
selection invalidation after admission, pre-hold probe disclosure and the requested `fab4234` failure baseline.
All new launch and grant-boundary coverage is CPU-only. Logs and the archive export are outside git, under
`<external-review>/`.

| Item | Change (file:line) | Regression coverage | Open points |
|---|---|---|---|
| A: independent 3 finding 1, portal PC hold | `hyperloom/web/api.py:1736` retains preflight's resources; `:1758` uses them for an actual placement request. Legacy starts retain the profile nodes/router resources. | `tests/test_placement_admission_selection.py:84`, `test_portal_holds_selected_resources_without_prefill`: a real API mutation and SQLite admission hold exactly the PC without a prefill service and refuse its benchmark with HTTP 409; legacy holds exactly the nodes/router and remains allowed beside that PC benchmark. | None for this finding. Snapshot launches retain their conservative holds. |
| B: implementation 13 item 8 / independent 3 finding 2, reselection race | `hyperloom/recipes/preflight.py:304` captures the prepared selection. `hyperloom/recipes/launch.py:272` records original recipe path, effective digest, placement/legacy kind and resources; `:294` refuses differences with `recipe selection changed after admission; retry`. `hyperloom/cli.py:726` binds that record to admission and `:419` passes it to `up`; `hyperloom/web/api.py:1806` carries the portal admission through its in-process or detached CLI hand-off (`:1476`). `hyperloom/orchestrate.py:668` checks after final preparation and before either launch's state publication; `hyperloom/recipes/launch.py:309` also guards placement entry. | `tests/test_placement_admission_selection.py:105`, `test_admitted_selection_survives_grant_cutoff`: real grant startup cutoff, auto-pick, prepare and SQLite holds, with fake native adapter/render. Expiry from 600 s remaining to 59 s selects legacy while a node benchmark holds halo-1; CLI, portal and detached argv/child-admission paths refuse without launch state or artifact publication. Unchanged selections pass all three hand-offs. `tests/test_placement_lifecycle.py:255` additionally launches an unchanged admitted selection through real placement supervision with its CPU fake child. Snapshot paths map to original inputs before comparison. | None for this race. No resource union or grant/window extension is used. |
| C: independent 3 finding 3, pre-hold live probes | `docs/api.md:20` explicitly discloses read-only live node fact probes for legacy auto-selection in a mixed directory before admission. | `tests/test_placement_admission_selection.py:170`, `test_mixed_legacy_preflight_documents_live_probes_before_hold`: checks the disclosure and observes only the node benchmark hold during each source sample, followed by the normal benchmark refusal. | This probe-order difference is intentional and now explicit. Pure-legacy directories retain their previous order. |
| D: product's fab4234 baseline by test name and cause | `<external-review>/pytest-fab4234-compare.md` lists all 46 failing IDs, whether each fails at fab4234, exact first-error equality, first/terminal exception-type equality and the observed cause. The export is `fab4234-tree`, produced by `git archive fab4234`, without another worktree. | Full Python 3.13 suites: cf14941 before edits **46 failed, 3648 passed, 69 skipped**; archived fab4234 **46 failed, 1574 passed, 57 skipped**. All failing IDs equal fix6. All exception types match; 39 first error lines match exactly and seven differ only in the tree root or temporary router-log path. Assertions verify the per-ID/block mapping and all 46 normalized causes. | No HEAD-only failures in this baseline. The shared missing-dependency/planner/router failures remain; this is baseline evidence, not a green-suite claim. |

Before production edits, `pytest-round14-red.log` reproduced A, all three B hand-offs and the missing C disclosure:
**5 failed, 4 passed**. The CLI/in-process portal reached the forbidden first state write; the detached path
reselected at its second admission instead of retaining the portal selection. The unchanged-selection controls and
legacy portal control passed. The B portal cases isolate A's resource bug so it cannot mask the race.

Focused validation: **1184 passed** (`pytest-round14-green.log`), followed by **85 passed, 1 skipped**
(`pytest-round14-final-focused.log`) covering the final lifecycle change, legacy equality and G6. G6's **21 rows
remain byte-identical** in the fresh-interpreter test; the restored oracle SHA-256 remains
`40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`, with no fixture change.

The before/fab4234 full runs used `python3.13 -m pytest -q -p no:cacheprovider`, with unchanged
TEMP/TMP outside the repository. This round makes no contract, native-engine, capability, grant, measurement or
G6 oracle change. No commit, push, GPU work or network access was performed.

The first edited-HEAD full attempt (`pytest-cf14941-after-attempt1.log`) reported **47 failed, 3656 passed,
69 skipped**: the extra SSE snapshot test raised `KeyError: 'overview'`. Its module immediately passed all six
tests (`pytest-round14-sse-recheck.log`). The SSE test, registry and stream-projection sources are byte-identical
at fab4234 and cf14941 and remain unmodified. An event-controlled reproduction in both trees confirms that an
in-flight default registry load can overwrite the SSE fixture's isolated registry, removing the expected
`why.overview` entry (`pytest-round14-sse-race-head.log`, `pytest-round14-sse-race-fab4234.log`). The comparison
report discloses this intermittent failure and its cause; a fresh full run is recorded separately.

Final full suite (`pytest-cf14941-after.log`): **46 failed, 3657 passed, 69 skipped** in 471.55 s. The SSE test
passes in this run. `pytest-round14-failure-comparison.log` asserts exact equality of the 46 failing-ID sets for
fix6, before, after and fab4234, with no added or missing failures. All first and terminal exception types and all
46 first-error causes match both baselines after only tree-root/temporary-router-log path substitutions. The nine
new regression cases all pass. `pytest-round14-artifacts.log` records G6 equality, unchanged HEAD/branch,
unchanged external TEMP/TMP and LF for every modified file. The full per-ID report includes the final run and the
disclosed intermittent SSE attempt; its source-equality hashes are in `pytest-round14-sse-source-equality.log`.

## Lead review of f1626ff (contract rev 21c, section 7.1)

All requested findings are addressed in this worktree. The adapter retains its null engine SHA and empty
capabilities. This is CPU integration evidence, with no engine, CUDA or live driver qualification. No submodule
initialisation, network access, commit, push or additional worktree was used. Logs are outside git in the system
temporary directory; TEMP/TMP were not changed.

The first regression run (`hl-placement-lead-red.log`) reproduced all 14 initial review cases before the existing
production paths were changed. Later checks expanded the signal cases to include Markdown ledger syntax,
partial records, unreadable counters and total cache bytes. `hl-placement-prefs-red.log` separately reproduced
the disappearance of a known preference store before that edge case was fixed.

In the following table, test names without a module prefix are in `tests/test_placement_lead_review.py`.

| Item | Change (file:line) | Regression coverage | Open points |
|---|---|---|---|
| M1: live PC-BIG admission | `hyperloom/pc_big.py:30` reuses the legacy PRO lock path, `rtx.pro_busy`, the whole-ledger reader, and the turn ledger parser/state reader. Since the rebase onto the hardened features base the ledger path is the configured `pc.turn_ledger` (no portal preference or telemetry store is read). Held/busy/open/partial/unreadable signals refuse. `hyperloom/recipes/grants.py:64` and `hyperloom/recipes/measurements.py:74` check before CUDA work. | `test_m1_each_live_signal_refuses_probe_before_cuda` checks probe acquisition, placement grant admission and pre-start policy validation against each signal, with a forbidden CUDA loader. | None for admission. Optional unconfigured sensors are documented in `placement-grants.md`; configured unreadable sensors refuse. |
| M1: windows opening after admission | `hyperloom/recipes/grants.py:166` carries the live guard into supervision; `hyperloom/placement_supervision.py:14` checks it at runtime, including watchdog memory checks. Once configured, the ledger cannot disappear and disable the guard (a missing or unreadable configured ledger refuses). | `test_m1_watchdog_stops_when_live_window_opens` opens each live signal after admission and verifies one fake-child kill; the configured-ledger tests in `tests/test_placement_lead_review.py` and `tests/test_pc_big_ownership.py` cover a removed or unreadable ledger. | Detection occurs on the next bounded watchdog sample; it does not acquire or extend a PC-BIG window. |
| M2: driver-free identity and gate ordering | `hyperloom/windows_adapters.py:6` gets LUIDs from DXGI and PCI addresses from read-only D3DKMT queries. `hyperloom/gpu_counters.py:44` joins them to NVML UUIDs without nvcuda/cuInit. `hyperloom/recipes/measurements.py:109` and `hyperloom/rtx.py:779` construct observers after every lock and gate. | `test_m2_measurement_observer_follows_lock_and_gate`, `test_m2_service_observer_follows_lock_and_gate`, `test_m2_passive_luid_mapping_never_loads_cuda`; `tests/test_placement_counter_identity.py` retains reversed-inventory and incomplete/duplicate mapping refusals; `tests/test_windows_adapters.py:10` checks COM/query ABI and cleanup with fakes. | Live WDDM driver behavior remains unqualified under this task's no-GPU constraint. |
| Privacy: local paths | Portable placeholders replace machine-specific roots in this document, `tests/fixtures/render/README.md` and the older phase task document. `tests/test_placement_privacy.py:65` now scans documentation Markdown, shipped source string literals and shipped JSON metadata. | `test_docs_and_source_literals_have_no_local_identity`, `test_privacy_patterns_cover_docs_and_decoded_source_strings`, and the committed-artifact privacy test. | None. Runtime identities remain local. |
| Privacy: named agents | `hyperloom/replica_gate.py:73`, `hyperloom/recipes/runtime.py:42`, the placement preset's release-owner metadata, `docs/replica-fidelity.md` and `docs/placement-render.md` use generic engine/release-owner roles. | The same committed-artifact scan catches the original handles in strings and documentation. | None. The release publication requirement is unchanged. |
| Portal busy ordering | `hyperloom/web/api.py:1730` checks the active job before preflight and retains the later mutex check for admission races. | `test_portal_busy_precedes_preflight` forbids preflight while another job is active. | None. |
| Portal 400 audit/redaction | `hyperloom/web/api.py:1745` audits the complete local refusal, then returns only a placement error code (when present) and a generic message. Argument construction/parsing is inside the audited preflight boundary. | `test_portal_400_audits_full_reason_and_redacts_response` verifies audit detail and response omission of a path and PID; `tests/test_placement_command_preflight.py` retains real missing/corrupt snapshot 400 checks with the new public envelope. | None. |
| Planner/pick audit | `hyperloom/web/features/planner.py:28` marks what-if, placement, solve and pick as audited, using the existing success/refusal audit mechanism. | `test_planner_actions_are_audited` verifies all four registrations. | None. |
| Solver cache bounds | `hyperloom/recipes/solver.py:19` caps selections at 64 files / 64 MiB, rejects a single oversized record, and evicts oldest writes under the runtime lock. | `test_solver_cache_evicts_oldest_by_count_and_bytes` and `test_solver_cache_total_byte_limit_evicts_oldest`. | Evicted selections require a fresh solve before pick; documented in `placement-solver.md`. |
| Passive PRO/device lock checks | `hyperloom/procs.py:132` reads Windows mandatory-lock state or Linux flock inventory; `hyperloom/recipes/preflight.py:86` uses it without taking the device lock. | `test_preflight_lock_probe_never_acquires` forbids the lock factory, refuses a real held lock and admits its unlocked stale file. | None. The helper still acquires its lifetime locks at launch. |
| Device policy flag / legacy equality | `hyperloom/config.py:269` accepts `pc_big_conflict`; `hyperloom/pc_big.py:11` applies it by configured UUID, defaulting to conflict for devices other than the configured PRO. `grants.py` and `measurements.py` have no model-name test. `hyperloom/config.py:44` retains the legacy unconfigured-4090 default exclusion and identity requirement; `device_policy.py:5` applies that data policy. | `test_policy_uses_configured_conflict_flag_with_arbitrary_names`, existing cross-view policy cases and the unchanged G6 oracle. | The policy/default distinction and unchanged legacy exclusion are explained in `placement-grants.md`. |
| Ambiguous configuration | `hyperloom/device_policy.py:14` excludes multiple configured matches instead of falling back to a permissive policy. Measurement acquisition also requires exactly one configured UUID. | `test_device_policy_ambiguous_identity_is_excluded`; documented in `placement-grants.md`. | None. |
| Temporary directories | Placement/content/core/edit test fixtures use the default `tempfile` location, with no repository-root override. | `test_test_directories_and_runner_are_portable` scans every test module for the original override. | None; TEMP/TMP are unchanged. |
| Linux path portability | `tests/test_placement_render.py:77` normalizes separators before replacing the runtime root. `tests/test_placement_lifecycle.py:141` simulates the Windows service filesystem with native temporary paths. | Existing `PlacementRenderTests.test_single_and_two_gpu_goldens` and `test_01_evaluate_prepare_up_and_real_watchdog_with_fake_child` pass on Linux. | Linux validation uses Python 3.12; the required full before/after suites use Windows Python 3.13. |
| Test runner | `pyproject.toml:12` declares the pytest test extra; `.github/workflows/ci.yml:41` installs it and runs pytest; README and test instructions agree. Existing unittest classes remain supported. | `test_test_directories_and_runner_are_portable` verifies the runner/dependency agreement; full pytest collects both styles. | Chose the permitted pytest option because 22 existing modules use fixtures/parameterization; a broad rewrite to unittest would add unrelated test migration risk. No runtime dependency was added. |

Validation: the full Python 3.13 baseline is **46 failed, 3657 passed, 69 skipped** in 465.62 s
(`hl-placement-before313.log`). Its failing IDs equal the supplied 46-ID `pytest-fix6.log` set exactly
(`hl-placement-failure-comparison.log`). Focused final validation: **184 passed**
(`hl-placement-final-focused.log`). The final full-suite result is recorded below after completion.

Linux validation: **31 passed** (`hl-placement-linux-final.log`), covering rendering, the integrated fake
lifecycle, lead-review safety/privacy regressions and the adapter ABI test. Native Linux Git cannot resolve this
Windows worktree's git pointer, so the final run routes only its read-only Git invocations through Windows Git;
no git configuration or worktree was changed. The distro had no pytest installed; the run used existing
pure-Python runner packages read-only, with plugin autoload and bytecode writes disabled. An earlier run passed
the other 30 tests with the git-dependent scan deselected (`hl-placement-linux.log`).

G6 remains **21 cases byte-identical** to the frozen eb9ae96 oracle. Its fixture is unchanged and its restored
SHA-256 remains `40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`.
The earlier interrupted after-run was stopped to add the separately reproduced preference-disappearance case;
it is not used as final validation evidence. The first complete after attempt reported **47 failed, 3674 passed,
69 skipped** (`hl-placement-after313-attempt1.log`): its only additional failure was
`tests/test_web_recipes.py::JobTests::test_deploy_job_binds_the_planned_snapshot`, whose old assertion expected
private preflight text in the public API response. `tests/test_web_recipes.py:663` now checks the redacted envelope
and verifies the complete original reason in the local audit. The isolated test passes
(`hl-placement-portal-envelope.log`); the final complete run below supersedes that attempt.

Final full Python 3.13 suite: **46 failed, 3675 passed, 69 skipped** in 526.95 s
(`hl-placement-after313.log`). `hl-placement-failure-comparison.log` asserts exact equality of the reference,
before and final after failing-ID sets: all 46 IDs match, with no added or missing failures. The 18 additional
cases pass. The empty TensorMeld submodule remains untouched. The final privacy scan and `git diff --check` pass;
all changed/new files use LF. HEAD remains f1626ff on hl/placement, with no commit or push.

## Follow-up of f9ba5b4: ownership, identity and remaining review findings

Contract rev 21c. Both reviews' remaining findings are addressed below. All regressions use CPU process/counter
fakes; device and helper lifetime locks in the integrated ownership/handover cases are real OS locks. The ten new
cases first failed against the original implementation. Corrected integration fixtures were also replayed against
the original source in memory: both own-lock and reviewed-handover cases refused with an active PC-BIG window.

| Item | Change (file:line) | Regression | Open points |
|---|---|---|---|
| A / own review 15: own PRO lock and activity | `hyperloom/pc_big.py:51` accepts explicit proven owners; owned PROs skip the external-lock/device-total checks and inspect foreign compute processes instead. `hyperloom/rtx.py:784` passes ownership only after all locks are acquired; `hyperloom/placement_supervision.py:100` accounts for the helper and child during supervision. PRO acquisition preserves configured lock-file spelling even on case-sensitive filesystems. The ledger remains mandatory when configured; unknown foreign process memory refuses. | `tests/test_pc_big_ownership.py:41`, `test_two_device_helper_and_watchdog_do_not_self_lock`: real two-device helper launch and locks, own parent/child activity accepted, a competing legacy lock cannot be acquired (with case-sensitive filename simulation), foreign N/A activity and a new ledger start refused, locks released on teardown. | No hardware qualification is claimed. |
| A: planned handover | `hyperloom/recipes/preflight.py:28` passes owners established by the existing ready/PID/device-set/helper-lock verification into grant and pre-start checks. Ownership never enters the serialized guard or policy. Fresh unowned helper admission and device-lock acquisition remain mandatory after the old service stops. `docs/placement-grants.md` documents the handover boundary. | `tests/test_pc_big_ownership.py:114`, `test_reviewed_handover_guard_uses_only_proven_live_owners`: real old PRO/helper locks, reviewed attributable child accepted, foreign process and mismatched ready PID refused. | Ledger and scheduled PC-BIG windows still refuse; reviewing a replacement never extends or waives them. |
| B / own review 14: missing top-level PRO UUID | `hyperloom/pc_big.py:11` derives PROs from configured local prefill-service devices and placement anchors as well as the optional explicit UUID. Every derived identity is checked; no reliable identity for a conflicting device refuses `PL_E_GRANT`. Configured UUID spelling is retained for legacy lock filenames. `hyperloom/recipes/grants.py:153` supplies bound anchors. | `test_missing_pro_uuid_cannot_grant_with_zero_live_probes` reproduces the zero-probe grant through the real measurement grant path; a simulated held derived-PRO lock now refuses. `test_conflicting_probe_without_reliable_pro_identity_refuses` covers absence of all identities. | Missing preferences still means no ledger signal, matching the legacy ledger guard. Legacy launches are unchanged. |
| C: root pytest collection | `pyproject.toml:23` sets `testpaths = ["tests"]` and excludes `third_party` and hidden directories. README, CI and UI test instructions describe the same root command. | `test_root_pytest_excludes_submodule_collection`; the existing portable-runner regression now checks both settings. | The submodule remains empty and was not initialized. |
| D4: watchdog cost and transient queries | `hyperloom/placement_supervision.py:12` / `:160` recheck PC-BIG once per five monotonic seconds, outside the half-second memory checks. Only GPU query failures receive three consecutive attempts; successful checks reset the count. Ledger/lock failures refuse on the next PC-BIG check, and attributable-cap failures retain their existing immediate behavior. | `test_watchdog_rate_limits_pc_big_and_tolerates_only_transient_counters`: repeated memory/full ticks trigger one live query; first two query failures survive, third refuses, success resets, unreadable lock refuses. | Full ledger reads are bounded and run at most once per PC-BIG interval; no legacy ledger/watchdog code changes. |
| D5: portal 400 reason | `hyperloom/web/api.py:224` scrubs paths, credentials, UUIDs and process IDs from the retained reason; `:1757` returns recipe-wide preflight wording and keeps the original local audit detail. | `test_legacy_recipe_portal_error_retains_redacted_reason`, plus existing audit and real portal preflight regressions. | No new response schema or audit delivery dependency. |
| D6: privacy residue | `tests/test_placement_privacy.py:21` / `:76` cover mounted-drive/home paths, agent references, nested docs/test READMEs and shipped TOML. Local examples and review references use role/placeholders; shipped provenance is scrubbed and `hyperloom/recipes/builtin.json` hashes are refreshed. Optional upstream comparison still checks all executable recipe fields. | `test_privacy_scan_catches_nested_docs_and_shipped_recipe_references`, existing whole-repository privacy cases and builtin manifest verification. | No executable recipe values changed. |
| D7: sticky admission facts | `hyperloom/recipes/grants.py:159` builds one guard, checks it and carries that same dict into supervision. `hyperloom/recipes/measurements.py:227` retains one guard across pre-inventory admission and the helper handoff. | `test_known_store_survives_policy_and_measurement_handoff`: deleting the configured ledger after admission refuses both the placement policy's helper check and measurement admission after inventory; the ledger requirement is asserted in the carried guard. | No ledger or preferences writes are introduced. |
| Device exclusion | `hyperloom/device_policy.py:5` has an explicit placement policy: physical configured identity and `excluded` determine eligibility, without model-name defaults or name fallback. `hyperloom/recipes/devices.py:19` applies it to candidates. The default legacy policy is unchanged. | `test_configured_device_policy_does_not_inherit_model_name_default`: configured generic/model-name cases, explicit exclusion and unmatched identity; existing cross-view legacy comparisons and G6 remain. | The legacy unconfigured-device default remains intentionally equal. |
| Informational nits | `hyperloom/windows_adapters.py:66` documents the Windows PCI-domain-zero assumption; `docs/replica-fidelity.md:57` fixes sentence capitalization. Existing API documentation describes the early busy refusal. | Existing Windows adapter and portal busy-order coverage. | PCI domains other than zero remain unsupported by this Windows adapter mapping. |

Focused validation before final full runs: **123 passed, 2 skipped**, followed by **10 passed** for the integrated
regressions. G6's **21 cases remain byte-identical** to the unchanged frozen oracle. RTX definition/branch comparison
confirms that only the placement part of `service_run` changed: legacy service branches, reservation/gates,
busy/idle queries, the legacy watchdog and device-lock helper definitions remain identical to f9ba5b4.

Final full Python 3.13 validation:

- Exact-HEAD baseline: **46 failed, 3675 passed, 69 skipped**, 469.98 s (`hl-placement-before313-clean.log`).
- Final edited tree: **46 failed, 3685 passed, 69 skipped**, 471.95 s (`hl-placement-after313-final.log`).
- `hl-placement-failing-ids-final.log` confirms both failing-ID sets equal all 46 supplied reference IDs exactly,
  with no added or missing failures. All ten new cases pass. Final affected checks: **174 passed**.
- G6: all **21 cases byte-identical**; privacy scan, LF verification and `git diff --check` pass.

The first overlapping baseline attempt was invalidated when a late manifest test read an edited example; it is not used as
baseline evidence. The replacement baseline restored exact HEAD contents in this same worktree, then restored all
edits before the final after run. The temporary validation snapshot was removed. Logs stay outside git; no additional worktree, network, GPU work, commit or push.

## Follow-up of 658d62f: owned PRO activity, watchdog latency and public refusals

Contract rev 21c. Both reviews' remaining findings are addressed here. All new regressions use CPU process and
counter fakes; the integrated helper and reviewed handover retain real OS device/helper locks. The original
implementation first failed ten launch/handover, public-response, lock-latency and documentation cases
(`hl-placement-r17-red.log`). The independent residual/attribution matrix reproduced thirteen failures before the
production changes (`hl-placement-r17-residual-red.log`), including missed unattributed use and unavailable totals.

| Item | Change (file:line) | Regression | Open points |
|---|---|---|---|
| A: PRO scope and WDDM activity | `hyperloom/pc_big.py:79` intersects normalized PRO identities with owners. N/A desktop rows alone are ignored; foreign numeric PRO use above idle still refuses. Each owned PRO's device total minus the observer's bytes for all owned PIDs must stay at or below the idle threshold. Assist process rows never enter this guard. | `tests/test_pc_big_ownership.py:41` exercises real helper startup and watchdog checks with PRO/assist desktop rows, own process use, numeric foreign PRO use, assist numeric activity, residual boundary/overflow and ledger refusal. `:129` covers reviewed two-device handover, foreign numeric PRO use, unattributed totals and stale ownership. `:324` covers pre-start/watchdog attribution, missing/negative samples, query failures, immediate observer refusal and counter recovery. | No live WDDM qualification under the no-GPU constraint. Placement caps retain full attributable process usage. |
| A: observer lifetime and counter errors | `hyperloom/rtx.py:788` and `hyperloom/placement_supervision.py:165` pass their existing observer. `hyperloom/recipes/measurements.py:108` creates and registers cleanup of its observer after locks/gates and before the owned check. `hyperloom/recipes/grants.py:64` and `hyperloom/recipes/preflight.py:28` carry the handover observer factory through admission. `hyperloom/pc_big.py:52` preserves `CounterUnavailable` for GPU query failures; attribution failures refuse immediately. Borrowed observers stay with their caller; temporary admission observers are closed. | The same integrated/matrix cases, existing measurement lock/gate/observer ordering and cleanup regressions, and the three-strike/recovery checks. | Unowned PROs retain external-lock and legacy busy checks. Ledger and scheduled-window refusals remain enforced. |
| B: unowned PRO lock latency | `hyperloom/placement_supervision.py:160` checks only unowned PRO locks on each full tick (about two seconds). Ledger/GPU work remains on its five-second schedule. | `tests/test_pc_big_ownership.py:288` makes an external lock held or unreadable between full ticks and requires refusal at the next two-second tick, with no additional ledger/GPU queries or counter strike. Existing rate-limit coverage retains the five-second query cadence. | Ledger/GPU detection remains bounded by the next scheduled full tick. |
| C: public HTTP 400 | `hyperloom/web/api.py:224` returns fixed public wording; the existing refusal boundary keeps arbitrary exception text only in the local audit. Existing portal expectations now check that envelope. | `tests/test_pc_big_ownership.py:266` passes a native missing-file exception with an apostrophe in its quoted path, a parenthesized PID, bare/MIG UUIDs and an unknown private code through the real API boundary. Every response is identical, the audit retains the full original reason, and no job is created. | None. This intentionally removes arbitrary actionable text from the public response. |
| D: early HTTP 409 ordering | `docs/api.md:28` lists busy-before-preflight ordering under legacy-visible changes and documents the repeated mutex check after preflight. The HTTP 400 entry also describes the fixed public response and local audit. | `tests/test_pc_big_ownership.py:313` checks the legacy-visible entry; the existing portal busy-order regression forbids preflight while another job is active. | None. No runtime ordering change was needed. |

The exact-HEAD full Python 3.13 baseline is **46 failed, 3685 passed, 69 skipped** in 467.09 s
(`hl-placement-r17-before313.log`). Its failing IDs equal all 46 reference IDs exactly
(`hl-placement-r17-failure-comparison.log`). The ownership regression module passes **37 cases** after the fixes.

Final full Python 3.13 suite: **46 failed, 3712 passed, 69 skipped** in 467.23 s
(`hl-placement-r17-after313.log`). The reference, clean baseline and final failing-ID sets are exactly equal,
with no added or missing failures; all 27 additional cases pass (`hl-placement-r17-failure-comparison.log`).
The broad focused run found the two known empty-submodule failures and two old public-reason expectations;
the latter now assert the fixed envelope and both pass (`hl-placement-r17-envelope-recheck.log`). Ownership,
privacy and G6 checks pass **41 cases** (`hl-placement-r17-artifact-tests.log`).

G6 retains all **21 cases byte-identically**, with the unchanged fixture and reconstructed SHA-256
`40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`.
Legacy reservation, gate, busy-query and watchdog definitions remain AST-equal to eb9ae96; the device-lock
definition matches after inlining its extracted path helper, and the legacy helper acquisition/gate/verify
branch is AST-equal (`hl-placement-r17-legacy-ast.log`). Against 658d62f, only the placement part of RTX
`service_run` changes (`hl-placement-r17-ast.log`). LF verification and `git diff --check` pass.

Logs remain outside git. TEMP/TMP were not changed; the empty submodule is untouched. No network, GPU work,
additional worktree, commit or push was used.

## Follow-up of ce445cc: residual brackets and real-renderer handover

Contract rev 21c. Both reviews' remaining findings are addressed below. The clean Python 3.13 baseline completed
before any tracked edit: **46 failed, 3712 passed, 69 skipped** in 467.62 s. Its failing IDs equal the supplied
46-ID reference exactly (`hl-placement-r18-before313.log`, `hl-placement-r18-failure-comparison.log`).

CPU reproductions first confirmed false refusal during own growth, hidden foreign N/A activity during own release,
and acceptance of a negative total (`hl-placement-r18-residual-repro.log`). The regression run reproduced the real
launch/RTX renderer's ownership loss and the sampling defects (`hl-placement-r18-red.log`). After correcting a test
fixture's lock cleanup, observer lifetime regressions separately reproduced repeated construction on success and
refusal (`hl-placement-r18-observer-red.log`).

| Item | Change (file:line) | Regression | Open points |
|---|---|---|---|
| A: both residual sampling findings | `hyperloom/pc_big.py:59` brackets each device total with two samples per owned PID, summing per-PID maxima/minima. Definite foreign activity refuses; definite clear state proceeds. Ambiguity resamples immediately for at most three brackets, then raises `CounterUnavailable`. Negative/non-integer totals and totals more than one MiB below the lower attribution also refuse. `hyperloom/recipes/preflight.py:176` uses the same bounded check for reviewed idle admission. | `tests/test_pc_big_sampling.py:19`, `test_residual_brackets_bound_each_owned_pid`: growth without a kill, release with persistent foreign N/A activity refused on the second bracket, stable clear/foreign cases, crossing PID changes, persistent ambiguity, invalid totals, rounding tolerance, missing samples on either side, watchdog strikes/recovery, and unchanged full-process cap enforcement. Runs at pre-start, handover and watchdog boundaries. | No live WDDM qualification under the no-GPU constraint. Three unavailable watchdog checks still stop the turn; admission/start refuse immediately. |
| B: ownership lost at the real renderer | `hyperloom/recipes/launch.py:161` re-verifies reviewed owners and passes them transiently through `render_placement`, `hyperloom/rtx.py:274` and `:358` into validation. No ownership is added to a policy, Request, ServiceSpec or saved record. Fresh unowned validation and device-lock acquisition after stopping remain mandatory. | `tests/test_placement_lifecycle.py:138`, `test_01_evaluate_prepare_up_and_real_watchdog_with_fake_child[True]`: complete deployment through real preparation, runtime decision, launch/RTX rendering, reviewed stop, fresh admission, real snapshot pin and replacement helper/watchdog. Synthetic qualified release metadata, CPU core/process/counter fakes and real device/helper locks exercise the entire path. Saved state has no owner/observer exemptions; replacement cap overflow still stops the child. Existing after-stop lock/port refusal cases remain covered. | The production release remains unpublished and refused. No renderer is replaced by this regression. |
| C: repeated handover observers | `hyperloom/recipes/preflight.py:18` owns one observer per reviewed admission in an `ExitStack`; `hyperloom/recipes/grants.py:64`, `:85`, `:160` and supervision/idle checks borrow it. `hyperloom/recipes/preflight.py:167` registers cleanup at construction. The observer factory remains available for CPU tests. | `tests/test_placement_preflight.py:24`, `test_handover_admission_owns_one_observer`: exactly one construction and close on success, core refusal, foreign activity and a final missing sample. | None. Borrowed observers are never closed by a callee. |

Focused validation: **1219 passed, 2 skipped** (`hl-placement-r18-focused.log`). The handover regression with its
real snapshot pin additionally passes both ordinary and reviewed startup (`hl-placement-r18-handover.log`).
The observer fixture was then strengthened to exercise every grant/supervision check. Replaying HEAD's modules in
memory reproduced six constructions on successful admission; the final implementation constructs and closes one
(`hl-placement-r18-observer-final-red.log`, `hl-placement-r18-observer-final.log`). An incomplete after-run was
stopped for that fixture improvement and is not used as final evidence.

Final full Python 3.13 suite: **46 failed, 3759 passed, 69 skipped** in 473.88 s
(`hl-placement-r18-after313.log`). The reference, clean baseline and final after failing-ID sets are exactly equal:
all 46 pre-existing failures, none added or missing. All **47 additional cases pass**
(`hl-placement-r18-failure-comparison.log`). Both full runs emit background admission-thread warnings; the final
run also records a `StopIteration` when an existing admission thread encounters the fidelity test's patched sleep.
Those warnings remain an open test-isolation point; the admission-thread implementation is unchanged.

G6 retains all **21 cases byte-identically** to the unchanged frozen oracle. Legacy reservation, gate, busy-query
and watchdog definitions are AST-equal to eb9ae96; the device-lock definition matches after inlining its path
helper, and the legacy helper acquisition/gate/verify branch matches. `service_run` and the legacy rendering body
also remain AST-equal to HEAD (`hl-placement-r18-legacy-artifacts.log`). Privacy, LF and `git diff --check` pass.

Logs remain outside git. TEMP/TMP were not changed, and the submodule remains empty. HEAD is still ce445cc on
hl/placement. No network, GPU work, additional worktree, commit or push was used.

## Follow-up of a7d9792: bounded loading and observer reuse

Contract rev 21c. The clean full Python 3.13 baseline completed before tracked edits: **46 failed, 3759 passed,
69 skipped** in 483.34 s. Its failing IDs exactly equal the 46 supplied reference IDs. CPU reproductions confirmed
that sustained own growth killed on check three, a transient allocation killed on check one, and reviewed
preparation constructed two observers. Regression tests reproduced the defects before implementation.

| Item | Change | Regression | Open points |
|---|---|---|---|
| A: sustained own growth | `hyperloom/gpu_counters.py:34` reads device memory through the observer's initialized NVML session and a UUID handle; Windows batches every owned PID into one PDH snapshot per side. `hyperloom/pc_big.py:63` retains nvidia-smi as fallback and distinguishes growth-only ambiguity. `hyperloom/placement_supervision.py:163` allows it without adding or resetting strikes until 60 seconds after the first full check, shortened by the ready timeout; this interval never restarts. Release/mixed ambiguity still strikes, and other devices/caps remain checked. | `tests/test_pc_big_loading.py:17`: native ABI and shared-snapshot check, fallback, sustained growth beyond three checks, retained strikes/recovery, finite growth allowance and release/mixed refusal. Existing release with persistent foreign use remains refused. | Native timing and loading behavior require live qualification. Ambiguous growth can delay foreign-use refusal until it resolves or the allowance expires. Growth after the allowance receives the normal three strikes. Admission/start have no allowance. |
| B: transient own allocation | `hyperloom/pc_big.py:106` requires two consecutive busy brackets within a watchdog check; `hyperloom/placement_supervision.py:170` enables this only for its GPU checks. | `tests/test_pc_big_loading.py:171`, `test_own_spike_only_in_total_needs_two_busy_watchdog_brackets` covers one transient spike, persistent foreign totals, and unchanged single-verdict admission/handover. | Repeated spikes can still coincide with both brackets; live qualification remains open. |
| C: bracket and WDDM assumptions | `docs/placement-supervision.md:44` and `docs/placement-grants.md:52` state that attribution must be monotonic per PID within each bracket. Spikes can falsely refuse; dips can conceal foreign use for a check. The total below attribution refusal remains fail-closed. | `tests/test_pc_big_loading.py:196`, `test_live_qualification_documents_bracket_assumption_and_total_accounting` checks that both supervision and this resolution retain the open qualification points. | Exercise non-monotonic spikes/dips and compare summed PDH Dedicated Usage with NVML memory.used under load, including shared/aliased allocations and driver accounting. The one MiB tolerance and total below attribution refusal require live WDDM qualification before enabling release. |
| D: shared pre-stop observer and UUID keys | `hyperloom/recipes/launch.py:139` owns the reviewed evaluation/render observer in an ExitStack; session/preflight and the real RTX renderer borrow it. `hyperloom/pc_big.py:83` uses `grants.uuid_key` for owned attribution. | `tests/test_placement_lifecycle.py:236` requires one construction/close through reviewed preparation. `tests/test_pc_big_loading.py:189`, `test_owned_busy_normalizes_uuid_like_handover_filter` covers surrounding whitespace and case. | None. Ownership/observers remain transient; replacement after stopping still performs fresh unowned admission and takes the device locks. |

No live GPU or WDDM qualification was performed. Production placement remains unpublished and refused.

Final full Python 3.13 suite: **46 failed, 3780 passed, 69 skipped** in 685.16 s. Reference, clean baseline and
final failing-ID sets are exactly equal: none added or missing. All **21 additional cases pass**. The focused
placement/admission/lifecycle/legacy/privacy run passed **379 tests, 3 skipped**. Before/after logs are
`hl-placement-before-r19.log` and `hl-placement-after-r19.log`; exact ID comparison is
`hl-placement-failure-comparison-r19.log`. Regression-first evidence is `hl-placement-red-r19.log`; the focused
run is `hl-placement-focused-r19.log`. Logs are outside git.

The final full run emitted two background admission-thread warnings when pre-existing tests patched global sleep
(one StopIteration and one KeyboardInterrupt). That test-isolation issue remains open; admission-thread code is
unchanged. The final run completed without interruption despite taking longer than the baseline.

G6 retains all **21 cases byte-identically**, including the unchanged frozen fixture and reconstructed SHA-256
`40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`. Legacy reservation, gate, busy-query and
watchdog definitions are AST-equal to eb9ae96; the device-lock definition matches after path-helper inlining,
and the legacy helper acquisition/gate/verify branch matches. The helper service_run, legacy rendering body,
legacy evaluator and legacy picker remain AST-equal to HEAD (`hl-placement-legacy-ast-r19.log`). LF, UTF-8,
privacy and diff whitespace checks pass. TEMP/TMP were not changed; the submodule remains empty. Work stayed on
the existing branch and HEAD a7d9792. No network, GPU work, additional worktree, commit or push was used.

## Follow-up of 7e6d7aa: measurement supervision and progress before readiness

Contract rev 21c. The clean full Python 3.13 baseline completed before edits: **46 failed, 3780 passed,
69 skipped** in 584.82 s. All 46 failing IDs exactly match the supplied reference. Regression-first execution
with unchanged production code recorded **14 failed, 4 passed** across the 18 new CPU cases. The failures include
the real measurement watchdog's missing field, the reserved-memory offset, fallback loading beyond 60 seconds,
stalled growth, readiness delivery by the helper and repeated reviewed-resource lookup.

| Item | Change (file:line) | Regression | Open points |
|---|---|---|---|
| A: explicit measurement watchdog | `hyperloom/recipes/measurements.py:103` supplies an explicit zero ready timeout, so probes have zero load grace. | `tests/test_pc_big_review.py:21`, `test_measurement_uses_real_watchdog_with_zero_load_grace`: fake measurement child/counters with the real placement watchdog and inherited fail-closed loop. A synchronous scheduler starts no thread and performs no real wait. Normal completion succeeds; growth ambiguity still adds a strike. | None for this defect. Live measurement hardware remains unqualified by CPU tests. |
| B: comparable native device totals | `hyperloom/gpu_counters.py:20`, `:35` and `:50` define and configure the versioned NVML v2 structure/API once per session, return allocated `used` without reserved bytes, and refuse v1 substitution. Unavailable v2 falls back through the existing nvidia-smi path. | `tests/test_pc_big_review.py:78`, `test_native_total_matches_allocated_memory_or_falls_back`: fake v1-only, v2, unsupported and version-mismatch DLLs; version word, field offsets, reserved-memory exclusion, fallback and cleanup. | Verify NVML v2 used == nvidia-smi memory.used at idle and under load on the qualified driver. WDDM attribution/total accounting remains a release prerequisite. |
| C: progress and readiness bound | `hyperloom/pc_big.py:85` supplies each device's bracket high bound; `hyperloom/placement_supervision.py:101` and `:169` retain previous full-check bounds and allow growth ambiguity only when each ambiguous device gains at least 1 GiB, readiness is pending and the fixed ready deadline has not elapsed. `hyperloom/rtx.py:864` ends grace when the helper accepts readiness and health. Earlier strikes are preserved; the first ambiguity without history strikes. Bracket width and high bounds are logged locally. | `tests/test_pc_big_review.py:158`, `:200` and `:257`: high-bound growth with a stationary lower bound, exact/sub-GiB progress, retained strikes, stalls, readiness, timeout, bounded hidden foreign N/A use and immediate large foreign residuals; realistic 90 GiB loading at 1.2 GiB/s through the real fallback survives. Real helper readiness is exercised with a fake scheduler and child. | Monotonic attribution within each bracket remains an assumption. Coincident spikes/dips and live fallback timings require qualification; ambiguous foreign use can be delayed only while the progress/readiness/deadline conditions hold. |
| D: lookup and documentation nits | `hyperloom/recipes/launch.py:140` reuses one reviewed-resource snapshot for observer creation and rendering. `hyperloom/gpu_counters.py:54` normalizes UUID keys and uses the canonical GPU prefix for driver lookup; ABI declarations are hoisted. `docs/placement-supervision.md:60` documents retained strikes and delayed foreign-use detection. | `tests/test_pc_big_review.py:226`, `test_prepare_reuses_one_reviewed_resource_snapshot` covers changing ownership with/without an initial observer. The native test covers mixed-case/whitespace lookup, normalized handle caching and one-time signature setup. `test_loading_documentation_covers_retained_strikes_and_delayed_foreign_detection` guards both conservative edge cases and the native-total qualification. | None for these nits. Ownership and observers remain transient; replacement still repeats unowned admission after shutdown. |

All new regressions use fake counters/processes and clocks or synchronous scheduling. They never sleep in real
time or start background threads. The production release remains unpublished and refused; no live GPU/WDDM
qualification is claimed.

Final full Python 3.13 suite: **46 failed, 3798 passed, 69 skipped** in 480.58 s. The reference, clean baseline
and final failing-ID sets are exactly equal: all 46 pre-existing IDs, none added or missing. All **18 additional
cases pass**. The focused lifecycle/admission/legacy/privacy run passed **419 tests, 2 skipped** with thread
warnings treated as errors. A separate run forbidding real-time sleep passed all 18 new cases in 0.39 s and
verified that the live-thread set was unchanged.

The full run emitted one known admission-heartbeat thread warning when an existing test patched global sleep
to raise KeyboardInterrupt. Admission-thread code is unchanged; that pre-existing test-isolation point remains
open. The new regressions start no background threads.

G6 retains all **21 cases byte-identically**, with the unchanged frozen fixture and reconstructed SHA-256
`40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`. Legacy reservation, gates, busy queries,
watchdog and helper acquisition/gate/verification remain AST-equal to eb9ae96; device locking matches after
path-helper inlining. Removing the new placement-only readiness signal leaves service_run AST-equal to 7e6d7aa.
Legacy rendering, launch preparation, evaluator/picker and the unowned PC-BIG query loop also retain AST equality.

Evidence logs outside git: `hl-placement-r20-before313.log`, `hl-placement-r20-after313.log`,
`hl-placement-r20-failure-comparison.log`, `hl-placement-r20-red.log`, `hl-placement-r20-focused.log`,
`hl-placement-r20-regression-isolation.log`, `hl-placement-r20-legacy-ast.log`,
`hl-placement-r20-privacy.log` and `hl-placement-r20-final-checks.log`.
Privacy, UTF-8, LF and diff whitespace checks pass. TEMP/TMP were unchanged; submodules remain uninitialized.
That follow-up is recorded in commit `583b5a9` on `hl/placement` (based on `7e6d7aa`). Its validation used
no network, GPU work, additional worktree or push.

## Test-extra conformance follow-up of 583b5a9

The test extra pins pytest **8.4.2** (the version of the reference full-suite runs; the runs below also passed with 9.0.2); runtime dependencies remain empty. CI and the Docker
helper retain unittest discovery, install `.[test]` and run pytest. README documents both commands.
All 27 pytest imports are guarded by module-level `unittest.SkipTest` when pytest is absent. All 87
baseline test modules import without discovery errors, with or without the extra available.

Final Python 3.13.7 results:

- Stdlib unittest: **1833 tests in 428.811 s; 3 failures, 44 errors, 96 skipped**.
- Pytest 9.0.2: **3800 passed, 70 skipped, 47 failure events, 7 warnings**, plus **1519 passed subtests**,
  in 546.92 s. Warnings include the previously documented background admission-thread issue.

Both runs have exactly the same **46 unique failing IDs**, with none added or missing. The existing
`test_answers_equal_the_cli` fails in two subtests, accounting for 47 events. Collection has no import
errors. The owner's reference run with pytest 8.4.2 has the same 46 failing IDs.

The unittest environment has no pytest; the full runner was staged offline from existing local packages.
Stdlib `zoneinfo` reads the existing IANA database via `PYTHONTZPATH`, without importing a timezone package.
Earlier attempts lacked that data or hit one socket abort; the socket test passes alone and in the final
standalone suite. No test behavior was changed for those environment issues.

G6 retains all **21 cases byte-identically**, with the unchanged fixture and reconstructed SHA-256
`40489d24e0a9b64d9b6186b2742d2fff734e4d66e310dd1776d8b62c340168e7`.
Evidence outside git: `hl-placement-conformance-unittest313.log`, `hl-placement-conformance-pytest313-final.log`,
`hl-placement-conformance-failures.log` (all 46 IDs), `hl-placement-conformance-changes.log` (file:line)
and `hl-placement-conformance-artifacts.log` (G6 and baseline imports).
UTF-8/LF, diff whitespace and Docker shell syntax checks pass. TEMP/TMP were unchanged; submodules remain
uninitialized. HEAD remains `583b5a9` on `hl/placement`; no network, GPU work, additional worktree, commit
or push was used for this round.

## Rebase minor fixes against a8d112c

Portal deploy preflight preserves foreign-configuration conflicts as HTTP 409 with the base's message. Regression cases
compare foreign and unrecorded target states with the base deploy check and CLI exit 4 transport mapping, before
any job, hold or stop. A specific `Conflict` subtype keeps CLI exit 4 while other preflight refusals, including
stop-list conflicts, retain the fixed redacted HTTP 400 and full local audit text.

`probe --manifest` captures stdout and stderr in binary temporary files, reads at most `MAX_JSON_BYTES + 1` per
channel and decodes strict UTF-8 through the core's bounded reader. Oversize and malformed encoding refuse;
deadline handling and valid refusal text remain covered. Core output-limit and encoding diagnostics name
`ts-recipe` for placement while keeping legacy `ss-recipe` wording.

Focused validation: **214 passed, 3 skipped**, including the new regressions, legacy comparisons against a8d112c,
all **21 G6 renders byte-identical**, and privacy checks. Full Python 3.13.7 / pytest 8.4.2: **47 failed, 3847 passed,
69 skipped, 4 warnings** in 479.00 s. The failing-ID set exactly matches the reference's 47: none added or missing.
Unittest discovery has zero import errors: **1858 tests** with the test extra, **1876** with stdlib only.
Evidence logs remain outside git: `hl-placement-minors-focused-final.log`, `hl-placement-minors-pytest313-final.log`,
`hl-placement-minors-failure-comparison.log` and `hl-placement-minors-checks.log`. TEMP/TMP are unchanged.

## Delta review follow-ups after d053db7

Foreign-owner and foreign-compute admission cases now use the PID of a reaped trivial child instead of PID
arithmetic; no other placement tests use that pattern. Reviewed helper ownership probes `helper.lock` passively
with `procs.lock_held`, without taking or creating a lock. Regression coverage checks held, stale, missing and
unreadable helper locks. Legacy launch locking is unchanged.

Core temporary inputs again use UTF-8 `write_text`, matching a8d112c's platform newline behavior; placement does
not require exact input bytes. A regression compares actual temporary input bytes with the base core call.

Focused validation: **100 passed, 3 skipped**, including legacy comparisons against a8d112c and all **21 G6 cases
byte-identical** to the unchanged oracle. Unittest discovery has zero import errors: **1859 tests** with the
test extra and **1877** with stdlib only. Full Python 3.13.7 / pytest 8.4.2: **47 failed, 3849 passed, 69 skipped**
in 537.32 s; failing IDs exactly match the base reference's 47, with none added or missing. Evidence logs remain
outside git; TEMP/TMP are unchanged. UTF-8/LF and `git diff --check` pass.
