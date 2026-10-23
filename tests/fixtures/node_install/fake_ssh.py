#!/usr/bin/env python3
"""Execute only our synthetic peer commands locally, without a connection."""
import os
import sys

if sys.argv[sys.argv.index("--") + 1] != "halo-2":
    raise SystemExit("unexpected synthetic peer")
os.execv("/bin/bash", ["bash", "-c", sys.argv[-1]])
