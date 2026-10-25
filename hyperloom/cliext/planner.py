"""`hyperloom whatif` (recipe what-if planner) and `hyperloom placement` (placement preview).

whatif: a bounded set of variants of one recipe (the base plus field overrides, each variant ONE overrides layer) is
evaluated against the last saved hardware facts of a deployment profile. Every variant IS the existing recipe command
line (`hyperloom recipe explain ... --all` and `hyperloom recipe plan ...`), run in this process by the CLI's own
parser and ``recipes.commands.run``; the answers are set side by side. Nothing is deployed, probed or read from a model
file.

placement: TensorMeld's heterogeneous planner (`python -m tensormeld plan-hetero`, the pinned copy) over the model's
parts and a hardware description built from the saved facts, in three scenarios (nodes only, nodes + RTX prefill,
all devices). Advisory, never qualified.

The portal runs these same command lines (web/features/planner.py).
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

WHATIF_SCHEMA = "hyperloom/planner-whatif-v1"
PLACEMENT_SCHEMA = "hyperloom/placement-preview-v1"
FACTS_SCHEMA = "hyperloom/recipe-facts-v1"
OVERRIDES_SCHEMA = "strix-stream/recipe-overrides-v1"
HARDWARE_SCHEMA = "tensormeld/hetero-hardware-v1"
MAX_VARIANTS = 12                    # the base included
MAX_PATHS = 256                      # fields per variant (one overrides layer)
MAX_NAME = 64
MAX_VARIANTS_BYTES = 256 * 1024
MAX_FACTS_FILES = 256
MAX_FACTS_BYTES = 4 * 1024 * 1024
LAYER_KEEP_S = 24 * 3600
LAYER_KEEP_FILES = 512
WHATIF_DEADLINE_S = 100.0
PLACEMENT_DEADLINE_S = 120.0
SCENARIO_DEADLINE_S = 45.0
STDOUT_CAP = 8 * 1024 * 1024
STDERR_CAP = 64 * 1024
TEMPLATE_MEMORY_TOLERANCE = 0.10     # a template device's rates apply to a device whose memory is within 10 %
GIB = 1 << 30
BUDGET_FIELDS = ("device_gib", "host_pinned_gib", "workspace_gib", "staging_buffers", "staging_mib", "tier_mode",
                 "max_context_tokens", "max_batch")
FLOOR_FIELDS = ("limits.host_free_ram_floor_gib", "limits.host_free_commit_floor_gib")
OBJECTIVES = ("latency", "decode", "prefill", "throughput")
ADVISORY = "advisory, not qualified"


class Refused(ValueError):
    """The batch itself is refused (exit 1 with the message)."""


# --- command line ------------------------------------------------------------------------------------------------

def add(commands):
    from hyperloom.cli import config_path
    whatif = commands.add_parser("whatif", help="recipe variants side by side on the saved hardware facts (read-only)")
    whatif.add_argument("recipe", help="a recipe id (or a file)")
    whatif.add_argument("--profile", help="the deployment profile (default: the one serving the recipe's model)")
    whatif.add_argument("--facts", help="a saved facts name of <config dir>/run/facts (or a file); default: the last "
                                        "saved facts of the profile")
    whatif.add_argument("--vary", action="append", default=[], metavar="PATH=V1,V2,...",
                        help="values of one settable field (repeatable; the variants are every combination)")
    whatif.add_argument("--variants", help='a JSON file (- = stdin) {"variants": [{"name": ..., "set": {path: value}}]}')
    whatif.add_argument("--prompt-tokens", type=int, help="also the route a request of this many tokens would take")
    whatif.add_argument("--deadline-s", type=float, default=WHATIF_DEADLINE_S, help=argparse.SUPPRESS)
    whatif.add_argument("--catalog-only", action="store_true", help=argparse.SUPPRESS)   # the portal: ids only
    whatif.add_argument("--config", type=Path, default=config_path())
    whatif.set_defaults(ext_run=run_whatif)
    placement = commands.add_parser("placement", help="candidate placements of a model on the saved hardware facts "
                                                      "(TensorMeld planner; advisory, not qualified)")
    placement.add_argument("model", help="a recipe model id, e.g. deepseek-v4.1-flash")
    placement.add_argument("--facts", help="a saved facts name (or a file); default: the newest facts of a profile "
                                           "serving the model")
    placement.add_argument("--prompt", type=int, default=8192, help="prompt tokens of the planned request")
    placement.add_argument("--output", type=int, default=256, help="generated tokens of the planned request")
    placement.add_argument("--slots", type=int, default=1, help="concurrent sequences")
    placement.add_argument("--objective", choices=OBJECTIVES, default="latency")
    placement.add_argument("--top-k", type=int, default=5)
    placement.add_argument("--deadline-s", type=float, default=PLACEMENT_DEADLINE_S, help=argparse.SUPPRESS)
    placement.add_argument("--config", type=Path, default=config_path())
    placement.set_defaults(ext_run=run_placement)


def _emit(result):
    sys.stdout.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


def run_whatif(args):
    from hyperloom.recipes import service
    try:
        result = whatif(args)
    except service.NotFound as exc:
        print(f"hyperloom: {exc}", file=sys.stderr)
        return 3
    _emit(result)
    return 0


def run_placement(args):
    result = placement(args)
    _emit(result)
    return 0 if any(s.get("status") not in (None, "unavailable", "failed") for s in result["scenarios"]) else 1


def _loader(path):
    from hyperloom import cli
    return cli._cluster(path)


# --- saved facts -------------------------------------------------------------------------------------------------

def saved_facts(directory=None):
    """[(name, path, record)] of the readable facts records in the facts directory (bounded), newest first."""
    from hyperloom.recipes import service
    directory = Path(directory or service.facts_dir())
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.json"))[:MAX_FACTS_FILES]:
        record = read_facts(path, directory)
        if record is not None:
            rows.append((path.stem, path, record))
    rows.sort(key=lambda row: -_number(row[2].get("observed_at"), 0))
    return rows


def read_facts(path, directory=None):
    """A facts record (a regular file, inside ``directory`` when given, size bounded, structure checked) or None."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FACTS_BYTES:
            return None
        if directory is not None and Path(os.path.realpath(path)).parent != Path(os.path.realpath(directory)):
            return None
        record = json.loads(path.read_text(encoding="utf-8"), parse_constant=_constant)
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != FACTS_SCHEMA or not isinstance(record.get("profile"), str):
        return None
    facts, raw, inventory = record.get("facts"), record.get("raw"), record.get("inventory")
    if not isinstance(facts, dict) or not isinstance(facts.get("devices", {}), dict) or             not isinstance(facts.get("links", []), list) or not isinstance(raw, dict) or not isinstance(inventory, dict):
        return None
    if "observed_at" in record and _number(record["observed_at"]) is None:
        return None
    return record


def _number(value, default=None):
    return value if type(value) in (int, float) and math.isfinite(value) else default


def resolve_facts(ref, profiles):
    """(name, path, record) of the facts to use: ``ref`` (a name in the facts directory, or a file), else the newest
    record of one of ``profiles``. Refused when there is none."""
    from hyperloom.recipes import service
    if ref:
        candidate = Path(ref)
        if not (candidate.suffix == ".json" and candidate.is_file()):
            if not service.ID.fullmatch(ref) or Path(ref).name != ref:
                raise Refused(f"--facts {ref}: a saved facts name or an existing file")
            candidate = service.facts_dir() / (ref.removesuffix(".json") + ".json")
            record = read_facts(candidate, service.facts_dir())
        else:
            record = read_facts(candidate)
        if record is None:
            raise Refused(f"--facts {ref}: no readable {FACTS_SCHEMA} record at {candidate}")
        if profiles and record.get("profile") not in profiles:
            raise Refused(f"--facts {ref}: facts of profile {record.get('profile')}, not "
                          f"{' or '.join(profiles)}")
        return candidate.stem, candidate, record
    for name, path, record in saved_facts():
        if record.get("profile") in profiles:
            return name, path, record
    wanted = profiles[0] if len(profiles) == 1 else " or ".join(profiles) or "this model"
    raise Refused(f"no saved hardware facts for profile {wanted}: run `hyperloom recipe probe "
                  f"{profiles[0] if profiles else 'PROFILE'} --out NAME` (the Recipes page's probe job)")


def facts_summary(name, record):
    observed = _number(record.get("observed_at"))
    return {"name": name, "profile": record.get("profile"), "model": (record.get("facts") or {}).get("model"),
            "observed_at": observed, "label": "saved facts, not live"}


# --- variants ----------------------------------------------------------------------------------------------------

def split_values(text):
    """Comma-separated value texts; commas inside brackets, braces or quotes do not split."""
    out, depth, quote, current = [], 0, None, []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    out.append("".join(current).strip())
    if any(not part for part in out) or quote or depth:
        raise Refused(f'"{text}": expected V1,V2,... (no empty value, balanced brackets and quotes)')
    return out


def _json_value(path, value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise Refused(f"{path}: value {value!r} is not a plain value ({exc})") from exc
    return value


def parse_vary(items):
    """[(path, [values])] from repeated PATH=V1,V2 arguments (each value read like --recipe-set)."""
    from hyperloom.recipes import reader
    out, seen = [], set()
    for item in items:
        if "=" not in item:
            raise Refused(f'--vary "{item}": expected PATH=V1,V2,...')
        path, raw = (part.strip() for part in item.split("=", 1))
        if not path or path in seen:
            raise Refused(f'--vary "{item}": ' + ("a path is required" if not path else f"{path} is varied twice"))
        seen.add(path)
        values = []
        for text in split_values(raw):
            parsed = reader.parse_set(f"{path}={text}")[1]
            values.append(_json_value(path, parsed))
        out.append((path, values))
    return out


def _label(value):
    return json.dumps(value) if not isinstance(value, str) else value


def _short(path):
    """A readable short form of a settable path: routes.R.placement.S.F -> R.F, limits.F -> F."""
    parts = path.split(".")
    if parts[0] == "routes" and len(parts) >= 3:
        return f"{parts[1]}.{parts[-1]}"
    return parts[-1] if parts[0] in ("limits", "match") else path


def default_name(sets):
    name = ", ".join(f"{_short(path)}={_label(v)}" for path, v in sets.items())
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1] + "…"


def build_variants(vary, explicit):
    """The base plus every combination of ``vary`` plus the explicit variants; refused above MAX_VARIANTS."""
    combos = 1
    for _, values in vary:
        combos *= len(values)
        if combos > MAX_VARIANTS:
            break
    total = 1 + (combos if vary else 0) + len(explicit)
    if total > MAX_VARIANTS:
        raise Refused(f"{total} variants (the base included); at most {MAX_VARIANTS}: vary fewer values")
    variants = [{"id": "base", "name": "base (as written)", "set": {}}]
    if vary:
        for values in itertools.product(*(v for _, v in vary)):
            sets = {path: value for (path, _), value in zip(vary, values)}
            variants.append({"id": f"v{len(variants)}", "name": default_name(sets), "set": sets})
    for item in explicit:
        variants.append({"id": f"v{len(variants)}", "name": item["name"] or default_name(item["set"]),
                         "set": item["set"]})
    names = [v["name"] for v in variants]
    for index, variant in enumerate(variants):
        if names.count(variant["name"]) > 1:
            variant["name"] = (variant["name"][: MAX_NAME - 6] + f" #{index}")
    return variants


def parse_variants(text):
    """Explicit variants: {"variants": [{"name"?, "set": {path: value}}]} (or the list itself)."""
    if len(text.encode("utf-8")) > MAX_VARIANTS_BYTES:
        raise Refused(f"variants larger than {MAX_VARIANTS_BYTES // 1024} KiB")
    try:
        data = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except ValueError as exc:
        raise Refused(f"variants: not JSON ({exc})") from exc
    items = data.get("variants") if isinstance(data, dict) else data
    if not isinstance(items, list) or len(items) > MAX_VARIANTS - 1:
        raise Refused(f"variants: expected a list of at most {MAX_VARIANTS - 1} variants")
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {"name", "set"} or not isinstance(item.get("set"), dict):
            raise Refused(f"variants[{index}]: expected {{\"name\", \"set\": {{path: value}}}}")
        name = item.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME):
            raise Refused(f"variants[{index}].name: a nonempty string of at most {MAX_NAME} characters")
        sets = item["set"]
        if not sets or len(sets) > MAX_PATHS or any(not isinstance(k, str) or not k or len(k) > 256 for k in sets):
            raise Refused(f"variants[{index}].set: 1..{MAX_PATHS} settable paths")
        out.append({"name": name.strip() if name else None,
                    "set": {k: _json_value(k, v) for k, v in sets.items()}})
    return out


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _constant(name):
    raise ValueError(f"{name} is not allowed")


def layer_text(recipe_name, recipe_sha256, sets):
    """One overrides layer (the shape `hyperloom recipe save-layer` writes)."""
    return json.dumps({"schema": OVERRIDES_SCHEMA, "recipe": recipe_name, "recipe_sha256": recipe_sha256,
                       "source": "hyperloom whatif", "set": sets}, indent=1, sort_keys=False) + "\n"


def layer_directory():
    from hyperloom.recipes import service
    return service.runtime_dir() / "planner" / "variants"


def publish_layer(text):
    """The layer file, content-addressed (the same variant asked again keeps the same plan snapshot digest)."""
    data = text.encode("utf-8")
    directory = layer_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(data).hexdigest() + ".json")
    if not target.is_file() or target.read_bytes() != data:
        temporary = directory / f".{target.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, target)
    else:
        os.utime(target)
    return target


def prune_layers(now=None):
    """Remove variant layers older than a day, and the oldest beyond LAYER_KEEP_FILES."""
    directory = layer_directory()
    if not directory.is_dir():
        return
    now = time.time() if now is None else now
    files = []
    for path in directory.iterdir():
        try:
            if path.is_file() and not path.is_symlink():
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    files.sort()
    for index, (mtime, path) in enumerate(files):
        if now - mtime > LAYER_KEEP_S or len(files) - index > LAYER_KEEP_FILES:
            try:
                path.unlink()
            except OSError:
                pass


# --- the recipe command lines ------------------------------------------------------------------------------------

def recipe_line(argv):
    """Run one `hyperloom recipe ...` argv in this process (the CLI's parser and commands.run, cli.main's exit codes):
    {"exit_code", "ok", "result", "error"}."""
    from hyperloom.web import recipes as recipe_web
    try:
        outcome = recipe_web.in_process(argv, _loader)
    except ValueError as exc:            # the parser refused the argv
        return {"exit_code": 2, "ok": False, "result": None, "error": str(exc)}
    except Exception as exc:  # one failing command line is that variant's answer, not the batch's
        return {"exit_code": 1, "ok": False, "result": None, "error": f"{type(exc).__name__}: {exc}"}
    text = outcome.stdout.decode("utf-8", "replace").strip()
    error = outcome.stderr.decode("utf-8", "replace").strip()
    return {"exit_code": outcome.code, "ok": outcome.code == 0,
            "result": json.loads(text) if text else None,
            "error": "\n".join(line.removeprefix("hyperloom: ") for line in error.splitlines() if line.strip()) or None}


def _argv(operation, arguments, config, extra=()):
    """service.argv (the recipe API's own validator) plus files that live outside the API's name-only directories,
    inserted before --config as the portal's field edits are."""
    from hyperloom.recipes import service
    argv = service.argv(operation, arguments, config=config)
    position = argv.index("--config")
    argv[position:position] = list(extra)
    return argv


def _command_line(argv, names):
    from hyperloom.recipes import service
    return service.command_line([names.get(a, a) for a in argv])


def resolve_recipe(ref, cluster, catalog_only=False):
    """(path, catalog id or None): an id-shaped reference is looked up in the catalog first (it never names a file in
    the working directory); only a non-catalog reference that is an existing file is read as a file (CLI only)."""
    from hyperloom.recipes import service
    ref = str(ref)
    if service.ID.fullmatch(ref) and "/" not in ref and "\\" not in ref:
        try:
            row = service.find(ref, cluster)
            return Path(row["path"]), row["id"]
        except service.NotFound:
            if catalog_only or not Path(ref).is_file():
                raise
    elif catalog_only:
        raise service.NotFound(f"{ref}: not a recipe id")
    path = service.locate(ref, cluster)
    return path, service.identify(path, cluster)


class Budget:
    """The batch's own time budget, checked before every recipe command line."""

    def __init__(self, seconds):
        self.end = time.monotonic() + max(1.0, min(float(seconds), 600.0))

    def left(self):
        return self.end - time.monotonic()


def whatif(args):
    from hyperloom.recipes import reader, service, session
    started = time.monotonic()
    budget = Budget(args.deadline_s)
    service.use_config(args.config)
    cluster = _loader(args.config)
    path, recipe_id = resolve_recipe(args.recipe, cluster, args.catalog_only)
    text = session.read_text(path)
    recipe = reader.parse(text, path.name)
    profile = service.deployment_profile(cluster, path, args.profile)
    if profile not in cluster.profiles:
        raise Refused(f"unknown profile: {profile}")
    if args.prompt_tokens is not None and not 1 <= args.prompt_tokens <= 2 ** 24:
        raise Refused("--prompt-tokens must be in 1..16777216")
    explicit = []
    if args.variants:
        source = sys.stdin.read(MAX_VARIANTS_BYTES + 1) if args.variants == "-" else _read_bounded(args.variants)
        explicit = parse_variants(source)
    variants = build_variants(parse_vary(args.vary), explicit)
    facts_name, facts_path, record = resolve_facts(args.facts, [profile])
    recipe_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prune_layers()
    context = {"path": str(path), "shown": recipe_id or str(path), "profile": profile, "facts": facts_path,
               "recipe": recipe, "sha256": recipe_sha, "config": args.config, "prompt_tokens": args.prompt_tokens,
               "budget": budget, "paths": list(dict.fromkeys(p for v in variants for p in v["set"]))}
    shown = recipe_line(_argv("show", {"recipe": str(path)}, args.config))
    row = shown["result"] if shown["ok"] and isinstance(shown["result"], dict) else {}
    derived = bool(row.get("derived_from"))
    context.update(predictions=row.get("tradeoffs") or [], derived=derived,
                   base_fields={f["path"]: f.get("value") for f in row.get("fields") or []
                                if isinstance(f, dict) and "path" in f})
    hosts = ((record.get("raw") or {}).get("hosts")) or {}
    out = {"schema": WHATIF_SCHEMA, "recipe": recipe_id or str(path), "name": recipe["name"],
           "model": recipe["match"]["model"], "sha256": recipe_sha, "profile": profile,
           "facts": dict(facts_summary(facts_name, record),
                         hosts={h: {k: _number(v) for k, v in item.items() if k in ("free_ram_gib", "free_commit_gib")}
                                for h, item in hosts.items() if isinstance(item, dict)}),
           "prompt_tokens": args.prompt_tokens, "limits": {"variants": MAX_VARIANTS, "paths": MAX_PATHS},
           "predictions": {"rows": context["predictions"], "source": "the recipe's own [[x-predicted]] rows",
                           "inherited": derived, "error": None if shown["ok"] else shown["error"]},
           "notes": ["nothing is deployed: each variant is `hyperloom recipe explain` and `hyperloom recipe plan` on "
                     "the saved facts",
                     "the admission decision uses the saved facts (observed at a saved time, not live); the RTX service "
                     "plan inside `recipe plan` is computed now on this machine (current free RAM, saved header "
                     "descriptors)", "a fresh probe is the recipes probe job"],
           "variants": []}
    for variant in variants:
        if budget.left() <= 0:
            out["variants"].append(dict(variant, evaluated=False, why="not evaluated: the batch's time budget ran out"))
            continue
        out["variants"].append(evaluate_variant(variant, context))
    out["elapsed_s"] = round(time.monotonic() - started, 3)
    return out


def _read_bounded(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_VARIANTS_BYTES + 1)
    return data.decode("utf-8")


def evaluate_variant(variant, context):
    path = context["path"]
    names = {path: context["shown"], str(context["facts"]): Path(context["facts"]).name}
    extra = ["--facts", str(context["facts"])]
    row = dict(variant, evaluated=True, layer=None)
    layer_path = None
    if variant["set"]:
        text = layer_text(context["recipe"]["name"], context["sha256"], variant["set"])
        layer_path = str(publish_layer(text))
        names[layer_path] = f"{variant['id']}.json"
        extra += ["--recipe-overrides", layer_path]
        row["layer"] = {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "save_cli": "hyperloom recipe save-layer NAME --recipe " + _quote(context["shown"]) + "".join(
                            " --set " + _quote(f"{p}={_toml(v)}") for p, v in variant["set"].items())}
    explain_args = {"recipe": path, "profile": context["profile"], "all": True}
    if context["prompt_tokens"] is not None:
        explain_args["prompt_tokens"] = context["prompt_tokens"]
    explain_argv = _argv("explain", explain_args, context["config"], extra)
    plan_argv = _argv("plan", {"recipe": path, "profile": context["profile"]}, context["config"], extra)
    row["cli"] = {"explain": _command_line(explain_argv, names), "plan": _command_line(plan_argv, names)}
    explained = recipe_line(explain_argv)
    if context["budget"].left() > 0:
        planned = recipe_line(plan_argv)
    else:
        planned = {"exit_code": None, "ok": False, "result": None,
                   "error": "not run: the batch's time budget ran out"}
    row["explain"] = {k: explained[k] for k in ("exit_code", "ok", "error")}
    row["plan"] = {k: planned[k] for k in ("exit_code", "ok", "error")}
    row.update(_rename(summarize(variant, explained["result"], planned["result"],
                                 explain_error=explained["error"], plan_error=planned["error"],
                                 base_fields=context.get("base_fields"), paths=context.get("paths")),
                       {layer_path: names[layer_path]} if layer_path else {}))
    derived = context["derived"]
    row["predictions"] = {"label": ("predicted (recipe)" if not variant["set"] and not derived else
                                    "base recipe prediction, not re-measured for this variant" if not derived else
                                    "base recipe prediction (a copy's inherited numbers), not re-measured")}
    return row


def _rename(value, mapping):
    """Replace the planner's private file paths in strings by their displayed names."""
    if not mapping:
        return value
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_rename(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _rename(v, mapping) for k, v in value.items()}
    return value


def _toml(value):
    """A value as --set text (JSON scalars and arrays are TOML value text)."""
    return json.dumps(value)


def _quote(text):
    import shlex
    return shlex.quote(text)


def _unavailable(why):
    return {"value": None, "why": why}


def summarize(variant, explained, planned, *, explain_error=None, plan_error=None, base_fields=None, paths=None):
    """The side-by-side rows of one variant from the two command outputs. Semantic fields decide (explain exits 0 while
    reporting a refusal); anything a command did not report is {"value": None, "why": ...}."""
    explained = explained if isinstance(explained, dict) else {}
    planned = planned if isinstance(planned, dict) else {}
    fields = {f["path"]: f for f in explained.get("fields") or [] if isinstance(f, dict) and "path" in f}
    hardware = explained.get("hardware") if isinstance(explained.get("hardware"), dict) else None
    decision = (hardware or {}).get("deploy") if isinstance((hardware or {}).get("deploy"), dict) else None
    up = planned.get("up") if isinstance(planned.get("up"), dict) else {}
    preview = planned.get("preview") if isinstance(planned.get("preview"), dict) else {}
    plan_eval = planned.get("evaluation") if up.get("ok") else preview.get("evaluation")
    plan_eval = plan_eval if isinstance(plan_eval, dict) else {}
    plan_decision = plan_eval.get("deploy") if isinstance(plan_eval.get("deploy"), dict) else None
    no_explain = explain_error or "explain did not answer"
    shown = {}
    for path in dict.fromkeys([*(paths or []), *variant["set"], *FLOOR_FIELDS]):
        item = fields.get(path)
        if item:
            shown[path] = {"value": item.get("value"), "layer": item.get("layer"), "origin": item.get("origin"),
                           "shadowed": item.get("shadowed") or []}
        elif fields and path in (base_fields or {}) and path not in variant["set"]:
            # explain lists what it resolved; a field left at its schema default is the recipe's own value.
            shown[path] = {"value": base_fields[path], "layer": "default", "origin": "recipe show", "shadowed": []}
        else:
            shown[path] = _unavailable(no_explain if not fields else "not a field of this recipe")
    budgets = {}
    for path, item in fields.items():
        parts = path.split(".")
        if len(parts) == 5 and parts[0] == "routes" and parts[2] == "placement" and parts[4] in BUDGET_FIELDS:
            budgets.setdefault(parts[1], {}).setdefault(parts[3], {})[parts[4]] = item.get("value")
    # Charges: the plan's decision when up passes (it carries the service's planned disk tier), else explain's.
    if up.get("ok") and plan_decision is not None:
        charge_source, charge_label = plan_decision, "cap model charge (hyperloom-cap-v1), as up would admit it"
    elif decision is not None:
        charge_source = decision
        charge_label = "cap model charge (hyperloom-cap-v1), without the service's planned disk tier (up refused)"
    else:
        charge_source, charge_label = None, None
    charges = [{k: req.get(k) for k in ("route", "set", "member", "id", "host_bytes", "commit_bytes", "disk_bytes",
                                        "model")}
               for req in (charge_source or {}).get("requirements") or [] if isinstance(req, dict)]
    refused = (hardware or {}).get("refused")
    no_decision = ("no decision: " + str(refused) if refused else
                   "no decision: the hardware does not match" if (hardware or {}).get("match") else
                   "no decision: " + no_explain)
    table = planned.get("route_table") if up.get("ok") and isinstance(planned.get("route_table"), dict) else None
    no_table = "no route table (up refused: " + str(up.get("refused") or plan_error or "plan did not answer") + ")"
    prefill = {p.get("id"): p for p in (decision or {}).get("prefill") or [] if isinstance(p, dict)}
    compiled = {r.get("id"): r for r in (table or {}).get("routes") or [] if isinstance(r, dict)}
    routes = []
    for route in explained.get("routes") or []:
        if not isinstance(route, dict):
            continue
        rid = route.get("id")
        entry = {"id": rid, "phase": route.get("phase"), "devices": route.get("devices"),
                 "enabled_in_recipe": route.get("enabled"), "when": route.get("when")}
        if decision is None:
            entry["decision"] = _unavailable(no_decision)
        elif route.get("phase") == "decode":
            entry["deployed"] = (decision.get("decode") or {}).get("id") == rid
        elif rid in prefill:
            entry.update(place=prefill[rid].get("place"), admitted=prefill[rid].get("admitted"),
                         why=prefill[rid].get("why"))
        if route.get("phase") == "prefill":
            entry["table"] = ({"enabled": compiled[rid].get("enabled"), "charges_gib": compiled[rid].get("charges"),
                               "commit_charges_gib": compiled[rid].get("commit_charges")}
                              if rid in compiled else _unavailable(no_table))
        routes.append(entry)
    service = planned.get("service") if up.get("ok") else preview.get("service")
    if isinstance(service, dict):
        plan_doc = service.get("plan") if isinstance(service.get("plan"), dict) else None
        service_row = {"start": service.get("start"), "why": service.get("why"),
                       "vram_cap_gib": service.get("vram_cap_gib"), "host_gib": service.get("host_gib"),
                       "plan": None if plan_doc is None else {
                           k: plan_doc.get(k) for k in ("vram_cap_gib", "predicted_peak_gib", "resident_gib",
                                                        "pinned_host_gib", "disk_gib", "fixed_gib", "stream_slots_gib",
                                                        "resident_units", "streamed_units", "tier_units", "notes")},
                       "label": "planned now on this machine (current free RAM, saved header descriptors)"}
    elif decision is not None:
        service_row = dict(decision.get("service") or {}, plan=None,
                           label="decision only: " + str(preview.get("label") or plan_error or
                                                         "plan reported no service plan"))
    else:
        service_row = _unavailable(no_decision)
    return {"fields": shown,
            "match": (hardware or {}).get("match") if hardware else None,
            "fits": None if hardware is None else not (hardware.get("match") or []),
            "untuned": explained.get("untuned"), "refused_layers": explained.get("refused_layers"),
            "decision": None if decision is None else {"decode": (decision.get("decode") or {}).get("id"),
                                                       "decode_flags": (decision.get("decode") or {}).get("flags")},
            "refused": refused if hardware is not None else no_explain,
            "routes": routes,
            "simulated_request": explained.get("simulated_request"),
            "resources": {"budgets": budgets, "budgets_label": "the recipe's placement fields (effective values)",
                          "charges": charges if charge_source is not None else _unavailable(no_decision),
                          "charges_label": charge_label, "service": service_row},
            "floors": {"floor_gib": _field_value(fields, FLOOR_FIELDS[0]),
                       "commit_floor_gib": _field_value(fields, FLOOR_FIELDS[1]),
                       "hosts": ((plan_eval.get("availability") or (hardware or {}).get("availability") or {})
                                 .get("hosts")), "hosts_label": "saved facts"},
            "up": {"ok": up.get("ok"), "refused": up.get("refused") if up else (plan_error or "plan did not answer"),
                   "conditions": up.get("conditions"), "preview": preview.get("label"),
                   "inputs": planned.get("inputs")}}


def _field_value(fields, path):
    item = fields.get(path)
    return None if item is None else item.get("value")


# --- placement preview -------------------------------------------------------------------------------------------

def tensormeld_root():
    from hyperloom.render import repo_root
    return Path(repo_root()) / "third_party" / "tensormeld"


def parts_file(model):
    return tensormeld_root() / "examples" / "hetero" / f"{model}.parts.json"


def template_file():
    return tensormeld_root() / "examples" / "hetero" / "lab-hardware.json"


def planner_available():
    """(available, why): the pinned TensorMeld has its heterogeneous planner and the example hardware."""
    source = tensormeld_root() / "src" / "tensormeld" / "hetero_planner.py"
    if not source.is_file():
        return False, "the pinned TensorMeld has no heterogeneous planner (third_party/tensormeld)"
    if not template_file().is_file():
        return False, "the pinned TensorMeld has no example hardware (examples/hetero/lab-hardware.json)"
    return True, None


def models_with_parts(models):
    out = {}
    available, why = planner_available()
    for model in models:
        if not available:
            out[model] = {"available": False, "why": why}
        elif parts_file(model).is_file():
            out[model] = {"available": True}
        else:
            out[model] = {"available": False, "why": f"the pinned TensorMeld has no model parts for {model}"}
    return out


def _load_json(path, limit=2 * 1024 * 1024):
    data = Path(path).read_bytes()
    if len(data) > limit:
        raise Refused(f"{Path(path).name}: larger than {limit // 1024} KiB")
    return json.loads(data.decode("utf-8"))


def _template(templates, kind, backend, memory_bytes):
    """The template device of this kind whose memory is closest and within the tolerance, or None."""
    best = None
    for device in templates:
        if device.get("kind") != kind or device.get("backend") != backend:
            continue
        reference = device.get("memory_bytes")
        if not isinstance(reference, int) or reference <= 0:
            continue
        gap = abs(memory_bytes - reference) / reference
        if gap <= TEMPLATE_MEMORY_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, device)
    return None if best is None else best[1]


RATE_KEYS = ("mem_bytes_per_s", "prefill_ops_per_s", "prefill_rates", "supports")


def _rates(template):
    """The template's rate inputs, every planner default written out explicitly."""
    out = {k: template[k] for k in RATE_KEYS if k in template}
    out["decode_ops_per_s"] = template.get("decode_ops_per_s", template.get("prefill_ops_per_s"))
    out["step_overhead_us"] = template.get("step_overhead_us", 0)
    out["crossing_overhead_us"] = template.get("crossing_overhead_us", 0)
    return out


def _rate_note(template):
    notes = [f"rates from the TensorMeld example hardware device {template.get('id')} "
             f"(provenance there: {template.get('provenance', 'not stated')}); this device matches it only by kind and "
             f"memory size, so they are estimates here"]
    if "decode_ops_per_s" not in template:
        notes.append("decode_ops_per_s = prefill_ops_per_s (the planner's own default)")
    for key in ("step_overhead_us", "crossing_overhead_us"):
        if key not in template:
            notes.append(f"{key} = 0 (not in the example)")
    notes.append("supports = the example's modelled backend capabilities, not verified on this device")
    return "; ".join(notes)


def build_hardware(cluster, record, model, template):
    """(hardware JSON, sources, excluded, notes) from a saved facts record. Memory comes from the facts; rates, the
    reserve and the supported parts from the example device of the same kind and memory size; anything without such
    evidence is left out and listed, never filled in. Every input the facts do not hold is named in ``sources``."""
    from hyperloom.recipes import facts as recipe_facts
    pc = recipe_facts.control_machine(cluster)
    gathered = record.get("facts") or {}
    raw = record.get("raw") or {}
    available = raw.get("devices") if isinstance(raw.get("devices"), dict) else {}
    hosts = raw.get("hosts") if isinstance(raw.get("hosts"), dict) else {}
    usb4_state = raw.get("usb4") if isinstance(raw.get("usb4"), dict) else {}
    templates = [d for d in template.get("devices") or [] if isinstance(d, dict)]
    template_nodes = {n.get("id"): n for n in template.get("nodes") or [] if isinstance(n, dict)}
    template_links = [link for link in template.get("links") or [] if isinstance(link, dict)]
    devices_out, sources, excluded, notes = [], [], [], []
    machines = cluster.machines
    nodes = []
    asset = recipe_facts.FAMILY_MODELS_BY_ID.get(model)

    def node(mid, disk_gbps, template_device):
        if any(n["id"] == mid for n in nodes):
            return
        configured = bool(asset) and asset in (cluster.assets.get(mid) or {})
        row = {"id": mid, "model_files": [model] if configured else [], "disk_free_bytes": 0}
        if _number(disk_gbps) and disk_gbps > 0:
            row["disk_bytes_per_s"] = int(disk_gbps * 1e9)
            disk = "measured disk read from the saved facts"
        else:
            reference = template_nodes.get((template_device or {}).get("node")) or {}
            row["disk_bytes_per_s"] = int(reference.get("disk_bytes_per_s") or 1_000_000_000)
            disk = ("TensorMeld example hardware (not measured here)" if reference.get("disk_bytes_per_s")
                    else "1 GB/s, the planner's default (not measured here)")
        nodes.append(row)
        sources.append({"node": mid, "model_files": ("configured asset, presence not verified" if configured else
                                                     "no configured asset for this model"),
                        "disk_read": disk, "disk_free": "not known (0; the planner does not use it for placement)"})
    devices = gathered.get("devices") if isinstance(gathered.get("devices"), dict) else {}
    inventory = {g.get("uuid"): g for g in (record.get("inventory") or {}).get("cuda") or []
                 if isinstance(g, dict) and g.get("uuid")}
    node_gpus = []
    control_devices = getattr(machines.get(pc), "devices", None) or []
    for key, fact in devices.items():
        if not isinstance(fact, dict):
            continue
        memory = _number(fact.get("memory_gib"))
        if key in machines and key != pc:
            kind, backend, ident, host = "integrated_gpu", "hip", f"{key}.gpu", key
        elif key in inventory:
            local = next((d.get("id") for d in control_devices if isinstance(d, dict) and d.get("uuid") == key), None)
            kind, backend, host = "discrete_gpu", "cuda", pc
            ident = f"{pc}.{local or 'cuda' + str(list(inventory).index(key))}"
        else:
            continue
        label = fact.get("name") or key
        if memory is None or memory <= 0:
            excluded.append({"device": ident, "name": label, "why": "memory not in the saved facts"})
            continue
        memory_bytes = int(memory * GIB)
        chosen = _template(templates, kind, backend, memory_bytes)
        if chosen is None:
            excluded.append({"device": ident, "name": label,
                             "why": f"no example {kind} with memory within {TEMPLATE_MEMORY_TOLERANCE:.0%} of "
                                    f"{memory:.1f} GiB: no rate evidence for it"})
            continue
        state = available.get(key) if isinstance(available.get(key), dict) else None
        why = (state or {}).get("why") or ""
        if state is None:
            availability, why = "unavailable", "availability not in the saved facts"
        elif kind == "discrete_gpu":
            busy = state.get("available") is False and "busy" in why.lower()
            availability = "unavailable" if busy or state.get("available") not in (True, False) else "preemptible"
            if state.get("available") is False and not busy:
                notes.append(f"{ident}: {why} (still planned: this is a preview, not the profile's launch)")
        else:
            availability = "available" if state.get("available") is True else "unavailable"
        node(host, fact.get("disk_read_gbytes_ps"), chosen)
        device = {"id": ident, "node": host, "kind": kind, "backend": backend, "memory_bytes": memory_bytes,
                  "reserved_bytes": int(chosen.get("reserved_bytes", 0)), **_rates(chosen),
                  "availability": availability, "provenance": "estimate"}
        devices_out.append(device)
        sources.append({"device": ident, "name": label, "memory": "saved facts", "rates": _rate_note(chosen),
                        "reserve": f"{int(chosen.get('reserved_bytes', 0)) / GIB:.1f} GiB, from the example device",
                        "availability": availability + (f" ({why})" if why else "")})
        if kind == "integrated_gpu" and availability != "unavailable":
            node_gpus.append((key, ident))
    # The control machine's host RAM, for the parts the example host device runs (token lookups).
    host_total = next((_number(f.get("host_ram_gib")) for k, f in devices.items()
                       if k in inventory and isinstance(f, dict) and _number(f.get("host_ram_gib"))), None)
    host_template = next((d for d in templates if d.get("kind") == "cpu" and d.get("backend") == "host"), None)
    free = _number((hosts.get(pc) or {}).get("free_ram_gib")) if isinstance(hosts.get(pc), dict) else None
    if host_template is not None and (host_total is None or free is None):
        excluded.append({"device": f"{pc}.host", "name": "host RAM",
                         "why": "the control machine's host RAM total or free RAM is not in the saved facts"})
    elif host_template is not None:
        total = int(host_total * GIB)
        allowance = int(host_template.get("memory_bytes", 0)) - int(host_template.get("reserved_bytes", 0))
        usable = max(0, min(allowance, int(free * GIB), total))
        node(pc, None, host_template)
        devices_out.append({"id": f"{pc}.host", "node": pc, "kind": "cpu", "backend": "host", "memory_bytes": total,
                            "reserved_bytes": total - usable, **_rates(host_template),
                            "availability": "available", "provenance": "estimate"})
        sources.append({"device": f"{pc}.host", "name": "host RAM",
                        "memory": f"saved facts: total {host_total:.1f} GiB, usable {usable / GIB:.1f} GiB = the "
                                  f"smaller of the saved free RAM ({free:.1f} GiB) and the example's allowance "
                                  f"({allowance / GIB:.1f} GiB)", "rates": _rate_note(host_template),
                        "availability": "available"})
    links_out, groups = [], []
    by_kind = {}
    for link in template_links:
        a = next((d for d in templates if d.get("node") == link.get("a")), None)
        b = next((d for d in templates if d.get("node") == link.get("b")), None)
        kind = "usb4" if "usb4" in str(link.get("id")) else "ethernet"
        pair = "nodes" if a and b and a.get("backend") == b.get("backend") == "hip" else "control"
        by_kind.setdefault((kind, pair), link)
    known = {n["id"] for n in nodes}
    for link in gathered.get("links") or []:
        if not isinstance(link, dict) or len(link.get("hosts") or []) != 2:
            continue
        a, b = link["hosts"]
        if a not in known or b not in known or a == b:
            continue
        if link.get("kind") == "ethernet" and _number(link.get("gbps")) and link["gbps"] > 0:
            reference = by_kind.get(("ethernet", "control" if pc in (a, b) else "nodes")) or by_kind.get(
                ("ethernet", "control"))
            row = {"id": f"eth-{a}-{b}", "a": a, "b": b, "bytes_per_s": int(link["gbps"] * 1e9 / 8),
                   "latency_us": float((reference or {}).get("latency_us", 50)), "provenance": "estimate",
                   "notes": "negotiated NIC speed from the saved facts: an assumption about the whole path, not "
                            "measured throughput"}
            if pc in (a, b):
                row["shared_group"] = f"{pc}-nic"
            links_out.append(row)
            sources.append({"link": row["id"], "rate": "negotiated NIC speed from the saved facts (an assumption about "
                                                       "the whole path, not measured throughput)",
                            "latency": ("TensorMeld example hardware link " + str(reference.get("id"))) if reference
                            else "50 us, assumed"})
        elif link.get("kind") == "usb4":
            ident = f"usb4-{a}-{b}"
            if not all(isinstance(usb4_state.get(h), dict) and usb4_state[h].get("up") is True for h in (a, b)):
                excluded.append({"link": ident, "why": "USB4 not up on both nodes in the saved facts"})
                continue
            reference = by_kind.get(("usb4", "nodes"))
            if reference is None:
                excluded.append({"link": ident, "why": "no USB4 link in the TensorMeld example hardware"})
                continue
            row = {"id": ident, "a": a, "b": b, "bytes_per_s": int(reference["bytes_per_s"]),
                   "latency_us": float(reference.get("latency_us", 0)), "provenance": "estimate",
                   "notes": "USB4 bandwidth is not probed by the facts: rate from the TensorMeld example hardware"}
            links_out.append(row)
            sources.append({"link": ident, "rate": f"TensorMeld example hardware link {reference.get('id')} "
                                                   f"(provenance there: {reference.get('provenance', 'not stated')}; "
                                                   "not probed here)",
                            "latency": "TensorMeld example hardware"})
            template_group = next((g for g in template.get("groups") or []
                                   if isinstance(g, dict) and g.get("link") == reference.get("id")), None)
            members = [d for mid, d in node_gpus if mid in (a, b)]
            if template_group is not None and len(members) == 2:
                groups.append({"id": f"pair-{a}-{b}", "devices": members, "link": ident,
                               "supports": list(template_group.get("supports") or ["*"])})
                sources.append({"group": f"pair-{a}-{b}", "supports": "the example group's modelled engine group "
                                                                      "modes over that link"})
    hardware = {"schema": HARDWARE_SCHEMA, "nodes": nodes, "devices": devices_out, "links": links_out,
                "groups": groups, "notes": "built by hyperloom placement from saved facts; rates from the TensorMeld "
                                           "example hardware (estimates here)"}
    return hardware, sources, excluded, notes


SCENARIOS = (("nodes", "nodes only"), ("rtx-prefill", "nodes + RTX prefill"),
             ("all", "all devices (the planner may put hot parts on the RTX)"))


def scenario_hardware(hardware, scenario, pc):
    """A copy of the hardware with this scenario's availability (nothing unavailable becomes available); None when
    the scenario needs an RTX the inputs do not have."""
    copy = json.loads(json.dumps(hardware))
    cuda = [d for d in copy["devices"] if d["backend"] == "cuda" and d["availability"] != "unavailable"]
    if scenario == "nodes":
        for device in copy["devices"]:
            if device["node"] == pc:
                device["availability"] = "unavailable"
    elif not cuda:
        return None
    elif scenario == "rtx-prefill":
        for device in cuda:
            device["availability"] = "prefill-only"
    return copy


def bounded_child(argv, *, env, cwd, timeout_s):
    """(returncode | None, stdout, stderr, why): one child with a deadline and capped output (killed on either)."""
    from hyperloom.winproc import no_window
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, cwd=cwd, **no_window())
    out, err, overflow = bytearray(), bytearray(), threading.Event()

    def drain(stream, buffer, cap):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if len(buffer) + len(chunk) > cap:
                overflow.set()
                buffer.extend(chunk[: max(0, cap - len(buffer))])
                process.kill()
                break
            buffer.extend(chunk)
    readers = [threading.Thread(target=drain, args=(process.stdout, out, STDOUT_CAP), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, err, STDERR_CAP), daemon=True)]
    for reader in readers:
        reader.start()
    why = None
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        why = "deadline"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
    for reader in readers:
        reader.join(timeout=5)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if overflow.is_set():
        why = "output overflow"
    return (None if why else process.returncode), bytes(out), bytes(err), why


def run_planner(parts, hardware_path, request, *, timeout_s, workdir):
    """One `python -m tensormeld plan-hetero` run (the pinned copy, this interpreter): (result | None, error, cli).
    Any answer with the plan schema is a result, whatever the exit code (no feasible placement included)."""
    source = str(tensormeld_root() / "src")
    options = ["--prompt", str(request["prompt"]), "--context", str(request["prompt"] + request["output"]),
               "--output", str(request["output"]), "--slots", str(request["slots"]),
               "--objective", request["objective"], "--top-k", str(request["top_k"])]
    argv = [sys.executable, "-m", "tensormeld", "plan-hetero", str(parts), str(hardware_path), *options]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONPATH=source + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    cli = " ".join(["python", "-m", "tensormeld", "plan-hetero", f"examples/hetero/{Path(parts).name}",
                    "hardware.json", *options])
    code, out, err, why = bounded_child(argv, env=env, cwd=workdir, timeout_s=timeout_s)
    message = err.decode("utf-8", "replace").strip().removeprefix("error: ")
    if why:
        return None, {"deadline": f"the planner did not finish in {timeout_s:.0f} s (stopped)",
                      "output overflow": "the planner printed more than allowed (stopped)"}[why], cli
    try:
        result = json.loads(out.decode("utf-8")) if out.strip() else None
    except ValueError:
        result = None
    if isinstance(result, dict) and result.get("schema") == "tensormeld/hetero-plan-v1":
        return result, None, cli
    return None, message or (f"the planner failed (exit {code})" if code else "the planner printed no plan"), cli


def _candidate(c):
    if not isinstance(c, dict):
        return None
    decode = c.get("decode") or {}
    prefill = c.get("prefill") or {}
    per_set = lambda placement: None if not isinstance(placement, dict) else {  # noqa: E731
        s: {k: row.get(k) for k in ("parts", "weight_bytes", "kinds")}
        for s, row in placement.items() if isinstance(row, dict)}
    out = {"name": c.get("name"), "mode": c.get("mode"), "feasible": c.get("feasible"), "devices": c.get("devices"),
           "sets": c.get("sets"), "memory_gib": c.get("memory_gib"), "memory_label": "per device, as the planner computes it",
           "decode_tok_s": decode.get("tokens_per_s"), "aggregate_tok_s": c.get("aggregate_tokens_per_s"),
           "step_ms": decode.get("step_ms"), "wire_share": decode.get("wire_share"),
           "prefill_tok_s": prefill.get("tokens_per_s"), "ttft_s": c.get("ttft_s"), "request_s": c.get("request_s"),
           "uses_preemptible": c.get("uses_preemptible"), "problems": c.get("problems"),
           "placement": per_set(c.get("placement")), "prefill_placement": per_set(c.get("prefill_placement")),
           "placement_label": "weight bytes per device set"}
    if c.get("replicas"):
        out["replicas"] = c["replicas"]
        out["replicas_label"] = "memory and placement of one replica (the planner reports one map)"
    return out


def shape_plan(result):
    """The parts of a hetero plan the preview shows (every rate is the planner's prediction)."""
    fallback = result.get("fallback") if isinstance(result.get("fallback"), dict) else {}
    co_resident = fallback.get("co_resident") if isinstance(fallback.get("co_resident"), dict) else None
    return {"status": result.get("status"), "qualified": result.get("qualified"), "executable": result.get("executable"),
            "unmeasured_inputs": result.get("unmeasured_inputs"), "evaluated": result.get("evaluated"),
            "best": _candidate(result.get("best")),
            "candidates": [c for c in (_candidate(c) for c in result.get("candidates") or []) if c],
            "best_by_mode": {mode: (c or {}).get("name") for mode, c in (result.get("best_by_mode") or {}).items()},
            "fallback": None if not fallback else {
                "without": fallback.get("without"), "best": _candidate(fallback.get("best")),
                "co_resident": None if co_resident is None else {
                    k: co_resident.get(k) for k in ("fits", "problems", "memory_gib", "meaning")}},
            "infeasible": [{"name": c.get("name"), "problem": (c.get("problems") or [None])[0]}
                           for c in result.get("infeasible") or [] if isinstance(c, dict)][:20],
            "hardware_fingerprint": result.get("hardware_fingerprint"),
            "model_fingerprint": result.get("model_fingerprint"), "basis": "predicted (planner)"}


def placement(args):
    from hyperloom.recipes import facts as recipe_facts, service
    started = time.monotonic()
    budget = Budget(args.deadline_s)
    service.use_config(args.config)
    cluster = _loader(args.config)
    model = args.model
    if model not in recipe_facts.FAMILY_MODELS_BY_ID:
        raise Refused(f"unknown model {model} (known: {', '.join(sorted(recipe_facts.FAMILY_MODELS_BY_ID))})")
    for name, value, low, high in (("--prompt", args.prompt, 1, 1 << 20), ("--output", args.output, 1, 65536),
                                   ("--slots", args.slots, 1, 64), ("--top-k", args.top_k, 1, 10)):
        if not low <= value <= high:
            raise Refused(f"{name} must be in {low}..{high}")
    available, why = planner_available()
    if not available:
        raise Refused(why)
    parts = parts_file(model)
    if not parts.is_file():
        raise Refused(f"unavailable · the pinned TensorMeld has no model parts for {model}")
    profiles = service.model_profiles(cluster, model)
    facts_name, _, record = resolve_facts(args.facts, profiles if not args.facts else [])
    template = _load_json(template_file())
    hardware, sources, excluded, notes = build_hardware(cluster, record, model, template)
    pc = recipe_facts.control_machine(cluster)
    request = {"prompt": args.prompt, "output": args.output, "context": args.prompt + args.output,
               "slots": args.slots, "objective": args.objective, "top_k": args.top_k}
    out = {"schema": PLACEMENT_SCHEMA, "advisory": ADVISORY, "model": model, "facts": facts_summary(facts_name, record),
           "request": request, "hardware": hardware, "hardware_sources": sources, "excluded": excluded,
           "notes": notes + ["every rate below is predicted by TensorMeld's planner from these inputs; nothing is "
                             "measured, deployed or qualified here"],
           "tensormeld": {"parts": f"examples/hetero/{parts.name}", "hardware_template": "examples/hetero/lab-hardware.json"},
           "scenarios": []}
    with tempfile.TemporaryDirectory(prefix="hl-placement-") as directory:
        for ident, label in SCENARIOS:
            row = {"id": ident, "label": label}
            hw = scenario_hardware(hardware, ident, pc)
            if hw is None:
                row.update(status="unavailable", why=f"no available RTX device in the planner's inputs (saved facts "
                                                     f"{facts_name}; see excluded)")
            elif not [d for d in hw["devices"] if d["availability"] != "unavailable"]:
                row.update(status="unavailable", why="no available device in the planner's inputs")
            elif budget.left() < 1:
                row.update(status="unavailable", why="not run: the time budget ran out")
            else:
                path = Path(directory) / f"{ident}.hardware.json"
                path.write_text(json.dumps(hw, indent=1), encoding="utf-8")
                result, error, cli = run_planner(parts, path, request, workdir=directory,
                                                 timeout_s=min(SCENARIO_DEADLINE_S, budget.left()))
                row["cli"] = cli
                row["availability"] = {d["id"]: d["availability"] for d in hw["devices"]}
                if result is None:
                    row.update(status="failed", error=error)
                else:
                    row.update(shape_plan(result))
            out["scenarios"].append(row)
    out["elapsed_s"] = round(time.monotonic() - started, 3)
    return out
