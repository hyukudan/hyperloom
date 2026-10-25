"""`hyperloom telemetry ...`: the fleet collectors and the metrics history from the command line.

    hyperloom telemetry status                     series in the store and each machine's last collection
    hyperloom telemetry sample [--collector C] [--record]
                                                   one round of fleet-nodes / fleet-control (the collectors' own
                                                   functions); without --record nothing is written
    hyperloom telemetry query SERIES [--range S] [--start T] [--end T] [--tier raw|m1|m15] [--label k=v ...]
                                                   the same document as GET history/<series>
    hyperloom telemetry thresholds [--set JSON | --reset]
                                                   memory headroom thresholds (same function as POST fleet/thresholds/set)

The store is <config dir>/telemetry.sqlite3; status and query never create it. Node calls take the same per-machine
gate as the portal's collectors, so a manual sample never adds a second call in flight to a machine.
"""

import json
import sys


COLLECTORS = ("fleet-nodes", "fleet-control")


def _api(args):
    from hyperloom import cli
    from hyperloom.web.api import API
    return API(args.config or cli.config_path())


def _store_path(api):
    return api.config_path.parent / "telemetry.sqlite3"


def _existing(api):
    """Attach the existing store file (never create one); None when there is none."""
    from hyperloom.telemetry.store import Store
    path = _store_path(api)
    if not path.is_file():
        return None
    api.telemetry = Store(path, clock=api.clock)
    return api.telemetry


def _print(value):
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, default=str) + "\n")


def status(args):
    from hyperloom.web.features import machines
    api = _api(args)
    store = _existing(api)
    if store is None:
        _print({"store": None, "why": f"no telemetry store at {_store_path(api)} (the portal creates it)"})
        return 1
    now = api.clock()
    hosts = {}
    for key, ts, value in store.items(machines.HOSTS):
        interval = machines.CONTROL_INTERVAL if value.get("access") == "local" else machines.NODE_INTERVAL
        state, why = machines.status_of(value, now, interval)
        hosts[key] = {"status": state, "why": why, "last_ok_age_s": None if value.get("last_ok") is None
                      else round(now - value["last_ok"], 1), "error": value.get("error"),
                      "failures": value.get("failures"), "skipped": value.get("skipped")}
    _print({"store": str(_store_path(api)), "series": store.names(), "machines": hosts})
    return 0


def sample(args):
    from hyperloom import telemetry as tm
    from hyperloom.web.features import machines
    api = _api(args)
    if args.record:
        store = tm.open_store(api)
    else:
        store = _existing(api)          # previous documents are read; nothing is written
    now = api.clock()
    names = COLLECTORS if args.collector == "all" else (args.collector,)
    out = {}
    for name in names:
        round_ = machines.collect_nodes(api, now) if name == "fleet-nodes" else machines.collect_control(api, now)
        if args.record:
            points = machines.persist(api, store, round_)
            recorded = store.record(points, ts=now, interval=machines.NODE_INTERVAL if name == "fleet-nodes"
                                    else machines.CONTROL_INTERVAL)
        else:
            points, recorded = [p for r in round_["machines"] for p in r["gauges"]], 0
        out[name] = {"machines": {r["machine"]: ({"skipped": r["skipped"]} if "skipped" in r else
                                                 {"ok": r["doc"]["ok"], "error": r["doc"]["error"],
                                                  "counters": len(r["counters"]), "engines": [k for k, _ in r["engines"]]})
                                  for r in round_["machines"]},
                     "points": [{"series": n, "labels": l, "value": v} for n, l, v, *_ in points],
                     "recorded": recorded}
        if "why" in round_:
            out[name]["why"] = round_["why"]
    _print({"recorded": bool(args.record), "collectors": out})
    return 0


def query(args):
    from hyperloom.web.features import history
    api = _api(args)
    if _existing(api) is None:
        _print({"store": None, "why": f"no telemetry store at {_store_path(api)} (the portal creates it)"})
        return 1
    request = {}
    for key in ("range", "start", "end", "tier"):
        if getattr(args, key) is not None:
            request[key] = str(getattr(args, key))
    for item in args.label or []:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ValueError("--label takes k=v")
        request["l." + name] = value
    _print(history.series(api, args.series, request))
    return 0


def thresholds(args):
    from hyperloom.web.features import machines
    api = _api(args)
    if args.set is None and not args.reset:
        _existing(api)
        _print(machines.get_thresholds(api, {}))
        return 0
    arguments = {"reset": True} if args.reset else json.loads(args.set)
    try:
        result = machines.set_thresholds(api, arguments)
    except ValueError as exc:
        api.audit("fleet/thresholds/set", arguments, {"state": "refused", "error": str(exc)})
        raise
    api.audit("fleet/thresholds/set", arguments, {"state": "succeeded", "result": result})
    _print(result)
    return 0


def add(commands):
    parser = commands.add_parser("telemetry", help="fleet collectors and metrics history")
    sub = parser.add_subparsers(dest="telemetry_command", required=True)
    common = {"help": "cluster configuration (default: the user configuration)"}
    one = sub.add_parser("status", help="series and each machine's last collection")
    one.add_argument("--config", **common)
    one.set_defaults(ext_run=status)
    one = sub.add_parser("sample", help="run one collection round (dry run unless --record)")
    one.add_argument("--config", **common)
    one.add_argument("--collector", choices=(*COLLECTORS, "all"), default="all")
    one.add_argument("--record", action="store_true", help="write the round to the store like the portal does")
    one.set_defaults(ext_run=sample)
    one = sub.add_parser("query", help="history of one series (GET history/<series>)")
    one.add_argument("--config", **common)
    one.add_argument("series")
    one.add_argument("--range", type=float)
    one.add_argument("--start", type=float)
    one.add_argument("--end", type=float)
    one.add_argument("--tier", choices=("raw", "m1", "m15"))
    one.add_argument("--label", action="append", metavar="K=V")
    one.set_defaults(ext_run=query)
    one = sub.add_parser("thresholds", help="memory headroom thresholds (F2)")
    one.add_argument("--config", **common)
    group = one.add_mutually_exclusive_group()
    group.add_argument("--set", metavar="JSON", help='e.g. {"unified": {"amber_gib": 10}}')
    group.add_argument("--reset", action="store_true")
    one.set_defaults(ext_run=thresholds)
