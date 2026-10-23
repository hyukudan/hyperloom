# Phase 0 tasks (no GPU, no writes on real nodes)

Read `docs/SPEC.md` first: §2 architecture, §2.3 config, §2.4–2.7, §3, §5, §13, §15 and §16. Sections 15 and 16 override earlier text. Package name `telar`, node tool `telar-node`, labels `tlr-`.

## Rules for every worker
- Python ≥ 3.11 on the control machine and ≥ 3.10 for `node/telar-node`; **stdlib only**; tests use `unittest` (`python -m unittest discover -s tests -v`). Tests must pass on Windows (this machine) and be portable to Linux.
- **Edit only the files you own** (listed per task). Shared files that already exist (`telar/__init__.py`, `tests/__init__.py`) are read-only. Never edit `third_party/` (pinned submodules, read-only). **Do not commit, push or change git config**; the reviewer commits.
- **No identifying data in any file:**
  - no IPv4 except 127.0.0.1 and the documentation ranges 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24;
  - no real hostnames: use `halo-1`, `halo-2`, `workstation`;
  - no user names: use `<user>` / `operator`;
  - no `/home/<real name>`, no keys or passwords.
  - Fixtures taken from real lab outputs must be sanitized.
- Do not name inference engines other than our own components.
- No GPU use, no ssh to real machines, no network listeners except on 127.0.0.1 inside tests.
- Keep code small and plain, with comments only where needed. No new dependencies.
- When done, print a short summary: files created, how to run your tests, the test count, and open issues.

## Interfaces (stable names other tasks import)
- `telar.config`:
  - `ConfigError(ValueError)`;
  - `load(path) -> Cluster`;
  - `parse(obj: dict) -> Cluster`;
  - dataclasses `Cluster(machines: dict[str, Machine], assets, profiles, router, pc, raw)` and `Machine(id, os, access, http_address, ssh_target, ssh_port, install_root, lab_mode, devices)`.
- `telar.render`:
  - `render_router(cluster, profile_name) -> dict` (a TensorMeld `replica-router-v1` object);
  - `validate_router(obj)`, which imports `tensormeld.replica_router` from `third_party/tensormeld/src` and runs its parser;
  - `render_processes(cluster, profile_name) -> list[ProcessSpec]`;
  - `ProcessSpec(machine_id, role, argv: list[str], env: dict[str,str], ready: str)`, where `ready` is `"qwen-stderr"`, `"ds41-json"` or `"prefill-json"`.
- `telar.remote`:
  - `RemoteError`;
  - `keyscan(address, port) -> list[HostKey]` and `HostKey(algorithm, key_b64, sha256_fingerprint)`;
  - `KnownHosts(path).pin(alias, address, port, hostkey)`;
  - `Remote(machine, known_hosts, ssh="ssh").call(verb: str, payload: dict, timeout) -> dict`.
- `telar.adapters`:
  - `prom.parse(text) -> dict[(name, labels-tuple), float]`;
  - `router.parse_status(obj) -> RouterStatus`;
  - `engine.parse_status(obj) -> EngineStatus`;
  - `engine.parse_metrics(text) -> EngineMetrics`;
  - `prefill.parse_health(obj)`;
  - `telar.stats.Window` (rate and quantile helpers, reset-aware).
- `telar.procs`: `LocalChild(argv, env, log_dir, name)` with `.start()`, `.alive()`, `.stop(grace_s)`. On Windows it uses a Job Object (kill-on-close) and `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`; on POSIX a process group + SIGTERM.

## Task A: config + render (owner: worker A)
**Files**
- `telar/config.py`, `telar/render.py`, `telar/presets/qwen-replicas.json`, `telar/presets/ds41-pair.json`
- `tests/test_config.py`, `tests/test_render.py`, `tests/fixtures/render/*`
- `examples/cluster.example.json` (synthetic, documentation IPs)

**Config**
- Schema `telar/cluster-v1` (SPEC §2.3 + §13.1 `machines[]` + §16), with strict validation: unknown or duplicate keys rejected; HTTP addresses must be numeric private or loopback IPs; ssh targets validated (no leading `-`, no whitespace or control characters); install root must be absolute. It holds no secrets, only key-file paths, kept separate for the control machine and the nodes.

**Router render**
- From the profile plus `router` settings. It must satisfy TensorMeld's parser: a top-level `api_key_file` for a non-loopback listen, a `max_inflight` for every replica, and `node_profile` when policy is `auto`.
- Test it with `validate_router`, by importing the real parser from `third_party/tensormeld/src`.

**Presets** are data only.
- **Qwen:** derived from the lab receipt `D:\tmp\cluster-opus-spec\campaign\qwen\rep\runs\r3\prod-c1.cmd`, plus `D:\tmp\cluster-opus-spec\campaign\qwen\harness-copy\qwen_cmd.sh` and `campaign\qwen\rep\qserve.py` (read-only). Keep the exact flag order and values. Model, sidecar, golden, vocab and calibration-token file come from asset paths. The calibration token IDs are positional arguments: the render reads them from an asset file, and the test uses a synthetic ID file. Env is explicit.
- **DeepSeek pair:** from `third_party/engine/docs/ds41-production-defaults.md`: `--transport usb4gpu`, `--prefill-path device`, DSpark flags, and the identity env.

**Golden test**
- Rendering each preset with no overrides equals a golden argv/env JSON fixture, compared tokenized.
- Overrides are allowed only for the capacity knobs in SPEC §3.1, within qualified values. Reject everything else, including any loader/interpreter env (`LD_*`, `PYTHON*`, …) and the reserved identity/transport/device env.

## Task B: adapters + stats (owner: worker B)
**Files**
- `telar/adapters/__init__.py`, `prom.py`, `router.py`, `engine.py`, `prefill.py`
- `telar/stats.py`
- `tests/test_adapters.py`, `tests/test_stats.py`, `tests/fixtures/adapters/*`

**Sources**
- Router status `tensormeld/replica-router-status-v1` and its `/metrics`: see `third_party/tensormeld/src/tensormeld/replica_router.py`.
- Engine `strix-serve-status-v1` and `/metrics`: see `third_party/engine/src/serve/api.cpp`; DeepSeek extras in `apps/deepseek41_serve_main.cpp`.
- CUDA prefill service `/health` and `/v1/describe`: `apps/cuda_ds41_prefill_service_main.cu`.

**Fixtures**
- Prefer real captured outputs. Search read-only under `D:\tmp\cluster-opus-spec` (e.g. `campaign/**/` for `api/status` or `router-final.json` dumps) and sanitize them.
- Otherwise build fixtures exactly from the code's field names, and note which kind each fixture is in a README beside them.

**Behaviour**
- Adapters check the schema id. Unknown versions are an error; missing fields become `None` plus a `why` string.

**Stats**
- Rates are reset-aware: a counter decrease means a restart.
- Recent requests are deduplicated by id. Quantiles cover the last N=50 requests or 10 min and report `n`.
- Throughput is labelled "completed".
- Engine-side TTFT = queue_ms + prefill_ms, labelled as such.

## Task C: remote + telar-node (owner: worker C)
**Files**
- `telar/remote.py`
- `node/telar-node`: a single-file Python 3.10+ stdlib script with no third-party imports
- `tests/test_remote.py`, `tests/test_node.py`, `tests/fixtures/ssh/*` (a fake ssh script)

**Remote**
- Follow SPEC §2.6 (bootstrap, fixed verbs) and §15 #1, host-key pinning:
  - `keyscan` uses `ssh-keyscan`;
  - pinning writes to telar's own known_hosts;
  - every ssh/scp call uses `-F <telar minimal config> -o UserKnownHostsFile=<file> -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=none -o BatchMode=yes -o ConnectTimeout=10`, plus the port.
- Verbs come from an allow-list. Arguments are bounded UTF-8 JSON sent on stdin. The reply is one JSON document on stdout.
- Timeouts and cancellation are handled.

**`telar-node` verbs for Phase 0:** `version`, `doctor`, `gpu`. All are read-only, and each output lists the commands it ran.
- **doctor** (SPEC §4 table):
  - OS and python; cmake, ninja, git, compiler versions;
  - `/opt/rocm/core-10.1` and the ROCm package version;
  - gfx target from `/sys/class/kfd/kfd/topology/nodes/*/properties`, passively;
  - disk free;
  - the USB4 checks: `/sys/module/thunderbolt_stream`, `/dev/tbstream*`, `usb4stream-describe` run **without arguments** and parsed as JSON (`version`, `topology`), and `/run/usb4stream-layout.json`;
  - lab files: the gpu-turn scripts' sha256, `.gpu-turn/holder`, `maintenance`, `layout-state` under a configurable lab dir;
  - Secure Boot via `mokutil --sb-state` if present, informational only.
- **gpu:** AMD from sysfs (`mem_info_vram_used/total`, `mem_info_gtt_*`, `gpu_busy_percent`, hwmon temp/power); RAM from `/proc/meminfo`; load; `uname -r`.
- Missing files mean a check result with `"status": "unavailable"`, never a crash.
- Make filesystem roots injectable (`--root` for tests) and test against a fake sysfs tree.

## Task D: process control, CLI and Windows install (owner: worker D)
**Files**
- `telar/procs.py`, `telar/cli.py`, `telar/__main__.py`
- `pyproject.toml` (name `telar`, version 0.1.0, console script `telar = telar.cli:main`, no dependencies)
- `windows/install.ps1`, `windows/telar.cmd`
- `tests/test_procs.py`, `tests/test_cli.py`

**`procs.LocalChild`**
- Starts the child from an absolute interpreter or binary path and drains stdout/stderr on threads into bounded rotating log files.
- **Windows:** Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via ctypes, `CREATE_NEW_PROCESS_GROUP`; stop = `CTRL_BREAK_EVENT`, then a grace period, then `TerminateJobObject`.
- **POSIX:** `start_new_session` + SIGTERM, then SIGKILL.
- A single-instance lock file plus a port check prevents duplicates.
- **Tests:**
  - a dummy child is killed when the parent's job handle closes (spawn a helper parent process and kill it);
  - graceful stop works;
  - a port already in use refuses the start.

**CLI** (argparse; thin wrappers that import the interfaces above lazily so missing modules fail with a clear message):
- `telar config validate PATH`
- `telar render PROFILE [--config PATH]` prints the router JSON and ProcessSpecs as JSON
- `telar doctor [MACHINE]` (local uses `node/telar-node doctor` directly; remote uses `telar.remote`)
- `telar router start|stop|status [--config PATH]` starts the rendered router locally as a `LocalChild` (`<abs python> -m tensormeld serve-replicas <file>`, with `PYTHONPATH` pointing at `third_party/tensormeld/src` or the installed package)
- `telar up` / `telar down` only print "Phase 1a" for now

The config default path is `%APPDATA%\telar\cluster.json` on Windows and `~/.config/telar/cluster.json` on POSIX.

**`install.ps1`**
- Resolve one interpreter: `py -3.13`/`-3.12`/`-3.11`, or `python` ≥ 3.11, recording its absolute path.
- Check git and ssh.exe.
- Create the venv at `$env:LOCALAPPDATA\telar\venv` using PowerShell path APIs.
- `<venv>\Scripts\python.exe -m pip install third_party\tensormeld .` (offline when `wheels\` exists).
- Create a Start-menu shortcut "Telar" running `<venv>\Scripts\python.exe -m telar ui` (the UI comes in Phase 1b) and `windows\telar.cmd`.
- Support `-WhatIf`, and test it with paths containing spaces.
- Add a unittest that runs `install.ps1 -WhatIf` through `powershell -NoProfile` when on Windows (skip otherwise).
