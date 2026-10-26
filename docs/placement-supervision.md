Placement supervision (set 7)
=============================

The service helper accepts an optional `ServiceSpec.placement_supervision` policy. It contains operator `grants`,
one `caps_gib` entry per granted UUID, aggregate `host_pinned_gib` and `host_pageable_gib`, the operator `host_grant`,
and `free_commit_floor_gib`. UUID keys are compared case-insensitively. Caps above grants are refused without
clamping. Fixed operator window boundaries are rechecked throughout the process lifetime; no window is opened.

Before spawn, pinned plus pageable must fit the host grant and available physical memory; MemAvailable must be at
least 82 GB, and the charge must leave the configured commit floor. Runtime checks retain the legacy physical-RAM
floor/strike cadence and additionally enforce every device and host grant deadline. Each device lock is acquired
in normalized UUID order before any gate runs. Any lock, gate, identity or start failure releases the held locks
in reverse order. Each grant has its own gate; the legacy single gate cannot replace these gates.

Placement counters are attributable bytes, including context memory. Windows uses the English PDH
`GPU Process Memory(*)/Dedicated Usage` counter and maps adapter LUIDs to UUIDs using NVML. Linux uses NVML compute
and graphics process lists and deduplicates a PID appearing in both. Missing mappings, N/A, invalid samples and
observer failures refuse/stop the process. Activity on an ungranted adapter stops it. Device-total subtraction is
never used for a placement cap. Device totals remain available for passive identity/capacity/idle checks.

Legacy rendering, visibility checks, watchdog sampling and its fallback remain unchanged when the policy is absent.
Placement visibility/fidelity evidence uses attributable counters. The placement adapter remains disabled pending
the later release, admission and rendering steps. Engine checkpoint/accounting validation remains authoritative.

CPU-only tests: `python -m pytest -q tests/test_placement_supervision.py tests/test_rtx.py tests/test_no_window.py`.
Native driver behavior is not qualified by these fake-counter/process tests. Operational policy documents and
watchdog logs contain local identities and belong in local runtime storage, never in committed fixtures.
