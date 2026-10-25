"""``hyperloom alerts``: alert channels, through the same functions as the portal.

    hyperloom alerts [list] [--json]
    hyperloom alerts add --name NAME --kind webhook|push [--min-severity S] [--reminders R] --url-stdin
    hyperloom alerts edit ID [--name NAME] [--min-severity S] [--reminders R] [--url-stdin]
    hyperloom alerts remove ID --yes
    hyperloom alerts test ID

The channel URL is read from standard input (one line), so it never appears in the command line or the shell
history; it is kept in the secret directory and shown masked. Every change is written to the portal's audit log
with the URL masked.
"""

import argparse
import json
from pathlib import Path
import sys

from hyperloom import cli
from hyperloom.cliext.health import closing_store, open_shim, refuse


def _url(args):
    if not getattr(args, "url_stdin", False):
        return None
    line = sys.stdin.readline().strip()
    if not line:
        raise ValueError("no URL on standard input")
    return line


def listing(args):
    from hyperloom.web.features import alerts
    shim, code = open_shim(args)
    if shim is None:
        return code
    document = alerts.document(shim, {})
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    rows = document["channels"] or []
    if not rows:
        print("no alert channels")
    for channel in rows:
        print(f"{channel['id']}  {channel['name']} · {channel['kind']} · {channel['url_masked']} · "
              f"from {channel['min_severity']} · reminders {channel['reminders']}")
    print(f"outbox: {document['outbox']['length']} message(s)")
    return 0


def save(args, creating):
    from hyperloom.web.features import alerts
    shim, _ = open_shim(args, create=True)
    try:
        arguments = {key: value for key, value in (("id", None if creating else args.id), ("name", args.name),
                                                     ("kind", getattr(args, "kind", None)),
                                                     ("min_severity", args.min_severity), ("reminders", args.reminders),
                                                     ("url", _url(args))) if value is not None}
        result = alerts.channels_post(shim, arguments)
    except (ValueError, LookupError, PermissionError) as exc:
        return refuse(exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def remove(args):
    from hyperloom.web.features import alerts
    if not args.yes:
        return refuse(ValueError("removing a channel needs --yes"))
    shim, code = open_shim(args)
    if shim is None:
        return code
    try:
        result = alerts.delete_post(shim, {"id": args.id})
    except (ValueError, LookupError) as exc:
        return refuse(exc)
    print(json.dumps(result, sort_keys=True))
    return 0


def test(args):
    from hyperloom.web.features import alerts
    shim, code = open_shim(args)
    if shim is None:
        return code
    try:
        result = alerts.test_post(shim, {"id": args.id})
    except (ValueError, LookupError, PermissionError) as exc:
        return refuse(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


def add(commands):
    parser = commands.add_parser("alerts", help="alert channels (webhooks and push topics)")
    parser.add_argument("--config", type=Path, default=cli.config_path())
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(ext_run=closing_store(listing))
    sub = parser.add_subparsers(dest="alerts_command")

    def command(name, run, text):
        child = sub.add_parser(name, help=text)
        child.add_argument("--config", type=Path, default=argparse.SUPPRESS)      # keeps the parent's value
        child.set_defaults(ext_run=closing_store(run))
        return child

    command("list", listing, "the channels (URLs masked)").add_argument("--json", action="store_true")
    for name, creating in (("add", True), ("edit", False)):
        child = command(name, lambda args, creating=creating: save(args, creating),
                        "add a channel" if creating else "change a channel")
        if creating:
            child.add_argument("--name", required=True)
            child.add_argument("--kind", required=True, choices=("webhook", "push"))
        else:
            child.add_argument("id")
            child.add_argument("--name")
        child.add_argument("--min-severity", choices=("info", "warning", "critical"))
        child.add_argument("--reminders", choices=("none", "1h", "4h", "12h", "24h"))
        child.add_argument("--url-stdin", action="store_true", required=creating,
                           help="read the channel URL from standard input (one line)")
    gone = command("remove", remove, "remove a channel")
    gone.add_argument("id")
    gone.add_argument("--yes", action="store_true")
    command("test", test, "send a test message now").add_argument("id")
