# Plan: Phase 1b (portal) and Phase 2 (DeepSeek pair)

Status: plan, 2026-10-23. It builds on the Phase 1a acceptance results recorded in `docs/acceptance-1a.md`.

## Phase 1b: the portal (thin UI over the 1a CLI)

**Input:** `docs/ui/UI-SPEC.md` (§§1–11) and the mockups in `docs/ui/mockups/`.

**Tasks** (three Sol workers, disjoint files):

| Task | Owner files | Content |
|---|---|---|
| U1 server | `hyperloom/web/server.py`, `hyperloom/web/api.py`, `tests/test_web_*.py` | stdlib `http.server` on 127.0.0.1; Host allow-list, Origin check, JSON + `X-Hyperloom-CSRF`; GET-only reads; one mutating job at a time; `/hyperloom/api/{devices,topology,models,profiles,stats,logs,settings}` documents built from orchestrate / adapters / doctor, with `null` + `why` for missing sources (UI-SPEC §9); `hyperloom ui` opens the browser |
| U2 front end | `hyperloom/web/static/{index.html,app.css,app.js}` | hash routing over the seven screens; `app.css` derived from `docs/ui/mockups/ds.css`; the same SVG chart helpers; every state in UI-SPEC §4; capability matrix (§10): later-phase controls disabled with their blocker |
| U3 tests and accessibility | `tests/test_web_security.py`, `tests/ui/` | security negatives (foreign Origin, rebinding Host, missing token, malformed JSON); a headless Chrome smoke test per route at 1440 px and a real 390 px viewport (DevTools emulation) in both themes; contrast check of the tokens; keyboard walkthrough script |

**Acceptance**
1. Security negatives pass.
2. Every 1a action can be done from the UI: install, up and down for qwen-replicas with the router local or on a node, status, logs.
3. Stats tiles match the router/engine JSON within one poll.
4. Screenshots are compared side by side with the mockups by the reviewer.
5. The page works with no network: no CDN.

**GPU:** only the 1a profile, in the same short `hl-` single turns.

## Phase 2: DeepSeek pair

**Prerequisites**
- Engine pin `70ec42b9`, which has the production contract.
- strix-trellis `5acf090`.
- Assets present on both nodes (checked: Q2 GGUF 365.7 GB, DSpark 7.97 GB, overlay c-u6 7.59 GB, packs ours-K4/K5, DSpark vocab).
- USB4 layout 1, reported as healthy by main.

**Work**
1. `hyperloom install ds41-pair --build-on halo-1`:
   - trellis build (HIP), then the engine with `-DSTRIX_DS41_TRELLIS=ON -DSTRIX_DS41_TRELLIS_API=v12` and the trellis paths, target `strix-deepseek41-serve` plus `strix-deepseek41-rank`;
   - push to halo-2 with a sha check. The engine's rank hello also requires equal executable shas.
2. Asset manifest for the pair:
   - sizes on every start;
   - sha256 for the packs and drafter;
   - the 365 GB base model is hashed **once**, in a scheduled job outside measurement turns, and then trusted by `(path, size, mtime, inode)`.
3. `hyperloom up ds41-pair`:
   - one `pair` lease (`hl-ds41-<id>`) on halo-1 through `gpu-turn-pair.sh`;
   - layout 1 re-checked on both nodes after acquire;
   - rank 1 is a peer process with a dead-man on halo-2;
   - readiness from both `READY` events plus `/health`;
   - the router (local or on a node) has rank 0 as its single replica.

**Acceptance (pair turns of at most 20 min, labels `hl-`)**
1. Golden render equals the pin's production line, including `--transport usb4gpu`, `--prefill-path device`, DSpark and the identity env.
2. Both ranks are `READY`, and `trellis_identity` in `/api/status` equals the production identity.
3. Token regression: 8 greedy prompts × 64 tokens through hyperloom equal a lab-launched pair with the same binary sha.
4. Negatives: mismatched releases on the ranks are refused; a wrong layout aborts after acquire; a same-size wrong asset is caught by the manifest.
5. Stop: `/admin/drain` then SIGTERM; both turns are released; no process is left on either node.
6. Serve C1/C4 against the lab launch, interleaved, with an investigation threshold of 3 %.

**Phase 2.5 (after 2):** placement proposals from `plan-hetero` over the rendered descriptor, limited to qualified profiles (SPEC §13.5).

**Phase 3a (RTX prefill) stays blocked** until the user fits the heatsink, main reopens the PRO, and requests R12/R13 land.
