"""`hyperloom endpoints ...`: other chat-completions-compatible endpoints and the exposure check (F17, F40).

The same functions as the portal feature (hyperloom/web/features/endpoints.py): `list`, `add`, `remove` edit the
allow-list (audited like the portal's POSTs), `check` runs a forced identification round and `exposure` an
exposure round in this process. Output passes through the portal's redaction.
"""

import json
import sys


def _api():
    from hyperloom import cli
    from hyperloom.web.api import API
    return API(cli.config_path())


def _print(value, as_json):
    from hyperloom.web.api import redact
    value = redact(value)
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    for line in value:
        print(line)


def _list(args):
    from hyperloom import telemetry
    from hyperloom.web.features import endpoints
    api = _api()
    telemetry.open_store(api)
    document = endpoints.index(api, {})
    rows = document["targets"] or []
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        print("no endpoint targets (add one with: hyperloom endpoints add --label L --host H --port N)")
        return 0
    lines = []
    for row in rows:
        found = row.get("probe") or {}
        state = "not probed yet" if not found else ("up" if found.get("up") else "down: " + str(found.get("error")))
        lines.append(f"{row['id']}\t{row['scheme']}://{row['host']}:{row['port']}\t{found.get('kind') or '-'}\t{state}\t{row['label']}")
    _print(lines, False)
    return 0


def _audited(action, arguments, function):
    from hyperloom.web.features import endpoints
    api = _api()
    try:
        result = function(api, arguments)
    except (ValueError, LookupError, PermissionError, RuntimeError) as exc:
        api.audit(action, arguments, {"state": "refused", "error": str(exc), "via": "cli"})
        print("hyperloom: " + str(exc).strip("'\""), file=sys.stderr)
        return 2
    api.audit(action, arguments, {"state": "succeeded", "result": result, "via": "cli"})
    return result


def _add(args):
    from hyperloom.web.features import endpoints
    arguments = {"label": args.label, "scheme": args.scheme, "host": args.host, "port": args.port}
    if args.id:
        arguments["id"] = args.id
    if args.key_name:
        arguments["key_name"] = args.key_name
    result = _audited("endpoints/save", arguments, endpoints.save)
    if isinstance(result, int):
        return result
    target = result["target"]
    print(f"saved {target['id']}: {target['scheme']}://{target['host']}:{target['port']}")
    return 0


def _remove(args):
    from hyperloom.web.features import endpoints
    result = _audited("endpoints/remove", {"id": args.id}, endpoints.remove)
    if isinstance(result, int):
        return result
    print("removed " + result["removed"])
    return 0


def _check(args):
    from hyperloom.web.features import endpoints
    api = _api()
    try:
        results = endpoints.run_round(api, api.clock(), identify=True)
    except PermissionError as exc:
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    if args.json:
        _print(results, True)
        return 0
    lines = [f"{tid}\t{r['kind']}\t{'up' if r['up'] else 'down: ' + str(r['error'])}\t"
             f"models {r['models_total'] if r['models_total'] is not None else 'unavailable'}" for tid, r in sorted(results.items())]
    _print(lines or ["no endpoint targets"], False)
    return 0


def _exposure(args):
    from hyperloom.web.features import endpoints
    api = _api()
    try:
        document = endpoints.exposure_round(api, api.clock())
    except PermissionError as exc:
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    red = 1 if (document.get("summary") or {}).get("red") else 0
    if args.json:
        _print(document, True)
        return red
    rows = document.get("rows")
    if rows is None:
        print("exposure unavailable: " + str((document.get("why") or {}).get("rows")))
        return 1
    _print([f"{row['verdict'].upper():8}{row['id']}\t{row.get('probe_address')}:{row.get('port')}\t{row['reason']}"
            for row in rows] or ["no endpoints configured"], False)
    return red


def add(commands):
    parser = commands.add_parser("endpoints", help="other chat-completions-compatible endpoints and the exposure check")
    sub = parser.add_subparsers(dest="endpoints_command", required=True)
    listing = sub.add_parser("list", help="allow-listed endpoint targets with their latest probe")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(ext_run=_list)
    adding = sub.add_parser("add", help="add or update an endpoint target")
    adding.add_argument("--label", required=True)
    adding.add_argument("--host", required=True, help="private or loopback IP, or a configured machine id")
    adding.add_argument("--port", required=True, type=int)
    adding.add_argument("--scheme", choices=("http", "https"), default="http")
    adding.add_argument("--id", help="update this target (default: a new id from the label)")
    adding.add_argument("--key-name", help="name of a hyperloom secret sent as the bearer credential")
    adding.set_defaults(ext_run=_add)
    removing = sub.add_parser("remove", help="remove an endpoint target")
    removing.add_argument("id")
    removing.set_defaults(ext_run=_remove)
    checking = sub.add_parser("check", help="identify and probe every target now")
    checking.add_argument("--json", action="store_true")
    checking.set_defaults(ext_run=_check)
    exposing = sub.add_parser("exposure", help="run the exposure check now (exit 1 when any endpoint is red)")
    exposing.add_argument("--json", action="store_true")
    exposing.set_defaults(ext_run=_exposure)
