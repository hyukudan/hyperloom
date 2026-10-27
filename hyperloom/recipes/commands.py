"""`hyperloom recipe ...`: probe, validate, explain, diff, pick (offline and read-only: no process starts, no model
reads except `probe --descriptors` / `probe --disks`, the operator's explicit requests), and the catalog operations of
service.py (list, show, select, plan, active, routes, save, delete, save-layer, delete-layer). The portal runs this same
command line for every recipe request."""
from __future__ import annotations

import atexit
import json
from pathlib import Path
import shutil
import sys
import tempfile

from .. import orchestrate
from . import core, descriptors, facts, probe, reader, session
from .reader import RecipeError


def runtime_dir():
    from .service import runtime_dir as run
    return run()


def cache():
    return facts.Cache(runtime_dir() / "probe-cache.json")


def store():
    return descriptors.Store(runtime_dir() / "descriptors")


def recipe_dirs(cluster, extra=()):
    """Where `--recipe auto` and pick look: the built-in examples (not the pending ones), the configured directories
    and the user directory, aliases removed (the catalog's own list)."""
    from .service import candidate_dirs
    return candidate_dirs(cluster, extra)


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


def pick_command(cluster, args, dirs=None):
    dirs = recipe_dirs(cluster, args.dir or []) if dirs is None else dirs
    chosen, rows = session.pick(cluster, args.profile, dirs, layers_of(args), source_of(cluster, args),
                                core.backend(cluster))
    return {"profile": args.profile, "chosen": chosen.path if chosen else None, "candidates": rows}


def run(cluster_loader, args):
    """(result, ok) for one `hyperloom recipe` operation. The portal runs this same command line."""
    from . import service
    with service.configured(args.config):
        return _run(cluster_loader, args, service)


def _run(cluster_loader, args, service):
    # Operations that need no hardware run without a cluster configuration (then without configured directories).
    offline = args.operation in ("diff", "list", "show", "save", "delete", "save-layer", "delete-layer",
                                 "save-receipt", "deploy", "ready", "matrix") or (
        args.operation in ("validate", "explain") and args.profile is None)
    cluster = _optional_cluster(cluster_loader, args.config) if offline else cluster_loader(args.config)
    if args.operation in ("validate", "explain") and str(args.file) == "-":
        args.file = _stdin_recipe()
    # A recipe id (qwen3.8-flash-next.lab) or a file; every file is read once, from a private copy.
    for name in ("file", "a", "b"):
        if getattr(args, name, None) is not None:
            setattr(args, name, service.locate(getattr(args, name), cluster))
    # plan snapshots its inputs itself (content-addressed: the snapshot a deploy then launches).
    with service.snapshot_args(args, () if args.operation == "plan" else ("file", "a", "b", "recipe_profile",
                                                                          "recipe_overrides")) as originals:
        result, ok = _dispatch(cluster, args, service)
        return originals(result), ok


def text(args, result):
    """The --format text rendering of a ready / matrix result."""
    from . import ready
    return ready.ready_text(result) if args.operation == "ready" else ready.matrix_text(result)


def _dispatch(cluster, args, service):
    if args.operation == "ready":
        from . import ready
        result = ready.check(cluster, args.recipe, args.profile, config=_config_arg(args.config), facts=args.facts)
        return result, result["ready"]
    if args.operation == "matrix":
        from . import ready
        return ready.matrix(cluster, config=_config_arg(args.config)), True
    if args.operation == "probe":
        with service.runtime_lock():      # probes write the descriptor store, the disk cache and evidence
            return probe_command(cluster, args), True
    if args.operation == "validate":
        return validate_command(cluster, args)
    if args.operation == "explain":
        return explain_command(cluster, args), True
    if args.operation == "diff":
        return diff_command(cluster, args), True
    if args.operation == "pick":
        with tempfile.TemporaryDirectory(prefix="hl-recipe-pick-") as directory:
            dirs, mapping = service.snapshot_dirs(recipe_dirs(cluster, args.dir or []), directory)
            chosen = pick_command(cluster, args, dirs)
            chosen["candidates"] = service.ids(chosen["candidates"], cluster, mapping)
            chosen = service.rename(chosen, mapping)
        return chosen, chosen["chosen"] is not None
    if args.operation == "list":
        return service.list_recipes(cluster, args.model), True
    if args.operation == "show":
        return service.show(cluster, args.recipe), True
    if args.operation == "select":
        result = service.select(cluster, args.model, args.profile, layers=layers_of(args),
                                source_for=None if args.facts is None else lambda _: source_of(cluster, args))
        return result, any(p["chosen"] for p in result["profiles"])
    if args.operation == "plan":
        result = service.plan(cluster, args.recipe, args.profile, layers=layers_of(args),
                              source=source_of(cluster, args))
        return result, result["up"]["ok"]
    if args.operation == "active":
        return service.active(cluster, args.profile), True
    if args.operation == "routes":
        return service.routes(cluster, args.profile), True
    if args.operation == "save":
        text = None if args.from_file is None else _read_source(args.from_file)
        return service.save(cluster, args.id, text=text, base=args.base, base_sha256=args.base_sha256,
                            sets=[reader.parse_set(s) for s in args.set or []], name=args.name,
                            replace=args.replace), True
    if args.operation == "delete":
        return service.delete(cluster, args.id, args.sha256), True
    if args.operation == "save-layer":
        text = None if args.from_file is None else _read_source(args.from_file)
        return service.save_layer(cluster, args.name, args.recipe, [reader.parse_set(s) for s in args.set or []],
                                  text=text, replace=args.replace), True
    if args.operation == "deploy":
        return service.deploy(args.inputs, args.stop, approval=_approval(args), job=args.job), True
    if args.operation == "save-receipt":
        return service.save_receipt(cluster, args.name, _read_source(args.from_file), replace=args.replace), True
    if args.operation == "delete-layer":
        return service.delete_layer(cluster, args.name, args.sha256), True
    raise RecipeError(f"unknown recipe operation {args.operation}")


def _approval(args):
    from datetime import datetime
    supplied = [args.approval_ref is not None, args.approval_lease is not None, args.approval_end is not None]
    if not any(supplied):
        return None
    if not all(supplied):
        raise ValueError("approval requires --approval-ref, --approval-lease, and --approval-end")
    end = datetime.fromisoformat(args.approval_end.replace("Z", "+00:00"))
    if end.tzinfo is None:
        raise ValueError("--approval-end must include a timezone")
    return {"ref": args.approval_ref, "lease_s": args.approval_lease, "end_epoch": end.timestamp()}


def _config_arg(path):
    """The --config value to repeat in suggested commands: None for the default configuration file."""
    from ..cli import config_path
    return None if Path(path) == config_path() else str(path)


def _optional_cluster(cluster_loader, path):
    """The cluster when its configuration file exists (an invalid one is still an error); else None."""
    return cluster_loader(path) if Path(path).is_file() else None


def _read_source(path):
    """Recipe text from a file, or from stdin for "-" (UTF-8, at most the save limit)."""
    from .service import MAX_SAVE_BYTES
    if str(path) != "-":
        return session.read_text(path)
    data = sys.stdin.buffer.read(MAX_SAVE_BYTES + 1)
    if len(data) > MAX_SAVE_BYTES:
        raise RecipeError(f"recipe text on stdin larger than {MAX_SAVE_BYTES // 1024} KiB")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise RecipeError(f"recipe text on stdin: {exc}") from exc


def _stdin_recipe():
    """`validate -` / `explain -`: the recipe text from stdin, in a private file for this run."""
    text = _read_source("-")
    directory = tempfile.mkdtemp(prefix="hl-recipe-stdin-")
    atexit.register(shutil.rmtree, directory, True)
    path = Path(directory) / ("stdin.json" if text.lstrip().startswith("{") else "stdin.toml")
    path.write_bytes(text.encode("utf-8"))
    return path
