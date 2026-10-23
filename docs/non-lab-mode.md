# Non-lab mode: engines as `systemd --user` units (optional, off by default)

Lab machines run GPU work only through bounded leases (`gpu-turn.sh` / `gpu-turn-pair.sh`, SPEC §2.6). Non-lab
mode is for machines you own outright, where no turn scripts exist and serving should outlive a 20-minute turn.
It wraps the **same** node `supervise` in a user unit, with no turn and no deadline.

## When it is allowed
- Every machine of the profile sets `"lab_mode": false` in `cluster.json` (the default is `true`). The control
  machine refuses otherwise, before any remote call.
- The node refuses as well when the install root was installed in lab mode (`etc/install-policy.json` records a
  `lab_dir`) or when a request carries a lab directory.
- Only `qwen-replicas`. The DeepSeek pair needs a peer turn on the second node and stays lease-only.
- Profile names must match `[a-z0-9-]{1,40}` (they name the unit `hl-<profile>.service`).

## Commands
```
hyperloom install qwen-replicas                        # as usual: builds and activates a release
hyperloom service install qwen-replicas [--enable]     # render, push keys, write the unit on every node
hyperloom service start   qwen-replicas
hyperloom service status  qwen-replicas                # systemd state + the latest supervise record
hyperloom service stop    qwen-replicas                # drain/stop through supervise
hyperloom service remove  qwen-replicas                # refuses while the unit is active
```
`install` renders the profile exactly like `up` (same preset, release id and per-node key files; no raw env, the
numerical identity is not editable) and sends the process list to each node. The node:
1. validates it with the lease validator (`kind: none`, label `hl-svc-<profile>`; reserved and loader env names are
   still rejected) and requires immutable `releases/<id>/bin/` executables and local processes only;
2. copies `hyperloom-node`, `hyperloom_lease.py` and `hyperloom_service.py` into `<root>/bin/`, writes the spec to
   `<root>/etc/service-<profile>.json`, and writes `~/.config/systemd/user/hl-<profile>.service` atomically;
3. runs `systemctl --user daemon-reload` (and `enable` with `--enable`). Nothing else is changed: no root, no system
   units, no lingering.

## The unit
The content is fixed by code; only the validated profile id and plain absolute paths (`[A-Za-z0-9_./-]`) are
substituted, and it holds no secrets (keys stay in `<root>/etc/keys`, mode 0600).
- `ExecStart=<python3> <root>/bin/hyperloom_service.py service-run <spec>`: publishes a lease record owned by the
  unit's process and runs `supervise` in the foreground (logs in `<root>/log/`, records in `<root>/run/`).
- `KillMode=mixed`: `systemctl stop` sends SIGTERM to `supervise` only, which drains (DeepSeek `/admin/drain`) or
  terminates its process groups with the usual grace period; after `TimeoutStopSec=90` systemd kills whatever is
  left in the control group.
- `Restart=on-failure`, `RestartSec=10`, at most 3 starts in 600 s. A requested stop exits 0; an engine exit or a
  readiness timeout exits 1 and restarts.
- "No deadline" is a 10-year supervise cutoff.

While a unit runs, its lease record is active, so `activate` and `uninstall` refuse as they do during a lease.

## Your own steps (never run by hyperloom)
- A user manager must be running for `systemctl --user` (log in, or `loginctl enable-linger <user>` if you want the
  units to run without a session; that is your decision and needs no hyperloom code).
- The router is not part of the unit. Start it on the control machine with `hyperloom router start --profile
  <profile>` and a `router.replica_api_key_files` entry per node, or keep using `hyperloom up` on lab machines.
