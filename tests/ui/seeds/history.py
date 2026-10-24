"""Fixture history for the browser smoke: two nodes' GPU busy over the last hour with a ten-minute gap."""


def seed(store, now):
    store.describe("gpu_busy_percent", "gauge", unit="%", interval=60)
    for machine, base in (("halo-1", 40.0), ("halo-2", 65.0)):
        t = now - 3600
        while t < now:
            if not now - 1800 < t < now - 1200:          # an offline window: a gap, not zeros
                store.record([("gpu_busy_percent", {"machine": machine}, base + (t % 600) / 30)], ts=t)
            t += 60
