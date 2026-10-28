"""One node call per machine at a time, across threads, portal processes and the CLI.

Every node verb over ssh goes through ``hold`` (blocking, bounded wait) or ``try_hold`` (non-blocking, for
samplers that simply skip a busy machine this round). The gate is an OS file lock at
``<config dir>/node-locks/<machine>.lock`` plus an in-process lock, and it is reentrant for the thread that holds
it, so a holder may call ``API.node`` (which takes the gate itself) without deadlocking.

The portal never creates a lock file while it answers a request (its GETs have no side effects): ``portal`` makes
the files of every configured machine at startup (``prepare`` again after a configuration save) and from then on
this process only opens existing files. A machine without a file yet (added to the configuration by hand while the
portal runs) is gated in this process only until the CLI, which creates files as before, or a save makes it.
"""

from contextlib import contextmanager
from pathlib import Path
import re
import threading
import time

from . import procs


_guard = threading.Lock()
_locks = {}       # path -> threading.Lock
_owners = {}      # path -> [thread ident, depth, InstanceLock or None (in-process only)]
_create = True    # False in the portal process (see ``portal``)


def gate_path(config_path, machine_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(machine_id)):
        raise ValueError("invalid machine id for the node gate")
    return Path(config_path).parent / "node-locks" / (str(machine_id) + ".lock")


def prepare(config_path, machine_ids):
    """Create the lock files of these machines (startup, configuration save; never while answering a GET)."""
    for machine_id in machine_ids:
        try:
            path = gate_path(config_path, machine_id)
        except ValueError:
            continue
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as stream:
                if stream.tell() == 0:
                    stream.write(b"\0")


def portal(config_path, machine_ids):
    """This process is the portal: the lock files are made now, and node calls only open existing ones."""
    global _create
    prepare(config_path, machine_ids)
    _create = False


def _enter(path, deadline, reentrant=True):
    key = str(path)
    me = threading.get_ident()
    with _guard:
        owner = _owners.get(key)
        if owner is not None and owner[0] == me and reentrant:
            owner[1] += 1
            return True
        if owner is not None and not reentrant:
            return False
        local = _locks.setdefault(key, threading.Lock())
    while True:
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        if local.acquire(timeout=-1 if wait is None else wait):
            break
        return False
    while True:
        try:
            handle = procs.InstanceLock(path).acquire(create=_create)
            break
        except FileNotFoundError:
            handle = None          # portal, no lock file yet: this process's own lock only (nothing created)
            break
        except (procs.ProcessError, OSError):
            if deadline is None or time.monotonic() >= deadline:
                local.release()
                return False
            time.sleep(0.05)
    with _guard:
        _owners[key] = [me, 1, handle]
    return True


def _leave(path):
    key = str(path)
    with _guard:
        owner = _owners[key]
        owner[1] -= 1
        if owner[1]:
            return
        del _owners[key]
        local = _locks[key]
    try:
        if owner[2] is not None:
            owner[2].close()
    finally:
        local.release()


@contextmanager
def hold(config_path, machine_id, wait=15.0):
    """Hold the machine's gate for a node call; TimeoutError when another call keeps it longer than `wait`."""
    path = gate_path(config_path, machine_id)
    if not _enter(path, time.monotonic() + wait):
        raise TimeoutError(f"node {machine_id} is busy with another call")
    try:
        yield
    finally:
        _leave(path)


def try_hold(config_path, machine_id):
    """Take the gate now or not at all (never reentrant: a machine already held, even by this thread, is busy): a
    release function, or None. A worker thread that makes the call for the taker first calls ``adopt``."""
    path = gate_path(config_path, machine_id)
    if not _enter(path, time.monotonic(), reentrant=False):
        return None
    released = []

    def release():
        if not released:
            released.append(True)
            _leave(path)
    return release


def adopt(config_path, machine_id):
    """Make the calling thread the holder of a gate another thread took with ``try_hold`` (hand-off to the worker
    that makes the node call), so that worker's own ``hold`` (inside API.node) is reentrant."""
    key = str(gate_path(config_path, machine_id))
    with _guard:
        owner = _owners.get(key)
        if owner is None:
            raise RuntimeError("adopting a node gate nobody holds")
        owner[0] = threading.get_ident()
