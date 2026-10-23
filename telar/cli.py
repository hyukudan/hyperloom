"""Phase 0 command line and local router controller."""

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


_controllers = []


def config_path():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "telar" / "cluster.json"
    return Path.home() / ".config" / "telar" / "cluster.json"


def _module(name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f"{name} is unavailable; complete the telar installation ({exc})") from exc


def _json(obj):
    print(json.dumps(obj, indent=2))


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
    procs = _module("telar.procs")
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
    source = Path(__file__).resolve().parent.parent / "third_party" / "tensormeld" / "src"
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
    cluster = _module("telar.config").load(args.config)
    renderer = _module("telar.render")
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
    argv = [str(Path(sys.executable).absolute()), "-m", "telar", "_router-run",
            str(directory), str(rendered_path), token]
    # The controller imports telar from this checkout or from the installed package.
    controller_env = dict(router_env)
    controller_env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parent.parent), router_env.get("PYTHONPATH", "")])
    options = {"start_new_session": True}
    if os.name == "nt":
        # Keep a console for CTRL_BREAK delivery without opening a visible window.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
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
    procs = _module("telar.procs")
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
        cluster = _module("telar.config").load(args.config)
        try:
            machine = cluster.machines[args.machine]
        except KeyError as exc:
            raise ValueError("unknown machine: " + args.machine) from exc
        if machine.access != "local":
            if machine.access != "ssh":
                raise ValueError("agent access is a later phase")
            remote = _module("telar.remote")
            _json(remote.Remote(machine, config_path().parent / "known_hosts").call("doctor", {}, timeout=60))
            return
    script = Path(__file__).resolve().parent.parent / "node" / "telar-node"
    if not script.is_file():
        script = Path(sys.prefix) / "share" / "telar" / "node" / "telar-node"
    if not script.is_file():
        raise RuntimeError("node/telar-node is unavailable; complete the telar installation")
    result = subprocess.run([str(Path(sys.executable).absolute()), str(script), "doctor"],
                            input="{}", text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError("local doctor failed: " + result.stderr.strip())
    _json(json.loads(result.stdout))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="telar")
    commands = parser.add_subparsers(dest="command", required=True)
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
    commands.add_parser("up")
    commands.add_parser("down")
    # Internal controller entry point; not part of the public interface.
    helper = commands.add_parser("_router-run", help=argparse.SUPPRESS)
    helper.add_argument("directory")
    helper.add_argument("rendered")
    helper.add_argument("token")
    args = parser.parse_args(argv)
    try:
        if args.command in ("up", "down"):
            print("Phase 1a")
        elif args.command == "config":
            _module("telar.config").load(args.path)
            _json({"valid": True})
        elif args.command == "render":
            cluster = _module("telar.config").load(args.config)
            renderer = _module("telar.render")
            rendered = renderer.render_router(cluster, args.profile)
            renderer.validate_router(rendered)
            _json({"router": rendered, "processes": [asdict(spec) for spec in
                                                     renderer.render_processes(cluster, args.profile)]})
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
        print(f"telar: {exc}", file=sys.stderr)
        return 1
