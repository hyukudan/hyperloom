"""`hyperloom logsearch`: the portal's log search from the command line (same functions)."""

import json
import re
import time
from pathlib import Path


def _api(args):
    from hyperloom.cliext.ops import cli_api
    return cli_api(args)


def seconds(text):
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip())
    if not match:
        raise ValueError("a window like 30m, 2h or 7d")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def search(args):
    from hyperloom.web.features import logsearch
    api = _api(args)
    arguments = {"regex" if args.regex else "text": args.pattern}
    if args.ignore_case:
        arguments["ignore_case"] = True
    if args.machine:
        arguments["machines"] = args.machine
    if args.root:
        arguments["roots"] = args.root
    if args.since:
        arguments["window_s"] = seconds(args.since)
    for key in ("process", "level", "context", "max_results"):
        if getattr(args, key) is not None:
            arguments[key] = getattr(args, key)
    doc = logsearch.query(api, arguments)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0
    for row in doc["results"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["mtime"])) if row.get("mtime") else "?"
        for line in row.get("before") or []:
            print(f"  {line}")
        print(f"{row['machine']} {row['root']}/{row['file']}:{row['line']} (file modified {when})\n> {row['text']}")
        for line in row.get("after") or []:
            print(f"  {line}")
    for row in doc["machines"]:
        report = row.get("report") or {}
        state = row["error"] or (f"{report.get('files')} files, {report.get('bytes')} bytes, {report.get('results')} "
                                 "results" + (", stopped at " + ", ".join(report["truncated"]) if report.get("truncated") else ""))
        print(f"# {row['machine']}: {state}")
    return 0


def listing(args):
    from hyperloom.web.features import logsearch
    print(json.dumps(logsearch.document(_api(args), {}), indent=2, default=str))
    return 0


def roots(args):
    from hyperloom.web.features import logsearch
    api = _api(args)
    arguments = {"machine": args.machine, "roots": [] if args.clear else list(args.paths)}
    result = logsearch.save_roots(api, arguments)
    api.audit("logsearch/roots", arguments, {"state": "succeeded", "result": result})
    print(json.dumps(result, indent=2))
    return 0


def add(commands):
    from hyperloom.cli import config_path
    from hyperloom.cliext.ops import safe
    parser = commands.add_parser("logsearch", help="search hyperloom and configured lab logs on every machine (read-only)")
    sub = parser.add_subparsers(dest="logsearch_operation", required=True)
    query = sub.add_parser("query", help="search by literal text (or --regex)")
    query.add_argument("pattern")
    query.add_argument("--regex", action="store_true", help="a restricted regex (no backreference, lookaround or "
                                                             "quantified group; at most 128 characters)")
    query.add_argument("-i", "--ignore-case", action="store_true")
    query.add_argument("--machine", action="append", help="machine id (repeatable; default all)")
    query.add_argument("--root", action="append", help="root id from 'logsearch list' (repeatable; default all)")
    query.add_argument("--since", help="only files modified in this window, e.g. 30m, 2h, 7d")
    query.add_argument("--process", help="file names containing this text")
    query.add_argument("--level", choices=("any", "warning", "error"))
    query.add_argument("--context", type=int, choices=(0, 1, 2))
    query.add_argument("--max-results", type=int)
    query.add_argument("--json", action="store_true")
    query.add_argument("--config", type=Path, default=config_path())
    query.set_defaults(ext_run=safe(search))
    show = sub.add_parser("list", help="machines and their searchable roots")
    show.add_argument("--config", type=Path, default=config_path())
    show.set_defaults(ext_run=safe(listing))
    change = sub.add_parser("roots", help="set the lab roots searched on a machine")
    change.add_argument("machine")
    change.add_argument("paths", nargs="*")
    change.add_argument("--clear", action="store_true")
    change.add_argument("--config", type=Path, default=config_path())
    change.set_defaults(ext_run=safe(roots))
