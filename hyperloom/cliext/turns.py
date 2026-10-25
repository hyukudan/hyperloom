"""`hyperloom turns`: the GPU turn queue of every lab node and the big-job ledger (the portal's `turns` document,
from the same functions); `hyperloom turns settings`: its preferences."""

import json
import time
from pathlib import Path


def _api(args):
    from hyperloom.cliext.ops import cli_api
    return cli_api(args)


def _clock(value, now):
    return time.strftime("%H:%M:%S", time.localtime(value)) if isinstance(value, (int, float)) else "?"


def _duration(value):
    if not isinstance(value, (int, float)):
        return "unavailable"
    value = int(value)
    return f"{value // 60}:{value % 60:02d}" if value >= 0 else "-" + _duration(-value)


def show(args):
    from hyperloom.web.features import turns
    api = _api(args)
    doc = turns.compose(api, turns.fetch(api))
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0
    for node in doc["nodes"]:
        print(f"{node['machine']}: {node['state']}" + (f" ({node['why'].get('state')})" if node["why"].get("state") else ""))
        holder = node.get("holder")
        if holder and holder.get("parsed"):
            left = "past its cap" if holder["past_cap"] else _duration(holder["remaining_s"]) + " left"
            print(f"  holder: {holder['label']} ({holder['kind']}, {holder['side']}) since {holder['since_clock']}, "
                  f"cap {holder['cap_s']} s ({holder['cap_source']}), {left}"
                  + (f" · {holder['pair_state']}" if holder.get("pair_state") else ""))
        elif node["state"] == "ok":
            print("  holder: none (" + node["why"].get("holder", "free") + ")")
        for row in node.get("waiting") or []:
            estimate = _duration(row["estimated_start_s"]) if row.get("estimated_start_s") is not None else "unavailable"
            print(f"  {row['position']:>2}. {row['label'] or '?'} waited {_duration(row['waited_s'])}, "
                  f"estimated start in {estimate} (estimate)")
        if node.get("unverified"):
            print(f"  {len(node['unverified'])} waiting ticket(s) unverified")
        for turn in (node.get("recent") or [])[:args.recent]:
            print(f"  {turn['start_clock']} {turn['label']} {turn['kind']} {_duration(turn['duration_s'])} {turn['outcome']}")
    if doc.get("pair_queue"):
        print("pair queue:")
        for row in doc["pair_queue"]:
            print(f"  {row['position']:>2}. {row['kind']} {row.get('label') or row.get('label_why')} waited {_duration(row['waited_s'])}")
    ledger = doc["ledger"]
    if ledger.get("state") is None:
        print("big-job ledger: unavailable · " + ledger.get("why", ""))
    else:
        print("big-job ledger: " + ledger["state"])
        for row in ledger["holders"] + ledger["stale"]:
            print(f"  {row['owner']} since {row['clock']} ({_duration(row['elapsed_s'])}, cap {row['cap_s']} s"
                  + (", STALE" if row in ledger["stale"] else "") + ")")
    return 0


def settings(args):
    from hyperloom.web.features import turns
    api = _api(args)
    arguments = {}
    if args.clear_ledger_path:
        arguments["ledger_path"] = None
    elif args.ledger_path is not None:
        arguments["ledger_path"] = str(Path(args.ledger_path).absolute())
    for key in ("ledger_cap_s", "pair_cap_s"):
        if getattr(args, key) is not None:
            arguments[key] = getattr(args, key)
    result = turns.save_settings(api, arguments) if arguments else {"settings": turns._settings(api)}
    if arguments:
        api.audit("turns/settings", arguments, {"state": "succeeded", "result": result})
    print(json.dumps(result, indent=2))
    return 0


def add(commands):
    from hyperloom.cli import config_path
    from hyperloom.cliext.ops import safe
    parser = commands.add_parser("turns", help="the GPU turn queue of the lab nodes and the big-job ledger (read-only)")
    parser.add_argument("--config", type=Path, default=config_path())
    parser.add_argument("--json", action="store_true", help="the portal's turns document")
    parser.add_argument("--recent", type=int, default=5, help="recent turns to list per node")
    parser.set_defaults(ext_run=safe(show))
    sub = parser.add_subparsers(dest="turns_operation")
    change = sub.add_parser("settings", help="the ledger path and caps")
    change.add_argument("--config", type=Path, default=config_path())
    group = change.add_mutually_exclusive_group()
    group.add_argument("--ledger-path", help="the coordination log with the big-job lines (read on this machine)")
    group.add_argument("--clear-ledger-path", action="store_true")
    change.add_argument("--ledger-cap-s", type=int, help="cap of a big job whose line states none (60..7200)")
    change.add_argument("--pair-cap-s", type=int, help="the lab rule's cap for pair turns (60..7200)")
    change.set_defaults(ext_run=safe(settings))
