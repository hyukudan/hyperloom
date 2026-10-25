"""`hyperloom placement` (cliext/planner.py): the planner's hardware description built from saved facts, the three
scenarios, the bounded planner child and the shaping of its answer. The facts are saved records (no machine is
contacted); one test runs the pinned TensorMeld planner for real."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from hyperloom import cli
from hyperloom.cliext import planner
from tests.recipe_fixtures import PRO, facts, isolated

FIXTURE = Path(__file__).parent / "fixtures" / "planner" / "plan-hetero-qwen.json"
OTHER = "GPU-00000000-0000-0000-0000-000000000002"


def parse_hardware(hardware):
    """TensorMeld's own hardware parser (the pinned copy)."""
    source = str(planner.tensormeld_root() / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from tensormeld.hetero_hardware import parse_hardware as parse
    return parse(hardware)


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.env = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.cluster = cli._cluster(self.env.config)
        self.template = planner._load_json(planner.template_file())

    def build(self, record, model="deepseek-v4.1-flash"):
        return planner.build_hardware(self.cluster, record, model, self.template)

    def test_devices_from_saved_facts_with_borrowed_rates_disclosed(self):
        hardware, sources, excluded, notes = self.build(facts("deepseek-v4.1-flash", "ds41-rtx"))
        parsed = parse_hardware(hardware)                      # TensorMeld accepts it as it is
        self.assertEqual(sorted(d.id for d in parsed.devices),
                         ["halo-1.gpu", "halo-2.gpu", "workstation.host", "workstation.rtx-pro"])
        by_id = {d["id"]: d for d in hardware["devices"]}
        for device in hardware["devices"]:
            self.assertEqual(device["provenance"], "estimate")      # borrowed rates are never "measured" here
            for key in ("decode_ops_per_s", "step_overhead_us", "crossing_overhead_us", "mem_bytes_per_s"):
                self.assertIn(key, device)
        self.assertEqual(by_id["halo-1.gpu"]["memory_bytes"], int(120.0 * planner.GIB))   # from the facts
        self.assertEqual(by_id["workstation.rtx-pro"]["availability"], "preemptible")
        host = by_id["workstation.host"]
        usable = host["memory_bytes"] - host["reserved_bytes"]
        allowance = next(d["memory_bytes"] - d["reserved_bytes"] for d in self.template["devices"] if d["kind"] == "cpu")
        self.assertEqual(usable, min(allowance, int(100.0 * planner.GIB)))
        rates = next(s for s in sources if s.get("device") == "halo-1.gpu")["rates"]
        self.assertIn("provenance there: measured", rates)
        self.assertIn("estimates here", rates)
        self.assertIn("not verified on this device", rates)
        disk = next(s for s in sources if s.get("node") == "halo-1")
        self.assertEqual(disk["disk_read"], "measured disk read from the saved facts")
        self.assertIn("not known", disk["disk_free"])
        self.assertEqual(excluded, [])
        links = {link["id"]: link for link in hardware["links"]}
        self.assertEqual(links["eth-workstation-halo-1"]["bytes_per_s"], int(2.5e9 / 8))
        self.assertIn("usb4-halo-1-halo-2", links)
        self.assertEqual(hardware["groups"][0]["devices"], ["halo-1.gpu", "halo-2.gpu"])
        self.assertNotIn("*", hardware["groups"][0]["supports"])

    def test_no_evidence_no_device_and_availability_fails_closed(self):
        record = facts("deepseek-v4.1-flash", "ds41-rtx")
        record["inventory"]["cuda"].append({"uuid": OTHER, "name": "NVIDIA GeForce RTX 4090", "memory_gib": 23.6})
        record["facts"]["devices"][OTHER] = {"name": "NVIDIA GeForce RTX 4090", "memory_gib": 23.6, "host_ram_gib": 127.7}
        record["raw"]["devices"][OTHER] = {"available": False, "why": "not the profile's prefill device"}
        record["raw"]["devices"]["halo-2"] = {"available": False, "why": "node unreachable"}
        del record["raw"]["devices"]["halo-1"]
        record["raw"]["usb4"]["halo-2"] = {"up": False}
        hardware, _, excluded, _ = self.build(record)
        by_id = {d["id"]: d for d in hardware["devices"]}
        self.assertNotIn("workstation.rtx-4090", by_id)
        self.assertIn("no rate evidence", next(x for x in excluded if x.get("device") == "workstation.rtx-4090")["why"])
        self.assertEqual(by_id["halo-1.gpu"]["availability"], "unavailable")     # missing availability: closed
        self.assertEqual(by_id["halo-2.gpu"]["availability"], "unavailable")
        self.assertNotIn("usb4-halo-1-halo-2", {link["id"] for link in hardware["links"]})
        self.assertEqual(hardware["groups"], [])
        self.assertTrue(any(x.get("link") == "usb4-halo-1-halo-2" for x in excluded))

    def test_a_busy_rtx_stays_unavailable_in_every_scenario(self):
        record = facts("deepseek-v4.1-flash", "ds41-rtx")
        record["raw"]["devices"][PRO] = {"available": False, "why": "RTX PRO busy: another job"}
        hardware, *_ = self.build(record)
        self.assertIsNone(planner.scenario_hardware(hardware, "rtx-prefill", "workstation"))
        self.assertIsNone(planner.scenario_hardware(hardware, "all", "workstation"))

    def test_scenarios_change_only_what_they_name(self):
        hardware, *_ = self.build(facts("deepseek-v4.1-flash", "ds41-rtx"))
        before = deepcopy(hardware)
        nodes = {d["id"]: d["availability"] for d in planner.scenario_hardware(hardware, "nodes", "workstation")["devices"]}
        self.assertEqual(nodes, {"workstation.rtx-pro": "unavailable", "halo-1.gpu": "available",
                                 "halo-2.gpu": "available", "workstation.host": "unavailable"})
        lent = {d["id"]: d["availability"] for d in planner.scenario_hardware(hardware, "rtx-prefill", "workstation")["devices"]}
        self.assertEqual(lent["workstation.rtx-pro"], "prefill-only")
        self.assertEqual(lent["workstation.host"], "available")
        self.assertEqual(hardware, before)                      # the base description is never modified

    def test_missing_host_total_leaves_the_host_device_out(self):
        record = facts("qwen3.8-flash-next", "qwen-replicas")
        record["inventory"]["cuda"] = []
        del record["facts"]["devices"][PRO]
        hardware, _, excluded, _ = self.build(record, "qwen3.8-flash-next")
        self.assertEqual(sorted(d["id"] for d in hardware["devices"]), ["halo-1.gpu", "halo-2.gpu"])
        self.assertIn("host RAM total", next(x for x in excluded if x.get("device") == "workstation.host")["why"])


class ChildTests(unittest.TestCase):
    def test_deadline_and_output_cap(self):
        code, _, _, why = planner.bounded_child([sys.executable, "-c", "import time; time.sleep(30)"], env=None,
                                                cwd=None, timeout_s=1)
        self.assertEqual((code, why), (None, "deadline"))
        with mock.patch.object(planner, "STDOUT_CAP", 1000):
            code, out, _, why = planner.bounded_child([sys.executable, "-c", "print('x' * 100000)"], env=None,
                                                      cwd=None, timeout_s=30)
        self.assertEqual((code, why, len(out)), (None, "output overflow", 1000))

    def test_any_plan_answer_is_a_result_whatever_the_exit_code(self):
        plan = json.dumps({"schema": "tensormeld/hetero-plan-v1", "status": "NO_FEASIBLE_PLACEMENT", "best": None,
                           "candidates": []}).encode()
        request = {"prompt": 8, "output": 8, "slots": 1, "objective": "latency", "top_k": 1}
        with mock.patch.object(planner, "bounded_child", return_value=(2, plan, b"", None)):
            result, error, line = planner.run_planner(Path("m.parts.json"), Path("hw.json"), request, timeout_s=5,
                                                      workdir=".")
        self.assertEqual((result["status"], error), ("NO_FEASIBLE_PLACEMENT", None))
        self.assertIn("--context 16", line)
        with mock.patch.object(planner, "bounded_child", return_value=(1, b"", b"error: bad input", None)):
            result, error, _ = planner.run_planner(Path("m.parts.json"), Path("hw.json"), request, timeout_s=5,
                                                   workdir=".")
        self.assertEqual((result, error), (None, "bad input"))


class ShapeTests(unittest.TestCase):
    def test_shape_keeps_sets_replicas_and_co_residency(self):
        shaped = planner.shape_plan(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(shaped["basis"], "predicted (planner)")
        self.assertFalse(shaped["qualified"])
        replicas = next(c for c in shaped["candidates"] if c["mode"] == "replicas")
        self.assertIn("one replica", replicas["replicas_label"])
        self.assertTrue(replicas["sets"])
        self.assertEqual(replicas["placement_label"], "weight bytes per device set")
        self.assertIn("co_resident", shaped["fallback"])


class PlacementRunTests(unittest.TestCase):
    def test_three_scenarios_with_the_pinned_planner(self):
        with isolated() as env:
            env.save_facts("ds-now", "deepseek-v4.1-flash", "ds41-rtx")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), mock.patch("sys.stderr", err):
                code = cli.main(["placement", "deepseek-v4.1-flash", "--top-k", "2", "--config", str(env.config)])
            self.assertEqual(code, 0, err.getvalue())
            result = json.loads(out.getvalue())
        self.assertEqual(result["advisory"], "advisory, not qualified")
        self.assertEqual(result["facts"]["name"], "ds-now")
        self.assertEqual(result["request"]["context"], 8192 + 256)
        scenarios = {s["id"]: s for s in result["scenarios"]}
        self.assertEqual(list(scenarios), ["nodes", "rtx-prefill", "all"])
        for scenario in scenarios.values():
            self.assertEqual(scenario["status"], "OK", scenario)
            self.assertFalse(scenario["qualified"])
            self.assertLessEqual(len(scenario["candidates"]), 2)
        self.assertTrue(all(not d.startswith("workstation") for d in scenarios["nodes"]["best"]["devices"]))
        self.assertEqual(scenarios["rtx-prefill"]["availability"]["workstation.rtx-pro"], "prefill-only")
        self.assertIn("--context 8448", scenarios["all"]["cli"])

    def test_refusals(self):
        with isolated() as env:
            for argv, message in ((["placement", "no-model"], "unknown model"),
                                  (["placement", "minimax-m3.1-flash"], "no model parts"),
                                  (["placement", "qwen3.8-flash-next"], "no saved hardware facts"),
                                  (["placement", "qwen3.8-flash-next", "--slots", "99"], "--slots must be")):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", err):
                    code = cli.main([*argv, "--config", str(env.config)])
                self.assertEqual(code, 1, argv)
                self.assertIn(message, err.getvalue())


if __name__ == "__main__":
    unittest.main()
