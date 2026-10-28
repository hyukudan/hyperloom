from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from hyperloom.config import ConfigError, load, parse


ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / "examples/cluster.example.json").read_text(encoding="utf-8"))


class ConfigTests(unittest.TestCase):
    def test_lab_fields_share_one_schema_for_parse_load_and_cli(self):
        from contextlib import redirect_stdout
        import io
        from hyperloom import cli, orchestrate
        obj = example()
        obj["machines"][1].update(lab_dir="/opt/lab", peer_ssh={"halo-2": "operator@halo-2"})
        parsed = parse(obj)
        self.assertEqual(parsed.machines["halo-1"].lab_dir, "/opt/lab")
        self.assertEqual(parsed.machines["halo-1"].peer_ssh, {"halo-2": "operator@halo-2"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            path.write_text(json.dumps(obj), encoding="utf-8")
            self.assertEqual(load(path), parsed)
            self.assertEqual(orchestrate.load_cluster(path), parsed)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["config", "validate", str(path)]), 0)

    def test_lab_fields_reject_invalid_paths_peers_and_aliases(self):
        bad = [("lab_dir", value) for value in (None, "relative", "/opt/../lab", "D:\\lab", "/opt\\lab", "/opt/lab\n")]
        bad += [("peer_ssh", value) for value in (None, [], {"unknown": "halo-2"},
                {"halo-2": "-bad"}, {"halo-2": "halo-2 extra"}, {"halo-2": "halo-2\n"},
                {"halo-2": "halo-2\x00"}, {"halo-2": 2}, {"halo-2": ""},
                {"halo-2": "halo-2;command"}, {"halo-2": "operator@@halo-2"})]
        for key, value in bad:
            obj = example()
            obj["machines"][1][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                parse(obj)

    def test_example_and_machine_interface(self):
        cluster = load(ROOT / "examples/cluster.example.json")
        self.assertEqual(len(cluster.machines), 3)
        worker = cluster.machines["halo-1"]
        self.assertEqual((worker.os, worker.access, worker.ssh_port), ("linux", "ssh", 22))
        self.assertTrue(worker.lab_mode)
        self.assertEqual(worker.devices[0]["id"], "gpu")

    def test_parse_copies_input(self):
        obj = example()
        cluster = parse(obj)
        obj["router"]["max_inflight"] = 1
        self.assertEqual(cluster.router["max_inflight"], 8)

    def test_unknown_keys_at_every_level(self):
        paths = [(), ("machines", 0), ("machines", 1, "devices", 0), ("assets", "halo-1", "model"),
                 ("profiles", "qwen-replicas"), ("router",), ("router", "limits"),
                 ("router", "node_profile"), ("pc",), ("pc", "cuda")]
        for path in paths:
            with self.subTest(path=path):
                obj = example()
                target = obj
                for key in path:
                    target = target[key]
                target["unexpected"] = "value"
                with self.assertRaises(ConfigError):
                    parse(obj)

    def test_duplicate_json_keys_at_every_depth(self):
        for raw in ('{"schema":1,"schema":2}', '{"router":{"limits":{"max_waiting":1,"max_waiting":2}}}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cluster.json"
                path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "duplicate"):
                    load(path)

    def test_bad_json_encoding_constants_and_size(self):
        for content in (b'\xff', b'{', b'{"value":NaN}', b' ' * (256 * 1024 + 1)):
            with self.subTest(content=content[:20]), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cluster.json"
                path.write_bytes(content)
                with self.assertRaises(ConfigError):
                    load(path)

    def test_missing_file_is_config_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ConfigError):
            load(Path(tmp) / "missing.json")

    def test_addresses_are_numeric_private_or_loopback(self):
        for value in ("127.0.0.1", "192.0.2.10", "198.51.100.10", "203.0.113.10", "::1", "fd00::1"):
            obj = example()
            obj["machines"][1]["http_address"] = value
            self.assertEqual(parse(obj).machines["halo-1"].http_address, value)
        for value in ("halo-1", "0" * 4, "::", "ff02::1", "fe80::1", "::ffff:0:0",
                      "::ffff:7f00:1", "::7f00:1", "100::1", "2001::1", "2002::1",
                      "127.0.0.1:80", "::1%halo-1"):
            with self.subTest(value=value):
                obj = example()
                obj["machines"][1]["http_address"] = value
                with self.assertRaises(ConfigError):
                    parse(obj)

    def test_release_id_is_twelve_lowercase_hex_characters(self):
        obj = example()
        for profile in obj["profiles"].values():
            profile["release_id"] = "abcdef012345"
        self.assertEqual(parse(obj).profiles["qwen-replicas"]["release_id"], "abcdef012345")
        for value in ("a" * 64, "a" * 11, "A" * 12, "g" * 12):
            obj["profiles"]["qwen-replicas"]["release_id"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "12 lowercase hex"):
                parse(obj)

    def test_ssh_target_safety(self):
        for value in ("-halo-1", "halo-1 halo-2", "halo-1\n", "halo-1\x00", "halo-1\x7f", "halo-1\u2003", "halo-1\x80", "halo-1\u200b"):
            with self.subTest(value=value):
                obj = example()
                obj["machines"][1]["ssh_target"] = value
                with self.assertRaises(ConfigError):
                    parse(obj)
        for value in ("halo-1", "operator@halo-1"):
            obj = example()
            obj["machines"][1]["ssh_target"] = value
            parse(obj)

    def test_absolute_paths_for_correct_host_os(self):
        for index, path in ((0, "relative"), (0, "C:relative"), (1, "relative"), (1, "C:/hyperloom")):
            with self.subTest(index=index, path=path):
                obj = example()
                obj["machines"][index]["install_root"] = path
                with self.assertRaises(ConfigError):
                    parse(obj)
        obj = example()
        obj["machines"][0]["install_root"] = "C:/path with spaces/hyperloom"
        parse(obj)

    def test_duplicate_or_missing_references(self):
        mutations = [lambda o: o["machines"].append(deepcopy(o["machines"][1])),
                     lambda o: o["profiles"]["qwen-replicas"].update(nodes=["halo-1", "halo-1"]),
                     lambda o: o["profiles"]["qwen-replicas"].update(nodes=["missing"]),
                     lambda o: o["profiles"]["ds41-pair"].update(rank1="halo-1"),
                     lambda o: o["machines"][1]["devices"].append({"id":"gpu"}),
                     lambda o: o["router"].update(machine="missing")]
        for change in mutations:
            obj = example()
            change(obj)
            with self.subTest(change=change), self.assertRaises(ConfigError):
                parse(obj)

    def test_types_versions_and_bounds(self):
        cases = [((), "schema", "hyperloom/cluster-v2"), ((), "machines", []),
                 (("machines", 1), "ssh_port", True), (("machines", 1), "lab_mode", "true"),
                 (("machines", 1), "os", "other"), (("machines", 1), "access", "other"),
                 (("router",), "max_inflight", 65), (("router",), "balance_active_decodes", 1),
                 (("router",), "machine", []), (("router",), "machine", {}),
                 (("router",), "policy", "rtx"), (("router", "limits"), "max_connections", False),
                 (("router", "limits"), "queue_timeout_s", float("inf")),
                 (("assets", "halo-1", "model"), "bytes", -1),
                 (("assets", "halo-1", "model"), "sha256", "x" * 64),
                 (("profiles", "qwen-replicas"), "receipt", "unqualified"),
                 (("profiles", "qwen-replicas"), "release_id", "../unqualified"),
                 (("profiles", "qwen-replicas"), "port", 65536),
                 (("pc",), "ui_listen", "192.0.2.10:18700")]
        for path, key, value in cases:
            with self.subTest(path=path, key=key, value=value):
                obj = example()
                target = obj
                for part in path:
                    target = target[part]
                target[key] = value
                with self.assertRaises(ConfigError):
                    parse(obj)

    def test_capacity_overrides_only(self):
        for profile, valid in (("qwen-replicas", {"--context":12288, "--http-max-queue":0,
                                               "--http-max-connections":32, "--http-default-max-tokens":128,
                                               "--http-port":18502}),
                               ("ds41-pair", {"--context":4096, "--slots":4, "--peer-timeout":90,
                                              "--default-max-tokens":128, "--http-port":18432})):
            obj = example()
            obj["profiles"][profile]["overrides"] = valid
            parse(obj)
        bad = [{"LD_PRELOAD":"file"}, {"PYTHONPATH":"file"}, {"env":{}},
               {"STRIX_DS41_TRELLIS":"file"}, {"CUDA_VISIBLE_DEVICES":"0"},
               {"--transport":"emulate"}, {"--mtp-spec-min-p":0.5}, {"--context":8192},
               {"--http-port":True}, {"--http-max-queue":-1}]
        for overrides in bad:
            for name in ("qwen-replicas", "ds41-pair"):
                with self.subTest(overrides=overrides, name=name):
                    obj = example()
                    obj["profiles"][name]["overrides"] = overrides
                    with self.assertRaises(ConfigError):
                        parse(obj)

    def test_legacy_nodes_and_inline_secrets_rejected(self):
        for field in ("nodes", "api_key", "password", "env"):
            obj = example()
            obj[field] = "synthetic"
            with self.assertRaises(ConfigError):
                parse(obj)


    def test_the_ledger_and_the_mqtt_broker_are_configuration(self):
        obj = example()
        ledger = str(Path(tempfile.gettempdir()) / "coordination.md")
        obj.setdefault("pc", {}).update(turn_ledger=ledger, alert_local_ports=[8090],
                                        mqtt_broker={"host": "broker.lan", "port": 8883, "tls": True})
        cluster = parse(obj)
        self.assertEqual(cluster.pc["alert_local_ports"], [8090])
        self.assertEqual((cluster.pc["turn_ledger"], cluster.pc["mqtt_broker"]["host"]),
                         (ledger, "broker.lan"))
        for key, value in (("turn_ledger", "relative.md"), ("mqtt_broker", {"host": "a b"}),
                           ("mqtt_broker", {"host": "x", "port": 0}), ("mqtt_broker", {"host": "x", "tls": "yes"}),
                           ("mqtt_broker", {"host": "x", "password": "p"}), ("mqtt_broker", {}),
                           ("alert_local_ports", [0]), ("alert_local_ports", "8090"),
                           ("alert_local_ports", list(range(1, 18)))):
            bad = example()
            bad.setdefault("pc", {})[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                parse(bad)

if __name__ == "__main__":
    unittest.main()
