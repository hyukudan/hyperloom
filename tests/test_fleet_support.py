"""Shared fakes for the fleet tests (no tests here): machines, an API double with an in-memory store, recorded node
answers and launch records."""

from tests.portable_fixtures import read_fixture
import copy
import ipaddress
import json
import socket
from pathlib import Path
from types import SimpleNamespace

from hyperloom import config
from hyperloom.telemetry.store import Store


LOOPBACK = str(ipaddress.IPv4Address(socket.INADDR_LOOPBACK))
LAN = str(ipaddress.IPv4Address((10 << 24) + 9))           # a private address, never contacted
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fleet"


def host(name):
    return json.loads(read_fixture(FIXTURES / f"host-{name}.json"))


def nvidia_csv():
    return read_fixture(FIXTURES / "nvidia-gpus.csv"), read_fixture(FIXTURES / "nvidia-apps.csv")


def machine(mid, os_name="linux", access="ssh", devices=None):
    # The address field is never used by these tests (node calls are faked).
    return config.Machine(mid, os_name, access, "unused", mid if access == "ssh" else None, 22, "/opt/hyperloom/" + mid,
                          True, devices or [{"id": "gpu"}])


def lab():
    machines = {"workstation": machine("workstation", "windows", "local", [{"id": "GPU-d4e5f6a7", "excluded": True}]),
                "halo-1": machine("halo-1"), "halo-2": machine("halo-2")}
    return SimpleNamespace(machines=machines, profiles={}, router={}, pc={}, raw={}, assets={})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeAPI:
    def __init__(self, directory, cluster=None, clock=None):
        self.config_path = Path(directory) / "cluster.json"
        self._cluster = cluster or lab()
        self.clock = clock or Clock()
        self.telemetry = Store(":memory:", clock=self.clock)
        self.orchestrate = SimpleNamespace(_read=lambda path: {})
        self.answers = {}
        self.calls = []

    def cluster(self):
        return self._cluster

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", "server_time": self.clock(), "poll_interval_s": 2, "why": {}, **fields}

    def node(self, machine, verb, payload=None):
        self.calls.append((machine.id, verb, copy.deepcopy(payload)))
        answer = self.answers[machine.id]
        if isinstance(answer, Exception):
            raise answer
        return answer(payload) if callable(answer) else copy.deepcopy(answer)

    def audit(self, action, arguments, result):
        self.calls.append(("audit", action, result.get("state")))


def bump(answer, rx=0, tx=0, read=0, boot=None, uptime=None):
    """A later answer of the same node: counters advanced, optionally another boot."""
    answer = copy.deepcopy(answer)
    checks = answer["checks"]
    for row in checks["net"]["value"]:
        row["rx_bytes"] += rx
        row["tx_bytes"] += tx
    for row in checks["diskstats"]["value"]:
        row["read_bytes"] += read
    if boot:
        checks["boot_id"]["value"] = boot
    if uptime is not None:
        checks["uptime_s"]["value"] = uptime
    return answer


def pair_records(port=18441, started=1_799_999_000.0):
    """Launch records: a loopback pair (rank 0 on halo-1) and a LAN replica profile on halo-2."""
    return {
        "mm": {"profile": "mm", "nodes": ["halo-1", "halo-2"], "started_at": started, "launch": {"family": "minimax-pair"},
               "process_specs": [{"machine_id": "halo-1", "role": "rank0"}, {"machine_id": "halo-2", "role": "rank1"}],
               "router": {"machine": "halo-1"},
               "rendered_router": {"replicas": [{"id": "halo-1", "node": "halo-1", "address": LOOPBACK, "port": port}]}},
    }


def replica_records():
    return {"qw": {"profile": "qw", "nodes": ["halo-2"], "started_at": 1_799_999_500.0, "launch": {"family": "qwen-replicas"},
                   "router": {"machine": "local"}, "prefill_service": {"machine": "workstation", "port": 18900},
                   "rendered_router": {"replicas": [{"id": "halo-2", "node": "halo-2", "address": LAN, "port": 18501}]}}}
