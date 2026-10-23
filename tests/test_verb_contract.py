"""Keep the controller allow-list and the independently shipped node in sync."""
import ast
import importlib.util
from pathlib import Path
import runpy
import unittest

from hyperloom import orchestrate, remote


class VerbContractTests(unittest.TestCase):
    def test_orchestrator_transport_and_node_verbs_agree(self):
        node = Path(__file__).resolve().parents[1] / "node"
        supported = set(runpy.run_path(str(node / "hyperloom-node"))["VERBS"])
        for name in ("hyperloom_install", "hyperloom_lease", "hyperloom_service"):
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
