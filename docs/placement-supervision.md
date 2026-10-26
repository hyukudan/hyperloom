Placement supervision (set 7)
=============================

The service helper accepts an optional `ServiceSpec.placement_supervision` policy. It contains operator `grants`,
one `caps_gib` entry and a core-produced integer `caps_bytes` entry per granted UUID, aggregate `host_pinned_gib` and `host_pageable_gib`, the operator `host_grant`,
and `free_commit_floor_gib`. UUID keys are compared case-insensitively. Caps above grants are refused without
clamping. VRAM comparisons use the integer bytes without converting GiB in Python. Fixed operator window boundaries
and the earliest configured PC-BIG conflict are rechecked throughout the process lifetime; no window is opened.
The live PC-BIG guard also rereads the PRO lock/busy signal and configured ledger at watchdog cadence.
An open or partial ledger record, held lock, busy PRO or unreadable signal stops the owned child.

Before spawn, pinned plus pageable must fit the host grant and available physical memory; MemAvailable must be at
least 82 GB, and the charge must leave the configured commit floor. Runtime checks retain the legacy physical-RAM
floor/strike cadence and additionally enforce every device and host grant deadline. Each device lock is acquired
in normalized UUID order before any gate runs. Any lock, gate, identity or start failure releases the held locks
in reverse order. Each grant has its own gate; the legacy single gate cannot replace these gates.

Placement counters are attributable bytes, including context memory. Windows uses the English PDH
`GPU Process Memory(*)/Dedicated Usage` counter. DXGI adapter descriptions supply LUIDs; read-only D3DKMT
adapter-address queries supply PCI addresses, and NVML's PCI lookup supplies the corresponding UUIDs. No nvcuda
load or cuInit occurs in this observer or in preflight. Enumeration order is never an identity. Missing, duplicate,
zero or incomplete mappings refuse admission. Runtime observer construction additionally follows every device
lock and operator gate in both the measurement worker's parent and the service helper.
Linux uses NVML compute
and graphics process lists and deduplicates a PID appearing in both. Missing mappings, N/A, invalid samples and
observer failures refuse/stop the process. Activity on an ungranted adapter stops it. Device-total subtraction is
never used for a placement cap. Device totals remain available for passive identity/capacity/idle checks.

Legacy rendering, visibility checks, watchdog sampling and its fallback remain unchanged when the policy is absent.
Placement visibility/fidelity evidence uses attributable counters. The complete opt-in decode launch path remains
guarded by the shipped unpublished release pin. Engine checkpoint/accounting validation remains authoritative.

CPU-only tests: `python -m pytest -q tests/test_placement_supervision.py tests/test_rtx.py tests/test_no_window.py`.
Native driver behavior is not qualified by these fake-counter/process tests. Operational policy documents and
watchdog logs contain local identities and belong in local runtime storage, never in committed fixtures.

Owned PC-BIG residual checks use two attribution snapshots around a device total. The observer reads the total
in-process through its existing NVML session by UUID. NVML v2 `used` excludes driver/firmware-reserved memory;
the v1 total includes it and is never substituted. Windows takes all owned PIDs from one PDH collection on
each side. Observers without native totals, or unavailable NVML v2 totals, retain the nvidia-smi fallback. Placement
caps always use full attributable process bytes. Admission/start refuse a single busy verdict; the watchdog
requires two consecutive busy brackets in the same check, so a temporary allocation seen only in one total
does not stop a normal turn. Numeric foreign-process evidence, locks and ledgers retain their immediate checks.

The residual bounds assume attribution is monotonic per PID within each bracket. An allocation/free spike can
produce a false busy verdict; a release/reallocation dip can conceal foreign use for that check. Two busy
brackets reduce transient refusals but do not establish that assumption. Live qualification must exercise both
patterns under loading and serving workloads. This remains an open qualification point.

When all three brackets are ambiguous and every owned PID's after sample is at least its before sample, the
watchdog reports own load in progress. It adds no counter strike only while each ambiguous device's owned
attribution high bound has grown by at least 1 GiB since the previous full check, the replica has not passed its
ready event and health check, and its ready timeout has not elapsed since watchdog construction. The first
ambiguous check without a previous high bound receives a strike. Readiness ends the allowance permanently;
stalled or sub-GiB growth, release-shaped ambiguity and mixed changes receive the usual counter strikes.
Measurement probes explicitly have zero load grace. Admission/start receive no load allowance. Caps, grants,
deadlines and all other devices remain checked during loading. Ambiguity logs include the high bounds and the
maximum measured bracket width in seconds for live timing qualification.

Qualifying progress preserves earlier strikes: with two already recorded, the first later strike can stop the
child immediately. During qualifying ambiguous growth, foreign N/A use up to the
idle threshold plus own growth within a bracket can remain hidden; detection can be delayed until progress stalls,
readiness succeeds or the ready timeout ends,
then stop via "counters failed persistently" rather than "window is active". Larger residuals and numeric foreign
rows still refuse immediately. Consecutive qualifying checks consume at least 1 GiB of high-bound headroom on
each ambiguous device and remain bounded by its cap and ready timeout.

Live WDDM qualification also remains open for the total below attribution refusal: under PRO load, compare the
sum of owned PIDs' PDH Dedicated Usage with NVML memory.used (and the nvidia-smi fallback). Establish whether the
sum stays within the device total plus the one MiB rounding tolerance, including shared/aliased allocations and
driver accounting. Persistent disagreement still refuses; CPU fakes do not qualify this relationship.
Also verify NVML v2 used == nvidia-smi memory.used at idle and under load on the qualified driver.

Pure admission inspects every touched device's existing OS lock without acquiring it and checks the selected local
port before any admission hold, published placement, launch state or helper directory. A missing lock creates no
file or directory; an unlocked stale lock is allowed. A held/unreadable lock or occupied port is `PL_E_BUSY`.
Windows reads the mandatorily locked byte; Linux reads the flock entries in `/proc/locks`. The socket check closes immediately. The helper still takes its lifetime device locks and
the owned child repeats the port check, so races after admission cannot bypass the launch checks.

A reviewed deploy switch may credit the old child's measured private resident host bytes, bounded by its private
commit, to the local host's physical/commit checks before shutdown. The reviewed controller, matching ready PID
and devices, and held helper lock must prove ownership. Each PID earns credit once; budgets, shared/file-backed
pages and unknown counters earn none. Credit changes only the hypothetical admission sample. After stopping the
reviewed set, deploy gathers fresh physical RAM and commit without credit before calling up; the helper repeats
the checks before spawn. Foreign pressure that still crosses any floor refuses before progress or pins.

CLI up (including snapshot inputs), CLI deploy and direct deploy share passive approval, deadline and queue-timeout
validation before admission holds, progress, snapshot publication or pins. Snapshot launches validate the saved
intent. Approval parsing is shared too. Deploy checks again under its snapshot pin, and up checks at launch so an
approval expiring after passive admission cannot bypass the later checks.

The earlier CUDA-LUID implementation has been replaced by DXGI/D3DKMT; its historical CUDA export audit is
not a qualification of this observer. CPU ABI fakes exercise the NVML PCI/UUID join and adapter queries without
loading a CUDA driver. Live WDDM driver behavior remains a separate qualification prerequisite.
