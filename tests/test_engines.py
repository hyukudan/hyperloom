"""Engine adapter registry, the w5/MiniMax presets, unpublished-source bundles, and the MiniMax pair lifecycle."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyperloom import bundles, config, engines, knobs, orchestrate as orch, render, secrets
from hyperloom.config import ConfigError, parse
from hyperloom.render import render_processes, render_router, validate_router
from tests.test_config import ROOT, example
from tests.test_orchestrate import RELEASE, FakeRemote
from tests.test_render import FIXTURES


NODE = ROOT / "node"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_engines_under_test", NODE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


node_install = _load("hyperloom_install")
lease = _load("hyperloom_lease")
NEW = ("qwen-replicas-w5", "ds41-pair-w5", "minimax-pair")
KEYS = {"halo-1": "/opt/hyperloom/secrets/engine.key", "halo-2": "/opt/hyperloom/secrets/engine.key"}


def cluster_obj():
    """The example cluster plus one profile per new preset (synthetic paths; MiniMax model directory asset)."""
    obj = example()
    for mid in ("halo-1", "halo-2"):
        obj["assets"][mid]["calibration_tokens"]["control_path"] = str(FIXTURES / "calibration.tokens")
        obj["assets"][mid]["mm_model"] = {"path": "/models/minimax-m3.1-flash-exl3", "bytes": 0, "sha256": "0" * 64}
    obj["profiles"]["qwen-replicas-w5"] = {"preset": "qwen-replicas-w5", "nodes": ["halo-1", "halo-2"],
                                           "api_key_files": dict(KEYS), "release_id": "0" * 12}
    obj["profiles"]["ds41-pair-w5"] = {"preset": "ds41-pair-w5", "rank0": "halo-1", "rank1": "halo-2",
                                       "api_key_files": dict(KEYS), "release_id": "0" * 12}
    obj["profiles"]["minimax-pair"] = {"rank0": "halo-1", "rank1": "halo-2", "release_id": "0" * 12}
    return obj


def git_available():
    return shutil.which("git") is not None


class RegistryTests(unittest.TestCase):
    def test_every_preset_has_a_family_adapter_and_its_own_pin(self):
        lock = engines.lock()
        for name in config.PRESET_NAMES:
            with self.subTest(name=name):
                adapter, data = engines.for_preset(name)
                self.assertEqual(adapter.binary, data["binary"])
                self.assertIn(adapter.binary, adapter.targets)
                engines.check_components(data)
                pins = engines.components(data)
                self.assertEqual(pins["engine"]["commit"], data["engine_sha"])
                self.assertEqual(knobs.engine_pin(data), data["engine_sha"])
                self.assertEqual(pins["tensormeld"]["commit"], lock["components"]["tensormeld"]["commit"])
                # Only qualified advanced knobs: each is recorded at the preset's own pin.
                for knob in knobs.check_table(data).values():
                    self.assertEqual(knob["qualification"]["engine_sha"], knobs.engine_pin(data))
                self.assertEqual(data["argv"][0], "{binary}")
                self.assertIn("{port}", data["argv"])
                self.assertIn(render.port_flag(data), data["knobs"])

    def test_original_presets_keep_the_lock_pins_and_recipe(self):
        lock = engines.lock()
        for name in ("qwen-replicas", "ds41-pair", "ds41-pair-long"):
            data = config.preset(name)
            self.assertNotIn("components", data)
            self.assertEqual(engines.components(data)["engine"]["commit"], lock["components"]["engine"]["commit"])
            self.assertEqual(engines.recipe_options(data, config.family(name)), {})

    def test_families_and_topologies(self):
        self.assertEqual({name: config.family(name) for name in config.PRESET_NAMES},
                         {"qwen-replicas": "qwen-replicas", "qwen-replicas-w5": "qwen-replicas",
                          "ds41-pair": "ds41-pair", "ds41-pair-long": "ds41-pair", "ds41-pair-w5": "ds41-pair",
                          "minimax-pair": "minimax-pair"})
        mm = engines.adapter("minimax-pair")
        self.assertEqual((mm.topology, mm.ready, mm.drain, mm.bind, mm.key_file, mm.layout_required),
                         ("pair", "minimax-stdout", "leader-first", "loopback", False, "1"))
        self.assertEqual(engines.adapter("ds41-pair").drain, "admin-drain")
        self.assertTrue(all(row.label.startswith("hl-") for row in engines.FAMILIES.values()))
        self.assertEqual(engines.members({"rank0": "a", "rank1": "b"}, "minimax-pair"), ["a", "b"])
        self.assertEqual(engines.router_members("minimax-pair", ["a", "b"]), ["a"])
        self.assertEqual(engines.router_members("qwen-replicas", ["a", "b"]), ["a", "b"])
        with self.assertRaises(ConfigError):
            engines.adapter("other")

    def test_new_preset_pins_and_build_options(self):
        w5 = config.preset("ds41-pair-w5")
        self.assertEqual(engines.components(w5)["strix-trellis"]["commit"], "c95a169523dbf21ec636aab803f29d2c1d8a5eb0")
        self.assertEqual(engines.recipe_options(w5, "ds41-pair"),
                         {"trellis_api": "dense_r1", "trellis_configure": ["-DSTRIX_TRELLIS_ENABLE_EXPERIMENTAL_TP=ON"]})
        mm = config.preset("minimax-pair")
        self.assertEqual(engines.components(mm)["strix-trellis"], {
            "repo": "https://github.com/hyukudan/strix-trellis", "commit_prefix": "92cfd46e",
            "note": mm["components"]["strix-trellis"]["note"]})
        options = engines.recipe_options(mm, "minimax-pair")
        self.assertEqual(options["trellis_mode"], "minimax")
        self.assertIn("-DSTRIX_FAMILY_MINIMAX=ON", options["configure"])
        self.assertNotIn("-DSTRIX_ENABLE_EXPERIMENTAL_Q4K_A8_RUNTIME=ON", options["configure"])
        qwen = config.preset("qwen-replicas-w5")
        self.assertEqual(engines.recipe_options(qwen, "qwen-replicas"), {})

    def test_component_pins_are_checked(self):
        data = config.preset("ds41-pair-w5")
        for bad in ({"engine": {"repo": "x", "commit": "abc"}}, {"engine": {"repo": "-x", "commit": "a" * 40}},
                    {"strix-trellis": {"repo": "x"}}, {"engine": {"repo": "x", "commit": "b" * 40}},
                    {"engine": {"repo": "x", "commit": data["engine_sha"], "extra": 1}}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                engines.check_components(dict(data, components=bad))
        with self.assertRaises(ConfigError):
            engines.recipe_options(dict(data, build={"trellis_api": "other"}), "ds41-pair")
        with self.assertRaises(ConfigError):
            engines.recipe_options(dict(data, build={"trellis_configure": ["-DX=1;rm"]}), "ds41-pair")
        with self.assertRaises(ConfigError):
            engines.recipe_options(dict(data, build={"trellis_api": "dense_r1"}), "qwen-replicas")


class PresetRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyperloom engines ")
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / "router.key"
        self.key.write_text("synthetic-test-token\n", encoding="ascii")
        self.obj = cluster_obj()

    def test_golden_renders_of_the_new_presets(self):
        cluster = parse(self.obj)
        for name in NEW:
            with self.subTest(name=name):
                expected = json.loads((FIXTURES / (name + ".golden.json")).read_text(encoding="utf-8"))
                self.assertEqual([asdict(p) for p in render_processes(cluster, name)], expected)

    def test_qwen_w5_is_the_rfix_line_and_ds41_w5_the_dense_trellis_identity(self):
        cluster = parse(self.obj)
        self.assertEqual(render_processes(cluster, "qwen-replicas-w5"), render_processes(cluster, "qwen-replicas"))
        old, new = render_processes(cluster, "ds41-pair"), render_processes(cluster, "ds41-pair-w5")
        self.assertEqual([p.argv for p in old], [p.argv for p in new])
        self.assertEqual({k: v for k, v in new[0].env.items() if k not in old[0].env},
                         {"STRIX_DS41_TRELLIS_DENSE_FULL": "1"})
        self.assertEqual(new[0].env["STRIX_DS41_TRELLIS_SITES"], "all")
        # No w5 code default is set: they are on when unset.
        self.assertFalse({"STRIX_DS41_BATCH_SHARED", "STRIX_DIST_CRC_PARALLEL", "STRIX_DS41_READY_LEAN"} & set(new[0].env))

    def test_minimax_pair_line(self):
        rank0, rank1 = render_processes(parse(self.obj), "minimax-pair")
        self.assertEqual((rank0.role, rank1.role, rank0.ready), ("rank0", "rank1", "minimax-stdout"))
        self.assertEqual(rank0.env, {})
        for rank, process in enumerate((rank0, rank1)):
            argv = process.argv
            self.assertEqual(argv[argv.index("--rank") + 1], str(rank))
            self.assertEqual(argv[argv.index("--bind") + 1], "127.0.0.1")
            self.assertEqual(argv[argv.index("--transport") + 1], "usb4-auto")
            self.assertNotIn("--http-api-key-file", argv)
        self.obj["profiles"]["minimax-pair"]["overrides"] = {"--port": 18442, "--max-tokens": 1024}
        argv = render_processes(parse(self.obj), "minimax-pair")[0].argv
        self.assertEqual(argv[argv.index("--port") + 1], "18442")
        self.assertEqual(argv[argv.index("--max-tokens") + 1], "1024")
        for bad in ({"--slots": 4}, {"--capacity": 16384}, {"--transport": 1}):
            self.obj["profiles"]["minimax-pair"]["overrides"] = bad
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse(self.obj)

    def test_minimax_router_runs_on_rank0_and_reaches_loopback(self):
        with self.assertRaisesRegex(ConfigError, "loopback only: run the router on halo-1"):
            render_router(parse(self.obj), "minimax-pair")
        self.obj["router"].update(machine="halo-2", api_key_file=str(self.key))
        with self.assertRaisesRegex(ConfigError, "loopback only"):
            render_router(parse(self.obj), "minimax-pair")
        self.obj["router"].update(machine="halo-1")
        router = render_router(parse(self.obj), "minimax-pair")
        self.assertEqual(router["replicas"], [{"id": "halo-1", "node": "halo-1", "address": "127.0.0.1",
                                               "port": 18441, "max_inflight": 8}])
        parsed = validate_router(router)
        self.assertEqual([(r.address, r.port, r.api_key) for r in parsed.replicas], [("127.0.0.1", 18441, None)])

    def test_w5_pair_router_is_rank0_with_its_key(self):
        for mid in ("halo-1", "halo-2"):
            self.obj["router"]["replica_api_key_files"][mid] = str(self.key)
        router = render_router(parse(self.obj), "ds41-pair-w5")
        self.assertEqual([(r["id"], r["address"], r["api_key_file"]) for r in router["replicas"]],
                         [("halo-1", "192.0.2.10", str(self.key))])

    def test_profile_shape_follows_the_topology(self):
        bad = deepcopy(self.obj)
        bad["profiles"]["minimax-pair"] = {"nodes": ["halo-1", "halo-2"]}
        with self.assertRaisesRegex(ConfigError, "rank0 and rank1"):
            parse(bad)
        bad = deepcopy(self.obj)
        bad["profiles"]["qwen-replicas-w5"]["rank0"] = "halo-1"
        with self.assertRaisesRegex(ConfigError, "replica profile uses nodes"):
            parse(bad)
        bad = deepcopy(self.obj)
        bad["profiles"]["minimax-pair"]["api_key_files"] = dict(KEYS)  # the engine takes no key
        with self.assertRaises(ConfigError):
            parse(bad)

    def test_launch_summary_names_family_and_preset(self):
        summary = render.launch_summary(parse(self.obj), "minimax-pair")
        self.assertEqual((summary["family"], summary["preset"], summary["production"]),
                         ("minimax-pair", "minimax-pair", True))
        self.assertEqual(summary["engine_sha"], config.preset("minimax-pair")["engine_sha"])

    def test_rtx_service_hands_off_only_at_its_own_engine_pin(self):
        from hyperloom import rtx
        self.obj["profiles"]["ds41-pair-w5"]["prefill_service"] = {
            "preset": "rtx-prefill-ds41", "machine": "workstation", "device": "rtx-pro"}
        self.obj["machines"][0]["devices"] = [{"id": "rtx-pro", "type": "nvidia", "uuid": "GPU-test"}]
        try:
            cluster = parse(self.obj)
        except ConfigError:
            return  # refused at parse time is acceptable too
        with self.assertRaisesRegex(ConfigError, "qualified at engine"):
            rtx._service(cluster, "ds41-pair-w5")


class BundleTests(unittest.TestCase):
    def setUp(self):
        if not git_available():
            self.skipTest("git unavailable")
        self.tmp = tempfile.TemporaryDirectory(prefix="hyperloom bundles ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "engine source"
        self.git("init", "--quiet", str(self.repo))
        for name, text in (("a.txt", "one"), ("b.txt", "two")):
            (self.repo / name).write_text(text, encoding="utf-8")
            self.git("-C", str(self.repo), "add", name)
            self.git("-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     "commit", "--quiet", "-m", name)
        self.pinned = self.git("-C", str(self.repo), "rev-parse", "HEAD~0")
        self.first = self.git("-C", str(self.repo), "rev-parse", "HEAD~1")
        # Unrelated refs that must never travel in a bundle.
        self.git("-C", str(self.repo), "branch", "side", self.first)
        self.git("-C", str(self.repo), "tag", "unrelated", self.first)
        self.refs = self.git("-C", str(self.repo), "for-each-ref")

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    def test_parse_sources(self):
        source = bundles.parse("engine=D:/src/engine@" + "a" * 40)
        self.assertEqual((source.name, source.directory, source.commit), ("engine", Path("D:/src/engine"), "a" * 40))
        self.assertIsNone(bundles.parse("strix-trellis=/src/a@b/c").commit)
        for bad in ("engine", "=dir", "Engine=dir", "engine=", "engine=@" + "a" * 40):
            with self.subTest(bad=bad), self.assertRaises(bundles.BundleError):
                bundles.parse(bad)
        with self.assertRaises(bundles.BundleError):
            bundles.parse_all(["engine=a", "engine=b"])

    def test_bundle_has_one_ref_and_leaves_the_source_untouched(self):
        source = bundles.parse("engine=" + str(self.repo))
        commit = bundles.resolve(source, {"commit": self.pinned})
        bundle = bundles.create(source, commit, self.base)
        self.assertEqual(bundles.heads(bundle.path), {bundles.REF: self.pinned})
        self.assertEqual(bundle.sha256, hashlib.sha256(bundle.path.read_bytes()).hexdigest())
        self.assertEqual(self.git("-C", str(self.repo), "for-each-ref"), self.refs)
        self.assertFalse((self.repo / ".git" / "objects" / "info" / "alternates").exists())
        # The node fetches exactly that commit from the staged bundle, never from a remote.
        root = (self.base / "install root").resolve()
        (root / "src").mkdir(parents=True)
        (root / "incoming").mkdir()
        shutil.copy(bundle.path, root / "incoming" / (commit + ".bundle"))
        item = {"repo": "https://example.invalid/engine", "commit": commit, "published": False,
                "bundle_sha256": bundle.sha256}
        sources = node_install.fetch_sources(root, {"engine": item})
        self.assertEqual(self.git("-C", str(sources["engine"]), "rev-parse", "HEAD"), commit)
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            node_install.fetch_sources(root, {"engine": dict(item, bundle_sha256="0" * 64)})

    def test_shallow_source_checkout_bundles_at_depth_one(self):
        shallow = self.base / "shallow source"
        self.git("clone", "--quiet", "--depth", "1", self.repo.resolve().as_uri(), str(shallow))
        source = bundles.parse("engine=" + str(shallow))
        bundle = bundles.create(source, bundles.resolve(source, {"commit": self.pinned}), self.base)
        root = (self.base / "shallow root").resolve()
        (root / "src").mkdir(parents=True)
        (root / "incoming").mkdir()
        shutil.copy(bundle.path, root / "incoming" / (self.pinned + ".bundle"))
        item = {"repo": "r", "commit": self.pinned, "published": False, "bundle_sha256": bundle.sha256}
        fetched = node_install.fetch_sources(root, {"engine": item})["engine"]
        self.assertEqual(self.git("-C", str(fetched), "rev-parse", "HEAD"), self.pinned)
        self.assertEqual((fetched / "b.txt").read_text(encoding="utf-8"), "two")
        # Fetching again (an interrupted or repeated install) is idempotent.
        node_install.fetch_sources(root, {"engine": item})

    def test_resolve_refuses_another_commit_and_honours_prefix_pins(self):
        source = bundles.parse("engine=" + str(self.repo))
        with self.assertRaisesRegex(bundles.BundleError, "pin"):
            bundles.resolve(bundles.parse(f"engine={self.repo}@{self.first}"), {"commit": self.pinned})
        with self.assertRaises(bundles.BundleError):
            bundles.resolve(source, {"commit": "f" * 40})
        self.assertEqual(bundles.resolve(bundles.parse(f"engine={self.repo}@{self.pinned}"),
                                         {"commit_prefix": self.pinned[:8]}), self.pinned)
        with self.assertRaisesRegex(bundles.BundleError, "prefix"):
            bundles.resolve(bundles.parse(f"engine={self.repo}@{self.first}"), {"commit_prefix": self.pinned[:8]})
        with self.assertRaises(bundles.BundleError):
            bundles.resolve(bundles.parse("engine=" + str(self.base / "missing")), {"commit": self.pinned})

    def test_node_accepts_only_a_verified_stage(self):
        source = bundles.parse("engine=" + str(self.repo))
        bundle = bundles.create(source, self.pinned, self.base)
        root = (self.base / "node root").resolve()
        (root / "incoming").mkdir(parents=True)
        prepared = node_install.bundle_prepare(root, {"commit": self.pinned})
        stage = Path(prepared["stage"])
        self.assertEqual(stage.parent, root / "incoming")
        payload = {"commit": self.pinned, "stage": str(stage), "sha256": bundle.sha256, "bytes": bundle.bytes}
        shutil.copy(bundle.path, stage)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            node_install.bundle_accept(root, dict(payload, sha256="0" * 64))
        self.assertFalse(stage.exists())  # a refused stage is removed
        shutil.copy(bundle.path, stage)
        with self.assertRaisesRegex(ValueError, "exactly"):
            node_install.bundle_accept(root, dict(payload, commit=self.first))
        shutil.copy(bundle.path, stage)
        accepted = node_install.bundle_accept(root, payload)
        self.assertEqual(Path(accepted["path"]), root / "incoming" / (self.pinned + ".bundle"))
        for bad in (dict(payload, stage=str(root / "elsewhere.bundle")), dict(payload, bytes=0),
                    dict(payload, commit="abc")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                node_install.bundle_accept(root, bad)

    def test_prepare_sweeps_stale_stages_and_discard_removes_one(self):
        root = (self.base / "sweep root").resolve()
        incoming = root / "incoming"
        incoming.mkdir(parents=True)
        old = incoming / (".stage-" + "1" * 32 + ".bundle")
        young = incoming / (".stage-" + "2" * 32 + ".bundle")
        accepted = incoming / (self.pinned + ".bundle")
        for path in (old, young, accepted):
            path.write_bytes(b"partial")
        hours_ago = time.time() - 2 * 3600
        os.utime(old, (hours_ago, hours_ago))
        os.utime(accepted, (hours_ago, hours_ago))
        prepared = node_install.bundle_prepare(root, {"commit": self.pinned})
        self.assertFalse(old.exists())  # an interrupted upload older than an hour
        self.assertTrue(young.exists())  # a concurrent upload in progress is kept
        self.assertTrue(accepted.exists())  # accepted bundles are not stages
        self.assertNotEqual(Path(prepared["stage"]), young)
        self.assertEqual(node_install.sweep_stages(incoming, now=time.time() + 2 * 3600), [young.name])
        stage = Path(prepared["stage"])
        stage.write_bytes(b"partial")
        self.assertEqual(node_install.bundle_discard(root, {"stage": str(stage)}), {"removed": True})
        self.assertFalse(stage.exists())
        self.assertEqual(node_install.bundle_discard(root, {"stage": str(stage)}), {"removed": False})
        for bad in (str(accepted), str(root / ("x.stage-" + "3" * 32 + ".bundle"))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                node_install.bundle_discard(root, {"stage": bad})
        self.assertTrue(accepted.exists())

    def test_install_payload_validation_for_unpublished_components(self):
        payload = {"components": {"engine": {"repo": "r", "commit": "a" * 40, "published": False,
                                             "bundle_sha256": "b" * 64}},
                   "recipe": {"configure": [], "targets": ["t"], "ctest_regex": "x", "trellis": False,
                              "trellis_mode": "minimax", "trellis_api": "dense_r1",
                              "trellis_configure": ["-DSTRIX_TRELLIS_ENABLE_EXPERIMENTAL_TP=ON"]}}
        node_install.validate_install(payload)
        for change in ({"published": True}, {"bundle_sha256": "x"}, {"other": 1}):
            bad = deepcopy(payload)
            bad["components"]["engine"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                node_install.validate_install(bad)
        for change in ({"trellis_mode": "other"}, {"trellis_api": "v99"}, {"trellis_configure": ["-DA=$(x)"]}):
            bad = deepcopy(payload)
            bad["recipe"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                node_install.validate_install(bad)


class UploadRemote(FakeRemote):
    uploads = []

    def upload(self, local, remote_path, timeout=3600):
        self.calls.append((self.machine.id, "upload", {"stage": remote_path,
                                                         "sha256": hashlib.sha256(Path(local).read_bytes()).hexdigest()}))

    def call(self, verb, payload, timeout=60):
        if verb == "bundle-prepare":
            self.calls.append((self.machine.id, verb, deepcopy(payload)))
            return {"stage": payload["root"] + "/incoming/.stage-" + "c" * 32 + ".bundle"}
        if verb == "bundle-accept":
            self.calls.append((self.machine.id, verb, deepcopy(payload)))
            return {"path": payload["root"] + "/incoming/" + payload["commit"] + ".bundle"}
        if verb == "releases":
            self.calls.append((self.machine.id, verb, deepcopy(payload)))
            components = {"engine": {"repo": "r", "commit": "a" * 40, "published": False, "bundle_sha256": "b" * 64},
                          "tensormeld": {"repo": "r", "commit": "c" * 40}}
            return {"active": {}, "releases": [{"release_id": RELEASE, "manifest": {
                "components": components, "runtime_env": {}, "recipe": {}}}]}
        return super().call(verb, payload, timeout)


class LifecycleTests(unittest.TestCase):
    """install (with a bundle) and up/down of the new presets through a fake node transport."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyperloom engine lifecycle ")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.calls, self.leases = [], {}
        obj = cluster_obj()
        for row in obj["machines"][1:]:
            row.update(lab_dir="/opt/lab", peer_ssh={"halo-1": "halo-1", "halo-2": "halo-2"})
        for name in NEW:
            obj["profiles"][name]["release_id"] = RELEASE
        self.cluster = parse(obj)
        patches = [
            patch.object(secrets, "runtime_dir", return_value=self.directory / "run"),
            patch.object(secrets, "_directory", return_value=self.directory / "stored"),
            patch.object(orch.remote, "Remote", side_effect=lambda m, k: UploadRemote(m, self.calls, self.leases, set())),
            patch.object(orch, "_start_up_watch"),
            patch.object(orch.assets, "inspect", return_value=[]),
            patch.object(orch, "_start_local_router", side_effect=lambda *a, **k: {"machine": "local",
                         "directory": str(self.directory), "endpoint": a[1]["listen"]}),
            patch.object(orch, "_stop_local_router"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def verbs(self):
        return [verb for _, verb, _ in self.calls]

    def test_minimax_install_needs_a_trellis_source_before_any_node_work(self):
        with self.assertRaisesRegex(orch.OrchestrationError, "pins only 92cfd46e.*--source strix-trellis"):
            orch.install(self.cluster, "halo-1", profile="minimax-pair")
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(ValueError, "names no component"):
            orch.install(self.cluster, "halo-1", profile="minimax-pair",
                         sources={"other": bundles.Source("other", Path("."), None)})
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(git_available(), "git unavailable")
    def test_install_with_a_source_stages_the_bundle_before_the_build(self):
        repo = self.directory / "w5 source"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        run = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
        run("init", "--quiet", str(repo))
        (repo / "f").write_text("x", encoding="utf-8")
        run("-C", str(repo), "add", "f")
        run("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "x")
        commit = run("-C", str(repo), "rev-parse", "HEAD")
        data = deepcopy(config.preset("ds41-pair-w5"))
        data["components"]["engine"]["commit"] = data["engine_sha"] = commit
        real = config.preset
        with patch.object(orch.render, "preset", side_effect=lambda n: data if n == "ds41-pair-w5" else real(n)):
            release = orch.install(self.cluster, "halo-1", profile="ds41-pair-w5",
                                   sources={"engine": bundles.parse("engine=" + str(repo))})
        self.assertEqual(release, RELEASE)
        verbs = self.verbs()
        self.assertLess(verbs.index("bundle-prepare"), verbs.index("upload"))
        self.assertLess(verbs.index("upload"), verbs.index("bundle-accept"))
        self.assertLess(verbs.index("bundle-accept"), verbs.index("install"))
        accept = next(p for _, v, p in self.calls if v == "bundle-accept")
        upload = next(p for _, v, p in self.calls if v == "upload")
        self.assertEqual((accept["commit"], accept["sha256"]), (commit, upload["sha256"]))
        payload = next(p for _, v, p in self.calls if v == "install")
        self.assertEqual(payload["components"]["engine"], {"repo": data["components"]["engine"]["repo"], "commit": commit,
                                                           "published": False, "bundle_sha256": accept["sha256"]})
        self.assertEqual(payload["components"]["strix-trellis"]["commit"], "c95a169523dbf21ec636aab803f29d2c1d8a5eb0")
        self.assertNotIn("published", payload["components"]["tensormeld"])
        recipe = payload["recipe"]
        self.assertEqual((recipe["trellis"], recipe["trellis_api"], recipe["targets"]),
                         (True, "dense_r1", ["strix-deepseek41-serve"]))
        self.assertIn("--dspark-vocab", recipe["required_flags"]["strix-deepseek41-serve"])
        node_install.validate_install(payload)

    def test_original_presets_build_exactly_mains_recipe(self):
        """qwen-replicas, ds41-pair and ds41-pair-long declare no components: their install payload (recipe with
        required_flags and options, and components) is pinned to what main computes, so their release ids stay
        main's. The expected values were computed by main's install path (tests/fixtures/main_recipes.json)."""
        expected = json.loads((ROOT / "tests" / "fixtures" / "main_recipes.json").read_text(encoding="utf-8"))["presets"]
        self.assertEqual(sorted(expected), ["ds41-pair", "ds41-pair-long", "qwen-replicas"])
        obj = cluster_obj()
        for row in obj["machines"][1:]:
            row.update(lab_dir="/opt/lab", peer_ssh={"halo-1": "halo-1", "halo-2": "halo-2"})
        obj["profiles"]["ds41-pair-long"] = {"preset": "ds41-pair-long", "rank0": "halo-1", "rank1": "halo-2",
                                             "api_key_files": dict(KEYS), "release_id": "0" * 12}
        cluster = parse(obj)
        for name, row in expected.items():
            with self.subTest(preset=name):
                self.calls.clear()
                orch.install(cluster, "halo-1", profile=name)
                payload = next(p for _, v, p in self.calls if v == "install")
                self.assertEqual(payload["recipe"], row["recipe"])
                self.assertEqual(payload["components"], row["components"])
        settings = {"advanced": {"serve_sched": "interleave"}}
        self.assertEqual(orch._knob_targets(settings, "ds41-pair-long"), orch._knob_targets(settings, "ds41-pair"))
        self.assertEqual(orch._recipe_preset("ds41-pair-long"), "ds41-pair")
        self.assertEqual(orch._recipe_preset("ds41-pair-w5"), "ds41-pair-w5")

    @unittest.skipUnless(git_available(), "git unavailable")
    def test_failed_upload_discards_its_stage_and_never_builds(self):
        repo = self.directory / "w5 failed source"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        run = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
        run("init", "--quiet", str(repo))
        (repo / "f").write_text("x", encoding="utf-8")
        run("-C", str(repo), "add", "f")
        run("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "x")
        commit = run("-C", str(repo), "rev-parse", "HEAD")
        data = deepcopy(config.preset("ds41-pair-w5"))
        data["components"]["engine"]["commit"] = data["engine_sha"] = commit
        real = config.preset
        failure = orch.remote.RemoteError("scp: connection lost")
        with patch.object(orch.render, "preset", side_effect=lambda n: data if n == "ds41-pair-w5" else real(n)),                 patch.object(UploadRemote, "upload", side_effect=failure):
            with self.assertRaisesRegex(orch.remote.RemoteError, "connection lost"):
                orch.install(self.cluster, "halo-1", profile="ds41-pair-w5",
                             sources={"engine": bundles.parse("engine=" + str(repo))})
        prepare = next(p for _, v, p in self.calls if v == "bundle-prepare")
        discard = next(p for _, v, p in self.calls if v == "bundle-discard")
        self.assertEqual(discard, {"root": prepare["root"], "stage": prepare["root"] + "/incoming/.stage-" + "c" * 32 + ".bundle"})
        self.assertNotIn("bundle-accept", self.verbs())
        self.assertNotIn("install", self.verbs())

    def test_qwen_w5_install_recipe_and_published_fetch(self):
        orch.install(self.cluster, "halo-1", profile="qwen-replicas-w5")
        payload = next(p for _, v, p in self.calls if v == "install")
        self.assertEqual(payload["components"]["engine"], {"repo": "https://github.com/hyukudan/strix-halo-engine-lab",
                                                           "commit": "96123427a996c9e3cacd6aba148f149b85b09d9e"})
        self.assertEqual(payload["recipe"]["configure"], orch._versions()["toolchains"]["hip_configure"])
        self.assertFalse(payload["recipe"]["trellis"])
        self.assertNotIn("bundle-prepare", self.verbs())

    def test_minimax_up_refuses_a_router_off_rank0(self):
        for router in ("local", "halo-2"):
            with self.subTest(router=router), self.assertRaisesRegex(ValueError, "--router halo-1"):
                orch.up(self.cluster, "minimax-pair", router=router)
        self.assertEqual(self.calls, [])

    def test_minimax_pair_up_and_down(self):
        state = orch.up(self.cluster, "minimax-pair", router="halo-1")
        leases = [p for _, v, p in self.calls if v == "lease"]
        engine, router = leases
        self.assertRegex(engine["label"], r"^hl-mm-[0-9a-f]{4}$")
        self.assertEqual((engine["kind"], engine["layout_required"], engine["peer_ssh"]), ("pair", "1", "halo-2"))
        self.assertEqual([p["name"] for p in engine["processes"]], ["rank0", "rank1"])
        self.assertEqual([p["machine"] for p in engine["processes"]], ["local", "peer"])
        for process in engine["processes"]:
            self.assertEqual(process["ready"], "minimax-stdout")
            self.assertEqual(process["health_url"], "http://127.0.0.1:18441/health")
            self.assertIsNone(process["key_file"])
            if os.name == "posix":  # node paths are POSIX paths
                lease.validate_process(process)
        if os.name == "posix":
            lease.validate_spec(engine)
        # No engine key is pushed (the engine takes none); only the router's own LAN key on rank 0's machine.
        secrets_put = [(mid, p["name"]) for mid, v, p in self.calls if v == "put-secret"]
        self.assertEqual(secrets_put, [("halo-1", orch._secret_name("minimax-pair", "router"))])
        self.assertEqual(router["kind"], "none")
        rendered = json.loads(router["processes"][0]["argv"][4])
        self.assertEqual(rendered["replicas"][0]["address"], "127.0.0.1")
        self.assertEqual(state["launch"]["unpublished_sources"], ["engine"])
        self.assertEqual(state["launch"]["family"], "minimax-pair")
        status = orch.status("minimax-pair")
        self.assertEqual(status["unpublished_sources"], ["engine"])
        self.calls.clear()
        orch.down("minimax-pair")
        self.assertEqual(self.verbs().count("stop"), 2)
        self.assertFalse(orch._state_path("minimax-pair").exists())

    def test_ds41_w5_pair_up_uses_the_ds41_adapter(self):
        orch.up(self.cluster, "ds41-pair-w5")
        engine = next(p for _, v, p in self.calls if v == "lease")
        self.assertRegex(engine["label"], r"^hl-ds41-[0-9a-f]{4}$")
        self.assertEqual([p["ready"] for p in engine["processes"]], ["ds41-json", "ds41-json"])
        self.assertEqual({p["health_url"] for p in engine["processes"]}, {"http://192.0.2.10:18431/health"})
        self.assertTrue(all(p["key_file"] for p in engine["processes"]))
        orch.down("ds41-pair-w5")

    def test_qwen_w5_replicas_take_single_turns(self):
        orch.up(self.cluster, "qwen-replicas-w5")
        payloads = [p for _, v, p in self.calls if v == "lease"]
        self.assertEqual([p["kind"] for p in payloads], ["single", "single"])
        self.assertEqual([p["processes"][0]["ready"] for p in payloads], ["qwen-stderr", "qwen-stderr"])
        orch.down("qwen-replicas-w5")


class MinimaxNodeTests(unittest.TestCase):
    """Readiness and stop order of the MiniMax pair on the node (apps/minimax_serve_main.cpp events)."""

    def proc(self, rank):
        return {"name": f"rank{rank}", "machine": "local", "argv": ["/opt/r/bin/strix-minimax-serve", "/m", "--rank", str(rank)],
                "env": {}, "ready": "minimax-stdout", "health_url": "http://127.0.0.1:18441/health", "ready_timeout_s": 600}

    def test_rank0_ready_line_and_rank1_load_line(self):
        rank0 = ("connected: rank 0 transport usb4-auto\nrank 0: load 44.0 s, device used 87.4 GiB\n"
                 "READY rank 0 http://127.0.0.1:18441 model minimax-m3.1-flash slots 8 context 8256\n")
        rank1 = "connected: rank 1 transport usb4-auto\nrank 1: load 44.6 s, device used 87.4 GiB\n"
        self.assertTrue(lease.adapter_ready("minimax-stdout", rank0, "", 0))
        self.assertFalse(lease.adapter_ready("minimax-stdout", "", rank0, 0))  # stdout only
        self.assertFalse(lease.adapter_ready("minimax-stdout", rank0.split("READY")[0], "", 0))
        self.assertTrue(lease.adapter_ready("minimax-stdout", rank1, "", 1))
        self.assertFalse(lease.adapter_ready("minimax-stdout", "connected: rank 1 transport usb4-auto\n", "", 1))
        self.assertFalse(lease.adapter_ready("minimax-stdout", rank1, "", 0))
        self.assertIn("minimax-stdout", lease.READY_ADAPTERS)

    def test_readiness_also_needs_rank0_health(self):
        child = lease.Child(Path("/opt/r"), "abc", self.proc(1))
        with patch.object(child, "logs", return_value=("rank 1: load 1.0 s, device used 1 GiB\n", "")):
            with patch.object(lease, "http", return_value=False):
                self.assertFalse(child.check_ready())
            with patch.object(lease, "http", return_value=True) as http:
                self.assertTrue(child.check_ready())
                http.assert_called_with("http://127.0.0.1:18441/health")

    def test_rank0_is_stopped_first_and_waited_for(self):
        events = []
        children = []
        rank1 = lease.Child(Path("/opt/r"), "abc", self.proc(1))
        rank1.peer = True
        rank1.process = Mock(pid=201, poll=Mock(side_effect=lambda: 0 if ("terminate", 201) in events else None), stdin=None)
        rank0 = lease.Child(Path("/opt/r"), "abc", self.proc(0))
        polls = iter([None, None, 0])
        rank0.process = Mock(pid=200, poll=Mock(side_effect=lambda: next(polls, 0)))
        children = [rank0, rank1]
        rank1.terminate = lambda: events.append(("terminate", 201))
        with patch.object(lease, "http", side_effect=lambda *a, **k: events.append("drain")), \
                patch.object(lease, "group_signal", side_effect=lambda p, sig: events.append((p.pid, sig))), \
                patch.object(lease, "signal", SimpleNamespace(**dict(vars(signal), SIGKILL=9))), \
                patch.object(lease.time, "sleep"):
            lease.stop_children(children)
        self.assertNotIn("drain", events)  # no HTTP drain route in the MiniMax serve
        self.assertEqual(events[0], (200, signal.SIGTERM))
        self.assertEqual(events[1], ("terminate", 201))
        self.assertEqual([e[0] for e in events[2:]], [200, 201])


if __name__ == "__main__":
    unittest.main()
