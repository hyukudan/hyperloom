"""`hyperloom up <profile> --recipe FILE|auto`: everything decided before any lease (design §9.6), the checks right
before and after the RTX service starts, the nodes' floors after their engines load, and the router's route table and
state file."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time

from .. import engines, render, rtx
from ..config import ConfigError
from . import apply, commands, core, descriptors, facts, probe, runtime, session
from .reader import RecipeError

STUB_ENV = "HYPERLOOM_RECIPE_STUB"
NODE_POLL_S = 3.0


@dataclass
class Options:
    recipe: str                      # a file path or "auto"
    layers: session.Layers = field(default_factory=session.Layers)
    dirs: list = field(default_factory=list)
    # The snapshot the launch reads (service.launch_inputs): [{role, original, id, path, sha256}]; with exact_dirs,
    # `auto` picks among exactly ``dirs`` (snapshotted candidates) instead of the live recipe directories.
    inputs: list = field(default_factory=list)
    exact_dirs: bool = False
    digest: str | None = None
    pin: Path | None = None          # the launch's pin on its snapshot (service.acquire_pin), released after up
    components: dict | None = None  # local cache sidecar; never inserted in the recipe or desc-v1
    selected_content: dict | None = None
    placement_inputs: dict | None = None  # per-route cached configuration references, never acquisition requests


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
    layers: session.Layers | None = None
    inputs: list = field(default_factory=list)
    digest: str | None = None
    placement_request: runtime.Request | None = None

    def record(self):
        """What the profile state keeps about the recipe (shown by `hyperloom status` and `recipe active`): the
        decision, the inputs as snapshotted, the layers, and the effective recipe with its explain trace as launched."""
        e = self.evaluation
        same = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))  # noqa: E731
        chosen = [i for i in self.inputs if i["role"] != "candidate" or same(i["path"], e.path)]
        return dict(e.summary(), production=self.production,
                    service={"start": self.service_start, "why": self.service_why,
                             "vram_cap_gib": self.vram_cap_gib, "host_gib": self.host_gib, "plan": self.plan},
                    route_table={"effective_sha256": self.table["effective_sha256"],
                                 "state_file": self.table["state"]["file"],
                                 "routes": [{"id": r["id"], "enabled": r["enabled"]} for r in self.table["routes"]]},
                    inputs=[dict(i, role="recipe") if i["role"] == "candidate" else i for i in chosen],
                    inputs_digest=self.digest,
                    layers=None if self.layers is None else {
                        "profile": str(self.layers.profile) if self.layers.profile else None,
                        "overrides": [str(o) for o in self.layers.overrides],
                        "set": [[path, value] for path, value in self.layers.sets],
                        "workload": self.layers.workload},
                    effective=e.effective.to_json(), explain=e.effective.trace)


def _backend(cluster):
    """(backend, production): the core when installed; the stub only with HYPERLOOM_RECIPE_STUB=1, non-production."""
    chosen = core.backend(cluster)
    if chosen.authoritative:
        return chosen, True
    if os.environ.get(STUB_ENV) == "1":
        chosen.label = "stub (HYPERLOOM_RECIPE_STUB=1, non-production)"
        return chosen, False
    raise RecipeError("up --recipe needs the strix-stream recipe core (ts-recipe or ss-recipe: pc.recipe_core, "
                      f"HYPERLOOM_TS_RECIPE, HYPERLOOM_SS_RECIPE or PATH); {STUB_ENV}=1 runs the stub reader as a "
                      "non-production launch")


def check_router():
    """Refuse when the pinned TensorMeld router cannot apply a recipe's route table."""
    if "routes" not in render.router_policies():
        raise RecipeError("the pinned TensorMeld router has no prefill policy \"routes\" (recipes are applied per request "
                          "by it); update third_party/tensormeld")


def refusal_of(evaluation):
    """Why `up` refuses this evaluation (hardware match, then the decision), or None."""
    if evaluation.problems:
        return "recipe does not match this hardware: " + "; ".join(evaluation.problems)
    if evaluation.refusal:
        return "recipe refused: " + evaluation.refusal
    return None


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
    engine = (data.get("placement") or {}).get("engine")
    if engine in rtx.WHOLE_GIB_HOST and host != int(host):
        return None, f"the {engine} service takes --host-gib in whole GiB; host_pinned_gib {host:g} cannot be launched exactly"
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
    if plan is None:
        return None, None
    rtx.exact_host(plan, host)
    return plan.to_dict(), None


def prepare(cluster, profile, options, *, router, vram_cap_gib, state_dir, source=None, reviewed=None):
    """Resolve, probe, match, plan and decide; refuse with every reason before any lease or process exists."""
    if options.components is not None:
        commands.components_store().load(options.components, selected=options.selected_content)
    backend, production = _backend(cluster)
    if vram_cap_gib is not None:
        raise RecipeError("--vram-cap-gib and --recipe both set the RTX cap: put it in the recipe (device_gib)")
    from . import preflight
    placement = preflight.includes(options)
    if not placement:
        check_router()
    source = source or commands.live_source(cluster)
    with ExitStack() as stack:
        owners, _, _ = preflight._reviewed_resources(reviewed, cluster) if placement and reviewed else ({}, {}, {})
        observer = preflight._open_observer(stack, None) if owners else None
        if options.recipe == "auto":
            dirs = options.dirs if options.exact_dirs else commands.recipe_dirs(cluster, options.dirs)
            chosen, rows = session.pick(cluster, profile, dirs, options.layers, source, backend,
                                        **({"reviewed": reviewed} if reviewed is not None else {}),
                                        **({"observer": observer} if observer is not None else {}))
            if chosen is None:
                raise RecipeError("no recipe fits: " + "; ".join(f"{r['path']}: {r['result']}" for r in rows)
                                  if rows else "no recipe files in " + ", ".join(map(str, commands.recipe_dirs(cluster))))
            evaluation = chosen
        else:
            evaluation = session.evaluate(cluster, profile, options.recipe, options.layers, source, backend,
                                          **({"placement_inputs": options.placement_inputs}
                                             if options.placement_inputs is not None else {}),
                                          **({"reviewed": reviewed} if reviewed is not None else {}),
                                          **({"observer": observer} if observer is not None else {}))
        why = refusal_of(evaluation)
        if why is not None:
            raise RecipeError(why)
        if evaluation.decision.get("placement"):
            if router != "local":
                raise RecipeError("the placement decode replica is loopback-only: use --router local")
            request = runtime.request(evaluation.decision)
            # Pure render (including artifact/release checks) before any state, helper or gate exists.
            spec = render_placement(cluster, profile, request, api_key="0" * 64, owners=owners, observer=observer)
            table = {"effective_sha256": evaluation.effective_sha256, "state": {"file": None}, "routes": []}
            return Launch(evaluation, production and not evaluation.effective.untuned, True, None, None, None,
                          table, spec.placement, options.layers, options.inputs, options.digest, request)
    if placement:
        check_router()
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
        elif plan is not None and plan.get("disk_gib", 0) > 0:
            # The planned file tier: those members need their disk available at admission (core §7.2).
            offload = {p["id"] for p in decision["prefill"] if p["place"] == "offload"}
            sid = next(s_ for s_, r in evaluation.bound["roles"].items() if r == "service")
            decision["requirements"] = apply.with_disk(decision["requirements"], offload, sid,
                                                       plan["disk_gib"] * apply.GIB, evaluation.effective_sha256)
    table = apply.route_table(evaluation.effective.to_json(), evaluation.effective.file_sha256, evaluation.bound,
                              decision, Path(state_dir) / "route-state.json", evaluation.context,
                              service_started=service["start"])
    # The pinned router's own parser, before any lease (the table is refused here, not after the service started).
    why = render.check_route_table(table)
    if why is not None:
        raise RecipeError("the pinned TensorMeld router refuses the route table: " + why)
    return Launch(evaluation, production and not evaluation.effective.untuned, service["start"], service["why"],
                  cap, host, table, plan, options.layers, options.inputs,
                  options.digest)


def verify_launch(launch, selected, profile, processes, release_rows):
    """The final render `up` is about to start must be the launch whose engine declarations admission checked (the
    release id and key paths are normalized away), and each declaration must come from the executable of the release
    being started (``release_rows``: {machine: its release listing row}); refused before any lease otherwise. Only
    when admission checked them (a recipe workspace_gib set on the nodes): saved declarations are otherwise not part
    of the decision."""
    if not ((launch.evaluation.decision or {}).get("decode") or {}).get("runtime_checked"):
        return
    rows = (launch.evaluation.context.limits or {}).get("node_runtime") or {}
    checked = {mid: row for mid, row in rows.items() if "gib" in row}
    if not checked:
        return
    final = {mid: sha for mid, (_, sha, _) in facts.launch_identities(selected, profile, processes).items()}
    changed = sorted(mid for mid, row in checked.items() if final.get(mid) != row["launch_sha256"])
    if changed:
        raise RecipeError("the launch about to start differs from the one its engine declarations were checked for on "
                          + ", ".join(changed) + " (diagnostics or a changed flag); nothing was started")
    binary = engines.release_adapter(selected.profiles[profile].get("preset", profile)).binary
    for mid, row in sorted(checked.items()):
        built = (((release_rows.get(mid) or {}).get("manifest") or {}).get("binaries") or {}).get(binary)
        if built is None or row.get("executable_sha256") != built:
            raise RecipeError(f"{mid}: the engine declaration was made by executable "
                              f"{str(row.get('executable_sha256'))[:12]}, not the {binary} of the release being started "
                              f"({str(built)[:12]}); run the describe command with the installed binary; nothing was "
                              "started")


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


def render_placement(cluster, profile, request, *, api_key, owners=None, observer=None):
    """Pure rendering of already admitted local artifacts, before any launch-state or lease mutation."""
    return rtx.render_service(cluster, profile, api_key=api_key, handoff_api_key="0" * 64,
                              free_physical_gib=0, placement_request=request, placement_owners=owners,
                              placement_observer=observer)


def selection(cluster, profile, launch, *, router="local", resources=None):
    """The selected input (using its original path across snapshots), kind and admission resources."""
    from .. import admission, orchestrate
    path = launch.evaluation.path
    key = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    for item in launch.inputs:
        if item["role"] in ("recipe", "candidate") and key(item["path"]) == key(path):
            path = item["original"]
            break
    placement = launch.placement_request is not None
    if resources is None:
        if placement:
            resources = {launch.evaluation.context.pc}
        else:
            try:
                resources = admission.profile_resources(cluster, profile, orchestrate, router)
            except Exception:
                resources = {"*"}
    return {"path": key(path), "effective_sha256": launch.evaluation.effective_sha256,
            "kind": "placement" if placement else "legacy", "resources": sorted(resources)}


def check_admission(cluster, profile, launch, admitted, *, router="local"):
    """Fresh preparation may refuse a launch, but may never change what the existing hold admitted."""
    if admitted is not None:
        # A conservative wildcard hold remains a wildcard after resolving the snapshot/profile.
        resources = {"*"} if admitted["resources"] == ["*"] else None
        if selection(cluster, profile, launch, router=router, resources=resources) != admitted:
            raise RecipeError("recipe selection changed after admission; retry")


def up_placement(cluster, profile, launch, *, deadline_s, cancelled=None, admitted_selection=None):
    """The RTX-alone decode path: no remote engines or parked-import/prefill router are involved."""
    from dataclasses import replace
    import uuid
    from .. import orchestrate, procs, secrets

    check_admission(cluster, profile, launch, admitted_selection)
    request = launch.placement_request
    spec = render_placement(cluster, profile, request, api_key="0" * 64)
    spec = replace(spec, deadline=min(spec.deadline, time.time() + deadline_s))
    path = orchestrate._state_path(profile)
    lock = procs.InstanceLock(path.with_suffix(".lock")).acquire()
    handlers = ExitStack()
    owned = False
    state = {"profile": profile, "leases": [], "router": None, "machines": {}, "keys": [],
             "known_hosts": str(orchestrate._known_hosts()), "token": uuid.uuid4().hex,
             "phase": "starting", "production": launch.production, "diagnostic": [],
             "recipe": launch.record(), "deadline": spec.deadline, "started_at": time.time(),
             "nodes": [], "devices": list(spec.placement_supervision["caps_bytes"])}
    from . import service
    if service.configured_file() is not None:
        state["config"] = orchestrate.config_record(service.configured_file())

    def check():
        if cancelled is not None and cancelled():
            raise orchestrate.OrchestrationError("profile stop requested")
        orchestrate._check_stop(path)
    try:
        if path.exists():
            raise orchestrate.OrchestrationError("profile has saved running state; run down before up")
        if cancelled is not None and cancelled():
            raise orchestrate.OrchestrationError("profile stop requested")
        path.with_suffix(".stop").unlink(missing_ok=True)
        handlers.enter_context(orchestrate._up_handlers(path))
        request.files.publish()
        owned = True
        orchestrate._write(path, state)
        orchestrate._start_up_watch(path, state["token"])
        state["prefill_service"] = rtx.prepare_service(spec)
        orchestrate._write(path, state)
        rtx.launch_service(state["prefill_service"], spec, detached=orchestrate._detached,
                           owner={"state_path": str(path), "token": state["token"]}, check=check)
        check()
        state["phase"] = "running"
        orchestrate._write(path, state)
        return state
    except BaseException:
        if owned:
            try:
                orchestrate._down_state(state)
                path.unlink(missing_ok=True)
            except Exception:
                orchestrate._write(path, state)
        raise
    finally:
        handlers.close()
        lock.close()


def route_state_spec(launch, service_record, reservation_end, state):
    """What the router helper needs to write the route state every second, including how to reach the nodes."""
    e = launch.evaluation
    cuda = [m["id"] for s, members in e.bound["binding"].items() if e.bound["roles"][s] == "service" for m in members]
    idle = [m["id"] for s, members in e.bound["binding"].items() if e.bound["roles"][s] == "idle" for m in members]
    disk = sorted({m for r in launch.table["routes"] for m in r["disk_members"]})
    return {"file": launch.table["state"]["file"], "service_dir": service_record["directory"] if service_record else None,
            "uuids": cuda, "idle": idle, "nodes": list(e.context.nodes), "pc": e.context.pc,
            "disk": {m: state.get("disk_paths", {}).get(m) for m in disk},
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
    for uuid in spec.get("idle", []):
        devices[uuid] = {"available": False, "why": "not run by this deployment"}
    for member, model in (spec.get("disk") or {}).items():
        # The member's planned file tier reads its model file: available while the file is there.
        devices.setdefault(member, {"available": False, "why": "not bound"})["disk_available"] = bool(
            model and os.path.isfile(model))
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
