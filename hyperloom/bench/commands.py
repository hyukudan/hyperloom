"""One description of every run kind's options, shared by the CLI parser, the portal's command preview and the
pages: ``arguments_from(kind, namespace)`` turns parsed CLI options into the same JSON arguments a portal POST sends,
and ``command_line(kind, arguments)`` renders the CLI command for portal arguments (F28's live preview)."""

import re

from . import ab, decode, demo, prefill, quality, tools


SLUGS = {"qa": "short QA", "arithmetic": "arithmetic chains", "state": "state tracking",
         "instructions": "instruction following", "recall": "long-context recall"}
NAMES = {v: k for k, v in SLUGS.items()}

# (flag, argument key, kind of value): int, ints (comma list), str, strs (repeatable), bool (switch), slugs
OPTIONS = {
    "decode": [("--prompt-set", "prompt_set", "str"), ("--levels", "levels", "ints"), ("--max-tokens", "max_tokens", "int")],
    "prefill": [("--sizes", "sizes", "ints"), ("--max-context", "max_context", "int"), ("--max-tokens", "max_tokens", "int"),
                ("--trials", "trials", "int")],
    "ab": [("--prompt-set", "prompt_set", "str"), ("--cases", "cases", "int"), ("--max-tokens", "max_tokens", "int"),
           ("--seed", "seed", "int")],
    "quality": [("--categories", "categories", "slugs"), ("--max-tokens", "max_tokens", "int"),
                ("--concurrency", "concurrency", "int")],
    "tools": [("--preset", "preset", "str"), ("--repeats", "repeats", "int"), ("--max-tokens", "max_tokens", "int")],
    "demo": [("--prompt-set", "prompt_set", "str"), ("--count", "count", "int"), ("--text", "texts", "strs"),
             ("--max-tokens", "max_tokens", "int")],
}
COMMON = [("--model", "model", "str"), ("--token-budget", "token_budget", "int"), ("--trace", "trace", "bool")]
SAMPLING = [("--temperature", "temperature", "float"), ("--top-p", "top_p", "float"), ("--sample-seed", "seed", "int")]
KINDS = {"decode": decode, "prefill": prefill, "ab": ab, "quality": quality, "tools": tools, "demo": demo}


def _dest(flag):
    return flag.lstrip("-").replace("-", "_")


def add_options(parser, kind):
    """argparse options of a kind (the CLI)."""
    if kind == "ab":
        for arm in ("a", "b"):
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument(f"--{arm}-profile", help=f"arm {arm.upper()}: a running profile")
            group.add_argument(f"--{arm}-target", help=f"arm {arm.upper()}: an allow-listed ad-hoc target id")
            parser.add_argument(f"--{arm}-tokenizer", help=f"arm {arm.upper()}: declared tokenizer id (when not reported)")
    else:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--profile", help="a running profile (its router)")
        group.add_argument("--target", help="an allow-listed ad-hoc target id")
        if kind != "prefill":
            parser.add_argument("--thinking", choices=("on", "off"), help="where the target declares a thinking switch")
        if kind in ("decode", "quality", "tools", "demo"):
            for flag, _, value in SAMPLING:
                parser.add_argument(flag, type=float if value == "float" else int,
                                    help="sampling, only on a target that declares it honours sampling")
    for flag, key, value in COMMON + OPTIONS[kind]:
        if kind == "ab" and key == "model":
            continue
        if value == "bool":
            parser.add_argument(flag, action="store_true")
        elif value == "strs":
            parser.add_argument(flag, action="append")
        else:
            parser.add_argument(flag, type={"int": int, "float": float}.get(value, str))


def _target(profile, target):
    return {"profile": profile} if profile else {"target": target}


def arguments_from(kind, namespace):
    """The portal arguments of a parsed CLI command (validation happens in the kind's own validate)."""
    get = lambda name: getattr(namespace, name, None)
    arguments = {}
    if kind == "ab":
        arguments["arms"] = {arm: _target(get(f"{arm}_profile"), get(f"{arm}_target")) for arm in ("a", "b")}
        declared = {arm: get(f"{arm}_tokenizer") for arm in ("a", "b") if get(f"{arm}_tokenizer")}
        if declared:
            arguments["tokenizers"] = declared
    else:
        arguments["target"] = _target(get("profile"), get("target"))
        if get("thinking"):
            arguments["thinking"] = get("thinking") == "on"
        sampling = {key: get(_dest(flag)) for flag, key, _ in SAMPLING if get(_dest(flag)) is not None}
        if sampling:
            arguments["sampling"] = sampling
    for flag, key, value in COMMON + OPTIONS[kind]:
        item = get(_dest(flag))
        if item is None or item is False:
            continue
        if value == "ints":
            try:
                item = [int(v) for v in str(item).split(",") if v.strip()]
            except ValueError:
                raise ValueError(f"{flag} takes comma-separated integers") from None
        elif value == "slugs":
            names = [v.strip() for v in str(item).split(",") if v.strip()]
            unknown = [n for n in names if n not in SLUGS]
            if unknown:
                raise ValueError(f"{flag}: unknown category {', '.join(unknown)} (use {', '.join(SLUGS)})")
            item = [SLUGS[n] for n in names]
        arguments[key] = item
    return arguments


def _quote(value):
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z0-9._,:/=+-]+", text) else '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def command_line(kind, arguments):
    """The CLI command equivalent to portal ``arguments`` (no validation; unknown keys are ignored)."""
    if kind not in OPTIONS or not isinstance(arguments, dict):
        raise ValueError("unknown bench kind")
    parts = ["hyperloom", "bench", kind]

    def target(prefix, value):
        if isinstance(value, dict) and value.get("profile"):
            parts.extend([f"--{prefix}profile", _quote(value["profile"])])
        elif isinstance(value, dict) and value.get("target"):
            parts.extend([f"--{prefix}target", _quote(value["target"])])
    if kind == "ab":
        for arm in ("a", "b"):
            target(f"{arm}-", (arguments.get("arms") or {}).get(arm))
            tokenizer = (arguments.get("tokenizers") or {}).get(arm)
            if tokenizer:
                parts.extend([f"--{arm}-tokenizer", _quote(tokenizer)])
    else:
        target("", arguments.get("target"))
        if arguments.get("thinking") is not None:
            parts.extend(["--thinking", "on" if arguments["thinking"] else "off"])
        for flag, key, _ in SAMPLING:
            value = (arguments.get("sampling") or {}).get(key)
            if value is not None:
                parts.extend([flag, _quote(value)])
    for flag, key, value in COMMON + OPTIONS[kind]:
        item = arguments.get(key)
        if item is None or item is False:
            continue
        if value == "bool":
            parts.append(flag)
        elif value == "ints":
            parts.extend([flag, ",".join(str(v) for v in item)])
        elif value == "slugs":
            parts.extend([flag, ",".join(NAMES.get(v, v) for v in item)])
        elif value == "strs":
            for text in item:
                parts.extend([flag, _quote(text)])
        else:
            parts.extend([flag, _quote(item)])
    return " ".join(parts)
