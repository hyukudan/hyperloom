"""Placement admission before holds, snapshot pins, progress files, leases or launch state.

Only cached JSON/stat and read-only hardware queries are permitted here. Tierstream owns capability and budget
validation. The qualified adapter remains a separate release prerequisite; no preflight opens an operator gate.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from .. import gpu_counters, placement_supervision, rtx
from . import commands, content_cache, grants, reader, session, sysmem
from .reader import RecipeError


def check(cluster, path, layers, backend, recipe, bound, report, raw, effective_sha, *, inputs=None,
          cache=None, observer_factory=None, busy=None, now=None):
    """All Hyperloom-determinable placement refusals. No leases, locks, gates, helpers or children."""
    now = time.time() if now is None else now
    used = grants.check(cluster, recipe, bound["binding"], report, raw["hosts"], now=now)
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
    plans = {}
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
                                            identity=dict(report, binding=bound["binding"])).document
        pinned = max((p["host_pinned_gib"] for p in route["placement"].values()), default=0)
        pageable = max((p.get("host_pageable_gib", 0) for p in route["placement"].values()), default=0)
        policy = {"grants": list(used.values()),
                  "caps_gib": {u: g["vram_cap_gib"] for u, g in used.items()},
                  "host_pinned_gib": pinned, "host_pageable_gib": pageable,
                  "host_grant": cluster.pc.get("cuda", {}).get("host_grant"),
                  "free_commit_floor_gib": max(4, recipe["limits"]["host_free_commit_floor_gib"])}
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
    for uuid in sorted(used):
        try:
            reason = (busy or rtx.pro_busy)(used[uuid]["uuid"])
        except rtx.ServiceError as exc:
            raise RecipeError("PL_E_BUSY device idle state unknown: " + str(exc)) from exc
        if reason:
            raise RecipeError("PL_E_BUSY " + reason)
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
    return plans


def includes(options):
    """Conservative routing check, never an admission decision or a second placement planner."""
    paths = ([p for d in options.dirs if Path(d).is_dir() for p in Path(d).iterdir()
              if p.suffix in session.SUFFIXES] if options.recipe == "auto" else [Path(options.recipe)])
    for path in paths:
        recipe = reader.parse(session.read_text(path), str(path))
        if any(reader.placement_mode(r) for r in recipe["routes"]):
            return True
    # Read only the layer's field names: profile freshness and values remain the authoritative reader's job.
    fields = [field for field, _ in options.layers.sets]
    for path in ([options.layers.profile] if options.layers.profile else []) + list(options.layers.overrides):
        fields.extend(reader.load_text(session.read_text(path), str(path)).get("set", {}))
    return any("components" in field.split(".") or "assist" in field.split(".") for field in fields)


def before_options(cluster, profile, options, *, router="local", cap=None, source=None):
    if not includes(options):
        return
    from .. import orchestrate
    from . import launch
    orchestrate.check_launch(cluster, profile, router, recipe=True)
    launch.prepare(cluster, profile, options, router=router, vram_cap_gib=cap,
                   state_dir=orchestrate._state_path(profile).parent, source=source)


def snapshot(digest, profile=None, *, source=None):
    from . import service
    try:
        cluster, intent, options = service.options_from_inputs(digest, profile)
    except service.NotFound:
        # No placement document exists to admit. Keep legacy missing-snapshot job/error handling unchanged.
        return
    before_options(cluster, intent["profile"], options, router=intent["router"], source=source)


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
            snapshot(args.inputs if deploy else args.recipe_inputs, None if deploy else args.profile)
            return
        cluster = (load_cluster or main_cli._cluster)(args.config)
        layers = session.Layers(profile=args.recipe_profile, overrides=list(args.recipe_overrides or []),
                                sets=[reader.parse_set(s) for s in args.recipe_set or []],
                                workload=args.recipe_workload or "default")
        dirs = service.candidate_dirs(cluster, args.recipe_dir or []) if args.recipe == "auto" else []
        path = "auto" if args.recipe == "auto" else str(service.locate(args.recipe, cluster, for_up=True))
        profile = args.profile if path == "auto" else service.deployment_profile(cluster, path, args.profile)
        before_options(cluster, profile, launch.Options(path, layers, dirs), router=args.router, cap=args.vram_cap_gib)
