"""No control-machine subprocess may open a console window on Windows (user-visible flashing).

Every subprocess.Popen/run/call/check_call/check_output in hyperloom/ must pass CREATE_NO_WINDOW, either directly
(**no_window(...)) or through a local dict variable assigned from no_window(...) in the same function. POSIX-only
alternatives in a conditional expression are fine as long as the Windows branch uses no_window().
DETACHED_PROCESS and CREATE_NEW_CONSOLE are forbidden (see hyperloom/winproc.py).
"""
import ast
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1] / "hyperloom"
CALLS = {"Popen", "run", "call", "check_call", "check_output"}


def _uses_no_window(node):
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "no_window"
               for n in ast.walk(node))


def _subprocess_calls(tree):
    for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]:
        assigned = {}
        for n in ast.walk(func):
            if isinstance(n, ast.Assign):
                for target in n.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(n.value)
        for n in ast.walk(func):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CALLS
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"):
                yield n, assigned


class NoWindowTests(unittest.TestCase):
    def test_every_subprocess_call_hides_its_console(self):
        missing = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call, assigned in _subprocess_calls(tree):
                ok = False
                for kw in call.keywords:
                    if kw.arg is None:  # **kwargs
                        if _uses_no_window(kw.value):
                            ok = True
                        elif isinstance(kw.value, ast.Name) and any(_uses_no_window(v) for v in assigned.get(kw.value.id, [])):
                            ok = True
                    elif kw.arg == "creationflags" and "CREATE_NO_WINDOW" in ast.unparse(kw.value):
                        ok = True
                if not ok:
                    missing.append(f"{path.relative_to(PACKAGE.parent)}:{call.lineno}")
        # Recursion through the seen set is not needed: calls are collected per enclosing scope.
        self.assertEqual(sorted(set(missing)), [], "subprocess calls without CREATE_NO_WINDOW on Windows")

    def test_no_detached_or_new_console(self):
        offenders = [f"{p.relative_to(PACKAGE.parent)}" for p in PACKAGE.rglob("*.py")
                     if p.name != "winproc.py" and any(w in p.read_text(encoding="utf-8")
                                                        for w in ("DETACHED_PROCESS", "CREATE_NEW_CONSOLE"))]
        self.assertEqual(offenders, [])

    def test_helper_flags(self):
        from hyperloom import winproc
        with mock.patch.object(winproc.os, "name", "nt"):
            self.assertEqual(winproc.no_window(0x200)["creationflags"], 0x200 | winproc.CREATE_NO_WINDOW)
        with mock.patch.object(winproc.os, "name", "posix"):
            self.assertEqual(winproc.no_window(0x200), {})

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_real_child_gets_no_console_window(self):
        # A console child started with no_window() has a console (hidden) but no window.
        import sys
        from hyperloom.winproc import no_window
        code = ("import ctypes; k=ctypes.windll.kernel32; b=(ctypes.c_uint32*4)(); "
                "print(int(bool(k.GetConsoleWindow())), k.GetConsoleProcessList(b,4)>0)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                             **no_window()).stdout.split()
        self.assertEqual(out, ["0", "True"])


if __name__ == "__main__":
    unittest.main()
