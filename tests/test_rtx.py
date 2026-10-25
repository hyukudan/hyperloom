"""Phase 3a: the RTX prefill service preset, reservation, render, watchdog, helper and acceptance checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from unittest import mock

from hyperloom import rtx
from hyperloom.config import ConfigError, parse
from hyperloom.render import render_router, validate_router
from tests.test_config import example

DS41_SIZES = json.loads((Path(__file__).parent / "fixtures" / "vramplan" / "ds41-sizes.json").read_text())
_PATCHES = []


def setUpModule():
    # The placement plan reads the model header; the example's asset path is not a real file.
    from hyperloom.placement import ds41
    _PATCHES.append(mock.patch.object(ds41, "sizes", return_value=DS41_SIZES))
    _PATCHES[-1].start()


def tearDownModule():
    for patch in _PATCHES:
        patch.stop()


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

    def test_long_pair_variant_runs_as_the_pair(self):
        from hyperloom.config import family, preset
        long = preset("ds41-pair-long")
        self.assertEqual(family("ds41-pair-long"), "ds41-pair")
        self.assertTrue(long["qualified"])
        base = preset("ds41-pair")
        self.assertEqual(long["argv"][:len(base["argv"])], base["argv"])
        self.assertEqual(long["env"], base["env"])
        self.assertEqual(long["argv"][len(base["argv"]):], ["--context", "33024", "--slots", "2"])


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

    def test_reservation_gate_is_an_absolute_command(self):
        obj = rtx_example()
        obj["pc"]["cuda"]["reservation"]["gate"] = ["C:/tools/gate.exe", "--device", "pro"]
        self.assertEqual(rtx.render_service(parse(obj), "ds41-rtx", api_key=KEY_A, handoff_api_key=KEY_B,
                                            free_physical_gib=100.0).gate, ["C:/tools/gate.exe", "--device", "pro"])
        for bad in ([], "C:/tools/gate.exe", ["gate.exe"], ["C:/g.exe", ""], ["C:/g.exe", 1], ["C:/g.exe", "a\0b"]):
            obj = rtx_example()
            obj["pc"]["cuda"]["reservation"]["gate"] = bad
            with self.subTest(gate=bad), self.assertRaises(ConfigError):
                parse(obj)

    def test_gate_result(self):
        def run(code, out=b"", err=b""):
            return lambda *a, **k: subprocess.CompletedProcess(a, code, out, err)
        self.assertIsNone(rtx.run_gate(["C:/g.exe"], run(0, b"open")))
        self.assertEqual(rtx.run_gate(["C:/g.exe"], run(3, b"PC-BIG open: someone\nmore")),
                         "gate refused (rc 3): PC-BIG open: someone")
        self.assertIn("no output", rtx.run_gate(["C:/g.exe"], run(1)))

        def raises(exc):
            def run(*a, **k):
                raise exc
            return run
        for exc in (OSError("missing"), subprocess.TimeoutExpired("g", 60)):
            self.assertIn("did not run", rtx.run_gate(["C:/g.exe"], raises(exc)))

    def test_gate_runs_a_real_command(self):
        import sys
        self.assertIsNone(rtx.run_gate([sys.executable, "-c", "print('open')"]))
        self.assertEqual(rtx.run_gate([sys.executable, "-c", "print('PC-BIG open: x'); raise SystemExit(3)"]),
                         "gate refused (rc 3): PC-BIG open: x")

    def test_reservation_window_opens_at_start(self):
        obj = rtx_example()
        reservation = obj["pc"]["cuda"]["reservation"]
        del reservation["expires_at"]
        reservation["window_s"] = 1200
        parse(obj)
        for change in ({"window_s": 1201}, {"window_s": 30}, {"expires_at": "2026-10-24T10:00:00Z"}):
            bad = rtx_example()
            del bad["pc"]["cuda"]["reservation"]["expires_at"]
            bad["pc"]["cuda"]["reservation"].update({"window_s": 1200, **change})
            with self.assertRaises(ConfigError):
                parse(bad)
        del reservation["window_s"]
        with self.assertRaisesRegex(ConfigError, "exactly one"):
            parse(obj)
        reservation.update(window_s=3600, extended_approval="coord main ACK 60 min")
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

    def test_vram_cap_replans_the_placement(self):
        self.assertIsNone(self.render().placement)
        hi, lo = self.render(vram_cap_gib=90), self.render(vram_cap_gib=48)
        value = lambda spec, flag: spec.argv[spec.argv.index(flag) + 1]
        self.assertEqual((hi.vram_cap_gib, lo.vram_cap_gib), (90, 48))  # the watchdog's hard stop
        self.assertEqual((value(hi, "--vram-cap-gib"), value(lo, "--vram-cap-gib")), ("86.3", "45"))
        self.assertLess(lo.placement["resident_gib"], hi.placement["resident_gib"])
        self.assertLessEqual(lo.placement["predicted_peak_gib"], 48)
        self.assertIn("predicted peak", lo.placement["explain"])
        self.assertEqual(rtx.prepare_service(lo)["placement"]["vram_cap_gib"], 48)

    def test_vram_cap_never_raises_the_reservation(self):
        with self.assertRaises(ConfigError):
            self.render(vram_cap_gib=95)
        with self.assertRaises(ConfigError):
            self.render(vram_cap_gib=0)

    def test_vram_cap_never_raises_the_default_without_a_reservation(self):
        self.assertEqual(rtx.effective_cap(None, None), 96)
        self.assertEqual(rtx.effective_cap(None, 48), 48)
        with self.assertRaises(ConfigError):
            rtx.effective_cap(None, 200)

    def test_an_unplannable_model_only_blocks_a_capped_start(self):
        from hyperloom.placement import ds41
        with mock.patch.object(ds41, "sizes", side_effect=OSError("no header")):
            self.assertIsNone(self.render().placement)
            with self.assertRaises(ConfigError):
                self.render(vram_cap_gib=48)

    def test_a_cap_too_small_for_the_engine_is_refused_before_launch(self):
        with self.assertRaises(ConfigError) as ctx:
            self.render(vram_cap_gib=12)
        self.assertIn("no placement", str(ctx.exception))

    def test_placement_token_without_a_placement_block(self):
        with self.assertRaises(ConfigError):
            rtx.expand_placement({"argv": ["{placement}"]}, None)

    def test_lab_launch_line(self):
        spec = self.render()
        argv = spec.argv
        self.assertTrue(argv[0].endswith("\\cuda\\9211b07e13c6\\bin\\strix-cuda-ds41-prefill-service.exe"))
        # The lab window's line (model --port --positions 32768 --host-gib --host-commit-floor-gib 14
        # --host-pin-wait-s 90 --exact) plus loopback bind, the reservation's VRAM cap and the keys: unchanged when
        # no --vram-cap-gib is given (nothing is planned).
        self.assertEqual(argv[1:], ["C:/hyperloom/assets/ds41.gguf", "--host", "127.0.0.1", "--port", "8095",
                                    "--positions", "32768", "--vram-cap-gib", "90", "--host-gib", "72",
                                    "--host-commit-floor-gib", "14", "--host-pin-wait-s", "90", "--exact",
                                    "--api-key", KEY_A, "--handoff-api-key", KEY_B])
        self.assertEqual(spec.env["CUDA_DEVICE_ORDER"], "PCI_BUS_ID")
        self.assertEqual(spec.env["CUDA_VISIBLE_DEVICES"], PRO)
        self.assertEqual(spec.consistency, {"service_positions": 32768, "decode_context": 33024,
                                            "reserve_tokens": 1024, "longest_offload": 31999,
                                            "note": "prompts of 32000..32768 tokens fail the import and fall back to local prefill"})
        self.assertIsNone(spec.binary_sha256)  # not built yet: the helper refuses to launch it

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
        cluster = parse(rtx_example())
        cluster.profiles["ds41-rtx"]["nodes"] = ["halo-1", "halo-2"]
        with self.assertRaisesRegex(ConfigError, "at most 1 decode replica"):
            rtx.render_service(cluster, "ds41-rtx", api_key=KEY_A, handoff_api_key=KEY_B, free_physical_gib=100.0)

    @unittest.skipUnless(os.name == "nt", "the service runs on the Windows control machine; its files use Windows paths")
    def test_service_model_size_is_checked_on_every_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.gguf"
            model.write_bytes(b"x" * 10)
            obj = rtx_example()
            obj["assets"]["workstation"]["ds41_model"].update(path=str(model), bytes=10)
            self.render(obj)
            obj["assets"]["workstation"]["ds41_model"]["bytes"] = 11
            with self.assertRaisesRegex(ConfigError, "10 bytes"):
                self.render(obj)
            model.unlink()
            with self.assertRaisesRegex(ConfigError, "unavailable"):
                self.render(obj)

    def test_window_longer_than_20_minutes_needs_an_extended_approval(self):
        with self.assertRaisesRegex(ConfigError, "20 min"):
            self.render(rtx_example(reservation_s=1800))
        obj = rtx_example(reservation_s=1800)
        obj["pc"]["cuda"]["reservation"]["extended_approval"] = "coord 21:40 main ACK 30 min"
        self.render(obj)

    @unittest.skipUnless(os.name == "nt", "the service runs on the Windows control machine; its files use Windows paths")
    def test_binary_sha_comes_from_the_release_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = rtx_example()
            obj["machines"][0]["install_root"] = tmp
            release = Path(tmp) / "cuda" / "9211b07e13c6"
            release.mkdir(parents=True)
            (release / "MANIFEST.json").write_text(json.dumps(
                {"binaries": {"strix-cuda-ds41-prefill-service.exe": "c" * 64}}), encoding="utf-8")
            self.assertEqual(self.render(obj).binary_sha256, "c" * 64)

    def test_cuda_switches_are_not_inherited_but_toolkit_paths_are(self):
        with mock.patch.dict(os.environ, {"CUDA_LAUNCH_BLOCKING": "1", "CUDA_MPS_PIPE_DIRECTORY": "x",
                                          "PYTHONPATH": "x", "STRIX_DS41_TRACE": "1", "CUDA_PATH": "C:/cuda"}):
            spec = self.render()
        for name in ("CUDA_LAUNCH_BLOCKING", "CUDA_MPS_PIPE_DIRECTORY", "PYTHONPATH", "STRIX_DS41_TRACE"):
            self.assertNotIn(name, spec.env)
        self.assertEqual(spec.env["CUDA_PATH"], "C:/cuda")
        self.assertEqual(spec.env["CUDA_VISIBLE_DEVICES"], PRO)

    def test_window_deadline_starts_at_render(self):
        obj = rtx_example()
        del obj["pc"]["cuda"]["reservation"]["expires_at"]
        obj["pc"]["cuda"]["reservation"]["window_s"] = 900
        spec = self.render(obj, now=1_000_000.0)
        self.assertEqual(spec.deadline, 1_000_900.0)

    def test_preview_has_no_infinite_deadline(self):
        obj = rtx_example(reservation_s=None)
        obj["machines"][0]["lab_mode"] = False
        with mock.patch.object(rtx, "memory_status", lambda: {"free_physical_gib": 100.0, "free_commit_gib": 100.0}):
            preview = rtx.render_preview(parse(obj), "ds41-rtx")
        self.assertIsNone(preview["deadline"])
        json.dumps(preview, allow_nan=False)


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


def _memory(commit=60.0):
    return lambda: {"free_physical_gib": 50.0, "free_commit_gib": commit}


class WatchdogTests(unittest.TestCase):
    def watchdog(self, memory=None, apps=(), used=1000, now=None, spec=None):
        lines = []
        dog = rtx.Watchdog(spec or _spec(), 42, lambda: None, lines.append, memory=memory or _memory(),
                           apps=lambda: list(apps), used=lambda _: used, clock=lambda: now or time.time())
        dog.baseline_mib = 600
        return dog, lines

    def test_healthy_sample_logs_and_keeps_running(self):
        dog, lines = self.watchdog(apps=[(PRO, 42, None)], used=60000)
        self.assertIsNone(dog.check())
        self.assertEqual(lines[0]["vram_mib"], 59400)  # WDDM: device use above the baseline

    def test_commit_floor(self):
        dog, _ = self.watchdog(memory=_memory(3.0))
        self.assertIn("free commit", dog.check())

    def test_available_ram_floor_needs_consecutive_samples(self):
        low = lambda: {"free_physical_gib": 0.9, "free_commit_gib": 60.0}
        dog, _ = self.watchdog(memory=low)
        for _ in range(rtx.PHYSICAL_STRIKES - 1):
            self.assertIsNone(dog.check_memory()[0])
        self.assertIn("available RAM", dog.check_memory()[0])
        dog, _ = self.watchdog(memory=low)
        dog.check_memory()
        dog.memory = _memory()
        self.assertIsNone(dog.check_memory()[0])
        self.assertEqual(dog.physical_strikes, 0)

    def test_visible_on_another_gpu(self):
        dog, _ = self.watchdog(apps=[(PRO, 42, 100), (OTHER, 42, 100)])
        self.assertIn(OTHER, dog.check())

    def test_vram_cap_per_process(self):
        dog, _ = self.watchdog(apps=[(PRO, 42, 91 * 1024)])
        self.assertIn("cap", dog.check())

    def test_deadline(self):
        dog, _ = self.watchdog(spec=_spec(deadline=time.time() - 1))
        self.assertEqual(dog.check(), "deadline")

    def test_persistent_smi_failure_is_fatal_transient_is_not(self):
        def failing():
            raise rtx.ServiceError("nvidia-smi timed out")
        dog, lines = self.watchdog()
        dog.apps = failing
        self.assertIsNone(dog.check())
        self.assertIsNone(dog.check())
        self.assertIn("nvidia-smi failed 3 times", dog.check())
        dog, _ = self.watchdog(apps=[(PRO, 42, 100)])
        dog.smi_failures = 2
        self.assertIsNone(dog.check())
        self.assertEqual(dog.smi_failures, 0)

    def test_unmeasurable_vram_is_fatal(self):
        dog, _ = self.watchdog(apps=[(PRO, 42, None)])
        dog.baseline_mib = None
        self.assertIn("cannot be measured", dog.check())

    def test_ready_event_is_scrubbed_of_keys(self):
        event = {"event": "READY", "detail": "listening key=" + "ab" * 32, "argv": ["--api-key", "ab" * 32]}
        clean = rtx._scrub(event, ["ab" * 32, ""])
        self.assertNotIn("ab" * 32, json.dumps(clean))
        self.assertEqual(clean["event"], "READY")

    def run_thread(self, **kw):
        kills = []
        dog = rtx.Watchdog(_spec(), 42, lambda: kills.append(1), lambda _: None, interval=0.01,
                           memory_interval=0.01, **kw).start()
        dog._thread.join(2)
        dog.stop()
        return dog, kills

    def test_kill_runs_exactly_once_from_the_thread(self):
        dog, kills = self.run_thread(memory=_memory(1.0), apps=lambda: [], used=lambda _: 0)
        self.assertEqual(kills, [1])
        self.assertIn("free commit", dog.reason)
        self.assertFalse(dog.alive())

    def test_fails_closed_on_any_watchdog_error(self):
        def broken():
            raise ValueError("[N/A]")
        for kw in ({"memory": broken, "apps": lambda: [], "used": lambda _: 0},
                   {"memory": _memory(), "apps": lambda: [("x",)], "used": lambda _: 0}):
            dog, kills = self.run_thread(**kw)
            self.assertEqual(kills, [1])
            self.assertIn("watchdog error", dog.reason)

    def test_a_failing_kill_still_records_the_reason(self):
        def kill():
            raise OSError("gone")
        dog = rtx.Watchdog(_spec(), 42, kill, lambda _: None, memory=_memory(1.0), apps=lambda: [],
                           used=lambda _: 0, interval=0.01, memory_interval=0.01).start()
        dog._thread.join(2)
        self.assertIn("free commit", dog.reason)

    def test_memory_is_sampled_between_gpu_samples(self):
        calls = {"memory": 0, "apps": 0}

        def memory():
            calls["memory"] += 1
            return {"free_physical_gib": 50.0, "free_commit_gib": 60.0 if calls["memory"] < 6 else 1.0}

        def apps():
            calls["apps"] += 1
            return []
        dog = rtx.Watchdog(_spec(), 42, lambda: None, lambda _: None, memory=memory, apps=apps, used=lambda _: 0,
                           interval=60, memory_interval=0.01).start()
        dog._thread.join(2)
        self.assertIn("free commit", dog.reason)
        self.assertEqual(calls["apps"], 1)


class FakeChild:
    instances = []

    def __init__(self, argv, env, directory, name, port=None):
        self.argv, self.env, self.directory = argv, env, Path(directory)
        self.process = mock.Mock(pid=4242, returncode=None)
        self.stopped = False
        FakeChild.instances.append(self)

    def start(self):
        (self.directory / "service.stdout.log").write_bytes(b'{"event":"loaded"}\n{"event":"READY"}\n')
        return self

    def alive(self):
        return not self.stopped

    def stop(self, grace_s=5):
        self.stopped = True


class FakeDog:
    def __init__(self, *args, **kw):
        self.reason, self.running = None, True

    def start(self):
        return self

    def alive(self):
        return self.running

    def stop(self):
        self.running = False


def _binary_spec(**kw):
    import sys
    return _spec(**dict({"argv": [sys.executable] + _spec().argv[1:], "binary_sha256": rtx._sha256(sys.executable)}, **kw))


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        FakeChild.instances = []

    def write(self, spec=None, owner=None):
        path = self.directory / "spec.json"
        path.write_text(json.dumps({"spec": dict((spec or _binary_spec()).__dict__), "owner": owner}), encoding="utf-8")
        return path

    def run_helper(self, spec_path, **kw):
        options = dict(child_factory=FakeChild, watchdog_factory=FakeDog, verify=lambda u: {"uuid": u},
                       health=lambda port: {"status": "ok"},
                       lock_factory=lambda u: rtx.procs.InstanceLock(self.directory / "device.lock"))
        options.update(kw)
        thread = threading.Thread(target=rtx.service_run, args=(spec_path,), kwargs=options)
        thread.start()
        return thread

    def state(self):
        path = self.directory / "state.json"
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except (ValueError, PermissionError):
            return {}

    def wait_state(self, *states, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state().get("state") in states:
                return self.state()
            time.sleep(0.02)
        self.fail(f"no state in {states}: {self.state()}")

    def test_ready_then_stop_and_keys_leave_the_disk(self):
        spec_path = self.write()
        thread = self.run_helper(spec_path)
        self.assertEqual(self.wait_state("ready")["pid"], 4242)
        self.assertFalse(spec_path.exists())
        self.assertEqual(FakeChild.instances[0].env["CUDA_VISIBLE_DEVICES"], PRO)
        (self.directory / "stop").touch()
        thread.join(5)
        self.assertEqual(self.state()["state"], "stopped")
        self.assertTrue(FakeChild.instances[0].stopped)
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn(KEY_A, path.read_text(encoding="utf-8", errors="replace"), path.name)

    def test_device_check_failure_starts_nothing(self):
        def refuse(uuid):
            raise rtx.ServiceError("not a PRO")
        self.run_helper(self.write(), verify=refuse).join(5)
        self.assertEqual(FakeChild.instances, [])
        self.assertIn("not a PRO", self.state()["reason"])

    def test_binary_must_match_its_manifest(self):
        self.run_helper(self.write(_binary_spec(binary_sha256="0" * 64))).join(5)
        self.assertEqual(FakeChild.instances, [])
        self.assertIn("does not match", self.state()["reason"])
        self.run_helper(self.write(_spec())).join(5)
        self.assertIn("no release manifest", self.state()["reason"])

    def test_operator_gate_runs_before_the_device_is_touched(self):
        calls = []

        def verify(uuid):
            calls.append("verify")
            return {"uuid": uuid}

        def gate(argv):
            calls.append(("gate", argv))
            return "gate refused (rc 1): PC-BIG open: someone"
        self.run_helper(self.write(_binary_spec(gate=["C:/g.exe"])), verify=verify, gate_runner=gate).join(5)
        self.assertEqual(calls, [("gate", ["C:/g.exe"])])
        self.assertEqual(FakeChild.instances, [])
        self.assertIn("PC-BIG open: someone", self.state()["reason"])
        calls.clear()
        thread = self.run_helper(self.write(_binary_spec(gate=["C:/g.exe"])), verify=verify,
                                 gate_runner=lambda argv: calls.append("gate"))
        self.wait_state("ready")
        rtx.stop_service({"directory": str(self.directory)}, timeout_s=5)
        thread.join(5)
        self.assertEqual(calls, ["gate", "verify"])

    def test_stop_requested_before_the_child_starts_nothing(self):
        (self.directory / "stop").touch()
        self.run_helper(self.write()).join(5)
        self.assertEqual(FakeChild.instances, [])
        self.assertIn("before start", self.state()["reason"])

    def test_stop_service_before_the_helper_runs_wins_the_race(self):
        """B3: stop_service with no helper alive marks stopped; a helper starting afterwards launches nothing."""
        spec_path = self.write()
        result = rtx.stop_service({"directory": str(self.directory)}, timeout_s=5)
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(spec_path.exists())  # the keys are gone too
        self.run_helper(spec_path).join(5)
        self.assertEqual(FakeChild.instances, [])
        self.assertEqual(self.state()["state"], "stopped")

    def test_stop_service_waits_for_a_live_helper(self):
        thread = self.run_helper(self.write())
        self.wait_state("ready")
        result = rtx.stop_service({"directory": str(self.directory)}, timeout_s=5)
        thread.join(5)
        self.assertEqual(result["state"], "stopped")
        self.assertTrue(FakeChild.instances[0].stopped)

    def test_watchdog_reason_dead_watchdog_and_deadline_stop_the_service(self):
        class Killing(FakeDog):
            def start(self):
                self.reason = "free commit 1.0 GiB below 4"
                return self

        class Dead(FakeDog):
            def alive(self):
                return False
        for factory, spec, want in ((Killing, _binary_spec(), "watchdog: free commit"),
                                    (Dead, _binary_spec(), "watchdog stopped"),
                                    (FakeDog, _binary_spec(deadline=time.time() - 1), "deadline")):
            FakeChild.instances = []
            self.run_helper(self.write(spec), watchdog_factory=factory).join(5)
            self.assertIn(want, self.state()["reason"])
            self.assertTrue(FakeChild.instances[0].stopped)

    def test_readiness_needs_health_within_the_ready_timeout(self):
        thread = self.run_helper(self.write(_binary_spec(ready_timeout_s=1)), health=lambda port: {"status": "failed"})
        thread.join(5)
        self.assertEqual(self.state()["reason"], "readiness timed out")
        self.assertTrue(FakeChild.instances[0].stopped)

    def test_health_strikes_after_ready_stop_the_service(self):
        answers = iter([{"status": "ok"}] + [{"status": "failed"}] * 10)
        with mock.patch.object(rtx, "HEALTH_INTERVAL_S", 0.01):
            self.run_helper(self.write(), health=lambda port: next(answers)).join(10)
        self.assertEqual(self.state()["reason"], "health failed")

    def test_controller_ownership_gone_stops_the_service(self):
        """B1: a controller that died (its state file gone) does not leave the service running."""
        owner_state = self.directory / "up.json"
        owner_state.write_text(json.dumps({"token": "t1"}), encoding="utf-8")
        with mock.patch.object(rtx, "OWNER_INTERVAL_S", 0.05):
            thread = self.run_helper(self.write(owner={"state_path": str(owner_state), "token": "t1"}))
            self.wait_state("ready")
            owner_state.unlink()
            thread.join(10)
        self.assertEqual(self.state()["reason"], "controller ownership gone")
        self.assertTrue(FakeChild.instances[0].stopped)

    def test_one_heavy_job_per_device(self):
        lock = rtx.procs.InstanceLock(self.directory / "device.lock").acquire()
        try:
            self.run_helper(self.write()).join(5)
        finally:
            lock.close()
        self.assertEqual(FakeChild.instances, [])
        self.assertIn("another instance", self.state()["reason"])


class DeviceTests(unittest.TestCase):
    def smi(self, stdout, code=0):
        return lambda *a, **kw: mock.Mock(returncode=code, stdout=stdout, stderr="")

    def smi_by_query(self, gpu, apps="", used="639"):
        def run(argv, **kw):
            query = next(a for a in argv if a.startswith("--query"))
            out = {"--query-gpu=uuid,name,pci.bus_id,memory.total": gpu,
                   "--query-compute-apps=gpu_uuid,pid,used_memory": apps,
                   "--query-gpu=memory.used": used + "\n"}[query]
            return mock.Mock(returncode=0, stdout=out, stderr="")
        return run

    def test_verify_pro(self):
        row = f"{PRO}, NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 00000000:01:00.0, 97887\n"
        info = rtx.verify_pro(PRO, self.smi_by_query(row))
        self.assertEqual(info["memory_total_mib"], 97887)
        # Another job on the PRO (a listed process with real memory, or device memory under WDDM) refuses the start.
        with self.assertRaisesRegex(rtx.ServiceError, "busy: 85000 MiB"):
            rtx.verify_pro(PRO, self.smi_by_query(row, used="85000"))
        # WDDM: desktop apps listed with [N/A] memory on an idle PRO are not a job; a real job shows device memory.
        rtx.verify_pro(PRO, self.smi_by_query(row, apps=f"{PRO}, 29048, [N/A]\n{PRO}, 80700, [N/A]\n", used="40"))
        with self.assertRaisesRegex(rtx.ServiceError, "busy: 70000 MiB"):
            rtx.verify_pro(PRO, self.smi_by_query(row, apps=f"{PRO}, 29048, [N/A]\n", used="70000"))
        with self.assertRaisesRegex(rtx.ServiceError, "busy: compute processes .777."):
            rtx.verify_pro(PRO, self.smi_by_query(row, apps=f"{PRO}, 777, 5000\n", used="40"))
        rtx.verify_pro(PRO, self.smi_by_query(row, apps=f"{OTHER}, 777, 300\n"))
        with self.assertRaises(rtx.ServiceError):
            rtx.verify_pro(PRO, self.smi(f"{PRO}, NVIDIA GeForce RTX 4090, 00000000:02:00.0, 24564\n"))
        with self.assertRaises(rtx.ServiceError):
            rtx.verify_pro(PRO, self.smi("", code=6))

    def test_compute_apps_handles_wddm_na(self):
        rows = rtx.compute_apps(self.smi(f"{PRO}, 42, [N/A]\n{OTHER}, 7, 300\n"))
        self.assertEqual(rows, [(PRO, 42, None), (OTHER, 7, 300)])

    def test_smi_is_short_and_never_opens_a_window(self):
        calls = []
        rtx.compute_apps(lambda *a, **kw: calls.append(kw) or mock.Mock(returncode=0, stdout="", stderr=""))
        self.assertEqual(calls[0]["timeout"], rtx.SMI_TIMEOUT_S)
        if os.name == "nt":
            self.assertTrue(calls[0]["creationflags"] & 0x08000000)

    def test_visibility_is_unverified_without_per_process_rows(self):
        service = {"pid": 4242, "device": PRO}
        memory = lambda: {PRO: 90000, OTHER: 3700}
        self.assertIsNone(rtx.visibility(service, lambda: [], memory)["only_pro"])
        self.assertTrue(rtx.visibility(service, lambda: [(PRO, 4242, None)], memory)["only_pro"])
        self.assertFalse(rtx.visibility(service, lambda: [(PRO, 4242, 1), (OTHER, 4242, 1)], memory)["only_pro"])
        self.assertEqual(rtx.visibility(service, lambda: [], memory)["device_memory_mib"][OTHER], 3700)


class AcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = parse(rtx_example())
        state = {"profile": "ds41-rtx", "router": {"endpoint": {"address": "127.0.0.1", "port": 18600}},
                 "prefill_service": {"port": 8095, "pid": 4242, "device": PRO}}
        for target, value in (("_running", lambda profile: (state, state["prefill_service"])),
                              ("_keys", lambda profile, suffix: "k" * 64)):
            patcher = mock.patch.object(rtx, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_prompt_ids_are_checked_against_the_oracle(self):
        directory = Path(self.tmp.name)
        (directory / "p.txt").write_text("[1, 2, 3]\n[4]", encoding="utf-8")
        entry = {"source": "p.txt", "tokens": 6, "ids_sha256": hashlib.sha256(b"1,2,3,4,1,2").hexdigest()}
        self.assertEqual(rtx.prompt_ids(directory, entry), [1, 2, 3, 4, 1, 2])
        with self.assertRaisesRegex(rtx.ServiceError, "differ"):
            rtx.prompt_ids(directory, dict(entry, tokens=5))

    def test_fidelity_gate_pass_and_fail(self):
        data = rtx.service_preset("rtx-prefill-ds41")
        by_len = {e["tokens"]: e for e in data["fidelity"]["oracle"]}

        def request(address, port, method, path, body, key):
            e = by_len[len(body["tokens"])]
            return 200, {}, {"sha256": e["state_sha256"], "next_token": e["next_token"], "logits_fnv": e["logits_fnv"],
                             "state_bytes": e["state_bytes"], "fast": {"dense": False, "moe": False, "attention": False}}

        def gate(request=request, apps=lambda: [(PRO, 4242, None)]):
            return rtx.fidelity_gate(self.cluster, "ds41-rtx", self.tmp.name, request=request, apps=apps,
                                     memory=lambda: {PRO: 90000})

        with mock.patch.object(rtx, "prompt_ids", lambda d, e: [0] * e["tokens"]):
            result = gate()
            self.assertTrue(result["pass"], result)
            self.assertTrue(result["visibility"]["only_pro"])
            self.assertFalse(gate(apps=lambda: [(PRO, 4242, None), (OTHER, 4242, None)])["pass"])
            unverified = gate(apps=lambda: [])
            self.assertTrue(unverified["pass"])
            self.assertIsNone(unverified["visibility"]["only_pro"])

            def fast(*args):
                status, headers, reply = request(*args)
                return status, headers, dict(reply, fast={"dense": True})
            self.assertFalse(gate(request=fast)["pass"])

            def other_state(*args):
                status, headers, reply = request(*args)
                return status, headers, dict(reply, sha256="f" * 64)
            self.assertFalse(gate(request=other_state)["pass"])

    def test_standalone_gate_launches_like_up_and_always_stops(self):
        calls = []

        def start(spec, detached):
            calls.append(("start", spec))
            return {"port": spec.port, "pid": 7, "device": PRO, "directory": "d", "ready": {"event": "READY"}}

        def gate(cluster, profile, prompts, service, key):
            calls.append(("gate", key))
            self.assertIn("--api-key", calls[0][1].argv)
            self.assertEqual(calls[0][1].argv[calls[0][1].argv.index("--api-key") + 1], key)
            return {"pass": True}

        with mock.patch.object(rtx, "memory_status", lambda: {"free_physical_gib": 100.0, "free_commit_gib": 100.0}):
            result = rtx.standalone_gate(self.cluster, "ds41-rtx", self.tmp.name, detached=None, start=start,
                                         stop=lambda record: calls.append(("stop", record["port"])), gate=gate)
            self.assertNotIn(calls[1][1], json.dumps(result))  # the key never reaches the report
            self.assertEqual([c[0] for c in calls], ["start", "gate", "stop"])

            def failing(*args, **kw):
                raise rtx.ServiceError("boom")
            calls.clear()
            with self.assertRaises(rtx.ServiceError):
                rtx.standalone_gate(self.cluster, "ds41-rtx", self.tmp.name, detached=None, start=start,
                                    stop=lambda record: calls.append(("stop", 1)), gate=failing)
            self.assertEqual(calls[-1][0], "stop")

    def test_placement_long_offloads_short_stays_local(self):
        counts = {"local": 0, "offload": 0, "fallback": 0, "cache": 0}
        served = {"n": 0}

        def request(address, port, method, path, body, key):
            if path == "/tokenize":
                return 200, {}, {"tokens": [0] * len(body["prompt"].split(" "))}
            if path == "/api/status":
                return 200, {}, {"prefill": {"placements": dict(counts)}}
            place = "offload" if len(body["prompt"].split(" ")) > 2048 else "local"
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

        def old_key(address, port, method, path, body, key):
            if path == "/api/status":
                return 200, {}, {"prefill": {"counts": dict(counts)}}
            return request(address, port, method, path, body, key)
        with self.assertRaisesRegex(rtx.ServiceError, "no prefill placements"):
            rtx.placement_check(self.cluster, "ds41-rtx", request=old_key, health=lambda port: {"served": served["n"]})

    def test_router_status_names_its_placement_counts(self):
        """The check reads the key the pinned router publishes (W3 read a key the router never sent)."""
        router = (Path(__file__).resolve().parents[1] / "third_party" / "tensormeld" / "src" / "tensormeld"
                  / "replica_router.py")
        if not router.is_file():
            self.skipTest("pinned TensorMeld not checked out")
        source = router.read_text(encoding="utf-8")
        self.assertIn('"placements": dict(self.counts)', source)


@unittest.skipUnless(os.name == "nt", "the service runs on the Windows control machine; its files use Windows paths")
class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        obj = rtx_example()
        obj["machines"][0]["install_root"] = self.tmp.name
        self.cluster = parse(obj)
        self.release = Path(self.tmp.name) / "cuda" / "9211b07e13c6"
        self.argv = None

    def build(self, child, dog=FakeDog):
        return rtx.build(self.cluster, "ds41-rtx", Path(self.tmp.name) / "engine", child_factory=child,
                         watchdog_factory=dog, poll_s=0.01,
                         lock_factory=lambda u: rtx.procs.InstanceLock(Path(self.tmp.name) / "d.lock"))

    def fake(self, code=0):
        test = self

        class Child(FakeChild):
            def start(inner):
                staging = Path(inner.argv[inner.argv.index("-Output") + 1])
                if code == 0:
                    (staging / "MANIFEST.json").write_text(json.dumps({"binaries": {"x.exe": "a" * 64}}), encoding="utf-8")
                inner.process.wait = lambda *a: code
                inner.stopped = True  # the build finished
                test.argv = inner.argv
                return inner
        return Child

    def test_publishes_a_new_release_and_never_rebuilds_in_place(self):
        manifest = self.build(self.fake())
        self.assertEqual(manifest["binaries"]["x.exe"], "a" * 64)
        self.assertTrue((self.release / "MANIFEST.json").exists())
        self.assertIn("-ConfigureJson", self.argv)
        self.assertTrue(self.argv[0].lower().endswith("powershell.exe"))
        with self.assertRaisesRegex(rtx.ServiceError, "never rebuilt"):
            self.build(self.fake())

    def test_failure_or_watchdog_publishes_nothing(self):
        with self.assertRaisesRegex(rtx.ServiceError, "failed"):
            self.build(self.fake(code=1))

        class Killing(FakeDog):
            def start(self):
                self.reason = "free commit 1.0 GiB below 4"
                return self
        with self.assertRaisesRegex(rtx.ServiceError, "stopped: free commit"):
            self.build(self.fake(), dog=Killing)
        self.assertFalse(self.release.exists())

    def test_operator_gate_refuses_the_build_before_it_starts(self):
        obj = rtx_example()
        obj["machines"][0]["install_root"] = self.tmp.name
        obj["pc"]["cuda"]["reservation"]["gate"] = ["C:/g.exe"]
        started = []

        class Child(FakeChild):
            def start(inner):
                started.append(1)
        with self.assertRaisesRegex(rtx.ServiceError, "someone"):
            rtx.build(parse(obj), "ds41-rtx", Path(self.tmp.name) / "engine", child_factory=Child,
                      watchdog_factory=FakeDog, poll_s=0.01, gate_runner=lambda argv: "gate refused (rc 1): someone",
                      lock_factory=lambda u: rtx.procs.InstanceLock(Path(self.tmp.name) / "d.lock"))
        self.assertEqual(started, [])

    def test_lab_mode_build_needs_a_reservation(self):
        with self.assertRaisesRegex(rtx.ServiceError, "reservation"):
            rtx.build(parse(rtx_example(reservation_s=None)), "ds41-rtx", Path(self.tmp.name))


class UpDownTests(unittest.TestCase):
    """The service starts after the pair is READY and before the router; down stops router, service, then engines."""

    @staticmethod
    def modules():
        from hyperloom import orchestrate, secrets
        from tests import test_orchestrate
        return orchestrate, secrets, test_orchestrate

    def rtx_cluster(self):
        from hyperloom.config import Machine
        _, _, fixtures = self.modules()
        cluster = fixtures.cluster_fixture(pair=True)
        cluster.machines["workstation"] = Machine("workstation", "windows", "local", "127.0.0.1", None, 22, "C:/hyperloom",
                                                  True, [{"id": "rtx-pro", "type": "nvidia", "uuid": PRO}])
        settings = cluster.profiles.pop("ds41-pair")
        cluster.profiles["ds41-rtx"] = dict(settings, preset="ds41-pair-long", release_id=fixtures.RELEASE,
                                            prefill_service={"preset": "rtx-prefill-ds41", "machine": "workstation",
                                                             "device": "rtx-pro"})
        return cluster

    def processes(self, cluster, profile):
        return self.modules()[2].OrchestrationTests.processes(self, cluster, profile)

    def local(self, profile, rendered, keys, **kwargs):
        return self.modules()[2].OrchestrationTests.local(self, profile, rendered, keys, **kwargs)

    def setUp(self):
        orch, _, fixtures = self.modules()
        fixtures.OrchestrationTests.setUp(self)
        spec = _spec()
        self.launched = []

        def launch(record, spec, **kw):
            # B1: the record with its directory is already in the controller's state file.
            saved = json.loads(orch._state_path("ds41-rtx").read_text())
            self.launched.append((saved["prefill_service"]["directory"], kw["owner"]))
            self.calls.append(("local", "service-start", {}))
            record.update(pid=1)
            return record

        for target, effect in (
                ("render_service", lambda cluster, profile, **kw: (self.calls.append(("local", "service-render", kw)), spec)[1]),
                ("launch_service", launch),
                ("stop_service", lambda record: self.calls.append(("local", "service-stop", {"directory": record["directory"]}))),
                ("memory_status", lambda: {"free_physical_gib": 100.0, "free_commit_gib": 100.0})):
            patcher = mock.patch.object(orch.rtx, target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_order_keys_ownership_and_router_services(self):
        orch, secrets, _ = self.modules()
        state = orch.up(self.rtx_cluster(), "ds41-rtx", devices=["workstation:rtx-pro"])
        order = [verb for _, verb, _ in self.calls if verb in ("lease", "service-start", "router-start")]
        self.assertEqual(order, ["lease", "service-start", "router-start"])
        directory, owner = self.launched[0]
        self.assertTrue(directory)
        self.assertEqual(owner, {"state_path": str(orch._state_path("ds41-rtx")), "token": state["token"]})
        render_kw = next(p for _, verb, p in self.calls if verb == "service-render")
        self.assertEqual(render_kw["handoff_api_key"], secrets.get(orch._secret_name("ds41-rtx", "halo-1")))
        self.assertEqual(render_kw["api_key"], secrets.get(orch._secret_name("ds41-rtx", "prefill")))
        rendered = next(p for _, verb, p in self.calls if verb == "router-start")["rendered"]
        self.assertEqual(rendered["prefill"]["policy"], "auto")
        self.assertEqual(rendered["prefill"]["services"][0]["port"], 8095)
        self.assertNotIn(render_kw["api_key"], json.dumps(state))
        orch.down("ds41-rtx")
        order = [verb for _, verb, _ in self.calls if verb in ("router-stop", "service-stop", "stop")]
        self.assertEqual(order[:3], ["router-stop", "service-stop", "stop"])

    def test_service_requires_the_local_router(self):
        orch, _, _ = self.modules()
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            orch.up(self.rtx_cluster(), "ds41-rtx", router="halo-1")
        self.assertFalse([c for c in self.calls if c[1] == "lease"])

    def test_service_failure_stops_the_prepared_service_and_rolls_back_the_pair(self):
        orch, _, _ = self.modules()
        with mock.patch.object(orch.rtx, "launch_service", side_effect=rtx.ServiceError("no READY")):
            with self.assertRaisesRegex(rtx.ServiceError, "no READY"):
                orch.up(self.rtx_cluster(), "ds41-rtx")
        stops = [p for _, verb, p in self.calls if verb == "service-stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["directory"])
        self.assertTrue(all(row["state"] == "stopped" for row in self.leases.values()))
        self.assertFalse([c for c in self.calls if c[1] == "router-start"])

    def test_a_failing_service_stop_still_stops_the_engines(self):
        orch, _, _ = self.modules()
        orch.up(self.rtx_cluster(), "ds41-rtx")
        with mock.patch.object(orch.rtx, "stop_service", side_effect=rtx.ServiceError("did not confirm stop")):
            with self.assertRaisesRegex(orch.OrchestrationError, "prefill service: did not confirm"):
                orch._down_state(json.loads(orch._state_path("ds41-rtx").read_text()))
        self.assertTrue(all(row["state"] == "stopped" for row in self.leases.values()))


class RealChildTests(unittest.TestCase):
    """The helper with the real LocalChild (Job Object on Windows) and the real watchdog around a stand-in service."""

    def setUp(self):
        import socket
        import sys
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        fake = Path(__file__).with_name("fixtures") / "prefill" / "fake_service.py"
        self.spec = _spec(port=self.port, argv=[sys.executable, str(fake), "--port", str(self.port), "--api-key", KEY_A],
                          ready_timeout_s=30, binary_sha256=rtx._sha256(sys.executable))
        self.spec_path = self.directory / "spec.json"
        self.spec_path.write_text(json.dumps({"spec": dict(self.spec.__dict__)}), encoding="utf-8")

    def state(self):
        path = self.directory / "state.json"
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except (ValueError, PermissionError):
            return {}

    def wait_state(self, *states, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state().get("state") in states:
                return self.state()
            time.sleep(0.1)
        self.fail(f"no state in {states}: {self.state()}")

    def wait_down(self, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                rtx._health(self.port)
            except OSError:
                return True
            time.sleep(0.1)
        return False

    def helper(self, dog):
        return threading.Thread(target=rtx.service_run, args=(self.spec_path,), kwargs=dict(
            watchdog_factory=dog, verify=lambda u: {"uuid": u},
            lock_factory=lambda u: rtx.procs.InstanceLock(self.directory / "device.lock")))

    def test_job_owned_child_ready_health_and_stop(self):
        thread = self.helper(FakeDog)
        thread.start()
        state = self.wait_state("ready", "stopped")
        self.assertEqual(state["state"], "ready", state)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, state["pid"])  # PROCESS_QUERY_LIMITED_INFORMATION
            result = wintypes.BOOL()
            self.assertTrue(kernel.IsProcessInJob(handle, None, ctypes.byref(result)))
            kernel.CloseHandle(handle)
            self.assertTrue(result.value, "the service is not in a Job Object")
        status, _, reply = rtx._request("127.0.0.1", self.port, "POST", "/v1/prefill", {"tokens": [1, 2]}, KEY_A)
        self.assertEqual((status, reply["tokens"]), (200, 2))
        loaded = json.loads((self.directory / "service.stdout.log").read_text().splitlines()[0])
        self.assertEqual(loaded["env_device"], PRO)
        (self.directory / "stop").touch()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.state()["state"], "stopped")
        self.assertTrue(self.wait_down())

    def test_real_watchdog_kills_the_service(self):
        level = {"commit": 60.0}

        class Dog(rtx.Watchdog):
            def __init__(inner, *args, **kw):
                super().__init__(*args, memory=lambda: {"free_physical_gib": 50.0, "free_commit_gib": level["commit"]},
                                 apps=lambda: [], used=lambda u: 0, memory_interval=0.05, **kw)
        thread = self.helper(Dog)
        thread.start()
        self.wait_state("ready", "stopped")
        level["commit"] = 1.0
        thread.join(30)
        self.assertIn("watchdog: free commit", self.state()["reason"])
        self.assertTrue(self.wait_down())
        log = (self.directory / "watchdog.jsonl").read_text().splitlines()
        self.assertIn("kill", json.loads(log[-1]))

    @unittest.skipUnless(os.name == "nt", "Job Object kill-on-close is the Windows parent-death path")
    def test_service_dies_with_its_helper(self):
        import subprocess
        import sys
        driver = Path(__file__).with_name("fixtures") / "prefill" / "helper_driver.py"
        helper = subprocess.Popen([sys.executable, str(driver), str(self.spec_path)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, creationflags=0x08000000)
        try:
            self.assertEqual(self.wait_state("ready", "stopped")["state"], "ready")
            helper.kill()  # TerminateProcess: no cleanup code runs in the helper
            helper.wait(10)
            self.assertTrue(self.wait_down(), "the service outlived its helper")
            stopped = rtx.stop_service({"directory": str(self.directory)}, timeout_s=10)
            self.assertEqual(stopped["reason"], "helper gone")
        finally:
            if helper.poll() is None:
                helper.kill()


if __name__ == "__main__":
    unittest.main()
