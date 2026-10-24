"""Pinned OpenSSH transport for the small hyperloom-node JSON protocol."""

import ast
from dataclasses import dataclass
import base64
import hashlib
import ipaddress
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .paths import repo_root
from .winproc import no_window


MAX_JSON = 256 * 1024
VERBS = frozenset({"version", "doctor", "gpu",
                   # hyperloom_install (phase 1a)
                   "install", "job", "releases", "activate", "uninstall", "verify-model", "stat-assets", "push-release",
                   # hyperloom_lease (phase 1a)
                   "lease", "cancel", "status", "logs", "stop", "put-secret",
                   "peer-run", "supervise-internal",
                   # hyperloom_download (phase 4)
                   "download-plan", "download", "download-cancel"})


def node_files(directory=None):
    """Files bootstrap copies into ~/.hyperloom-boot: the node CLI, the extension modules its EXTENSIONS
    tuple declares, and build-node.sh. The tuple is read from the node CLI's source (never executed), and the
    node derives its own TOOL_FILES from the same tuple, so the two lists cannot drift."""
    path = (Path(directory) if directory is not None else Path(__file__).resolve().parent.parent / "node") / "hyperloom-node"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        # bootstrap then refuses: hyperloom-node itself is missing or unreadable.
        return ("hyperloom-node", "build-node.sh")
    for statement in tree.body:
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "EXTENSIONS"):
            extensions = ast.literal_eval(statement.value)
            if (not isinstance(extensions, tuple)
                    or not all(isinstance(name, str) and re.fullmatch(r"[a-z_][a-z0-9_]*", name) for name in extensions)):
                raise RuntimeError("hyperloom-node EXTENSIONS must be a tuple of module names")
            return ("hyperloom-node", *(name + ".py" for name in extensions), "build-node.sh")
    raise RuntimeError("hyperloom-node declares no EXTENSIONS")


NODE_FILES = node_files()


def tool_digest(directory=None):
    """Same digest as hyperloom-node's version check, over this checkout's node files."""
    directory = Path(directory) if directory is not None else repo_root() / "node"
    digest = hashlib.sha256()
    for name in sorted(NODE_FILES):
        try:
            data = (directory / name).read_bytes()
        except OSError:
            data = b"missing"
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return digest.hexdigest()


class RemoteError(RuntimeError):
    pass


class RemoteTimeout(RemoteError):
    """The command ran out of time or was cancelled (not a failure of the tool itself)."""


@dataclass(frozen=True)
class HostKey:
    algorithm: str
    key_b64: str
    sha256_fingerprint: str


def _hostkey(algorithm, key_b64):
    if algorithm not in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}:
        raise RemoteError("unsupported host-key algorithm")
    try:
        blob = base64.b64decode(key_b64, validate=True)
        length = int.from_bytes(blob[:4], "big")
        if len(blob) < 8 or blob[4:4 + length].decode("ascii") != algorithm:
            raise ValueError("invalid key blob")
    except (ValueError, UnicodeError) as exc:
        raise RemoteError("invalid host key") from exc
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return HostKey(algorithm, key_b64, fingerprint)


def _target(value):
    if (not isinstance(value, str) or not value or value.startswith("-")
            or not re.fullmatch(r"[\w.@:\[\]-]+", value, flags=re.ASCII)
            or value.count("@") > 1):
        raise RemoteError("invalid ssh target or alias")
    return value


def _address(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise RemoteError("ssh address must be a numeric IP") from exc


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise RemoteError("invalid ssh port")
    return value


def _token(host, port):
    return host if port == 22 else f"[{host}]:{port}"


def _config_path(path):
    # -o values use ssh_config syntax even when passed as a single argv token.
    value = path.as_posix().replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if any(ord(c) < 32 for c in value):
        raise RemoteError("invalid known_hosts path")
    # Quote only when needed: MSYS-runtime ssh builds mangle embedded quotes from a Windows command line.
    return '"' + value + '"' if any(c.isspace() for c in value) else value


def _write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".hyperloom-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stop(child):
    if os.name == "nt":
        if child.poll() is None:
            # scp can have an ssh child: terminate the whole local process tree.
            try:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False, **no_window())
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child.poll() is None:
        child.kill()


def openssh_tool(name):
    """Resolve ssh/scp/ssh-keyscan. On Windows prefer the native system OpenSSH: it parses Windows command
    lines correctly (MSYS-runtime builds on PATH do not) and is often not on PATH. Otherwise use PATH."""
    if os.name == "nt":
        system = ntpath.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", name + ".exe")
        if os.path.isfile(system):
            return system
    return shutil.which(name) or name


def _run(argv, data=b"", timeout=30, cancel_event=None, limit=MAX_JSON, executable=None):
    """Drain bounded pipes while allowing timeout and cooperative cancellation."""
    if executable is None and argv and argv[0] in ("ssh", "scp", "ssh-keyscan"):
        executable = openssh_tool(argv[0])
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise RemoteError("timeout must be positive and finite")
    if cancel_event is not None and cancel_event.is_set():
        raise RemoteError("remote operation cancelled")
    try:
        child = subprocess.Popen(argv, executable=executable, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=os.name != "nt", **no_window())
    except OSError as exc:
        raise RemoteError("could not start OpenSSH command") from exc
    output = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read(stream, result):
        try:
            while chunk := stream.read(4096):
                room = limit - len(result)
                result.extend(chunk[:max(0, room)])
                if len(chunk) > room:
                    overflow.set()
        finally:
            stream.close()

    def write():
        try:
            child.stdin.write(data)
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            child.stdin.close()

    workers = [threading.Thread(target=read, args=(child.stdout, output[0])),
               threading.Thread(target=read, args=(child.stderr, output[1])),
               threading.Thread(target=write)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while child.poll() is None or any(worker.is_alive() for worker in workers):
            if overflow.is_set():
                raise RemoteError("OpenSSH output exceeds the size limit")
            if cancel_event is not None and cancel_event.is_set():
                raise RemoteTimeout("remote operation cancelled")
            if time.monotonic() >= deadline:
                raise RemoteTimeout("remote operation timed out")
            time.sleep(0.01)
        if overflow.is_set():
            raise RemoteError("OpenSSH output exceeds the size limit")
        if child.returncode:
            # Do not copy remote stderr, which could contain sensitive paths.
            raise RemoteError(f"OpenSSH command failed (exit {child.returncode})")
        completed = True
        return bytes(output[0])
    finally:
        if not completed:
            _stop(child)
        child.wait()
        for worker in workers:
            worker.join(timeout=2)


def keyscan(address, port):
    """Return candidates only; scanning never authorizes a key or bootstraps."""
    address, port = _address(address), _port(port)
    argv = ["ssh-keyscan", "-t", "ed25519,ecdsa,rsa", "-p", str(port), address]
    try:
        return _keys(_run(argv))
    except RemoteTimeout:
        # An unreachable or slow host times out with any tool: do not double the wait.
        raise
    except RemoteError:
        # Windows' bundled ssh-keyscan 9.5 offers a key exchange it cannot complete
        # (sntrup761) and finds no keys; retry with the one on PATH or Git for Windows'
        # (which usually has only Git\cmd on PATH).
        if os.name != "nt":
            raise
        native = ntpath.normcase(openssh_tool("ssh-keyscan"))
        git = shutil.which("git")
        candidates = [shutil.which("ssh-keyscan"),
                      git and ntpath.join(ntpath.dirname(ntpath.dirname(git)), "usr", "bin", "ssh-keyscan.exe")]
        fallback = next((c for c in candidates if c and os.path.isfile(c) and ntpath.normcase(c) != native), None)
        if fallback is None:
            raise
        return _keys(_run(argv, executable=fallback))


def _keys(raw):
    result = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3:
                raise RemoteError("malformed keyscan output")
            key = _hostkey(parts[1], parts[2])
            if key not in result:
                result.append(key)
    except UnicodeError as exc:
        raise RemoteError("keyscan output is not UTF-8") from exc
    if not result:
        raise RemoteError("keyscan returned no host keys")
    return result


class KnownHosts:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.config_path = self.path.with_name(self.path.name + ".ssh_config")

    def _lines(self):
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise RemoteError("cannot read hyperloom known_hosts") from exc

    def pin(self, alias, address, port, hostkey):
        """Persist the exact operator-confirmed key; refuse silent replacement."""
        alias, address, port = _target(alias), _address(address), _port(port)
        expected = _hostkey(hostkey.algorithm, hostkey.key_b64)
        if expected != hostkey:
            raise RemoteError("host-key fingerprint mismatch")
        tokens = {_token(alias, port), _token(address, port)}
        lines = self._lines()
        for line in lines:
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            if tokens.intersection(parts[0].split(",")):
                if len(parts) != 3 or parts[1:] != [hostkey.algorithm, hostkey.key_b64]:
                    raise RemoteError("host key changed; explicit re-pairing required")
        entry = f"{','.join(sorted(tokens))} {hostkey.algorithm} {hostkey.key_b64}"
        if entry not in lines:
            lines.append(entry)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write(self.path, "\n".join(lines) + "\n")
        except OSError as exc:
            raise RemoteError("cannot write hyperloom known_hosts") from exc

    def options(self, alias, address, port):
        alias_token, address_token = _token(alias, port), _token(address, port)
        if not any(len(parts := line.split()) == 3
                   and {alias_token, address_token}.issubset(parts[0].split(","))
                   for line in self._lines()):
            raise RemoteError("unknown host key; confirm a fingerprint before connecting")
        try:
            _write(self.config_path, "# Hyperloom: ignore ambient SSH configuration.\n")
        except OSError as exc:
            raise RemoteError("cannot write hyperloom ssh config") from exc
        return ["-F", str(self.config_path),
                "-o", "UserKnownHostsFile=" + _config_path(self.path),
                "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=none",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", f"HostKeyAlias={alias_token}", "-o", f"Hostname={address}",
                "-o", "UpdateHostKeys=no"]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(obj, dict):
            raise ValueError("JSON object required")
        return obj
    except (ValueError, UnicodeError, RecursionError) as exc:
        head = raw[:200].decode("utf-8", errors="replace")
        raise RemoteError(f"expected one UTF-8 JSON object from hyperloom-node; got: {head!r}") from exc


class Remote:
    def __init__(self, machine, known_hosts, ssh="ssh"):
        self.machine = machine
        self.known_hosts = known_hosts if isinstance(known_hosts, KnownHosts) else KnownHosts(known_hosts)
        self.ssh = ssh
        self.alias = _target(machine.id)
        self.address = _address(machine.http_address)
        self.target = _target(machine.ssh_target)
        self.port = _port(machine.ssh_port)
        self.node_path = None

    def _ssh(self, command, data=b"", timeout=30, cancel_event=None):
        options = self.known_hosts.options(self.alias, self.address, self.port)
        return _run([self.ssh, *options, "-p", str(self.port), "-T", "--", self.target, command],
                    data, timeout, cancel_event)

    def _path(self, raw):
        try:
            path = raw.decode("utf-8").rstrip("\n")
            if not PurePosixPath(path).is_absolute() or any(ord(c) < 32 for c in path):
                raise ValueError("invalid remote boot path")
        except (UnicodeError, ValueError) as exc:
            raise RemoteError("invalid remote boot path") from exc
        self.node_path = path

    def has_tool(self, timeout=30, *, cancel_event=None):
        """Whether ~/.hyperloom-boot/hyperloom-node exists (read-only; changes nothing on the node)."""
        raw = self._ssh('if test -f "$HOME/.hyperloom-boot/hyperloom-node"; then printf \'yes\\n\'; '
                        "else printf 'no\\n'; fi", timeout=timeout, cancel_event=cancel_event)
        answer = raw.strip()
        if answer not in (b"yes", b"no"):
            raise RemoteError("unexpected reply to the node tool check")
        return answer == b"yes"

    def bootstrap(self, source=None, timeout=30, *, scp="scp", cancel_event=None):
        source = Path(source) if source is not None else repo_root() / "node"
        directory = source if source.is_dir() else source.parent
        sources = [directory / name for name in NODE_FILES]
        if any(not path.is_file() for path in sources):
            raise RemoteError("hyperloom-node bootstrap files are missing")
        self._path(self._ssh('umask 077; mkdir -p -- "$HOME/.hyperloom-boot" && '
                             'printf \'%s\\n\' "$HOME/.hyperloom-boot/hyperloom-node"',
                             timeout=timeout, cancel_event=cancel_event))
        options = self.known_hosts.options(self.alias, self.address, self.port)
        # Stage, then rename into place: a running build-node.sh or supervisor keeps its old inode.
        stage = ".stage-" + os.urandom(6).hex()
        boot = PurePosixPath(self.node_path).parent
        self._ssh("umask 077; mkdir -- " + shlex.quote(str(boot / stage)), timeout=timeout, cancel_event=cancel_event)
        _run([scp, *options, "-P", str(self.port), "--", *(str(path.absolute()) for path in sources),
              f"{self.target}:.hyperloom-boot/{stage}/"], timeout=timeout, cancel_event=cancel_event)
        self._ssh("cd -- " + shlex.quote(str(boot)) + " && chmod 700 -- "
                  + " ".join(shlex.quote(stage + "/" + name) for name in NODE_FILES)
                  + " && for f in " + " ".join(shlex.quote(name) for name in NODE_FILES)
                  + "; do mv -f -- " + shlex.quote(stage) + '/"$f" "$f" || exit 1; done && rmdir -- ' + shlex.quote(stage),
                  timeout=timeout, cancel_event=cancel_event)
        return self.call("version", {}, timeout, cancel_event=cancel_event)

    def call(self, verb, payload, timeout=30, *, cancel_event=None):
        if not isinstance(verb, str) or verb not in VERBS:
            raise RemoteError("unsupported hyperloom-node verb")
        if not isinstance(payload, dict):
            raise RemoteError("payload must be a JSON object")
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise RemoteError("payload must be UTF-8 JSON") from exc
        if len(data) > MAX_JSON:
            raise RemoteError("payload exceeds the size limit")
        if self.node_path is None:
            self._path(self._ssh('printf \'%s\\n\' "$HOME/.hyperloom-boot/hyperloom-node"',
                                 timeout=timeout, cancel_event=cancel_event))
        raw = self._ssh(shlex.quote(self.node_path) + " " + verb, data, timeout, cancel_event)
        return _json(raw)
