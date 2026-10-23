"""User-scoped API keys and short-lived router key files."""

import ctypes
import os
from pathlib import Path
import re
import secrets as random
import tempfile


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", name):
        raise ValueError("invalid secret name")
    return name


def _directory():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "hyperloom/secrets"
    return Path.home() / ".config/hyperloom/secrets"


def runtime_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "hyperloom/run"
    return Path.home() / ".config/hyperloom/run"


def _dpapi(data, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; no machine-scope flag: only this user can decrypt.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _private(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    # Protected DACL: OWNER RIGHTS grants access only to the file's creating user.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _write(path, data, *, replace=True, exists_ok=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".key-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            _private(temporary)
            stream.write(data)
        if replace:
            os.replace(temporary, path)
        else:
            # Linking a complete private file gives ensure() create-if-absent semantics.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not exists_ok:
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def get(name):
    path = _directory() / _name(name)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if os.name == "nt":
        data = _dpapi(data, decrypt=True)
    value = data.decode("ascii")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid stored API key")
    return value


def ensure(name):
    _name(name)
    value = get(name)
    if value is None:
        data = random.token_hex(32).encode("ascii")
        _write(_directory() / name, _dpapi(data) if os.name == "nt" else data, replace=False)
        value = get(name)
    return value


def rotate(name):
    _name(name)
    data = random.token_hex(32).encode("ascii")
    _write(_directory() / name, _dpapi(data) if os.name == "nt" else data)
    return data.decode("ascii")


def path_for_router(name):
    value = ensure(name)
    path = runtime_dir() / (_name(name) + "-" + random.token_hex(8) + ".key")
    _write(path, value.encode("ascii"))
    return path


def export(name, destination):
    value = get(name)
    if value is None:
        raise ValueError("no saved router client key; run up first")
    _write(Path(destination), value.encode("ascii"), replace=False, exists_ok=False)
