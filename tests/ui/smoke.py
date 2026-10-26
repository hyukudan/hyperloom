"""Offline portal fixtures, Chrome smoke checks, and WCAG token checks (stdlib only)."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import dataclasses
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.portable_fixtures import read_fixture
ROUTES = ("devices", "topology", "models", "recipes", "engines", "stats", "logs", "settings")
TITLES = ("Devices", "Topology & links", "Models & placement", "Recipes & profiles", "Engines", "Live stats", "Logs",
          "Settings & pairing")
API = "/hyperloom/api/"
# A node-only Qwen recipe that fits the fixture's one-node profile (test data, not a lab recipe).
FIXTURE_RECIPE = """schema = "strix-stream/recipe-v1"
name = "qwen-fixture-two"
description = "Fixture: two Qwen node replicas (UI test data)"

[match]
model = "qwen3.8-flash-next"

[[match.devices]]
id = "nodes"
kind = "hip"
count = 2
memory_min_gib = 100.0

[limits]
host_free_ram_floor_gib = 1.0

[[routes]]
id = "prefill_replicas"
phase = "prefill"
devices = ["nodes"]
execute = "replicas"
handoff_route = "decode_replicas"
[routes.placement.nodes]
max_context_tokens = 12288
max_batch = 8

[[routes]]
id = "decode_replicas"
phase = "decode"
devices = ["nodes"]
execute = "replicas"
[routes.placement.nodes]
max_context_tokens = 12288
max_batch = 8
"""


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def eventually(check, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for fixture/browser readiness")
        time.sleep(0.05)


class FixtureServer:
    """A separate process runs the real CLI/server, with backend calls replaced."""

    def __init__(self, *, blocked=False, port=None):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyperloom-ui-")
        self.directory = Path(self.temporary.name)
        self.port = port or free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.url = self.origin + "/"
        self.output = (self.directory / "server.log").open("w+b")
        command = [sys.executable, str(Path(__file__).resolve()), "--fixture-server",
                   "--port", str(self.port), "--fixture-dir", str(self.directory)]
        if blocked:
            command.append("--blocked")
        self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.output, stderr=self.output)
        try:
            eventually(self._ready)
        except BaseException:
            self.close()
            raise

    def _ready(self):
        if self.process.poll() is not None:
            self.output.seek(0)
            raise AssertionError("fixture server exited:\n" + self.output.read().decode("utf-8", "replace"))
        try:
            status, _, body = self.request("GET", "/")
            return status == 200 and bool(body)
        except OSError:
            return False

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            try:
                data = response.read()
            except ConnectionError:
                # A refusal sent before the request body was read (e.g. 413) closes the connection; Windows may then
                # reset it under the client while it reads the answer's body. The status already arrived.
                data = b""
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def calls(self):
        path = self.directory / "calls.jsonl"
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def release(self):
        (self.directory / "release").touch()

    def close(self):
        self.release()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.output.close()
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fixture_server(args):
    from hyperloom import cli, config, orchestrate, remote, render, secrets
    from hyperloom.web import api as web_api
    from hyperloom.web import recipes as recipe_web

    directory = args.fixture_dir
    directory.mkdir(parents=True, exist_ok=True)
    os.environ.update(APPDATA=str(directory), LOCALAPPDATA=str(directory),
                      XDG_CONFIG_HOME=str(directory), XDG_DATA_HOME=str(directory))
    machine = config.Machine("halo-1", "linux", "ssh", "192.0.2.10", "halo-1", 22,
                             "/opt/hyperloom/halo-1", True, [{"id": "gpu"}])
    assets = {"halo-1": {"qwen-model": {"path": "/opt/models/qwen.gguf", "bytes": 123456789,
                                         "sha256": "a" * 64}}}
    raw = {"schema": "hyperloom/cluster-v1", "machines": [{"id": "halo-1", "os": "linux",
           "access": "ssh", "http_address": "192.0.2.10", "ssh_target": "halo-1", "ssh_port": 22,
           "install_root": "/opt/hyperloom/halo-1", "lab_mode": True, "devices": [{"id": "gpu"}]}], "assets": assets,
           "profiles": {"qwen-replicas": {"nodes": ["halo-1"]}}, "router": {}, "pc": {}}
    cluster = config.Cluster({machine.id: machine}, assets, raw["profiles"], {}, {}, raw)
    inspection = json.loads(read_fixture(ROOT / "tests/fixtures/web/inspection.json"))
    for mid, system, access, address in (("workstation", "windows", "local", "192.0.2.20"),
                                          ("halo-2", "linux", "ssh", "192.0.2.11")):
        cluster.machines[mid] = config.Machine(mid, system, access, address, mid, 22,
                                             "/opt/hyperloom/" + mid, True, [{"id": "gpu"}])
    # A two-node profile for the recipes walkthrough's deploy (its recipe needs a replica on each node).
    cluster.profiles["qwen-two"] = {"preset": "qwen-replicas", "nodes": ["halo-1", "halo-2"]}
    ordered = {mid: cluster.machines[mid] for mid in ("workstation", "halo-1", "halo-2")}
    cluster.machines.clear()
    cluster.machines.update(ordered)
    config_file = directory / "hyperloom" / "cluster.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(raw), encoding="utf-8", newline="\n")
    # All-zero synthetic public-key bytes; there is no private key or SSH peer.
    key_blob = struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32) + bytes(32)
    (directory / "known_hosts").write_text(
        "halo-1 ssh-ed25519 " + base64.b64encode(key_blob).decode("ascii") + "\n",
        encoding="utf-8", newline="\n")
    lock = threading.Lock()
    running = {"value": not args.blocked}
    cancelled = threading.Event()
    fixture_path = ROOT / "tests/fixtures/adapters"
    router_status = json.loads(read_fixture(fixture_path / "router-status.json"))
    router_status["replicas"] = [router_status["replicas"][0]]
    router_status["replicas"][0]["endpoint"] = "127.0.0.1:18501"
    engine_status = json.loads((fixture_path / "engine-status.json").read_text(encoding="utf-8"))
    for index, request in enumerate(engine_status.get("requests", [])):
        request["time"] = time.time() - index - 1
    engine_metrics = (fixture_path / "engine-metrics.txt").read_text(encoding="utf-8")

    def record(action, **values):
        with lock, (directory / "calls.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"action": action, **values}, default=repr) + "\n")

    def up(*a, **kw):
        cancelled.clear()
        if kw.get("cancelled") and kw["cancelled"]():    # the job's hand-over, checked as orchestrate.up does
            record("up-cancelled")
            raise RuntimeError("profile stop requested")
        record("up", arguments={k: v for k, v in kw.items() if k != "cancelled"})
        if args.blocked:
            eventually(lambda: (directory / "release").exists(), timeout=60)
        running["value"] = not cancelled.is_set()
        return {"profile": "qwen-replicas", "state": "running" if running["value"] else "stopped"}

    def down(*a, **kw):
        cancelled.set()
        record("down")
        running["value"] = False
        (directory / "release").touch()
        return {"profile": "qwen-replicas", "state": "stopped"}

    def status(profile=None, config_path=None, skip=None):
        row = {"profile": "qwen-replicas", "nodes": {}, "router": None}
        if running["value"]:
            row["router"] = router_status
            row["nodes"] = {"halo-1": {"leases": [{"lease": "fixture-lease", "label": "hl-qwen-fixture",
                "state": "ready", "start_time": time.time() - 60, "deadline": time.time() + 600, "cap_s": 1200, "drain_margin_s": 60,
                "processes": [{"name": "replica", "state": "ready", "ready_after_s": 0.25,
                               "endpoint": "127.0.0.1:18501", "binary_sha256": "a" * 64}]}]}}
        else:
            row["state"] = "stopped"
        return row if profile else {"qwen-replicas": row}

    class FakeRemote:
        def __init__(self, machine, *a, **kw):
            self.machine = machine

        def call(self, verb, payload, timeout=60):
            record("remote:" + verb)
            if verb in ("doctor", "gpu"):
                return inspection[self.machine.id][verb]
            return {}

    def forbidden(*a, **kw):
        raise AssertionError("fixture attempted an unfaked process/network operation")

    real_connect = socket.socket.connect
    real_bind = socket.socket.bind

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            return forbidden()
        return real_connect(sock, address)

    def bind(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            return forbidden()
        return real_bind(sock, address)

    def install(*a, **kw):
        record("install")
        return "a" * 12

    def read_state(path):
        if path.name == "up-qwen-replicas.json" and running["value"]:
            # Launched from this portal's configuration: a state without the record belongs to no portal.
            return {"release_id": "a" * 12, "nodes": ["halo-1"], "machines": {"halo-1": {}},
                    "config": {"path": str(config_file.resolve()), "sha256": None},
                    "started_at": time.time() - 60, "router": {"machine": "local", "directory": str(directory / "router")},
                    "devices": ["halo-1:gpu"],
                    "process_specs": [{"machine_id": "halo-1", "role": "replica", "argv": ["/opt/hyperloom/halo-1/releases/" + "a" * 12 + "/bin/strix-hip-decode-token", "--http-port", "18501"], "env": {"STRIX_COHORT_ROWS": "32"}}]}
        if path.name == "router.json":
            return {"listen": {"address": "127.0.0.1", "port": 18600}, "policy": "nodes"}
        if path.name == "state.json":
            return {"state": "ready", "pid": 1000}
        raise FileNotFoundError(path)

    with ExitStack() as stack:
        # The fixture portal is no lab: its pollers are not paced and no GPU turn pauses them (the polling budget
        # itself is tested in tests/test_pollbudget.py), so every page shows its recorded node answers at once.
        # For the same reason the node poll (hyperloom.nodepoll, tested in tests/test_nodepoll.py) does not run: the
        # pages read the recorded node answers directly.
        from hyperloom import nodepoll, pollbudget
        replacements = [(nodepoll, "start", lambda api: None),
                        (pollbudget.Budget, "node", lambda self, *a, **kw: None),
                        (pollbudget.Budget, "engine", lambda self, *a, **kw: None),
                        (pollbudget.Budget, "engine_paused", lambda self, *a, **kw: None),
                        (config, "load", lambda *a, **kw: cluster),
                        (orchestrate, "load_cluster", lambda *a, **kw: cluster),
                        (orchestrate, "up", up), (orchestrate, "down", down),
                        (orchestrate, "status", status), (orchestrate, "install", install),
                        (orchestrate, "logs", lambda *a, **kw: {"stdout": "READY fixture", "stderr": ""}),
                        (remote, "Remote", FakeRemote),
                        (orchestrate, "_read", read_state),
                        (web_api.API, "_probe", lambda self, machine: inspection[machine.id].get("probe")),
                        (web_api.API, "_node", lambda self, machine, verb, payload=None: (record("remote:" + verb) or inspection.get(machine.id, inspection["workstation"])[verb])),
                        (remote, "keyscan", lambda *a, **kw: []),
                        (secrets, "rotate", lambda *a, **kw: record("keys/rotate")),
                        (secrets, "runtime_dir", lambda: directory / "run"),
                        (secrets, "_directory", lambda: directory / "secrets"),
                        (cli, "config_path", lambda: config_file),
                        (web_api, "_http", lambda endpoint, suffix, text=False:
                         engine_metrics if suffix == "/metrics" else engine_status),
                        (subprocess, "Popen", forbidden), (subprocess, "run", forbidden),
                        (socket.socket, "connect", connect), (socket.socket, "bind", bind)]
        for owner, name, value in replacements:
            stack.enter_context(patch.object(owner, name, value))
        # Recipe requests run the CLI's parser and commands.run here (the fixture refuses subprocesses), on the
        # stub reader; the pinned router is taken to accept the routes policy so a plan can pass.
        def recipe_run(self, argv, *, stdin=None, deadline_s, cancelled=lambda: False):
            with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin or b""), encoding="utf-8")):
                try:
                    return recipe_web.in_process(argv, lambda path: cluster)
                except Exception as exc:          # e.g. a probe the fixture refuses: a refusal like the CLI's
                    return recipe_web.Result(1, b"", f"hyperloom: {exc}".encode())
        stack.enter_context(patch.object(recipe_web.Runner, "run", recipe_run))
        # Hardware facts for recipes: a saved observation of the fixture's node (no probe runs here).
        from hyperloom.recipes import commands as recipe_commands, session as recipe_session
        node = {"name": "AMD Radeon 8060S", "memory_gib": 120.0, "host_ram_gib": 125.0}
        observation = recipe_session.facts_record(
            "qwen-two", {"cuda": []}, {"model": "qwen3.8-flash-next", "devices": {"halo-1": node, "halo-2": node},
                                       "links": []},
            {"devices": {"halo-1": {"available": True}, "halo-2": {"available": True}},
             "hosts": {"workstation": {"free_ram_gib": 100.0, "free_commit_gib": 150.0}, "halo-1": {"free_ram_gib": 500.0},
                       "halo-2": {"free_ram_gib": 500.0}}, "lan": {"halo-1": {"up": True}, "halo-2": {"up": True}}, "usb4": {}}, [])
        (directory / "run" / "facts").mkdir(parents=True, exist_ok=True)
        (directory / "run" / "facts" / "fixture.json").write_text(json.dumps(observation), encoding="utf-8")
        stack.enter_context(patch.object(recipe_commands, "live_source", lambda cluster, measure_disks=False:
                                         recipe_session.saved_source(directory / "run" / "facts" / "fixture.json")))
        (config_file.parent / "recipes").mkdir(exist_ok=True)
        (config_file.parent / "recipes" / "qwen3.8-flash-next.fixture.toml").write_text(FIXTURE_RECIPE, encoding="utf-8")
        # Layers in the renamed (tierstream/*) spelling: the page sorts them by kind like the legacy ids.
        (config_file.parent / "recipes" / "layers").mkdir()
        (config_file.parent / "recipes" / "layers" / "fixture-tuned.toml").write_text(
            'schema = "tierstream/recipe-profile-v1"\nrecipe = "qwen-fixture-two"\nrecipe_sha256 = "' + "0" * 64
            + '"\nsource = "tune"\n[set]\n', encoding="utf-8")
        (config_file.parent / "recipes" / "layers" / "fixture-floor.toml").write_text(
            'schema = "tierstream/recipe-overrides-v1"\nrecipe = "qwen-fixture-two"\nsource = "fixture"\n'
            '[set]\n"limits.host_free_ram_floor_gib" = 6\n', encoding="utf-8")
        stack.enter_context(patch.object(render, "router_policies", lambda: ("nodes", "rtx", "auto", "routes")))
        stack.enter_context(patch.object(render, "check_route_table", lambda table: None))
        # The fixture's engine states its prefill bounds (one prefill of up to 8 prompts, whole prompts), so the
        # fixture recipe's local prefill route is admissible; the real presets cite them per engine build.
        from hyperloom.recipes import facts as recipe_facts
        real_context = recipe_facts.context

        def fixture_context(cluster, profile):
            ctx = real_context(cluster, profile)
            return dataclasses.replace(ctx, limits=dict(ctx.limits, prefill_batch=(8, None), prefill_chunk=0))
        stack.enter_context(patch.object(recipe_facts, "context", fixture_context))
        for owner, name, value in seed_fixtures(directory, time.time()):
            stack.enter_context(patch.object(owner, name, value))
        os.environ.update(HYPERLOOM_RECIPE_STUB="1", HYPERLOOM_TS_RECIPE="", HYPERLOOM_SS_RECIPE="")
        os.environ["HYPERLOOM_UI_INPROCESS"] = "1"  # patched backends: keep jobs in this process
        return cli.main(["ui", "--port", str(args.port), "--no-browser"])


def contrast_ratio(first, second):
    def luminance(value):
        value = value.lstrip("#")
        if len(value) == 3:
            value = "".join(ch * 2 for ch in value)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
            raise AssertionError("unsupported colour token: " + value)
        channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        channels = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    values = sorted((luminance(first), luminance(second)))
    return (values[1] + 0.05) / (values[0] + 0.05)


THEME_SELECTORS = {"light": r":root\s*\{",
                   "dark": r":root\[data-theme\s*=\s*[\"']?dark[\"']?\]\s*\{",
                   # True black is a shade of the dark theme: its block overrides the dark tokens.
                   "black": r":root\[data-theme\s*=\s*[\"']?dark[\"']?\]\[data-shade\s*=\s*[\"']?black[\"']?\]\s*\{"}


def check_contrast(path=None, themes_checked=("light", "dark")):
    css = (path or ROOT / "hyperloom/web/static/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    themes = {}
    for theme in themes_checked:
        match = re.search(THEME_SELECTORS[theme] + r"([^{}]*)\}", css, re.S)
        if not match:
            raise AssertionError("missing explicit " + theme + " colour tokens")
        themes[theme] = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match[1]))
    if "black" in themes:
        base = dict(themes.get("dark") or check_tokens(css, "dark"))
        base.update(themes["black"])
        themes["black"] = base
    pairs = [(text, surface) for text in ("--text", "--text-2", "--text-3")
             for surface in ("--bg", "--surface", "--surface-2")]
    pairs += [("--accent-text", "--accent-soft"), ("--on-accent", "--accent")]
    pairs += [("--" + state, "--" + state + "-soft") for state in ("ok", "warn", "err", "info")]
    failures = []
    for theme, values in themes.items():
        for foreground, background in pairs:
            if foreground not in values or background not in values:
                raise AssertionError(f"{theme}: missing {foreground}/{background}")
            ratio = contrast_ratio(values[foreground].strip(), values[background].strip())
            if ratio < 4.5:
                failures.append(f"{theme} {foreground} on {background}: {ratio:.3f}:1")
    if failures:
        raise AssertionError("Text contrast below 4.5:1:\n" + "\n".join(failures))
    return len(pairs) * len(themes)


def check_tokens(css, theme):
    match = re.search(THEME_SELECTORS[theme] + r"([^{}]*)\}", css, re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match[1])) if match else {}


def find_chrome():
    candidates = [os.environ.get("CHROME")]
    candidates += [shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(variable):
            candidates.append(str(Path(os.environ[variable]) / "Google/Chrome/Application/chrome.exe"))
    candidates += ["/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/chromium"]
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)


def get_json(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"DevTools HTTP {response.status}")
        return json.loads(response.read())
    finally:
        connection.close()


class DevTools:
    """Minimal RFC 6455 transport for CDP; no websocket package is needed."""

    def __init__(self, url):
        self.url = url
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1":
            raise AssertionError("DevTools must use loopback")
        self.socket = socket.create_connection(("127.0.0.1", parsed.port), timeout=10)
        self.stream = self.socket.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {parsed.path} HTTP/1.1\r\nHost: 127.0.0.1:{parsed.port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        self.socket.sendall(request.encode("ascii"))
        status = self.stream.readline()
        headers = {}
        while (line := self.stream.readline()) not in (b"\r\n", b""):
            name, value = line.decode("ascii").split(":", 1)
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if b" 101 " not in status or headers.get("sec-websocket-accept") != expected:
            self.close()
            raise AssertionError("DevTools handshake failed")
        self.sequence = 0
        self.errors = []
        self.responses = {}
        self.paused = []      # Fetch.requestPaused events (only while a test enables request interception)

    def close(self):
        self.stream.close()
        self.socket.close()

    def reconnect(self):
        """A fresh session on the same page target (an answer can be lost while the page moves between renderer
        processes, e.g. back from the browser's own error page); session settings and start-up scripts are not
        carried over."""
        errors = self.errors
        self.close()
        self.__init__(self.url)
        self.errors = errors
        self.call("Runtime.enable")
        self.call("Page.enable")

    def send(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes((0x80 | opcode, 0x80 | (size if size < 126 else 126 if size < 65536 else 127)))
        if size >= 126:
            header += struct.pack("!H" if size < 65536 else "!Q", size)
        self.socket.sendall(header + mask + bytes(ch ^ mask[i % 4] for i, ch in enumerate(payload)))

    def read_exact(self, size):
        data = self.stream.read(size)
        if len(data) != size:
            raise EOFError("Chrome closed DevTools")
        return data

    def receive(self):
        fragments = bytearray()
        while True:
            first, second = self.read_exact(2)
            size = second & 127
            if size in (126, 127):
                size = struct.unpack("!H" if size == 126 else "!Q", self.read_exact(2 if size == 126 else 8))[0]
            if size > 64 * 1024 * 1024:
                raise AssertionError("oversize DevTools frame")
            mask = self.read_exact(4) if second & 128 else None
            data = self.read_exact(size)
            if mask:
                data = bytes(ch ^ mask[i % 4] for i, ch in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise EOFError("Chrome closed DevTools")
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            fragments.extend(data)
            if first & 128:
                return json.loads(fragments)

    def call(self, method, **params):
        self.sequence += 1
        sequence = self.sequence
        self.send(json.dumps({"id": sequence, "method": method, "params": params}).encode())
        deadline = time.monotonic() + 20
        while sequence not in self.responses:
            if time.monotonic() > deadline:
                raise TimeoutError("DevTools command timed out: " + method)
            message = self.receive()
            if "id" in message:
                self.responses[message["id"]] = message
            elif message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message)
            elif message.get("method") == "Runtime.consoleAPICalled" and message.get("params", {}).get("type") in ("error", "assert"):
                self.errors.append(message)
            elif message.get("method") == "Fetch.requestPaused":
                self.paused.append(message["params"])
        response = self.responses.pop(sequence)
        if "error" in response:
            raise AssertionError(f"{method}: {response['error']}")
        return response.get("result", {})

    def paused_request(self, timeout=20):
        """The next request paused by Fetch interception (events arrive while commands run)."""
        deadline = time.monotonic() + timeout
        while not self.paused:
            if time.monotonic() > deadline:
                raise AssertionError("no intercepted request arrived")
            self.evaluate("new Promise(resolve => setTimeout(resolve, 50))")
        return self.paused.pop(0)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")


def key(client, name, *, shift=False):
    code = {"Tab": 9, "Escape": 27, "Enter": 13, " ": 32}[name]
    for event in ("keyDown", "keyUp"):
        client.call("Input.dispatchKeyEvent", type=event, key=name,
                    code="Space" if name == " " else name,
                    windowsVirtualKeyCode=code, nativeVirtualKeyCode=code,
                    modifiers=8 if shift else 0,
                    **({"text": " "} if name == " " and event == "keyDown" else {}))


def keyboard_walkthrough(client, server):
    """Use real key events to reach, open, trap and dismiss each mutation dialog."""
    dialogs = (("devices", "add"), ("engines", "start-dialog"),
               ("engines", "install-dialog"), ("devices", "doctor-dialog"),
               ("settings", "rotate-dialog"))
    for route, action in dialogs:
        client.call("Page.navigate", url=server.url + "#/" + route)
        selector = f'button[data-action="{action}"]'
        eventually(lambda: client.evaluate(
            f"document.querySelector({json.dumps(selector)})?.disabled === false"))
        for _ in range(160):
            if client.evaluate(f"document.activeElement.matches({json.dumps(selector)})"):
                break
            key(client, "Tab")
        else:
            raise AssertionError("Keyboard cannot reach " + action)
        key(client, " ")
        eventually(lambda: client.evaluate("!!document.querySelector('dialog[open]')"))
        # Let at least one ordinary two-second poll render behind the dialog.
        client.evaluate("new Promise(resolve => setTimeout(resolve, 2200))")
        named = client.evaluate("""(() => {
          const d = document.querySelector('dialog[open]');
          return !!(d.getAttribute('aria-label') ||
            document.getElementById(d.getAttribute('aria-labelledby'))?.textContent.trim());
        })()""")
        if not named:
            raise AssertionError("Unnamed dialog: " + action)
        for backwards in (False, True):
            for _ in range(20):
                key(client, "Tab", shift=backwards)
                if not client.evaluate("!!document.activeElement.closest('dialog[open]')"):
                    raise AssertionError("Dialog focus escaped: " + action)
        if action == "rotate-dialog":
            # A valid name ensures native required-field validation cannot mask
            # accidental Enter submission of this destructive operation.
            client.evaluate("document.querySelector('dialog[open] input:not([type=hidden])').focus()")
            client.call("Input.insertText", text="fixture-engine")
            key(client, "Enter")
            client.evaluate("new Promise(resolve => setTimeout(resolve, 100))")
            if not client.evaluate("!!document.querySelector('dialog[open]')"):
                raise AssertionError("Enter confirmed a destructive rotation")
            if any(row["action"] == "keys/rotate" for row in server.calls()):
                raise AssertionError("Enter dispatched a destructive rotation")
        key(client, "Escape")
        eventually(lambda: client.evaluate("!document.querySelector('dialog[open]')"))
        if not client.evaluate(f"document.activeElement.matches({json.dumps(selector)})"):
            raise AssertionError("Dialog failed to return focus: " + action + " -- " + json.dumps(client.evaluate(
                f"(() => {{ const b = document.querySelector({json.dumps(selector)}); return {{active: document.activeElement.outerHTML.slice(0, 200), "
                "opener: b && b.outerHTML.slice(0, 200), connection: document.getElementById('connection').textContent, "
                "fresh: window.Hyperloom.state.fresh}}; }})()")))
        if client.errors:
            raise AssertionError(json.dumps(client.errors))
        print("PASS keyboard " + action + " (Tab, reverse Tab, Space, Escape)", flush=True)


def recipes_walkthrough(client, server, output=None):
    """Drive the Recipes & profiles page: Get started (a hardware class lists its recipes, readiness answers step by
    step with the next command), open a recipe, an invalid field shows its inline error, a server refusal is
    shown, fixing it clears the errors, the preview answers (its refusal gates Deploy with the reason), and a copy is
    saved into the user directory and listed."""
    def wait(expression, timeout=60):
        try:
            return eventually(lambda: client.evaluate(expression), timeout=timeout)
        except AssertionError:
            raise AssertionError("recipes walkthrough: timed out waiting for " + expression + " -- page: "
                                 + str(client.evaluate("document.querySelector('main').innerText"))[:4000]) from None

    def field(path, value):
        client.evaluate(f"(() => {{ const el = document.querySelector('[data-path=\"{path}\"]'); el.value = {json.dumps(value)}; "
                        "el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true})); })()")

    def click(selector):
        client.evaluate(f"document.querySelector({json.dumps(selector)}).click()")
    client.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    client.call("Page.navigate", url=server.url + "#/recipes")
    wait("!!document.querySelector('[data-recipe-action=start-class][data-class=\"rtx-prefill\"]')")
    if not client.evaluate("[...document.querySelectorAll('[data-recipe-action=start-class]')].every(b => !b.disabled || b.title)"):
        raise AssertionError("recipes walkthrough: a disabled hardware class must say why")
    click('[data-recipe-action=start-class][data-class="rtx-prefill"]')
    wait("!!document.querySelector('[data-recipe-action=start-ready][data-id=\"qwen3.8-flash-next.lab\"]')")
    click('[data-recipe-action=start-ready][data-id="qwen3.8-flash-next.lab"]')
    wait("/router applies recipe routes/.test(document.querySelector('main').innerText)"
         " && /recipe ready/.test(document.querySelector('main').innerText)")
    if output is not None:
        png = base64.b64decode(client.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"])
        (Path(output) / "recipes-get-started.png").write_bytes(png)
    wait("!!document.querySelector('[data-recipe-action=open][data-id=\"qwen3.8-flash-next.lab\"]')")
    click('[data-recipe-action=open][data-id="qwen3.8-flash-next.lab"]')
    minimum, maximum = "routes.prefill_rtx.when.prompt_tokens_min", "routes.prefill_rtx.when.prompt_tokens_max"
    wait(f"!!document.querySelector('[data-path=\"{minimum}\"]')")
    field(minimum, "-5")
    wait(f"(() => {{ const el = document.querySelector('[data-path=\"{minimum}\"]'); const e = document.getElementById(el.id + '-error'); "
         "return !!e && /outside/.test(e.textContent) && el.getAttribute('aria-describedby').includes(e.id); })()")
    if not client.evaluate("document.querySelector('[data-recipe-action=plan]').disabled"):
        raise AssertionError("recipes walkthrough: an invalid field must block the preview")
    field(minimum, "1024")
    field(maximum, "10")
    wait("[...document.querySelectorAll('main .field-error')].some(e => /refused|prompt_tokens/.test(e.textContent))")
    click('[data-recipe-action="reset-edits"]')
    field(minimum, "2048")
    wait("!document.querySelector('main .field-error') && /recipe explain/.test(document.body.innerText)")
    click('[data-recipe-action="plan"]')
    wait("/recipe plan/.test(document.body.innerText)")
    ready = client.evaluate("!document.querySelector('[data-recipe-action=deploy]').disabled")
    if not ready:
        reason = client.evaluate("document.querySelector('[data-recipe-action=deploy]').title")
        if not reason or reason == "undefined":
            raise AssertionError("Deploy is disabled without its reason")
    else:
        click('[data-recipe-action="deploy"]')
        wait("document.getElementById('recipe-dialog')?.open")
        click("#recipe-dialog-confirm")
        wait("!document.getElementById('job-panel').hidden")
    click('[data-recipe-action="save-copy"]')
    wait("document.getElementById('recipe-dialog')?.open")
    client.evaluate("document.getElementById('copy-id').value = 'qwen3.8-flash-next.walk'; "
                    "document.getElementById('copy-name').value = 'qwen-walk'")
    click("#recipe-dialog-confirm")
    wait("!!document.querySelector('[data-recipe-action=open][data-id=\"qwen3.8-flash-next.walk\"]')")
    # Export: the copy (now selected) as a bundle file, through `hyperloom recipe export`.
    wait("!!document.querySelector('[data-recipe-action=export]')")
    click('[data-recipe-action="export"]')
    wait("/Exported qwen3.8-flash-next.walk/.test(document.body.innerText)")
    # A recipe that fits the fixture: the preview passes, Deploy is enabled and its job runs to the end.
    client.evaluate("(() => { const el = document.getElementById('recipe-profile'); el.value = 'qwen-two'; "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); })()")
    click('[data-recipe-action=open][data-id="qwen3.8-flash-next.fixture"]')
    wait("!!document.querySelector('[data-recipe-action=plan]') && /qwen-fixture-two/.test(document.body.innerText)")
    wait("[...document.querySelectorAll('#recipe-tuning option')].some(o => o.value === 'fixture-tuned.toml')"
         " && !!document.querySelector('[data-layer=\"fixture-floor.toml\"]')"
         " && !document.querySelector('[data-layer=\"fixture-tuned.toml\"]')")
    click('[data-recipe-action="plan"]')
    wait("!document.querySelector('[data-recipe-action=deploy]').disabled")
    click('[data-recipe-action="deploy"]')
    wait("document.getElementById('recipe-dialog')?.open")
    click("#recipe-dialog-confirm")
    wait("/(succeeded|failed)/.test(document.getElementById('job-panel').innerText)", timeout=120)
    job = client.evaluate("document.getElementById('job-panel').innerText")
    if "recipes/deploy" not in job or "succeeded" not in job:
        raise AssertionError("recipes walkthrough: the deploy job did not succeed: " + job[:2000])
    if client.errors:
        raise AssertionError("Browser console/exception errors: " + json.dumps(client.errors))
    return ("deploy job" if ready else "deploy gated by the preview's refusal") + ", fixture recipe deployed by a job"


def preflight_refusal_walkthrough(client, server):
    """A real synchronous legacy recipe refusal stays in the job-error panel through polling."""
    client.call("Page.navigate", url=server.url + "#/recipes")
    eventually(lambda: client.evaluate("!!window.Hyperloom && Hyperloom.state.fresh && !Hyperloom.mutationReason()"))
    for action, arguments, message in (
            ("up", {"profile": "qwen-replicas", "recipe_inputs": "0" * 64, "node": ["halo-1"]},
             "planned snapshot as it is"),
            ("recipes/deploy", {"inputs": "0" * 64}, "no snapshot")):
        eventually(lambda: client.evaluate("Hyperloom.state.fresh && !Hyperloom.mutationReason()"))
        client.evaluate(f"window.refusal = null; Hyperloom.mutate({json.dumps(action)}, {json.dumps(arguments)})"
                        ".catch(e => { window.refusal = {message: e.message, status: e.status}; });")
        eventually(lambda: client.evaluate("window.refusal !== null"))
        refusal = client.evaluate("window.refusal")
        if refusal["status"] != 400 or message not in refusal["message"]:
            raise AssertionError("unexpected preflight refusal: " + json.dumps(refusal))
        eventually(lambda: client.evaluate("!document.getElementById('job-panel').hidden && "
                                           "document.getElementById('job-panel').innerText.includes(window.refusal.message)"), timeout=5)
        client.evaluate("Hyperloom.refresh()")
        if not client.evaluate("document.getElementById('job-panel').innerText.includes(window.refusal.message)"):
            raise AssertionError("polling erased the preflight refusal")
    if client.evaluate("!!document.querySelector('#job-panel [data-action=cancel], #job-panel [data-action=stop]')"):
        raise AssertionError("a refused launch must have no running-job controls")
    if any(call["action"] == "up" for call in server.calls()):
        raise AssertionError("preflight refusal started the legacy engine")
    jobs = client.evaluate("Hyperloom.request('jobs')")
    if jobs["jobs"]:
        raise AssertionError("preflight refusal created a job")
    if client.errors:
        raise AssertionError("Browser console/exception errors: " + json.dumps(client.errors))
    return "HTTP 400 message retained in job panel; no job or launch"


def feature_pages(client, server, output, width, theme):
    """Every registered feature page (static/pages): it renders its title, finishes loading, does not overflow
    and raises no console error; one screenshot per page, width and theme (page-<id>-<width>-<theme>.png)."""
    pages = client.evaluate("Object.entries(window.HyperloomPages || {}).map(([id, p]) => [id, p.title || id])")
    count = 0
    for page, title in pages:
        client.call("Page.navigate", url=server.url + "#/" + page)
        eventually(lambda: client.evaluate("document.readyState === 'complete' && !!document.querySelector('main')"))
        eventually(lambda: client.evaluate("document.querySelector('main').getAttribute('aria-busy') === 'false'"), timeout=30)
        client.evaluate("new Promise(resolve => setTimeout(resolve, 1500))")
        metrics = client.evaluate("({width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth, heading:document.querySelector('h1')?.textContent, body:document.body.innerText})")
        if metrics["heading"] != title:
            raise AssertionError(f"page {page}: did not render its title: {metrics['heading']!r}")
        if metrics["scroll"] > width:
            raise AssertionError(f"page {page}/{width}: document overflows to {metrics['scroll']}px")
        if "data unavailable" in metrics["body"] and "Retry" in metrics["body"]:
            raise AssertionError(f"page {page}: its documents failed to load: {metrics['body'][:400]}")
        if client.errors:
            raise AssertionError(f"page {page}: browser console/exception errors: " + json.dumps(client.errors))
        png = base64.b64decode(client.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"])
        (output / f"page-{page}-{width}-{theme}.png").write_bytes(png)
        count += 1
        print(f"PASS page {page} {width}x1000 {theme}", flush=True)
    return count


def chord(client, letter, *, ctrl=True):
    """Ctrl+<letter> as real key events (the palette and sidebar shortcuts)."""
    code = ord(letter.upper())
    for event in ("keyDown", "keyUp"):
        client.call("Input.dispatchKeyEvent", type=event, key=letter, code="Key" + letter.upper(),
                    windowsVirtualKeyCode=code, nativeVirtualKeyCode=code, modifiers=2 if ctrl else 0)


def shell_walkthrough(client, server, output):
    """Portal shell (UI-SPEC section 12): heartbeat and freshness, palette, sidebar toggle, deep links, preferences
    (true black, compact, Spanish, refresh interval) through the real prefs feature, the exceptions strip, banner,
    tab badge, favicon and notifications from deterministic scenarios, the sidebar machine list from an overview
    scenario, and the narrow tab bar. Screenshots: shell-<view>-<theme>-<width>.png."""
    def wait(expression, timeout=30):
        try:
            return eventually(lambda: client.evaluate(expression), timeout=timeout)
        except AssertionError:
            raise AssertionError("shell walkthrough: timed out waiting for " + expression + " -- page: "
                                 + str(client.evaluate("document.body.innerText"))[:3000]) from None

    def shot(view, theme, width):
        png = base64.b64decode(client.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"])
        (output / f"shell-{view}-{theme}-{width}.png").write_bytes(png)

    def viewport(width, theme):
        client.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
        client.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme},
                                                          {"name": "prefers-reduced-motion", "value": "reduce"}])

    loads = iter(range(1, 1000))

    def go(hash_, ready="document.querySelector('main').getAttribute('aria-busy') === 'false'", reload=False):
        client.call("Page.navigate", url=server.url + (f"?load={next(loads)}" if reload else "") + hash_)
        wait("document.readyState === 'complete' && !!document.querySelector('main')")
        wait(ready)

    def select(element_id, value):
        client.evaluate(f"(() => {{ const el = document.getElementById({json.dumps(element_id)}); el.value = {json.dumps(value)}; "
                        "el.dispatchEvent(new Event('change', {bubbles: true})); })()")

    def saved(field, value):
        def check():
            status, _, body = server.request("GET", API + "prefs")
            return status == 200 and json.loads(body)["values"][field] == value
        try:
            eventually(check, timeout=30)
        except AssertionError:
            raise AssertionError(f"shell: preference {field}={value!r} not saved; page says: "
                                 + str(client.evaluate("[document.querySelector('#main [role=status]')?.textContent, JSON.stringify(window.Hyperloom.prefs())]"))) from None

    def scenario(content):
        # The fixture process reads this file on every request; Windows refuses to replace or remove a file another
        # process has open, so a refused attempt is repeated.
        path = server.directory / "shell-scenario.json"
        for _ in range(100):
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    staged = path.with_suffix(".tmp")
                    staged.write_text(json.dumps(content), encoding="utf-8")
                    os.replace(staged, path)
                return
            except PermissionError:
                time.sleep(0.05)
        raise AssertionError("shell: the scenario file stayed locked")

    # A recording stand-in for the browser's notifications (headless Chrome shows none).
    client.call("Page.addScriptToEvaluateOnNewDocument", source="""
      window.__notes = [];
      window.Notification = class { static get permission() { return "granted"; }
        static requestPermission() { return Promise.resolve("granted"); }
        constructor(title, options) { window.__notes.push(options && options.tag); } };""")
    viewport(1440, "light")
    go("#/devices")
    # Freshness: optional documents that are absent (404) never make the view stale or block controls.
    client.evaluate("new Promise(resolve => setTimeout(resolve, 6500))")
    if not client.evaluate("document.getElementById('connection').hidden && !document.getElementById('add-machine').disabled"):
        raise AssertionError("shell: the view went stale or blocked controls without optional documents: "
                             + client.evaluate("document.getElementById('connection').textContent"))
    if client.evaluate("document.getElementById('heartbeat').textContent") != "Live · 2 s":
        raise AssertionError("shell: heartbeat pill missing")
    # Leaving a page: its documents no longer age the view (page A -> page B, more than the stale limit later).
    go("#/history")
    go("#/devices")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 7000))")
    left = client.evaluate("({hidden: document.getElementById('connection').hidden, blocker: window.Hyperloom.mutationReason(), "
                           "kept: Object.keys(window.Hyperloom.state.fetched).filter(k => k.startsWith('history'))})")
    if not left["hidden"] or left["blocker"] or left["kept"]:
        raise AssertionError("shell: a page left behind still ages the view: " + json.dumps(left))
    # Palette: Ctrl+K from a focused control, type, Enter navigates; Escape returns focus.
    client.evaluate("document.getElementById('palette-open').focus()")
    chord(client, "k")
    wait("document.getElementById('palette').open && document.activeElement.id === 'palette-input'")
    client.call("Input.insertText", text="metrics hist")
    wait("document.querySelector('#palette-list li.on .pl')?.textContent === 'Metrics history'")
    if client.evaluate("document.getElementById('palette-input').getAttribute('aria-activedescendant')") != "palette-option-0":
        raise AssertionError("shell: palette active option not exposed")
    key(client, "Enter")
    wait("location.hash === '#/history' && !document.getElementById('palette').open && document.querySelector('h1').textContent === 'Metrics history'")
    client.evaluate("document.getElementById('palette-open').focus()")
    chord(client, "k")
    wait("document.getElementById('palette').open")
    client.call("Input.insertText", text="h")
    wait("document.querySelectorAll('#palette-list li[role=option]').length > 3")
    shot("palette", "light", 1440)
    key(client, "Escape")
    wait("!document.getElementById('palette').open && document.activeElement.id === 'palette-open'")
    # A blocked action names its blocker and does not run.
    client.evaluate("window.Hyperloom.state.busy = true")
    chord(client, "k")
    wait("document.getElementById('palette').open")
    client.call("Input.insertText", text="run doctor")
    wait("document.querySelector('#palette-list li.on')?.getAttribute('aria-disabled') === 'true'")
    key(client, "Enter")
    wait("/Another action is running/.test(document.getElementById('palette-status').textContent)")
    if client.evaluate("document.getElementById('action-dialog').open"):
        raise AssertionError("shell: a blocked palette action opened its dialog")
    key(client, "Escape")
    client.evaluate("window.Hyperloom.state.busy = false")
    # Ctrl+B collapses and restores the sidebar.
    client.evaluate("document.activeElement.blur()")
    chord(client, "b")
    wait("document.querySelector('.app').classList.contains('collapsed')")
    shot("sidebar-collapsed", "light", 1440)
    chord(client, "b")
    wait("!document.querySelector('.app').classList.contains('collapsed')")
    # Deep links: a query route of a registered page, a profile selection, a settings section.
    go("#/history?probe=1")
    if client.evaluate("document.querySelector('h1').textContent") != "Metrics history":
        raise AssertionError("shell: a feature page route with a query did not render")
    go("#/engines?profile=qwen-two", "document.getElementById('profile-select')?.value === 'qwen-two'")
    go("#/settings?section=panels", "document.activeElement?.id === 'prefs-panels'")
    shot("settings", "light", 1440)
    # Preferences through the real feature: true black, compact density, Spanish, refresh interval.
    select("pref-theme", "black")
    wait("document.documentElement.dataset.shade === 'black' && getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() === '#000000'")
    saved("theme", "black")
    select("pref-density", "compact")
    wait("document.documentElement.dataset.density === 'compact'")
    saved("density", "compact")
    go("#/devices")
    shot("compact", "black", 1440)
    go("#/settings")
    select("pref-language", "es")
    wait("document.querySelector('h1').textContent === 'Ajustes y emparejamiento' && document.documentElement.lang === 'es' && /Dispositivos/.test(document.getElementById('navigation').innerText)")
    saved("language", "es")
    shot("spanish", "black", 1440)
    for element, value in (("pref-language", "en"), ("pref-theme", "system"), ("pref-density", "comfortable")):
        select(element, value)
    wait("document.querySelector('h1').textContent === 'Settings & pairing' && !document.documentElement.dataset.shade")
    saved("density", "comfortable")
    select("pref-refresh_s", "10")
    saved("refresh_s", 10)
    go("#/devices")
    wait("document.getElementById('heartbeat').textContent === 'Live · 10 s'")
    for _ in range(2):    # one evaluation stays under the DevTools socket timeout
        client.evaluate("new Promise(resolve => setTimeout(resolve, 6300))")
    if not client.evaluate("document.getElementById('connection').hidden && !document.getElementById('add-machine').disabled"):
        raise AssertionError("shell: a 10 s refresh interval made the view stale between polls")
    go("#/settings")
    select("pref-refresh_s", "2")
    saved("refresh_s", 2)
    # Save ordering: two quick changes of one field; the first answer must not overwrite the newer choice, and a
    # failed second save keeps it in this browser only (a later poll and a reload do not undo it).
    go("#/settings")
    client.call("Fetch.enable", patterns=[{"urlPattern": "*/hyperloom/api/prefs/save", "requestStage": "Request"}])
    try:
        client.evaluate("(window.Hyperloom.setPref({theme: 'dark'}), window.Hyperloom.setPref({theme: 'light'}), true)")
        first = client.paused_request()
        if json.loads(first["request"].get("postData") or "{}") != {"theme": "dark"}:
            raise AssertionError("shell: first save body " + str(first["request"].get("postData")))
        client.call("Fetch.continueRequest", requestId=first["requestId"])
        second = client.paused_request()
        if json.loads(second["request"].get("postData") or "{}") != {"theme": "light"}:
            raise AssertionError("shell: second save body " + str(second["request"].get("postData")))
        if client.evaluate("window.Hyperloom.prefs().theme") != "light":
            raise AssertionError("shell: the first save's answer overwrote the newer choice")
        client.call("Fetch.failRequest", requestId=second["requestId"], errorReason="Failed")
        retry = client.paused_request()          # a refused connection is retried once
        client.call("Fetch.failRequest", requestId=retry["requestId"], errorReason="Failed")
    finally:
        client.call("Fetch.disable")
    wait("/browser only/.test(document.querySelector('#main [role=status]')?.textContent || '')")
    saved("theme", "dark")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 4500))")
    if client.evaluate("window.Hyperloom.prefs().theme + document.documentElement.dataset.theme") != "lightlight":
        raise AssertionError("shell: a poll overrode a browser-only preference")
    go("#/settings", reload=True)
    client.evaluate("new Promise(resolve => setTimeout(resolve, 3000))")
    if client.evaluate("window.Hyperloom.prefs().theme") != "light":
        raise AssertionError("shell: a reload lost a browser-only preference")
    select("pref-theme", "system")
    saved("theme", "system")
    wait("!/browser only/.test(document.querySelector('#main [role=status]')?.textContent || '')")
    # Time zones: a chosen zone applies its own rules at each timestamp (winter and summer offsets differ).
    select("pref-timezone", "Europe/Madrid")
    saved("timezone", "Europe/Madrid")
    times = client.evaluate("[1736935200, 1752573600].map(t => window.Hyperloom.time(t))")  # 10:00 UTC in January and July
    if not (times[0].startswith("11:00") and times[1].startswith("12:00")):
        raise AssertionError("shell: zone rules not applied per timestamp: " + json.dumps(times))
    client.call("Emulation.setTimezoneOverride", timezoneId="Asia/Tokyo")
    try:
        if not client.evaluate("window.Hyperloom.time(1736935200)").startswith("11:00"):
            raise AssertionError("shell: a browser in another zone did not use the chosen zone")
    finally:
        client.call("Emulation.setTimezoneOverride", timezoneId="")
    if client.evaluate("document.getElementById('pref-timezone').options.length") < 3:
        raise AssertionError("shell: the zone list from the portal is missing")
    # A zone the portal accepts but this browser cannot format: this browser is made to refuse one accepted zone
    # ("Factory"); it is not offered and, once saved, times show in UTC and the settings page says so.
    client.call("Page.addScriptToEvaluateOnNewDocument", source="""(() => {
      const Real = Intl.DateTimeFormat;
      function Refusing(locale, options) {
        if (options && options.timeZone === "Factory") throw new RangeError("Invalid time zone specified: Factory");
        return new Real(locale, options);
      }
      Refusing.prototype = Real.prototype;
      Refusing.supportedLocalesOf = Real.supportedLocalesOf.bind(Real);
      Intl.DateTimeFormat = Refusing; })();""")
    go("#/settings", "document.getElementById('pref-timezone')?.options.length > 3", reload=True)
    if client.evaluate("[...document.getElementById('pref-timezone').options].some(o => o.value === 'Factory')"):
        raise AssertionError("shell: a zone this browser cannot format is offered")
    token = client.evaluate("document.querySelector('meta[name=hyperloom-csrf]').content")
    status, _, body = server.request("POST", API + "prefs/save", json.dumps({"timezone": "Factory"}).encode(),
                                     {"Origin": server.origin, "Content-Type": "application/json", "X-Hyperloom-CSRF": token})
    if status != 200:
        raise AssertionError("shell: the portal refused a zone it lists: " + body.decode("utf-8", "replace")[:300])
    go("#/settings", "window.Hyperloom.prefs().timezone === 'Factory'", reload=True)
    wait("/Factory cannot be shown by this browser; UTC used/.test(document.getElementById('prefs-data').innerText)")
    if not (client.evaluate("window.Hyperloom.time(1736935200)").startswith("10:00") and client.evaluate("window.Hyperloom.zone().name") == "UTC"):
        raise AssertionError("shell: an unsupported saved zone did not fall back to UTC")
    select("pref-timezone", "local")
    saved("timezone", "local")
    # Recipe deep link: opens the recipe on load and again on reload.
    for reload in (False, True):
        go("#/recipes?recipe=qwen3.8-flash-next.lab", "!!document.querySelector('[data-path=\"routes.prefill_rtx.when.prompt_tokens_min\"]')", reload=reload)
    # Exceptions strip, banner, tab badge, favicon dot and notifications from deterministic scenarios.
    now = time.time()
    finding = lambda ident, severity, title, machine, age: {"id": ident, "severity": severity, "title": title,
                                                             "machine": machine, "since": now - age}
    # The fleet overview's machine rows, as its document serializes them.
    overview = {"machines": [{"id": "halo-1", "status": "online", "engines": [{"profile": "qwen", "role": "rank 0", "throughput": 41.5}]},
                             {"id": "halo-2", "status": "stale", "engines": [{"profile": "qwen", "role": "rank 1", "throughput": None, "follower_of": "halo-1"},
                                                                            {"profile": "small", "role": "replica", "throughput": 7}]}]}
    base = {"counts": {"critical": 1, "warning": 1, "info": 0}, "banner": "GPU thermal slowdown on halo-1 gpu0", "evaluated_at": now, "stale": False,
            "strip": [finding("hot", "critical", "GPU hot", "halo-1", 300), finding("disk", "warning", "Disk at 92 %", "halo-2", 4000)]}
    scenario({"health/summary": base, "overview": overview})
    client.evaluate("localStorage.setItem('hyperloom-notify', 'true')")
    go("#/devices", "document.querySelectorAll('#exceptions li.exception').length === 2", reload=True)
    wait("document.title.startsWith('(2) Hyperloom') && document.getElementById('favicon').href.includes('%23d32f2f')")
    wait("document.getElementById('throttle-banner').textContent === 'GPU thermal slowdown on halo-1 gpu0' && !document.getElementById('throttle-banner').hidden")
    wait("/41[.,]5 tok\\/s/.test(document.getElementById('nav-machines').innerText) && /7 tok.s · rank 1 of qwen · rank 0 on halo-1/.test(document.getElementById('nav-machines').innerText)")
    if client.evaluate("document.querySelector('#exceptions .pill').textContent") != "critical":
        raise AssertionError("shell: strip must list the critical finding first with its severity word")
    if client.evaluate("window.__notes.length"):
        raise AssertionError("shell: findings present at page load must not raise notifications")
    shot("strip", "light", 1440)
    viewport(1440, "dark")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 300))")
    shot("strip", "dark", 1440)
    viewport(1440, "light")
    fresh = dict(base, counts={"critical": 1, "warning": 2}, strip=base["strip"] + [finding("w2", "warning", "Collector stale", "halo-2", 20)])
    scenario({"health/summary": fresh, "overview": overview})
    wait("window.__notes.length === 1 && window.__notes[0] === 'w2' && document.title.startsWith('(3) ')")
    stale = dict(fresh, stale=True, evaluated_at=now - 600, counts={"critical": 1, "warning": 3},
                 strip=fresh["strip"] + [finding("w3", "warning", "Engine down", "halo-1", 10)])
    scenario({"health/summary": stale, "overview": overview})
    wait("/evaluation stale/.test(document.getElementById('exceptions').innerText) && document.title.startsWith('(4?) ')")
    wait("document.getElementById('throttle-banner').textContent.startsWith('Last known:')")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 2500))")
    if client.evaluate("window.__notes.length") != 1:
        raise AssertionError("shell: a stale evaluation raised a notification")
    scenario({"health/summary": dict(stale, stale=False, evaluated_at=time.time()), "overview": overview})
    wait("window.__notes.length === 2 && window.__notes[1] === 'w3'")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 2500))")
    if client.evaluate("window.__notes.length") != 2:
        raise AssertionError("shell: a finding was notified twice")
    # An empty summary ages too: clear evaluation, failing requests (stale, "(?)"), then a fresh clear answer.
    scenario({"health/summary": dict(base, counts={}, strip=[], banner=None, evaluated_at=time.time()), "overview": overview})
    wait("document.getElementById('exceptions').hidden && !document.title.startsWith('(')")
    scenario({"health/summary": "fail", "overview": overview})
    wait("!document.getElementById('exceptions').hidden && /evaluation stale/.test(document.getElementById('exceptions').innerText) && document.title.startsWith('(?) ')", timeout=40)
    scenario({"health/summary": dict(base, counts={}, strip=[], banner=None, evaluated_at=time.time()), "overview": overview})
    wait("document.getElementById('exceptions').hidden && !document.title.startsWith('(')")
    scenario({"health/summary": dict(stale, stale=False, evaluated_at=time.time()), "overview": overview})
    wait("document.title.startsWith('(4) ')")
    # Failing requests age the last known summary: marked stale, no notification; a fresh answer recovers.
    scenario({"health/summary": "fail", "overview": overview})
    wait("/evaluation stale/.test(document.getElementById('exceptions').innerText) && document.title.startsWith('(4?) ')", timeout=40)
    scenario({"health/summary": dict(stale, stale=False, evaluated_at=time.time(), strip=stale["strip"] + [finding("w4", "warning", "Late", "halo-1", 5)]), "overview": overview})
    wait("!/evaluation stale/.test(document.getElementById('exceptions').innerText) && window.__notes.length === 3 && window.__notes[2] === 'w4'")
    # A page that lists a shell document itself shares the shell's snapshot: answer, 404 (the page shows its
    # error state), then the same answer again (the page renders it again although its content is unchanged).
    owner = client.call("Page.addScriptToEvaluateOnNewDocument", source="""(window.HyperloomPages ||= {})["probe-owner"] = {
      title: "Probe owner", group: "Operations", order: 99, endpoints: () => ["health/summary"],
      render: () => `<p id="probe-owner">critical ${window.Hyperloom.state.docs["health/summary"].counts.critical}</p>`};""")["identifier"]
    try:
        scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview})
        go("#/probe-owner", "document.getElementById('probe-owner')?.textContent === 'critical 1'", reload=True)
        scenario({"health/summary": "missing", "overview": overview})
        wait("/Probe owner data unavailable/.test(document.querySelector('main').innerText) && document.getElementById('exceptions').hidden")
        scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview})
        wait("document.getElementById('probe-owner')?.textContent === 'critical 1' && !document.getElementById('exceptions').hidden")
        # An answer older than one already applied is dropped: a slow optional request sent from another page
        # arrives after the owning page's newer answer (content, then a 404) and changes nothing.
        for late in (dict(base, evaluated_at=time.time(), counts={"critical": 1}, delay_s=6),
                     dict(base, evaluated_at=time.time(), missing=True, delay_s=6)):
            go("#/devices")
            scenario({"health/summary": late, "overview": overview})
            client.evaluate("new Promise(resolve => setTimeout(resolve, 2500))")   # an optional round is now waiting
            scenario({"health/summary": dict(base, evaluated_at=time.time(), counts={"critical": 2}), "overview": overview})
            client.call("Page.navigate", url=server.url + "#/probe-owner")
            wait("document.getElementById('probe-owner')?.textContent === 'critical 2'")
            client.evaluate("new Promise(resolve => setTimeout(resolve, 7000))")
            if client.evaluate("document.getElementById('probe-owner')?.textContent") != "critical 2":
                raise AssertionError("shell: a superseded answer replaced a newer one: "
                                     + str(client.evaluate("document.querySelector('main').innerText"))[:300])
        scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview})
    finally:
        client.call("Page.removeScriptToEvaluateOnNewDocument", identifier=owner)
    # A slow optional document never holds back the screen's own documents nor makes the view stale.
    scenario({"health/summary": dict(base, evaluated_at=time.time(), delay_s=9), "overview": overview})
    go("#/devices")
    for _ in range(3):
        client.evaluate("new Promise(resolve => setTimeout(resolve, 3000))")
        age = client.evaluate("Date.now() - window.Hyperloom.state.fetched.devices")
        if age > 4500 or not client.evaluate("document.getElementById('connection').hidden && !document.getElementById('add-machine').disabled"):
            raise AssertionError(f"shell: a slow optional document delayed the screen ({age} ms since the last devices answer)")
    scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview})
    # A slow core document: the action blocker (what every mutation control and dialog asks) is set whenever a
    # current document is older than the stale limit, at any moment (not only after the next one-second check).
    scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview, "jobs_delay_s": 8})
    samples = client.evaluate("""new Promise(resolve => { const out = {aged: 0, violations: 0}; const started = Date.now();
      const timer = setInterval(() => {
        const age = Date.now() - (window.Hyperloom.state.fetched.devices || Date.now());
        if (age > 6000) { out.aged++; if (!window.Hyperloom.mutationReason()) out.violations++; }
        if (Date.now() - started > 9000) { clearInterval(timer); resolve(out); } }, 100); })""")
    if not samples["aged"] or samples["violations"]:
        raise AssertionError("shell: the action blocker was clear with an old document: " + json.dumps(samples))
    scenario({"health/summary": dict(base, evaluated_at=time.time()), "overview": overview})
    wait("!window.Hyperloom.mutationReason()", timeout=40)
    # Info tooltips: shown on focus, dismissed by Escape and kept dismissed across a poll; the pointer can move
    # from the button into the tooltip.
    go("#/stats", "!!document.querySelector('main .info-btn')")
    tip_id = client.evaluate("(() => { const b = document.querySelector('main .info-btn'); b.focus(); return b.getAttribute('aria-describedby'); })()")
    shown = f"getComputedStyle(document.getElementById({json.dumps(tip_id)})).display === 'block'"
    wait(shown)
    key(client, "Escape")
    wait("!(" + shown + ")")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 2600))")
    if client.evaluate(shown) or client.evaluate("document.activeElement.getAttribute('aria-describedby')") != tip_id:
        raise AssertionError("shell: a poll reopened a dismissed tooltip or lost its focus")
    client.evaluate("document.activeElement.blur()")
    box = client.evaluate(f"(() => {{ const b = document.querySelector('[aria-describedby={json.dumps(tip_id)}]').getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2, b.bottom]; }})()")
    for x, y in ((box[0], box[1]), (box[0], box[2] + 3)):
        client.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        if not client.evaluate(shown):
            raise AssertionError(f"shell: the tooltip closed while the pointer moved from its button towards it ({x}, {y})")
    tip_box = client.evaluate(f"(() => {{ const b = document.getElementById({json.dumps(tip_id)}).getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; }})()")
    client.call("Input.dispatchMouseEvent", type="mouseMoved", x=tip_box[0], y=tip_box[1])
    if not client.evaluate(shown):
        raise AssertionError("shell: the tooltip closed under the pointer")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 2600))")        # a poll re-renders the page
    if not client.evaluate(shown):
        raise AssertionError("shell: a poll closed the tooltip under the pointer")
    client.call("Input.dispatchMouseEvent", type="mouseMoved", x=5, y=995)
    # Narrow: the bottom tab bar, the strip and no horizontal overflow; nothing hidden under the bar.
    viewport(390, "light")
    go("#/devices", "document.querySelectorAll('#tabbar a').length === 4")
    metrics = client.evaluate("""(() => { const bar = document.getElementById('tabbar').getBoundingClientRect();
      const pad = parseFloat(getComputedStyle(document.querySelector('.main')).paddingBottom);
      return {display: getComputedStyle(document.getElementById('tabbar')).display, height: bar.height, pad,
              scroll: document.documentElement.scrollWidth, current: document.querySelector('#tabbar a[aria-current=page]')?.textContent}; })()""")
    if metrics["display"] != "grid" or metrics["pad"] < metrics["height"] or metrics["scroll"] > 390 or metrics["current"] != "Machines":
        raise AssertionError("shell: narrow tab bar " + json.dumps(metrics))
    shot("tabbar", "light", 390)
    viewport(390, "dark")
    client.evaluate("new Promise(resolve => setTimeout(resolve, 300))")
    shot("tabbar", "dark", 390)
    go("#/settings")
    shot("settings", "dark", 390)
    viewport(1440, "dark")
    go("#/settings")
    shot("settings", "dark", 1440)
    viewport(390, "light")
    go("#/settings")
    shot("settings", "light", 390)
    viewport(1440, "light")
    scenario({"health/summary": "missing", "overview": "missing"})
    go("#/devices", "document.getElementById('exceptions').hidden && !document.title.startsWith('(')", reload=True)
    scenario(None)
    client.evaluate("localStorage.removeItem('hyperloom-notify')")
    if client.errors:
        raise AssertionError("shell: browser console/exception errors: " + json.dumps(client.errors))
    return "palette, Ctrl+B, deep links, black/compact/Spanish/refresh preferences, strip/banner/badge/favicon/notifications, tab bar"


def shell_service_worker(client, server):
    """The installable shell's worker: only static files come from its cache; navigations, API reads and POSTs
    fail offline instead of being answered from a cache; a restarted portal's new CSRF page is shown; when the
    portal stops serving /sw.js the page removes the worker, and the worker removes itself on a navigation the
    page script does not see. Returns the fixture server now serving (the walkthrough restarts it)."""
    def wait(expression, timeout=30):
        try:
            return eventually(lambda: client.evaluate(expression), timeout=timeout)
        except AssertionError:
            raise AssertionError("service worker: timed out waiting for " + expression) from None

    registered = "navigator.serviceWorker.getRegistrations().then(rs => rs.filter(r => (r.active || r.waiting || r.installing)?.scriptURL === location.origin + '/sw.js').length)"
    shell_caches = "caches.keys().then(ks => ks.filter(k => k.startsWith('hyperloom-shell-')))"
    client.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    client.call("Page.navigate", url=server.url + "#/devices")
    wait("document.readyState === 'complete'")
    wait("Promise.race([navigator.serviceWorker.ready.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 1000))])")
    client.call("Page.reload")
    wait("document.readyState === 'complete' && !!navigator.serviceWorker.controller")
    wait("document.querySelector('main').getAttribute('aria-busy') === 'false'")
    entries = client.evaluate("caches.open('hyperloom-shell-v1').then(c => c.keys()).then(rs => rs.map(r => new URL(r.url).pathname))")
    if "/app.js" not in entries or any(p.startswith("/hyperloom/api") or p in ("/", "/index.html", "/sw.js") for p in entries):
        raise AssertionError("service worker: unexpected cache entries " + json.dumps(entries))
    if client.evaluate(shell_caches) != ["hyperloom-shell-v1"]:
        raise AssertionError("service worker: unexpected caches")
    client.call("Network.enable")
    client.call("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
    try:
        offline = client.evaluate("""(async () => {
          const outcome = async work => { try { const r = await work(); return r.status; } catch (_) { return 'failed'; } };
          return {api: await outcome(() => fetch('/hyperloom/api/devices', {cache: 'no-store'})),
                  post: await outcome(() => fetch('/hyperloom/api/prefs/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})),
                  script: await outcome(() => fetch('/app.js'))}; })()""")
        if offline != {"api": "failed", "post": "failed", "script": 200}:
            raise AssertionError("service worker: offline answers " + json.dumps(offline))
        client.call("Page.navigate", url=server.url + "?offline=1")
        client.evaluate("new Promise(resolve => setTimeout(resolve, 1500))")
        if client.evaluate("!!document.querySelector('meta[name=hyperloom-csrf]')"):
            raise AssertionError("service worker: an offline navigation was answered from a cache")
        # The browser's own offline error page refuses the smoke's start-up script (storage): not a portal error.
        client.errors[:] = [e for e in client.errors
                            if not e.get("params", {}).get("exceptionDetails", {}).get("url", "").startswith("chrome-error://")]
    finally:
        client.call("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
    # Back from the browser's error page on a fresh session.
    client.call("Page.navigate", url=server.url + "#/devices")
    time.sleep(2)
    client.reconnect()
    client.call("Page.reload")
    wait("document.readyState === 'complete' && !!document.querySelector('meta[name=hyperloom-csrf]')")
    before = client.evaluate("document.querySelector('meta[name=hyperloom-csrf]').content")
    server.process.terminate()
    server.process.wait(timeout=10)
    restarted = FixtureServer(port=server.port)
    try:
        client.call("Page.reload")
        wait("document.readyState === 'complete' && !!document.querySelector('meta[name=hyperloom-csrf]')")
        after = client.evaluate("document.querySelector('meta[name=hyperloom-csrf]').content")
        served = re.search(rb'name="hyperloom-csrf" content="([^"]+)"', restarted.request("GET", "/")[2])[1].decode()
        if after == before or after != served:
            raise AssertionError("service worker: the reloaded page did not show the new portal's CSRF page")
        # The portal stops serving /sw.js: the page removes the worker and the shell caches on load.
        (restarted.directory / "no-sw").touch()
        client.call("Page.reload")
        wait(f"{registered}.then(n => n === 0)")
        wait(f"{shell_caches}.then(ks => ks.length === 0)")   # page-side removal
        # Served again: registered again. Then the worker alone notices on a navigation the page never runs.
        (restarted.directory / "no-sw").unlink()
        client.call("Page.reload")
        wait("Promise.race([navigator.serviceWorker.ready.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 1000))])")
        wait(f"{registered}.then(n => n === 1)")
        (restarted.directory / "no-sw").touch()
        client.call("ServiceWorker.enable")
        for attempt in range(3):
            # A stopped worker starts again for the next navigation with no recent self-check. The stop command can
            # take long on a loaded machine; a later attempt repeats it.
            try:
                client.socket.settimeout(45)
                client.call("ServiceWorker.stopAllWorkers")
                client.socket.settimeout(10)
            except OSError:                  # no answer in time: continue on a fresh session
                client.reconnect()
            time.sleep(1.5)
            client.call("Page.navigate", url=server.url + f"not-a-page-{attempt}")
            wait("document.readyState === 'complete' && !document.querySelector('meta[name=hyperloom-csrf]')")
            try:
                wait(f"{registered}.then(n => n === 0)", timeout=15)
                break
            except AssertionError:
                if attempt == 2:
                    raise
        wait(f"{shell_caches}.then(ks => ks.length === 0)")
        client.call("ServiceWorker.disable")
        if client.errors:
            raise AssertionError("service worker: console/exception errors: " + json.dumps(client.errors))
    finally:
        (restarted.directory / "no-sw").unlink(missing_ok=True)
        restarted.close()
    return "static-only cache, offline navigation/API/POST not cached, new CSRF page after restart, page-side and worker-side removal"


def seed_fixtures(directory, now):
    """Feature fixture data: every tests/ui/seeds/<name>.py may define seed(store, now) (telemetry history)
    and patches(directory, now) -> [(owner, attribute, value)] (fake node verbs, engine documents)."""
    import importlib.util
    from hyperloom.telemetry.store import Store
    store = Store(directory / "hyperloom" / "telemetry.sqlite3")
    patches = []
    for path in sorted((ROOT / "tests" / "ui" / "seeds").glob("*.py")):
        spec = importlib.util.spec_from_file_location("hyperloom_ui_seed_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "seed"):
            module.seed(store, now)
        if hasattr(module, "patches"):
            patches.extend(module.patches(directory, now))
    store.close()
    return patches


def browser_smoke(chrome, server, output, *, keyboard=False, keyboard_only=False, recipes_only=False,
                  refusals_only=False):
    with tempfile.TemporaryDirectory(prefix="hyperloom-chrome-", ignore_cleanup_errors=True) as profile:
        port = free_port()
        command = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   "--disable-domain-reliability", "--disable-extensions", "--metrics-recording-only",
                   "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication",
                   "--proxy-server=direct://", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                   "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
                   f"--user-data-dir={profile}", "about:blank"]
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.append("--no-sandbox")
        with (output / "chrome.log").open("w+b") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            client = None
            try:
                def ready():
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError("Chrome exited: " + log.read().decode("utf-8", "replace"))
                    try:
                        return next((row for row in get_json(port, "/json/list") if row["type"] == "page"), None)
                    except OSError:
                        return None
                target = eventually(ready)
                client = DevTools(target["webSocketDebuggerUrl"])
                client.call("Runtime.enable")
                client.call("Page.enable")
                client.call("Page.addScriptToEvaluateOnNewDocument", source="localStorage.removeItem('hyperloom-theme');")
                captures = 0
                for width in (() if keyboard_only or recipes_only or refusals_only else (1440, 390)):
                    client.call("Emulation.setDeviceMetricsOverride", width=width, height=1000,
                                deviceScaleFactor=1, mobile=False)
                    backgrounds = {}
                    for theme in ("light", "dark"):
                        client.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme},
                                                                          {"name": "prefers-reduced-motion", "value": "reduce"}])
                        for route, title in zip(ROUTES, TITLES):
                            client.call("Page.navigate", url=server.url + "#/" + route)
                            eventually(lambda: client.evaluate("document.readyState === 'complete' && !!document.querySelector('main') && document.body.innerText.length > 100"))
                            client.evaluate("new Promise(resolve => setTimeout(resolve, 2500))")
                            metrics = client.evaluate("({width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth, theme:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(), heading:document.querySelector('h1')?.textContent, body:document.body.innerText})")
                            if metrics["width"] != width:
                                raise AssertionError(f"{route}: expected real {width}px viewport, got {metrics['width']}")
                            if metrics["heading"] != title:
                                raise AssertionError(f"{route}: route did not render: {metrics}")
                            if client.evaluate("document.querySelector('main').getAttribute('aria-busy') === 'true'"):
                                raise AssertionError(f"{route}: API rendering is still loading")
                            backgrounds[theme] = metrics["theme"]
                            if metrics["scroll"] > width:
                                raise AssertionError(f"{route}/{width}: document overflows to {metrics['scroll']}px")
                            if len(set(client.evaluate("[...document.querySelectorAll('#navigation svg')].map(s => s.innerHTML)"))) != client.evaluate("document.querySelectorAll('#navigation a').length"):
                                raise AssertionError("Navigation icons must be distinct")
                            if route == "devices":
                                for text in ("Windows 11", "80.0 GB free of 131.0 GB", "ROCm 10.1.0", "unified · GPU-visible", "4/4 streams openable · layout 1 (host)", "Cable B trained below Gen3 x2"):
                                    if text not in metrics["body"]:
                                        raise AssertionError("Missing device summary: " + text)
                                for text in ("PRETTY_NAME=", "gtt_total_bytes", "vEthernet", "/data0", "system: Windows"):
                                    if text in metrics["body"]:
                                        raise AssertionError("Raw probe detail reached device summary: " + text)
                                if client.evaluate("[...document.querySelectorAll('.dev .hd .pill')].map(p => p.textContent).join(',')") != "online,online,degraded":
                                    raise AssertionError("Only the machine with a cable warning should be degraded")
                                if client.evaluate("document.querySelector('.packages')?.open"):
                                    raise AssertionError("ROCm package details should start collapsed")
                            if route == "recipes":
                                for text in ("Profiles of", "qwen3.8-flash-next.lab", "Deployment profile", "Active"):
                                    if text not in metrics["body"]:
                                        raise AssertionError("Recipes page is missing: " + text)
                            if route == "topology" and not client.evaluate("!!document.querySelector('svg.topo')"):
                                raise AssertionError("Cluster map missing")
                            if route == "topology" and width == 390:
                                if not client.evaluate("document.querySelectorAll('.topo-link-list article').length === 7 && getComputedStyle(document.querySelector('.topo-link-list')).display === 'grid'"):
                                    raise AssertionError("Narrow topology must show readable link cards")
                            if route == "engines":
                                if "{port}" in metrics["body"] or "Production unavailable" in metrics["body"]:
                                    raise AssertionError("Unresolved preset parameters reached Engines")
                                if "engine default" not in metrics["body"] or "saved launch argv/env" not in metrics["body"]:
                                    raise AssertionError("Engine defaults or saved configuration missing")
                            if client.errors:
                                raise AssertionError("Browser console/exception errors: " + json.dumps(client.errors))
                            screenshot = client.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                            png = base64.b64decode(screenshot["data"])
                            (output / f"{route}-{width}-{theme}.png").write_bytes(png)
                            if png[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack("!II", png[16:24]) != (width, 1000):
                                raise AssertionError("Screenshot dimensions do not match the emulated viewport")
                            captures += 1
                            print(f"PASS {route} {width}x1000 {theme}", flush=True)
                        captures += feature_pages(client, server, output, width, theme)
                    if not backgrounds.get("light") or backgrounds["light"] == backgrounds.get("dark"):
                        raise AssertionError("System light/dark preference did not change the rendered theme")
                if not keyboard_only and not recipes_only and not refusals_only:
                    print("PASS shell walkthrough: " + shell_walkthrough(client, server, output), flush=True)
                if keyboard or keyboard_only:
                    # Desktop makes every opener visible; the manual walkthrough
                    # additionally covers the narrow navigation menu and NVDA.
                    client.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                                deviceScaleFactor=1, mobile=False)
                    keyboard_walkthrough(client, server)
                # Drain events after the final screenshot too.
                client.evaluate("true")
                if client.errors:
                    raise AssertionError(json.dumps(client.errors))
                if not keyboard_only:
                    print("PASS preflight refusal: " + preflight_refusal_walkthrough(client, server), flush=True)
                if not keyboard_only and not refusals_only:
                    print("PASS recipes walkthrough: edit, inline refusal, fix, preview, " + recipes_walkthrough(client, server, output)
                          + ", save copy", flush=True)
                    if not recipes_only:
                        print("PASS service worker: " + shell_service_worker(client, server), flush=True)
                return captures
            finally:
                if client:
                    client.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contrast-only", action="store_true")
    parser.add_argument("--keyboard", action="store_true", help="also check dialog keyboard focus and destructive Enter handling")
    parser.add_argument("--keyboard-only", action="store_true", help="run the dialog keyboard pass without screenshots")
    parser.add_argument("--recipes-only", action="store_true", help="run only the Recipes & profiles walkthrough")
    parser.add_argument("--refusals-only", action="store_true", help="run only the synchronous launch refusal walkthrough")
    parser.add_argument("--serve", action="store_true", help="keep a fake-backed UI running for keyboard review")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--chrome", help="Chrome/Chromium executable (or set CHROME)")
    parser.add_argument("--output", type=Path, help="screenshot directory (default: a retained temp directory)")
    parser.add_argument("--fixture-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--blocked", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture_server:
        return fixture_server(args)
    if args.serve:
        with FixtureServer(port=args.port, blocked=args.blocked) as server:
            print("Fixture portal: " + server.url + "#/devices", flush=True)
            print("All actions are faked; Ctrl+C stops this server.", flush=True)
            try:
                server.process.wait()
            except KeyboardInterrupt:
                pass
        return 0
    print(f"PASS {check_contrast(themes_checked=('light', 'dark', 'black'))} text/surface contrast pairs (light, dark, true black)")
    if args.contrast_only:
        return 0
    chrome = args.chrome or find_chrome()
    if not chrome:
        print("SKIP browser smoke: Chrome/Chromium is absent (set CHROME or --chrome).")
        return 0
    output = args.output or Path(tempfile.mkdtemp(prefix="hyperloom-ui-screenshots-"))
    output.mkdir(parents=True, exist_ok=True)
    print("Screenshots: " + str(output), flush=True)
    with FixtureServer() as server:
        count = browser_smoke(chrome, server, output, keyboard=args.keyboard, keyboard_only=args.keyboard_only,
                              recipes_only=args.recipes_only, refusals_only=args.refusals_only)
    print(f"PASS {count} route/viewport/theme captures; no console errors" if count else "PASS requested walkthrough; no console errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
