"""`hyperloom usb4 [MACHINE] [--json] [--rate SECONDS]`: USB4 link health of the Linux nodes from a live,
read-only node call (the same node verb and the same summary the portal's usb4 page uses).

Interrupt counts are since boot; with --rate the command samples twice and prints interrupts per second of each
host interface's control and data vectors (unavailable when a ring was opened or closed in between). Kernel
evidence counts cover the running boot as far as the journal holds it.
"""

import json
import sys
import time


def add(commands):
    parser = commands.add_parser("usb4", help="USB4 link health of the Linux nodes (read-only)")
    parser.add_argument("machine", nargs="?", help="machine id (default: every Linux node)")
    parser.add_argument("--json", action="store_true", help="print the summaries as JSON")
    parser.add_argument("--rate", type=float, default=0, metavar="SECONDS",
                        help="sample twice, SECONDS apart (1-60), to report interrupts per second")
    parser.set_defaults(ext_run=run)


def _api():
    from hyperloom.web.api import API
    return API()


def collect(api, machine, rate=0, sleep=time.sleep, clock=time.time):
    """Summary of one machine: one node call with an initial journal scan of the running boot; with `rate`, a
    second call `rate` seconds later gives interrupts per second."""
    from hyperloom.web.features import usb4
    first_at = clock()
    first = usb4.fetch(api, machine, {"journal": {}})
    if not isinstance(first, dict) or first.get("error"):
        raise RuntimeError((first or {}).get("error") or "node returned no USB4 report")
    summary = usb4.summarise(first, first_at)
    summary["journal"] = summary.pop("journal_read")
    if rate:
        sleep(rate)
        second_at = clock()
        second = usb4.fetch(api, machine, {})
        if not isinstance(second, dict) or second.get("error"):
            raise RuntimeError((second or {}).get("error") or "node returned no USB4 report")
        later = usb4.summarise(second, second_at, summary)
        later.pop("journal_read")
        later["journal"] = summary["journal"]
        summary = later
        summary["interrupts"]["nhis"] = rates(first, second, second_at - first_at)
    elif summary["interrupts"].get("status") == "ok":
        summary["interrupts"]["nhis"] = rates(first, None, None)
    return summary


def rates(first, second, gap):
    """NHI/ring rows from two node reports (same boot, same vector and IRQ membership), else counts only."""
    from hyperloom.web.features import usb4
    def vectors(report):
        part = (report or {}).get("interrupts") or {}
        return part.get("vectors") or [] if part.get("status") == "ok" else []
    now, before = vectors(second or first), vectors(first)
    if second is None or first.get("boot_id") != second.get("boot_id"):
        rows = usb4.nhi_rates(now, {}, 0.0, None)
        for row in rows:
            row["why"] = "counts since boot (pass --rate SECONDS for interrupts per second)" if second is None \
                else "the node rebooted between the samples"
        return rows
    old = {(v["nhi"], v["vector"]): v for v in before}
    table = {}
    for vector in now:
        prior = old.get((vector["nhi"], vector["vector"]))
        if prior and prior["irq"] == vector["irq"] and vector["count"] >= prior["count"]:
            table[(vector["nhi"], vector["vector"])] = (gap, gap, vector["count"] - prior["count"], vector["irq"], False)
    return usb4.nhi_rates(now, table, gap, {"collected_at": 0.0, "interrupts": {"vectors": before}})


def _rate(value):
    return "unavailable" if value is None else f"{value:,.0f}/s"


def text(machine, summary):
    lines = [f"{machine} · kernel {summary.get('kernel') or 'unavailable'}"]
    links = summary.get("links")
    if links is None:
        lines.append("  links: unavailable · " + str(summary.get("links_why")))
    for link in links or []:
        def side(name):
            d = link[name]
            if d.get("gbps") is None:
                return f"{name} unavailable ({d.get('raw_speed')!r} x {d.get('raw_lanes')!r})"
            return f"{name} {d['gbps_per_lane']:g} Gb/s x{d['lanes']} = {d['gbps']:g} Gb/s"
        flag = ("full rate" if link.get("full_rate") else
                "BELOW FULL RATE (" + ", ".join(link.get("below_full_rate") or []) + ")" if link.get("full_rate") is False
                else "rate unknown")
        lines.append(f"  link {link['router']} -> {link.get('peer') or 'peer unknown'}: {side('rx')}, {side('tx')} · {flag}")
    layout = summary.get("layout") or {}
    if layout.get("status") != "ok":
        lines.append("  streams: unavailable · " + str(layout.get("why")))
    else:
        lines.append(f"  layout published {layout.get('generated_at') or 'at an unknown time'}")
        for stream in summary.get("streams") or []:
            published, live, config = stream.get("published") or {}, stream.get("live") or {}, stream.get("config") or {}
            mode = "busy poll" if config.get("busy_poll") else "interrupts"
            if config.get("gpu_direct"):
                mode += ", GPU direct"
            lines.append(f"  stream {stream['id']} {stream.get('device') or ''}: published openable={published.get('openable')}"
                         f" attached={published.get('attached')} · live device={live.get('device_present')}"
                         f" attached={live.get('attached_now')} · {mode}, ring {config.get('ring_size')}")
    interrupts = summary.get("interrupts") or {}
    if interrupts.get("status") != "ok":
        lines.append("  interrupts: unavailable · " + str(interrupts.get("why")))
    for row in interrupts.get("nhis") or []:
        lines.append(f"  interrupts {row['nhi']} {row['ring']} ({row['vectors']} vectors): {row['count']:,} since boot, "
                     f"{_rate(row['rate_per_s'])}" + (f" · {row['why']}" if row.get("why") else ""))
    journal = summary.get("journal") or {}
    if journal.get("status") != "ok":
        lines.append("  kernel evidence: unavailable · " + str(journal.get("why")))
    else:
        counts = ", ".join(f"{kind} {count}" for kind, count in (journal.get("counts") or {}).items())
        lines.append(f"  kernel evidence this boot: {counts}"
                     + ("" if journal.get("complete") else f" (lower bounds: {journal.get('incomplete_why')})"))
    return "\n".join(lines)


def run(args):
    from hyperloom.web.features import usb4
    if args.rate and not 1 <= args.rate <= 60:
        print("hyperloom usb4: --rate must be 1..60 seconds", file=sys.stderr)
        return 2
    api = _api()
    targets = usb4.machines(api)
    if args.machine:
        targets = [m for m in targets if m.id == args.machine]
        if not targets:
            print(f"hyperloom usb4: {args.machine} is not a Linux node of this configuration", file=sys.stderr)
            return 2
    if not targets:
        print("hyperloom usb4: no Linux nodes configured", file=sys.stderr)
        return 2
    results, status = {}, 0
    for machine in targets:
        try:
            results[machine.id] = collect(api, machine, args.rate)
        except Exception as exc:
            results[machine.id] = {"error": f"{type(exc).__name__}: {exc}"}
            status = 1
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
    else:
        for machine, summary in results.items():
            print(f"{machine} · unavailable · {summary['error']}" if "error" in summary else text(machine, summary))
    return status
