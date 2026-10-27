"""`hyperloom recipe ready` and `hyperloom recipe matrix`: what is still missing before `up --recipe` can run a recipe,
as an ordered checklist with the next command for each missing step, and which recipe covers which hardware class.

Offline: the saved configuration and state only (no ssh, no HTTP, no model reads). Every refusal is the text validate,
plan and up give (their own functions are called); the hardware step evaluates the newest saved probe of the profile,
so it is as old as that probe. What only `up` itself can check is listed under ``checked_at_start``."""
from __future__ import annotations

from pathlib import Path
import time

from .reader import RecipeError

OK, TODO, BLOCKED, WARN, SKIPPED = "ok", "todo", "blocked", "warn", "skipped"
SAMPLE_CONFIG = "examples/cluster.example.json"


def _step(key, title, state, detail=None, fix=None):
    return {"step": key, "title": title, "state": state, "detail": detail, "fix": fix}


def _quote(value):
    value = str(value)
    return value if value and all(c.isalnum() or c in "._-/\\:" for c in value) else '"' + value.replace('"', '\\"') + '"'


def check(cluster, ref, profile=None, *, config=None, facts=None, now=None):
    """The checklist for recipe ``ref`` (an id or a file) on ``profile`` (default: the only profile serving its
    model). Steps after a failed one that depends on it are ``skipped`` with the reason."""
    from .. import orchestrate
    from ..cliext import planner
    from . import core, launch, probe, service, session
    now = time.time() if now is None else now
    path = service.locate(ref, cluster)              # NotFound: exit 3 / HTTP 404, like show
    rid = service.identify(path, cluster) or Path(path).stem
    config_arg = f" --config {_quote(config)}" if config else ""
    steps, model = [], None

    # 1. The recipe itself: listed without error, not pending, reads (and resolves) cleanly.
    try:
        service.locate(ref, cluster, for_up=True)
        resolved = session.resolve(path, session.Layers(), backend=core.backend(cluster))
        model = resolved.recipe["match"]["model"]
        steps.append(_step("recipe", "recipe reads and is launchable", OK,
                           f'{resolved.recipe["name"]} for "{model}"'))
    except RecipeError as exc:
        steps.append(_step("recipe", "recipe reads and is launchable", BLOCKED, str(exc),
                           f"hyperloom recipe list{config_arg}"))

    # 2. The recipe core (production launches need it).
    try:
        backend, production = launch._backend(cluster)
        steps.append(_step("core", "recipe core installed", OK if production else WARN, backend.label,
                           None if production else "install ts-recipe and set pc.recipe_core"))
    except RecipeError as exc:
        steps.append(_step("core", "recipe core installed", TODO, str(exc),
                           "install ts-recipe and set pc.recipe_core (or HYPERLOOM_TS_RECIPE)"))

    # 3. The pinned router applies route tables.
    try:
        launch.check_router()
        steps.append(_step("router", "router applies recipe routes", OK, "TensorMeld policy routes"))
    except RecipeError as exc:
        steps.append(_step("router", "router applies recipe routes", BLOCKED, str(exc)))

    # 4. A cluster configuration with a profile that serves the recipe's model.
    if cluster is None:
        steps.append(_step("config", "cluster configuration", TODO, "no cluster configuration file",
                           f"describe your machines from {SAMPLE_CONFIG}, then hyperloom config validate"))
    elif model is not None:
        try:
            profile = service.serving_profiles(cluster, model, profile)[0] if profile else \
                service.deployment_profile(cluster, path)
            steps.append(_step("profile", "a profile serves the model", OK, profile))
        except RecipeError as exc:
            serving = service.model_profiles(cluster, model)
            fix = (" | ".join(f"hyperloom recipe ready {_quote(rid)} {_quote(p)}{config_arg}" for p in serving)
                   if serving else f"add a profile for {model} to the configuration (see {SAMPLE_CONFIG})")
            profile = None
            steps.append(_step("profile", "a profile serves the model", TODO, str(exc), fix))
    blocked = next((s for s in steps if s["state"] in (TODO, BLOCKED) and s["step"] in ("recipe", "config", "profile")),
                   None)

    # 5. The launch checks `up` makes before any I/O (engine binding, router placement).
    if blocked is None and profile is not None:
        try:
            orchestrate.check_launch(cluster, profile, "local", recipe=True)
            steps.append(_step("launch", "profile can run with a recipe", OK))
        except (OSError, ValueError, KeyError) as exc:
            steps.append(_step("launch", "profile can run with a recipe", BLOCKED, str(exc)))
            blocked = steps[-1]

    # 6. Hardware: the newest saved probe of the profile, evaluated as up would (match, admission, decision).
    probe_fix = (f"hyperloom recipe probe {_quote(profile)} --out "
                 f"{_quote(service.facts_dir() / (str(profile) + '.json'))}{config_arg}")
    evaluation = None
    if blocked is not None or profile is None:
        steps.append(_step("hardware", "hardware matches (saved probe)", SKIPPED,
                           f"needs step {blocked['step'] if blocked else 'profile'}"))
    else:
        try:
            name, facts_path, record = planner.resolve_facts(facts, [profile])
        except planner.Refused as exc:
            steps.append(_step("hardware", "hardware matches (saved probe)", TODO,
                               str(exc) if facts else f"no saved probe of profile {profile}", probe_fix))
        else:
            age = now - record.get("observed_at", 0) if "observed_at" in record else None
            try:
                evaluation = session.evaluate(cluster, profile, path, session.Layers(),
                                              session.saved_source(facts_path), core.backend(cluster))
            except RecipeError as exc:
                steps.append(_step("hardware", "hardware matches (saved probe)", BLOCKED, str(exc), probe_fix))
            else:
                why = launch.refusal_of(evaluation)
                stale = age is None or age > probe.CACHE_TTL_S
                when = "at an unrecorded time" if age is None else f"{age / 86400:.1f} days ago"
                if why is not None:
                    steps.append(_step("hardware", "hardware matches (saved probe)", BLOCKED,
                                       f"{why} (probe {name}, {when})", probe_fix))
                    evaluation = None
                else:
                    steps.append(_step("hardware", "hardware matches (saved probe)", WARN if stale else OK,
                                       f"probe {name}, {when}" + (": probe again before up" if stale else ""),
                                       probe_fix if stale else None))

    # 7. The RTX prefill service, when the decision starts it: planned from saved header descriptors and this
    #    machine's free RAM now (the only live read: a local one, as plan makes it).
    if evaluation is not None:
        decision = evaluation.decision
        if decision["service"]["start"]:
            cap, host = launch._service_budget(evaluation)
            plan, why = launch.plan_service(cluster, profile, cap, host)
            if why is not None:
                # As up decides: the service is not started and every prefill stays on the nodes.
                steps.append(_step("service", "RTX prefill service plans", WARN,
                                   f"planner: {why}; up keeps every prefill on the nodes",
                                   f"hyperloom recipe probe {_quote(profile)} --descriptors{config_arg}"
                                   if "descriptor" in why else None))
            else:
                steps.append(_step("service", "RTX prefill service plans", OK,
                                   f"{cap:g} GiB VRAM, {host:g} GiB host" if plan else "nothing to plan"))
        else:
            steps.append(_step("service", "RTX prefill service plans", SKIPPED,
                               decision["service"].get("why") or "the decision keeps every prefill on the nodes"))

    ready = all(s["state"] in (OK, WARN, SKIPPED) for s in steps) and evaluation is not None
    first = next((s for s in steps if s["state"] in (TODO, BLOCKED)), None)
    up = f"hyperloom up {_quote(profile)} --recipe {_quote(rid)}{config_arg}" if profile else None
    plan_cmd = f"hyperloom recipe plan {_quote(rid)} {_quote(profile)}{config_arg}" if profile else None
    return {"recipe": rid, "file": str(path), "model": model, "profile": profile, "ready": ready, "steps": steps,
            "next": ({"step": first["step"], "fix": first["fix"]} if first else
                     {"step": "plan", "fix": plan_cmd} if ready else None),
            "commands": {"plan": plan_cmd, "up": up}, "checked_at_start": service.CHECKED_AT_START}


# --- matrix ------------------------------------------------------------------------------------------------------

HARDWARE = {"strix-single": "one Strix node", "strix-pair": "Strix pair (TP2 over USB4)",
            "strix-replicas": "Strix nodes, one replica each", "rtx": "RTX PRO decode",
            "rtx-prefill": "RTX prefill + Strix decode", "hetero": "RTX + Strix sharing decode"}


def hardware_class(recipe):
    """The hardware class of a recipe, from its enabled routes: decode topology, plus RTX prefill when an enabled
    prefill route runs on a cuda device while decode runs on hip."""
    kinds = {d["id"]: (d["kind"], d.get("count") or 1) for d in recipe["match"]["devices"]}
    routes = [r for r in recipe["routes"] if r["enabled"]]
    decode = [r for r in routes if r["phase"] == "decode"]
    if any(r["execute"] == "hetero" for r in decode):
        return "hetero"
    if any(kinds.get(d, ("",))[0] == "cuda" for r in decode for d in r["devices"]):
        return "rtx"
    rtx_prefill = any(r["phase"] == "prefill" and any(kinds.get(d, ("",))[0] == "cuda" for d in r["devices"])
                      for r in routes)
    if rtx_prefill:
        return "rtx-prefill"
    if any(r["execute"] == "tp" for r in decode):
        return "strix-pair"
    if any(r["execute"] == "replicas" for r in decode):
        return "strix-replicas"
    return "strix-single"


def _with_source(figure, rows):
    """A headline figure with the scope and evidence of the trade-off row it was taken from."""
    same = next((r for r in rows if all(r.get(k) == figure.get(k) for k in ("route", "value", "unit", "basis", "label"))),
                {})
    return dict(figure, scope=same.get("scope"), evidence=same.get("evidence"))


def _figure(key, figure, evidence):
    """A headline figure with its sources (the recipe's x-evidence refs). It counts as measured only when its basis
    says so and every evidence id it names resolves to a source; anything else is shown with its basis."""
    ids = figure.get("evidence")
    ids = [ids] if isinstance(ids, str) else [i for i in ids or [] if isinstance(i, str)]
    sources = [{"id": i, "ref": evidence[i].get("ref") if isinstance(evidence.get(i), dict) else None} for i in ids]
    measured = (figure.get("basis") == "measured" and not figure.get("inherited") and bool(sources)
                and all(s["ref"] for s in sources))
    return dict(figure, key=key, sources=sources, measured=measured)


def matrix(cluster, *, config=None):
    """{models, hardware, cells}: per model x hardware class, every recipe with its status, headline figures (each
    with its basis and evidence; none = unmeasured) and the command that runs it. Offline: the catalog and the
    configuration only."""
    from . import reader, service, session
    config_arg = f" --config {_quote(config)}" if config else ""
    cells = {}
    for row in service.catalog(cluster):
        if row.get("overflow") or row["error"] or not row["model"]:
            continue
        try:
            text = session.read_text(row["path"])
            recipe = reader.parse(text, Path(row["path"]).name)
            evidence = reader.load_text(text, Path(row["path"]).name).get("x-evidence")
        except (OSError, RecipeError):
            continue
        evidence = evidence if isinstance(evidence, dict) else {}
        cls = hardware_class(recipe)
        profiles = service.model_profiles(cluster, row["model"]) if cluster is not None else []
        launchable = row["status"] != "pending"
        entry = {"id": row["id"], "status": row["status"], "blocker": row["blocker"], "source": row["source"],
                 "description": row["description"],
                 "figures": [_figure(k, _with_source(f, row["tradeoffs"]), evidence)
                             for k, f in row["headline"].items()],
                 "profiles": profiles,
                 "ready": f"hyperloom recipe ready {_quote(row['id'])}{config_arg}",
                 "command": (f"hyperloom up {_quote(profiles[0]) if len(profiles) == 1 else 'PROFILE'} --recipe "
                             f"{_quote(row['id'])}{config_arg}") if launchable else None}
        cells.setdefault(row["model"], {}).setdefault(cls, []).append(entry)
    models = sorted(set(cells) | set(service.models(cluster)))
    return {"models": models, "hardware": [{"class": k, "label": v} for k, v in HARDWARE.items()],
            "cells": [{"model": m, "class": c, "recipes": cells.get(m, {}).get(c, [])}
                      for m in models for c in HARDWARE]}


# --- text output -------------------------------------------------------------------------------------------------

MARK = {OK: "[ok]", TODO: "[todo]", BLOCKED: "[blocked]", WARN: "[warn]", SKIPPED: "[skip]"}


def ready_text(result):
    lines = [f"recipe {result['recipe']} ({result['model'] or 'model unknown'}) on profile "
             f"{result['profile'] or '(none)'}: {'READY' if result['ready'] else 'NOT READY'}"]
    for s in result["steps"]:
        lines.append(f"  {MARK[s['state']]:<10}{s['title']}" + (f": {s['detail']}" if s["detail"] else ""))
        if s["fix"] and s["state"] != OK:
            lines.append(f"  {'':<10}-> {s['fix']}")
    if result["ready"]:
        lines += ["next:", f"  {result['commands']['plan']}   (dry run: what up decides)",
                  f"  {result['commands']['up']}"]
    lines.append("checked only when up starts: " + "; ".join(result["checked_at_start"]))
    return "\n".join(lines)


def _figure_label(figure):
    """TTFT figures name their prompt size (the headline key: ttft_8k_s -> TTFT 8K)."""
    key = figure.get("key") or ""
    return f"{figure['label']} {key[5:-2].upper()}" if key.startswith("ttft_") and key.endswith("_s") else figure["label"]


def matrix_text(result):
    lines = []
    labels = {h["class"]: h["label"] for h in result["hardware"]}
    for model in result["models"]:
        lines.append(model)
        for cell in (c for c in result["cells"] if c["model"] == model):
            if not cell["recipes"]:
                lines.append(f"  {labels[cell['class']]:<30} no recipe")
                continue
            for r in cell["recipes"]:
                numeric = [f for f in r["figures"] if isinstance(f["value"], (int, float))]
                measured = ", ".join(f"{_figure_label(f)} {f['value']:g} {f['unit']}" for f in numeric if f["measured"])
                estimates = ", ".join(f"{_figure_label(f)} {f['value']:g} {f['unit']} ({f['basis'] or 'unmeasured'})"
                                      for f in numeric if not f["measured"])
                figures = "; ".join(part for part in (measured and "measured: " + measured,
                                                      estimates and "estimates: " + estimates) if part)
                state = r["status"] + (f": {r['blocker']}" if r["blocker"] else "")
                lines.append(f"  {labels[cell['class']]:<30} {r['id']} [{state}]" + (f" {figures}" if figures else ""))
                if r["command"]:
                    lines.append(f"  {'':<30} {r['ready']}")
    return "\n".join(lines)
