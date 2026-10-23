"""Subprocess controller and watchdog sharing a disk-backed fake Remote."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hyperloom import orchestrate as orch, secrets
from tests.test_orchestrate import FakeRemote, OrchestrationTests, cluster_fixture


directory = Path(sys.argv[1])
secrets.runtime_dir = lambda: directory / "run"
secrets._directory = lambda: directory / "stored"


class DiskRemote(FakeRemote):
    def __init__(self, machine, known_hosts=None):
        super().__init__(machine, [], {}, set())

    def call(self, verb, payload, timeout=60):
        ledger = directory / "leases.json"
        self.leases = orch._read(ledger) if ledger.exists() else {}
        result = super().call(verb, payload, timeout)
        if verb == "lease":
            self.leases[result["lease"]]["state"] = "queued"
        if verb in ("lease", "cancel", "stop"):
            orch._write(ledger, self.leases)
        if verb == "status" and len(self.leases) == 2:
            (directory / "waiting").touch()
        return result


orch._client = lambda machine, known_hosts=None: DiskRemote(machine)
if sys.argv[2] == "watch":
    orch.up_watch(sys.argv[3], sys.argv[4])
else:
    def watch(path, token):
        helper = orch._detached([sys.executable, __file__, str(directory), "watch", str(path), token],
                                directory / "watch.log")
        (directory / "watch.pid").write_text(str(helper.pid))

    orch._start_up_watch = watch
    orch.render.render_processes = lambda cluster, profile: OrchestrationTests.processes(None, cluster, profile)
    orch.up(cluster_fixture(), "qwen-replicas")
