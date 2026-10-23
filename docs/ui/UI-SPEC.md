# UI-SPEC — `telar` portal

Status: design for main/user review (2026-10-23); sections 9-11 answer Sol review 2 (../SPEC-review-sol-2.md). Author: the product design session (Opus). Builder: Sol workers, following SPEC.md §2.4 and this file.

- Mockups: `mockups/*.html`, sharing `mockups/ds.css` and `mockups/shell.js`.
- Rendered images: `img/<screen>-{desktop,desktop-dark,narrow}.png`, at 1440 px and 390 px. Regenerate them with `./render.sh <screen>...`.
- **Every number in the mockups is sample data** (each page is marked). The figures are shaped like our measured receipts, but they are not a measurement.

## 1. Who uses it and what they need to see

**Operator.** One person, at the control machine (the Windows PC with the RTX PRO), running a small shared lab cluster. The questions they bring, in order:

1. **Is it up, and is it healthy?** Machines online, processes ready, no errors or stalls, links OK.
2. **How fast is it now?** Aggregate and per-request tok/s, TTFT, queue depth, draft acceptance, where prefill went.
3. **Where is the time going?** Per-node decode step breakdown, link latency, memory headroom.
4. **What exactly is running?** Model and quant identity, exactness mode, release/binary sha, which settings differ from production.
5. **What can I run, and how do I switch?** Placement proposals on the connected machines, with expected numbers.
6. **Safely change something:** start/stop/switch, capacity parameters, pairing a new machine, rotating keys. Every change is audited and can be rolled back.
7. **Why did it fail?** Events, warnings and failures with reasons, and a log bundle.

**Design principles**
- The production state is the obvious default, and any deviation from it is always visible.
- Lab rules (GPU turns, layout, PC reservation) appear wherever they constrain an action.
- No screen offers an action the backend would refuse. Disabled controls carry their reason.

## 2. Information architecture

| # | Screen | Purpose | Image |
|---|---|---|---|
| 1 | **Devices** | every machine with its accelerators, memory, usage, drivers, roles, health and last seen; discovered machines waiting to pair | `img/devices-*.png` |
| 2 | **Topology & links** | map of machines, roles and links (Ethernet, USB4 cables); measured bandwidth and latency; USB4 layout (read-only) | `img/topology-*.png` |
| 3 | **Models & placement** | model library with per-machine presence and verification; planner proposals for the connected set with expected numbers; start/switch | `img/models-*.png` |
| 4 | **Engines** | running profile, lease/turn countdown, processes and readiness, parameters (basic only) with diff vs production, effective config, change history with rollback | `img/engines-*.png` |
| 5 | **Live stats** | KPIs, throughput and TTFT charts, per-node step breakdown, memory headroom, link health, recent requests | `img/stats-*.png` |
| 6 | **Logs** | merged live log with parsed events (READY, POISONED, STOPPED, warnings), filters, recent failures, redacted bundle | `img/logs-*.png` |
| 7 | **Settings & pairing** | pairing flow, trusted machines and revocation, secrets storage and rotation, lab rules, config file / versions / import–export | `img/settings-*.png` |

**Global chrome**
- **Left sidebar** (collapses to a top bar with a menu under 760 px). It holds the brand, the seven screens, badges (new machines, warnings), the control machine and the telar version / lab mode.
- **Page header:** title, context tags (active profile, "Production preset · exact"), and the primary actions on the right.
- **Global status** is shown in the narrow top bar as "Running / Stopped / Degraded".

## 3. Screens

### 3.1 Devices
- **Shows:**
  - an announcement banner for discovered, unpaired machines;
  - KPI row: machines online, accelerators in use (with the reason one is excluded), GPU memory headroom (total and minimum per device), link health;
  - one card per machine.
- **Machine card:**
  - Header: name, OS, CPU, RAM, disk free, last seen, health pill (Online / Degraded / Offline / Unpaired).
  - Role tags: Portal, Router, DeepSeek rank N, Qwen replica, RTX prefill. The GPU-turn countdown appears when a lease is held.
  - Accelerator box per device:
    - name and memory, e.g. "RTX PRO 6000 Blackwell · 96 GB VRAM", or "Radeon 8060S · gfx1151 · 128 GB unified · 113 GB GPU-visible";
    - memory and GPU % meters with temperature and power;
    - driver/runtime: CUDA driver + toolkit, ROCm, kernel + **USB4 stream driver loaded**, USB4 layout;
    - policy state: an excluded device (the RTX 4090) shows "Excluded" with the policy reason.
  - Release / component shas for nodes, and a warnings inset (for example a USB4 cable retrain).
- **"Discovered, not paired" table:** name, OS, accelerators, announced-from address, key fingerprint, and a Pair button.
- **Why:** it answers "what do I have and is it healthy" in one glance, and the per-device minimum headroom shows the binding constraint.
- **Data sources:**
  - `telar-node gpu` / `doctor` (sysfs, hwmon, `usb4stream-describe`, `uname`, ROCm version);
  - `tensormeld probe` (OS, RAM, NVIDIA devices; request TM-R3 adds UUID/PCI, CPU model, AMD memory);
  - `nvidia-smi -i <uuid>` on the PC;
  - roles come from `telar`'s own lease/process state.

### 3.2 Topology & links
- **Shows:**
  - an SVG cluster map: machines with their accelerators and role chips; Ethernet links labelled with rate and latency; USB4 cables drawn per cable, with dashed warn styling when a cable has a warning;
  - a link table: kind, bandwidth, p50/p99 latency, when and how it was measured (link-probe vs live engine), health;
  - a footer stating the USB4 layout and that the portal never changes it.
- **Action:** "Measure links" runs TensorMeld `link-probe` between the chosen machines. Each probe is bounded and labelled; probes that load USB4 run only inside a pair lease.
- **Why:** the operator sees at once whether a slowdown is a link problem.

### 3.3 Models & placement
- **Model library:** model, quant/packs, size, and a per-machine state (**verified** = sha manifest matched; **size only**; **absent**; **mismatch**), plus the identity (sha prefix, pinned source revision).
  - An inline banner explains a "size only" state and offers "Schedule hash". Hashing runs only outside measurement turns.
  - Downloads and conversions are never automatic. "Add model files" registers paths and shows the exact pinned commands.
- **Proposals:** cards produced from TensorMeld `plan-hetero` over the connected hardware, plus the qualified receipts. Each card has:
  - a state: running / ready to start / needs files / not available, with its blocker (for example "component request R14");
  - the placement: who does what;
  - expected decode tok/s, TTFT and memory per device, taken from receipts and labelled as such;
  - the exactness identity;
  - conflicts (e.g. "stops the DeepSeek pair; cannot share a node").
- **Action:** "Switch to this…" opens a confirmation listing what will stop, which leases are needed and how long they last.

### 3.4 Engines
- **Start dialog** (SPEC §16.2): "Router runs on" (default: this machine, or any paired machine) and the machines/devices to link, pre-filled from the profile; unreachable declared machines block the start with their name.
- **Profile header:**
  - name, state, start time, release id (the same on both ranks), identity;
  - the **GPU turn countdown** (label, time left, cap, drain margin, "extension needs main's approval").
- **Process table:** router, ranks and RTX prefill, each with its machine, state (starting / ready / draining / stopped / failed + reason), time to ready, endpoint, binary sha and a Logs link.
- **Parameters:**
  - basic capacity knobs only;
  - each field shows its production value and its qualified range or values;
  - a changed field is highlighted, and a "N changes vs production" pill appears;
  - the Advanced section is locked, with the reason;
  - "Reset to production" and "Save · restart required".
- **Effective configuration:** what the processes actually run with now, per setting, with its source (production / override / reservation).
- **Change history:** the audit log (when, who, old → new) with **Roll back** per entry.
- **Why:** it answers "what exactly is running" and "what did I change" without reading command lines.

### 3.5 Live stats
- **KPIs** (each with a 15-minute sparkline):
  - throughput (completed tok/s over a 1-min window);
  - TTFT p50, client side, with p95 and n;
  - queue (waiting, and running/slots);
  - draft acceptance (DSpark or MTP, plus tokens per step);
  - prefill placement (RTX offload / local / cache);
  - errors · stalls (with fallbacks).
- **Charts:**
  - throughput: aggregate plus per-request median;
  - TTFT p50/p95;
  - range selector: 5 min / 15 min / 1 h.
- **"Where the decode step goes":** ms per token per rank, stacked: attention, MoE experts, dense/shared, USB4 exchange, other. It needs engine request R16 and is hidden when the engine does not provide it.
- **Memory headroom:** per device. **Links:** USB4 p50/p99 and rate, Ethernet, RTX state handoff.
- **Recent requests:** time, route (offload RTX / local / cache hit), prompt tokens, cached, TTFT, decode tok/s, output, draft acceptance, status. Exportable as CSV.
- **Labelling rules** (SPEC §3.2):
  - every value says whether it is client-side or engine-side;
  - counter-derived throughput says "completed";
  - quantiles show n;
  - missing sources show "unavailable", never zero.

### 3.6 Logs
- Filters: process, level (events / + warnings / everything), text.
- **Live log:**
  - monospace, with timestamps;
  - parsed events are highlighted: `READY` in green, `POISONED`/errors in red, warnings in amber;
  - portal actions (lease requested/granted, checks passed) are interleaved with the process output.
- A warning banner summarises repeated warnings, for example USB4 retrains.
- **Recent failures** are persistent across restarts and each opens a redacted bundle.
- **Secrets are redacted** everywhere.

### 3.7 Settings & pairing
- **Pairing wizard (3 steps):**
  1. On the new machine, `telar-node pair` shows a one-time code and its key fingerprint.
  2. The operator types the code in the portal, compares the fingerprint, and picks the role: GPU worker or observer only.
  3. Keys are exchanged, and the machine then accepts commands only from this portal.
  - "Discovery only announces; it never grants access."
- **Trusted machines:** paired date, key suffix, access, and Revoke.
- **Secrets:** where they are stored (Windows Credential Manager / DPAPI; nodes: files 0600), Rotate, and "never shown".
- **Lab rules:** a read-only summary of GPU-turn rules, the USB4 layout requirement, the PRO reservation, the 4090 exclusion and build/hash scheduling.
- **Configuration:** file path, version, last save, validation result (including TensorMeld's parser), history, redacted export, and import with validation.

## 4. States (every screen)

| State | Treatment |
|---|---|
| Empty (first run) | Devices shows "Add the first machine": the pairing wizard, or "Add by address" (ssh). The other screens show a one-line explanation plus the next action. |
| Loading | skeleton blocks the size of the cards; no spinners over data that is already there |
| Stale | if a poll is older than 3 intervals, values are greyed and labelled "as of HH:MM:SS" |
| Offline device | card pill "Offline · last seen 4 min ago"; meters greyed; roles marked "unreachable"; actions needing it disabled with the reason |
| Unpaired device | only name/OS/accelerators/fingerprint; no actions except Pair / Ignore |
| Degraded | amber pill, plus a one-line inset saying what is wrong and what still works |
| Running | green pill; GPU-turn countdown visible on Engines and in the device role tags |
| Starting / stopping | per-process progress (starting → ready, draining → stopped) with elapsed time; Stop is still possible |
| Error | red pill with the parsed reason (e.g. `POISONED peer timeout`), and links to the log line and bundle |
| Not available | grey card, plus the blocker (component request, missing files, lab rule) |

## 5. Interactions and safety

- **Mutating actions:** start, stop, switch, save params, pair, revoke, rotate, import.
  - Each takes a confirmation that lists its effects, for example "will stop: DeepSeek pair (2 processes); will request: 2 GPU turns of up to 20 min".
  - One mutating job runs at a time, and a job panel shows its progress.
- **Parameter changes** apply on the next start, never hot. Each save is a config version that can be rolled back.
- **Keyboard:** every control is reachable by Tab, with a visible focus ring (2 px accent outline). Esc closes dialogs; Enter confirms the primary action.
- **Polling:** status every 2 s, stats every 2 s, logs streamed. The UI pauses polling in a background tab.

## 6. Design system

**Tokens** (`mockups/ds.css`, `:root` and `[data-theme=dark]`). The built UI follows `prefers-color-scheme` and offers a manual toggle.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` / `--surface` / `--surface-2` | `#f6f7f9` / `#fff` / `#f0f2f5` | `#0f1218` / `#161a22` / `#1c212b` | page / cards / insets |
| `--border` / `--border-strong` | `#e2e5ea` / `#cdd2d9` | `#272d39` / `#353c4a` | dividers / inputs |
| `--text` / `--text-2` / `--text-3` | `#141821` / `#4a5262` / `#7a8394` | `#e8ebf1` / `#aab2c0` / `#7c8596` | primary / secondary / labels |
| `--accent` | `#2f6fde` | `#5b93f0` | primary actions, selection, data series 1 |
| `--ok` / `--warn` / `--err` / `--info` (+ `-soft`) | `#1f9d61` / `#c77b0a` / `#d03b3b` / `#5b6bd6` | `#3cc483` / `#e6a23c` / `#ef6464` / `#8b97f0` | status only, never decoration |
| `--s1..--s5` | blue, teal, magenta, amber, violet | lighter variants | chart series (fixed meaning per chart) |

**Type**
- Inter or Segoe UI, 14 px base. Headings 18 / 15 / 14 px at 650 weight. KPI values 26 px with tabular numbers.
- Mono (Cascadia Mono / JetBrains Mono) 12.5 px for shas, endpoints and logs.

**Shape and spacing**
- Spacing on a 4 px grid: cards pad 16–18 px, gaps 16 px, page gutters 28 px (16 px narrow).
- Radii: 10 px cards, 8 px insets and buttons, 999 px pills.

**Components:** sidebar nav, page header, card, KPI tile (with sparkline), meter (stacked segments allowed), pill (status), tag (role/identity), button (default / primary / danger / small / disabled), segmented control, input (with "changed" state), key-value list, table, banner (info / warn), log view, SVG line chart, stacked bar, topology map.

**Charts**
- Line charts have a light grid, y-axis ticks with units, and x labels at start, middle and now.
- The legend sits in the card header; a series never changes colour between charts.
- No chart without units, and no dual axes.

**Responsive:** under 760 px the sidebar becomes a top bar, grids collapse to 1 column (KPIs to 2), low-value table columns hide (`.hide-n`), and wide tables scroll inside their card.

## 7. Accessibility
- Contrast meets WCAG AA: text ≥ 4.5:1 and UI components ≥ 3:1, in both themes. Status is never conveyed by colour alone: every pill has text.
- Charts have a text summary (`aria-label` with the last value and range) and a "view as table" toggle.
- Live regions: only error and new-warning announcements are `aria-live="polite"`; numeric updates are not announced.
- Motion: no animations except a 150 ms fade on state change; `prefers-reduced-motion` disables it.
- Targets are at least 32 px (44 px on narrow).

## 8. Build notes for Sol
- **Stack:**
  - stdlib `http.server` serving `telar/web/static/` (one `index.html`, `app.css` derived from `ds.css`, `app.js`);
  - vanilla JS with hash routing (`#/devices`, `#/stats`, …);
  - **no CDN, no npm, no build step**; the portal works offline.
- **API:** `GET /telar/api/{devices,topology,models,profiles,stats,logs,settings}` returns JSON. Mutations are `POST` with JSON plus the `X-Telar-CSRF` header; Host and Origin are checked (SPEC §2.4).
  - Each widget maps to one field in those documents.
  - Missing data comes back as `null` with a `why` string, and the UI renders "unavailable · <why>".
- **Fidelity:** the built screens must match the mockups in layout, components and states. Pixel-exact is not required, but the reviewer compares side by side at 1440 and 390 px in both themes.
- **Charts:** the same small SVG helper approach as `shell.js` (`lineChart`, `spark`), with no chart library.
- **Tests:** unittest for the API documents (schema of each), the security negative tests, and a headless screenshot smoke test that renders each route with fixtures and checks for no JS errors.

## 9. Widget → data source map (build contract)

Every widget renders from exactly one source. "Request" means the widget stays hidden or shows "unavailable · needs <id>" until that component request lands; it never shows a placeholder number.

| Screen / widget | Source (schema / endpoint) | Derivation / window | Request |
|---|---|---|---|
| Devices: OS, RAM, NICs, NVIDIA name / memory / driver | `tensormeld probe` (`schema_version 1`) | as reported | TM-R14 for UUID / PCI / index, CPU model, CUDA version |
| Devices: CPU model, disk free, AMD VRAM/GTT, ROCm, kernel, USB4 driver, layout | `telar-node doctor` / `gpu` (`/proc/cpuinfo`, `statvfs`, sysfs `mem_info_*`, ROCm package version, `uname -r`, `/sys/module/thunderbolt_stream`, `/run/usb4stream-layout.json`) | passive reads | — |
| Devices: GPU %, temperature, power | NVIDIA `nvidia-smi -i <uuid>`; AMD hwmon + `gpu_busy_percent` | 2 s poll | — (unavailable if the sysfs node is missing) |
| Devices: roles, GPU-turn countdown | telar lease state (`run/<lease>.json`, deadline from the lease spec) | countdown = deadline − server time; never derived from browser polling | — |
| Topology: Ethernet bandwidth / RTT | `tensormeld link-probe` receipt (`link-probe-v1`: `rtt_us_p50`/`p99` at the stated payload, `bulk.bytes_per_s`) | dated receipt, labelled RTT | — |
| Topology: USB4 cable state / warnings | `usb4stream-describe` JSON (`version`, `topology`) | current state only | R11 (versioned contract) |
| Topology / Stats: USB4 per-cable rate and latency, retrain history | — | — | **R18** (engine/driver per-stream counters and latency histogram) |
| Stats: USB4 exchange mean | engine `/api/status` exchange totals (`deepseek41_serve_main.cpp` exchange counters) | mean over window | — |
| Models: presence / verified | telar asset manifest + `telar-node verify-model` | sha256 or size-only | — |
| Models: proposals | telar **profile adapter** over `plan-hetero` output (SPEC §13.5) | qualified profiles only; receipt numbers labelled "receipt", model estimates labelled "estimate" | R17, TM-R15 |
| Engines: processes, state, ready-after, sha | `supervise` events (READY / POISONED / STOPPED, start time, exe sha) | — | — |
| Engines: effective configuration | telar rendered argv/env + preset provenance today; component `/config` later | — | E-C2/E-C3 |
| Engines: change history / rollback | telar config versions (audit log) | — | — |
| Stats: throughput | engine `/metrics` `strix_generated_tokens_total` | rate over 10 s windows, reset-aware; "completed" | — |
| Stats: TTFT | engine `/api/status` recent requests `queue_ms + prefill_ms` | p50/p95 over the last N=50 requests, with n shown; "engine-side" | client TTFT only from bench receipts; router first-byte via R7 (labelled "router first byte") |
| Stats: queue | engine `/metrics` `strix_queue_{waiting,running}`; router `/api/status` `waiting` | latest | — |
| Stats: draft acceptance | engine `/api/status` MTP / DSpark counters | accepted ÷ proposed over the window | — |
| Stats: prefill placement | router `/api/status` `prefill.placements` | delta over the window | — |
| Stats: step breakdown | engine `/api/status` step summary | critical-path accounting per step | **R16 / E-C5** |
| Stats: memory headroom | DeepSeek `/api/status` device free/total; AMD sysfs; `nvidia-smi` | latest | — |
| Stats: recent requests (route column) | engine `/api/status` recent (completed only) + router request correlation | join by request id | **R7** (router request log with ids); until then the route column is hidden |
| Logs | `supervise` captured stdout/stderr + telar actions | parsed events | — |
| Settings: trusted machines | telar pinned host keys (MVP); agent enrollments later | — | TM-R10..R12 |
| Settings: secrets | telar DPAPI store (ctypes `CryptProtectData`) / node 0600 files | — | — |

## 10. Capability matrix by phase (what the built UI enables)

| Capability | 1b (MVP UI) | 2 / 2.5 | 4 |
|---|---|---|---|
| Add machine by address + host-key pin | ✓ | ✓ | ✓ |
| Discover / pair / revoke enrollment | shown as "later phase", disabled | disabled | ✓ (TM-R10..R13) |
| Disconnect machine (uninstall + unpin key) | ✓ | ✓ | ✓ |
| Start / stop / switch profile | Qwen replicas | + DeepSeek pair, + RTX prefill (3a) | + split decode (R14) |
| Basic parameters + audit / rollback | ✓ (file + restart) | ✓ | config-file path per component (E-C*, TM-C*) |
| Topology map + Ethernet link-probe | list only | ✓ | ✓ |
| USB4 per-cable telemetry | hidden | hidden until R18 | ✓ |
| Step breakdown | hidden | until R16 | ✓ |
| Placement proposals | — | 2.5 | ✓ |
| Rotate keys | engine keys (restart) | ✓ | + agent certs |

Disabled controls always name their blocker (phase or request id).

## 11. Additional rules from the second review

- **Rollback:** only `telar` configuration versions roll back. Reservations, GPU-turn approvals, pairings and secrets are not configuration and cannot be rolled back. Every restore is revalidated against current permissions, assets, qualification and leases. **Stop and cancel never wait behind a confirmation dialog or a running job.**
- **Accessibility (adds to §7):**
  - contrast tokens meet AA (light `--text-3 #646c7b`, status colours darkened; dark primary buttons use dark text);
  - native labelled controls (`<label for>`, radio groups for segmented controls);
  - the menu button has an accessible name;
  - dialogs have a name, trap focus and return it on close;
  - errors are associated with their fields (`aria-describedby`);
  - Enter never confirms a destructive action;
  - the log view is keyboard-scrollable.
  - Tests: automated contrast check of the tokens, keyboard-only walkthrough of every mutation, and a screen-reader smoke pass (NVDA) before 1b is accepted.
- **Narrow:**
  - the built UI is tested in a real 390 px viewport (DevTools device emulation in the headless test). The mockup capture crops a forced 390 px layout because headless Chrome has a minimum window width;
  - Add by address and failure actions stay visible on narrow;
  - Topology becomes the link list (the map is hidden);
  - logs wrap and scroll.
- **Stale / resume:** after a background tab resumes, or on reconnect, the UI fetches authoritative state before it re-enables any mutation. Lease expiry is enforced server-side by `supervise`, independently of the browser.
- **Sample data coherence:** mockup fixtures come from one configuration (context 32768 for the RTX profile, so the 8K/32K requests fit). Identity is shown as the qualified identity ("P17.5 · trellis quality-gated · RTX prefill fast path"), never as a blanket "exact".
