"""Operator-owned placement grants. Checking a grant never opens a window or runs its gate."""
from __future__ import annotations

import time
import re

from ..config import ConfigError, number, record, text
from .. import pc_big, rtx
from .reader import RecipeError, placement_mode

HOST_MAX_GIB = 74
HOST_START_BYTES = 82_000_000_000


def uuid_key(value):
    return value.strip().lower()


def parse_window(value, where):
    window = record(value, where, ("starts_at", "expires_at", "kind"))
    start = rtx.expires(window["starts_at"], where + ".starts_at")
    end = rtx.expires(window["expires_at"], where + ".expires_at")
    if end <= start:
        raise ConfigError(f"{where}: expires_at must follow starts_at")
    if window["kind"] not in ("PC-BIG", "device"):
        raise ConfigError(f"{where}.kind: expected PC-BIG or device")
    return window


def parse_grants(value, where="pc.cuda.grants"):
    if not isinstance(value, list) or len(value) > 128:
        raise ConfigError(f"{where}: expected a list of at most 128 grants")
    seen = set()
    for i, grant in enumerate(value):
        at = f"{where}[{i}]"
        record(grant, at, ("uuid", "vram_cap_gib", "approval", "window", "gate"))
        key = uuid_key(text(grant["uuid"], at + ".uuid"))
        if not re.fullmatch(r"gpu-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", key):
            raise ConfigError(f"{at}.uuid: expected a CUDA GPU UUID")
        if key in seen:
            raise ConfigError(f"{at}.uuid: duplicate grant")
        seen.add(key)
        number(grant["vram_cap_gib"], at + ".vram_cap_gib", 0, 1024)
        text(grant["approval"], at + ".approval")
        parse_window(grant["window"], at + ".window")
        rtx.gate(grant["gate"], at + ".gate")
    return value


def parse_host_grant(value):
    at = "pc.cuda.host_grant"
    record(value, at, ("host_gib", "approval", "window"))
    number(value["host_gib"], at + ".host_gib", 0, HOST_MAX_GIB)
    text(value["approval"], at + ".approval")
    parse_window(value["window"], at + ".window")
    return value


def _active(window, now, *, starting=False):
    return (rtx.expires(window["starts_at"], "window.starts_at") <= now
            and rtx.expires(window["expires_at"], "window.expires_at") > now + (60 if starting else 0))


def pc_big_deadline(cluster, selected, *, now):
    """Earliest live or scheduled PC-BIG conflict, selected by configured policy rather than model name."""
    try:
        guard = pc_big.context(cluster, selected)
        if guard is None:
            return None
        pc_big.require_clear(guard)
    except rtx.ServiceError as exc:
        raise RecipeError("PL_E_GRANT " + str(exc)) from exc
    cuda = cluster.pc.get("cuda", {})
    windows = [g["window"] for g in cuda.get("grants", [])]
    if cuda.get("host_grant"):
        windows.append(cuda["host_grant"]["window"])
    starts = [max(now, rtx.expires(w["starts_at"], "PC-BIG starts_at")) for w in windows
              if w["kind"] == "PC-BIG" and rtx.expires(w["expires_at"], "PC-BIG expires_at") > now]
    reservation = cuda.get("reservation")
    if reservation and ("window_s" in reservation or rtx.reservation_end(reservation, now) > now):
        starts.append(now)
    return min(starts) if starts else None


def check(cluster, recipe, binding, report, hosts, *, now=None, touched=None, anchors_only=False):
    """Validate every enabled route's anchor and nonzero assists. Return the grants used, without clamping.

    Tierstream supplies binding (including alias merging and strict selectors). Inactive assists consume no grant.
    The operator gate, locks and runtime counters belong to supervision, not this passive preflight.
    """
    now = time.time() if now is None else now
    cuda = cluster.pc.get("cuda", {})
    grants = parse_grants(cuda.get("grants", []))
    by_uuid = {uuid_key(g["uuid"]): g for g in grants}
    rows = {row["id"]: row for row in report["devices"]}
    host_grant = cuda.get("host_grant")
    if host_grant is not None:
        parse_host_grant(host_grant)
    touched = None if touched is None else {uuid_key(u) for u in touched}
    used = {}
    for route in recipe["routes"]:
        if not route.get("enabled", True) or not placement_mode(route):
            continue
        budgets = {sid: route["placement"][sid]["device_gib"] for sid in route["devices"]}
        if not anchors_only:
            budgets.update({sid: a["device_gib"] for sid, a in route.get("assist", {}).items()
                            if a["device_gib"] > 0 and binding.get(sid)})
        for sid, budget in budgets.items():
            members = binding.get(sid, [])
            if not members:
                raise RecipeError(f"PL_E_UNKNOWN_DEVICE {sid}: enabled device is unbound")
            for member in members:
                row = rows[member["id"]]
                if touched is not None and uuid_key(row.get("uuid", "")) not in touched:
                    continue
                if row["kind"] != "cuda":
                    raise RecipeError(f"PL_E_CAPABILITY {sid}: placement grants currently support CUDA only")
                uuid = row.get("uuid")
                grant = by_uuid.get(uuid_key(uuid)) if uuid else None
                if grant is None:
                    raise RecipeError(f"PL_E_GRANT {sid}: no operator grant for the selected UUID")
                if not _active(grant["window"], now, starting=True):
                    raise RecipeError(f"PL_E_GRANT {sid}: grant is not open or expires within 60 s")
                if budget > grant["vram_cap_gib"]:
                    raise RecipeError(f"PL_E_GRANT {sid}: device_gib {budget:g} above grant {grant['vram_cap_gib']:g}")
                used[uuid_key(uuid)] = grant
        for sid in route["devices"]:
            placement = route["placement"][sid]
            host = placement["host_pinned_gib"] + placement.get("host_pageable_gib", 0)
            if placement["host_pinned_gib"] > HOST_MAX_GIB:
                raise RecipeError("PL_E_GRANT pinned host budget exceeds 74 GiB")
            if host > 0 and (host_grant is None or not _active(host_grant["window"], now, starting=True)
                             or host > host_grant["host_gib"]):
                raise RecipeError("PL_E_GRANT missing, expired or insufficient host grant")
            for member in binding[sid]:
                free = hosts.get(member["host"], {}).get("free_ram_gib")
                if free is None or free * (1 << 30) < HOST_START_BYTES:
                    raise RecipeError("PL_E_GRANT MemAvailable must be at least 82 GB before start")
    if touched is not None and used.keys() != touched:
        raise RecipeError("PL_E_GRANT core touched a device without an enabled, bound grant")
    deadline = pc_big_deadline(cluster, [row for row in rows.values()
                                       if uuid_key(row.get("uuid", "")) in used], now=now)
    ends = [rtx.expires(g["window"]["expires_at"], "grant expires_at") for g in used.values()]
    if host_grant is not None:
        ends.append(rtx.expires(host_grant["window"]["expires_at"], "host grant expires_at"))
    if deadline is not None and ends and deadline < min(ends):
        raise RecipeError("PL_E_GRANT selected device turn may not overlap a PC-BIG window")
    return used


def policy(cluster, recipe, route, binding, report, plan, hosts, *, now):
    """Carry Tierstream's integer budgets and the complete operator-window constraint into supervision."""
    devices = plan.get("devices", [])
    used = check(cluster, recipe, binding, report, hosts, now=now, touched=[d["uuid"] for d in devices])
    budgets = {sid: route["placement"][sid]["device_gib"] for sid in route["devices"]}
    budgets.update({sid: a["device_gib"] for sid, a in route.get("assist", {}).items()})
    host = plan["host"]
    return {"grants": list(used.values()),
            "caps_gib": {uuid_key(d["uuid"]): budgets[d["set"]] for d in devices},
            "caps_bytes": {uuid_key(d["uuid"]): d["budget_bytes"] for d in devices},
            "host_pinned_gib": host["pinned_budget_bytes"] / rtx.GIB,
            "host_pageable_gib": host["pageable_budget_bytes"] / rtx.GIB,
            "host_grant": cluster.pc.get("cuda", {}).get("host_grant"),
            "free_commit_floor_gib": max(4, recipe["limits"]["host_free_commit_floor_gib"]),
            "pc_big_deadline": pc_big_deadline(cluster, devices, now=now),
            "pc_big_guard": pc_big.context(cluster, devices)}
