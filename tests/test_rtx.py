"""Phase 3a: the RTX prefill service preset, reservation, render, watchdog, helper and acceptance checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hyperloom import rtx
from hyperloom.config import ConfigError, parse
from hyperloom.render import render_router, validate_router
from tests.test_config import example

PRO = "GPU-00000000-0000-0000-0000-000000000001"
OTHER = "GPU-00000000-0000-0000-0000-000000000002"
KEY_A, KEY_B = "a" * 64, "b" * 64


def _utc(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rtx_example(reservation_s=900):
    obj = example()
    workstation = next(m for m in obj["machines"] if m["id"] == "workstation")
    workstation["devices"] = [{"id": "rtx-pro", "type": "nvidia", "uuid": PRO},
                              {"id": "rtx-4090", "type": "nvidia", "uuid": OTHER, "excluded": True}]
    obj["pc"]["cuda"] = {"pro_uuid": PRO, "reservation": None if reservation_s is None else {
        "owner": "operator", "approval": "coord 21:00 main ACK PC-BIG", "expires_at": _utc(reservation_s),
        "vram_cap_gib": 90, "host_gib": 72, "free_commit_floor_gib": 4}}
    obj["assets"]["workstation"] = {"ds41_model": {"path": "C:/hyperloom/assets/ds41.gguf", "bytes": 0, "sha256": "0" * 64}}
    obj["profiles"]["ds41-rtx"] = dict(obj["profiles"]["ds41-pair"], preset="ds41-pair-long",
                                       prefill_service={"preset": "rtx-prefill-ds41", "machine": "workstation",
                                                        "device": "rtx-pro"})
    obj["profiles"]["ds41-rtx"].pop("receipt")
    return obj


class PresetTests(unittest.TestCase):
    def test_preset_is_model_agnostic_data(self):
        data = rtx.service_preset("rtx-prefill-ds41")
        self.assertEqual(data["schema"], rtx.SCHEMA)
        self.assertIn("--exact", data["argv"])
        self.assertEqual(data["decode_presets"], ["ds41-pair"])
        self.assertEqual(data["profile"]["provenance"], "measured")
        self.assertEqual([e["tokens"] for e in data["fidelity"]["oracle"]], [512, 2048, 8192, 32768])
        self.assertNotIn("deepseek", Path(rtx.__file__).read_text(encoding="utf-8").lower())

    def test_unknown_or_malformed_preset_names(self):
        for name in ("ds41-pair", "rtx-prefill-../x", "rtx-prefill-missing"):
            with self.assertRaises(ConfigError):
                rtx.service_preset(name)

    def test_long_pair_variant_runs_as_the_pair_and_is_marked_unqualified(self):
        from hyperloom.config import family, preset
        long = preset("ds41-pair-long")
        self.assertEqual(family("ds41-pair-long"), "ds41-pair")
        self.assertFalse(long["qualified"])
        base = preset("ds41-pair")
        self.assertEqual(long["argv"][:len(base["argv"])], base["argv"])
        self.assertEqual(long["env"], base["env"])
        self.assertEqual(long["argv"][len(base["argv"]):], ["--context", "10240", "--slots", "1"])


class ConfigTests(unittest.TestCase):
    def test_valid_reservation_and_service(self):
        cluster = parse(rtx_example())
        self.assertIn("prefill_service", cluster.profiles["ds41-rtx"])

    def test_reservation_fields_and_time(self):
        for change in ({"expires_at": "tomorrow"}, {"vram_cap_gib": 0}, {"free_commit_floor_gib": 0}):
            obj = rtx_example()
            obj["pc"]["cuda"]["reservation"].update(change)
            with self.assertRaises(ConfigError):
                parse(obj)
        obj = rtx_example()
        del obj["pc"]["cuda"]["reservation"]["owner"]
        with self.assertRaisesRegex(ConfigError, "missing keys"):
            parse(obj)

    def test_service_must_be_local_windows_nvidia_with_uuid(self):
        for change in ({"machine": "halo-1"}, {"device": "rtx-4090"}, {"device": "gpu"}):
            obj = rtx_example()
            obj["profiles"]["ds41-rtx"]["prefill_service"].update(change)
            with self.assertRaises(ConfigError):
                parse(obj)
        obj = rtx_example()
        obj["machines"][0]["devices"][0]["uuid"] = "0"
        with self.assertRaisesRegex(ConfigError, "GPU-"):
            parse(obj)

    def test_service_refuses_other_decode_families(self):
        obj = rtx_example()
        obj["profiles"]["qwen-replicas"]["prefill_service"] = obj["profiles"]["ds41-rtx"]["prefill_service"]
        with self.assertRaisesRegex(ConfigError, "does not hand off"):
            parse(obj)

    def test_unqualified_positions_refused(self):
        obj = rtx_example()
        obj["profiles"]["ds41-rtx"]["prefill_service"]["positions"] = 65536
        with self.assertRaisesRegex(ConfigError, "positions"):
            parse(obj)


class RenderTests(unittest.TestCase):
    def render(self, obj=None, **kw):
        return rtx.render_service(parse(obj or rtx_example()), "ds41-rtx", api_key=KEY_A, handoff_api_key=KEY_B,
                                  free_physical_gib=kw.pop("free", 100.0), **kw)

    def test_lab_launch_line(self):
        spec = self.render()
        argv = spec.argv
        self.assertTrue(argv[0].endswith("\\cuda\\9211b07e13c6\\bin\\strix-cuda-ds41-prefill-service.exe"))
        # The lab window's line (model --port --positions 32768 --host-gib --host-commit-floor-gib 14
        # --host-pin-wait-s 90 --exact) plus loopback bind, the reservation's VRAM cap and the keys.
        self.assertEqual(argv[1:], ["C:/hyperloom/assets/ds41.gguf", "--host", "127.0.0.1", "--port", "8095",
                                    "--positions", "32768", "--vram-cap-gib", "90", "--host-gib", "72",
                                    "--host-commit-floor-gib", "14", "--host-pin-wait-s", "90", "--exact",
                                    "--api-key", KEY_A, "--handoff-api-key", KEY_B])
        self.assertEqual(spec.env["CUDA_DEVICE_ORDER"], "PCI_BUS_ID")
        self.assertEqual(spec.env["CUDA_VISIBLE_DEVICES"], PRO)
        self.assertEqual(spec.consistency, {"service_positions": 32768, "decode_context": 10240,
                                            "reserve_tokens": 1024, "longest_offload": 9215})

    def test_keys_redacted_everywhere_but_the_child(self):
        spec = self.render()
        text = json.dumps(spec.redacted)
        self.assertNotIn(KEY_A, text)
        self.assertNotIn(KEY_B, text)
        self.assertEqual(spec.redacted.count("<redacted>"), 2)

    def test_host_tier_is_bounded_by_free_memory_and_the_reservation(self):
        self.assertEqual(self.render(free=60.0).argv[self.render(free=60.0).argv.index("--host-gib") + 1], "40")
        self.assertEqual(self.render(free=10.0).argv[self.render(free=10.0).argv.index("--host-gib") + 1], "0")
        obj = rtx_example()
        obj["pc"]["cuda"]["reservation"]["host_gib"] = 200
        argv = self.render(obj, free=200.0).argv
        self.assertEqual(argv[argv.index("--host-gib") + 1], "80")

    def test_lab_mode_needs_a_current_reservation(self):
        with self.assertRaisesRegex(ConfigError, "reservation"):
            self.render(rtx_example(reservation_s=None))
        with self.assertRaisesRegex(ConfigError, "expired"):
            self.render(rtx_example(reservation_s=30))

    def test_production_context_is_too_short_for_offload(self):
        obj = rtx_example()
        obj["profiles"]["ds41-rtx"]["preset"] = "ds41-pair"
        with self.assertRaisesRegex(ConfigError, "at least 8192"):
            self.render(obj)

    def test_one_decode_replica_per_handoff_key(self):
        data = rtx.service_preset("rtx-prefill-ds41")
        self.assertEqual(data["decode_profiles"]["ds41-pair"]["max_decode_replicas"], 1)

    def test_cuda_env_cannot_be_inherited(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "1", "PYTHONPATH": "x"}):
            spec = self.render()
        self.assertEqual(spec.env["CUDA_VISIBLE_DEVICES"], PRO)
        self.assertNotIn("PYTHONPATH", spec.env)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyperloom rtx ")
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / "service.key"
        self.key.write_text("synthetic-test-token\n", encoding="ascii")
        self.obj = rtx_example()
        for mid in ("halo-1", "halo-2", "workstation"):
            self.obj["router"]["replica_api_key_files"][mid] = str(self.key)

    def test_auto_policy_with_the_measured_service_and_pair_curves(self):
        rendered = render_router(parse(self.obj), "ds41-rtx")
        prefill = rendered["prefill"]
        self.assertEqual(prefill["policy"], "auto")
        self.assertEqual(prefill["services"][0]["address"], "127.0.0.1")
        self.assertEqual(prefill["services"][0]["port"], 8095)
        self.assertEqual(prefill["node_profile"]["prefill_curve"][3], [2048, 4.192])  # not the cluster's Qwen curve
        self.assertEqual(len(rendered["replicas"]), 1)
        parsed = validate_router(rendered)
        self.assertEqual(parsed.prefill.services[0].api_key, "synthetic-test-token")
        self.assertEqual(parsed.prefill.service_profiles[0].model, "deepseek41")
        self.assertEqual(parsed.prefill.handoff_path, "/admin/import-state")

    def test_router_must_be_local(self):
        self.obj["router"]["machine"] = "halo-1"
        self.obj["router"]["api_key_file"] = "/opt/hyperloom/secrets/router.key"
        with self.assertRaisesRegex(ConfigError, "loopback-only"):
            render_router(parse(self.obj), "ds41-rtx")

    def test_service_key_file_required(self):
        del self.obj["router"]["replica_api_key_files"]["workstation"]
        with self.assertRaisesRegex(ConfigError, "prefill service key"):
            render_router(parse(self.obj), "ds41-rtx")


def _spec(**kw):
    base = rtx.render_service(parse(rtx_example()), "ds41-rtx", api_key=KEY_A, handoff_api_key=KEY_B,
                              free_physical_gib=100.0)
    return rtx.ServiceSpec(**dict(base.__dict__, **kw))


class WatchdogTests(unittest.TestCase):
    def watchdog(self, memory=None, apps=(), used=1000, now=None, spec=None):
        lines = []
        dog = rtx.Watchdog(spec or _spec(), 42, lambda: None, lines.append,
                           memory=lambda: memory or {"free_physical_gib": 50.0, "free_commit_gib": 60.0},
                           apps=lambda: list(apps), used=lambda _: used, clock=lambda: now or time.time())
        dog.baseline_mib = 600
        return dog, lines

    def test_healthy_sample_logs_and_keeps_running(self):
        dog, lines = self.watchdog(apps=[(PRO, 42, None)], used=60000)
        self.assertIsNone(dog.check())
        self.assertEqual(lines[0]["vram_mib"], 59400)  # WDDM: device use above the baseline

    def test_commit_floor(self):
        dog, _ = self.watchdog(memory={"free_physical_gib": 1.0, "free_commit_gib": 3.0})
        self.assertIn("free commit", dog.check())

    def test_visible_on_another_gpu(self):
        dog, _ = self.watchdog(apps=[(PRO, 42, 100), (OTHER, 42, 100)])
        self.assertIn(OTHER, dog.check())

    def test_vram_cap_per_process(self):
        dog, _ = self.watchdog(apps=[(PRO, 42, 91 * 1024)])
        self.assertIn("cap", dog.check())

    def test_deadline(self):
        dog, _ = self.watchdog(spec=_spec(deadline=time.time() - 1))
        self.assertEqual(dog.check(), "reservation deadline")

    def test_kill_runs_once_from_the_thread(self):
        killed = threading.Event()
        dog = rtx.Watchdog(_spec(), 42, killed.set, lambda _: None,
                           memory=lambda: {"free_physical_gib": 1.0, "free_commit_gib": 1.0},
                           apps=lambda: [], used=lambda _: 0, interval=0.01).start()
        self.assertTrue(killed.wait(2))
        dog.stop()
        self.assertIn("free commit", dog.reason)


class FakeChild:
    def __init__(self, argv, env, directory, name, port=None):
        self.argv, self.env, self.directory = argv, env, Path(directory)
        self.process = mock.Mock(pid=4242, returncode=None)
        self.stopped = False

    def start(self):
        (self.directory / "service.stdout.log").write_bytes(b'{"event":"loaded"}\n{"event":"READY","http":"127.0.0.1:8095"}\n')
        return self

    def alive(self):
        return not self.stopped

    def stop(self, grace_s=5):
        self.stopped = True


class FakeDog:
    reason = None

    def __init__(self, *args, **kw):
        pass

    def start(self):
        return self

    def stop(self):
        pass


class HelperTests(unittest.TestCase):
    def test_ready_then_stop_and_keys_leave_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec_path = directory / "spec.json"
            spec_path.write_text(json.dumps({"spec": dict(_spec().__dict__)}), encoding="utf-8")
            children = []

            def factory(*args, **kw):
                children.append(FakeChild(*args, **kw))
                return children[-1]

            thread = threading.Thread(target=rtx.service_run, args=(spec_path,), kwargs=dict(
                child_factory=factory, watchdog_factory=FakeDog, verify=lambda u: {"uuid": u},
                health=lambda port: {"status": "ok"}))
            thread.start()
            end = time.monotonic() + 5
            while time.monotonic() < end and not (directory / "state.json").exists() or \
                    json.loads((directory / "state.json").read_text())["state"] != "ready":
                time.sleep(0.05)
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["state"], "ready")
            self.assertFalse(spec_path.exists())
            self.assertEqual(children[0].env["CUDA_VISIBLE_DEVICES"], PRO)
            (directory / "stop").touch()
            thread.join(5)
            final = json.loads((directory / "state.json").read_text())
            self.assertEqual(final["state"], "stopped")
            self.assertTrue(children[0].stopped)
            for path in directory.iterdir():
                self.assertNotIn(KEY_A, path.read_text(encoding="utf-8", errors="replace"), path.name)

    def test_device_check_failure_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps({"spec": dict(_spec().__dict__)}), encoding="utf-8")
            factory = mock.Mock()

            def refuse(uuid):
                raise rtx.ServiceError("not a PRO")

            rtx.service_run(spec_path, child_factory=factory, watchdog_factory=FakeDog, verify=refuse)
            factory.assert_not_called()
            self.assertIn("not a PRO", json.loads((Path(tmp) / "state.json").read_text())["reason"])


class DeviceTests(unittest.TestCase):
    def smi(self, stdout, code=0):
        return lambda *a, **kw: mock.Mock(returncode=code, stdout=stdout, stderr="")

    def test_verify_pro(self):
        info = rtx.verify_pro(PRO, self.smi(f"{PRO}, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 00000000:01:00.0, 97887\n"))
        self.assertEqual(info["memory_total_mib"], 97887)
        with self.assertRaises(rtx.ServiceError):
            rtx.verify_pro(PRO, self.smi(f"{PRO}, NVIDIA GeForce RTX 4090, 00000000:02:00.0, 24564\n"))
        with self.assertRaises(rtx.ServiceError):
            rtx.verify_pro(PRO, self.smi("", code=6))

    def test_compute_apps_handles_wddm_na(self):
        rows = rtx.compute_apps(self.smi(f"{PRO}, 42, [N/A]\n{OTHER}, 7, 300\n"))
        self.assertEqual(rows, [(PRO, 42, None), (OTHER, 7, 300)])

    def test_smi_never_opens_a_window(self):
        calls = []
        rtx.compute_apps(lambda *a, **kw: calls.append(kw) or mock.Mock(returncode=0, stdout="", stderr=""))
        if os.name == "nt":
            self.assertTrue(calls[0]["creationflags"] & 0x08000000)


class AcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = parse(rtx_example())
        state = {"profile": "ds41-rtx", "router": {"endpoint": {"address": "127.0.0.1", "port": 18600}},
                 "prefill_service": {"port": 8095, "pid": 4242, "device": PRO}}
        patcher = mock.patch.object(rtx, "_running", lambda profile: (state, state["prefill_service"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        keys = mock.patch.object(rtx, "_keys", lambda profile, suffix: "k" * 64)
        keys.start()
        self.addCleanup(keys.stop)

    def test_prompt_ids_are_checked_against_the_oracle(self):
        directory = Path(self.tmp.name)
        (directory / "p.txt").write_text("[1, 2, 3]\n[4]", encoding="utf-8")
        ids = [1, 2, 3, 4, 1, 2]
        entry = {"source": "p.txt", "tokens": 6, "ids_sha256": hashlib.sha256(b"1,2,3,4,1,2").hexdigest()}
        self.assertEqual(rtx.prompt_ids(directory, entry), ids)
        with self.assertRaisesRegex(rtx.ServiceError, "differ"):
            rtx.prompt_ids(directory, dict(entry, tokens=5))

    def test_fidelity_gate_pass_and_fail(self):
        data = rtx.service_preset("rtx-prefill-ds41")
        by_len = {e["tokens"]: e for e in data["fidelity"]["oracle"]}

        def request(address, port, method, path, body, key):
            e = by_len[len(body["tokens"])]
            return 200, {}, {"sha256": e["state_sha256"], "next_token": e["next_token"], "logits_fnv": e["logits_fnv"],
                             "state_bytes": e["state_bytes"], "fast": {"dense": False, "moe": False, "attention": False}}

        with mock.patch.object(rtx, "prompt_ids", lambda d, e: [0] * e["tokens"]):
            result = rtx.fidelity_gate(self.cluster, "ds41-rtx", self.tmp.name, request=request,
                                       apps=lambda: [(PRO, 4242, None)])
            self.assertTrue(result["pass"], result)
            self.assertIsNone(result["quality_gate"])
            bad = rtx.fidelity_gate(self.cluster, "ds41-rtx", self.tmp.name, request=request,
                                    apps=lambda: [(PRO, 4242, None), (OTHER, 4242, None)])
            self.assertFalse(bad["pass"])

            def fast(*args):
                status, headers, reply = request(*args)
                return status, headers, dict(reply, fast={"dense": True})
            self.assertFalse(rtx.fidelity_gate(self.cluster, "ds41-rtx", self.tmp.name, request=fast,
                                               apps=lambda: [])["pass"])

    def test_placement_long_offloads_short_stays_local(self):
        counts = {"local": 0, "offload": 0, "fallback": 0, "cache": 0}
        served = {"n": 0}

        def request(address, port, method, path, body, key):
            if path == "/tokenize":
                return 200, {}, {"tokens": [0] * len(body["prompt"].split(" "))}
            if path == "/api/status":
                return 200, {}, {"prefill": {"counts": dict(counts)}}
            words = len(body["prompt"].split(" "))
            place = "offload" if words > 2048 else "local"
            counts[place] += 1
            served["n"] += place == "offload"
            return 200, {"x-tensormeld-prefill": place + ":x"}, {"usage": {"completion_tokens": 16}}

        result = rtx.placement_check(self.cluster, "ds41-rtx", request=request,
                                     health=lambda port: {"served": served["n"]})
        self.assertTrue(result["pass"], result)
        self.assertEqual([r["placement"] for r in result["rows"]], ["offload:x", "local:x"])

        def fallback(address, port, method, path, body, key):
            status, headers, reply = request(address, port, method, path, body, key)
            if path == "/v1/completions" and headers["x-tensormeld-prefill"].startswith("offload"):
                counts["offload"] -= 1
                counts["fallback"] += 1
                served["n"] -= 1
                return status, {"x-tensormeld-prefill": "fallback:http502"}, reply
            return status, headers, reply
        self.assertFalse(rtx.placement_check(self.cluster, "ds41-rtx", request=fallback,
                                             health=lambda port: {"served": served["n"]})["pass"])


if __name__ == "__main__":
    unittest.main()


from hyperloom import orchestrate as orch, secrets  # noqa: E402
from hyperloom.config import Machine  # noqa: E402
from tests import test_orchestrate  # noqa: E402


class UpDownTests(unittest.TestCase):
    """The service starts after the pair is READY and before the router; down stops router, service, then engines."""
    processes = test_orchestrate.OrchestrationTests.processes
    local = test_orchestrate.OrchestrationTests.local

    def rtx_cluster(self):
        cluster_fixture = test_orchestrate.cluster_fixture
        cluster = cluster_fixture(pair=True)
        cluster.machines["workstation"] = Machine("workstation", "windows", "local", "127.0.0.1", None, 22, "C:/hyperloom",
                                                  True, [{"id": "rtx-pro", "type": "nvidia", "uuid": PRO}])
        settings = cluster.profiles.pop("ds41-pair")
        cluster.profiles["ds41-rtx"] = dict(settings, preset="ds41-pair-long", release_id=test_orchestrate.RELEASE, prefill_service={
            "preset": "rtx-prefill-ds41", "machine": "workstation", "device": "rtx-pro"})
        return cluster

    def setUp(self):
        test_orchestrate.OrchestrationTests.setUp(self)
        spec = _spec()
        for target, effect in (
                ("render_service", lambda cluster, profile, **kw: (self.calls.append(("local", "service-render", kw)), spec)[1]),
                ("start_service", lambda spec, **kw: (self.calls.append(("local", "service-start", {})),
                                                      {"machine": "workstation", "directory": "d", "port": 8095,
                                                       "pid": 1, "device": PRO, "argv": spec.redacted})[1]),
                ("stop_service", lambda record: self.calls.append(("local", "service-stop", {}))),
                ("memory_status", lambda: {"free_physical_gib": 100.0, "free_commit_gib": 100.0})):
            patcher = mock.patch.object(orch.rtx, target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_order_keys_and_router_services(self):
        state = orch.up(self.rtx_cluster(), "ds41-rtx", devices=["workstation:rtx-pro"])
        order = [verb for _, verb, _ in self.calls if verb in ("lease", "service-start", "router-start")]
        self.assertEqual(order, ["lease", "service-start", "router-start"])
        render_kw = next(p for _, verb, p in self.calls if verb == "service-render")
        self.assertEqual(render_kw["handoff_api_key"], secrets.get(orch._secret_name("ds41-rtx", "halo-1")))
        self.assertEqual(render_kw["api_key"], secrets.get(orch._secret_name("ds41-rtx", "prefill")))
        rendered = next(p for _, verb, p in self.calls if verb == "router-start")["rendered"]
        self.assertEqual(rendered["prefill"]["policy"], "auto")
        self.assertEqual(rendered["prefill"]["services"][0]["port"], 8095)
        self.assertEqual(state["prefill_service"]["port"], 8095)
        self.assertNotIn(render_kw["api_key"], json.dumps(state))
        orch.down("ds41-rtx")
        order = [verb for _, verb, _ in self.calls if verb in ("router-stop", "service-stop", "stop")]
        self.assertEqual(order[:3], ["router-stop", "service-stop", "stop"])

    def test_service_requires_the_local_router(self):
        cluster = self.rtx_cluster()
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            orch.up(cluster, "ds41-rtx", router="halo-1")
        self.assertFalse([c for c in self.calls if c[1] == "lease"])

    def test_service_failure_rolls_back_the_pair(self):
        with mock.patch.object(orch.rtx, "start_service", side_effect=rtx.ServiceError("no READY")):
            with self.assertRaisesRegex(rtx.ServiceError, "no READY"):
                orch.up(self.rtx_cluster(), "ds41-rtx")
        self.assertTrue(all(row["state"] == "stopped" for row in self.leases.values()))
        self.assertFalse([c for c in self.calls if c[1] == "router-start"])


class RealChildTests(unittest.TestCase):
    """The helper with the real LocalChild (Job Object on Windows) around a stand-in service process."""

    def test_job_owned_child_ready_health_and_stop(self):
        import socket
        import sys
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        fake = Path(__file__).with_name("fixtures") / "prefill" / "fake_service.py"
        spec = _spec(port=port, argv=[sys.executable, str(fake), "--port", str(port), "--api-key", KEY_A],
                     ready_timeout_s=30)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec_path = directory / "spec.json"
            spec_path.write_text(json.dumps({"spec": dict(spec.__dict__)}), encoding="utf-8")
            thread = threading.Thread(target=rtx.service_run, args=(spec_path,),
                                      kwargs=dict(watchdog_factory=FakeDog, verify=lambda u: {"uuid": u}))
            thread.start()
            end = time.monotonic() + 30
            state = {}
            while time.monotonic() < end:
                if (directory / "state.json").exists():
                    state = json.loads((directory / "state.json").read_text())
                    if state["state"] in ("ready", "stopped"):
                        break
                time.sleep(0.1)
            self.assertEqual(state.get("state"), "ready", state)
            self.assertEqual(rtx._health(port)["status"], "ok")
            status, _, reply = rtx._request("127.0.0.1", port, "POST", "/v1/prefill", {"tokens": [1, 2]}, KEY_A)
            self.assertEqual((status, reply["tokens"]), (200, 2))
            loaded = json.loads((directory / "service.stdout.log").read_text().splitlines()[0])
            self.assertEqual(loaded["env_device"], PRO)
            (directory / "stop").touch()
            thread.join(30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(json.loads((directory / "state.json").read_text())["state"], "stopped")
            with self.assertRaises(OSError):
                rtx._health(port)
