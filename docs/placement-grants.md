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
other than the derived PRO identities, independent of the observed model name. An explicit false is an operator policy
choice; it does not grant a turn or remove that device's lock, idle check or gate. A conflicting device is refused
when the PRO's existing `pcbig-<uuid>.lock` is held, `rtx.pro_busy` reports activity, or the ledger has any open
PC-BIG start (including stale starts). The ledger path comes from `pc.turn_ledger` in the cluster configuration,
captured in the turn's guard. The guard reads no portal setting or telemetry database. Each signal is checked before
measurements/admission and again throughout supervision; partial ledger lines and unreadable configured signals
refuse. An absent `pc.turn_ledger` configures no ledger signal. PRO identities come from `pc.cuda.pro_uuid`, configured local
prefill-service profile devices and bound placement anchors. Every derived PRO is checked. A conflicting probe
without any reliable PRO identity refuses `PL_E_GRANT`; omitting the optional UUID never disables live checks.
A configured ledger that disappears or becomes unreadable refuses; it cannot silently disable supervision.
Checks never acquire the PRO lock or write the ledger or settings. The ledger path is captured when a turn is admitted: adding `pc.turn_ledger` to the configuration during a running turn takes effect at the next admission.

After acquiring all selected locks, a placement helper skips the lock check for its own PROs. A legacy PC-BIG
helper cannot acquire the same lock until the turn releases it. PRO lock acquisition preserves the configured UUID
spelling, including on case-sensitive filesystems; normalized comparison keys never select a different lock file.
The guard therefore checks compute processes on
those PROs and excludes only the helper and child PIDs. Assist process rows never trigger this PRO guard. WDDM
desktop rows with N/A memory alone are ignored. Foreign numeric PRO use above the idle threshold refuses, as does
PRO device memory above the bounded attribution for the turn's own PIDs. Each device-total read is bracketed by
two attributable snapshots. Windows collects all owned PIDs together; the observer's existing NVML session supplies
the total by UUID, with nvidia-smi as fallback. Assuming attribution is monotonic per PID within each bracket,
the sum of each PID's larger sample bounds foreign activity from below; the sum of the smaller samples bounds it
from above. Ambiguous brackets are resampled immediately, up to three brackets. Admission/start refuse one busy
verdict; the watchdog requires two consecutive busy brackets within a check. Growth-only ambiguity receives a
watchdog load allowance for the first 60 seconds of full checks, shortened by the ready timeout, without adding
or resetting strikes. It never restarts; release/mixed ambiguity and growth after that limit remain unavailable
counters. An invalid total (negative, non-integer, or more than one MiB below the smaller attribution) also refuses.
Non-monotonic spikes/dips and WDDM total/attribution ordering remain open live-qualification points, described in
placement-supervision.md. This subtraction applies only to PC-BIG detection; placement caps still cover the full
attributable process usage.
The guard retains the ledger check and refuses unreadable state. The watchdog checks unowned PRO locks on each
full tick (about two seconds), with ledger and GPU queries at five-second intervals, independently of the
half-second host-memory checks. Unavailable GPU counters receive three consecutive attempts; admission refuses the
first failure. Lock failures refuse on the next full tick and ledger failures on the next ledger check.
Per-process placement cap/attribution failures still stop immediately.

Planned replacement may passively account for a reviewed old service's lock and child PID only while its recorded
ready state, PID, exact device set and held helper lifetime lock prove ownership. Foreign PRO processes and ledger
windows still refuse. The later device idle check additionally subtracts only that reviewed child's attributable
memory using the same brackets. One observer is shared by all admission checks and pre-stop launch/RTX rendering,
then closed on success or refusal. Verified owners pass transiently through rendering and validation.
This pre-stop ownership is never written into the replacement policy: after the controller stops the old
service, the replacement must pass fresh unowned checks and acquire every device lock before starting. A stale
review, mismatched state or unreleased old lock cannot authorize a new helper.

Ambiguous configuration (multiple device records matching one physical card) is excluded in both portal views
and refused by placement/measurement selection. The legacy unconfigured-4090 exclusion remains in
`config.DEFAULT_DEVICE_POLICIES`: it requires a physical identity match before a configured policy may override
exclusion. A same-name card cannot inherit another card's flags. Other classes retain their legacy name fallback.
This default affects legacy inventory/views; explicitly configured placement candidates keep their existing
non-excluded, UUID-bound selection rule. G6 argv/environment/release/refusal bytes remain unchanged.
