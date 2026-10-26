"""Configured placement candidates; Tierstream owns selectors, aliases and required/optional assignment."""
from copy import deepcopy

from ..device_policy import resolve

from .grants import uuid_key
from .reader import RecipeError

IDENTITY = ("uuid", "pci", "arch", "platform", "memory_bytes", "driver", "host_ram_gib",
            "disk_read_gbytes_ps", "h2d_gbytes_ps", "pci_path", "link", "via", "numa_node")


def candidates(cluster, ctx, inventory, facts):
    observed = {uuid_key(g["uuid"]): g for g in inventory.get("cuda", [])}
    rows, ids = [], set()
    for mid in dict.fromkeys([ctx.pc, *ctx.nodes]):
        machine = cluster.machines[mid]
        for device in machine.devices:
            if resolve(device, machine.devices, placement=True)["excluded"]:
                continue
            kind = "cuda" if device.get("type") in ("nvidia", "cuda") else "hip"
            if kind == "cuda":
                uuid = device.get("uuid")
                if not uuid:
                    raise RecipeError("PL_E_UNKNOWN_DEVICE configured CUDA candidates need a UUID")
                live = observed.get(uuid_key(uuid)) if mid == ctx.pc else None
                ident = uuid
            else:
                ident, live = mid, facts["devices"].get(mid)
            item = facts["devices"].get(ident, {})
            present = live is not None and item.get("present", True)
            values = dict(device)
            if live is not None:
                values.update(live)
                values.update(item)
            probe_id = ident if ident not in ids else f"{ident}:{device['id']}"
            ids.add(probe_id)
            row = {"id": probe_id, "host": mid, "kind": kind, "present": bool(present),
                   "name": values.get("name") or device["id"], "memory_gib": values.get("memory_gib") or 0}
            for key in IDENTITY:
                if values.get(key) is not None:
                    row[key] = deepcopy(values[key])
            rows.append(row)
    report = {"model": facts["model"], "devices": rows, "links": deepcopy(facts.get("links", []))}
    report.update({key: deepcopy(facts.get(key, "unknown")) for key in ("xfer_host", "file_store", "engram_store")})
    for key in ("engine_sha256", "model_sha256"):
        if facts.get(key):
            report[key] = facts[key]
    return report


def bound(recipe, ctx, binding):
    """Adapt the core's assignment to orchestration roles without choosing a different device."""
    roles, why = {}, {}
    executing = {sid for route in recipe["routes"] for sid in route["devices"]}
    for device in recipe["match"]["devices"]:
        sid = device["id"]
        members = binding.get(sid, [])
        if sid in executing:
            roles[sid] = "decode"
        elif members and any(sid in route.get("assist", {}) for route in recipe["routes"]):
            roles[sid] = "assist"
        else:
            roles[sid] = "idle"
            why[sid] = "device set is unbound or unused"
    return {"binding": binding, "roles": roles, "why": why}
