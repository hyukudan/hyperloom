"""CLI for the token ledger, census, RTX prefill cache and token value (the portal's functions, same code path).

hyperloom tokens [show] [--range R] [--by kind|model|machine] [--json]
hyperloom tokens export [--range R]                CSV on stdout (formula prefixes escaped)
hyperloom tokens reset --yes                       audited, like the portal's reset
hyperloom tokens config [--census-dir D|none] [--logs-dir D|none]
hyperloom census [--json]
hyperloom rtx-cache [show] [--json] | rtx-cache parse ID
hyperloom token-value [show] [--range R] [--json] | token-value set FILE.json ("-" = stdin)

Reads open the portal's telemetry store only when it exists (otherwise the documents say why); writes go through the
portal API's audited dispatch.
"""

import json
from pathlib import Path
import sys


RANGES = ("3600", "86400", "604800", "2592000", "all")
_OPENED = []


def _api(args, write=False):
    from hyperloom.telemetry.store import Store
    from hyperloom.web.api import API
    api = API(args.config, audit_path=Path(args.config).parent / "audit.jsonl")
    path = Path(args.config).parent / "telemetry.sqlite3"
    if write or path.exists():
        api.telemetry = Store(path)          # the portal's store file (created only by a write)
        _OPENED.append(api.telemetry)
    return api


def _closing(run):
    """Close the stores a command opened (the CLI process may live on, e.g. in tests)."""
    def wrapped(args):
        try:
            return run(args)
        finally:
            while _OPENED:
                _OPENED.pop().close()
    return wrapped


def _post(api, action, arguments):
    from hyperloom.web.api import APIError
    try:
        return api.feature_post(action, arguments)[1]
    except APIError as exc:
        raise SystemExit(f"hyperloom: {action} refused: {exc}") from None


def _get(api, handler, query):
    try:
        return handler(api, query)
    except (ValueError, LookupError, PermissionError) as exc:
        raise SystemExit(f"hyperloom: {exc}") from None


def _print(document, as_json, text):
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
    else:
        print(text(document))


def _num(value, digits=0):
    return "unavailable" if value is None else f"{value:,.{digits}f}"


def _why(document, key):
    return document.get("why", {}).get(key) or "source not reported"


def _tokens_text(doc):
    if doc.get("kpis") is None:
        return "token ledger unavailable: " + _why(doc, "kpis")
    k = doc["kpis"]
    lines = [f"Token ledger · range {doc['range']} s (from {doc['start']:.0f}) · zone {doc['timezone']} ({doc['timezone_source']})",
             f"  total {_num(k['total'])}  input {_num(k['input'])}  generated {_num(k['generated'])}",
             f"  computed prefill {_num(k['computed'])} ({k['computed_tag']})  cache-restored "
             f"{_num(k['cached']) if k['cached'] is not None else 'unavailable · ' + _why(doc, 'kpis.cached')}  imported "
             f"{_num(k['imported']) if k['imported'] is not None else 'unavailable · ' + _why(doc, 'kpis.imported')}",
             "", "  model                         input        generated    cached       share"]
    for m in doc["models"]:
        share = "unavailable" if m["cached_share"] is None else f"{m['cached_share'] * 100:.1f} %"
        cached = "unavailable" if m["cached"] is None else _num(m["cached"])
        lines.append(f"  {m['model'][:28]:<28}  {_num(m['input']):>11}  {_num(m['generated']):>11}  {cached:>11}  {share}")
    lines += [""] + [f"  - {text}" for text in doc["insights"]]
    lines += [f"  note: {n}" for n in doc.get("notes") or []]
    return "\n".join(lines)


def run_tokens(args):
    from hyperloom.web.features import ledger
    if args.action == "reset":
        if not args.yes:
            raise SystemExit("hyperloom: tokens reset deletes every ledger record; repeat with --yes")
        print(json.dumps(_post(_api(args, write=True), "ledger/reset", {"confirm": "reset"}), indent=2))
        return 0
    if args.action == "config":
        arguments = {}
        for key, value in (("census_dir", args.census_dir), ("logs_dir", args.logs_dir)):
            if value is not None:
                arguments[key] = None if value.lower() == "none" else value
        if not arguments:
            api = _api(args)
            print(json.dumps(ledger.settings(api), indent=2))
            return 0
        print(json.dumps(_post(_api(args, write=True), "ledger/settings", arguments), indent=2))
        return 0
    api = _api(args)
    if args.action == "export":
        try:
            text, truncated = ledger.csv_text(api, {"range": args.range})
        except (ValueError, PermissionError) as exc:
            raise SystemExit(f"hyperloom: {exc}") from None
        sys.stdout.write(text)
        if truncated:
            print(f"hyperloom: {truncated} rows beyond {ledger.CSV_ROWS} not exported", file=sys.stderr)
        return 0
    _print(_get(api, ledger.ledger, {"range": args.range, "by": args.by}), args.json, _tokens_text)
    return 0


def _census_text(doc):
    if doc.get("artifacts") is None:
        return "census unavailable: " + _why(doc, "artifacts")
    lines = []
    for artifact in doc["artifacts"]:
        lines.append(f"{artifact['file']}: {artifact['engine']} {artifact['sha']} (binary {artifact['binsha'] or 'unavailable'})")
        for cell in artifact["cells"]:
            tag = cell["ceiling_tag"] or "unavailable"
            lines.append(f"  {cell['cell'][:44]:<44} ceiling {_num(cell['ceiling_tok_s'], 1):>9} tok/s [{tag}]  "
                         f"% ceiling {_num(cell['pct_ceiling'], 1):>6}  live: {cell['match']['why']}")
    for name, rate in (doc.get("live") or {}).items():
        lines.append(f"live {name}: {_num(rate['tok_s'], 1)} tok/s ({rate['label']})" + (f" · {rate['why']}" if rate["why"] else ""))
    lines += [f"rejected {r['file']}: {r['why']}" for r in doc.get("rejected") or []]
    return "\n".join(lines)


def _fresh(api, name):
    """Build the snapshot document directly (the CLI has no ledger-files collector running)."""
    from hyperloom.web.features import ledger
    fields = (ledger.build_census if name == "census" else ledger.build_rtx)(api, api.clock())
    if name == "rtx":
        store = getattr(api, "telemetry", None)
        fields["last"] = store.get(ledger.BATCH, "last") if store is not None else None
    return api.document("ledger-" + name, **fields)


def run_census(args):
    _print(_fresh(_api(args), "census"), args.json, _census_text)
    return 0


def _rtx_text(doc):
    lines = []
    for service in doc.get("services") or []:
        lines.append(f"service {service['profile']}: health {json.dumps(service['health'])}")
        provider = service.get("provider")
        lines.append("  provider counters: " + (", ".join(f"{c['name']}={c['value']}" for c in provider["counters"])
                                               if provider else "unavailable · " + service["why"].get("provider", "")))
    if not doc.get("services"):
        lines.append("services: unavailable · " + _why(doc, "services"))
    if doc.get("runs") is None:
        lines.append("runs: unavailable · " + _why(doc, "runs"))
    else:
        lines += [f"run {r['id']}  {r['name']}  {r['bytes']} bytes" for r in doc["runs"]]
    last = doc.get("last")
    if last:
        s = last["stats"]
        lines.append(f"last parsed {last['name']}: hits {s['total']['hits']}/{s['total']['routed']} routed, wait "
                     f"{_num(s['wait_s'], 3)} s, pinned {s['total']['bytes_pinned']} B, disk {s['total']['bytes_disk']} B")
    return "\n".join(lines)


def run_rtx(args):
    from hyperloom.web.features import ledger
    if args.action == "parse":
        if not args.id:
            raise SystemExit("hyperloom: rtx-cache parse needs a run id (see hyperloom rtx-cache)")
        result = _post(_api(args, write=True), "ledger/rtx-parse", {"id": args.id})
        _print(result, args.json, lambda r: _rtx_text({"services": [], "runs": [], "last": r, "why": {"services": "-"}}))
        return 0
    _print(_fresh(_api(args), "rtx"), args.json, _rtx_text)
    return 0


def _value_text(doc):
    if doc.get("models") is None:
        return "token value unavailable: " + _why(doc, "models")
    currency = doc.get("currency") or ""
    lines = [f"Token value · range {doc['range']} s · {doc['basis']}"]
    for row in doc["models"]:
        state = "exact" if row.get("exact") else "QUALIFIED"
        lines.append(f"  {row['model'][:28]:<28} total {_num(row['total'], 2)} {currency} ({state})  savings "
                     f"{_num(row['savings'], 2)}")
        for text in [*row.get("qualifiers", []), *row["why"].values()]:
            lines.append(f"      - {text}")
    totals = doc.get("totals")
    if totals:
        lines.append(f"  all priced models: {_num(totals['total'], 2)} {currency}, cache savings {_num(totals['savings'], 2)} "
                     f"{currency} ({'exact' if totals.get('exact') else 'QUALIFIED'})")
        lines += [f"      - {text}" for text in totals.get("qualifiers", [])]
    lines += [f"  note: {v}" for v in doc["why"].values()]
    return "\n".join(lines)


def run_value(args):
    from hyperloom.web.features import ledger
    if args.action == "set":
        if not args.file:
            raise SystemExit("hyperloom: token-value set needs a JSON file (or - for stdin)")
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        try:
            table = json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"hyperloom: price table is not JSON: {exc}") from None
        print(json.dumps(_post(_api(args, write=True), "ledger/prices", table), indent=2))
        return 0
    _print(_get(_api(args), ledger.value, {"range": args.range}), args.json, _value_text)
    return 0


def add(commands):
    from hyperloom.cli import config_path
    tokens = commands.add_parser("tokens", help="token ledger: totals, export, reset, directories")
    tokens.add_argument("action", nargs="?", default="show", choices=("show", "export", "reset", "config"))
    tokens.add_argument("--range", default="86400", choices=RANGES)
    tokens.add_argument("--by", default="kind", choices=("kind", "model", "machine"))
    tokens.add_argument("--json", action="store_true")
    tokens.add_argument("--yes", action="store_true", help="confirm a reset")
    tokens.add_argument("--census-dir", help="census artifacts directory (absolute path, or none)")
    tokens.add_argument("--logs-dir", help="batch RTX run logs directory (absolute path, or none)")
    tokens.add_argument("--config", type=Path, default=config_path())
    tokens.set_defaults(ext_run=_closing(run_tokens))
    census = commands.add_parser("census", help="hardware-ceiling census artifacts and their match with running profiles")
    census.add_argument("--json", action="store_true")
    census.add_argument("--config", type=Path, default=config_path())
    census.set_defaults(ext_run=_closing(run_census))
    rtx = commands.add_parser("rtx-cache", help="RTX prefill service counters and batch-run strix-stream stats")
    rtx.add_argument("action", nargs="?", default="show", choices=("show", "parse"))
    rtx.add_argument("id", nargs="?")
    rtx.add_argument("--json", action="store_true")
    rtx.add_argument("--config", type=Path, default=config_path())
    rtx.set_defaults(ext_run=_closing(run_rtx))
    value = commands.add_parser("token-value", help="price the ledger's tokens from the local price table")
    value.add_argument("action", nargs="?", default="show", choices=("show", "set"))
    value.add_argument("file", nargs="?")
    value.add_argument("--range", default="86400", choices=RANGES)
    value.add_argument("--json", action="store_true")
    value.add_argument("--config", type=Path, default=config_path())
    value.set_defaults(ext_run=_closing(run_value))
