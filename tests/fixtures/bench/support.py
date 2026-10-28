"""Shared set-up for the bench tests: a portal API with the bench feature, an in-memory store and admission
database, a fake chat-completions server, one running profile ("qwen") whose router is the fake server and one
allow-listed ad-hoc target ("fake")."""

from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_server import FakeServer  # noqa: E402

from hyperloom import admission, secrets  # noqa: E402
from hyperloom.telemetry.store import Store  # noqa: E402
from hyperloom.web import features  # noqa: E402
from hyperloom.web.api import API  # noqa: E402


KEY = "ab" * 32


class Bench:
    def __init__(self, test, **server):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.server = FakeServer(**server)
        test.addCleanup(self.server.close)
        features.reset(features.load(("bench",)))
        test.addCleanup(features.reset)
        orchestrator = mock.Mock()
        orchestrator.endpoint.side_effect = lambda profile, config_path=None: {"url": f"http://127.0.0.1:{self.server.port}",
                                                             "key_name": "key-router-fixture"}
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=orchestrator, remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.api.telemetry = Store(":memory:")
        test.addCleanup(self.api.telemetry.close)
        self.api.admission_db = admission.Admission(":memory:")
        test.addCleanup(self.api.admission_db.close)
        machines = {mid: SimpleNamespace(id=mid, http_address=address, ssh_target=mid, access=access)
                    for mid, address, access in (("workstation", "192.168.50.10", "local"),
                                                 ("halo-1", "192.168.50.11", "ssh"), ("halo-2", "192.168.50.12", "ssh"))}
        self.cluster = SimpleNamespace(machines=machines, profiles={"qwen": {"nodes": ["halo-1"]}, "other": {"nodes": ["halo-2"]}},
                                       router={}, raw={})
        self.api.cluster = lambda: self.cluster
        self.profiles = [{"id": "qwen", "state": "running", "family": "qwen-replicas", "release": "r" * 12,
                          "processes": [{"binary_sha256": "b" * 64}], "status": {"router": {"prefill": {"policy": "nodes"}}}},
                         {"id": "other", "state": "stopped", "family": "qwen-replicas", "processes": [], "status": {}}]
        self.api.snapshot = lambda: {"profiles": {"profiles": self.profiles}}
        self.api._resources_of_profile = lambda profile, router=None, shown=False: {"halo-1", "router"} if profile == "qwen" else {"halo-2", "router"}
        patcher = mock.patch.object(secrets, "get", lambda name: KEY if name == "key-router-fixture" else None)
        patcher.start()
        test.addCleanup(patcher.stop)
        from hyperloom.bench import targets
        # The fake server listens on loopback: the control machine's address, so the target holds it like up.
        targets.save(self.api, {"id": "fake", "scheme": "http", "host": "127.0.0.1", "port": self.server.port,
                                "context_tokens": 65536})

    def start(self, action, arguments):
        return self.api.mutate(action, arguments)

    def wait(self, job, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.api.read("jobs/" + job["id"])
            if record["state"] != "running":
                return record
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def run(self, action, arguments, timeout=30):
        job = self.start(action, arguments)
        record = self.wait(job, timeout)
        return record, self.api.read("bench/runs/" + job["arguments"]["run_id"])["run"]
