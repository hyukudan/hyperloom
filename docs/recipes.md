# Recipes in hyperloom

Status: `hl/recipes`, 2026-10-24. Unit-tested on Windows and WSL; not yet run on the cluster.

A **recipe** (strix-stream `recipe-v1`, documented in strix-stream's `docs/recipes.md`) says how one model runs on
one set of hardware: which hardware it is for (`match`), which devices serve a request by workload (`routes`), where
each route keeps the weights (`placement`), and what to do when a route cannot run (`fallback`, `limits`). Exactness
is never a recipe option: every route computes the same bits.

hyperloom applies a recipe to a profile. strix-stream's recipe core owns the rules (schema, layers, match, select,
plan). hyperloom probes the hardware, binds the recipe to the profile's machines, decides what `up` starts, and has the
router apply the prefill routes to every request.

## Commands

```text
hyperloom up <profile> --recipe FILE|auto [--recipe-profile P] [--recipe-overrides O]... [--recipe-set PATH=VALUE]...
                       [--recipe-workload NAME] [--recipe-dir DIR]...
hyperloom recipe probe    <profile> [--disks] [--descriptors] [--evidence NODE=RECEIPT]... [--out FACTS.json]
hyperloom recipe validate FILE [<profile>] [--facts FACTS.json] [layers]
hyperloom recipe explain  FILE [<profile>] [--facts FACTS.json] [layers] [--all] [--prompt-tokens N]
                          [--unavailable DEVICE_ID]... [--link-down LINK]... [--free-ram HOST=GIB]...
hyperloom recipe diff     A B [--a-set PATH=VALUE]... [--b-set PATH=VALUE]...
hyperloom recipe pick     <profile> [--dir DIR]... [--facts FACTS.json] [layers]
```

- **validate** checks the recipe and its layers, and with a profile also the hardware match and what `up` would
  decide. It exits with 1 and lists every reason when something is refused.
- **explain** lists every field that is not at its default, with its value, the layer it came from (`recipe`,
  `profile`, `override`) and the values it replaced. With a profile it adds the binding, the availability, the `up`
  decision and, with `--prompt-tokens`, the route a request of that length would get. `--unavailable`,
  `--link-down` and `--free-ram` simulate a busy device, a link that is down or low RAM.
- **diff** compares the two *effective* recipes field by field, after their layers are applied. Comments and key
  order do not count as differences; route order does.
- **pick** shows what `up --recipe auto` would choose and why every other candidate was not chosen. A candidate can
  be invalid, for another model, or refused with its reason. Two files that use the same recipe name are both refused.
  Among the recipes that fit, the most specific one wins (the most hardware minimums), and ties go to the name.
- **probe** prints the facts that are checked. It reads nothing from a model file unless asked: `--disks` measures
  the disk read speed of the service's model file and `--descriptors` saves the model's file headers for planning.
  `--out` saves the facts so `validate`, `explain` and `pick` can run offline with `--facts`.

`--recipe-workload` names the workload class the run serves. A profile that was tuned for a different class is
stale. A stale profile is not applied: the run uses the recipe alone and is marked `untuned`.

## What `up --recipe` does, in order

1. **Resolve** the recipe with its layers: the autotuner profile first, then override files, then `--recipe-set`.
   This runs through the strix-stream core (`ss-recipe`: `pc.recipe_core`, `HYPERLOOM_SS_RECIPE` or `ss-recipe` on
   `PATH`). The core's effective document, hardware match and route selection are used throughout. A stale profile
   is dropped and the run is marked `untuned`. Without the core, `up --recipe` is refused unless
   `HYPERLOOM_RECIPE_STUB=1` is set; that uses hyperloom's offline reader and records the run as not production.
   `up --recipe` needs the local router, because the router reads the route state written on the control machine.
2. **Probe and bind.** The hip set binds to the profile's nodes in rank order. The cuda set binds to the profile's
   prefill-service GPU (by UUID). The probe report contains exactly those devices, so the core can only match the
   hardware that `up` launches.
3. **Match** capabilities: the model, device names and memory, host RAM, disk read speed and link speed. Every unmet
   requirement is listed, for example `match.devices.rtx: GPU-… (NVIDIA GeForce RTX 4090, 23.6 GiB): name does not
   contain "RTX PRO 6000"; memory 23.60 GiB < 94`.
4. **Decide**, before any lease or process starts:
   - **Decode.** Exactly one decode configuration is deployed. Its execution must equal the profile's topology (TP
     for a pair, replicas for replicas). A recipe that picks a different decode route depending on the workload is
     refused: split it into one recipe per configuration.
   - **Prefill routes.** A route either runs on the decode processes (local) or offloads to the RTX prefill service
     and hands the state off to the decode route. Offloading is allowed only for engines with the parked-import
     contract (`import-state-parked`: an imported state waits in a free slot and only a prompt that starts with its
     tokens takes it).
   - **Admission at `up`** (core §7). A device set must be available, its links up, and its RAM must fit. Each member
     is charged hyperloom's conservative cap model: the pinned tier, the staging ring, the process overhead and, on
     unified-memory nodes, the whole device budget. The charge must leave `free_ram_gib` above the recipe's floor.
     The RTX service's charge must also fit within free commit minus the reservation's commit floor.
   - **Fallbacks.** If the RTX PRO is busy, the reservation is missing or about to expire, the PC-to-node link is
     down, or RAM is low, the offload route is not admissible. Every prefill then stays on the nodes: no PRO lease
     is taken and the service is not started. The reason is recorded.
   - **Launch mapping.** Every placement field maps to a launch flag. A field that is not at its default and has no
     flag is refused, never dropped. The RTX service takes `device_gib` as its VRAM cap and `host_pinned_gib` as its
     host budget. Both are always forwarded, zero included; a budget above what this start allows is refused, not
     clamped. The service is **planned now, before any lease**, from saved header descriptors; if the planner refuses,
     every prefill stays on the nodes.
   - **Bounds.** Each route's `max_context_tokens` and `max_batch` must fit what the launch allocates. The service
     runs one prompt at a time, up to its positions. The nodes run up to their engine's context and concurrent
     sequences: `--context`/`--slots` for DeepSeek (engine defaults 4096/4), `--context`/`--http-batch` for Qwen,
     `--capacity`/`--slots` for MiniMax, profile overrides included. A bound hyperloom cannot resolve is refused.
     Routes that run on the same processes must have the same placement.
5. **After the node engines are ready** (their allocations are done), each node's free RAM, read with the node tool's
   `gpu` verb, must still be above the recipe's floor. If not, `up` fails and the leases are stopped.
6. **Recheck right before the service starts:** the PRO is idle, the reservation is valid, and free RAM and commit
   are above their floors. If any check fails, the service is skipped and the nodes keep every prefill.
7. **After the service is ready** (its allocations are done), RAM and commit are measured again. If either is below
   its floor, the service is stopped and the nodes keep every prefill.
8. **Router.** Every recipe launch gets `prefill.policy = "routes"` and the route table
   (`tensormeld/prefill-route-table-v1`). When the service is not started, its offload routes are compiled disabled,
   so node-only runs still get the recipe's context bounds and RAM floors on every request. The local router helper
   rewrites `route-state.json` every second, from measurements only:
   - the PRO is available while the service is ready and the reservation lasts;
   - a node is available while it has answered the `gpu` verb within the last 10 s (it is polled every 3 s, and its
     free RAM is recorded) and the router reports its replica up;
   - the PC-to-node link and the pair's own link are up while the router reports the replica up;
   - the control machine's free RAM and commit are measured each time.

   A measurement that is missing or too old fails closed.

`hyperloom status` shows the recipe under `recipe`: the file and its sha256, the reader, the effective sha256, the
binding, the match, availability, the decision, and the service decision with its reason.

## Request time: the router's route table

The TensorMeld router (`policy: "routes"`, `tensormeld/prefill_routes.py`) evaluates the table on every generation
request:

- **Prompt length.** The prompt is tokenized by the replica, which applies its chat template. The prompt-length
  thresholds apply to the tokens the prefill computes: the prompt minus the chosen replica's cached prefix. A request
  whose length cannot be determined is refused with HTTP 400: tokenizing failed, or the request has more than one
  prompt.
- **Context.** The context is the prompt plus the request's `max_tokens`, or the engine's default generation budget
  when the request does not set one. It must fit the route's `max_context_tokens`.
- **Selection.** The routes are walked in order and disabled routes are skipped. A route that matches but cannot be
  admitted follows its `fallback` chain; a fallback target must match too. A route whose process is already running
  is charged nothing, but its hosts must still be above the RAM floor (and the commit floor, when the recipe sets
  one). If nothing can be admitted, the request is refused with HTTP 503 and the trace.
- **Availability.** The state file is ANDed with the router's own health: the service must be up and the decode
  replica must be up. A state file that is missing, unreadable or more than 10 s old makes every route inadmissible,
  so selection fails closed.
- **Hand-off.** The RTX service slot is reserved with a hard 30 s deadline (`wait_s`). If the hand-off fails, the
  walk continues at the route's fallback for that request. Imports are parked by token prefix, so a late import
  cannot change any request's tokens.
- **Status.** `/api/status` → `prefill_routes` counts the requests admitted per route, the refusals with the last
  refusal's trace, and the failed hand-offs.

Decode is not switched per request: a running request keeps its route.

## Probes

| fact | cuda (control machine) | hip (each node, ssh `gpu` / `doctor`) |
|---|---|---|
| name, memory | nvidia-smi `name`, `memory.total` | DRM `product_name`; GTT total (the GPU-mappable pool) |
| host RAM, free RAM | GlobalMemoryStatusEx / `/proc/meminfo` | node `ram` |
| disk read | `recipe probe --disks`: sequential direct I/O (`FILE_FLAG_NO_BUFFERING` / `O_DIRECT`), ≤ 256 MiB, ≤ 2 s. A buffered read is not accepted as evidence. | a saved receipt (`--evidence NODE=FILE`: JSON with `disk_read_gbytes_ps`, `method: "direct"`, `host`, `path` naming that node and its model file). It is used only for that model path, and only for 7 days. |
| links | — | PC–node: the node answered and has a physical NIC that is up (negotiated speed); a link whose speed is unknown is not reported. Pair: both nodes report the USB4 layout and an openable stream. USB4 bandwidth is not probed and is reported as 0 Gb/s, so only `gbps_min = 0` passes. |

Disk results are cached in `<config dir>/run/probe-cache.json` for 7 days. The cache key is the machine, the volume,
the path, the size, the mtime, the method and the request size. Planning reads only saved header descriptors
(`<config dir>/run/descriptors/`). If a descriptor is missing or stale, the run is refused with the command that
refreshes it.

## Requirements and limitations (v1)

- **TensorMeld.** Every recipe launch needs TensorMeld with `prefill.policy = "routes"` (branch
  `router/recipe-routes`). `up --recipe` is refused while the pinned TensorMeld does not have it.
- **Admission charges.** hyperloom's engines do not read strix-stream plans yet, so admission charges each member
  hyperloom's conservative cap model instead of a compiled plan: the pinned budget, the staging ring and the process
  overhead, plus the device budget on unified-memory nodes. The commit charge is the host bytes plus the device bytes
  of a discrete device.
- **Node placements.** No node preset takes a placement from recipes yet. A node placement field that is not at its
  default is refused until the engines read strix-stream plan-v1.
- **Batch on prefill routes.** Batch predicates on prefill routes are refused: the router cannot see how many
  sequences are in a step.
- **MiniMax.** The MiniMax pair binds loopback, so its router must run on rank 0. Because recipes need the local
  router, `up --recipe` does not run it yet. Its offload routes also wait for its serve-side hand-off contract and the
  `import-state-parked` capability.
