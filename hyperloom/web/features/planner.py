"""Recipe what-if planner (F33) and placement preview (F34).

GET  planner            -> the page document: profiles serving recipe models, saved hardware facts (name, profile,
                           observed time), placement inputs per model, limits. Reads our own facts directory only.
POST planner/whatif     -> `hyperloom whatif RECIPE --profile P [--facts NAME] [--vary PATH=V1,V2]... [--variants -]
                           [--prompt-tokens N]`
POST planner/placement  -> `hyperloom placement MODEL [--facts NAME] [--prompt N] [--output N] [--slots N]
                           [--objective O] [--top-k K]`

Every POST is that command line (cliext/planner.py), run by the portal's recipe runner: shared admission with the
recipe requests (2 running, 4 waiting, else 503), one deadline from admission, capped output, the whole process tree
removed on deadline, overflow or portal shutdown. The answer carries the command line, its exit code, its JSON and
its message. Saving a variant as a layer is the existing audited POST recipes/save-layer.
"""

import json
import math
import re

DEADLINE_S = 150
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PATH = re.compile(r"[A-Za-z0-9_.-]{1,256}")
WHATIF_KEYS = {"recipe", "profile", "facts", "vary", "variants", "prompt_tokens"}
PLACEMENT_KEYS = {"model", "facts", "prompt", "output", "slots", "objective", "top_k"}
_facts_cache = {}


def register(reg):
    from hyperloom.cliext import planner as cli_planner
    reg.get("planner", page)
    reg.post("planner/whatif", whatif)
    reg.post("planner/placement", placement)
    reg.capability("planner-whatif", True, "recipe variants side by side on saved hardware facts (nothing deployed)")
    from hyperloom.recipes import facts as recipe_facts
    available, why = cli_planner.planner_available()
    if available and not any(cli_planner.parts_file(m).is_file() for m in recipe_facts.FAMILY_MODELS_BY_ID):
        available, why = False, "the pinned TensorMeld has model parts for none of the recipe models"
    reg.capability("placement-preview", available,
                   "advisory placements from the pinned TensorMeld planner, not qualified" if available else why)


# --- GET -------------------------------------------------------------------------------------------------------------

def _facts_rows(cli_planner):
    """Saved facts records (bounded; each file parsed once per (name, size, mtime))."""
    from hyperloom.recipes import service
    directory = service.facts_dir()
    rows, seen = [], set()
    if not directory.is_dir():
        return rows, None
    files = sorted(directory.glob("*.json"))
    for path in files[: cli_planner.MAX_FACTS_FILES]:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        seen.add(key)
        if key not in _facts_cache:
            record = cli_planner.read_facts(path)
            _facts_cache[key] = None if record is None else dict(
                cli_planner.facts_summary(path.stem, record),
                devices=len((record.get("facts") or {}).get("devices") or {}))
        if _facts_cache[key] is not None:
            rows.append(_facts_cache[key])
    for key in [k for k in _facts_cache if k not in seen]:
        _facts_cache.pop(key, None)
    rows.sort(key=lambda r: -(r["observed_at"] or 0))
    more = len(files) - cli_planner.MAX_FACTS_FILES
    return rows, (f"{more} more facts files are not listed (at most {cli_planner.MAX_FACTS_FILES})" if more > 0 else None)


def page(api, query):
    from hyperloom.recipes import service
    if query:
        raise ValueError("planner takes no query arguments")
    with service.configured(api.config_path):     # the portal's configuration, for this request only
        return _page(api)


def _page(api):
    from hyperloom.cliext import planner as cli_planner
    from hyperloom.recipes import service
    why = {}
    try:
        cluster = api.cluster()
    except Exception as exc:  # an unreadable configuration is shown, not raised
        cluster, why["profiles"] = None, f"cluster configuration unavailable: {exc}"
    models = service.models(cluster) if cluster is not None else {}
    profiles = [{"id": p, "model": m} for m, names in models.items() for p in names]
    facts, more = _facts_rows(cli_planner)
    if more:
        why["facts"] = more
    if not facts:
        why["facts"] = "no saved hardware facts: run a probe with an output name on the Recipes page"
    available, reason = cli_planner.planner_available()
    return api.document("planner", profiles=profiles, models=sorted(models), facts=facts,
                        placement={"available": available, "why": reason,
                                   "models": cli_planner.models_with_parts(sorted(models)),
                                   "objectives": list(cli_planner.OBJECTIVES), "advisory": cli_planner.ADVISORY},
                        limits={"variants": cli_planner.MAX_VARIANTS, "paths": cli_planner.MAX_PATHS,
                                "prompt_tokens": [1, 1 << 24], "prompt": [1, 1 << 20], "output": [1, 65536],
                                "slots": [1, 64], "top_k": [1, 10], "deadline_s": DEADLINE_S},
                        why=why)


# --- POST ------------------------------------------------------------------------------------------------------------

def _name(arguments, key, required=False):
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"{key} must be an id [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    return value


def _int(arguments, key, low, high):
    value = arguments.get(key)
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer in {low}..{high}")
    return value


def _value_text(value, where):
    """A JSON value as --set value text (JSON scalars and arrays are TOML value text)."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        if value is None:
            raise ValueError(f"{where}: null is not a value")
        if isinstance(value, str) and ("\n" in value or "\0" in value or len(value) > 1024):
            raise ValueError(f"{where}: a string of at most 1024 characters on one line")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{where}: numbers must be finite")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, list) and len(value) <= 256:
        return json.dumps([json.loads(_value_text(v, where)) for v in value])
    raise ValueError(f"{where}: values are numbers, booleans, strings or arrays of them")


def whatif_argv(api, arguments):
    from hyperloom.cliext import planner as cli_planner
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = set(arguments) - WHATIF_KEYS
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)))
    from hyperloom.recipes import service
    if arguments.get("recipe") is None:
        raise ValueError("recipe is required")
    # A catalog id only, as every recipe reference of the portal: no path, drive, stream or file suffix.
    argv = ["whatif", service.recipe_ref("recipe", arguments["recipe"]), "--catalog-only"]
    for key, flag in (("profile", "--profile"), ("facts", "--facts")):
        if _name(arguments, key) is not None:
            argv += [flag, arguments[key]]
    vary = arguments.get("vary")
    vary = {} if vary is None else vary
    if not isinstance(vary, dict) or len(vary) > 8:
        raise ValueError("vary must be an object of at most 8 paths, each with a list of values")
    combos = 1
    for path, values in vary.items():
        if not isinstance(path, str) or not PATH.fullmatch(path):
            raise ValueError(f"vary: {path!r} is not a settable path")
        if not isinstance(values, list) or not 1 <= len(values) <= cli_planner.MAX_VARIANTS:
            raise ValueError(f"vary.{path}: 1..{cli_planner.MAX_VARIANTS} values")
        combos *= len(values)
        argv += ["--vary", f"{path}=" + ",".join(_value_text(v, f"vary.{path}") for v in values)]
    variants = arguments.get("variants")
    stdin = None
    if variants is not None:
        if not isinstance(variants, list) or len(variants) > cli_planner.MAX_VARIANTS - 1:
            raise ValueError(f"variants: a list of at most {cli_planner.MAX_VARIANTS - 1} variants")
        checked = []
        for index, item in enumerate(variants):
            where = f"variants[{index}]"
            if not isinstance(item, dict) or set(item) - {"name", "set"} or not isinstance(item.get("set"), dict):
                raise ValueError(f'{where}: expected {{"name", "set": {{path: value}}}}')
            name = item.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > cli_planner.MAX_NAME
                                     or "\n" in name or "\0" in name):
                raise ValueError(f"{where}.name: a nonempty line of at most {cli_planner.MAX_NAME} characters")
            if not 1 <= len(item["set"]) <= cli_planner.MAX_PATHS:
                raise ValueError(f"{where}.set: 1..{cli_planner.MAX_PATHS} settable paths")
            sets = {}
            for path, value in item["set"].items():
                if not PATH.fullmatch(path):
                    raise ValueError(f"{where}.set: {path!r} is not a settable path")
                sets[path] = json.loads(_value_text(value, f"{where}.set.{path}"))
            checked.append({"name": name, "set": sets} if name is not None else {"set": sets})
        stdin = json.dumps({"variants": checked}).encode("utf-8")
        argv += ["--variants", "-"]
    total = 1 + (combos if vary else 0) + len(variants or [])
    if total > cli_planner.MAX_VARIANTS:
        raise ValueError(f"{total} variants (the base included); at most {cli_planner.MAX_VARIANTS}")
    tokens = _int(arguments, "prompt_tokens", 1, 1 << 24)
    if tokens is not None:
        argv += ["--prompt-tokens", str(tokens)]
    return argv + ["--config", str(api.config_path)], stdin


def placement_argv(api, arguments):
    from hyperloom.cliext import planner as cli_planner
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = set(arguments) - PLACEMENT_KEYS
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)))
    argv = ["placement", _name(arguments, "model", required=True), "--catalog-only"]
    if _name(arguments, "facts") is not None:
        argv += ["--facts", arguments["facts"]]
    for key, flag, low, high in (("prompt", "--prompt", 1, 1 << 20), ("output", "--output", 1, 65536),
                                 ("slots", "--slots", 1, 64), ("top_k", "--top-k", 1, 10)):
        value = _int(arguments, key, low, high)
        if value is not None:
            argv += [flag, str(value)]
    objective = arguments.get("objective")
    if objective is not None:
        if objective not in cli_planner.OBJECTIVES:
            raise ValueError("objective must be one of " + ", ".join(cli_planner.OBJECTIVES))
        argv += ["--objective", objective]
    return argv + ["--config", str(api.config_path)], None


def run(api, argv, stdin):
    """The command line through the portal's recipe runner (tests and the browser fixture replace this)."""
    from hyperloom.web import recipes as recipe_web
    from hyperloom.web.api import APIError
    try:
        return api.recipes.run(argv, stdin=stdin, deadline_s=DEADLINE_S)
    except recipe_web.Busy as exc:
        raise APIError(str(exc), 503) from exc


def answer(api, name, argv, outcome):
    """The planner document of a command that ran to its end: command line, exit code, JSON result, message. A command
    the runner stopped, or one that printed no JSON, is an HTTP error whose message names the reason and the command
    line (the portal's standard error envelope)."""
    from hyperloom.recipes import service
    from hyperloom.web.api import APIError
    cli = service.command_line([a for a in argv if a != "--catalog-only"])
    if outcome.why:
        reason = {"deadline": f"the {name} command did not finish in {DEADLINE_S} s (stopped: deadline)",
                  "output overflow": f"the {name} command printed more than allowed (stopped: output overflow)",
                  "client gone": f"the {name} command was cancelled (stopped: client gone)"}.get(
                      outcome.why, f"the {name} command was stopped ({outcome.why})")
        raise APIError(f"{reason} · CLI: {cli}", {"deadline": 504, "output overflow": 502}.get(outcome.why, 499))
    message = outcome.stderr.decode("utf-8", "replace").strip()
    error = "\n".join(line.removeprefix("hyperloom: ") for line in message.splitlines() if line.strip())[-16384:] or None
    if outcome.code == 2:
        raise ValueError(error or "invalid arguments")
    if outcome.code == 3:
        raise LookupError(error or "unknown recipe")
    if outcome.code not in (0, 1):
        raise APIError(f"the {name} command failed (exit {outcome.code}): {error or 'no message'} · CLI: {cli}", 502)
    text = outcome.stdout.decode("utf-8", "replace").strip()
    result = None
    try:
        result = json.loads(text) if text else None
    except ValueError:
        result = None
    # Exit 0 carries the command's JSON; exit 1 is a refusal: its JSON when it printed one, else its message only.
    if (outcome.code == 0 and not isinstance(result, dict)) or (text and not isinstance(result, dict)):
        raise APIError(f"the {name} command printed no JSON · CLI: {cli}", 502)
    if outcome.code == 1 and result is None and not error:
        raise APIError(f"the {name} command refused without a message · CLI: {cli}", 502)
    return api.document(f"planner-{name}", cli=cli, exit_code=outcome.code, ok=outcome.code == 0, result=result,
                        error=error)


def whatif(api, arguments):
    argv, stdin = whatif_argv(api, arguments)
    return answer(api, "whatif", argv, run(api, argv, stdin))


def placement(api, arguments):
    argv, stdin = placement_argv(api, arguments)
    return answer(api, "placement", argv, run(api, argv, stdin))
