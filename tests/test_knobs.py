from tests.portable_fixtures import read_fixture
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import importlib.util

from hyperloom import knobs, orchestrate as orch
from hyperloom.config import ConfigError, parse, preset
from hyperloom.render import launch_summary, render_processes
from tests.test_config import example, ROOT


FIXTURES = ROOT / "tests/fixtures/render"
_spec = importlib.util.spec_from_file_location("install_knobs_under_test", ROOT / "node" / "hyperloom_install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


class AdvancedKnobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyperloom knobs ")
        self.addCleanup(self.tmp.cleanup)
        self.obj = example()
        for mid in ("halo-1", "halo-2"):
            self.obj["assets"][mid]["calibration_tokens"]["control_path"] = str(FIXTURES / "calibration.tokens")

    def render(self, profile, advanced):
        self.obj["profiles"][profile]["advanced"] = advanced
        cluster = parse(self.obj)
        return render_processes(cluster, profile), launch_summary(cluster, profile)

    def test_real_presets_are_qualified_at_the_pin(self):
        for name in ("ds41-pair", "qwen-replicas"):
            data = preset(name)
            self.assertEqual(data["engine_sha"], knobs.engine_pin())
            for knob in knobs.check_table(data).values():
                self.assertEqual(knob["qualification"]["engine_sha"], knobs.engine_pin())

    def test_production_values_render_the_golden_line(self):
        for profile, advanced in (("ds41-pair", {"dspark_max_inflight": 4, "dspark_ranks": 2, "serve_sched": "interleave"}),
                                  ("qwen-replicas", {"mtp_ngram_draft": "auto"})):
            expected = json.loads(read_fixture(FIXTURES / f"{profile}.golden.json"))
            processes, launch = self.render(profile, advanced)
            self.assertEqual([asdict(p) for p in processes], expected)
            self.assertEqual((launch["production"], launch["advanced"]), (True, {}))

    def test_non_production_values_are_visible_overrides(self):
        processes, launch = self.render("ds41-pair", {"dspark_max_inflight": 0, "serve_sched": "off"})
        for spec in processes:
            self.assertEqual(spec.argv[-2:], ["--dspark-max-inflight", "0"])
            self.assertEqual(spec.env["STRIX_DS41_SERVE_SCHED"], "off")
        self.assertFalse(launch["production"])
        self.assertEqual(launch["advanced"], {"dspark_max_inflight": 0, "serve_sched": "off"})
        self.assertIn("docs/ds41-production-defaults.md", launch["qualification"]["serve_sched"]["evidence"])
        processes, launch = self.render("qwen-replicas", {"mtp_ngram_draft": "off"})
        self.assertEqual(processes[0].argv[-1], "--mtp-ngram-draft=off")
        self.assertFalse(launch["production"])

    def test_unqualified_values_and_names_refused(self):
        for advanced in ({"dspark_max_inflight": 2}, {"dspark_max_inflight": True}, {"serve_sched": "defer"},
                         {"cohort_rows": 16}, {"dspark_ranks": "1"}):
            with self.subTest(advanced=advanced), self.assertRaises(ConfigError):
                self.render("ds41-pair", advanced)
        with self.assertRaises(ConfigError):
            self.render("qwen-replicas", {"serve_sched": "off"})

    def test_pin_mismatch_refuses_advanced_knobs(self):
        with patch.object(knobs, "engine_pin", return_value="f" * 40):
            with self.assertRaisesRegex(ConfigError, "not qualified at the pinned engine"):
                self.render("ds41-pair", {"dspark_ranks": 1})
            self.render("ds41-pair", {})  # the production line needs no advanced qualification

    def test_requires_and_conflicts(self):
        data = deepcopy(preset("ds41-pair"))
        table = data["advanced"]
        table["dspark_ranks"]["requires"] = {"serve_sched": ["interleave"]}
        table["dspark_max_inflight"]["conflicts"] = {"dspark_ranks": [1]}
        knobs.validate({"dspark_ranks": 1}, data)
        with self.assertRaisesRegex(ConfigError, "requires serve_sched"):
            knobs.validate({"dspark_ranks": 1, "serve_sched": "off"}, data)
        with self.assertRaisesRegex(ConfigError, "conflicts with dspark_ranks=1"):
            knobs.validate({"dspark_ranks": 1, "dspark_max_inflight": 0}, data)
        table["dspark_ranks"]["requires"] = {"serve_sched": ["never"]}
        with self.assertRaisesRegex(ConfigError, "qualified values"):
            knobs.check_table(data)
        table["dspark_ranks"]["requires"] = {"dspark_ranks": [2]}
        with self.assertRaisesRegex(ConfigError, "itself"):
            knobs.check_table(data)

    def test_table_rejects_identity_reserved_and_unqualified_rows(self):
        base = preset("ds41-pair")
        row = base["advanced"]["dspark_ranks"]
        cases = {
            "transport": dict(row, target="--transport", values=["usb4gpu", "host"], production="usb4gpu"),
            "in_line": dict(row, target="--prefill-path"),
            "basic": dict(row, target="--slots"),
            "identity_env": dict(row, kind="env", target="STRIX_DS41_TRELLIS_SITES"),
            "transport_env": dict(row, kind="env", target="STRIX_DIST_USB4_EAGER"),
            "loader_env": dict(row, kind="env", target="LD_PRELOAD"),
            "other_pin": dict(row, qualification=dict(row["qualification"], engine_sha="0" * 40)),
            "no_evidence": dict(row, qualification=dict(row["qualification"], evidence=" ")),
            "no_production": dict(row, production=3),
            "one_value": dict(row, values=[2], production=2),
            "bad_kind": dict(row, kind="shell"),
            "bad_value": dict(row, values=[2, "1; rm"]),
        }
        for name, bad in cases.items():
            data = deepcopy(base)
            data["advanced"] = {"knob": bad}
            with self.subTest(name), self.assertRaises(ConfigError):
                knobs.check_table(data)


class CapabilityCheckTests(unittest.TestCase):
    """Advanced knob targets are checked statically in the built binary, and up refuses unchecked ones."""

    def test_install_checks_every_advanced_target(self):
        for kind in ("ds41-pair", "qwen-replicas"):
            data = preset(kind)
            checked = orch._required_flags(kind, [data["binary"], "other-target"])
            self.assertEqual(set(checked), {data["binary"]})
            for knob in data["advanced"].values():
                self.assertIn(knob["target"], checked[data["binary"]])
        self.assertIn("STRIX_DS41_SERVE_SCHED", orch._required_flags("ds41-pair", ["strix-deepseek41-serve"])["strix-deepseek41-serve"])

    def test_node_accepts_flags_and_env_names_only(self):
        for value in ("--dspark-ranks", "--mtp-ngram-draft", "STRIX_DS41_SERVE_SCHED"):
            self.assertTrue(install.checked_string(value), value)
        for value in ("-x", "--", "STRIX_", "LD_PRELOAD", "--a b", "STRIX_x", 7, None, "--" + "a" * 200):
            self.assertFalse(install.checked_string(value), value)

    def release_client(self, checked):
        reply = {"active": {"ds41-pair": "a" * 12}, "releases": [{"release_id": "a" * 12, "manifest": {
            "recipe": {"required_flags": {"strix-deepseek41-serve": checked}}}}]}
        return Mock(machine=SimpleNamespace(id="halo-1"), **{"call.return_value": reply})

    def test_up_refuses_a_knob_the_release_was_not_checked_for(self):
        machine = SimpleNamespace(id="halo-1", install_root="/opt/hyperloom/halo-1")
        settings = {"rank0": "halo-1", "rank1": "halo-2", "advanced": {"serve_sched": "off", "dspark_ranks": 2}}
        needs = orch._knob_targets(settings, "ds41-pair")
        self.assertEqual(needs, ("strix-deepseek41-serve", ["STRIX_DS41_SERVE_SCHED"]))
        with self.assertRaisesRegex(orch.OrchestrationError, "not checked for advanced knob target STRIX_DS41_SERVE_SCHED"):
            orch._release(self.release_client(["--rank"]), machine, "ds41-pair", None, needs=needs)
        self.assertEqual(orch._release(self.release_client(["--rank", "STRIX_DS41_SERVE_SCHED"]), machine,
                                       "ds41-pair", None, needs=needs), "a" * 12)
        # Production values render nothing new, so an older release still runs them.
        production = orch._knob_targets({"advanced": {"serve_sched": "interleave"}}, "ds41-pair")
        self.assertEqual(orch._release(self.release_client([]), machine, "ds41-pair", None, needs=production), "a" * 12)


if __name__ == "__main__":
    unittest.main()
