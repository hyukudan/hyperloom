# telar

`telar` is the integration and control layer that weaves unlike machines into one inference cluster: a control machine (typically Windows with a discrete GPU) and one or more Strix Halo nodes.

It pulls pinned versions of three components and drives them through their existing interfaces:

- [TensorMeld](https://github.com/hyukudan/tensormeld): router, planner, benchmarks.
- The Strix Halo engine ([strix-halo-engine-lab](https://github.com/hyukudan/strix-halo-engine-lab)): HIP serve apps and the CUDA prefill service.
- [strix-trellis](https://github.com/hyukudan/strix-trellis).

No component code is copied here. Gaps are filed as requests in each component's own repository.

**Status:** design approved; Phase 0 (skeleton) in progress. Private repository.

## What it gives
- **On the control machine:** the `telar` CLI and a local web portal. The portal lets you add machines, install, start/stop profiles, set capacity parameters, and see devices, links, live statistics and logs.
- **On each node:** `telar-node`, which builds the pinned engine and runs it under a supervisor. In lab mode the supervisor runs inside the lab's GPU-turn queue.

## Documents
- [docs/SPEC.md](docs/SPEC.md): the product specification, with phases, acceptance tests and component requests.
- [docs/ui/UI-SPEC.md](docs/ui/UI-SPEC.md): the portal design. Mockups are in `docs/ui/mockups/` and images in `docs/ui/img/`, all with sample data.

## Rules for this repository
- Never commit IPs, hostnames, user names, passwords or keys. All of these are configured at install time, outside the repo.
- Do not change production defaults or numerics of any component.
- Do not name other inference engines.
- Commits use the author `hyukudan <19479239+hyukudan@users.noreply.github.com>`.
