"""User-confirmed downloads of pinned model files (Python 3.10+, stdlib only).

``download-plan`` is read-only: it returns the exact command per file and a confirmation
code. ``download`` starts one low-priority background job only when the caller's code equals
the code of the plan recomputed here. Files go only into a directory the operator dedicated to
hyperloom downloads (it holds a ``.hyperloom-assets`` marker). Each file goes to
``<dir>/.<name>.hyperloom-part`` (held under an exclusive lock), is verified (size + sha256,
read with O_DIRECT where supported) and only then linked to its name; an existing destination
is never touched.
"""

import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import uuid

try:
    import hyperloom_install as install
except ImportError:
    # Loaded by path (tests, tools): the install module ships beside this file.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hyperloom_install as install

try:
    import fcntl
except ImportError:  # not POSIX: background jobs are refused there anyway
    fcntl = None


VERBS = {"download-plan", "download", "download-cancel"}
HOSTS = {"huggingface.co"}
MAX_FILES = 64
HEADROOM = 1 << 30
# The operator creates this file in a directory dedicated to hyperloom downloads. Lab model
# directories never have it, so a manifest path inside one is refused rather than written into.
ASSET_MARKER = ".hyperloom-assets"
CURL = ("--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "5",
        "--retry", "3", "--connect-timeout", "30", "--speed-limit", "1024", "--speed-time", "300",
        "--continue-at", "-", "--silent", "--show-error", "--output")


def lab_quiet(lab):
    """Refuse while any GPU turn is held, single or pair: a model-sized download fills the page
    cache, and reclaim of a serving engine's registered model pages stalls its decode."""
    install.lab_idle(lab)
    if lab is None:
        return
    try:
        holder = (install.absolute(lab) / ".gpu-turn" / "holder").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if holder:
        raise ValueError("downloads are refused while a GPU turn is held: " + holder.split()[0])


def no_lease(root):
    """Refuse while a hyperloom lease of this install root is not finished (it may be serving)."""
    live = install.active_leases(root)
    if live:
        raise ValueError("downloads are refused while a hyperloom lease is active: " + str(live[0].get("lease")))


def source_url(item):
    repo, revision, name = (install.text(item.get(k)) for k in ("repo", "revision", "file"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("invalid source repository")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be an immutable 40-hex commit")
    parts = name.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", part) for part in parts):
        raise ValueError("invalid source file path")
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}"
    if urlsplit(url).hostname not in HOSTS:
        raise ValueError("source host is not allowed")
    return url


def target(item, protected=()):
    destination = install.absolute(item.get("destination"))
    directory = install.absolute(item.get("directory"))
    if destination.parent != directory or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", destination.name):
        raise ValueError("destination must be a plain file name in the asset directory")
    resolved = directory.resolve()
    if resolved != directory or not resolved.is_dir():
        raise ValueError("asset directory must exist and contain no symlinks")
    if any(install.overlap(resolved, Path(other).resolve()) for other in protected):
        raise ValueError("asset directory overlaps the install root or the lab directory")
    marker = directory / ASSET_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"asset directory is not dedicated to hyperloom downloads (no {ASSET_MARKER} file): "
                         + str(directory))
    if os.path.lexists(destination):
        raise ValueError("destination already exists; nothing is overwritten: " + str(destination))
    part = directory / ("." + destination.name + ".hyperloom-part")
    if part.is_symlink() or (part.exists() and not part.is_file()):
        raise ValueError("partial download path is not a regular file")
    return destination, part


def plan(payload, root):
    files = payload.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("files must list 1..64 entries")
    curl = shutil.which("curl", path="/usr/local/bin:/usr/bin:/bin")
    if curl is None:
        raise ValueError("curl is not installed on this machine")
    prefix = ["nice", "-n", "19"] + (["ionice", "-c3"] if shutil.which("ionice") else [])
    rows, need, directories = [], {}, set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("file entry must be an object")
        size, digest = item.get("bytes"), item.get("sha256")
        if type(size) is not int or size <= 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("every file needs pinned bytes and sha256")
        destination, part = target(item, [root] + ([payload["lab_dir"]] if payload.get("lab_dir") else []))
        if str(destination) in directories:
            raise ValueError("duplicate destination")
        directories.add(str(destination))
        rows.append({"url": source_url(item), "destination": str(destination), "part": str(part),
                     "bytes": size, "sha256": digest,
                     "argv": [*prefix, curl, *CURL, str(part), source_url(item)]})
        have = part.stat().st_size if part.exists() else 0
        need[destination.parent] = need.get(destination.parent, 0) + max(0, size - have)
    code = hashlib.sha256(install.canonical(rows)).hexdigest()[:12]
    disk = []
    for directory, required in need.items():
        free = shutil.disk_usage(directory).free
        disk.append({"directory": str(directory), "required_bytes": required, "free_bytes": free,
                     "ok": free >= required + HEADROOM})
    return {"files": rows, "disk": disk, "confirm": code}


def lock_part(part):
    """Open (creating if needed) and exclusively lock the partial file: one writer per destination."""
    fd = os.open(part, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if fcntl is None:
        # Not POSIX (unit tests only; node jobs require POSIX): no lock, and no open handle.
        os.close(fd)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ValueError("another download job is writing " + str(part)) from None
    return fd


def fetch(row, stop, turn_check):
    """Run the planned command for one file and verify it; returns a failure reason or None."""
    part, destination = Path(row["part"]), Path(row["destination"])
    if os.path.lexists(destination):
        return "destination appeared during the job: " + row["destination"]
    try:
        lock = lock_part(part)
    except ValueError as exc:
        return str(exc)
    try:
        process = subprocess.Popen(row["argv"], stdin=subprocess.DEVNULL, start_new_session=True)
        while process.poll() is None:
            reason = stop() or turn_check()
            if reason:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait()
                return reason
            time.sleep(2)
        if process.returncode:
            return f"download command exited {process.returncode}; rerun to resume"
        size = part.stat().st_size
        if size != row["bytes"]:
            part.unlink(missing_ok=True)
            return "verification failed (size); partial file removed"
        last = [0.0]

        def check():
            # The GPU-turn and lease rules hold while hashing too (once a second is enough).
            now = time.monotonic()
            if now - last[0] >= 1:
                last[0] = now
                reason = stop() or turn_check()
                if reason:
                    raise ValueError(reason)

        try:
            # main's hasher: O_DIRECT where the filesystem supports it, so the model is not
            # pulled through the page cache a second time.
            digest = install.hash_file(part, check=check)
        except ValueError as exc:
            return str(exc)
        if digest != row["sha256"]:
            part.unlink(missing_ok=True)
            return "verification failed (sha256); partial file removed"
        try:
            # link() fails when the destination exists: never overwrite, even in a race.
            os.link(part, destination)
        finally:
            part.unlink(missing_ok=True)
        return None
    finally:
        if lock is not None:
            os.close(lock)


def job_main(spec_path):
    spec = install.read_json(install.absolute(spec_path))
    root = install.checked_root({"root": spec["root"]})
    job = install.identifier(spec["job"])
    path = install.inside(root, "run", "job-" + job + ".json")
    state = {"job": job, "kind": "download", "pid": os.getpid(), "boot_id": install.boot_id(),
             "state": "running", "started": time.time(),
             "log": str(install.inside(root, "log", "job-" + job + ".log")), "done": []}
    install.atomic_json(path, state)
    stop_file = install.inside(root, "run", "job-" + job + ".stop")

    def turn():
        try:
            lab_quiet(spec.get("lab_dir"))
            no_lease(root)
        except ValueError as exc:
            return str(exc) + "; partial file kept, rerun to resume"
        return None

    try:
        for row in spec["files"]:
            reason = fetch(row, lambda: "cancelled" if stop_file.exists() else None, turn)
            if reason:
                raise ValueError(reason)
            state["done"].append(row["destination"])
            install.atomic_json(path, state)
        state["state"] = "succeeded"
    except Exception as exc:
        state.update(state="cancelled" if str(exc) == "cancelled" else "failed", error=str(exc))
    finally:
        state["finished"] = time.time()
        install.atomic_json(path, state)
        install.absolute(spec_path).unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
    return 0 if state["state"] == "succeeded" else 1


def start(root, payload):
    result = plan(payload, root)
    if payload.get("confirm") != result["confirm"]:
        raise ValueError("confirmation code does not match the current plan; review the plan again")
    if not all(row["ok"] for row in result["disk"]):
        raise ValueError("not enough free disk space for the planned files")
    if os.name != "posix":
        raise ValueError("node background jobs require POSIX")
    job = uuid.uuid4().hex
    spec = install.inside(root, "run", "job-" + job + ".spec.json")
    state_path = install.inside(root, "run", "job-" + job + ".json")
    log = install.inside(root, "log", "job-" + job + ".log")
    install.atomic_json(spec, {"root": str(root), "job": job, "lab_dir": payload.get("lab_dir"),
                               "files": result["files"]})
    install.atomic_json(state_path, {"job": job, "kind": "download", "state": "queued", "log": str(log),
                                     "boot_id": install.boot_id()})
    # The job itself hashes the files: idle CPU and I/O priority, like verify-model.
    prefix = ["nice", "-n", "19"] + (["ionice", "-c3"] if shutil.which("ionice") else [])
    with log.open("ab") as output:
        child = subprocess.Popen(["nohup", *prefix, sys.executable, str(Path(__file__).resolve()), "--job", str(spec)],
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 start_new_session=True, close_fds=True)
    return {"job": job, "pid": child.pid, "log": str(log), "confirm": result["confirm"]}


def run(verb, payload):
    result = {"schema": "hyperloom/node-v1", "verb": verb}
    try:
        if verb not in VERBS or not isinstance(payload, dict):
            raise ValueError("unsupported download request")
        root = install.checked_root(payload)
        policy = install.read_json(root / "etc" / "install-policy.json")
        payload = {**payload, "lab_dir": payload.get("lab_dir") or policy.get("lab_dir")}
        if verb == "download-plan":
            result.update(plan(payload, root))
        elif verb == "download":
            lab_quiet(payload["lab_dir"])
            no_lease(root)
            result.update(start(root, payload))
        else:
            job = install.identifier(payload.get("job"))
            path = install.inside(root, "run", "job-" + job + ".json")
            state = install.read_json(path)
            if state.get("kind") != "download":
                raise ValueError("not a download job")
            state = install.settle_stale(path, state)
            if state["state"] in {"queued", "running"}:
                install.inside(root, "run", "job-" + job + ".stop").touch()
            result.update(job=job, state=state["state"])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        raise SystemExit(job_main(sys.argv[2]))
    raise SystemExit("internal job entry point only")
