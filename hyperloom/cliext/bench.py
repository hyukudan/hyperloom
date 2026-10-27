"""``hyperloom bench``: the benchmark commands; each calls the same function as the portal endpoint.

    hyperloom bench decode|prefill|ab|quality|tools|demo ...   run (holds a C11 admission, Ctrl-C cancels)
    hyperloom bench targets list|add|remove|capabilities        ad-hoc allow-list and profile declarations
    hyperloom bench runs list|show|clear                        stored runs
    hyperloom bench compare A B | share ID | guard [--target]   history, share summary, admission view
    hyperloom bench receipt ID --out FILE                       write an A/B run's receipt
    hyperloom bench verify-receipt FILE                         recompute every equality of a receipt offline

Exit codes: 0 done, 1 failed or verification mismatch, 2 invalid arguments, 3 refused by admission, 4 cancelled.
"""

import json
from pathlib import Path
import sys

from hyperloom import admission, cli, telemetry


def _api():
    from hyperloom.web.api import API
    api = API(cli.config_path())
    telemetry.open_store(api)       # the CLI may create the store (GET handlers never do)
    return api


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _fail(message, code):
    print("hyperloom bench: " + str(message), file=sys.stderr)
    return code


def _run(kind, args):
    from hyperloom.bench import ab, commands, runs, store as bench_store
    from hyperloom.web.features import bench as feature
    api = _api()
    try:
        arguments = commands.arguments_from(kind, args)
        module = ab if kind == "ab" else feature.KIND_MODULES[kind]
        arguments = module.validate(api, arguments)
        resources = ab.resources(api, arguments) if kind == "ab" else feature._resources(api, arguments)
    except PermissionError as exc:
        return _fail(exc, 3)
    except (ValueError, LookupError) as exc:
        return _fail(str(exc).strip("'\""), 2)
    try:
        with runs.hold(api.config_path, resources, f"cli bench/{kind} {arguments['run_id'][:8]}") as (db, hold_id):
            context = runs.CLIContext(runs.DEADLINES[kind], hold_id, db, quiet=args.quiet)
            with runs.interrupt_cancels(context):
                try:
                    result = feature._job(kind)(api, arguments, context)
                except Exception as exc:
                    result = None
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    error = None
    except admission.Refused as exc:
        return _fail(exc, 3)
    run = bench_store.get(telemetry.open_store(api), arguments["run_id"], detail=False)
    _print({"run_id": run["id"], "state": run["state"], "summary": run["summary"]})
    if error:
        return _fail(error, 1)
    if result is None:
        return 4
    return 0 if run["state"] == "succeeded" else 1


def _targets(args):
    from hyperloom.bench import targets
    api = _api()
    try:
        if args.action == "list":
            rows, why = targets.listing(api)
            _print({"targets": rows, "why": why})
        elif args.action == "add":
            entry = {"id": args.id, "scheme": args.scheme, "host": args.host, "port": args.port}
            for key in ("label", "context_tokens"):
                if getattr(args, key) is not None:
                    entry[key] = getattr(args, key)
            if args.machines:
                entry["machines"] = "external" if args.machines == "external" else args.machines.split(",")
            if args.sampling:
                entry["sampling"] = True
            if args.thinking:
                entry["thinking"] = True
            result = targets.save(api, entry)
            api.audit("bench/targets/save", entry, {"state": "succeeded", "result": result})
            _print(result)
        elif args.action == "remove":
            result = targets.remove(api, {"id": args.id})
            api.audit("bench/targets/remove", {"id": args.id}, {"state": "succeeded", "result": result})
            _print(result)
        else:
            arguments = {"profile": args.profile, "thinking": args.thinking_switch == "on"}
            result = targets.save_capabilities(api, arguments)
            api.audit("bench/targets/capabilities", arguments, {"state": "succeeded", "result": result})
            _print(result)
    except (ValueError, LookupError) as exc:
        return _fail(str(exc).strip("'\""), 2)
    return 0


def _runs(args):
    from hyperloom.bench import store as bench_store
    api = _api()
    store = telemetry.open_store(api)
    try:
        if args.action == "list":
            _print(bench_store.runs(store, args.kind, None, args.limit))
        elif args.action == "show":
            _print(bench_store.get(store, args.id))
        else:
            if not args.yes:
                return _fail("clearing runs needs --yes", 2)
            removed = bench_store.clear(bench_store.opened(api), args.kind, args.run)
            arguments = {"confirm": "clear", "kind": args.kind, "run": args.run}
            api.audit("bench/runs/clear", arguments, {"state": "succeeded", "result": {"removed": len(removed)}})
            _print({"removed": len(removed)})
    except (ValueError, LookupError) as exc:
        return _fail(str(exc).strip("'\""), 2)
    return 0


def _history(args):
    from hyperloom.bench import guard, history, store as bench_store
    api = _api()
    store = telemetry.open_store(api)
    try:
        if args.command_name == "compare":
            _print(history.compare(*history.load_pair(store, args.a, args.b)))
        elif args.command_name == "share":
            print(history.share_text(history.share(bench_store.get(store, args.id, detail=False))))
        elif args.command_name == "guard":
            _print(guard.document(api, {"target": args.target} if args.target else {}))
        else:
            run = bench_store.get(store, args.id)
            receipt = (run.get("detail") or {}).get("receipt") if run["kind"] == "ab" else None
            if not receipt:
                return _fail("run " + args.id + " has no exactness receipt", 2)
            Path(args.out).write_text(json.dumps(receipt, indent=1, sort_keys=True), encoding="utf-8")
            print("receipt written: " + str(args.out))
    except (ValueError, LookupError) as exc:
        return _fail(str(exc).strip("'\""), 2)
    return 0


def _verify(args):
    from hyperloom.bench import ab
    try:
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read {args.file}: {exc}", 2)
    receipt = document.get("receipt") if isinstance(document, dict) and "receipt" in document else document
    result = ab.verify(receipt)
    _print(result)
    return 0 if result["ok"] else 1


def add(commands):
    from hyperloom.bench import commands as forms, store as bench_store
    bench = commands.add_parser("bench", help="benchmarks and evaluation against served endpoints")
    sub = bench.add_subparsers(dest="bench_command", required=True)
    for kind in ("decode", "prefill", "ab", "quality", "tools", "demo"):
        parser = sub.add_parser(kind, help=f"run a {kind} benchmark (admitted like a portal run)")
        forms.add_options(parser, kind)
        parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
        parser.set_defaults(ext_run=lambda args, kind=kind: _run(kind, args))
    targets = sub.add_parser("targets", help="ad-hoc targets and profile declarations")
    actions = targets.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    added = actions.add_parser("add")
    added.add_argument("id")
    added.add_argument("--scheme", choices=("http", "https"), required=True)
    added.add_argument("--host", required=True, help="a configured machine id or a private or loopback IP")
    added.add_argument("--port", type=int, required=True)
    added.add_argument("--label")
    added.add_argument("--machines", help='machines it forwards to (configured ids, comma-separated), or "external" '
                                          "for an address that is no configured machine")
    added.add_argument("--context-tokens", dest="context_tokens", type=int)
    added.add_argument("--sampling", action="store_true", help="the endpoint honours sampling parameters")
    added.add_argument("--thinking", action="store_true", help="the endpoint honours a thinking switch")
    removed = actions.add_parser("remove")
    removed.add_argument("id")
    caps = actions.add_parser("capabilities")
    caps.add_argument("profile")
    caps.add_argument("--thinking", dest="thinking_switch", choices=("on", "off"), required=True)
    targets.set_defaults(ext_run=_targets)
    stored = sub.add_parser("runs", help="stored runs")
    run_actions = stored.add_subparsers(dest="action", required=True)
    listed = run_actions.add_parser("list")
    listed.add_argument("--kind", choices=bench_store.KINDS)
    listed.add_argument("--limit", type=int, default=50)
    shown = run_actions.add_parser("show")
    shown.add_argument("id")
    cleared = run_actions.add_parser("clear")
    cleared.add_argument("--kind", choices=bench_store.KINDS)
    cleared.add_argument("--run")
    cleared.add_argument("--yes", action="store_true")
    stored.set_defaults(ext_run=_runs)
    compared = sub.add_parser("compare", help="two runs of one kind side by side")
    compared.add_argument("a")
    compared.add_argument("b")
    compared.set_defaults(ext_run=_history, command_name="compare")
    shared = sub.add_parser("share", help="the allow-listed share summary of a run, as text")
    shared.add_argument("id")
    shared.set_defaults(ext_run=_history, command_name="share")
    guarded = sub.add_parser("guard", help="admission rules, holds and whether a target would be admitted now")
    guarded.add_argument("--target", help="profile:<name> or target:<id>")
    guarded.set_defaults(ext_run=_history, command_name="guard")
    receipt = sub.add_parser("receipt", help="write an A/B run's exactness receipt to a file")
    receipt.add_argument("id")
    receipt.add_argument("--out", required=True)
    receipt.set_defaults(ext_run=_history, command_name="receipt")
    verify = sub.add_parser("verify-receipt", help="recompute every equality of an A/B receipt offline")
    verify.add_argument("file")
    verify.set_defaults(ext_run=_verify)
