# Placement rendering

The opt-in `rtx-decode-ds41-placement` adapter is for the RTX-alone DS decode replica. Its `engine_sha` is deliberately
null until claudeinfo-release publishes the pinned release. Rendering also requires an explicit `placement-v1`
capability for that release. The selected replica at a919c257 does not parse the placement flags, so the shipped
capability list is empty. A published SHA alone does not enable launch.
Qualification also requires the published SHA to equal `replica_args.AUDITED_REVISION`, bound to the snapshot
revision by `tools/check_replica_args.py`. A different release refuses with `PL_E_CAPABILITY` until its parser is
audited and the mirror/snapshot are explicitly repinned. Test binaries use this identity without qualifying a
production release; the shipped preset remains unpublished.
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
Every touched UUID needs an integer byte cap with `caps_bytes <= caps_gib * GiB <= grant * GiB`; missing or
inconsistent caps are refused without clamping, including a half-byte conversion that rounds above the GiB cap.

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
Output options name future files; rendering checks existing targets and parents without creating them. File formats,
imported state positions, reference coverage and receipt contents remain native validation obligations. Numeric syntax, native
enums, the request environment allowlist and argv-only bounds are checked before admission on **every occurrence**,
including values overwritten later. Phase-dependent required inputs are checked for every supplied phase list.

The table below is derived from all 97 branches of `parse_args` in `apps/cuda_ds41_replica.cu` and the request
environment allowlist in `apps/cuda_ds41_replica_serve.hpp` at **a919c257c511b2ae4688737f2127e02eecee2255**.
`tests/fixtures/replica-parser-a919c257.json` records the extracted conversions, source lines and SHA-256 of **43**
complete source files (normalized to LF): the app, serve header, nine out-of-line parsing/configuration helpers,
and every recursively included local quoted header. The helper list is `PARSER_HELPERS` in the audit tool; it
includes argument-time dispatch setters, imported-state decoding, receipt pin lookup, controller tables, vocabulary
specs and TierSite's row/predict checks. Before qualifying another release, run this offline audit against its source:

```powershell
python tools/check_replica_args.py --source PATH/TO/REPLICA
```

Any source change fails the audit; explicitly review the parser, update the mirror and repin the snapshot before
enabling that release. The regression also checks snapshot/classification/table agreement without needing a replica
checkout. Regenerate the table after an intentional rule change with `python tools/check_replica_args.py --write`.
That command does **not** refresh the source snapshot. A new out-of-line parsing callee requires adding its
implementation to `PARSER_HELPERS`; changes to the calling file already invalidate the pin. The only unresolved
quoted includes are `tierstream/ts.h` and `strix_stream/ss_test.h`: those external library interfaces are an explicit
engine-owned boundary, not implicitly resolved from another checkout or an installed library.

U32 means the Windows parser's `std::stoul` followed by a uint32 cast: leading ASCII whitespace, optional sign,
decimal numeric prefix, magnitude <= 4294967295, negative values negated modulo 2^32. Suffixes are ignored by native
`std::sto*` calls; these are not strict whole-token conversions. F32/F64 mean `std::stof`/`std::stod`: decimal or
hexadecimal floating prefix, signed infinity and NaN (including a payload) accepted; overflow and underflow to zero
refused, representable Windows subnormals accepted. No probability [0,1] range is invented. Native `split` skips empty
fields. Tokens themselves must be nonempty strings without NUL/CR/LF. Options and values are separate argv tokens.
All enums are case-sensitive. The first six controller costs must remain positive after conversion from ms to float
seconds; the optional seventh draft cost has no positivity check in native code.

The following is the complete refusal audit for that revision. Sources are relative to the replica root and line
numbers name the pinned source, not this repository. The option table below supplies the individual source line
and disposition for **every** parser branch, including all conversions of placement-owned options. “Yes, conflict”
means pure rendering refuses the option altogether with `PL_E_CONFLICT`, so no value can reach native parsing.
The startup cutoff is construction of `CudaPrefill` at `apps/cuda_ds41_replica.cu:647`; its GGUF/model reading and
allocation are engine-owned. The audit also follows argv-only configuration checks that native defers past that
cutoff, so their location after loading does not conceal a preflight rule.

| Rule / refusal | Source file:line at a919c257 | Mirrored / ownership and reason |
|---|---|---|
| A positional MODEL is required before options; it must be a readable regular file. | apps/cuda_ds41_replica.cu:271; apps/cuda_ds41_replica.cu:219 | Yes. Missing model, unreadable/stat-failing model, empty or NUL/CR/LF-bearing tokens are refused synchronously. Token framing is also a hyperloom spool constraint. |
| Every value option needs a separate value; unknown options, extra positional tokens and `--option=value` are refused. | apps/cuda_ds41_replica.cu:319; apps/cuda_ds41_replica.cu:441 | Yes. The 97-branch table exhausts the accepted option names and arities. A following option cannot hide a missing value. |
| Every U32, signed-long, F32 and F64 conversion can reject invalid syntax / native overflow / underflow. | apps/cuda_ds41_replica.cu:322; apps/cuda_ds41_replica.cu:327; apps/cuda_ds41_replica.cu:334; apps/cuda_ds41_replica.cu:366; apps/cuda_ds41_replica.cu:374; apps/cuda_ds41_replica.cu:392; apps/cuda_ds41_replica.cu:430 | Yes: conversions below for accepted options; conflict for allocation/dispatch options. Windows widths and numeric-prefix semantics are described above. No additional probability, sign, window-size or serve-idle range is invented. |
| `--env` needs NAME=VALUE and one of the two exact allowed names. | apps/cuda_ds41_replica.cu:464; apps/cuda_ds41_replica_serve.hpp:51 | Yes, for every occurrence. Empty VALUE and further `=` characters are allowed. |
| A serving worker needs nonzero absolute-position and row capacities. | apps/cuda_ds41_replica.cu:470; apps/cuda_ds41_replica.cu:471 | Yes for known admitted capacities; argv capacity overrides conflict. Emitting these capacities from the placement documents instead of requiring legacy flags is external placement-loader work; the current release cannot launch placement. |
| `--import-state` is required and readable. | apps/cuda_ds41_replica.cu:492; src/deepseek41_attention_state.cpp:82; src/deepseek41_attention_state.cpp:430 | Yes for presence/readability on every occurrence; state bytes are engine-owned as listed below. |
| The only cache is `tierstream`. | apps/cuda_ds41_replica.cu:493 | Yes, conflict: placement owns `--cache`, even the matching value. |
| `steps + 16`, and for chat phases `chat_prompt + chat_steps + 16`, must fit INT_MAX. | apps/cuda_ds41_replica.cu:507; apps/cuda_ds41_replica.cu:514 | Yes, without integer wrap, including overwritten numeric and phase occurrences. |
| Unknown phase family; missing/nondecimal phase suffix; illegal trailing `w`; malformed `b` budget; schats `f` must be 1..5. Phase numeric conversions must fit. | apps/cuda_ds41_replica.cu:520; apps/cuda_ds41_replica.cu:534; apps/cuda_ds41_replica.cu:571; apps/cuda_ds41_replica.cu:580; apps/cuda_ds41_replica.cu:586; apps/cuda_ds41_replica.cu:589; apps/cuda_ds41_replica.cu:1852; apps/cuda_ds41_replica.cu:1925 | Yes. `split` skips empty fields; pressure without a suffix means K=0. `b` is not part of K. All supplied phase lists are checked. |
| Smoke/wsmoke require layers < 40; gsmoke/csmoke require layers 3..39; csmoke also requires stream hashes. Partial models cannot run exact/chat phases and require a reference. | apps/cuda_ds41_replica.cu:526; apps/cuda_ds41_replica.cu:527; apps/cuda_ds41_replica.cu:530; apps/cuda_ds41_replica.cu:531; apps/cuda_ds41_replica.cu:533; apps/cuda_ds41_replica.cu:542; apps/cuda_ds41_replica.cu:543 | Yes: every smoke family is refused in this full-model adapter; `--layers` and `--stream-hashes` conflict. No partial-model exemption can bypass full-model checks. |
| Resolved max_rows must be 1..8; window phases require >= 2; speculative phases require <= block_size+1 = 6. | apps/cuda_ds41_replica.cu:536; apps/cuda_ds41_replica.cu:537; apps/cuda_ds41_replica.cu:538; apps/cuda_ds41_replica.cu:540; include/strix/ds41_dspark.hpp:45 | Yes when `placement.config.max_rows` is present. Missing capacity cannot justify accepting an unconditional K violation; loader-derived capacities must still be enforced by the engine. |
| Spec/schats need a drafter. | apps/cuda_ds41_replica.cu:539 | Yes, currently refused: the legacy required `--draft` cannot be supplied because it is placement-owned. The native placement loader must load the admitted drafter and replace this legacy requirement before speculative startup becomes supported. |
| Exact/pressure/wforce/spec require `--ref`; chat/dchat/schat phases require `--chats` and `--seqs-file`. Default phases have these same requirements. | apps/cuda_ds41_replica.cu:541; apps/cuda_ds41_replica.cu:544 | Yes, for every supplied phase list and for the default list when no `--phases` is given. |
| Exact/pressure/wforce/spec require at least 64 steps. | apps/cuda_ds41_replica.cu:554 | Yes; every supplied steps value must meet the requirement if any supplied phase list needs it. |
| Each chats index must convert with U32; its bound depends on sequence-file contents. | apps/cuda_ds41_replica.cu:568; apps/cuda_ds41_replica.cu:1859; apps/cuda_ds41_replica.cu:1928 | Yes for conversion; engine-owned index bound, because it requires reading the sequence file. |
| Phase K <= max(predict, 8) and max_rows x K <= 64. | apps/cuda_ds41_replica.cu:587; apps/cuda_ds41_replica.cu:590; src/cuda/ds41_tier_site.cu:266 | Yes: unconditional K <= 64 even without capacities; product against admitted max_rows; prediction bound against cached allocation flags when available. The first digit run is K; optional b/f fields do not change it. |
| Allocated max_rows x max_predict <= 64, even if all requested phases use K=0. | src/cuda/ds41_tier_site.cu:75; apps/cuda_ds41_replica.cu:780 | Yes when the cached allocation reference is available, with native max_predict=max(predict,8). If max_rows is unavailable, even the minimum valid row count must fit. Loader-specific capacities unavailable to pure rendering remain engine-owned. |
| Full model needs a readable `--receipt`; its admitted hash binding must pass. | apps/cuda_ds41_replica.cu:596; apps/cuda_ds41_replica.cu:213; apps/cuda_ds41_replica.cu:224; apps/cuda_ds41_replica.cu:226 | Yes for presence/readability; engine-owned binding to pinned model size/mtime/hash, which requires receipt contents. No model hashing during render. |
| Optional reference/routes/sequence/residency-seed/winpf-prior/controller-prior inputs must be readable. Controller `none` and `builtin[:CAP]` need no file; decimal CAP converts with U32, preserving Windows drive colons. | apps/cuda_ds41_replica.cu:136; apps/cuda_ds41_replica.cu:173; apps/cuda_ds41_replica.cu:558; apps/cuda_ds41_replica.cu:760; apps/cuda_ds41_replica.cu:788; apps/cuda_ds41_replica.cu:1135; src/ds41_dspark_driver.cpp:362 | Yes for every input occurrence, including overwritten paths. Content validation remains engine-owned. |
| Routes' seqmod needs two nonempty colon-separated U32 fields (extra fields ignored). | apps/cuda_ds41_replica.cu:170; apps/cuda_ds41_replica.cu:171 | Yes, even if no routes file is supplied. Native adds no M>0 or fold<M validation; see replica follow-up below. |
| `--window` is controller, probe or fixed:U32; controller scope is run or chat; reset-cache is warm or open. | apps/cuda_ds41_replica.cu:1170; apps/cuda_ds41_replica.cu:1171; apps/cuda_ds41_replica.cu:1174; apps/cuda_ds41_replica.cu:2127 | Yes. The first two checks are deferred until configuration, and reset-cache until a served request, but pure rendering checks all occurrences now. |
| Explicit controller costs other than none require a prior other than none; 6 or 7 F64 values; first six positive after float(ms / 1000). | apps/cuda_ds41_replica.cu:1146; apps/cuda_ds41_replica.cu:1148; apps/cuda_ds41_replica.cu:1150; apps/cuda_ds41_replica.cu:1153; apps/cuda_ds41_replica.cu:1155; apps/cuda_ds41_replica.cu:1157 | Yes. `--controller-prior none` without explicit costs drops the default costs and is accepted. All explicit cost/prior occurrences participate in checking. |
| JSON/timeline/trace/controller-save/residency-save output cannot be opened, flushed or atomically renamed. | apps/cuda_ds41_replica.cu:617; apps/cuda_ds41_replica.cu:1167; src/ds41_dspark_driver.cpp:401; src/ds41_dspark_driver.cpp:409; apps/cuda_ds41_replica_serve.hpp:109; apps/cuda_ds41_replica_serve.hpp:111; apps/cuda_ds41_replica_serve.hpp:119; apps/cuda_ds41_replica.cu:2223 | Yes for determinable failures: target is a directory/non-file, parent missing/non-directory, known non-writable file/parent; residency also checks the `.tmp` target and rename parent. No creation, write or truncation. Actual ACL/sharing/open/flush/rename failures and races remain engine-owned runtime I/O. |
| Absolute capacity must cover imported.position + needed_extra. | apps/cuda_ds41_replica.cu:633; apps/cuda_ds41_replica.cu:634 | Yes for the lower bound needed_extra <= admitted config.max_context_tokens, even at imported position zero. Engine-owned for the remaining imported-position term, because rendering does not read state contents. |
| Draft keep other than 0/64 needs a stats file; draft vocabulary has full/numeric/files:/auto: syntax; warmup-graphs entries convert with U32. | apps/cuda_ds41_replica.cu:664; src/ds41_dspark_vocab.cpp:55; src/ds41_dspark_vocab.cpp:59; src/ds41_dspark_vocab.cpp:68; src/ds41_dspark_vocab.cpp:96; src/ds41_dspark_vocab.cpp:97; apps/cuda_ds41_replica.cu:2036 | Yes, conflict for all associated allocation/graph options. An argv-only required combination cannot bypass placement by matching the plan. |
| Repeated scalar/flag options and mutually overriding dispatch flags. | apps/cuda_ds41_replica.cu:315; apps/cuda_ds41_replica.cu:440; apps/cuda_ds41_replica.cu:486; apps/cuda_ds41_replica.cu:487 | No native duplicate-option refusal exists. Scalars are usually last-wins; env accumulates; router-pf0 and qakv-auto have explicit precedence. Hyperloom deliberately validates every supplied value/input/phase and conflicts on every allocation override, including repeats/equals spellings. There is no additional native generic mutual-exclusion rule. |

The remaining startup refusals depend on contents, resources or a loaded external implementation. They are
explicitly engine-owned; a native syntax check that is delayed until after loading is **not** included in this
exemption. File readability and the argv-only parts of these checks are covered above.

| Engine-owned refusal | Source file:line at a919c257 | Why it cannot be decided by this pure renderer |
|---|---|---|
| Imported state: truncated file, vector count > 2^36, bad magic/version, >64 layers, checksum/digest mismatch, token-history length != position. | src/deepseek41_attention_state.cpp:94; src/deepseek41_attention_state.cpp:113; src/deepseek41_attention_state.cpp:228; src/deepseek41_attention_state.cpp:229; src/deepseek41_attention_state.cpp:234; src/deepseek41_attention_state.cpp:238; src/deepseek41_attention_state.cpp:254; src/deepseek41_attention_state.cpp:255 | Requires state bytes. The file reader calls read_state directly; Bytes-only deserialize/frame/slice/merge errors in the same file are not called by this startup path. |
| Reference must cover every imported-position + i for i < steps; sequence-file token strings must convert, selected chat index must exist. | apps/cuda_ds41_replica.cu:553; apps/cuda_ds41_replica.cu:564; apps/cuda_ds41_replica.cu:568 | Requires reference/sequence contents and imported position; pure rendering only opens inputs for readability. |
| Receipt sha256/admitted_sha256/match/bytes/mtime must bind the pinned target; file can disappear or change after preflight. | apps/cuda_ds41_replica.cu:213; apps/cuda_ds41_replica.cu:219; apps/cuda_ds41_replica.cu:224; apps/cuda_ds41_replica.cu:226; src/deepseek41_model.cpp:708 | Requires receipt contents and authoritative model identity; later file I/O races remain possible. |
| Model tensor/format/geometry checks, drafter loading, physical CUDA/device/host availability, pinned peak, allocation/API failure. | apps/cuda_ds41_replica.cu:647; apps/cuda_ds41_replica.cu:657; apps/cuda_ds41_replica.cu:743; apps/cuda_ds41_replica.cu:744; apps/cuda_ds41_replica.cu:745; include/strix/cuda_ds41_common.hpp:32 | Reads the model or allocates/queries live resources. Existing cached-manifest and supervision admission does not replace the engine's V5/V6 obligations. |
| Draft keep stats lines need the complete counts array and at least one route_stats line. | apps/cuda_ds41_replica.cu:687; apps/cuda_ds41_replica.cu:688; apps/cuda_ds41_replica.cu:691 | File contents; argv selection is already refused as placement-owned. |
| Routes token layer/expert conversions, residency seed model/geometry header, numeric/in-range/nonempty keys, winpf-prior format. | apps/cuda_ds41_replica.cu:184; apps/cuda_ds41_replica.cu:187; apps/cuda_ds41_replica.cu:765; apps/cuda_ds41_replica.cu:769; apps/cuda_ds41_replica.cu:770; apps/cuda_ds41_replica.cu:773; apps/cuda_ds41_replica.cu:788; src/cuda/ds41_tier_site.cu:133 | Reads content; residency depends on model geometry and winpf parsing is in the external library's ts_open. |
| Survival table needs correct header/bins, depth lines, seen/hit counts, hits <= seen and no trailing data; WindowController::set_table rejects hits > seen. | src/ds41_dspark_driver.cpp:339; src/ds41_dspark_driver.cpp:371; src/ds41_dspark_driver.cpp:374; src/ds41_dspark_driver.cpp:376; src/ds41_dspark_driver.cpp:378; src/ds41_dspark_driver.cpp:380; src/ds41_dspark_driver.cpp:381; src/ds41_dspark_driver.cpp:394 | Reads table contents; builtin table and its cap do not introduce another argv bound. |
| Vocabulary file size multiple of four, IDs in range, available language/core files. | src/ds41_dspark_vocab.cpp:25; src/ds41_dspark_vocab.cpp:27; src/ds41_dspark_vocab.cpp:32; src/ds41_dspark_vocab.cpp:77; src/ds41_dspark_vocab.cpp:86; src/ds41_dspark_vocab.cpp:89 | Reads selected files/prompt; the allocation-affecting vocab override is refused before admission. |
| TierSite rows x model top_k <= SS_DG_MAX_IDS; slots/spare targets fit model keys; routed layers exist; external descriptor/plan/open/view validation (rev 3.4 absent_err, protocol, part strides, gated layer order and unit_base). | src/cuda/ds41_tier_site.cu:24; src/cuda/ds41_tier_site.cu:39; src/cuda/ds41_tier_site.cu:75; src/cuda/ds41_tier_site.cu:79; src/cuda/ds41_tier_site.cu:135; src/cuda/ds41_tier_site.cu:138; src/cuda/ds41_tier_site.cu:140; src/cuda/ds41_tier_site.cu:142; src/cuda/ds41_tier_site.cu:146 | Model-dependent geometry or external placement loader; the independent rows/predict and per-phase K limits at :75/:266 are mirrored above, not blanket engine-owned. |
| Spool directory creation, existing lock, request-file read, atomic response write/flush/rename failures. | apps/cuda_ds41_replica_serve.hpp:81; apps/cuda_ds41_replica_serve.hpp:109; apps/cuda_ds41_replica_serve.hpp:111; apps/cuda_ds41_replica_serve.hpp:119; apps/cuda_ds41_replica_serve.hpp:132; apps/cuda_ds41_replica_serve.hpp:134 | Owned service helper selects a fresh spool after pure admission; actual filesystem operations are necessarily later. User-supplied serve controls are refused by the service renderer. |

For completeness, these are **post-load/request/execution** refusals, rather than additional startup-parser rules:

- Request-only option/load-time/launch-option and arity checks (`apps/cuda_ds41_replica_serve.hpp:64`, :65, :66,
  :68, :72) remain with the worker's runtime allowlist; pure startup has a distinct positional-model grammar.
- `reset-cache open` requires a linked reset implementation; schedule changes need ts_dg_set_sched; requests must
  fit the worker's loaded positions, test-hooks and drafter, and cannot use gsmoke/csmoke
  (`apps/cuda_ds41_replica.cu:2128`, :2130, :2133, :2136, :2138, :2140). These require the loaded worker/library.
- gsmoke/csmoke/wsmoke need an earlier smoke hash run, and gsmoke conflicts with stream hashes
  (`apps/cuda_ds41_replica.cu:1493`, :1494, :1587, :1643). All such phases already fail this adapter's full-model
  smoke check. Missing per-step reference tokens (:1660, :1697, :1782), Engram mismatches (:865), graph/kernel/API
  failures and TierSite token-state/wait-timeout checks (`src/cuda/ds41_tier_site.cu:183`, :272, :288, :308, :315,
  :322, :354, :385, :390, :407, :411, :425, :431) depend on contents or execution, not new startup argv checks.

The argument-time setters introduce no additional refusals: they assign/normalize globals
(`src/cuda/ds41_attention.cu:1059`, :1060; `src/cuda/ds41_core_norm.cu:400`, :401;
`src/cuda/ds41_core_mma.cu:318`, :319; `src/cuda/ds41_router_v2.cu:240`). CudaPrefillOptions and TierWinpf are
aggregate defaults (`include/strix/cuda_ds41_prefill.hpp:74`, `include/strix/cuda_ds41_tier_site.hpp:60`).
BatchWindowController, window_policy_from_env and generate_greedy/generate_speculative in the fingerprinted
driver file are not called by this app's parser/configuration path; their separate validation is not a hidden
replica argv rule (`src/ds41_dspark_driver.cpp:146`, :267, :299, :428, :437, :446, :452, :458, :471).

Before-admission regressions cover every mirrored class: `tests/test_placement_argv_preflight.py` checks required files,
every accepted option's arity, readability, duplicates and all capacity/dispatch conflicts;
`tests/test_placement_argv_values.py` checks every allowed numeric conversion, enums, environment and overwritten
values; `tests/test_placement_argv_capacities.py` checks every phase family, repeated phase lists, the three reported
K violations, admitted row/predict/position boundaries, controller required combinations and output failures.
The tests forbid leases/pins, publication, gates, helper/process/lock creation and state writes, and compare the
filesystem before/after. Fingerprint regressions mutate every helper/header, including a transitive include.

Replica follow-up remains explicit: implement the placement loader and its capacity/shape derivation, expose any
loader-derived prediction limit not represented by cached allocation flags, and load an admitted drafter without
`--draft`. Native `seed_order` currently takes M from `--seqmod` and uses `% M` (`apps/cuda_ds41_replica.cu:179`)
without checking M != 0: that is a potential native division-by-zero bug, not a parser refusal to mirror. The
replica must define/refuse it; hyperloom must review that parser change and repin before qualifying the release.

<!-- replica-parser-table:start -->
| Native option | Source at a919c257 | Native conversion | Pure rendering rule |
|---|---|---|---|
| `--arena-gib` | apps/cuda_ds41_replica.cu:334 | double | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--attn-fork` | apps/cuda_ds41_replica.cu:414 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--attn-h1` | apps/cuda_ds41_replica.cu:419 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--attn-pf` | apps/cuda_ds41_replica.cu:420 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--attn1` | apps/cuda_ds41_replica.cu:418 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--cache` | apps/cuda_ds41_replica.cu:386 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--chat-logits` | apps/cuda_ds41_replica.cu:358 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--chat-prompt` | apps/cuda_ds41_replica.cu:345 | u32 | U32; chat-prompt + chat-steps + 16 <= INT_MAX when chats are selected. |
| `--chat-steps` | apps/cuda_ds41_replica.cu:346 | u32 | U32; chat-prompt + chat-steps + 16 <= INT_MAX when chats are selected. |
| `--chats` | apps/cuda_ds41_replica.cu:344 | value | Each nonempty comma-separated field uses U32; file-dependent index bound engine-owned. |
| `--controller-costs` | apps/cuda_ds41_replica.cu:380 | value | none or 6/7 comma-separated F64 costs; first 6 > 0 after float(ms / 1000); requires prior != none. |
| `--controller-explore` | apps/cuda_ds41_replica.cu:381 | u32 | U32; no additional parser range. |
| `--controller-learn` | apps/cuda_ds41_replica.cu:382 | flag | Flag; takes no value. |
| `--controller-prior` | apps/cuda_ds41_replica.cu:377 | value | none, builtin[:CAP], or readable FILE[:CAP]; decimal CAP uses U32. |
| `--controller-prior-alt` | apps/cuda_ds41_replica.cu:379 | flag | Flag; takes no value. |
| `--controller-save` | apps/cuda_ds41_replica.cu:378 | value | Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned. |
| `--controller-scope` | apps/cuda_ds41_replica.cu:369 | value | One of: chat, run. |
| `--draft` | apps/cuda_ds41_replica.cu:365 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--draft-device` | apps/cuda_ds41_replica.cu:366 | long | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--draft-keep` | apps/cuda_ds41_replica.cu:374 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--draft-keep-from` | apps/cuda_ds41_replica.cu:375 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--draft-trace` | apps/cuda_ds41_replica.cu:376 | value | Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned. |
| `--draft-vocab` | apps/cuda_ds41_replica.cu:367 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--dsrtxk-off` | apps/cuda_ds41_replica.cu:426 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--engram` | apps/cuda_ds41_replica.cu:354 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--engram-cache-mb` | apps/cuda_ds41_replica.cu:389 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--env` | apps/cuda_ds41_replica.cu:440 | value | NAME=VALUE; NAME is STRIX_DS41_CUDA_ENGRAM_CHECK or STRIX_DS41_REPLICA_WINDOW_ENGRAM_PREFETCH; arbitrary/empty VALUE. |
| `--flush-ready` | apps/cuda_ds41_replica.cu:387 | flag | Flag; takes no value. |
| `--fuse-hc` | apps/cuda_ds41_replica.cu:361 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--fuse-off` | apps/cuda_ds41_replica.cu:427 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--fuse-shared` | apps/cuda_ds41_replica.cu:362 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--hc-cluster` | apps/cuda_ds41_replica.cu:423 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--hc-partial5` | apps/cuda_ds41_replica.cu:424 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--host-margin-gib` | apps/cuda_ds41_replica.cu:353 | double | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--host-spares` | apps/cuda_ds41_replica.cu:336 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--import-state` | apps/cuda_ds41_replica.cu:331 | value | Readable regular input file; contents engine-owned. |
| `--json` | apps/cuda_ds41_replica.cu:349 | value | Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned. |
| `--layers` | apps/cuda_ds41_replica.cu:355 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--max-pinned-gib` | apps/cuda_ds41_replica.cu:356 | double | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--max-positions` | apps/cuda_ds41_replica.cu:432 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--max-rows` | apps/cuda_ds41_replica.cu:384 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--moe-expand` | apps/cuda_ds41_replica.cu:416 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--moe-v1b3` | apps/cuda_ds41_replica.cu:425 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--no-flush-ready` | apps/cuda_ds41_replica.cu:388 | flag | Flag; takes no value. |
| `--no-wb-defer` | apps/cuda_ds41_replica.cu:397 | flag | Flag; takes no value. |
| `--ob-expand` | apps/cuda_ds41_replica.cu:415 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--ob-one` | apps/cuda_ds41_replica.cu:417 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--pdl` | apps/cuda_ds41_replica.cu:402 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--pdl-np` | apps/cuda_ds41_replica.cu:403 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--pdl-regs` | apps/cuda_ds41_replica.cu:407 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--phases` | apps/cuda_ds41_replica.cu:341 | value | Native grammar; every K <= 64; admitted max_rows x K <= 64 and K <= max(cached --predict, 8) when known; per-occurrence required inputs; smoke refused for full-model placement. |
| `--predict` | apps/cuda_ds41_replica.cu:337 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--prefetch-layer` | apps/cuda_ds41_replica.cu:338 | u32 | U32; no additional parser range. |
| `--prefetch-token` | apps/cuda_ds41_replica.cu:339 | u32 | U32; no additional parser range. |
| `--pressure-free` | apps/cuda_ds41_replica.cu:359 | u32 | U32; no additional parser range. |
| `--pressure-recent` | apps/cuda_ds41_replica.cu:360 | u32 | U32; no additional parser range. |
| `--pressure-spares` | apps/cuda_ds41_replica.cu:340 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--probe-phase` | apps/cuda_ds41_replica.cu:383 | value | Opaque comma-separated phase filter; native imposes no enum or numeric syntax. |
| `--qakv-auto` | apps/cuda_ds41_replica.cu:413 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--qakv-s1` | apps/cuda_ds41_replica.cu:412 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--qual` | apps/cuda_ds41_replica.cu:434 | flag | Flag; takes no value. |
| `--receipt` | apps/cuda_ds41_replica.cu:357 | value | Readable regular input file; contents engine-owned. |
| `--records-inline` | apps/cuda_ds41_replica.cu:428 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--ref` | apps/cuda_ds41_replica.cu:332 | value | Readable regular input file; contents engine-owned. |
| `--reset-cache` | apps/cuda_ds41_replica.cu:439 | value | One of: open, warm. |
| `--residency-save` | apps/cuda_ds41_replica.cu:435 | value | Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned. |
| `--residency-seed` | apps/cuda_ds41_replica.cu:436 | value | Readable regular input file; contents engine-owned. |
| `--route-stats` | apps/cuda_ds41_replica.cu:373 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--router-pf` | apps/cuda_ds41_replica.cu:400 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--router-pf0` | apps/cuda_ds41_replica.cu:401 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--router-v2` | apps/cuda_ds41_replica.cu:399 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--routes` | apps/cuda_ds41_replica.cu:342 | value | Readable regular input file; contents engine-owned. |
| `--seqmod` | apps/cuda_ds41_replica.cu:343 | value | First two nonempty colon-separated fields use U32; extra fields ignored, as native. |
| `--seqs-file` | apps/cuda_ds41_replica.cu:347 | value | Readable regular input file; contents engine-owned. |
| `--serve` | apps/cuda_ds41_replica.cu:429 | value | Spool path; helper-owned in service rendering. |
| `--serve-idle` | apps/cuda_ds41_replica.cu:430 | double | F64; no additional parser range. Helper-owned in service rendering. |
| `--serve-keep-on-fail` | apps/cuda_ds41_replica.cu:433 | flag | Flag; takes no value. |
| `--serve-max` | apps/cuda_ds41_replica.cu:431 | u32 | U32; no additional parser range. Helper-owned in service rendering. |
| `--shared-fork` | apps/cuda_ds41_replica.cu:411 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--spares` | apps/cuda_ds41_replica.cu:335 | u32 | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--steps` | apps/cuda_ds41_replica.cu:333 | u32 | U32; steps + 16 <= INT_MAX; >= 64 for exact/pressure/wforce/spec phases. |
| `--stream-hashes` | apps/cuda_ds41_replica.cu:385 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--test-hooks` | apps/cuda_ds41_replica.cu:438 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--timeline` | apps/cuda_ds41_replica.cu:350 | value | Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned. |
| `--timing` | apps/cuda_ds41_replica.cu:348 | flag | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--verify` | apps/cuda_ds41_replica.cu:351 | u32 | U32; no additional parser range. |
| `--warm` | apps/cuda_ds41_replica.cu:352 | u32 | U32; no additional parser range. |
| `--warmup-graphs` | apps/cuda_ds41_replica.cu:437 | value | Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings). |
| `--wb-alt` | apps/cuda_ds41_replica.cu:398 | u32 | U32; no additional parser range. |
| `--wb-defer` | apps/cuda_ds41_replica.cu:396 | flag | Flag; takes no value. |
| `--window` | apps/cuda_ds41_replica.cu:368 | value | controller, probe, or fixed:U32. |
| `--winpf-l1-cap` | apps/cuda_ds41_replica.cu:391 | u32 | U32; no additional parser range. |
| `--winpf-pmin` | apps/cuda_ds41_replica.cu:392 | float | F32. |
| `--winpf-prior` | apps/cuda_ds41_replica.cu:390 | value | Readable regular input file; contents engine-owned. |
| `--winpf-prior-cap` | apps/cuda_ds41_replica.cu:393 | u32 | U32; no additional parser range. |
| `--winpf-prior-pmin` | apps/cuda_ds41_replica.cu:394 | float | F32. |
| `--winpf-revert` | apps/cuda_ds41_replica.cu:395 | u32 | U32; no additional parser range. |
<!-- replica-parser-table:end -->

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

Engine-dependent open point: context-v1 ingestion. The helper does not ingest context observations yet: the
published engine must first define and emit a realised checkpoint-1 JSONL stream, including its location and
per-start ownership. Guessing a stream from request outputs could ingest stale runs or checkpoint-2 readings.
The unused `launch.ingest_context` and `commands.manifest_store` wrappers have been removed. Manifest acquisition
already persists through `content_cache.ManifestStore`; the CPU-only `runtime.ingest` parser remains tested for
the future integration. No start or stop writes a context-v1 document today. The proposed JSONL event shape
is `event=context`, `checkpoint=1`, the six context identity fields (`arch`, `device_name`, `driver`, `platform`,
`build_sha256`, `signature`), `ctx_observed` in bytes, and an attributable `method` (`pdh-process-dedicated`, `nvml-process`, or
`amdsmi-process`). Checkpoint 2 and other events do not enter the context allowance. Samples remain oldest first;
Tierstream owns their aggregation. Input logs and context documents remain local.

The single-/two-device G6 fixture contains placeholders only. Tests generate physical identities and absolute paths
at runtime and use a test-only release SHA; these do not qualify a production release.

Placement fidelity uses the admitted `devices[].lines[].component` entries retained in the service record. A placed
`drafter` requires both `chats0` and `schats0`, with every plain and speculative token/end-state arm compared against
the pinned reference. A plan without a drafter requires only `chats0`. This capability never comes from argv:
placement argv has no `--draft`. Both cases still require attributable visibility on every touched GPU.
