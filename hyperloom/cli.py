"""Command line and local router controllers."""

import argparse
from dataclasses import asdict
import importlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from . import cliext
from .paths import repo_root
from .winproc import no_window, retry_sharing


_controllers = []


def config_path():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "hyperloom" / "cluster.json"
    return Path.home() / ".config" / "hyperloom" / "cluster.json"


def _scope(args):
    """The configuration whose launches a status, logs or metrics command shows (None: every configuration's)."""
    return None if args.all_configs else args.config


def _module(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f"{name} is unavailable; complete the hyperloom installation ({exc})") from exc


def _json(obj):
    print(json.dumps(obj, indent=2))


def _cluster(path):
    config = _module("hyperloom.config")
    return _module("hyperloom.orchestrate").load_cluster(path, config)


def _write(path, obj):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        # The router controller and the CLI read and replace the same state files.
        retry_sharing(lambda: temporary.replace(path))
    finally:
        temporary.unlink(missing_ok=True)


def _runtime():
    return config_path().parent / "run"


def _active(directory):
    procs = _module("hyperloom.procs")
    lock = procs.InstanceLock(directory / "router.lock")
    try:
        lock.acquire()
    except procs.ProcessError:
        return True
    lock.close()
    return False


def _state(directory):
    try:
        return retry_sharing(lambda: json.loads((directory / "router-state.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def _router_env():
    env = dict(os.environ)
    source = repo_root() / "third_party" / "tensormeld" / "src"
    if source.is_dir():
        env["PYTHONPATH"] = str(source)
    else:
        _module("tensormeld")
        env.pop("PYTHONPATH", None)
    return env


def _endpoint(rendered):
    listen = rendered["listen"]
    address, port = listen["address"], listen["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("router start requires a fixed port in 1..65535")
    ipaddress.ip_address(address)
    return address, port


def _url(endpoint, path):
    address, port = endpoint
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{port}{path}"


def _get_json(endpoint, path):
    request = Request(_url(endpoint, path))
    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("router response exceeds 1 MiB")
    return json.loads(body)


def _router_start(args):
    cluster = _cluster(args.config)
    renderer = _module("hyperloom.render")
    profiles = list(cluster.profiles)
    profile = args.profile or (profiles[0] if len(profiles) == 1 else None)
    if profile is None:
        raise ValueError("choose a router profile with --profile")
    rendered = renderer.render_router(cluster, profile)
    renderer.validate_router(rendered)
    _endpoint(rendered)
    router_env = _router_env()
    directory = _runtime()
    directory.mkdir(parents=True, exist_ok=True)
    if _active(directory):
        raise RuntimeError("router is already running")
    token = uuid.uuid4().hex
    rendered_path = directory / ("router-" + token + ".json")
    rendered_path.write_text(json.dumps(rendered), encoding="utf-8")
    argv = [str(Path(sys.executable).absolute()), "-m", "hyperloom", "_router-run",
            str(directory), str(rendered_path), token]
    # The controller imports hyperloom from this checkout or from the installed package.
    controller_env = dict(router_env)
    controller_env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root()), router_env.get("PYTHONPATH", "")])
    options = {"start_new_session": True}
    if os.name == "nt":
        # A hidden console of its own (never shown, even with Windows Terminal as the default host);
        # the router child shares it, so CTRL_BREAK from this helper still reaches it.
        options = no_window()
    with (directory / "controller.log").open("wb") as log:
        helper = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  env=controller_env, **options)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = _state(directory)
            if state.get("token") == token and state.get("status") == "running":
                _controllers.append(helper)
                _json(state)
                return
            if helper.poll() is not None:
                raise RuntimeError("router failed to start; see " + str(directory / "controller.log"))
            time.sleep(0.1)
        raise RuntimeError("router start timed out; see " + str(directory / "controller.log"))
    except BaseException:
        (directory / (token + ".stop")).touch()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait(timeout=5)
        raise
    finally:
        # The helper owns its rendered file once launched.
        if helper.poll() is not None:
            rendered_path.unlink(missing_ok=True)


def _router_run(args):
    procs = _module("hyperloom.procs")
    directory, path = Path(args.directory), Path(args.rendered)
    rendered = json.loads(path.read_text(encoding="utf-8"))
    endpoint = _endpoint(rendered)
    child = procs.LocalChild(
        [str(Path(sys.executable).absolute()), "-m", "tensormeld", "serve-replicas", str(path)],
        _router_env(), directory, "router", port=endpoint)
    stop_path = directory / (args.token + ".stop")
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    state = {"token": args.token, "controller_pid": os.getpid(), "profile_config": str(path)}
    started = False
    try:
        child.start()
        started = True
        state["pid"] = child.process.pid
        state["status"] = "starting"
        _write(directory / "router-state.json", state)
        deadline = time.monotonic() + 10
        while child.alive() and not interrupted and not stop_path.exists():
            try:
                status = _get_json(endpoint, "/api/status")
                if status.get("schema") != "tensormeld/replica-router-status-v1":
                    raise ValueError("unexpected router status schema")
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("router status did not become available")
                time.sleep(0.1)
        if not child.alive():
            raise RuntimeError("router exited before readiness")
        if interrupted or stop_path.exists():
            return
        state["status"] = "running"
        _write(directory / "router-state.json", state)
        while child.alive() and not interrupted and not stop_path.exists():
            time.sleep(0.1)
        try:
            _write(directory / "router-final.json", _get_json(endpoint, "/api/status"))
        except (OSError, ValueError):
            pass
    finally:
        if started:
            state["status"] = "stopped"
            # Write before releasing the instance lock, so a successor's state is safe.
            try:
                _write(directory / "router-state.json", state)
            finally:
                child.stop(5)
        path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)


def _router_stop():
    directory = _runtime()
    if not _active(directory):
        _json({"status": "stopped"})
        return
    state = _state(directory)
    token = state.get("token")
    if not token or not isinstance(token, str) or len(token) != 32 or any(
            ch not in "0123456789abcdef" for ch in token):
        raise RuntimeError("router controller is starting; retry stop shortly")
    (directory / (token + ".stop")).touch()
    deadline = time.monotonic() + 12
    while _active(directory):
        if time.monotonic() >= deadline:
            raise RuntimeError("router controller did not stop; see controller.log")
        time.sleep(0.1)
    for helper in _controllers[:]:
        helper.poll()
        if helper.returncode is not None:
            _controllers.remove(helper)
    _json({"status": "stopped"})


def _doctor(args):
    if args.machine:
        cluster = _cluster(args.config)
        try:
            machine = cluster.machines[args.machine]
        except KeyError as exc:
            raise ValueError("unknown machine: " + args.machine) from exc
        if machine.access != "local":
            if machine.access != "ssh":
                raise ValueError("agent access is a later phase")
            remote = _module("hyperloom.remote")
            client = remote.Remote(machine, config_path().parent / "known_hosts")
            # Read-only: the node tool reaches a node only through `hyperloom nodes update` (which owns the
            # live-lease guard); a machine without it, or with a stale one, is reported here, never changed.
            tool = {"expected": remote.tool_digest()}
            if not client.has_tool(timeout=60):
                _json({"node_tool": {**tool, "deployed": None, "status": "absent",
                                     "hint": f"run 'hyperloom nodes update <profile> --machine {args.machine}' "
                                             "(a machine without the tool has no lease to protect)"}})
                return
            version = client.call("version", {}, timeout=60)
            tool["deployed"] = ((version.get("checks") or {}).get("tool") or {}).get("value")
            tool["status"] = "current" if tool["deployed"] == tool["expected"] else "stale"
            if tool["status"] == "stale":
                tool["hint"] = f"run 'hyperloom nodes update <profile> --machine {args.machine}'"
            result = client.call("doctor", {}, timeout=60)
            if isinstance(result, dict):
                result["node_tool"] = tool
            _json(result)
            return
    script = repo_root() / "node" / "hyperloom-node"
    if not script.is_file():
        raise RuntimeError("node/hyperloom-node is unavailable; complete the hyperloom installation")
    result = subprocess.run([str(Path(sys.executable).absolute()), str(script), "doctor"],
                            input="{}", text=True, capture_output=True, timeout=60, **no_window())
    if result.returncode:
        raise RuntimeError("local doctor failed: " + result.stderr.strip())
    _json(json.loads(result.stdout))


def _hosts(args):
    """Scan never authorizes; pin stores only the key whose fingerprint the operator confirmed."""
    remote = _module("hyperloom.remote")
    orchestrate = _module("hyperloom.orchestrate")
    cluster = _cluster(args.config)
    if args.machine not in cluster.machines:
        raise ValueError(f"unknown machine: {args.machine}")
    machine = cluster.machines[args.machine]
    candidates = remote.keyscan(machine.http_address, machine.ssh_port)
    found = [{"algorithm": k.algorithm, "fingerprint": k.sha256_fingerprint} for k in candidates]
    if args.operation == "scan":
        return {"machine": machine.id, "address": machine.http_address, "candidates": found}
    if not args.fingerprint:
        raise ValueError("pin requires --fingerprint (compare with ssh-keygen -lf on the machine)")
    match = [k for k in candidates if k.sha256_fingerprint == args.fingerprint]
    if not match:
        raise ValueError("no scanned host key has that fingerprint; nothing pinned")
    # Remote clients key their pinned entry on the machine id (HostKeyAlias).
    remote.KnownHosts(orchestrate._known_hosts()).pin(machine.id, machine.http_address, machine.ssh_port, match[0])
    return {"machine": machine.id, "pinned": match[0].sha256_fingerprint, "algorithm": match[0].algorithm}


VRAMPLAN_ENGINES = ("minimax-rtx-prefill", "qwen-rtx-prefill", "ds41-rtx-prefill", "ds41-rtx-front", "ds41-rtx-hot",
                    "ds41-node", "desc-v1")


def _vramplan(args):
    vramplan, placement = _module("hyperloom.vramplan"), _module("hyperloom.placement")
    params = {}
    if args.engine.endswith("-prefill"):
        if args.positions is None:
            raise ValueError(f"{args.engine} needs --positions")
        params["positions"] = args.positions
    if args.layers is not None:
        if args.engine != "minimax-rtx-prefill":
            raise ValueError("--layers is the minimax-rtx-prefill validation subset only")
        params["layers"] = args.layers
    if args.placement_out and args.engine not in ("ds41-rtx-hot", "desc-v1"):
        raise ValueError("--placement-out is written for ds41-rtx-hot and desc-v1 only")
    if args.engine == "desc-v1":
        params.update(workspace_bytes=args.workspace_bytes, ring_slots=args.ring_slots,
                      dynamic_slots_per_layer=args.dynamic_slots, positions=args.positions)
    if args.mtp:
        params["mtp_gguf"] = args.mtp
    if args.engine in ("ds41-rtx-hot", "ds41-node"):
        if not args.routes:
            raise ValueError(f"{args.engine} needs --routes")
        params["routes"] = args.routes
    if args.engine == "ds41-node":
        if args.mode is None or args.fixed_gib is None:
            raise ValueError("ds41-node needs --mode and --fixed-gib")
        params.update(mode=args.mode, fixed_gib=args.fixed_gib)
        if args.mode != "replica":
            params["rest"] = args.rest
    inputs = vramplan.Inputs(args.vram_cap_gib, host_gib=args.host_gib, margin_gib=args.margin_gib)
    plan = placement.plan(args.engine, args.model, inputs, **params)
    result = dict(plan.to_dict(), explain=vramplan.explain(plan).splitlines())
    if args.engine == "desc-v1" and args.placement_out:
        desc = _module("hyperloom.placement.desc")
        args.placement_out.write_text(json.dumps(desc.plan_v1(args.model, plan)), encoding="utf-8")
        result["flags"] = {"plan-v1": str(args.placement_out)}
    if args.engine == "ds41-rtx-hot" and args.placement_out:
        if not args.expert_servers:
            raise ValueError("--placement-out needs --expert-servers RANK0,RANK1 (machine ids)")
        ds41 = _module("hyperloom.placement.ds41")
        budget = round(args.vram_cap_gib - plan.margin_bytes / vramplan.GIB, 3)
        ds41.write_placement(args.placement_out, ds41.placement_json(
            plan, model_file=Path(args.model).name, budget_gib=budget, servers=args.expert_servers.split(",")))
        result["flags"] = {"placement": str(args.placement_out)}
    return result


def _version():
    from . import __version__
    try:
        release = json.loads((repo_root() / "RELEASE.json").read_text(encoding="utf-8"))
        return f"hyperloom {__version__} ({release['name']}, commit {release['commit'][:12]})"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return f"hyperloom {__version__} (checkout)"


def up_command(args, *, orchestrate=None, load_cluster=None):
    """`hyperloom up PROFILE ...` from its parsed arguments; the portal's in-process path calls this too."""
    # The launch state's configuration, the recipe directories and a job's cancel files derive from --config for this
    # call only (the portal runs requests on other threads).
    with _module("hyperloom.recipes.service").configured(args.config):
        return _up_command(args, orchestrate, load_cluster)


def _up_command(args, orchestrate, load_cluster):
    orchestrate = orchestrate or _module("hyperloom.orchestrate")
    approval = _module("hyperloom.recipes.commands")._approval(args)
    cluster = None if args.recipe_inputs is not None else (load_cluster or _cluster)(args.config)
    from .recipes import preflight
    preflight.cli(args, load_cluster=lambda _: cluster)
    recipe = None
    if args.recipe_inputs is not None:
        service = _module("hyperloom.recipes.service")
        pin = service.acquire_pin(args.recipe_inputs)         # before the snapshot is read
        try:
            cluster, intent, recipe = service.options_from_inputs(args.recipe_inputs, args.profile)
        except BaseException:
            service.release_pin(pin)
            raise
        recipe.pin = pin
        args.profile, args.deadline, args.queue_timeout = intent["profile"], intent["deadline_s"], intent["queue_timeout_s"]
    elif args.recipe is not None:
        reader = _module("hyperloom.recipes.reader")
        layers = _module("hyperloom.recipes.session").Layers(
            profile=args.recipe_profile, overrides=list(args.recipe_overrides or []),
            sets=[reader.parse_set(s) for s in args.recipe_set or []],
            workload=args.recipe_workload or "default")
        service = _module("hyperloom.recipes.service")
        cluster, intent, recipe = service.up_options(
            cluster, args.recipe, args.profile, layers=layers, dirs=list(args.recipe_dir or []),
            approval=approval,
            intent={"router": args.router, "deadline_s": args.deadline, "queue_timeout_s": args.queue_timeout})
        args.profile = intent["profile"]
    launch = lambda cancelled=None: orchestrate.up(  # noqa: E731
        cluster, args.profile, router=args.router, nodes=args.node, devices=args.device, deadline_s=args.deadline,
        approval=approval, queue_timeout_s=args.queue_timeout, diagnostics=args.diagnostic,
        vram_cap_gib=args.vram_cap_gib, recipe=recipe, **({"cancelled": cancelled} if cancelled else {}))
    try:
        if getattr(args, "job", None):
            # A portal job (with a recipe or without): its cancel marker is checked right after up owns the profile
            # (the hand-over), and its progress tells the cancel whether the launch had begun (then the cancel stops
            # the profile).
            with _module("hyperloom.recipes.service").launch_progress(args.job, "up", args.profile) as progress:
                return launch(progress.handoff)
        return launch()
    finally:
        if recipe is not None:
            _module("hyperloom.recipes.service").release_pin(recipe.pin)


def build_parser(parser_class=argparse.ArgumentParser):
    """The command line; the portal API parses its argv with the same parser (one code path)."""
    parser = parser_class(prog="hyperloom")
    parser.add_argument("--version", action="version", version=_version())
    commands = parser.add_subparsers(dest="command", required=True)
    ui = commands.add_parser("ui", help="open the local portal")
    ui.add_argument("--port", type=int, default=18700)
    ui.add_argument("--no-browser", action="store_true")
    config = commands.add_parser("config").add_subparsers(dest="operation", required=True)
    config.add_parser("validate").add_argument("path", type=Path)
    render = commands.add_parser("render")
    render.add_argument("profile")
    render.add_argument("--config", type=Path, default=config_path())
    doctor = commands.add_parser("doctor")
    doctor.add_argument("machine", nargs="?")
    doctor.add_argument("--config", type=Path, default=config_path())
    router = commands.add_parser("router")
    router.add_argument("operation", choices=("start", "stop", "status"))
    router.add_argument("--config", type=Path, default=config_path())
    router.add_argument("--profile")
    install = commands.add_parser("install")
    install.add_argument("profile")
    install.add_argument("--build-on")
    install.add_argument("--source", action="append", default=[], metavar="NAME=DIR[@COMMIT]",
                         help="build component NAME (engine, strix-trellis) from a local repository: its pinned commit "
                              "is shipped as a git bundle instead of being fetched from the remote")
    install.add_argument("--config", type=Path, default=config_path())
    assets = commands.add_parser("assets", help="verify assets, inspect the integrity cache, or run pinned downloads")
    asset_commands = assets.add_subparsers(dest="operation", required=True)
    for operation in ("verify", "status"):
        asset_command = asset_commands.add_parser(operation)
        asset_command.add_argument("profile")
        asset_command.add_argument("--config", type=Path, default=config_path())
        if operation == "verify":
            asset_command.add_argument("--include-large", action="store_true")
            asset_command.add_argument("--machine")
    asset_sources = asset_commands.add_parser("sources", help="show each asset's pinned source (read-only)")
    asset_sources.add_argument("profile")
    asset_sources.add_argument("--config", type=Path, default=config_path())
    asset_download = asset_commands.add_parser("download", help="show the exact download plan; run it only with --confirm")
    asset_download.add_argument("profile")
    asset_download.add_argument("asset")
    asset_download.add_argument("--machine", required=True)
    asset_download.add_argument("--confirm", help="the code printed by the plan; nothing runs without it")
    asset_download.add_argument("--config", type=Path, default=config_path())
    for name in ("job", "cancel"):
        asset_job = asset_commands.add_parser(name, help="a download job's state" if name == "job" else "cancel a download job")
        asset_job.add_argument("machine")
        asset_job.add_argument("job")
        asset_job.add_argument("--config", type=Path, default=config_path())
    nodes = commands.add_parser("nodes", help="node tool maintenance")
    node_commands = nodes.add_subparsers(dest="operation", required=True)
    update = node_commands.add_parser("update", help="redeploy the node tool to the profile's machines")
    update.add_argument("profile")
    update.add_argument("--machine")
    update.add_argument("--force", action="store_true", help="also while a lease is live")
    update.add_argument("--config", type=Path, default=config_path())
    up = commands.add_parser("up")
    # Keep the no-argument Phase 0 probe for existing launchers.
    up.add_argument("profile", nargs="?")
    up.add_argument("--config", type=Path, default=config_path())
    up.add_argument("--router", default="local")
    up.add_argument("--node", action="extend", nargs="+", default=None)
    up.add_argument("--device", action="extend", nargs="+", default=None)
    up.add_argument("--deadline", type=int, default=1140)
    up.add_argument("--queue-timeout", type=int, default=3600, help="seconds to wait for the GPU turn (60..21600)")
    up.add_argument("--approval-ref")
    up.add_argument("--approval-lease", type=int)
    up.add_argument("--approval-end")
    up.add_argument("--vram-cap-gib", type=float, default=None,
                    help="RTX prefill service: plan its placement for this cap (at most the reservation's) and "
                         "guard it there")
    up.add_argument("--diagnostic", action="append", default=None,
                    help="non-production diagnostic from the preset (repeatable); output goes to the node log/")
    up.add_argument("--recipe", help="a recipe id (e.g. qwen3.8-flash-next.lab; then the profile may be omitted), a "
                                     "recipe file, or auto: the most specific recipe that fits this hardware")
    up.add_argument("--recipe-profile", type=Path, help="the autotuner's profile for the recipe (refused when stale)")
    up.add_argument("--recipe-overrides", type=Path, action="append", help="a recipe overrides file (repeatable)")
    up.add_argument("--recipe-set", action="append", metavar="PATH=VALUE", help="one recipe override (repeatable)")
    up.add_argument("--recipe-dir", type=Path, action="append", help="--recipe auto: also search this directory")
    up.add_argument("--recipe-workload", help="the workload class this run serves (default: default)")
    up.add_argument("--recipe-inputs", metavar="DIGEST",
                    help="launch exactly a recipe snapshot that `hyperloom recipe plan` made (its `inputs` digest)")
    up.add_argument("--job", help="a hex id; the file <config dir>/run/deploy-cancel/ID cancels this launch at its "
                                  "hand-over (the portal's cancel writes it)")
    down = commands.add_parser("down")
    down.add_argument("profile", nargs="?")
    down.add_argument("--force", action="store_true")
    down.add_argument("--config", type=Path, default=config_path(),
                      help="the configuration whose admission the stop registers with (as up)")
    commands.add_parser("endpoint").add_argument("profile")
    status = commands.add_parser("status")
    status.add_argument("profile", nargs="?")
    logs = commands.add_parser("logs")
    logs.add_argument("profile")
    logs.add_argument("process")
    logs.add_argument("--tail", type=int, default=100)
    hosts = commands.add_parser("hosts", help="scan or pin a machine's ssh host key")
    hosts.add_argument("operation", choices=("scan", "pin"))
    hosts.add_argument("machine")
    hosts.add_argument("--fingerprint", help="SHA256:... confirmed on the machine itself (required for pin)")
    hosts.add_argument("--config", type=Path, default=config_path())
    metrics = commands.add_parser("metrics", help="Prometheus re-export of router and engine telemetry")
    metrics.add_argument("operation", choices=("dump", "serve"))
    metrics.add_argument("profile", nargs="?")
    metrics.add_argument("--listen", default="127.0.0.1:18701", help="loopback IP:port (serve only)")
    # Launch states of every configuration share one runtime directory: these commands show the --config one's.
    for command in (status, logs, metrics):
        scope = command.add_mutually_exclusive_group()
        scope.add_argument("--config", type=Path, default=config_path(),
                           help="show only the launches of this configuration (default: the default one) and "
                                "those that record none")
        scope.add_argument("--all-configs", action="store_true", help="show every configuration's launches")
    service = commands.add_parser("service", help="non-lab systemd --user units (lab_mode: false only)")
    service.add_argument("operation", choices=("install", "start", "stop", "status", "remove"))
    service.add_argument("profile")
    service.add_argument("--enable", action="store_true", help="install: also enable the unit at login")
    service.add_argument("--config", type=Path, default=config_path())
    keys = commands.add_parser("keys").add_subparsers(dest="key_operation", required=True)
    keys.add_parser("rotate").add_argument("name")
    export = keys.add_parser("export")
    export.add_argument("profile")
    export.add_argument("--out", type=Path, required=True)
    prefill = commands.add_parser("prefill", help="RTX prefill service: device check, CUDA build, fidelity gate")
    prefill.add_argument("operation", choices=("check", "build", "gate", "placement", "plan"))
    prefill.add_argument("profile")
    prefill.add_argument("--config", type=Path, default=config_path())
    prefill.add_argument("--prompts", type=Path, help="gate/placement: reference prompt file(s) directory")
    prefill.add_argument("--source", type=Path, help="build: engine checkout at the preset's pinned commit")
    prefill.add_argument("--standalone", action="store_true", help="gate: launch the service alone, gate it, stop it")
    prefill.add_argument("--attach", type=int, help="gate: an already running service on this loopback port")
    prefill.add_argument("--vram-cap-gib", type=float, help="plan: the cap to plan for (default: the reservation's)")
    vplan = commands.add_parser("vramplan", help="RTX placement for a VRAM cap from the model files (prints the flags)")
    # Literal list (the parser imports nothing); hyperloom.placement.ENGINES is the same set (tested).
    vplan.add_argument("engine", choices=VRAMPLAN_ENGINES)
    vplan.add_argument("--model", required=True, help="model file or directory (headers only are read)")
    vplan.add_argument("--vram-cap-gib", type=float, required=True)
    vplan.add_argument("--host-gib", type=float, help="pinned host budget (required for ds41-rtx-prefill)")
    vplan.add_argument("--positions", type=int, help="prefill engines: prompt / context positions")
    vplan.add_argument("--layers", type=int, help="minimax-rtx-prefill: the engine's --layers subset (validation)")
    vplan.add_argument("--mtp", help="qwen-rtx-prefill: the MTP sidecar GGUF")
    vplan.add_argument("--routes", help="ds41-rtx-hot: routes file (measured routing frequency)")
    vplan.add_argument("--placement-out", type=Path,
                       help="ds41-rtx-hot: the hot-expert placement JSON; desc-v1: the strix-stream plan-v1 JSON")
    vplan.add_argument("--expert-servers", help="ds41-rtx-hot: the two expert-server machine ids (rank 0,rank 1)")
    vplan.add_argument("--mode", choices=("replica", "pair-rank0", "pair-rank1"),
                       help="ds41-node: a full replica, or one rank of the EP pair")
    vplan.add_argument("--fixed-gib", type=float,
                       help="ds41-node: the node's measured non-routed-expert use (engine /api/status memory)")
    vplan.add_argument("--rest", choices=("peer-computes", "disk"), default="peer-computes",
                       help="ds41-node pair: the other half computed by the peer (EP) or streamed from local NVMe")
    vplan.add_argument("--margin-gib", type=float, default=2.0)
    vplan.add_argument("--workspace-bytes", type=int, default=0, help="desc-v1: the engine's workspace on the device")
    vplan.add_argument("--ring-slots", type=int, default=0,
                       help="desc-v1: stream slots per ring (0: the descriptor's, else the default)")
    vplan.add_argument("--dynamic-slots", type=int, default=0, help="desc-v1: dynamic slots per layer")
    recipe = commands.add_parser("recipe", help="recipes: probe the hardware, validate, explain, diff, auto-pick")
    recipe_commands = recipe.add_subparsers(dest="operation", required=True)

    def layered(command):
        command.add_argument("--recipe-profile", type=Path, help="the autotuner's profile (refused when stale)")
        command.add_argument("--recipe-overrides", type=Path, action="append", help="an overrides file (repeatable)")
        command.add_argument("--recipe-set", action="append", metavar="PATH=VALUE", help="one override (repeatable)")
        command.add_argument("--recipe-workload", default="default",
                             help="the workload class this run serves (a profile tuned for another one is stale)")
        command.add_argument("--config", type=Path, default=config_path())

    recipe_probe = recipe_commands.add_parser("probe", help="the profile's hardware facts (read-only)")
    recipe_probe.add_argument("profile")
    recipe_probe.add_argument("--disks", action="store_true",
                              help="measure the model disk's read bandwidth now (direct I/O, <= 256 MiB, <= 2 s)")
    recipe_probe.add_argument("--descriptors", action="store_true",
                              help="read and save the model's file headers for planning (explicit header read)")
    recipe_probe.add_argument("--evidence", action="append", metavar="NODE=RECEIPT",
                              help="a node's measured disk bandwidth from a receipt file (repeatable)")
    recipe_probe.add_argument("--describe-command", action="store_true",
                              help="print each node's exact engine memory declaration command and launch sha256")
    recipe_probe.add_argument("--declaration", action="append", metavar="NODE=RECEIPT",
                              help="store a node's engine memory declaration receipt for its exact launch (repeatable)")
    recipe_probe.add_argument("--out", type=Path, help="save the facts for offline validate/explain --facts")
    recipe_probe.add_argument("--config", type=Path, default=config_path())
    for name, text in (("validate", "schema, hardware match and the up decision"),
                       ("explain", "every field's value and origin, the routes, and what up would do")):
        command = recipe_commands.add_parser(name, help=text)
        command.add_argument("file", type=Path)
        command.add_argument("profile", nargs="?")
        command.add_argument("--facts", type=Path, help="a saved `recipe probe --out` file instead of live probes")
        layered(command)
        if name == "explain":
            command.add_argument("--all", action="store_true", help="also the fields left at their defaults")
            command.add_argument("--prompt-tokens", type=int, help="simulate the route of a request this long")
            command.add_argument("--unavailable", action="append", metavar="DEVICE_ID", help="what-if: device down")
            command.add_argument("--link-down", action="append", metavar="LINK_ID", help="what-if: link down")
            command.add_argument("--free-ram", action="append", metavar="HOST=GIB", help="what-if: host free RAM")
    recipe_diff = recipe_commands.add_parser("diff", help="field-level difference of two effective recipes")
    recipe_diff.add_argument("a", type=Path)
    recipe_diff.add_argument("b", type=Path)
    recipe_diff.add_argument("--a-set", action="append", metavar="PATH=VALUE")
    recipe_diff.add_argument("--b-set", action="append", metavar="PATH=VALUE")
    recipe_diff.add_argument("--config", type=Path, default=config_path())
    recipe_pick = recipe_commands.add_parser("pick", help="the recipe `up --recipe auto` would choose, and why")
    recipe_pick.add_argument("profile")
    recipe_pick.add_argument("--dir", action="append", type=Path, help="also search this directory (repeatable)")
    recipe_pick.add_argument("--facts", type=Path)
    layered(recipe_pick)
    recipe_list = recipe_commands.add_parser("list", help="every recipe: built-in examples, configured and user ones")
    recipe_list.add_argument("--model", help="only the recipes for this model id")
    recipe_list.add_argument("--config", type=Path, default=config_path())
    recipe_ready = recipe_commands.add_parser("ready", help="what is still missing before up can run a recipe, with "
                                              "the next command (offline: saved state only)")
    recipe_ready.add_argument("recipe", help="a recipe id or file")
    recipe_ready.add_argument("profile", nargs="?", help="default: the only profile serving the recipe's model")
    recipe_ready.add_argument("--facts", help="a saved probe (name or file); default: the profile's newest")
    recipe_ready.add_argument("--format", choices=("json", "text"), default="json")
    recipe_ready.add_argument("--config", type=Path, default=config_path())
    recipe_matrix = recipe_commands.add_parser("matrix", help="which recipe covers each model x hardware class")
    recipe_matrix.add_argument("--format", choices=("json", "text"), default="json")
    recipe_matrix.add_argument("--config", type=Path, default=config_path())
    recipe_export = recipe_commands.add_parser("export", help="a recipe and its layers as one data-only bundle")
    recipe_export.add_argument("recipe", help="a recipe id or file")
    recipe_export.add_argument("--layer", action="append", metavar="NAME",
                               help="a user layer (file name in the layers directory) to include (repeatable)")
    recipe_export.add_argument("--out", type=Path, help="write the bundle to this file instead of printing it")
    recipe_export.add_argument("--config", type=Path, default=config_path())
    recipe_import = recipe_commands.add_parser("import", help="write a bundle's recipe and layers into the user "
                                               "directories (checked first; nothing runs)")
    recipe_import.add_argument("bundle", help="the bundle file (- = stdin)")
    recipe_import.add_argument("--id", help="the recipe id to save it as (default: the bundle's)")
    recipe_import.add_argument("--replace", metavar="SHA256", help="replace the user recipe that has this sha256")
    recipe_import.add_argument("--config", type=Path, default=config_path())
    recipe_show = recipe_commands.add_parser("show", help="one recipe: text, routes, settable fields")
    recipe_show.add_argument("recipe", help="a recipe id or file")
    recipe_show.add_argument("--config", type=Path, default=config_path())
    recipe_select = recipe_commands.add_parser("select", help="the recipe each profile serving MODEL would run, and why")
    recipe_select.add_argument("model")
    recipe_select.add_argument("--profile", help="only this profile")
    recipe_select.add_argument("--facts", type=Path)
    layered(recipe_select)
    recipe_plan = recipe_commands.add_parser("plan", help="what up --recipe decides before any lease (dry run)")
    recipe_plan.add_argument("recipe", help="a recipe id, a file or auto")
    recipe_plan.add_argument("profile")
    recipe_plan.add_argument("--facts", type=Path)
    layered(recipe_plan)
    for name, text in (("active", "the recipe each running profile was launched with, explained"),
                       ("routes", "per-request route counts (RTX / nodes) and the route state now")):
        command = recipe_commands.add_parser(name, help=text)
        command.add_argument("profile", nargs="?")
        command.add_argument("--config", type=Path, default=config_path())
    recipe_save = recipe_commands.add_parser("save", help="write a user recipe (validated; the user directory only)")
    recipe_save.add_argument("id", help="the new recipe id (file stem), e.g. qwen3.8-flash-next.mine")
    source = recipe_save.add_mutually_exclusive_group(required=True)
    source.add_argument("--from", dest="from_file", type=Path, help="the recipe text (TOML or JSON; - = stdin)")
    source.add_argument("--base", help="a recipe id or file to copy with --set values (saved as JSON)")
    recipe_save.add_argument("--base-sha256", help="with --base: the sha256 of the base as it was edited (required)")
    recipe_save.add_argument("--set", action="append", metavar="PATH=VALUE",
                             help="with --base: one value (repeatable; all of them form one overrides layer)")
    recipe_save.add_argument("--name", help="with --base: the copy's recipe name (default: the id)")
    recipe_save.add_argument("--replace", metavar="SHA256", help="replace the user recipe that has this sha256")
    recipe_save.add_argument("--config", type=Path, default=config_path())
    recipe_delete = recipe_commands.add_parser("delete", help="remove a user recipe")
    recipe_delete.add_argument("id")
    recipe_delete.add_argument("--sha256", required=True, help="the sha256 of the file being removed")
    recipe_delete.add_argument("--config", type=Path, default=config_path())
    layer_save = recipe_commands.add_parser("save-layer", help="write an overrides layer into the user layers directory")
    layer_save.add_argument("name", help="the layer's name (file stem)")
    layer_source = layer_save.add_mutually_exclusive_group(required=True)
    layer_source.add_argument("--recipe", help="build an overrides layer from --set values for this recipe (id or file)")
    layer_source.add_argument("--from", dest="from_file", type=Path,
                              help="a whole overrides or tuning-profile document (TOML or JSON; - = stdin)")
    layer_save.add_argument("--set", action="append", metavar="PATH=VALUE", help="with --recipe (repeatable)")
    layer_save.add_argument("--replace", metavar="SHA256", help="replace the layer that has this sha256")
    layer_save.add_argument("--config", type=Path, default=config_path())
    deploy = recipe_commands.add_parser("deploy", help="stop the reviewed profiles and launch a planned snapshot")
    deploy.add_argument("inputs", help="the `inputs` digest `hyperloom recipe plan` printed")
    deploy.add_argument("--stop", action="append", default=[], metavar="PROFILE",
                        help="a running profile to stop first (repeatable; must be exactly the running set)")
    deploy.add_argument("--job", help="a hex id; the file <config dir>/run/deploy-cancel/ID cancels this deploy")
    deploy.add_argument("--approval-ref")
    deploy.add_argument("--approval-lease", type=int)
    deploy.add_argument("--approval-end")
    deploy.add_argument("--config", type=Path, default=config_path())
    receipt = recipe_commands.add_parser("save-receipt", help="store a node disk-read receipt for probe --evidence")
    receipt.add_argument("name", help="the receipt name (file stem)")
    receipt.add_argument("--from", dest="from_file", type=Path, required=True, help="the receipt JSON (- = stdin)")
    receipt.add_argument("--replace", metavar="SHA256", help="replace the receipt that has this sha256")
    receipt.add_argument("--config", type=Path, default=config_path())
    layer_delete = recipe_commands.add_parser("delete-layer", help="remove an overrides layer of the user directory")
    layer_delete.add_argument("name")
    layer_delete.add_argument("--sha256", required=True, help="the sha256 of the file being removed")
    layer_delete.add_argument("--config", type=Path, default=config_path())
    # Internal controller entry point; not part of the public interface.
    helper = commands.add_parser("_prefill-run", help=argparse.SUPPRESS)
    helper.add_argument("spec")
    helper = commands.add_parser("_router-run", help=argparse.SUPPRESS)
    helper.add_argument("directory")
    helper.add_argument("rendered")
    helper.add_argument("token")
    launch = commands.add_parser("_up-router", help=argparse.SUPPRESS)
    launch.add_argument("spec")
    watch = commands.add_parser("_up-watch", help=argparse.SUPPRESS)
    watch.add_argument("path")
    watch.add_argument("token")
    cliext.add_all(commands)
    return parser


def _admission_hold(args):
    """Contract C11 for direct CLI use: a deploy/start/install takes a "core" hold (refused while a benchmark
    holds its resources), a stop a "stop" hold (cancels overlapping benchmarks). Returns a release function."""
    from .recipes import preflight
    preflight.cli(args)
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
