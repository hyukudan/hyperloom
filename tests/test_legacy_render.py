"""G6: every shipped legacy line stays equal to the independent eb9ae96 oracle."""
from tests.portable_fixtures import read_fixture
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/render/legacy-eb9ae96.json"


class LegacyRenderTests(unittest.TestCase):
    def test_frozen_legacy_argv_env_release_and_refusals(self):
        with tempfile.TemporaryDirectory(prefix="hl-g6-") as tmp:
            actual = Path(tmp) / "render.json"
            subprocess.run([sys.executable, str(ROOT / "tests/legacy_render.py"), "--source", str(ROOT),
                            "--output", str(actual), "--raw"], check=True, capture_output=True, text=True, timeout=60)
            self.assertEqual(actual.read_bytes(), read_fixture(FIXTURE).encode("utf-8"),
                             "legacy render differs from eb9ae96; report the diff before changing the oracle")

    def test_oracle_covers_every_shipped_preset_and_cap(self):
        doc = json.loads(read_fixture(FIXTURE))
        self.assertEqual(doc["oracle"], "eb9ae96")
        presets = {p.stem for p in (ROOT / "hyperloom/presets").glob("*.json")
                   if "argv" in json.loads(p.read_text(encoding="utf-8"))}
        profiles = {r["profile"] for r in doc["rows"]}
        self.assertEqual(profiles - {"ds41-rtx"}, presets - {"rtx-prefill-ds41"})
        for profile in profiles:
            self.assertEqual({r["vram_cap_gib"] for r in doc["rows"] if r["profile"] == profile}, {None, 90, 8})
        service = [r for r in doc["rows"] if r["profile"] == "ds41-rtx"]
        self.assertTrue(all("service" in r for r in service if r["vram_cap_gib"] != 8))
        self.assertIn("no placement at 8 GiB", service[-1]["refusal"]["message"])
