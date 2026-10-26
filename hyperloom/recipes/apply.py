"""Apply an effective recipe to a hyperloom profile: bind its device sets to the profile's devices, build the probe
report and the admission state, decide what `up` deploys, and compile the router's route table.

Rules (design wave4/recipes/orch/design.md §9-§11): one deployed decode configuration; prefill routes are chosen per
request by the router from a versioned table with the core's §7 semantics; capabilities (match) are kept apart from
availability (select); every placement field maps to a launch flag or is refused; routes sharing a process share its
placement; an offload route needs an engine with the parked-import contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from . import reader
from .reader import RecipeError

ROUTE_TABLE_SCHEMA = "tensormeld/prefill-route-table-v1"
ROUTE_STATE_SCHEMA = "tensormeld/prefill-route-state-v1"
STATE_MAX_AGE_S = 10
OFFLOAD_MAX_WAIT_S = 30.0
TOPOLOGY_EXECUTE = {"pair": "tp", "replicas": "replicas"}
PARKED_IMPORT = "import-state-parked"
GIB = 1 << 30
# hyperloom's engines are not strix-stream adopters yet: their per-member host requirement is this conservative cap
# model (core §7.3 counts what a compiled plan allocates; hyperloom counts the budgets the engine is launched with).
CAP_MODEL = "hyperloom-cap-v1"
BOUND_FIELDS = ("max_context_tokens", "max_batch")   # admission bounds, checked against the launch (check_bounds)

# Placement fields a launch takes from a recipe; any other field must keep its schema default (refused otherwise).
SERVICE_FIELDS = {"ds41-rtx-prefill": {"device_gib": "vram_cap_gib", "host_pinned_gib": "host_gib"}}
NODE_FIELDS = {}   # no node preset takes a placement from recipes yet (plan-v1 input lands with the strix-stream adopters)
# Node placement fields checked against the engine's own declaration instead of applied (no launch flag sets them).
NODE_CHECKED = ("workspace_gib",)
RUNTIME_TOLERANCE_GIB = 0.05
# Fields a local prefill route may set differently from the decode route on the same processes (each has its own rule
# in check_endpoints); every other field must be equal.
PER_ROUTE_FIELDS = ("max_context_tokens", "max_batch", "chunk_tokens")


@dataclass
class Context:
    """What the profile launches. ``nodes`` in rank order; ``service`` = {"machine", "device", "uuid", "engine",
    "host_overhead_gib"} of its RTX prefill service, or None. ``capabilities`` = the decode family's adapter
    capabilities; ``node_overhead_gib`` = the node engine's host RAM beyond its device budget."""
    profile: str
    model: str
    topology: str
    nodes: list
    pc: str
    capabilities: tuple = ()
    service: dict | None = None
    node_overhead_gib: float = 4.0
    descriptors: dict | None = None      # probe id -> sha256 identifying the model files that member loads
    limits: dict | None = None           # {"service_positions", "node_context", "default_generation_tokens"}


def _defaults():
    return reader.placement_defaults()


def _sets(recipe, kind):
    return [d for d in recipe["match"]["devices"] if d["kind"] == kind]


def bind(recipe, ctx, inventory):
    """Set -> [{"id", "host"}] for exactly the devices the profile launches (hip: its nodes in rank order; cuda: its
    service GPU), plus ``roles`` (decode | service | idle) and why an idle set does not run. ``inventory`` =
    {"cuda": [{"uuid", "name", "memory_gib"}...]} of the control machine; an idle cuda set binds to the first GPU whose
    name matches, so the hardware can still match while the profile does not use it."""
    hip = _sets(recipe, "hip")
    if len(hip) != 1:
        raise RecipeError(f"profile {ctx.profile}: the recipe needs exactly one hip set (the nodes); it has {len(hip)}")
    binding, roles, why = {}, {}, {}
    for device in recipe["match"]["devices"]:
        sid = device["id"]
        if device["kind"] == "cpu":
            raise RecipeError(f"match.devices.{sid}: cpu sets are not run by hyperloom")
        if device["kind"] == "hip":
            if device["count"] != len(ctx.nodes):
                raise RecipeError(f"match.devices.{sid}: count {device['count']}, but profile {ctx.profile} runs "
                                  f"{len(ctx.nodes)} node(s) ({', '.join(ctx.nodes)})")
            binding[sid] = [{"id": mid, "host": mid} for mid in ctx.nodes]
            roles[sid] = "decode"
            continue
        if device["count"] != 1:
            raise RecipeError(f"match.devices.{sid}: hyperloom runs one cuda device per set (count {device['count']})")
        if ctx.service is not None and not any(r == "service" for r in roles.values()):
            binding[sid] = [{"id": ctx.service["uuid"], "host": ctx.pc}]
            roles[sid] = "service"
            continue
        name = (device["name"] or "").lower()
        gpu = next((g for g in inventory.get("cuda", []) if name in g["name"].lower()), None)
        binding[sid] = [{"id": gpu["uuid"], "host": ctx.pc}] if gpu else []
        roles[sid] = "idle"
        why[sid] = (f"profile {ctx.profile} has no prefill service" if ctx.service is None
                    else f"profile {ctx.profile} runs one prefill service")
    return {"binding": binding, "roles": roles, "why": why}


def decode_set(bound):
    return next(s for s, r in bound["roles"].items() if r == "decode")


def probe_report(recipe, bound, facts, *, cluster=None, ctx=None, inventory=None):
    """The core's probe report (§8.2) with exactly the bound devices, so the core's binding can only pick them.
    ``facts``: {"model", "model_sha256", "devices": {probe id: {kind, name, memory_gib, host_ram_gib,
    disk_read_gbytes_ps, driver}}, "links": [{kind, hosts, gbps}]} (capability values only)."""
    if any(reader.placement_mode(r) for r in recipe["routes"]):
        from . import devices
        if cluster is None or ctx is None or inventory is None:
            raise RecipeError("placement probe requires configured candidates")
        return devices.candidates(cluster, ctx, inventory, facts)
    devices = []
    for device in recipe["match"]["devices"]:
        for member in bound["binding"][device["id"]]:
            item = facts["devices"].get(member["id"], {})
            row = {"id": member["id"], "kind": device["kind"], "name": item.get("name"),
                   "memory_gib": item.get("memory_gib"), "host": member["host"]}
            for key in ("host_ram_gib", "disk_read_gbytes_ps", "h2d_gbytes_ps", "driver"):
                if item.get(key) is not None:
                    row[key] = item[key]
            devices.append(row)
    report = {"model": facts["model"], "devices": devices, "links": deepcopy(facts.get("links", []))}
    for key in ("engine_sha256", "model_sha256"):
        if facts.get(key):
            report[key] = facts[key]
    return report


def match(recipe, report, bound):
    """§3.1 (stub of `ss-recipe match`) over the bound devices: every unmet requirement, [] when the hardware fits."""
    problems = []
    if report.get("model") != recipe["match"]["model"]:
        problems.append(f'match.model: the recipe is for "{recipe["match"]["model"]}", the profile serves '
                        f'"{report.get("model")}"')
    if recipe["match"]["model_sha256"] and report.get("model_sha256") != recipe["match"]["model_sha256"]:
        problems.append("match.model_sha256: the model file differs from the one the recipe was written for")
    rows = {d["id"]: d for d in report["devices"]}
    hosts = {}
    for device in recipe["match"]["devices"]:
        members = bound["binding"][device["id"]]
        hosts[device["id"]] = [m["host"] for m in members]
        if len(members) != device["count"]:
            problems.append(f"match.devices.{device['id']}: needs {device['count']} {device['kind']} device(s); "
                            f"bound {len(members)}")
            continue
        for member in members:
            unmet = _unmet(device, rows[member["id"]])
            if unmet:
                row = rows[member["id"]]
                problems.append(f"match.devices.{device['id']}: {member['id']} ({row.get('name') or '?'}, "
                                f"{_gib(row.get('memory_gib'))}): {'; '.join(unmet)}")
        internal = device["internal_link"]
        unique_hosts = sorted(set(hosts[device["id"]]))
        if internal is not None and len(unique_hosts) >= 2:
            for i, a in enumerate(unique_hosts):
                for b in unique_hosts[i + 1:]:
                    if not _link_ok(internal, report.get("links", []), a, b):
                        problems.append(f"match.devices.{device['id']}.internal_link: needs {internal['kind']} "
                                        f">= {internal['gbps_min']:g} Gb/s between {a} and {b}; " + _links_seen(report))
    for link in recipe["match"]["links"]:
        a, b = link["between"]
        for ha in set(hosts.get(a, [])):
            for hb in set(hosts.get(b, [])):
                if ha != hb and not _link_ok(link, report.get("links", []), ha, hb):
                    problems.append(f"match.links.{link['id']}: needs {link['kind']} >= {link['gbps_min']:g} Gb/s "
                                    f"between {ha} ({a}) and {hb} ({b}); " + _links_seen(report))
    return problems


def _unmet(device, row):
    unmet = []
    if device["name"] and device["name"].lower() not in (row.get("name") or "").lower():
        unmet.append(f'name does not contain "{device["name"]}"')
    for key, field, label in (("memory_gib", "memory_min_gib", "memory"),
                              ("host_ram_gib", "host_ram_min_gib", "host RAM"),
                              ("disk_read_gbytes_ps", "disk_read_gbytes_ps_min", "disk read")):
        minimum = device[field]
        if minimum <= 0:
            continue
        value = row.get(key)
        unit = "GB/s" if key == "disk_read_gbytes_ps" else "GiB"
        if value is None:
            hint = (" (run `hyperloom recipe probe <profile> --disks`, or --evidence for a node)"
                    if key == "disk_read_gbytes_ps" else "")
            unmet.append(f"{label} not measured, needs >= {minimum:g} {unit}{hint}")
        elif value < minimum:
            unmet.append(f"{label} {value:.2f} {unit} < {minimum:g}")
    return unmet


def _gib(value):
    return "?" if value is None else f"{value:.1f} GiB"


def _link_ok(need, links, a, b):
    for probed in links:
        if set(probed.get("hosts", [])) != {a, b}:
            continue
        if need["kind"] != "other" and probed.get("kind") != need["kind"]:
            continue
        if need["gbps_min"] <= 0 or (probed.get("gbps") is not None and probed["gbps"] >= need["gbps_min"]):
            return True
    return False


def _links_seen(report):
    seen = [f"{p.get('kind')} {'-'.join(p.get('hosts', []))} "
            f"{'?' if p.get('gbps') is None else format(p['gbps'], 'g')} Gb/s" for p in report.get("links", [])]
    return "probed: " + (", ".join(seen) or "none")


def descriptors(bound, ctx):
    """Per set, the descriptor sha256 of each member (the model files it loads), in binding order."""
    known = ctx.descriptors or {}
    return {s: [known.get(m["id"], "0" * 64) for m in members] for s, members in bound["binding"].items()}


def admission_state(bound, availability, ctx, running=(), requirements=(), phase="prefill", tokens=0, batch=1,
                    context=None):
    """The core's request state (§8.3). ``availability`` = {"devices": {probe id: {available, why}},
    "links": {link id or "<set>.internal": {up, why}}, "hosts": {host: {free_ram_gib}}}."""
    return {"phase": phase, "prompt_tokens": tokens, "context_tokens": tokens if context is None else context,
            "batch": batch, "session_route": None, "binding": deepcopy(bound["binding"]),
            "devices": deepcopy(availability["devices"]), "links": deepcopy(availability["links"]),
            "hosts": deepcopy(availability["hosts"]), "descriptors": descriptors(bound, ctx),
            "running": deepcopy(list(running)), "requirements": deepcopy(list(requirements))}


# --- deployment decision -----------------------------------------------------------------------------------------

def classify(route, bound, decode):
    """'local' (prefilled by the decode processes) or 'offload' (one cuda set handing off to the deployed decode
    route); anything else is refused."""
    dset = decode["devices"][0]
    if route["devices"] == [dset] and route["handoff_route"] in (None, decode["id"]):
        return "local"
    if (len(route["devices"]) == 1 and bound["roles"].get(route["devices"][0]) in ("service", "idle")
            and route["execute"] == "single" and route["handoff_route"] == decode["id"]):
        return "offload"
    raise RecipeError(f'routes.{route["id"]}: hyperloom runs a prefill route either on the decode set "{dset}" or on '
                      f'one cuda set handing off to decode route "{decode["id"]}"')


def _boundaries(routes, low, high, floor):
    points = {floor}
    for r in routes:
        points.add(max(r["when"][low], floor))
        if r["when"][high] is not None:
            points.add(r["when"][high] + 1)
    return sorted(points)


def decode_route(recipe, ctx, bound, state, recipe_sha, select=None):
    """The one decode route hyperloom deploys: the same route for every workload region, running the profile's
    topology on the decode set; refused otherwise, naming the conflicting routes."""
    routes = [r for r in recipe["routes"] if r["phase"] == "decode"]
    hetero = [r["id"] for r in routes if r["execute"] == "hetero"]
    if hetero:
        raise RecipeError(f"routes.{hetero[0]}: execute = hetero decode needs a profile whose engine runs it; no hyperloom "
                          f"profile does (profile {ctx.profile} runs {ctx.topology} on the nodes)")
    chosen = {}
    for tokens in _boundaries(routes, "prompt_tokens_min", "prompt_tokens_max", 0):
        for batch in _boundaries(routes, "batch_min", "batch_max", 1):
            region = dict(state, phase="decode", prompt_tokens=tokens, context_tokens=tokens, batch=batch)
            result = select(region) if select else reader.select(recipe, region, recipe_sha)
            chosen.setdefault(result["route"], (tokens, batch))
    if len(chosen) > 1:
        cases = "; ".join(f'"{rid}" at prompt_tokens={t}, batch={b}' for rid, (t, b) in chosen.items())
        raise RecipeError(f"the recipe chooses decode by workload ({cases}); hyperloom deploys one decode "
                          "configuration: split it into one recipe per configuration")
    rid = next(iter(chosen))
    route = next(r for r in routes if r["id"] == rid)
    dset = decode_set(bound)
    want = TOPOLOGY_EXECUTE[ctx.topology]
    if route["devices"] != [dset] or route["execute"] != want:
        raise RecipeError(f'decode route "{rid}" runs {route["execute"]} on {", ".join(route["devices"])}; profile '
                          f'{ctx.profile} runs {ctx.topology} ({want}) on "{dset}"')
    return route


def _placement_diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def check_local_prefill(route, decode, ctx):
    """A prefill route run by the decode processes: its context within the decode route's, its max_batch equal to the
    prompts the engine's scheduler runs per prefill, its chunk_tokens equal to the engine's prefill pieces, and every
    other field equal to the decode route's (one process has one placement)."""
    dset = decode["devices"][0]
    base, placement = decode["placement"][dset], route["placement"][dset]
    where = f"routes.{route['id']}.placement.{dset}"
    diff = [k for k in _placement_diff(base, placement) if k not in PER_ROUTE_FIELDS]
    if diff:
        raise RecipeError(f'routes.{route["id"]} and routes.{decode["id"]} run on the same "{dset}" processes with '
                          f"different placements ({', '.join(diff)})")
    if placement["max_context_tokens"] > base["max_context_tokens"]:
        raise RecipeError(f"{where}.max_context_tokens: {placement['max_context_tokens']} > the decode route's "
                          f"{base['max_context_tokens']} on the same processes")
    limits = ctx.limits or {}
    batch, why = limits.get("prefill_batch") or (None, "the engine's prefill concurrency is unknown")
    if batch is None:
        raise RecipeError(f"{where}.max_batch: {why}")
    if placement["max_batch"] != batch:
        raise RecipeError(f"{where}.max_batch: {placement['max_batch']} != the {batch} prompt(s) the engine's scheduler "
                          "runs per prefill (a different bound is not enforced by the engine)")
    chunk = limits.get("prefill_chunk")
    if chunk is None:
        raise RecipeError(f"{where}.chunk_tokens: the engine's prefill piece size is unknown for profile {ctx.profile}")
    if placement["chunk_tokens"] != chunk:
        shown = "0 (whole prompt)" if placement["chunk_tokens"] == 0 else str(placement["chunk_tokens"])
        raise RecipeError(f"{where}.chunk_tokens: {shown} != the engine's prefill pieces of {chunk} rows")


def runtime_checked(route, set_id):
    """R4 applies: the route sets a non-default workspace_gib on that set (the default is never checked)."""
    workspace = route["placement"][set_id]["workspace_gib"]
    return workspace is not None and workspace != _defaults()["workspace_gib"]


def check_runtime(route, set_id, ctx, bound):
    """R4: a non-default workspace_gib on the nodes must equal the engine's declared runtime for each deployed member
    (the engine sizes it itself; nothing applies it)."""
    placement = route["placement"][set_id]
    if not runtime_checked(route, set_id):
        return
    where = f"routes.{route['id']}.placement.{set_id}.workspace_gib"
    runtime = (ctx.limits or {}).get("node_runtime") or {}
    for member in bound["binding"][set_id]:
        row = runtime.get(member["id"]) or {"why": "no engine memory declaration"}
        if "gib" not in row:
            raise RecipeError(f"{where}: {member['id']}: {row['why']}; it is checked, not applied")
        if abs(placement["workspace_gib"] - row["gib"]) > RUNTIME_TOLERANCE_GIB:
            raise RecipeError(f"{where}: {placement['workspace_gib']:g} GiB != the {row['gib']:.2f} GiB the engine "
                              f"declares on {member['id']} for this launch (it sizes its runtime itself)")


def check_endpoints(recipe, bound, decode, ctx):
    """Routes on the decode processes follow check_local_prefill; offload routes on one service share its placement."""
    offload = []
    for route in recipe["routes"]:
        if route["phase"] != "prefill":
            continue
        if classify(route, bound, decode) == "local":
            check_local_prefill(route, decode, ctx)
        else:
            offload.append(route)
    for other in offload[1:]:
        first = offload[0]
        diff = _placement_diff(first["placement"][first["devices"][0]], other["placement"][other["devices"][0]])
        if diff:
            raise RecipeError(f"routes.{first['id']} and routes.{other['id']} use one prefill service with different "
                              f"placements ({', '.join(diff)})")


def launch_flags(route, set_id, table, what, memory_min, checked=(), *, placement_files=None):
    """The launch values a placement maps to; any other non-default field is refused, never dropped (``checked``
    fields are verified elsewhere against the engine's declaration)."""
    if reader.placement_mode(route):
        if placement_files is None:
            raise RecipeError("PL_E_CAPABILITY placement needs the qualified decode adapter and runtime artifacts")
        placement_files.read()
        return dict(zip(placement_files.argv()[::2], placement_files.argv()[1::2]))
    placement = route["placement"][set_id]
    defaults = dict(_defaults(), device_gib=memory_min)
    unmapped = sorted(k for k in placement if k not in table and k not in BOUND_FIELDS and k not in checked
                      and placement[k] != defaults[k])
    if unmapped:
        raise RecipeError(f"routes.{route['id']}.placement.{set_id}: {', '.join(unmapped)} cannot be applied to {what} "
                          "(no launch flag); remove it or keep the default")
    return {flag: placement[k] for k, flag in table.items() if placement[k] is not None and placement[k] != defaults.get(k)}


def check_bounds(route, set_id, kind, ctx):
    """A route's admission bounds must be within what its launch allocates: the RTX service runs one prompt at a time
    up to its positions; the nodes' engine up to its context."""
    placement = route["placement"][set_id]
    limits = ctx.limits or {}
    where = f"routes.{route['id']}.placement.{set_id}"
    if kind == "service":
        positions = limits.get("service_positions")
        if placement["max_batch"] != 1:
            raise RecipeError(f"{where}.max_batch: the prefill service runs one prompt at a time (set 1)")
        if positions is not None and placement["max_context_tokens"] > positions:
            raise RecipeError(f"{where}.max_context_tokens: {placement['max_context_tokens']} > the service's "
                              f"{positions} positions")
    else:
        context_flag, batch_flag = limits.get("node_flags", ("--context", "--slots"))
        for value, bound, flag, field in ((placement["max_context_tokens"], limits.get("node_context"), context_flag,
                                           "max_context_tokens"),
                                          (placement["max_batch"], limits.get("node_batch"), batch_flag, "max_batch")):
            if bound is None:
                raise RecipeError(f"{where}.{field}: profile {ctx.profile} does not set {flag} and hyperloom does not "
                                  "know the engine's default; set it in the profile's overrides")
            if value > bound:
                raise RecipeError(f"{where}.{field}: {value} > the engine's {flag} {bound}")


def refuse_unsupported(recipe, *, adapter=None):
    if any(reader.placement_mode(r) for r in recipe["routes"]):
        from . import runtime
        if adapter is None:
            raise RecipeError("PL_E_CAPABILITY placement requires the opt-in RTX decode adapter (schema only for legacy engines)")
        runtime.qualified(adapter)
        return
    if any(any(key in device for key in ("optional", "uuid", "pci", "index"))
           for device in recipe["match"]["devices"]):
        raise RecipeError("PL_E_CAPABILITY placement selectors require a qualified placement adapter (schema only)")
    for route in recipe["routes"]:
        if reader.placement_mode(route) or any(key in placement for placement in route["placement"].values()
                                               for key in reader.HOST):
            raise RecipeError(f"routes.{route['id']}: PL_E_CAPABILITY placement launch is unavailable until the "
                              "placement adapter and release are qualified (schema only)")
        w = route["when"]
        if route["phase"] == "prefill" and (w["batch_min"] != 1 or w["batch_max"] is not None):
            raise RecipeError(f"routes.{route['id']}.when: batch predicates on prefill routes are not supported by the "
                              "router (it cannot observe sequences per step)")


def process_routes(recipe, bound, decode):
    """Routes served by the decode processes (the decode route and every local prefill route)."""
    return [decode["id"]] + [r["id"] for r in recipe["routes"]
                             if r["phase"] == "prefill" and classify(r, bound, decode) == "local"]


def requirements(recipe, recipe_sha, ctx, bound):
    """One requirement per member of every route (core §8.3 shape), host_bytes from hyperloom's cap model: pinned tier
    + staging ring + process overhead, plus the whole device budget on unified (hip) sets."""
    sets = {d["id"]: d for d in recipe["match"]["devices"]}
    in_use = descriptors(bound, ctx)
    out = []
    for route in recipe["routes"]:
        for set_id in route["devices"]:
            placement = route["placement"][set_id]
            unified = sets[set_id]["kind"] in ("hip", "cpu")
            device = placement["device_gib"] if placement["device_gib"] is not None else sets[set_id]["memory_min_gib"]
            overhead = (ctx.node_overhead_gib if unified
                        else (ctx.service or {}).get("host_overhead_gib", ctx.node_overhead_gib))
            gib = (placement["host_pinned_gib"] + placement["staging_buffers"] * placement["staging_mib"] / 1024.0
                   + overhead + (device if unified else 0.0))
            host_bytes = int(round(gib * GIB))
            # Windows bills a discrete device's allocations to commit (core §7.3): host bytes + device bytes there.
            commit_bytes = host_bytes + (0 if unified else int(round(device * GIB)))
            for member, row in enumerate(bound["binding"].get(set_id, [])):
                req = {"route": route["id"], "set": set_id, "member": member, "id": row["id"],
                       "effective_sha256": recipe_sha, "descriptor_sha256": in_use[set_id][member],
                       "host_bytes": host_bytes, "commit_bytes": commit_bytes, "disk_bytes": 0, "model": CAP_MODEL}
                req["plan_id"] = hashlib.sha256(json.dumps(
                    [recipe_sha, route["id"], set_id, member, row["id"], req["descriptor_sha256"], CAP_MODEL,
                     host_bytes, commit_bytes]).encode()).hexdigest()
                out.append(req)
    return out


def with_disk(reqs, route_ids, set_id, disk_bytes, recipe_sha):
    """The requirements with a planned disk tier on one set's members (their plan ids follow)."""
    out = []
    for req in reqs:
        if req["route"] in route_ids and req["set"] == set_id and disk_bytes > 0:
            req = dict(req, disk_bytes=int(disk_bytes))
            req["plan_id"] = hashlib.sha256(json.dumps(
                [recipe_sha, req["route"], set_id, req["member"], req["id"], req["descriptor_sha256"], CAP_MODEL,
                 req["host_bytes"], req["commit_bytes"], req["disk_bytes"]]).encode()).hexdigest()
        out.append(req)
    return out


def running_entry(route_id, recipe_sha, reqs):
    return {"route": route_id, "effective_sha256": recipe_sha,
            "plan_ids": sorted(r["plan_id"] for r in reqs if r["route"] == route_id)}


def deploy(recipe, ctx, bound, availability, recipe_sha, select=None):
    """What `up` runs, decided before anything starts: the decode route (admitted against the nodes' free RAM before
    launch), each prefill route's place and admission at up, and whether the prefill service starts."""
    refuse_unsupported(recipe)
    if (ctx.limits or {}).get("default_generation_tokens") is None:
        raise RecipeError(f"profile {ctx.profile}: the decode engine's default generation budget is unknown (the router "
                          "needs it for requests without max_tokens); set it in the profile's overrides")
    by_id = {r["id"]: r for r in recipe["routes"]}
    reqs = requirements(recipe, recipe_sha, ctx, bound)
    pre = admission_state(bound, availability, ctx, requirements=reqs)
    decode = decode_route(recipe, ctx, bound, pre, recipe_sha, select)
    check_endpoints(recipe, bound, decode, ctx)
    check_bounds(decode, decode["devices"][0], "node", ctx)
    sets = {d["id"]: d for d in recipe["match"]["devices"]}
    decode_flags = launch_flags(decode, decode["devices"][0], NODE_FIELDS.get(ctx.profile, {}), f"profile {ctx.profile}",
                                sets[decode["devices"][0]]["memory_min_gib"], checked=NODE_CHECKED)
    check_runtime(decode, decode["devices"][0], ctx, bound)
    # Prefill routes are judged as they will be once the decode processes run: their charge is taken out of the free
    # RAM figures and every route they serve is running (no charge, credited by plan id).
    _, decode_charge = reader.admissible(recipe, decode, pre, by_id, recipe_sha)
    after = deepcopy(availability)
    for host, charge in decode_charge.items():
        if after["hosts"].get(host, {}).get("free_ram_gib") is not None:
            after["hosts"][host]["free_ram_gib"] -= charge / GIB
    running = [running_entry(rid, recipe_sha, reqs) for rid in process_routes(recipe, bound, decode)]
    state = admission_state(bound, after, ctx, running=running, requirements=reqs)
    prefill, service_flags = [], None
    for route in recipe["routes"]:
        if route["phase"] != "prefill":
            continue
        place = classify(route, bound, decode)
        if place == "local":
            check_bounds(route, decode["devices"][0], "node", ctx)
        reason, charge = reader.admissible(recipe, route, state, by_id, recipe_sha)
        if place == "offload":
            sid = route["devices"][0]
            host = ctx.pc
            commit = state["hosts"].get(host, {}).get("free_commit_gib")
            floor = (ctx.limits or {}).get("commit_floor_gib")
            # cudaHostAlloc pins committed memory: the service's charge must fit free commit too (design §10.8).
            if reason is None and commit is not None and floor is not None and commit - charge.get(host, 0) / GIB < floor:
                reason = (f"host {host}: free commit {commit:.1f} GiB - charge {charge.get(host, 0) / GIB:.1f} GiB "
                          f"< commit floor {floor:g} GiB")
            if bound["roles"][sid] == "idle":
                reason = reason or bound["why"][sid]
            elif PARKED_IMPORT not in ctx.capabilities:
                raise RecipeError(f"routes.{route['id']}: the decode engine of profile {ctx.profile} has no parked "
                                  "import contract; offload routes are not admitted for it")
            else:
                check_bounds(route, sid, "service", ctx)
                flags = launch_flags(route, sid, SERVICE_FIELDS.get(ctx.service["engine"], {}),
                                     f"the {ctx.service['engine']} service", sets[sid]["memory_min_gib"])
                service_flags = flags if service_flags is None else service_flags
        prefill.append({"id": route["id"], "place": place, "admitted": reason is None, "why": reason})
    offload = [p for p in prefill if p["place"] == "offload"]
    if ctx.service is None:
        service = {"start": False, "why": "the profile has no prefill service"}
    elif not offload:
        service = {"start": False, "why": "no recipe route uses the prefill service"}
    elif not any(p["admitted"] for p in offload):
        service = {"start": False, "why": "; ".join(f'{p["id"]}: {p["why"]}' for p in offload)}
    else:
        service = {"start": True, "why": None, "flags": service_flags or {}}
    if not any(p["admitted"] for p in prefill):
        raise RecipeError("no admissible prefill route at up (" +
                          "; ".join(f'{p["id"]}: {p["why"]}' for p in prefill) + ")")
    return {"decode": {"id": decode["id"], "flags": decode_flags,
                       "runtime_checked": runtime_checked(decode, decode["devices"][0])},
            "prefill": prefill, "service": service,
            "running": running, "requirements": reqs, "effective_sha256": recipe_sha}


# --- router table and state ---------------------------------------------------------------------------------------

def route_table(eff_json, recipe_sha256, bound, decision, state_file, ctx, service_started=True):
    """`tensormeld/prefill-route-table-v1`: the decision table the router evaluates per request (core §7 over the prefill
    routes): per route the sets and links it needs (handoff route included, internal links of multi-host sets), its
    context/batch bounds, the handoff route's prompt bounds, the per-host charge of its members' requirements, and
    which process runs it (charged 0 while that process is up)."""
    by_id = {r["id"]: r for r in eff_json["routes"]}
    sets = {d["id"]: d for d in eff_json["match"]["devices"]}
    decode = by_id[decision["decode"]["id"]]
    routes = []
    for route in eff_json["routes"]:
        if route["phase"] != "prefill":
            continue
        place = classify(route, bound, decode)
        involved = [route] + ([by_id[route["handoff_route"]]] if route["handoff_route"] else [])
        route_sets = list(dict.fromkeys(s for r in involved for s in r["devices"]))
        links = list(dict.fromkeys(l for r in involved for l in r["links"]))
        links += [f"{s}.internal" for s in route_sets if sets[s]["internal_link"] is not None
                  and len({m["host"] for m in bound["binding"][s]}) >= 2]
        charge, commit = {}, {}
        for req in decision["requirements"]:
            if req["route"] == route["id"]:
                host = bound["binding"][req["set"]][req["member"]]["host"]
                charge[host] = charge.get(host, 0.0) + req["host_bytes"] / GIB
                commit[host] = commit.get(host, 0.0) + req["commit_bytes"] / GIB
        hosts = list(dict.fromkeys(m["host"] for s in route_sets for m in bound["binding"][s]))
        disk_members = sorted({r["id"] for r in decision["requirements"]
                               if r["route"] in {x["id"] for x in involved} and r.get("disk_bytes", 0) > 0})
        handoff = by_id[route["handoff_route"]]["when"] if route["handoff_route"] else None
        routes.append({"id": route["id"], "place": place,
                       # An offload route without a started service never admits (the nodes take every prompt).
                       "enabled": route["enabled"] and (place == "local" or service_started)
                       and (route["handoff_route"] is None or by_id[route["handoff_route"]]["enabled"]),
                       "when": {k: route["when"][k] for k in ("prompt_tokens_min", "prompt_tokens_max")},
                       "handoff_when": None if handoff is None else {
                           k: handoff[k] for k in ("prompt_tokens_min", "prompt_tokens_max")},
                       "max_context_tokens": min(r["placement"][s]["max_context_tokens"] for r in involved
                                                 for s in r["devices"]),
                       "fallback": route["fallback"], "sets": route_sets, "links": links, "hosts": hosts,
                       "disk_members": disk_members,
                       "charges": {h: round(g, 6) for h, g in sorted(charge.items())},
                       "commit_charges": {h: round(g, 6) for h, g in sorted(commit.items())},
                       "running_while": "decode" if place == "local" else "service"})
    limits = ctx.limits or {}
    return {"schema": ROUTE_TABLE_SCHEMA, "recipe": eff_json["name"], "recipe_sha256": recipe_sha256,
            "effective_sha256": decision["effective_sha256"], "floor_gib": eff_json["limits"]["host_free_ram_floor_gib"],
            "commit_floor_gib": eff_json["limits"]["host_free_commit_floor_gib"],
            "binding": deepcopy(bound["binding"]), "roles": dict(bound["roles"]), "routes": routes,
            "default_generation_tokens": limits["default_generation_tokens"],
            "state": {"file": str(state_file), "max_age_s": STATE_MAX_AGE_S},
            "offload_max_wait_s": OFFLOAD_MAX_WAIT_S}


def route_state(availability, at):
    """The router's availability file, stamped with ``at`` (epoch seconds)."""
    return {"schema": ROUTE_STATE_SCHEMA, "at": at, "devices": deepcopy(availability["devices"]),
            "links": deepcopy(availability["links"]), "hosts": deepcopy(availability["hosts"])}
