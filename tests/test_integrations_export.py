"""F43 (exporter: telemetry store, ledger, findings, histogram passthrough) and F46 (server-side CSV export)."""

import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from hyperloom import metrics_export
from hyperloom.adapters import prom
from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.features import history, integrations

from tests.test_metrics_export import ROUTER, fake_fetch, healthy, labels


NOW = 1_800_000_000.0


class StoreExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hyperloom store ")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "telemetry.sqlite3"
        self.store = Store(self.path, clock=lambda: NOW)
        self.addCleanup(self.store.close)
        patcher = mock.patch.object(metrics_export.secrets, "runtime_dir", return_value=Path(directory.name) / "run")
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, now=NOW, **kw):
        text = metrics_export.collect({}, fake_fetch({}), telemetry=self.path, now=now, launches={}, families={}, **kw)
        return text, prom.parse(text)

    def test_latest_fresh_values_only_and_unknown_omitted(self):
        self.store.describe("node_gpu_busy_percent", "gauge", unit="%", interval=5)
        self.store.describe("node_up", "state", interval=5, legend={"0": "down", "1": "up"})
        self.store.describe("other_series", "gauge", interval=5)
        self.store.record([("node_gpu_busy_percent", {"machine": "halo-1", "device": "card0"}, 40)], ts=NOW - 10)
        self.store.record([("node_gpu_busy_percent", {"machine": "halo-1", "device": "card0"}, 55)], ts=NOW - 4)
        self.store.record([("node_gpu_busy_percent", {"machine": "halo-2", "device": "card0"}, 70)], ts=NOW - 16)
        self.store.record([("node_up", {"machine": "halo-1"}, 1), ("other_series", {"machine": "halo-1"}, 3)], ts=NOW - 2)
        text, samples = self.collect()
        self.assertEqual(samples[("hyperloom_node_gpu_busy_percent", labels(machine="halo-1", device="card0"))], 55)
        self.assertNotIn(("hyperloom_node_gpu_busy_percent", labels(machine="halo-2", device="card0")), samples)  # stale
        self.assertEqual(samples[("hyperloom_node_up", labels(machine="halo-1"))], 1)
        self.assertFalse(any(key[0] == "hyperloom_other_series" for key in samples))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="telemetry"))], 1)
        self.assertIn("# TYPE hyperloom_node_gpu_busy_percent gauge", text)
        self.assertIn("node_gpu_busy_percent (%) from the hyperloom telemetry store", text)

    def test_counter_deltas_are_not_exported(self):
        # A stored delta does not record the time it spans, so no rate (and no counter) is derived from it; a
        # producer that publishes a rate gauge (e.g. node_net_rx_bytes_per_second) is exported like any gauge.
        self.store.describe("node_net_rx_bytes", "delta", unit="bytes", interval=5)
        self.store.describe("node_net_rx_bytes_per_second", "gauge", unit="B/s", interval=5)
        rx = {"machine": "halo-1", "iface": "eth0"}
        self.store.record([("node_net_rx_bytes", rx, 1000)], ts=NOW - 7)
        self.store.record([("node_net_rx_bytes", rx, 5000), ("node_net_rx_bytes_per_second", rx, 800.0)], ts=NOW - 2)
        _, samples = self.collect()
        self.assertFalse(any(key[0].startswith("hyperloom_node_net_rx_bytes") and key[0].endswith(("bytes", "_total"))
                             for key in samples))
        self.assertEqual(samples[("hyperloom_node_net_rx_bytes_per_second", labels(**rx))], 800.0)

    def test_owned_name_spaces_are_never_taken_by_passthrough(self):
        # Store series (fresh or stale), ledger, findings and the router/engine families own their name spaces,
        # whatever a scrape contains, so a passed-through family can never take one of their names.
        out = metrics_export.Exposition()
        for name in ("hyperloom_node_gpu_clock_mhz", "hyperloom_usb4_link_gbps", "hyperloom_ledger_x", "hyperloom_finding"):
            self.assertFalse(out.passthrough_instance(name, "gauge", [(name, {"caller": "a"}, 1)], "x"))
        self.assertFalse(out.passthrough_instance("hyperloom_node_gpu_clock_mhz", "gauge",
                                                  [("hyperloom_node_gpu_clock_mhz", {"caller": "a"}, 1)], "x"))
        self.assertFalse(out.passthrough_instance("hyperloom_engine_waiting", "gauge",          # a hyperloom family
                                                  [("hyperloom_engine_waiting", {"caller": "a"}, 1)], "x"))

    def test_unsupported_schema_is_source_down(self):
        other = self.path.parent / "other.sqlite3"
        db = sqlite3.connect(other)
        db.execute("CREATE TABLE something (x)")
        db.commit()
        db.close()
        samples = prom.parse(metrics_export.collect({}, fake_fetch({}), telemetry=other, launches={}, families={}))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="telemetry",
                                                                reason="telemetry store has an unsupported schema"))], 0)

    def test_series_without_a_descriptor_are_left_out(self):
        self.store.record([("node_mystery", {"machine": "halo-1"}, 5, "delta")], ts=NOW)
        self.store.execute("DELETE FROM metrics WHERE name='node_mystery'")
        _, samples = self.collect()
        self.assertFalse(any(key[0] == "hyperloom_node_mystery" for key in samples))

    def test_long_label_values_reject_the_series(self):
        self.store.describe("node_load1", "gauge", interval=5)
        self.store.record([("node_load1", {"machine": "x" * 129}, 1), ("node_load1", {"machine": "halo-1"}, 2)], ts=NOW)
        _, samples = self.collect()
        self.assertEqual([key for key in samples if key[0] == "hyperloom_node_load1"],
                         [("hyperloom_node_load1", labels(machine="halo-1"))])

    def test_missing_and_unreadable_store_mark_the_source_down(self):
        missing = Path(self.path.parent / "absent.sqlite3")
        samples = prom.parse(metrics_export.collect({}, fake_fetch({}), telemetry=missing, launches={}, families={}))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="telemetry", reason=metrics_export.STORE_MISSING))], 0)
        self.assertFalse(missing.exists())                       # never created
        broken = self.path.parent / "broken.sqlite3"
        broken.write_bytes(b"not a database" * 100)
        samples = prom.parse(metrics_export.collect({}, fake_fetch({}), telemetry=broken, launches={}, families={}))
        key = [k for k in samples if k[0] == "hyperloom_source_up" and dict(k[1])["source"] == "telemetry"]
        self.assertEqual(samples[key[0]], 0)
        self.assertIn("unreadable", dict(key[0][1])["reason"])

    def test_read_only_and_deadline(self):
        self.store.describe("node_load1", "gauge", interval=5)
        self.store.record([("node_load1", {"machine": "halo-1"}, 2)], ts=NOW)
        db = metrics_export.open_readonly(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM raw")
        db.close()
        db = metrics_export.open_readonly(self.path, deadline=time.monotonic() - 1)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c").fetchall()
        db.close()
        text, samples = self.collect(deadline_s=0)
        key = [k for k in samples if k[0] == "hyperloom_source_up" and dict(k[1])["source"] == "telemetry"][0]
        self.assertEqual(samples[key], 0)
        self.assertNotIn("hyperloom_node_load1", text)

    def test_ledger_totals(self):
        self.store.ensure_table("CREATE TABLE IF NOT EXISTS ledger (hour INTEGER NOT NULL, model TEXT NOT NULL, "
                                "machine TEXT NOT NULL, profile TEXT NOT NULL, kind TEXT NOT NULL, tokens REAL NOT NULL "
                                "DEFAULT 0, observed_s REAL NOT NULL DEFAULT 0, uncertain_tokens REAL NOT NULL DEFAULT 0, "
                                "PRIMARY KEY(hour, model, machine, profile, kind))")
        rows = [(0, "m", "halo-1", "qwen", "generated", 10), (3600, "m", "halo-1", "qwen", "generated", 15),
                (0, "m", "halo-1", "qwen", "prompt", 7)]
        self.store.transaction([("INSERT INTO ledger(hour, model, machine, profile, kind, tokens) VALUES (?, ?, ?, ?, ?, ?)",
                                 row) for row in rows])
        text, samples = self.collect()
        one = dict(model="m", machine="halo-1", profile="qwen")
        self.assertEqual(samples[("hyperloom_ledger_tokens_total", labels(kind="generated", **one))], 25)
        self.assertEqual(samples[("hyperloom_ledger_tokens_total", labels(kind="prompt", **one))], 7)
        self.assertIn("# TYPE hyperloom_ledger_tokens_total counter", text)

    def finding(self, key, **value):
        self.store.put("health/state", key, value)

    def test_findings_standing_fresh_and_counted(self):
        _, samples = self.collect()
        self.assertFalse(any(key[0] == "hyperloom_finding" for key in samples))       # health not present
        self.assertFalse(any(dict(key[1]).get("source") == "health" for key in samples))
        self.store.put("health/meta", "meta", {"evaluated_at": NOW - 5})
        self.finding("a", rule="gpu_hot", severity="critical", state="firing", machine="halo-1", subject="card0")
        self.finding("b", rule="gpu_hot", severity="warning", state="clearing", machine="halo-2", subject="card0")
        self.finding("c", rule="gpu_hot", severity="warning", state="pending", machine="halo-1", subject="card1")
        self.finding("d", rule="disk_full", severity="warning", state="firing", machine="halo-1", subject="/")
        self.finding("e", rule="disk_full", severity="warning", state="firing", machine="halo-1", subject="/")
        self.finding("f", rule="odd", severity="fatal", state="firing")
        _, samples = self.collect()
        found = {dict(key[1])["rule"] + "/" + dict(key[1]).get("machine", "") + "/" + dict(key[1])["severity"]: value
                 for key, value in samples.items() if key[0] == "hyperloom_finding"}
        self.assertEqual(found, {"gpu_hot/halo-1/critical": 1, "gpu_hot/halo-2/warning": 1, "disk_full/halo-1/warning": 2})
        self.assertEqual(samples[("hyperloom_source_up", labels(source="health"))], 1)
        self.store.put("health/meta", "meta", {"evaluated_at": NOW - 600})
        _, samples = self.collect()
        self.assertFalse(any(key[0] == "hyperloom_finding" for key in samples))
        self.assertEqual(samples[("hyperloom_source_up", labels(source="health", reason="health evaluation stale"))], 0)


ROUTER_METRICS = """# HELP tensormeld_router_waiting Waiting.
# TYPE tensormeld_router_waiting gauge
tensormeld_router_waiting 0
# TYPE tensormeld_router_ttft_seconds histogram
tensormeld_router_ttft_seconds_bucket{route="r1",caller="ops",le="1"} 3
tensormeld_router_ttft_seconds_bucket{route="r1",caller="ops",le="+Inf"} 4
tensormeld_router_ttft_seconds_bucket{route="r1",caller="ops",le="0.1"} 1
tensormeld_router_ttft_seconds_sum{route="r1",caller="ops"} 2.5
tensormeld_router_ttft_seconds_count{route="r1",caller="ops"} 4
tensormeld_router_ttft_seconds_bucket{route="r2",caller="ops",le="1"} 5
tensormeld_router_ttft_seconds_bucket{route="r2",caller="ops",le="+Inf"} 4
tensormeld_router_ttft_seconds_sum{route="r2",caller="ops"} 2
tensormeld_router_ttft_seconds_count{route="r2",caller="ops"} 4
tensormeld_router_ttft_seconds_bucket{route="r3",le="+Inf"} 2
tensormeld_router_ttft_seconds_count{route="r3"} 2
tensormeld_router_ttft_seconds_bucket{route="r4",le="+Inf"} -1
tensormeld_router_ttft_seconds_sum{route="r4"} 0
tensormeld_router_ttft_seconds_count{route="r4"} -1
tensormeld_router_ttft_seconds_bucket{profile="x",le="+Inf"} 1
tensormeld_router_ttft_seconds_sum{profile="x"} 1
tensormeld_router_ttft_seconds_count{profile="x"} 1
tensormeld_router_ttft_seconds_bucket{profile="x",exported_profile="y",le="+Inf"} 1
tensormeld_router_ttft_seconds_sum{profile="x",exported_profile="y"} 1
tensormeld_router_ttft_seconds_count{profile="x",exported_profile="y"} 1
# TYPE tensormeld_router_caller_requests_total counter
tensormeld_router_caller_requests_total{caller="ops"} 9
tensormeld_router_caller_requests_total{caller="anonymous"} 2
# TYPE tensormeld_router_requests_total counter
tensormeld_router_requests_total 11
"""

ENGINE_HISTOGRAM = """
# TYPE strix_ttft_seconds histogram
strix_ttft_seconds_bucket{le="0.5"} 2
strix_ttft_seconds_bucket{le="+Inf"} 3
strix_ttft_seconds_sum 1.25
strix_ttft_seconds_count 3
"""


class PassthroughTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(metrics_export.secrets, "runtime_dir", return_value=Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_router_and_engine_histograms_and_callers(self):
        documents = healthy()
        documents[(ROUTER["address"], "/metrics")] = ROUTER_METRICS
        engine = sorted(key for key in documents if key[1] == "/metrics" and key[0] != ROUTER["address"])[0]
        documents[engine] += ENGINE_HISTOGRAM
        text = metrics_export.collect({"qwen": ROUTER}, fake_fetch(documents), telemetry=None, launches={}, families={})
        samples = prom.parse(text)
        base = dict(profile="qwen", route="r1", caller="ops")
        self.assertEqual(samples[("hyperloom_tensormeld_router_ttft_seconds_bucket", labels(le="+Inf", **base))], 4)
        self.assertEqual(samples[("hyperloom_tensormeld_router_ttft_seconds_count", labels(**base))], 4)
        # Buckets ascending by le, then sum and count, under one TYPE line.
        lines = [line for line in text.splitlines() if "route=\"r1\"" in line]
        self.assertEqual([line.split(',le="')[1].split('"')[0] if ',le="' in line else line.split("{")[0] for line in lines],
                         ["0.1", "1", "+Inf", "hyperloom_tensormeld_router_ttft_seconds_sum",
                          "hyperloom_tensormeld_router_ttft_seconds_count"])
        self.assertEqual(text.count("# TYPE hyperloom_tensormeld_router_ttft_seconds histogram"), 1)
        # Invalid (non-monotonic, +Inf != count) and incomplete (no sum) instances are left out whole.
        self.assertFalse(any(dict(key[1]).get("route") in ("r2", "r3", "r4") for key in samples))
        # An upstream profile label is kept as exported_profile; a collision with an existing one drops the instance.
        kept = [dict(key[1]) for key in samples if key[0] == "hyperloom_tensormeld_router_ttft_seconds_count"
                and "exported_profile" in dict(key[1])]
        self.assertEqual(kept, [{"exported_profile": "x", "profile": "qwen"}])
        self.assertEqual(samples[("hyperloom_tensormeld_router_caller_requests_total", labels(profile="qwen", caller="ops"))], 9)
        self.assertNotIn(("hyperloom_tensormeld_router_requests_total", labels(profile="qwen")), samples)
        self.assertNotIn("hyperloom_tensormeld_router_waiting", text)
        engine = dict(profile="qwen", replica="halo-1", machine="halo-1")
        self.assertEqual(samples[("hyperloom_strix_ttft_seconds_bucket", labels(le="0.5", **engine))], 2)
        self.assertEqual(samples[("hyperloom_source_up", labels(source="router_metrics", profile="qwen"))], 1)

    def test_sample_names_belong_to_one_family(self):
        out = metrics_export.Exposition()
        out.add("hyperloom_x_count", "gauge", {}, 1)
        rows = [("hyperloom_x_bucket", {"le": "+Inf"}, 1), ("hyperloom_x_sum", {}, 1), ("hyperloom_x_count", {}, 1)]
        self.assertFalse(out.passthrough_instance("hyperloom_x", "histogram", rows, "x"))
        self.assertTrue(out.passthrough_instance("hyperloom_y", "histogram",
                                                 [(n.replace("_x", "_y"), l, v) for n, l, v in rows], "y"))
        self.assertFalse(out.passthrough_instance("hyperloom_y", "histogram",
                                                  [(n.replace("_x", "_y"), l, v) for n, l, v in rows], "y"))
        out.add("hyperloom_y", "gauge", {}, 5)       # a hyperloom family can never join a passed-through one
        self.assertEqual(prom.parse(out.text())[("hyperloom_x_count", ())], 1)
        self.assertNotIn(("hyperloom_y", ()), prom.parse(out.text()))

    def test_budget_omits_whole_instances(self):
        lines = ["# TYPE big histogram"]
        for index in range(800):
            lines += [f'big_bucket{{i="{index}",le="+Inf"}} 1', f'big_sum{{i="{index}"}} 1', f'big_count{{i="{index}"}} 1']
        out = metrics_export.Exposition()
        copied = metrics_export.passthrough(out, "\n".join(lines) + "\n", {"profile": "p"}, "engine")
        self.assertLessEqual(copied, metrics_export.PASSTHROUGH_LIMIT)
        self.assertEqual(copied % 3, 0)
        counts = {dict(key[1])["i"] for key in prom.parse(out.text()) if key[0] == "hyperloom_big_count"}
        buckets = {dict(key[1])["i"] for key in prom.parse(out.text()) if key[0] == "hyperloom_big_bucket"}
        self.assertEqual(counts, buckets)


class FakeAPI:
    def __init__(self, documents, store=None):
        self.documents = documents
        self.telemetry = store
        self.sampler = None
        self.clock = lambda: NOW

    def document(self, name, **fields):
        return {"schema": f"hyperloom/ui-{name}-v1", **fields}

    def read(self, name, query):
        if name.startswith("history/"):
            return history.series(self, name[len("history/"):], query)
        if name not in self.documents:
            raise LookupError("unknown endpoint")
        self.last_query = (name, dict(query))
        return self.documents[name]


class CSVExportTests(unittest.TestCase):
    def setUp(self):
        reg = features.Registry()
        history.register(reg)
        integrations.register(reg)
        reg.get("energy", lambda api, q: {})
        reg.get("bench/runs", lambda api, q: {})
        features.reset(reg)
        self.addCleanup(features.reset)
        self.store = Store(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.api = FakeAPI({
            "devices": {"machines": [{"id": "halo-1", "name": "=HYPERLINK(\"x\")", "os": "linux", "health": "online",
                                      "access": "ssh", "ssh_target": "secret-target", "cpu_model": "+cpu",
                                      "disk_free_bytes": 5, "last_seen": -3}]},
            "stats": {"recent_requests": None, "why": {"recent_requests": "router not running"}},
            "energy": {"buckets": [{"start": 0, "end": 3600, "label": "00:00", "machines": {"halo-1": 2.0, "halo-2": 3.5},
                                    "coverage": 0.9}]},
            "bench/runs": {"runs": [{"id": "r1", "kind": "decode", "state": "done", "started": 1, "finished": 2,
                                     "model": "m", "target": "local", "summary": {"tok_s": 41.5, "note": "@x"}}]},
        }, self.store)

    def export(self, name, **query):
        return integrations.export(self.api, name + ".csv", query)

    def test_devices_formula_escaping_and_share_mode(self):
        result = self.export("devices")
        rows = result["csv"].split("\r\n")
        self.assertEqual(rows[0], "Machine,Name,OS,Health,Access,CPU,Host RAM,Disk free (bytes),Last seen")
        self.assertEqual(rows[1], "halo-1,\"'=HYPERLINK(\"\"x\"\")\",linux,online,ssh,'+cpu,,5,-3")
        self.assertNotIn("secret-target", result["csv"])
        shared = self.export("devices", share="1")
        self.assertEqual(shared["csv"].split("\r\n")[0], "Machine,OS,Health")
        self.assertEqual((shared["filename"], shared["share"]), ("hyperloom-devices-share.csv", True))

    def test_header_cells_are_escaped(self):
        self.assertEqual(integrations.to_csv([["k", "=label"]], [{"k": " -1x"}]), "'=label\r\n' -1x\r\n")
        self.assertEqual(integrations.cell(-5), "-5")
        self.assertEqual(integrations.cell(float("nan")), "")

    def test_unavailable_rows_unknown_targets_and_arguments(self):
        with self.assertRaises(PermissionError):
            self.export("stats")
        for name in ("settings", "../devices", "integrations", "devices.csv", "ledger"):
            with self.assertRaises(LookupError, msg=name):
                self.export(name)
        with self.assertRaises(ValueError):
            self.export("devices", range="3600")
        with self.assertRaises(ValueError):
            self.export("energy", range="1; drop")
        with self.assertRaises(ValueError):
            self.export("devices", share="yes")
        with self.assertRaises(LookupError):
            integrations.export(self.api, "devices.json", {})

    def test_energy_and_bench_adapters(self):
        energy = self.export("energy", range="24h")
        self.assertEqual(self.api.last_query, ("energy", {"range": "24h"}))
        self.assertEqual(energy["rows"], 2)
        self.assertIn("halo-2,3.5,0.9", energy["csv"])
        shared = self.export("energy", share="1")
        self.assertEqual(shared["csv"].split("\r\n")[1], "0,1970-01-01T00:00:00Z,00:00,5.5,0.9")
        bench = self.export("bench/runs", kind="decode", limit="20")
        self.assertEqual(bench["columns"][-1], "summary.tok_s")
        self.assertNotIn("@x", bench["csv"])
        self.assertNotIn("r1", self.export("bench/runs", share="1")["csv"].split("\r\n")[1])

    def test_history_rows_with_labels(self):
        self.store.describe("node_load1", "gauge", interval=60)
        for index in range(3):
            self.store.record([("node_load1", {"machine": "halo-1", "core": "all"}, index)], ts=NOW - 600 + index * 60)
        result = self.export("history/node_load1", range="3600", tier="m1")
        header = next(csv.reader(io.StringIO(result["csv"])))
        self.assertEqual(header[:4], ["Time (epoch s, UTC)", "Time (UTC)", "core", "machine"])
        self.assertEqual(result["rows"], 3)
        self.assertNotIn("core", self.export("history/node_load1", range="3600", share="1")["csv"].split("\r\n")[0])
        with self.assertRaises(ValueError):
            self.export("history/node_load1", foo="1")
        with self.assertRaises(ValueError):
            self.export("history/..%2Fdevices")

    def test_targets_follow_the_loaded_features(self):
        names = [target["document"] for target in integrations.export_targets(self.api)]
        self.assertEqual(names[:4], ["devices", "stats", "energy", "bench/runs"])
        self.assertNotIn("ledger", names)

    def test_size_and_row_caps(self):
        with mock.patch.object(integrations, "EXPORT_MAX_BYTES", 100):
            with self.assertRaises(ValueError):
                integrations.to_csv([["k", "k"]], [{"k": "x" * 60}, {"k": "y" * 60}])
        self.api.documents["devices"] = {"machines": [{"id": str(i)} for i in range(5)]}
        with mock.patch.object(integrations, "EXPORT_MAX_ROWS", 4):
            with self.assertRaises(ValueError):
                self.export("devices")


if __name__ == "__main__":
    unittest.main()
