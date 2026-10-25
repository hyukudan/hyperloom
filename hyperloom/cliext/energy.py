"""`hyperloom energy` and `hyperloom thermals`: the same functions as the portal's energy feature.

    hyperloom energy [show] [--range 24h|7d|30d] [--json]   energy estimate (from the stored quarter-hour rows)
    hyperloom energy compute                                 recompute what is not yet archived (the collector run)
    hyperloom energy price [--set P --currency EUR | --clear]
    hyperloom energy thresholds [--gpu WARN CRIT] [--cpu WARN CRIT|--no-cpu] [--sensor WARN CRIT|--no-sensor]
    hyperloom energy archive [--json]                        every archived month
    hyperloom energy reset --yes                             archive, then clear the energy rows (audited)
    hyperloom thermals [--range day|week] [--machine ID] [--json]
"""

import argparse
import json
import sys


def _api():
    global _zone
    from hyperloom import cli, telemetry
    from hyperloom.web.api import API
    from hyperloom.web.features import energy
    api = API(cli.config_path())
    _zone = energy.zone_of(telemetry.open_store(api))
    return api


def _post(api, action, arguments):
    """A write through the portal's own dispatcher: the same handler, validation, redaction and one audit record."""
    _status, document = api.feature_post(action, arguments)
    return document


def _wh(value):
    if value is None:
        return "unavailable"
    return f"{value / 1000:.2f} kWh" if abs(value) >= 1000 else f"{value:.1f} Wh"


def _pct(value):
    return "unavailable" if value is None else f"{value * 100:.0f} %"


def _show(args):
    from hyperloom.web.features import energy
    api = _api()
    document = energy.energy_document(api, {"range": args.range})
    if args.json:
        print(json.dumps(document, indent=2, allow_nan=False))
        return 0
    print(document["banner"] + ".")
    if document.get("machines") is None:
        print("unavailable · " + document["why"].get("machines", "no data"))
        return 0
    totals = document.get("totals")
    if totals:
        peak = totals.get("peak")
        print(f"Range {document['range']}: {_wh(totals['wh'])} integrated, coverage {_pct(totals['coverage'])}, "
              f"average {totals['avg_w']:.0f} W" if totals.get("avg_w") is not None else
              f"Range {document['range']}: {_wh(totals['wh'])} integrated, coverage {_pct(totals['coverage'])}")
        if totals.get("partial"):
            print("Partial: " + document["why"].get("totals", ", ".join(totals["partial"])))
        if peak:
            print(f"Peak hour starts {peak.get('label') or _when(peak['start'])}: {_wh(peak['wh'])}")
    else:
        print("Totals unavailable · " + document["why"].get("totals", ""))
    cost = document.get("cost")
    print(f"Estimated cost: {cost['value']:.2f} {cost['currency']} at {cost['price_per_kwh']} per kWh ({cost['basis']})" if cost else
          "Estimated cost unavailable · " + document["why"].get("cost", ""))
    efficiency = document.get("efficiency")
    print(f"Energy per 1k generated tokens: {efficiency['wh_per_1k_tokens']:.2f} Wh over "
          f"{efficiency['both_s'] / 3600:.2f} h where both were observed" if efficiency else
          "Energy per 1k generated tokens unavailable · " + document["why"].get("efficiency", ""))
    for machine in document["machines"]:
        note = machine.get("why", {}).get("wh")
        print(f"\n{machine['id']}: {_wh(machine['wh'])}, coverage {_pct(machine['coverage'])}" + (f" ({note})" if note else ""))
        for domain in machine["domains"]:
            state = "added" if domain["counted"] else "not added: " + (domain["overlap"] or "")
            print(f"  {domain['label']} ({domain['source']}): {_wh(domain['wh'])}, coverage {_pct(domain['coverage'])}, "
                  f"{state}")
        print("  not covered: " + ", ".join(machine["uncovered"]))
    if document.get("archive"):
        print("\nMonthly archive:")
        for month in document["archive"]:
            cost = f"{month['cost']:.2f} {month['currency']}" if month.get("cost") is not None else \
                "cost unavailable · " + (month.get("cost_why") or "")
            print(f"  {month['month']}{' (open)' if month['open'] else ''}: {_wh(month['wh'])}, {cost}")
    return 0


_zone = None


def _when(ts):
    """A time in the configured zone (prefs/ui timezone, else the control machine's)."""
    from hyperloom.web.features import energy
    zone = _zone or energy.Zone()
    return zone.local(ts).strftime("%Y-%m-%d %H:%M ") + zone.name


def _compute(args):
    from hyperloom import telemetry
    from hyperloom.web.features import energy
    api = _api()
    summary = energy.compute(telemetry.open_store(api), api.clock())
    print(json.dumps(summary, indent=2) if args.json else
          f"Recomputed {summary['quarters']} quarter hours from {_when(summary['from'])}; archived up to "
          f"{_when(summary['final_through'])}{'' if summary['applied'] else ' (another run was faster; nothing changed)'}.")
    return 0


def _price(args):
    from hyperloom.web.features import energy
    api = _api()
    if args.clear or args.set is not None:
        if args.clear and args.set is not None:
            raise ValueError("--set and --clear exclude each other")
        arguments = {"clear": True} if args.clear else {"price_per_kwh": args.set, "currency": args.currency}
        _post(api, "energy/price", arguments)
    price = energy.price_of(api.telemetry)
    print(f"Energy price: {price['price_per_kwh']} {price['currency']} per kWh" if price else "No energy price configured.")
    return 0


def _thresholds(args):
    from hyperloom.web.features import energy
    api = _api()
    current = energy.thresholds_of(api.telemetry)
    changed = args.gpu or args.cpu or args.sensor or args.no_cpu or args.no_sensor
    if changed:
        arguments = {"gpu": _pair(args.gpu) if args.gpu else current["gpu"]}
        for kind in ("cpu", "sensor"):
            value = getattr(args, kind)
            if getattr(args, "no_" + kind):
                arguments[kind] = None
            elif value:
                arguments[kind] = _pair(value)
            elif kind in current:
                arguments[kind] = current[kind]
        _post(api, "energy/thresholds", arguments)
        current = energy.thresholds_of(api.telemetry)
    for kind, limits in current.items():
        print(f"{kind}: warn {limits['warn']:g} C, critical {limits['crit']:g} C")
    return 0


def _finite(text):
    """A finite number (the portal's JSON parser refuses NaN and infinities before any handler runs; so does this)."""
    import math
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a finite number")
    return value


def _pair(values):
    return {"warn": float(values[0]), "crit": float(values[1])}


def _archive(args):
    from hyperloom.web.features import energy
    api = _api()
    months = energy.archive_months(api.telemetry)
    if args.json:
        print(json.dumps(months, indent=2, allow_nan=False))
        return 0
    if not months:
        print("No month archived yet.")
    for month in months:
        cost = f"{month['cost']:.2f} {month['currency']}" if month.get("cost") is not None else             "cost unavailable · " + (month.get("cost_why") or "")
        per = "" if month.get("wh_per_1k_tokens") is None else f", {month['wh_per_1k_tokens']:.2f} Wh per 1k generated tokens"
        print(f"{month['month']} ({month['zone']}): {_wh(month['wh'])}, {cost}{per}")
    return 0


def _reset(args):
    if not args.yes:
        print("hyperloom: energy reset archives everything not yet archived and then clears the energy rows; "
              "the monthly archive is kept. Repeat with --yes to confirm.", file=sys.stderr)
        return 2
    api = _api()
    document = _post(api, "energy/reset", {"confirm": "reset"})
    print(f"Energy history reset; {document['archived_quarters']} quarter hours archived first.")
    return 0


def _thermals(args):
    from hyperloom.web.features import energy
    api = _api()
    document = energy.thermals_document(api, {"range": args.range})
    machines = document.get("machines")
    if machines is not None and args.machine:
        machines = [m for m in machines if m["id"] == args.machine]
        document = dict(document, machines=machines)
    if args.json:
        print(json.dumps(document, indent=2, allow_nan=False))
        return 0
    if machines is None:
        print("unavailable · " + document["why"].get("machines", "no data"))
        return 0
    if not machines:
        print("No temperature samples recorded in this range.")
    limits = document["thresholds"]
    for machine in machines:
        print(f"{machine['id']}:")
        for line in machine["lines"]:
            above = "" if line["warn_buckets"] is None else (
                f", {line['warn_buckets']} of {line['sampled_buckets']} sampled {document['step'] // 60}-minute buckets "
                "reached warn")
            print(f"  {line['label']}: last {line['last']:.1f} C, max {line['max']:.1f} C{above}")
        for period in machine["throttle"]:
            print(f"  throttled{' (device ' + period['device'] + ')' if period.get('device') else ''}: "
                  f"{_when(period['start'])} - {_when(period['end'])}")
    print("Thresholds: " + "; ".join(f"{k} warn {v['warn']:g} C / crit {v['crit']:g} C" for k, v in limits.items()))
    return 0


def _run(handler):
    def run(args):
        try:
            return handler(args)
        except (ValueError, LookupError, PermissionError) as exc:
            print(f"hyperloom: {exc}", file=sys.stderr)
            return 2
    return run


def add(commands):
    parser = commands.add_parser("energy", help="energy estimate from power sensors (not metered)")
    parser.add_argument("--range", choices=("24h", "7d", "30d"), default="24h")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(ext_run=_run(_show))
    sub = parser.add_subparsers(dest="energy_command")
    show = sub.add_parser("show", help="energy per machine and domain, cost, energy per 1k generated tokens")
    show.add_argument("--range", choices=("24h", "7d", "30d"), default="24h")
    show.add_argument("--json", action="store_true")
    show.set_defaults(ext_run=_run(_show))
    compute = sub.add_parser("compute", help="recompute what is not yet archived (what the collector does)")
    compute.add_argument("--json", action="store_true")
    compute.set_defaults(ext_run=_run(_compute))
    price = sub.add_parser("price", help="show or set the energy price per kWh")
    price.add_argument("--set", type=_finite, metavar="PRICE")
    price.add_argument("--currency", default="EUR")
    price.add_argument("--clear", action="store_true")
    price.set_defaults(ext_run=_run(_price))
    thresholds = sub.add_parser("thresholds", help="show or set the thermal thresholds (degrees Celsius)")
    for kind in ("gpu", "cpu", "sensor"):
        thresholds.add_argument("--" + kind, nargs=2, type=_finite, metavar=("WARN", "CRIT"))
    thresholds.add_argument("--no-cpu", action="store_true")
    thresholds.add_argument("--no-sensor", action="store_true")
    thresholds.set_defaults(ext_run=_run(_thresholds))
    archive = sub.add_parser("archive", help="every archived month (permanent; never removed by a reset)")
    archive.add_argument("--json", action="store_true")
    archive.set_defaults(ext_run=_run(_archive))
    reset = sub.add_parser("reset", help="archive, then clear the energy rows (the monthly archive is kept)")
    reset.add_argument("--yes", action="store_true")
    reset.set_defaults(ext_run=_run(_reset))
    thermals = commands.add_parser("thermals", help="temperature history per machine and sensor")
    thermals.add_argument("--range", choices=("day", "week"), default="day")
    thermals.add_argument("--machine")
    thermals.add_argument("--json", action="store_true")
    thermals.set_defaults(ext_run=_run(_thermals))
