"""Offline portal fixtures, Chrome smoke checks, and WCAG token checks (stdlib only)."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import hashlib
import http.client
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
ROUTES = ("devices", "topology", "models", "engines", "stats", "logs", "settings")
TITLES = ("Devices", "Topology & links", "Models & placement", "Engines", "Live stats", "Logs", "Settings & pairing")
API = "/hyperloom/api/"


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
            return response.status, dict(response.getheaders()), response.read()
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
    from hyperloom import cli, config, orchestrate, remote, secrets
    from hyperloom.web import api as web_api

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
    inspection = json.loads((ROOT / "tests/fixtures/web/inspection.json").read_text(encoding="utf-8"))
    for mid, system, access, address in (("workstation", "windows", "local", "192.0.2.20"),
                                          ("halo-2", "linux", "ssh", "192.0.2.11")):
        cluster.machines[mid] = config.Machine(mid, system, access, address, mid, 22,
                                             "/opt/hyperloom/" + mid, True, [{"id": "gpu"}])
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
    router_status = json.loads((fixture_path / "router-status.json").read_text(encoding="utf-8"))
    router_status["replicas"] = [router_status["replicas"][0]]
    router_status["replicas"][0]["endpoint"] = "127.0.0.1:18501"
    engine_status = json.loads((fixture_path / "engine-status.json").read_text(encoding="utf-8"))
    for index, request in enumerate(engine_status.get("requests", [])):
        request["time"] = time.time() - index - 1
    engine_metrics = (fixture_path / "engine-metrics.txt").read_text(encoding="utf-8")

    def record(action, **values):
        with lock, (directory / "calls.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"action": action, **values}) + "\n")

    def up(*a, **kw):
        cancelled.clear()
        record("up", arguments=kw)
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

    def status(profile=None):
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
            return {"release_id": "a" * 12, "nodes": ["halo-1"], "machines": {"halo-1": {}},
                    "started_at": time.time() - 60, "router": {"machine": "local", "directory": str(directory / "router")},
                    "devices": ["halo-1:gpu"],
                    "process_specs": [{"machine_id": "halo-1", "role": "replica", "argv": ["/opt/hyperloom/halo-1/releases/" + "a" * 12 + "/bin/strix-hip-decode-token", "--http-port", "18501"], "env": {"STRIX_COHORT_ROWS": "32"}}]}
        if path.name == "router.json":
            return {"listen": {"address": "127.0.0.1", "port": 18600}, "policy": "nodes"}
        if path.name == "state.json":
            return {"state": "ready", "pid": 1000}
        raise FileNotFoundError(path)

    with ExitStack() as stack:
        replacements = [(config, "load", lambda *a, **kw: cluster),
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


def check_contrast(path=None):
    css = (path or ROOT / "hyperloom/web/static/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    themes = {}
    for theme, selector in (("light", r":root\s*\{"),
                            ("dark", r":root\[data-theme\s*=\s*[\"']?dark[\"']?\]\s*\{")):
        match = re.search(selector + r"([^{}]*)\}", css, re.S)
        if not match:
            raise AssertionError("missing explicit " + theme + " colour tokens")
        themes[theme] = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match[1]))
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

    def close(self):
        self.stream.close()
        self.socket.close()

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
        response = self.responses.pop(sequence)
        if "error" in response:
            raise AssertionError(f"{method}: {response['error']}")
        return response.get("result", {})

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
            raise AssertionError("Dialog failed to return focus: " + action)
        if client.errors:
            raise AssertionError(json.dumps(client.errors))
        print("PASS keyboard " + action + " (Tab, reverse Tab, Space, Escape)", flush=True)


def browser_smoke(chrome, server, output, *, keyboard=False, keyboard_only=False):
    with tempfile.TemporaryDirectory(prefix="hyperloom-chrome-") as profile:
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
                for width in (() if keyboard_only else (1440, 390)):
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
                            if len(set(client.evaluate("[...document.querySelectorAll('#navigation svg')].map(s => s.innerHTML)"))) != 7:
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
                    if not backgrounds.get("light") or backgrounds["light"] == backgrounds.get("dark"):
                        raise AssertionError("System light/dark preference did not change the rendered theme")
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
    print(f"PASS {check_contrast()} text/surface contrast pairs")
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
        count = browser_smoke(chrome, server, output, keyboard=args.keyboard, keyboard_only=args.keyboard_only)
    print(f"PASS {count} route/viewport/theme captures; no console errors" if count else "PASS keyboard walkthrough; no console errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
