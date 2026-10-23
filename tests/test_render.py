from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from telar.config import ConfigError, parse
from telar.render import render_processes, render_router, validate_router
from tests.test_config import example, ROOT


FIXTURES = ROOT / "tests/fixtures/render"


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="telar render ")
        self.addCleanup(self.tmp.cleanup)
        self.obj = example()
        self.key = Path(self.tmp.name) / "router host.key"
        self.key.write_text("synthetic-test-token\n", encoding="ascii")
        for mid in ("halo-1", "halo-2"):
            self.obj["assets"][mid]["calibration_tokens"]["control_path"] = str(FIXTURES / "calibration.tokens")
            self.obj["router"]["replica_api_key_files"][mid] = str(self.key)

    def test_qwen_receipt_golden_argv_and_explicit_env(self):
        expected = json.loads((FIXTURES / "qwen-replicas.golden.json").read_text())
        actual = render_processes(parse(self.obj), "qwen-replicas")
        self.assertEqual([asdict(p) for p in actual], expected)
        self.assertEqual(actual[0].argv[2:5], ["11", "22", "33"])
        self.assertEqual(len(actual[0].env), 19)  # rfix production replica set
        self.assertEqual(actual[0].argv[actual[0].argv.index("--http-default-max-tokens")+1], "256")

    def test_ds41_contract_golden_both_ranks(self):
        expected = json.loads((FIXTURES / "ds41-pair.golden.json").read_text())
        actual = render_processes(parse(self.obj), "ds41-pair")
        self.assertEqual([asdict(p) for p in actual], expected)

    def test_launch_path_is_an_immutable_manifest_release(self):
        cluster = parse(self.obj)
        path = render_processes(cluster, "qwen-replicas")[0].argv[0]
        self.assertIn("/releases/" + "0" * 12 + "/bin/", path)
        del self.obj["profiles"]["qwen-replicas"]["release_id"]
        with self.assertRaisesRegex(ConfigError, "release_id"):
            render_processes(parse(self.obj), "qwen-replicas")

    def test_router_real_pinned_parser_and_import_path_restored(self):
        before = sys.path.copy()
        obj = render_router(parse(self.obj), "qwen-replicas")
        parsed = validate_router(obj)
        self.assertEqual(sys.path, before)
        self.assertEqual(len(parsed.replicas), 2)
        self.assertEqual([r.max_inflight for r in parsed.replicas], [8, 8])
        self.assertEqual(parsed.prefill.policy, "auto")
        self.assertIsNotNone(parsed.prefill.node_profile)
        self.assertEqual(parsed.prefill.cache_block_tokens, 1024)
        self.assertEqual(parsed.replicas[0].api_key, "synthetic-test-token")

    def test_pair_router_has_rank_zero_only(self):
        obj = render_router(parse(self.obj), "ds41-pair")
        parsed = validate_router(obj)
        self.assertEqual([(r.id, r.port) for r in parsed.replicas], [("halo-1", 18431)])

    def test_non_loopback_requires_top_level_router_key(self):
        self.obj["router"].update(machine="halo-1")
        cluster = parse(self.obj)
        with self.assertRaisesRegex(ConfigError, "non-loopback"):
            render_router(cluster, "qwen-replicas")
        self.obj["router"]["api_key_file"] = str(self.key)
        router = render_router(parse(self.obj), "qwen-replicas")
        self.assertEqual(router["api_key_file"], str(self.key))
        self.assertEqual(validate_router(router).listen_address, "192.0.2.10")

    def test_ipv6_listen_and_remote_machine(self):
        self.obj["machines"][1]["http_address"] = "fd00::1"
        self.obj["router"].update(machine="halo-1", api_key_file=str(self.key))
        parsed = validate_router(render_router(parse(self.obj), "qwen-replicas"))
        self.assertEqual(parsed.listen_address, "fd00::1")
        self.obj["router"].update(machine="local", listen="[::1]:18601")
        self.assertEqual(validate_router(render_router(parse(self.obj), "qwen-replicas")).listen_port, 18601)

    def test_key_paths_for_router_and_workers_stay_separate(self):
        cluster = parse(self.obj)
        self.assertEqual(render_router(cluster, "qwen-replicas")["replicas"][0]["api_key_file"], str(self.key))
        argv = render_processes(cluster, "qwen-replicas")[0].argv
        self.assertEqual(argv[argv.index("--http-api-key-file")+1], "/opt/telar/secrets/engine.key")

    def test_capacity_overrides_are_tokenized_and_router_ports_match(self):
        for name, overrides in (("qwen-replicas", {"--http-port":18502,"--http-max-queue":12}),
                                ("ds41-pair", {"--http-port":18432,"--peer-timeout":90})):
            with self.subTest(name=name):
                self.obj["profiles"][name]["overrides"] = overrides
                cluster = parse(self.obj)
                for process in render_processes(cluster, name):
                    for flag, value in overrides.items():
                        self.assertEqual(process.argv[process.argv.index(flag)+1], str(value))
                        self.assertEqual(process.argv.count(flag), 1)
                self.assertEqual(render_router(cluster, name)["replicas"][0]["port"], overrides["--http-port"])

    def test_render_is_repeatable_and_does_not_mutate_cluster(self):
        cluster = parse(self.obj)
        before = deepcopy(cluster.raw)
        router = render_router(cluster, "qwen-replicas")
        router["prefill"]["node_profile"]["restore_s"] = 12
        self.assertEqual(render_router(cluster, "qwen-replicas")["prefill"]["node_profile"]["restore_s"], 0.01)
        self.assertEqual(render_processes(cluster, "qwen-replicas"), render_processes(cluster, "qwen-replicas"))
        self.assertEqual(cluster.raw, before)

    def test_unknown_profile_and_missing_assets_or_keys(self):
        with self.assertRaises(ConfigError):
            render_processes(parse(self.obj), "missing")
        for path in (("assets", "halo-1", "golden"), ("profiles", "qwen-replicas", "api_key_files"),
                     ("router", "replica_api_key_files", "halo-1")):
            with self.subTest(path=path):
                obj = deepcopy(self.obj)
                target = obj
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                with self.assertRaises(ConfigError):
                    if path[0] == "router":
                        render_router(parse(obj), "qwen-replicas")
                    else:
                        render_processes(parse(obj), "qwen-replicas")

    def test_pair_auto_policy_needs_explicit_measurements(self):
        del self.obj["router"]["node_profile"]
        with self.assertRaisesRegex(ConfigError, "node_profile"):
            render_router(parse(self.obj), "ds41-pair")
        self.obj["router"]["policy"] = "nodes"
        validate_router(render_router(parse(self.obj), "ds41-pair"))

    def test_calibration_file_is_bounded_validated_and_integrity_checked(self):
        for content in (b'', b'1 -2', b'1 --context 2', b'NaN', b'1,,', b'2147483648', b'\xff', b'1 ' * 140000):
            with self.subTest(content=content[:20]):
                obj = deepcopy(self.obj)
                path = Path(self.tmp.name) / "synthetic.tokens"
                path.write_bytes(content)
                obj["assets"]["halo-1"]["calibration_tokens"].update(
                    control_path=str(path), bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
                with self.assertRaises(ConfigError):
                    render_processes(parse(obj), "qwen-replicas")
        self.obj["assets"]["halo-1"]["calibration_tokens"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ConfigError, "mismatch"):
            render_processes(parse(self.obj), "qwen-replicas")

    def test_missing_calibration_file_is_actionable(self):
        self.obj["assets"]["halo-1"]["calibration_tokens"]["control_path"] = str(Path(self.tmp.name) / "missing")
        with self.assertRaisesRegex(ConfigError, "control_path"):
            render_processes(parse(self.obj), "qwen-replicas")

    def test_pinned_sidecar_and_golden_cannot_change_identity(self):
        for asset in ("sidecar", "golden"):
            obj = deepcopy(self.obj)
            obj["assets"]["halo-1"][asset]["sha256"] = "0" * 64
            with self.subTest(asset=asset), self.assertRaises(ConfigError):
                render_processes(parse(obj), "qwen-replicas")

    def test_tensor_meld_parser_rejects_invalid_rendered_contract(self):
        router = render_router(parse(self.obj), "qwen-replicas")
        del router["replicas"][0]["max_inflight"]
        with self.assertRaises(ValueError):
            validate_router(router)
        router = render_router(parse(self.obj), "qwen-replicas")
        del router["prefill"]["node_profile"]
        with self.assertRaises(ValueError):
            validate_router(router)


if __name__ == "__main__":
    unittest.main()
