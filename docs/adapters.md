# Engine adapters, per-preset pins and unpublished sources

Each model runs on its own engine. hyperloom keeps that modular with one adapter per engine family
(`hyperloom/engines.py`). Presets stay pure data: argv, env, assets and pins. The adapter decides how the
family is built, placed, waited for and stopped.

## Adapter registry

| | `qwen-replicas` | `ds41-pair` | `minimax-pair` |
|---|---|---|---|
| binary / build target | `strix-hip-decode-token` | `strix-deepseek41-serve` | `strix-minimax-serve` |
| topology | replicas: one node per process, one single turn each | pair: TP2 rank 0 / rank 1, one PAIR turn | pair |
| layout check after acquire | none | layout 1 on both nodes | layout 1 on both nodes |
| readiness (node) | stderr `strix serve: listening on ADDR:PORT model ...` | stdout JSON `{"event":"READY"}` | rank 0: stdout `READY rank 0 http://ADDR:PORT model ...`; rank 1: stdout `rank 1: load <s> s` |
| health | `/health` = 200 on the replica | `/health` = 200 on rank 0 (both ranks) | `/health` = 200 on rank 0 (both ranks) |
| stop | SIGTERM (no HTTP drain, R2) | `POST /admin/drain` on rank 0, then SIGTERM | SIGTERM rank 0 first and wait for it to exit (≤ 30 s), then rank 1 |
| bind / auth | node private address + key file | node private address + key file | 127.0.0.1, no key (see below) |
| router | every replica | rank 0 as the single replica | rank 0 as the single replica, through a router on rank 0's machine |
| trellis build | none | `STRIX_DS41_TRELLIS*` (API per preset) | `STRIX_MINIMAX_TRELLIS_DIR` tree |
| label | `hl-qwen-<node>-<id>` | `hl-ds41-<id>` | `hl-mm-<id>` |
| capability check | static: every argv flag is a string in the ELF (never executed) | same | same |

A preset names its family with `"family"`. Without the key, the preset name is the family, as in the original
presets. `config.PRESET_NAMES` lists every launchable preset.

### MiniMax pair: events, exit codes, auth

These come from `apps/minimax_serve_main.cpp` at 862a2f0d.

- **Rank 0** prints, in this order:
  - `connected: rank 0 transport ...`
  - `rank 0: load <s> s, device used <GiB> GiB`
  - `READY rank 0 http://BIND:PORT model ID slots S context C`, after its HTTP listener is up
  - `rank 0: STOPPED`, at the end
- **Rank 1** never serves HTTP. It prints `connected: ...`, then `rank 1: load ...`, follows the control blocks, and
  prints `rank 1: STOPPED` when rank 0 sends the shutdown block.
- **Exit codes:** both ranks exit 0 on a normal stop and 2 on an error (an exception, or the usage message).
- **SIGTERM / SIGINT on rank 0:** its scheduler ends, cancels running requests, and sends the shutdown block to rank 1.
- **SIGTERM on rank 1:** it only sets a flag. Its follow loop does not check that flag, so rank 1 ends when the shutdown
  block arrives or when the transport times out (60 s, `STRIX_DIST_*_TIMEOUT_MS`). That is why the node stops rank 0
  first (`leader-first`).
- **No API key.** The serve has no key option and no key file. Generation routes are unauthenticated, and `--bind`
  defaults to 127.0.0.1.
  - **Safe choice:** the preset binds 127.0.0.1, and `up` and `render_router` refuse any router not on rank 0's machine
    (`--router <rank0>`).
  - The router listens on that machine's private address with its own key file (SPEC §2.7, TensorMeld's
    non-loopback rule), so generation on the LAN is authenticated by the router.
  - The rank 0 health check runs on rank 0's machine (the lease supervisor), against `http://127.0.0.1:PORT/health`.
  - The portal's engine statistics for this family are therefore "unavailable": the engine is not reachable from the
    control machine, and its `/metrics` is `mm_*` text with no `/api/status`.
- **No DSpark in serve.** `--dspark`/`--dspark-exl3` exist only in `strix-minimax-rank` at this pin, so the preset has
  one asset: `mm_model`, the EXL3 model directory with `tokenizer.json`.

## Per-preset component pins

`versions.lock` keeps the default pins. A preset may add `"components": {name: {repo, commit}}`, and those rows
override the lock for that preset only:

- `engine_sha` must equal the preset's engine pin.
- The advanced-knob rule "preset engine_sha == pinned engine" uses the preset's own pin.
- Diagnostics are cited at the preset's own `engine_sha`.

A pin is a full SHA. A source that only exists in a local repository, and whose full SHA is not yet known here, may
instead record a `commit_prefix`. Install refuses such a pin unless `--source` resolves it to a full SHA starting
with the prefix. The full SHA then goes into the release manifest.

| preset | family | engine | strix-trellis | build |
|---|---|---|---|---|
| `qwen-replicas`, `ds41-pair`, `ds41-pair-long` | (unchanged) | lock 9211b07e | lock 5acf0905 (v12) | lock `hip_configure` |
| `qwen-replicas-w5` | qwen-replicas | integrate/qwen-w5 96123427 | (unused) | lock `hip_configure` |
| `ds41-pair-w5` | ds41-pair | integrate/ds41-w5 16841fe5 | c95a1695 (RC2 + `strix_weights_device_span`) | lock `hip_configure`, `STRIX_DS41_TRELLIS_API=dense_r1`, trellis `-DSTRIX_TRELLIS_ENABLE_EXPERIMENTAL_TP=ON` |
| `minimax-pair` | minimax-pair | minimax/engine 862a2f0d | prefix `92cfd46e` (qualification-v03 tree) | own configure: HIP runtime + `STRIX_FAMILY_MINIMAX=ON`, trellis tree at `STRIX_MINIMAX_TRELLIS_DIR` |

The original presets keep their recipe byte for byte, and therefore their release ids. The node recipe gains the
optional keys `trellis_mode`, `trellis_api` and `trellis_configure` only when a preset sets them.

### Build recipes (`node/build-node.sh`)

- **`ds41` trellis mode** (the default):
  - strix-trellis is built with HIP into `<build>/trellis`, plus the preset's trellis flags.
  - The engine gets `-DSTRIX_DS41_TRELLIS=ON -DSTRIX_DS41_TRELLIS_API=<api> -DSTRIX_DS41_TRELLIS_LIBRARY=...
    -DSTRIX_DS41_TRELLIS_INCLUDE_DIR=...`. The api is `v12` unless the preset says otherwise.
- **`minimax` trellis mode:**
  - strix-trellis is built into `<build>/mm-trellis/build-qualification-v03`.
  - `<build>/mm-trellis/include` links to the source's `include`. This is the layout the engine's CMake expects.
  - The engine gets `-DSTRIX_MINIMAX_TRELLIS_DIR=<build>/mm-trellis`.
- **Host tests:** the same named ctest subset for all three engines
  (`strix-serve-{frontend,deepseek-chat,api}-tests`, `strix-host-upload-tests`); all four exist at every pin.

## Unpublished sources (`hyperloom install PROFILE --source NAME=DIR[@COMMIT]`)

The w5 integration branches, the MiniMax engine and the trellis commits they link are on no remote, and they are
not pushed. To install them:

1. On the control machine, `bundles.resolve` checks that the commit in DIR equals the preset's pin (or matches its
   prefix, plus `@COMMIT` when given).
2. `bundles.create` writes a bundle that carries exactly `refs/hyperloom/source` at that commit, at depth 1 (the commit
   and its tree, like the remote `fetch --depth 1`; a few MB for the engine). It works in a throwaway bare repository
   that borrows DIR's objects through `objects/info/alternates`, so DIR gains no ref, object or config. Shallow source
   checkouts work too.
3. The node verb `bundle-prepare` creates `<root>/incoming` (0700) and returns a fresh staging name. `scp` copies the
   bundle there.
4. `bundle-accept` checks size, sha256 and the single head, then renames the file to `<root>/incoming/<commit>.bundle`.
5. In the install payload the component becomes `{"repo", "commit", "published": false, "bundle_sha256"}`. The node's
   `fetch_sources` re-verifies the bundle, marks the commit shallow and fetches `refs/hyperloom/source` from it, never
   from the remote. It then checks `rev-parse HEAD`.
6. That component record goes into `MANIFEST.json`, so a release built from a bundle has a different release id from
   a published build of the same commit.
7. `up` records the release's unpublished components in `launch.unpublished_sources`, and `status` shows them as
   `unpublished_sources`.

Without `--source`, a component is fetched from its remote by commit, as before. `--source tensormeld` is refused,
because peers fetch the router package from its remote.

## Presets

All three are qualified from the named receipt. Paths, addresses and ports are abstracted in the goldens under
`tests/fixtures/render/`.

- **`ds41-pair-w5`:**
  - Argv: the ds41-pair production line (`--transport usb4gpu --prefill-path device --dspark --dspark-vocab auto:`).
  - Env: the identity from `docs/ds41-production-defaults.md` at 16841fe5: overlay, `CB4=1`, `TRELLIS=K4`,
    `TRELLIS_MIX=head=K5`, `TRELLIS_SITES=all`, `TRELLIS_DENSE_FULL=1`, `TRELLIS_HEADOUT=1`.
  - Every integrate/ds41-w5 code default is on when unset, so none is set.
  - Receipt: the integrate/ds41-w5 combined confirmation (wave4/dsint/report.md).
- **`qwen-replicas-w5`:**
  - The rfix replica argv and environment, unchanged.
  - The w5 defaults (`INTPREFILL_DENSE_F16_TUNE=4`, `INTPREFILL_MOE_TUNE=82`,
    `MODEL_REGISTER_CHUNK_BYTES=268435456`) are on in code since 5e9315e5, so none is set.
  - The w5 opt-ins stay off: ngram `ctx`/`copy`, wide copy rows, `DRAFT_CACHE=fast`, `HC_PERSISTENT_B`.
  - Receipt: w5g validation (wave4/qwenint/w5-e2e.md).
  - The router curves are the rfix measurements.
- **`minimax-pair`:**
  - Argv spells out the serve's defaults: `--transport usb4-auto --bind 127.0.0.1 --port 18441
    --model-id minimax-m3.1-flash --slots 8 --capacity 8256 --max-rows 4096 --max-tokens 4096`.
  - No env: every `STRIX_MM_*` default is on in code.
  - Receipt: s2 serve (wave4/mmopt/fork-report.md §3).
  - Knobs: `--port` and `--max-tokens`. `--slots` and `--capacity` are fixed at the qualified values.

The new presets carry no advanced-knob or diagnostic tables. The existing tables cite file lines and qualifications
at 9211b07e. They are added per pin once re-verified.

An RTX prefill service hands off only to a decode preset at its own engine pin. This keeps `rtx-prefill-ds41`
(9211b07e) off `ds41-pair-w5`.

## Open questions (safe choice taken meanwhile)

1. **MiniMax trellis pin:** only the short id `92cfd46e` (qualification-v03) is recorded. Install needs
   `--source strix-trellis=<checkout>@<full sha>`.
   - Please confirm the full SHA and the trellis CMake options the engine was built with. Defaults are used: HIP on,
     experimental TP off.
   - Also confirm whether `STRIX_WARNINGS_AS_ERRORS` (default ON) and the absence of
     `STRIX_ENABLE_EXPERIMENTAL_Q4K_A8_RUNTIME` match the measured build.
2. **MiniMax rank 1 readiness:** taken as its `rank 1: load` line plus rank 0's `/health`. Please confirm that rank 1 is
   ready to follow at that point (rank 0's READY implies both loads finished only if load includes a collective).
3. **MiniMax load skew:** the serve sets 60 s dist timeouts. The rank app needed 600 s after a 166 s rank-0 load
   (t1c). The preset does not override the transport env, which stays reserved. Please confirm, or set the default in
   the engine.
4. **MiniMax API key:** none is supported, so the engine is loopback-only behind a router on rank 0. A
   `--api-key-file` (like the other serves) would allow LAN bind and a router on the control machine.
5. **ds41-w5 trellis build:** `-DSTRIX_TRELLIS_ENABLE_EXPERIMENTAL_TP=ON` is assumed (dense_r1 needs it), with all other
   trellis options at their CMake defaults. Please confirm that this is how bin-w5's library was built.
6. **ds41-w5 receipt:** wave4/dsint/report.md had not been written when this preset was made. The receipt names it;
   please confirm the final cells (serve C1..C8, N8) before treating the preset as qualified.
7. **ds41-w5 knobs:** `--slots 4` and `--context 4096` remain the only qualified values, as at 9211b07e.
