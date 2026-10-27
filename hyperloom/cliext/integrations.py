"""CLI for integrations: the same functions the portal calls.

hyperloom integrations status                      the integrations document (JSON)
hyperloom integrations mqtt-save [--enable|--disable] [--prefix P]   (the broker: pc.mqtt_broker)
                                 [--username U | --no-username] [--client-id C] [--interval S]
                                 [--password-stdin | --clear-password]
hyperloom integrations mqtt-test                   connect and publish <prefix>/portal/test once
hyperloom integrations export <document> [--share] [--arg key=value ...] [-o FILE]
"""

import json
from pathlib import Path
import sys


def _api(args):
    from hyperloom.web.api import API
    config = Path(args.config)
    return API(config, audit_path=config.parent / "audit.jsonl")     # the audit log next to that configuration


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _status(args):
    from hyperloom.web.api import redact
    from hyperloom.web.features import integrations
    api = _api(args)
    from hyperloom import telemetry
    if (api.config_path.parent / "telemetry.sqlite3").is_file():
        telemetry.open_store(api)           # the CLI reads the portal's settings when the store exists
    _print(redact(integrations.state(api, {})))
    return 0


def _save(args):
    from hyperloom.web.features import integrations
    arguments = {}
    if args.enabled is not None:
        arguments["enabled"] = args.enabled
    for key, value in (("topic_prefix", args.prefix), ("client_id", args.client_id), ("interval_s", args.interval)):
        if value is not None:
            arguments[key] = value
    if args.no_username:
        arguments["username"] = None
    elif args.username is not None:
        arguments["username"] = args.username
    if args.password_stdin:
        arguments["password"] = sys.stdin.readline().rstrip("\r\n")
    if args.clear_password:
        arguments["clear_password"] = True
    try:
        _print(integrations.save_audited(_api(args), arguments))
    except ValueError as exc:
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    return 0


def _test(args):
    from hyperloom.web.features import integrations
    try:
        result = integrations.test_publish(_api(args), {})
    except (ValueError, PermissionError) as exc:
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    _print(result)
    return 0 if result["ok"] else 1


def _export(args):
    from hyperloom.web.api import APIError
    query = {}
    for item in args.arg or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print("hyperloom: --arg takes key=value", file=sys.stderr)
            return 2
        query[key] = value
    if args.share:
        query["share"] = "1"
    api = _api(args)
    from hyperloom import telemetry
    if (api.config_path.parent / "telemetry.sqlite3").is_file():
        telemetry.open_store(api)
    try:
        document = api.read("integrations/export/" + args.document + ".csv", query)
    except APIError as exc:
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    if args.output:
        Path(args.output).write_text(document["csv"], encoding="utf-8", newline="")
        print(f"{document['rows']} rows -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(document["csv"])
    return 0


def add(commands):
    from hyperloom.cli import config_path
    parser = commands.add_parser("integrations", help="live stream, MQTT publishing and CSV export")
    sub = parser.add_subparsers(dest="integrations_command", required=True)
    status = sub.add_parser("status", help="state of the exporter, stream, MQTT and export targets")
    status.set_defaults(ext_run=_status)
    save = sub.add_parser("mqtt-save", help="save the MQTT broker settings (audited)")
    enabled = save.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_const", const=True)
    enabled.add_argument("--disable", dest="enabled", action="store_const", const=False)
    save.add_argument("--prefix", help="topic prefix")
    user = save.add_mutually_exclusive_group()
    user.add_argument("--username")
    user.add_argument("--no-username", action="store_true")
    save.add_argument("--client-id")
    save.add_argument("--interval", type=int, help="metrics interval in seconds (5..300)")
    password = save.add_mutually_exclusive_group()
    password.add_argument("--password-stdin", action="store_true", help="read the broker password from stdin")
    password.add_argument("--clear-password", action="store_true")
    save.set_defaults(ext_run=_save, enabled=None, tls=None)
    test = sub.add_parser("mqtt-test", help="connect with the saved settings and publish one test message")
    test.set_defaults(ext_run=_test)
    export = sub.add_parser("export", help="CSV of an exportable document (see status for the targets)")
    export.add_argument("document", help="e.g. devices, stats, ledger, energy, bench/runs, history/<series>")
    export.add_argument("--share", action="store_true", help="only the fields cleared for sharing")
    export.add_argument("--arg", action="append", metavar="KEY=VALUE", help="argument of the document (e.g. range=86400)")
    export.add_argument("-o", "--output", help="write the CSV to this file instead of stdout")
    export.set_defaults(ext_run=_export)
    for command in (status, save, test, export):
        command.add_argument("--config", type=Path, default=config_path())
