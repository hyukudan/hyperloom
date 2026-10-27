"""The package sandbox applies to both test runners and inherited CLI environments."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import tests
from hyperloom import cli
from hyperloom.recipes import service
from hyperloom.web.api import API


class TestIsolationTests(unittest.TestCase):
    def test_default_config_audit_and_admission_are_isolated(self):
        with service.configured(None):
            self.assertTrue(service.config_dir().is_relative_to(tests._test_root))
        api = API()
        self.assertTrue(api.audit_path.is_relative_to(tests._test_root))
        self.assertEqual(os.environ["HYPERLOOM_ADMISSION_DB"], ":memory:")
        self.assertFalse(tests._test_root.is_relative_to(Path(__file__).resolve().parents[1]))

    def test_cli_child_inherits_isolation(self):
        child = subprocess.run([sys.executable, "-c",
            "import json, os; from hyperloom.cli import config_path; "
            "print(json.dumps([str(config_path()), os.environ['HYPERLOOM_ADMISSION_DB']]))"],
            check=True, capture_output=True, text=True, timeout=30)
        path, database = json.loads(child.stdout)
        self.assertEqual(Path(path), cli.config_path())
        self.assertEqual(database, ":memory:")

    def test_real_configuration_writes_fail_before_open(self):
        for root in tests._real_config_dirs:
            with self.subTest(root=root), self.assertRaisesRegex(AssertionError, "real per-user"):
                # The audit hook rejects this before the OS can open or create anything.
                with (Path(root) / "audit.jsonl").open("a", encoding="utf-8"):
                    self.fail("real audit was opened for writing")

    def test_real_configuration_mutations_are_guarded(self):
        for root in tests._real_config_dirs:
            target = str(Path(root) / "guard-probe")
            for event, args in (("os.mkdir", (target, 0o700, -1)), ("os.remove", (target, -1)),
                                ("os.rmdir", (target, -1)), ("os.rename", ("guard-probe", target, -1, -1)),
                                ("open", (target, None, os.O_RDWR)), ("os.utime", (target, None, None, -1)),
                                ("shutil.rmtree", (target, None)), ("sqlite3.connect", (target,))):
                with self.subTest(event=event), self.assertRaisesRegex(AssertionError, "real per-user"):
                    sys.audit(event, *args)

    def test_guard_allows_read_only_audits(self):
        for root in tests._real_config_dirs:
            sys.audit("open", str(Path(root) / "audit.jsonl"), "r", os.O_RDONLY)
