"""Synthetic session-cache responses, parsed through the real collector for smoke."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyperloom.web.features import observe, sessioncache


def seed(store, now):
    fixtures = Path(__file__).resolve().parents[2] / "fixtures" / "sessioncache"
    api = SimpleNamespace(orchestrate=SimpleNamespace(_secret_name=lambda p, n: "fixture-node"),
                          config_path=Path(store.path).with_name("cluster.json"),
                          cluster=lambda: SimpleNamespace(profiles={p: {} for p in
                              ("session-demo", "normal-minimax", "qwen-session-demo")}, machines={
                                  "node" + str(i): SimpleNamespace(http_address="engine-" + letter + ".example")
                                  for i, letter in enumerate("abc")}))
    launches = [{"profile": "session-demo", "family": "ds41-pair", "replicas": [
        {"id": "rank0", "node": "node0", "address": "engine-a.example", "port": 18501, "loopback": False}]},
        {"profile": "normal-minimax", "family": "minimax-pair", "replicas": [
        {"id": "rank0", "node": "node1", "address": "engine-b.example", "port": 18501, "loopback": False}]}]
    launches.append({"profile": "qwen-session-demo", "family": "qwen-replicas", "replicas": [
        {"id": "replica0", "node": "node2", "address": "engine-c.example", "port": 18501, "loopback": False}]})
    qwen = json.loads((fixtures / "qwen.json").read_text())
    response = json.loads((fixtures / "deepseek.json").read_text())
    def http(url, headers, deadline, **kw):
        if url.endswith("/v1/sessions"):
            return qwen["list"]
        if url.endswith("/v1/cache/stats"):
            return qwen["stats"]
        return {"status": "ok"} if url.endswith("/health") else response[kw["json_body"]["op"]]
    with patch.object(observe, "launches", return_value=(launches, {"profiles": 0, "replicas": 0})),             patch.object(observe, "_bearer", return_value={}), patch.object(sessioncache, "_http", side_effect=http):
        sessioncache.collect(api, now, store)


def patches(directory, now):
    # Retain seeded responses; never let the browser fixture contact engine endpoints.
    from hyperloom.web.api import API
    real_cluster = API.cluster
    def cluster(api):
        value = real_cluster(api)
        for name in ("session-demo", "normal-minimax", "qwen-session-demo"):
            value.profiles.setdefault(name, {"preset": "qwen-replicas", "nodes": ["halo-1"]})
        return value
    return [(sessioncache, "collect", lambda api, now: []), (API, "cluster", cluster)]
