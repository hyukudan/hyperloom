#!/usr/bin/env python3
"""Synthetic build tools: no fetching, compiling, GPU access or network."""
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
trace = os.environ.get("FAKE_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as stream:
        stream.write(json.dumps([tool, *args]) + "\n")
if "--version" in args or tool == "dpkg-query":
    print(tool + " synthetic version 1")
elif tool == "git":
    if args[0] == "init":
        (Path(args[1]) / ".git").mkdir()
    else:
        source = Path(args[1])
        verb = args[2]
        if verb == "fetch":
            (source / ".fake-commit").write_text(args[-1])
        elif verb == "rev-parse":
            print((source / ".fake-commit").read_text())
elif tool == "cmake":
    Path(args[args.index("-B") + 1]).mkdir(parents=True, exist_ok=True)
elif tool == "ninja":
    directory = Path(args[args.index("-C") + 1])
    for target in args[args.index("-j6") + 1:]:
        binary = directory / target
        binary.write_text("#!/bin/sh\necho 'Usage: synthetic target --help'\nexit "
                          + os.environ.get("FAKE_HELP_EXIT", "0") + "\n")
        binary.chmod(0o755)
elif tool == "ctest":
    if "--show-only=json-v1" in args:
        directory = args[args.index("--test-dir") + 1]
        target = os.environ.get("FAKE_TEST_TARGET", "strix-host-tests")
        print(json.dumps({"tests": [{"name": target, "command": [directory + "/" + target]}]}))
    elif os.environ.get("FAKE_CTEST_FAIL"):
        sys.exit(1)
elif tool == "systemd-run":
    if os.environ.get("FAKE_SYSTEMD") != "1":
        sys.exit(1)
    command = args[args.index("MemoryMax=16G") + 1:]
    os.execvp(command[0], command)
else:
    raise SystemExit("unexpected fake tool")
