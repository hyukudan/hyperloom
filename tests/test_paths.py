from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telar import paths


class RepositoryPathsTests(unittest.TestCase):
    def test_checkout_root_is_package_parent(self):
        self.assertEqual(paths.repo_root(), Path(paths.__file__).resolve().parent.parent)

    def test_incomplete_install_explains_editable_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(paths, "_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, r"node, versions.lock.*pip install -e"):
                    paths.repo_root()
                (root / "node").mkdir()
                with self.assertRaisesRegex(RuntimeError, "versions.lock"):
                    paths.repo_root()
                (root / "versions.lock").touch()
                self.assertEqual(paths.repo_root(), root)
