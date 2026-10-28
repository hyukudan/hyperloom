
import atexit as _atexit
import contextlib as _contextlib
import os as _os
from pathlib import Path as _Path
import re as _re
import site as _site
import shutil as _shutil
import sys as _sys
import tempfile as _tempfile
from urllib.parse import unquote as _unquote, urlsplit as _urlsplit

# Scope: the audit hook below is defence in depth for writes made by this Python process. Native writers (git, a
# shell, cmd) and fresh child interpreters do not run the hook; they are kept away from the real configuration only
# by the redirected environment that they inherit.
_GUARD_ROOTS = "HYPERLOOM_TEST_REAL_CONFIG_DIRS"        # exported once, so nested test runs guard the originals
_ORIGINAL_LOCALAPPDATA = "HYPERLOOM_TEST_ORIGINAL_LOCALAPPDATA"   # per-user browser installs (tests/ui/smoke.py)
_ORIGINAL_CHROME = "HYPERLOOM_TEST_CHROME"
_ROAMING_APP_DATA = "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"
_LOCAL_APP_DATA = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"


def _known_folder(folder_id):
    """A Windows known folder as the shell resolves it (APPDATA/LOCALAPPDATA in the environment do not change it)."""
    import ctypes
    import uuid
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    pointer = ctypes.c_void_p()
    try:
        result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    except (AttributeError, OSError):
        return None
    try:
        return ctypes.wstring_at(pointer.value) if result == 0 and pointer.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pointer)


def _profile_directory():
    """The Windows profile directory of this process's user, read from the account (not from USERPROFILE)."""
    import ctypes
    from ctypes import wintypes
    buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    try:
        function = ctypes.windll.userenv.GetUserProfileDirectoryW
        function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        # -4: GetCurrentProcessToken()
        return buffer.value if function(wintypes.HANDLE(-4), buffer, ctypes.byref(size)) else None
    except (AttributeError, OSError):
        return None


def _without_extended_prefix(path):
    if path[:4] in ("\\\\?\\", "//?/"):
        return "\\\\" + path[8:] if path[4:8].upper() in ("UNC\\", "UNC/") else path[4:]
    return path


def _spellings(path):
    """The spellings to compare: as written (absolute, without a \\\\?\\ prefix) and canonical (links, junctions,
    8.3 short names and per-package AppData redirection resolved), both case folded."""
    path = _without_extended_prefix(_os.fsdecode(path))
    try:
        canonical = _os.path.realpath(path)
    except (OSError, ValueError):
        canonical = path
    return {_os.path.normcase(_os.path.abspath(_without_extended_prefix(form))) for form in (path, canonical)}


def _uri_path(database):
    """The file a sqlite3 ``file:`` URI names (file:///C:/x?mode=rwc -> C:/x)."""
    path = _unquote(_urlsplit(database).path)
    return path[1:] if _os.name == "nt" and _re.match(r"/[A-Za-z]:", path) else path


def _original_roots():
    if _os.environ.get(_GUARD_ROOTS):
        roots = set(_os.environ[_GUARD_ROOTS].split(_os.pathsep))
    else:
        roots = {_Path(_os.environ.get("APPDATA", _Path.home() / "AppData" / "Roaming")) / "hyperloom",
                 _Path(_os.environ.get("LOCALAPPDATA", _Path.home() / "AppData" / "Local")) / "hyperloom",
                 _Path.home() / ".config" / "hyperloom"}
        if _os.environ.get("XDG_CONFIG_HOME"):
            roots.add(_Path(_os.environ["XDG_CONFIG_HOME"]) / "hyperloom")
    if _os.name == "nt":
        roots.update(_Path(folder) / "hyperloom" for folder in map(_known_folder, (_ROAMING_APP_DATA, _LOCAL_APP_DATA))
                     if folder)
        # The shell's known folders ignore APPDATA/LOCALAPPDATA but expand USERPROFILE; the account's profile
        # directory ignores the environment altogether.
        for profile in {_os.environ.get("USERPROFILE"), _profile_directory()} - {None, ""}:
            roots.update(_Path(profile) / "AppData" / name / "hyperloom" for name in ("Roaming", "Local"))
    else:
        import pwd
        roots.add(_Path(pwd.getpwuid(_os.getuid()).pw_dir) / ".config" / "hyperloom")
    return tuple(sorted({form for root in roots for form in _spellings(root)}))


# Install before importing application modules. CLI children inherit the same sandbox.
_real_config_dirs = _original_roots()
_os.environ.setdefault(_GUARD_ROOTS, _os.pathsep.join(_real_config_dirs))
_os.environ.setdefault(_ORIGINAL_LOCALAPPDATA, _os.environ.get("LOCALAPPDATA", ""))
# Child Pythons keep the user site (test tools installed with pip --user) after APPDATA/HOME move.
_os.environ.setdefault("PYTHONUSERBASE", _site.getuserbase())


def _inside_real_config(path):
    return any(form == root or form.startswith(root.rstrip(_os.sep) + _os.sep)
               for form in _spellings(path) for root in _real_config_dirs)


# Resolve the browser before LOCALAPPDATA moves; the executable stays installed, its profile stays temporary.
if _ORIGINAL_CHROME not in _os.environ:
    _chrome_candidates = [_os.environ.get("CHROME")]
    _chrome_candidates += [_shutil.which(name) for name in
                          ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")]
    _chrome_candidates += [str(_Path(_os.environ[name]) / "Google/Chrome/Application/chrome.exe")
                           for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", _ORIGINAL_LOCALAPPDATA)
                           if _os.environ.get(name)]
    _chrome_candidates += ["/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/chromium"]
    _os.environ[_ORIGINAL_CHROME] = next((str(_Path(path).resolve()) for path in _chrome_candidates
                                        if path and _Path(path).is_file()), "")

_outer_admission_db = _os.environ.get("HYPERLOOM_ADMISSION_DB")
_temporary = _tempfile.TemporaryDirectory(prefix="hl-tests-", ignore_cleanup_errors=True)
_test_root = _Path(_temporary.name)
# Hyperloom's config/secrets/runtime roots: APPDATA and LOCALAPPDATA on Windows, HOME (~/.config) and
# XDG_CONFIG_HOME elsewhere. USERPROFILE retains the Windows identity required by Chrome's debugging checks, and
# HOME stays on Windows (Python ignores it there; Git for Windows reads its global config from it).
for _name, _relative in (("APPDATA", "appdata"), ("LOCALAPPDATA", "local"), ("HOME", "home"),
                         ("XDG_CONFIG_HOME", "home/.config")):
    if _name == "HOME" and _os.name == "nt":
        continue
    _directory = _test_root / _relative
    _directory.mkdir(parents=True, exist_ok=True)
    _os.environ[_name] = str(_directory)
# An outer value is kept (a deliberate file database), unless it points into a real configuration directory.
if _outer_admission_db not in (None, "", ":memory:") and _inside_real_config(
        _uri_path(_outer_admission_db) if _outer_admission_db.startswith("file:") else _outer_admission_db):
    _os.environ["HYPERLOOM_ADMISSION_DB"] = ":memory:"
_os.environ.setdefault("HYPERLOOM_ADMISSION_DB", ":memory:")


class RealConfigWrite(BaseException):
    """A guard trip. Not an Exception, so application ``except Exception`` handlers cannot absorb it."""


# Every trip, also one raised in a worker thread or caught by a bare except: any left at exit fail the run.
_trips = []


@_contextlib.contextmanager
def expected_guard_trips():
    """For the guard's own tests: trips inside the block are expected and do not fail the run."""
    start = len(_trips)
    try:
        yield _trips
    finally:
        del _trips[start:]


def _fail_run_on_trips():
    # Runs after pytest or unittest has reported; os._exit replaces whatever exit status the runner chose.
    if _trips:
        _sys.stdout.flush()
        _sys.stderr.write("FAILED: tests attempted to modify the real per-user configuration: %r\n" % (_trips,))
        _sys.stderr.flush()
        _os._exit(3)


_atexit.register(_fail_run_on_trips)


def _guard_real_config(event, args):
    """Fail before a Python filesystem mutation can reach the original user configuration."""
    if event == "open":
        mode, flags = args[1:3]
        if not ((mode and any(char in mode for char in "wax+")) or
                flags & (_os.O_WRONLY | _os.O_RDWR | _os.O_CREAT | _os.O_TRUNC | _os.O_APPEND)):
            return
        paths = args[:1]
    elif event in ("os.rename", "os.link", "os.symlink"):
        paths = args[:2]
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
                   "shutil.rmtree", "sqlite3.connect"):
        paths = args[:1]
        if event == "sqlite3.connect" and isinstance(paths[0], (str, bytes)):
            database = _os.fsdecode(paths[0])
            if database in ("", ":memory:"):
                return
            if database.startswith("file:"):
                paths = (_uri_path(database),)
    else:
        return
    for path in paths:
        if isinstance(path, (str, bytes, _os.PathLike)) and _inside_real_config(path):
            _trips.append((event, _os.fsdecode(path)))
            raise RealConfigWrite("test attempted to modify the real per-user configuration directory")


_sys.addaudithook(_guard_real_config)
