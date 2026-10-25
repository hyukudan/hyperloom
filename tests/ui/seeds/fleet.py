"""Fixture fleet data for the browser smoke: recorded node answers for halo-1 and halo-2, a control machine with two
cards, eight hours of history with an offline gap, and a LAN replica on halo-1 as the engine collector records it.
The portal's own fleet collectors keep running against these answers (node calls and system reads are faked)."""

import copy
import itertools
import json
import math
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "fleet"


def _host(name):
    return json.loads((FIXTURES / f"host-{name}.json").read_text(encoding="utf-8"))


def _control(machines):
    gpus = (FIXTURES / "nvidia-gpus.csv").read_text(encoding="utf-8")
    apps = (FIXTURES / "nvidia-apps.csv").read_text(encoding="utf-8")
    gib = 2 ** 30
    return {"schema": "hyperloom/node-v1", "verb": "host", "checks": {
        "uptime_s": {"status": "ok", "value": 93000.0},
        "memory": {"status": "ok", "value": {"MemTotal": 128 * gib, "MemAvailable": 61 * gib, "CommitTotal": 98 * gib,
                                             "CommitLimit": 152 * gib, "CommitAvailable": 54 * gib}},
        "cpu": {"status": "ok", "value": {"busy_percent": 12.5, "cores": 32}},
        "load": {"status": "unavailable", "value": None, "why": "Windows has no load average"},
        "sensors": {"status": "unavailable", "value": None, "why": "no CPU sensor readable without elevated access"},
        "disks": {"status": "ok", "value": [{"mount": "C:", "fstype": None, "total_bytes": 2000 * gib, "used_bytes": 1460 * gib,
                                             "free_bytes": 540 * gib}]},
        "diskstats": {"status": "unavailable", "value": None, "why": "per-disk counters are not read on Windows"},
        "net": {"status": "ok", "value": [{"iface": "Ethernet", "luid": 1, "rx_bytes": 10 ** 9, "tx_bytes": 2 * 10 ** 9,
                                           "physical": True, "operstate": "up"}]},
        "gpus": {"status": "unavailable", "value": None, "why": "no AMD GPU on this machine"},
        "gpu_processes": {"status": "unavailable", "value": None, "why": "see the processes of each NVIDIA card"},
        "listeners": {"status": "unavailable", "value": None, "why": "not read on the control machine"},
    }}, {"status": "ok", "value": machines.parse_nvidia(gpus, apps)}


def seed(store, now):
    """Eight hours of node series (one sample every 5 min, every 15 s in the last hour) with a 20-minute offline gap
    on halo-2."""
    for name, unit in (("node_gpu_busy_percent", "%"), ("node_gpu_power_watts", "W"), ("node_gpu_power_limit_watts", "W"),
                       ("node_gpu_temp_celsius", "celsius"), ("node_cpu_busy_percent", "%")):
        store.describe(name, "gauge", unit=unit, interval=5)
    devices = {"halo-1": [("card0", "PPT", None, "amdgpu/edge")], "halo-2": [("card0", "PPT", None, "amdgpu/edge")],
               "workstation": [("GPU-a0b1c2d3", "board", 498.0, "gpu"), ("GPU-d4e5f6a7", "board", 450.0, "gpu")]}
    batches = []
    t = now - 8 * 3600
    while t < now:
        for machine, cards in devices.items():
            if machine == "halo-2" and now - 3000 < t < now - 1800:
                continue
            phase = (t / 900.0) + (1.0 if machine == "halo-2" else 0.0)
            points = [("node_cpu_busy_percent", {"machine": machine}, 20 + 15 * math.sin(phase))]
            for index, (device, sensor, limit, temp) in enumerate(cards):
                busy = max(0.0, min(100.0, 55 + 40 * math.sin(phase + index)))
                labels = {"machine": machine, "device": device}
                points += [("node_gpu_busy_percent", labels, busy),
                           ("node_gpu_power_watts", {**labels, "sensor": sensor}, (limit or 120) * (0.15 + 0.6 * busy / 100)),
                           ("node_gpu_temp_celsius", {**labels, "sensor": temp}, 42 + 0.35 * busy)]
                if limit:
                    points.append(("node_gpu_power_limit_watts", labels, limit))
            step = 300 if t < now - 3600 else 15     # coarse history, the last hour every 15 s
            batches.append((points, t, step))
        t += 300 if t < now - 3600 else 15

    # One transaction for the whole seed (thousands of single-sample transactions are slow on some file systems).
    def insert(pending):
        for points, ts, step in batches:
            store._insert(pending, store._rows(points), ts, step)
    store._atomic(insert)


def patches(directory, now):
    from hyperloom.web.features import machines

    answers = {"halo-1": _host("halo-1"), "halo-2": _host("halo-2")}
    rounds = itertools.count()

    def call_node(api, machine, payload):
        step = next(rounds)
        answer = copy.deepcopy(answers[machine.id])
        for row in answer["checks"]["net"]["value"]:
            if row["operstate"] == "up":
                row["rx_bytes"] += step * 2_500_000
                row["tx_bytes"] += step * 400_000
        for row in answer["checks"]["diskstats"]["value"]:
            row["read_bytes"] += step * 8_000_000
            row["write_bytes"] += step * 1_000_000
        return answer

    ticks = itertools.count()

    def control_host(state):
        answer, _ = _control(machines)
        answer["checks"]["net"]["value"][0]["rx_bytes"] += next(ticks) * 1_200_000
        return answer

    def launch_records(api):
        return {"qwen-replicas": {"profile": "qwen-replicas", "nodes": ["halo-1"], "started_at": now - 3600,
                                  "launch": {"family": "qwen-replicas"}, "router": {"machine": "local"},
                                  "rendered_router": {"replicas": [{"id": "halo-1", "node": "halo-1", "port": 18501,
                                                                     "address": "lan"}]}}}

    return [(machines, "_call_node", call_node), (machines, "_control_host", control_host),
            (machines, "_control_nvidia", lambda: _control(machines)[1]), (machines, "launch_records", launch_records)]
