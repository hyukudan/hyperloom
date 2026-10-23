"""Bounded GPU turns and their foreground owner (Python 3.10+, stdlib only)."""

import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid


VERBS = {"lease", "cancel", "status", "logs", "stop", "put-secret", "peer-run",
         "supervise-internal"}
MAX_JSON = 256 * 1024
MAX_LOG = 64 * 1024
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
TERMINAL = {"stopped", "failed", "aborted", "cancelled"}
USB_LAYOUT = Path("/run/usb4stream-layout.json")
_launches = []


def text(value, name):
    if (not isinstance(value, str) or not value
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)):
        raise ValueError(name + " must be nonempty text without control characters")
    return value


def path(value):
    value = text(value, "path")
    result = Path(value)
    if not result.is_absolute() or ".." in result.parts:
        raise ValueError("absolute path without parent traversal required")
    return result


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", value):
        raise ValueError("invalid name")
    return value


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be a positive finite number")
    return value


def read_json(filename):
    with filename.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    return decode(raw)


def decode(raw):
    if len(raw) > MAX_JSON:
        raise ValueError("JSON exceeds 256 KiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON")
        return value

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=finite,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def atomic(filename, raw):
    temporary = filename.with_name(filename.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(filename, value):
    atomic(filename, json.dumps(value, allow_nan=False).encode("utf-8"))


def inside(root, *parts):
    result = root.joinpath(*parts)
    if not result.resolve().is_relative_to(root):
        raise ValueError("path escapes marked root")
    return result


def marked_root(value, lab=None):
    requested = path(value)
    if requested.is_symlink():
        raise ValueError("root must not be a symlink")
    root = requested.resolve()
    home = Path.home().resolve()
    if home == root or home.is_relative_to(root):
        raise ValueError("root contains HOME")
    policy_file = inside(root, "etc", "install-policy.json")
    policy = read_json(policy_file) if policy_file.is_file() else {}
    protected = list(policy.get("asset_paths", []))
    protected.extend(value for value in (lab, policy.get("lab_dir")) if value is not None)
    for value in protected:
        protected_path = path(value).resolve()
        if protected_path.is_relative_to(root) or root.is_relative_to(protected_path):
            raise ValueError("root overlaps lab or configured asset path")
    if any((ancestor / ".git").exists() for ancestor in (root, *root.parents)):
        raise ValueError("root is in a repository")
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if relative.parts and relative.parts[0] == "src":
            dirs[:] = []  # Installed component sources are expected repositories.
        elif ".git" in dirs or ".git" in files:
            raise ValueError("root contains a repository")
    marker = inside(root, ".telar-root")
    if marker.is_symlink() or not marker.is_file() or not marker.read_text(encoding="utf-8").strip():
        raise ValueError("marked install root required")
    for name in ("run", "log", "etc"):
        inside(root, name).mkdir(exist_ok=True)
    return root


def environment(spec_env, runtime_env=None):
    if not isinstance(spec_env, dict) or (runtime_env is not None and not isinstance(runtime_env, dict)):
        raise ValueError("env must be an object")
    result = {"PATH": BASE_PATH, "HOME": str(Path.home())}
    for key, value in (runtime_env or {}).items():
        if key != "LD_LIBRARY_PATH":
            raise ValueError("unsupported release runtime_env")
        # Each loader path is absolute, including when several are supplied.
        for component in text(value, "runtime_env").split(":"):
            parsed = PurePosixPath(component)
            if not parsed.is_absolute() or ".." in parsed.parts:
                raise ValueError("runtime loader path must be absolute")
        result[key] = value
    for key, value in spec_env.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key.startswith(("LD_", "PYTHON")) or key in {"BASH_ENV", "ENV", "PATH", "HOME", "GPU_TURN_MAX"}):
            raise ValueError("forbidden spec env name")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("env values must be strings without NUL")
        result[key] = value
    return result


def runtime_env(root, argv):
    executable = path(argv[0]).resolve()
    releases = inside(root, "releases")
    if executable.is_relative_to(releases):
        relative = executable.relative_to(releases)
        if len(relative.parts) != 3 or relative.parts[1] != "bin":
            raise ValueError("immutable release executable required")
        manifest = read_json(inside(root, "releases", relative.parts[0], "MANIFEST.json"))
        return manifest.get("runtime_env", {})
    return {}


def health_url(value):
    parsed = urlsplit(text(value, "health_url"))
    try:
        address = ipaddress.ip_address(parsed.hostname)
        valid = (address.is_private or address.is_loopback) and not (
            address.is_unspecified or address.is_multicast or address.is_link_local)
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            valid = False
    except (ValueError, TypeError):
        valid = False
    if (not valid or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path != "/health" or parsed.query or parsed.fragment):
        raise ValueError("health_url must be private numeric HTTP /health with a port")
    return value


def validate_process(proc):
    if not isinstance(proc, dict):
        raise ValueError("process must be an object")
    proc = dict(proc)
    identifier(proc.get("name"))
    if proc.get("machine", "local") not in {"local", "peer"}:
        raise ValueError("invalid process machine")
    proc.setdefault("machine", "local")
    argv = proc.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 4096:
        raise ValueError("argv must be a nonempty list")
    for argument in argv:
        if not isinstance(argument, str) or "\0" in argument:
            raise ValueError("argv entries must be strings without NUL")
    path(argv[0])
    environment(proc.get("env", {}))
    proc.setdefault("env", {})
    if proc.get("ready") not in {"qwen-stderr", "ds41-json", "http-health"}:
        raise ValueError("unknown readiness adapter")
    health_url(proc.get("health_url"))
    proc["ready_timeout_s"] = number(proc.get("ready_timeout_s", 120), "ready_timeout_s")
    if proc.get("key_file") is not None:
        path(proc["key_file"])
    return proc


def validate_spec(payload, now=None):
    spec = dict(payload)
    if not re.fullmatch(r"tlr-[a-z0-9-]{1,40}", text(spec.get("label"), "label")):
        raise ValueError("invalid lease label")
    kind = spec.get("kind")
    if kind not in {"single", "pair", "none"}:
        raise ValueError("invalid lease kind")
    path(spec.get("root"))
    if spec.get("lab_dir") is not None:
        path(spec["lab_dir"])
    if kind != "none" and spec.get("lab_dir") is None:
        raise ValueError("GPU leases require a lab turn script")
    deadline = number(spec.get("deadline_s", 1140), "deadline_s")
    spec["deadline_s"] = deadline
    approval = spec.get("approval")
    if approval is not None:
        if not isinstance(approval, dict):
            raise ValueError("approval must be an object")
        lease_s = number(approval.get("lease_s"), "approval lease_s")
        if type(lease_s) is not int:
            raise ValueError("approval lease_s must be an integer")
        text(approval.get("ref"), "approval reference")
        if not approval["ref"].strip():
            raise ValueError("approval reference is empty")
        end = number(approval.get("end_epoch"), "approval end_epoch")
        if end <= (time.time() if now is None else now) or lease_s < deadline + 60:
            raise ValueError("approval expired or duration insufficient")
    if deadline > 1140 and approval is None and kind != "none":
        raise ValueError("long GPU lease requires explicit approval")
    processes = spec.get("processes")
    if not isinstance(processes, list) or not 1 <= len(processes) <= 128:
        raise ValueError("processes must contain 1..128 entries")
    spec["processes"] = [validate_process(proc) for proc in processes]
    names = [proc["name"] for proc in spec["processes"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate process names")
    if spec.get("peer_ssh") is not None:
        target = spec["peer_ssh"]
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", target) or target.startswith("-"):
            raise ValueError("invalid peer ssh target")
    if spec.get("peer_root") is not None:
        path(spec["peer_root"])
    if spec.get("peer_lab_dir") is not None:
        path(spec["peer_lab_dir"])
    if any(proc["machine"] == "peer" for proc in spec["processes"]):
        if kind != "pair" or not spec.get("peer_ssh") or not spec.get("peer_root"):
            raise ValueError("peer processes require a pair with peer_ssh and peer_root")
    if spec.get("layout_required") not in {None, "1"}:
        raise ValueError("only production layout 1 is supported")
    if spec.get("layout_required") and not spec.get("lab_dir"):
        raise ValueError("layout check requires a lab directory")
    return spec


def record_path(root, lease):
    return inside(root, "run", "lease-" + identifier(lease) + ".json")


def stop_path(root, lease):
    return inside(root, "run", "lease-" + identifier(lease) + ".stop")


@contextmanager
def state_lock(root, lease):
    import fcntl

    filename = inside(root, "run", "lease-" + identifier(lease) + ".lock")
    with os.fdopen(os.open(filename, os.O_RDWR | os.O_CREAT, 0o600), "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def start_lease(payload):
    spec = validate_spec(payload)
    root = marked_root(spec["root"], spec.get("lab_dir"))
    if os.name != "posix":
        raise ValueError("node leases require POSIX process groups")
    spec["root"] = str(root)
    lease = uuid.uuid4().hex
    spec["lease"] = lease
    filename = inside(root, "run", "spec-" + lease + ".json")
    write_json(filename, spec)
    state = {"lease": lease, "label": spec["label"], "kind": spec["kind"],
             "state": "queued", "queued_at": time.time(), "processes": [],
             "deadline_s": spec["deadline_s"], "approval": spec.get("approval"), "reason": None}
    if spec.get("profile") is not None:
        state["profile"] = text(spec["profile"], "profile")
    write_json(record_path(root, lease), state)
    boot = Path(__file__).resolve().with_name("telar-node")
    command = [str(boot), "supervise-internal", str(filename)]
    env = environment({})
    if spec["kind"] != "none":
        script = "gpu-turn-pair.sh" if spec["kind"] == "pair" else "gpu-turn.sh"
        command = [str(path(spec["lab_dir"]) / script), spec["label"], *command]
        if spec["kind"] == "single" and spec.get("approval") is not None:
            env["GPU_TURN_MAX"] = str(spec["approval"]["lease_s"])
    # start_new_session is setsid without an extra executable or shell.
    try:
        with inside(root, "log", "lease-" + lease + ".log").open("ab") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=env, start_new_session=True, close_fds=True)
        # The supervisor waits for this separate publication, avoiding a queued/acquired race.
        write_json(inside(root, "run", "owner-" + lease + ".json"), {"pid": process.pid})
        _launches.append(process)
    except OSError:
        state.update(state="failed", reason="turn-launch-failed")
        write_json(record_path(root, lease), state)
        raise
    return {"lease": lease}


def ssh_command(target, command):
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "--", target, command]


def peer_call(spec, verb, payload):
    # Both nodes bootstrap to this fixed home-relative location.
    command = '"$HOME/.telar-boot/telar-node" ' + verb
    result = subprocess.run(ssh_command(spec["peer_ssh"], command),
                            input=json.dumps(payload).encode(), capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("peer " + verb + " failed")
    reply = decode(result.stdout)
    if reply.get("error"):
        raise ValueError("peer " + verb + " failed")
    return reply


def layout_check(spec):
    lab = path(spec["lab_dir"])
    holder = (lab / ".gpu-turn" / "holder").read_text().split()
    index = 1 if holder and holder[0] == "PAIR" else 0
    if len(holder) <= index or holder[index] != spec["label"]:
        raise ValueError("lab holder does not show this lease label")
    if spec.get("layout_required") is None:
        return
    required = spec["layout_required"]
    words = (lab / ".gpu-turn" / "layout-state").read_text().split()
    if not words or words[0] != required:
        raise ValueError("lab layout mismatch")
    layout = read_json(USB_LAYOUT)
    if str(layout.get("layout", layout.get("layout_id"))) != required:
        raise ValueError("USB4 layout mismatch")
    if spec["kind"] == "pair":
        reply = peer_call(spec, "supervise-internal", {"check_layout": required,
                          "lab_dir": spec.get("peer_lab_dir") or spec["lab_dir"]})
        if not reply.get("layout_ok"):
            raise ValueError("peer layout mismatch")


def log_path(root, lease, name, stream):
    return inside(root, "log", "lease-" + identifier(lease) + "-" + identifier(name) + "." + stream + ".log")


def tail_file(filename, count=MAX_LOG):
    try:
        with filename.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - count))
            return stream.read(count).decode("utf-8", errors="ignore")
    except FileNotFoundError:
        return ""


def adapter_ready(adapter, stdout, stderr):
    if adapter == "http-health":
        return True
    if adapter == "qwen-stderr":
        return bool(re.search(r"strix serve: listening on \S+:\d+ model\b", stderr))
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "READY":
            return True
    return False


def http(url, *, drain=False, key_file=None, timeout=0.5):
    parsed = urlsplit(url)
    if drain:
        # Admin calls stay on the process's host, even when health binds a private IP.
        parsed = parsed._replace(netloc="127.0.0.1:" + str(parsed.port), path="/admin/drain")
    headers = {}
    if key_file:
        key = path(key_file).read_text(encoding="utf-8").strip()
        headers["Authorization"] = "Bearer " + key
    request = Request(urlunsplit(parsed), data=b"{}" if drain else None,
                      headers=headers, method="POST" if drain else "GET")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def key_file(proc):
    if proc.get("key_file"):
        return proc["key_file"]
    for flag in ("--http-api-key-file", "--api-key-file"):
        if flag in proc["argv"]:
            index = proc["argv"].index(flag)
            if index + 1 < len(proc["argv"]):
                return proc["argv"][index + 1]
    return None


def ds41_follower(proc):
    argv = proc["argv"]
    return (proc["ready"] == "ds41-json" and "--rank" in argv
            and argv[argv.index("--rank") + 1:argv.index("--rank") + 2] == ["1"])


def group_signal(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


class Child:
    def __init__(self, root, lease, proc, spec=None):
        self.root, self.lease, self.spec, self.proc = root, lease, spec, proc
        self.peer = proc["machine"] == "peer" and spec is not None
        self.files = []
        self.process = None
        self.started = time.monotonic()
        self.ready = False
        self.peer_pid = None

    def start(self):
        argv = self.proc["argv"]
        env = environment(self.proc["env"], runtime_env(self.root, argv)) if not self.peer else environment({})
        for stream in ("stdout", "stderr"):
            self.files.append(log_path(self.root, self.lease, self.proc["name"], stream).open("ab", buffering=0))
        try:
            if self.peer:
                argv = ssh_command(self.spec["peer_ssh"], '"$HOME/.telar-boot/telar-node" peer-run')
            self.process = subprocess.Popen(argv, env=env, start_new_session=True,
                                            stdin=subprocess.PIPE if self.peer else subprocess.DEVNULL,
                                            stdout=self.files[0], stderr=self.files[1], close_fds=True)
            if self.peer:
                payload = {"root": self.spec["peer_root"], "lease": self.lease, "process": self.proc,
                           "profile": self.spec.get("profile"), "label": self.spec["label"],
                           "deadline": self.spec.get("deadline")}
                self.process.stdin.write(json.dumps(payload).encode() + b"\n")
                self.process.stdin.flush()
        except BaseException:
            if self.process is not None:
                group_signal(self.process, signal.SIGKILL)
                self.process.wait()
            self.close()
            raise

    def logs(self):
        if self.peer:
            reply = peer_call(self.spec, "logs", {"root": self.spec["peer_root"],
                              "lease": self.lease, "name": self.proc["name"], "tail": MAX_LOG})
            self.peer_pid = reply.get("pid")
            return reply["stdout"], reply["stderr"]
        return tuple(tail_file(log_path(self.root, self.lease, self.proc["name"], stream))
                     for stream in ("stdout", "stderr"))

    def check_ready(self):
        stdout, stderr = self.logs()
        return adapter_ready(self.proc["ready"], stdout, stderr) and http(self.proc["health_url"])

    def terminate(self):
        if self.peer:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        else:
            if self.proc["ready"] == "ds41-json" and not ds41_follower(self.proc) and self.process.poll() is None:
                try:
                    http(self.proc["health_url"], drain=True, key_file=key_file(self.proc))
                except (ValueError, OSError):
                    pass
            group_signal(self.process, signal.SIGTERM)

    def close(self):
        for stream in self.files:
            stream.close()


def stop_children(children):
    # All drains and TERM requests precede the shared grace period.
    for child in reversed(children):
        try:
            child.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + 30
    while any(child.process.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.05)
    for child in children:
        # A departed group leader may still have live descendants.
        try:
            group_signal(child.process, signal.SIGKILL)
            child.process.wait()
        finally:
            child.close()


def supervise(filename):
    filename = path(str(filename))
    # Approval may expire while queued; acquisition records that as a deadline.
    spec = validate_spec(read_json(filename), now=0)
    root = marked_root(spec["root"], spec.get("lab_dir"))
    lease = identifier(spec["lease"])
    if filename.resolve() != inside(root, "run", "spec-" + lease + ".json").resolve():
        raise ValueError("spec file must belong to this lease")
    state_file = record_path(root, lease)
    state = read_json(state_file)
    interrupted = threading.Event()
    old_handlers = {}

    def interrupt(*_):
        interrupted.set()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        old_handlers[sig] = signal.signal(sig, interrupt)
    children = []
    final, reason = "failed", "supervisor-error"
    try:
        owner_file = inside(root, "run", "owner-" + lease + ".json")
        limit = time.monotonic() + 10
        while not owner_file.exists():
            if time.monotonic() >= limit:
                raise ValueError("turn owner pid was not published")
            time.sleep(0.02)
        with state_lock(root, lease):
            state = read_json(state_file)
            if state.get("state") == "cancelled":
                final, reason = "cancelled", "requested"
                return {"lease": lease}
            state.update(state="acquired", pid=os.getpid(), turn_pid=read_json(owner_file)["pid"],
                         start_time=time.time())
            end = state["start_time"] + spec["deadline_s"]
            if spec.get("approval"):
                end = min(end, spec["approval"]["end_epoch"] - 60)
            state["deadline"] = end
            write_json(state_file, state)
        cutoff = time.monotonic() + max(0, end - time.time())
        spec["deadline"] = end
        if spec.get("lab_dir") and spec["kind"] != "none":
            try:
                layout_check(spec)
            except (OSError, ValueError) as exc:
                final, reason = "aborted", str(exc)
                return {"lease": lease}
        for proc in spec["processes"]:
            if interrupted.is_set() or stop_path(root, lease).exists() or time.monotonic() >= cutoff:
                break
            child = Child(root, lease, proc, spec)
            child.start()
            children.append(child)
            digest = None
            if not child.peer:
                digest = hashlib.sha256()
                with path(proc["argv"][0]).open("rb") as executable:
                    for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest = digest.hexdigest()
            state["processes"].append({"name": proc["name"], "machine": proc["machine"],
                                       "pid": child.process.pid, "start_time": time.time(),
                                       "exe_sha256": digest, "ready": False})
            write_json(state_file, state)
        while True:
            if interrupted.is_set() or stop_path(root, lease).exists():
                final, reason = "stopped", "requested"
                break
            if time.monotonic() >= cutoff:
                final, reason = "stopped", "deadline"
                break
            failure = next((c for c in children if c.process.poll() is not None), None)
            if failure:
                final = "stopped" if state["state"] == "ready" else "failed"
                reason = "process-exit:" + failure.proc["name"]
                break
            for index, child in enumerate(children):
                if not child.ready:
                    child.ready = child.check_ready()
                    state["processes"][index]["ready"] = child.ready
                    if child.peer_pid is not None:
                        state["processes"][index]["peer_pid"] = child.peer_pid
                    if not child.ready and time.monotonic() - child.started >= child.proc["ready_timeout_s"]:
                        raise ValueError("readiness-timeout:" + child.proc["name"])
            if children and all(child.ready for child in children) and state["state"] != "ready":
                state["state"] = "ready"
                write_json(state_file, state)
            time.sleep(0.1)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    finally:
        try:
            stop_children(children)
        finally:
            state.update(state=final, reason=reason, end_time=time.time())
            for entry, child in zip(state["processes"], children):
                entry["returncode"] = child.process.returncode
            write_json(state_file, state)
            for sig, old in old_handlers.items():
                signal.signal(sig, old)
    return {"lease": lease}


def peer_state(root, payload, child, finished=False):
    lease = payload["lease"]
    with state_lock(root, lease):
        filename = record_path(root, lease)
        state = read_json(filename) if filename.exists() else {
            "lease": lease, "label": payload.get("label"), "kind": "pair-peer",
            "profile": payload.get("profile"), "start_time": time.time(),
            "deadline": payload.get("deadline"), "processes": []}
        entry = next((entry for entry in state["processes"] if entry["name"] == child.proc["name"]), None)
        if entry is None:
            entry = {"name": child.proc["name"], "machine": "local", "pid": child.process.pid,
                     "start_time": time.time()}
            state["processes"].append(entry)
        if finished:
            entry["returncode"] = child.process.returncode
        stopped = all("returncode" in entry for entry in state["processes"])
        state.update(state="stopped" if stopped else "acquired",
                     reason="peer-channel-closed" if stopped else None)
        write_json(filename, state)


def peer_run(payload, stream=None):
    if os.name != "posix":
        raise ValueError("peer-run requires POSIX process groups")
    root = marked_root(payload["root"])
    lease = identifier(payload["lease"])
    proc = validate_process(payload["process"])
    proc["machine"] = "local"
    child = Child(root, lease, proc)
    eof = threading.Event()
    stream = sys.stdin.buffer if stream is None else stream

    def watch():
        try:
            # Avoid a daemon thread holding stdin's buffered-reader lock during
            # interpreter shutdown when the deadline ends before channel EOF.
            read = (lambda: os.read(stream.fileno(), 1)) if stream is sys.stdin.buffer else (lambda: stream.read(1))
            while read():
                pass
        finally:
            eof.set()

    old_handlers = {sig: signal.signal(sig, lambda *_: eof.set())
                    for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        child.start()
        peer_state(root, payload, child)
        threading.Thread(target=watch, daemon=True).start()
        while (child.process.poll() is None and not eof.wait(0.05)
               and not stop_path(root, lease).exists()
               and (payload.get("deadline") is None or time.time() < payload["deadline"])):
            pass
    finally:
        if child.process is not None:
            stop_children([child])
            peer_state(root, payload, child, finished=True)
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
    return {"pid": child.process.pid, "returncode": child.process.returncode}


def logs(payload, root):
    lease, name = identifier(payload.get("lease")), identifier(payload.get("name"))
    count = payload.get("tail", MAX_LOG)
    if type(count) is not int or count < 0:
        raise ValueError("tail must be nonnegative")
    count = min(count, MAX_LOG)
    # tail counts lines; the byte budget is shared by both streams.
    streams = []
    for stream in ("stdout", "stderr"):
        raw = tail_file(log_path(root, lease, name, stream))
        streams.append("".join(raw.splitlines(keepends=True)[-count:]) if count else "")
    stdout, stderr = streams
    if len(stdout.encode()) + len(stderr.encode()) > MAX_LOG:
        stderr = stderr.encode()[-MAX_LOG // 2:].decode("utf-8", errors="ignore")
        stdout = stdout.encode()[-(MAX_LOG - len(stderr.encode())):].decode("utf-8", errors="ignore")
    reply = {"stdout": stdout, "stderr": stderr}
    state_file = record_path(root, lease)
    if state_file.exists():
        state = read_json(state_file)
        entry = next((entry for entry in state.get("processes", []) if entry["name"] == name), {})
        if "pid" in entry:
            reply["pid"] = entry["pid"]
    return reply


def run(verb, payload):
    result = {"schema": "telar/node-v1", "verb": verb}
    try:
        if verb not in VERBS:
            raise ValueError("unsupported lease verb")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if verb == "lease":
            result.update(start_lease(payload))
        elif verb == "supervise-internal":
            if "check_layout" in payload:
                required = payload["check_layout"]
                lab = path(payload["lab_dir"])
                words = (lab / ".gpu-turn" / "layout-state").read_text().split()
                layout = read_json(USB_LAYOUT)
                result["layout_ok"] = bool(words and words[0] == required and
                    str(layout.get("layout", layout.get("layout_id"))) == required)
            else:
                result.update(supervise(payload["spec_file"]))
        elif verb == "peer-run":
            result.update(peer_run(payload))
        else:
            root = marked_root(payload.get("root"))
            if verb == "status":
                leases = []
                for filename in sorted(inside(root, "run").glob("lease-*.json")):
                    state = read_json(inside(root, "run", filename.name))
                    owner = inside(root, "run", "owner-" + identifier(state["lease"]) + ".json")
                    if state["state"] == "queued" and owner.exists():
                        state["turn_pid"] = read_json(owner)["pid"]
                    leases.append(state)
                result["leases"] = leases
                result["jobs"] = [read_json(inside(root, "run", filename.name))
                                  for filename in sorted(inside(root, "run").glob("job-*.json"))
                                  if not filename.name.endswith(".spec.json")]
                if payload.get("job") is not None:
                    job = identifier(payload["job"])
                    result["job"] = read_json(inside(root, "run", "job-" + job + ".json"))
                for process in _launches[:]:
                    if process.poll() is not None:
                        _launches.remove(process)
            elif verb == "logs":
                result.update(logs(payload, root))
            elif verb == "put-secret":
                name = identifier(payload.get("name"))
                raw = base64.b64decode(payload.get("value_b64", ""), validate=True)
                if not raw or len(raw) > MAX_LOG:
                    raise ValueError("secret must contain 1..65536 bytes")
                keys = inside(root, "etc", "keys")
                keys.mkdir(exist_ok=True, mode=0o700)
                atomic(inside(root, "etc", "keys", name), raw)
                result["name"] = name
            else:
                lease = identifier(payload.get("lease"))
                state_file = record_path(root, lease)
                state = read_json(state_file)
                if state["state"] not in TERMINAL:
                    atomic(stop_path(root, lease), b"")
                    if verb == "cancel" and state["state"] == "queued":
                        with state_lock(root, lease):
                            owner = read_json(inside(root, "run", "owner-" + lease + ".json"))
                            state = read_json(state_file)
                            if state["state"] == "queued":
                                try:
                                    os.killpg(owner["pid"], signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                                state.update(state="cancelled", reason="requested", end_time=time.time())
                                write_json(state_file, state)
                result.update(lease=lease, state=state["state"])
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    return result


def entry(verb, spec_file=None):
    try:
        if verb == "supervise-internal" and spec_file is not None:
            payload = {"spec_file": spec_file}
        elif verb == "peer-run":
            # One JSON line starts the child; the remaining channel is the dead-man.
            payload = decode(sys.stdin.buffer.readline(MAX_JSON + 1))
        else:
            payload = decode(sys.stdin.buffer.read(MAX_JSON + 1))
        result = run(verb, payload)
    except (ValueError, OSError, RecursionError) as exc:
        result = {"schema": "telar/node-v1", "verb": verb, "error": str(exc)}
    print(json.dumps(result, allow_nan=False), flush=True)
    return 2 if result.get("error") else 0


# The fixed bootstrap dispatcher buffers JSON to EOF. These two private entries
# need a spec-file argument or a live stdin, so handle them before that buffering.
if (Path(sys.argv[0]).name == "telar-node" and len(sys.argv) > 1
        and (sys.argv[1] == "peer-run" or (sys.argv[1] == "supervise-internal" and len(sys.argv) == 3))):
    raise SystemExit(entry(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))

if __name__ == "__main__":
    raise SystemExit(entry(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
