"""``hyperloom health``: findings, rules and the activity log, through the same functions as the portal.

    hyperloom health [--json]                         active and pending findings (the portal's health document)
    hyperloom health rules [--json]                   rules, parameters and availability
    hyperloom health set-rule RULE [--enable|--disable] [--hold S] [--clear S] [--param NAME=VALUE ...] [--reset]
    hyperloom health events [--category C] [--machine M] [--severity S] [--before ID] [--limit N] [--json]
    hyperloom health clear-events --older-than 24h|7d|all --yes

Findings are evaluated by the portal; these commands read and change the state it keeps (the telemetry store next to
the configuration). Rule changes and clears are written to the portal's audit log.
"""

import argparse
import json
import math
from pathlib import Path
import sys
import time

from hyperloom import cli


class Shim:
    """The part of the portal API the health, alerts and events functions use, over the configuration's store."""

    def __init__(self, config, store):
        self.config_path, self.telemetry, self.clock = Path(config), store, time.time
        self._api = None

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", "server_time": self.clock(), "why": {}, **fields}

    def audit(self, action, arguments, result):
        if self._api is None:
            from hyperloom.web.api import API
            self._api = API(self.config_path, audit_path=self.config_path.parent / "audit.jsonl")
        self._api.audit(action, arguments, result)


def open_shim(args, create=False):
    """(Shim, None) or (None, exit code) when there is no store yet and the command only reads."""
    from hyperloom.telemetry.store import Store
    path = Path(args.config).parent / "telemetry.sqlite3"
    if not create and not path.exists():
        print("hyperloom: no health data yet: the portal (hyperloom ui) has not run with this configuration",
              file=sys.stderr)
        return None, 1
    store = Store(path)
    getattr(args, "_hyperloom_opened", []).append(store)
    return Shim(args.config, store), None


def closing_store(run):
    """Run a command and close any store it opened (an open SQLite file cannot be removed on Windows)."""
    def wrapped(args):
        opened = []
        args._hyperloom_opened = opened
        try:
            return run(args)
        finally:
            for store in opened:
                store.close()
    wrapped.__name__ = run.__name__
    return wrapped


def refuse(exc):
    print("hyperloom: " + str(exc).strip("'\""), file=sys.stderr)
    return 1


def _age(now, ts):
    if not isinstance(ts, (int, float)):
        return "-"
    seconds = max(0, int(now - ts))
    return f"{seconds // 3600} h {seconds % 3600 // 60} min" if seconds >= 3600 else f"{seconds // 60} min {seconds % 60} s"


def findings(args):
    from hyperloom.web.features import health
    shim, code = open_shim(args)
    if shim is None:
        return code
    document = health.findings_document(shim, {})
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    now = document["server_time"]
    rows = document["findings"] or []
    if document.get("evaluated_at") is None:
        print("no evaluation has run yet")
    elif document.get("stale"):
        print(f"evaluation stale: last run {_age(now, document['evaluated_at'])} ago")
    if not rows:
        print("no active findings")
    for finding in rows:
        print(f"{finding['severity']:<8} {finding['title']} · {finding.get('detail') or ''} · for {_age(now, finding.get('since'))}"
              + (" · clearing" if finding.get("state") == "clearing" else ""))
    for finding in document["pending"] or []:
        print(f"pending  {finding['title']} · {finding.get('detail') or ''}")
    return 0


def rules(args):
    from hyperloom.web.features import health
    shim, code = open_shim(args)
    if shim is None:
        return code
    document = health.rules_document(shim, {})
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    for rule in document["rules"] or []:
        params = ", ".join(f"{k}={v:g}" for k, v in sorted(rule["params"].items()))
        state = "disabled" if not rule["enabled"] else f"{rule['active']} active, {rule['pending']} pending"
        print(f"{rule['id']:<22} hold {rule['hold_s']:g} s · clear {rule['clear_s']:g} s · {state}"
              + (f" · {params}" if params else "") + (f" · {rule['why']}" if rule.get("why") else ""))
    return 0


def _param(text):
    name, sep, value = text.partition("=")
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not sep or not name or not math.isfinite(number):
        raise argparse.ArgumentTypeError("expected NAME=NUMBER")
    return name, number


def set_rule(args):
    from hyperloom.web.features import health
    shim, _ = open_shim(args, create=True)
    arguments = {"rule": args.rule}
    if args.reset:
        arguments["reset"] = True
    if args.enabled is not None:
        arguments["enabled"] = args.enabled
    if args.hold is not None:
        arguments["hold_s"] = args.hold
    if args.clear is not None:
        arguments["clear_s"] = args.clear
    if args.param:
        arguments["params"] = dict(args.param)
    try:
        result = health.set_rule_post(shim, arguments)
    except (ValueError, LookupError) as exc:
        return refuse(exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def events(args):
    from hyperloom.web.features import events as feature
    shim, code = open_shim(args)
    if shim is None:
        return code
    query = {key: str(value) for key, value in (("category", args.category), ("machine", args.machine),
                                                 ("severity", args.severity), ("before", args.before),
                                                 ("limit", args.limit)) if value is not None}
    try:
        document = feature.document(shim, query)
    except ValueError as exc:
        return refuse(exc)
    if args.json:
        document.pop("clear", None)          # confirmation codes are for the portal page only
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    for event in document["events"] or []:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        print(f"{event['id']:>6} {when} {event['severity']:<8} {event['category']:<8} "
              + (f"{event['machine']} · " if event.get("machine") else "") + event["message"])
    if document.get("next_before"):
        print(f"more: --before {document['next_before']}")
    return 0


def clear_events(args):
    from hyperloom.web.features import events as feature
    if not args.yes:
        return refuse(ValueError("clearing the activity log needs --yes"))
    shim, code = open_shim(args)
    if shim is None:
        return code
    try:
        removed = feature.clear(shim.telemetry, args.older_than, shim.clock())
    except ValueError as exc:
        shim.audit("events/clear", {"older_than": "[invalid]"}, {"state": "refused", "error": str(exc)})
        return refuse(exc)
    result = {"older_than": args.older_than, "removed": removed}
    shim.audit("events/clear", {"older_than": args.older_than}, {"state": "succeeded", "result": result})
    print(json.dumps(result, sort_keys=True))
    return 0


def add(commands):
    parser = commands.add_parser("health", help="health findings, rules and the activity log")
    parser.add_argument("--config", type=Path, default=cli.config_path())
    parser.add_argument("--json", action="store_true", help="print the document as JSON")
    parser.set_defaults(ext_run=closing_store(findings))
    sub = parser.add_subparsers(dest="health_command")

    def command(name, run, text):
        child = sub.add_parser(name, help=text)
        child.add_argument("--config", type=Path, default=cli.config_path())
        child.set_defaults(ext_run=closing_store(run))
        return child

    command("rules", rules, "rules, parameters and availability").add_argument("--json", action="store_true")
    change = command("set-rule", set_rule, "enable/disable a rule or change its holds and parameters")
    change.add_argument("rule")
    toggle = change.add_mutually_exclusive_group()
    toggle.add_argument("--enable", dest="enabled", action="store_const", const=True)
    toggle.add_argument("--disable", dest="enabled", action="store_const", const=False)
    change.add_argument("--hold", type=float, help="seconds the condition must hold before the finding fires")
    change.add_argument("--clear", type=float, help="seconds the condition must be absent before it clears")
    change.add_argument("--param", type=_param, action="append", metavar="NAME=VALUE")
    change.add_argument("--reset", action="store_true", help="back to the defaults")
    log = command("events", events, "the activity log, newest first")
    log.add_argument("--category")
    log.add_argument("--machine")
    log.add_argument("--severity", choices=("info", "warning", "critical"))
    log.add_argument("--before", type=int)
    log.add_argument("--limit", type=int)
    log.add_argument("--json", action="store_true")
    wipe = command("clear-events", clear_events, "clear the activity log")
    wipe.add_argument("--older-than", required=True, choices=("24h", "7d", "all"))
    wipe.add_argument("--yes", action="store_true", help="confirm the clear")
