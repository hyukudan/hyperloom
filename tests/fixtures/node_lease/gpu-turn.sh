#!/usr/bin/env python3
"""Fake turn: record args/env, wait if queued, publish holder, run supervisor."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

lab = Path(__file__).resolve().parent
turn = lab / ".gpu-turn"
turn.mkdir(exist_ok=True)
(turn / "invocation.json").write_text(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))
while (turn / "block").exists():
    time.sleep(0.05)
pair = "pair" in Path(__file__).name
(turn / "holder").write_text(("PAIR " if pair else "") + sys.argv[1] + " since synthetic")
raise SystemExit(subprocess.call(sys.argv[2:]))
