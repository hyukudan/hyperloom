"""``hyperloom models``: the model library from the command line (the same functions as the portal's library page).

    hyperloom models list [--machine M] [--refresh]       model units per machine (cached scan, or scan now)
    hyperloom models verify M ROOT NAME                    refused: not available in this release
    hyperloom models roots M [--set DIR ...] [--clear]
    hyperloom models ledger-accept                         accept the current RTX big-job ledger as the baseline
"""

import json
from pathlib import Path
import sys


def _store(create=False):
    from hyperloom import cli
    from hyperloom.telemetry.store import Store
    path = cli.config_path().parent / "telemetry.sqlite3"
    if not create and not path.exists():
        return None
    return Store(str(path))


def _cluster():
    from hyperloom import cli
    return cli._cluster(cli.config_path())


def _print(value):
    print(json.dumps(value, indent=2, default=str))


def run(args):
    from hyperloom import cli, downloads
    operation = args.models_operation
    if operation == "verify":
        # Held like copies between machines: refused before the configuration is read or any machine is called.
        print("hyperloom: " + downloads.VERIFY_HELD, file=sys.stderr)
        return 1
    cluster = _cluster()
    if operation == "list":
        store = _store(create=args.refresh)
        try:
            machines = [args.machine] if args.machine else list(cluster.machines)
            result = {}
            for mid in machines:
                if args.refresh:
                    inventory = downloads.inventory(cluster, mid, store)
                    if store is not None and not inventory.get("busy"):
                        store.put("library/inventory", mid, inventory)
                else:
                    inventory = store.get("library/inventory", mid) if store is not None else None
                    if inventory is None:
                        inventory = {"machine": mid, "why": "not scanned yet: run with --refresh"}
                result[mid] = inventory
            _print(result)
            return 0
        finally:
            if store is not None:
                store.close()
    if operation == "roots":
        store = _store(create=bool(args.set) or args.clear)
        try:
            if args.set or args.clear:
                _print(downloads.set_roots(cluster, store, args.machine, [] if args.clear else args.set))
            else:
                _print({"machine": args.machine, "roots": downloads.library_roots(cluster, store).get(args.machine)})
            return 0
        finally:
            if store is not None:
                store.close()
    store = _store()
    try:
        ledger = downloads.ledger_guard(cli.config_path(), store)
        if operation == "ledger-accept":
            _print(ledger.accept())
            return 0
        raise ValueError("unknown models operation")
    except (LookupError, PermissionError) as exc:
        print(f"hyperloom: {str(exc).strip(chr(39))}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def add(commands):
    models = commands.add_parser("models", help="model library: inventory")
    operations = models.add_subparsers(dest="models_operation", required=True)
    listing = operations.add_parser("list", help="model units per machine (the last scan, or --refresh)")
    listing.add_argument("--machine")
    listing.add_argument("--refresh", action="store_true", help="scan now (read-only on every machine)")
    verify = operations.add_parser("verify", help="not available in this release (refused)")
    verify.add_argument("machine")
    verify.add_argument("model_root")
    verify.add_argument("name")
    roots = operations.add_parser("roots", help="show or set a machine's extra model roots")
    roots.add_argument("machine")
    roots.add_argument("--set", nargs="+", metavar="DIR")
    roots.add_argument("--clear", action="store_true")
    operations.add_parser("ledger-accept", help="accept the current RTX big-job ledger as the guard's baseline")
    models.set_defaults(ext_run=run)
