"""Operations (F38 node tool, F39 power, F41 machine configuration, F42 refresh and intervals) and the small
helpers the turns and log-search features share.

GET  ops/nodes                    node tool digest per machine vs this checkout, update actions and their blockers
GET  ops/power                    per machine: profiles to stop, Wake-on-LAN and shutdown availability
GET  ops/config                   the configuration's machines in order, version, frozen fields, extras
GET  ops/collectors               collectors: state, registered default, interval bounds
POST ops/wake {machine}                                       (audited) Wake-on-LAN magic packet
POST ops/hostkey/scan {machine}                               host-key candidates (nothing is pinned)
POST ops/config/validate {op, ...}                            validation of an edit, nothing written
POST ops/config/save {version, op, ...}                       (audited) add | edit | remove | move
POST ops/machine-extras {machine, mac?, wol_broadcast?, hide_disks?, hide_interfaces?}   (audited)
POST telemetry/refresh {collector}                            refresh now (coalesced with a running collection)
POST telemetry/interval {collector, seconds}                  (audited) interval within the collector's bounds
jobs ops/nodes-update {profile, machine?}  ops/shutdown {machine, confirm}  ops/machine-test {machine}

GET documents use the shared core snapshot and cached node checks (they never start a fresh status round); the
authoritative checks (saved launches, the node's own lease records, running jobs) run inside the jobs and the
configuration save, right before acting, and an unknown state refuses. Preferences live in the telemetry store's
document table (namespace ``ops``). The shutdown helper (an executable) is set only from the command line.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import socket
import stat
import subprocess
import threading
import time
import uuid

from hyperloom import config as config_module
from hyperloom import telemetry
from hyperloom.winproc import no_window, retry_sharing


NAMESPACE = "ops"
FINAL_LEASE_STATES = ("stopped", "failed", "aborted", "cancelled", "canceled", "finished")
RUNNING_PROFILE_STATES = ("running", "starting", "stopping", "degraded")
FROZEN_FIELDS = ("id", "os", "access", "http_address", "ssh_target", "ssh_port", "install_root", "lab_dir", "lab_mode",
                 "peer_ssh")
MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
NODE_TOOL_INTERVAL = 60.0
ONLINE_WINDOW_S = 120.0
UPDATE_MIN_LEFT_S = 180.0
HELPER_OUTPUT = 64 * 1024
REQUESTS_TABLE = """CREATE TABLE IF NOT EXISTS ops_requests (id TEXT PRIMARY KEY, kind TEXT NOT NULL, collector TEXT NOT NULL,
    seconds REAL, created REAL NOT NULL, state TEXT NOT NULL, claimed REAL, owner TEXT, retries INTEGER NOT NULL DEFAULT 0,
    finished REAL, result TEXT);"""
REQUEST_KEEP_S = 600.0
CLAIM_TIMEOUT_S = 60.0


# -- preferences and small helpers --------------------------------------------------------------------------------
def store_for_read(api):
    """The telemetry store for a GET (None when this process has not opened it; never created here)."""
    return telemetry.existing_store(api)


def store_for_write(api):
    return telemetry.open_store(api)


def pref(api, namespace, key, default=None, store=None):
    store = store if store is not None else store_for_read(api)
    if store is None:
        return default
    try:
        return store.get(namespace, key, default)
    except Exception:
        return default


def set_pref(api, namespace, key, value):
    store = store_for_write(api)
    if value is None:
        store.delete(namespace, key)
    else:
        store.put(namespace, key, value)
    return value


def check_path(value, where, windows=None):
    """An absolute path text (no control characters, <= 1024); windows=None: either convention."""
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError(where + " must be an absolute path")
    posix, win = PurePosixPath(value), PureWindowsPath(value)
    if windows:
        absolute, parts = win.is_absolute() and not value.startswith(("\\\\", "//")), win.parts
    elif windows is False:
        absolute, parts = posix.is_absolute() and "\\" not in value, posix.parts
    else:
        absolute, parts = win.is_absolute() or posix.is_absolute(), posix.parts + win.parts
    if not absolute or ".." in parts:
        raise ValueError(where + " must be an absolute path without '..'")
    return value


def strict(arguments, allowed, required=()):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    unknown = set(arguments) - set(allowed)
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)))
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ValueError("missing arguments: " + ", ".join(missing))
    return arguments


def machine_of(api, mid, cluster=None):
    cluster = cluster or api.cluster()
    if not isinstance(mid, str) or mid not in cluster.machines:
        raise LookupError("unknown machine " + str(mid)[:64])
    return cluster.machines[mid]


def _node_call(api, machine, verb, payload=None):
    """The one place this package calls a node verb (the browser smoke seed replaces it)."""
    return api.node(machine, verb, payload or {})


def safe_open(path):
    """A binary stream of a regular file reached without following any link or reparse point.

    POSIX: opened from "/" one component at a time with O_NOFOLLOW (so no ancestor can be a link) and checked to be a
    regular file by fstat. Windows: opened by handle without following reparse points; the handle's own final path
    must equal the configured path itself (normalized case: a junction or link anywhere in it makes them differ)
    and its attributes must be a plain file."""
    path = Path(path)
    if not path.is_absolute():
        raise OSError("absolute path required")
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        try:
            if not stat.S_ISREG(os.fstat(leaf).st_mode):
                raise OSError("not a regular file")
            os.set_blocking(leaf, True)
            return os.fdopen(leaf, "rb")
        except OSError:
            os.close(leaf)
            raise
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

        class Info(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
                        ("written", wintypes.FILETIME), ("serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
                        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                        ("index_low", wintypes.DWORD)]
        kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
        handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)   # no reparse following
        if handle in (None, wintypes.HANDLE(-1).value):
            raise OSError("cannot open the file")
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            if not kernel.GetFinalPathNameByHandleW(handle, buffer, 1024, 0):
                raise OSError("cannot read the final path")
            final = buffer.value[4:] if buffer.value.startswith("\\\\?\\") else buffer.value
            info = Info()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise OSError("cannot read the file information")
            attributes = info.attributes
            if os.path.normcase(final) != os.path.normcase(os.path.abspath(path)) or attributes & (0x400 | 0x10):
                raise OSError("not a plain file at the configured path")
            return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")
        except OSError:
            kernel.CloseHandle(handle)
            raise
    raise OSError("no safe way to open files on this platform")


# -- occupancy (in use) -------------------------------------------------------------------------------------------
def statuses(api):
    """orchestrate.status() for every saved launch ({} when none) and the error text (fresh: jobs and saves only)."""
    try:
        return api.orchestrate.status() or {}, None
    except Exception as exc:
        return {}, f"profile status unavailable: {exc}"


def live_leases(status_map, mid=None):
    """[(profile, machine, lease)] of every lease not in a final state (optionally on one machine)."""
    found = []
    for profile, status in (status_map or {}).items():
        for machine, node in ((status or {}).get("nodes") or {}).items():
            if mid is not None and machine != mid:
                continue
            for lease in (node or {}).get("leases", []) or []:
                if isinstance(lease, dict) and lease.get("state") not in FINAL_LEASE_STATES:
                    found.append((profile, machine, lease.get("lease") or lease.get("label") or "?"))
    return found


def profiles_using(cluster, mid):
    if cluster is None:
        return []
    names = []
    for name, settings in cluster.profiles.items():
        members = list(settings.get("nodes") or []) + [settings[k] for k in ("rank0", "rank1") if k in settings]
        service = settings.get("prefill_service")
        if mid in members or (isinstance(service, dict) and service.get("machine") == mid):
            names.append(name)
    return names


def core_job_running(api):
    with api._job_lock:
        active = api._active
        return api._jobs[active]["action"] if active and active in api._jobs else None


def bench_holds(api, resources):
    """Labels of benchmark holds overlapping these resources (shared admission database)."""
    try:
        holds = api._admission().holds()
    except Exception:
        return []
    resources = set(resources)
    return [h.get("label") or h["id"] for h in holds if h["kind"] == "bench" and not h.get("cancelled")
            and (resources & set(h["resources"]) or "*" in h["resources"] or "*" in resources)]


def _placed_on(placement, mid, cluster):
    """True when a router/service placement ("local" = the control machine) is this machine."""
    if placement == "local" and cluster is not None:
        return cluster.machines.get(mid) is not None and cluster.machines[mid].access == "local"
    return placement == mid


def snapshot_occupancy(api, mid):
    """Why a machine looks in use from the shared 2 s snapshot (GET documents), or None."""
    try:
        rows = api.snapshot()["profiles"].get("profiles") or []
        cluster = api.cluster()
    except Exception:
        return "profile state unavailable"
    for row in rows:
        on = mid in (row.get("nodes") or []) or _placed_on(row.get("router"), mid, cluster)
        if not on:
            continue
        if row.get("state") in RUNNING_PROFILE_STATES:
            return f"profile {row['id']} is {row['state']}"
        live = [lease for lease in row.get("leases") or [] if lease.get("machine") == mid
                and lease.get("state") not in FINAL_LEASE_STATES]
        if live:
            return f"lease {live[0].get('lease') or live[0].get('label') or '?'} of {row['id']} is live"
    if row_state_unknown(rows):
        return "profile state unavailable"
    return None


def row_state_unknown(rows):
    return any(row.get("state") is None and (row.get("why") or {}).get("state") for row in rows)


def occupancy(api, cluster, mid, inside_job=False):
    """(reason, unknown): the authoritative in-use check run right before acting.

    In use = a saved launch naming the machine (its nodes or its router), a lease not in a final state in the node's
    own records, or a running core job (not checked from inside an exclusive job, which holds that slot itself).
    unknown = some source could not be read (callers refuse on it)."""
    machine = cluster.machines.get(mid)
    job = None if inside_job else core_job_running(api)
    if job:
        return f"another job is running ({job})", False
    status_map, error = statuses(api)
    if error:
        return error, True
    for profile, status in status_map.items():
        if not isinstance(status, dict) or status.get("state") == "stopped" and not status.get("nodes"):
            continue
        if mid in (status.get("nodes") or {}):
            node = status["nodes"][mid] or {}
            if node.get("error"):
                return f"saved launch of {profile} on {mid}: node unreadable ({node['error']})", True
            return f"saved launch of profile {profile} uses {mid}", False
        try:
            saved = api.orchestrate._read(api.orchestrate._state_path(profile))
            if (_placed_on((saved.get("router") or {}).get("machine"), mid, cluster) or mid in (saved.get("machines") or {})
                    or (saved.get("prefill_service") and machine is not None and machine.access == "local")):
                return f"saved launch of profile {profile} uses {mid}", False
        except FileNotFoundError:
            pass
        except Exception as exc:
            return f"saved launch of {profile} unreadable: {exc}", True
    if machine is not None and machine.access not in ("ssh", "local"):
        return f"{mid} uses {machine.access} access: its lease records cannot be read", True
    if machine is not None and machine.access == "ssh":
        try:
            reply = _node_call(api, machine, "status", {"root": machine.install_root})
            if not isinstance(reply, dict) or reply.get("error"):
                raise ValueError((reply or {}).get("error") or "malformed reply")
            leases = reply.get("leases")
            if not isinstance(leases, list) or any(not isinstance(row, dict) or "state" not in row for row in leases):
                raise ValueError("the reply carries no lease list")
        except Exception as exc:
            return f"the node's lease records are unreadable ({str(exc)[:200]})", True
        live = [row for row in leases if row.get("state") not in FINAL_LEASE_STATES]
        if live:
            return f"lease {live[0].get('lease') or live[0].get('label') or '?'} is live on {mid}", False
    return None, False


def now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# -- F38 node tool ------------------------------------------------------------------------------------------------
_digest_cache = {"key": None, "value": None}


def expected_digest():
    from hyperloom import remote
    from hyperloom.paths import repo_root
    node = repo_root() / "node"
    key = []
    for name in remote.NODE_FILES:
        try:
            info = (node / name).stat()
            key.append((name, info.st_size, info.st_mtime_ns))
        except OSError:
            key.append((name, None, None))
    if _digest_cache["key"] != key:
        _digest_cache.update(key=key, value=remote.tool_digest())
    return _digest_cache["value"]


def _tool_of(reply):
    return (((reply or {}).get("checks") or {}).get("tool") or {}).get("value")


def macs_in(reply):
    """Unicast MACs of a `host` reply: only the agreed keys `interfaces[].mac` and `macs`."""
    found = []
    if not isinstance(reply, dict):
        return found
    values = list(reply.get("macs") or []) if isinstance(reply.get("macs"), list) else []
    interfaces = reply.get("interfaces")
    if isinstance(interfaces, dict) and interfaces.get("status") == "ok":
        interfaces = interfaces.get("value")
    for row in interfaces if isinstance(interfaces, list) else []:
        if isinstance(row, dict):
            values.append(row.get("mac"))
    for value in values[:64]:
        if isinstance(value, str) and MAC.fullmatch(value.lower()):
            mac = value.lower()
            if mac != "00:00:00:00:00:00" and not int(mac[:2], 16) & 1 and mac not in found:
                found.append(mac)
    return found


def sample_nodes(api, now):
    """Collector ops-nodes: one version call per ssh machine (and one host call for its MACs); no series."""
    store = store_for_write(api)
    cluster = api.cluster()
    known = dict(store.get(NAMESPACE, "node_tool", {}) or {})
    macs = dict(store.get(NAMESPACE, "macs", {}) or {})
    lock = threading.Lock()

    def one(machine):
        row = {"checked_at": now, "deployed": None, "error": None, "seen_at": (known.get(machine.id) or {}).get("seen_at")}
        try:
            reply = _node_call(api, machine, "version", {})
            if not isinstance(reply, dict) or reply.get("error"):
                raise ValueError((reply or {}).get("error") or "malformed reply")
            row.update(deployed=_tool_of(reply), seen_at=now)
        except Exception as exc:
            row["error"] = str(exc)[:300]
        found = []
        if row["error"] is None:
            try:
                found = macs_in(_node_call(api, machine, "host", {}))
            except Exception:
                pass
        with lock:
            known[machine.id] = row
            if found:
                macs[machine.id] = {"macs": found, "learned_at": now}
    threads = [threading.Thread(target=one, args=(m,), daemon=True) for m in cluster.machines.values() if m.access == "ssh"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    with lock:
        store.put(NAMESPACE, "node_tool", known)
        store.put(NAMESPACE, "macs", macs)
    return []


def update_blocker(api, cluster, profile, machine=None):
    """The blocker of a node tool update shown in documents (snapshot-based; the job checks again for real)."""
    job = core_job_running(api)
    if job:
        return "another job is running (" + job + ")"
    if profile not in cluster.profiles:
        return "unknown profile"
    members = api.orchestrate._profile(cluster, profile)[2]
    targets = [machine] if machine else list(members)
    for mid in targets:
        reason = snapshot_occupancy(api, mid)
        if reason:
            return f"{mid}: {reason}; stop it first"
    benches = bench_holds(api, targets)
    if benches:
        return "a benchmark holds these machines (" + benches[0] + ")"
    return None


def nodes_document(api, query):
    cluster = api.cluster()
    expected = expected_digest()
    known = pref(api, NAMESPACE, "node_tool", {}) or {}
    why = {}
    if store_for_read(api) is None:
        why["machines"] = "node tool checks are recorded by the portal's samplers (not running in this process)"
    rows = []
    for mid, machine in cluster.machines.items():
        row = {"machine": mid, "access": machine.access, "os": machine.os, "expected": expected, "deployed": None,
               "status": None, "checked_at": None, "profiles": profiles_using(cluster, mid), "actions": [], "why": {}}
        if machine.access != "ssh":
            row["status"] = "local" if machine.access == "local" else "not reachable by ssh"
            row["why"]["deployed"] = ("the control machine runs this checkout's node tool directly"
                                      if machine.access == "local" else "agent access is not supported")
            rows.append(row)
            continue
        entry = known.get(mid)
        if entry:
            row.update(deployed=entry.get("deployed"), checked_at=entry.get("checked_at"))
            if entry.get("error"):
                row["status"] = "unreachable"
                row["why"]["deployed"] = entry["error"]
            else:
                row["status"] = "current" if entry.get("deployed") == expected else "stale"
        else:
            row["why"]["deployed"] = "not checked yet (the check runs every minute)"
        for profile in row["profiles"]:
            row["actions"].append({"profile": profile, "machine": mid, "blocked": update_blocker(api, cluster, profile, mid)})
        if not row["profiles"]:
            row["why"]["actions"] = f"not part of any profile: install the node tool with 'hyperloom doctor --machine {mid}'"
        rows.append(row)
    return api.document("ops-nodes", machines=rows, expected=expected, why=why,
                        rule="the update runs the profile's node tool update without force, machine by machine, and "
                             "is refused while a lease is live or the state cannot be read",
                        limitation="an update already running on one machine cannot be interrupted: the node tool "
                                   "update checks the leases and then copies the tool without a cancellation point, so "
                                   "a cancellation that arrives during its lease check can still be followed by the "
                                   "copy; the job keeps its admission until that call returns, and a machine is only "
                                   "started with at least 180 s left (a start threshold, not a completion bound)")


def validate_nodes_update(api, arguments):
    strict(arguments, ("profile", "machine"), ("profile",))
    cluster = api.cluster()
    profile, machine = arguments["profile"], arguments.get("machine")
    if not isinstance(profile, str) or profile not in cluster.profiles:
        raise LookupError("unknown profile " + str(profile)[:64])
    members = api.orchestrate._profile(cluster, profile)[2]
    if machine is not None and machine not in members:
        raise ValueError("machine is not part of the profile")
    return {"profile": profile, **({"machine": machine} if machine else {})}


def update_resources(api, arguments):
    cluster = api.cluster()
    members = api.orchestrate._profile(cluster, arguments["profile"])[2]
    return {arguments["machine"]} if arguments.get("machine") else {str(m) for m in members}


def nodes_update(api, arguments, ctx=None, deadline=None):
    """The profile-scoped node tool update (never forced), one machine at a time; the same function the CLI runs.

    Before each machine: cancellation and the remaining deadline are checked and the machine's occupancy is read
    fresh (saved launches, the node's own lease records); any live lease or unknown state refuses."""
    arguments = validate_nodes_update(api, arguments)
    cluster = api.cluster()
    members = api.orchestrate._profile(cluster, arguments["profile"])[2]
    targets = [arguments["machine"]] if arguments.get("machine") else [str(m) for m in members]
    deadline = deadline if deadline is not None else (ctx.deadline if ctx is not None else time.monotonic() + 600)
    done, skipped = {}, []
    for mid in targets:
        if ctx is not None and ctx.cancelled():
            skipped.extend(t for t in targets if t not in done)
            break
        if deadline - time.monotonic() < UPDATE_MIN_LEFT_S:
            skipped.extend(t for t in targets if t not in done)
            break
        reason, unknown = occupancy(api, cluster, mid, inside_job=ctx is not None)
        if reason:
            if done:
                return {"updated": done, "refused": {mid: reason}, "skipped": [t for t in targets if t not in done and t != mid]}
            raise PermissionError(f"{mid}: {reason}" + ("; nothing was changed" if unknown else "; stop it first"))
        if ctx is not None:
            ctx(f"updating the node tool on {mid}")
        result = api.orchestrate.nodes_update(cluster, arguments["profile"], machine=mid, force=False)
        done.update(result or {})
        _remember_update(api, result)
    out = {"updated": done, "skipped": skipped,
           "note": "machines were updated one at a time; an update in progress on one machine cannot be interrupted "
                   "(a cancellation during its lease check can still be followed by the copy), and a machine is only "
                   "started with at least 180 s left, which is a start threshold, not a completion bound"}
    if ctx is not None and ctx.cancelled() and skipped:
        return None
    return out


def _remember_update(api, result):
    try:
        store = store_for_write(api)
        known = dict(store.get(NAMESPACE, "node_tool", {}) or {})
        now = api.clock()
        for mid, row in (result or {}).items():
            known[mid] = {"checked_at": now, "seen_at": now, "deployed": (row or {}).get("tool"), "error": None}
        store.put(NAMESPACE, "node_tool", known)
    except Exception:
        pass


# -- F39 power ----------------------------------------------------------------------------------------------------
def magic_packet(mac):
    if not isinstance(mac, str) or not MAC.fullmatch(mac.lower()):
        raise ValueError("invalid MAC address (six hex pairs separated by ':')")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def extras_of(api, mid, store=None):
    return dict((pref(api, NAMESPACE, "machines", {}, store=store) or {}).get(mid) or {})


def wake_macs(api, machine):
    extras = extras_of(api, machine.id)
    if extras.get("mac"):
        return [extras["mac"]], "configured"
    learned = ((pref(api, NAMESPACE, "macs", {}) or {}).get(machine.id) or {}).get("macs") or []
    return list(learned), ("learned from the node tool" if learned else None)


def last_seen(api, mid):
    return ((pref(api, NAMESPACE, "node_tool", {}) or {}).get(mid) or {}).get("seen_at")


def wake_blocker(api, machine):
    if machine.access == "local":
        return "this is the control machine"
    macs, _ = wake_macs(api, machine)
    if not macs:
        return "no MAC address known (set one in Machines; learning it needs the node tool's host report)"
    seen = last_seen(api, machine.id)
    if seen is not None and api.clock() - seen < ONLINE_WINDOW_S:
        return "the machine answered in the last 2 minutes"
    return None


def send_magic(macs, targets, port=9, sock_factory=None):
    sent = []
    sock = (sock_factory or socket.socket)(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for target in targets:
                sock.sendto(packet, (target, port))
                sent.append({"mac": mac, "to": f"{target}:{port}"})
    finally:
        sock.close()
    return sent


def wake(api, arguments):
    strict(arguments, ("machine",), ("machine",))
    machine = machine_of(api, arguments["machine"])
    blocker = wake_blocker(api, machine)
    if blocker:
        raise PermissionError(blocker)
    macs, source = wake_macs(api, machine)
    targets = ["255.255.255.255"]
    broadcast = extras_of(api, machine.id).get("wol_broadcast")
    if broadcast:
        targets.append(broadcast)
    sent = send_magic(macs, targets)
    return {"machine": machine.id, "mac_source": source, "sent": sent,
            "note": "magic packet sent by UDP broadcast; the machine shows online once its node tool answers"}


def shutdown_helper(api, store=None):
    value = pref(api, NAMESPACE, "shutdown_helper", None, store=store)
    if not value:
        return None, "no shutdown helper configured (hyperloom ops settings --shutdown-helper PATH)"
    if not Path(value).is_file():
        return None, "the configured shutdown helper is not a file on this machine"
    return value, None


def shutdown_blocker(api, machine):
    """The blocker shown in documents (snapshot-based; the job re-checks authoritatively before acting)."""
    _, why = shutdown_helper(api)
    if why:
        return why
    if machine.access == "local":
        return "this machine runs the portal"
    if machine.access != "ssh":
        return f"{machine.access} access: the machine's lease state cannot be read"
    reason = snapshot_occupancy(api, machine.id)
    if reason:
        return reason + "; stop it first"
    job = core_job_running(api)
    if job:
        return "another job is running (" + job + ")"
    benches = bench_holds(api, {machine.id})
    if benches:
        return "a benchmark holds this machine (" + benches[0] + ")"
    return None


def validate_shutdown(api, arguments):
    strict(arguments, ("machine", "confirm"), ("machine", "confirm"))
    machine = machine_of(api, arguments["machine"])
    if arguments["confirm"] != machine.id:
        raise ValueError("type the machine id to confirm")
    if machine.access == "local":
        raise PermissionError("this machine runs the portal")
    if machine.access != "ssh":
        raise PermissionError(f"{machine.access} access: the machine's lease state cannot be read")
    _, why = shutdown_helper(api)
    if why:
        raise PermissionError(why)
    return {"machine": machine.id, "confirm": machine.id}


def _bounded_run(argv, timeout, cancelled=lambda: False):
    """Run argv without a shell; stdout/stderr drained into bounded buffers; killed at the timeout or on cancel."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window())
    buffers = [bytearray(), bytearray()]

    def drain(stream, buffer):
        try:
            while chunk := stream.read(4096):
                if len(buffer) < HELPER_OUTPUT:
                    buffer.extend(chunk[:HELPER_OUTPUT - len(buffer)])
        except (OSError, ValueError):
            pass
    readers = [threading.Thread(target=drain, args=(child.stdout, buffers[0]), daemon=True),
               threading.Thread(target=drain, args=(child.stderr, buffers[1]), daemon=True)]
    for reader in readers:
        reader.start()
    end = time.monotonic() + timeout
    stopped = None
    while child.poll() is None:
        if time.monotonic() >= end:
            stopped = "timed out"
        elif cancelled():
            stopped = "cancelled"
        if stopped:
            child.kill()
            break
        time.sleep(0.05)
    child.wait()
    for reader in readers:
        reader.join(2)
    for stream in (child.stdout, child.stderr):
        stream.close()
    return child.returncode, bytes(buffers[0]), bytes(buffers[1]), stopped


def shutdown(api, arguments, ctx=None, runner=None):
    arguments = validate_shutdown(api, arguments)
    cluster = api.cluster()
    helper, why = shutdown_helper(api)
    if why:
        raise PermissionError(why)
    reason, unknown = occupancy(api, cluster, arguments["machine"], inside_job=ctx is not None)
    if reason:
        raise PermissionError(reason + ("; nothing was changed" if unknown else "; stop it first"))
    if ctx is not None:
        if ctx.cancelled():
            return None
        ctx("running the shutdown helper for " + arguments["machine"])
    from hyperloom.web.api import redact
    code, out, err, stopped = (runner or _bounded_run)([helper, arguments["machine"]], 30,
                                     (lambda: ctx.cancelled()) if ctx is not None else (lambda: False))
    result = {"machine": arguments["machine"], "exit_code": code, "stopped": stopped,
              "stdout": redact(out.decode("utf-8", "replace")[-2000:]),
              "stderr": redact(err.decode("utf-8", "replace")[-2000:])}
    if stopped:
        raise RuntimeError(f"the shutdown helper was {stopped}")
    if code:
        raise RuntimeError(f"the shutdown helper exited with {code}: {result['stderr'][-300:]}")
    return result


def power_document(api, query):
    cluster = api.cluster()
    try:
        profile_rows = {row["id"]: row for row in api.snapshot()["profiles"].get("profiles") or []}
    except Exception:
        profile_rows = {}
    helper, helper_why = shutdown_helper(api)
    rows = []
    for mid, machine in cluster.machines.items():
        macs, source = wake_macs(api, machine)
        profiles = [{"profile": p, "state": (profile_rows.get(p) or {}).get("state")} for p in profiles_using(cluster, mid)]
        rows.append({"machine": mid, "access": machine.access, "os": machine.os, "profiles": profiles,
                     "stop": [p for p in profiles if p["state"] in RUNNING_PROFILE_STATES],
                     "wake": {"macs": macs, "source": source, "blocked": wake_blocker(api, machine)},
                     "shutdown": {"blocked": shutdown_blocker(api, machine)}, "last_seen": last_seen(api, mid)})
    return api.document("ops-power", machines=rows, shutdown_helper=helper,
                        why={"shutdown_helper": helper_why} if helper_why else {},
                        rule="powering a machine off asks to type its id; engines stop through the profile that owns them")


# -- F41 configuration --------------------------------------------------------------------------------------------
def _read_config(api):
    path = Path(api.config_path)
    data = retry_sharing(path.read_bytes)
    if len(data) > config_module.MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds 256 KiB")
    raw = config_module.read_json(path)
    return raw, hashlib.sha256(data).hexdigest()


def _device_identity(device):
    return {k: device.get(k) for k in ("id", "uuid")} if isinstance(device, dict) else device


def apply_edit(raw, edit):
    """New raw configuration with one machine edit applied (raises ValueError/LookupError)."""
    op = edit.get("op")
    machines = list(raw.get("machines") or [])
    ids = [row.get("id") for row in machines]
    new = dict(raw)
    if op == "add":
        machine = edit.get("machine")
        if not isinstance(machine, dict):
            raise ValueError("machine must be an object")
        if machine.get("id") in ids:
            raise ValueError("a machine with this id exists")
        index = edit.get("index", len(machines))
        if type(index) is not int or not 0 <= index <= len(machines):
            raise ValueError("index out of range")
        machines.insert(index, machine)
    elif op in ("edit", "remove", "move"):
        mid = edit.get("id")
        if mid not in ids:
            raise LookupError("unknown machine " + str(mid)[:64])
        position = ids.index(mid)
        if op == "edit":
            machine = edit.get("machine")
            if not isinstance(machine, dict):
                raise ValueError("machine must be an object")
            if machine.get("id") != mid and machine.get("id") in ids:
                raise ValueError("a machine with this id exists")
            machines[position] = machine
        elif op == "remove":
            machines.pop(position)
        else:
            index = edit.get("index")
            if type(index) is not int or not 0 <= index < len(machines):
                raise ValueError("index out of range")
            machines.insert(index, machines.pop(position))
    else:
        raise ValueError("op must be add, edit, remove or move")
    new["machines"] = machines
    return new


def frozen_violation(old_row, new_row, op):
    """What an edit changes of a machine's identity (None when it keeps it)."""
    if op == "remove":
        return "it cannot be removed"
    if op == "move":
        return None
    changed = [key for key in FROZEN_FIELDS if old_row.get(key) != new_row.get(key)]
    if [_device_identity(d) for d in old_row.get("devices", [])] != [_device_identity(d) for d in new_row.get("devices", [])]:
        changed.append("devices (ids/uuids)")
    return ("these fields are frozen: " + ", ".join(changed)) if changed else None


EDIT_KEYS = ("op", "id", "machine", "index")


def _edit(arguments, extra=()):
    strict(arguments, (*EDIT_KEYS, *extra), ("op",))
    if "machine" in arguments and len(json.dumps(arguments["machine"], ensure_ascii=True)) > 16384:
        raise ValueError("machine entry too large")
    return arguments


def check_edit(api, arguments, raw=None, fresh=True):
    """(new_raw, cluster) after the frozen-identity rule and the full schema. fresh=False (validation only) uses the
    snapshot's occupancy; the save uses the authoritative check and refuses on unknown state."""
    if raw is None:
        raw, _ = _read_config(api)
    new_raw = apply_edit(raw, arguments)
    op = arguments["op"]
    if op in ("edit", "remove"):
        mid = arguments["id"]
        old_row = next(row for row in raw["machines"] if row.get("id") == mid)
        violation = frozen_violation(old_row, arguments.get("machine") or {}, op)
        if violation:
            try:
                current = config_module.parse(raw)
            except config_module.ConfigError as exc:
                raise PermissionError(f"the current configuration does not validate ({exc}); identity changes and "
                                      "removals are refused until it does (edit other fields or the file by hand)") from None
            if fresh:
                reason, unknown = occupancy(api, current, mid)
            else:
                reason, unknown = snapshot_occupancy(api, mid), False
            if reason:
                hint = (" (an unreadable state keeps the identity frozen; edit the configuration file by hand if the "
                        "machine is known to be idle)") if unknown else ""
                raise PermissionError(f"{mid} is in use or its state cannot be read ({reason}): {violation}{hint}")
    try:
        cluster = config_module.parse(new_raw)
    except config_module.ConfigError as exc:
        raise ValueError("invalid configuration: " + str(exc)) from None
    if len((json.dumps(new_raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")) > config_module.MAX_CONFIG_BYTES:
        raise ValueError("the configuration would exceed 256 KiB")
    return new_raw, cluster


def validate_config(api, arguments):
    _edit(arguments, ("version",))
    try:
        check_edit(api, arguments, fresh=False)
    except (ValueError, PermissionError, LookupError) as exc:
        return {"valid": False, "error": str(exc).strip("'\"")}
    return {"valid": True, "error": None}


_write_lock = threading.Lock()


def write_config(path, raw):
    """Atomic replace: a unique temporary file in the same directory (O_EXCL, the current file's permissions,
    fsync), the previous file kept as <name>.prev, os.replace retried on sharing violations, the temporary removed
    on failure. Returns the new version (sha256 of the bytes)."""
    path = Path(path)
    data = (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > config_module.MAX_CONFIG_BYTES:
        raise ValueError("configuration would exceed 256 KiB")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            previous = path.with_name(path.name + ".prev")
            content = retry_sharing(path.read_bytes)
            retry_sharing(lambda: previous.write_bytes(content))
        retry_sharing(lambda: os.replace(temporary, path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(data).hexdigest()


class ConfigLock:
    """Cross-process lock around a configuration edit (re-read .. replace), retried up to 5 s."""

    def __init__(self, config_path, wait_s=5.0):
        from hyperloom import procs
        self.lock = procs.InstanceLock(Path(config_path).with_name(Path(config_path).name + ".lock"))
        self.wait_s = wait_s

    def __enter__(self):
        from hyperloom import procs
        end = time.monotonic() + self.wait_s
        while True:
            try:
                self.lock.acquire()
                return self
            except procs.ProcessError:
                if time.monotonic() >= end:
                    raise PermissionError("another process is editing the configuration; retry") from None
                time.sleep(0.1)

    def __exit__(self, *exc):
        self.lock.close()


def save_config(api, arguments):
    """POST ops/config/save: the page must send the version it reviewed."""
    _edit(arguments, ("version",))
    if not isinstance(arguments.get("version"), str):
        raise ValueError("version is required: reload the configuration and review it")
    return save_config_any(api, arguments)


def save_config_any(api, arguments):
    """The save itself (the command line may omit the version: it re-reads and applies under the lock)."""
    _edit(arguments, ("version",))
    job = core_job_running(api)
    if job:
        raise PermissionError("another job is running (" + job + ")")
    target = arguments.get("id") or (arguments.get("machine") or {}).get("id") or "*"
    hold = None
    try:
        hold = api._admission().acquire("core", {str(target)}, label="config save " + str(target)[:64])
    except PermissionError as exc:
        raise PermissionError(str(exc)) from None
    try:
        with _write_lock, ConfigLock(api.config_path):
            raw, version = _read_config(api)
            if "version" in arguments and arguments["version"] != version:
                raise PermissionError("the configuration changed since it was read; reload and review")
            new_raw, cluster = check_edit(api, arguments, raw, fresh=True)
            new_version = write_config(api.config_path, new_raw)
    finally:
        if hold:
            try:
                api._admission().release(hold)
            except Exception:
                pass
    return {"saved": True, "op": arguments["op"], "version": new_version, "machines": list(cluster.machines),
            "note": "the portal reads the configuration on every snapshot; no restart is needed"}


def _pinned(api, cluster):
    from hyperloom import remote
    result = {}
    if cluster is None:
        return result
    try:
        lines = Path(api.orchestrate._known_hosts()).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    tokens = set()
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and not line.startswith("#"):
            tokens.update(parts[0].split(","))
    for mid, machine in cluster.machines.items():
        if machine.access == "ssh":
            alias = mid if machine.ssh_port == 22 else f"[{mid}]:{machine.ssh_port}"
            result[mid] = alias in tokens
    return result


def config_document(api, query):
    try:
        raw, version = _read_config(api)
    except (OSError, ValueError) as exc:
        return api.document("ops-config", machines=None, version=None, why={"machines": "configuration unreadable: " + str(exc)})
    try:
        cluster = config_module.parse(raw)
        error = None
    except config_module.ConfigError as exc:
        cluster, error = None, str(exc)
    extras = pref(api, NAMESPACE, "machines", {}) or {}
    pinned = _pinned(api, cluster)
    known = pref(api, NAMESPACE, "node_tool", {}) or {}
    rows = []
    for index, row in enumerate(raw.get("machines") or []):
        mid = row.get("id")
        entry = known.get(mid) or {}
        item = {"index": index, "id": mid, "entry": row, "extras": extras.get(mid) or {}, "frozen": None,
                "profiles": profiles_using(cluster, mid), "assets": sorted((raw.get("assets") or {}).get(mid, {})),
                "router": (raw.get("router") or {}).get("machine") == mid, "pinned": pinned.get(mid),
                "reachable": None if not entry else entry.get("error") is None}
        if cluster is not None and mid in cluster.machines:
            item["frozen"] = snapshot_occupancy(api, mid)
            if item["frozen"] is None and cluster.machines[mid].access == "ssh":
                if not entry:
                    item["frozen"] = "lease state not checked yet (identity edits wait for the node's first answer)"
                elif entry.get("error"):
                    item["frozen"] = "lease state unknown: the node did not answer its last check"
        elif cluster is None:
            item["frozen"] = "the configuration does not validate: identity changes wait until it does"
        rows.append(item)
    why = {"configuration": "the configuration does not validate: " + error} if error else {}
    return api.document("ops-config", machines=rows, version=version, path=str(api.config_path), why=why,
                        frozen_fields=[*FROZEN_FIELDS, "devices (ids/uuids)"],
                        rule="a machine used by a saved launch, a live lease or a running job keeps its identity "
                             "(checked again when saving; an unreadable state refuses); edits are validated, "
                             "version-checked and written atomically")


def machine_extras(api, arguments):
    strict(arguments, ("machine", "mac", "wol_broadcast", "hide_disks", "hide_interfaces"), ("machine",))
    machine_of(api, arguments["machine"])
    store = store_for_write(api)
    current = extras_of(api, arguments["machine"], store=store)
    if "mac" in arguments:
        if arguments["mac"] in (None, ""):
            current.pop("mac", None)
        else:
            magic_packet(arguments["mac"])
            current["mac"] = arguments["mac"].lower()
    if "wol_broadcast" in arguments:
        value = arguments["wol_broadcast"]
        if value in (None, ""):
            current.pop("wol_broadcast", None)
        else:
            import ipaddress
            try:
                ip = ipaddress.IPv4Address(value)
            except (ValueError, TypeError):
                raise ValueError("wol_broadcast must be an IPv4 address") from None
            if not ip.is_private:
                raise ValueError("wol_broadcast must be a private LAN broadcast address")
            current["wol_broadcast"] = str(ip)
    for key in ("hide_disks", "hide_interfaces"):
        if key in arguments:
            value = arguments[key]
            if (not isinstance(value, list) or len(value) > 32
                    or any(not isinstance(v, str) or not v or len(v) > 128 or any(ord(c) < 32 for c in v) for v in value)):
                raise ValueError(key + " must be a list of at most 32 names")
            current[key] = sorted(set(value))
    every = dict(pref(api, NAMESPACE, "machines", {}, store=store) or {})
    every[arguments["machine"]] = current
    set_pref(api, NAMESPACE, "machines", every)
    return {"machine": arguments["machine"], "extras": current}


def scan_hostkey(api, arguments):
    strict(arguments, ("machine",), ("machine",))
    machine = machine_of(api, arguments["machine"])
    if machine.access != "ssh":
        raise ValueError("only ssh machines have a host key to pin")
    keys = api.remote.keyscan(machine.http_address, machine.ssh_port)
    return {"machine": machine.id, "address": machine.http_address,
            "candidates": [{"algorithm": k.algorithm, "fingerprint": k.sha256_fingerprint} for k in keys],
            "next": "compare one fingerprint with the machine itself (ssh-keygen -lf on the machine), then pin it"}


def _step(name, status, detail):
    return {"step": name, "status": status, "detail": str(detail)[:500]}


def machine_test(api, arguments, ctx=None):
    """ssh, node tool, GPU, engines, USB4 as pass/fail/skipped; never bootstraps or changes anything."""
    strict(arguments, ("machine",), ("machine",))
    cluster = api.cluster()
    machine = machine_of(api, arguments["machine"], cluster)
    steps = []
    cancelled = (lambda: ctx.cancelled()) if ctx is not None else (lambda: False)
    report = ctx if ctx is not None else (lambda text: None)
    reachable = True
    if machine.access == "ssh" and not _pinned(api, cluster).get(machine.id):
        steps.append(_step("ssh", "skipped", "host key not pinned: scan it, compare the fingerprint on the machine, pin it"))
        reachable = False
    elif machine.access != "ssh":
        steps.append(_step("ssh", "skipped", "local machine"))
    version = None
    if reachable:
        report("calling the node tool")
        try:
            version = _node_call(api, machine, "version", {})
            if not isinstance(version, dict) or version.get("error"):
                raise ValueError((version or {}).get("error") or "malformed reply")
            if machine.access == "ssh":
                steps.append(_step("ssh", "pass", "the node tool answered over the pinned connection"))
        except Exception as exc:
            reachable = False
            if machine.access == "ssh":
                steps.append(_step("ssh", "fail", exc))
                steps.append(_step("node tool", "skipped", "ssh failed"))
            else:
                steps.append(_step("node tool", "fail", exc))
    if reachable and version is not None:
        deployed, expected = _tool_of(version), expected_digest()
        steps.append(_step("node tool", "pass" if deployed == expected else "fail", "current" if deployed == expected else
                           f"stale: deployed {str(deployed)[:12]}, this checkout {expected[:12]} (update it from the node tool tab)"))
    if not reachable:
        steps.extend(_step(name, "skipped", "the node tool is not reachable") for name in ("GPU", "USB4"))
    elif machine.os != "linux":
        steps.append(_step("GPU", "skipped", "the control machine's GPUs are sampled by the fleet package"))
        steps.append(_step("USB4", "skipped", "USB4 streams live on the Linux nodes"))
    else:
        if not cancelled():
            report("reading GPUs")
            try:
                amd = ((_node_call(api, machine, "gpu", {}).get("checks") or {}).get("amd") or {})
                cards = amd.get("value") if amd.get("status") == "ok" else None
                steps.append(_step("GPU", "pass" if cards else "fail",
                                   f"{len(cards)} AMD GPU(s)" if cards else amd.get("why") or "no AMD GPU reported"))
            except Exception as exc:
                steps.append(_step("GPU", "fail", exc))
        if not cancelled():
            report("reading USB4")
            try:
                usb = ((_node_call(api, machine, "doctor", {}).get("checks") or {}).get("usb4") or {})
                streams = (usb.get("streams") or {}).get("value") or {}
                if not usb:
                    steps.append(_step("USB4", "skipped", "not reported by this node tool"))
                elif (usb.get("module") or {}).get("status") != "ok":
                    steps.append(_step("USB4", "skipped", "USB4 stream driver not loaded"))
                else:
                    count = len(streams.get("openable") or [])
                    steps.append(_step("USB4", "pass" if count else "fail",
                                       f"{count} of {streams.get('count', '?')} streams openable (published layout)"))
            except Exception as exc:
                steps.append(_step("USB4", "fail", exc))
    if cancelled():
        return None
    report("reading engines")
    using = profiles_using(cluster, machine.id)
    status_map, error = statuses(api)
    if error:
        steps.append(_step("engines", "fail", error))
    elif not using:
        steps.append(_step("engines", "skipped", "no profile uses this machine"))
    else:
        leases = live_leases(status_map, machine.id)
        steps.append(_step("engines", "pass", (f"{len(leases)} live lease(s): " + ", ".join(f"{p}/{l}" for p, _, l in leases))
                           if leases else "profiles " + ", ".join(using) + ": nothing running"))
    return {"machine": machine.id, "steps": steps, "passed": all(step["status"] != "fail" for step in steps)}


# -- F42 collectors -----------------------------------------------------------------------------------------------
def _registered():
    from hyperloom.web import features
    return features.registry().collectors


def bounds(default):
    return max(1.0, default / 2), min(3600.0, max(60.0, default * 30))


def check_collector(name):
    if not isinstance(name, str) or name not in _registered():
        raise LookupError("unknown collector " + str(name)[:64])
    return name


def check_seconds(name, seconds):
    low, high = bounds(_registered()[name][1])
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not low <= seconds <= high:
        raise ValueError(f"seconds must be a number in {low:g}..{high:g} for {name}")
    return float(seconds)


def collectors_document(api, query):
    registered = _registered()
    status = telemetry.status(api)
    rows = []
    for name, (_, default) in sorted(registered.items()):
        low, high = bounds(default)
        state = (status or {}).get(name)
        rows.append({"collector": name, "default_s": default, "min_s": low, "max_s": high,
                     "interval_s": state.get("interval_s") if state else None, "state": state,
                     "blocked": None if status is not None else "samplers are not running in this portal process"})
    why = {} if status is not None else {"state": "samplers are not running in this portal process"}
    return api.document("ops-collectors", collectors=rows, why=why, persisted=pref(api, NAMESPACE, "intervals", {}) or {},
                        rule="refresh now joins a collection already running; intervals stay within each collector's bounds")


def refresh(api, arguments):
    strict(arguments, ("collector",), ("collector",))
    name = check_collector(arguments["collector"])
    sampler = getattr(api, "sampler", None)
    if sampler is None:
        raise PermissionError("samplers are not running in this portal process")
    since = sampler.clock()
    woke = sampler.refresh(name)
    return {"collector": name, "state": "queued" if woke else "joined", "requested_at": since,
            "note": "the collector was woken" if woke else
                    "a collection was running; it is confirmed by the next collection that starts after this request"}


def interval(api, arguments):
    strict(arguments, ("collector", "seconds"), ("collector", "seconds"))
    name = check_collector(arguments["collector"])
    seconds = check_seconds(name, arguments["seconds"])
    store = store_for_write(api)
    saved = dict(store.get(NAMESPACE, "intervals", {}) or {})
    saved[name] = seconds
    store.put(NAMESPACE, "intervals", saved)
    applied = False
    sampler = getattr(api, "sampler", None)
    if sampler is not None:
        sampler.set_interval(name, seconds)
        _applied.setdefault(id(sampler), {})[name] = seconds
        applied = True
    return {"collector": name, "seconds": seconds, "applied": applied, "persisted": True}


_applied = {}       # id(sampler) -> {collector: seconds last applied from the persisted intervals}


def _requests(store):
    store.ensure_table(REQUESTS_TABLE)
    return store


def queue_request(api, kind, arguments):
    """CLI: record a refresh or interval request for the running portal (validated here); returns its id."""
    if kind == "refresh":
        strict(arguments, ("collector",), ("collector",))
        name, seconds = check_collector(arguments["collector"]), None
    elif kind == "interval":
        strict(arguments, ("collector", "seconds"), ("collector", "seconds"))
        name = check_collector(arguments["collector"])
        seconds = check_seconds(name, arguments["seconds"])
    else:
        raise ValueError("unknown request kind")
    store = _requests(store_for_write(api))
    rid = uuid.uuid4().hex
    now = api.clock()
    store.transaction([("DELETE FROM ops_requests WHERE state IN ('done', 'failed') AND finished < ?", (now - REQUEST_KEEP_S,)),
                       ("INSERT INTO ops_requests(id, kind, collector, seconds, created, state) VALUES (?, ?, ?, ?, ?, 'pending')",
                        (rid, kind, name, seconds, now))])
    if kind == "interval":
        saved = dict(store.get(NAMESPACE, "intervals", {}) or {})
        saved[name] = seconds
        store.put(NAMESPACE, "intervals", saved)
    return rid


def request_state(api, rid):
    rows = _requests(store_for_write(api)).execute(
        "SELECT kind, collector, seconds, created, state, claimed, finished, result FROM ops_requests WHERE id=?", (rid,))
    if not rows:
        return None
    kind, collector, seconds, created, state, claimed, finished, result = rows[0]
    return {"id": rid, "kind": kind, "collector": collector, "seconds": seconds, "created": created, "state": state,
            "claimed": claimed, "finished": finished, "result": json.loads(result) if result else None}


def _finish(store, rid, owner, state, result, now):
    store.execute("UPDATE ops_requests SET state=?, finished=?, result=? WHERE id=? AND owner=? AND state='claimed'",
                  (state, now, json.dumps(result, ensure_ascii=True), rid, owner))


def sample_control(api, now):
    """Collector ops (2 s): applies changed persisted intervals and the CLI's queued requests; records nothing."""
    sampler = getattr(api, "sampler", None)
    if sampler is None:
        return []
    store = store_for_write(api)
    registered = _registered()
    applied = _applied.setdefault(id(sampler), {})
    for name, seconds in (store.get(NAMESPACE, "intervals", {}) or {}).items():
        if name in sampler.collectors and name in registered and applied.get(name) != seconds:
            low, high = bounds(registered[name][1])
            if isinstance(seconds, (int, float)) and math.isfinite(seconds) and low <= seconds <= high:
                if sampler.collectors[name][1] != seconds:
                    sampler.set_interval(name, seconds)
                applied[name] = seconds
    _requests(store)
    clock = sampler.clock
    # Claimant liveness: a request claimed but not yet handed to the sampler (state 'claimed') for 60 s goes back to
    # pending under a new owner; the owner token keeps the expired claimant from writing over the retry. Once handed
    # over (queued/joined) a request keeps its target time and completes by the objective rule below, checked by any
    # portal process, until its retention ends.
    store.execute("UPDATE ops_requests SET state='pending', owner=NULL, retries=retries+1 WHERE state='claimed' AND "
                  "claimed < ?", (now - CLAIM_TIMEOUT_S,))
    owner = uuid.uuid4().hex
    # Untouched requests are claimable for 10 minutes; a recovered request that already carries a target stays
    # claimable for its own retention (enforced below).
    claimed = store.execute("UPDATE ops_requests SET state='claimed', claimed=?, owner=? WHERE state='pending' AND "
                            "(result IS NOT NULL OR created >= ?) RETURNING id, kind, collector, seconds, result",
                            (now, owner, now - REQUEST_KEEP_S))
    for rid, kind, name, seconds, previous in claimed:
        if name not in sampler.collectors:
            _finish(store, rid, owner, "failed", {"error": "unknown collector in this portal"}, now)
        elif kind == "interval":
            sampler.set_interval(name, seconds)
            applied[name] = seconds
            _finish(store, rid, owner, "done", {"applied": seconds}, now)
        else:
            # The target is recorded BEFORE the collector is woken, and a recovered request keeps its first target
            # (it may wake the collector again, never re-target).
            detail = json.loads(previous) if previous else {}
            if detail.get("requested_at") is None:
                detail = {"requested_at": clock(), "keep_s": max(REQUEST_KEEP_S, 2 * sampler.collectors[name][1] + 60)}
                store.execute("UPDATE ops_requests SET result=? WHERE id=? AND owner=? AND state='claimed'",
                              (json.dumps(detail), rid, owner))
            woke = sampler.refresh(name)
            store.execute("UPDATE ops_requests SET state=? WHERE id=? AND owner=? AND state='claimed'",
                          ("queued" if woke else "joined", rid, owner))
    # A refresh is done once a collection that STARTED after it was requested has finished (the sampler stamps
    # last_ok/last_error with a run's start time on its own clock); a run that began earlier, or at the same clock
    # reading, never satisfies it.
    state = sampler.snapshot()
    for rid, name, result, created in store.execute(
            "SELECT id, collector, result, created FROM ops_requests WHERE state IN ('pending', 'claimed', 'queued', "
            "'joined') AND kind='refresh' AND result IS NOT NULL"):
        info = state.get(name) or {}
        detail = json.loads(result) if result else {}
        since = detail.get("requested_at")
        ok, failed = info.get("last_ok") or 0, info.get("last_error") or 0
        if since is not None and max(ok, failed) > since:
            outcome = {**detail, "ok": ok >= failed, "error": None if ok >= failed else info.get("error")}
            store.execute("UPDATE ops_requests SET state=?, finished=?, result=? WHERE id=? AND state IN "
                          "('pending', 'claimed', 'queued', 'joined')",
                          ("done" if ok >= failed else "failed", now, json.dumps(outcome), rid))
        elif now - created > detail.get("keep_s", REQUEST_KEEP_S):
            store.execute("UPDATE ops_requests SET state='failed', finished=?, result=? WHERE id=? AND state IN "
                          "('pending', 'claimed', 'queued', 'joined')",
                          (now, json.dumps({**detail, "error": "no collection finished within the request's retention"}), rid))
    store.execute("DELETE FROM ops_requests WHERE state IN ('done', 'failed') AND finished < ?", (now - REQUEST_KEEP_S,))
    store.execute("UPDATE ops_requests SET state='failed', finished=?, result=? WHERE state='pending' AND result IS NULL "
                  "AND created < ?", (now, json.dumps({"error": "no running portal picked it up within 10 minutes"}),
                                      now - REQUEST_KEEP_S))
    return []


def settings(api, arguments):
    """CLI only: the shutdown helper path (an executable on the control machine)."""
    strict(arguments, ("shutdown_helper",))
    if "shutdown_helper" in arguments:
        value = arguments["shutdown_helper"]
        if value is not None:
            check_path(value, "shutdown helper", windows=os.name == "nt")
            if not Path(value).is_file():
                raise ValueError("the shutdown helper must be an existing file")
        set_pref(api, NAMESPACE, "shutdown_helper", value)
    return {"shutdown_helper": pref(api, NAMESPACE, "shutdown_helper", None, store=store_for_write(api))}


def register(reg):
    reg.get("ops/nodes", nodes_document)
    reg.get("ops/power", power_document)
    reg.get("ops/config", config_document)
    reg.get("ops/collectors", collectors_document)
    reg.post("ops/wake", wake, audited=True)
    reg.post("ops/hostkey/scan", scan_hostkey)
    reg.post("ops/config/validate", validate_config)
    reg.post("ops/config/save", save_config, audited=True)
    reg.post("ops/machine-extras", machine_extras, audited=True)
    reg.post("telemetry/refresh", refresh)
    reg.post("telemetry/interval", interval, audited=True)
    reg.job("ops/nodes-update", lambda api, a, ctx: nodes_update(api, a, ctx), validate=validate_nodes_update,
            exclusive=True, resources=update_resources, deadline_s=900)
    reg.job("ops/shutdown", lambda api, a, ctx: shutdown(api, a, ctx), validate=validate_shutdown, exclusive=True,
            resources=lambda api, a: {a["machine"]}, deadline_s=90)
    reg.job("ops/machine-test", lambda api, a, ctx: machine_test(api, a, ctx),
            validate=lambda api, a: (strict(a, ("machine",), ("machine",)), machine_of(api, a["machine"]), a)[2],
            deadline_s=180)
    reg.collector("ops-nodes", sample_nodes, interval=NODE_TOOL_INTERVAL)
    reg.collector("ops", sample_control, interval=2)
