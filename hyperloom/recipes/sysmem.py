"""Advisory, read-only NVAPI DRS query, isolated so a stalled driver can be terminated after five seconds."""
from __future__ import annotations

import ctypes as C
import json
import ntpath
import os
from pathlib import PureWindowsPath
import subprocess
import sys

from ..winproc import no_window
from .reader import RecipeError

UNKNOWN = "unknown"
NO_FALLBACK = "prefer_no_sysmem_fallback"
_SETTING_NAME = "cuda - sysmem fallback policy"


def _path(value):
    return ntpath.normcase(ntpath.normpath(value))


def profile_matches(executable, application):
    """Require an exact release path, or a filename explicitly scoped to that directory; never basename alone."""
    name, folder = application.get("name", ""), application.get("folder", "")
    if not name:
        return False
    if PureWindowsPath(name).is_absolute():
        return _path(name) == _path(executable)
    return bool(folder and _path(ntpath.join(folder, name)) == _path(executable))


def read_policy(executable, api):
    """The injected API exposes only reads and session cleanup. No profile/settings mutation is available."""
    session = api.create()
    try:
        api.load(session)
        profile, application = api.find(session, executable)
        if not profile_matches(executable, application):
            return UNKNOWN
        for name, kind, value in api.settings(session, profile):
            if name.strip().casefold() == _SETTING_NAME and kind == 0:  # NVDRS_DWORD_TYPE
                # NVAPI's CUDA Sysmem Fallback Policy: default=0, prefer fallback=1, prefer no fallback=2.
                return NO_FALLBACK if value == 2 else "fallback_allowed" if value in (0, 1) else UNKNOWN
        return UNKNOWN
    finally:
        api.destroy(session)


class Drs:
    """NVAPI public QueryInterface IDs; only Initialize, DRS reads, session lifecycle and Unload."""

    def __init__(self):
        self.dll = C.CDLL("nvapi64.dll" if C.sizeof(C.c_void_p) == 8 else "nvapi.dll")
        self.dll.nvapi_QueryInterface.argtypes = [C.c_uint32]
        self.dll.nvapi_QueryInterface.restype = C.c_void_p

        class Binary(C.Structure):
            _fields_ = [("length", C.c_uint32), ("data", C.c_ubyte * 4096)]

        class Value(C.Union):
            _fields_ = [("dword", C.c_uint32), ("binary", Binary), ("string", C.c_uint16 * 2048)]

        class Setting(C.Structure):
            _fields_ = [("version", C.c_uint32), ("name", C.c_uint16 * 2048), ("id", C.c_uint32),
                        ("kind", C.c_uint32), ("location", C.c_uint32), ("current_predefined", C.c_uint32),
                        ("predefined_valid", C.c_uint32), ("predefined", Value), ("current", Value)]

        class Application(C.Structure):
            _fields_ = [("version", C.c_uint32), ("predefined", C.c_uint32),
                        ("name", C.c_uint16 * 2048), ("friendly", C.c_uint16 * 2048),
                        ("launcher", C.c_uint16 * 2048), ("folder", C.c_uint16 * 2048)]

        self.Setting, self.Application = Setting, Application
        self._initialize = self.function(0x0150E828)
        self._unload = self.function(0xD22BDD7E)
        self._create = self.function(0x0694D52E, C.POINTER(C.c_void_p))
        self._destroy = self.function(0xDAD9CFF8, C.c_void_p)
        self._load = self.function(0x375DBD6B, C.c_void_p)
        self._find = self.function(0xEEE566B2, C.c_void_p, C.c_wchar_p, C.POINTER(C.c_void_p),
                                   C.POINTER(Application))
        self._enum = self.function(0xAE3039DA, C.c_void_p, C.c_void_p, C.c_uint32,
                                   C.POINTER(C.c_uint32), C.POINTER(Setting))
        self.check(self._initialize())

    def function(self, ident, *args):
        address = self.dll.nvapi_QueryInterface(ident)
        if not address:
            raise OSError("NVAPI DRS read interface unavailable")
        return C.CFUNCTYPE(C.c_int, *args)(address)

    @staticmethod
    def check(status):
        if status:
            raise OSError(f"NVAPI DRS read failed ({status})")

    @staticmethod
    def string(value):
        return bytes(value).decode("utf-16-le").split("\0", 1)[0]

    def create(self):
        session = C.c_void_p()
        try:
            self.check(self._create(C.byref(session)))
        except Exception:
            self._unload()
            raise
        return session

    def destroy(self, session):
        try:
            self.check(self._destroy(session))
        finally:
            self._unload()

    def load(self, session):
        self.check(self._load(session))

    def find(self, session, executable):
        application, profile = self.Application(), C.c_void_p()
        application.version = C.sizeof(application) | (2 << 16)  # NVDRS_APPLICATION_V2 (fileInFolder)
        self.check(self._find(session, executable, C.byref(profile), C.byref(application)))
        return profile, {"name": self.string(application.name), "folder": self.string(application.folder)}

    def settings(self, session, profile):
        for start in range(0, 65536, 64):
            rows = (self.Setting * 64)()
            for row in rows:
                row.version = C.sizeof(row) | (1 << 16)
            count = C.c_uint32(len(rows))
            status = self._enum(session, profile, start, C.byref(count), rows)
            if status == -7:  # NVAPI_END_ENUMERATION
                return
            self.check(status)
            if count.value > len(rows):
                raise OSError("invalid NVAPI DRS setting count")
            for row in rows[:count.value]:
                yield self.string(row.name), row.kind, row.current.dword
            if count.value < len(rows):
                return
        raise OSError("NVAPI DRS settings enumeration exceeded limit")


def probe(executable, *, run=subprocess.run, platform=None):
    if (platform or os.name) != "nt":
        return "not_applicable"
    if not PureWindowsPath(executable).is_absolute():
        return UNKNOWN
    try:
        result = run([sys.executable, "-m", "hyperloom.recipes.sysmem", executable], capture_output=True,
                     text=True, timeout=5, stdin=subprocess.DEVNULL, **no_window())
        value = json.loads(result.stdout) if result.returncode == 0 else UNKNOWN
        return value if value in (UNKNOWN, NO_FALLBACK, "fallback_allowed") else UNKNOWN
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return UNKNOWN


def require(policy, *, placement, platform):
    """An advisory preflight refusal; the engine independently checks the effective policy at every start."""
    if placement and platform == "windows-wddm" and policy != NO_FALLBACK:
        raise RecipeError("PL_E_SYSMEM placement requires Prefer No Sysmem Fallback (policy is " + str(policy) + ")")


if __name__ == "__main__":
    try:
        value = read_policy(sys.argv[1], Drs())
    except Exception:  # read failure stays unknown; never include local paths in output
        value = UNKNOWN
    print(json.dumps(value))
