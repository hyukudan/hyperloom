Placement admission uses operator-written local configuration. The portal has no grant writer. Legacy recipes
continue using `pc.cuda.reservation` and their existing device binding.

`pc.cuda.grants` is an array with one entry per UUID. Each entry requires `uuid`, `vram_cap_gib`, `approval`,
`window`, and `gate` (the existing absolute executable plus arguments syntax). Duplicate UUIDs, including case
variants, are invalid. A window has UTC `starts_at` and `expires_at` timestamps (`YYYY-MM-DDTHH:MM:SSZ`) and
`kind` (`PC-BIG` or `device`). These are ledger window boundaries supplied by the operator; Hyperloom does not open
or extend them. Admission requires more than 60 seconds remaining. Supervision will execute each gate under its
device lock; passive planning does not execute gates.

`pc.cuda.host_grant` requires `host_gib`, `approval`, and `window` in the same format. It covers pinned plus pageable
host budgets and cannot exceed 74 GiB. Admission also requires MemAvailable of at least 82,000,000,000 bytes.
Device budgets above the selected UUID's grant are refused without changing the recipe. A selected 4090 requires
its own grant and cannot work while any configured PC-BIG grant, host grant or legacy reservation is active.
A relative legacy reservation is conservatively treated as active because only its operator gate knows the slot.

Placement probes list all configured, non-excluded candidates with `present`, including configured devices missing
from the passive inventory. CUDA configuration requires a UUID; optional `name`, `memory_gib`, `pci`, `arch`, and
`driver` describe an absent candidate so Tierstream can distinguish configured absence from a typo. Live inventory
overrides those metadata fields. Unconfigured observed GPUs are omitted. No name-based exclusion applies to this
report. Tierstream performs strict selector validation, alias merging, PCI ordering, required assignment, and then
optional assignment. Missing compatible core support refuses placement; there is no Python binding fallback.

Placement launch requires the published opt-in decode release; the shipped null pin still refuses. Grant checks
cover the core's touched devices: an absent optional assist consumes no grant even with a positive configured budget.
An explicit destination requiring that assist is Tierstream's refusal. A 4090 turn whose lifetime would overlap
any configured PC-BIG window (including a future one) is refused. The earliest conflict is also carried into supervision.
These checks do not enable the legacy prefill service to consume placement-mode recipes. All operational UUIDs, paths, grants
and probe documents belong in local configuration/runtime storage; committed examples use portable identities.

`machines[].devices[].pc_big_conflict` is an optional boolean policy flag. It defaults to true for local devices
other than `pc.cuda.pro_uuid`, independent of the observed model name. An explicit false is an operator policy
choice; it does not grant a turn or remove that device's lock, idle check or gate. A conflicting device is refused
when the PRO's existing `pcbig-<uuid>.lock` is held, `rtx.pro_busy` reports activity, or the ledger has any open
PC-BIG start (including stale starts). The ledger path is the existing `prefs` / `turns/ledger_path` preference
in the configuration directory's telemetry store. Each signal is checked before measurements/admission and again
throughout supervision; partial ledger lines and unreadable configured signals refuse. An absent optional
preference store/path configures no ledger signal. `pc.cuda.pro_uuid` identifies the PRO lock and busy sensor.
Once observed, a preference store or ledger setting cannot disappear and silently disable supervision.
Checks never acquire the PRO lock or write the ledger/preferences.

Ambiguous configuration (multiple device records matching one physical card) is excluded in both portal views
and refused by placement/measurement selection. The legacy unconfigured-4090 exclusion remains in
`config.DEFAULT_DEVICE_POLICIES`: it requires a physical identity match before a configured policy may override
exclusion. A same-name card cannot inherit another card's flags. Other classes retain their legacy name fallback.
This default affects legacy inventory/views; explicitly configured placement candidates keep their existing
non-excluded, UUID-bound selection rule. G6 argv/environment/release/refusal bytes remain unchanged.
