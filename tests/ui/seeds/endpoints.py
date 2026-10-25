"""Fixture endpoints for the browser smoke: three allow-listed targets (one of each outcome), fleet listener tables,
an hour of reachability and token history, and a fake transport so the real collectors and Check now run against
canned replies instead of the network."""

import json
from pathlib import Path

from hyperloom.web.features import endpoints


FIXTURES = Path(__file__).resolve().parents[1].parent / "fixtures" / "endpoints"
STATUS = json.loads((FIXTURES / "strix-status.json").read_text(encoding="utf-8"))
METRICS = (FIXTURES / "strix-metrics.txt").read_text(encoding="utf-8")
TARGETS = [
    {"id": "rtx-box", "rev": "a1" * 8, "label": "RTX box", "scheme": "http", "host": "192.0.2.20", "port": 8080,
     "key_name": None, "added": 0},
    {"id": "lab-engine", "rev": "b2" * 8, "label": "Lab engine", "scheme": "http", "host": "halo-2", "port": 18431,
     "key_name": "engine-key", "added": 0},
    {"id": "spare", "rev": "c3" * 8, "label": "Spare server", "scheme": "http", "host": "192.0.2.40", "port": 9000,
     "key_name": None, "added": 0},
]
MODELS = {"object": "list", "data": [{"id": "qwen3.8-flash-next", "object": "model", "max_model_len": 32768}]}


class FakeSession(endpoints.Session):
    """Canned replies per (address, port); everything else is refused, like a closed port."""

    def __init__(self, scheme, address, port, key=None, **kw):
        super().__init__(scheme, address, port, key, **kw)

    def get(self, path, auth=True):
        if self.left <= 0:
            return endpoints.Reply(error="request budget for this round used up")
        self.left -= 1
        self.requests.append(path)
        where = (self.address, self.port)
        reply = lambda obj, status=200: endpoints.Reply(status, json.dumps(obj).encode(), 4.2)
        if where == ("192.0.2.20", 8080):          # a props-server answering without credentials
            if path == "/props":
                return reply({"default_generation_settings": {"n_ctx": 32768}})
            if path == "/v1/models":
                return reply(MODELS)
            return endpoints.Reply(404, b"", 1.0)
        if where in (("192.0.2.11", 18431), ("192.0.2.10", 18501), ("192.0.2.11", 18501)):   # our engines
            if not (auth and self.key) and path == "/v1/models":
                return endpoints.Reply(401, b"{}", 1.5)
            if path == "/api/status":
                return reply(STATUS)
            if path == "/metrics":
                return endpoints.Reply(200, METRICS.encode(), 2.0)
            if path == "/v1/models":
                return reply(MODELS)
            return endpoints.Reply(404, b"", 1.0)
        return endpoints.Reply(error="connection refused", ms=0.5)


def seed(store, now):
    store.put(endpoints.PREFS, endpoints.TARGETS_KEY, {"version": 1, "targets": TARGETS})
    for machine, ports in (("halo-1", [(18501, "lan"), (22, "any")]), ("halo-2", [(18431, "lan"), (18501, "lan")])):
        store.put("fleet/host", machine, {"ts": now, "ok": True, "error": None, "host": {
            "schema": "hyperloom/node-v1", "verb": "host",
            "checks": {"listeners": {"status": "ok", "value": [{"port": p, "family": "ipv4", "scope": s} for p, s in ports]}}}})
    for name, kind, unit, legend in endpoints.SERIES:
        store.describe(name, kind, unit=unit, interval=endpoints.INTERVAL_S, legend=legend)
    t = now - 3600
    while t < now:
        offline = now - 1500 < t < now - 1200                       # a gap: the samplers were stopped
        if not offline:
            store.record([("endpoint_up", {"endpoint": "rtx-box"}, 1, "state"),
                          ("endpoint_up", {"endpoint": "lab-engine"}, 0 if now - 2400 < t < now - 2100 else 1, "state"),
                          ("endpoint_up", {"endpoint": "spare"}, 0, "state"),
                          ("endpoint_generated_tokens", {"endpoint": "lab-engine"}, 180 + (t % 900) / 5, "delta"),
                          ("endpoint_prompt_tokens", {"endpoint": "lab-engine"}, 900 + (t % 600), "delta")], ts=t, interval=60)
        t += 60


def patches(directory, now):
    from hyperloom import secrets
    secrets.ensure("engine-key")              # the lab engine's key, in the fixture's own secret directory
    # The fleet answers above stay fresh for the whole smoke run.
    return [(endpoints, "Session", FakeSession), (endpoints, "LISTENER_FRESH_S", 10 ** 9)]
