# SPEC — `hyperloom`: one installable product that weaves TensorMeld, the Strix Halo engine and a discrete-GPU front into one cluster

Status: **FINAL for main's OK** (2026-10-23; sections 13-14 added after the 03:09 crash for the user's addenda A and B; second Sol review in SPEC-review-sol-2.md, answered in section 15).
- Author: the product design session (Opus).
- History: `SPEC-draft.md` was reviewed by Sol (high) in `SPEC-review-sol.md`. Every review point is answered in §12. The spec body below already includes the accepted changes.
- Language: English. Repo: **private** `hyukudan/hyperloom`.

## 0. Goal, scope, non-goals

**Goal.** One private repository that pulls in pinned versions of TensorMeld, the Strix Halo inference engine and strix-trellis. It builds them, installs them and drives them. It gives:

- **Windows (the PC with the RTX PRO 6000):** a front end to configure, install, start/stop, set parameters and see statistics. The PC runs the router and, in Phase 3, the RTX prefill service.
- **Strix Halo nodes (Linux):** a node package (`hyperloom-node`) that builds and installs the engine and runs it safely inside the lab's GPU-turn rules.

Always **the simplest way that works**.

**This repo is the integration and control layer.** TensorMeld, the engine, strix-trellis and the USB4 driver stay in their own repos. They are pinned by commit and driven only through the interfaces they already have: CLI flags, environment variables, config files, stdout/stderr events, and the HTTP `/health`, `/metrics` and `/api/status` endpoints.

- No component code is copied or forked here. That includes TensorMeld's supervisor, the engine's HTTP layer and the lab harness.
- What is missing from a component is a **component request** (§10).
- Where a phase depends on such a request, the request is a **phase prerequisite**, not something `hyperloom` works around.

**Non-goals**
- No new inference code, kernels or numerics.
- No change to any production default.
- No kernel, driver or USB4-layout management.
- No automatic model downloads or conversions.
- No installer framework (MSI, NSIS, Electron, Qt).
- No campaign/benchmark framework of our own: qualification reuses `tensormeld bench-protocol` and the lab's tmtest externally.

## 1. Inventory of what exists (read from the real code, 2026-10-23)

### 1.1 TensorMeld (`hyukudan/tensormeld`, public; pin `7165e98`, 0.2.0a2)

Two pins, kept apart on purpose: `versions.lock` (`7165e98`) is the TensorMeld every node release installs and that node-hosted routers run, so it is part of every release id; the `third_party/tensormeld` submodule (`409003a`, public main with the prefill `routes` policy) is the copy the local router and the router-config parser run on the control machine. Moving the node pin changes every release id and needs a reinstall and requalification; moving the submodule does not.

**Package**
- Pure Python ≥ 3.11 with **zero runtime dependencies**. Building it needs `setuptools>=68`.
- One console script, `tensormeld` (also `python -m tensormeld`). It runs on Windows and Linux. Tests: about 800 stdlib unittest tests; CI covers Ubuntu and Windows.

**`serve-replicas CONFIG`: the router**
- Config schema `tensormeld/replica-router-v1` (`replica_router.py::RouterConfig.parse`).
- Strict parsing: unknown or duplicate keys rejected, 256 KiB maximum.
- Listen and replica addresses must be **numeric loopback or private IPs**, never hostnames.
- A non-loopback `listen` requires a **top-level** `api_key_file`.
- Every replica needs `max_inflight` (1..64). Its optional `api_key_file` is read **on the router's host**.
- `prefill.policy auto` requires `node_profile`, even when there are no services.

**Router endpoints**
- `POST /v1/chat/completions`, `/v1/completions`, `/v1/messages`, relayed unchanged including SSE.
- `/tokenize`, `/detokenize`, `GET /v1/models`, `GET /health`.
- `GET /api/status` (`tensormeld/replica-router-status-v1`): queue `waiting`, `rejected`, and per replica state/inflight/assigned/completed/failed/last_health_ms/last_error, plus `prefill` placements when services are configured.
- `GET /metrics`: router and per-replica counters. It does **not** include prefill placements; those are only in the JSON.
- Health, status and metrics need no authentication.
- `X-TensorMeld-Replica` appears on relayed responses; `X-TensorMeld-Prefill` only when a prefill decision was made.
- **No tok/s, TTFT or latency figures.**

**Other behaviour**
- A request is retried on another replica only if no byte had been sent yet.
- Offload to an RTX prefill service is `POST /v1/prefill {tokens, handoff, session}`. **It sends neither the decode key nor `reserve_tokens`.**
- **Shutdown:** `cli.py::_serve_replicas` waits for SIGINT/SIGTERM handlers. On Windows `terminate()` is not a catchable SIGTERM (see §2.5).

**Other commands used**
- `bench-replicas` / `bench-protocol`: protocol-bench needs `--execute`, rejects cache hits, and its waves must be complete and valid.
- `plan-hetero`: advisory only.
- `deploy-pipeline`: **not used** before its real-engine qualification. Its `build_argv` forms do not match the CUDA FRONT's flags and cannot express DSpark or `--prefill-path device`; it has no GPU-turn integration.

**Not present:** no UI, no systemd use, no node install logic.

### 1.2 Engine (`hyukudan/strix-halo-engine-lab`, public)

**Refs at inspection**

| Ref | Commit | Contents |
|---|---|---|
| `origin/main` | `5592b48e` | Qwen integration (PR #40) and DeepSeek P10–P15 (PR #41) |
| `publish/main-20261023` | `bfddf9c1` | local, unpushed; contains `integrate/ds41` (`2b8ad5e3`: P16–P17.5, production defaults), `integrate/qwen-next`, the hetero branches and `publish/rtx-reconcile` (an ancestor), plus the public redactions |

- **Pins are immutable SHAs reachable on GitHub.** Phase 1 pins a commit of `origin/main`. Phase 2+ pins the commit on `main` produced by the publish merge, after it is pushed. Unpushed commits are never pinned.
- A dated **capability matrix** (`versions.lock`) records per pin which apps and flags exist, and the campaign receipt the pin is qualified against.

**Build**
- CMake ≥ 3.24, Ninja, C++20. No presets, no build script, no submodules.
- **HIP:**
  - ROCm core 10.1 (`/opt/rocm/core-10.1`, `amdclang++`); `gfx1151` hard-set; `-DSTRIX_ENABLE_HIP_RUNTIME=ON`.
  - The **Qwen production preset needs `-DSTRIX_ENABLE_EXPERIMENTAL_Q4K_A8_RUNTIME=ON`**, because it uses `--q4k-a8g32-gate-up`, and the default is OFF.
  - Release build type.
- **CUDA (publish line):** `-DSTRIX_CUDA_DS41_ONLY=ON -DCMAKE_CUDA_ARCHITECTURES=120` with MSVC (flags handled by CMake).
- **strix-trellis:** a prebuilt static archive plus header through `-DSTRIX_DS41_TRELLIS=ON -DSTRIX_DS41_TRELLIS_API=v12 -DSTRIX_DS41_TRELLIS_LIBRARY=… -DSTRIX_DS41_TRELLIS_INCLUDE_DIR=…`.

**Qwen server: `strix-hip-decode-token --http-serve`**
- Prints **`strix serve: listening on ADDR:PORT model …` on stderr**.
- **It has no `on_drain`, so `POST /admin/drain` is not served**; only DeepSeek's serve sets it.
- The production replica line is about 98 flags plus 13 `STRIX_*` variables, assembled by lab scripts (tmtest `qwen_cmd.sh`, `campaign/qwen/rep/qserve.py`).
- **Startup assets it needs:**
  - the merged model GGUF;
  - the MTP sidecar (SHA/size pinned);
  - **calibration prompt token IDs**;
  - a **SHA-pinned MTP golden file**;
  - the **draft-vocab directory** (`--mtp-draft-vocab auto:<dir>`);
  - the converter/source identity pins.
- Reference receipt: `campaign/qwen/rep/runs/r3/prod-c1.cmd` (its default max tokens is **256**). Its prompt IDs are elided in the `.cmd`, so it is not executable as is.

**DeepSeek server: `strix-deepseek41-serve`**
- Usage: `MODEL --rank 0|1 --transport …`, or `--group-manifest`.
- **Production contract** (`docs/ds41-production-defaults.md` on `integrate/ds41` and the publish line):
  - `--transport usb4gpu` (manifest `usb4_gpu_auto`), which falls back to the host path in layout 1;
  - `--prefill-path device --dspark <DSpark.gguf> --dspark-vocab auto:<vocab>`;
  - identity env `STRIX_DS41_OVERLAY`, `CB4=1`, `TRELLIS=<K4>`, `TRELLIS_MIX=head=<K5>`, `TRELLIS_SITES=shexp,head`, `TRELLIS_HEADOUT=1`;
  - every other production default is ON when unset.
- JSON events on **stdout**, rank-tagged: `READY`, `POISONED` (exit 2), `STOPPED`. SIGTERM drains; `/admin/drain` is supported.
- **Rank hello:** it fingerprints the executable SHA, the base-model **size**, the configuration and the serve identity. It does **not** hash the model bytes.
- `/admin/import-state` authenticates, and rejects states that would fill the context.

**Shared HTTP layer (`src/serve/api.cpp`)**
- Generation (authenticated with the key file): `/v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/models`, `/tokenize`.
- **Public:** `/health` (`ok|failed|draining`), `/metrics`, `/api/status` (`strix-serve-status-v1`).
- `/metrics` counts finished-request tokens, queue and busy seconds.
- `/api/status` lists recent completed requests with `queue_ms`, `prefill_ms`, `decode_ms`, `prefill_tok_s` and `decode_tok_s`. DeepSeek adds slots, pool, nested device memory and `trellis_identity`.

**CUDA apps (publish line)**
- **`strix-cuda-ds41-prefill-service`:**
  - `/health` is public. `/v1/describe` is authenticated.
  - `POST /v1/prefill` takes `handoff` and `reserve_tokens`. The handoff URL is supplied by the caller.
  - Keys are passed as **literal `--api-key` / `--handoff-api-key` arguments**.
  - It has no `/metrics` and no `/api/status`.
- **`strix-cuda-ds41-pipeline-front`:** a finite experiment driver (`--stage --cuts --token-file`, prompt/state files). It has **no HTTP server and no `/health`**. The HIP/host `strix-deepseek41-pipeline-front` does have `--http`.

### 1.3 strix-trellis (`hyukudan/strix-trellis`, public, Apache-2.0, v0.3.0)
- CMake with `STRIX_TRELLIS_ENABLE_HIP`; ROCm discovery follows its README.
- Produces `libstrix_trellis.a` and the `strix_trellis.h` C ABI. The archive is linked into HIP binaries, so runtime ROCm shared libraries are still needed.

### 1.4 USB4 driver (`hyukudan/strix-halo-usb4-driver`, public, GPL-2.0)
- **What it provides:**
  - a 7.3-rc5 patch series with the `thunderbolt_stream` module, `/dev/tbstream*` and configfs stream settings;
  - **`usb4stream-describe`**: with no arguments it prints a **JSON document** (`version`, `topology`: cable state, openability, warnings); `--summary` prints text; there is no `--json` flag.
- **Nodes today:** custom unsigned kernels with Secure Boot off (`…-20261020poll` is the GRUB default; `…-20261022lat` was booted one-shot).
- **Layouts:** layout 1 (four host streams) is production. 2b and 2bq are applied per pair-turn label by the main session's root watcher v3.
- **Product decision:** a **documented prerequisite with a read-only check**. The product never builds, installs or configures the kernel, the driver, the configfs settings or the layout.

### 1.5 Models and assets (paths configured per node; never moved by the product)

| Profile | Assets (each with an expected size + sha256 in a trusted manifest) |
|---|---|
| qwen-replicas | merged Qwen3.8-Flash-Next UD-Q4_K_XL GGUF (about 111 GB; a pinned public Hugging Face repo + revision (recorded in the local asset manifest), shards merged); distilled MTP sidecar `…distillG-Q8_0.gguf` (2.79 GB, local); calibration token-ID file; MTP golden; draft-vocab dir |
| ds41-pair | `DeepSeek-V4.1-Flash-1017-Q2.gguf` (365.7 GB; a pinned public Hugging Face repo + revision (recorded in the local asset manifest)); `…-DSpark-MXFP4.gguf`; DSpark vocab; overlay `c-u6.gguf`; trellis packs `ours-K4`, `ours-K5` (with their own sha manifests) |
| ds41-rtx (PC) | PC copy of the Q2 GGUF (and any PC-side pack the pinned profile names) |

- Qwen (about 107 GiB) and DeepSeek (about 80 GiB per node) **never share a node**. Profiles are mutually exclusive per node.
- **No asset is generated silently.** A missing golden or vocab is a doctor failure that points to the lab procedure that produces it.

### 1.6 How the cluster is operated today

**GPU-turn queue**
- Every node GPU process runs under `<lab>/gpu-turn.sh <label> <cmd>`. It holds a node flock on fd 9 while the command runs.
- **The live copies on the nodes cap single turns at 20 min** (timeout, then KILL). `GPU_TURN_MAX` is allowed only by request for a final measurement round. The copies in `the lab workspace` are older and lack this.
- `gpu-turn-pair.sh` runs on node 1. It holds node 1's lock and node 2's lock, the latter over a live ssh stdin channel.
- Pair scripts wait until both nodes' `.gpu-turn/layout-state` matches their label.

**Qwen replicas**
- One server per node on its LAN IP, port 18501, with a key file. Started with `setsid`/`nohup`.
- The router runs **on node 1, on loopback, port 18600**, with `balance_active_decodes=true`, `prefill.policy auto` and `block_tokens 1024`. **It has never run on the PC.**

**DeepSeek pair**
- `p9-pair.sh` starts rank 1 over ssh and rank 0 locally, then waits for "connected"; HTTP on 18431.

**RTX front**
- Built with MSVC 19.44, CUDA 13.0.88, CMake and Ninja `-j8`.
- The prefill service runs on 8095.
- Always `CUDA_DEVICE_ORDER=PCI_BUS_ID` + `CUDA_VISIBLE_DEVICES=<PRO index or UUID>`.
- Lab **PC reservations** are owner-approved, bounded windows with a memory watchdog (`hetero/w2/e2e/pin5/watchdog.ps1`).

**Network and binaries**
- PC ↔ nodes: 1 GbE ssh with keys (the PC → node path measured about 12 MB/s). Node ↔ node: LAN at about 115 MB/s, and USB4 for the pair.
- Binaries live in per-agent scratch directories. There is no service management.

**What the product automates:**
- pinned builds;
- one install root per node;
- rendering every component config from one file;
- start/stop with lease ownership and readiness;
- the router on the PC;
- stats;
- logs.

**What stays manual:** kernel/driver/layout, ROCm/CUDA/MSVC installation, ssh keys, model downloads and conversions, and lab approvals (GPU-turn extensions, PC reservations, build windows).

## 2. Architecture

```
Windows PC                                          Strix Halo node 1 / node 2
+-------------------------------------------+       +------------------------------------------+
| hyperloom  (Python stdlib; CLI first, thin UI)  |  ssh  | hyperloom-node (python3 stdlib, single file)   |
|  cluster.json  %APPDATA%\hyperloom\             |------>|  doctor | install | activate | lease ... |
|  router child: tensormeld serve-replicas  | 1GbE  |  lease = gpu-turn.sh <label>             |
|     (in a Windows Job Object)             |       |          hyperloom-node supervise <spec.json>  |
|  pollers: router, engines, nodes          |<------|  engines bind node LAN IP (+ key file)   |
|  UI http://127.0.0.1:18700 (Phase 1b)     | HTTP  |                                          |
+-------------------------------------------+       +------------------------------------------+
```

### 2.1 Repository layout
```
hyperloom/
  README.md  SECURITY.md  LICENSE (proprietary, private for now)  NOTICE (component licenses)
  versions.lock                # pins (SHA), capability matrix, toolchain versions, qualifying receipts
  third_party/{tensormeld,engine,strix-trellis}   # git submodules, read-only here
  hyperloom/            config.py render.py remote.py lease.py stats.py adapters/ web/ presets/
  node/           hyperloom-node   build-node.sh
  windows/        install.ps1  hyperloom.cmd  build-cuda-front.ps1 (Phase 3)
  tests/          unittest; fake engines that REPLAY real captured outputs; fake ssh
  docs/           install, prerequisites, auth matrix, lab mode, component requests
```

### 2.2 Components: git submodules at immutable SHAs
- **Why submodules:** all three repos are public, the nodes need source, and a gitlink is one reviewable SHA. No release artifacts exist yet.
- **`versions.lock`** has one row per component: SHA, the date the row was checked, the capability flags, and the receipt the pin was qualified against (campaign receipt + binary sha).
- **Nodes fetch by SHA:** `git init; git fetch --depth 1 origin <sha>; git checkout FETCH_HEAD`. GitHub serves reachable SHAs.
- **Offline fallback:** `hyperloom dist` exports **each submodule** explicitly (`git -C third_party/<c> archive <sha>`), not just the parent repo.
- Pin changes are ordinary reviewed commits; there is no `hyperloom pin` automation in the MVP.

### 2.3 One versioned config file: `%APPDATA%\hyperloom\cluster.json` (schema `hyperloom/cluster-v1`)
- Strict validation, TensorMeld style. It **never contains secrets**, only key-file paths.
- It keeps **HTTP addresses** (numeric private IPs) separate from **ssh targets** (aliases or user@host), and **PC-local key paths** separate from **node-local key paths**.

```jsonc
{ "schema": "hyperloom/cluster-v1",
  "pc": { "router_listen": "127.0.0.1:18600", "ui_listen": "127.0.0.1:18700",
          "cuda": { "pro_uuid": "GPU-…", "reservation": null } },
  "nodes": [ { "id": "node1", "http_address": "<private IP>", "ssh_target": "<alias>",
               "install_root": "/home/<u>/hyperloom", "lab_mode": true }, { "id": "node2", … } ],
  "assets": { "<node id>": { "<asset name>": { "path": "…", "bytes": 0, "sha256": "…" } } },
  "profiles": { "qwen-replicas": { "nodes": ["node1","node2"], "port": 18501,
                                   "receipt": "qwen-rep-r3", "overrides": {} },
                "ds41-pair": { "rank0": "node1", "rank1": "node2", "port": 18431,
                               "receipt": "<P17.5 receipt id>", "overrides": {} } },
  "router": { "policy": "auto", "balance_active_decodes": true, "max_inflight": 8 } }
```

**`render.py`** is pure, golden-tested and versioned. From `cluster.json` and the selected preset it produces:
- the TensorMeld router JSON, **validated by importing TensorMeld's own `RouterConfig.parse`**;
- per node: a tokenized **argv list** and an **explicit env map** for every process. The env is built from `env -i` plus a sanitized base; loader/interpreter variables (`LD_*`, `PYTHON*`, …) and reserved device/transport/identity variables are always rejected;
- the lease spec JSON (§2.6).

**Keys**
- Generated by `hyperloom` (32 random bytes, hex) into `%APPDATA%\hyperloom\secrets\` with a user-only ACL.
- Pushed to the nodes as `0600` files over ssh stdin.
- Never placed on a command line, except where a component accepts only literal arguments (CUDA service; see §6 and R12).

### 2.4 Front end: CLI first, then a thin local web UI (stdlib `http.server`)
- **Phase 1a is the `hyperloom` CLI**: `doctor`, `install`, `start`, `status`, `stop`, `logs`. Phase 1b adds `hyperloom ui`: one static page with vanilla JS, a thin layer over the same functions.
- **Why a local web UI:** Python is already required, it adds no dependencies and no build step, and it is the same code on Linux for tests. It stays **outside** the TensorMeld process, so no product code goes into TensorMeld.
- **Control-plane security (required before any mutating UI action):**
  - listen on 127.0.0.1 only;
  - accept **only** `Host: 127.0.0.1:<port>` / `localhost:<port>`, which defeats DNS rebinding;
  - GET is read-only;
  - mutations are `POST` with `Content-Type: application/json`, a custom header `X-Hyperloom-CSRF: <per-launch token>`, and an `Origin` that must equal the UI origin;
  - no CORS headers; bounded body size; one mutating job at a time;
  - negative tests cover a foreign Origin, a rebinding Host, a missing token and malformed JSON.
- **Pages:**
  - **Setup:** nodes, ssh test, assets with a verify button.
  - **Install:** doctor, then build/install with the log streamed.
  - **Run:** profile start/stop, lease state with time left, per-process state.
  - **Parameters:** basic knobs only in the MVP.
  - **Stats.**
  - **Logs:** tail, plus a redacted bundle.

### 2.5 PC processes: the router child and lifecycle on Windows
- **Launch:** `hyperloom` runs the router as `<venv abs path>\python.exe -m tensormeld serve-replicas <rendered.json>` with `CREATE_NEW_PROCESS_GROUP`. stdout and stderr are drained continuously by threads into bounded log files.
- **Crash cleanup:** the child is assigned to a **Windows Job Object** (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, via ctypes), so it dies if `hyperloom` crashes. The same applies to CUDA children in Phase 3.
- **Single instance:** a lock file plus a port check prevents duplicate controllers or routers.
- **Graceful stop:**
  - first stop admitting: the router is stopped *before* engines, and engines are drained or stopped afterwards;
  - then `CTRL_BREAK_EVENT` with a grace period, then Job termination;
  - the router is stateless, so `hyperloom` takes the final `/api/status` snapshot itself before stopping it;
  - TensorMeld handling SIGBREAK is requested (R8) but is not needed.
- **Acceptance covers:** closing the UI window, killing the `hyperloom` process (no orphan router), restart, and port reuse.

### 2.6 Node side: `hyperloom-node`, leases and the supervisor

**Install root**
- Dedicated and marked: `<root>/.hyperloom-root` holds a random id.
- Refused if the canonical path (symlinks resolved) is or overlaps `$HOME`, `<lab>`, any repository, or any configured asset path.
- Uninstall deletes only inside a marked root, never an active release, and never assets.

```
<root>/bin/hyperloom-node   src/<component>-<sha>/   releases/<release-id>/{bin,lib,include,MANIFEST.json}
<root>/etc/ (keys 0600, rendered specs)   run/ (lease state)   log/ (rotated, keep 20)
```

- **`release-id`** = sha256 of `MANIFEST.json`. The manifest covers component SHAs, the full configure recipe, toolchain versions (amdclang, ROCm package version, cmake, ninja), the preset versions, the `hyperloom-node` version, and the sha256 of every binary.
- **Launches use the immutable `releases/<id>/bin/...` path**, never a `current` symlink. Activation therefore cannot change a running process.

**Bootstrap** (fixed commands, no free-form shell)
- `ssh -T -o BatchMode=yes -o ConnectTimeout=10 -- <target> 'mkdir -p -- "$HOME/.hyperloom-boot"'`, then `scp` of `hyperloom-node`.
- After that, every call is `ssh … -- <target> '<absolute path>/.hyperloom-boot/hyperloom-node <verb>'`, with a **fixed verb** from an allow-list and **arguments sent as bounded UTF-8 JSON on stdin**.
- The remote home is expanded once per process and node (at bootstrap, or by one read-only `printf` lookup before the first call); the absolute path is used afterwards, one ssh session per call.
- `ssh_target` is validated: no leading `-`, no whitespace or control characters.
- Host-key, scp and `ssh.exe` prerequisites are checked by `hyperloom doctor`.
- Tested against a real OpenSSH server in a local test VM/container (Windows `ssh.exe` client) with spaces, quotes, Unicode, EOF and cancellation, plus fake ssh for unit tests.

**Build**
- **`build-node.sh`** is a fixed recipe per pin, taken from `versions.lock`:
  - trellis (Phase 2) with HIP and the ROCm discovery variables from its README;
  - the engine with `-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_HIP_ARCHITECTURES=gfx1151 -DCMAKE_HIP_COMPILER=/opt/rocm/core-10.1/bin/amdclang++ -DSTRIX_ENABLE_HIP_RUNTIME=ON -DSTRIX_ENABLE_EXPERIMENTAL_Q4K_A8_RUNTIME=ON`, plus the trellis options for DeepSeek;
  - only the needed targets, and the engine's host-only test targets, which are built and then run with a named ctest `-L`/`-R` subset recorded in the lock.
- **Capability check** before a release can be activated: `--help` / `--describe-json` show the flags and apps the preset needs.
- **Resource envelope:** the whole build job (configure, build, tests) runs under one `systemd-run --user --scope -p MemoryMax=16G nice -n 19` when `systemd-run --user` works, or `nice` plus `ulimit -v` otherwise.
- **Lab mode:** builds and full-model hashing run only when no pair or measurement turn is active, or in a window main approves. They are refused while a pair turn is held.
- The build does not touch the GPU, but it uses CPU and page cache, so it is scheduled, not assumed harmless.

**Two-node release copy and activation (transactional)**
1. Build on node 1.
2. Copy the release to node 2 over the node-to-node LAN (rsync over the existing node ↔ node ssh), or relay through the PC.
3. Verify every binary sha on node 2 against the manifest.
4. `activate` happens **only when the profile is stopped on both nodes**. Both are activated, or both are rolled back. A pair always launches the same `release-id` on both ranks; `hyperloom` checks this before starting.

**Leases (lab mode; the only way a GPU process starts)**
- **`hyperloom-node lease <spec>`** runs `setsid <gpu-turn.sh or gpu-turn-pair.sh> <label> <root>/bin/hyperloom-node supervise <spec.json>`. The lease is therefore exactly a GPU turn. The flock (and, for pairs, the peer lock with its ssh stdin channel) is held by the turn scripts for as long as `supervise` lives.
- **Labels:** `hl-<profile>-<short id>`, from a namespace that main approves and that **must not match any watcher v3 mapping**. The UI does not offer free-form labels.
- **`supervise` is the single foreground owner of the lease.** It:
  1. re-checks preconditions **after** acquiring the turn: doctor-lite, and the expected layout on both nodes (`layout-state` = `1` and `/run/usb4stream-layout.json` agree) for ds41 profiles. On a mismatch it aborts with no start;
  2. starts the engines in their own process groups. It records pid, start time and exe sha, and captures stdout and stderr separately;
  3. waits for readiness through **per-binary adapters**: DeepSeek JSON `READY` with rank and `/health`; Qwen stderr `strix serve: listening` and `/health` 200. It times out with full cleanup on partial starts, for example rank 1 up and rank 0 failing;
  4. enforces the **deadline**: lease end minus a drain margin. The default is the live single-turn cap (1200 s) minus 60 s, and there is no general extension setting. A longer lease needs an explicit `--approved-lease-s N --approval "<coord line ref>"`, which is logged; it is for main-approved sessions only. Pair turns use the same deadline field; the pair script's own limits are recorded per live-script identity;
  5. stops on request, deadline, engine death or controller loss:
     - DeepSeek: `/admin/drain`, then SIGTERM;
     - Qwen: SIGTERM, after the router has stopped sending to it (no HTTP drain; R2);
     - then SIGKILL of the process group after a timeout.
     It reports `STOPPED` with the reason, then exits, which releases the turn.
- **No detached GPU children ever outlive `supervise`.** Single-node turns are never nested inside a pair turn.
- **Before a lease is offered**, `hyperloom-node doctor` records the **live gpu-turn script identity** (sha256 of the live scripts) and the capability notes approved for that identity.
- **Controller loss:** `supervise` does not depend on the PC's ssh session. `hyperloom` reattaches by reading `run/<lease>.json`.
- **Stop while queued:** `hyperloom-node cancel <lease>` kills the waiting turn-script process group. Nothing has started at that point, so nothing leaks.

**Optional non-lab mode (Phase 4):** `systemd --user` units wrapping the same `supervise` with no turn and no deadline. Off by default.

### 2.7 Network and auth matrix

| Endpoint | Bind | Auth |
|---|---|---|
| UI | PC 127.0.0.1:18700 | Host/Origin/CSRF (§2.4) |
| Router | PC 127.0.0.1:18600 | none needed on loopback; a top-level `api_key_file` is required if bound to a LAN IP (not in the MVP) |
| Qwen / DeepSeek engines | node `http_address` (private) | key file for generation and admin. `/health`, `/metrics` and `/api/status` are public: on the trusted LAN only, documented. |
| RTX prefill service | PC 127.0.0.1 only (the router is local) | `--api-key` literal (R12). Handoff URL fixed by `hyperloom` to rank 0; `--handoff-api-key` = rank 0's key |

- Traffic is plain HTTP on a private LAN; this is documented as an accepted risk.
- Bundles and logs redact every key file and literal key argument.

## 3. Parameters and statistics

### 3.1 Presets and knobs
- **A preset** is a data file per profile and engine pin, derived from **one named campaign receipt plus the engine SHA**: Qwen r3 `prod-c1` for the replicas; for the pair, the pin's `ds41-production-defaults.md` plus its P17.5 receipt.
- **Rendering a preset with no overrides reproduces that receipt's argv and explicit env exactly** (tokenized comparison; only configured paths, IPs and ports abstracted). For example Qwen `--http-default-max-tokens 256` and DeepSeek `--transport usb4gpu`.
- Any friendlier default is a **named, visible override**, not a silent change.
- Presets **translate data only**. They do not invent flags. New or changed knobs come from the component first.

**Knobs in the MVP UI:** capacity, queue and ports only.

| Profile | Knobs |
|---|---|
| Qwen | `--http-max-queue`, `--http-max-connections`, `--http-default-max-tokens`, `--http-port`; `--context` only within the values the receipt qualifies |
| DeepSeek | `--default-max-tokens`, `--peer-timeout`, port; `--slots` and `--context` only within qualified values |
| Router | `max_inflight`, `limits`, `policy nodes\|auto` |

**Not in the MVP:**
- Algorithm switches (DSpark on/off, cohort rows, draft scoring/cache, prefix-row settings, scheduler variables). They are added later **per pin**, each with a qualification record and range/dependency validation.
- Raw env. Numerics, identity, transport and layout are never editable.
- Diagnostics such as `STRIX_DS41_STEP_TRACE` are a later "diagnostic run" mode, clearly labelled non-production, with output into `log/`.

### 3.2 Statistics (per-component adapters, versioned)

| Source | Data | Notes |
|---|---|---|
| Router `/api/status` (`replica-router-status-v1`) | queue `waiting`, rejected, per replica up/inflight/assigned/completed/failed/last health, prefill placements (JSON only) | the adapter checks the schema id |
| Router `/metrics` | counters | |
| Engine `/metrics` | completed requests, prompt, cached and generated token totals; running/waiting; busy seconds | shown as **"completed tok/s over the sampling window"**, not live decode. Counter resets and restarts are detected (decrease = reset). |
| Engine `/api/status` (`strix-serve-status-v1`) | recent completed requests: queue, prefill and decode ms, prefill/decode tok/s; DeepSeek slots, pool, nested device memory, trellis identity | deduplicated by request id; quantiles over the last N=50 requests or 10 min, shown with the sample count. **"Engine-side TTFT (queue+prefill)"** is labelled as such. |
| Prefill service `/health` (+ `/v1/describe` with key) | state and describe | no metrics. Unsupported fields show as "unavailable". |
| `hyperloom-node gpu` | VRAM/GTT used (`/sys/class/drm/card*/device/mem_info_*`), temperature and power (hwmon), RAM, load, `uname -r` | passive sysfs reads only, which do not initialize the GPU |
| `hyperloom-node doctor` | `usb4stream-describe` JSON (version, topology) plus the layout file | read-only |
| PC | `nvidia-smi --query-gpu … -i <PRO uuid>` | Phase 3 |
| Bench | `tensormeld bench-protocol --execute` receipts | **client** TTFT and throughput; never mixed with engine-side figures |

- Stats live in in-memory ring buffers. Only bench receipts are saved.
- Missing stats become component requests (R3, R4, R7).

## 4. Build and install

| Artifact | Built where | When |
|---|---|---|
| `hyperloom` + TensorMeld | PC venv `pip install` (needs setuptools, from PyPI or a bundled wheel) | install |
| strix-trellis `.a` | node 1 | node install (Phase 2) |
| engine HIP apps | node 1, copied to node 2 with sha verification | node install |
| engine CUDA apps | PC `build-cuda-front.ps1` | Phase 3, in a granted PC reservation |

**Windows `install.ps1`**
1. Resolve **one** interpreter: `py -3.13` / `-3.12` / `-3.11`, or `python` ≥ 3.11, and record its absolute path.
2. Check `git` and `ssh.exe`.
3. Create the venv at `$env:LOCALAPPDATA\hyperloom\venv` using PowerShell path APIs.
4. Use `<venv>\Scripts\python.exe -m pip install` for `third_party\tensormeld` and `.`. Network access to PyPI is needed for setuptools unless `wheels\` is present (the offline `hyperloom dist` bundles setuptools and wheel wheels).
5. Create a Start-menu shortcut that runs `<venv>\Scripts\python.exe -m hyperloom ui`, plus `hyperloom.cmd`.

Tested with paths containing spaces, on Python 3.11, 3.12 and 3.13.

**Node doctor**
- Read-only; every command it runs is listed in the output.

| Check | When required |
|---|---|
| OS, python3 ≥ 3.10, cmake ≥ 3.24, ninja, git, compiler | always |
| `/opt/rocm/core-10.1` and package version; gfx1151 present (sysfs `/sys/class/kfd/kfd/topology/nodes/*/properties` gfx target, passive) | always |
| disk space | always |
| assets: existence and size, sha256 on request | per profile |
| USB4 | ds41 only (below) |
| lab: gpu-turn script identities, current holder, `maintenance`, `layout-state` | lab mode |

- **USB4 check (ds41 only):** `thunderbolt_stream` loaded; `usb4stream-describe` JSON parses with a known `version`; the profile's required cables and streams are up and openable. Its warnings are shown, but only those that break the profile's need fail the check; host fallback stays usable.
- **Active probes** (`rocminfo`, which initializes the GPU) run only inside a lease.

**Asset integrity**
- First use writes a trusted manifest (sha256 of every asset on both nodes, computed in a scheduled hashing job).
- Starts check size plus a `(path, size, mtime, inode)` cache. The cache is labelled a convenience, not an integrity guarantee, and a full re-hash is available on demand.
- A pair start refuses mismatched manifests between ranks.

**CUDA build (Phase 3)**
- `vswhere`, then `vcvars64`, then a check for CUDA 13.0, then `-DSTRIX_CUDA_DS41_ONLY=ON -DCMAKE_CUDA_ARCHITECTURES=120`, `-j8`, low priority.
- "Reproducible" means a clean build from the pinned SHA with the recorded toolchain, giving a manifest with binary shas. Equal shas across rebuilds are reported, not required.

## 5. Lab safety rules (enforced by code and tests)

1. **No identifying data in product-owned tracked files:** no IPs other than loopback and documentation ranges, no `user@host`, no `/home/<name>`, no hostnames, no secrets, plus a deny-list.
   - `tests/test_hygiene.py` scans product-owned files **and file names**; submodule contents are not scanned.
   - Pins are checked separately against each component's own publication policy.
   - No lab reports, coordination logs or credentials are imported into the repo.
2. **No other inference engines named** in product-owned files; the same scanner enforces it. Component license notices are preserved in `NOTICE`.
3. **Production defaults and numerics unchanged:** presets equal receipts (§3.1); there is no raw env; numerics knobs are not exposed.
4. **No change to the production layout or the node system:**
   - no root;
   - no writes to `<lab>` (the turn scripts write their own logs);
   - no layout, configfs, kernel, GRUB, modprobe or system-unit changes;
   - labels never trigger watcher mappings;
   - ds41 profiles require layout 1 and abort otherwise.
5. **GPU use on the nodes only through leases** (= gpu-turn / gpu-turn-pair); there is no code path that starts a GPU binary outside `supervise`.
6. **CUDA only on the RTX PRO:** every CUDA child, probes included, gets `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<verified PRO UUID>`. The resolved device name is checked (`nvidia-smi -i <uuid>`) before work starts. The 4090 is never used.
7. **PC heavy work** (CUDA builds, the prefill service) needs, in lab mode, an **operator-granted PC reservation** in `pc.cuda.reservation`: owner/approval reference, deadline, VRAM cap, host-memory budget and free-commit floor. It runs under a PID-specific watchdog (the same policy as the lab watchdog), and nothing starts without it. A UI override cannot grant a reservation.
8. **No automatic model downloads or conversions.** The Assets page shows the exact pinned commands; a user-confirmed "run" is Phase 4. Conversions needing tools outside the pinned components are documented only.

## 6. Phases and acceptance

**Every phase ends with:**
- unit and hygiene tests green;
- an Opus review of each Sol task, one commit per task;
- a report in ds41-coord.md.

**Rules for GPU acceptance runs:**
- they run in short authorized leases (≤ 10 min each), at times main approves;
- performance thresholds are **investigation thresholds** compared in **contemporaneous interleaved AB/BA rounds** with frozen corpus, binary, receipt, layout and cache state; they are not absolute pass gates against historical numbers;
- `bench-protocol --execute` must produce complete, valid waves.

### Phase 0: skeleton, no GPU
**Scope**
- Repo, submodules, `versions.lock`, `cluster-v1`, render with TensorMeld-parser validation.
- Router adapter, engine adapters with **fixtures captured from real outputs**, ssh transport, `hyperloom-node doctor` and `gpu`, `install.ps1`, the Windows Job Object router child.

**Acceptance**
1. Clean install on Python 3.11/3.12/3.13, with a path containing spaces.
2. `hyperloom doctor` on both real nodes, read-only, with the commands listed.
3. Golden renders: the Qwen r3 preset equals the r3 receipt argv/env, tokenized, using the **real calibration IDs** from the lab's prompt file through the asset manifest.
4. Router child lifecycle tests: window close, killed parent, restart, port reuse; no orphans.
5. ssh transport tests against a real OpenSSH test server, plus failure cases (unreachable, bad key, EOF, cancel).

### Phase 1a: Qwen two replicas + router, CLI
**Scope:** `build-node.sh` (engine pin on `origin/main`), transactional install on both nodes, Qwen asset manifest, `lease`/`supervise`, router on the PC, `status`, `stop`, `logs`.

**Acceptance**
1. `hyperloom start qwen-replicas`: two leases acquired, both replicas ready (stderr adapter and `/health`), router `/health` 200 with `replicas_up: 2`.
2. Four parallel chat requests through the router: all succeed, and both replicas show `assigned > 0`.
3. **Failure matrix:**
   - engine killed mid-stream: stream fails, `supervise` stops, turn released, no leaked processes (checked with `ps` and the lock holder, not only the turn log);
   - start with a missing asset: refused before the lease;
   - deadline: drained or stopped before the cap, with a `STOPPED deadline` event;
   - controller killed: `supervise` continues to the deadline; reattach works;
   - stop while queued;
   - partial start (one node fails): both cleaned up.
4. **Token regression gate:** a fixed greedy set (8 prompts × 64 tokens, `return_token_ids`) through the product equals the same set on the same binary sha launched by the lab harness. This is a regression gate, not a numerics proof.
5. **Router on PC vs on node 1:** interleaved `bench-protocol` rounds (r3 corpus, levels 1/4/6). A difference above 5 % aggregate or 100 ms client TTFT p50 is reported to main for a decision. The node-1 router option is **not** built unless main asks.
6. Rollback to the previous release; uninstall leaves nothing outside the marked root.

### Phase 1b: thin UI over 1a
**Scope:** Setup, Install, Run, Parameters (capacity knobs), Stats, Logs pages.

**Acceptance**
1. The security negative tests pass.
2. Every 1a acceptance action can be done from the UI.
3. The stats tiles match the router and engine JSON within one poll.

### Phase 2: DeepSeek pair (needs the publish merge pushed to engine `main`)
**Scope**
- trellis build, ds41 preset from the pin's production contract (`usb4gpu`), asset manifests for both ranks.
- Pair lease through `gpu-turn-pair.sh` with the layout-1 post-acquire check.
- Router with rank 0 as a single replica.

**Acceptance**
1. Golden render equals the pin's production line.
2. Both `READY`; `trellis_identity` equals the production identity.
3. Token regression gate against the lab-launched pair with the same binary.
4. Negative tests:
   - mismatched release on the two ranks: refused by `hyperloom`, and by the engine hello when forced;
   - a same-size wrong asset: caught by the manifest;
   - a wrong layout: aborted after acquire.
5. Interleaved serve C1/C4 vs the lab launch: investigation threshold 3 %.

### Phase 3a: RTX prefill service (prerequisites: R12, R13; a granted PC reservation)
**Scope**
- CUDA build; prefill service on 127.0.0.1 inside a Job Object with the PRO UUID.
- Router `prefill.policy auto` with the service's measured profile.
- Handoff fixed to rank 0 with `--handoff-api-key` = rank 0's key.
- Service positions, pair context/pool and output reserve set consistently. The pair profile for 3a uses a qualified context ≥ the prompts it offloads, for example 8K+ for the 8K test.

**Acceptance**
1. The process is visible only on the PRO.
2. An 8K prompt is placed `offload` (router header), imported, and decoded with **no local-prefill fallback**.
3. Short prompts are placed `local`.
4. Exactness: a **named fidelity gate** with an oracle for the RTX profile's identity (exact arm vs trellis quality arm, from the qualification receipt) is a prerequisite. The product test is launch-equivalence against the lab-launched service.

### Phase 3b: split-decode FRONT — deferred
It starts only when the engine ships a CUDA FRONT with an HTTP serving API, `/health`, readiness events and a qualified launch contract (R14). It also needs either TensorMeld `deploy-pipeline` qualified with real engines and GPU leases (R10), or `hyperloom` launching it through `supervise` like any other server. `hyperloom` does not reimplement the experiment drivers.

### Phase 4: polish
- Non-lab `systemd --user` mode.
- Binary release tarball.
- A user-confirmed run of pinned download commands.
- Diagnostic-run mode.
- Per-pin qualified advanced knobs.
- Prometheus re-export.
- CI for the pure-Python tests.
- From-scratch install docs.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Phase 2+ engine pin not yet on `main` | Phase 1 needs only `origin/main`; Phase 2 waits (prerequisite) |
| Qwen production line lives in lab scripts | preset per receipt + R1; golden tests |
| Live gpu-turn semantics differ from the local copies | doctor records the live script identity; a lease is offered only for an approved identity |
| 20-min cap vs serving | deadline-aware `supervise`; longer only with an explicit main approval reference; non-lab mode later |
| Router on PC adds latency | measured in Phase 1a; main decides |
| Builds and hashing disturb measurements | scheduled outside measurement turns; refused during pair turns |
| Windows process/ssh quirks | Job Objects; fixed-verb ssh with JSON stdin; real OpenSSH tests |
| CUDA service takes keys on the command line | loopback only; redaction; R12 |
| Scope creep | MVP = 1a CLI; UI thin; everything else is a later phase |

## 8. Conventions
- **Repo:** `hyukudan/hyperloom` (private). Package and CLI: `hyperloom`. Node tool: `hyperloom-node`.
- **Python:** stdlib only (PC ≥ 3.11, node system python3 ≥ 3.10); `unittest`.
- **Commit author** `hyukudan <19479239+hyukudan@users.noreply.github.com>`, with these trailers:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01YQhXyYBxNnjq4twCoox8bT
  ```
- **License:** proprietary/private for now; the `NOTICE` file preserves component licenses.

## 9. Build process with Sol (after main's OK)
- One phase at a time. Sol workers get **disjoint** tasks (files/modules), for example in Phase 0:
  - (a) config + render + golden tests;
  - (b) adapters + fixtures;
  - (c) remote/ssh + `hyperloom-node` doctor;
  - (d) the Windows install and router child.
- Opus reviews each diff, runs the tests and commits.
- No GPU, no node writes beyond `doctor` until Phase 1a, and then only in leases approved by main.

## 10. Component requests (filed in each component's repo; not done here)

| # | Component | Request | Needed by |
|---|---|---|---|
| R1 | engine | Own the Qwen replica production configuration (preset file or `--preset`), including assets and identity pins, versioned with the binary | P1 (workaround: preset in hyperloom) |
| R2 | engine | Qwen `--http-serve`: implement `on_drain` (`/admin/drain`), and print the listening line also as a JSON `READY` event on stdout, like DeepSeek | P1 nice-to-have |
| R3 | engine | `/metrics`: TTFT/prefill/decode histograms, `strix_build_info{commit}`, device-memory gauges (both servers) | P1b nice-to-have |
| R4 | engine | `/api/status`: engine commit + effective non-default `STRIX_*` set | P1b nice-to-have |
| R5 | engine | CMake presets / build script for gfx1151 (incl. Q4K_A8 runtime, trellis) and for the Windows CUDA front | P2/P3 |
| R6 | engine | Accept split GGUF or provide a merge tool in-repo | P4 |
| R7 | TensorMeld | Router TTFT (first SSE byte) and latency histograms; prefill placements in `/metrics` | P1b nice-to-have |
| R8 | TensorMeld | Handle `SIGBREAK` / `CTRL_BREAK_EVENT` in `serve-replicas` for graceful Windows shutdown; fix README `#developer-setup` | P1 nice-to-have |
| R9 | TensorMeld | Router offload: forward `reserve_tokens` and a per-service decode-key mapping | P3a |
| R10 | TensorMeld | Qualify `deploy-pipeline` with real apps, production argv (DSpark, `--prefill-path device`) and an external lease hook | P3b |
| R11 | driver | Document `usb4stream-describe` JSON `version` as a stable contract | P2 |
| R12 | engine | CUDA prefill service: `--api-key-file` / `--handoff-api-key-file`, and an allow-list of handoff destinations | P3a |
| R13 | engine | CUDA prefill service: `/metrics` + `/api/status` like the shared serve layer | P3a |
| R14 | engine | CUDA split-decode FRONT as an HTTP server (serve routes, `/health`, READY events) | P3b |
| R15 | strix-trellis | Tagged releases | P2 nice-to-have |

## 11. Questions for main/user
1. **Router location:** the PC by default, as the brief says. Phase 1a measures it against node 1 and you decide. OK?
2. **Serving under the lab GPU-turn cap:** sessions end at 20 min unless you give an approval reference for a longer lease. Who may approve it (main only)?
3. **Label namespace** `hl-*` for leases: please confirm that it maps to no watcher layout.
4. **Repo name** `hyperloom`: OK?
5. **Phase 2 waits** for the engine publish merge to be pushed to `main`. OK?

## 12. Answers to Sol's review (`SPEC-review-sol.md`)

A = accepted, P = partly accepted, R = rejected. Section references are to this SPEC.

| # | Verdict | Answer |
|---|---|---|
| 1 | A | Verified: README enables `Q4K_A8_RUNTIME`; the preset uses `--q4k-a8g32-gate-up`. The recipe includes Release + the option; capability check before activation; named ctest subset (§2.6). |
| 2 | A | Calibration IDs, golden and draft-vocab dir added to assets; no silent generation; golden test uses the real IDs (§1.5, §6 P0.3). |
| 3 | A | Verified in `ds41-production-defaults.md`: `usb4gpu`. Preset derived from the pin's contract; `camp-lib.sh` is historical only (§1.2, §3.1). |
| 4 | A | Verified: no `--json`; no-arg output is JSON. Doctor parses it, checks the version, and is profile-specific about warnings (§4). |
| 5 | A | Live cap documented; no general extension; explicit approval reference only; live-script identity recorded (§2.6, §11.2). |
| 6 | A | Single foreground `supervise` inside the turn owns the process groups, deadline, cleanup and partial starts; no nesting; post-acquire checks (§2.6). |
| 7 | A | Fixed `hl-*` namespace approved by main, never matching the watcher; layout 1 required for ds41; post-acquire verification on both nodes; abort on mismatch (§2.6, §5.4). |
| 8 | A | Verified: Qwen listening line on stderr, no `on_drain`. Per-binary adapters; capability-gated drain; R2 (§2.6, §10). |
| 9 | A | Absolute venv python, drained pipes, Job Object, CTRL_BREAK + grace, single instance, lifecycle tests; R8 (§2.5). |
| 10 | A | Verified: the CUDA FRONT has no HTTP. 3b deferred until R14; the hyperloom-own-launch fallback removed (§6 P3b). |
| 11 | A | `deploy-pipeline` not used before R10; no copying of its modules (§1.1, §6). |
| 12 | A | Key mapping, `--handoff-api-key`, context/positions/reserve consistency, no-fallback test; R9 (§6 P3a). |
| 13 | A | Fidelity gate with a named oracle is a prerequisite; launch-equivalence is separate; escape clause removed (§6 P3a). |
| 14 | A | Host allow-list, Origin, JSON + custom CSRF header, GET read-only, no CORS, bounds, negative tests (§2.4). |
| 15 | A | Auth matrix; prefill service on loopback; redaction; R12 (§2.7). |
| 16 | A | Fixed bootstrap and verb allow-list, JSON stdin, target validation, home expanded once, real OpenSSH tests (§2.6). |
| 17 | A | Marked root with overlap refusal; immutable release paths; transactional two-node activation only when stopped; never delete the active release (§2.6). |
| 18 | A | Router constraints added; render validated with TensorMeld's parser; HTTP vs ssh addresses and PC vs node key paths separated. The node-router option is not built unless main asks (§2.3, §6 P1a.5). |
| 19 | A | Corrected: rtx-reconcile is an ancestor of `publish/main-20261023` (verified). SHA pins + dated capability matrix; fetch by SHA (§1.2, §2.2). |
| 20 | A | Verified: r3 has `--http-default-max-tokens 256`. One receipt per preset, exact, tokenized comparison; friendlier values only as visible overrides (§3.1). |
| 21 | A | MVP knobs are capacity/queue/ports only; no raw env; algorithm switches later, per pin with qualification; env sanitized (§3.1, §2.3). |
| 22 | A | Builds and hashing scheduled, refused during pair turns; whole job in the scope; user-scope fallback; passive vs active probes separated; "never starves" claim removed (§2.6, §4). |
| 23 | A | Operator-granted PC reservation with budgets, deadline and PID watchdog; PCI_BUS_ID + verified UUID on every CUDA child; a UI override grants nothing (§5.7). |
| 24 | A | One resolved interpreter, PowerShell path APIs, absolute venv python, setuptools dependency documented, wheels for offline, tests on 3.11–3.13 with spaces (§4). |
| 25 | A | `release-id` = hash of the full manifest (toolchain, presets, `hyperloom-node`, binary shas); runtime libs checked; per-submodule export (§2.2, §2.6). |
| 26 | A | Per-component, versioned adapters; unavailable fields shown as such; prefill placements from JSON (§3.2). |
| 27 | A | Labelled "completed tok/s over window"; deduplication, reset handling, quantile window with N; client TTFT kept separate (§3.2). |
| 28 | A | Trusted asset manifest on both ranks; mtime cache is a convenience only; negative tests with a same-size wrong asset and mismatched ranks (§4, §6 P2). |
| 29 | A | Frozen inputs, interleaved AB/BA, `--execute` complete waves, thresholds are investigation thresholds, short leases; the "reproducible" build is defined (§6, §4). |
| 30 | A | Failure matrix gates 1a; leaks checked independently of the turn logs; the greedy gate is a regression gate only; fixtures are captured from real outputs (§6). |
| 31 | A | Hygiene scan covers product-owned files and file names only; pins checked separately; NOTICE; presets are data only; no copying of supervisor/API/harness (§5.1, §3.1). |
| 32 | P | Accepted: CLI-first MVP (1a) with one Qwen profile, existing assets, one router location; downloads, tuning, re-export and 3b deferred; lab harness reused externally. **Partly rejected:** the UI stays in Phase 1 (as 1b, right after 1a qualifies), because the user's request is explicitly a Windows front end for configuring and installing. Source install stays in 1a because there are no binary artifacts to install otherwise. |


## 13. Addendum A (user): any number of machines, discovery, pairing, device view, placement

### 13.1 Model
- **The cluster is one control machine plus 1..N machines.** Any of them may be Windows or Linux. The fixed "PC + two Strix Halo" setup is only the first instance of the general model.
- **The portal (`hyperloom`) runs wherever the operator is.** Typically that is the Windows PC with the discrete GPU, but it also runs on Linux; the code is the same stdlib Python.
- **`cluster-v1` changes:** `nodes[]` becomes `machines[]`, each with an `os` field and an `access` field:
  - `ssh`: what exists today;
  - `agent`: TensorMeld's enrolled agent, once that agent ships (TM-R10..R12).
  - The control machine itself is `access: local`.
- **`hyperloom-node` is portable stdlib Python.**
  - Linux workers are Phase 1.
  - A Windows *worker* (e.g. a second CUDA box) is Phase 4. It needs Windows OpenSSH server and Python, plus Windows support in the lease wrapper. There is no gpu-turn on Windows, so a PC reservation applies instead (§5.7).

### 13.2 What TensorMeld has today (read from `27d3bfe`) and what we reuse

| Need | TensorMeld today | Product decision |
|---|---|---|
| Enrollment / pairing | `agent.py` HostAgent with HMAC capability envelopes, `private_transport.py` mTLS with exact cert pinning, `remote_control.py` private endpoints. **Library only**: no CLI, no daemon (`serve_private_endpoint_once` is a test harness), no pairing/CSR, no persistent enrollment registry; real mTLS tested only on Linux CI loopback | MVP joins machines by **"add by address"** over the operator's ssh keys, which is what TensorMeld's working multi-node path (`SshLauncher`) already uses. The pairing confirmation is the ssh host-key fingerprint, shown in the UI and compared by the operator. The TensorMeld agent replaces ssh when TM-R10..R12 land. hyperloom does **not** build its own mTLS/agent stack. |
| LAN discovery | none (the spec allows discovery for *reachability only*, never authorization) | **TM-R13:** a signed UDP announce in TensorMeld (`tensormeld agent announce`) carrying name, OS, accelerator summary and key fingerprint. hyperloom only *lists* announcements ("Discovered, not paired"); pairing is always explicit. Until then: add by address. |
| Inventory | `tensormeld probe`: OS, logical CPUs, RAM, NVIDIA via nvidia-smi (no UUID/PCI), AMD sysfs presence only (no memory, no ROCm), NICs; no CPU model, no IPs | hyperloom shows probe output plus `hyperloom-node gpu/doctor` (AMD memory from sysfs, ROCm version, kernel, USB4 driver, layout). **TM-R14:** extend probe with NVIDIA UUID/PCI bus/index, CPU model, AMD memory (VRAM/GTT, UMA), ROCm/CUDA versions, Windows AMD. |
| Hardware descriptor | `hetero-hardware-v1` is written by hand; nothing converts probe output to it | hyperloom **renders** a descriptor from the inventory, measured link-probe results and the qualified receipts (devices' rates from receipts, `provenance: measured\|declared`). This is data translation only, validated by TensorMeld's parser. **TM-R15:** `probe → hetero-hardware-v1` helper upstream; hyperloom switches to it. |
| Engine self-description | `engine-describe-v1` via `--describe-json`, sha-checked | used as is (DeepSeek serve supports it). **R17:** Qwen serve `--describe-json`. |
| Links | `link-probe serve/run`: token over plain TCP; RTT ladder p50/p90/p99, bulk bytes/s, `suggested_link` | used as is for Ethernet links, started over ssh with a fresh token. USB4 link health comes from the driver status (`usb4stream-describe`) and engine stats, **not** from probing the pair's streams. **TM-R16:** link-probe over the enrolled mTLS channel when the agent exists. |

### 13.3 Discovery and joining flow (MVP → later)

**MVP: add by address**
1. The operator enters an address and an ssh target.
2. `hyperloom` connects with `BatchMode`. If the host key is new, the UI shows its fingerprint and the operator must confirm it against the machine's own `ssh-keygen -lf`.
3. `hyperloom-node` is bootstrapped (§2.6), then `doctor` + `gpu` + `tensormeld probe` run, and the machine appears in Devices.
4. Access is ssh with the operator's key. Nothing listens on the worker.

**Later (after TM-R10..R13): discover and pair**
1. On the new machine, `hyperloom-node pair` starts the TensorMeld agent in **enrollment mode**. It shows a one-time code (6 digits, 5 min) and its key fingerprint, and announces itself (TM-R13).
2. The portal lists the announcement.
3. The operator types the code and checks the fingerprint.
4. A code-authenticated exchange (TensorMeld's choice, TM-R11) issues mutual certificates. The worker then accepts commands only from that portal's pinned certificate.
5. **Revoke** deletes the enrollment on both sides.

**Rules:** no unauthenticated LAN workers; an announcement never grants anything; roles are `gpu-worker` or `observer`.

### 13.4 Device view
- The Devices, Topology and Models screens in `docs/ui/UI-SPEC.md` §3.1–3.3 show:
  - per machine: name, OS, CPU, RAM, disk free, health, last seen;
  - per accelerator: type and memory (VRAM, or unified + GPU-visible), driver/runtime (CUDA, ROCm, kernel + USB4 driver), live usage (GPU %, memory, temperature, power), policy state (the 4090 "Excluded");
  - links (Ethernet rate and latency from link-probe; USB4 cables from the driver and engine);
  - roles (router, replica, pair rank, RTX prefill).
- Missing fields show "unavailable · <why>".

### 13.5 Placement
- **Proposals** = TensorMeld `plan-hetero` over the rendered descriptor and model parts, **filtered to qualified profiles**. A profile is qualified when a preset and receipt exist for the pinned engine.
- Each proposal shows the expected numbers from its receipt, the memory per device and any conflicts.
- Unqualified planner candidates appear as "not available" with their blocker; they are never started.
- **Start / Switch** goes through the normal lease path (§2.6).

### 13.6 Phasing
- **Phase 0/1:** `machines[]` schema; add by address; inventory; the Devices view in 1b.
- **Phase 2:** Topology view with link-probe.
- **Phase 2.5:** placement proposals from `plan-hetero`.
- **Phase 4:** discover + pair through the TensorMeld agent (needs TM-R10..R13); a Windows worker.

**Acceptance (1b)**
- Devices shows the 3 real machines with correct accelerators, memory and drivers.
- An offline machine turns "Offline · last seen" within 3 polls.
- Adding a machine with an unknown host key requires fingerprint confirmation.
- Removing a machine leaves no `hyperloom` files on it (`hyperloom-node uninstall`).

## 14. Addendum B (user): UI design, and the secure configuration interface in the components

### 14.1 UI
- The design is in `docs/ui/UI-SPEC.md`, with mockups in `ui/mockups/` and images in `ui/img/` (desktop light/dark and narrow, for all 7 screens).
- **Build order:**
  1. main shows the mockups to the user;
  2. Sol builds Phase 1b against them;
  3. Opus reviews side by side.

### 14.2 How configuration reaches each component (target design)

**Principles**
- One versioned, schema-validated config file per component, plus a published schema.
- Parameters are on an allow-list, with types and ranges, and each is marked `production | advanced | experimental`. Production defaults stay the default.
- Transport is **file + restart**, which is the simplest safe path and the one used first. An authenticated admin endpoint is used only where a live change is genuinely needed, and only on loopback or the LAN with a token or mTLS.
- No secrets on command lines. Secrets go in OS-protected storage:
  - control machine: Windows Credential Manager (DPAPI) or the Linux user keyring / 0600 files;
  - workers: 0600 files under the marked install root.
- The audit log and rollback live in `hyperloom`: every config version, with who, what and old → new. Components report the **config sha** they actually loaded, so the audit can be checked against reality.

**Flow**
1. `hyperloom` validates the change against the component's own schema (by running the component's validator).
2. It writes a new config version.
3. It restarts the process inside a lease.
4. The component's `READY` event and effective-config endpoint report the `config_sha256`.
5. `hyperloom` checks that this equals the version it wrote; if not, it rolls back and stops.

### 14.3 Work items in the component repos (implemented there; acceptance tests listed)

| # | Repo | Work item | Acceptance |
|---|---|---|---|
| E-C1 | engine | `--config FILE` loader (schema `strix/serve-config-v1`, JSON) for `strix-deepseek41-serve`, `strix-hip-decode-token --http-serve` and the CUDA prefill service. It maps keys to today's flags and `STRIX_*` env, rejects unknown keys, checks types/ranges, and marks each key's level. Precedence: config < explicit flag (so lab scripts keep working). | the same run via env/flags and via an equivalent config gives **bitwise-identical** token ids; unknown key, out-of-range value and experimental-key-without-opt-in are each rejected with a clear error; no config = today's production defaults |
| E-C2 | engine | `--print-schema` (JSON Schema, with levels and production values) and `--print-effective-config` (redacted) | the schema validates every shipped preset; effective config of the production preset = the production defaults doc |
| E-C3 | engine | `GET /config` (effective config, redacted, `config_sha256`) on the shared serve layer and the CUDA service; `READY` events carry `config_sha256` and the binary sha | sha matches the file; no secret appears in the output |
| E-C4 | engine | key **files** only: `--api-key-file` / `--handoff-api-key-file` on the CUDA service (= R12); literal key flags deprecated | `/proc/<pid>/cmdline` contains no key |
| E-C5 | engine | a compact step-trace summary in `/api/status` (per-rank ms per token by phase: attention, MoE, dense/shared, exchange, other; median over the last N steps), off unless enabled (= R16) | enabling it changes no token; summary sums equal the step time within 2 % |
| TM-C1 | TensorMeld | export the JSON Schema of `replica-router-v1` (and the v2 config); `validate-config` also accepts router configs | hyperloom's renders validate in both the parser and the schema |
| TM-C2 | TensorMeld | router `GET /admin/effective-config` (redacted, `config_sha256`); optional authenticated `POST /admin/config` (loopback + token) for validate-then-apply of **limits / max_inflight / policy only**, with atomic swap and the previous config kept | a bad config is rejected without effect; apply + rollback round-trips; no auth means 401 |
| TM-C3 | TensorMeld | SIGBREAK/CTRL_BREAK graceful stop on Windows (= R8) | the final status is written on Windows stop |
| TM-C4 | TensorMeld | OS-protected secret storage helper (DPAPI on Windows, 0600 or keyring on Linux) used by the router and the agent | a key round-trips; the file is unreadable by other users |
| D-C1 | driver | a versioned read-only status contract (`usb4stream-describe` JSON `version`, documented fields) (= R11). **No runtime knobs from the portal**; the documented safe ones may be shown read-only | schema doc plus a fixture test in the driver repo |

**Until these land:** `hyperloom` uses the preset/argv path from §3.1, file + restart only, which is safe today. Each item above switches one component to the config-file path without changing numerics.

### 14.4 Added component requests (continuing §10)

| # | Component | Request | Needed by |
|---|---|---|---|
| R16 | engine | step-trace summary in `/api/status` (= E-C5) | UI "where the decode step goes" |
| R17 | engine | Qwen serve `--describe-json` | placement |
| TM-R10 | TensorMeld | agent daemon CLI (`tensormeld agent serve`) with a persistent enrollment registry | Phase 4 pairing |
| TM-R11 | TensorMeld | pairing protocol: one-time code → mutual certificates, revocation persisted | Phase 4 |
| TM-R12 | TensorMeld | agent operations for inventory (probe), lease-wrapped launch and logs, so ssh is no longer needed | Phase 4 |
| TM-R13 | TensorMeld | signed LAN announce (reachability only) | Phase 4 discovery |
| TM-R14 | TensorMeld | probe: NVIDIA UUID/PCI/index, CPU model, AMD memory + ROCm, CUDA version, AMD on Windows | Devices view (hyperloom fills gaps meanwhile) |
| TM-R15 | TensorMeld | `probe → hetero-hardware-v1` helper | placement |
| TM-R16 | TensorMeld | link-probe over the mTLS channel | Phase 4 |


## 15. Answers to Sol's second review (`SPEC-review-sol-2.md`; §13–14 + UI)

A = accepted, P = partly accepted. Each answer is the binding change. Where it amends §13/§14 or the UI-SPEC, that text applies and overrides the earlier wording.

| # | Verdict | Change |
|---|---|---|
| 1 | A | **Host-key pinning replaces the plain BatchMode wording in §13.3.** `hyperloom` first fetches the candidate host key with `ssh-keyscan -t ed25519,ecdsa,rsa -p <port> <addr>`, without bootstrapping anything. The UI shows the full SHA-256 fingerprint and the algorithm. Once the operator confirms, hyperloom writes that exact key to its **own** `known_hosts` (`%APPDATA%\hyperloom\known_hosts`). Every later ssh and scp call uses `-o UserKnownHostsFile=<hyperloom file> -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=none` (with `-F` set to a hyperloom-owned minimal config, so the operator's ambient config cannot weaken checking), plus the alias, port and address recorded at pairing. A changed key is a hard failure. Tests: unknown key refused, cancellation, key replaced between confirmation and bootstrap, aliases, non-default port. |
| 2 | A | **TM-R11 acceptance criteria:** the pairing protocol binds the full public key and the role; attempts are bounded (5) and the code is single use; the code expires and does not survive a restart; MITM and replay tests; announcements are untrusted claims with bounded size, rate and lifetime. **Revocation:** local distrust is immediate; remote revocation stays *pending* while the worker is offline, active sessions end, and rejection persists across restarts. The UI never reports remote deletion that has not been confirmed. |
| 3 | A | **§13.5 profile adapter:** pinned descriptor fixtures per qualified profile, giving assets, capabilities (`engine-describe-v1`; note that `origin/main` emits `strix/engine-capabilities-v1`, so the adapter accepts only pins with v1), state compatibility, topology, context/slots and reservations. The full pack and drafter footprint comes from the asset manifest, not from GGUF tensor counts. UMA memory is counted once per node. Fitted rates are labelled *estimate* and receipt numbers *receipt*. A test reproduces every displayed card and its blockers. The v1 limits (64 nodes, 128 devices) are documented. |
| 4 | A | **E-C1 adds `--validate-config FILE` / `--resolve-config FILE`**: CPU-only, no weights. One resolver covers config, flags and inherited env with a single precedence, rejects duplicates, and checks dependencies and experimental opt-ins on every input path. The engine reports **two digests**: `config_file_sha256` and `effective_config_sha256` (canonical resolved config, with sources and binary identity). hyperloom compares the *effective* digest from `READY` with its own resolution before it marks the process ready. `/config` and the router's effective-config endpoint require the admin key (they are *not* added to the public telemetry routes). Tests: conflicting overrides, opt-in through env, flag and file. |
| 5 | A | **Two baselines in E-C1/E-C2.** (a) No-config behaviour is preserved exactly, including unset-env semantics and the CUDA service's raw defaults (fast paths ON unless `--exact`); this work changes no default. (b) Each qualified preset is frozen separately. Acceptance has two steps: first resolved argv/env/identity equality without a GPU; then bounded regression gates in leases comparing config launch with legacy launch across cache, concurrency and prefill/handoff paths, using the existing numerical/state oracles wherever bitwise equality is claimed. "Bitwise" is claimed only where an oracle exists. |
| 6 | A | The widget → source map is UI-SPEC §9. Client TTFT comes only from bench receipts; router timing is labelled "router first byte" (R7, extended to a request log with ids for the route column); the queue+prefill estimate is never presented as client TTFT. The UI-SPEC §3.1 reference is corrected to TM-R14. |
| 7 | A | The USB4 telemetry in the mockups is reduced to what exists: current driver state and warnings, plus the engine's exchange aggregate. Per-cable rate, latency and retrain history go behind the new **R18** (engine/driver per-stream counters and latency histogram). Link-probe is labelled RTT at a stated payload. Active USB4 probing has been removed from the UI. |
| 8 | A | **E-C5 redefined:** non-overlapping critical-path accounting per step (overlapped exchange counted once, as exposed wait), normalized per *step*, with emitted tokens per step reported separately. The median of the totals is reported on its own; the sum-of-medians check is dropped. Tests: deterministic traces, token identity on qualified runs, overhead with tracing off and on; off by default. |
| 9 | A | Capability matrix (UI-SPEC §10). Pair, discovery and revoke are shown as "later phase" and disabled in 1b. SSH removal is called **Disconnect**. The no-command-line-secret claim is qualified until R12/E-C4. Tests cover Windows process command lines (`Win32_Process.CommandLine`) as well as `/proc`, secret ACLs and redaction, and rotation failure/recovery (old key kept until the restart succeeds). |
| 10 | A | Mockup fixtures are coherent: RTX profile context 32768; identity shown as "P17.5 · trellis quality-gated · RTX prefill fast path"; no blanket "exact". |
| 11 | A | Reservations, approvals, pairings and secrets are excluded from config rollback. Restores are revalidated. Save = staged; restart is explicit, with failed-start recovery (automatic return to the previous version). Hot apply (TM-C2 POST) is **deferred**: only file + restart until a test proves that limits can be reduced under active streams with no dropped requests and no over-admission. Stop/cancel are never blocked by confirmations or jobs. |
| 12 | A | Tokens fixed and verified ≥ 4.5:1 for text (light text-3 5.29:1 on surface; status pills 4.7–5.9:1; dark primary button 6.1:1). Labelled native controls, accessible menu, dialog/focus rules, contrast + keyboard + screen-reader checks (UI-SPEC §11). |
| 13 | A | Real 390 px viewport in the built-UI tests (the crop is only for mockups and is documented). Add by address stays visible on narrow, Topology becomes a list, logs wrap and scroll, and authoritative state is fetched on resume before mutations; lease expiry is server-side. |
| 14 | P | The commit author identity and trailers in §8 are **required** by the user's rules and are exempt in the hygiene scan (an explicit allow-list for those strings). The publication set excludes review logs, prompts and local transcripts (`product/*.log`, `review*-prompt.txt` stay outside the repo). The mockups use synthetic machine labels (`workstation`, `halo-1..3`) and documentation-range IPs only. **Not done:** removing the author line from the spec documents. They are internal design documents that are not published, and main needs to know who wrote them. |

### 15.1 Added request
| # | Component | Request | Needed by |
|---|---|---|---|
| R18 | engine + driver | per-stream USB4 counters (bytes, waits), a latency histogram and a link-retrain event counter, exposed read-only (engine `/api/status`, driver status JSON) | Topology / Stats USB4 detail |


## 16. Decisions from main/user (05:18) — binding; they override earlier text

### 16.1 Name
- Repo: **private `hyukudan/hyperloom`** (user decision 05:26; created as `telar` and renamed). No public visibility and no releases.
- CLI and package: `hyperloom` (short alias `hloom`). Node tool: `hyperloom-node`. GPU-turn labels: **`hl-<profile>-<short id>`**.
- Every earlier mention of `tmc`, `tensormeld-cluster` or the label prefix `tmc-` reads as `hyperloom` / `hl-`.

### 16.2 Router placement chosen at launch
- The router is **not** fixed to the PC. `hyperloom up` chooses where it runs and which machines and devices to link:
  ```
  hyperloom up <profile> [--router local|<machine id>] [--node <machine id> ...] [--device <machine id>:<device> ...]
  hyperloom up --plan <profile-file.json>          # the same choices as a file (schema hyperloom/launch-v1)
  ```
- **Default:** `--router local`, meaning the machine where the CLI runs.
  - With `--router <machine id>`, hyperloom starts `tensormeld serve-replicas` on that machine through `hyperloom-node` (TensorMeld is installed there from the same submodule pin).
  - The router listens on that machine's private address with a key file, per TensorMeld's rule that a non-loopback listen needs `api_key_file`. The portal/CLI reaches it at that address.
- **Linking:** hyperloom links the machines and devices that are both *declared* (flags or the plan file) and *reachable* (paired/added and passing doctor). Discovered-but-unpaired machines are never linked.
  - If a declared machine or device is unreachable, `up` fails before any lease is taken and names the missing machine. The exception is `--allow-partial`, where the profile itself defines a degraded form, e.g. one Qwen replica.
  - Example: `hyperloom up ds41-rtx --router local --node halo-1 --node halo-2 --device workstation:rtx-pro`.
- **Effect on Phase 1a acceptance #5:** "router on PC vs node 1" stops being a decision gate. It becomes a measurement of both placements, reported to main, and the default stays `local`.
- **UI:** the Engines start dialog has a "Router runs on" selector (default: this machine) and machine/device checkboxes, pre-filled from the profile. The UI-SPEC Engines screen (§3.4) gains this selector.

### 16.3 GPU leases longer than 20 min
- Only **main** may approve them, and only with an explicit ACK line in `ds41-coord.md` that names the **owner, the maximum duration and the end time**. Approval is never implicit.
- `hyperloom-node supervise` accepts `--approved-lease-s N --approval "<coord line timestamp + text>"`. It refuses when N exceeds the approval's duration or the approval's end time is in the past.
- The approval reference is logged in the lease record and shown in the UI.
- `hyperloom` never writes approvals itself.

### 16.4 Label prefix `hl-`: proof that it cannot trigger a layout change
**Source checked:** the layout watcher v3 run by the main session, `layout-watch2.sh` (sha256 `238e64f68cb333eb…`, 22 lines). It runs on the PC and drives both nodes over ssh.
- It reads `<lab>/.gpu-turn/holder` on node 1 and takes the label only from a line starting `PAIR` (`awk '/^PAIR/{print $2}'`).
- It then evaluates:
  ```
  case "$lbl" in <prefixA>*|<prefixB>*|<prefixC>*) want=2bq;;
                 <prefixD>*|<prefixE>*|<prefixF>*) want=2b;;
                 *) want=1; lbl=-;; esac
  ```
  (The six real patterns are lab agent names; each begins with `s`, `f` or `c`.)

**Proof**
- Bash `case` patterns are matched against the **whole** word. Every non-default pattern starts with a literal lab-agent prefix, and the first characters are `s`, `f` or `c`.
- A label starting `hl-` has first character `h`, which differs from the first character of every pattern (`s`, `f`, `c`). So no pattern matches a `hl-*` label.
- The label falls to `*)`, which gives **layout 1**: the production layout and the one ds41 profiles require.
- Single-node turns are not read at all; only `PAIR` lines are.

**Ongoing guarantee**
- The proof covers this watcher version. `hyperloom-node` does not rely on it alone.
- After acquiring any pair turn, `supervise` re-checks that both nodes' `layout-state` and `/run/usb4stream-layout.json` show layout 1. Otherwise it aborts with no start (§2.6).
- We ask main to keep `h*` out of any future watcher mapping. The doctor records the watcher's script sha when it can read it and warns if it changes.

### 16.5 Engine pin
- **All phases pin the engine at PR #42 head `70ec42b9b5ead86c42cd673eff19bea3c09bd1df`** (branch `publish/main-20261023`, on GitHub), including Phase 1a (Qwen) and Phase 2 (DeepSeek). Phase 2 does **not** wait for the merge.
- When PR #42 lands on `main`, the pin moves to the merge commit. That is a normal reviewed pin change, with the acceptance tests of the affected phases rerun.
- This supersedes §1.2's "Phase 1 pins origin/main" and §6 Phase 2's "needs the publish merge".
