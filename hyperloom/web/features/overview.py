"""Fleet overview (F1): the lab's KPI strip and one card per machine.

GET overview -> {kpis: {throughput, power, memory, generated_24h, machines_online}, machines: [card, ...]}

Built only from what is already collected: the fleet documents in the telemetry store (``fleet/host``,
``fleet/engine``), the local launch records (roles), the store's series and the core snapshot if it is already
cached. A GET never calls a node and never refreshes the snapshot. A distributed engine is counted once, on its
rank 0; every number names its observer and missing values say why.
"""

import math

from hyperloom import telemetry
from hyperloom.web.features import machines as fleet


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _turn_holder(snapshot, snapshot_why, machine_id):
    """The GPU-turn holder from the node data the core snapshot already holds (doctor's lab check)."""
    if snapshot is None:
        return None, snapshot_why
    rows = ((snapshot.get("devices") or {}).get("machines")) or []
    row = next((r for r in rows if isinstance(r, dict) and r.get("id") == machine_id), None)
    if row is None:
        return None, "machine not in the portal snapshot"
    lab = ((((row.get("doctor") or {}).get("checks") or {}).get("lab")) or {})
    if lab.get("status") != "ok":
        return None, lab.get("why") or "lab directory not reported by this machine"
    holder = (lab.get("value") or {}).get("holder") or {}
    if holder.get("status") != "ok":
        return None, holder.get("why") or "no holder file"
    text = str(holder.get("value") or "").strip()
    return (text[:200] or "free"), None


def _tokens_24h(store, now):
    if store is None:
        return None, "telemetry store not open in this portal process"
    if store.descriptor("engine_generated_tokens") is None:
        return None, "needs the engine token counters (observability collector)"
    result = store.query("engine_generated_tokens", now - 86400, now)
    sums = [p[5] for line in result["lines"] for p in line["points"] if _finite(p[5])]
    if not sums:
        return None, "no generated tokens recorded in the last 24 h"
    return sum(sums), None


def card(api, machine, store, now, snapshot, records, control_id):
    snapshot, snapshot_why = snapshot
    view = fleet.machine_view(api, machine, store, now, records, control_id)
    entry, checks = view["entry"], view["checks"]
    role_rows = fleet.roles(records, control_id).get(machine.id, [])
    badges = [{"profile": r["profile"], "role": r["role"]} for r in role_rows]
    if machine.access == "local":
        badges.insert(0, {"profile": None, "role": "portal"})
    holder, holder_why = _turn_holder(snapshot, snapshot_why, machine.id) if machine.access == "ssh" else (
        None, "GPU turns apply to the nodes")
    why = {}
    if holder is None:
        why["turn_holder"] = holder_why
    uptime = fleet._value(checks.get("uptime_s"))
    if uptime is None:
        why["uptime_s"] = fleet._why(checks.get("uptime_s"), view["status_why"] or "not reported")
    gpus = [{k: g.get(k) for k in ("kind", "device", "name", "busy_percent", "power_w", "power_sensor", "power_limit_w",
                                   "throttle", "excluded", "observer")}
            | {"temperature_c": max((t["celsius"] for t in g.get("temperatures") or [] if _finite(t.get("celsius"))), default=None)}
            for g in view["gpus"]]
    if not gpus:
        why["gpus"] = fleet._why(checks.get("gpus"), view["status_why"] or "no GPU reported")
    engines = view["engines"]
    return {"id": machine.id, "os": machine.os, "access": machine.access, "status": view["status"],
            "status_why": view["status_why"], "last_ok": (entry or {}).get("last_ok"), "roles": badges,
            "idle": not role_rows, "uptime_s": uptime, "headroom": {"limiting": view["limiting"],
                                                                     "why": view["limiting_why"],
                                                                     "domains": view["domains"]},
            "gpus": gpus, "engines": engines, "turn_holder": holder,
            "emitted_rate": None, "why": {**why, "emitted_rate": "needs armed engine instrumentation (C2)"}}


def overview(api, query):
    if query:
        raise ValueError("overview takes no query arguments")
    cluster = api.cluster()
    store = telemetry.existing_store(api)
    now = api.clock()
    snapshot = fleet.cached_snapshot(api)
    records = fleet.launch_records(api)
    control = fleet.control_machine(cluster)
    control_id = control.id if control else None
    cards = [card(api, machine, store, now, snapshot, records, control_id) for machine in cluster.machines.values()]
    why = {}
    if store is None:
        why["machines"] = "telemetry store not open in this portal process (samplers disabled)"

    # Completed-token throughput: every engine once (rank 1 rows carry none).
    rates = [(c["id"], e["profile"], e["throughput"]) for c in cards for e in c["engines"] if _finite(e.get("throughput"))]
    serving = [e for c in cards for e in c["engines"] if e["role"] in ("rank 0", "replica")]
    throughput = {"value": sum(r[2] for r in rates) if rates else None, "unit": "tok/s",
                  "label": "completed tok/s · engine counters", "observer": "engine",
                  "engines": len(rates), "measured": True}
    if not rates:
        throughput["why"] = "no engine is serving" if not serving else "engine counters not reported yet"
    elif len(rates) < len(serving):
        throughput["partial"] = f"{len(rates)} of {len(serving)} serving engines report a counter"

    # Lab power: disjoint sensor domains only, each named; what is not covered is listed.
    covered, total = [], 0.0
    for c in cards:
        if c["status"] != "online":
            continue
        for g in c["gpus"]:
            if _finite(g.get("power_w")):
                domain = "GPU package (PPT)" if g.get("power_sensor") == "PPT" else (
                    "board" if g.get("kind") == "nvidia" else "GPU " + str(g.get("power_sensor")))
                covered.append({"machine": c["id"], "device": g.get("device"), "domain": domain, "watts": g["power_w"]})
                total += g["power_w"]
    power = {"value": total if covered else None, "unit": "W", "covered": covered, "observer": "node and control machine sensors",
             "label": "sensor readings, not metered",
             "not_covered": ["boards, fans, disks and power supplies", "the control machine's CPU and the rest of its host"]}
    if not covered:
        power["why"] = "no fresh power sensor reading"

    # Memory per domain, never merged.
    memory = {}
    for c in cards:
        if c["status"] != "online":
            continue
        for d in c["headroom"]["domains"]:
            if d.get("excluded") or not (_finite(d.get("used_bytes")) and _finite(d.get("total_bytes"))):
                continue
            row = memory.setdefault(d["domain"], {"domain": d["domain"], "used_bytes": 0, "total_bytes": 0, "machines": []})
            row["used_bytes"] += d["used_bytes"]
            row["total_bytes"] += d["total_bytes"]
            if c["id"] not in row["machines"]:
                row["machines"].append(c["id"])
    labels = {"system": "System", "gpu-visible": "GPU-visible (VRAM + GTT)", "vram": "RTX VRAM", "commit": "Windows commit"}
    memory_rows = [dict(row, label=labels.get(name, name)) for name, row in memory.items()]

    tokens, tokens_why = _tokens_24h(store, now)
    online = sum(c["status"] == "online" for c in cards)
    kpis = {"throughput": throughput, "power": power,
            "memory": {"domains": memory_rows, "observer": "node and control machine",
                       **({} if memory_rows else {"why": "no fresh memory reading"})},
            "generated_24h": {"value": tokens, "unit": "tokens", "label": "generated tokens, last 24 h", "observer": "engine",
                           **({"why": tokens_why} if tokens is None else {})},
            "machines_online": {"value": online, "total": len(cards), "observer": "fleet collectors"}}
    return api.document("overview", kpis=kpis, machines=cards, snapshot_cached=snapshot[0] is not None,
                        thresholds=fleet.thresholds(store), why=why)


def register(reg):
    reg.get("overview", overview)
