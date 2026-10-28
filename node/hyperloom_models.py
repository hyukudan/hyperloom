"""Model library: inventory and manifest verification (Python 3.10+, stdlib only).

Verbs (node extension contract: VERBS and run(verb, payload) -> dict):

- ``models``: read-only inventory of model roots (units, sizes, file counts, newest mtime, manifest status).
  Bounded by a deadline, directory entries, depth, units, manifest bytes and the reply size; it creates nothing.
- ``model-verify`` / ``copy-job`` / ``copy-cancel``: verification is not part of this release (it has no design
  that runs inside a lease or a GPU turn yet); the three verbs answer so before anything is read or written, and
  no background process exists behind them.
- ``copy-plan`` / ``copy``: copies between machines are not part of this release; both answer so, and nothing
  runs behind them.

``verify_unit`` (a bounded, cache-free hash of one unit in the calling process) stays for that later design; no
verb runs it. Reads never fill the page cache (direct I/O, or cache advice after each chunk, or unbuffered handles
on Windows).
"""

import ctypes
import errno
import hashlib
import json
import mmap
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import threading
import time

try:
    import hyperloom_install as install
except ImportError:
    # Loaded by path (tests, the control machine): the sibling modules ship beside this file.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hyperloom_install as install

try:
    import fcntl
except ImportError:  # not POSIX: node jobs are refused there; inventory and verification work
    fcntl = None


VERBS = {"models", "model-verify", "copy-plan", "copy", "copy-job", "copy-cancel"}
NOT_IN_RELEASE = "copies between machines are not available in this release"
VERIFY_NOT_IN_RELEASE = "model verification is not available in this release"
SCHEMA = "hyperloom/node-v1"
MAX_REPLY = 200 * 1024
DEADLINE_S = 5.0
MAX_ENTRIES = 20000
MAX_DEPTH = 4
MAX_UNITS = 500
MAX_ROOTS = 16
MANIFEST_FILE_MAX = 1 << 20
MANIFEST_TOTAL_MAX = 16 << 20
MANIFEST_LINES = 20000
MODEL_SUFFIXES = (".gguf", ".safetensors", ".bin", ".pt", ".pth", ".onnx")
SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/run", "/boot", "/etc")
BLOCK = 8 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}")
COMPONENT = re.compile(r"[^/\\:\x00-\x1f\x7f]{1,255}")
LINE = re.compile(r"([0-9a-fA-F]{64})\s[\s*]?(.+)")
DIRECT_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


# Constant remote commands: requests travel as JSON on stdin, never in the command line.
_request = threading.local()


def request_left():
    """Seconds left of the synchronous verb being served (None in jobs and stream entry points)."""
    deadline = getattr(_request, "deadline", None)
    return None if deadline is None else deadline - time.monotonic()


def request_check():
    left = request_left()
    if left is not None and left <= 0:
        raise ValueError("the request ran past its %d s deadline" % DEADLINE_S)


class Stopped(Exception):
    """The job was cancelled or its pause limit was reached (the message is the outcome)."""


class Bound(Exception):
    """A scan budget was reached (the message names it)."""


# ---------------------------------------------------------------------------------------------------------------
# Validation

def text(value, what="value"):
    try:
        return install.text(value)
    except ValueError:
        raise ValueError(what + " must be nonempty text without control characters") from None


def unit_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("unit name must be a plain file or directory name")
    return value


def rel_path(value, what="path"):
    """A relative POSIX path of plain components (no '.', '..', empty part, backslash or colon)."""
    if not isinstance(value, str) or not value or len(value) > 1024 or value.startswith("/"):
        raise ValueError(what + " must be a relative path")
    for part in value.split("/"):
        if part in (".", "..") or not COMPONENT.fullmatch(part):
            raise ValueError(what + " has an invalid component: " + value)
    return value


def read_root(value):
    """A model root to read: absolute, an existing directory, not a system directory; nothing below is followed."""
    path = install.absolute(value)
    if os.name == "posix":
        name = str(PurePosixPath(str(path)))
        if name == "/" or any(name == d or name.startswith(d + "/") for d in SYSTEM_DIRS):
            raise ValueError("model root is a filesystem root or a system directory: " + name)
    elif path == Path(path.anchor):
        raise ValueError("model root is a filesystem root: " + str(path))
    if not path.is_dir():
        raise ValueError("model root is not an existing directory: " + str(path))
    return path


def deadline_of(payload):
    value = payload.get("deadline_s", DEADLINE_S)
    if type(value) not in (int, float) or not 0 < value <= DEADLINE_S:
        raise ValueError("deadline_s must be in (0, 5]")
    return time.monotonic() + value


def keys(payload, allowed, required=()):
    extra = set(payload) - set(allowed)
    missing = set(required) - set(payload)
    if extra or missing:
        raise ValueError("unexpected fields " + str(sorted(extra)) + "; missing fields " + str(sorted(missing)))


# ---------------------------------------------------------------------------------------------------------------
# Guard policy

class Policy:
    def __init__(self, root, lab, required=False):
        self.root, self.lab, self.required = Path(root), lab, required

    def payload(self):
        return {"root": str(self.root), "lab_dir": self.lab, "lab_required": self.required}


def marked_root(value):
    """An existing marked install root, checked without creating anything (unlike install.checked_root)."""
    requested = install.absolute(value)
    if requested.is_symlink():
        raise ValueError("install root must not be a symlink")
    root = requested.resolve()
    marker = root / ".hyperloom-root"
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("unmarked install root")
    if not re.fullmatch(r"[0-9a-f]{32}", marker.read_text(encoding="ascii").strip()):
        raise ValueError("invalid root marker")
    for name in ("etc", "run", "log"):
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("install root has no " + name + " directory")
    policy = root / "etc" / "install-policy.json"
    if policy.is_symlink() or not policy.is_file():
        raise ValueError("install root has no install policy")
    return root, install.read_json(policy)


def effective_policy(payload):
    """The guard policy: the node's recorded lab directory wins; a different one sent by the caller is refused; in
    lab mode (lab_required) a missing lab directory refuses rather than skipping the turn guard."""
    root, recorded = marked_root(payload.get("root"))
    sent = payload.get("lab_dir")
    if sent is not None:
        sent = str(install.absolute(sent))
    lab = recorded.get("lab_dir")
    if lab is not None:
        lab = str(install.absolute(lab))
        if sent is not None and sent != lab:
            raise ValueError("the lab directory sent differs from the node's install policy")
    lab = lab or sent
    required = payload.get("lab_required", False)
    if type(required) is not bool:
        raise ValueError("lab_required must be a boolean")
    if required and lab is None:
        raise ValueError("turn guard has no lab directory (lab mode needs one)")
    return Policy(root, lab, required)


# ---------------------------------------------------------------------------------------------------------------
# Inventory

class Budget:
    def __init__(self, deadline, tick=None):
        self.deadline, self.entries, self.manifest_bytes = deadline, 0, 0
        self.tick = tick            # a verification's stop/guard check, serviced while it scans and parses
        self.snapshots = {}         # manifest path -> (sha256, size) of the exact bytes that were parsed

    def check(self):
        if self.tick is not None:
            started = time.monotonic()
            self.tick()
            self.deadline += time.monotonic() - started   # a guard pause does not use up the scan budget
        if time.monotonic() >= self.deadline:
            raise Bound("deadline")

    def entry(self):
        self.check()
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise Bound("files")


class Entry:
    __slots__ = ("name", "kind", "size", "mtime_ns")

    def __init__(self, name, kind, size=0, mtime_ns=0):
        self.name, self.kind, self.size, self.mtime_ns = name, kind, size, mtime_ns


class Tree:
    """Read access below one model root. POSIX: everything is descriptor-relative from the root, never following a
    link below it (a directory replaced by a link after listing is refused when opened). Windows (the control
    machine): listings by path with links and junctions skipped, and file contents through the unbuffered handles
    that check their final path (open_protected)."""

    def __init__(self, root):
        self.root = read_root(root)
        self.fd = None
        if os.name == "posix":
            self.fd = os.open(str(self.root), os.O_RDONLY | DIRECTORY)
        else:
            # Resolved once; the root directory stays open for the scan (an open directory cannot be renamed, nor
            # can any directory above it), and every handle below is checked against this canonical path.
            self.canonical = Path(os.path.realpath(str(self.root)))
            self.handle = windows_open_dir(self.canonical, [])

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if getattr(self, "handle", None) is not None:
            windows_api().CloseHandle(self.handle)
            self.handle = None

    def open(self, path, expected_size=None, expected_identity=None):
        """A cache-free Reader for a file of this tree (bound to the same root the listings used)."""
        rel_path(path)
        parts = path.split("/")
        if self.fd is None and getattr(self, "handle", None) is None:
            raise ValueError("the tree is closed")
        if self.fd is None:
            reader = windows_open(self.canonical, parts, expected_size, resolved=True)
        else:
            directory = open_dir_chain(self.fd, parts[:-1])
            try:
                reader = open_in(directory, parts[-1], expected_size)
            finally:
                os.close(directory)
        if expected_identity is not None and list(reader.identity[:2]) != list(expected_identity):
            reader.close()
            raise ValueError(path + " is not the file that was planned (it was replaced)")
        return reader

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def list(self, parts, budget):
        """Entries of a directory (bounded while enumerating; sorted only once the bounded list is complete)."""
        rows = []
        if self.fd is not None:
            directory = open_dir_chain(self.fd, parts)
            try:
                with os.scandir(directory) as listing:
                    for entry in listing:
                        budget.entry()
                        rows.append(self._entry(entry))
            finally:
                os.close(directory)
        else:
            # The directory is held open (no delete sharing: it cannot be renamed or replaced meanwhile) after its
            # handle proved it is a plain directory at the expected place, then listed.
            handle = windows_open_dir(self.canonical, parts)
            try:
                for entry in windows_list(handle):            # enumerated through the pinned handle itself
                    budget.entry()
                    rows.append(entry)
            finally:
                windows_api().CloseHandle(handle)
        rows.sort(key=lambda e: e.name)
        return rows

    @staticmethod
    def _entry(entry):
        info = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or not COMPONENT.fullmatch(entry.name):
            return Entry(entry.name, "link")
        if stat.S_ISDIR(info.st_mode):
            return Entry(entry.name, "dir")
        if stat.S_ISREG(info.st_mode):
            return Entry(entry.name, "file", info.st_size, info.st_mtime_ns)
        return Entry(entry.name, "other")

    def read(self, path, limit):
        """The bytes of a small regular file (no link followed on the way), at most ``limit`` bytes."""
        parts = path.split("/")
        if self.fd is None:
            reader = self.open(path)
            try:
                if reader.size > limit:
                    raise ValueError("file exceeds the read limit: " + path)
                data, offset = bytearray(), 0
                while offset < reader.size:
                    chunk = reader.read_at(offset)
                    if not chunk:
                        break
                    data += chunk
                    offset += len(chunk)
                return bytes(data[:reader.size])
            finally:
                reader.close()
        directory = open_dir_chain(self.fd, parts[:-1])
        try:
            fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file: " + path)
            if info.st_size > limit:
                raise ValueError("file exceeds the read limit: " + path)
            data = stream.read(limit + 1)
            if hasattr(os, "posix_fadvise"):
                drop_cache(stream.fileno())
        if len(data) > limit:
            raise ValueError("file exceeds the read limit: " + path)
        return data

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return (self.root / name).lstat()


def parse_manifest(tree, path, size, budget):
    """[(digest, relpath)] and the count of invalid lines of one manifest file. A manifest that cannot be read in
    full (over the per-file byte or line limit, or unreadable) is a scan bound, never a partial answer: what it lists
    is unknown, so nothing it might cover can be called complete."""
    budget.check()                               # before every read, also of an empty manifest
    if size > MANIFEST_FILE_MAX:
        raise Bound("manifest size")
    allowance = MANIFEST_TOTAL_MAX - budget.manifest_bytes
    if size > allowance:
        raise Bound("manifest bytes")
    limit = min(MANIFEST_FILE_MAX, allowance)
    try:
        # Never more than this file's limit and what is left of the aggregate allowance, whatever its size now.
        raw = tree.read(path, limit)
    except (OSError, ValueError):
        budget.manifest_bytes += limit           # what it may have consumed: counted in full
        raise Bound("changed while scanned") from None
    budget.manifest_bytes += len(raw)            # charged as read, before anything else
    budget.check()                               # and after it
    if len(raw) != size:                     # it changed since it was listed: what it lists is not known
        raise Bound("changed while scanned")
    budget.snapshots[path] = (hashlib.sha256(raw).hexdigest(), len(raw))
    rows, invalid = [], 0
    for number, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
        if not number % 1024:
            budget.check()
        if number >= MANIFEST_LINES:
            raise Bound("manifest lines")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = LINE.fullmatch(line.rstrip("\r"))
        try:
            if not match:
                raise ValueError
            rows.append((match[1].lower(), rel_path(match[2])))
        except ValueError:
            invalid += 1
    return rows, invalid


def walk_unit(tree, name, budget):
    """Regular files of a directory unit (path relative to the root -> (bytes, mtime_ns)), its manifests, the
    count of links and unaddressable names, and the bound reached (None when complete)."""
    files, manifests, links = {}, [], 0
    stack = [(name, 1)]
    try:
        while stack:
            relative, depth = stack.pop()
            for entry in tree.list(relative.split("/"), budget):
                path = relative + "/" + entry.name
                if entry.kind == "dir":
                    if depth + 1 >= MAX_DEPTH:      # files below it would exceed the depth bound
                        raise Bound("depth")
                    stack.append((path, depth + 1))
                elif entry.kind == "file":
                    files[path] = (entry.size, entry.mtime_ns)
                    if entry.name.endswith(".sha256"):
                        manifests.append(path)
                else:
                    links += 1
    except Bound as exc:
        return files, manifests, links, str(exc)
    except (OSError, ValueError):
        return files, manifests, links, "changed while scanned"
    return files, manifests, links, None


def sibling_index(siblings, budget):
    """unit name -> [(sibling manifest, its rows for that unit, its invalid line count)], built once per root. A
    sibling manifest without one valid line is attributed to the unit its name belongs to (``<unit>.sha256``), so
    its invalid lines are still reported."""
    index = {}
    for sibling, (rows, bad) in sorted(siblings.items()):
        budget.check()
        mine = {}
        for number, (digest, rel) in enumerate(rows):
            if not number % 1024:
                budget.check()
            mine.setdefault(rel.split("/", 1)[0], []).append((digest, rel))
        if not mine and bad:
            mine[sibling[:-len(".sha256")]] = []
        for unit, unit_rows in mine.items():
            index.setdefault(unit, []).append((sibling, unit_rows, bad))
    return index


def unit_entries(tree, name, files, manifests, index, budget):
    """Manifest entries of a unit: path relative to the root -> set of digests; the invalid line count; and the
    paths listed more than once (every file must be listed exactly once for a complete manifest)."""
    entries, invalid, seen, repeated = {}, 0, set(), set()

    def add(path, digest):
        if path in seen:
            repeated.add(path)
        seen.add(path)
        entries.setdefault(path, set()).add(digest)
    for manifest in manifests:
        directory = manifest.rsplit("/", 1)[0]
        rows, bad = parse_manifest(tree, manifest, files[manifest][0], budget)
        invalid += bad
        for digest, rel in rows:
            add(directory + "/" + rel, digest)
    for _, rows, bad in index.get(name, ()):
        budget.check()
        invalid += bad
        for digest, rel in rows:
            add(rel, digest)
    return entries, invalid, repeated


def root_listing(tree, budget):
    """Units, sibling manifests (name -> size) and the other-file count at a root's top level, or the bound hit."""
    units, siblings, other, hit = [], {}, 0, None
    try:
        for entry in tree.list([], budget):
            if entry.name.startswith("."):
                continue
            if entry.kind == "file" and entry.name.endswith(".sha256") and NAME.fullmatch(entry.name):
                siblings[entry.name] = entry.size
            elif NAME.fullmatch(entry.name) and entry.kind == "dir":
                units.append((entry.name, "dir"))
            elif NAME.fullmatch(entry.name) and entry.kind == "file" and entry.name.lower().endswith(MODEL_SUFFIXES):
                units.append((entry.name, "file", entry.size, entry.mtime_ns))
            else:
                other += 1
    except Bound as exc:
        hit = str(exc)
    return units, siblings, other, hit


def scan_root(value, budget):
    """One model root: its units with sizes and manifest status. Never this account's home or a directory above it
    (an asset kept directly in the home must not make the whole home a model root)."""
    try:
        home = Path.home().resolve()
        if home == Path(value).resolve() or home.is_relative_to(Path(value).resolve()):
            raise ValueError("a home directory (or one above it) is not a model root")
    except RuntimeError:
        pass
    with Tree(value) as tree:
        return _scan(tree, budget)


def _scan(tree, budget):
    root = tree.root
    result = {"path": str(root), "units": [], "other_files": 0, "truncated": []}
    try:
        result["free_bytes"] = shutil.disk_usage(root).free
    except OSError:
        result["free_bytes"] = None
    units, sibling_sizes, result["other_files"], hit = root_listing(tree, budget)
    if len(units) > MAX_UNITS:
        units = units[:MAX_UNITS]
        result["truncated"].append("units")
    siblings = {}
    for name, size in sibling_sizes.items():
        if hit:
            break
        try:
            siblings[name] = parse_manifest(tree, name, size, budget)
        except Bound as exc:
            hit = str(exc)
    index = {}
    if not hit:
        try:
            index = sibling_index(siblings, budget)
        except Bound as exc:
            hit = str(exc)
    if hit:
        result["truncated"].append(hit)
    for item in units:
        name, kind = item[0], item[1]
        unit = {"name": name, "kind": kind}
        unit_hit = hit
        if not unit_hit:
            if kind == "dir":
                files, manifests, links, unit_hit = walk_unit(tree, name, budget)
            else:
                files, manifests, links = {name: (item[2], item[3])}, [], 0
        if not unit_hit:
            try:
                entries, invalid, repeated = unit_entries(tree, name, files, manifests, index, budget)
                budget.check()
            except Bound as exc:
                unit_hit = str(exc)
        if unit_hit:
            unit.update(bytes=None, files=None, newest_mtime=None, links=None,
                        manifest={"status": "unknown", "why": "scan bound reached: " + unit_hit})
            result["truncated"].append(unit_hit)
            result["units"].append(unit)
            continue
        data = {path for path in files if path not in manifests}
        listed = data & set(entries)
        missing = sorted(set(entries) - set(files))
        conflicts = sorted(path for path, digests in entries.items() if len(digests) > 1)
        unlisted = sorted(data - set(entries))
        covering = sorted(manifests + [sibling for sibling, _, _ in index.get(name, ())])
        status = ("partial" if not entries and invalid else "none" if not entries else "conflict" if conflicts
                  else "missing" if missing else "partial" if unlisted or invalid or repeated else "complete")
        unit.update(bytes=sum(size for size, _ in files.values()), files=len(files),
                    newest_mtime=max(mtime for _, mtime in files.values()) / 1e9 if files else None, links=links,
                    manifest={"status": status, "listed": len(listed), "missing": len(missing),
                              "unlisted": len(unlisted), "invalid": invalid, "conflicts": len(conflicts),
                              "repeated": len(repeated),
                              "manifests": covering[:32]})
        result["units"].append(unit)
    result["truncated"] = sorted(set(result["truncated"]))
    return result


def scan_lock(policy):
    """Coalesce scans of one machine: a non-blocking lock on the install root's existing run directory."""
    if fcntl is None or policy is None:
        return None
    fd = os.open(policy.root / "run", os.O_RDONLY | DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    return fd


def models(payload):
    keys(payload, ("root", "lab_dir", "lab_required", "roots", "deadline_s"), ("roots",))
    roots = payload["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS:
        raise ValueError("roots must list 1..16 model roots")
    budget = Budget(deadline_of(payload))
    policy = effective_policy(payload) if payload.get("root") is not None else None
    lock = scan_lock(policy)
    if lock is False:
        return {"busy": True}
    try:
        rows = []
        for value in roots:
            try:
                rows.append(scan_root(value, budget))
            except (OSError, ValueError) as exc:
                rows.append({"path": str(value), "error": str(exc), "units": [], "truncated": []})
    finally:
        if lock is not None:
            os.close(lock)
    return {"roots": rows, "cache": cache_control()}


# ---------------------------------------------------------------------------------------------------------------
# Cache-free reading and hashing

def drop_cache(fd, offset=0, length=0):
    os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)


def cache_control():
    """How this machine keeps model reads out of the cache: 'direct', 'advice' or 'unbuffered'; None = refused."""
    if os.name == "nt":
        return "unbuffered" if windows_api() is not None else None
    if hasattr(os, "O_DIRECT") and hasattr(os, "preadv"):
        return "direct"
    if hasattr(os, "posix_fadvise"):
        return "advice"
    return None


class Reader:
    """Reads of an opened file that never leave its pages cached: O_DIRECT where the filesystem supports it, else
    reads followed by DONTNEED advice; Windows: an unbuffered handle with sector-aligned requests."""

    def __init__(self, fd=None, handle=None, direct=False, size=0, align=4096):
        self.fd, self.handle, self.direct, self.size, self.align = fd, handle, direct, size, align
        self.identity = None
        self.buffer = mmap.mmap(-1, BLOCK)
        self.view = memoryview(self.buffer)

    def read_at(self, offset):
        """Up to one block of bytes starting at offset; b'' at the end of the file."""
        if offset >= self.size:
            return b""
        if self.handle is not None or self.direct:
            aligned = offset - offset % self.align
            count = (windows_read(self.handle, aligned, self.buffer) if self.handle is not None
                     else os.preadv(self.fd, [self.view], aligned))
            if count <= offset - aligned:
                return b""
            return bytes(self.view[offset - aligned:count])
        data = os.pread(self.fd, BLOCK, offset)
        drop_cache(self.fd, offset, len(data))
        return data

    def current_identity(self):
        if self.handle is not None:
            info = _FileInfo()
            if not windows_api().GetFileInformationByHandle(self.handle, ctypes.byref(info)):
                raise ValueError("file information unavailable")
            return (info.volume, (info.index_high << 32) | info.index_low, (info.size_high << 32) | info.size_low,
                    (info.written_high << 32) | info.written_low)
        info = os.fstat(self.fd)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def close(self):
        self.view.release()
        self.buffer.close()
        if self.handle is not None:
            windows_api().CloseHandle(self.handle)
            self.handle = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def hash_reader(reader, check=None, progress=None):
    digest, offset = hashlib.sha256(), 0
    if check is not None:
        check()                                   # also for an empty file
    while offset < reader.size:
        if check is not None:
            check()
        data = reader.read_at(offset)
        if not data:
            raise ValueError("file shrank while it was read")
        digest.update(data)
        offset += len(data)
        if progress is not None:
            progress(offset)
    if reader.identity is not None and reader.current_identity() != tuple(reader.identity):
        raise ValueError("the file changed while it was read")
    return digest.hexdigest()


def open_dir_chain(base_fd, parts, create=False):
    """Descriptor-relative walk through plain directories (no symlink is followed); returns an fd."""
    fd = os.dup(base_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_in(directory, leaf, expected_size=None):
    """A cache-free Reader for one file in an opened directory (no symlink, regular file, the expected size)."""
    reader = None
    if hasattr(os, "O_DIRECT") and hasattr(os, "preadv"):
        try:
            fd = os.open(leaf, os.O_RDONLY | NOFOLLOW | NONBLOCK | os.O_DIRECT, dir_fd=directory)
        except OSError as exc:
            if exc.errno not in DIRECT_UNSUPPORTED:
                raise
            fd = None
        if fd is not None:
            reader = Reader(fd=fd, direct=True, size=os.fstat(fd).st_size)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                reader.close()
                raise ValueError("not a regular file: " + leaf)
            try:
                if reader.size:
                    with reader.view[:4096] as probe:     # some filesystems accept O_DIRECT, then refuse reads
                        os.preadv(fd, [probe], 0)
            except OSError as exc:
                reader.close()
                reader = None
                if exc.errno not in DIRECT_UNSUPPORTED:
                    raise
    if reader is None:
        if not hasattr(os, "posix_fadvise"):
            raise ValueError("this machine cannot read without filling the page cache; refused")
        fd = os.open(leaf, os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=directory)
        reader = Reader(fd=fd, direct=False, size=os.fstat(fd).st_size)
    info = os.fstat(reader.fd)
    if not stat.S_ISREG(info.st_mode):
        reader.close()
        raise ValueError("not a regular file: " + leaf)
    if expected_size is not None and info.st_size != expected_size:
        reader.close()
        raise ValueError(f"{leaf}: size {info.st_size} differs from the planned {expected_size}")
    reader.identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return reader


def open_protected(root, path, expected_size=None, expected_identity=None):
    """A cache-free Reader for <root>/<path> with no symlink or reparse point below the root (the root itself may
    be reached through a link); with ``expected_identity`` ([device, file id]) it must be that very file."""
    rel_path(path)
    parts = path.split("/")
    if os.name == "nt":
        reader = windows_open(Path(root), parts, expected_size)
    else:
        root_fd = os.open(str(root), os.O_RDONLY | DIRECTORY)
        try:
            directory = open_dir_chain(root_fd, parts[:-1])
        finally:
            os.close(root_fd)
        try:
            reader = open_in(directory, parts[-1], expected_size)
        finally:
            os.close(directory)
    if expected_identity is not None and list(reader.identity[:2]) != list(expected_identity):
        reader.close()
        raise ValueError(path + " is not the file that was planned (it was replaced)")
    return reader


# Windows (the control machine's verification): unbuffered handles that deny writers, checked against
# their final path so that a link or junction below the root is refused.

_windows = None


def windows_api():
    global _windows
    if os.name != "nt":
        return None
    if _windows is None:
        try:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            k32.ReadFile.restype = wintypes.BOOL
            k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
            k32.SetFilePointerEx.restype = wintypes.BOOL
            k32.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
            k32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.GetFileInformationByHandle.restype = wintypes.BOOL
            k32.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD)]
            k32.GetDiskFreeSpaceW.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            _windows = k32
        except (OSError, AttributeError):
            _windows = False
    return _windows or None


class _FileInfo(ctypes.Structure):
    # BY_HANDLE_FILE_INFORMATION: FILETIME is two DWORDs (4-byte aligned), so no 64-bit fields here.
    _fields_ = [("attributes", ctypes.c_uint32), ("created_low", ctypes.c_uint32), ("created_high", ctypes.c_uint32),
                ("accessed_low", ctypes.c_uint32), ("accessed_high", ctypes.c_uint32),
                ("written_low", ctypes.c_uint32), ("written_high", ctypes.c_uint32),
                ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
                ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32), ("index_high", ctypes.c_uint32),
                ("index_low", ctypes.c_uint32)]


def _final_path(k32, handle):
    buffer = ctypes.create_unicode_buffer(32768)
    count = k32.GetFinalPathNameByHandleW(handle, buffer, 32768, 0)
    if not count or count >= 32768:
        raise ValueError("final path unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith("\\\\?\\") else value


def sector_alignment(k32, path):
    from ctypes import wintypes
    anchor = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    values = [wintypes.DWORD() for _ in range(4)]
    if not k32.GetDiskFreeSpaceW(anchor, *(ctypes.byref(v) for v in values)):
        raise ValueError("the volume's sector size is unavailable: " + anchor)
    sector = values[1].value
    if sector <= 0 or BLOCK % sector or sector & (sector - 1):
        raise ValueError("unsupported sector size %d on %s" % (sector, anchor))
    return max(4096, sector)


def windows_open(root, parts, expected_size, resolved=False):
    k32 = windows_api()
    if k32 is None:
        raise ValueError("unbuffered reads are unavailable on this machine; refused")
    path = os.path.join(str(root) if resolved else os.path.realpath(str(root)), *parts)
    # GENERIC_READ, FILE_SHARE_READ only (no writer or rename while it is read), OPEN_EXISTING,
    # FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT.
    handle = k32.CreateFileW(path, 0x80000000, 0x1, None, 3, 0x20000000 | 0x08000000 | 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ValueError(f"cannot open {path} unbuffered (error {ctypes.get_last_error()})")
    try:
        info = _FileInfo()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ValueError("file information unavailable: " + path)
        if info.attributes & 0x400 or info.attributes & 0x10:
            raise ValueError("not a regular file (directory or reparse point): " + path)
        if os.path.normcase(_final_path(k32, handle)) != os.path.normcase(path):
            raise ValueError("the path resolves elsewhere (a link or junction on the way): " + path)
        size = (info.size_high << 32) | info.size_low
        if expected_size is not None and size != expected_size:
            raise ValueError(f"{path}: size {size} differs from the planned {expected_size}")
        align = sector_alignment(k32, path)
    except BaseException:
        k32.CloseHandle(handle)
        raise
    reader = Reader(handle=handle, size=size, align=align)
    reader.identity = (info.volume, (info.index_high << 32) | info.index_low, size,
                       (info.written_high << 32) | info.written_low)
    return reader


def windows_open_dir(root, parts):
    """A handle on a directory below a model root that is neither a link nor a junction, at exactly root/parts
    (checked on the handle), opened without delete sharing so it cannot be renamed or replaced while held."""
    k32 = windows_api()
    if k32 is None:
        raise ValueError("directory handles are unavailable on this machine")
    path = os.path.join(str(root), *parts)
    # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    handle = k32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0x02000000 | 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ValueError(f"cannot open the directory {path} (error {ctypes.get_last_error()})")
    try:
        info = _FileInfo()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ValueError("directory information unavailable: " + path)
        if info.attributes & 0x400 or not info.attributes & 0x10:
            raise ValueError("not a plain directory (a link or junction): " + path)
        if os.path.normcase(_final_path(k32, handle)) != os.path.normcase(path):
            raise ValueError("the directory resolves elsewhere (a link or junction on the way): " + path)
    except BaseException:
        k32.CloseHandle(handle)
        raise
    return handle


def windows_list(handle):
    """Entries of a directory read through its handle (FileIdBothDirectoryInfo), so the listing is of the very
    directory the handle pinned; yielded one by one, so the caller's budget stops the enumeration itself.
    Reparse points are reported as links."""
    from ctypes import wintypes
    k32 = windows_api()
    query = k32.GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(64 * 1024)
    first = True
    while True:
        # FileIdBothDirectoryRestartInfo (11) for the first call, FileIdBothDirectoryInfo (10) after.
        if not query(handle, 11 if first else 10, buffer, len(buffer)):
            code = ctypes.get_last_error()
            if code == 18:                                    # ERROR_NO_MORE_FILES
                return
            raise OSError(code, "directory listing failed")
        first = False
        offset = 0
        raw = buffer.raw
        while True:
            following = int.from_bytes(raw[offset:offset + 4], "little")
            attributes = int.from_bytes(raw[offset + 56:offset + 60], "little")
            name_bytes = int.from_bytes(raw[offset + 60:offset + 64], "little")
            name = raw[offset + 104:offset + 104 + name_bytes].decode("utf-16-le")
            size = int.from_bytes(raw[offset + 40:offset + 48], "little", signed=True)
            written = int.from_bytes(raw[offset + 24:offset + 32], "little", signed=True)
            if name not in (".", ".."):
                if attributes & 0x400 or not COMPONENT.fullmatch(name):
                    yield Entry(name, "link")
                elif attributes & 0x10:
                    yield Entry(name, "dir")
                else:
                    yield Entry(name, "file", size, (written - 116444736000000000) * 100)
            if not following:
                break
            offset += following


def windows_replace(source, target):
    """Durable rename over an existing file on Windows (MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)."""
    from ctypes import wintypes
    k32 = windows_api()
    if k32 is None:
        raise ValueError("durable replacement is unavailable on this machine")
    move = k32.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    if not move(str(source), str(target), 0x1 | 0x8):
        code = ctypes.get_last_error()
        raise PermissionError(code, "replacement failed", str(target)) if code in (5, 32) else \
            OSError(code, "replacement failed", str(target))


def windows_read(handle, offset, buffer):
    from ctypes import wintypes
    k32 = windows_api()
    if not k32.SetFilePointerEx(handle, offset, None, 0):
        raise OSError(ctypes.get_last_error(), "seek failed")
    count = wintypes.DWORD()
    address = ctypes.addressof(ctypes.c_char.from_buffer(buffer))
    if not k32.ReadFile(handle, address, len(buffer), ctypes.byref(count), None):
        raise OSError(ctypes.get_last_error(), "read failed")
    return count.value


# ---------------------------------------------------------------------------------------------------------------
# Verification

def verify_unit(model_root, name, check=None, progress=None, budget_s=30.0):
    """Hash every manifest entry of one unit (cache-free, protected opens); counts and the first 50 failures."""
    name = unit_name(name)
    if check is not None:
        check()                                   # before anything is read
    budget = Budget(time.monotonic() + budget_s, check)
    with Tree(model_root) as tree:
        root = tree.root
        units, sibling_sizes, _, hit = root_listing(tree, budget)
        unit = next((u for u in units if u[0] == name), None)
        if hit:
            raise ValueError("the scan reached a bound (" + hit + "); verification refused")
        if unit is None:
            raise LookupError("no such model unit: " + name)
        if unit[1] == "dir":
            files, manifests, links, hit = walk_unit(tree, name, budget)
        else:
            files, manifests, links = {name: (unit[2], unit[3])}, [], 0
        if hit:
            raise ValueError("the scan reached a bound (" + hit + "); verification refused")
        try:
            siblings = {s: parse_manifest(tree, s, size, budget) for s, size in sibling_sizes.items()}
            entries, invalid, _ = unit_entries(tree, name, files, manifests, sibling_index(siblings, budget),
                                               budget)
        except Bound as exc:
            raise ValueError("the scan reached a bound (" + str(exc) + "); verification refused") from None
        if not entries:
            raise ValueError(name + " has no manifest entries to verify against")
        counts = {"ok": 0, "mismatch": 0, "missing": 0, "changed": 0, "conflict": 0}
        failures, hashed = [], 0
        for path in sorted(entries):
            if check is not None:
                check()                           # every entry, also the ones that are not read
            digests = entries[path]
            result, why = None, None
            if len(digests) > 1:
                result = "conflict"
            elif path not in files:
                result = "missing"
            else:
                try:
                    reader = tree.open(path, files[path][0])
                except (OSError, ValueError) as exc:
                    result, why = "changed", str(exc)
                else:
                    try:
                        base = hashed
                        digest = hash_reader(reader, check, None if progress is None else
                                             (lambda done: progress(base + done)))
                        result = "ok" if digest == next(iter(digests)) else "mismatch"
                        hashed += files[path][0]
                    except Stopped:
                        raise                                 # cancellation or the pause limit ends the run
                    except ValueError as exc:
                        result, why = "changed", str(exc)
                    finally:
                        reader.close()
            counts[result] += 1
            if result != "ok":
                failures.append({"path": path, "result": result, **({"why": why} if why else {})})
        unlisted = len([p for p in files if p not in entries and p not in manifests])
        return {"unit": name, "model_root": str(root), "entries": len(entries), "bytes_hashed": hashed,
                "counts": counts, "unlisted": unlisted, "invalid_lines": invalid, "failures": failures[:50],
                "ok": counts["ok"] == len(entries)}


# ---------------------------------------------------------------------------------------------------------------
# Envelope

def bounded(result):
    """Trim a reply to the envelope cap by rule (an error rather than a truncated JSON when it cannot fit). Costs
    are bounded: every unit is serialized once, units are dropped from the end of the longest roots by their sizes,
    and the whole reply is serialized once more at the end."""
    def size():
        return len(json.dumps(result, ensure_ascii=True, allow_nan=False))
    total = size()
    if total <= MAX_REPLY:
        return result
    roots = result.get("roots") or []
    lengths = [[len(json.dumps(unit, ensure_ascii=True, allow_nan=False)) + 1 for unit in root.get("units", [])]
               for root in roots]
    excess = total - MAX_REPLY + 64 * (len(roots) + 1)        # room for the added "output" markers
    while excess > 0 and any(lengths):
        index = max(range(len(roots)), key=lambda i: len(lengths[i]))
        roots[index]["units"].pop()
        excess -= lengths[index].pop()
        if "output" not in roots[index].setdefault("truncated", []):
            roots[index]["truncated"].append("output")
    if "tail" in result and size() > MAX_REPLY:
        result["tail"] = result["tail"][-4096:]
    if size() > MAX_REPLY:
        return {"schema": result["schema"], "verb": result["verb"], "error": "reply exceeds the size limit"}
    return result


ERROR_TEXT_MAX = 2048


def run(verb, payload):
    result = {"schema": SCHEMA, "verb": verb if verb in VERBS else "unsupported"}
    _request.deadline = time.monotonic() + DEADLINE_S   # C5: every synchronous verb, lock waits included
    try:
        if verb not in VERBS or not isinstance(payload, dict):
            raise ValueError("unsupported model library request")
        if verb == "models":
            result.update(models(payload))
        elif verb in ("copy-plan", "copy"):
            raise ValueError(NOT_IN_RELEASE)
        else:                                          # model-verify, copy-job, copy-cancel
            raise ValueError(VERIFY_NOT_IN_RELEASE)
        result = bounded(result)
        request_check()
    except (OSError, ValueError, LookupError, TypeError, KeyError, UnicodeError,
            Stopped) as exc:
        text = str(exc).strip("'\"") if isinstance(exc, LookupError) else str(exc)
        if len(text) > ERROR_TEXT_MAX:
            text = text[:ERROR_TEXT_MAX] + " (truncated)"
        result = {"schema": SCHEMA, "verb": verb if verb in VERBS else "unsupported", "error": text}
    finally:
        _request.deadline = None
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > MAX_REPLY:     # the last line of defence
        result = {"schema": SCHEMA, "verb": result["verb"], "error": "the reply exceeds the size limit"}
    return result


if __name__ == "__main__":
    raise SystemExit("internal entry points only")
