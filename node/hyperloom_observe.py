"""Read-only host telemetry: CPU and sensors, memory domains, network, disks, GPU processes, listeners and the
read-only engine status path for loopback engines.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).

Verb ``host``. Payload (every key optional, nothing else accepted):
    cpu            {"boot_id": str, "total": int, "idle": int}: the counters the previous answer returned; busy % is
                   then the delta against them (no sleep). Without it, two /proc/stat reads 200 ms apart.
    engine_status  {"port": 1024..65535, "paths": [...]} or a list of at most 4 of them; paths from
                   /health, /api/status, /api/status?detail=1, /metrics. Each is a GET to this node's IPv4 loopback address on <port>, nothing else.

Every section is {"status": "ok", "value": ...} or {"status": "unavailable", "value": null, "why": ...}. Reads are
bounded (a 5 s deadline between sections, 1 MiB per file, scan budgets, 256 KiB of output) and never follow a path
from the payload; nothing is written, no device is opened, no process is started.
"""

import ipaddress
import json
import os
import re
import socket
import time

VERBS = {"host"}

DEADLINE_S = 5.0
MAX_FILE = 1024 * 1024
MAX_OUTPUT = 256 * 1024
MAX_HWMON = 64
MAX_CARDS = 16
MAX_MOUNTS = 32
MAX_KFD = 256
MAX_TCP_LINES = 4096
MAX_ENGINES = 4
READ_BUDGET = 8 * 1024 * 1024
FILE_BUDGET = 4096
CHUNK = 64 * 1024
LOOPBACK = str(ipaddress.IPv4Address(socket.INADDR_LOOPBACK))
ENGINE_PATHS = ("/health", "/api/status", "/api/status?detail=1", "/metrics")
ENGINE_TIMEOUT_S = 2.0
ENGINE_MAX_BYTES = 1024 * 1024
REAL_FS = frozenset(("ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "f2fs", "zfs", "ntfs", "ntfs3"))
WHOLE_DISK = re.compile(r"(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)")


def ok(value):
    return {"status": "ok", "value": value}


def unavailable(why):
    return {"status": "unavailable", "value": None, "why": why}


class Reader:
    """Bounded reads under fixed roots (tests pass a fixture root; the node always reads /).

    Every opened file must resolve under <root>/sys or <root>/proc (sysfs class entries are links into
    /sys/devices; nothing may lead outside). One call reads at most READ_BUDGET bytes from at most FILE_BUDGET
    files; scans check the deadline per entry."""

    def __init__(self, root="/", deadline_s=DEADLINE_S, clock=time.monotonic):
        self.root = os.path.abspath(root)
        self.clock = clock
        self.started = clock()
        self.wall_started = time.time()
        self.deadline = self.started + deadline_s
        self.allowed = tuple(os.path.realpath(self.path(top)) for top in ("/sys", "/proc"))
        self.bytes = 0
        self.files = 0

    def late(self):
        return self.clock() >= self.deadline

    def remaining(self):
        return self.deadline - self.clock()

    def path(self, logical):
        return logical if self.root == os.path.abspath("/") else os.path.join(self.root, logical.lstrip("/"))

    def _contained(self, physical):
        real = os.path.realpath(physical)
        return any(real == top or real.startswith(top.rstrip(os.sep) + os.sep) for top in self.allowed)

    def _spend(self):
        self.files += 1
        if self.files > FILE_BUDGET:
            raise ValueError("file budget of this call exhausted")
        if self.late():
            raise ValueError("node deadline reached")

    def text(self, logical, limit=MAX_FILE):
        self._spend()
        physical = self.path(logical)
        if not self._contained(physical):
            raise ValueError("outside the readable roots")
        limit = min(limit, READ_BUDGET - self.bytes)
        with open(physical, "rb") as stream:
            raw = stream.read(limit + 1)
        self.bytes += len(raw)
        if len(raw) > limit:
            raise ValueError("file exceeds the read limit")
        return raw.decode("utf-8", "replace")

    def value(self, logical):
        try:
            return self.text(logical, 4096).strip()
        except (OSError, ValueError):
            return None

    def integer(self, logical):
        value = self.value(logical)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def listdir(self, logical, limit):
        try:
            self._spend()
            physical = self.path(logical)
            if not self._contained(physical):
                return None
            names = sorted(os.listdir(physical))
        except (OSError, ValueError):
            return None
        return names[:limit]

    def exists(self, logical):
        physical = self.path(logical)
        return os.path.lexists(physical) and self._contained(os.path.dirname(physical))

    def link_name(self, logical):
        try:
            return os.path.basename(os.readlink(self.path(logical)))
        except OSError:
            return None

    def owner(self, logical):
        try:
            physical = self.path(logical)
            return os.stat(physical).st_uid if self._contained(physical) else None
        except OSError:
            return None


def _natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def boot_id(reader):
    value = reader.value("/proc/sys/kernel/random/boot_id")
    return ok(value) if value and re.fullmatch(r"[0-9a-f-]{8,64}", value) else unavailable("boot id not readable")


def uptime(reader):
    value = reader.value("/proc/uptime")
    try:
        return ok(float(value.split()[0]))
    except (AttributeError, IndexError, ValueError):
        return unavailable("uptime not readable")


def load(reader):
    value = reader.value("/proc/loadavg")
    try:
        numbers = [float(v) for v in value.split()[:3]]
        if len(numbers) != 3:
            raise ValueError
        return ok(numbers)
    except (AttributeError, ValueError):
        return unavailable("loadavg not readable")


def _stat(reader):
    text = reader.text("/proc/stat", 256 * 1024)
    total = idle = None
    cores = 0
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu":
            values = [int(v) for v in fields[1:9]]
            if len(values) < 4:
                raise ValueError("short cpu line")
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
        elif re.fullmatch(r"cpu\d+", fields[0]):
            cores += 1
    if total is None:
        raise ValueError("cpu line missing")
    return total, idle, cores


def cpu(reader, baseline, current_boot, sleep=time.sleep):
    try:
        total, idle, cores = _stat(reader)
        if (isinstance(baseline, dict) and baseline.get("boot_id") == current_boot and current_boot
                and total > baseline["total"] and idle >= baseline["idle"]):
            before_total, before_idle = baseline["total"], baseline["idle"]
        else:
            before_total, before_idle = total, idle
            sleep(0.2)
            total, idle, cores = _stat(reader)
        span = total - before_total
        busy = None if span <= 0 else max(0.0, min(100.0, 100.0 * (span - (idle - before_idle)) / span))
        value = {"busy_percent": busy, "total": total, "idle": idle, "cores": cores}
        if busy is None:
            value["why"] = {"busy_percent": "no CPU time elapsed between the two reads"}
        return ok(value)
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable("/proc/stat not readable")


MEMINFO = ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree", "Shmem")


def memory(reader):
    try:
        text = reader.text("/proc/meminfo", 64 * 1024)
    except (OSError, ValueError):
        return unavailable("/proc/meminfo not readable")
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(\w+):\s+(\d+)\s+kB", line.strip())
        if match and match[1] in MEMINFO:
            values[match[1]] = int(match[2]) * 1024
    return ok(values) if "MemTotal" in values else unavailable("MemTotal missing from meminfo")


def _chip(reader, base):
    """One hwmon directory: name, temperatures, power and frequency inputs with their labels."""
    names = reader.listdir(base, 512) or []
    chip = {"hwmon": os.path.basename(base), "name": reader.value(base + "/name"),
            "device": reader.link_name(base + "/device"), "temps": [], "power": [], "freq": []}
    for name in names:
        match = re.fullmatch(r"(temp|power|freq)(\d+)_(input|average|cap)", name)
        if not match:
            continue
        kind, index, field = match.groups()
        if kind == "temp" and field != "input" or kind == "freq" and field != "input":
            continue
        raw = reader.integer(base + "/" + name)
        if raw is None:
            continue
        label = reader.value(f"{base}/{kind}{index}_label") or f"{kind}{index}"
        if kind == "temp":
            chip["temps"].append({"sensor": label, "input": f"temp{index}", "celsius": raw / 1000})
        elif kind == "power":
            chip["power"].append({"sensor": label, "input": f"power{index}", "field": field, "watts": raw / 1e6})
        else:
            chip["freq"].append({"sensor": label, "mhz": raw / 1e6})
    return chip


def sensors(reader):
    names = reader.listdir("/sys/class/hwmon", MAX_HWMON)
    if names is None:
        return unavailable("/sys/class/hwmon not readable")
    chips = [_chip(reader, "/sys/class/hwmon/" + name) for name in sorted(names, key=_natural)
             if re.fullmatch(r"hwmon\d+", name)]
    return ok(chips) if chips else unavailable("no hwmon sensors")


def _sclk(reader, device):
    text = reader.value(device + "/pp_dpm_sclk")
    for line in (text or "").splitlines():
        match = re.fullmatch(r"\s*\d+:\s*(\d+)\s*[Mm]hz\s*\*\s*", line)
        if match:
            return int(match[1])
    return None


def gpus(reader):
    names = reader.listdir("/sys/class/drm", 256)
    if names is None:
        return unavailable("/sys/class/drm not readable")
    cards = []
    for name in sorted((n for n in names if re.fullmatch(r"card\d+", n)), key=_natural)[:MAX_CARDS]:
        device = f"/sys/class/drm/{name}/device"
        if reader.value(device + "/vendor") != "0x1002":
            continue
        row = {"card": name, "busy_percent": reader.integer(device + "/gpu_busy_percent")}
        for key, filename in (("vram_used", "mem_info_vram_used"), ("vram_total", "mem_info_vram_total"),
                              ("vis_vram_used", "mem_info_vis_vram_used"), ("vis_vram_total", "mem_info_vis_vram_total"),
                              ("gtt_used", "mem_info_gtt_used"), ("gtt_total", "mem_info_gtt_total")):
            row[key + "_bytes"] = reader.integer(device + "/" + filename)
        hwmons = [n for n in (reader.listdir(device + "/hwmon", 8) or []) if re.fullmatch(r"hwmon\d+", n)]
        chip = _chip(reader, device + "/hwmon/" + hwmons[0]) if hwmons else None
        row["hwmon"] = chip["hwmon"] if chip else None
        row["temps"] = chip["temps"] if chip else []
        power = [p for p in (chip or {}).get("power", []) if p["field"] in ("average", "input")]
        average = [p for p in power if p["field"] == "average"]
        chosen = (average or power or [None])[0]
        row["power_w"] = chosen["watts"] if chosen else None
        row["power_sensor"] = chosen["sensor"] if chosen else None
        row["power_field"] = chosen["field"] if chosen else None
        caps = [p for p in (chip or {}).get("power", []) if p["field"] == "cap"]
        row["power_cap_w"] = caps[0]["watts"] if caps else None
        sclk = _sclk(reader, device)
        if sclk is None:
            freq = [f for f in (chip or {}).get("freq", []) if f["sensor"].lower() == "sclk"]
            sclk = freq[0]["mhz"] if freq else None
        row["sclk_mhz"] = sclk
        row["why"] = {key: "not reported by this card" for key, value in row.items() if value is None}
        if row["power_cap_w"] is None:
            row["why"]["power_cap_w"] = "no readable power cap for this card"
        cards.append(row)
    return ok(cards) if cards else unavailable("no AMD GPU in /sys/class/drm")


def _unescape(path):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), path)


def disks(reader, statvfs=None):
    try:
        text = reader.text("/proc/self/mounts", 256 * 1024)
    except (OSError, ValueError):
        return unavailable("/proc/self/mounts not readable")
    rows, seen = [], set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[2] not in REAL_FS or not fields[0].startswith("/dev/"):
            continue
        if fields[0] in seen or len(rows) >= MAX_MOUNTS:
            continue
        seen.add(fields[0])
        mount = _unescape(fields[1])
        try:
            st = (statvfs or os.statvfs)(reader.path(mount))
        except OSError:
            rows.append({"mount": mount, "fstype": fields[2], "total_bytes": None, "used_bytes": None,
                         "free_bytes": None, "why": "statvfs failed"})
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        rows.append({"mount": mount, "fstype": fields[2], "total_bytes": total, "used_bytes": used, "free_bytes": free})
    return ok(rows) if rows else unavailable("no real filesystems mounted")


def diskstats(reader):
    try:
        text = reader.text("/proc/diskstats", 256 * 1024)
    except (OSError, ValueError):
        return unavailable("/proc/diskstats not readable")
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 10 and WHOLE_DISK.fullmatch(fields[2]):
            try:
                rows.append({"device": fields[2], "dev": f"{int(fields[0])}:{int(fields[1])}",
                             "read_bytes": int(fields[5]) * 512, "write_bytes": int(fields[9]) * 512})
            except ValueError:
                continue
    return ok(rows[:64]) if rows else unavailable("no whole-disk counters in /proc/diskstats")


def network(reader):
    try:
        text = reader.text("/proc/net/dev", 256 * 1024)
    except (OSError, ValueError):
        return unavailable("/proc/net/dev not readable"), unavailable("/proc/net/dev not readable")
    rows, nics = [], []
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        fields = data.split()
        if name == "lo" or len(fields) < 9 or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,32}", name):
            continue
        physical = reader.exists(f"/sys/class/net/{name}/device")
        try:
            rows.append({"iface": name, "ifindex": reader.integer(f"/sys/class/net/{name}/ifindex"),
                         "rx_bytes": int(fields[0]), "tx_bytes": int(fields[8]), "physical": physical,
                         "operstate": reader.value(f"/sys/class/net/{name}/operstate")})
        except ValueError:
            continue
        if physical:
            mac = reader.value(f"/sys/class/net/{name}/address")
            if mac and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
                nics.append({"iface": name, "mac": mac})
        if len(rows) >= 64:
            break
    return (ok(rows) if rows else unavailable("no interfaces besides loopback"),
            ok(nics) if nics else unavailable("no physical NIC with a MAC address"))


def gpu_processes(reader, uid=None):
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    names = reader.listdir("/sys/class/kfd/kfd/proc", MAX_KFD)
    if names is None:
        return unavailable("/sys/class/kfd/kfd/proc not readable")
    rows = []
    for name in names:
        if not name.isdigit():
            continue
        base = "/sys/class/kfd/kfd/proc/" + name
        vram = [reader.integer(base + "/" + f) for f in (reader.listdir(base, 64) or []) if re.fullmatch(r"vram_\d+", f)]
        own = reader.owner("/proc/" + name) == uid and uid is not None
        row = {"pid": int(name), "own": own, "comm": reader.value(f"/proc/{name}/comm") if own else None,
               "vram_bytes": sum(v for v in vram if v is not None) if any(v is not None for v in vram) else None}
        if not own:
            row["why"] = {"comm": "process of another user"}
        rows.append(row)
    return ok(rows)


def _scope(address_hex, family):
    if family == "ipv4":
        raw = bytes.fromhex(address_hex)[::-1]
        if raw == b"\0\0\0\0":
            return "any"
        return "loopback" if raw[0] == 127 else "lan"
    raw = bytes.fromhex(address_hex)
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    if words == bytes(16):
        return "any"
    if words == bytes(15) + b"\1":
        return "loopback"
    if words[:12] == bytes(10) + b"\xff\xff":
        return "loopback" if words[12] == 127 else "any" if words[12:] == bytes(4) else "lan"
    return "lan"


def listeners(reader):
    found, readable = set(), False
    for name, family in (("tcp", "ipv4"), ("tcp6", "ipv6")):
        try:
            text = reader.text("/proc/net/" + name, MAX_FILE)
        except (OSError, ValueError):
            continue
        readable = True
        for line in text.splitlines()[1:MAX_TCP_LINES + 1]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                address, port = fields[1].split(":")
                found.add((int(port, 16), family, _scope(address, family)))
            except ValueError:
                continue
    if not readable:
        return unavailable("/proc/net/tcp not readable")
    return ok([{"port": p, "family": f, "scope": s} for p, f, s in sorted(found)])


def _engine_targets(value):
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if not items or len(items) > MAX_ENGINES:
        raise ValueError(f"engine_status takes one object or a list of 1..{MAX_ENGINES}")
    targets = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"port", "paths"} or "port" not in item:
            raise ValueError("engine_status entries are {port, paths}")
        port, paths = item["port"], item.get("paths", list(ENGINE_PATHS))
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("engine_status port must be an integer in 1024..65535")
        if (not isinstance(paths, list) or not paths or len(paths) > len(ENGINE_PATHS)
                or any(path not in ENGINE_PATHS for path in paths) or len(set(paths)) != len(paths)):
            raise ValueError("engine_status paths must be a subset of " + ", ".join(ENGINE_PATHS))
        targets.append((port, paths))
    if len({port for port, _ in targets}) != len(targets):
        raise ValueError("engine_status ports must be distinct")
    return targets


MAX_HEADER = 64 * 1024
CRLF_TEXT = "\r\n"
CRLF = b"\r\n"


def _dechunk(body):
    out, rest = bytearray(), body
    while True:
        line, sep, rest = rest.partition(CRLF)
        if not sep:
            raise ValueError("truncated chunked body")
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            return bytes(out)
        if len(rest) < size + 2:
            raise ValueError("truncated chunked body")
        out += rest[:size]
        rest = rest[size + 2:]


def _fetch(port, path, reader, connect=socket.create_connection):
    """GET one fixed path on this node's IPv4 loopback address. Every connect, send and receive is bounded by the
    absolute deadline (2 s per path and the call's own deadline), so a peer that trickles bytes is cut off at the
    deadline; at most 1 MiB of body; no redirect is followed (a 3xx is an outcome); no proxy is involved."""
    stop = min(reader.deadline, reader.clock() + ENGINE_TIMEOUT_S)

    def left():
        return stop - reader.clock()

    if left() <= 0.05:
        return unavailable("node deadline reached")
    try:
        with connect((LOOPBACK, port), timeout=left()) as sock:
            request = (f"GET {path} HTTP/1.1" + CRLF_TEXT + f"Host: {LOOPBACK}:{port}" + CRLF_TEXT + "Accept: */*" + CRLF_TEXT
                       + "Connection: close" + CRLF_TEXT + CRLF_TEXT).encode("ascii")
            sock.settimeout(max(0.01, left()))
            sock.sendall(request)
            data = bytearray()
            while True:
                remaining = left()
                if remaining <= 0:
                    return unavailable("engine response not complete within its deadline")
                sock.settimeout(remaining)
                chunk = sock.recv(CHUNK)
                if not chunk:
                    break
                data += chunk
                if len(data) > ENGINE_MAX_BYTES + MAX_HEADER:
                    return unavailable("response exceeds 1 MiB")
    except socket.timeout:
        return unavailable("engine response not complete within its deadline")
    except OSError as exc:
        return unavailable("engine not reachable on loopback: " + type(exc).__name__)
    head, sep, body = bytes(data).partition(CRLF + CRLF)
    if not sep or len(head) > MAX_HEADER:
        return unavailable("malformed HTTP response")
    lines = head.decode("latin-1").split(CRLF_TEXT)
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        return unavailable("malformed HTTP status line")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if not 200 <= status < 300:
        return unavailable(f"HTTP {status}" + (" (redirects are not followed)" if 300 <= status < 400 else ""))
    try:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = _dechunk(body)
        elif headers.get("content-length", "").isdigit() and len(body) < int(headers["content-length"]):
            return unavailable("engine response truncated")
    except ValueError as exc:
        return unavailable(str(exc))
    if len(body) > ENGINE_MAX_BYTES:
        return unavailable("response exceeds 1 MiB")
    return ok({"http_status": status, "content_type": headers.get("content-type", "")[:100],
               "body": body.decode("utf-8", "replace")})


def engine_status(reader, targets, connect=socket.create_connection):
    rows = []
    for port, paths in targets:
        # The node's wall clock when this call began: with the timings below the control machine places each answer
        # on one clock across samples.
        row = {"port": port, "wall_s": round(reader.wall_started, 6), "paths": {}}
        for path in paths:
            started = reader.clock()
            result = unavailable("node deadline reached") if reader.late() else _fetch(port, path, reader, connect)
            # When the request was sent and its answer complete, in seconds since the call began: the control
            # machine uses them to check that the reads around a counter came from one engine process.
            result["timing"] = {"start_s": round(started - reader.started, 6), "end_s": round(reader.clock() - reader.started, 6)}
            row["paths"][path] = result
        rows.append(row)
    return ok(rows)


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False))


def _trim(result):
    """Keep the answer within the node output budget: drop engine bodies, largest first."""
    if _size(result) <= MAX_OUTPUT:
        return result
    engines = result["checks"].get("engine_status", {}).get("value") or []
    bodies = sorted(((len(entry["value"]["body"]), row, path) for row in engines for path, entry in row["paths"].items()
                     if entry.get("status") == "ok"), key=lambda item: item[0], reverse=True)
    for _, row, path in bodies:
        row["paths"][path] = unavailable("response exceeds the node output budget")
        if _size(result) <= MAX_OUTPUT:
            return result
    raise ValueError("host output exceeds the node output budget")


def collect(payload, root="/", clock=time.monotonic, sleep=time.sleep, statvfs=None, connect=socket.create_connection, uid=None):
    if not isinstance(payload, dict) or set(payload) - {"cpu", "engine_status"}:
        raise ValueError("host payload takes only cpu and engine_status")
    baseline = payload.get("cpu")
    if baseline is not None and (not isinstance(baseline, dict) or set(baseline) != {"boot_id", "total", "idle"}
                                 or not isinstance(baseline["boot_id"], str)
                                 or any(type(baseline[k]) is not int or baseline[k] < 0 for k in ("total", "idle"))):
        raise ValueError("cpu baseline must be {boot_id, total, idle} from a previous answer")
    targets = _engine_targets(payload.get("engine_status"))
    reader = Reader(root, clock=clock)
    checks = {}
    late = unavailable("node deadline reached")

    def section(name, function):
        if reader.late():
            checks[name] = late
            return
        try:
            checks[name] = function()
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            checks[name] = unavailable(f"{name} unreadable: {type(exc).__name__}")

    section("boot_id", lambda: boot_id(reader))
    current_boot = checks["boot_id"].get("value")
    section("uptime_s", lambda: uptime(reader))
    section("load", lambda: load(reader))
    section("cpu", lambda: cpu(reader, baseline, current_boot, sleep))
    section("memory", lambda: memory(reader))
    section("gpus", lambda: gpus(reader))
    section("sensors", lambda: sensors(reader))
    section("disks", lambda: disks(reader, statvfs))
    section("diskstats", lambda: diskstats(reader))
    if reader.late():
        checks["net"] = checks["nics"] = late
    else:
        checks["net"], checks["nics"] = network(reader)
    section("gpu_processes", lambda: gpu_processes(reader, uid))
    section("listeners", lambda: listeners(reader))
    if targets:
        section("engine_status", lambda: engine_status(reader, targets, connect))
    return _trim({"schema": "hyperloom/node-v1", "verb": "host", "checks": checks})


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return collect(payload)
