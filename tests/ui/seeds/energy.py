"""Fixture history for the energy and thermals pages: two days of power for the three fixture machines (one node
offline for three hours), generated tokens, a week of temperatures with a throttle period, a price and two
archived months. Synthetic values, not observations of machines."""

import math


def _bulk(store, batches):
    """Record [(ts, interval, points)] in one transaction (fixture start-up time stays small)."""
    def work(pending):
        for ts, interval, points in batches:
            store._insert(pending, store._rows(points), ts, interval)
    store._atomic(work)


def seed(store, now):
    from hyperloom.web.features import energy

    store.describe("node_gpu_power_watts", "gauge", unit="W", interval=60)
    store.describe("node_cpu_power_watts", "gauge", unit="W", interval=60)
    store.describe("engine_generated_tokens", "delta", unit="tokens", interval=60)
    for name in ("node_gpu_temp_celsius", "node_cpu_temp_celsius", "node_sensor_temp_celsius"):
        store.describe(name, "gauge", unit="celsius", interval=300)
    store.describe("node_gpu_throttled", "state", interval=300, legend={"0": "no", "1": "throttled"})

    def wave(t, period, low, high, phase=0.0):
        return low + (high - low) * (0.5 + 0.5 * math.sin(2 * math.pi * t / period + phase))

    # Temperatures every 5 minutes for a week; a throttle period on halo-1 four hours ago.
    batches = []
    t = now - 7 * 86400
    while t < now:
        busy = 1 if now - 5 * 3600 < t < now - 4 * 3600 else 0
        points = [
            ("node_gpu_temp_celsius", {"machine": "workstation", "device": "0", "sensor": "edge"}, wave(t, 86400, 38, 71)),
            ("node_gpu_temp_celsius", {"machine": "workstation", "device": "1", "sensor": "edge"}, wave(t, 86400, 35, 64, 1)),
            ("node_sensor_temp_celsius", {"machine": "workstation", "sensor": "nvme0"}, wave(t, 43200, 36, 48)),
            ("node_gpu_temp_celsius", {"machine": "halo-1", "device": "0", "sensor": "edge"}, 88.5 if busy else wave(t, 86400, 45, 78, 2)),
            ("node_cpu_temp_celsius", {"machine": "halo-1", "sensor": "package"}, 84.0 if busy else wave(t, 86400, 42, 70, 2)),
            ("node_sensor_temp_celsius", {"machine": "halo-1", "sensor": "nvme0"}, wave(t, 43200, 38, 52, 2)),
            ("node_gpu_throttled", {"machine": "halo-1", "device": "0"}, busy, "state"),
        ]
        if not now - 30 * 3600 < t < now - 27 * 3600:          # halo-2 offline: a gap, not zeros
            points += [
                ("node_gpu_temp_celsius", {"machine": "halo-2", "device": "0", "sensor": "edge"}, wave(t, 86400, 44, 80, 3)),
                ("node_cpu_temp_celsius", {"machine": "halo-2", "sensor": "package"}, wave(t, 86400, 41, 72, 3)),
            ]
        batches.append((t, 300, points))
        t += 300

    # Power and tokens every minute for two days.
    t = now - 2 * 86400
    while t < now:
        points = [
            ("node_gpu_power_watts", {"machine": "workstation", "device": "0"}, wave(t, 86400, 60, 420)),
            ("node_gpu_power_watts", {"machine": "workstation", "device": "1"}, wave(t, 86400, 25, 260, 1)),
            ("node_gpu_power_watts", {"machine": "halo-1", "device": "0"}, wave(t, 86400, 35, 130, 2)),
            ("node_cpu_power_watts", {"machine": "halo-1"}, wave(t, 86400, 12, 45, 2)),
            ("engine_generated_tokens", {"profile": "qwen-two", "replica": "0", "model": "fixture", "instance": "a"},
             wave(t, 86400, 200, 2400, 2), "delta"),
        ]
        if not now - 30 * 3600 < t < now - 27 * 3600:
            points.append(("node_gpu_power_watts", {"machine": "halo-2", "device": "0"}, wave(t, 86400, 35, 140, 3)))
        batches.append((t, 60, points))
        t += 60
    _bulk(store, sorted(batches, key=lambda b: b[0]))
    store.prune(now)

    store.put("prefs", "energy/price", {"price_per_kwh": 0.24, "currency": "EUR"})
    energy._mark_verified(store, now - 3 * 86400, now)       # stands for the runs of a portal that recorded this
    energy.compute(store, now)
    # Two earlier archived months (synthetic) so the permanent archive table has history.
    month = energy.zone_of(store).local(now).replace(day=1)
    for back, scale in ((1, 1.0), (2, 0.8)):
        year, number = month.year, month.month - back
        while number < 1:
            year, number = year - 1, number + 12
        key = f"{year:04d}-{number:02d}"
        for machine, wh in (("workstation", 61000.0), ("halo-1", 24000.0), ("halo-2", 23500.0)):
            store.execute("INSERT OR IGNORE INTO energy_monthly(month, machine, wh, observed_s, priced_wh, cost, currency,"
                          " zone) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, machine, wh * scale, 2.5e6, wh * scale,
                                                                     wh * scale / 1000 * 0.24, "EUR", "fixture zone"))
        store.execute("INSERT OR IGNORE INTO energy_monthly_tokens(month, wh_both, tokens_both, both_s) VALUES (?, ?, ?, ?)",
                      (key, 52000.0 * scale, 9.1e6 * scale, 1.9e6))
