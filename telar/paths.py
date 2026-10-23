"""Repository resources used by the editable control-machine installation."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent


def repo_root():
    missing = [name for name in ("node", "versions.lock")
               if not ((_ROOT / name).is_dir() if name == "node" else (_ROOT / name).is_file())]
    if missing:
        raise RuntimeError("Telar repository resources missing: " + ", ".join(missing)
                           + "; install telar from its checkout with pip install -e .")
    return _ROOT
