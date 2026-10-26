Placement supervision (set 7)
=============================

The service helper accepts an optional `ServiceSpec.placement_supervision` policy. It contains operator `grants`,
one `caps_gib` entry and a core-produced integer `caps_bytes` entry per granted UUID, aggregate `host_pinned_gib` and `host_pageable_gib`, the operator `host_grant`,
and `free_commit_floor_gib`. UUID keys are compared case-insensitively. Caps above grants are refused without
clamping. VRAM comparisons use the integer bytes without converting GiB in Python. Fixed operator window boundaries
and the earliest configured PC-BIG conflict are rechecked throughout the process lifetime; no window is opened.

Before spawn, pinned plus pageable must fit the host grant and available physical memory; MemAvailable must be at
least 82 GB, and the charge must leave the configured commit floor. Runtime checks retain the legacy physical-RAM
floor/strike cadence and additionally enforce every device and host grant deadline. Each device lock is acquired
in normalized UUID order before any gate runs. Any lock, gate, identity or start failure releases the held locks
in reverse order. Each grant has its own gate; the legacy single gate cannot replace these gates.

Placement counters are attributable bytes, including context memory. Windows uses the English PDH
`GPU Process Memory(*)/Dedicated Usage` counter. A context-free CUDA driver inventory supplies LUIDs and PCI bus ids;
NVML's PCI lookup supplies the corresponding UUIDs. Enumeration order is never an identity. Missing, duplicate,
zero-mask or incomplete mappings refuse admission, including a CUDA visibility filter hiding an NVML adapter.
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

Pure admission checks every touched device's existing OS lock with `create=False` and checks the selected local
port before any admission hold, published placement, launch state or helper directory. A missing lock creates no
file or directory; an unlocked stale lock is allowed. A held/unreadable lock or occupied port is `PL_E_BUSY`.
Checks release their temporary lock/socket immediately. The helper still takes its lifetime device locks and
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

Static ABI verification (round-2 finding 15): the installed CUDA 13.0 `cuda.h` declares `cuInit`, `cuDeviceGetCount`,
`cuDeviceGet`, `cuDeviceGetLuid(char *, unsigned int *, CUdevice)` and
`cuDeviceGetPCIBusId(char *, int, CUdevice)`; all five occur in the installed `nvcuda.dll` export table (769 names).
`nvml.h` declares `nvmlDeviceGetHandleByPciBusId_v2(const char *, nvmlDevice_t *)` and
`nvmlDeviceGetUUID(nvmlDevice_t, char *, unsigned int)`; these and the inventory/process APIs used here occur in
`nvml.dll` (430 names). Neither the header nor that DLL exports `nvmlDeviceGetLuid`.
Verification parsed DLL bytes and header text without loading a driver. ABI fakes use `spec_set` restricted to
these exported names. The Windows mapper calls only driver initialization and device identity APIs, never a
context or a kernel. Live WDDM behavior still requires separate qualification.
