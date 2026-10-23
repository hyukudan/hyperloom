# Phase 1a tasks: Qwen two replicas + router, from the CLI (code only)

Read `docs/SPEC.md` §2.5–2.7, §4, §6 (Phase 1a), §15 and §16, plus `docs/phase0-tasks.md` (its rules apply unchanged). Read the Phase 0 code you build on: `telar/config.py`, `render.py`, `remote.py`, `procs.py`, `cli.py`, and `node/telar-node`.

**Scope boundary:** in this phase **nobody touches a GPU, a real node or a real lab script.** Tests use fakes and temp dirs. The reviewer runs the real acceptance later, in leases approved by main.

## Facts about the lab (read-only knowledge; never copy these scripts into the repo)

**Single GPU turn.** `<lab>/gpu-turn.sh <label> <cmd...>`:
- It blocks on a node flock and yields to pending pair turns.
- It writes `<lab>/.gpu-turn/holder` as `<label> since HH:MM:SS pid N`.
- It runs the command as `timeout -k 30 ${GPU_TURN_MAX:-1200} cmd` and logs `start` / `end` / `CAP`. Exit code 124 means the cap was hit.
- `GPU_TURN_MAX` may be set **only** with an approval from main (SPEC §16.3).

**Pair turn.** `<lab>/gpu-turn-pair.sh <label> <cmd...>`, run on node 1:
- It holds both nodes' locks and writes `PAIR <label> (local) since …` to the holder file.
- **It has no time cap**, so the supervisor must enforce the deadline itself (default 1200 s).

**Layout.** `<lab>/.gpu-turn/layout-state` holds `<layout> <label>`, where `1 -` is production. Labels starting `tlr-` never trigger a layout change (SPEC §16.4), but ds41 profiles must still re-check after acquire. A Qwen lease does not need layout 1.

**Engine processes:**
- They need `LD_LIBRARY_PATH=/opt/rocm/core-10.1/lib`, which the lab sets today.
- Qwen prints `strix serve: listening on ADDR:PORT model …` on **stderr** and has no `/admin/drain`; stop it with SIGTERM.
- DeepSeek prints JSON `READY`/`POISONED`/`STOPPED` on stdout and supports `/admin/drain`.
- Both serve `GET /health` with no auth.

**Node-to-node ssh:** node 1 can ssh to node 2 with BatchMode keys, using a node-side alias configured in `cluster.json` as `machines.<id>.peer_ssh.<peer id>`.

## Node extension contract (already wired; do not change `telar-node`)
- `node/telar-node` dispatches any verb it does not know to `telar_install.VERBS` / `telar_lease.VERBS`. Each module exposes `VERBS: set[str]` and `run(verb, payload: dict) -> dict`.
- The payload is the JSON from stdin. Return `{"schema": "telar/node-v1", "verb": ..., ...}`, plus `"error": "..."` on failure.
- The modules are imported from the same directory (`~/.telar-boot/`) and are **stdlib only, Python ≥ 3.10**.
- Every path in a payload is absolute and validated: no `..`, no control characters.
- **Marked install root:** `<root>/.telar-root` contains a random id written by `install` on first use. Refuse a root that is or contains `$HOME`, the lab dir, a git repository, or a configured asset path (canonicalized with `realpath`).

## Task F: node install and releases (owner: worker F)
**Files:** `node/telar_install.py`, `node/build-node.sh`, `tests/test_node_install.py`, `tests/fixtures/node_install/*`

**`VERBS = {"install", "job", "releases", "activate", "uninstall", "verify-model", "push-release"}`**

**`install`**
- Payload: `{root, lab_dir|null, components: {name: {repo, commit}}, recipe: {configure: [...], targets: [...], ctest_regex, trellis: bool}}`.
- Sources go into `<root>/src/<name>-<commit>` via `git init` + `git fetch --depth 1 origin <commit>` + `git checkout FETCH_HEAD`.
- It runs `build-node.sh` as a **background job** (`setsid`, nohup, its own process group) and returns `{job}` at once. Job state lives in `<root>/run/job-<id>.json`, with a log at `<root>/log/job-<id>.log`.
- **Build envelope:** `systemd-run --user --scope -p MemoryMax=16G nice -n 19` when `systemd-run --user` works, else `nice -n 19`.
- **Lab mode:** refuse to start while the lab holder line starts with `PAIR`.
- On success the job writes `<root>/releases/<release-id>/{bin,MANIFEST.json}`:
  - the manifest lists component commits, the configure recipe, toolchain versions (`amdclang++ --version`, ROCm version, cmake, ninja), every binary's sha256, the telar-node version, and `runtime_env: {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"}`;
  - `release-id` = the first 12 hex characters of the sha256 of the canonical manifest JSON.
- **Capability check:** run `<bin> --help` (expect exit 0 or a usage text) for every target, and record it.

**`build-node.sh <src-engine> <src-trellis|-> <build-dir> <out-bin> <targets...>`:** plain bash.
- With trellis it first builds trellis (`cmake -G Ninja -DSTRIX_TRELLIS_ENABLE_HIP=ON`) and passes `-DSTRIX_DS41_TRELLIS=ON -DSTRIX_DS41_TRELLIS_API=v12 -DSTRIX_DS41_TRELLIS_LIBRARY=… -DSTRIX_DS41_TRELLIS_INCLUDE_DIR=…`.
- It then configures the engine with the recipe flags passed as env/args, builds only the targets with `ninja -j6`, builds and runs `ctest -R <regex>`, and copies the binaries to `out-bin`.

**Other verbs**
- `job {root, job}` returns the recorded job fields (including `state`, and `release_id` or `verified` on success) plus `tail`, the last at most 64 KiB of its UTF-8 log. Both build and hash jobs use this interface. A failed job remains queryable with exit 0, with its diagnostic in `reason`; `error` is reserved for a failed query.
- `releases` lists releases with their manifests and marks the active one per profile.
- `activate {root, profile, release_id}` writes `<root>/etc/active-<profile>.json` atomically (temp file + rename). It refuses if a lease for that profile is running (check `telar_lease` state files under `<root>/run/`).
- `push-release {root, release_id, peer_ssh, peer_root}` copies a release to the peer node (rsync over ssh if present, else `tar | ssh tar`) and verifies the sha256 of every binary on the peer (ssh `sha256sum`). It also fetches the manifest's pinned TensorMeld source into `<peer_root>/src/tensormeld-<commit>` without building it.
- `verify-model {path, bytes, sha256|null}` checks the size; with a `sha256` it hashes in a background job in the same job framework, at `nice -n 19` / `ionice -c3`.
- `uninstall {root, keep_logs}` works only on a marked root, never deletes `releases/<active>` while a lease runs, and never touches anything outside the root.

**Tests:** temp roots; fake `git`, `cmake`, `ninja`, `ctest` and `systemd-run` scripts on PATH (POSIX only, so skip on Windows where needed); the marker refusals; release-id stability; activation refused while a lease is running; uninstall boundaries.

## Task G: leases and the supervisor (owner: worker G)
**Files:** `node/telar_lease.py`, `tests/test_node_lease.py`, `tests/fixtures/node_lease/*` (fake gpu-turn scripts, a fake engine that prints the Qwen stderr line or the DeepSeek READY JSON and serves `/health` on 127.0.0.1)

**`VERBS = {"lease", "cancel", "status", "logs", "stop", "put-secret", "peer-run"}`**

**`lease`** starts, and returns `{lease}` at once. Payload:
```
{root, lab_dir|null, label, kind: "single"|"pair"|"none", deadline_s, approval: null|{lease_s, ref, end_epoch},
 processes: [{name, machine: "local"|"peer", argv, env, ready: "qwen-stderr"|"ds41-json"|"http-health", health_url, ready_timeout_s}],
 peer_ssh|null, peer_root|null, peer_lab_dir|null, layout_required: null|"1"}
```
- It runs `setsid <lab>/gpu-turn.sh <label> <boot>/telar-node supervise-internal <spec-file>`: `gpu-turn-pair.sh` for `kind: pair`; no turn script for `kind: none`, which is the router (no GPU). The spec file is written 0600 in `<root>/run/`.
- `supervise-internal` is a private CLI entry. Implement it in `telar_lease.py` and add it to `VERBS`; it is a plain module function invoked by `telar-node`.
- **Labels** must match `^tlr-[a-z0-9-]{1,40}$`.
- **deadline_s:** the default and maximum is 1140 for `single` (the turn cap is 1200 minus the drain margin).
  - A larger value needs `approval` with `lease_s >= deadline_s + 60`, `end_epoch` in the future and a non-empty `ref`.
  - For single turns, `GPU_TURN_MAX = approval.lease_s` is passed to the turn script; it is never set otherwise.
  - For pair turns the supervisor enforces the deadline itself.

**The supervisor** (foreground; the only owner of the lease):
1. Write `run/lease-<id>.json` with state `queued`, then `acquired` once it runs. In lab mode, verify the holder file shows its label.
2. For `layout_required`, check `layout-state` on this node and (for pairs) on the peer over ssh, plus `/run/usb4stream-layout.json` readable. On a mismatch: state `aborted`, no start.
3. Start each process in its own process group (`start_new_session`) with `env -i`-style env: `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, the release `runtime_env`, plus the spec env.
   - **Reject** spec env names matching `LD_PRELOAD`, `LD_AUDIT`, `PYTHON*`, `BASH_ENV`, `ENV` (`LD_LIBRARY_PATH` only from `runtime_env`).
   - stdout and stderr go to separate log files under `<root>/log/`.
   - Peer processes run through `ssh <peer_ssh> -- <peer boot>/telar-node peer-run` with the process JSON on stdin. `peer-run` starts the child in its own group and kills the whole group when its stdin hits EOF (dead-man), and it streams nothing back except a final JSON status.
4. Readiness per adapter, plus `health_url` 200, within `ready_timeout_s`. If any process fails, stop all and set state `failed` with the reason (partial-start cleanup).
5. Run until `stop` is requested (the `run/lease-<id>.stop` file appears), the deadline is reached (60 s before the turn cap), or a process dies. Stop order:
   - DeepSeek gets `POST /admin/drain` if `ready == ds41-json` (localhost, with the key file if given), then SIGTERM;
   - Qwen and the router get SIGTERM;
   - SIGKILL the group after 30 s.
   - Final state `stopped` with reason `requested|deadline|process-exit:<name>`, then exit, which releases the turn.
6. Never leave children behind. On SIGTERM or SIGHUP to the supervisor, run the same stop path.

**Other verbs**
- `cancel {root, lease}` kills the queued turn-script process group if the lease is still `queued`; otherwise it behaves like `stop`.
- `status {root}` returns all leases with state, processes, pids, start time, deadline and reason. `logs {root, lease, name, tail}` returns at most 64 KiB.
- `put-secret {root, name, value_b64}` writes `<root>/etc/keys/<name>` with mode 0600 and atomic replace. Names match `^[a-z0-9-]{1,40}$`. The value is never echoed.

**Tests:**
- a fake gpu-turn script that records its args and env and runs the command;
- deadline enforcement for pairs;
- approval validation;
- readiness for both adapters;
- partial-start cleanup;
- stop order and drain call;
- a dead-man peer-run;
- no leaked processes (check with `os.kill(pid, 0)`);
- env rejection;
- label validation.

POSIX-only parts are skipped on Windows, and the pure logic is tested everywhere.

## Task H: control-machine orchestration and CLI (owner: worker H)
**Files:** `telar/orchestrate.py`, `telar/secrets.py`, `telar/cli.py` (you now own it), `telar/remote.py` (you own the edits: bootstrap copies `NODE_FILES` into `~/.telar-boot/` with one scp of the four files, then `chmod`; keep the rest intact), `tests/test_orchestrate.py`, `tests/test_secrets.py`, `tests/test_remote.py` (only the bootstrap tests)

**`telar.secrets`**
- API keys are 32 random bytes, hex, stored in `%APPDATA%\telar\secrets\<name>` on Windows, protected with **DPAPI** (`CryptProtectData` via ctypes, user scope), or `~/.config/telar/secrets` 0600 on POSIX.
- `get(name)`, `ensure(name)`, `path_for_router(name)` (the decrypted key written to a 0600 file under `%LOCALAPPDATA%\telar\run\` only while the router runs, deleted at stop), and `rotate(name)`.

**`telar.orchestrate`** (all node work goes through `telar.remote.Remote.call`):
- `install(cluster, build_machine, targets)`: bootstrap telar-node on every machine of the profile, `install` on the build machine (poll `job`), `push-release` to the others, then return the 12-character release id. The recipe comes from `versions.lock` `toolchains.hip_configure`; components include pinned TensorMeld source, which is fetched without building it.
- `up(cluster, profile, router="local"|machine_id, nodes=[...], devices=[...])`:
  - validates that every declared machine is reachable (doctor) **before** any lease;
  - ensures keys (`put-secret` on each node, from `telar.secrets`);
  - starts the leases (qwen-replicas: one `single` lease per node with label `tlr-qwen-<node>-<rand4>`; ds41-pair: one `pair` lease on rank 0's node, with `layout_required: "1"` and rank 1 as a `peer` process);
  - waits for `ready`;
  - starts the router: `local` = `procs.LocalChild` running `<abs python> -m tensormeld serve-replicas <rendered>`; `<machine>` = a `kind: none` lease on that machine running the router from `<root>/src/tensormeld-<commit>/src` via `PYTHONPATH`, with listen on its private address and a key file;
  - writes `%LOCALAPPDATA%\telar\run\up-<profile>.json` (lease ids, router).
  - A start failure stops everything it started.
- `down(profile)`: stop the router first, then `stop` every lease (and `cancel` queued ones). `status(profile)` merges node `status` with router `/api/status`.

**CLI**
- `telar install PROFILE [--build-on MACHINE]`
- `telar up PROFILE [--router local|MACHINE] [--node ID ...] [--device MACHINE:DEVICE ...] [--deadline S] [--approval-ref TEXT --approval-lease S --approval-end ISO]`
- `telar down PROFILE`
- `telar status [PROFILE]`
- `telar logs PROFILE PROCESS [--tail N]`
- `telar keys rotate NAME`
- `--node`/`--device` default to the profile's machines. A declared machine that is not in the config, or is unreachable, fails before any lease.

## Integration notes (worker I)

- The Windows installer installs telar with `pip install -e <checkout>` so node scripts, `versions.lock`, and pinned component sources stay available. `telar.paths.repo_root()` resolves the package parent once and explains missing repository resources.
- Release IDs are exactly 12 lowercase hex characters in config, node records, rendered paths, and orchestration.
- POSIX port probes use `SO_REUSEADDR` so a stopped router's TIME_WAIT connections do not block restart; Windows keeps `SO_EXCLUSIVEADDRUSE`.
- Node launches use Python's `start_new_session=True` (the POSIX `setsid` operation), without relying on a `setsid` executable. Single turns remain `<lab>/gpu-turn.sh <label> <cmd...>`; pairs use `<lab>/gpu-turn-pair.sh` on rank 0. Ambient/spec `GPU_TURN_MAX` cannot set the cap; only a validated single-turn approval can.
- Pair specs carry the peer's own lab directory. DeepSeek rank 1 emits READY but has no HTTP server: readiness checks the pair API on rank 0, and only rank 0 receives an HTTP drain. Rank-1 logs are fetched from its own node. Peer-run enforces the deadline even while its stdin channel stays open.
- Tests use fake tools and loopback listeners. `tests/test_verb_contract.py` checks orchestration calls against both node modules and the transport allow-list.

**Tests:** a fake `Remote` recording calls; up/down ordering; failure rollback; router local vs remote rendering; the DPAPI round trip on Windows (skipped elsewhere); the bootstrap copying all `NODE_FILES`.
