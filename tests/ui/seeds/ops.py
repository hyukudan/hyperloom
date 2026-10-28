"""Operations fixtures for the browser smoke: the turn queues of halo-1/halo-2 rebuilt from the recorded turn files
(clock times shifted to now) over a fake /proc, a big-job ledger, log files to search, node tool digests, a MAC
and a shutdown helper path. Node verbs of this package go through hyperloom.web.features.ops._node_call, which
these patches replace; nothing leaves the fixture process."""

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "ops"


def _module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "node" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shift_clock(text, delta):
    def move(match):
        seconds = (int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3]) + delta) % 86400
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return re.sub(r"\b(\d{2}):(\d{2}):(\d{2})\b", move, text)


def _local_seconds(epoch):
    local = time.localtime(epoch)
    return local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec


def _lab(directory, now):
    """Turn state trees and a fake /proc for both nodes."""
    trees, recorded = {}, 1792886970.0        # when the fixture files were recorded (02:09:30 node time)
    delta = int(round(now - recorded))
    clock_delta = _local_seconds(now) - (2 * 3600 + 9 * 60 + 30)
    proc = directory / "ops-proc"
    (proc / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    boot = now - 400000
    (proc / "stat").write_text(f"btime {int(boot)}\n")
    (proc / "uptime").write_text(f"{now - boot:.2f} 1.00\n")
    (proc / "sys/kernel/random/boot_id").write_text("00000000-0000-4000-8000-000000000001\n")

    def process(pid, started, label, pair=False):
        path = proc / str(pid)
        path.mkdir(exist_ok=True)
        ticks = int((started - boot) * 100)
        (path / "stat").write_text(f"{pid} (bash) S " + " ".join(["0"] * 18 + [str(ticks), "0"]) + "\n")
        script = "/opt/lab/gpu-turn-pair.sh" if pair else "/opt/lab/gpu-turn.sh"
        (path / "cmdline").write_bytes(b"\0".join([b"/bin/bash", script.encode(), label.encode(), b"./run.sh"]) + b"\0")

    for mid in ("halo-1", "halo-2"):
        state = directory / "ops-lab" / mid / ".gpu-turn"
        state.mkdir(parents=True, exist_ok=True)
        source = FIXTURES / mid
        holder = _shift_clock((source / "holder").read_text(), clock_delta)
        (state / "holder").write_text(holder)
        log = _shift_clock((source / "log").read_text(), clock_delta)
        (state / "log").write_text(log)
        os.utime(state / "log", (now - 20, now - 20))
        tickets = json.loads((source / "tickets.json").read_text())
        for queue, names in tickets.items():
            if names or queue == "single-q" or mid == "halo-1":
                (state / queue).mkdir(exist_ok=True)
            for index, name in enumerate(names[:6]):
                ns, pid = name.rsplit("-", 1)
                shifted = int(ns) + delta * 1_000_000_000
                (state / queue / f"{shifted}-{pid}").write_bytes(b"")
                label = re.findall(rf"wait  (?:PAIR )?(\S+) pid {pid}\b", log)
                process(int(pid), shifted / 1e9 - 0.5, label[-1] if label else f"job-{index}", pair=queue == "pair-q")
        holder_pid = re.search(r"pid (\d+)", holder)
        if holder_pid:
            process(int(holder_pid[1]), now - 900, holder.split()[1] if holder.startswith("PAIR") else holder.split()[0],
                    pair=holder.startswith("PAIR"))
        trees[mid] = state.parent
    return trees, proc, boot


LOGS = {"lease-qwen-replica.stdout.log": ["READY fixture replica on 127.0.0.1", "request done in 412 ms",
                                          "WARN slot wait 2.1 s (queue full)", "request done in 388 ms",
                                          "ERROR upstream closed the stream api_key=fixture-secret-value",
                                          "request done in 401 ms"],
        "job-build.log": ["configure ok", "building 12 targets", "build finished"]}


def _install_roots(directory):
    roots = {}
    for mid in ("halo-1", "halo-2"):
        root = directory / "ops-install" / mid
        (root / "log").mkdir(parents=True, exist_ok=True)
        (root / ".hyperloom-root").write_text("fixture\n")
        for name, lines in LOGS.items():
            (root / "log" / name).write_text("\n".join(f"{mid} {line}" for line in lines) + "\n")
        roots[mid] = root
    lab = directory / "ops-runs"
    lab.mkdir(exist_ok=True)
    (lab / "sweep-a.log").write_text("arm 1: 41.2 tok/s\narm 2: ERROR out of memory\narm 3: 40.8 tok/s\n")
    return roots, lab


CONFIG = {"schema": "hyperloom/cluster-v1",
          "machines": [{"id": "workstation", "os": "windows", "access": "local", "http_address": "192.0.2.20",
                        "install_root": "C:/hyperloom", "lab_mode": True},
                       {"id": "halo-1", "os": "linux", "access": "ssh", "http_address": "192.0.2.10", "ssh_target": "halo-1",
                        "ssh_port": 22, "install_root": "/opt/hyperloom/halo-1", "lab_mode": True, "lab_dir": "/opt/lab",
                        "devices": [{"id": "gpu"}]},
                       {"id": "halo-2", "os": "linux", "access": "ssh", "http_address": "192.0.2.11", "ssh_target": "halo-2",
                        "ssh_port": 22, "install_root": "/opt/hyperloom/halo-2", "lab_mode": True, "lab_dir": "/opt/lab",
                        "devices": [{"id": "gpu"}]}],
          "assets": {}, "profiles": {"qwen-replicas": {"nodes": ["halo-1"]},
                                     "qwen-two": {"preset": "qwen-replicas", "nodes": ["halo-1", "halo-2"]}},
          "router": {}, "pc": {}}


def seed(store, now):
    directory = Path(store.path).parent.parent
    # The configuration file the machine editor reads: the fixture cluster's three machines and two profiles.
    (Path(store.path).parent / "cluster.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    ledger = directory / "coordination.md"
    lines = []
    for minutes, text in ((-190, "job-a: PC-BIG start (sweep 1, ~20 min; tope 1500 s)"), (-168, "job-a: PC-BIG end (rc 0)"),
                          (-95, "job-b: **PC-BIG start** timing runs (cap 30 min)"), (-61, "job-b: PC-BIG end"),
                          (-12, "job-c: PC-BIG start (one window, tope 1200 s)")):
        stamp = time.localtime(now + minutes * 60)
        lines.append(f"- {stamp.tm_hour:02d}:{stamp.tm_min:02d} {text}")
    ledger.write_text("# coordination\n" + "\n".join(lines) + "\n", encoding="utf-8")
    store.put("turns", "settings", {"ledger_path": str(ledger), "ledger_cap_s": 1800, "pair_cap_s": 480})
    helper = directory / "shutdown-helper.cmd"
    helper.write_text("@echo fixture helper (never run by the smoke)\n")
    store.put("ops", "shutdown_helper", str(helper))
    store.put("ops", "machines", {"halo-2": {"mac": "02:00:5e:10:00:02"}})
    store.put("logsearch", "roots", {"halo-1": [str(directory / "ops-runs")]})


def patches(directory, now):
    from hyperloom import remote
    from hyperloom.web.features import ops, turns
    turns_node = _module("hyperloom_turns_fixture", "hyperloom_turns.py")
    search_node = _module("hyperloom_logsearch_fixture", "hyperloom_logsearch.py")
    trees, proc, boot = _lab(directory, now)
    installs, lab = _install_roots(directory)
    expected = remote.tool_digest()
    uid = os.stat(directory).st_uid

    def node_call(api, machine, verb, payload=None):
        payload = payload or {}
        if verb == "turns":
            current = time.time()
            (proc / "uptime").write_text(f"{current - boot:.2f} 1.00\n")     # a live uptime for the fixed boot time
            return turns_node.collect(trees[machine.id], proc=proc, now=current, uid=uid)
        if verb == "logs-search":
            local = dict(payload)
            if "root" in local:
                local["root"] = str(installs.get(machine.id, directory / "missing"))
            local["roots"] = [str(lab) if Path(r).name == "ops-runs" else r for r in local.get("roots", [])]
            local.pop("exclude", None)
            return search_node.run("logs-search", local)
        if verb == "version":
            return {"schema": "hyperloom/node-v1", "verb": "version",
                    "checks": {"tool": {"status": "ok", "value": expected if machine.id == "halo-1" else "0" * 64}}}
        if verb == "host":
            return {"schema": "hyperloom/node-v1", "verb": "host", "interfaces": [{"name": "eth0", "mac": "02:00:5e:10:00:01"}]}
        if verb == "status":
            return {"schema": "hyperloom/node-v1", "verb": "status", "leases": []}
        return api.node(machine, verb, payload)

    real_eligible = turns.eligible

    def eligible(cluster):
        chosen, skipped = real_eligible(cluster)
        for mid in list(skipped):
            machine = cluster.machines[mid]
            if machine.os == "linux" and machine.access == "ssh":
                skipped.pop(mid)
                chosen.append(dataclasses.replace(machine, lab_dir="/opt/lab"))
        return chosen, skipped

    return [(ops, "_node_call", node_call), (turns, "eligible", eligible)]
