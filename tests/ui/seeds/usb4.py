"""USB4 links fixture for the browser smoke: 24 h of trained link rate per node (one link retrained from 40 to
20 Gb/s five hours ago, a 20 min offline gap) and the recorded node reports of both nodes served in place of
the node call, with data-ring interrupts growing at a steady rate and an engine group whose counters advance."""

from tests.portable_fixtures import read_fixture
import copy
import json
from pathlib import Path
import time

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "usb4"
# (machine, router): rate before and after the retrain, in Gb/s per direction.
LINKS = {("halo-1", "0-2"): (40.0, 40.0), ("halo-1", "1-2"): (40.0, 20.0),
         ("halo-2", "0-2"): (40.0, 20.0), ("halo-2", "1-2"): (40.0, 40.0)}
DATA_RATE = 9000.0          # interrupts per second per data vector while the engine runs


def seed(store, now):
    store.describe("usb4_link_gbps", "gauge", unit="Gb/s", interval=10)
    retrain = now - 5 * 3600
    t = now - 86400
    while t < now - 120:
        if not now - 9 * 3600 < t < now - 9 * 3600 + 1200:
            points = [("usb4_link_gbps", {"machine": m, "router": r, "direction": d},
                       before if t < retrain else after, "gauge")
                      for (m, r), (before, after) in LINKS.items() for d in ("rx", "tx")]
            store.record(points, ts=t, interval=10)
        t += 60


def patches(directory, now):
    from hyperloom.web.features import usb4
    reports = {name: json.loads(read_fixture(FIXTURES / f"node-{name}.json"))
               for name in ("halo-1", "halo-2")}
    started = time.time()

    def fetch(api, machine, payload):
        report = copy.deepcopy(reports[machine.id])
        current = time.time()
        report["collected_at"] = current
        vectors = report["interrupts"]["vectors"]
        for nhi, first_irq in (("0000:c7:00.5", 55), ("0000:c7:00.6", 90)):
            for index in range(4):          # two ring pairs per host interface while the engine runs
                vectors.append({"nhi": nhi, "vector": 2 + index, "irq": first_irq + index, "ring": "data",
                                "domain": 0 if nhi.endswith("5") else 1,
                                "count": int(170_000_000 + 1_000_000 * index + DATA_RATE * (current - started))})
        journal = report["journal"]
        request = payload.get("journal")
        if request is None:
            report["journal"] = {"status": "skipped", "why": "not requested in this call"}
        elif request.get("cursor"):
            report["journal"] = {"status": "ok", "boot_id": journal["boot_id"], "mode": "follow",
                                 "cursor": request["cursor"], "entries": 0, "more": False, "complete": True,
                                 "incomplete_why": None, "malformed": 0, "scanned_from": None,
                                 "counts": dict.fromkeys(journal["counts"], 0), "matched": 0, "recent": []}
        return report

    def cached_stats(api, now_):
        elapsed = time.time() - started
        steps = 120_000 + int(30 * elapsed)
        return {"server_time": time.time(), "engines": [{
            "id": "ds41-pair/0", "profile": "ds41-pair", "why": {},
            "status": {"uptime_seconds": 4000 + elapsed, "batch": {"steps": steps},
                       "group": {"group_id": "local", "epoch": 0, "rank": 0, "world": 2, "state": "ok",
                                 "transport": "usb4gpu", "exchanges": steps * 2, "bytes_sent": steps * 2 * 917504,
                                 "exchange_seconds": steps * 2 * 0.00061}}}]}

    return [(usb4, "fetch", fetch), (usb4, "_cached_stats", cached_stats)]
