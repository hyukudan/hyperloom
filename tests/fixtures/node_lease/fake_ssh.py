#!/usr/bin/env python3
"""Local-only ssh stand-in; never opens a network connection."""
import os
from pathlib import Path
import shlex
import sys

target = sys.argv[sys.argv.index("--") + 1]
if target != "halo-2":
    raise SystemExit(2)
boot = Path(__file__).resolve().parent / "peer-boot" / "hyperloom-node"
verb = shlex.split(sys.argv[-1])[-1]
os.execv(sys.executable, [sys.executable, str(boot), verb])
