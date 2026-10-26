"""Runtime cache invalidation and stat-only admission. All content and identities are synthetic."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyperloom import modelfiles
from hyperloom.placement import desc
from hyperloom.recipes import commands, content_cache, core, descriptors, launch, placement, reader


class ContentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "run"
        self.model, self.draft = [self.root / name for name in ("model.gguf", "draft.gguf")]
        for path in (self.model, self.draft):
            path.write_bytes(b"synthetic content")
        self.selected = {"model": self.model, "draft": self.draft}
        self.ref = content_cache.reference("a" * 64, {"rows": 6, "context": 4096}, "b" * 64)
        self.document = {"schema": content_cache.COMPONENT_SCHEMA, "build_sha256": "a" * 64,
                         "receipts_sha256": "b" * 64, "config": {"max_rows": 6}, "components": []}
        self.cache = content_cache.ComponentsStore(self.runtime)
        files = []
        for path in (self.model, self.draft):
            stat = path.stat()
            files.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                          "sha256": "c" * 64, "header_sha256": "d" * 64})
        self.manifest = {"schema": content_cache.MANIFEST_SCHEMA, "files": files}
        self.manifest_sha = self.cache.manifests.save(self.manifest)
        self.cache.save(self.ref, self.document, manifest=self.manifest_sha, selected=self.selected)

    @contextmanager
    def no_content_reads(self):
        original = Path.open
        content = {p.resolve() for p in self.selected.values()}

        def checked(path, *args, **kwargs):
            self.assertNotIn(path.resolve(), content, "admission must not open selected content")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", checked), \
                mock.patch("builtins.open", side_effect=AssertionError("no content/header reads")), \
                mock.patch.object(modelfiles, "read", side_effect=AssertionError("no header refresh")):
            yield

    def test_runtime_stores_and_key_order_and_triple_invalidation(self):
        with mock.patch.object(commands, "runtime_dir", return_value=self.runtime):
            self.assertEqual(commands.components_store().directory, self.runtime / "components-v1")
            self.assertEqual(content_cache.ManifestStore(commands.runtime_dir()).directory, self.runtime / "content-manifests")
        reordered = content_cache.reference("a" * 64, {"context": 4096, "rows": 6}, "b" * 64)
        with self.no_content_reads():
            self.assertEqual(self.cache.load(reordered, selected=self.selected), self.document)
            for field, value in (("build_sha256", "e" * 64), ("flags", {"rows": 1, "context": 4096}),
                                 ("receipts_sha256", "f" * 64)):
                changed = dict(self.ref, **{field: value})
                with self.subTest(field=field), self.assertRaisesRegex(reader.RecipeError, "PL_E_MISSING_DESCRIPTOR"):
                    self.cache.load(changed, selected=self.selected)
        self.assertEqual(len(list((self.runtime / "components-v1").iterdir())), 1)

    def test_new_draft_mtime_with_same_size_and_same_second_refuses(self):
        stat = self.draft.stat()
        self.draft.write_bytes(b"changed draft    ")
        self.assertEqual(self.draft.stat().st_size, stat.st_size)
        changed_ns = stat.st_mtime_ns + 100
        os.utime(self.draft, ns=(stat.st_atime_ns, changed_ns))
        with self.no_content_reads(), self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE.*probe --manifest"):
            self.cache.load(self.ref, selected=self.selected)
        self.assertEqual(self.draft.stat().st_mtime_ns // 1_000_000_000, stat.st_mtime_ns // 1_000_000_000)

    def test_selection_size_missing_manifest_and_content_refuse_without_refresh(self):
        for selected in ({"model": self.model}, {**self.selected, "draft": self.model}, None):
            with self.subTest(selected=selected), self.no_content_reads(), \
                    self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
                self.cache.load(self.ref, selected=selected)
        self.draft.write_bytes(b"different size")
        with self.no_content_reads(), self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
            self.cache.load(self.ref, selected=self.selected)
        (self.runtime / "content-manifests" / (self.manifest_sha + ".json")).unlink()
        with self.no_content_reads(), self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
            self.cache.load(self.ref, selected=self.selected)

    def test_cache_document_and_manifest_integrity_and_build_receipts(self):
        for key in ("build_sha256", "receipts_sha256"):
            with self.subTest(key=key), self.assertRaisesRegex(reader.RecipeError, "PL_E_MISSING_DESCRIPTOR"):
                self.cache.save(self.ref, dict(self.document, **{key: "e" * 64}),
                                manifest=self.manifest_sha, selected=self.selected)
        saved = next((self.runtime / "components-v1").iterdir())
        record = json.loads(saved.read_text())
        record["document"]["config"]["max_rows"] = 1
        saved.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(reader.RecipeError, "PL_E_MISSING_DESCRIPTOR"):
            self.cache.load(self.ref, selected=self.selected)
        self.cache.save(self.ref, self.document, manifest=self.manifest_sha, selected=self.selected)
        manifest = self.runtime / "content-manifests" / (self.manifest_sha + ".json")
        changed = deepcopy(self.manifest)
        changed["files"][0]["mtime_ns"] += 1
        manifest.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
            self.cache.load(self.ref, selected=self.selected)

    def test_desc_sidecar_does_not_change_descriptor_bytes_or_hash(self):
        document = {"schema": desc.SCHEMA, "model": "synthetic", "layers": [{"index": 0, "kind": "dense"}]}
        path = self.root / "desc.json"
        before = json.dumps(document).encode()
        path.write_bytes(before)
        digest = hashlib.sha256(before).hexdigest()
        loaded, components = desc.load_inputs(path, components=self.ref, store=self.cache, selected=self.selected)
        self.assertEqual(loaded, document)
        self.assertEqual(components, self.document)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertNotIn("components", loaded)

    def test_place_and_solve_only_read_cache_and_invoke_cpu_core(self):
        calls = []

        def run(argv, **kwargs):
            self.assertEqual(argv[0], "cpu-ts-recipe")
            command = argv[1]
            calls.append(command)
            flag = "--components" if command == "place" else "--inputs"
            data = json.loads(Path(argv[argv.index(flag) + 1]).read_text())
            if command == "solve":
                data = data["choices"][0]["components"]
            self.assertEqual(data, self.document)
            Path(argv[argv.index("--json-out") + 1]).write_text('{"cached": true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="cached plan", stderr="")

        backend = core.CoreBackend(core.Core("cpu-ts-recipe", run=run))
        inputs = {"choices": [{"id": "spec", "components": self.ref, "selected_content": self.selected}]}
        before = deepcopy(inputs)
        with self.no_content_reads():
            placement.place("recipe.toml", route="decode", probe={}, components=self.ref, cache=self.cache,
                            selected_content=self.selected, backend=backend)
            placement.solve("recipe.toml", route="decode", probe={}, inputs=inputs, cache=self.cache, backend=backend)
        self.assertEqual(inputs, before)
        self.assertEqual(calls, ["place", "solve"])
        changed = dict(self.ref, flags={"rows": 1})
        with self.no_content_reads(), self.assertRaisesRegex(reader.RecipeError, "PL_E_MISSING_DESCRIPTOR"):
            placement.place("recipe.toml", route="decode", probe={}, components=changed, cache=self.cache,
                            selected_content=self.selected, backend=backend)
        self.assertEqual(calls, ["place", "solve"])

    def test_up_stale_cache_refuses_before_evaluation_or_any_launch(self):
        options = launch.Options("recipe.toml", components=self.ref, selected_content=self.selected)
        stat = self.draft.stat()
        os.utime(self.draft, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100))
        with mock.patch.object(commands, "components_store", return_value=self.cache), \
                mock.patch.object(launch, "_backend", side_effect=AssertionError("no evaluation")), \
                mock.patch.object(core.subprocess, "run", side_effect=AssertionError("no process")), \
                self.no_content_reads(), self.assertRaisesRegex(reader.RecipeError, "PL_E_STALE"):
            launch.prepare(None, "synthetic", options, router="local", vram_cap_gib=None,
                           state_dir=self.root / "launch-state")
        self.assertFalse((self.root / "launch-state").exists())

    def test_header_cache_detects_nanosecond_changes(self):
        store = descriptors.PlacementStore(self.runtime / "descriptors")
        with mock.patch.object(modelfiles, "read", return_value=({}, [])):
            store.save(self.model)
        stat = self.model.stat()
        os.utime(self.model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100))
        with self.no_content_reads(), self.assertRaisesRegex(descriptors.DescriptorError, "changed"):
            store.load(self.model)
