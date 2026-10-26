"""Read-only, attributable GPU memory observers. No CUDA calls or device-total estimates."""
from __future__ import annotations

import ctypes as C
import os
import re


class CounterError(RuntimeError):
    pass


def _check(status):
    if status:
        raise CounterError(f"GPU counter API failed ({status})")


class Nvml:
    """NVML inventory and Linux per-process accounting; loading NVML does not create a CUDA context."""

    def __init__(self):
        try:
            self.dll = C.CDLL("nvml.dll" if os.name == "nt" else "libnvidia-ml.so.1")
            _check(self.dll.nvmlInit_v2())
        except (OSError, AttributeError) as exc:
            raise CounterError("NVML unavailable") from exc

    def close(self):
        _check(self.dll.nvmlShutdown())

    def devices(self):
        count = C.c_uint()
        _check(self.dll.nvmlDeviceGetCount_v2(C.byref(count)))
        result = []
        for index in range(count.value):
            handle, uuid = C.c_void_p(), C.create_string_buffer(96)
            _check(self.dll.nvmlDeviceGetHandleByIndex_v2(index, C.byref(handle)))
            _check(self.dll.nvmlDeviceGetUUID(handle, uuid, len(uuid)))
            result.append((uuid.value.decode("ascii").lower(), handle))
        return result

    def luids(self):
        result = {}
        try:
            for uuid, handle in self.devices():
                luid, mask = C.create_string_buffer(8), C.c_uint()
                _check(self.dll.nvmlDeviceGetLuid(handle, luid, C.byref(mask)))
                # PDH spells the LUID as high-part then low-part, each in hexadecimal.
                low, high = int.from_bytes(luid.raw[:4], "little"), int.from_bytes(luid.raw[4:], "little")
                key = (high, low)
                if key in result:
                    raise CounterError("adapter LUID is ambiguous")
                result[key] = uuid
        except AttributeError as exc:
            raise CounterError("NVML adapter LUID mapping unavailable") from exc
        return result

    def sample(self, pid):
        class Process(C.Structure):
            _fields_ = [("pid", C.c_uint), ("used", C.c_ulonglong),
                        ("gpu_instance", C.c_uint), ("compute_instance", C.c_uint)]

        result = {}
        try:
            for uuid, handle in self.devices():
                allocations = {}
                for name in ("nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2"):
                    query = getattr(self.dll, name)
                    for attempt in range(3):
                        count = C.c_uint()
                        status = query(handle, C.byref(count), None)
                        if status not in (0, 7):  # NVML_ERROR_INSUFFICIENT_SIZE
                            _check(status)
                        if not count.value:
                            break
                        rows = (Process * (count.value + 16))()
                        count.value = len(rows)
                        status = query(handle, C.byref(count), rows)
                        if status == 7:
                            continue
                        _check(status)
                        for row in rows[:count.value]:
                            if row.pid == pid:
                                if row.used == (1 << 64) - 1:
                                    raise CounterError("per-process GPU memory is N/A")
                                # Sum distinct partitions; compute and graphics lists may repeat the same allocation.
                                key = (row.gpu_instance, row.compute_instance)
                                allocations[key] = max(allocations.get(key, 0), row.used)
                        break
                    else:
                        raise CounterError("GPU process list changed repeatedly")
                result[uuid] = sum(allocations.values())
        except AttributeError as exc:
            raise CounterError("NVML per-process counters unavailable") from exc
        return result


def pdh_sample(rows, luids, pid):
    """Decode a complete Dedicated Usage snapshot (instance, bytes, valid), including ungranted adapters."""
    result = dict.fromkeys(luids.values(), 0)
    seen = set()
    for instance, used, valid in rows:
        match = re.fullmatch(r"pid_(\d+)_luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_(\d+)(?:#\d+)?",
                             instance, re.I)
        if match is None:
            if instance.lower().startswith(f"pid_{pid}_"):
                raise CounterError("unrecognized process GPU counter instance")
            continue
        if int(match[1]) != pid:
            continue
        key = (int(match[2], 16), int(match[3], 16))
        uuid = luids.get(key)
        if uuid is None:
            raise CounterError("process active on an unmapped adapter LUID")
        if not valid or type(used) is not int or used < 0:
            raise CounterError("per-process Dedicated Usage is unavailable")
        identity = (key, int(match[4]))
        if identity in seen:
            raise CounterError("duplicate process GPU counter instance")
        seen.add(identity)
        result[uuid] += used
    return result


class Pdh:
    """English counter paths avoid localized counter-name lookup; handles are pointer-sized on Win64."""

    def __init__(self):
        self.dll = C.WinDLL("pdh.dll")
        self.query, self.counter = C.c_void_p(), C.c_void_p()
        self.dll.PdhOpenQueryW.argtypes = [C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
        self.dll.PdhAddEnglishCounterW.argtypes = [C.c_void_p, C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
        self.dll.PdhCollectQueryData.argtypes = [C.c_void_p]
        self.dll.PdhGetFormattedCounterArrayW.argtypes = [C.c_void_p, C.c_uint32,
                                                        C.POINTER(C.c_uint32), C.POINTER(C.c_uint32), C.c_void_p]
        self.dll.PdhCloseQuery.argtypes = [C.c_void_p]
        _check(self.dll.PdhOpenQueryW(None, 0, C.byref(self.query)))
        try:
            _check(self.dll.PdhAddEnglishCounterW(self.query, r"\GPU Process Memory(*)\Dedicated Usage",
                                                0, C.byref(self.counter)))
        except Exception:
            self.close()
            raise

    def close(self):
        if self.query.value:
            self.dll.PdhCloseQuery(self.query)
            self.query = C.c_void_p()

    def rows(self):
        class Value(C.Structure):
            _fields_ = [("status", C.c_uint32), ("value", C.c_int64)]

        class Item(C.Structure):
            _fields_ = [("name", C.c_wchar_p), ("value", Value)]

        _check(self.dll.PdhCollectQueryData(self.query))
        for attempt in range(3):
            size, count = C.c_uint32(), C.c_uint32()
            status = self.dll.PdhGetFormattedCounterArrayW(self.counter, 0x400, C.byref(size), C.byref(count), None)
            if status & 0xffffffff != 0x800007D2:  # PDH_MORE_DATA
                _check(status)
            if not size.value:
                return []
            buffer = C.create_string_buffer(size.value)
            status = self.dll.PdhGetFormattedCounterArrayW(self.counter, 0x400, C.byref(size), C.byref(count), buffer)
            if status & 0xffffffff == 0x800007D2:
                continue
            _check(status)
            rows = C.cast(buffer, C.POINTER(Item))
            return [(rows[i].name, rows[i].value.value, rows[i].value.status in (0, 1)) for i in range(count.value)]
        raise CounterError("PDH process list changed repeatedly")


class Observer:
    def __init__(self):
        self.nvml, self.pdh = Nvml(), None
        try:
            self.luids = self.nvml.luids() if os.name == "nt" else None
            self.pdh = Pdh() if os.name == "nt" else None
        except Exception:
            self.close()
            raise

    def sample(self, pid):
        return pdh_sample(self.pdh.rows(), self.luids, pid) if self.pdh else self.nvml.sample(pid)

    def close(self):
        try:
            if self.pdh:
                self.pdh.close()
        finally:
            self.nvml.close()
