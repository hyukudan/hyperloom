# Phase 1b tasks: the portal (thin web UI over the 1a CLI)

**Read first**
- `docs/ui/UI-SPEC.md` (all of it, especially §§2–11), the mockups `docs/ui/mockups/*.html` and the images in `docs/ui/img/`.
- SPEC §2.4 (security) and §16.
- `docs/plan-1b-2.md`.
- The rules in `docs/phase0-tasks.md` apply: stdlib only, no identifying data, no commits, no GPU, no ssh to real machines.

**The portal is a thin layer.** Every action calls the same functions as the CLI (`hyperloom.orchestrate`, `hyperloom.config`, `hyperloom.remote` doctor, `hyperloom.adapters`, `hyperloom.stats`). No new orchestration logic goes into the web layer.

## Task U1: server and API (owner: worker U1)
**Files:** `hyperloom/web/__init__.py`, `hyperloom/web/server.py`, `hyperloom/web/api.py`, `hyperloom/cli.py` (only add the `ui` subcommand), `tests/test_web_api.py`

**Server**
- `hyperloom ui [--port 18700] [--no-browser]` runs stdlib `ThreadingHTTPServer` on **127.0.0.1 only** and serves `hyperloom/web/static/` read-only.
- **Security** (UI-SPEC §11, SPEC §2.4):
  - exact `Host` allow-list (`127.0.0.1:<port>`, `localhost:<port>`);
  - GET is read-only;
  - mutations are `POST` + `Content-Type: application/json` + header `X-Hyperloom-CSRF: <per-launch token>` (the token is injected into `index.html` at serve time) + an `Origin` equal to the UI origin;
  - no CORS headers; bodies at most 64 KiB; one mutating job at a time (a job id with polled progress).
  - Stop and cancel are never blocked by a running job.

**API**
- `GET /hyperloom/api/{devices,topology,models,profiles,stats,logs,settings}` returns the JSON documents of UI-SPEC §9.
- Each field comes from its listed source. A missing source is `null` plus a `why` string (`"needs R16"`, `"machine offline"`, …), never a fake value.
- Data is cached per poll (2 s) so concurrent tabs do not multiply ssh calls.

**Mutations**
- `POST /hyperloom/api/{up,down,install,hosts/pin,doctor,keys/rotate}`. The arguments are the CLI's.
- Every mutation is written to the audit log (`%APPDATA%\hyperloom\audit.jsonl`: when, action, arguments with secrets redacted, result).

**Tests**
- Every endpoint, with a fake orchestrate and fake remote.
- The security negatives: a foreign Origin, a rebinding Host, a missing or wrong token, a GET with side effects refused, a wrong content type, an oversize body, a concurrent mutation refused.

## Task U2: front end (owner: worker U2)
**Files:** `hyperloom/web/static/index.html`, `app.css`, `app.js` (no other files; no CDN, no build step)

- One page with hash routing over the seven screens.
- `app.css` is derived from `docs/ui/mockups/ds.css` (the same tokens, light and dark via `prefers-color-scheme` plus a manual toggle).
- Every widget renders from the U1 JSON. `null` + `why` renders as "unavailable · why".
- All states of UI-SPEC §4: empty, loading, stale, offline, unpaired, degraded, running, starting/stopping, error, not available.
- The capability matrix (UI-SPEC §10): later-phase controls are disabled with their blocker (for example "later phase · TensorMeld agent").
- The Engines start dialog has "Router runs on" (this machine or any paired machine) and node/device checkboxes (SPEC §16.2).
- Confirmation dialogs list their effects. Stop and cancel have no confirmation delay.
- The chart helpers follow `shell.js` (SVG, no library).
- Polling pauses in a background tab, and authoritative state is re-fetched before mutations are re-enabled.
- Accessibility (UI-SPEC §7, §11): labelled native controls, an accessible menu button, dialog focus trap and return, `aria-live` only for errors and new warnings, `prefers-reduced-motion`.

## Task U3: UI tests (owner: worker U3)
**Files:** `tests/test_web_security.py` (end-to-end security against a real `hyperloom ui` server on a free port with fakes), `tests/ui/smoke.py`, `tests/ui/README.md`

- **`smoke.py`** starts the server with fixture data (fake orchestrate) and uses headless Chrome (`--headless=new`, found via the usual Windows/Linux paths; skip when absent) to load each route at 1440×1000 and in a **real 390 px viewport**. Use the DevTools protocol over `--remote-debugging-port` with stdlib `websocket`-free HTTP + `Emulation.setDeviceMetricsOverride`; if that is too heavy, use a `--window-size=390,…` window together with a CSS check that `document.documentElement.clientWidth == 390`.
- It runs in light and dark, saves screenshots to a temp dir, and fails on any console error (`Runtime.consoleAPICalled` / `Runtime.exceptionThrown`).
- A **contrast check** parses the colour tokens from `app.css` and asserts ≥ 4.5:1 for text tokens on their surfaces, in both themes.
- A **keyboard walkthrough** script (documented in the README) is runnable by the reviewer.
