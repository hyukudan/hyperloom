"""Fixture health state for the browser smoke: active and pending findings, a changed rule, an activity log and one
alert channel whose sender is replaced (nothing leaves the machine)."""

import os


GIB = 1 << 30
HOST = ".".join(("hooks", "example", "test"))           # a reserved test name, built at run time


def seed(store, now):
    store.describe("node_gpu_temp_celsius", "gauge", unit="celsius", interval=5)
    store.describe("node_disk_used_bytes", "gauge", unit="bytes", interval=5)
    store.describe("node_disk_total_bytes", "gauge", unit="bytes", interval=5)
    store.record([("node_gpu_temp_celsius", {"machine": "halo-1", "device": "card0", "sensor": "edge"}, 92.0),
                  ("node_disk_used_bytes", {"machine": "halo-2", "mount": "/"}, 460 * GIB),
                  ("node_disk_total_bytes", {"machine": "halo-2", "mount": "/"}, 500 * GIB)], ts=now)
    findings = [
        {"id": "gpu-hot:halo-1/card0", "rule": "gpu-hot", "subject": "halo-1/card0", "title": "GPU hot", "state": "firing",
         "severity": "critical", "detail": "halo-1 card0 at 92 °C (edge)", "machine": "halo-1", "since": now - 1260,
         "first_true": now - 1350, "last_true": now, "last_observed": now, "occurrence": "a1b2c3d4e5f60718",
         "hint": "Check airflow and fan state; sustained heat throttles decode."},
        {"id": "disk-full:halo-2/disk:/", "rule": "disk-full", "subject": "halo-2/disk:/", "title": "Disk nearly full",
         "state": "firing", "severity": "warning", "detail": "halo-2 / 92 % used", "machine": "halo-2", "since": now - 7300,
         "first_true": now - 7400, "last_true": now, "last_observed": now, "occurrence": "b1b2c3d4e5f60718",
         "hint": "Free space: old releases, logs or model copies."},
        {"id": "usb4-below-rate:halo-1/usb4/1-2/rx", "rule": "usb4-below-rate", "subject": "halo-1/usb4/1-2/rx",
         "title": "USB4 link below full rate", "state": "clearing", "severity": "warning",
         "detail": "halo-1 USB4 1-2 rx trained at 20 Gb/s of 40", "machine": "halo-1", "since": now - 600,
         "first_true": now - 700, "last_true": now - 20, "false_since": now - 10, "last_false": now - 10,
         "last_observed": now - 10, "occurrence": "c1b2c3d4e5f60718",
         "hint": "Reseat or replace the cable; a retrain shows in the link history."},
        {"id": "gpu-low-clock:halo-2/card0", "rule": "gpu-low-clock", "subject": "halo-2/card0",
         "title": "GPU clock low while busy", "state": "pending", "severity": "warning",
         "detail": "halo-2 card0 at 600 MHz while 97 % busy", "machine": "halo-2", "since": None, "first_true": now - 40,
         "last_true": now, "last_observed": now, "occurrence": None, "hint": "Look for a power cap, thermal limit or power profile."}]
    for finding in findings:
        store.put("health/state", finding["id"], finding)
    store.put("health/config", "gpu-hot", {"params": {"warn_c": 83.0}})
    store.put("health/meta", "last", {"evaluated_at": now, "duration_s": 0.02, "overflow": 0,
                                      "sources": {"series:engine_up": "no samples recorded yet",
                                                  "feature:turns": "needs the turns document (feature not loaded)"},
                                      "rules": {"gpu-hot": 1, "disk-full": 1, "turn-over-cap": None, "engine-down": None}})
    store.put("alerts/channels", "ch-0a1b2c3d", {"id": "ch-0a1b2c3d", "name": "Lab channel", "kind": "webhook",
                                                 "min_severity": "warning", "reminders": "4h",
                                                 "url_masked": f"https://{HOST}/…", "url_version": "0123456789abcdef",
                                                 "created": now - 86400,
                                                 "updated": now - 86400})
    for key, finding in (("ch-0a1b2c3d|gpu-hot:halo-1/card0", findings[0]), ("ch-0a1b2c3d|disk-full:halo-2/disk:/", findings[1])):
        store.put("alerts/track", key, {"occurrence": finding["occurrence"], "severity": finding["severity"],
                                        "queued_at": finding["since"], "delivered_at": finding["since"] + 2,
                                        "reminded_at": None, "reminder_queued": False})
    events = [
        (now - 7300, "health", "warning", "Disk nearly full: halo-2 / 92 % used", "halo-2"),
        (now - 7290, "alerts", "info", f"alert sent to Lab channel (https://{HOST}/…): 1 firing", None),
        (now - 3600, "jobs", "info", "up profile qwen-replicas accepted (job 3f2a9c1e)", None),
        (now - 3540, "leases", "info", "lease acquired on halo-1 · hl-qwen-fixture", "halo-1"),
        (now - 3500, "jobs", "info", "up profile qwen-replicas succeeded (job 3f2a9c1e)", None),
        (now - 1800, "usb4", "warning", "2 USB4 kernel evidence lines new on halo-1 (reset 2)", "halo-1"),
        (now - 1260, "health", "critical", "GPU hot: halo-1 card0 at 92 °C (edge)", "halo-1"),
        (now - 1255, "alerts", "warning", f"alert to Lab channel (https://{HOST}/…) failed (HTTP 500); attempt 1, next in 5 s", None),
        (now - 1250, "alerts", "info", f"alert sent to Lab channel (https://{HOST}/…): 1 firing", None),
        (now - 900, "config", "info", "health/rules/set succeeded", None)]
    for ts, category, severity, message, machine in events:
        store.event(category, severity, message, machine=machine, ts=ts)


def patches(directory, now):
    """The channel's URL in the fixture's secret directory, and a sender that never opens a connection."""
    from hyperloom.web.features import alerts
    path = directory / "secrets" / "alert-urls" / "ch-0a1b2c3d.0123456789abcdef"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = f"https://{HOST}/fixture-topic".encode("ascii")
    if os.name == "nt":
        from hyperloom import secrets
        data = secrets._dpapi(data)
    path.write_bytes(data)

    def offline_send(url, channel_kind, kind, message_id, items, now, timeout=alerts.SEND_TIMEOUT):
        return {"ok": True, "status": 204}
    return [(alerts, "send", offline_send)]
