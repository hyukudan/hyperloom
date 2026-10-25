"""`hyperloom ops`: node tool status and update, power actions, machine configuration and collector requests
(the portal's operations page from the command line; the same functions)."""

import json
import sys
import threading
import time
from pathlib import Path


def cli_api(args):
    """A portal API object for command-line use: the configuration given, the shared store, and a snapshot of
    the profiles only (no device inventory round)."""
    from hyperloom import telemetry
    from hyperloom.web.api import API
    api = API(getattr(args, "config", None))
    telemetry.open_store(api)
    cache = {}

    def snapshot():
        if "profiles" not in cache:
            try:
                cluster = api.cluster()
                statuses = api.orchestrate.status()
                rows = api._profiles(cluster, statuses)
            except Exception as exc:
                rows = [{"id": "?", "state": None, "why": {"state": str(exc)}}]
            cache["profiles"] = {"profiles": {"profiles": rows}}
        return cache["profiles"]
    api.snapshot = snapshot
    return api


class Context:
    """The job context of a command-line run: progress on stderr, Ctrl+C cancels, a deadline."""

    def __init__(self, deadline_s):
        self.deadline = time.monotonic() + deadline_s
        self.reason = None

    def __call__(self, text):
        print("hyperloom: " + str(text), file=sys.stderr)

    def cancelled(self):
        if self.reason is None and time.monotonic() >= self.deadline:
            self.reason = "deadline reached"
        return self.reason is not None


def safe(run):
    """A command whose not-found errors print like the CLI's other refusals (exit 1)."""
    def wrapped(args):
        try:
            return run(args)
        except LookupError as exc:
            raise ValueError(str(exc).strip("'\"")) from None
    return wrapped


def _hold(api, resources, label):
    """The shared "core" admission hold a deploy-like command takes (renewed every 30 s); returns a release."""
    from hyperloom import admission
    database = admission.for_config(api.config_path)
    try:
        hold = database.acquire("core", resources, label=label)
    except admission.Refused as exc:
        raise RuntimeError(str(exc)) from exc
    stop = threading.Event()

    def renew():
        while not stop.wait(30):
            try:
                database.renew(hold)
            except Exception:  # noqa: BLE001 - a transient database error: retried at the next renewal
                pass
    threading.Thread(target=renew, name="hyperloom-cli-admission", daemon=True).start()

    def release():
        stop.set()
        database.release(hold)
    return release


def _print(value):
    print(json.dumps(value, indent=2, default=str))
    return 0


def _audited(api, action, arguments, work):
    try:
        result = work()
    except Exception as exc:
        api.audit(action, arguments, {"state": "refused", "error": str(exc)})
        raise
    api.audit(action, arguments, {"state": "succeeded", "result": result})
    return result


def nodes(args):
    from hyperloom.web.features import ops
    return _print(ops.nodes_document(cli_api(args), {}))


def nodes_update(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    arguments = {"profile": args.profile, **({"machine": args.machine} if args.machine else {})}
    arguments = ops.validate_nodes_update(api, arguments)
    release = _hold(api, ops.update_resources(api, arguments), "cli ops nodes-update " + args.profile)
    try:
        return _print(_audited(api, "ops/nodes-update", arguments,
                               lambda: ops.nodes_update(api, arguments, Context(900), deadline=time.monotonic() + 900)))
    finally:
        release()


def wake(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    return _print(_audited(api, "ops/wake", {"machine": args.machine}, lambda: ops.wake(api, {"machine": args.machine})))


def shutdown(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    arguments = ops.validate_shutdown(api, {"machine": args.machine, "confirm": args.confirm or ""})
    release = _hold(api, {args.machine}, "cli ops shutdown " + args.machine)
    try:
        return _print(_audited(api, "ops/shutdown", arguments, lambda: ops.shutdown(api, arguments, Context(90))))
    finally:
        release()


def settings(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    arguments = {}
    if args.clear_shutdown_helper:
        arguments["shutdown_helper"] = None
    elif args.shutdown_helper:
        arguments["shutdown_helper"] = str(Path(args.shutdown_helper).absolute())
    return _print(_audited(api, "ops/settings", arguments, lambda: ops.settings(api, arguments)))


def _machine_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def machine(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    operation = args.machine_operation
    if operation == "list":
        return _print(ops.config_document(api, {}))
    if operation in ("test", "scan"):
        if operation == "scan":
            return _print(ops.scan_hostkey(api, {"machine": args.id}))
        return _print(ops.machine_test(api, {"machine": args.id}, Context(180)))
    if operation == "extras":
        arguments = {"machine": args.id}
        if args.mac is not None:
            arguments["mac"] = args.mac
        if args.wol_broadcast is not None:
            arguments["wol_broadcast"] = args.wol_broadcast
        if args.hide_disk is not None:
            arguments["hide_disks"] = args.hide_disk
        if args.hide_interface is not None:
            arguments["hide_interfaces"] = args.hide_interface
        return _print(_audited(api, "ops/machine-extras", arguments, lambda: ops.machine_extras(api, arguments)))
    edit = {"op": operation}
    if operation == "add":
        edit["machine"] = _machine_file(args.file)
        if args.index is not None:
            edit["index"] = args.index
    elif operation == "edit":
        edit.update(id=args.id, machine=_machine_file(args.file))
    elif operation == "remove":
        if args.confirm != args.id:
            raise ValueError("type the machine id with --confirm to remove it")
        edit["id"] = args.id
    elif operation == "move":
        edit.update(id=args.id, index=args.index)
    elif operation == "validate":
        edit = json.loads(Path(args.file).read_text(encoding="utf-8"))
        return _print(ops.validate_config(api, edit))
    return _print(_audited(api, "ops/config/save", edit, lambda: ops.save_config_any(api, edit)))


def collectors(args):
    from hyperloom.web.features import ops
    return _print(ops.collectors_document(cli_api(args), {}))


MEANING = {"pending": "not claimed yet: no running portal has picked it up",
           "queued": "claimed; waiting for a collection that starts after the request",
           "joined": "claimed while a collection was running; confirmed by the next collection",
           "claimed": "claimed by a portal, being applied", "done": "done", "failed": "failed"}


def request(args):
    from hyperloom.web.features import ops
    api = cli_api(args)
    kind = args.ops_operation
    arguments = {"collector": args.collector, **({"seconds": args.seconds} if kind == "interval" else {})}
    rid = ops.queue_request(api, kind, arguments)
    end = time.monotonic() + args.wait
    state = ops.request_state(api, rid)
    while state and state["state"] not in ("done", "failed") and time.monotonic() < end:
        time.sleep(0.5)
        state = ops.request_state(api, rid)
    state = state or {"id": rid, "state": "failed", "result": {"error": "the request record disappeared"}}
    state["meaning"] = MEANING.get(state["state"], state["state"])
    _print(state)
    return 0 if state["state"] == "done" else 1 if state["state"] == "failed" else 2


def add(commands):
    from hyperloom.cli import config_path
    parser = commands.add_parser("ops", help="node tool, power, machine configuration and collectors")
    sub = parser.add_subparsers(dest="ops_operation", required=True)

    def command(name, run, text):
        item = sub.add_parser(name, help=text)
        item.add_argument("--config", type=Path, default=config_path())
        item.set_defaults(ext_run=safe(run))
        return item
    command("nodes", nodes, "deployed node tool digest per machine vs this checkout")
    item = command("nodes-update", nodes_update, "update the node tool of a profile's machines (never forced; "
                                                 "refused while a lease is live)")
    item.add_argument("profile")
    item.add_argument("--machine")
    item = command("wake", wake, "send a Wake-on-LAN magic packet to a machine")
    item.add_argument("machine")
    item = command("shutdown", shutdown, "power a machine off through the configured shutdown helper")
    item.add_argument("machine")
    item.add_argument("--confirm", help="the machine id again (required)")
    item = command("settings", settings, "the shutdown helper (an executable on this machine)")
    group = item.add_mutually_exclusive_group()
    group.add_argument("--shutdown-helper")
    group.add_argument("--clear-shutdown-helper", action="store_true")
    item = command("machine", machine, "list, validate, add, edit, remove, move, test or scan machines")
    machines = item.add_subparsers(dest="machine_operation", required=True)

    def operation(name, text):
        child = machines.add_parser(name, help=text)
        child.add_argument("--config", type=Path, default=config_path())
        child.set_defaults(ext_run=safe(machine))
        return child
    operation("list", "the configuration's machines, frozen fields and extras")
    operation("validate", "validate an edit given as a JSON file ({op, id?, machine?, index?})").add_argument("file")
    child = operation("add", "add a machine from a JSON file")
    child.add_argument("file")
    child.add_argument("--index", type=int)
    child = operation("edit", "replace a machine's entry from a JSON file")
    child.add_argument("id")
    child.add_argument("file")
    child = operation("remove", "remove a machine")
    child.add_argument("id")
    child.add_argument("--confirm")
    child = operation("move", "move a machine to a position")
    child.add_argument("id")
    child.add_argument("index", type=int)
    operation("test", "ssh, node tool, GPU, engines and USB4 checks (read-only)").add_argument("id")
    operation("scan", "host-key candidates of a machine (nothing is pinned)").add_argument("id")
    child = operation("extras", "MAC, broadcast address and hidden disks/interfaces of a machine")
    child.add_argument("id")
    child.add_argument("--mac")
    child.add_argument("--wol-broadcast")
    child.add_argument("--hide-disk", action="append")
    child.add_argument("--hide-interface", action="append")
    command("collectors", collectors, "collectors, their intervals and bounds")
    item = command("refresh", request, "ask the running portal to collect now")
    item.add_argument("collector")
    item.add_argument("--wait", type=float, default=10.0)
    item = command("interval", request, "set a collector's interval (persisted; applied by the running portal)")
    item.add_argument("collector")
    item.add_argument("seconds", type=float)
    item.add_argument("--wait", type=float, default=10.0)
