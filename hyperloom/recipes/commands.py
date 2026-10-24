"""`hyperloom recipe probe|validate|explain|diff|pick`: offline and read-only (no process starts, no model reads
except `probe --descriptors` / `probe --disks`, which are the operator's explicit requests)."""
from __future__ import annotations

import json
from pathlib import Path

from .. import orchestrate
from . import core, descriptors, facts, probe, reader, session
from .reader import RecipeError


def runtime_dir():
    from ..cli import config_path
    return config_path().parent / "run"


def cache():
    return facts.Cache(runtime_dir() / "probe-cache.json")


def store():
    return descriptors.Store(runtime_dir() / "descriptors")


def recipe_dirs(cluster, extra=()):
    from ..cli import config_path
    dirs = list(extra) + list(cluster.raw.get("pc", {}).get("recipe_dirs", [])) + [config_path().parent / "recipes"]
    return [Path(d) for d in dirs]


def node_call(cluster):
    def call(mid, verb):
        machine = cluster.machines[mid]
        client = orchestrate._client(machine)
        return orchestrate._call(client, verb, {"lab_dir": orchestrate._lab(cluster, mid)}, timeout=60)
    return call


def live_source(cluster, measure_disks=False):
    def source(ctx):
        return facts.gather(cluster, ctx, call=node_call(cluster), cache=cache(), measure_disks=measure_disks)
    return source


def layers_of(args):
    return session.Layers(profile=args.recipe_profile, overrides=list(args.recipe_overrides or []),
                          sets=[reader.parse_set(s) for s in (args.recipe_set or [])],
                          workload=getattr(args, "recipe_workload", None) or "default")


def source_of(cluster, args):
    return session.saved_source(args.facts) if getattr(args, "facts", None) else live_source(cluster)


def model_paths(cluster, ctx):
    """(machine, path) of the model file of every launched device: nodes' model asset, the service's model asset."""
    kind_asset = facts.FAMILY_MODELS[next(k for k, v in facts.FAMILY_MODELS.items() if v[0] == ctx.model)][1]
    rows = [(mid, cluster.assets.get(mid, {}).get(kind_asset, {}).get("path")) for mid in ctx.nodes]
    if ctx.service is not None and ctx.service.get("model_asset"):
        asset = cluster.assets.get(ctx.pc, {}).get(ctx.service["model_asset"], {})
        rows.append((ctx.pc, asset.get("control_path", asset.get("path"))))
    return rows


def probe_command(cluster, args):
    ctx = facts.context(cluster, args.profile)
    out = {"profile": args.profile}
    if args.descriptors:
        if ctx.service is None or not ctx.service.get("model_asset"):
            raise RecipeError(f"profile {args.profile} plans nothing on this machine (no prefill service)")
        asset = cluster.assets.get(ctx.pc, {}).get(ctx.service["model_asset"])
        if asset is None:
            raise RecipeError(f"{ctx.pc}: missing asset {ctx.service['model_asset']}")
        out["descriptors"] = store().save(asset.get("control_path", asset["path"]))
    for item in args.evidence or []:
        machine, _, receipt = item.partition("=")
        if machine not in ctx.nodes or not receipt:
            raise RecipeError(f"--evidence {item}: expected NODE=RECEIPT with a node of the profile")
        path = dict(model_paths(cluster, ctx)).get(machine)
        if not path:
            raise RecipeError(f"--evidence {item}: {machine} has no model asset for this profile")
        out.setdefault("evidence", {})[machine] = cache().put_evidence(machine, path, receipt)
    if args.describe_command or args.declaration:
        rows = facts.describe_commands(cluster, args.profile)
        if args.describe_command:
            out["describe_commands"] = rows
        by_machine = {r["machine"]: r for r in rows}
        for item in args.declaration or []:
            machine, _, receipt = item.partition("=")
            if machine not in by_machine or not receipt:
                raise RecipeError(f"--declaration {item}: expected NODE=RECEIPT with a node of the profile")
            row = by_machine[machine]
            out.setdefault("declarations", {})[machine] = cache().put_declaration(machine, row["rank"],
                                                                                   row["launch_sha256"], receipt)
    inventory, gathered, raw, notes = facts.gather(cluster, ctx, call=node_call(cluster), cache=cache(),
                                                   measure_disks=args.disks)
    record = session.facts_record(args.profile, inventory, gathered, raw, notes)
    if args.out:
        Path(args.out).write_text(json.dumps(record, indent=1), encoding="utf-8")
        out["facts_file"] = str(args.out)
    out.update(facts=gathered, availability=raw, notes=notes)
    return out


def validate_command(cluster, args):
    backend = core.backend(cluster)
    if args.profile is None:
        eff = session.resolve(args.file, layers_of(args), backend=backend)
        return {"recipe": eff.recipe["name"], "reader": backend.label, "valid": True,
                "untuned": eff.untuned, "refused_layers": eff.refused, "hardware": "not checked (no profile)"}, True
    evaluation = session.evaluate(cluster, args.profile, args.file, layers_of(args), source_of(cluster, args), backend)
    return dict(evaluation.summary(), valid=evaluation.ok), evaluation.ok


def explain_command(cluster, args):
    backend = core.backend(cluster)
    eff = session.resolve(args.file, layers_of(args), backend=backend)
    trace = eff.trace
    fields = trace["fields"] if args.all else [f for f in trace["fields"] if f["layer"] != "default" or f["shadowed"]]
    out = {"recipe": eff.recipe["name"], "reader": backend.label, "file": str(args.file),
           "untuned": eff.untuned, "refused_layers": eff.refused, "fields": fields,
           "routes": [{"id": r["id"], "phase": r["phase"], "devices": r["devices"], "execute": r["execute"],
                       "when": {k: v for k, v in r["when"].items() if v not in (None, 0, 1) or k == "prompt_tokens_min"},
                       "fallback": r["fallback"], "handoff_route": r["handoff_route"], "enabled": r["enabled"]}
                      for r in eff.recipe["routes"]]}
    if args.profile is not None:
        evaluation = session.evaluate(cluster, args.profile, args.file, layers_of(args), source_of(cluster, args),
                                      backend)
        _what_if(evaluation, args)
        out["hardware"] = evaluation.summary()
        if args.prompt_tokens is not None:
            try:
                out["simulated_request"] = dict(session.simulate(evaluation, args.prompt_tokens),
                                                prompt_tokens=args.prompt_tokens)
            except RecipeError as exc:
                out["simulated_request"] = {"prompt_tokens": args.prompt_tokens, "refused": str(exc)}
    return out


def _what_if(evaluation, args):
    """Apply --unavailable / --link-down / --free-ram to the evaluated availability and decide again."""
    changed = False
    for ident in args.unavailable or []:
        evaluation.availability["devices"][ident] = {"available": False, "why": "--unavailable"}
        changed = True
    for link in args.link_down or []:
        evaluation.availability["links"][link] = {"up": False, "why": "--link-down"}
        changed = True
    for item in args.free_ram or []:
        host, _, gib = item.partition("=")
        evaluation.availability["hosts"][host] = dict(evaluation.availability["hosts"].get(host, {}),
                                                      free_ram_gib=float(gib))
        changed = True
    if changed and not evaluation.problems:
        from . import apply
        try:
            evaluation.decision = apply.deploy(evaluation.effective.recipe, evaluation.context, evaluation.bound,
                                               evaluation.availability, evaluation.effective_sha256)
            evaluation.refusal = None
        except RecipeError as exc:
            evaluation.decision, evaluation.refusal = None, str(exc)


def diff_command(cluster, args):
    backend = core.backend(cluster)
    a = session.resolve(args.a, session.Layers(sets=[reader.parse_set(s) for s in args.a_set or []]), backend=backend)
    b = session.resolve(args.b, session.Layers(sets=[reader.parse_set(s) for s in args.b_set or []]), backend=backend)
    return dict(session.diff(a, b), a=str(args.a), b=str(args.b))


def pick_command(cluster, args):
    chosen, rows = session.pick(cluster, args.profile, recipe_dirs(cluster, args.dir or []), layers_of(args),
                                source_of(cluster, args), core.backend(cluster))
    return {"profile": args.profile, "chosen": chosen.path if chosen else None, "candidates": rows}


def run(cluster_loader, args):
    """(result, ok) for one `hyperloom recipe` operation."""
    offline = args.operation == "diff" or (args.operation in ("validate", "explain") and args.profile is None)
    cluster = None if offline else cluster_loader(args.config)
    if args.operation == "probe":
        return probe_command(cluster, args), True
    if args.operation == "validate":
        return validate_command(cluster, args)
    if args.operation == "explain":
        return explain_command(cluster, args), True
    if args.operation == "diff":
        return diff_command(cluster, args), True
    chosen = pick_command(cluster, args)
    return chosen, chosen["chosen"] is not None
