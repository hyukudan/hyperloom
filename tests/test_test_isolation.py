"""The package sandbox applies to both test runners and inherited CLI environments."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests
from hyperloom import cli
from hyperloom.recipes import service
from hyperloom.web.api import API
from tests.ui import smoke

ROOT = Path(__file__).resolve().parents[1]


def _child(code, **environment):
    """Run ``code`` in a fresh interpreter that imports the test package (from the repository root)."""
    env = {**os.environ, **environment}
    for name in [name for name, value in environment.items() if value is None]:
        env.pop(name)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


class TestIsolationTests(unittest.TestCase):
    def test_default_config_audit_and_admission_are_isolated(self):
        with service.configured(None):
            self.assertTrue(service.config_dir().is_relative_to(tests._test_root))
        api = API()
        self.assertTrue(api.audit_path.is_relative_to(tests._test_root))
        if tests._outer_admission_db is None:
            self.assertEqual(os.environ["HYPERLOOM_ADMISSION_DB"], ":memory:")
        self.assertFalse(tests._inside_real_config(os.environ["HYPERLOOM_ADMISSION_DB"]))
        self.assertFalse(tests._test_root.is_relative_to(Path(__file__).resolve().parents[1]))

    def test_cli_child_inherits_isolation(self):
        child = subprocess.run([sys.executable, "-c",
            "import json, os; from hyperloom.cli import config_path; "
            "print(json.dumps([str(config_path()), os.environ['HYPERLOOM_ADMISSION_DB']]))"],
            check=True, capture_output=True, text=True, timeout=30)
        path, database = json.loads(child.stdout)
        self.assertEqual(Path(path), cli.config_path())
        self.assertEqual(database, os.environ["HYPERLOOM_ADMISSION_DB"])

    def test_real_configuration_writes_fail_before_open(self):
        for root in tests._real_config_dirs:
            with self.subTest(root=root), tests.expected_guard_trips() as trips, \
                    self.assertRaisesRegex(tests.RealConfigWrite, "real per-user"):
                # The audit hook rejects this before the OS can open or create anything.
                with (Path(root) / "audit.jsonl").open("a", encoding="utf-8"):
                    self.fail("real audit was opened for writing")
            self.assertNotIn(("open", str(Path(root) / "audit.jsonl")), trips)

    def test_real_configuration_mutations_are_guarded(self):
        for root in tests._real_config_dirs:
            target = str(Path(root) / "guard-probe")
            for event, args in (("os.mkdir", (target, 0o700, -1)), ("os.remove", (target, -1)),
                                ("os.rmdir", (target, -1)), ("os.rename", ("guard-probe", target, -1, -1)),
                                ("open", (target, None, os.O_RDWR)), ("os.utime", (target, None, None, -1)),
                                ("shutil.rmtree", (target, None)), ("sqlite3.connect", (target,)),
                                ("sqlite3.connect", (Path(target).as_uri() + "?mode=rwc",))):
                with self.subTest(event=event, args=args), tests.expected_guard_trips(), \
                        self.assertRaisesRegex(tests.RealConfigWrite, "real per-user"):
                    sys.audit(event, *args)

    def test_guard_allows_read_only_audits(self):
        for root in tests._real_config_dirs:
            sys.audit("open", str(Path(root) / "audit.jsonl"), "r", os.O_RDONLY)
        sys.audit("sqlite3.connect", ":memory:")

    def test_guard_roots_do_not_trust_the_current_environment(self):
        exported = set(os.environ[tests._GUARD_ROOTS].split(os.pathsep))
        self.assertTrue(exported <= set(tests._real_config_dirs))
        self.assertFalse(any(Path(root).is_relative_to(tests._test_root) for root in exported))
        code = "import json, tests; print(json.dumps(tests._real_config_dirs))"
        with tempfile.TemporaryDirectory() as elsewhere:
            # A redirected environment without the exported roots still guards the original roots.
            moved = set(json.loads(_child(code, APPDATA=elsewhere, LOCALAPPDATA=elsewhere, HOME=elsewhere,
                                          XDG_CONFIG_HOME=elsewhere, **{tests._GUARD_ROOTS: None}).stdout))
            profile = set(json.loads(_child(code, APPDATA=elsewhere, LOCALAPPDATA=elsewhere, HOME=elsewhere,
                                            USERPROFILE=elsewhere, XDG_CONFIG_HOME=elsewhere,
                                            **{tests._GUARD_ROOTS: None}).stdout))
        if os.name == "nt":
            for folder in (tests._ROAMING_APP_DATA, tests._LOCAL_APP_DATA):
                with self.subTest(folder=folder):
                    self.assertTrue(tests._spellings(Path(tests._known_folder(folder)) / "hyperloom") <= moved)
            for name in ("Roaming", "Local"):
                with self.subTest(profile=name):
                    self.assertTrue(tests._spellings(Path(tests._profile_directory(), "AppData", name, "hyperloom"))
                                    <= profile)
        else:
            import pwd
            self.assertTrue(tests._spellings(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/hyperloom") <= profile)
        # A nested run (this sandboxed environment) keeps guarding every original root.
        nested = set(json.loads(_child(code).stdout))
        self.assertTrue(set(tests._real_config_dirs) <= nested)

    def test_alternate_spellings_of_a_guarded_root_are_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            guarded = Path(directory) / "Guarded Configuration Root"
            guarded.mkdir()
            alias = Path(directory) / "alias"
            spellings = [guarded / "x", alias / "x", (guarded / "x").as_posix()]
            try:
                spellings.append(os.path.relpath(guarded / "x"))
            except ValueError:
                pass                    # another drive: no relative spelling
            if os.name == "nt":
                import _winapi
                import ctypes
                spellings.append("\\\\?\\" + str(guarded / "x"))
                buffer = ctypes.create_unicode_buffer(1024)
                if ctypes.windll.kernel32.GetShortPathNameW(str(guarded), buffer, len(buffer)) and \
                        Path(buffer.value).name != guarded.name:
                    spellings.append(Path(buffer.value) / "x")      # 8.3 short name, when the volume has them
                _winapi.CreateJunction(str(guarded), str(alias))
            else:
                os.symlink(guarded, alias)
            with mock.patch.object(tests, "_real_config_dirs", tuple(sorted(tests._spellings(guarded)))):
                for spelling in spellings:
                    with self.subTest(spelling=str(spelling)), tests.expected_guard_trips(), \
                            self.assertRaises(tests.RealConfigWrite):
                        Path(spelling).write_text("x", encoding="utf-8")
                for spelling in spellings[:2]:
                    # sqlite3 file: URIs (percent-encoded blanks, query) name the same file.
                    with self.subTest(uri=str(spelling)), tests.expected_guard_trips(), \
                            self.assertRaises(tests.RealConfigWrite):
                        sqlite3.connect(Path(str(spelling) + ".db").as_uri() + "?mode=rwc", uri=True).close()
                self.assertEqual(list(guarded.iterdir()), [])

    def test_guard_trips_cannot_be_swallowed(self):
        root = Path(tests._real_config_dirs[0])
        with tests.expected_guard_trips() as trips:
            start = len(trips)
            with self.assertRaises(tests.RealConfigWrite):
                try:
                    (root / "guard-probe").write_text("x", encoding="utf-8")
                except Exception:
                    self.fail("an application except Exception absorbed the guard trip")
            self.assertEqual(trips[start:], [("open", str(root / "guard-probe"))])
        self.assertFalse(issubclass(tests.RealConfigWrite, Exception))
        with tempfile.TemporaryDirectory() as directory:
            guarded = Path(directory) / "guarded"
            guarded.mkdir()
            # A trip that the code under test catches with a bare except still fails the whole run at exit.
            child = _child("import tests\ntry:\n    open(r'%s', 'w')\nexcept BaseException:\n    pass\n"
                           "print('finished')" % (guarded / "x"), **{tests._GUARD_ROOTS: str(guarded)})
            self.assertEqual(child.returncode, 3, child.stderr)
            self.assertIn("finished", child.stdout)
            self.assertIn("real per-user configuration", child.stderr)
            self.assertFalse((guarded / "x").exists())
            clean = _child("import tests\nprint('finished')", **{tests._GUARD_ROOTS: str(guarded)})
            self.assertEqual(clean.returncode, 0, clean.stderr)

    def test_admission_database_override_is_kept_unless_it_is_real_configuration(self):
        code = "import os, tests; print(os.environ['HYPERLOOM_ADMISSION_DB'])"
        with tempfile.TemporaryDirectory() as directory:
            chosen = str(Path(directory) / "admission.sqlite3")
            self.assertEqual(_child(code, HYPERLOOM_ADMISSION_DB=chosen).stdout.strip(), chosen)
        self.assertEqual(_child(code, HYPERLOOM_ADMISSION_DB=None).stdout.strip(), ":memory:")
        real = str(Path(tests._real_config_dirs[0]) / "run" / "admission.sqlite3")
        for value in (real, Path(real).as_uri()):
            with self.subTest(value=value):
                self.assertEqual(_child(code, HYPERLOOM_ADMISSION_DB=value).stdout.strip(), ":memory:")

    def test_sandbox_keeps_user_tools_reachable(self):
        for name in ("PYTHONUSERBASE", tests._ORIGINAL_LOCALAPPDATA):
            with self.subTest(name=name):
                self.assertFalse(Path(os.environ[name]).is_relative_to(tests._test_root))
        if os.name == "nt":
            # Git for Windows reads the global config from HOME; hyperloom's Windows roots do not use it.
            self.assertFalse(Path(os.environ.get("HOME", os.sep)).is_relative_to(tests._test_root))
        with tempfile.TemporaryDirectory() as directory:
            chrome = Path(directory) / "Google/Chrome/Application/chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            with mock.patch.dict(os.environ, {tests._ORIGINAL_LOCALAPPDATA: directory}, clear=True), \
                    mock.patch.object(smoke.shutil, "which", return_value=None):
                self.assertEqual(smoke.find_chrome(), str(chrome.resolve()))
            # A fresh package import discovers this per-user install before moving LOCALAPPDATA.
            child = _child("import json, os\nfrom unittest import mock\n"
                           "os.environ.pop('PROGRAMFILES', None)\nos.environ.pop('PROGRAMFILES(X86)', None)\n"
                           "with mock.patch('shutil.which', return_value=None):\n    import tests\n"
                           "from tests.ui.smoke import find_chrome\n"
                           "print(json.dumps([find_chrome(), str(tests._test_root)]))",
                           CHROME=None, LOCALAPPDATA=directory, PROGRAMFILES=None,
                           **{"PROGRAMFILES(X86)": None, tests._ORIGINAL_LOCALAPPDATA: directory,
                              tests._ORIGINAL_CHROME: None, "PATH": ""})
            self.assertEqual(child.returncode, 0, child.stderr)
            found, sandbox = json.loads(child.stdout)
            self.assertEqual(found, str(chrome.resolve()))
            self.assertFalse(Path(found).is_relative_to(sandbox))
