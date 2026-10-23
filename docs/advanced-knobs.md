# Advanced knobs (per pin, qualified)

SPEC §3.1 keeps algorithm switches out of the MVP until each one has a qualification record at the engine pin.
This page describes how they are now exposed.

## Where they come from
- Each preset (`hyperloom/presets/<profile>.json`) has an `advanced` table. A row names:
  - `kind` and `target`: an argv flag (`argv` = `--flag value`, `argv-equals` = `--flag=value`) or a `STRIX_*` variable;
  - `values`: the only qualified values (2..16), and `production`: the value the production line runs;
  - `qualification`: the campaign `receipt`, the `engine_sha` it was measured at, and the `evidence` (document at that pin);
  - optional `requires` / `conflicts` on other knobs' values, and a one-line `effect`.
- The table is data copied from the pinned engine's documents; hyperloom does not invent flags.
- Refused as targets: identity, transport, layout, numerics, endpoints and asset flags; the production identity env; `STRIX_DIST_*`;
  loader/interpreter variables; any flag the production line or a basic knob already sets.

## Rows at engine `70ec42b9`

| profile | knob | values (production first) | evidence at the pin |
|---|---|---|---|
| ds41-pair | `dspark_max_inflight` → `--dspark-max-inflight` | 4, 0 | `docs/ds41-production-defaults.md` (P17.1) |
| ds41-pair | `dspark_ranks` → `--dspark-ranks` | 2, 1 | `docs/ds41-production-defaults.md` (P17.1) |
| ds41-pair | `serve_sched` → `STRIX_DS41_SERVE_SCHED` | interleave, off | `docs/ds41-production-defaults.md` (P17.1) |
| qwen-replicas | `mtp_ngram_draft` → `--mtp-ngram-draft=` | auto, off | `docs/decode-defaults.md` (D-055) |

All four only change scheduling or which drafts are proposed; the engine documents the output as unchanged
(the target verifies every token). Opt-ins the pin lists without a campaign row (for example `--cache-profile agent`,
`STRIX_DS41_OB_SPLIT`) are not offered.

## Use
```jsonc
"profiles": { "ds41-pair": { "rank0": "halo-1", "rank1": "halo-2", "advanced": { "dspark_max_inflight": 0 } } }
```
- Values are checked against the table; `requires`/`conflicts` are checked on the effective values.
- Advanced knobs are refused unless the preset's `engine_sha` equals the engine commit in `versions.lock`
  (a pin move needs the table re-qualified).
- A production value renders nothing new, so the golden production line is unchanged.
- Any other value is appended as a named override. `hyperloom render` and `hyperloom status` then show
  `"launch": {"production": false, "advanced": {...}, "qualification": {...}}`, so the launch can never be
  mistaken for the production line.
