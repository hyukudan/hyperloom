
import os as _os
from pathlib import Path as _Path
import sys as _sys
import tempfile as _tempfile

# Install before importing application modules. CLI children inherit the same sandbox.
_real_config_dirs = tuple({_os.path.normcase(_os.path.abspath(path)) for path in (
    _Path(_os.environ.get("APPDATA", _Path.home() / "AppData" / "Roaming")) / "hyperloom",
    _Path(_os.environ.get("LOCALAPPDATA", _Path.home() / "AppData" / "Local")) / "hyperloom",
    _Path.home() / ".config" / "hyperloom",
)})
_temporary = _tempfile.TemporaryDirectory(prefix="hl-tests-", ignore_cleanup_errors=True)
_test_root = _Path(_temporary.name)
# USERPROFILE retains the Windows identity required by Chrome's debugging checks.
# Hyperloom's Windows config/secrets/runtime roots use APPDATA and LOCALAPPDATA.
for _name, _relative in (("APPDATA", "appdata"), ("LOCALAPPDATA", "local"), ("HOME", "home"),
                         ("XDG_CONFIG_HOME", "home/.config")):
    _directory = _test_root / _relative
    _directory.mkdir(parents=True, exist_ok=True)
    _os.environ[_name] = str(_directory)
_os.environ["HYPERLOOM_ADMISSION_DB"] = ":memory:"


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
    else:
        return
    for path in paths:
        if isinstance(path, (str, bytes, _os.PathLike)):
            absolute = _os.path.normcase(_os.path.abspath(_os.fsdecode(path)))
            if any(absolute == root or absolute.startswith(root + _os.sep) for root in _real_config_dirs):
                raise AssertionError("test attempted to modify the real per-user configuration directory")


_sys.addaudithook(_guard_real_config)
