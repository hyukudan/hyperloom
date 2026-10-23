"""Synthetic OpenSSH stand-in; never opens a network connection."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


argv = sys.argv[1:]
mode = os.environ.get("TELAR_FAKE_MODE", "ok")
log = os.environ.get("TELAR_FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": argv, "pid": os.getpid()}) + "\n")

if argv[0] == "ssh-keyscan":
    if mode == "empty":
        raise SystemExit(0)
    if mode == "malformed_scan":
        print("not a key")
        raise SystemExit(0)
    key = os.environ["TELAR_FAKE_KEY"]
    print("# synthetic keyscan")
    print(f"[192.0.2.1]:2222 ssh-ed25519 {key}")
    print(f"[192.0.2.1]:2222 ssh-ed25519 {key}")
    raise SystemExit(0)

for flag in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=none",
             "BatchMode=yes", "ConnectTimeout=10"):
    if flag not in argv:
        raise SystemExit(90)
known = next(shlex.split(value.split("=", 1)[1])[0].replace("%%", "%")
             for value in argv if value.startswith("UserKnownHostsFile="))
expected = os.environ["TELAR_FAKE_KEY"]
if expected not in Path(known).read_text(encoding="utf-8"):
    raise SystemExit(255)
if argv[0] == "scp":
    raise SystemExit(0)

if mode == "badexit":
    raise SystemExit(255)
if mode == "sleep":
    time.sleep(30)
if mode == "descendant":
    descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path(os.environ["TELAR_FAKE_CHILD_LOG"]).write_text(json.dumps({"pid": descendant.pid}))
    time.sleep(30)
if mode == "huge":
    sys.stdout.write("x" * (1024 * 1024))
    raise SystemExit(0)
if mode == "stderr_huge":
    sys.stderr.write("x" * (1024 * 1024))
    raise SystemExit(0)

command = argv[-1]
if command.startswith("umask ") or command.startswith("printf "):
    sys.stdout.buffer.write("/opt/operator space ' Ω/.telar-boot/telar-node\n".encode("utf-8"))
elif command.startswith("chmod "):
    pass
elif mode == "eof":
    pass
elif mode == "malformed":
    print("{}\n{}")
elif mode == "duplicate":
    print('{"same":1,"same":2}')
elif mode == "wrongtype":
    print("[]")
elif mode == "nonutf8":
    sys.stdout.buffer.write(b"\xff")
else:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    result = {"verb": command.rsplit(" ", 1)[-1], "payload": payload}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
