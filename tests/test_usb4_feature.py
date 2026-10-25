"""Portal feature usb4: collector accounting (C3/C10), stored state, GET document, refresh, exchange view."""

import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from hyperloom import config
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.features import usb4

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {name: json.loads((ROOT / f"tests/fixtures/usb4/node-{name}.json").read_text(encoding="utf-8"))
           for name in ("halo-1", "halo-2")}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeAPI:
    def __init__(self, clock):
        self.clock = clock
        self.telemetry = Store(":memory:", clock=clock)
        machines = {mid: config.Machine(mid, system, access, "192.0.2.%d" % i, mid, 22, "/opt/hyperloom/" + mid, True,
                                        [{"id": "gpu"}])
                    for i, (mid, system, access) in enumerate((("workstation", "windows", "local"),
                                                                ("halo-1", "linux", "ssh"), ("halo-2", "linux", "ssh")))}
        self._cluster = config.Cluster(machines, {}, {"ds-pair": {"rank0": "halo-1", "rank1": "halo-2"},
                                                      "qwen-two": {"nodes": ["halo-1", "halo-2"]}}, {}, {}, {})
        self.sampler = None

    def cluster(self):
        return self._cluster

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", "server_time": self.clock(), "why": {}, **fields}


def report(machine, data_counts=None, journal=None, **changes):
    """A recorded node report; `data_counts` adds data-ring vectors {vector: (irq, count)} to both NHIs."""
    value = copy.deepcopy(REPORTS[machine])
    for nhi in ("0000:c7:00.5", "0000:c7:00.6"):
        for vector, (irq, count) in (data_counts or {}).items():
            value["interrupts"]["vectors"].append({"nhi": nhi, "vector": vector, "irq": irq + (35 if nhi.endswith("6") else 0),
                                                   "ring": "data", "domain": 0 if nhi.endswith("5") else 1, "count": count})
    value["journal"] = journal if journal is not None else {"status": "skipped", "why": "not requested in this call"}
    value.update(changes)
    return value


def follow(boot, cursor, counts=None, recent=None, complete=True, more=False):
    counts = dict(dict.fromkeys(usb4.KINDS, 0), **(counts or {}))
    return {"status": "ok", "boot_id": boot, "mode": "follow", "cursor": cursor, "entries": sum(counts.values()),
            "more": more, "complete": complete, "incomplete_why": None if complete else "1 malformed journal entries were skipped",
            "malformed": 0 if complete else 1, "scanned_from": None, "counts": counts, "matched": sum(counts.values()),
            "recent": recent or []}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeAPI(self.clock)
        self.addCleanup(self.api.telemetry.close)
        self.store = self.api.telemetry
        self.calls = []
        self.replies = {}

        def fetch(api, machine, payload):
            self.calls.append((machine.id, payload))
            reply = self.replies[machine.id]
            if isinstance(reply, Exception):
                raise reply
            reply = copy.deepcopy(reply(payload) if callable(reply) else reply)
            if "journal" not in payload:
                reply["journal"] = {"status": "skipped", "why": "not requested in this call"}
            return reply
        patcher = mock.patch.object(usb4, "fetch", fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(usb4, "_cached_stats", lambda api, now: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_at(self, t):
        self.clock.now = t
        points = usb4.sample(self.api, t)
        self.store.record(points, ts=t, interval=usb4.INTERVAL_S)
        return points

    def series(self, name, **labels):
        result = self.store.query(name, self.clock.now - 3600, self.clock.now + 1, labels=labels, tier="raw")
        return {json.dumps(line["labels"], sort_keys=True): [p[1] for p in line["points"]] for line in result["lines"]}

    def test_links_streams_and_sources_are_recorded_for_linux_nodes_only(self):
        self.replies = {"halo-1": report("halo-1", journal=REPORTS["halo-1"]["journal"]),
                        "halo-2": report("halo-2", journal=REPORTS["halo-2"]["journal"])}
        points = self.run_at(1_800_000_000.0)
        self.assertEqual(sorted({m for m, _ in self.calls}), ["halo-1", "halo-2"])
        self.assertEqual(self.calls[0][1], {"journal": {"boot_id": None, "cursor": None}})
        rates = {(p[1]["machine"], p[1]["router"], p[1]["direction"]): p[2] for p in points if p[0] == "usb4_link_gbps"}
        self.assertEqual(rates[("halo-1", "0-2", "rx")], 40.0)
        self.assertEqual(rates[("halo-2", "0-2", "tx")], 20.0)
        states = [p for p in points if p[0] == "usb4_stream_attached"]
        self.assertEqual(len(states), 8)
        self.assertTrue(all(p[2] == 1 and p[3] == "state" for p in states))
        node = self.store.get(usb4.NODE_NS, "halo-2")
        self.assertEqual({k: v["status"] for k, v in node["sources"].items()},
                         {"links": "ok", "layout": "ok", "live": "ok", "parameters": "ok", "interrupts": "ok", "journal": "ok"})
        self.assertEqual(node["summary"]["links"][0]["below_full_rate"], ["rx", "tx"])
        self.assertEqual(self.store.descriptor("usb4_stream_attached")["legend"], {"0": "detached", "1": "attached"})

    def test_live_attachment_is_never_taken_from_the_published_layout(self):
        broken = report("halo-1")
        broken["layout"]["live"] = {"status": "unavailable", "why": "1 live checks failed"}
        for stream in broken["layout"]["streams"]:
            stream["live"] = {"device_present": None, "attached_now": None, "attached_why": "service check failed"}
        self.replies = {"halo-1": broken, "halo-2": report("halo-2")}
        points = self.run_at(1_800_000_000.0)
        self.assertFalse([p for p in points if p[0] == "usb4_stream_attached" and p[1]["machine"] == "halo-1"])
        sources = self.store.get(usb4.NODE_NS, "halo-1")["sources"]
        self.assertEqual((sources["live"]["status"], sources["layout"]["status"]), ("unavailable", "ok"))
        for step in range(1, 5):
            self.run_at(1_800_000_000.0 + 10 * step)
        view = usb4.document(self.api, {})["machines"][0]
        self.assertTrue(view["stale"]["live"])
        self.assertFalse(view["stale"]["layout"])

    def test_interrupt_rates_need_the_same_vectors_over_one_window(self):
        t0 = 1_800_000_000.0
        self.replies = {"halo-1": report("halo-1", {2: (55, 1000), 3: (56, 2000)}), "halo-2": report("halo-2")}
        self.run_at(t0)
        self.replies["halo-1"] = report("halo-1", {2: (55, 6000), 3: (56, 12000)})
        self.run_at(t0 + 10)
        nhis = {(r["nhi"], r["ring"]): r for r in self.store.get(usb4.NODE_NS, "halo-1")["summary"]["interrupts"]["nhis"]}
        self.assertEqual(nhis[("0000:c7:00.5", "data")]["rate_per_s"], 1500.0)
        self.assertIsNotNone(nhis[("0000:c7:00.5", "control")]["rate_per_s"])
        deltas = self.series("usb4_ring_irqs", machine="halo-1", nhi="0000:c7:00.5", ring="data")
        self.assertEqual(sorted(sum(v) for v in deltas.values()), [5000.0, 10000.0])
        # A ring closed: the class has no rate (vector 2 alone would understate it).
        self.replies["halo-1"] = report("halo-1", {2: (55, 9000)})
        self.run_at(t0 + 20)
        nhis = {(r["nhi"], r["ring"]): r for r in self.store.get(usb4.NODE_NS, "halo-1")["summary"]["interrupts"]["nhis"]}
        self.assertIsNone(nhis[("0000:c7:00.5", "data")]["rate_per_s"])
        self.assertIn("opened or closed", nhis[("0000:c7:00.5", "data")]["why"])
        # Same membership again but the vector got a new IRQ (ring freed and reallocated): a new baseline.
        self.replies["halo-1"] = report("halo-1", {2: (55, 9500)})
        self.run_at(t0 + 30)
        self.assertEqual({(r["nhi"], r["ring"]): r["rate_per_s"] for r in
                          self.store.get(usb4.NODE_NS, "halo-1")["summary"]["interrupts"]["nhis"]}[("0000:c7:00.5", "data")], 50.0)
        self.replies["halo-1"] = report("halo-1", {2: (70, 10)})
        self.run_at(t0 + 40)
        summary = self.store.get(usb4.NODE_NS, "halo-1")["summary"]
        row = {(r["nhi"], r["ring"]): r for r in summary["interrupts"]["nhis"]}[("0000:c7:00.5", "data")]
        self.assertIsNone(row["rate_per_s"])
        outcome = [v for v in summary["interrupts"]["vectors"] if v["nhi"] == "0000:c7:00.5" and v["vector"] == 2][0]
        self.assertEqual(outcome["outcome"], "reset")
        # A missed sample breaks the common window even with the same vectors.
        self.replies["halo-1"] = report("halo-1", {2: (70, 20)})
        self.run_at(t0 + 50)
        self.replies["halo-1"] = RuntimeError("ssh timed out")
        self.run_at(t0 + 60)
        self.replies["halo-1"] = report("halo-1", {2: (70, 40)})
        self.run_at(t0 + 70)
        row = {(r["nhi"], r["ring"]): r for r in self.store.get(usb4.NODE_NS, "halo-1")["summary"]["interrupts"]["nhis"]}[("0000:c7:00.5", "data")]
        self.assertEqual(row["rate_per_s"], 1.0)          # 20 interrupts over the 20 s since the last recorded sample

    def test_kernel_evidence_is_counted_once_per_boot(self):
        t0 = 1_800_000_000.0
        initial = REPORTS["halo-1"]["journal"]
        boot, cursor = initial["boot_id"], initial["cursor"]
        self.replies = {"halo-1": report("halo-1", journal=initial), "halo-2": report("halo-2", journal=REPORTS["halo-2"]["journal"])}
        self.run_at(t0)
        doc = self.store.get(usb4.JOURNAL_NS, "halo-1")
        self.assertEqual((doc["generation"], doc["totals"]["host_reset"], doc["cursor"]), (1, initial["counts"]["host_reset"], cursor))
        self.assertEqual(self.series("usb4_kernel_evidence", machine="halo-1"), {})     # first sample: a baseline
        # Not due again for 60 s.
        self.calls.clear()
        self.run_at(t0 + 10)
        self.assertEqual(dict(self.calls)["halo-1"], {})
        # Follow: new lines become deltas; zero counts are recorded as observed zeros.
        self.replies["halo-1"] = report("halo-1", journal=follow(boot, cursor[:-1] + "1", {"quarantine": 2},
                                                                 [{"ts": t0 + 50, "kind": "quarantine", "message": "tbstream: GPU context quarantined"}]))
        self.calls.clear()
        self.run_at(t0 + 60)
        self.assertEqual(dict(self.calls)["halo-1"], {"journal": {"boot_id": boot, "cursor": cursor}})
        deltas = self.series("usb4_kernel_evidence", machine="halo-1", kind="quarantine")
        self.assertEqual(list(deltas.values()), [[2.0]])
        self.assertEqual(list(self.series("usb4_kernel_evidence", machine="halo-1", kind="host_reset").values()), [[0.0]])
        doc = self.store.get(usb4.JOURNAL_NS, "halo-1")
        self.assertEqual((doc["totals"]["quarantine"], doc["last_new_at"], doc["recent"][-1]["kind"]), (2, t0 + 60, "quarantine"))
        # A crash after the counters but before the cursor was stored: the same read again counts nothing twice.
        saved = copy.deepcopy(doc)
        before = self.store.get(usb4.JOURNAL_NS, "halo-1")
        self.store.put(usb4.JOURNAL_NS, "halo-1", dict(before, cursor=cursor, totals=dict(before["totals"], quarantine=0)))
        usb4._account_journal(self.store, "halo-1", follow(boot, saved["cursor"], {"quarantine": 2}), t0 + 61)
        self.assertEqual(sum(sum(v) for v in self.series("usb4_kernel_evidence", machine="halo-1", kind="quarantine").values()), 2.0)
        # A rescan of the same boot (cursor lost) starts a new generation: a baseline, never added to the totals.
        rescan = dict(copy.deepcopy(initial), mode="cursor_lost")
        doc = usb4._account_journal(self.store, "halo-1", rescan, t0 + 120)
        self.assertEqual((doc["generation"], doc["totals"]["quarantine"], doc["totals"]["host_reset"]),
                         (2, 0, initial["counts"]["host_reset"]))
        self.assertEqual(sum(sum(v) for v in self.series("usb4_kernel_evidence", machine="halo-1", kind="host_reset").values()), 0.0)
        # A new boot replaces everything.
        other = dict(copy.deepcopy(initial), boot_id="e" * 32, cursor=cursor.replace("b=" + boot, "b=" + "e" * 32),
                     complete=False, incomplete_why="the oldest kernel entry held for this boot is not the first message")
        doc = usb4._account_journal(self.store, "halo-1", other, t0 + 180)
        self.assertEqual((doc["boot_id"], doc["generation"], doc["complete"], doc["last_new_at"]), ("e" * 32, 1, False, None))
        # An unreadable journal keeps the totals and says why.
        doc = usb4._account_journal(self.store, "halo-1", {"status": "unavailable", "why": "kernel journal not readable"}, t0 + 240)
        self.assertEqual((doc["status"], doc["totals"]["host_reset"], doc["why"]), ("unavailable", initial["counts"]["host_reset"], "kernel journal not readable"))

    def test_more_entries_pending_and_malformed_reads(self):
        t0 = 1_800_000_000.0
        initial = REPORTS["halo-1"]["journal"]
        self.replies = {"halo-1": report("halo-1", journal=initial), "halo-2": report("halo-2")}
        self.run_at(t0)
        self.replies["halo-1"] = report("halo-1", journal=follow(initial["boot_id"], initial["cursor"], more=True, complete=False))
        self.run_at(t0 + 60)
        doc = self.store.get(usb4.JOURNAL_NS, "halo-1")
        self.assertEqual((doc["more"], doc["complete"]), (True, False))
        self.assertIn("malformed", doc["incomplete_why"])
        self.calls.clear()
        self.run_at(t0 + 70)                                # a pending batch is read on the next tick
        self.assertIn("journal", dict(self.calls)["halo-1"])

    def test_node_failure_keeps_the_last_summary_and_marks_sources(self):
        self.replies = {"halo-1": report("halo-1"), "halo-2": report("halo-2")}
        self.run_at(1_800_000_000.0)
        self.replies["halo-1"] = {"schema": "hyperloom/node-v1", "verb": "usb4", "error": "unknown verb usb4"}
        self.run_at(1_800_000_010.0)
        node = self.store.get(usb4.NODE_NS, "halo-1")
        self.assertEqual(node["error"], "unknown verb usb4")
        self.assertEqual(node["summary"]["collected_at"], 1_800_000_000.0)
        self.assertEqual(node["sources"]["links"]["status"], "unavailable")
        self.assertEqual(node["sources"]["links"]["last_ok"], 1_800_000_000.0)

    def test_link_changes_are_compared_within_one_boot(self):
        self.replies = {"halo-1": report("halo-1"), "halo-2": report("halo-2")}
        self.run_at(1_800_000_000.0)
        retrained = report("halo-1")
        retrained["links"][0]["rx"].update(gbps=20.0, gbps_per_lane=10.0, full_rate=False)
        self.replies["halo-1"] = retrained
        self.run_at(1_800_000_010.0)
        link = self.store.get(usb4.NODE_NS, "halo-1")["summary"]["links"][0]
        self.assertEqual(link["changed"], {"at": 1_800_000_010.0, "from": {"rx": 40.0, "tx": 40.0}, "to": {"rx": 20.0, "tx": 40.0}})
        rebooted = report("halo-1", boot_id="f" * 32)
        self.replies["halo-1"] = rebooted
        self.run_at(1_800_000_020.0)
        self.assertIsNone(self.store.get(usb4.NODE_NS, "halo-1")["summary"]["links"][0]["changed"])


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeAPI(self.clock)
        self.addCleanup(self.api.telemetry.close)

    def stats(self, t, steps, uptime=4000.0, epoch=0, bytes_sent=None, group_id="local"):
        group = {"group_id": group_id, "epoch": epoch, "rank": 0, "world": 2, "transport": "usb4gpu",
                 "exchanges": steps * 2, "exchange_seconds": steps * 2 * 0.0005,
                 "bytes_sent": steps * 2 * 1024 if bytes_sent is None else bytes_sent}
        return {"server_time": t, "engines": [{"id": "ds-pair/0", "profile": "ds-pair", "why": {},
                                                "status": {"uptime_seconds": uptime, "batch": {"steps": steps}, "group": group}}]}

    def observe(self, stats):
        self.clock.now = stats["server_time"]
        self.api._cached = {"stats": stats}
        usb4._exchange(self.api, self.api.telemetry, self.clock.now)
        return self.api.telemetry.get(usb4.EXCHANGE_NS, "ds-pair/0")["view"]

    def test_windows_need_the_same_engine_and_group(self):
        t0 = 1_800_000_000.0
        view = self.observe(self.stats(t0, 1000))
        self.assertEqual((view["machines"], view["window"]), (["halo-1", "halo-2"], None))
        self.assertIn("two observations", view["why"]["window"])
        self.assertEqual(view["lifetime"]["seconds_per_exchange"], 0.0005)
        self.assertNotIn("seconds_per_step", view["lifetime"])
        view = self.observe(self.stats(t0 + 10, 1300, uptime=4010.0))
        self.assertAlmostEqual(view["window"]["seconds_per_step"], 0.001)
        self.assertAlmostEqual(view["window"]["bytes_per_step"], 2048.0)
        self.assertEqual((view["window"]["steps"], view["window"]["span_s"]), (300, 10.0))
        # The same snapshot again is not a new observation.
        self.api._cached = {"stats": self.stats(t0 + 10, 1300, uptime=4010.0)}
        usb4._exchange(self.api, self.api.telemetry, t0 + 15)
        self.assertEqual(self.api.telemetry.get(usb4.EXCHANGE_NS, "ds-pair/0")["observed_at"], t0 + 10)
        view = self.observe(self.stats(t0 + 20, 1600, uptime=4020.0, epoch=1))
        self.assertIsNone(view["window"])
        self.assertIn("group changed", view["why"]["window"])
        view = self.observe(self.stats(t0 + 30, 1900, uptime=5.0, epoch=1))
        self.assertIn("restart", view["why"]["window"])
        stats = self.stats(t0 + 40, 2200, epoch=1)
        del stats["engines"][0]["status"]["uptime_seconds"]
        view = self.observe(stats)
        self.assertIn("uptime not reported", view["why"]["window"])

    def test_each_ratio_needs_its_own_counters(self):
        t0 = 1_800_000_000.0
        self.observe(self.stats(t0, 1000))
        stats = self.stats(t0 + 10, 1300, uptime=4010.0, bytes_sent=0)
        view = self.observe(stats)
        self.assertAlmostEqual(view["window"]["seconds_per_step"], 0.001)     # timing survives
        self.assertIsNone(view["window"]["bytes_per_step"])                  # bytes went down
        self.assertIn("went down", view["why"]["bytes_per_step"])
        view = self.observe(self.stats(t0 + 20, 1300, uptime=4020.0, bytes_sent=0))
        self.assertIsNone(view["window"]["seconds_per_step"])
        self.assertIn("idle", view["why"]["seconds_per_step"])

    def test_old_snapshots_are_not_used(self):
        self.api._cached = {"stats": self.stats(1_800_000_000.0 - 31, 1000)}
        usb4._exchange(self.api, self.api.telemetry, 1_800_000_000.0)
        self.assertIsNone(self.api.telemetry.get(usb4.EXCHANGE_NS, "ds-pair/0"))
        self.api._cached = None
        usb4._exchange(self.api, self.api.telemetry, 1_800_000_000.0)
        doc = usb4.document(self.api, {})
        self.assertEqual(doc["exchange"], [])
        self.assertIn("not sampled", doc["why"]["exchange"])


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeAPI(self.clock)
        self.addCleanup(self.api.telemetry.close)

    def test_get_reads_only_the_store(self):
        with mock.patch.object(usb4, "fetch", side_effect=AssertionError("a GET called a node")), \
                mock.patch.object(usb4, "_cached_stats", side_effect=AssertionError("a GET read the snapshot")):
            doc = usb4.document(self.api, {})
        self.assertEqual(doc["schema"], "hyperloom/ui-usb4-v1")
        self.assertEqual([m["id"] for m in doc["machines"]], ["halo-1", "halo-2"])
        self.assertIn("not collected yet", doc["machines"][0]["why"]["summary"])
        self.assertEqual(doc["full_rate"]["gbps"], 40.0)
        self.assertFalse(doc["samplers"])
        with self.assertRaises(ValueError):
            usb4.document(self.api, {"path": "/etc/passwd"})
        self.api.telemetry = None
        doc = usb4.document(self.api, {})
        self.assertIn("not open", doc["why"]["machines"])
        self.assertIsNone(doc["machines"][0]["summary"])

    def test_refresh_and_registration(self):
        with self.assertRaises(PermissionError):
            usb4.refresh(self.api, {})
        with self.assertRaises(ValueError):
            usb4.refresh(self.api, {"machine": "halo-1"})
        self.api.sampler = mock.Mock()
        self.assertTrue(usb4.refresh(self.api, {})["requested"])
        self.api.sampler.refresh.assert_called_once_with("usb4")
        registry = features.load(("usb4",))
        self.assertEqual((registry.loaded, registry.failed), (["usb4"], {}))
        self.assertIn("usb4", registry.gets)
        self.assertIn("usb4/refresh", registry.posts)
        self.assertEqual(registry.collectors["usb4"][1], 10.0)

    def test_document_through_the_portal_api_is_redacted_and_strict(self):
        from hyperloom.web.api import API, APIError
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = features.load(("usb4",))
        features.reset(registry)
        self.addCleanup(features.reset)
        api = API(Path(directory.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                  audit_path=Path(directory.name) / "audit.jsonl")
        api.orchestrate.load_cluster.return_value = self.api.cluster()
        document = api.read("usb4", {})
        self.assertEqual(document["schema"], "hyperloom/ui-usb4-v1")
        with self.assertRaises(APIError) as caught:
            api.read("usb4", {"x": "1"})
        self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
