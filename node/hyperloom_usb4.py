"""Read-only USB4 link telemetry: thunderbolt sysfs link speed/lanes, the published stream layout, module
parameters, ring interrupt counts and kernel evidence lines. Never opens a stream device, never reads or writes
configfs or debugfs, never opens a module parameter that has no read permission.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).

Payload (all optional): {"journal": {"boot_id": str | null, "cursor": str | null}}. Without "journal" the kernel
log is not read. Journal modes, always bound to the running boot (--boot=<id>): "follow" starts AT the stored
cursor and continues only when the first entry returned is exactly that entry (the journal repositions silently
to a neighbour when the entry is gone); "initial" scans the newest entries of this boot (no cursor for it yet);
"cursor_lost" is an initial scan made because the stored entry is no longer in the journal. The caller stores
the returned cursor per boot. Totals of an initial scan are complete only when the scan reached the first
entries of the boot (monotonic time under two minutes) and no entry was malformed.

Reads are confined to fixed roots (thunderbolt sysfs and the devices it resolves to, the two module parameter
directories, /proc, the published layout file and stat of /dev/tbstreamN) and to a byte budget; the whole
collection runs under one deadline: a part not finished in time is reported unavailable and a journal child
still running is killed.

Link speeds in sysfs are per lane: the trained rate of a direction is the per-lane speed times the lanes. The
first two MSI-X vectors of every host interface (NHI) serve its control rings; further vectors are data rings.
"""

import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import threading
import time

VERBS = {"usb4"}
SCHEMA = "hyperloom/node-v1"
DEADLINE_S = 5.0
MAX_OUTPUT = 256 * 1024
MAX_FILE = 1024 * 1024
READ_BUDGET = 4 * 1024 * 1024
MAX_ENTRIES = 256
SMALL = 4096
FULL_LANE_GBPS = 20.0          # USB4 Gen3 lane
FULL_LANES = 2
MAX_LINKS, MAX_STREAMS, MAX_CABLES, MAX_PARAMS = 64, 64, 16, 64
JOURNAL_FOLLOW, JOURNAL_INITIAL = 2000, 20000
JOURNAL_TIMEOUT_S = 4.0
JOURNAL_STDOUT_CAP = 24 * 1024 * 1024
RECENT_LINES, LINE_CHARS = 20, 300

SPEED = re.compile(r"\s*(\d+(?:\.\d+)?)\s*Gb/s\s*")
ROUTER = re.compile(r"(\d{1,3})-([0-9a-f]{1,16})")       # N-R: a router or XDomain on domain N, route R
SERVICE = re.compile(r"\d{1,3}-[0-9a-f]{1,16}\.\d{1,3}")
STREAM_KEY = re.compile(r"(\d{1,3}-[0-9a-f]{1,16}\.\d{1,3})/([A-Za-z0-9_.-]{1,64})")
DOMAIN = re.compile(r"domain(\d{1,3})")
PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
PARAM = re.compile(r"[a-z0-9_]{1,64}")
MSIX = re.compile(r"^\s*(\d+):\s+((?:\d+\s+)+)(.*?)PCI-MSIX-(\S+)\s+(\d+)-edge\s+(.*)$")
BOOT_ID = re.compile(r"[0-9a-f]{32}")
CURSOR = re.compile(r"[!-~]{1,512}")
CONTROL_VECTORS = 2
CONFIG_KEYS = ("busy_poll", "rx_busy_poll", "gpu_direct", "gpu_poll", "ring_size", "throttling", "irq_cpu",
               "spin_us", "latency_qos_us", "rx_napi", "open_timeout_ms", "gpu_quarantine")
MATCH = re.compile(r"thunderbolt|tbstream|usb4", re.I)
# Ordered: the first matching substring names the kind of a USB4 kernel line.
EVIDENCE = (("reset_pending", "host interface reset after dma teardown pending"),
            ("host_reset", "host interface reset"),
            ("quarantine", "quarantine"),
            ("ring_leaked", "leaked by an abandoned dma owner"),
            ("ring_leaked", "ring leaked"),
            ("contexts_retained", "gpu contexts retained"),
            ("hop_deactivation_failed", "hop deactivation failed"),
            ("close_not_sent", "close not sent"),
            ("teardown", "teardown"))
KINDS = tuple(dict.fromkeys(kind for kind, _ in EVIDENCE))
UNREADABLE = ("not seeing messages", "no journal files", "permission denied", "access denied")
# Resolved locations a read, listing or existence check may land in (a sysfs entry is a link into /sys/devices).
# Nothing under /dev is ever read: stream devices are only lstat()ed through Reader.device().
READ_ALLOWED = ("/sys/bus/thunderbolt", "/sys/devices", "/sys/module/thunderbolt", "/sys/module/thunderbolt_stream",
                "/proc/interrupts", "/proc/sys/kernel/random/boot_id", "/run/usb4stream-layout.json")
DEVICE = re.compile(r"/dev/tbstream\d{1,3}")


class Deadline(Exception):
    pass


def unavailable(why):
    return {"status": "unavailable", "why": why}


def classify(message):
    """Evidence kind of a kernel line, or None when the line is not USB4 related or names nothing tracked."""
    if not MATCH.search(message):
        return None
    lower = message.lower()
    for kind, needle in EVIDENCE:
        if needle in lower:
            return kind
    return None


def normal_boot(value):
    """Boot ids as 32 lower-case hex digits (the kernel prints dashes, journal cursors do not)."""
    text = value.replace("-", "").strip().lower() if isinstance(value, str) else ""
    return text if BOOT_ID.fullmatch(text) else None


def cursor_boot(cursor):
    for part in cursor.split(";") if isinstance(cursor, str) else ():
        if part.startswith("b="):
            return normal_boot(part[2:])
    return None


def link_direction(raw_speed, raw_lanes):
    match = SPEED.fullmatch(raw_speed or "")
    speed = float(match[1]) if match else None
    lanes = int(raw_lanes) if raw_lanes and raw_lanes.isdecimal() else None
    gbps = speed * lanes if speed is not None and lanes in (1, 2) else None
    full = None if gbps is None else (speed >= FULL_LANE_GBPS and lanes >= FULL_LANES)
    return {"gbps_per_lane": speed, "lanes": lanes, "gbps": gbps, "full_rate": full,
            "raw_speed": raw_speed, "raw_lanes": raw_lanes}


def interrupt_vectors(text):
    """[{nhi, vector, irq, ring, count}] of the thunderbolt MSI-X lines of /proc/interrupts (count over CPUs)."""
    out = []
    for line in text.splitlines():
        match = MSIX.match(line)
        if not match or "thunderbolt" not in [name.strip() for name in match[6].split(",")]:
            continue
        if not PCI.fullmatch(match[4]):
            continue
        vector = int(match[5])
        out.append({"nhi": match[4], "vector": vector, "irq": int(match[1]),
                    "ring": "control" if vector < CONTROL_VECTORS else "data",
                    "count": sum(int(value) for value in match[2].split())})
    return sorted(out, key=lambda row: (row["nhi"], row["vector"]))


def _strict(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    return json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                      parse_constant=lambda text: (_ for _ in ()).throw(ValueError(text)))


def _scalar(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:64]
    return None


def _int(value):
    return value if type(value) is int else None


def _bool(value):
    return value if type(value) is bool else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def same_entry(first, second):
    """Two cursors name one journal entry when their sequence-number id, sequence number and boot agree."""
    def fields(cursor):
        parts = dict(part.split("=", 1) for part in cursor.split(";") if "=" in part)
        return parts.get("s"), parts.get("i"), normal_boot(parts.get("b"))
    one, two = fields(first), fields(second)
    return None not in one and one == two


def boot_start(first):
    """True when the entry is the kernel's first message of a boot (kernel time 0, or its version banner), so
    the scan covers the boot from its start."""
    if not first:
        return False
    source, message = first
    return source == "0" or (isinstance(message, str) and message.startswith("Linux version "))


def _usec(value):
    try:
        return int(value) / 1e6
    except (TypeError, ValueError):
        return None


class Reader:
    """Bounded filesystem access with one policy per operation:

    - read(): regular files only, resolved inside READ_ALLOWED (never /dev); opened without following a final
      symlink and without blocking, and checked again on the open descriptor;
    - probe()/link(): existence and symlink names inside READ_ALLOWED;
    - device(): lstat of an index-built /dev/tbstreamN, nothing else under /dev, never opened.

    With `root` (tests, offline trees) absolute paths map under it and every resolution must stay inside it.
    `budget` caps the bytes read by one collection (the journal is bounded separately). Outcomes distinguish an
    observed absence ("absent") from a failed inspection ("error")."""

    def __init__(self, root=None, runner=None, clock=time.time, budget=READ_BUDGET):
        self.root = Path(root).resolve() if root is not None else None
        self.runner = runner
        self.clock = clock
        self.budget = budget

    def _map(self, logical):
        parts = PurePosixPath(logical).parts
        if not isinstance(logical, str) or not PurePosixPath(logical).is_absolute() or ".." in parts:
            raise ValueError("absolute path without parent traversal required")
        base = self.root if self.root is not None else Path("/")
        return base, base.joinpath(*parts[1:])

    def path(self, logical):
        """The mapped path of `logical`, refused unless it resolves inside the root (when set) and inside one
        of the READ_ALLOWED locations."""
        base, mapped = self._map(logical)
        resolved = mapped.resolve()
        if self.root is not None and resolved != self.root and self.root not in resolved.parents:
            raise ValueError("path escapes the inspection root")
        for allowed in READ_ALLOWED:
            anchor = base.joinpath(*PurePosixPath(allowed).parts[1:])
            if resolved == anchor or anchor in resolved.parents:
                return mapped
        raise ValueError("path resolves outside the USB4 sources")

    def read(self, logical, limit=SMALL):
        try:
            path = self.path(logical)
            if not stat.S_ISREG(os.stat(path).st_mode):
                return None
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return None
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(min(limit, max(0, self.budget)) + 1)
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return None
        self.budget -= len(raw)
        if len(raw) > limit or self.budget < 0:
            return None
        try:
            return raw.decode("utf-8").strip()
        except UnicodeError:
            return None

    def listdir(self, logical):
        try:
            names = []
            with os.scandir(self.path(logical)) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) >= MAX_ENTRIES:
                        break
            return sorted(names)
        except (OSError, ValueError):
            return []

    def probe(self, logical):
        """("present", stat) | ("absent", None) | ("error", why) for a path inside READ_ALLOWED."""
        try:
            return "present", os.stat(self.path(logical))
        except (FileNotFoundError, NotADirectoryError):
            return "absent", None
        except ValueError as exc:
            return "error", str(exc)
        except OSError as exc:
            return "error", exc.strerror or type(exc).__name__

    def stat(self, logical):
        state, value = self.probe(logical)
        return value if state == "present" else None

    def link(self, logical):
        """("link", target name) | ("absent", None) | ("error", why): the basename of a symlink's target."""
        try:
            base, mapped = self._map(logical)
            self.path(str(PurePosixPath(logical).parent))       # the directory must be an allowed source
            return "link", os.path.basename(os.readlink(mapped))
        except (FileNotFoundError, NotADirectoryError):
            return "absent", None
        except ValueError as exc:
            return "error", str(exc)
        except OSError as exc:
            return "error", exc.strerror or type(exc).__name__

    def link_name(self, logical):
        state, value = self.link(logical)
        return value if state == "link" else None

    def device(self, logical):
        """("char", stat) | ("other", stat) | ("absent", None) | ("error", why) for /dev/tbstreamN (lstat only)."""
        if not isinstance(logical, str) or not DEVICE.fullmatch(logical):
            return "error", "not a stream device path"
        try:
            _base, mapped = self._map(logical)
            info = os.lstat(mapped)
        except FileNotFoundError:
            return "absent", None
        except (OSError, ValueError) as exc:
            return "error", getattr(exc, "strerror", None) or str(exc)
        return ("char" if stat.S_ISCHR(info.st_mode) else "other"), info

    def parent_name(self, logical):
        """Name of the directory that holds the resolved entry (a domain's PCI function)."""
        try:
            return self.path(logical).resolve().parent.name
        except (OSError, ValueError):
            return None


class Collector:
    def __init__(self, reader=None, deadline_s=DEADLINE_S):
        self.io = reader or Reader()
        self.runner = self.io.runner or subprocess.Popen
        self.deadline_s = deadline_s
        self.end = time.monotonic() + deadline_s
        self.child = None
        self.expired = threading.Event()

    def remaining(self):
        return self.end - time.monotonic()

    def check(self):
        if self.remaining() <= 0 or self.expired.is_set():
            raise Deadline()

    # -- boot and domains ------------------------------------------------------------------------------------
    def boot_id(self):
        return normal_boot(self.io.read("/proc/sys/kernel/random/boot_id"))

    def domains(self):
        """{domain: NHI PCI address}: the parent of each resolved /sys/bus/thunderbolt/devices/domainN."""
        out = {}
        for name in self.io.listdir("/sys/bus/thunderbolt/devices"):
            match = DOMAIN.fullmatch(name)
            if match:
                parent = self.io.parent_name("/sys/bus/thunderbolt/devices/" + name)
                out[int(match[1])] = parent if parent and PCI.fullmatch(parent) else None
        return out

    # -- links -----------------------------------------------------------------------------------------------
    def links(self, domains):
        base = "/sys/bus/thunderbolt/devices"
        out = []
        for name in self.io.listdir(base):
            match = ROUTER.fullmatch(name)
            if not match:
                continue
            self.check()
            if self.io.parent_name(base + "/" + name) is None:
                continue                    # not resolvable inside the allowed sysfs locations
            entry = base + "/" + name
            raw = {f: self.io.read(entry + "/" + f) for f in ("rx_speed", "rx_lanes", "tx_speed", "tx_lanes")}
            if raw["rx_speed"] is None and raw["tx_speed"] is None:
                continue                    # host routers and devices without a trained link of their own
            rx = link_direction(raw["rx_speed"], raw["rx_lanes"])
            tx = link_direction(raw["tx_speed"], raw["tx_lanes"])
            domain, route = int(match[1]), int(match[2], 16)
            below = [d for d, value in (("rx", rx), ("tx", tx)) if value["full_rate"] is False]
            known = None not in (rx["full_rate"], tx["full_rate"])
            out.append({"router": name, "domain": domain, "nhi": domains.get(domain),
                        "usb4_port": route & 0xFF if route else None,
                        "peer": _text(self.io.read(entry + "/device_name"), 64),
                        "peer_vendor": _text(self.io.read(entry + "/vendor_name"), 64),
                        "peer_unique_id": _text(self.io.read(entry + "/unique_id"), 64),
                        "rx": rx, "tx": tx, "full_rate": (not below) if known else (False if below else None),
                        "below_full_rate": below})
            if len(out) >= MAX_LINKS:
                break
        return out

    # -- published layout ----------------------------------------------------------------------------------
    def layout(self):
        path = "/run/usb4stream-layout.json"
        info = self.io.stat(path)
        if info is None:
            return unavailable("no published layout (/run/usb4stream-layout.json missing)")
        raw = self.io.read(path, limit=MAX_FILE)
        if raw is None:
            return unavailable("published layout unreadable or larger than 1 MiB")
        try:
            value = _strict(raw)
        except (ValueError, RecursionError):
            return unavailable("published layout is not valid JSON")
        if not isinstance(value, dict):
            return unavailable("published layout is not an object")
        topology = value.get("topology") if isinstance(value.get("topology"), dict) else {}
        streams = []
        for key in sorted(value):
            match = STREAM_KEY.fullmatch(key)
            row = value[key]
            if not match or not isinstance(row, dict):
                continue
            config = row.get("config") if isinstance(row.get("config"), dict) else {}
            local = row.get("local") if isinstance(row.get("local"), dict) else {}
            index = row.get("index") if type(row.get("index")) is int and 0 <= row["index"] < 1000 else None
            streams.append({
                "id": key, "service": match[1], "stream": match[2], "index": index,
                "device": None if index is None else "/dev/tbstream%d" % index,
                "in_hopid": _int(row.get("in_hopid")), "out_hopid": _int(row.get("out_hopid")),
                "cable": _text(row.get("cable"), 8), "domain": _int(local.get("domain")),
                "usb4_port": _int(local.get("usb4_port")),
                "published": {"exists": _bool(row.get("exists")), "attached": _bool(row.get("attached")),
                              "openable": _bool(row.get("openable")),
                              "openable_reason": _text(row.get("openable_reason"), 300)},
                "config": {k: _scalar(config[k]) for k in CONFIG_KEYS if k in config}})
            if len(streams) >= MAX_STREAMS:
                break
        cables = []
        raw_cables = topology.get("cables") if isinstance(topology.get("cables"), list) else []
        for cable in raw_cables[:MAX_CABLES]:
            if not isinstance(cable, dict):
                continue
            local = cable.get("local") if isinstance(cable.get("local"), dict) else {}
            remote = cable.get("remote") if isinstance(cable.get("remote"), dict) else {}
            link = cable.get("link") if isinstance(cable.get("link"), dict) else {}
            names = cable.get("streams") if isinstance(cable.get("streams"), list) else []
            cables.append({"id": _text(cable.get("id"), 8), "state": _text(cable.get("state"), 32),
                           "domain": _int(local.get("domain")), "usb4_port": _int(local.get("usb4_port")),
                           "nhi": _text(local.get("pci"), 16), "peer": _text(remote.get("device_name"), 64),
                           "router": _text(remote.get("xdomain"), 24),
                           "gbps": link.get("gbps") if _number(link.get("gbps")) else None,
                           "full_speed": _bool(link.get("full_speed")),
                           "streams": [s for s in names if isinstance(s, str) and STREAM_KEY.fullmatch(s)][:16],
                           "streams_openable": _int(cable.get("streams_openable"))})
        warnings = topology.get("warnings") if isinstance(topology.get("warnings"), list) else []
        return {"status": "ok", "version": _scalar(value.get("version")),
                "generated_at": _text(topology.get("generated_at"), 40), "modified_at": info.st_mtime,
                "streams": streams, "cables": cables,
                "warnings": [w[:300] for w in warnings if isinstance(w, str)][:32]}

    def live_streams(self, streams):
        """Live cross-checks without opening anything: lstat of the device node and the service driver link.
        An observed absence is False; a failed inspection is None with its reason. Returns the reasons of the
        failed inspections (empty when every check observed something)."""
        failed = []
        for row in streams:
            self.check()
            live = {}
            if row["device"] is None:
                live["device_present"], live["device_why"] = None, "the layout gives no stream index"
                failed.append(row["id"] + ": no stream index")
            else:
                state, value = self.io.device(row["device"])
                if state == "error":
                    live["device_present"], live["device_why"] = None, "device check failed: " + str(value)
                    failed.append(row["id"] + ": " + live["device_why"])
                else:
                    live["device_present"] = state == "char"
                    if state == "other":
                        live["device_why"] = row["device"] + " exists but is not a character device"
            entry = "/sys/bus/thunderbolt/devices/" + row["service"]
            state, value = self.io.probe(entry)
            if state == "absent":
                live.update(attached_now=False, driver=None,
                            attached_why="service " + row["service"] + " not present (no peer on that port)")
            elif state == "error":
                live.update(attached_now=None, driver=None, attached_why="service check failed: " + str(value))
                failed.append(row["id"] + ": " + live["attached_why"])
            else:
                kind, name = self.io.link(entry + "/driver")
                if kind == "error":
                    live.update(attached_now=None, driver=None, attached_why="driver check failed: " + str(name))
                    failed.append(row["id"] + ": " + live["attached_why"])
                else:
                    live.update(driver=name, attached_now=name == "thunderbolt_stream")
                    if name != "thunderbolt_stream":
                        live["attached_why"] = ("no driver bound to the service" if name is None
                                                else "service bound to " + name)
            row["live"] = live
        return failed

    # -- module parameters -----------------------------------------------------------------------------------
    def parameters(self):
        out = {}
        for module in ("thunderbolt", "thunderbolt_stream"):
            base = "/sys/module/" + module + "/parameters"
            state, why = self.io.probe("/sys/module/" + module)
            if state != "present":
                out[module] = unavailable("module not loaded" if state == "absent" else "module check failed: " + str(why))
                continue
            values, write_only, unreadable = {}, [], []
            for name in self.io.listdir(base)[:MAX_PARAMS]:
                self.check()
                if not PARAM.fullmatch(name):
                    continue
                info = self.io.stat(base + "/" + name)
                if info is None or not stat.S_ISREG(info.st_mode):
                    continue
                if not info.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
                    write_only.append(name)            # never opened: writing such a parameter is an action
                    continue
                values[name] = self.io.read(base + "/" + name, limit=256)
                if values[name] is None:
                    unreadable.append(name)
            out[module] = {"status": "ok", "values": values, "write_only": write_only, "unreadable": unreadable}
        # A module that is not loaded is an observation too: the part itself is always observed.
        return {"status": "ok", "modules": out}

    # -- interrupts ------------------------------------------------------------------------------------------
    def interrupts(self, domains):
        text = self.io.read("/proc/interrupts", limit=MAX_FILE)
        if text is None:
            return unavailable("/proc/interrupts unreadable or larger than 1 MiB")
        by_pci = {pci: domain for domain, pci in domains.items() if pci}
        vectors = [dict(row, domain=by_pci.get(row["nhi"])) for row in interrupt_vectors(text)]
        return {"status": "ok", "vectors": vectors[:256],
                "note": "counts since boot per host-interface vector; busy-poll streams hold data rings without "
                        "interrupts and interrupts cannot be attributed to a single stream"}

    # -- kernel journal --------------------------------------------------------------------------------------
    def _journalctl(self, argv, timeout):
        """(returncode, [stdout lines]) with stdout and stderr merged, read up to the cap; the child is killed
        on the deadline or the cap. Raises OSError / TimeoutError / OverflowError."""
        env = {**os.environ, "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "", "PAGER": "", "LC_ALL": "C"}
        child = self.runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.child = child
        if self.expired.is_set():
            child.kill()
        chunks, size, over = [], [0], [False]

        def drain():
            while True:
                block = child.stdout.read(65536)
                if not block:
                    return
                size[0] += len(block)
                if size[0] > JOURNAL_STDOUT_CAP:
                    over[0] = True
                    return
                chunks.append(block)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive() or over[0]:
            child.kill()
            child.wait(2)
            reader.join(2)
            child.stdout.close()
            if over[0]:
                raise OverflowError()
            raise TimeoutError()
        try:
            code = child.wait(max(0.1, self.remaining()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(2)
            raise TimeoutError() from None
        finally:
            child.stdout.close()
            self.child = None
        return code, b"".join(chunks).splitlines()

    def journal(self, request, boot_id, lost=False):
        cursor = request.get("cursor")
        follow = not lost and cursor is not None and cursor_boot(cursor) == boot_id
        mode = "follow" if follow else "cursor_lost" if lost else "initial"
        argv = ["journalctl", "-k", "--boot=" + boot_id, "-o", "json",
                "--output-fields=MESSAGE,_SOURCE_MONOTONIC_TIMESTAMP", "--no-pager"]
        # Follow starts AT the stored entry (one more than the batch) so a silent reposition is detected.
        argv += ["--cursor=" + cursor, "-n", str(JOURNAL_FOLLOW + 1)] if follow else ["-n", str(JOURNAL_INITIAL)]
        timeout = min(JOURNAL_TIMEOUT_S, self.remaining())
        if timeout <= 0.2:
            return unavailable("node deadline reached before the kernel journal was read")
        try:
            code, lines = self._journalctl(argv, timeout)
        except FileNotFoundError:
            return unavailable("journalctl not installed")
        except TimeoutError:
            return unavailable("journalctl did not answer within %.1f s" % timeout)
        except OverflowError:
            return unavailable("journalctl output exceeds the read budget")
        except OSError as exc:
            return unavailable("journalctl failed to start: " + (exc.strerror or type(exc).__name__))
        counts = dict.fromkeys(KINDS, 0)
        recent, notes = [], []
        entries = malformed = foreign = 0
        last_cursor = first_ts = first_entry = None
        anchored = None
        for raw in lines:
            if not raw.startswith(b"{"):
                notes.append(raw[:300].decode("utf-8", "replace"))
                continue
            try:
                entry = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                malformed += 1
                continue
            if (not isinstance(entry, dict) or not isinstance(entry.get("__CURSOR"), str)
                    or not CURSOR.fullmatch(entry["__CURSOR"])):
                malformed += 1
                continue
            if cursor_boot(entry["__CURSOR"]) != boot_id:
                foreign += 1
                continue
            if follow and anchored is None:
                anchored = same_entry(entry["__CURSOR"], cursor)
                if anchored:
                    continue                # the stored entry itself: counted by the previous read
            entries += 1
            last_cursor = entry["__CURSOR"]
            ts = _usec(entry.get("__REALTIME_TIMESTAMP"))
            first_ts = ts if first_ts is None else first_ts
            message = entry.get("MESSAGE")
            if isinstance(message, list):      # non-UTF-8 journal fields arrive as byte arrays
                try:
                    message = bytes(message[:4096]).decode("utf-8", "replace")
                except (TypeError, ValueError):
                    message = None
            if first_entry is None:
                first_entry = (entry.get("_SOURCE_MONOTONIC_TIMESTAMP"), message)
            if not isinstance(message, str):
                malformed += 1                 # an entry without a readable message may hide evidence
                continue
            kind = classify(message)
            if kind is None:
                continue
            counts[kind] += 1
            recent.append({"ts": ts, "kind": kind, "message": message[:LINE_CHARS]})
            del recent[:-RECENT_LINES]
        text = " ".join(notes).lower()
        if code:
            if follow and "cursor" in text:
                return self.journal(request, boot_id, lost=True)
            return unavailable("journalctl failed (exit %d): %s" % (code, " ".join(notes)[:200]))
        if any(marker in text for marker in UNREADABLE):
            return unavailable("kernel journal not readable by this user: " + " ".join(notes)[:200])
        if foreign:
            return unavailable("journalctl returned %d entries of another boot; the stored cursor is kept" % foreign)
        if follow and not anchored:
            # The stored entry is gone (rotation or vacuum): the journal positioned at a neighbour instead.
            return self.journal(request, boot_id, lost=True)
        if not follow and entries == 0:
            return unavailable("kernel journal not readable by this user (no kernel entries visible for this boot)")
        reasons = []
        if not follow:
            if entries >= JOURNAL_INITIAL:
                reasons.append("this boot has more than %d kernel entries: the oldest were not scanned" % JOURNAL_INITIAL)
            elif not boot_start(first_entry):
                reasons.append("the oldest kernel entry held for this boot is not the first message of the boot "
                               "(older entries rotated out)")
        if malformed:
            reasons.append("%d malformed journal entries were skipped" % malformed)
        if notes:
            reasons.append("journalctl reported: " + " | ".join(notes)[:200])
        return {"status": "ok", "boot_id": boot_id, "mode": mode,
                "cursor": last_cursor or (cursor if follow else None), "entries": entries,
                "more": follow and entries >= JOURNAL_FOLLOW, "complete": not reasons,
                "incomplete_why": "; ".join(reasons) or None, "malformed": malformed,
                "scanned_from": first_ts, "counts": counts, "matched": sum(counts.values()), "recent": recent}

    # -- verb ------------------------------------------------------------------------------------------------
    def _collect(self, payload, result):
        boot_id = self.boot_id()
        result["boot_id"] = boot_id
        if boot_id is None:
            result["why"]["boot_id"] = "boot id unreadable"
        domains = self.domains()
        result["domains"] = {str(k): v for k, v in sorted(domains.items())}
        parts = (("links", lambda: self.links(domains)), ("layout", self.layout),
                 ("parameters", self.parameters), ("interrupts", lambda: self.interrupts(domains)))
        for name, part in parts:
            try:
                self.check()
                result[name] = part()
            except Deadline:
                result[name] = unavailable("node deadline reached")
        if isinstance(result["links"], list) and not result["links"]:
            result["why"]["links"] = ("thunderbolt driver not loaded" if self.io.stat("/sys/bus/thunderbolt") is None
                                      else "no USB4 link reports a trained speed (no cable or no peer)")
        if result["layout"].get("status") == "ok":
            try:
                failed = self.live_streams(result["layout"]["streams"])
                result["layout"]["live"] = ({"status": "ok"} if not failed else
                                            unavailable("%d live checks failed: %s" % (len(failed), "; ".join(failed)[:300])))
            except Deadline:
                result["layout"]["live"] = unavailable("node deadline reached before the live checks")
        request = payload.get("journal")
        if request is None:
            result["journal"] = {"status": "skipped", "why": "not requested in this call"}
        elif boot_id is None:
            result["journal"] = unavailable("boot id unreadable: a cursor cannot be tied to a boot")
        else:
            result["journal"] = self.journal(request, boot_id)

    def collect(self, payload):
        """Run the collection under the overall deadline: parts not finished in time are unavailable and a
        journal child still running is killed."""
        result = {"schema": SCHEMA, "verb": "usb4", "collected_at": self.io.clock(), "kernel": platform.release(),
                  "full_rate": {"gbps_per_lane": FULL_LANE_GBPS, "lanes": FULL_LANES}, "why": {}}
        failure = []

        def work():
            try:
                self._collect(payload, result)
            except Exception as exc:       # reported, never raised past the verb
                failure.append(f"{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=work, daemon=True, name="usb4-collect")
        worker.start()
        worker.join(self.deadline_s + 0.5)
        if worker.is_alive():
            self.expired.set()
            child = self.child
            if child is not None:
                child.kill()
            worker.join(1.0)
            snapshot = json.loads(json.dumps(dict(result), default=str))
            for name in ("links", "layout", "parameters", "interrupts", "journal"):
                snapshot.setdefault(name, unavailable("node deadline reached"))
            snapshot["why"] = dict(snapshot.get("why") or {}, deadline="collection exceeded %g s" % self.deadline_s)
            return cap(snapshot)
        if failure:
            return {"schema": SCHEMA, "verb": "usb4", "error": "USB4 collection failed: " + failure[0][:300]}
        return cap(result)


def cap(result):
    """Keep the encoded result within MAX_OUTPUT by trimming the long lists (and saying so)."""
    def size():
        return len(json.dumps(result, ensure_ascii=True, allow_nan=False))
    if size() <= MAX_OUTPUT:
        return result
    result["truncated"] = True
    journal = result.get("journal") if isinstance(result.get("journal"), dict) else {}
    layout = result.get("layout") if isinstance(result.get("layout"), dict) else {}
    interrupts = result.get("interrupts") if isinstance(result.get("interrupts"), dict) else {}
    for owner, key in ((journal, "recent"), (layout, "warnings"), (layout, "streams"), (interrupts, "vectors"),
                       (result, "links")):
        while isinstance(owner.get(key), list) and owner[key] and size() > MAX_OUTPUT:
            owner[key] = owner[key][: len(owner[key]) // 2]
    if size() > MAX_OUTPUT:
        return {"schema": SCHEMA, "verb": "usb4", "error": "USB4 report exceeds the output limit"}
    return result


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - {"journal"}:
        raise ValueError("payload accepts only journal")
    request = payload.get("journal")
    if request is None:
        return {}
    if not isinstance(request, dict) or set(request) - {"boot_id", "cursor"}:
        raise ValueError("journal accepts only boot_id and cursor")
    for key, pattern in (("boot_id", BOOT_ID), ("cursor", CURSOR)):
        value = request.get(key)
        if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
            raise ValueError("journal " + key + " is malformed")
    return {"journal": {"boot_id": request.get("boot_id"), "cursor": request.get("cursor")}}


def run(verb, payload, reader=None):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return Collector(reader).collect(validate(payload))
