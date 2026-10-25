"""The observability pages rendered (with a minimal page shell) against documents the collectors produced from
partial engine answers: missing counts are shown as unavailable with why, never as a fabricated value."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import HERE, NODE1, Env, fixture

NODE = shutil.which("node")


def render(page, docs):
    answer = subprocess.run([NODE, str(HERE / "render.js")], input=json.dumps({"page": page, "docs": docs}),
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    if answer.returncode:
        raise AssertionError(answer.stderr)
    return answer.stdout


@unittest.skipUnless(NODE, "needs node to render the pages")
class PartialCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()
        self.env.launch("qwen", [("halo-1", NODE1, 18501)])
        self.env.router()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def collected(self, **speculative):
        status = fixture("engine-status-c1.json")
        for key, value in speculative.items():
            if value is None:
                status["speculative"].pop(key, None)
            else:
                status["speculative"][key] = value
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        return (observe.speculative_document(self.env.api, {}), observe.engines_document(self.env.api, {}))

    def test_missing_accepted_count(self):
        spec, engines = self.collected(accepted=None, proposed=10)
        row = spec["engines"][0]
        self.assertIsNone(row["acceptance"])
        self.assertIsNone(row["accepted_per_cycle"])
        self.assertIn("without an accepted count", row["why"]["acceptance"])
        page = render("speculative", {"observe/speculative": spec})
        self.assertIn("without an accepted count", page)
        self.assertNotIn("null", page)
        panel = render("engine-panel", {"observe/engines": engines})
        self.assertNotIn("null", panel)
        self.assertNotIn("0.00", panel)
        self.assertIn("without an accepted count", panel)
        self.assertIn("no accepted count", panel)

    def test_missing_proposed_count(self):
        spec, engines = self.collected(proposed=None)
        self.assertIn("without a proposed count", spec["engines"][0]["why"]["acceptance"])
        for page, docs in (("speculative", {"observe/speculative": spec}), ("engine-panel", {"observe/engines": engines})):
            self.assertNotIn("null", render(page, docs))

    def test_complete_counts(self):
        spec, engines = self.collected()
        page = render("speculative", {"observe/speculative": spec})
        self.assertIn("84 / 115", page)
        self.assertIn("84 / 115", render("engine-panel", {"observe/engines": engines}))


if __name__ == "__main__":
    unittest.main()
