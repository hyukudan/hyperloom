"""Gather the facts a recipe is checked against, for one profile: its context (model, topology, machines, service),
the device inventory, capability facts (for match) and availability (for admission).

Every remote call goes through the node tool's existing read-only verbs (`gpu`, `doctor`); local facts come from
nvidia-smi and the OS. Disk bandwidth is read from the probe cache unless the operator asked for a measurement.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .. import engines, render, rtx
from ..config import ConfigError
from . import apply, probe
from .reader import RecipeError

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
# The prompts one prefill forward of the decode engine runs, as ENFORCED by its scheduler, bound to the engine build and
# the prefill mode it holds for (a recipe's local prefill max_batch must equal it; core §6 max_batch).
FAMILY_PREFILL_BATCH = {"ds41-pair": {"batch": 1, "engine_sha": "9211b07e13c64ab80ae04fcbfcbeab50fe0cbf04",
                                      "mode": ("--prefill-path", "device"),
                                      "source": "apps/deepseek41_serve_main.cpp:33-38 (the other requests wait during "
                                                "the prefill), :1516 (DevicePrefill holds one sequence)"}}
# The prefill piece size the decode engine runs (flag, engine default or None): a local prefill route's chunk_tokens
# must equal it (0 = whole prompt equals no engine here). strix-deepseek41-serve: --prefill-rows 4096 (:146).
FAMILY_PREFILL_CHUNK = {"ds41-pair": ("--prefill-rows", 4096),
                        "qwen-replicas": ("--known-prefix-prefill-rows", None),
                        "minimax-pair": (None, None)}
DECLARATION_SCHEMA = "hyperloom/engine-describe-v1"
DESCRIBE_FLAG = {"ds41-pair": "--describe-capabilities-json"}     # the engine's memory declaration (:2100-2146)
MAX_DECLARED_BYTES = 1 << 50
MAX_RECEIPT_BYTES = 4 << 20     # a receipt file is read at most this far (the core's output cap)
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def read_receipt(receipt_path):
    """A receipt's raw bytes and its JSON, read at most MAX_RECEIPT_BYTES."""
    with open(receipt_path, "rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise probe.ProbeError(f"{receipt_path}: larger than {MAX_RECEIPT_BYTES >> 20} MiB")
    try:
        return raw, json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise probe.ProbeError(f"{receipt_path}: not JSON") from exc


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
    adapter = engines.release_adapter(settings.get("preset", profile))
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
              "prefill_batch": _prefill_batch(kind, settings, data),
              "prefill_chunk": (_flag_value(settings, data, *FAMILY_PREFILL_CHUNK[kind])
                                if FAMILY_PREFILL_CHUNK[kind][0] else None),
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


def _flag_text(settings, data, flag):
    value = settings.get("overrides", {}).get(flag)
    argv = data.get("argv", [])
    if value is None and flag in argv:
        value = argv[argv.index(flag) + 1]
    return None if value is None else str(value)


def _prefill_batch(kind, settings, data):
    """(enforced prompts per prefill forward, None) or (None, why): only for the engine build and mode it was read at."""
    fact = FAMILY_PREFILL_BATCH.get(kind)
    if fact is None:
        return None, f"hyperloom has no cited prefill concurrency for {kind}"
    if data.get("engine_sha") != fact["engine_sha"]:
        return None, (f"the prefill concurrency of {kind} is known for engine {fact['engine_sha'][:12]}, the preset runs "
                      f"{str(data.get('engine_sha'))[:12]}")
    flag, value = fact["mode"]
    if _flag_text(settings, data, flag) != value:
        return None, f"the prefill concurrency of {kind} holds for {flag} {value} only"
    return fact["batch"], None


def launch_sha256(spec, engine_sha, assets):
    """One node process's launch identity: its rendered argv and env, the engine build and the sha256 of every asset
    the preset loads (a file replaced at the same path is another launch)."""
    return hashlib.sha256(json.dumps({"argv": spec.argv, "env": spec.env, "engine_sha": engine_sha, "assets": assets},
                                     sort_keys=True).encode()).hexdigest()


# `up` installs the release and writes the key files after the recipe check: the release id only picks the install
# directory of the preset's engine build (engine_sha is hashed) and a key file is a secret's location, so the launch
# identity renders both as these fixed placeholders; `up` re-checks the identity on its final render (launch.py).
IDENTITY_RELEASE = "0" * 12
IDENTITY_KEY = "{key_file}"


def _identity_cluster(cluster, profile):
    from copy import deepcopy
    copy = deepcopy(cluster)
    settings = copy.profiles[profile]
    settings["release_id"] = IDENTITY_RELEASE
    settings["api_key_files"] = {mid: IDENTITY_KEY for mid in engines.members(settings, render.family(
        settings.get("preset", profile)))}
    return copy


def launch_identities(cluster, profile, processes=None):
    """{machine: (rank, launch sha256, spec)} of the profile's node processes; ``processes`` = an already rendered
    final launch (from a cluster whose release id and key paths are set), normalized the same way."""
    settings = cluster.profiles[profile]
    name = settings.get("preset", profile)
    data = render.preset(name)
    if processes is None:
        processes = render.render_processes(_identity_cluster(cluster, profile), profile)
    else:
        release = settings.get("release_id")
        keys = settings.get("api_key_files") or {}
        normal = []
        for spec in processes:
            argv = [IDENTITY_KEY if keys.get(spec.machine_id) == a else
                    a.replace(f"/releases/{release}/", f"/releases/{IDENTITY_RELEASE}/") if release else a
                    for a in spec.argv]
            normal.append(type(spec)(spec.machine_id, spec.role, argv, dict(spec.env), spec.ready))
        processes = normal
    out = {}
    for rank, spec in enumerate(processes):
        assets = {a: cluster.assets.get(spec.machine_id, {}).get(a, {}).get("sha256") for a in data["assets"]}
        out[spec.machine_id] = (rank, launch_sha256(spec, data["engine_sha"], assets), spec)
    return out


def describe_commands(cluster, profile):
    """Per node process: its launch sha256 and the engine's own memory declaration command for that exact launch."""
    settings = cluster.profiles[profile]
    name = settings.get("preset", profile)
    kind = render.family(name)
    if kind not in DESCRIBE_FLAG:
        raise ConfigError(f"profile {profile}: the {kind} engine declares no memory model")
    rows = []
    for machine, (rank, sha, spec) in launch_identities(cluster, profile).items():
        rows.append({"machine": machine, "rank": rank, "launch_sha256": sha,
                     "argv": spec.argv + [DESCRIBE_FLAG[kind]], "env": spec.env,
                     "note": f"{IDENTITY_KEY} and release {IDENTITY_RELEASE} stand for the key file and release `up` "
                             "installs; run it with the installed binary and the node's key file path"})
    return rows


def node_runtime(cluster, ctx, cache):
    """Per node: {"gib": declared runtime} for the exact launch, or {"why"}: the engine's declaration receipt (R4),
    runtime = workspace + state per slot x slots + KV pool of that rank."""
    try:
        rows = describe_commands(cluster, ctx.profile)
    except ConfigError as exc:
        return {mid: {"why": str(exc)} for mid in ctx.nodes}
    slots = (ctx.limits or {}).get("node_batch")
    out = {}
    for row in rows:
        entry = cache.declaration(row["machine"], row["launch_sha256"]) if cache is not None else None
        if entry is None:
            out[row["machine"]] = {"why": "no engine memory declaration for this exact launch (recipe probe "
                                          "--describe-command, then --declaration NODE=RECEIPT)"}
        elif slots is None:
            out[row["machine"]] = {"why": "the launched slots are unknown"}
        else:
            out[row["machine"]] = {"gib": (entry["workspace_bytes"] + entry["state_bytes_per_slot"] * slots
                                           + entry["kv_pool_bytes"]) / probe.GIB,
                                   "receipt_sha256": entry["receipt_sha256"], "launch_sha256": row["launch_sha256"],
                                   "executable_sha256": entry.get("executable_sha256")}
    return out


def current_runtime(cluster, ctx, rows):
    """The runtime rows (live or from saved facts) that still describe this profile's launch: a row whose launch
    sha256 is not the current one becomes a refusal reason (saved facts outlive flag changes)."""
    try:
        current = {mid: sha for mid, (_, sha, _) in launch_identities(cluster, ctx.profile).items()}
    except ConfigError as exc:
        return {mid: {"why": str(exc)} for mid in ctx.nodes}
    out = {}
    for mid, row in (rows or {}).items():
        if "gib" in row and row.get("launch_sha256") != current.get(mid):
            out[mid] = {"why": "the saved engine declaration is for another launch (a flag, asset or build changed)"}
        else:
            out[mid] = row
    return out


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
           measurer=None, now=None, release_executable=None, release_commit=None, sysmem_reader=None,
           transfer_identity=None, placement=False):
    """(inventory, facts, availability, notes). ``call(machine_id, verb)`` runs a node verb and returns its result or
    raises; any failure is an unavailable fact with its reason, never an invented value."""
    now = time.time() if now is None else now
    placement = placement or release_executable is not None or transfer_identity is not None
    if gpus is None:
        gpus = (lambda: probe.nvidia_gpus(placement=True)) if placement else probe.nvidia_gpus
    memory = memory or probe.host_memory
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
        row = {"name": gpu["name"], "memory_gib": gpu["memory_gib"], "host_ram_gib": pc_mem["total_gib"],
               **{k: gpu[k] for k in ("uuid", "pci", "arch", "driver", "memory_bytes", "platform",
                                     "pci_path", "link", "via", "numa_node") if placement and k in gpu}}
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
            devices[mid] = {"name": None, "memory_gib": None, "present": False}
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
    facts = {"model": ctx.model, "devices": devices, "links": links,
             "node_runtime": node_runtime(cluster, ctx, cache)}
    # Store identities come from local cached provenance, never an implicit measurement or hostname lookup.
    if placement:
        from . import topology
        facts.update({key: (transfer_identity or {}).get(key, "unknown")
                      for key in ("file_store", "engram_store")})
        facts["xfer_host"] = ((transfer_identity or {}).get("xfer_host")
                              if transfer_identity and "xfer_host" in transfer_identity else topology.host())
    availability = {"devices": avail_devices, "hosts": hosts, "lan": lan, "usb4": usb4}
    if release_executable is not None:
        from . import sysmem
        facts.update(_identities(cluster, ctx, release_executable, release_commit, notes))
        availability["sysmem_policy"] = (sysmem_reader or sysmem.probe)(release_executable)
    return inventory, facts, availability, notes


def _identities(cluster, ctx, executable, commit, notes):
    """Hash the small release binary; use verified model metadata without reading model content."""
    from .. import assets
    from . import content_cache
    identities = {}
    try:
        binary = Path(executable)
        release = content_cache._read(binary.parent.parent / "MANIFEST.json", "PL_E_STALE")
        expected = release.get("binaries", {}).get(binary.name)
        if not content_cache._digest(expected) or (commit is not None and release.get("engine_commit") != commit):
            raise ValueError("release manifest differs from the selected build")
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError("release binary differs from its verified digest")
        identities["engine_sha256"] = digest
    except (OSError, ValueError, RecipeError) as exc:
        notes.append("engine identity unavailable: " + str(exc))
    try:
        name = FAMILY_MODELS_BY_ID.get(ctx.model)
        asset = cluster.assets.get(ctx.pc, {}).get(name)
        entry = assets.read_manifest().get(ctx.pc, {}).get(name)
        if asset is None or entry is None:
            raise ValueError("run assets verify for the selected model")
        stat = Path(asset.get("control_path", asset["path"])).stat()
        fresh = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino, dev=stat.st_dev)
        reason = assets._reason(asset, fresh, entry)
        if reason or not content_cache._digest(entry.get("sha256")):
            raise ValueError(reason or "verified model digest unavailable")
        identities["model_sha256"] = entry["sha256"]
    except (OSError, ValueError) as exc:
        notes.append("model identity unavailable: " + str(exc))
    return identities


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
    """The probe cache plus saved node disk evidence and engine memory declarations (receipts)."""

    def declaration(self, machine, launch):
        entry = self._load().get(f"describe|{machine}|{launch}")
        return entry if isinstance(entry, dict) and entry.get("launch_sha256") == launch else None

    def put_declaration(self, machine, rank, launch, receipt_path):
        """Store a node's engine memory declaration: `hyperloom/engine-describe-v1` {machine, rank, launch_sha256,
        output = the engine's describe JSON}; refused unless it names this machine, rank and exact launch."""
        raw, receipt = read_receipt(receipt_path)
        if not isinstance(receipt, dict) or receipt.get("schema") != DECLARATION_SCHEMA:
            raise probe.ProbeError(f"{receipt_path}: expected {DECLARATION_SCHEMA}")
        if type(receipt.get("rank")) is not int:
            raise probe.ProbeError(f"{receipt_path}: rank must be an integer")
        if (receipt.get("machine"), receipt.get("rank"), receipt.get("launch_sha256")) != (machine, rank, launch):
            raise probe.ProbeError(f"{receipt_path}: names {receipt.get('machine')} rank {receipt.get('rank')} launch "
                                   f"{str(receipt.get('launch_sha256'))[:12]}, not {machine} rank {rank} launch "
                                   f"{launch[:12]} (the launch changed: run the describe command again)")
        output = receipt.get("output")
        modes = output.get("modes") if isinstance(output, dict) else None
        if (not isinstance(output, dict) or output.get("schema") != "strix/engine-capabilities-v1"
                or not isinstance(modes, list) or len(modes) != 1 or not isinstance(modes[0], dict)):
            raise probe.ProbeError(f"{receipt_path}: output must be one strix/engine-capabilities-v1 mode")
        executable = output.get("executable_sha256")
        if not isinstance(executable, str) or not SHA256_HEX.fullmatch(executable):
            raise probe.ProbeError(f"{receipt_path}: output.executable_sha256 must be a sha256 (the engine build that "
                                   "declared it, compared with the release `up` starts)")
        memory = [m for m in modes[0].get("memory") or []
                  if isinstance(m, dict) and type(m.get("rank")) is int and m["rank"] == rank]
        if len(memory) != 1:
            raise probe.ProbeError(f"{receipt_path}: the declaration has no memory entry for rank {rank}")
        fields = {}
        for key in ("workspace_bytes", "state_bytes_per_slot", "kv_pool_bytes"):
            value = memory[0].get(key)
            if type(value) is not int or not 0 <= value < MAX_DECLARED_BYTES:
                raise probe.ProbeError(f"{receipt_path}: {key} must be an integer in [0, 2^50)")
            fields[key] = value
        data = self._load()
        key = f"describe|{machine}|{launch}"
        data[key] = dict(fields, launch_sha256=launch, rank=rank, receipt=str(receipt_path),
                         receipt_sha256=hashlib.sha256(raw).hexdigest(),
                         executable_sha256=executable)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
        return data[key]

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
        raw, receipt = read_receipt(receipt_path)
        if not isinstance(receipt, dict) or not EVIDENCE_SCHEMA_KEYS <= receipt.keys():
            raise probe.ProbeError(f"{receipt_path}: needs {', '.join(sorted(EVIDENCE_SCHEMA_KEYS))}")
        if receipt["method"] != "direct":
            raise probe.ProbeError(f"{receipt_path}: method must be direct (cache-bypassing reads)")
        if receipt["host"] != machine or receipt["path"] != model_path:
            raise probe.ProbeError(f"{receipt_path}: names {receipt['host']}:{receipt['path']}, not "
                                   f"{machine}:{model_path}")
        value = receipt["disk_read_gbytes_ps"]
        if type(value) not in (int, float) or not 0 < value < 1e6:   # NaN, infinities and huge integers fail too
            raise probe.ProbeError(f"{receipt_path}: disk_read_gbytes_ps must be a finite number in (0, 1e6)")
        data = self._load()
        key = f"evidence|{machine}|{model_path}"
        data[key] = {"disk_read_gbytes_ps": float(value), "method": "receipt", "receipt": str(receipt_path),
                     "receipt_sha256": hashlib.sha256(raw).hexdigest(), "path": model_path, "measured_at": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
        return data[key]
