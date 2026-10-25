"""Read-only view of the lab GPU-turn queue (holder, live tickets, recent turns).

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).

The turn tool keeps its state in ``<lab_dir>/.gpu-turn/``: ``holder`` (who holds the node's GPU), ``single-q`` and
``pair-q`` ticket files named ``<ns>-<pid>`` (``0-<ns>-<pid>`` = priority pair, ``0-0-single-<node>-<pid>`` = the
gate of an owed single turn), flag files and an append-only ``log``. This verb only reads them: nothing is written,
no process is signalled, symlinks are never followed, every read is bounded.

A ticket counts as waiting only when a live process of the current boot with its pid started no later than the
ticket was written (start times computed from /proc only underestimate, so a later start means a reused pid) and,
when its command line is readable, that process is a turn script. What this cannot prove: a pid reused within the
same 50 ms by another turn script.
"""

import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

VERBS = {"turns"}
SCHEMA = "hyperloom/node-v1"
STATE = ".gpu-turn"
MAX_LOG = 256 * 1024
MAX_SMALL = 64 * 1024
MAX_LINE = 1024
MAX_TICKETS = 512
MAX_ENUMERATE = 1024
MAX_EVENTS = 200
MAX_TURNS = 60
OUTPUT_BUDGET = 200 * 1024
LONG_OK_MAX_S = 7200
VERB_WORDS = ("wait", "admit", "start", "reap", "end", "CAP", "CLAMP", "BADCAP", "REFUSE")


def _error(message):
    return {"schema": SCHEMA, "verb": "turns", "error": message}


def lab_directory(payload):
    """The lab directory from the payload (an absolute POSIX path without '..')."""
    value = payload.get("lab_dir")
    if (not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value)
            or not PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or "\\" in value):
        raise ValueError("lab_dir must be an absolute POSIX path without '..'")
    return Path(value)


FD_WALK = os.open in os.supports_dir_fd and os.scandir in os.supports_fd and hasattr(os, "O_DIRECTORY")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class Reader:
    """Bounded, no-follow reads below one directory (the turn state directory).

    POSIX: the directory is opened once with O_DIRECTORY|O_NOFOLLOW and every entry is opened relative to that
    descriptor with O_NOFOLLOW (a swapped ancestor cannot redirect a read); every opened file must be a regular file
    (fstat), so a FIFO or a device is never read. Elsewhere (tests on the control machine): path-based with the same
    link refusal and file-type checks."""

    def __init__(self, base):
        self.base = Path(base)
        if os.path.islink(self.base):
            raise ValueError("symbolic link refused")
        self.fd = None
        if FD_WALK:
            self.fd = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
        elif not stat.S_ISDIR(os.lstat(self.base).st_mode):
            raise ValueError("turn state directory is not a plain directory")
        self.subdirs = {}

    def close(self):
        for fd in [self.fd, *self.subdirs.values()]:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd, self.subdirs = None, {}

    @staticmethod
    def _plain(name):
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise OSError("invalid entry name")
        return name

    def _dir(self, name):
        """Descriptor of a direct subdirectory (POSIX) or its path."""
        name = self._plain(name)
        if self.fd is None:
            path = self.base / name
            if os.path.islink(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
                raise OSError("not a plain directory")
            return path
        if name not in self.subdirs:
            self.subdirs[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=self.fd)
        return self.subdirs[name]

    def _open(self, parts):
        parts = [self._plain(p) for p in parts]
        flags = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        if self.fd is None:
            path = self.base.joinpath(*parts)
            if any(os.path.islink(self.base.joinpath(*parts[:i + 1])) for i in range(len(parts))):
                raise OSError("symbolic link refused")
            return os.open(path, flags | getattr(os, "O_BINARY", 0))
        parent = self.fd if len(parts) == 1 else self._dir(parts[0])
        if len(parts) > 2:
            raise OSError("too deep")
        return os.open(parts[-1], flags, dir_fd=parent)

    def stat(self, *parts):
        try:
            parts = [self._plain(p) for p in parts]
            if self.fd is None:
                path = self.base.joinpath(*parts)
                return os.lstat(path)
            parent = self.fd if len(parts) == 1 else self._dir(parts[0])
            return os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except OSError:
            return None

    def read(self, *parts, limit=MAX_SMALL, tail=False):
        """(bytes, truncated) of a regular file, or None when missing, unreadable or not a regular file."""
        try:
            fd = self._open(parts)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            truncated = info.st_size > limit
            if tail and truncated:
                os.lseek(fd, info.st_size - limit, os.SEEK_SET)
            data = b""
            while len(data) < limit:
                chunk = os.read(fd, min(65536, limit - len(data)))
                if not chunk:
                    break
                data += chunk
            return data, truncated
        except OSError:
            return None
        finally:
            os.close(fd)

    def listdir(self, name):
        """(sorted names, enumeration_truncated) of a direct subdirectory, or (None, False) when absent."""
        try:
            target = self._dir(name)
            names, truncated = [], False
            with os.scandir(target) as entries:
                for entry in entries:
                    if len(names) >= MAX_ENUMERATE:
                        truncated = True
                        break
                    names.append(entry.name)
            return sorted(names), truncated
        except OSError:
            return None, False


class Processes:
    """Process facts from a /proc tree (injectable for tests). Reads are bounded (stat <= 4 KiB, cmdline <= 8 KiB)."""

    def __init__(self, proc="/proc", uid=None, clock=time.time):
        self.proc = Path(proc)
        self.uid = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else None)
        self.btime = None
        text = self._read(self.proc / "stat", 65536)
        for line in (text or "").splitlines():
            if line.startswith("btime "):
                try:
                    self.btime = int(line.split()[1])
                except (ValueError, IndexError):
                    pass
        # Boot time as an interval: the wall clock read before and after the boot-time clock (process start times
        # count from it), or around /proc/uptime (10 ms resolution) for a /proc tree other than the live one.
        self.boot_lo = self.boot_hi = None
        if str(self.proc) == "/proc" and hasattr(time, "CLOCK_BOOTTIME") and clock is time.time:
            before = time.time()
            since_boot = time.clock_gettime(time.CLOCK_BOOTTIME)
            after = time.time()
            self.boot_lo, self.boot_hi = before - since_boot, after - since_boot
        else:
            before = clock()
            uptime = self._read(self.proc / "uptime", 256)
            after = clock()
            try:
                seconds = float(uptime.split()[0])
                self.boot_lo, self.boot_hi = before - seconds - 0.01, after - seconds
            except (AttributeError, ValueError, IndexError):
                if self.btime is not None:
                    self.boot_lo, self.boot_hi = self.btime, self.btime + 1.0
        self.boot = self.boot_lo
        boot = self._read(self.proc / "sys" / "kernel" / "random" / "boot_id", 128)
        self.boot_id = boot.strip()[:64] if boot and boot.strip() else None
        try:
            self.hz = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            self.hz = 100

    @staticmethod
    def _read(path, limit):
        try:
            with open(path, "rb") as stream:
                return stream.read(limit).decode("ascii", "replace")
        except OSError:
            return None

    def started(self, pid):
        """(earliest, latest) possible wall-clock start of a live process, or None (gone, zombie, unreadable)."""
        if self.boot_lo is None:
            return None
        raw = self._read(self.proc / str(pid) / "stat", 4096)
        try:
            fields = raw.rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                return None
            ticks = int(fields[19])
            return self.boot_lo + ticks / self.hz, self.boot_hi + (ticks + 1) / self.hz
        except (AttributeError, ValueError, IndexError):
            return None

    def label(self, pid):
        """(label, verified, why): the argument after a gpu-turn*.sh script in the command line (own uid only).
        verified is True for a turn script, False for another program, None when it cannot be read."""
        directory = self.proc / str(pid)
        try:
            owner = os.stat(directory).st_uid
        except OSError:
            return None, None, "process gone"
        if self.uid is not None and owner != self.uid:
            return None, None, "process of another user (command line not read)"
        try:
            with open(directory / "cmdline", "rb") as stream:
                argv = stream.read(8192).split(b"\0")
        except OSError:
            return None, None, "command line unreadable"
        for index, arg in enumerate(argv[:-1]):
            name = arg.decode("utf-8", "replace").rsplit("/", 1)[-1]
            if re.fullmatch(r"gpu-turn(?:-pair|-any)?\.sh", name):
                label = argv[index + 1].decode("utf-8", "replace")
                if label and len(label) <= 128 and not any(ord(c) < 32 for c in label):
                    return label, True, None
                return None, True, "turn script without a readable label"
        return None, False, "not a turn script process (pid reused)"

    def identify(self, pid, written, label=None):
        """Who wrote a record at `written` with this pid: ("verified", facts), ("unverified", facts) or (None, why).

        Two live processes never share a pid, so a live process whose whole start interval ends at or before the
        write is the writer; one that surely started later reuses the pid; one whose interval straddles the write
        time, or whose command line cannot be read, is unverified. verified also needs a turn script (with `label`,
        carrying that label)."""
        if self.boot_lo is None or written is None:
            return None, "boot time unreadable"
        if written < self.boot_lo:
            return None, "written before this boot"
        interval = self.started(pid)
        if interval is None:
            return None, "process gone"
        lo, hi = interval
        if lo > written:
            return None, "pid reused by a later process"
        found, script, why = self.label(pid)
        if script is False or self.started(pid) != interval:      # another program, or the pid changed meanwhile
            return None, "pid reused by another program"
        facts = {"started": [lo, hi], "label": found, "why": why}
        if script and label is not None and found != label:
            return None, "pid reused by another turn"
        if hi > written:
            facts["why"] = "the process start cannot be told apart from the write time (unverified)"
            return "unverified", facts
        if script is None:
            return "unverified", facts
        return "verified", facts


TICKET = re.compile(r"(?:(0)-)?([1-9][0-9]{8,19})-([1-9][0-9]{0,9})")
GATE = re.compile(r"0-0-single-([A-Za-z0-9._-]{1,64})-([1-9][0-9]{0,9})")
LOG_LABEL_WINDOW_S = 2.0


def tickets(reader, procs, queue, now, deadline):
    """(verified live tickets in the turn tool's order, unverified live ones, count of the others, enumeration cut) of
    one queue directory. Unverified = the process exists and is old enough but its command line cannot be read."""
    names, cut = reader.listdir(queue)
    if names is None:
        return None, None, 0, False
    live, unverified, dead = [], [], 0
    for name in names[:MAX_TICKETS]:
        if time.monotonic() >= deadline:
            cut = True
            break
        gate = GATE.fullmatch(name)
        match = TICKET.fullmatch(name)
        if gate:
            info = reader.stat(queue, name)
            row = {"name": name, "kind": "gate", "node": gate[1], "pid": int(gate[2]),
                   "ts": info.st_mtime if info else None, "ts_source": "file mtime"}
        elif match:
            row = {"name": name, "kind": "priority" if match[1] else ("pair" if queue == "pair-q" else "single"),
                   "pid": int(match[3]), "ts": int(match[2]) / 1e9, "ts_source": "ticket name"}
        else:
            continue
        verdict, facts = procs.identify(row["pid"], row["ts"])
        if verdict is None:
            dead += 1
            continue
        row.update(process_started=facts["started"], verified=verdict == "verified", waited_s=max(0.0, now - row["ts"]))
        if row["kind"] == "gate":
            row["label"], row["label_why"] = None, "gate of an owed single turn on " + row["node"]
        else:
            row["label"], row["label_why"] = facts["label"], facts["why"]
        if verdict != "verified":
            row["unverified_why"] = facts["why"] or "command line unreadable"
        (live if verdict == "verified" else unverified).append(row)
    return live, unverified, dead + max(0, len(names) - MAX_TICKETS), cut or len(names) > MAX_TICKETS


def _seconds(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


def local_epochs(day, seconds):
    """Every epoch at which this machine's local clock read `seconds` after midnight of `day` (two in the hour a
    clock change repeats, none in an hour it skips)."""
    hour, rest = divmod(int(seconds), 3600)
    minute, second = divmod(rest, 60)
    found = set()
    for dst in (0, 1, -1):
        try:
            epoch = time.mktime((day.year, day.month, day.day, hour, minute, second, 0, 0, dst))
        except (OverflowError, ValueError):
            continue
        local = time.localtime(epoch)
        if (local.tm_year, local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec) == (
                day.year, day.month, day.day, hour, minute, second):
            found.add(epoch)
    return found


def anchor_times(clock_seconds, anchor, tolerance=300):
    """Epoch estimates for clock-only (HH:MM:SS) values in file order; the newest is at or before `anchor`.

    Walking back from the newest line, each value takes the latest reading of the clock (on the same day as the
    next-newer line or the day before) that is not later than that line plus `tolerance` (writers may append slightly
    out of order). Midnight crossings and repeated clock-change hours resolve that way; a gap of a whole day or more
    cannot be seen in clock-only lines (callers say so)."""
    result = [None] * len(clock_seconds)
    ref = anchor + tolerance
    day = datetime.date.fromtimestamp(anchor)
    for index in range(len(clock_seconds) - 1, -1, -1):
        value = clock_seconds[index]
        candidates = local_epochs(day, value) | local_epochs(day - datetime.timedelta(days=1), value)
        if not candidates:      # a skipped wall time: read it as the plain local time
            candidates = {time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)) + value}
        earlier = [c for c in candidates if c <= ref]
        chosen = max(earlier) if earlier else min(candidates)
        result[index] = chosen
        ref = chosen + tolerance
        day = datetime.date.fromtimestamp(chosen)
    return result


LINE = re.compile(r"(\d{2}:\d{2}:\d{2}) (\S+)\s+(.*)")
SUBJECT = re.compile(r"(PAIR )?(\S+)(?: pid ([0-9]+))?(?:\s*(\(remote\)|\(local\)))?(.*)")
REAP = re.compile(r"reaped (\d+) pids(?: \[[^\]]*\])?; vram (\d+)->(\d+) MiB, gtt (\d+)->(\d+) MiB; ([0-9.]+) s")


def parse_log(data, anchor):
    """Events of the log tail (oldest first) with inferred epoch times."""
    rows = []
    for line in data.decode("utf-8", "replace").split("\n"):
        line = line[:MAX_LINE].rstrip("\r")
        match = LINE.fullmatch(line) if line else None
        if match:
            rows.append((match, line))
    times = anchor_times([_seconds(m[1]) for m, _ in rows], anchor)
    events = []
    for (match, line), ts in zip(rows, times):
        verb, rest = match[2], match[3]
        event = {"ts": ts, "clock": match[1], "verb": verb if verb in VERB_WORDS else "other"}
        if verb not in VERB_WORDS:
            event["raw"] = line[:300]
        else:
            subject = SUBJECT.fullmatch(rest)
            if subject:
                event.update(pair=bool(subject[1]), label=subject[2][:128],
                             pid=int(subject[3]) if subject[3] else None,
                             side=(subject[4] or "").strip("()") or None, detail=subject[5].strip()[:400])
                if verb == "CAP":
                    hit = re.search(r"hit (?:GPU_TURN_MAX=)?([0-9]+)s(?: \(rc ([0-9]+)\))?", event["detail"])
                    if hit:
                        event["cap_s"] = int(hit[1])
                        event["rc"] = int(hit[2]) if hit[2] else None
        events.append(event)
    return events


def _figures(text):
    match = REAP.search(text)
    if not match:
        return None
    return {"reaped": int(match[1]), "vram_mib": [int(match[2]), int(match[3])],
            "gtt_mib": [int(match[4]), int(match[5])], "seconds": float(match[6])}


def reap_figures(detail):
    """Reap figures of a reap line (the local part and, for a pair, the peer's)."""
    parts = detail.split("|", 1)
    out = {"warnings": detail.count("WARN"), "local": _figures(parts[0])}
    if len(parts) > 1:
        out["peer"] = _figures(parts[1])
    return out


def turns_from(events):
    """Turns rebuilt from start..end lines with the same (label, pid, side)."""
    open_turns, done = {}, []
    for event in events:
        if event["verb"] not in ("start", "reap", "end", "CAP") or "label" not in event:
            continue
        remote = event.get("side") == "remote"
        key = (event["label"], event.get("pid"), remote)
        if event["verb"] == "start":
            turn = {"label": event["label"], "pid": event.get("pid"),
                    "kind": "pair" if event.get("pair") else "single", "side": "remote" if remote else "local",
                    "start": event["ts"], "start_clock": event["clock"], "end": None, "duration_s": None,
                    "outcome": "running", "cap_hit_s": None, "rc": None, "reap": None, "retries": None}
            retries = re.search(r"after ([0-9]+) retries", event.get("detail") or "")
            if retries:
                turn["retries"] = int(retries[1])
            if key in open_turns:       # a start without an end: its end line is missing from the log
                previous = open_turns.pop(key)
                previous["outcome"] = "no end line"
                done.append(previous)
            open_turns[key] = turn
            continue
        turn = open_turns.get(key)
        if turn is None and event["verb"] == "CAP":     # a CAP line names the label only
            turn = next((t for (label, _, side), t in reversed(list(open_turns.items()))
                         if label == event["label"] and not side), None)
        if turn is None:
            continue
        if event["verb"] == "CAP":
            turn["cap_hit_s"], turn["rc"] = event.get("cap_s"), event.get("rc")
        elif event["verb"] == "reap":
            turn["reap"] = reap_figures(event.get("detail") or "")
        else:
            turn["end"] = event["ts"]
            turn["duration_s"] = event["ts"] - turn["start"]
            if turn["cap_hit_s"]:
                turn["outcome"] = f"cap hit ({turn['cap_hit_s']} s" + (f", rc {turn['rc']})" if turn["rc"] is not None else ")")
            else:
                turn["outcome"] = "ended (rc not logged)"
            if turn["reap"] and turn["reap"]["warnings"]:
                turn["outcome"] += " · reap warning"
            done.append(open_turns.pop(key))
    done.extend(open_turns.values())
    done.sort(key=lambda t: t["start"])
    return done


HOLDER = re.compile(r"(PAIR )?(\S{1,128}?)(?: \((local|remote)\))? since (\d{2}:\d{2}:\d{2})(?: pid ([0-9]+))?")


def holder_of(reader, procs, now):
    found = reader.read("holder", limit=512)
    if found is None:
        return None, "no holder file (the GPU is free)"
    text = found[0].decode("utf-8", "replace").strip()
    if not text:
        return None, "holder file empty (the GPU is free)"
    match = HOLDER.fullmatch(text.splitlines()[0])
    if not match:
        return {"raw": text[:200], "parsed": False}, "holder line not recognised"
    since = anchor_times([_seconds(match[4])], now)[0]
    row = {"raw": text[:200], "parsed": True, "kind": "pair" if match[1] else "single", "label": match[2],
           "side": match[3] or "local", "since_clock": match[4], "since": since, "ts_inferred": True,
           "elapsed_s": max(0.0, now - since), "pid": int(match[5]) if match[5] else None}
    if row["pid"] is not None:
        verdict, facts = procs.identify(row["pid"], since, label=row["label"])
        row["alive"] = verdict is not None
        row["verified"] = verdict == "verified"
        row["why_alive"] = (None if verdict == "verified" else facts["why"] or "command line unreadable" if verdict
                            else "stale holder file: " + facts)
    else:
        row["alive"] = row["verified"] = None
        row["why_alive"] = "the second node's holder line has no pid (the pair runs from the first node)"
    return row, None


def long_ok(reader):
    found = reader.read("long-ok", limit=MAX_SMALL)
    if found is None:
        return {}
    result = {}
    for line in found[0].decode("utf-8", "replace").splitlines()[:512]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= LONG_OK_MAX_S and len(parts[0]) <= 128:
            result[parts[0]] = int(parts[1])
    return result


def flags(reader):
    out = {}
    maintenance = reader.read("maintenance", limit=4096)
    if maintenance is None:
        out["maintenance"] = None
    else:
        lines = maintenance[0].decode("utf-8", "replace").strip().splitlines()
        out["maintenance"] = lines[0][:200] if lines and lines[0] else "set"
    for name in ("peer-down", "pair-pending", "pair-since"):
        info = reader.stat(name)
        out[name.replace("-", "_")] = None if info is None else info.st_mtime
    return out


def collect(lab_dir, proc="/proc", now=None, uid=None, budget_s=4.0):
    deadline = time.monotonic() + budget_s
    clock = time.time if now is None else (lambda: now)
    now = clock()
    base = Path(lab_dir) / STATE
    result = {"schema": SCHEMA, "verb": "turns", "now": now, "tz_offset_s": time.localtime(now).tm_gmtoff,
              "ts_inferred_note": "dates of log and holder lines are inferred from clock-only HH:MM:SS values; whole-day "
                                  "gaps and crossings hidden inside the tolerance cannot be detected",
              "identity_note": "a waiting ticket is verified when its live process surely started before the ticket "
                               "was written and is a turn script; otherwise it is listed as unverified",
              "why": {}}
    procs = Processes(proc, uid=uid, clock=clock)
    result.update(boot_id=procs.boot_id, btime=procs.btime, boot=[procs.boot_lo, procs.boot_hi])
    if procs.boot_lo is None:
        result["why"]["btime"] = "boot time unreadable: no ticket or holder can be checked"
    try:
        if os.path.islink(Path(lab_dir)):
            raise ValueError("symbolic link refused")
        reader = Reader(base)
    except (OSError, ValueError) as exc:
        result.update(present=False, holder=None, queues=None, flags=None, events=None, turns=None)
        result["why"]["state"] = "turn state directory unavailable: " + (
            exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc))
        return result
    try:
        result["present"] = True
        result["holder"], why = holder_of(reader, procs, now)
        if why:
            result["why"]["holder"] = why
        result["coordinator"] = reader.listdir("pair-q")[0] is not None
        result["flags"] = flags(reader)
        result["long_ok"] = long_ok(reader)
        queues, unverified, stale, cut = {}, {}, {}, []
        for queue in ("single-q", "pair-q"):
            rows, loose, dead, truncated = tickets(reader, procs, queue, now, deadline)
            queues[queue], unverified[queue], stale[queue] = rows, loose, dead
            if truncated:
                cut.append(queue)
            if rows is None:
                result["why"][queue] = "queue directory absent"
        result["queues"], result["unverified"], result["stale_tickets"] = queues, unverified, stale
        if cut:
            result["why"]["enumeration_truncated"] = "only the first entries of " + ", ".join(cut) + " were examined"
        log = reader.read("log", limit=MAX_LOG, tail=True)
        info = reader.stat("log")
        if log is None or info is None:
            result.update(events=None, turns=None, log=None)
            result["why"]["log"] = "turn log unreadable"
        else:
            data, truncated = log
            if truncated:
                data = data.split(b"\n", 1)[1] if b"\n" in data else b""
            events = parse_log(data, info.st_mtime)
            for rows in unverified.values():
                for row in rows or ():
                    waits = [e for e in events if e["verb"] == "wait" and e.get("pid") == row["pid"]
                             and abs(e["ts"] - row["ts"]) <= LOG_LABEL_WINDOW_S]
                    if waits:       # shown beside an unverified ticket, never used as its identity
                        row["logged_label"] = waits[-1]["label"]
            result["log"] = {"size": info.st_size, "mtime": info.st_mtime, "tail_bytes": len(data),
                             "truncated": truncated, "first_ts": events[0]["ts"] if events else None}
            result["turns"] = turns_from(events)[-MAX_TURNS:]
            result["events"] = events[-MAX_EVENTS:]
    finally:
        reader.close()
    fit(result)
    return result


def fit(result):
    """Keep the serialized reply under the output budget: drop the oldest events, then turns, and say so."""
    trimmed = set()
    while len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > OUTPUT_BUDGET:
        if result.get("events"):
            result["events"] = result["events"][max(1, len(result["events"]) // 2):]
            trimmed.add("events")
        elif result.get("turns"):
            result["turns"] = result["turns"][max(1, len(result["turns"]) // 2):]
            trimmed.add("turns")
        else:
            break
    if trimmed:
        result["why"]["trimmed"] = "oldest " + " and ".join(sorted(trimmed)) + " dropped to fit the output limit"


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    if set(payload) != {"lab_dir"}:
        return _error("the payload must be {\"lab_dir\": <absolute path>}")
    try:
        lab = lab_directory(payload)
    except ValueError as exc:
        return _error(str(exc))
    return collect(lab)
