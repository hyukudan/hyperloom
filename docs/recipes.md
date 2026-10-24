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
   After the core is installed, `up` requires it (`pc.recipe_core`, `HYPERLOOM_SS_RECIPE` or `ss-recipe` on `PATH`).
   Without it, `up --recipe` is refused unless `HYPERLOOM_RECIPE_STUB=1` is set. That uses hyperloom's offline
   reader and records the run as not production.
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
     exact host budget; a budget above what this start allows is refused, not clamped. Each route's
     `max_context_tokens` and `max_batch` must fit what the launch allocates: the service runs one prompt at a time,
     up to its positions, and the nodes up to their context. Routes that run on the same processes must have the same
     placement.
5. **Recheck right before the service starts:** the PRO is idle, the reservation is valid, and free RAM and commit
   are above their floors. If any check fails, the service is skipped and the nodes keep every prefill.
6. **After the service is ready** (its allocations are done), RAM and commit are measured again. If either is below
   its floor, the service is stopped and the nodes keep every prefill.
7. **Router.** With the service running, the router gets `prefill.policy = "routes"` and the route table
   (`tensormeld/prefill-route-table-v1`). Without the service, it gets `policy = "nodes"`. The local router helper rewrites
   `route-state.json` every second. The file holds whether the PRO is available (the service is ready and the
   reservation lasts), the control machine's free RAM, and the state of the links.

`hyperloom status` shows the recipe under `recipe`: the file and its sha256, the reader, the effective sha256, the
binding, the match, availability, the decision, and the service decision with its reason.

## Request time: the router's route table

The TensorMeld router (`policy: "routes"`, `tensormeld/prefill_routes.py`) evaluates the table on every generation
request:

- **Prompt length.** The prompt is tokenized by the replica, which applies its chat template; there is no cache
  discount. A request whose length cannot be determined is refused with HTTP 400: tokenizing failed, or the request
  has more than one prompt.
- **Context.** The context is the prompt plus the request's `max_tokens`, or the engine's default generation budget
  when the request does not set one. It must fit the route's `max_context_tokens`.
- **Selection.** The routes are walked in order. A route that matches but cannot be admitted follows its `fallback`
  chain; a fallback target must match too. If nothing can be admitted, the request is refused with HTTP 503 and the
  trace.
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
| disk read | `recipe probe --disks`: sequential direct I/O (`FILE_FLAG_NO_BUFFERING` / `O_DIRECT`), ≤ 256 MiB, ≤ 2 s. A buffered read is not accepted as evidence. | a saved receipt (`--evidence NODE=FILE`: JSON with `disk_read_gbytes_ps`, `method: "direct"`, `host`, `path` naming that node and its model file) |
| links | — | PC–node: the node answered and has a physical NIC that is up (negotiated speed). Pair: both nodes report the USB4 layout and an openable stream. USB4 bandwidth is not probed. |

Disk results are cached in `<config dir>/run/probe-cache.json` for 7 days. The cache key is the machine, the volume,
the path, the size, the mtime, the method and the request size. Planning reads only saved header descriptors
(`<config dir>/run/descriptors/`). If a descriptor is missing or stale, the run is refused with the command that
refreshes it.

## Requirements and limitations (v1)

- **TensorMeld.** The offload routes need TensorMeld with `prefill.policy = "routes"` (branch
  `router/recipe-routes`). `up --recipe` refuses a recipe that would start the service when the pinned TensorMeld
  does not have it.
- **Node placements.** No node preset takes a placement from recipes yet. A node placement field that is not at its
  default is refused until the engines read strix-stream plan-v1.
- **Batch on prefill routes.** Batch predicates on prefill routes are refused: the router cannot see how many
  sequences are in a step.
- **MiniMax.** The MiniMax pair does its RTX hand-off inside its serve process. Its offload routes are not admitted
  until that launch contract exists and the family declares `import-state-parked`. Until then its nodes route is
  used.
