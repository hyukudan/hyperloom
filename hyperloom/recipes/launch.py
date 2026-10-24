"""`hyperloom up <profile> --recipe FILE|auto`: everything decided before anything starts (design §9.6), the checks
right before and after the RTX service starts, and the router's route table and state file."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from .. import render, rtx
from . import apply, commands, core, descriptors, facts, session
from .reader import RecipeError

STUB_ENV = "HYPERLOOM_RECIPE_STUB"


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
    table: dict | None

    def record(self):
        """What the profile state keeps about the recipe (shown by `hyperloom status`)."""
        e = self.evaluation
        return dict(e.summary(), production=self.production,
                    service={"start": self.service_start, "why": self.service_why,
                             "vram_cap_gib": self.vram_cap_gib, "host_gib": self.host_gib},
                    route_table=None if self.table is None else {
                        "effective_sha256": self.table["effective_sha256"], "state_file": self.table["state"]["file"],
                        "routes": [r["id"] for r in self.table["routes"]]})


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


def prepare(cluster, profile, options, *, router, vram_cap_gib, state_dir, source=None):
    """Resolve, probe, match and decide; refuse with every reason before any lease or process exists."""
    backend, production = _backend(cluster)
    if vram_cap_gib is not None:
        raise RecipeError("--vram-cap-gib and --recipe both set the RTX cap: put it in the recipe (device_gib)")
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
    service = decision["service"]
    if service["start"] and router != "local":
        raise RecipeError("the recipe starts the RTX prefill service, which is loopback-only: use --router local")
    flags = service.get("flags", {}) if service["start"] else {}
    table = None
    if service["start"] and "routes" not in render.router_policies():
        raise RecipeError("the pinned TensorMeld router has no prefill policy \"routes\" (needed by this recipe's offload "
                          "routes); update third_party/tensormeld")
    if service["start"]:
        table = apply.route_table(evaluation.effective.to_json(), evaluation.effective.file_sha256, evaluation.bound,
                                  decision, Path(state_dir) / "route-state.json", evaluation.context)
    return Launch(evaluation, production and not evaluation.effective.untuned, service["start"], service["why"],
                  flags.get("vram_cap_gib"), flags.get("host_gib"), table)


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
    e = launch.evaluation
    floor = e.effective.recipe["limits"]["host_free_ram_floor_gib"]
    reservation = cluster.pc.get("cuda", {}).get("reservation") or {}
    commit_floor = reservation.get("free_commit_floor_gib", 4)
    charge = 0.0
    if before:
        offload = {p["id"] for p in e.decision["prefill"] if p["place"] == "offload"}
        per_route = {}
        for req in e.decision["requirements"]:
            host = e.bound["binding"][req["set"]][req["member"]]["host"]
            if req["route"] in offload and host == e.context.pc:
                per_route[req["route"]] = per_route.get(req["route"], 0.0) + req["host_bytes"] / apply.GIB
        charge = max(per_route.values(), default=0.0)
    status = memory()
    free, commit = status["free_physical_gib"], status["free_commit_gib"]
    if free - charge < floor:
        return f"free RAM {free:.1f} GiB - charge {charge:.1f} GiB < floor {floor:g} GiB"
    if commit - charge < commit_floor:
        return f"free commit {commit:.1f} GiB - charge {charge:.1f} GiB < commit floor {commit_floor:g} GiB"
    return None


def router_prefill(launch, cluster, profile, key_file):
    """The router's prefill block for a recipe launch: policy routes with the service, or plain nodes when the service
    is not started (every prefill is local; nothing to choose per request)."""
    if launch.table is None:
        return {"policy": "nodes"}
    data = rtx._service(cluster, profile)[1]
    return {"policy": "routes", "services": [rtx.router_service(cluster, profile, key_file)],
            "routes": launch.table, "handoff_path": data["handoff_path"],
            "wait_s": launch.table["offload_max_wait_s"]}


def route_state_spec(launch, service_record, reservation_end):
    """What the router helper needs to write the route state every second."""
    if launch.table is None:
        return None
    e = launch.evaluation
    return {"file": launch.table["state"]["file"], "service_dir": service_record["directory"] if service_record else None,
            "uuid": e.context.service["uuid"], "nodes": list(e.context.nodes), "pc": e.context.pc,
            "links": [l["id"] for l in e.effective.recipe["match"]["links"]]
            + [f"{d['id']}.internal" for d in e.effective.recipe["match"]["devices"] if d["internal_link"] is not None],
            "reservation_end": reservation_end}


def write_route_state(spec, *, memory=None, now=None):
    """One route-state snapshot: the PRO available while our service is ready and the reservation lasts; the nodes and
    links available (the router ANDs its own replica health); the control machine's free RAM now."""
    now = time.time() if now is None else now
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
    devices = {spec["uuid"]: {"available": available, **({"why": why} if why else {})}}
    devices.update({mid: {"available": True} for mid in spec["nodes"]})
    data = {"schema": apply.ROUTE_STATE_SCHEMA, "at": now, "devices": devices,
            # Node sets, the PC-node link and the pair's own link are ANDed with the router's replica health.
            "links": {lid: {"up": True} for lid in spec["links"]},
            "hosts": {spec["pc"]: {"free_ram_gib": status["free_physical_gib"],
                                   "free_commit_gib": status["free_commit_gib"]}}}
    path = Path(spec["file"])
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)
    return data


def planning(cluster):
    """Plans inside a recipe launch read saved header descriptors only (design §10.9)."""
    return descriptors.saved_only(commands.store())
