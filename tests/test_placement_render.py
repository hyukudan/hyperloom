"""G6 placement rendering with a test-only release and runtime-only physical identities."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from hyperloom.recipes import apply, content_cache, launch, runtime
from hyperloom.recipes.reader import RecipeError
from hyperloom import rtx


class PlacementRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = runtime.preset()
        self.data["engine_sha"] = "1" * 40  # test-only release; never a production/local engine pin
        self.uuids = ["GPU-" + str(uuid.uuid4()) for _ in range(2)]
        model = self.root / "model"
        model.write_bytes(b"synthetic model")
        stat = model.stat()
        self.manifest = {"schema": content_cache.MANIFEST_SCHEMA, "files": [
            {"path": str(model), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
             "sha256": "a" * 64, "header_sha256": "b" * 64}]}
        self.receipts = b'{"schema":"tierstream/receipts-v1"}\n'
        self.document = {"schema": "tierstream/placement-v1", "engine": self.data["engine"],
                         "manifest_sha256": content_cache._sha(self.manifest),
                         "receipts_sha256": hashlib.sha256(self.receipts).hexdigest(),
                         "devices": [{"kind": "cuda", "uuid": self.uuids[i],
                                      "pci": f"{0:04x}:{i + 1:02x}:{0:02x}.{0}"} for i in range(2)]}
        self.files = runtime.Files(*(self.root / n for n in ("placement.json", "manifest.json", "receipts.json")))
        self.files.manifest.write_bytes(json.dumps(self.manifest).encode())
        self.files.receipts.write_bytes(self.receipts)
        self.write()

    def write(self):
        self.files.placement.write_bytes(json.dumps(self.document).encode())

    def render(self, **kwargs):
        return runtime.render(self.files, binary="<release>/bin/replica", data=self.data, **kwargs)

    def test_single_and_two_gpu_goldens(self):
        from tests.test_recipes_grants import NOW, window
        golden = json.loads((Path(__file__).parent / "fixtures/render/placement.json").read_bytes())
        all_devices = deepcopy(self.document["devices"])
        for count in (1, 2):
            self.document["devices"] = deepcopy(all_devices[:count])
            for i, device in enumerate(self.document["devices"]):
                device.update(role="anchor" if i == 0 else "assist", budget_bytes=10 * rtx.GIB)
            self.document["devices"].reverse()  # output order must follow PCI, not input order
            self.write()
            policy = {"grants": [{"uuid": u, "vram_cap_gib": 10, "approval": "synthetic operator",
                                  "window": window(), "gate": [str(Path(__file__).resolve()), "fake"]}
                                 for u in self.uuids[:count]],
                      "caps_gib": {u.lower(): 10 for u in self.uuids[:count]}, "host_pinned_gib": 0,
                      "host_pageable_gib": 0, "free_commit_floor_gib": 4}
            machine = SimpleNamespace(access="local", os="windows", install_root=str(self.root))
            request = runtime.Request("local", self.files, policy, argv=("--exact",))
            with mock.patch.object(runtime, "preset", return_value=self.data), mock.patch.object(rtx.time, "time", return_value=NOW):
                spec = rtx.render_placement_service(SimpleNamespace(machines={"local": machine}), request, api_key="0" * 64)
            argv = [str(a).replace(str(self.root), "<runtime>").replace("\\", "/") for a in spec.redacted]
            env = {key: spec.env[key] for key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER")}
            env = {k: v.replace(self.uuids[0], "<anchor-uuid>").replace(self.uuids[1], "<assist-uuid>")
                   for k, v in env.items()}
            self.assertEqual({"argv": argv, "env": env, "release": self.data["engine_sha"]}, golden[str(count)])
            self.assertEqual(spec.placement, self.document)

    def test_unpublished_and_unsupported_engines_refuse(self):
        with self.assertRaisesRegex(RecipeError, "claudeinfo-release"):
            runtime.render(self.files, binary="unused")
        for engine in ("ds41-rtx-prefill", "hip", "other"):
            self.document["engine"] = engine
            self.write()
            with self.subTest(engine=engine), self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY"):
                self.render()

    def test_service_render_and_preview_use_exact_placement_supervision(self):
        from tests.test_recipes_grants import NOW, window
        for index, device in enumerate(self.document["devices"]):
            device.update(role="anchor" if index == 0 else "assist", budget_bytes=int(round(10.1 * rtx.GIB)))
        self.write()
        policy = {"grants": [{"uuid": u, "vram_cap_gib": 10.1, "approval": "synthetic operator",
                              "window": window(), "gate": [str(Path(__file__).resolve()), "fake"]}
                             for u in self.uuids],
                  "caps_gib": {u.lower(): 10.1 for u in self.uuids}, "host_pinned_gib": 0,
                  "host_pageable_gib": 0, "free_commit_floor_gib": 4}
        machine = SimpleNamespace(access="local", os="windows", install_root=str(self.root))
        cluster = SimpleNamespace(machines={"local": machine})
        request = runtime.Request("local", self.files, policy)
        with mock.patch.object(runtime, "preset", return_value=self.data), \
                mock.patch.object(rtx.time, "time", return_value=NOW), \
                mock.patch.object(rtx, "memory_status", side_effect=AssertionError("pure preview")):
            spec = rtx.render_service(cluster, "unused", api_key="a" * 64, handoff_api_key="b" * 64,
                                      free_physical_gib=0, placement_request=request)
            preview = rtx.render_preview(cluster, "unused", placement_request=request)
        self.assertIs(spec.placement_supervision, policy)
        self.assertIsInstance(spec.deadline, float)
        self.assertEqual(spec.argv.count("--placement"), 1)
        self.assertEqual(preview["placement"], self.document)
        self.assertNotIn("a" * 64, preview["argv"])
        self.assertEqual(spec.env["CUDA_VISIBLE_DEVICES"], ",".join(self.uuids))
        with self.assertRaisesRegex(rtx.ConfigError, "PL_E_CONFLICT"):
            rtx.render_service(cluster, "unused", api_key="a" * 64, handoff_api_key="b" * 64,
                               free_physical_gib=0, placement_request=request, vram_cap_gib=10)

    def test_every_legacy_sizing_sugar_and_duplicate_file_flag_refuses(self):
        for flag in sorted(runtime.SIZING | runtime.SUGAR | {"--placement", "--content-manifest", "--receipts"}):
            for tokens in ([flag, "1"], [flag + "=1"]):
                with self.subTest(argv=tokens), self.assertRaisesRegex(RecipeError, "PL_E_CONFLICT"):
                    self.render(argv=tokens)

    def test_recipe_launch_flags_are_only_runtime_files(self):
        route = {"id": "decode", "components": {"arena": {"units": 10}}, "placement": {
            "anchor": {"device_gib": 90, "host_pinned_gib": 74}}}
        with self.assertRaisesRegex(RecipeError, "PL_E_CAPABILITY"):
            apply.launch_flags(route, "anchor", {"device_gib": "vram_cap_gib"}, "legacy engine", 96)
        flags = apply.launch_flags(route, "anchor", {"device_gib": "vram_cap_gib"}, "decode adapter", 96,
                                   placement_files=self.files)
        self.assertEqual(flags, {"--placement": str(self.files.placement), "--content-manifest": str(self.files.manifest),
                                 "--receipts": str(self.files.receipts)})

    def test_stale_artifacts_and_duplicate_or_non_cuda_devices_refuse(self):
        original = deepcopy(self.document)
        self.files.receipts.write_bytes(self.receipts + b" ")
        with self.assertRaisesRegex(RecipeError, "PL_E_STALE.*receipts"):
            self.render()
        self.files.receipts.write_bytes(self.receipts)
        for devices, code in (([original["devices"][0]] * 2, "PL_E_SCHEMA"),
                              ([dict(original["devices"][0], kind="hip")], "PL_E_CAPABILITY"),
                              ([dict(original["devices"][0], uuid="")], "PL_E_UNKNOWN_DEVICE")):
            self.document["devices"] = devices
            self.write()
            with self.assertRaisesRegex(RecipeError, code):
                self.render()
        self.document = original
        self.write()
        (self.root / "model").write_bytes(b"changed")
        with self.assertRaisesRegex(RecipeError, "PL_E_STALE.*content"):
            self.render()

    def test_context_ingestion_keeps_exact_six_field_keys_and_attributable_history(self):
        event = dict(event="context", checkpoint=1, arch="sm_test", device_name="portable device class",
                     driver="test", platform="windows-wddm", build_sha256="a" * 64, signature="b" * 64,
                     ctx_observed=100, method="pdh-process-dedicated")
        realised = self.root / "realised.jsonl"
        rows = [event, dict(event, ctx_observed=200), dict(event, checkpoint=2, ctx_observed=999),
                dict(event, signature="c" * 64, ctx_observed=300), {"event": "READY"}]
        realised.write_bytes("\n".join(json.dumps(r) for r in rows).encode())
        with mock.patch("subprocess.run", side_effect=AssertionError("no engine or probe")):
            document = runtime.ingest(realised)
        self.assertEqual([e["samples"] for e in document["entries"]], [
            [{"bytes": 100, "method": "pdh-process-dedicated"}, {"bytes": 200, "method": "pdh-process-dedicated"}],
            [{"bytes": 300, "method": "pdh-process-dedicated"}]])
        self.assertNotIn("uuid", json.dumps(document))
        destination = self.root / "context-v1.json"
        self.assertEqual(launch.ingest_context(realised, destination), document)
        self.assertEqual(json.loads(destination.read_bytes()), document)
        for change in ({"method": "device-total"}, {"signature": ""}, {"ctx_observed": -1}):
            realised.write_bytes(json.dumps(dict(event, **change)).encode())
            with self.assertRaisesRegex(RecipeError, "PL_E_SCHEMA"):
                runtime.ingest(realised, document)
