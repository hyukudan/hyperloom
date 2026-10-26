"""Test doubles for the observability package: launch records, HTTP answers, a clock and a portal API."""

from tests.portable_fixtures import read_fixture
import ipaddress
import json
import socket
from pathlib import Path
from unittest import mock

from hyperloom import orchestrate, secrets
from hyperloom.telemetry.store import Store
from hyperloom.web.api import API
from hyperloom.web.features import observe


HERE = Path(__file__).resolve().parent
KEY = "ab" * 32
LOOPBACK = str(ipaddress.ip_address(socket.INADDR_LOOPBACK))
ROUTER = f"http://{LOOPBACK}:18600"


def doc_address(n):
    """An address of the documentation range (never routed), built from its number."""
    return str(ipaddress.IPv4Address(0xC0000200 + n))


NODE1, NODE2 = doc_address(1), doc_address(2)


def fixture(name):
    text = read_fixture(HERE / name)
    return json.loads(text) if name.endswith(".json") else text


class Clock:
    def __init__(self, now=1_800_000_010.0):
        self.now = now

    def __call__(self):
        return self.now


class Orchestrate:
    """The parts of the orchestrator the collectors read: launch records and secret names."""
    _secret_name = staticmethod(orchestrate._secret_name)

    @staticmethod
    def _read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def status(*args, **kwargs):
        raise AssertionError("the collectors must not call orchestrate.status (ssh)")


class HTTP:
    """url -> answer (dict/str), an exception instance, or a callable(url, headers) -> answer."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def __call__(self, url, headers=None, text=False, deadline=None):
        self.calls.append((url, dict(headers or {})))
        base = url.split("?", 1)[0]
        answer = self.routes.get(url, self.routes.get(base))
        if answer is None:
            raise OSError("connection refused: " + base)
        if callable(answer) and not isinstance(answer, (dict, str)):
            answer = answer(url, headers or {})
        if isinstance(answer, BaseException):
            raise answer
        return json.loads(json.dumps(answer)) if isinstance(answer, dict) else answer


class Env:
    """A portal API on an in-memory store, launch records in a temporary runtime directory and fake HTTP."""

    def __init__(self, directory, now=1_800_000_010.0):
        self.directory = Path(directory)
        self.run = self.directory / "run"
        self.run.mkdir(parents=True, exist_ok=True)
        self.clock = Clock(now)
        self.http = HTTP()
        self.store = Store(":memory:", clock=self.clock)
        self.api = API(config_path=self.directory / "cluster.json", orchestrator=Orchestrate(), clock=self.clock,
                       audit_path=self.directory / "audit.jsonl")
        self.api.telemetry = self.store
        self.patches = [mock.patch.object(secrets, "runtime_dir", lambda: self.run),
                        mock.patch.object(secrets, "get", lambda name: KEY),
                        mock.patch.object(observe, "_http", self.http)]

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        self.store.close()

    def launch(self, profile, replicas, family="qwen-replicas", router=(LOOPBACK, 18600), service_port=None):
        state = {"profile": profile, "router": {"machine": "local", "endpoint": {"address": router[0], "port": router[1]}},
                 "rendered_router": {"listen": {"address": router[0], "port": router[1]},
                                     "replicas": [{"id": r[0], "node": r[0], "address": r[1], "port": r[2]} for r in replicas]},
                 "launch": {"family": family}, "config": orchestrate.config_record(self.api.config_path)}
        if service_port:
            state["prefill_service"] = {"port": service_port}
        (self.run / f"up-{profile}.json").write_text(json.dumps(state), encoding="utf-8")

    def router(self, page=None, metrics=None, sessions=None, base=ROUTER + ""):
        self.http.routes[base + "/api/requests"] = page if page is not None else fixture("router-requests.json")
        self.http.routes[base + "/metrics"] = metrics if metrics is not None else fixture("router-metrics.txt")
        self.http.routes[base + "/api/sessions"] = sessions if sessions is not None else fixture("router-sessions.json")

    def engine(self, address, port, status=None, metrics=None):
        base = f"http://{address}:{port}"
        self.http.routes[base + "/health"] = {"status": "ok"}
        self.http.routes[base + "/api/status"] = status if status is not None else fixture("engine-status-c1.json")
        self.http.routes[base + "/metrics"] = metrics if metrics is not None else fixture("engine-metrics-c1.txt")

    def collect(self, advance=5.0):
        """One sampler round of both collectors (points recorded as the sampler does), then the clock moves on."""
        errors = []
        for sample in (observe.sample_router, observe.sample_engines):
            now = self.clock()
            try:
                self.store.record(sample(self.api, now), ts=now, interval=observe.INTERVAL)
            except RuntimeError as exc:
                errors.append(str(exc))
        self.clock.now += advance
        return errors
