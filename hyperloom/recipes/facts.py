"""Gather the facts a recipe is checked against, for one profile: its context (model, topology, machines, service),
the device inventory, capability facts (for match) and availability (for admission).

Every remote call goes through the node tool's existing read-only verbs (`gpu`, `doctor`); local facts come from
nvidia-smi and the OS. Disk bandwidth is read from the probe cache unless the operator asked for a measurement.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time

from .. import engines, render, rtx
from ..config import ConfigError
from . import apply, probe

# The model id each engine family serves (equals `match.model` of its recipes) and the asset that holds the model.
FAMILY_MODELS = {"qwen-replicas": ("qwen3.8-flash-next", "model"),
                 "ds41-pair": ("deepseek-v4.1-flash", "ds41_model"),
                 "minimax-pair": ("minimax-m3.1-flash", "mm_model")}
FAMILY_MODELS_BY_ID = {model: asset for model, asset in FAMILY_MODELS.values()}
DEFAULT_HOST_OVERHEAD_GIB = 4.0
# The decode engine's per-sequence context and its concurrent sequences: (flag, engine default or None = unknown).
# Defaults read from the engine at the pinned commit (strix-deepseek41-serve: context 4096, slots 4,
# apps/deepseek41_serve_main.cpp:139); a family whose default hyperloom does not know refuses a recipe unless the
# preset or the profile sets the flag.
# The generation budget the decode engine assumes for a request without max_tokens (flag, engine default or None).
FAMILY_GENERATION = {"ds41-pair": ("--default-max-tokens", 1024),     # apps/deepseek41_serve_main.cpp:141
                     "qwen-replicas": ("--http-default-max-tokens", None),
                     "minimax-pair": ("--max-tokens", None)}
FAMILY_BOUNDS = {"ds41-pair": (("--context", 4096), ("--slots", 4)),
                 "qwen-replicas": (("--context", None), ("--http-batch", None)),
                 "minimax-pair": (("--capacity", None), ("--slots", None))}
EVIDENCE_SCHEMA_KEYS = {"disk_read_gbytes_ps", "method", "host", "path"}


def control_machine(cluster):
    local = [m for m in cluster.machines.values() if m.access == "local"]
    if len(local) != 1:
        raise ConfigError("the cluster needs exactly one local (control) machine")
    return local[0].id


def context(cluster, profile):
    """apply.Context for a profile (no I/O)."""
    if profile not in cluster.profiles:
        raise ConfigError(f"unknown profile: {profile}")
    settings = cluster.profiles[profile]
    kind = render.family(settings.get("preset", profile))
    if kind not in FAMILY_MODELS:
        raise ConfigError(f"profile {profile}: family {kind} has no recipe model id")
    adapter = engines.adapter(kind)
    service = None
    if "prefill_service" in settings:
        s, data, _ = rtx._service(cluster, profile)
        machine = cluster.machines[s["machine"]]
        device = next(d for d in machine.devices if d["id"] == s["device"])
        service = {"machine": s["machine"], "device": s["device"], "uuid": device["uuid"],
                   "engine": (data.get("placement") or {}).get("engine"),
                   "host_overhead_gib": data.get("host_overhead_gib", DEFAULT_HOST_OVERHEAD_GIB),
                   "model_asset": (data.get("placement") or {}).get("model_asset")}
    data = render.preset(settings.get("preset", profile))
    nodes, pc = engines.members(settings, kind), control_machine(cluster)
    (context_flag, context_default), (batch_flag, batch_default) = FAMILY_BOUNDS[kind]
    limits = {"node_context": _flag_value(settings, data, context_flag, context_default),
              "node_batch": _flag_value(settings, data, batch_flag, batch_default),
              "node_flags": (context_flag, batch_flag),
              "default_generation_tokens": _flag_value(settings, data, *FAMILY_GENERATION[kind]),
              "commit_floor_gib": ((cluster.pc.get("cuda") or {}).get("reservation") or {}).get("free_commit_floor_gib", 4)}
    if service is not None:
        _, service_data, _ = rtx._service(cluster, profile)
        limits["service_positions"] = settings["prefill_service"].get("positions", service_data["positions"]["default"])
        rule = service_data["decode_profiles"][kind]["context"]
        limits["default_generation_tokens"] = rtx._decode_flag(cluster, profile, rule["reserve_flag"], rule["reserve_default"])
    known = {mid: _descriptor(cluster, mid, FAMILY_MODELS[kind][1]) for mid in nodes}
    if service is not None:
        known[service["uuid"]] = _descriptor(cluster, pc, service.get("model_asset") or "")
    return apply.Context(profile=profile, model=FAMILY_MODELS[kind][0], topology=adapter.topology, nodes=nodes, pc=pc,
                         capabilities=adapter.capabilities, service=service,
                         node_overhead_gib=data.get("host_overhead_gib", DEFAULT_HOST_OVERHEAD_GIB),
                         descriptors=known, limits=limits)


def _flag_value(settings, data, flag, default):
    """A decode-engine bound as launched: the profile override, else the preset's argv, else the engine default;
    None when none of them is known (a recipe bound is then refused, never assumed)."""
    value = settings.get("overrides", {}).get(flag)
    argv = data.get("argv", [])
    if value is None and flag in argv:
        value = argv[argv.index(flag) + 1]
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return None


def _descriptor(cluster, machine, asset_name):
    """The identity of the model files a member loads: the manifest's sha256 and path of its model asset."""
    asset = cluster.assets.get(machine, {}).get(asset_name)
    if asset is None:
        return "0" * 64
    return hashlib.sha256(f"{asset.get('sha256')}|{asset.get('path')}".encode()).hexdigest()


def _ok(result):
    return isinstance(result, dict)


def gather(cluster, ctx, *, call, gpus=None, memory=None, pro_busy=None, cache=None, measure_disks=False,
           measurer=None, now=None):
    """(inventory, facts, availability, notes). ``call(machine_id, verb)`` runs a node verb and returns its result or
    raises; any failure is an unavailable fact with its reason, never an invented value."""
    now = time.time() if now is None else now
    gpus, memory = gpus or probe.nvidia_gpus, memory or probe.host_memory
    pro_busy, measurer = pro_busy or rtx.pro_busy, measurer or probe.read_bandwidth
    notes = []
    try:
        inventory = {"cuda": gpus()}
    except probe.ProbeError as exc:
        inventory = {"cuda": []}
        notes.append(f"nvidia-smi: {exc}")
    pc_mem = memory()
    devices, avail_devices, links, avail_links = {}, {}, [], {}
    hosts = {ctx.pc: {"free_ram_gib": pc_mem["free_gib"], "free_commit_gib": pc_mem["free_commit_gib"]}}
    pc_assets = cluster.assets.get(ctx.pc, {})
    for gpu in inventory["cuda"]:
        row = {"name": gpu["name"], "memory_gib": gpu["memory_gib"], "host_ram_gib": pc_mem["total_gib"]}
        if ctx.service is not None and gpu["uuid"] == ctx.service["uuid"]:
            asset = pc_assets.get(ctx.service.get("model_asset") or "")
            if asset is not None and cache is not None:
                path = asset.get("control_path", asset["path"])
                disk = probe.disk_fact(cache, ctx.pc, path, probe.file_stat(path), measure_disks,
                                       lambda: measurer(path))
                if disk["value"] is not None:
                    row["disk_read_gbytes_ps"] = disk["value"]
                else:
                    notes.append(f"{ctx.pc} disk: {disk['why']}")
            busy = None
            try:
                busy = pro_busy(gpu["uuid"])
            except rtx.ServiceError as exc:
                busy = str(exc)
            avail_devices[gpu["uuid"]] = ({"available": True} if busy is None else
                                          {"available": False, "why": "RTX PRO busy: " + busy})
            reason = _reservation_problem(cluster, ctx, now)
            if reason is not None and avail_devices[gpu["uuid"]]["available"]:
                avail_devices[gpu["uuid"]] = {"available": False, "why": reason}
        else:
            avail_devices[gpu["uuid"]] = {"available": False, "why": "not the profile's prefill device"}
        devices[gpu["uuid"]] = row
    lan, usb4 = {}, {}
    for mid in ctx.nodes:
        try:
            gpu_result = call(mid, "gpu")
            doctor_result = call(mid, "doctor")
        except Exception as exc:  # any remote failure is the node being unavailable
            devices[mid] = {"name": None, "memory_gib": None}
            avail_devices[mid] = {"available": False, "why": f"node unreachable: {exc}"}
            hosts[mid] = {"free_ram_gib": None}
            lan[mid] = {"up": False, "why": f"node unreachable: {exc}"}
            continue
        node = probe.node_device(gpu_result)
        devices[mid] = {"name": node["name"]["value"], "memory_gib": _gtt(gpu_result),
                        "host_ram_gib": node["host_ram_gib"]["value"]}
        model = cluster.assets.get(mid, {}).get(FAMILY_MODELS_BY_ID.get(ctx.model, ""), {}).get("path")
        evidence = cache.evidence(mid, model, now) if cache is not None and model else None
        if evidence is not None:
            devices[mid]["disk_read_gbytes_ps"] = evidence["disk_read_gbytes_ps"]
        avail_devices[mid] = {"available": True}
        hosts[mid] = {"free_ram_gib": node["host_free_ram_gib"]["value"]}
        lan_fact, usb4_fact = probe.node_links(doctor_result)
        lan[mid] = {"up": lan_fact["up"], "why": lan_fact.get("why"), "gbps": lan_fact["value"]}
        usb4[mid] = usb4_fact
    for mid in ctx.nodes:
        gbps = lan.get(mid, {}).get("gbps")
        if gbps is not None:   # a link whose speed is unknown is not reported (the core then refuses it), never guessed
            links.append({"kind": "ethernet", "hosts": [ctx.pc, mid], "gbps": gbps})
    if len(ctx.nodes) == 2:
        a, b = ctx.nodes
        # USB4 bandwidth is not probed here: 0 Gb/s satisfies only a gbps_min of 0 (design §3).
        links.append({"kind": "usb4", "hosts": [a, b], "gbps": 0.0})
        notes.append("USB4 bandwidth not probed: reported as 0 Gb/s (only gbps_min = 0 passes)")
    facts = {"model": ctx.model, "devices": devices, "links": links}
    availability = {"devices": avail_devices, "hosts": hosts, "lan": lan, "usb4": usb4}
    return inventory, facts, availability, notes


def _gtt(gpu_result):
    """GPU-mappable memory of a unified-memory node: the GTT pool (design §9.8), GiB."""
    cards = ((gpu_result or {}).get("checks", {}).get("amd") or {}).get("value") or []
    if not cards:
        return None
    gtt = (cards[0].get("gtt_total_bytes") or {}).get("value")
    return None if gtt is None else gtt / probe.GIB


def _reservation_problem(cluster, ctx, now):
    """Why the PRO cannot be used now under the lab rules (None when it can)."""
    machine = cluster.machines[ctx.service["machine"]]
    reservation = cluster.pc.get("cuda", {}).get("reservation")
    if reservation is None:
        return "lab mode: no operator-granted pc.cuda.reservation" if machine.lab_mode else None
    if rtx.reservation_end(reservation, now) <= now + 60:
        return "pc.cuda.reservation has expired or ends within 60 s"
    return None


def link_state(recipe, bound, availability):
    """Per recipe link id: up when every host pair it joins on the bound hosts is up (pc-nodes: the node answered and
    has a physical NIC up; usb4: both nodes report the layout and an openable stream)."""
    out = {}
    hosts = {s: [m["host"] for m in members] for s, members in bound["binding"].items()}
    for link in recipe["match"]["links"]:
        a, b = link["between"]
        why = None
        for ha in hosts.get(a, []):
            for hb in hosts.get(b, []):
                if ha == hb:
                    continue
                node = hb if ha not in availability["lan"] else ha
                if link["kind"] in ("usb4",):
                    for end in (ha, hb):
                        state = availability["usb4"].get(end, {})
                        if not state.get("up"):
                            why = why or f"{end}: {state.get('why', 'USB4 state unknown')}"
                else:
                    state = availability["lan"].get(node, {})
                    if not state.get("up"):
                        why = why or f"{node}: {state.get('why') or 'no physical interface up'}"
        if not hosts.get(a) or not hosts.get(b):
            why = why or "an end is not bound"
        out[link["id"]] = {"up": why is None, **({"why": why} if why else {})}
    return out


def availability(recipe, bound, raw):
    """The admission availability (devices by probe id, links by recipe link id and `<set>.internal`, hosts)."""
    links = link_state(recipe, bound, raw)
    for device in recipe["match"]["devices"]:
        hosts = sorted({m["host"] for m in bound["binding"].get(device["id"], [])})
        if device["internal_link"] is None or len(hosts) < 2:
            continue
        down = [h for h in hosts if not (raw.get("usb4", {}).get(h) or {}).get("up")]
        links[f"{device['id']}.internal"] = ({"up": True} if not down else
                                             {"up": False, "why": "; ".join(
                                                 f"{h}: {(raw['usb4'].get(h) or {}).get('why', 'USB4 state unknown')}"
                                                 for h in down)})
    return {"devices": raw["devices"], "links": links, "hosts": raw["hosts"]}


class Cache(probe.DiskCache):
    """The probe cache plus saved node disk evidence (receipts)."""

    def evidence(self, machine, model_path, now=None):
        """The node's saved disk measurement for this model path, when it is younger than the cache TTL."""
        entry = self._load().get(f"evidence|{machine}|{model_path}")
        now = time.time() if now is None else now
        if not isinstance(entry, dict) or entry.get("path") != model_path or now - entry.get("measured_at", 0) > probe.CACHE_TTL_S:
            return None
        return entry

    def put_evidence(self, machine, model_path, receipt_path):
        """Store a node's measured disk bandwidth from a receipt (JSON: disk_read_gbytes_ps, method "direct", host,
        path); refused unless the receipt names this machine and its model path."""
        raw = Path(receipt_path).read_bytes()
        try:
            receipt = json.loads(raw)
        except ValueError as exc:
            raise probe.ProbeError(f"{receipt_path}: not JSON") from exc
        if not isinstance(receipt, dict) or not EVIDENCE_SCHEMA_KEYS <= receipt.keys():
            raise probe.ProbeError(f"{receipt_path}: needs {', '.join(sorted(EVIDENCE_SCHEMA_KEYS))}")
        if receipt["method"] != "direct":
            raise probe.ProbeError(f"{receipt_path}: method must be direct (cache-bypassing reads)")
        if receipt["host"] != machine or receipt["path"] != model_path:
            raise probe.ProbeError(f"{receipt_path}: names {receipt['host']}:{receipt['path']}, not "
                                   f"{machine}:{model_path}")
        value = receipt["disk_read_gbytes_ps"]
        if type(value) not in (int, float) or not math.isfinite(value) or not value > 0:
            raise probe.ProbeError(f"{receipt_path}: disk_read_gbytes_ps must be a finite number > 0")
        data = self._load()
        key = f"evidence|{machine}|{model_path}"
        data[key] = {"disk_read_gbytes_ps": float(value), "method": "receipt", "receipt": str(receipt_path),
                     "receipt_sha256": hashlib.sha256(raw).hexdigest(), "path": model_path, "measured_at": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
        return data[key]
