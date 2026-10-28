"""Fixture data for the ledger pages: 30 days of hour records for two models on two machines (with an evening without
engines, a restart, an unreachable hour and a partly reported hour), their durable scrape intervals, the counted engines,
a price table, a census directory with the census fixtures and a run-log directory with the batch-run fixture. In the
fixture portal a fake DeepSeek engine answers the ledger collector (so the live throughput is real) and a fake RTX
prefill service reports provider counters and strix-stream statistics."""

import json
import math
from pathlib import Path
import shutil
import time

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ledger"
SOURCES = (("qwen", "halo-1", "halo-1", "qwen3.8-flash-next", 1.0), ("qwen", "halo-2", "halo-2", "qwen3.8-flash-next", 0.8),
           ("ds", "halo-1", "halo-1", "deepseek41", 0.45))


def _listed(profile, hours_ago):
    return not 200 <= hours_ago < 206 and not (profile == "ds" and hours_ago % 24 < 8)


def seed(store, now):
    from hyperloom.web.features import ledger
    ledger._prepared(store)
    end = int(now // 3600) * 3600
    for hours_ago in range(30 * 24, 0, -1):
        hour = end - hours_ago * 3600
        daily = 0.35 + 0.65 * max(0.0, math.sin((hour % 86400) / 86400 * 2 * math.pi - 1.2))
        for profile, replica, machine, model, weight in SOURCES:
            if not _listed(profile, hours_ago):
                continue
            generated = round(42000 * weight * daily)
            prompt = round(generated * (5.5 if profile == "ds" else 3.2))
            cached = round(prompt * (0.18 if profile == "ds" else 0.41))
            restart = hours_ago == 30 and profile == "qwen" and replica == "halo-1"
            observed = 1800.0 if restart else 3600.0
            for kind, tokens, span in (("generated", generated, 3600.0), ("prompt", prompt, 3600.0),
                                       ("cached", cached, 1800.0 if hours_ago == 12 and profile == "ds" else 3600.0),
                                       ("imported", 0, 3600.0)):
                store.execute(ledger.UPSERT, (hour, profile, replica, machine, model, kind, float(tokens), span, 3600.0,
                                              observed, 0.0, 0.0, 0.0))
            key = f"{profile}/{replica}"
            if restart:
                store.execute("INSERT INTO ledger_intervals(source, start, end, observed) VALUES (?, ?, ?, 1), (?, ?, ?, 0), "
                              "(?, ?, ?, 1)", (key, hour, hour + 900, key, hour + 900, hour + 2700, key, hour + 2700, hour + 3600))
            elif not (profile == "ds" and hours_ago == 52):
                store.execute("INSERT INTO ledger_intervals(source, start, end, observed) VALUES (?, ?, ?, 1)",
                              (key, hour, hour + 3600))
    store.execute("INSERT OR IGNORE INTO ledger_gaps(ident, start, end, profile, replica, machine, model, kind, reason, "
                  "tokens, instance) VALUES ('seed-restart', ?, ?, 'qwen', 'halo-1', 'halo-1', 'qwen3.8-flash-next', "
                  "'generated', 'engine restarted (new instance)', 1840, 'start:seed')", (end - 30 * 3600 + 900, end - 30 * 3600 + 2700))
    store.execute("INSERT INTO ledger_unreachable(source, profile, replica, machine, since, until, closed_by, why) "
                  "VALUES ('ds/halo-1', 'ds', 'halo-1', 'halo-1', ?, ?, 'scrape', 'engine /metrics unavailable')",
                  (end - 52 * 3600, end - 51 * 3600))
    for profile, replica, machine, model, _ in SOURCES[:2]:
        store.put(ledger.SOURCES, f"{profile}/{replica}", {
            "profile": profile, "replica": replica, "machine": machine, "model": model, "family": "qwen-replicas",
            "import_path": False, "reported": ["cached", "generated", "prompt"], "instance": f"start:seed-{replica}",
            "identity": "derived start time (bracketed status reads)", "last_ok": now - 3600, "last_try": now - 3600,
            "error": "profile stopped", "observer": ledger.OBSERVER})
    store.put(ledger.PREFS, "prices", {"currency": "EUR", "default": {"input": 0.6, "cached": 0.06, "output": 2.4},
                                       "models": {"deepseek41": {"input": 1.2, "cached": 0.12, "output": 4.8}}})
    store.put("prefs/ui", "timezone", "UTC")


def patches(directory, now):
    from hyperloom.telemetry.store import Store
    from hyperloom.web.features import ledger
    census, runs = Path(directory) / "census", Path(directory) / "runs"
    census.mkdir(exist_ok=True)
    runs.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / "census-qwen.json", census)
    shutil.copy(FIXTURES / "census-v1-ds.json", census)
    shutil.copy(FIXTURES / "rtx-run.log", runs)
    store = Store(Path(directory) / "hyperloom" / "telemetry.sqlite3")
    try:
        for name, path in (("census_dir", census), ("logs_dir", runs)):
            store.put(ledger.PREFS, name, ledger._directory_setting(str(path), name))
    finally:
        store.close()
    lines = (FIXTURES / "rtx-run.log").read_text(encoding="utf-8").splitlines()
    stream = json.loads([line for line in lines if "ss stats" in line][-1].split("ss stats ", 1)[1])
    ds = {"profile": "ds", "nodes": ["halo-1", "halo-2"], "devices": [],
          "launch": {"family": "ds41-pair", "preset": "deepseek41", "engine_sha": "cab238d6" + "0" * 32},
          "machines": {"halo-1": {"id": "halo-1", "http_address": "halo-1"}},
          "rendered_router": {"replicas": [{"id": "halo-1", "node": "halo-1", "address": "halo-1", "port": 18080}]},
          "prefill_service": {"port": 18500}}
    service = {"provider": {"name": "expert streamer", "counters": {"expert_hits": 18342, "expert_misses": 2210,
                                                                     "bytes_streamed": {"value": 96_468_000_000, "unit": "bytes"},
                                                                     "wait_ms": {"value": 4620, "unit": "ms"}}},
               "strix_stream": stream}
    born = time.time() - 4000

    def fetch(address, port, path, text=False, key=None, deadline=None):
        elapsed = time.time() - born
        if port == 18080:                          # the fake DeepSeek engine (rank 0)
            if path == "/metrics":
                generated = int(elapsed * 14.5)
                return (f"strix_generated_tokens_total {generated}\nstrix_prompt_tokens_total {generated * 5}\n"
                        f"strix_prompt_tokens_cached_total {generated}\n")
            return {"schema": "strix-serve-status-v1", "model": "deepseek41", "uptime_seconds": elapsed}
        if port != 18500:
            raise OSError("fixture: no engine here")
        if path == "/health":
            return {"status": "ok", "busy": False, "queued": 0, "served": 12, "batches": 9, "handoff_pending": 0}
        return service
    return [(ledger, "_states", lambda api: [ds]), (ledger, "_fetch", fetch), (ledger, "_key", lambda source: "fixture")]
