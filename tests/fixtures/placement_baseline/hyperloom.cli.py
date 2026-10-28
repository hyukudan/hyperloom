# Frozen function sources from main 8539c28; executed with the current module globals.

def _admission_hold(args):
    """Contract C11 for direct CLI use: a deploy/start/install takes a "core" hold (refused while a benchmark
    holds its resources), a stop a "stop" hold (cancels overlapping benchmarks). Returns a release function."""
    kind = {"up": "core", "install": "core", "down": "stop"}.get(args.command)
    if args.command == "recipe" and getattr(args, "operation", None) in ("deploy", "probe"):
        kind = "core"
    if kind is None or (args.command in ("up", "down") and args.profile is None
                        and not (getattr(args, "recipe", None) or getattr(args, "recipe_inputs", None))):
        return lambda: None
    admission = _module("hyperloom.admission")
    resources = {"*"}
    # A snapshot (--recipe-inputs) launches with the placement it was planned on: it holds everything.
    if getattr(args, "profile", None) and not getattr(args, "recipe_inputs", None):
        try:
            cluster = _cluster(getattr(args, "config", None) or config_path())
            resources = admission.profile_resources(cluster, args.profile, _module("hyperloom.orchestrate"),
                                                    getattr(args, "router", None) if args.command == "up" else None)
        except Exception:
            resources = {"*"}
    # The hold lives next to the configuration this command executes with (the portal's jobs pass the same one).
    try:
        database = admission.for_config(getattr(args, "config", None) or config_path())
        hold = database.acquire(kind, resources, label=f"cli {args.command} {getattr(args, 'profile', '') or ''}".strip())
    except admission.Refused as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - sqlite3.Error, OSError: the shared admission cannot be used
        if kind != "stop":
            raise RuntimeError(f"admission unavailable ({type(exc).__name__}: {exc}); nothing started") from exc
        # A stop never depends on the admission: it goes ahead (benchmarks of other processes are not cancelled).
        print(f"hyperloom: admission unavailable ({type(exc).__name__}: {exc}); stopping without its hold",
              file=sys.stderr)
        return lambda: None
    import threading
    stop = threading.Event()

    def renew():
        while not stop.wait(30):
            try:
                database.renew(hold)
            except Exception:  # noqa: BLE001 - a transient database error: retried at the next renewal
                pass
    try:
        threading.Thread(target=renew, name="hyperloom-cli-admission", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - e.g. can't start new thread: the hold is given back
        try:
            database.release(hold)
        except Exception:  # noqa: BLE001 - its lease lapses by itself
            pass
        if kind != "stop":
            raise RuntimeError(f"admission hold could not be kept ({type(exc).__name__}: {exc}); nothing started") from exc
        print(f"hyperloom: the admission hold could not be kept ({type(exc).__name__}: {exc}); stopping without it",
              file=sys.stderr)
        return lambda: None

    def release():
        # Runs in main's finally: a failing release never turns a finished command (a done down) into a failure;
        # the hold's lease then lapses by itself.
        stop.set()
        try:
            database.release(hold)
        except Exception as exc:  # noqa: BLE001
            print(f"hyperloom: the admission hold could not be released ({type(exc).__name__}: {exc}); "
                  "it lapses with its lease", file=sys.stderr)
    return release


def main(argv=None):
    args = build_parser().parse_args(argv)
    release = lambda: None
    try:
        if getattr(args, "ext_run", None) is not None:
            return args.ext_run(args)
        release = _admission_hold(args)
        if args.command == "ui":
            if not 0 <= args.port <= 65535:
                raise ValueError("ui port must be in 0..65535")
            _module("hyperloom.web.server").serve(args.port, no_browser=args.no_browser)
        elif args.command in ("up", "down") and args.profile is None and not (
                getattr(args, "recipe", None) or getattr(args, "recipe_inputs", None)):
            print("Phase 1a")
        elif args.command == "install":
            cluster = _cluster(args.config)
            orchestrate = _module("hyperloom.orchestrate")
            _, kind, mids = orchestrate._profile(cluster, args.profile)
            targets = list(_module("hyperloom.engines").adapter(kind).targets)
            sources = _module("hyperloom.bundles").parse_all(args.source)
            _json({"release_id": orchestrate.install(cluster, args.build_on or mids[0], targets, profile=args.profile,
                                                     sources=sources)})
        elif args.command == "nodes":
            _json(_module("hyperloom.orchestrate").nodes_update(_cluster(args.config), args.profile,
                                                                machine=args.machine, force=args.force))
        elif args.command == "assets" and args.operation in ("sources", "download", "job", "cancel"):
            downloads = _module("hyperloom.downloads")
            cluster = _cluster(args.config)
            if args.operation == "sources":
                _json(downloads.sources(cluster, args.profile))
            elif args.operation in ("job", "cancel"):
                if args.machine not in cluster.machines:
                    raise ValueError("unknown machine: " + args.machine)
                _json(downloads.job(cluster, args.machine, args.job, cancel=args.operation == "cancel"))
            elif args.confirm is None:
                _json(downloads.plan(cluster, args.profile, args.asset, args.machine))
            else:
                _json(downloads.start(cluster, args.profile, args.asset, args.machine, args.confirm))
        elif args.command == "assets":
            cluster = _cluster(args.config)
            assets = _module("hyperloom.assets")
            if args.operation == "verify":
                _json(assets.verify(cluster, args.profile, include_large=args.include_large, machine=args.machine))
            else:
                _json(assets.status(cluster, args.profile))
        elif args.command == "up":
            _json(up_command(args))
        elif args.command == "down":
            _json(_module("hyperloom.orchestrate").down(args.profile, force=args.force))
        elif args.command == "endpoint":
            _json(_module("hyperloom.orchestrate").endpoint(args.profile))
        elif args.command == "status":
            _json(_module("hyperloom.orchestrate").status(args.profile, config_path=_scope(args)))
        elif args.command == "logs":
            _json(_module("hyperloom.orchestrate").logs(args.profile, args.process, args.tail,
                                                        config_path=_scope(args), unknown=True))
        elif args.command == "metrics":
            exporter = _module("hyperloom.metrics_export")
            if args.operation == "dump":
                sys.stdout.write(exporter.collect(profile=args.profile, config_path=_scope(args)))
            else:
                exporter.serve(args.listen, args.profile, config_path=_scope(args))
        elif args.command == "hosts":
            _json(_hosts(args))
        elif args.command == "service":
            result = _module("hyperloom.service").run(_cluster(args.config), args.operation, args.profile,
                                                      enable=args.enable)
            _json(result)
            if not result["ok"]:
                return 1
        elif args.command == "keys":
            if args.key_operation == "export":
                _json(_module("hyperloom.orchestrate").export_key(args.profile, args.out))
            else:
                _module("hyperloom.secrets").rotate(args.name)
                _json({"name": args.name, "rotated": True})
        elif args.command == "_up-watch":
            _module("hyperloom.orchestrate").up_watch(args.path, args.token)
        elif args.command == "_up-router":
            _module("hyperloom.orchestrate").local_router_run(args.spec)
        elif args.command == "_prefill-run":
            _module("hyperloom.rtx").service_run(args.spec)
        elif args.command == "prefill":
            _json(_module("hyperloom.rtx").command(args.operation, _cluster(args.config), args.profile,
                                                   prompts=args.prompts, source=args.source,
                                                   attach=args.attach, standalone=args.standalone,
                                                   vram_cap_gib=args.vram_cap_gib))
        elif args.command == "vramplan":
            _json(_vramplan(args))
        elif args.command == "recipe":
            service = _module("hyperloom.recipes.service")
            try:
                result, ok = _module("hyperloom.recipes.commands").run(_cluster, args)
            except (service.NotFound, service.Conflict) as exc:
                # Own exit codes so callers (the portal) tell "no such recipe" and "changed / taken" apart.
                print(f"hyperloom: {exc}", file=sys.stderr)
                return 3 if isinstance(exc, service.NotFound) else 4
            if getattr(args, "format", "json") == "text":
                print(_module("hyperloom.recipes.commands").text(args, result))
            else:
                _json(result)
            if not ok:
                return 1
        elif args.command == "config":
            _cluster(args.path)
            _json({"valid": True})
        elif args.command == "render":
            cluster = _cluster(args.config)
            renderer = _module("hyperloom.render")
            rendered = renderer.render_router(cluster, args.profile)
            renderer.validate_router(rendered)
            result = {"router": rendered, "processes": [asdict(spec) for spec in
                                                        renderer.render_processes(cluster, args.profile)],
                      "launch": renderer.launch_summary(cluster, args.profile)}
            if "prefill_service" in cluster.profiles[args.profile]:
                result["prefill_service"] = _module("hyperloom.rtx").render_preview(cluster, args.profile)
            _json(result)
        elif args.command == "doctor":
            _doctor(args)
        elif args.command == "_router-run":
            _router_run(args)
        elif args.operation == "start":
            _router_start(args)
        elif args.operation == "stop":
            _router_stop()
        else:
            state = _state(_runtime())
            state["status"] = "running" if _active(_runtime()) else "stopped"
            _json(state)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"hyperloom: {exc}", file=sys.stderr)
        return 1
    finally:
        release()
