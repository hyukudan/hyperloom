"""Explicit measurements use only fake GPUs, counters and model readers."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom import cli, rtx
from hyperloom.recipes import content_cache, core, measurements as m, placement
from hyperloom.recipes.reader import RecipeError
from tests.test_recipes_grants import NOW, window


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.uuid = "GPU-" + str(uuid.uuid4())
        self.row = {"uuid": self.uuid, "driver": "600.1", "name": "test device"}
        self.grant = {"uuid": self.uuid, "vram_cap_gib": 4, "approval": "operator test", "window": window(),
                      "gate": [sys.executable, "fake-gate"]}
        machine = SimpleNamespace(access="local", devices=[{"uuid": self.uuid}])
        self.cluster = SimpleNamespace(pc={"cuda": {"grants": [self.grant], "pro_uuid": self.uuid}},
                                       machines={"local": machine})
        self.approval = {"approval": "operator test", **{k: v for k, v in window().items() if k != "kind"}}
        self.identity = m.key(self.uuid, self.row["driver"], "a" * 64, "store-test")
        self.samples = [{"h2d_mbps": v, "d2h_mbps": 10 * v, "h2d_lat_ns": 100 * v}
                        for v in (9, 1, 8, 2, 7, 3, 6, 4, 5)]

    def acquire(self, mode="xfer", **kwargs):
        return m.acquire(self.cluster, mode, runtime=self.root, uuid=self.uuid, xfer_sha256="a" * 64,
                         store_id="store-test", clock=lambda: NOW, inventory=lambda: [self.row], **kwargs)

    def test_explicit_xfer_acquisition_requests_placement_inventory(self):
        with mock.patch.object(m.probe, "nvidia_gpus", return_value=[self.row]) as inventory:
            m.acquire(self.cluster, "xfer", runtime=self.root, uuid=self.uuid, xfer_sha256="a" * 64,
                      store_id="store-test", clock=lambda: NOW,
                      gpu_runner=lambda row, grant: m.median(self.samples, m.XFER_METHOD))
        inventory.assert_called_once_with(placement=True)

    def test_top_level_only_exclusive_modes_and_dispatch(self):
        parser = cli.build_parser()
        for mode in ("xfer", "store", "manifest"):
            args = parser.parse_args(["probe", "--" + mode])
            self.assertEqual(args.command, "probe")
            self.assertTrue(getattr(args, mode))
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["recipe", "probe", "profile", "--xfer"])
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["probe", "--xfer", "--store"])

    def test_nine_run_median_provenance_and_exact_key_no_refresh(self):
        run = mock.Mock(return_value=m.median(self.samples, m.XFER_METHOD))
        result = self.acquire(gpu_runner=run)
        self.assertEqual(result["values"], {"h2d_mbps": 5, "d2h_mbps": 50, "h2d_lat_ns": 500})
        self.assertEqual(result["provenance"]["method"]["runs"], 9)
        self.assertEqual(result["provenance"]["samples"], self.samples)
        cache = m.Cache(self.root)
        self.assertEqual(cache.lookup(self.identity, "xfer")["status"], "measured")
        for field in self.identity:
            changed = dict(self.identity, **{field: "b" * 64 if field == "xfer_sha256" else "changed"})
            self.assertEqual(cache.lookup(changed, "xfer")["status"], "unpredicted")
        for identity in ({**self.identity, "driver": "unknown"}, {**self.identity, "xfer_sha256": "unknown"}):
            self.assertEqual(cache.lookup(identity, "xfer")["status"], "unpredicted")
        self.assertEqual(run.call_count, 1)
        for samples in ([], self.samples[:8], self.samples + self.samples[:1], [{"v": float("nan")}] * 9):
            with self.assertRaises(RecipeError):
                m.median(samples, {})

    def test_grant_missing_expired_disabled_and_excluded_have_no_child_or_cache(self):
        run = mock.Mock(side_effect=AssertionError("measurement"))
        for mode in ("missing", "expired", "disabled", "excluded"):
            saved = deepcopy(self.grant)
            if mode == "missing":
                self.cluster.pc["cuda"]["grants"] = []
            elif mode == "expired":
                self.grant["window"]["expires_at"] = "2029-12-31T23:59:59Z"
            elif mode == "disabled":
                self.grant["vram_cap_gib"] = 0
            else:
                self.cluster.machines["local"].devices[0]["excluded"] = True
            with self.subTest(mode=mode), self.assertRaises(RecipeError):
                self.acquire(gpu_runner=run)
            self.cluster.pc["cuda"]["grants"] = [self.grant]
            self.grant.clear()
            self.grant.update(saved)
        run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_4090_refused_for_device_host_and_legacy_big_windows(self):
        self.row["name"] = "RTX " + str(4000 + 90)
        self.cluster.machines["local"].devices[0]["pc_big_conflict"] = True
        run = mock.Mock(side_effect=AssertionError("measurement"))
        cuda = self.cluster.pc["cuda"]
        for source in ("device", "host", "reservation"):
            self.grant["window"] = window("PC-BIG" if source == "device" else "device")
            if source == "host":
                cuda["host_grant"] = {"host_gib": 74, "approval": "test", "window": window("PC-BIG")}
            if source == "reservation":
                cuda.pop("host_grant", None)
                cuda["reservation"] = {"window_s": 300}
            with self.subTest(source=source), self.assertRaisesRegex(RecipeError, "PC-BIG"):
                self.acquire(gpu_runner=run)
        run.assert_not_called()

    def test_store_window_nine_direct_runs_and_expiry(self):
        measure = mock.Mock(side_effect=[{"method": "direct", "gbytes_ps": v} for v in range(1, 10)])
        result = m._store("fake-model", self.approval, clock=lambda: NOW, measure=measure)
        self.assertEqual(result["values"], {"file_read_mbps": 5000})
        self.assertEqual(measure.call_count, 9)
        self.assertEqual(result["provenance"]["method"]["read_bytes"], m.probe.DISK_CHUNK)
        measure.reset_mock(side_effect=True)
        measure.return_value = {"method": "buffered", "gbytes_ps": 1}
        with self.assertRaisesRegex(RecipeError, "buffered"):
            m._store("fake-model", self.approval, clock=lambda: NOW, measure=measure)
        for approval in (None, {**self.approval, "approval": ""},
                         {**self.approval, "expires_at": "2029-12-31T23:59:59Z"}):
            run = mock.Mock(side_effect=AssertionError("read"))
            with self.assertRaises(RecipeError):
                self.acquire("store", files=["fake-model"], window=approval, store_runner=run)
            run.assert_not_called()

    def test_manifest_refuses_without_window_and_without_qualified_backend(self):
        run = mock.Mock(side_effect=AssertionError("full read"))
        with self.assertRaises(RecipeError):
            self.acquire("manifest", files=["fake-model"], manifest_runner=run)
        run.assert_not_called()
        with self.assertRaisesRegex(RecipeError, "qualified manifest CLI"):
            self.acquire("manifest", files=["fake-model"], window=self.approval)
        # The acquisition boundary accepts only the core's manifest, using fake paths and hashes.
        document = {"schema": content_cache.MANIFEST_SCHEMA, "files": [{
            "path": str(self.root / "fake-model"), "size": 1, "mtime_ns": 1,
            "sha256": "c" * 64, "header_sha256": "d" * 64}]}
        result = self.acquire("manifest", files=["fake-model"], window=self.approval,
                              manifest_runner=lambda *_: document)
        self.assertTrue(content_cache._digest(result["manifest_sha256"]))

    def test_solver_lookup_removes_stale_figures_without_acquisition(self):
        cache = m.Cache(self.root)
        probe = {"probe_xfer": {"anchor": {"h2d_mbps": 999}}}
        with mock.patch.object(m, "acquire", side_effect=AssertionError("implicit measurement")):
            self.assertEqual(placement.cached_measurements(probe, {"anchor": self.identity}, cache)["probe_xfer"], {})
            cache.save(self.identity, "xfer", m.median(self.samples, m.XFER_METHOD))
            found = placement.cached_measurements(probe, {"anchor": self.identity}, cache)
            self.assertEqual(found["probe_xfer"]["anchor"]["h2d_mbps"], 5)
            self.assertEqual(probe["probe_xfer"]["anchor"]["h2d_mbps"], 999)

    def test_place_and_solve_never_acquire_and_solve_replaces_stale_choice_figures(self):
        backend = core.CoreBackend(core.Core("fake-core"))
        inputs = {"choices": [{"id": "test", "probe_xfer": {"anchor": {"h2d_mbps": 999}}}]}
        with mock.patch.object(m, "acquire", side_effect=AssertionError("implicit measurement")), \
                mock.patch.object(backend, "place") as place, mock.patch.object(backend, "solve") as solve:
            placement.place("fake-recipe", route="test", probe={}, components={}, backend=backend)
            place.assert_called_once()
            placement.solve("fake-recipe", route="test", probe={}, inputs=inputs, backend=backend,
                            measurement_keys={"anchor": self.identity}, measurement_cache=m.Cache(self.root))
            sent = solve.call_args.kwargs
            self.assertEqual(sent["probe"]["probe_xfer"], {})
            self.assertEqual(sent["inputs"]["choices"][0]["probe_xfer"], {})
            self.assertEqual(inputs["choices"][0]["probe_xfer"]["anchor"]["h2d_mbps"], 999)

    def test_gpu_lock_gate_watchdog_precede_release_and_cleanup(self):
        events = []
        observer = mock.Mock()
        observer.sample.return_value = {self.uuid.lower(): 0}
        child = mock.Mock(pid=42, returncode=0)
        child.poll.return_value = 0
        child.communicate.side_effect = lambda *a, **k: (events.append("release") or json.dumps(self.samples), "")
        dog = mock.Mock(reason=None)
        dog.check.return_value = None
        dog.start.side_effect = lambda: events.append("watchdog")
        dog.stop.side_effect = observer.close
        lock = mock.Mock()
        job = mock.Mock()
        lock.acquire.side_effect = lambda: events.append("lock") or lock
        def gate(_):
            events.append("gate")
        with mock.patch.object(rtx, "memory_status", return_value={"free_physical_gib": 90, "free_commit_gib": 100}), \
                mock.patch.object(rtx, "pro_busy", return_value=None):
            result = m._gpu(self.row, self.grant, clock=lambda: NOW, lock_factory=lambda _: lock,
                            gate_runner=gate, observer_factory=lambda: observer, watchdog_factory=lambda *a, **k: dog,
                            popen=lambda *a, **k: events.append("child") or child, job_factory=lambda: job)
        self.assertEqual(events[:5], ["lock", "gate", "child", "watchdog", "release"])
        self.assertEqual(result["values"]["h2d_mbps"], 5)
        lock.close.assert_called_once()
        observer.close.assert_called_once()
        job.assign.assert_called_once_with(child)
        job.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
