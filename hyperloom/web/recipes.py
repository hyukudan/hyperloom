"""Recipe endpoints of the portal. Every request IS a `hyperloom recipe ...` command line: the JSON arguments are
checked and turned into the CLI's argv (recipes/service.py ``argv``), and that argv runs

- in a child process (`python -m hyperloom recipe ...`) for everything that resolves with the core, probes hardware,
  plans or writes: the answer is the CLI's own exit code, stdout JSON and stderr line; process-global state of those
  code paths (descriptor reads, the pinned router import) never touches the portal's threads;
- in process, parsed by the CLI's own parser and run by the same ``commands.run``, for the cheap GETs that only read
  files (list, show, active, routes).

Bounds: at most WORKERS children at a time and WAITING more queued (beyond that: 503 at once), one deadline from
admission to the end, capped output, cancellation when the client goes away, and every child's whole process tree is
removed on any of those and on portal shutdown (Windows: a kill-on-close job object; POSIX: its process group plus every
process that inherited its worker marker). Stop/cancel/down never wait here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from hyperloom.winproc import no_window

WORKERS = 2
WAITING = 4
OFFLINE_DEADLINE_S = 30
HARDWARE_DEADLINE_S = 150
STDOUT_CAP = 8 * 1024 * 1024
STDERR_CAP = 64 * 1024
ERROR_CHARS = 16 * 1024
MARKER = "HYPERLOOM_RECIPE_WORKER"
COMMAND = None          # tests replace the worker command; otherwise `python -m hyperloom`
# GET recipes[/...]: (operation, query keys -> argument names). Cheap and side-effect free; run in process.
READS = {"recipes": ("list", {"model": "model"}), "recipes/active": ("active", {"profile": "profile"}),
         "recipes/routes": ("routes", {"profile": "profile"})}
# POST recipes/<op>: synchronous answers (never jobs). WRITES are audited.
QUERIES = frozenset(("recipes/validate", "recipes/explain", "recipes/check", "recipes/diff", "recipes/pick",
                     "recipes/select", "recipes/plan"))
WRITES = frozenset(("recipes/save", "recipes/delete", "recipes/save-layer", "recipes/delete-layer",
                    "recipes/save-receipt"))
# Jobs (one mutating job at a time, like up/down): probes that may write, and the server-side switch.
JOBS = frozenset(("recipes/probe", "recipes/deploy"))
POSTS = QUERIES | WRITES
LARGE_BODY = frozenset(("recipes/save", "recipes/check", "recipes/save-layer"))   # recipe text (<= 256 KiB)
LARGE_BODY_BYTES = 1024 * 1024
# Exit codes of `hyperloom recipe` -> HTTP status of the answer (the body is the same document in every case).
STATUS = {0: 200, 1: 200, 2: 400, 3: 404, 4: 409}


class RaisingParser(argparse.ArgumentParser):
    """The CLI's parser, raising instead of printing usage and exiting."""

    def error(self, message):
        raise ValueError(message)

    def exit(self, status=0, message=None):
        raise ValueError(message or f"argument parser exit {status}")


class Busy(RuntimeError):
    pass


class Result:
    def __init__(self, code, stdout, stderr, why=None):
        self.code, self.stdout, self.stderr, self.why = code, stdout, stderr, why


class Runner:
    """Admission, deadlines and cleanup for recipe children."""

    def __init__(self, workers=WORKERS, waiting=WAITING):
        self._slots = threading.BoundedSemaphore(workers)
        self._lock = threading.Lock()
        self._admitted = 0
        self._limit = workers + waiting
        self._live = {}
        self._closed = False

    def run(self, argv, *, stdin=None, deadline_s, cancelled=lambda: False):
        end = time.monotonic() + deadline_s
        with self._lock:
            if self._closed or self._admitted >= self._limit:
                raise Busy("recipe requests are busy: try again shortly")
            self._admitted += 1
        try:
            while not self._slots.acquire(timeout=0.25):
                if time.monotonic() >= end:
                    return Result(None, b"", b"", "deadline")
                if cancelled():
                    return Result(None, b"", b"", "client gone")
            try:
                return child(argv, stdin=stdin, end=end, cancelled=cancelled, track=self._track)
            finally:
                self._slots.release()
        finally:
            with self._lock:
                self._admitted -= 1

    def _track(self, key, worker):
        with self._lock:
            if worker is None:
                self._live.pop(key, None)
            elif self._closed:
                worker.kill()
            else:
                self._live[key] = worker

    def close(self):
        """Portal shutdown: remove every live child tree."""
        with self._lock:
            self._closed = True
            workers = list(self._live.values())
        for worker in workers:
            worker.kill()


def _environment(marker):
    """The child imports the portal's own hyperloom, writes UTF-8 and carries the worker marker."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env[MARKER] = marker
    env["PYTHONPATH"] = package_root() + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("HYPERLOOM_UI_INPROCESS", None)
    return env


def package_root():
    return str(Path(__file__).resolve().parents[2])


def marked_processes(marker):
    """POSIX: pids of this user's processes whose environment carries the worker marker (descendants that left the
    worker's process group, e.g. ssh started with its own session)."""
    needle = f"{MARKER}={marker}".encode()
    found = []
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


class Worker:
    """One child and its whole tree."""

    def __init__(self, argv, stdin):
        self.marker = uuid.uuid4().hex
        self.job = None
        command = [*(COMMAND or [sys.executable, "-m", "hyperloom"]), *argv]
        if os.name == "nt":
            from hyperloom import procs
            self.job = procs._WindowsJob()
            # Hidden console, own group, suspended until it is inside the kill-on-close job (no descendant escapes).
            flags = no_window(subprocess.CREATE_NEW_PROCESS_GROUP | 0x4)
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, env=_environment(self.marker),
                                                cwd=package_root(), **flags)
                self.job.assign(self.process)
                procs.resume_suspended(self.process.pid)
            except BaseException:
                if getattr(self, "process", None) is not None:
                    self.process.kill()
                self.job.close()
                raise
        else:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, env=_environment(self.marker),
                                            cwd=package_root(), start_new_session=True, **no_window())
        self.stdin = stdin

    def kill(self):
        if self.job is not None:
            try:
                self.job.terminate()
            except OSError:
                pass
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass
            for _ in range(20):            # descendants that started their own session, until none is left
                pids = marked_processes(self.marker)
                if not pids:
                    break
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                time.sleep(0.05)
        try:
            self.process.kill()
        except OSError:
            pass

    def close(self):
        if self.job is not None:
            self.job.close()           # kill-on-close: anything still in the job ends here


def child(argv, *, stdin=None, end, cancelled=lambda: False, track=lambda key, worker: None):
    """Run `python -m hyperloom ARGV` until ``end`` (monotonic). Returns a Result; ``why`` is set when the child was
    stopped (deadline, output overflow, client gone)."""
    worker = Worker(argv, stdin)
    key = object()
    track(key, worker)
    out, err = bytearray(), bytearray()
    overflow = threading.Event()

    def drain(stream, buffer, cap):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if len(buffer) + len(chunk) > cap:
                overflow.set()
                buffer.extend(chunk[: max(0, cap - len(buffer))])
                worker.kill()
                break
            buffer.extend(chunk)
    readers = [threading.Thread(target=drain, args=(worker.process.stdout, out, STDOUT_CAP), daemon=True),
               threading.Thread(target=drain, args=(worker.process.stderr, err, STDERR_CAP), daemon=True)]
    def feed():
        try:
            if stdin is not None:
                worker.process.stdin.write(stdin)
            worker.process.stdin.close()
        except (OSError, ValueError):
            pass                      # the child exited or was stopped: its output says what happened
    # stdin is written on its own thread, so the deadline, cancellation and caps below supervise it too.
    readers.append(threading.Thread(target=feed, daemon=True))
    for reader in readers:
        reader.start()
    why = None
    try:
        while worker.process.poll() is None:
            if overflow.is_set():
                why = "output overflow"
            elif time.monotonic() >= end:
                why = "deadline"
            elif cancelled():
                why = "client gone"
            if why:
                worker.kill()
                break
            try:
                worker.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        worker.process.wait(timeout=30)
        for reader in readers:
            reader.join(timeout=5)
        if overflow.is_set():
            why = "output overflow"
    finally:
        if worker.process.poll() is None:
            worker.kill()
        worker.close()
        track(key, None)
        for stream in (worker.process.stdout, worker.process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return Result(worker.process.returncode, bytes(out), bytes(err), why)


def document(operation, argv, result, *, now, cli_files=None):
    """(HTTP status, the portal's answer): the CLI's exit code, stdout JSON (`result`), stderr text (`error`) and command
    line (`cli`)."""
    from hyperloom.recipes import service
    doc = {"schema": f"hyperloom/recipes-{operation}-v1", "server_time": now, "operation": operation,
           "cli": service.command_line(argv), "exit_code": result.code, "ok": result.code == 0 and not result.why,
           "result": None, "error": None}
    if cli_files:
        doc["cli_files"] = cli_files
    if result.why:
        doc.update(exit_code=None, ok=False, stopped=result.why,
                   error={"deadline": "the recipe command did not finish in time (stopped)",
                          "output overflow": "the recipe command printed more than allowed (stopped)",
                          "client gone": "the request was cancelled (client disconnected)"}.get(result.why, result.why))
        return {"deadline": 504, "output overflow": 502}.get(result.why, 499), doc
    text = result.stdout.decode("utf-8", "replace").strip()
    if text:
        try:
            doc["result"] = json.loads(text)
        except ValueError:
            doc.update(ok=False, error="the recipe command printed no JSON")
            return 502, doc
    message = result.stderr.decode("utf-8", "replace").strip()
    if message:
        lines = [line.removeprefix("hyperloom: ") for line in message.splitlines() if line.strip()]
        doc["error"] = "\n".join(lines)[-ERROR_CHARS:]
    if result.code not in STATUS:
        doc["ok"] = False
        doc["error"] = doc["error"] or f"the recipe command failed (exit {result.code})"
        return 500, doc
    return STATUS[result.code], doc


def in_process(argv, loader):
    """Run a cheap read operation in process: the CLI's parser and the same commands.run, with cli.main's error
    handling (a refusal is exit 1 with the message; unknown id 3; conflict 4)."""
    from hyperloom import cli
    from hyperloom.recipes import commands, service
    args = cli.build_parser(RaisingParser).parse_args(argv)
    try:
        value, ok = commands.run(loader, args)
    except service.NotFound as exc:
        return Result(3, b"", f"hyperloom: {exc}".encode())
    except service.Conflict as exc:
        return Result(4, b"", f"hyperloom: {exc}".encode())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        return Result(1, b"", f"hyperloom: {exc}".encode())
    return Result(0 if ok else 1, json.dumps(value, indent=2).encode("utf-8"), b"")


def audit_arguments(arguments):
    """Recipe/layer/receipt text is not copied into the audit log: its size and sha256 are."""
    if isinstance(arguments, dict) and isinstance(arguments.get("text"), str):
        data = arguments["text"].encode("utf-8")
        return dict(arguments, text={"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return arguments


class Request:
    """One POST recipes/<op>: its operation, argv, stdin, deadline and private files (removed by close())."""

    def __init__(self, action, arguments, *, config):
        from hyperloom.recipes import service
        self.directory = None
        self.cli_files = None
        operation = action.removeprefix("recipes/")
        if not isinstance(arguments, dict):
            raise ValueError("JSON object required")
        arguments = dict(arguments)
        self.stdin = None
        if operation == "check":
            # A draft's text, checked like `hyperloom recipe validate|explain - [PROFILE]` (nothing is written).
            text = arguments.pop("text", None)
            mode = arguments.pop("mode", "validate")
            if not isinstance(text, str) or not text:
                raise ValueError("text (the recipe) is required")
            if mode not in ("validate", "explain"):
                raise ValueError("mode must be validate or explain")
            operation, self.stdin = mode, text.encode("utf-8")
        elif operation in ("save", "save-layer", "save-receipt") and "text" in arguments:
            text = arguments.pop("text")
            if not isinstance(text, str) or not text:
                raise ValueError("text must be a nonempty document")
            self.stdin = text.encode("utf-8")
        edits = arguments.pop("edits", None)
        self.operation = operation
        self.argv = service.argv(operation, arguments, config=config, stdin=self.stdin is not None)
        if edits is not None:
            self._edits(service, operation, arguments, edits)
        hardware = operation in ("pick", "select", "plan", "probe") or (
            operation in ("validate", "explain") and arguments.get("profile"))
        self.deadline_s = HARDWARE_DEADLINE_S if hardware else OFFLINE_DEADLINE_S

    def _edits(self, service, operation, arguments, edits):
        """The field editor's changes as ONE overrides layer (a rank/list pair applies together, as in derived saves),
        passed after the named layers; `cli` names it edits.json and `cli_files` holds its text."""
        if operation not in ("validate", "explain", "plan"):
            raise ValueError("edits apply to validate, explain and plan")
        if not isinstance(edits, dict) or not edits or len(edits) > 256 or any(
                not isinstance(k, str) or not k for k in edits):
            raise ValueError("edits must be an object of at most 256 settable paths and values")
        recipe = arguments.get("recipe")
        row = service.find(recipe) if isinstance(recipe, str) else None
        if row is None or row["name"] is None:
            raise ValueError("edits need a recipe id from the catalog")
        layer = json.dumps({"schema": service.OVERRIDES_SCHEMA, "recipe": row["name"], "source": "portal field edits",
                            "set": edits}, indent=1)
        self.directory = tempfile.TemporaryDirectory(prefix="hl-recipe-edits-")
        path = Path(self.directory.name) / "edits.json"
        path.write_text(layer, encoding="utf-8")
        position = self.argv.index("--config")
        self.argv[position:position] = ["--recipe-overrides", str(path)]
        self.cli_files = {"edits.json": layer}
        self.edits_path = str(path)

    def command_line_argv(self):
        return [("edits.json" if self.cli_files and a == getattr(self, "edits_path", None) else a) for a in self.argv]

    def close(self):
        if self.directory is not None:
            self.directory.cleanup()


def read_request(endpoint, query, *, config):
    """(operation, argv) for GET recipes, recipes/active, recipes/routes and recipes/<id>."""
    from hyperloom.recipes import service
    if endpoint in READS:
        operation, keys = READS[endpoint]
        if set(query) - set(keys):
            raise ValueError("unknown query arguments: " + ", ".join(sorted(set(query) - set(keys))))
        return operation, service.argv(operation, {keys[k]: v for k, v in query.items() if v}, config=config)
    recipe_id = endpoint.removeprefix("recipes/")
    if query:
        raise ValueError("recipes/<id> takes no query arguments")
    return "show", service.argv("show", {"recipe": recipe_id}, config=config)
