import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from hyperloom.procs import InstanceLock, LocalChild, ProcessError


ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path(sys.executable).absolute())


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting for child")


def console_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyperloom process tests ")
        self.directory = Path(self.temporary.name)
        self.children = []

    def tearDown(self):
        for child in self.children:
            child.stop(0.1)
        self.temporary.cleanup()

    def child(self, code, **kwargs):
        child = LocalChild([PYTHON, "-u", "-c", code], dict(os.environ), self.directory,
                           "dummy", **kwargs)
        self.children.append(child)
        return child

    def test_absolute_executable_and_safe_name_required(self):
        with self.assertRaises(ValueError):
            LocalChild(["python"], {}, self.directory, "dummy")
        with self.assertRaises(ValueError):
            LocalChild([PYTHON], {}, self.directory, "../dummy")

    def test_both_logs_rotate_with_exact_bounds(self):
        child = self.child("import os; os.write(1,b'x'*20000); os.write(2,b'y'*20000)",
                           max_log_bytes=1024, log_backups=2).start()
        child.process.wait(timeout=8)
        child.stop(0)
        for stream, content in (("stdout", b"x"), ("stderr", b"y")):
            files = list(self.directory.glob("dummy." + stream + ".log*"))
            self.assertEqual(len(files), 3)
            for path in files:
                self.assertLessEqual(path.stat().st_size, 1024)
                self.assertEqual(set(path.read_bytes()), set(content))

    def test_single_instance_and_restart_after_stop(self):
        first = self.child("import time; time.sleep(60)").start()
        duplicate = self.child("pass")
        with self.assertRaisesRegex(ProcessError, "another instance"):
            duplicate.start()
        first.stop(0.1)
        duplicate.start()
        duplicate.process.wait(timeout=8)
        duplicate.stop(0)

    def test_port_conflict_refuses_start_then_reuses_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            endpoint = listener.getsockname()
            child = self.child("pass", port=endpoint)
            with self.assertRaisesRegex(ProcessError, "port unavailable"):
                child.start()
            self.assertIsNone(child.process)
        child.start()
        child.process.wait(timeout=8)
        child.stop(0)

    def test_failed_spawn_releases_instance_lock(self):
        bad = LocalChild([str(self.directory / "missing.exe")], {}, self.directory, "dummy")
        with self.assertRaises(OSError):
            bad.start()
        lock = InstanceLock(self.directory / "dummy.lock").acquire()
        lock.close()

    def test_graceful_stop_delivers_signal(self):
        marker = self.directory / "graceful"
        ready = self.directory / "ready"
        code = (
            "import signal,time; from pathlib import Path; "
            f"signal.signal(getattr(signal,'SIGBREAK',signal.SIGTERM), "
            f"lambda *_: (Path({str(marker)!r}).write_text('stopped'),exit(0))); "
            f"Path({str(ready)!r}).touch()\nwhile True: time.sleep(.03)"
        )
        # A private hidden console also exercises CTRL_BREAK from non-console test runners.
        parent_code = (
            "import os,sys,time; from pathlib import Path; from hyperloom.procs import LocalChild; "
            f"c=LocalChild([{PYTHON!r},'-u','-c',{code!r}],dict(os.environ),{str(self.directory)!r},'dummy').start(); "
            f"ready=Path({str(ready)!r}); "
            "deadline=time.monotonic()+8\n"
            "try:\n"
            " while not ready.exists() and time.monotonic()<deadline: time.sleep(.03)\n"
            " if not ready.exists(): raise RuntimeError('not ready')\n"
            " c.stop(3)\n"
            "finally: c.stop(.1)\n"
        )
        result = subprocess.run([PYTHON, "-c", parent_code], cwd=ROOT, capture_output=True,
                                timeout=15, **console_options())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(marker.read_text(), "stopped")

    def test_forced_stop_kills_uncooperative_child(self):
        ready = self.directory / "ready"
        code = ("import signal,time; from pathlib import Path; "
                "signal.signal(getattr(signal,'SIGBREAK',signal.SIGTERM),signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); time.sleep(60)")
        child = self.child(code).start()
        wait_for(ready.exists)
        child.stop(0.1)
        self.assertFalse(child.alive())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
    def test_killed_parent_closes_job_and_kills_child_and_descendant(self):
        marker = self.directory / "pids.json"
        child_code = (
            "import json,os,subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)"
        )
        parent_code = (
            "import os,time; from hyperloom.procs import LocalChild; "
            f"c=LocalChild([{PYTHON!r},'-c',{child_code!r}],dict(os.environ),"
            f"{str(self.directory)!r},'dummy').start(); time.sleep(60)"
        )
        parent = subprocess.Popen([PYTHON, "-c", parent_code], cwd=ROOT, **console_options())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handles = []
        try:
            wait_for(lambda: marker.exists() and marker.stat().st_size > 0)
            for pid in json.loads(marker.read_text()):
                handle = kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(handle)
                handles.append(handle)
            parent.kill()
            parent.wait(timeout=8)
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle, 8000), 0)
            InstanceLock(self.directory / "dummy.lock").acquire().close()
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=8)
            for handle in handles:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
