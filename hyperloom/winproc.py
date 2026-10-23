"""Windows child-process flags: never flash a console window.

Every subprocess the control machine starts goes through one of these helpers (tests/test_no_window.py checks).
- no_window(): short-lived console tools (ssh, scp, ssh-keyscan, taskkill, powershell, nvidia-smi, python probes).
  CREATE_NO_WINDOW gives the child a hidden console of its own; its own console children inherit it.
- Controller helpers (cli.py, orchestrate.py) also use no_window(): their router child shares the hidden console,
  so CTRL_BREAK from the helper still reaches it. CREATE_NEW_CONSOLE + SW_HIDE is not used: with Windows Terminal
  as the default console host SW_HIDE is ignored and the window shows.
- procs.py children share the parent's console (CTRL_BREAK) and get CREATE_NO_WINDOW when there is none.
Note: CREATE_NO_WINDOW is ignored together with DETACHED_PROCESS or CREATE_NEW_CONSOLE, and a DETACHED_PROCESS
has no console, so every console tool it starts would open a new visible window. Never use DETACHED_PROCESS.
"""
import os
import subprocess

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window(flags=0):
    """Keyword arguments for subprocess.run/Popen: adds CREATE_NO_WINDOW on Windows (to any extra flags)."""
    if os.name != "nt":
        return {}
    return {"creationflags": flags | CREATE_NO_WINDOW}


def has_console():
    """True when this process is attached to a console, visible or hidden (children may share it without flashing).
    GetConsoleWindow() is NULL inside a CREATE_NO_WINDOW console, so count the attached processes instead."""
    if os.name != "nt":
        return True
    import ctypes
    buffer = (ctypes.c_uint32 * 1)()
    return ctypes.windll.kernel32.GetConsoleProcessList(buffer, 1) > 0
