"""CLI of the observability views: the portal's collector functions run once, then its document builders.

    hyperloom requests [--profile P] [--model M] [--route R] [--outcome O] [--caller C] [--state S] [--limit N] [--json]
    hyperloom request ID [--json]
    hyperloom latency [--by model|route|caller] [--profile P] [--range S] [--json]
    hyperloom engine-panel [--profile P] [--json]
    hyperloom speculative [--profile P] [--json]
    hyperloom sessions [--profile P] [--json]
    hyperloom observe-settings [--affinity-idle-s N] [--retention-days N]

The read commands take one sample into a store of their own (nothing is written anywhere): the router's current
request ring and every engine's current status. History, counter deltas and loopback-bound engines (read by the
portal's node sampler) need the running portal.
"""

import json
import sys


ONE_SHOT = ("one-shot sample (CLI): the router's current request ring and the engines' current status; history and "
            "counter deltas need the portal's collectors; loopback-bound engines come from the portal's last node sample")


def _api():
    from hyperloom import cli
    from hyperloom.web.api import API
    return API(cli.config_path())


def _fleet_documents(api, store):
    """Copy the node sampler's engine documents from the portal's store file, opened read-only (never created,
    never written), into the CLI's own store: loopback-bound engines are read only by that sampler (C4). Their
    age is shown like the portal shows it."""
    import sqlite3
    path = api.config_path.parent / "telemetry.sqlite3"
    if not path.exists():
        return 0
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT key, value FROM docs WHERE namespace='fleet/engine' LIMIT 64").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return 0
    for key, value in rows:
        try:
            store.put("fleet/engine", key, json.loads(value))
        except ValueError:
            continue
    return len(rows)


def sample():
    """(api, store) after one collection of both collectors into an in-memory store."""
    from hyperloom.telemetry.store import Store
    from hyperloom.web.features import observe
    api = _api()
    store = Store(":memory:", clock=api.clock)
    api.telemetry = store
    _fleet_documents(api, store)
    now = api.clock()
    observe.collect_router(api, now, store)
    observe.collect_engines(api, now, store)
    return api, store


def _fmt(value, digits=0):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _print_why(document):
    for key, why in sorted((document.get("why") or {}).items()):
        if why:
            print(f"  {key}: {why}" if key in ("coverage", "sessions_cut") else f"  {key}: unavailable · {why}")


def _table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def _emit(args, document, render):
    if args.json:
        json.dump(document, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    print(ONE_SHOT)
    render(document)
    _print_why(document)
    return 0


def run_requests(args):
    from hyperloom.web.features import observe
    api, store = sample()
    query = {k: v for k, v in (("profile", args.profile), ("model", args.model), ("route", args.route),
                               ("outcome", args.outcome), ("caller", args.caller), ("state", args.state),
                               ("before", None if args.before is None else str(args.before)),
                               ("limit", None if args.limit is None else str(args.limit))) if v is not None}
    document = observe.requests_document(api, query, store)

    def render(doc):
        rows = []
        for row in doc.get("requests") or []:
            engine = row.get("engine") or {}
            rows.append([row["id"], row.get("profile"), row.get("state") or "-", row.get("outcome") or "-",
                         _fmt(row.get("model")), _fmt(row.get("route")), _fmt(row.get("caller")), _fmt(row.get("replica")),
                         _fmt(row["router"].get("ttft_ms"), 1), _fmt(engine.get("ttft_ms"), 1),
                         _fmt(row["router"].get("total_ms"), 1), _fmt(engine.get("prompt_tokens")),
                         _fmt(engine.get("cached_tokens")), _fmt(engine.get("imported_tokens")),
                         _fmt(engine.get("computed_tokens"))])
        _table(["id", "profile", "state", "outcome", "model", "route", "caller", "replica", "ttft ms (router)",
                "ttft ms (engine)", "total ms (router)", "prompt", "cached", "imported", "computed"], rows)
    return _emit(args, document, render)


def run_request(args):
    from hyperloom.web.features import observe
    api, store = sample()
    try:
        document = observe.request_document(api, args.id, {}, store)
    except (LookupError, ValueError) as exc:
        print("hyperloom: " + str(exc).strip("'\""), file=sys.stderr)
        return 2
    if args.json:
        return _emit(args, document, None)
    print(ONE_SHOT)
    row = document["request"]
    print(f"{row['id']} · {row.get('state')} · {row.get('outcome') or '-'} · {row.get('model') or '-'} · route "
          f"{row.get('route') or '-'} · caller {row.get('caller') or '-'}")
    print("router-observed (ms from arrival):")
    for phase in row["router"]["phases"]:
        print(f"  {phase['phase']:<18} {phase['start_ms']:>10.1f} -> {phase['end_ms']:>10.1f}")
    for attempt in row.get("attempts") or []:
        print(f"  attempt {attempt.get('n')} {attempt.get('kind')} -> {attempt.get('target')}: {attempt.get('outcome')}"
              + (f" ({attempt['why']})" if attempt.get("why") else ""))
    for block in (row.get("engine"), row.get("service")):
        if block:
            print(f"{block['observer']}-observed: " + ", ".join(f"{k} {_fmt(block.get(k), 1)}" for k in
                  ("queue_ms", "prefill_ms", "decode_ms", "ttft_ms", "computed_tokens") if k in block))
    _print_why(row)
    return 0


def run_latency(args):
    from hyperloom.web.features import observe
    api, store = sample()
    query = {"by": args.by, **({"profile": args.profile} if args.profile else {}),
             **({"range": str(args.range)} if args.range else {})}
    document = observe.latency_document(api, query, store)

    def render(doc):
        rows = []
        for group in doc.get("groups") or []:
            for observer in ("router", "engine"):
                metrics = group["metrics"]
                cells = [group["group"], observer]
                for key in ("ttft_ms", "queue_ms", "total_ms", "decode_tok_s"):
                    q = metrics[key][observer]
                    cells.append("-" if not q["n"] else f"{_fmt(q['p50'], 1)}/{_fmt(q['p95'], 1)}/{_fmt(q['p99'], 1)} (n {q['n']})")
                rows.append(cells)
        _table([args.by, "observer", "ttft ms p50/p95/p99", "queue ms", "total ms", "decode tok/s"], rows)
        for item in doc.get("histograms") or []:
            if item.get("family") == "_cut" or "count" not in item:
                print(f"{item['observer']} {item.get('profile') or ''}: {item.get('basis') or 'histograms cut'}")
                continue
            print(f"{item['observer']} {item['family']} {item.get('labels') or {}}: n {_fmt(item['count'])}, p50 "
                  f"{_fmt(item['p50'], 3)} p95 {_fmt(item['p95'], 3)} ({item['basis']})")
    return _emit(args, document, render)


def run_engines(args):
    from hyperloom.web.features import observe
    api, store = sample()
    document = observe.engines_document(api, {"profile": args.profile} if args.profile else {}, store)

    def render(doc):
        for row in doc.get("engines") or []:
            slots = row.get("slots") or {}
            accounting = row["prompt_accounting"]["since_armed"]
            print(f"{row['profile']}/{row['replica']} · {row.get('state')} · {row.get('model') or '-'} · instance "
                  f"{row.get('instance')} ({row.get('instance_basis')}) · via {row.get('via')}")
            print(f"  slots {_fmt(slots.get('busy'))}/{_fmt(slots.get('total'))} · context {_fmt(row.get('context_tokens'))}"
                  f" · queue {row.get('queue') or '-'} · generated {_fmt(row.get('generated_tokens'))}")
            print(f"  since armed: prompt {_fmt(accounting['prompt_tokens'])} = computed {_fmt(accounting['computed_tokens'])} + cached "
                  f"{_fmt(accounting['cached_tokens'])} + imported {_fmt(accounting['imported_tokens'])}")
            for key, why in sorted(row.get("why", {}).items()):
                print(f"  {key}: unavailable · {why}")
    return _emit(args, document, render)


def run_speculative(args):
    from hyperloom.web.features import observe
    api, store = sample()
    document = observe.speculative_document(api, {"profile": args.profile} if args.profile else {}, store)

    def render(doc):
        for row in doc.get("engines") or []:
            print(f"{row['profile']}/{row['replica']} · {row.get('method') or '-'} · accepted {_fmt(row.get('accepted'))} of "
                  f"{_fmt(row.get('proposed'))} ({_fmt(row.get('acceptance'), 3)}) · per cycle "
                  f"{_fmt(row.get('accepted_per_cycle'), 2)} · {row.get('basis') or ''}")
            for depth in row.get("depth") or []:
                print(f"  depth {depth['depth']}: reached {_fmt(depth.get('reached'))}, hit {_fmt(depth.get('hit'))}")
    return _emit(args, document, render)


def run_sessions(args):
    from hyperloom.web.features import observe
    api, store = sample()
    document = observe.sessions_document(api, {"profile": args.profile} if args.profile else {}, store)

    def render(doc):
        print(doc.get("label"))
        _table(["key", "profile", "endpoint", "turns", "replica", "last seen", "reused per turn"],
               [[row["key"], row["profile"], _fmt(row.get("endpoint")), _fmt(row.get("turns")), _fmt(row.get("replica")),
                 _fmt(row.get("last_seen"), 0), ",".join(_fmt(v) for v in row.get("reused_per_turn") or [])]
                for row in doc.get("sessions") or []])
    return _emit(args, document, render)


def run_settings(args):
    from hyperloom import telemetry
    from hyperloom.web.features import observe
    api = _api()
    changes = {k: v for k, v in (("affinity_idle_s", args.affinity_idle_s), ("retention_days", args.retention_days))
               if v is not None}
    if not changes:
        store = telemetry.open_store(api)
        print(json.dumps(observe.settings(store), sort_keys=True))
        return 0
    try:
        result = observe.save_settings(api, changes)
    except ValueError as exc:
        api.audit("observe/settings/save", changes, {"state": "refused", "error": str(exc)})
        print("hyperloom: " + str(exc), file=sys.stderr)
        return 2
    api.audit("observe/settings/save", changes, {"state": "succeeded", "result": result})
    print(json.dumps(result["settings"], sort_keys=True))
    return 0


def add(commands):
    common = {"help": "print the document as JSON"}
    requests = commands.add_parser("requests", help="recent and in-flight requests with router and engine timings")
    for flag in ("--profile", "--model", "--route", "--outcome", "--caller", "--state"):
        requests.add_argument(flag)
    requests.add_argument("--limit", type=int)
    requests.add_argument("--before", type=float, help="only requests that arrived before this epoch time")
    requests.add_argument("--json", action="store_true", **common)
    requests.set_defaults(ext_run=run_requests)
    request = commands.add_parser("request", help="one request: every phase and attempt")
    request.add_argument("id")
    request.add_argument("--json", action="store_true", **common)
    request.set_defaults(ext_run=run_request)
    latency = commands.add_parser("latency", help="TTFT, queue, total and decode rate percentiles per model/route/caller")
    latency.add_argument("--by", choices=("model", "route", "caller"), default="model")
    latency.add_argument("--profile")
    latency.add_argument("--range", type=int)
    latency.add_argument("--json", action="store_true", **common)
    latency.set_defaults(ext_run=run_latency)
    panel = commands.add_parser("engine-panel", help="per engine instance: slots, queue, caches, prompt accounting")
    panel.add_argument("--profile")
    panel.add_argument("--json", action="store_true", **common)
    panel.set_defaults(ext_run=run_engines)
    spec = commands.add_parser("speculative", help="speculative decoding acceptance per engine")
    spec.add_argument("--profile")
    spec.add_argument("--json", action="store_true", **common)
    spec.set_defaults(ext_run=run_speculative)
    sessions = commands.add_parser("sessions", help="observed conversation affinity (not confirmed cache residency)")
    sessions.add_argument("--profile")
    sessions.add_argument("--json", action="store_true", **common)
    sessions.set_defaults(ext_run=run_sessions)
    settings = commands.add_parser("observe-settings", help="conversation expiry and request-record retention")
    settings.add_argument("--affinity-idle-s", type=int)
    settings.add_argument("--retention-days", type=int)
    settings.set_defaults(ext_run=run_settings)
