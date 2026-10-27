"""Keep the controller allow-list and the independently shipped node in sync."""
import ast
import importlib.util
from pathlib import Path
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

from hyperloom import orchestrate, remote


class VerbContractTests(unittest.TestCase):
    def test_the_node_tool_writes_no_bytecode_beside_itself(self):
        """A verb that imports an extension leaves no __pycache__ in the tool's directory (~/.hyperloom-boot)."""
        node = Path(__file__).resolve().parents[1] / "node"
        with tempfile.TemporaryDirectory() as directory:
            for name in os.listdir(node):
                if name == "hyperloom-node" or name.endswith(".py"):
                    shutil.copy2(node / name, Path(directory) / name)
            environment = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
            subprocess.run([sys.executable, str(Path(directory) / "hyperloom-node"), "turns"], input="{}",
                           capture_output=True, text=True, timeout=60, env=environment)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir() if not p.name.endswith(".py")
                                    and p.name != "hyperloom-node"), [])


    def test_orchestrator_transport_and_node_verbs_agree(self):
        node = Path(__file__).resolve().parents[1] / "node"
        tool = runpy.run_path(str(node / "hyperloom-node"))
        supported = set(tool["VERBS"])
        self.assertTrue({"hyperloom_install", "hyperloom_lease", "hyperloom_download", "hyperloom_service"}
                        <= set(tool["EXTENSIONS"]))
        for name in tool["EXTENSIONS"]:
            spec = importlib.util.spec_from_file_location(name + "_contract", node / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertFalse(supported & module.VERBS)
            supported.update(module.VERBS)
        self.assertEqual(set(remote.VERBS), supported)
        tree = ast.parse(Path(orchestrate.__file__).read_text(encoding="utf-8"))
        used = set()
        for call in ast.walk(tree):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "_call"):
                verb = call.args[1]
                self.assertIsInstance(verb, (ast.Constant, ast.IfExp))
                choices = (verb.body, verb.orelse) if isinstance(verb, ast.IfExp) else (verb,)
                for choice in choices:
                    self.assertIsInstance(choice, ast.Constant)
                    used.add(choice.value)
        self.assertTrue({"install", "job", "push-release", "lease", "cancel", "stop"} <= used)
        self.assertFalse(used - supported)
