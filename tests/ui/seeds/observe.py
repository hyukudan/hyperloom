"""Fixture data for the observability pages: the portal's own collectors run on replayed component answers.

The router request log, sessions and histograms and the engine status/metrics are the synthetic fixtures of
tests/fixtures/observe, with their timestamps moved to the fixture's start; a loopback engine is served as the node
sampler would hand it over. History series (TTFT, acceptance, generated tokens) get an hour of samples with a gap.
"""

from tests.portable_fixtures import read_fixture
import copy
import ipaddress
import json
import socket
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "observe"
LOOPBACK = str(ipaddress.ip_address(socket.INADDR_LOOPBACK))
BASE = 1_800_000_000
NODE1 = str(ipaddress.IPv4Address(0xC0000201))         # documentation range, never routed


def _load(name):
    text = read_fixture(FIXTURES / name)
    return json.loads(text) if name.endswith(".json") else text


def _shift(now):
    """Fixture times are around BASE; move them so the newest request ended a few seconds ago."""
    return int(now) - 30 - BASE


def seed(store, now):
    labels = {"profile": "qwen-replicas", "replica": "halo-1", "machine": "halo-1", "model": "qwen3.8-flash-next",
              "instance": "5a5a5a5a5a5a5a5a"}
    store.describe("engine_ttft_seconds_p50", "gauge", unit="s", interval=30)
    store.describe("engine_draft_acceptance", "gauge", interval=30)
    store.describe("engine_draft_accepted_per_cycle", "gauge", unit="tokens", interval=30)
    store.describe("engine_generated_tokens", "delta", unit="tokens", interval=30)
    store.describe("engine_queue_waiting", "gauge", interval=30)
    t = now - 3600
    while t < now - 60:
        if not now - 1500 < t < now - 1100:          # an idle window with no samples: a gap, not zeros
            wave = (t % 900) / 900
            store.record([("engine_ttft_seconds_p50", {"kind": "router", "profile": "qwen-replicas", "model": labels["model"]}, 0.6 + 0.4 * wave),
                          ("engine_ttft_seconds_p50", {**labels, "kind": "engine", "basis": "first_token"}, 0.2 + 0.3 * wave),
                          ("engine_draft_acceptance", {**labels, "method": "mtp"}, 0.68 + 0.1 * wave),
                          ("engine_draft_accepted_per_cycle", {**labels, "method": "mtp"}, 1.9 + 0.6 * wave),
                          ("engine_generated_tokens", labels, round(450 + 210 * wave), "delta"),
                          ("engine_queue_waiting", labels, round(3 * wave))], ts=t, interval=30)
        t += 30


def patches(directory, now):
    from hyperloom.web.features import observe

    shift = _shift(now)
    page = _load("router-requests.json")
    for record in page["records"]:
        record["at"] += shift
    sessions = _load("router-sessions.json")
    for session in sessions["sessions"]:
        session["first_seen"] += shift
        session["last_seen"] += shift
        for turn in session["recent"]:
            turn["at"] += shift
    status = _load("engine-status-c1.json")
    status["instance"]["start_time"] += shift
    for request in status["requests"]:
        request["time"] += shift
    minimax = _load("minimax-status.json")
    minimax["instance"]["start_time"] += shift
    for request in minimax["requests"]:
        request["time"] += shift
    router = f"http://{LOOPBACK}:18600"
    answers = {router + "/api/requests": page, router + "/metrics": _load("router-metrics.txt"),
               router + "/api/sessions": sessions, f"http://{NODE1}:18501/health": {"status": "ok"}, f"http://{NODE1}:18501/api/status": status,
               f"http://{NODE1}:18501/metrics": _load("engine-metrics-c1.txt").replace(
                   "strix_engine_start_time_seconds 1799990000", f"strix_engine_start_time_seconds {1799990000 + shift}")}

    def http(url, headers=None, text=False, deadline=None):
        answer = answers.get(url.split("?", 1)[0])
        if answer is None:
            raise observe.Unavailable("HTTP 404")
        return copy.deepcopy(answer)

    def launches(api):
        return ([{"profile": "qwen-replicas", "router": router, "key_name": "fixture-router", "service_key": "fixture-prefill",
                  "replicas": [{"id": "halo-1", "node": "halo-1", "address": NODE1, "port": 18501, "loopback": False}],
                  "family": "qwen-replicas", "service_port": None},
                 {"profile": "minimax-pair", "router": f"http://{LOOPBACK}:18610", "key_name": "fixture-router-mm",
                  "service_key": "fixture-prefill-mm",
                  "replicas": [{"id": "halo-2", "node": "halo-2", "address": LOOPBACK, "port": 18601, "loopback": True}],
                  "family": "minimax-pair", "service_port": None}], {"profiles": 0, "replicas": 0})

    def via_fleet(store, profile, replica, observed):
        return {"status": copy.deepcopy(minimax), "metrics_text": _load("minimax-metrics-sched.txt"), "health": True,
                "why": {}, "observed_at": time.time(), "via": "node (fleet sampler)", "source_key": "halo-2:minimax-pair:18601"}

    return [(observe, "_http", http), (observe, "launches", launches), (observe, "_read_via_fleet", via_fleet)]
