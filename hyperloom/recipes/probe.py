"""Hardware facts for recipes: devices, host RAM, disk read bandwidth and links, as the recipe-v1 probe report.

Every fact carries where it came from (``probed``, ``cache``, ``unavailable``); a fact that could not be measured is
never invented. Disk reads are explicit operator actions (``measure=True``); otherwise the probe cache answers.
"""
from __future__ import annotations

import csv
import json
import mmap
import os
from pathlib import Path
import subprocess
import time

from ..winproc import no_window

GIB = 1024 ** 3
MIB = 1024 ** 2
DISK_PROBE_MAX_BYTES = 1024 * MIB
DISK_PROBE_DEFAULT_BYTES = 256 * MIB
DISK_PROBE_MAX_S = 2.0
DISK_CHUNK = 8 * MIB
CACHE_TTL_S = 7 * 24 * 3600
SMI_TIMEOUT_S = 20


class ProbeError(RuntimeError):
    pass


def fact(value, source, why=None):
    out = {"value": value, "source": source}
    if why:
        out["why"] = why
    return out


def missing(why):
    return fact(None, "unavailable", why)


# --- control machine ---------------------------------------------------------------------------------------------

def nvidia_gpus(run=subprocess.run, *, placement=False, topology_reader=None):
    """Passive identity and PCI capabilities. No context, transfer benchmark, or file measurement is created."""
    if not placement:
        return _legacy_nvidia_gpus(run)
    from . import topology
    topology_reader = topology_reader or topology.collect
    base = "uuid,name,memory.total,memory.used"
    identity = base + ",pci.bus_id,driver_version"
    for fields in (identity + ",compute_cap,driver_model.current", identity, base):
        try:
            out = run(["nvidia-smi", "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
                      capture_output=True, text=True, timeout=SMI_TIMEOUT_S, **no_window())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"nvidia-smi unavailable: {exc}") from exc
        if out.returncode == 0:
            break
    else:
        raise ProbeError("nvidia-smi failed: " + (out.stderr or out.stdout).strip()[:300])
    gpus = []
    for cells in csv.reader(out.stdout.splitlines(), skipinitialspace=True):
        cells = [c.strip() for c in cells]
        if len(cells) not in (4, 6, 8) or not cells[2].isdigit() or not cells[3].isdigit():
            continue
        row = {"uuid": cells[0], "name": " ".join(cells[1].split()), "memory_gib": int(cells[2]) / 1024,
               "used_gib": int(cells[3]) / 1024, "memory_bytes": int(cells[2]) * MIB,
               "pci": "unknown", "arch": "unknown", "driver": "unknown",
               "platform": "unknown" if os.name == "nt" else "linux"}
        if len(cells) >= 6:
            row["pci"] = topology.pci(cells[4])
            row["driver"] = cells[5] if cells[5] and cells[5] != "N/A" else "unknown"
        if len(cells) == 8:
            cc = cells[6].split(".")
            if len(cc) == 2 and all(c.isdigit() for c in cc):
                row["arch"] = "sm_" + str(int(cc[0])) + str(int(cc[1]))
            if os.name == "nt" and cells[7] in ("WDDM", "TCC"):
                row["platform"] = "windows-" + cells[7].lower()
        row.update(topology_reader(row["pci"]))
        gpus.append(row)
    return gpus


def _legacy_nvidia_gpus(run=subprocess.run):
    """[{uuid, name, memory_gib, used_gib}] for every NVIDIA GPU of this machine (nvidia-smi, passive)."""
    try:
        out = run(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=SMI_TIMEOUT_S, **no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"nvidia-smi unavailable: {exc}") from exc
    if out.returncode != 0:
        raise ProbeError("nvidia-smi failed: " + (out.stderr or out.stdout).strip()[:300])
    gpus = []
    for line in out.stdout.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) == 4 and cells[2].isdigit() and cells[3].isdigit():
            gpus.append({"uuid": cells[0], "name": cells[1], "memory_gib": int(cells[2]) / 1024,
                         "used_gib": int(cells[3]) / 1024})
    return gpus


def host_memory():
    """Total and free physical RAM of this machine, GiB (free = what pinned host buffers can still take)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Status(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
                        ("total_page", ctypes.c_uint64), ("avail_page", ctypes.c_uint64),
                        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
                        ("avail_extended", ctypes.c_uint64)]
        status = Status()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError()
        return {"total_gib": status.total_phys / GIB, "free_gib": status.avail_phys / GIB,
                "free_commit_gib": status.avail_page / GIB}
    info = {}
    with open("/proc/meminfo", encoding="ascii") as stream:
        for line in stream:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
    return {"total_gib": info["MemTotal"] / GIB, "free_gib": info["MemAvailable"] / GIB,
            "free_commit_gib": (info["CommitLimit"] - info["Committed_AS"]) / GIB}


# --- disk read bandwidth -----------------------------------------------------------------------------------------

def _offsets(size, chunk, count):
    """``count`` consecutive chunk-aligned offsets (a sequential run) starting near the middle of the file, shifted
    back so the run stays inside it; fewer when the file is smaller."""
    chunks = size // chunk
    if chunks <= 0:
        return []
    count = min(count, chunks)
    start = min(chunks // 2, chunks - count)
    return [(start + i) * chunk for i in range(count)]


def _read_windows_direct(path, offsets, chunk, deadline):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.VirtualAlloc.restype = ctypes.c_void_p
    kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    kernel32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_int64, ctypes.c_void_p, wintypes.DWORD]
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    GENERIC_READ, SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    NO_BUFFERING, SEQUENTIAL = 0x20000000, 0x08000000
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, SHARE_READ, None, OPEN_EXISTING,
                                  NO_BUFFERING | SEQUENTIAL, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW(FILE_FLAG_NO_BUFFERING) failed")
    buffer = kernel32.VirtualAlloc(None, chunk, 0x3000, 0x04)  # MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE
    try:
        if not buffer:
            raise OSError(ctypes.get_last_error(), "VirtualAlloc failed")
        total, start = 0, time.perf_counter()
        for offset in offsets:
            if not kernel32.SetFilePointerEx(handle, offset, None, 0):
                raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
            got = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buffer, chunk, ctypes.byref(got), None):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            total += got.value
            if time.perf_counter() >= deadline:
                break
        return total, time.perf_counter() - start
    finally:
        if buffer:
            kernel32.VirtualFree(buffer, 0, 0x8000)  # MEM_RELEASE
        kernel32.CloseHandle(handle)


def _read_posix(path, offsets, chunk, deadline, direct):
    flags = os.O_RDONLY | (getattr(os, "O_DIRECT", 0) if direct else 0)
    fd = os.open(path, flags)
    try:
        if not direct and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        buffer = mmap.mmap(-1, chunk)  # page-aligned, as O_DIRECT requires
        try:
            total, start = 0, time.perf_counter()
            for offset in offsets:
                total += os.preadv(fd, [buffer], offset)
                if time.perf_counter() >= deadline:
                    break
            return total, time.perf_counter() - start
        finally:
            buffer.close()
    finally:
        os.close(fd)


def read_bandwidth(path, max_bytes=DISK_PROBE_DEFAULT_BYTES, max_s=DISK_PROBE_MAX_S):
    """Sequential-chunk read bandwidth of the disk holding ``path``: GB/s (10^9 B/s) with the method used.
    ``direct`` bypasses the OS cache; ``buffered`` (the file system refused direct I/O) may be served from cache."""
    if not 0 < max_bytes <= DISK_PROBE_MAX_BYTES:
        raise ProbeError(f"disk probe size must be 1..{DISK_PROBE_MAX_BYTES // MIB} MiB")
    size = os.stat(path).st_size
    count = max(1, max_bytes // DISK_CHUNK)
    offsets = _offsets(size, DISK_CHUNK, count)
    if not offsets:
        raise ProbeError(f"{path}: smaller than one {DISK_CHUNK // MIB} MiB probe chunk")
    deadline = time.perf_counter() + max_s
    method = "direct"
    try:
        if os.name == "nt":
            total, seconds = _read_windows_direct(path, offsets, DISK_CHUNK, deadline)
        else:
            total, seconds = _read_posix(path, offsets, DISK_CHUNK, deadline, direct=True)
    except OSError:
        if os.name == "nt":
            raise
        method = "buffered"
        total, seconds = _read_posix(path, offsets, DISK_CHUNK, time.perf_counter() + max_s, direct=False)
    if total <= 0 or seconds <= 0:
        raise ProbeError(f"{path}: the probe read nothing")
    return {"gbytes_ps": total / seconds / 1e9, "bytes": total, "seconds": round(seconds, 4), "method": method}


class DiskCache:
    """Measured disk bandwidths, keyed by machine + path + size + mtime, valid CACHE_TTL_S."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def key(machine, path, stat, method="direct", chunk=DISK_CHUNK):
        """Machine + volume/device id + path + size + mtime + probe method + request size."""
        return f"{machine}|{stat['device']}|{path}|{stat['size']}|{int(stat['mtime'])}|{method}|{chunk}"

    def get(self, key, now=None):
        entry = self._load().get(key)
        now = time.time() if now is None else now
        if not isinstance(entry, dict) or now - entry.get("measured_at", 0) > CACHE_TTL_S:
            return None
        return entry

    def put(self, key, result, now=None):
        data = self._load()
        data[key] = dict(result, measured_at=time.time() if now is None else now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def file_stat(path):
    """The identity of a model file for the probe cache, or None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"device": st.st_dev, "size": st.st_size, "mtime": st.st_mtime}


def disk_fact(cache, machine, path, stat, measure, measurer):
    """The disk bandwidth fact of one model file: measured now (only when asked), else cached, else unavailable.
    A buffered measurement is reported but is no evidence: its value is withheld."""
    if stat is None:
        return missing(f"{path}: not found")
    key = DiskCache.key(machine, path, stat)
    if measure:
        try:
            result = measurer()
        except (OSError, ProbeError) as exc:
            return missing(f"disk probe failed: {exc}")
        if result["method"] != "direct":
            return missing(f"unverified: {result['method']} read ({result['gbytes_ps']:.2f} GB/s may come from the cache)")
        cache.put(key, result)
        return fact(result["gbytes_ps"], "probed") | {"method": result["method"]}
    entry = cache.get(key)
    if entry is None:
        return missing("not measured: run `hyperloom recipe probe <profile> --disks`")
    age_h = (time.time() - entry["measured_at"]) / 3600
    return fact(entry["gbytes_ps"], f"cache {age_h:.0f} h", None) | {"method": entry["method"]}


# --- nodes (node tool verbs) -------------------------------------------------------------------------------------

def _ok(check):
    return check.get("value") if isinstance(check, dict) and check.get("status") == "ok" else None


def node_device(gpu_result):
    """The Strix device of a node from the ``gpu`` verb: name, GPU-mappable memory (VRAM + GTT), host RAM, free RAM."""
    checks = (gpu_result or {}).get("checks", {})
    cards = _ok(checks.get("amd")) or []
    ram = _ok(checks.get("ram")) or {}
    out = {}
    if cards:
        card = cards[0]
        name = _ok(card.get("product_name"))
        vram, gtt = _ok(card.get("vram_total_bytes")), _ok(card.get("gtt_total_bytes"))
        out["name"] = fact(name, "probed") if name else missing("product_name not reported")
        out["memory_gib"] = (fact(((vram or 0) + (gtt or 0)) / GIB, "probed") if vram is not None or gtt is not None
                             else missing("VRAM/GTT totals not reported"))
    else:
        out["name"] = out["memory_gib"] = missing("no AMD GPU reported")
    out["host_ram_gib"] = (fact(ram["total_bytes"] / GIB, "probed") if "total_bytes" in ram
                           else missing("RAM not reported"))
    out["host_free_ram_gib"] = (fact(ram["available_bytes"] / GIB, "probed") if ram.get("available_bytes") is not None
                                else missing("MemAvailable not reported"))
    return out


def node_links(doctor_result):
    """(lan link fact, usb4 link fact) of one node from its ``doctor`` checks. LAN: the fastest physical NIC that is
    up (negotiated speed). USB4: up when the layout file exists and a stream is openable; its bandwidth is not probed
    here (that needs a transport turn), so it is unavailable unless the operator declared a measured value."""
    checks = (doctor_result or {}).get("checks", {})
    interfaces = _ok(checks.get("interfaces")) or []
    speeds = [i["negotiated_mbps"] for i in interfaces if i.get("state") == "up"
              and type(i.get("negotiated_mbps")) is int and i["negotiated_mbps"] > 0]
    lan = fact(max(speeds) / 1000, "probed") if speeds else missing("no physical interface up")
    lan["up"] = bool(speeds)
    usb4 = checks.get("usb4") or {}
    streams = _ok(usb4.get("streams")) or {}
    layout = _ok(usb4.get("layout"))
    openable = streams.get("openable") or []
    if layout is None:
        link = missing("USB4 layout file missing")
    elif not openable:
        link = missing("no openable USB4 stream")
    else:
        link = missing("USB4 bandwidth is not probed (declare a measured value)")
        link["streams"] = len(openable)
    link["up"] = layout is not None and bool(openable)
    return lan, link
