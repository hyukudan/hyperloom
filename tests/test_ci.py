"""CI workflow hygiene: actions pinned by SHA, private submodule fetched with a scoped read-only secret."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FETCH = ROOT / "tools" / "ci-fetch-tensormeld.sh"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_every_action_is_pinned_by_commit_sha(self):
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.text, re.M)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_checkout_never_persists_credentials(self):
        self.assertEqual(self.text.count("uses: actions/checkout@"), self.text.count("persist-credentials: false"))

    def test_secret_only_in_the_submodule_fetch_steps(self):
        steps = re.split(r"\n\s*- ", self.text)
        with_secret = [step for step in steps if "secrets." in step]
        self.assertTrue(with_secret)
        for step in with_secret:
            self.assertIn("TENSORMELD_TOKEN: ${{ secrets.TENSORMELD_TOKEN }}", step)
            self.assertIn("run: bash tools/ci-fetch-tensormeld.sh", step)
        self.assertNotIn("git submodule update", self.text)

    def test_fetch_fails_clearly_without_the_secret_and_keeps_it_off_argv(self):
        script = FETCH.read_text(encoding="utf-8")
        self.assertIn('if [[ -z "${TENSORMELD_TOKEN:-}" ]]', script)
        self.assertIn("exit 1", script)
        self.assertIn("fine-grained", script)
        self.assertIn("GIT_CONFIG_KEY_0=", script)
        command = [line for line in script.splitlines() if line.startswith("git ")]
        self.assertEqual(command, ["git submodule update --init --depth 1 third_party/tensormeld"])


if __name__ == "__main__":
    unittest.main()
