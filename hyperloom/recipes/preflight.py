"""Placement admission before holds, snapshot pins, progress files, leases or launch state.

Only cached JSON/stat and read-only hardware queries are permitted here. Tierstream owns capability and budget
validation. The qualified adapter remains a separate release prerequisite; no preflight opens an operator gate.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from .. import gpu_counters, placement_supervision, procs, rtx
from . import commands, content_cache, grants, reader, session, sysmem
from .reader import RecipeError


def check(cluster, path, layers, backend, recipe, bound, report, raw, effective_sha, *, inputs=None,
          cache=None, observer_factory=None, busy=None, now=None, admission=None, identity=None, reviewed=None):
    """All Hyperloom-determinable placement refusals. No leases, locks, gates, helpers or children."""
    now = time.time() if now is None else now
    used = grants.check(cluster, recipe, bound["binding"], report, raw["hosts"], now=now, anchors_only=True)
    rows = {grants.uuid_key(r.get("uuid", "")): r for r in report["devices"]}
    for uuid in used:
        row = rows[uuid]
        if any(not row.get(k) or row[k] == "unknown" for k in ("uuid", "driver", "arch", "platform")):
            raise RecipeError("PL_E_STALE placement device identity is unknown")
        sysmem.require(raw.get("sysmem_policy", "unknown"), placement=True, platform=row["platform"])
    if inputs is None:
        if not content_cache._digest(effective_sha):
            raise RecipeError("PL_E_STALE placement needs a current effective recipe identity")
        inputs = content_cache._read(commands.runtime_dir() / "placement-inputs" / (effective_sha + ".json"),
                                     "PL_E_MISSING_DESCRIPTOR")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("routes"), dict):
        raise RecipeError("PL_E_MISSING_DESCRIPTOR invalid cached placement inputs")
    routes = inputs["routes"]
    cache = cache or commands.components_store()
    plans, policies = {}, {}
    used = {}
    for route in recipe["routes"]:
        if not route.get("enabled", True) or not reader.placement_mode(route):
            continue
        saved = routes.get(route["id"])
        if not isinstance(saved, dict):
            raise RecipeError("PL_E_MISSING_DESCRIPTOR missing cached configuration for " + route["id"])
        components = cache.load(saved.get("components"), selected=saved.get("selected_content"))
        # One authoritative place call per enabled configuration. No Python sizing/capability fallback.
        plans[route["id"]] = backend.place(path, layers, route=route["id"], probe=report, components=components,
                                            context=saved.get("context"), desc=saved.get("desc"),
                                            identity=identity).document
        plan = plans[route["id"]]
        if not isinstance(plan.get("devices"), list) or not plan["devices"] or not isinstance(plan.get("host"), dict):
            raise RecipeError("PL_E_SCHEMA core returned no touched devices or host budgets")
        policy = grants.policy(cluster, recipe, route, bound["binding"], report, plan, raw["hosts"], now=now)
        policies[route["id"]] = policy
        used.update({grants.uuid_key(g["uuid"]): g for g in policy["grants"]})
        for device in plan["devices"]:
            row = rows.get(grants.uuid_key(device["uuid"]), {})
            if any(not row.get(k) or row[k] == "unknown" for k in ("uuid", "driver", "arch", "platform")):
                raise RecipeError("PL_E_STALE placement device identity is unknown")
            sysmem.require(raw.get("sysmem_policy", "unknown"), placement=True, platform=row["platform"])
        pinned, pageable = policy["host_pinned_gib"], policy["host_pageable_gib"]
        hosts = {m["host"] for sid in route["devices"] for m in bound["binding"][sid]}
        for host in hosts:
            memory = raw["hosts"].get(host, {})
            status = {"free_physical_gib": memory.get("free_ram_gib", float("nan")),
                      "free_commit_gib": memory.get("free_commit_gib", float("nan"))}
            try:
                placement_supervision.validate(policy, now=now, starting=True, memory=status)
                floor = max(recipe["limits"]["host_free_ram_floor_gib"],
                            max((p.get("host_available_floor_gib", 0) for p in route["placement"].values()), default=0))
                if status["free_physical_gib"] - pinned - pageable < floor:
                    raise rtx.ServiceError("placement host charge would cross available RAM floor")
            except (rtx.ServiceError, ValueError, TypeError) as exc:
                raise RecipeError("PL_E_FREE " + str(exc)) from exc
    owners, ports = _reviewed_resources(reviewed)
    for uuid in sorted(used):
        # Probe an existing OS lock only. A missing lock must not create the runtime directory or lock file.
        if uuid not in owners and rtx.device_lock_path(used[uuid]["uuid"]).exists():
            lock = rtx.device_lock(used[uuid]["uuid"])
            try:
                lock.acquire(create=False)
            except FileNotFoundError:
                pass  # disappeared since exists(); the helper takes a fresh lifetime lock
            except (procs.ProcessError, OSError) as exc:
                raise RecipeError("PL_E_BUSY device lock is already held or unavailable") from exc
            finally:
                lock.close()
        try:
            reason = (_handover_busy(used[uuid]["uuid"], owners[uuid], observer_factory)
                      if uuid in owners else (busy or rtx.pro_busy)(used[uuid]["uuid"]))
        except rtx.ServiceError as exc:
            raise RecipeError("PL_E_BUSY device idle state unknown: " + str(exc)) from exc
        if reason:
            raise RecipeError("PL_E_BUSY " + reason)
    port = inputs.get("port", 8096)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RecipeError("PL_E_SCHEMA placement port must be an integer in 1..65535")
    try:
        procs.check_port("127.0.0.1", port)
    except procs.ProcessError as exc:
        if not procs.port_owned("127.0.0.1", port, ports.get(port, ())):
            raise RecipeError("PL_E_BUSY " + str(exc)) from exc
    observer = None
    try:
        observer = (observer_factory or gpu_counters.Observer)()
        sample = observer.sample(os.getpid())
        if not used.keys() <= sample.keys() or any(type(v) is not int or v < 0 for v in sample.values()):
            raise gpu_counters.CounterError("attributable GPU counters unavailable")
    except (gpu_counters.CounterError, OSError) as exc:
        raise RecipeError("PL_E_CAPABILITY " + str(exc)) from exc
    finally:
        if observer is not None:
            observer.close()
    if admission is not None:
        admission.update(inputs=inputs, policies=policies)
    return plans


def _reviewed_resources(reviewed):
    """Only a reviewed controller's ready, live helper may account for its device locks and child port."""
    owners, ports = {}, {}
    for state in (reviewed or {}).values():
        record = state.get("prefill_service") or {}
        if not state.get("token") or not record.get("directory") or type(record.get("pid")) is not int:
            continue
        directory = Path(record["directory"])
        try:
            live = rtx._read(directory / "state.json")
            if live.get("state") != "ready" or live.get("pid") != record["pid"]:
                continue
            helper = procs.InstanceLock(directory / "helper.lock")
            try:
                helper.acquire(create=False)
            except procs.ProcessError:
                pass  # A ready helper holds every device lock until its child has stopped.
            else:
                continue  # A stale ready.json / controller record proves no ownership.
            finally:
                helper.close()
        except (OSError, ValueError):
            continue
        devices = live.get("device", [])
        devices = devices if isinstance(devices, list) else [devices]
        uuids = {grants.uuid_key(d.get("uuid", "")) for d in devices}
        expected = set((record.get("placement_supervision") or {}).get("caps_bytes", {}))
        expected = {grants.uuid_key(u) for u in expected} or {grants.uuid_key(record.get("device", ""))}
        if "" in expected or uuids != expected:
            continue
        for uuid in uuids:
            owners.setdefault(uuid, set()).add(record["pid"])
        ports.setdefault(record.get("port"), set()).add(record["pid"])
    return owners, ports


def _handover_busy(uuid, pids, observer_factory):
    """Subtract only the reviewed child's attributable bytes for this idle check, never for a placement cap."""
    key = grants.uuid_key(uuid)
    apps = rtx.compute_apps()
    foreign = [pid for gpu, pid, used in apps if grants.uuid_key(gpu) == key and pid not in pids
               and used is not None and used > rtx.PRO_IDLE_MIB]
    if foreign:
        return f"foreign compute processes {sorted(foreign)}"
    observer = None
    try:
        observer = (observer_factory or gpu_counters.Observer)()
        owned = 0
        for pid in pids:
            sample = observer.sample(pid)
            if key not in sample or type(sample[key]) is not int or sample[key] < 0:
                raise gpu_counters.CounterError("reviewed process GPU counters unavailable")
            owned += sample[key]
        residual = rtx.device_used_mib(uuid) * (1 << 20) - owned
        if residual > rtx.PRO_IDLE_MIB * (1 << 20):
            return "device memory not attributable to the reviewed deployment"
    except (gpu_counters.CounterError, OSError) as exc:
        raise rtx.ServiceError(str(exc)) from exc
    finally:
        if observer is not None:
            observer.close()
    return None


def includes(options):
    """Conservative routing check, never an admission decision or a second placement planner."""
    candidates = (session.candidates(options.dirs) if options.recipe == "auto" else
                  [{"recipe": reader.parse(session.read_text(options.recipe), options.recipe), "error": None}])
    for candidate in candidates:
        if candidate["error"]:
            continue
        recipe = candidate["recipe"]
        if any(reader.placement_mode(r) for r in recipe["routes"]):
            return True
    # Read only the layer's field names: profile freshness and values remain the authoritative reader's job.
    fields = [field for field, _ in options.layers.sets]
    for path in ([options.layers.profile] if options.layers.profile else []) + list(options.layers.overrides):
        fields.extend(reader.load_text(session.read_text(path), str(path)).get("set", {}))
    return any("components" in field.split(".") or "assist" in field.split(".") for field in fields)


def before_options(cluster, profile, options, *, router="local", cap=None, source=None, reviewed=None):
    if not includes(options):
        return
    from .. import orchestrate
    from . import launch
    orchestrate.check_launch(cluster, profile, router, recipe=True)
    launch.prepare(cluster, profile, options, router=router, vram_cap_gib=cap,
                   state_dir=orchestrate._state_path(profile).parent, source=source,
                   **({"reviewed": reviewed} if reviewed is not None else {}))


def snapshot(digest, profile=None, *, source=None, stop=None):
    from . import service
    try:
        cluster, intent, options = service.options_from_inputs(digest, profile)
    except service.NotFound:
        # No placement document exists to admit. Keep legacy missing-snapshot job/error handling unchanged.
        return
    reviewed = None
    if stop is not None and includes(options):
        reviewed = service._states()
        if sorted(set(stop)) != sorted(reviewed):
            raise service.Conflict("the running profiles differ from the reviewed stop list: review the switch again")
    before_options(cluster, intent["profile"], options, router=intent["router"], source=source, reviewed=reviewed)


def cli(args, *, load_cluster=None):
    """Read and validate original inputs before CLI/portal admission can write its hold database."""
    command = getattr(args, "command", None)
    deploy = command == "recipe" and getattr(args, "operation", None) == "deploy"
    if not deploy and (command != "up" or not (args.recipe or args.recipe_inputs)):
        return
    from .. import cli as main_cli
    from . import launch, service
    with service.configured(args.config):
        if deploy or args.recipe_inputs:
            snapshot(args.inputs if deploy else args.recipe_inputs, None if deploy else args.profile,
                     **({"stop": args.stop or []} if deploy else {}))
            return
        cluster = (load_cluster or main_cli._cluster)(args.config)
        layers = session.Layers(profile=args.recipe_profile, overrides=list(args.recipe_overrides or []),
                                sets=[reader.parse_set(s) for s in args.recipe_set or []],
                                workload=args.recipe_workload or "default")
        dirs = service.candidate_dirs(cluster, args.recipe_dir or []) if args.recipe == "auto" else []
        path = "auto" if args.recipe == "auto" else str(service.locate(args.recipe, cluster, for_up=True))
        profile = args.profile if path == "auto" else service.deployment_profile(cluster, path, args.profile)
        before_options(cluster, profile, launch.Options(path, layers, dirs), router=args.router, cap=args.vram_cap_gib)
