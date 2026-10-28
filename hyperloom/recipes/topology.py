"""Passive PCI capability collection. Missing properties never become guessed transfer profiles."""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import re
import uuid

UNKNOWN = "unknown"
_PCI = re.compile(r"(?:[0-9a-f]{4}|[0-9a-f]{8}):[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", re.I)
_SPEEDS = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}


def pci(value):
    if not isinstance(value, str) or not _PCI.fullmatch(value):
        return UNKNOWN
    domain, bus, device = value.lower().split(":")
    if int(domain, 16) > 0xffff:
        return UNKNOWN
    return f"{int(domain, 16):04x}:{bus}:{device}"


def _read(path):
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _positive(value):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else UNKNOWN
    except (TypeError, ValueError):
        return UNKNOWN


def _numa(value):
    try:
        parsed = int(value)
        return parsed if 0 <= parsed < 0xffffffff else UNKNOWN
    except (TypeError, ValueError):
        return UNKNOWN


def summarize(chain, *, complete=True):
    """Root-to-endpoint chain, with capability values at every PCI hop (never current/idle link state)."""
    good = complete and bool(chain)
    gens = [row.get("gen", UNKNOWN) for row in chain]
    widths = [row.get("width", UNKNOWN) for row in chain]
    via = [row.get("via", UNKNOWN) for row in chain[:-1]]
    return {"pci_path": [row["id"] for row in chain] if good else UNKNOWN,
            "link": {"gen": min(gens) if good and all(type(g) is int and g > 0 for g in gens) else UNKNOWN,
                     "width": min(widths) if good and all(type(w) is int and w > 0 for w in widths) else UNKNOWN},
            "via": ("chipset" if "chipset" in via else "direct" if via and all(v == "direct" for v in via)
                    else UNKNOWN) if good else UNKNOWN,
            "numa_node": chain[-1].get("numa_node", UNKNOWN) if good else UNKNOWN}


def bridge_class(vendor, device):
    """Conservative chipset identification. Unrecognized roots stay unknown, even with a readable PCI path.

    Promontory downstream bridges identify a chipset path; Matisse host GPP bridges identify CPU PCIe.
    A switch or an unknown root must not be inferred from path length alone.
    """
    if vendor == "1022" and device in {"43b4", "43b9", "43ba", "43bb", "43e9", "43ea", "43eb", "43f4", "43f5"}:
        return "chipset"
    if vendor == "1022" and device in {"1483", "1484"}:
        return "direct"
    return UNKNOWN


def linux(pci_id, *, root=Path("/sys/bus/pci/devices")):
    ident = pci(pci_id)
    if ident == UNKNOWN:
        return summarize([], complete=False)
    try:
        endpoint = (root / ident).resolve(strict=True)
    except OSError:
        return summarize([], complete=False)
    nodes = [p for p in [*reversed(endpoint.parents), endpoint] if _PCI.fullmatch(p.name)]
    chain = []
    for node in nodes:
        speed = _read(node / "max_link_speed")
        try:
            gen = _SPEEDS.get(float(speed.split()[0]), UNKNOWN)
        except (AttributeError, ValueError, IndexError):
            gen = UNKNOWN
        vendor, device = _read(node / "vendor"), _read(node / "device")
        chain.append({"id": pci(node.name), "gen": gen, "width": _positive(_read(node / "max_link_width")),
                      "via": bridge_class((vendor or "").removeprefix("0x"), (device or "").removeprefix("0x")),
                      "numa_node": _numa(_read(node / "numa_node"))})
    return summarize(chain, complete=bool(nodes) and pci(nodes[-1].name) == ident)


class CfgMgr:
    """CfgMgr32 device properties require neither elevation nor a kernel driver."""

    def __init__(self):
        class Key(C.Structure):
            _fields_ = [("guid", C.c_ubyte * 16), ("pid", C.c_uint32)]
        self.Key = Key
        self.dll = C.WinDLL("cfgmgr32.dll")
        self.dll.CM_Get_DevNode_PropertyW.argtypes = [C.c_uint32, C.POINTER(Key), C.POINTER(C.c_uint32),
                                                    C.c_void_p, C.POINTER(C.c_uint32), C.c_uint32]
        self.dll.CM_Get_Parent.argtypes = [C.POINTER(C.c_uint32), C.c_uint32, C.c_uint32]
        self.dll.CM_Get_Device_IDW.argtypes = [C.c_uint32, C.c_wchar_p, C.c_uint32, C.c_uint32]
        self.dll.CM_Get_Device_ID_List_SizeW.argtypes = [C.POINTER(C.c_uint32), C.c_wchar_p, C.c_uint32]
        self.dll.CM_Get_Device_ID_ListW.argtypes = [C.c_wchar_p, C.c_wchar_p, C.c_uint32, C.c_uint32]
        self.dll.CM_Locate_DevNodeW.argtypes = [C.POINTER(C.c_uint32), C.c_wchar_p, C.c_uint32]

    def prop(self, node, guid, pid):
        key = self.Key((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le), pid)
        kind, size = C.c_uint32(), C.c_uint32(65536)
        buffer = C.create_string_buffer(size.value)
        if self.dll.CM_Get_DevNode_PropertyW(node, C.byref(key), C.byref(kind), buffer, C.byref(size), 0):
            return None
        if kind.value == 7 and size.value == 4:  # DEVPROP_TYPE_UINT32
            return int.from_bytes(buffer.raw[:4], "little")
        if kind.value in (18, 0x2012):  # STRING / STRING_LIST
            return buffer.raw[:size.value].decode("utf-16-le").rstrip("\0").split("\0")
        return None

    def identity(self, node):
        value = C.create_unicode_buffer(4096)
        return value.value if not self.dll.CM_Get_Device_IDW(node, value, len(value), 0) else None

    def parent(self, node):
        parent = C.c_uint32()
        status = self.dll.CM_Get_Parent(C.byref(parent), node, 0)
        if status == 0x0d:  # CR_NO_SUCH_DEVNODE: root reached
            return None
        if status:
            raise OSError("device parent unavailable")
        return parent.value

    def nodes(self):
        size = C.c_uint32()
        if self.dll.CM_Get_Device_ID_List_SizeW(C.byref(size), "PCI", 0x101):  # ENUMERATOR | PRESENT
            raise OSError("PCI inventory unavailable")
        value = C.create_unicode_buffer(size.value)
        if self.dll.CM_Get_Device_ID_ListW("PCI", value, len(value), 0x101):
            raise OSError("PCI inventory unavailable")
        result = []
        for ident in value[:].rstrip("\0").split("\0"):
            node = C.c_uint32()
            if not self.dll.CM_Locate_DevNodeW(C.byref(node), ident, 0):
                result.append(node.value)
        return result


_DEVICE = "a45c254e-df1c-4efd-8020-67d146a850e0"
_PCI_DEVICE = "3ab22e31-8264-4b4e-9af5-a8d2d8e33e62"
_NUMA = "540b947e-8b40-45bc-a8a2-6a0b894cbda2"


def windows(pci_id, *, api=None):
    ident = pci(pci_id)
    if ident == UNKNOWN:
        return summarize([], complete=False)
    try:
        api = api or CfgMgr()
        matches = []
        for node in api.nodes():
            bus, address = api.prop(node, _DEVICE, 23), api.prop(node, _DEVICE, 30)
            if type(bus) is int and type(address) is int:
                candidate = f"0000:{bus:02x}:{address >> 16:02x}.{address & 0xffff}"
                if candidate == ident:
                    matches.append(node)
        if len(matches) != 1:
            return summarize([], complete=False)
        node, chain, seen = matches[0], [], set()
        while node is not None:
            if node in seen or len(seen) >= 64:
                return summarize([], complete=False)
            seen.add(node)
            instance = api.identity(node)
            if instance is None:
                return summarize([], complete=False)
            if instance.upper().startswith("PCI\\"):
                paths = api.prop(node, _DEVICE, 37)
                bus, address = api.prop(node, _DEVICE, 23), api.prop(node, _DEVICE, 30)
                bdf = (pci(f"0000:{bus:02x}:{address >> 16:02x}.{address & 0xffff}")
                       if type(bus) is int and type(address) is int and paths else UNKNOWN)
                model = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", instance, re.I)
                chain.append({"id": bdf,
                              "gen": _positive(api.prop(node, _PCI_DEVICE, 11)),
                              "width": _positive(api.prop(node, _PCI_DEVICE, 12)),
                              "numa_node": _numa(api.prop(node, _NUMA, 3)),
                              "via": bridge_class(model[1].lower(), model[2].lower()) if model else UNKNOWN})
            node = api.parent(node)
        return summarize(list(reversed(chain)), complete=all(r["id"] != UNKNOWN for r in chain))
    except (OSError, ValueError, AttributeError):
        return summarize([], complete=False)


def collect(pci_id):
    return windows(pci_id) if os.name == "nt" else linux(pci_id)


def _windows_count(relation):
    kernel = C.WinDLL("kernel32.dll", use_last_error=True)
    query = kernel.GetLogicalProcessorInformationEx
    query.argtypes = [C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)]
    size = C.c_uint32()
    query(relation, None, C.byref(size))
    if not size.value or size.value > 16 * 1024 * 1024:
        return UNKNOWN
    buffer = C.create_string_buffer(size.value)
    if not query(relation, buffer, C.byref(size)):
        return UNKNOWN
    offset, count = 0, 0
    while offset + 8 <= size.value:
        length = int.from_bytes(buffer.raw[offset + 4:offset + 8], "little")
        if length < 8 or offset + length > size.value:
            return UNKNOWN
        offset += length
        count += 1
    return count if offset == size.value and count else UNKNOWN


def host():
    """Transfer host class without hostname, serial, user, or address. Pinned policy is an explicit local input."""
    result = {"cpu_model": UNKNOWN, "sockets": UNKNOWN, "numa_nodes": UNKNOWN, "pinned_policy": UNKNOWN}
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                result["cpu_model"] = " ".join(winreg.QueryValueEx(key, "ProcessorNameString")[0].split())
            result["sockets"], result["numa_nodes"] = _windows_count(3), _windows_count(1)
        except OSError:
            pass
    else:
        text = _read(Path("/proc/cpuinfo"))
        if text:
            rows = [line.split(":", 1) for line in text.splitlines() if ":" in line]
            models = {value.strip() for key, value in rows if key.strip() == "model name"}
            sockets = {value.strip() for key, value in rows if key.strip() == "physical id"}
            if len(models) == 1:
                result["cpu_model"] = models.pop()
            if sockets:
                result["sockets"] = len(sockets)
        try:
            nodes = list(Path("/sys/devices/system/node").glob("node[0-9]*"))
            if nodes:
                result["numa_nodes"] = len(nodes)
        except OSError:
            pass
    return result
