"""Fleet documents: overview (F1), memory headroom by domain (F2), machine detail (F3), thresholds, no side effects
from GETs, and the text rule for this package's files."""

import base64
import codecs
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from hyperloom.telemetry.store import Store
from hyperloom.web import features
from hyperloom.web.api import API, APIError
from hyperloom.web.features import machines, overview

from tests.test_fleet_support import FakeAPI, bump, host, lab, nvidia_csv, pair_records, replica_records


GIB = 2 ** 30
ROOT = Path(__file__).resolve().parents[1]


def control_answer(commit_gib=60, rtx_free_gib=None):
    gpus, apps = nvidia_csv()
    cards = machines.parse_nvidia(gpus, apps)
    if rtx_free_gib is not None:
        cards[0]["memory_used_bytes"] = cards[0]["memory_total_bytes"] - int(rtx_free_gib * GIB)
    return {"schema": "hyperloom/node-v1", "verb": "host", "checks": {
        "uptime_s": {"status": "ok", "value": 7200.0},
        "memory": {"status": "ok", "value": {"MemTotal": 128 * GIB, "MemAvailable": 64 * GIB, "CommitTotal": 100 * GIB,
                                             "CommitLimit": (100 + commit_gib) * GIB, "CommitAvailable": commit_gib * GIB}},
        "nvidia": {"status": "ok", "value": cards}}}


class ThresholdTests(unittest.TestCase):
    def test_partial_updates_are_validated_as_a_whole(self):
        base = machines.thresholds(None)
        self.assertEqual(base, machines.DEFAULT_THRESHOLDS)
        merged = machines.validate_thresholds({"unified": {"amber_gib": 10}}, base)
        self.assertEqual(merged["unified"], {"amber_gib": 10.0, "red_gib": 4.0})
        self.assertEqual(machines.validate_thresholds({"commit": {"red_gib": None}}, base)["commit"]["red_gib"], None)
        self.assertEqual(machines.validate_thresholds({"rtx": {"amber_gib": 0, "red_gib": 0}}, base)["rtx"],
                         {"amber_gib": 0.0, "red_gib": 0.0})
        for bad in ({}, [], {"reset": False}, {"reset": True, "rtx": {}}, {"gpu": {"amber_gib": 1}},
                    {"rtx": {"amber_gib": 1, "red_gib": 2}}, {"unified": {"red_gib": 9}}, {"rtx": {"red_gib": None}},
                    {"rtx": {"amber_gib": -1}}, {"rtx": {"amber_gib": 2000}}, {"rtx": {"amber_gib": True}},
                    {"rtx": {"amber_gib": float("nan")}}, {"rtx": {"warn": 1}}, {"rtx": {}}, {"rtx": "1"}):
            with self.assertRaises(ValueError, msg=bad):
                machines.validate_thresholds(bad, base)
        self.assertEqual(machines.validate_thresholds({"reset": True}, merged), machines.DEFAULT_THRESHOLDS)

    def test_levels_are_strictly_under(self):
        limits = machines.DEFAULT_THRESHOLDS
        self.assertEqual(machines.level(8 * GIB, "unified", limits), "ok")
        self.assertEqual(machines.level(8 * GIB - 1, "unified", limits), "amber")
        self.assertEqual(machines.level(4 * GIB - 1, "unified", limits), "red")
        self.assertEqual(machines.level(0, "commit", limits), "amber")          # no red threshold for commit
        self.assertEqual(machines.level(None, "rtx", limits), "unknown")
        self.assertEqual(machines.level(1, None, limits), "info")
        zero = {"rtx": {"amber_gib": 0.0, "red_gib": 0.0}}
        self.assertEqual(machines.level(0, "rtx", zero), "ok")


class DomainTests(unittest.TestCase):
    def test_node_domains_are_separate_and_the_limit_is_named(self):
        entry = {"host": host("halo-1")}
        domains, limiting, why = machines.memory_domains(lab().machines["halo-1"], entry, machines.DEFAULT_THRESHOLDS)
        by = {d["domain"]: d for d in domains}
        self.assertEqual(set(by), {"system", "gpu-visible"})
        card = host("halo-1")["checks"]["gpus"]["value"][0]
        self.assertEqual(by["gpu-visible"]["used_bytes"], card["vram_used_bytes"] + card["gtt_used_bytes"])
        self.assertEqual(by["gpu-visible"]["total_bytes"], card["vram_total_bytes"] + card["gtt_total_bytes"])
        self.assertEqual(by["system"]["threshold"], "unified")
        self.assertIsNone(why)
        self.assertIn(limiting["domain"], ("system", "gpu-visible"))
        expected = min((d for d in domains), key=lambda d: d["available_bytes"])
        self.assertEqual(limiting["domain"], expected["domain"])

    def test_control_domains_commit_rtx_and_policy_exclusion(self):
        m = lab().machines["workstation"]
        domains, limiting, _ = machines.memory_domains(m, {"host": control_answer(commit_gib=7)}, machines.DEFAULT_THRESHOLDS)
        by = {(d["domain"], d["device"]): d for d in domains}
        self.assertEqual(by[("system", None)]["level"], "info")
        self.assertEqual(by[("commit", None)]["level"], "amber")
        self.assertTrue(by[("vram", "GPU-d4e5f6a7")]["excluded"])
        self.assertEqual(limiting["domain"], "commit")
        # A red RTX card wins over an amber commit headroom; the excluded card is never the limit.
        domains, limiting, _ = machines.memory_domains(m, {"host": control_answer(commit_gib=7, rtx_free_gib=0.5)}, machines.DEFAULT_THRESHOLDS)
        self.assertEqual((limiting["domain"], limiting["device"], limiting["level"]), ("vram", "GPU-a0b1c2d3", "red"))

    def test_zero_thresholds_and_stale_answers(self):
        m = lab().machines["workstation"]
        zero = {"unified": {"amber_gib": 0.0, "red_gib": 0.0}, "rtx": {"amber_gib": 0.0, "red_gib": 0.0},
                "commit": {"amber_gib": 0.0, "red_gib": None}}
        domains, limiting, _ = machines.memory_domains(m, {"host": control_answer()}, zero)
        self.assertEqual(limiting["level"], "ok")
        domains, limiting, why = machines.memory_domains(m, {"host": control_answer()}, machines.DEFAULT_THRESHOLDS, fresh=False)
        self.assertIsNone(limiting)
        self.assertIn("stale", why)
        self.assertTrue(all(d["level"] in ("unknown", "info") and d["stale"] for d in domains))

    def test_engine_report_sums_allocations_only(self):
        domains = [{"domain": "gpu-visible", "device": "card0", "used_bytes": 100 * GIB, "total_bytes": 120 * GIB,
                    "label": "GPU-visible"}]
        memory = {"weights_bytes": 60 * GIB, "state_bytes": 10 * GIB, "workspace_bytes": 5 * GIB, "trellis_bytes": 1 * GIB,
                  "state_bytes_per_slot": GIB, "device_free_bytes": 20 * GIB, "device_total_bytes": 120 * GIB}
        report = machines.engine_report(domains, [{"profile": "p", "memory": memory}])
        self.assertEqual(report["reports"][0]["reported_bytes"], 76 * GIB)
        self.assertEqual(report["unattributed_bytes"], 24 * GIB)
        self.assertNotIn("device_free_bytes", report["reports"][0]["parts"])
        other = dict(memory, device_total_bytes=24 * GIB)                    # reports about another device
        self.assertIsNone(machines.engine_report(domains, [{"profile": "p", "memory": other}])["unattributed_bytes"])
        two = domains + [{"domain": "vram", "device": "x", "used_bytes": GIB, "total_bytes": 2 * GIB, "label": "VRAM"}]
        self.assertIn("several GPU domains", machines.engine_report(two, [{"profile": "p", "memory": memory}])["why"])
        self.assertEqual(machines.engine_report(domains, [{"profile": "p", "memory": None}])["reports"], [])


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = FakeAPI(self.dir.name)
        self.records = {}
        patcher = mock.patch.object(machines, "launch_records", lambda api: self.records)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, now):
        self.api.clock.now = now
        points = machines.sample_nodes(self.api, now)
        with mock.patch.object(machines, "_control_host", lambda state: control_answer()), \
                mock.patch.object(machines, "_control_nvidia", lambda: control_answer()["checks"]["nvidia"]):
            points += machines.sample_control(self.api, now)
        self.api.telemetry.record(points, ts=now, interval=5)

    def test_machine_document(self):
        self.api.answers = {"halo-1": host("halo-1"), "halo-2": host("halo-2")}
        self.collect(1_800_000_000.0)
        self.api.answers["halo-1"] = bump(host("halo-1"), rx=500)
        self.collect(1_800_000_005.0)
        self.api.telemetry.event("health", "warning", "GPU hot", machine="halo-1")
        doc = machines.machine_document(self.api, "halo-1", {})
        self.assertEqual(doc["schema"], "hyperloom/ui-machine-v1")
        self.assertEqual(doc["status"], "online")
        self.assertEqual(doc["gpus"][0]["power_sensor"], "PPT")
        self.assertIn("power_limit_w", doc["gpus"][0]["why"])
        self.assertEqual(doc["memory"]["limiting"]["level"], "ok")
        self.assertEqual(doc["processes"][0]["comm"], host("halo-1")["checks"]["gpu_processes"]["value"][0]["comm"])
        self.assertIn("k10temp/Tctl", [t["sensor"] for t in doc["cpu"]["temperatures"]])
        self.assertEqual(doc["events"][0]["message"], "GPU hot")
        self.assertEqual(doc["history"]["node_gpu_busy_percent"], "history/node_gpu_busy_percent?l.machine=halo-1")
        self.assertAlmostEqual(next(n for n in doc["net"] if n["iface"] == "eno1")["rx_bytes_per_s"], 100.0)
        control = machines.machine_document(self.api, "workstation", {})
        self.assertEqual([g["kind"] for g in control["gpus"]], ["nvidia", "nvidia"])
        self.assertTrue(control["gpus"][1]["excluded"])
        self.assertEqual(control["gpus"][0]["throttle"], "other")
        self.assertEqual({p["device"] for p in control["processes"]}, {"GPU-a0b1c2d3", "GPU-d4e5f6a7"})
        with self.assertRaises(LookupError):
            machines.machine_document(self.api, "../etc", {})
        with self.assertRaises(ValueError):
            machines.machine_document(self.api, "halo-1", {"x": "1"})
        # Stale: no answer for more than three intervals.
        self.api.clock.now += 60
        stale = machines.machine_document(self.api, "halo-1", {})
        self.assertEqual(stale["status"], "stale")
        self.assertIsNone(stale["memory"]["limiting"])

    def test_without_a_store_every_field_says_why(self):
        self.api.telemetry = None
        doc = machines.machine_document(self.api, "halo-2", {})
        self.assertEqual(doc["status"], "unknown")
        self.assertIn("host", doc["why"])
        self.assertIn("events", doc["why"])
        ov = overview.overview(self.api, {})
        self.assertIn("machines", ov["why"])
        self.assertIsNone(ov["kpis"]["throughput"]["value"])
        self.assertTrue(ov["kpis"]["throughput"]["why"])

    def test_overview_counts_a_distributed_engine_once(self):
        self.records = pair_records()
        self.records.update(replica_records())
        tokens = {"n": 1000, "uptime": 100.0}

        def answer(payload):
            result = host("halo-1")
            ok = lambda body, a: {"status": "ok", "value": {"http_status": 200, "content_type": "", "body": body},
                                  "timing": {"start_s": a, "end_s": a + 0.02}}
            result["checks"]["engine_status"] = {"status": "ok", "value": [{"port": 18441, "wall_s": self.api.clock.now, "paths": {
                "/health": ok("ok", 0.0),
                "/api/status?detail=1": ok(json.dumps({"model": "mm-model", "context_tokens": 65536, "uptime_seconds": tokens["uptime"], "start_time": 5.0}), 0.02),
                "/metrics": ok(f"strix_generated_tokens_total {tokens['n']}\n", 0.04),
                "/api/status": ok(json.dumps({"uptime_seconds": tokens["uptime"] + 0.05, "start_time": 5.0}), 0.06)}}]}
            return result

        self.api.answers = {"halo-1": answer, "halo-2": host("halo-2")}
        self.collect(1_800_000_000.0)
        tokens.update(n=2000, uptime=105.0)
        self.collect(1_800_000_005.0)
        # The LAN replica (qw on halo-2) as the observability collector records it.
        store = self.api.telemetry
        labels = {"profile": "qw", "replica": "halo-2", "model": "qw-model"}
        store.describe("engine_generated_tokens", "delta", unit="tokens", interval=2)
        store.record([("engine_up", labels, 1, "state"), ("engine_queue_running", labels, 3), ("engine_queue_waiting", labels, 0)],
                     ts=1_800_000_004.0)
        for i in range(10):
            store.record([("engine_generated_tokens", labels, 100, "delta")], ts=1_800_000_005.0 - 59 + i * 6, interval=2)
        snapshot = {"devices": {"server_time": 1_800_000_000.0, "machines": [
            {"id": "halo-1", "doctor": {"checks": {"lab": {"status": "ok", "value": {"holder": {"status": "ok", "value": "hl-mm-pair 1234"}}}}}}]}}
        self.api._cached = snapshot
        doc = overview.overview(self.api, {})
        cards = {c["id"]: c for c in doc["machines"]}
        mm = next(e for e in cards["halo-1"]["engines"] if e["profile"] == "mm")
        self.assertEqual((mm["role"], mm["throughput"], mm["model"]), ("rank 0", 200.0, "mm-model"))
        rank1 = next(e for e in cards["halo-2"]["engines"] if e["profile"] == "mm")
        self.assertIsNone(rank1["throughput"])
        qw = next(e for e in cards["halo-2"]["engines"] if e["profile"] == "qw")
        self.assertEqual((qw["model"], qw["running"]), ("qw-model", 3))
        self.assertAlmostEqual(qw["throughput"], 1000 / 20)                         # 10 samples x 2 s in the last minute
        self.assertAlmostEqual(doc["kpis"]["throughput"]["value"], 200.0 + 50.0)    # mm once, qw once
        self.assertEqual(doc["kpis"]["machines_online"], {"value": 3, "total": 3, "observer": "fleet collectors"})
        self.assertEqual([r["role"] for r in cards["workstation"]["roles"]], ["portal", "prefill service", "router"])
        self.assertEqual({r["role"] for r in cards["halo-1"]["roles"]}, {"rank 0", "router"})
        self.assertEqual(cards["halo-1"]["turn_holder"], "hl-mm-pair 1234")
        self.assertIn("snapshot", cards["halo-2"]["why"]["turn_holder"] + " snapshot")
        power = doc["kpis"]["power"]
        self.assertEqual({c["domain"] for c in power["covered"]}, {"GPU package (PPT)", "board"})
        self.assertTrue(power["not_covered"])
        self.assertEqual({m["domain"] for m in doc["kpis"]["memory"]["domains"]}, {"system", "gpu-visible", "vram", "commit"})
        vram = next(m for m in doc["kpis"]["memory"]["domains"] if m["domain"] == "vram")
        self.assertEqual(vram["machines"], ["workstation"])                         # the excluded card is not counted
        self.assertIsNone(cards["halo-1"]["emitted_rate"])
        self.assertIn("C2", cards["halo-1"]["why"]["emitted_rate"])
        # An old cached snapshot is not used.
        self.api._cached = {"devices": {"server_time": 1_799_999_000.0, "machines": snapshot["devices"]["machines"]}}
        doc = overview.overview(self.api, {})
        halo1 = next(c for c in doc["machines"] if c["id"] == "halo-1")
        self.assertIsNone(halo1["turn_holder"])
        self.assertIn("older than 30 s", halo1["why"]["turn_holder"])


class PortalTests(unittest.TestCase):
    """Through the real API: registration, GETs read only, the thresholds POST is audited."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        reg = features.load(("overview", "machines", "history"))
        self.assertEqual(reg.failed, {})
        features.reset(reg)
        self.addCleanup(features.reset)
        orchestrator = mock.Mock()
        orchestrator.load_cluster.return_value = lab()
        remote = mock.Mock()
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=orchestrator, remote_module=remote,
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.api.telemetry = Store(":memory:")
        self.remote, self.orchestrator = remote, orchestrator
        patcher = mock.patch.object(machines, "launch_records", lambda api: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_gets_never_call_nodes_or_refresh_the_snapshot(self):
        with mock.patch.object(API, "_node", side_effect=AssertionError("node call from a GET")), \
                mock.patch.object(API, "_snapshot", side_effect=AssertionError("snapshot refresh from a GET")):
            for endpoint in ("overview", "machines/halo-1", "machines/workstation", "fleet/thresholds"):
                document = self.api.read(endpoint, {})
                self.assertTrue(document["schema"].startswith("hyperloom/ui-"), endpoint)
            with self.assertRaises(APIError) as caught:
                self.api.read("machines/nope", {})
            self.assertEqual(caught.exception.status, 404)
        self.orchestrator.status.assert_not_called()

    def test_thresholds_post_is_validated_and_audited(self):
        status, result = self.api.feature_post("fleet/thresholds/set", {"unified": {"amber_gib": 12}})
        self.assertEqual((status, result["thresholds"]["unified"]["amber_gib"]), (200, 12.0))
        self.assertEqual(self.api.read("fleet/thresholds", {})["thresholds"]["unified"]["amber_gib"], 12.0)
        with self.assertRaises(APIError) as caught:
            self.api.feature_post("fleet/thresholds/set", {"unified": {"red_gib": 99}})
        self.assertEqual(caught.exception.status, 400)
        log = [json.loads(line) for line in (Path(self.dir.name) / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["action"], e["result"]["state"]) for e in log],
                         [("fleet/thresholds/set", "succeeded"), ("fleet/thresholds/set", "refused")])
        self.api.telemetry = None
        self.assertIn("thresholds", self.api.read("fleet/thresholds", {})["why"])


class TextRuleTests(unittest.TestCase):
    """This package's files carry no address literals, home paths, lab host names, user names or review identities."""

    FILES = ["node/hyperloom_observe.py", "hyperloom/web/features/machines.py", "hyperloom/web/features/overview.py",
             "hyperloom/cliext/telemetry.py", "hyperloom/web/static/pages/overview.js", "hyperloom/web/static/pages/overview.css",
             "hyperloom/web/static/pages/machine.js", "hyperloom/web/static/pages/machine.css", "tests/ui/seeds/fleet.py",
             "tests/test_fleet_node.py", "tests/test_fleet_collect.py", "tests/test_fleet_docs.py", "tests/test_fleet_support.py"]
    # Stored encoded so this file stays clean itself.
    WORDS = [codecs.decode(w, "rot13") for w in ("pyhfgre1", "pyhfgre2", "fretvb", "fbypbqrk", "pbqrk", "pynhqr", "bchf", "fbaarg", "tcg-")]

    def paths(self):
        found = [ROOT / name for name in self.FILES]
        found += sorted((ROOT / "tests/fixtures/fleet").iterdir())
        return [p for p in found if p.is_file()]

    def test_files_are_clean(self):
        ipv4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
        # Four or more groups, or a "::" between hex groups (PCI addresses such as 0000:00:18.3 have three).
        ipv6 = re.compile(r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}|[0-9a-f]{0,4}::[0-9a-f]{1,4})(?![\w:])", re.I)
        home = re.compile(r"/home/\w|[A-Za-z]:\\Users\\|/Users/\w")
        mac = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
        for path in self.paths():
            text = path.read_text(encoding="utf-8")
            name = path.relative_to(ROOT).as_posix()
            self.assertFalse(ipv4.search(text), f"{name}: address literal {ipv4.search(text)}")
            self.assertFalse(ipv6.search(mac.sub("", text)), f"{name}: address literal {ipv6.search(mac.sub('', text))}")
            self.assertFalse(home.search(text), f"{name}: home path")
            for found in mac.findall(text):
                self.assertTrue(found.startswith("02:00:00:00:"), f"{name}: MAC {found}")
            lowered = text.lower()
            for word in self.WORDS:
                self.assertNotIn(word, lowered, f"{name}: forbidden identity")


if __name__ == "__main__":
    unittest.main()


class DocumentSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = FakeAPI(self.dir.name)
        patcher = mock.patch.object(machines, "launch_records", lambda api: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_rate_needs_the_current_device_identity(self):
        self.api.answers = {"halo-1": host("halo-1"), "halo-2": host("halo-2")}
        machines.sample_nodes(self.api, 1_800_000_000.0)
        self.api.answers["halo-1"] = bump(host("halo-1"), rx=900)
        self.api.clock.now = 1_800_000_005.0
        machines.sample_nodes(self.api, 1_800_000_005.0)
        eno = lambda doc: next(n for n in doc["net"] if n["iface"] == "eno1")
        self.assertAlmostEqual(eno(machines.machine_document(self.api, "halo-1", {}))["rx_bytes_per_s"], 180.0)
        unknown = bump(host("halo-1"), rx=1800)
        for row in unknown["checks"]["net"]["value"]:
            row.pop("ifindex")
        self.api.answers["halo-1"] = unknown
        self.api.clock.now = 1_800_000_006.0
        machines.sample_nodes(self.api, 1_800_000_006.0)
        self.assertIsNone(eno(machines.machine_document(self.api, "halo-1", {}))["rx_bytes_per_s"])

    def test_failed_nvidia_process_queries_reach_the_document(self):
        gpus, _ = nvidia_csv()
        failed = machines.parse_nvidia(gpus, None, "nvidia-smi exited with 6")
        answer = control_answer()
        answer["checks"]["nvidia"] = {"status": "ok", "value": failed}
        answer["checks"]["gpu_processes"] = {"status": "unavailable", "value": None, "why": "see the processes of each NVIDIA card"}
        self.api.telemetry.put(machines.HOSTS, "workstation", {"ts": self.api.clock(), "ok": True, "last_ok": self.api.clock(),
                                                               "access": "local", "host": answer, "instance": "boot-1"})
        doc = machines.machine_document(self.api, "workstation", {})
        self.assertIsNone(doc["processes"])
        self.assertIn("exited with 6", doc["why"]["processes"])
        # A node: its own list is kept and an NVIDIA failure is still named.
        node = host("halo-1")
        node["checks"]["nvidia"] = {"status": "ok", "value": failed}
        self.api.telemetry.put(machines.HOSTS, "halo-1", {"ts": self.api.clock(), "ok": True, "last_ok": self.api.clock(),
                                                          "access": "ssh", "host": node, "instance": "b"})
        doc = machines.machine_document(self.api, "halo-1", {})
        self.assertEqual(len(doc["processes"]), len(node["checks"]["gpu_processes"]["value"]))
        self.assertIn("GPU-a0b1c2d3", doc["why"]["processes"])
