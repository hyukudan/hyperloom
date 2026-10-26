"""Owned local process groups, bounded logs, and cross-platform instance locks."""

import ctypes
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
from .winproc import has_console, no_window


class ProcessError(RuntimeError):
    pass


def check_port(address, port):
    """Read-only availability check; the caller must still bind/recheck when it starts serving."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError as exc:
            raise ProcessError(f"port unavailable: {address}:{port}") from exc


def port_owned(address, port, pids):
    """Read-only proof that an occupied IPv4 listener belongs to a reviewed child; unknown owners fail closed."""
    if not pids:
        return False
    try:
        if os.name == "nt":
            from ctypes import wintypes
            class Row(ctypes.Structure):
                _fields_ = [(name, wintypes.DWORD) for name in
                            ("state", "local", "port", "remote", "remote_port", "pid")]
            query = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
            query.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                              wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
            query.restype = wintypes.DWORD
            size = wintypes.DWORD()
            if query(None, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 122:
                return False
            buffer = ctypes.create_string_buffer(size.value)
            if query(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0):
                return False
            count = wintypes.DWORD.from_buffer(buffer).value
            rows = (Row * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
            owners = {row.pid for row in rows if row.state == 2 and socket.ntohs(row.port & 0xffff) == port
                      and socket.inet_ntoa(bytes(wintypes.DWORD(row.local))) in (address, "0.0.0.0")}
            return bool(owners) and owners <= set(pids)
        inodes = set()
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            row = line.split()
            host, number = row[1].split(":")
            if (row[3] == "0A" and int(number, 16) == port
                    and socket.inet_ntoa(bytes.fromhex(host)[::-1]) in (address, "0.0.0.0")):
                inodes.add(row[9])
        owned = set()
        for pid in pids:
            for fd in (Path("/proc") / str(pid) / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    owned.add(target[8:-1])
        return bool(inodes) and inodes <= owned
    except (OSError, ValueError, IndexError):
        return False


class InstanceLock:
    """An OS lock, released even on a crash. The lock file is never unlinked."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self, create=True):
        """Take the lock (ProcessError when another holder has it). ``create=False``: an existing lock file only
        (FileNotFoundError otherwise), nothing created or written."""
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b" if create else "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                if create and self.path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ProcessError("another instance holds " + str(self.path)) from exc
        self.file = stream
        return self

    def close(self):
        if self.file is not None:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


if os.name == "nt":
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                    ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                    ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", _BasicLimits), ("IO", _IOCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenThread.restype = wintypes.HANDLE
    _kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel.ResumeThread.restype = wintypes.DWORD

    class _WindowsJob:
        def __init__(self):
            self.handle = _kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _ExtendedLimits()
            info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info),
                                                   ctypes.sizeof(info)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

        def assign(self, process):
            if not _kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())

        def terminate(self):
            if self.handle and not _kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self):
            if self.handle:
                _kernel.CloseHandle(self.handle)
                self.handle = None


if os.name == "nt":
    def resume_suspended(pid):
        """Resume the primary thread of a child created with CREATE_SUSPENDED (after assigning it to a job)."""
        # Popen closes the primary thread handle; reopen it while the child is suspended.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("tid", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("base", wintypes.LONG), ("delta", wintypes.LONG),
                        ("flags", wintypes.DWORD)]
        _kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        _kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        snapshot = _kernel.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = _kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == pid:
                    thread = _kernel.OpenThread(0x2, False, entry.tid)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        _kernel.CloseHandle(thread)
                    return
                found = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise ProcessError("cannot find suspended child thread")
        finally:
            _kernel.CloseHandle(snapshot)


class LocalChild:
    def __init__(self, argv, env, log_dir, name, *, port=None,
                 max_log_bytes=1024 * 1024, log_backups=3):
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError("child executable must be an absolute path")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid child name")
        if max_log_bytes < 256 or log_backups < 1:
            raise ValueError("log bounds must be positive (at least 256 bytes)")
        self.argv, self.env = list(argv), dict(env)
        self.log_dir, self.name, self.port = Path(log_dir), name, port
        self.max_log_bytes, self.log_backups = max_log_bytes, log_backups
        self.process = None
        self._job = None
        self._threads = []
        self._lock = InstanceLock(self.log_dir / (name + ".lock"))

    def _check_port(self):
        if self.port is None:
            return
        check_port(*self.port)

    def start(self):
        if self.process is not None:
            raise ProcessError("child has already been started")
        self._lock.acquire()
        try:
            self._check_port()
            # Share the parent's console so CTRL_BREAK reaches the child; without a console (e.g. pythonw),
            # give the child a hidden one instead of letting Windows open a visible window.
            options = {"start_new_session": True} if os.name != "nt" else (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if has_console()
                else no_window(subprocess.CREATE_NEW_PROCESS_GROUP | 0x4))
            if os.name == "nt":
                self._job = _WindowsJob()
            self.process = subprocess.Popen(self.argv, env=self.env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            **options)
            if self._job is not None:
                self._job.assign(self.process)
                self._resume_windows_child()
            for label, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
                path = self.log_dir / f"{self.name}.{label}.log"
                thread = threading.Thread(target=self._drain, args=(stream, path), daemon=True)
                thread.start()
                self._threads.append(thread)
            return self
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.wait()
                for stream in (self.process.stdout, self.process.stderr):
                    stream.close()
            if self._job:
                self._job.close()
            self._lock.close()
            raise

    def _resume_windows_child(self):
        resume_suspended(self.process.pid)

    def _drain(self, stream, path):
        output = path.open("ab")
        try:
            while chunk := stream.read1(min(8192, self.max_log_bytes)):
                offset = 0
                while offset < len(chunk):
                    if output.tell() >= self.max_log_bytes:
                        output.close()
                        path.with_name(path.name + f".{self.log_backups}").unlink(missing_ok=True)
                        for index in range(self.log_backups - 1, 0, -1):
                            older = path.with_name(path.name + f".{index}")
                            if older.exists():
                                older.replace(path.with_name(path.name + f".{index + 1}"))
                        path.replace(path.with_name(path.name + ".1"))
                        output = path.open("wb")
                    count = min(len(chunk) - offset, self.max_log_bytes - output.tell())
                    output.write(chunk[offset:offset + count])
                    output.flush()
                    offset += count
        finally:
            stream.close()
            output.close()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self, grace_s=5):
        if grace_s < 0:
            raise ValueError("grace period must not be negative")
        try:
            if self.alive():
                try:
                    if os.name == "nt":
                        self.process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(self.process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    self.process.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    if self._job:
                        self._job.terminate()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
        finally:
            if self._job:
                self._job.close()
            elif self.process is not None:
                # Also reap descendants when the group leader has already exited.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for thread in self._threads:
                thread.join(timeout=5)
            self._lock.close()
