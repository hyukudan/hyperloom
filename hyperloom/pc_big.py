"""Passive PC-BIG guard shared by placement admission, measurements and their watchdogs."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from . import downloads, procs, rtx


def context(cluster, selected):
    """Snapshot topology, not activity. Preferences and all live signals are reread on every check."""
    from .recipes import service
    cuda = cluster.pc.get("cuda", {})
    pro = cuda.get("pro_uuid")
    declared = [d for m in getattr(cluster, "machines", {}).values()
                if getattr(m, "access", None) == "local" for d in getattr(m, "devices", ())]
    for row in selected:
        uuid = str(row.get("uuid", "")).lower()
        matches = [d for d in declared if str(d.get("uuid", "")).lower() == uuid]
        if len(matches) > 1:
            raise rtx.ServiceError("PC-BIG device policy is ambiguous")
        default = uuid != str(pro or "").lower()
        conflict = matches[0].get("pc_big_conflict", default) if matches else default
        if conflict:
            return {"pro_uuid": pro, "preferences": str(service.config_dir() / "telemetry.sqlite3")}
    return None


def active(guard):
    """Held PRO lock, busy PRO or any open ledger start is a window. Unknown state refuses.

    Missing optional preferences/PRO configuration means no signal is configured. Existing but unreadable
    preferences, lock state, ledger bytes or busy counters are errors, never an idle result.
    """
    if not guard:
        return False
    try:
        pro = guard.get("pro_uuid")
        if pro:
            if procs.lock_held(rtx.device_lock_path(pro)) or rtx.pro_busy(pro):
                return True
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
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, IndexError) as exc:
        raise rtx.ServiceError("PC-BIG state unreadable; turn refused") from exc


def require_clear(guard):
    if active(guard):
        raise rtx.ServiceError("PC-BIG window is active; turn refused")
