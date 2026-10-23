"""The install capability check must never execute an engine binary (it can open GPU or USB4 devices)."""
import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

NODE = Path(__file__).resolve().parents[1] / "node" / "hyperloom_install.py"
spec = importlib.util.spec_from_file_location("hyperloom_install_static", NODE)
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class StaticCapabilityTests(unittest.TestCase):
    def test_flags_present_missing_and_not_elf(self):
        with tempfile.TemporaryDirectory() as d:
            elf = Path(d, "bin")
            elf.write_bytes(b"\x7fELF" + b"\0" * 8 + b"--prefill-path\0--transport\0")
            self.assertTrue(install.static_capabilities(elf, ["--prefill-path", "--transport"])["ok"])
            r = install.static_capabilities(elf, ["--prefill-path", "--dspark"])
            self.assertFalse(r["ok"])
            self.assertEqual(r["missing"], ["--dspark"])
            script = Path(d, "script")
            script.write_bytes(b"#!/bin/sh\n--prefill-path")
            self.assertFalse(install.static_capabilities(script, ["--prefill-path"])["ok"])

    def test_build_never_runs_a_target_binary(self):
        tree = ast.parse(NODE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "version" and node.args:
                first = node.args[0]
                if isinstance(first, ast.List) and first.elts:
                    head = first.elts[0]
                    # version() may run toolchain tools (cmake, ninja, amdclang++), never str(binary).
                    self.assertFalse(isinstance(head, ast.Call) and getattr(head.func, "id", None) == "str"
                                     and any(getattr(a, "id", None) == "binary" for a in head.args),
                                     "install executes a built engine binary")


if __name__ == "__main__":
    unittest.main()
