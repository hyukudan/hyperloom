#!/usr/bin/env python3
"""Synthetic curl: writes FAKE_BODY to the --output path; no network."""
import os
import sys
import time

args = sys.argv[1:]
with open(args[args.index("--output") + 1], "ab") as stream:
    stream.write(os.environ.get("FAKE_BODY", "payload").encode())
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
