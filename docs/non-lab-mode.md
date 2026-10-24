# Non-lab mode: engines as `systemd --user` units (optional, off by default)

Lab machines run GPU work only through bounded leases (`gpu-turn.sh` / `gpu-turn-pair.sh`, SPEC §2.6). Non-lab
mode is for machines you own outright, where no turn scripts exist and serving should outlive a 20-minute turn.
It wraps the **same** node `supervise` in a user unit, with no turn and no deadline.

## When it is allowed
- Every machine of the profile sets `"lab_mode": false` in `cluster.json` (the default is `true`). The control
  machine refuses otherwise, before any remote call.
- The node checks again and fails closed. It refuses unless the root's `etc/install-policy.json` exists and records
  `"lab_dir": null` explicitly (a root installed by `hyperloom install` with `lab_mode: false`), when a request carries
  a lab directory, and when lab turn files (`gpu-turn.sh`, `gpu-turn-pair.sh`, `.gpu-turn`) exist in `$HOME`, the
  install root's parent directory, a direct subdirectory of either, or a `PATH` directory. `service stop`, `status`
  and `remove` stay available so a unit can always be taken down.
- Leases and units never share a root:
  - a GPU lease (`single`/`pair`, or any lease with a lab directory) and a pair peer refuse while an
    `etc/service-<profile>.json` exists (an enabled unit would start again), and every lease refuses while a unit's
    lease record is live; `hl-svc-` labels are reserved for units;
  - `hyperloom install` with a lab directory refuses on a root with an installed unit, and `uninstall` refuses while
    one exists (its spec references the root's releases): run `hyperloom service remove` first;
  - a unit start (`service start`, and the unit itself at every (re)start) refuses while any lease record of the root
    is live or turn files are found.
- Only `qwen-replicas`. The DeepSeek pair needs a peer turn on the second node and stays lease-only.
- Profile names must match `[a-z0-9-]{1,40}` (they name the unit `hl-<profile>.service`).

## Commands
```
hyperloom install qwen-replicas                        # as usual: builds and activates a release
hyperloom service install qwen-replicas [--enable]     # render, push keys, write the unit on every node
hyperloom service start   qwen-replicas
hyperloom service status  qwen-replicas                # systemd state + the latest supervise record
hyperloom service stop    qwen-replicas                # drain/stop through supervise, waits up to 120 s
hyperloom service remove  qwen-replicas                # refuses while the unit is active
```
Every operation runs on every profile node and reports each node's result (or error); the command exits 1 when any
node failed, after trying the others.

`install` runs the same pre-launch checks as `up`, through one shared helper: the deployed node tool must match this
checkout, the profile's assets must pass the strict inspection, every node must run the same installed release (the
error names each node's release id), then each node gets its key file. It renders the profile exactly like `up` (same
preset, release id and per-node key files; no raw env, the numerical identity is not editable) and sends the process
list to each node. The node:
1. validates it with the lease validator (`kind: none`, label `hl-svc-<profile>`; reserved and loader env names are
   still rejected) and requires immutable `releases/<id>/bin/` executables and local processes only;
2. copies `hyperloom-node`, `hyperloom_lease.py` and `hyperloom_service.py` into `<root>/bin/`, writes the spec to
   `<root>/etc/service-<profile>.json`, and writes `~/.config/systemd/user/hl-<profile>.service` atomically;
3. runs `systemctl --user daemon-reload` (and `enable` with `--enable`) and reports what `is-enabled` says: a
   reinstall without `--enable` keeps an earlier enable. Nothing else is changed: no root, no system units, no
   lingering.

`start` and `stop` are queued with `systemctl --user ... --no-block`. `stop` then polls each node's status until the
unit is `inactive` or `failed`, for up to 120 s (more than `TimeoutStopSec=90`). `remove` first checks that a user
manager answers (otherwise `disable` could not remove the `default.target.wants` link), refuses while the unit is
active, then disables it, deletes the unit file and spec, reloads and runs `reset-failed`.

## The unit
The content is fixed by code; only the validated profile id and plain absolute paths (`[A-Za-z0-9_./-]`) are
substituted, and it holds no secrets (keys stay in `<root>/etc/keys`, mode 0600).
- `ExecStart=<python3> <root>/bin/hyperloom_service.py service-run <spec>`: publishes a lease record owned by the
  unit's process and runs `supervise` in the foreground (logs in `<root>/log/`, records in `<root>/run/`). The
  interpreter is the node's `sys.executable` as invoked (for example `/usr/bin/python3`), not its resolved versioned
  file.
- `KillMode=mixed`: `systemctl stop` sends SIGTERM to `supervise` only, which drains (DeepSeek `/admin/drain`) or
  terminates its process groups with the usual grace period; after `TimeoutStopSec=90` systemd kills whatever is
  left in the control group.
- Exit codes and restarts (`Restart=on-failure`, `RestartSec=10`):
  - a requested stop exits 0 and is not restarted;
  - an engine exit exits 1 and **is restarted, also after the engine was ready**;
  - a readiness timeout exits 3 and a refusal (lab files, a live lease, a bad spec) exits 4; neither is restarted
    (`RestartPreventExitStatus=3 4`), since the next attempt would only hold the GPU for another 600 s or refuse again;
  - at most 3 starts in 7200 s (`StartLimitBurst=3`, `StartLimitIntervalSec=7200`, well above three 600 s readiness
    windows plus restart delays), after which systemd leaves the unit failed.
- "No deadline" is a 10-year supervise cutoff.

While a unit runs, its lease record is active, so `activate` and `uninstall` refuse as they do during a lease.

A unit's lease record can also be stopped through the lease API (`stop` with its lease id). That is a requested stop:
the unit exits 0 and stays **inactive** (it is not restarted) until `hyperloom service start`.

Records left behind when the unit process dies without writing its result (SIGKILL, OOM kill, power loss) never block
the root: a record whose process is gone or whose boot id differs no longer counts as live for leases, `activate`
and `uninstall` (lease starts also rewrite it as failed), and each unit start (and `status`/`remove` of an inactive unit) marks the unit's
earlier unfinished records failed, since systemd runs one instance per unit. Each start keeps the newest 5 finished
records of the unit and deletes older ones with their spec, owner and log files.

## Your own steps (never run by hyperloom)
- A user manager must be running for `systemctl --user` (log in, or `loginctl enable-linger <user>` if you want the
  units to run without a session; that is your decision and needs no hyperloom code).
- The router is not part of the unit. Start it on the control machine with `hyperloom router start --profile
  <profile>` and a `router.replica_api_key_files` entry per node, or keep using `hyperloom up` on lab machines.
