"""Fleet collectors: node answers -> shared series, counters through baselines, one node call per machine, loopback
engines (C4), the control machine (C6) and the CLI."""

import copy
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from hyperloom import procs
from hyperloom.cliext import telemetry as cli_telemetry
from hyperloom.telemetry.store import BASELINES
from hyperloom.web.features import machines

from tests.test_fleet_support import FakeAPI, bump, host, lab, machine, nvidia_csv, pair_records, replica_records


def series(points):
    return {(name, tuple(sorted(labels.items()))): value for name, labels, value, *_ in points}


class HostPointsTests(unittest.TestCase):
    def test_recorded_answer_maps_to_the_shared_names(self):
        gauges, counters = machines.host_points("halo-1", host("halo-1"))
        found = series(gauges)
        names = {name for name, _ in found}
        self.assertEqual(names, {"node_cpu_busy_percent", "node_load1", "node_mem_available_bytes", "node_mem_total_bytes",
                                 "node_gpu_busy_percent", "node_gpu_temp_celsius", "node_gpu_power_watts",
                                 "node_gpu_clock_mhz", "node_gpu_mem_used_bytes", "node_gpu_mem_total_bytes",
                                 "node_cpu_temp_celsius", "node_sensor_temp_celsius", "node_disk_used_bytes",
                                 "node_disk_total_bytes"})
        self.assertNotIn("node_gpu_power_limit_watts", names)            # the APU reports no readable cap
        key = lambda name, **labels: (name, tuple(sorted({"machine": "halo-1", **labels}.items())))
        self.assertIn(key("node_gpu_power_watts", device="card0", sensor="PPT"), found)
        self.assertIn(key("node_gpu_temp_celsius", device="card0", sensor="amdgpu/edge"), found)
        self.assertIn(key("node_cpu_temp_celsius", sensor="k10temp/Tctl"), found)
        self.assertIn(key("node_sensor_temp_celsius", sensor="nvme/Composite"), found)
        self.assertIn(key("node_gpu_mem_used_bytes", device="card0", domain="gtt"), found)
        sensors = {dict(labels).get("sensor") for name, labels in found if name == "node_sensor_temp_celsius"}
        self.assertFalse(any(s.startswith("amdgpu") for s in sensors))   # GPU sensors are GPU series only
        self.assertEqual({name for name, *_ in counters}, {"node_disk_read_bytes", "node_disk_write_bytes",
                                                           "node_net_rx_bytes", "node_net_tx_bytes"})
        self.assertEqual({labels["iface"] for name, labels, _ in counters if name == "node_net_rx_bytes"}, {"eno1", "wlp195s0"})

    def test_sensor_identity_is_device_qualified_and_never_joins_unstable_ones(self):
        chips = [{"hwmon": "hwmon1", "name": "nvme", "device": "nvme0", "temps": [{"sensor": "Composite", "input": "temp1", "celsius": 50}]},
                 {"hwmon": "hwmon2", "name": "nvme", "device": "nvme1", "temps": [{"sensor": "Composite", "input": "temp1", "celsius": 51}]},
                 {"hwmon": "hwmon3", "name": "acpitz", "device": None, "temps": [{"sensor": "temp1", "input": "temp1", "celsius": 60}]},
                 {"hwmon": "hwmon4", "name": "acpitz", "device": None, "temps": [{"sensor": "temp1", "input": "temp1", "celsius": 61}]},
                 {"hwmon": "hwmon5", "name": "k10temp", "device": "x", "temps": [{"sensor": "Tccd", "input": "temp3", "celsius": 70},
                                                                                  {"sensor": "Tccd", "input": "temp4", "celsius": 71}],
                  "power": [{"sensor": "SVI2_P_Core", "input": "power1", "field": "input", "watts": 30.0}]}]
        answer = {"checks": {"sensors": {"status": "ok", "value": chips}}}
        found = series(machines.host_points("m", answer)[0])
        sensors = sorted(dict(labels)["sensor"] for name, labels in found)
        self.assertEqual(sensors, ["k10temp/SVI2_P_Core", "k10temp/Tccd#temp3", "k10temp/Tccd#temp4",
                                   "nvme@nvme0/Composite", "nvme@nvme1/Composite"])     # acpitz@hwmonN: not recorded
        self.assertIn(("node_cpu_power_watts", (("machine", "m"), ("sensor", "k10temp/SVI2_P_Core"))), found)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = FakeAPI(self.dir.name)
        self.store = self.api.telemetry
        self.records = {}
        patcher = mock.patch.object(machines, "launch_records", lambda api: self.records)
        patcher.start()
        self.addCleanup(patcher.stop)

    def round(self, now):
        self.api.clock.now = now
        return machines.sample_nodes(self.api, now)

    def test_counters_become_deltas_with_rates_bound_to_the_boot(self):
        first = host("halo-1")
        self.api.answers = {"halo-1": first, "halo-2": host("halo-2")}
        points = self.round(1_800_000_000.0)
        self.assertIn(("node_up", {"machine": "halo-1"}, 1), points)
        self.assertFalse(any(name.startswith("node_net") for name, *_ in points))       # deltas never go back to the sampler
        self.store.record(points, ts=1_800_000_000.0, interval=5)
        self.assertEqual(self.store.query("node_net_rx_bytes", 1_799_999_000, 1_800_000_100)["lines"], [])
        self.api.answers["halo-1"] = bump(first, rx=5000, tx=500, read=1000)
        self.round(1_800_000_005.0)
        lines = self.store.query("node_net_rx_bytes", 1_799_999_000, 1_800_000_100, labels={"machine": "halo-1", "iface": "eno1"}, tier="raw")["lines"]
        self.assertEqual([p[1] for p in lines[0]["points"]], [5000.0])
        doc = machines.machine_document(self.api, "halo-1", {})
        eno = next(n for n in doc["net"] if n["iface"] == "eno1")
        self.assertAlmostEqual(eno["rx_bytes_per_s"], 1000.0)
        self.assertAlmostEqual(eno["tx_bytes_per_s"], 100.0)
        self.assertAlmostEqual(doc["disk_io"][0]["read_bytes_per_s"], 200.0)
        # A reboot: a new instance, no negative or invented delta, and the old boot's rate is gone.
        self.api.answers["halo-1"] = bump(first, rx=-1000, boot="0000000f-0000-4000-8000-00000000000f")
        self.round(1_800_000_010.0)
        lines = self.store.query("node_net_rx_bytes", 1_799_999_000, 1_800_000_100, labels={"machine": "halo-1", "iface": "eno1"}, tier="raw")["lines"]
        self.assertEqual([p[1] for p in lines[0]["points"]], [5000.0])
        doc = machines.machine_document(self.api, "halo-1", {})
        self.assertIsNone(next(n for n in doc["net"] if n["iface"] == "eno1")["rx_bytes_per_s"])

    def test_cpu_baseline_goes_back_to_the_node(self):
        self.api.answers = {"halo-1": host("halo-1"), "halo-2": host("halo-2")}
        self.round(1_800_000_000.0)
        self.round(1_800_000_005.0)
        payloads = [payload for mid, verb, payload in self.api.calls if mid == "halo-1"]
        self.assertEqual(payloads[0], {})
        cpu = host("halo-1")["checks"]["cpu"]["value"]
        self.assertEqual(payloads[1], {"cpu": {"boot_id": host("halo-1")["checks"]["boot_id"]["value"],
                                               "total": cpu["total"], "idle": cpu["idle"]}})

    def test_failure_is_an_outcome_of_that_machine(self):
        self.api.answers = {"halo-1": RuntimeError("ssh: connection refused"), "halo-2": host("halo-2")}
        points = self.round(1_800_000_000.0)
        self.assertIn(("node_up", {"machine": "halo-1"}, 0), points)
        self.assertIn(("node_up", {"machine": "halo-2"}, 1), points)
        entry = self.store.get(machines.HOSTS, "halo-1")
        self.assertEqual((entry["ok"], entry["failures"]), (False, 1))
        self.assertIn("connection refused", entry["error"])
        self.api.answers["halo-1"] = {"schema": "hyperloom/node-v1", "verb": "host", "error": "not implemented in this node tool"}
        self.round(1_800_000_005.0)
        self.assertIn("not implemented", self.store.get(machines.HOSTS, "halo-1")["error"])

    def test_one_call_in_flight_per_machine(self):
        release, entered = threading.Event(), threading.Event()

        def slow(payload):
            entered.set()
            release.wait(10)
            return host("halo-1")

        self.api.answers = {"halo-1": slow, "halo-2": host("halo-2")}
        with mock.patch.object(machines, "ROUND_S", 0.3):
            self.round(1_800_000_000.0)            # halo-1 does not finish within the round
            self.assertTrue(entered.is_set())
            self.round(1_800_000_005.0)            # its call is still in flight: not called again
        calls = [mid for mid, verb, _ in self.api.calls]
        self.assertEqual(calls.count("halo-1"), 1)
        self.assertEqual(calls.count("halo-2"), 2)
        entry = self.store.get(machines.HOSTS, "halo-1")
        self.assertEqual(entry["skip_why"], "previous node call still in flight")
        release.set()
        for _ in range(100):
            if machines._state(self.api).machine_locks["halo-1"].acquire(blocking=False):
                machines._state(self.api).machine_locks["halo-1"].release()
                break
            time.sleep(0.02)
        # Another process holding the machine's gate also keeps this collector away.
        gate = procs.InstanceLock(machines.gate_path(self.api, "halo-2")).acquire()
        try:
            self.round(1_800_000_010.0)
        finally:
            gate.close()
        self.assertEqual([mid for mid, verb, _ in self.api.calls].count("halo-2"), 2)
        self.assertEqual(self.store.get(machines.HOSTS, "halo-2")["skip_why"], "previous node call still in flight")

    def test_loopback_engines_rank_zero_only_with_identity(self):
        self.records = pair_records()
        self.records.update(replica_records())
        targets = machines.loopback_engines(self.records)
        self.assertEqual(list(targets), ["halo-1"])                 # LAN replicas are not this package's to read
        self.assertEqual(targets["halo-1"][0]["port"], 18441)
        bodies = {"uptime": 100.0, "tokens": 1000}

        def answer(payload):
            self.assertEqual(payload["engine_status"], [{"port": 18441, "paths": machines.ENGINE_PATHS}])
            result = host("halo-1")
            status = json.dumps({"model": "mm-model", "context_tokens": 65536, "uptime_seconds": bodies["uptime"],
                                 "queue": {"queued": 1, "running": 2}})
            metrics = f"strix_generated_tokens_total {bodies['tokens']}\nstrix_queue_waiting 1\n"
            ok = lambda body, a, b: {"status": "ok", "value": {"http_status": 200, "content_type": "", "body": body},
                                     "timing": {"start_s": a, "end_s": b}}
            after = json.dumps({"uptime_seconds": bodies.get("after", bodies["uptime"] + 0.03)})
            result["checks"]["engine_status"] = {"status": "ok", "value": [{"port": 18441, "wall_s": self.api.clock.now + 0.5, "paths": {
                "/health": ok("ok", 0.08, 0.10), "/api/status?detail=1": ok(status, 0.10, 0.12),
                "/metrics": ok(metrics, 0.12, 0.14), "/api/status": ok(after, 0.14, 0.16)}}]}
            return result

        self.api.answers = {"halo-1": answer, "halo-2": host("halo-2")}
        self.round(1_800_000_000.0)
        halo2 = [payload for mid, verb, payload in self.api.calls if mid == "halo-2"]
        self.assertNotIn("engine_status", halo2[0])                 # rank 1 is never polled
        bodies.update(uptime=105.0, tokens=1500)
        self.round(1_800_000_005.0)
        key = machines.engine_key("halo-1", "mm", 18441)
        doc = self.store.get(machines.ENGINES, key)
        self.assertEqual((doc["replica"], doc["profile"], doc["health"]), ("halo-1", "mm", "ok"))
        self.assertTrue(doc["instance"].startswith("start:"))
        rows = machines.engine_rows(self.store, "halo-1", self.records, "workstation", 1_800_000_005.0)
        self.assertEqual(rows[0]["throughput"], 100.0)              # 500 tokens over 5 s
        self.assertEqual((rows[0]["model"], rows[0]["context_length"], rows[0]["running"], rows[0]["queued"]),
                         ("mm-model", 65536, 2, 1))
        follower = machines.engine_rows(self.store, "halo-2", self.records, "workstation", 1_800_000_005.0)
        rank1 = next(r for r in follower if r["profile"] == "mm")
        self.assertEqual((rank1["role"], rank1["follower_of"], rank1["throughput"]), ("rank 1", "halo-1", None))
        # A replacement between the status read and the metrics read: the bracket read shows a fresh process.
        bodies.update(uptime=110.0, tokens=99000, after=0.3)
        self.round(1_800_000_007.0)
        self.assertIsNone(self.store.get(machines.ENGINES, key)["instance"])
        self.assertIsNone(machines.engine_rows(self.store, "halo-1", self.records, "workstation", 1_800_000_007.0)[0]["throughput"])
        bodies.pop("after")
        # A replacement engine on the same port a second after a young start: a new instance, no false delta.
        bodies.update(uptime=4.0, tokens=9000)
        self.round(1_800_000_010.0)
        rows = machines.engine_rows(self.store, "halo-1", self.records, "workstation", 1_800_000_010.0)
        self.assertIsNone(rows[0]["throughput"])
        # The engine stopped: its document and rate go away.
        self.records = {}
        self.round(1_800_000_015.0)
        self.assertIsNone(self.store.get(machines.ENGINES, key))
        self.assertEqual(self.store.execute("SELECT key FROM fleet_rates WHERE key LIKE 'engine|%'"), [])


class EngineIdentityTests(unittest.TestCase):
    def test_bracket_proves_one_process_around_the_counter(self):
        quick = ({"start_s": 0.10, "end_s": 0.12}, {"start_s": 0.14, "end_s": 0.16})
        self.assertTrue(machines.bracketed({"uptime_seconds": 50.0}, {"uptime_seconds": 50.03}, *quick))
        self.assertFalse(machines.bracketed({"uptime_seconds": 50.0}, {"uptime_seconds": 0.2}, *quick))     # replaced
        self.assertFalse(machines.bracketed({"uptime_seconds": 50.5}, {"uptime_seconds": 51.5}, *quick))    # outside the window
        self.assertFalse(machines.bracketed({"uptime_seconds": 0.03}, {"uptime_seconds": 0.07}, *quick))    # too young: ambiguous
        # Slow reads (the first answer took 2 s, the second 2 s): an engine at 4.2 s cannot be told from a replacement.
        slow = ({"start_s": 0.0, "end_s": 2.0}, {"start_s": 2.6, "end_s": 4.6})
        self.assertFalse(machines.bracketed({"uptime_seconds": 4.2}, {"uptime_seconds": 4.4}, *slow))
        self.assertTrue(machines.bracketed({"uptime_seconds": 400.0}, {"uptime_seconds": 403.0}, *slow))
        self.assertFalse(machines.bracketed({"uptime_seconds": 400.0}, {"uptime_seconds": 403.0}, None, slow[1]))
        self.assertTrue(machines.bracketed({"uptime_seconds": 400}, {"uptime_seconds": 401}, *quick))          # whole seconds
        self.assertTrue(machines.bracketed({"instance_id": "a"}, {"instance_id": "a"}, None, None))
        self.assertFalse(machines.bracketed({"instance_id": "a"}, {"instance_id": "b"}, *quick))

    def sample(self, uptime, wall, a, b, previous=None, launch="L1"):
        status = {"uptime_seconds": uptime}
        window = machines.start_window(status, {"start_s": a, "end_s": b}, wall)
        instance, window = machines._instance(status, previous, launch, window)
        return {"instance": instance, "window": window, "launch": launch}

    def test_continuity_uses_the_start_window_on_the_node_clock(self):
        first = self.sample(100.0, 1000.0, 0.10, 0.12)
        self.assertEqual(self.sample(105.03, 1005.0, 0.10, 0.12, first)["instance"], first["instance"])    # same process
        self.assertNotEqual(self.sample(105.0, 1005.0, 0.10, 0.12, first, launch="L2")["instance"], first["instance"])
        self.assertNotEqual(self.sample(2.0, 1005.0, 0.10, 0.12, first)["instance"], first["instance"])     # restarted
        # The case of a replacement right after a sample whose answers came late in its round: captures at
        # 100.21 (uptime 3.25) and, after a replacement started at 100.30, at 103.61 (uptime 3.31).
        old = self.sample(3.25, 100.0, 0.20, 0.22)
        new = self.sample(3.31, 103.4, 0.20, 0.22, old)
        self.assertNotEqual(new["instance"], old["instance"])
        self.assertEqual(machines._instance({"instance_id": "abc"}, None, "L1", None)[0], "id:abc")
        self.assertEqual(machines._instance({"model": "x"}, None, "L1", None), (None, None))
        self.assertEqual(self.sample(1.0, 1.0, 0, 0.1, launch=None)["instance"], None)
        self.assertIsNone(machines.start_window({"uptime_seconds": 1.0}, None, 1.0))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = FakeAPI(self.dir.name)

    def test_nvidia_parsing_and_throttle_states(self):
        gpus, apps = nvidia_csv()
        cards = machines.parse_nvidia(gpus, apps)
        self.assertEqual([c["device"] for c in cards], ["GPU-a0b1c2d3", "GPU-d4e5f6a7"])
        pro, consumer = cards
        self.assertEqual((pro["power_w"], pro["power_limit_w"], pro["temperature_c"], pro["clock_mhz"]), (132.58, 498.0, 53.0, 2842.0))
        self.assertEqual(pro["memory_total_bytes"], 97887 * 1024 * 1024)
        self.assertEqual((pro["throttle"], pro["throttle_reasons"]), (3, ["reason bit 0x400"]))
        self.assertEqual((consumer["throttle"], consumer["throttle_reasons"]), (0, ["idle"]))
        self.assertEqual([p["comm"] for p in pro["processes"]], ["tool.exe", "engine.exe"])   # file names only
        self.assertIsNone(pro["processes"][0]["vram_bytes"])
        self.assertEqual(machines.throttle_state(0x20)[0], 2)
        self.assertEqual(machines.throttle_state(0x4 | 0x1)[0], 1)
        self.assertEqual(machines.throttle_state(0x40 | 0x4)[0], 2)
        self.assertEqual(machines.throttle_state(None), (None, None))
        broken = gpus.replace("132.58", "[N/A]").replace("P1", "[Not Supported]")
        card = machines.parse_nvidia(broken)[0]
        self.assertIsNone(card["power_w"])
        self.assertIn("power_w", card["why"])
        self.assertEqual(machines.parse_nvidia("garbage,1\n"), [])

    def test_nvidia_sample_failures_are_unavailable(self):
        def missing(arguments):
            raise FileNotFoundError("nvidia-smi not found")
        self.assertEqual(machines.nvidia_sample(missing)["status"], "unavailable")
        runs = []
        gpus, apps = nvidia_csv()
        sample = machines.nvidia_sample(lambda arguments: runs.append(arguments) or (gpus if "--query-gpu" in arguments[0] else apps))
        self.assertEqual(sample["status"], "ok")
        self.assertTrue(all(a[-1] == "--format=csv,noheader,nounits" for a in runs))

    def test_windows_answer_and_counter_cadence(self):
        class System:
            def __init__(self):
                self.ticks = [(1000, 2000), (1500, 3000)]
                self.rx = 1000

            def uptime(self):
                return 3600.0

            def memory(self):
                return {"MemTotal": 128 * 2 ** 30, "MemAvailable": 64 * 2 ** 30, "CommitTotal": 100 * 2 ** 30,
                        "CommitLimit": 160 * 2 ** 30, "CommitAvailable": 60 * 2 ** 30}

            def cpu_times(self):
                return self.ticks.pop(0) if self.ticks else (1500, 3000)

            def cores(self):
                return 32

            def disks(self):
                return [{"mount": "C:", "fstype": None, "total_bytes": 100, "used_bytes": 40, "free_bytes": 60}]

            def interfaces(self):
                self.rx += 2000
                return [{"iface": "Ethernet", "luid": 1, "rx_bytes": self.rx, "tx_bytes": 10, "physical": True, "operstate": "up"}]

        system = System()
        state = machines._state(self.api)
        state.windows["system"] = system
        gpus, apps = nvidia_csv()
        with mock.patch.object(machines.os, "name", "nt"), \
                mock.patch.object(machines, "_control_nvidia", lambda: {"status": "ok", "value": machines.parse_nvidia(gpus, apps)}):
            first = machines.sample_control(self.api, 1_800_000_000.0)
            second = machines.sample_control(self.api, 1_800_000_002.0)
            third = machines.sample_control(self.api, 1_800_000_006.0)
        found = series(second)
        self.assertEqual(found[("node_commit_available_bytes", (("machine", "workstation"),))], 60 * 2 ** 30)
        self.assertAlmostEqual(found[("node_cpu_busy_percent", (("machine", "workstation"),))], 50.0)
        self.assertNotIn(("node_cpu_busy_percent", (("machine", "workstation"),)), series(first))
        self.assertEqual(found[("node_gpu_throttled", (("device", "GPU-a0b1c2d3"), ("machine", "workstation")))], 3)
        self.assertIn(("node_gpu_mem_used_bytes", (("device", "GPU-d4e5f6a7"), ("domain", "vram"), ("machine", "workstation"))), found)
        # Counters are committed at most once per node interval (first round, then the round 6 s later).
        raw = self.api.telemetry.query("node_net_rx_bytes", 1_799_999_000, 1_800_000_100, tier="raw")["lines"]
        self.assertEqual([p[1] for p in raw[0]["points"]], [4000.0])         # 3000 -> 7000 bytes
        entry = self.api.telemetry.get(machines.HOSTS, "workstation")
        self.assertEqual(entry["access"], "local")
        self.assertEqual(len(entry["host"]["checks"]["nvidia"]["value"]), 2)
        self.assertTrue(third)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = FakeAPI(self.dir.name)
        self.api.answers = {"halo-1": host("halo-1"), "halo-2": host("halo-2")}
        patches = [mock.patch.object(cli_telemetry, "_api", lambda args: self.api),
                   mock.patch.object(machines, "launch_records", lambda api: {})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        from hyperloom import cli
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["telemetry", *argv])
        return code, json.loads(out.getvalue())

    def test_dry_run_sample_writes_nothing(self):
        machines.sample_nodes(self.api, 1_800_000_000.0)        # an existing portal state
        before = {ns: self.api.telemetry.items(ns) for ns in (machines.HOSTS, BASELINES)}
        with mock.patch.object(cli_telemetry, "_existing", lambda api: api.telemetry):
            self.api.answers["halo-1"] = bump(host("halo-1"), rx=100)
            code, out = self.run_cli("sample", "--collector", "fleet-nodes")
        self.assertEqual(code, 0)
        self.assertFalse(out["recorded"])
        self.assertIn({"series": "node_up", "labels": {"machine": "halo-1"}, "value": 1}, out["collectors"]["fleet-nodes"]["points"])
        self.assertEqual({ns: self.api.telemetry.items(ns) for ns in before}, before)

    def test_record_status_query_and_thresholds(self):
        with mock.patch.object(cli_telemetry, "_existing", lambda api: api.telemetry), \
                mock.patch("hyperloom.telemetry.open_store", lambda api: api.telemetry):
            code, out = self.run_cli("sample", "--collector", "fleet-nodes", "--record")
            self.assertEqual((code, out["recorded"]), (0, True))
            self.assertGreater(out["collectors"]["fleet-nodes"]["recorded"], 10)
            code, out = self.run_cli("status")
            self.assertEqual(out["machines"]["halo-1"]["status"], "online")
            self.assertIn("node_up", out["series"])
            code, out = self.run_cli("query", "node_up", "--range", "600", "--label", "machine=halo-1")
            self.assertEqual(out["series"], "node_up")
            self.assertEqual(len(out["lines"]), 1)
            code, out = self.run_cli("thresholds", "--set", '{"rtx": {"amber_gib": 3}}')
            self.assertEqual(out["thresholds"]["rtx"], {"amber_gib": 3.0, "red_gib": 1.0})
            self.assertIn(("audit", "fleet/thresholds/set", "succeeded"), self.api.calls)
            with self.assertRaises(ValueError):
                self.run_cli("thresholds", "--set", '{"rtx": {"red_gib": 9}}')
            self.assertIn(("audit", "fleet/thresholds/set", "refused"), self.api.calls)

    def test_status_without_a_store_says_so(self):
        with mock.patch.object(cli_telemetry, "_existing", lambda api: None):
            code, out = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIsNone(out["store"])


if __name__ == "__main__":
    unittest.main()
