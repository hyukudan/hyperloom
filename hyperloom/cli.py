"""Command line and local router controllers."""

import argparse
from datetime import datetime
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

from .paths import repo_root
from .winproc import no_window


_controllers = []


def config_path():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "hyperloom" / "cluster.json"
    return Path.home() / ".config" / "hyperloom" / "cluster.json"


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
        temporary.replace(path)
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
        return json.loads((directory / "router-state.json").read_text(encoding="utf-8"))
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
            _json(remote.Remote(machine, config_path().parent / "known_hosts").call("doctor", {}, timeout=60))
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hyperloom")
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
    install.add_argument("--config", type=Path, default=config_path())
    assets = commands.add_parser("assets", help="verify assets or inspect the integrity cache")
    asset_commands = assets.add_subparsers(dest="operation", required=True)
    for operation in ("verify", "status"):
        asset_command = asset_commands.add_parser(operation)
        asset_command.add_argument("profile")
        asset_command.add_argument("--config", type=Path, default=config_path())
        if operation == "verify":
            asset_command.add_argument("--include-large", action="store_true")
            asset_command.add_argument("--machine")
    up = commands.add_parser("up")
    # Keep the no-argument Phase 0 probe for existing launchers.
    up.add_argument("profile", nargs="?")
    up.add_argument("--config", type=Path, default=config_path())
    up.add_argument("--router", default="local")
    up.add_argument("--node", action="extend", nargs="+", default=None)
    up.add_argument("--device", action="extend", nargs="+", default=None)
    up.add_argument("--deadline", type=int, default=1140)
    up.add_argument("--approval-ref")
    up.add_argument("--approval-lease", type=int)
    up.add_argument("--approval-end")
    down = commands.add_parser("down")
    down.add_argument("profile", nargs="?")
    down.add_argument("--force", action="store_true")
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
    keys = commands.add_parser("keys").add_subparsers(dest="key_operation", required=True)
    keys.add_parser("rotate").add_argument("name")
    export = keys.add_parser("export")
    export.add_argument("profile")
    export.add_argument("--out", type=Path, required=True)
    prefill = commands.add_parser("prefill", help="RTX prefill service: device check, CUDA build, fidelity gate")
    prefill.add_argument("operation", choices=("check", "build", "gate", "placement"))
    prefill.add_argument("profile")
    prefill.add_argument("--config", type=Path, default=config_path())
    prefill.add_argument("--prompts", type=Path, help="gate/placement: reference prompt file(s) directory")
    prefill.add_argument("--source", type=Path, help="build: engine checkout at the preset's pinned commit")
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
    args = parser.parse_args(argv)
    try:
        if args.command == "ui":
            if not 0 <= args.port <= 65535:
                raise ValueError("ui port must be in 0..65535")
            _module("hyperloom.web.server").serve(args.port, no_browser=args.no_browser)
        elif args.command in ("up", "down") and args.profile is None:
            print("Phase 1a")
        elif args.command == "install":
            cluster = _cluster(args.config)
            orchestrate = _module("hyperloom.orchestrate")
            _, kind, mids = orchestrate._profile(cluster, args.profile)
            targets = ["strix-hip-decode-token" if kind == "qwen-replicas" else "strix-deepseek41-serve"]
            _json({"release_id": orchestrate.install(cluster, args.build_on or mids[0], targets, profile=args.profile)})
        elif args.command == "assets":
            cluster = _cluster(args.config)
            assets = _module("hyperloom.assets")
            if args.operation == "verify":
                _json(assets.verify(cluster, args.profile, include_large=args.include_large, machine=args.machine))
            else:
                _json(assets.status(cluster, args.profile))
        elif args.command == "up":
            supplied = [args.approval_ref is not None, args.approval_lease is not None, args.approval_end is not None]
            approval = None
            if any(supplied):
                if not all(supplied):
                    raise ValueError("approval requires --approval-ref, --approval-lease, and --approval-end")
                end = datetime.fromisoformat(args.approval_end.replace("Z", "+00:00"))
                if end.tzinfo is None:
                    raise ValueError("--approval-end must include a timezone")
                approval = {"ref": args.approval_ref, "lease_s": args.approval_lease, "end_epoch": end.timestamp()}
            cluster = _cluster(args.config)
            _json(_module("hyperloom.orchestrate").up(cluster, args.profile, router=args.router,
                  nodes=args.node, devices=args.device, deadline_s=args.deadline, approval=approval))
        elif args.command == "down":
            _json(_module("hyperloom.orchestrate").down(args.profile, force=args.force))
        elif args.command == "endpoint":
            _json(_module("hyperloom.orchestrate").endpoint(args.profile))
        elif args.command == "status":
            _json(_module("hyperloom.orchestrate").status(args.profile))
        elif args.command == "logs":
            _json(_module("hyperloom.orchestrate").logs(args.profile, args.process, args.tail))
        elif args.command == "hosts":
            _json(_hosts(args))
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
                                                   prompts=args.prompts, source=args.source))
        elif args.command == "config":
            _cluster(args.path)
            _json({"valid": True})
        elif args.command == "render":
            cluster = _cluster(args.config)
            renderer = _module("hyperloom.render")
            rendered = renderer.render_router(cluster, args.profile)
            renderer.validate_router(rendered)
            result = {"router": rendered, "processes": [asdict(spec) for spec in
                                                        renderer.render_processes(cluster, args.profile)]}
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
