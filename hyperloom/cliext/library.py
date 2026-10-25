"""``hyperloom models``: the model library from the command line (the same functions as the portal's library page).

    hyperloom models list [--machine M] [--refresh]       model units per machine (cached scan, or scan now)
    hyperloom models verify M ROOT NAME [--wait]           hash one unit against its manifests
    hyperloom models job M JOB [--wait] | cancel M JOB
    hyperloom models roots M [--set DIR ...] [--clear]
    hyperloom models ledger-accept                         accept the current RTX big-job ledger as the baseline

Exit codes for waited jobs: 0 succeeded, 1 failed, 2 cancelled.
"""

import json
from pathlib import Path
import sys
import threading


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


def _outcome(record):
    state = (record or {}).get("state")
    return 0 if state == "succeeded" else 2 if state == "cancelled" else 1


class _Interrupt:
    """Ctrl+C sets a flag the running verification polls (it then cancels its node job and stops)."""

    def __init__(self):
        self.event = threading.Event()

    def __call__(self):
        return self.event.is_set()


def _run_cancellable(function):
    """Run with Ctrl+C turned into a cancellation the running operation sees (it then cancels its node job and
    waits for the node's record, bounded); a second Ctrl+C interrupts at once."""
    import signal
    flag = _Interrupt()

    def interrupt(signum, frame):
        if flag.event.is_set():
            raise KeyboardInterrupt
        flag.event.set()
        print("hyperloom: cancelling (Ctrl+C again to stop waiting)", file=sys.stderr, flush=True)
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        return function(flag)
    finally:
        signal.signal(signal.SIGINT, previous)


def _progress(record):
    progress = record.get("progress") or {}
    paused = (record.get("paused") or {}).get("reason")
    text = ("paused: " + paused) if paused else progress.get("path") or record.get("state", "")
    print("hyperloom models: " + str(text), file=sys.stderr, flush=True)


def run(args):
    from hyperloom import cli, downloads
    cluster = _cluster()
    operation = args.models_operation
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
        if operation == "job":
            record = downloads.library_job(cluster, args.machine, args.job)
            if args.wait:
                record = _run_cancellable(lambda flag: downloads.watch_job(cluster, args.machine, args.job,
                                                                           cancelled=flag, progress=_progress))
                _print(record)
                return _outcome(record)
            _print(record)
            return 0
        if operation == "cancel":
            _print(downloads.library_job(cluster, args.machine, args.job, cancel=True))
            return 0
        if operation == "verify":
            def work(flag):
                if flag():
                    raise downloads.Cancelled("cancelled")
                started = downloads.verify_start(cluster, args.machine, args.model_root, args.name, store=store,
                                                 ledger=ledger, cancelled=flag, progress=_progress)
                if started["mode"] == "node" and (args.wait or flag()):
                    # Waiting, or interrupted while the node job was being started: follow it (a cancellation is
                    # sent to the node and its final record awaited).
                    return downloads.watch_job(cluster, args.machine, started["job"], cancelled=flag,
                                               progress=_progress)
                return started
            try:
                record = _run_cancellable(work)
            except downloads.Cancelled:
                print("hyperloom: verification cancelled", file=sys.stderr)
                return 2
            _print(record)
            return _outcome(record) if record.get("state") else 0
        raise ValueError("unknown models operation")
    except (LookupError, PermissionError) as exc:
        print(f"hyperloom: {str(exc).strip(chr(39))}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def add(commands):
    models = commands.add_parser("models", help="model library: inventory and verification")
    operations = models.add_subparsers(dest="models_operation", required=True)
    listing = operations.add_parser("list", help="model units per machine (the last scan, or --refresh)")
    listing.add_argument("--machine")
    listing.add_argument("--refresh", action="store_true", help="scan now (read-only on every machine)")
    verify = operations.add_parser("verify", help="hash one unit against its *.sha256 manifests")
    verify.add_argument("machine")
    verify.add_argument("model_root")
    verify.add_argument("name")
    verify.add_argument("--wait", action="store_true")
    job = operations.add_parser("job", help="a library job on a node")
    job.add_argument("machine")
    job.add_argument("job")
    job.add_argument("--wait", action="store_true")
    cancel = operations.add_parser("cancel", help="cancel a library job on a node")
    cancel.add_argument("machine")
    cancel.add_argument("job")
    roots = operations.add_parser("roots", help="show or set a machine's extra model roots")
    roots.add_argument("machine")
    roots.add_argument("--set", nargs="+", metavar="DIR")
    roots.add_argument("--clear", action="store_true")
    operations.add_parser("ledger-accept", help="accept the current RTX big-job ledger as the guard's baseline")
    models.set_defaults(ext_run=run)
