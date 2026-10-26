"""Passive PC-BIG guard shared by placement admission, measurements and their watchdogs."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sqlite3
import time

from . import downloads, gpu_counters, procs, rtx


def context(cluster, selected, *, anchors=()):
    """Snapshot topology, not activity. Preferences and all live signals are reread on every check."""
    from .recipes import service
    cuda = cluster.pc.get("cuda", {})
    # Keep configured spelling for legacy lock filenames (case-sensitive on Linux), compare owners by UUID key.
    pros = {str(u).lower(): str(u) for u in anchors if u}
    configured_pros = set()
    if cuda.get("pro_uuid"):
        pros[cuda["pro_uuid"].lower()] = cuda["pro_uuid"]
        configured_pros.add(cuda["pro_uuid"].lower())
    for profile in getattr(cluster, "profiles", {}).values():
        service_device = profile.get("prefill_service", {})
        machine = getattr(cluster, "machines", {}).get(service_device.get("machine"))
        if getattr(machine, "access", None) == "local":
            matches = [d for d in machine.devices if d.get("id") == service_device.get("device")]
            if len(matches) != 1 or not matches[0].get("uuid"):
                raise rtx.ServiceError("PC-BIG service device identity is unavailable")
            pros[matches[0]["uuid"].lower()] = matches[0]["uuid"]
            configured_pros.add(matches[0]["uuid"].lower())
    declared = [d for m in getattr(cluster, "machines", {}).values()
                if getattr(m, "access", None) == "local" for d in getattr(m, "devices", ())]
    for row in selected:
        uuid = str(row.get("uuid", "")).lower()
        matches = [d for d in declared if str(d.get("uuid", "")).lower() == uuid]
        if len(matches) > 1:
            raise rtx.ServiceError("PC-BIG device policy is ambiguous")
        default = uuid not in (configured_pros or pros)
        conflict = matches[0].get("pc_big_conflict", default) if matches else default
        if conflict:
            if not pros:
                raise rtx.ServiceError("PC-BIG PRO identity is unavailable; turn refused")
            return {"pro_uuids": [pros[u] for u in sorted(pros)],
                    "preferences": str(service.config_dir() / "telemetry.sqlite3")}
    return None


class CounterUnavailable(rtx.ServiceError):
    """Live GPU query failures and incoherent totals receive bounded watchdog retries."""


def _gpu_query(query, *args):
    try:
        return query(*args)
    except (OSError, ValueError, RuntimeError, IndexError) as exc:
        raise CounterUnavailable("PC-BIG GPU counters unavailable; turn refused") from exc


def owned_busy(uuid, pids, observer):
    """Bracket the device total with per-PID attribution; ambiguous intervals never prove idle."""
    key = uuid.lower()
    pids = sorted(pids)
    def attributed():
        result = {}
        for pid in pids:
            sample = observer.sample(pid)
            if key not in sample or type(sample[key]) is not int or sample[key] < 0:
                raise gpu_counters.CounterError("owned PRO attributable counters unavailable")
            result[pid] = sample[key]
        return result
    for _ in range(3):
        before = attributed()
        used = _gpu_query(rtx.device_used_mib, uuid)
        if type(used) is not int or used < 0:
            raise CounterUnavailable("PC-BIG device total is invalid; turn refused")
        after = attributed()
        hi = sum(max(before[pid], after[pid]) for pid in pids)
        lo = sum(min(before[pid], after[pid]) for pid in pids)
        total = used * (1 << 20)
        # The device counter reports whole MiB; tolerate at most one MiB of rounding below attribution.
        if total + (1 << 20) < lo:
            raise CounterUnavailable("PC-BIG device total is below attribution; turn refused")
        if total - hi > rtx.PRO_IDLE_MIB * (1 << 20):
            return True
        if total - lo <= rtx.PRO_IDLE_MIB * (1 << 20):
            return False
    raise CounterUnavailable("PC-BIG attribution changed across device totals; turn refused")


def active(guard, *, owners=None, observer=None, observer_factory=None, locks_only=False):
    """Held PRO lock, busy PRO or any open ledger start is a window. Unknown state refuses.

    Missing optional preferences means no ledger signal is configured. Existing but unreadable
    preferences, lock state, ledger bytes or busy counters are errors, never an idle result.
    """
    if not guard:
        return False
    try:
        owners = {u.lower(): set(pids) for u, pids in (owners or {}).items()}
        pros = guard.get("pro_uuids") or ([guard["pro_uuid"]] if guard.get("pro_uuid") else [])
        pros = {pro.lower(): pro for pro in pros}
        for pro in pros:
            if pro not in owners and procs.lock_held(rtx.device_lock_path(pros[pro])):
                return True
        if locks_only:
            return False
        # Ledger/lock failures remain immediate even if GPU queries are temporarily unavailable.
        if _ledger_active(guard):
            return True
        owned_pros = pros.keys() & owners.keys()
        with ExitStack() as stack:
            if owned_pros:
                apps = _gpu_query(rtx.compute_apps)
                for gpu, pid, used in apps:
                    key = gpu.lower()
                    # WDDM desktop N/A rows alone are not jobs; device residual catches unattributed use.
                    if (key in owned_pros and pid not in owners[key] and used is not None
                            and used > rtx.PRO_IDLE_MIB):
                        return True
                if observer is None:
                    observer = (observer_factory or gpu_counters.Observer)()
                    stack.callback(observer.close)
                for pro in sorted(owned_pros):
                    if owned_busy(pros[pro], owners[pro], observer):
                        return True
            for pro in pros:
                if pro not in owners and _gpu_query(rtx.pro_busy, pros[pro]):
                    return True
        return False
    except CounterUnavailable:
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, IndexError) as exc:
        raise rtx.ServiceError("PC-BIG state unreadable; turn refused") from exc


def _ledger_active(guard):
    preferences = Path(guard["preferences"])
    try:
        preferences.stat()
    except FileNotFoundError:
        if guard.get("preferences_required"):
            raise ValueError("known preferences are unavailable")
        return False
    guard["preferences_required"] = True
    from .telemetry.store import Store
    store = Store(preferences, readonly=True)
    try:
        path = downloads._pref(store, downloads.LEDGER_PREF)
    finally:
        store.close()
    if path is None:
        if guard.get("ledger_required"):
            raise ValueError("known ledger preference is unavailable")
        return False
    if not isinstance(path, str) or not path:
        raise ValueError("invalid ledger preference")
    guard["ledger_required"] = True
    # Reuse the legacy whole-file reader and complete-line parser. Unlike the portal's tail view, this
    # retains old open starts regardless of their age; an incomplete line also fails closed.
    ledger = downloads.LedgerGuard(preferences.parent, path)
    _, data = ledger._read(time.monotonic() + ledger.BUDGET_S)
    text = data.decode("utf-8", "strict")
    _, partial = ledger.open_starts(data)
    from .web.features import turns
    now = time.time()
    state = turns.ledger_state(turns.parse_ledger(text, now), now, turns.LEDGER_CAP_DEFAULT_S)
    return bool(state["holders"] or state["stale"] or partial)


def require_clear(guard, *, owners=None, observer=None, observer_factory=None, locks_only=False):
    if active(guard, owners=owners, observer=observer, observer_factory=observer_factory, locks_only=locks_only):
        raise rtx.ServiceError("PC-BIG window is active; turn refused")
