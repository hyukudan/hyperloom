"""`hyperloom up <profile> --recipe FILE|auto`: everything decided before any lease (design §9.6), the checks right
before and after the RTX service starts, the nodes' floors after their engines load, and the router's route table and
state file."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from .. import render, rtx
from ..config import ConfigError
from . import apply, commands, core, descriptors, facts, probe, session
from .reader import RecipeError

STUB_ENV = "HYPERLOOM_RECIPE_STUB"
NODE_POLL_S = 3.0


@dataclass
class Options:
    recipe: str                      # a file path or "auto"
    layers: session.Layers = field(default_factory=session.Layers)
    dirs: list = field(default_factory=list)


@dataclass
class Launch:
    evaluation: session.Evaluation
    production: bool
    service_start: bool
    service_why: str | None
    vram_cap_gib: float | None
    host_gib: float | None
    table: dict
    plan: dict | None = None

    def record(self):
        """What the profile state keeps about the recipe (shown by `hyperloom status`)."""
        e = self.evaluation
        return dict(e.summary(), production=self.production,
                    service={"start": self.service_start, "why": self.service_why,
                             "vram_cap_gib": self.vram_cap_gib, "host_gib": self.host_gib, "plan": self.plan},
                    route_table={"effective_sha256": self.table["effective_sha256"],
                                 "state_file": self.table["state"]["file"],
                                 "routes": [{"id": r["id"], "enabled": r["enabled"]} for r in self.table["routes"]]})


def _backend(cluster):
    """(backend, production): the core when installed; the stub only with HYPERLOOM_RECIPE_STUB=1, non-production."""
    chosen = core.backend(cluster)
    if chosen.authoritative:
        return chosen, True
    if os.environ.get(STUB_ENV) == "1":
        chosen.label = "stub (HYPERLOOM_RECIPE_STUB=1, non-production)"
        return chosen, False
    raise RecipeError("up --recipe needs the strix-stream recipe core (ss-recipe: pc.recipe_core, "
                      f"HYPERLOOM_SS_RECIPE or PATH); {STUB_ENV}=1 runs the stub reader as a non-production launch")


def _service_budget(evaluation):
    """(cap, host) of the offload routes' service placement, as the recipe resolved them (defaults included)."""
    for p in evaluation.decision["prefill"]:
        if p["place"] == "offload":
            route = next(r for r in evaluation.effective.recipe["routes"] if r["id"] == p["id"])
            placement = route["placement"][route["devices"][0]]
            return placement["device_gib"], placement["host_pinned_gib"]
    return None, None


def plan_service(cluster, profile, cap, host, *, free_physical_gib=None):
    """Plan the RTX service at the recipe's budget before any lease: the reservation's cap, the host rule and the
    planner (saved header descriptors only). Returns (plan dict, None) or (None, why it cannot start)."""
    s, data, _ = rtx._service(cluster, profile)
    reservation = cluster.pc.get("cuda", {}).get("reservation")
    try:
        rtx.effective_cap(reservation, cap)
        free = rtx.memory_status()["free_physical_gib"] if free_physical_gib is None else free_physical_gib
        allowed = rtx.host_gib(data, reservation or {"host_gib": 4096}, free)
        if host > allowed:
            return None, (f"host_pinned_gib {host:g} is above what this start allows ({allowed:g} GiB: the preset's "
                          "maximum, the reservation and free RAM)")
        asset = cluster.assets.get(s["machine"], {}).get((data.get("placement") or {}).get("model_asset") or "")
        if asset is None:
            return None, "the service's model asset is not configured"
        positions = s.get("positions", data["positions"]["default"])
        with planning(cluster):
            plan = rtx.plan_placement(data, asset["path"], cap, host, positions)
    except (ConfigError, descriptors.DescriptorError) as exc:
        return None, str(exc)
    return (None if plan is None else plan.to_dict()), None


def prepare(cluster, profile, options, *, router, vram_cap_gib, state_dir, source=None):
    """Resolve, probe, match, plan and decide; refuse with every reason before any lease or process exists."""
    backend, production = _backend(cluster)
    if vram_cap_gib is not None:
        raise RecipeError("--vram-cap-gib and --recipe both set the RTX cap: put it in the recipe (device_gib)")
    if "routes" not in render.router_policies():
        raise RecipeError("the pinned TensorMeld router has no prefill policy \"routes\" (recipes are applied per request "
                          "by it); update third_party/tensormeld")
    source = source or commands.live_source(cluster)
    if options.recipe == "auto":
        chosen, rows = session.pick(cluster, profile, commands.recipe_dirs(cluster, options.dirs), options.layers,
                                    source, backend)
        if chosen is None:
            raise RecipeError("no recipe fits: " + "; ".join(f"{r['path']}: {r['result']}" for r in rows)
                              if rows else "no recipe files in " + ", ".join(map(str, commands.recipe_dirs(cluster))))
        evaluation = chosen
    else:
        evaluation = session.evaluate(cluster, profile, options.recipe, options.layers, source, backend)
    if evaluation.problems:
        raise RecipeError("recipe does not match this hardware: " + "; ".join(evaluation.problems))
    if evaluation.refusal:
        raise RecipeError("recipe refused: " + evaluation.refusal)
    decision = evaluation.decision
    service = dict(decision["service"])
    cap, host, plan = None, None, None
    if service["start"]:
        if router != "local":
            raise RecipeError("the recipe starts the RTX prefill service, which is loopback-only: use --router local")
        # The budget is forwarded exactly, zero included (Sol-5 #5); the planner runs now, before any lease (#2).
        cap, host = _service_budget(evaluation)
        plan, why = plan_service(cluster, profile, cap, host)
        if why is not None:
            service = {"start": False, "why": "planner: " + why}
            decision["service"] = service
    table = apply.route_table(evaluation.effective.to_json(), evaluation.effective.file_sha256, evaluation.bound,
                              decision, Path(state_dir) / "route-state.json", evaluation.context,
                              service_started=service["start"])
    return Launch(evaluation, production and not evaluation.effective.untuned, service["start"], service["why"],
                  cap, host, table, plan)


def recheck_before_service(launch, cluster, *, memory=None, pro_busy=None):
    """Immediately before the service starts: the PRO idle, the reservation valid, and the service's charge within
    free RAM and commit. Returns None (go) or why it is skipped; a failed device query skips too (fail closed)."""
    ctx = launch.evaluation.context
    try:
        busy = (pro_busy or rtx.pro_busy)(ctx.service["uuid"])
    except rtx.ServiceError as exc:
        return f"RTX PRO state unknown: {exc}"
    if busy:
        return "RTX PRO busy: " + busy
    reason = facts._reservation_problem(cluster, ctx, time.time())
    if reason:
        return reason
    return _memory_problem(launch, cluster, memory or rtx.memory_status, before=True)


def recheck_after_ready(launch, cluster, *, memory=None):
    """After the service's READY (its allocations done): free RAM and commit still above their floors, else why the
    service must be stopped."""
    return _memory_problem(launch, cluster, memory or rtx.memory_status, before=False)


def _memory_problem(launch, cluster, memory, *, before):
    """Free RAM against the recipe's floor; free commit against the reservation's commit floor (host bytes, as the
    watchdog measures it) and, when the recipe sets one, against its commit floor with the core's commit charge."""
    e = launch.evaluation
    limits = e.effective.recipe["limits"]
    reservation_floor = (cluster.pc.get("cuda", {}).get("reservation") or {}).get("free_commit_floor_gib", 4)
    host = commit = 0.0
    if before:
        offload = {p["id"] for p in e.decision["prefill"] if p["place"] == "offload"}
        per_host, per_commit = {}, {}
        for req in e.decision["requirements"]:
            where = e.bound["binding"][req["set"]][req["member"]]["host"]
            if req["route"] in offload and where == e.context.pc:
                per_host[req["route"]] = per_host.get(req["route"], 0.0) + req["host_bytes"] / apply.GIB
                per_commit[req["route"]] = per_commit.get(req["route"], 0.0) + req["commit_bytes"] / apply.GIB
        host, commit = max(per_host.values(), default=0.0), max(per_commit.values(), default=0.0)
    status = memory()
    free, free_commit = status["free_physical_gib"], status["free_commit_gib"]
    if free - host < limits["host_free_ram_floor_gib"]:
        return f"free RAM {free:.1f} GiB - charge {host:.1f} GiB < floor {limits['host_free_ram_floor_gib']:g} GiB"
    if free_commit - host < reservation_floor:
        return f"free commit {free_commit:.1f} GiB - charge {host:.1f} GiB < commit floor {reservation_floor:g} GiB"
    if limits["host_free_commit_floor_gib"] > 0 and free_commit - commit < limits["host_free_commit_floor_gib"]:
        return (f"free commit {free_commit:.1f} GiB - commit charge {commit:.1f} GiB < the recipe's commit floor "
                f"{limits['host_free_commit_floor_gib']:g} GiB")
    return None


def node_floors(launch, read_ram):
    """After the node engines are READY (their allocations done): every node keeps the recipe's free-RAM floor.
    ``read_ram(machine)`` -> free GiB or None. Returns why up must stop, or None."""
    floor = launch.evaluation.effective.recipe["limits"]["host_free_ram_floor_gib"]
    for mid in launch.evaluation.context.nodes:
        free = read_ram(mid)
        if free is None:
            return f"{mid}: free RAM not reported after the engine loaded"
        if free < floor:
            return f"{mid}: free RAM {free:.1f} GiB after the engine loaded < floor {floor:g} GiB"
    return None


def router_prefill(launch, cluster, profile, key_file):
    """The router's prefill block for a recipe launch: always the route table (node-only runs keep the recipe's context
    bounds and RAM floors per request); the service entry only when the service was started."""
    block = {"policy": "routes", "routes": launch.table, "wait_s": launch.table["offload_max_wait_s"]}
    if launch.service_start:
        data = rtx._service(cluster, profile)[1]
        block.update(services=[rtx.router_service(cluster, profile, key_file)], handoff_path=data["handoff_path"])
    return block


def route_state_spec(launch, service_record, reservation_end, state):
    """What the router helper needs to write the route state every second, including how to reach the nodes."""
    e = launch.evaluation
    cuda = [m["id"] for s, members in e.bound["binding"].items() if e.bound["roles"][s] != "decode" for m in members]
    return {"file": launch.table["state"]["file"], "service_dir": service_record["directory"] if service_record else None,
            "uuids": cuda, "nodes": list(e.context.nodes), "pc": e.context.pc,
            "links": [l["id"] for l in e.effective.recipe["match"]["links"]],
            "internal": [f"{d['id']}.internal" for d in e.effective.recipe["match"]["devices"]
                         if d["internal_link"] is not None],
            "reservation_end": reservation_end, "max_age_s": launch.table["state"]["max_age_s"],
            "machines": {mid: state["machines"][mid] for mid in e.context.nodes if mid in state.get("machines", {})},
            "known_hosts": state.get("known_hosts")}


def node_ram(gpu_result):
    """Free RAM (GiB) a node reports with the `gpu` verb, or None."""
    value = probe.node_device(gpu_result)["host_free_ram_gib"]["value"]
    return value


def write_route_state(spec, *, memory=None, now=None, nodes=None, replicas=None):
    """One route-state snapshot from measurements only (fail closed):
    - the PRO: available while our service is ready and the reservation lasts;
    - each node: available while it answered within max_age_s and the router reports its replica up;
    - the PC-node link and the pair's own link: up while the router reports the replica up (the router reaches it
      over that network; a TP pair does not serve without its link);
    - free RAM: the control machine now, each node from its last answer (omitted when older than max_age_s).
    ``nodes`` = {machine: {"free_ram_gib", "at"}}; ``replicas`` = {machine: "up" | other state} from the router."""
    now = time.time() if now is None else now
    nodes, replicas = nodes or {}, replicas or {}
    available, why = True, None
    if spec["service_dir"] is None:
        available, why = False, "prefill service not started"
    else:
        try:
            state = json.loads((Path(spec["service_dir"]) / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("state") != "ready":
            available, why = False, "prefill service " + str(state.get("state", "gone"))
    if available and spec["reservation_end"] is not None and spec["reservation_end"] <= now + 60:
        available, why = False, "reservation ends within 60 s"
    status = (memory or rtx.memory_status)()
    devices = {uuid: {"available": available, **({"why": why} if why else {})} for uuid in spec["uuids"]}
    hosts = {spec["pc"]: {"free_ram_gib": status["free_physical_gib"], "free_commit_gib": status["free_commit_gib"]}}
    all_up = bool(spec["nodes"]) and all(replicas.get(mid) == "up" for mid in spec["nodes"] if mid in replicas) \
        and any(mid in replicas for mid in spec["nodes"])
    for mid in spec["nodes"]:
        seen = nodes.get(mid)
        fresh = seen is not None and now - seen["at"] <= spec["max_age_s"] and seen.get("free_ram_gib") is not None
        replica = replicas.get(mid, "up" if all_up else None)
        if fresh and replica == "up":
            devices[mid] = {"available": True}
        else:
            devices[mid] = {"available": False, "why": "no recent answer from the node" if not fresh
                            else f"router reports the replica {replica or 'unknown'}"}
        if fresh:
            hosts[mid] = {"free_ram_gib": seen["free_ram_gib"]}
    link = {"up": True} if all_up else {"up": False, "why": "router reports the decode replica down"}
    data = {"schema": apply.ROUTE_STATE_SCHEMA, "at": now, "devices": devices,
            "links": {lid: dict(link) for lid in spec["links"] + spec["internal"]}, "hosts": hosts}
    path = Path(spec["file"])
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)
    return data


def planning(cluster):
    """Plans inside a recipe launch read saved header descriptors only (design §10.9)."""
    return descriptors.saved_only(commands.store())
