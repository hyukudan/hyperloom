import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from hyperloom import secrets


class SecretTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom secrets ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for name, result in (("_directory", self.directory / "stored"),
                             ("runtime_dir", self.directory / "run")):
            patcher = patch.object(secrets, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ensure_persists_and_rotate_replaces_without_echo(self):
        self.assertIsNone(secrets.get("replica"))
        first = secrets.ensure("replica")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(secrets.ensure("replica"), first)
        self.assertEqual(secrets.get("replica"), first)
        second = secrets.rotate("replica")
        self.assertNotEqual(first, second)
        self.assertEqual(secrets.get("replica"), second)
        if os.name == "nt":
            self.assertNotIn(second.encode(), (self.directory / "stored/replica").read_bytes())
        else:
            self.assertEqual(stat.S_IMODE((self.directory / "stored/replica").stat().st_mode), 0o600)

    def test_router_paths_are_private_distinct_and_removable(self):
        first = secrets.path_for_router("replica")
        second = secrets.path_for_router("replica")
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.directory / "run")
        self.assertEqual(first.read_text(), secrets.get("replica"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        first.unlink()
        second.unlink()
        self.assertEqual(list((self.directory / "run").iterdir()), [])

    def test_names_cannot_escape_directories(self):
        for name in ("../key", "A", "key/path", "", "x" * 41, "key\n", None):
            for function in (secrets.get, secrets.ensure, secrets.rotate, secrets.path_for_router):
                with self.subTest(name=name, function=function.__name__), self.assertRaises(ValueError):
                    function(name)
        self.assertFalse((self.directory / "stored").exists())

    def test_invalid_key_is_not_silently_replaced(self):
        secrets.ensure("replica")
        path = self.directory / "stored/replica"
        path.write_bytes(b"invalid")
        with self.assertRaises((ValueError, OSError)):
            secrets.ensure("replica")
        self.assertEqual(path.read_bytes(), b"invalid")

    def test_failed_rotation_keeps_previous_key_and_cleans_temporary(self):
        first = secrets.ensure("replica")
        with patch.object(secrets.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                secrets.rotate("replica")
        self.assertEqual(secrets.get("replica"), first)
        self.assertEqual([p.name for p in (self.directory / "stored").iterdir()], ["replica"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI user scope")
    def test_dpapi_round_trip_and_corruption(self):
        plaintext = b"fixture secret bytes"
        encrypted = secrets._dpapi(plaintext)
        self.assertNotEqual(encrypted, plaintext)
        self.assertEqual(secrets._dpapi(encrypted, decrypt=True), plaintext)
        with self.assertRaises(OSError):
            secrets._dpapi(b"not a DPAPI blob", decrypt=True)

    @unittest.skipUnless(os.name == "nt", "Windows protected key DACL")
    def test_windows_key_has_protected_owner_only_dacl(self):
        import ctypes
        from ctypes import wintypes
        path = secrets.path_for_router("replica")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        needed = wintypes.DWORD()
        advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        self.assertTrue(advapi.GetFileSecurityW(str(path), 4, buffer, needed, ctypes.byref(needed)))
        text = ctypes.c_void_p()
        self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buffer, 1, 4, ctypes.byref(text), None))
        try:
            self.assertEqual(ctypes.wstring_at(text), "D:P(A;;FA;;;OW)")
        finally:
            kernel.LocalFree(text)


if __name__ == "__main__":
    unittest.main()
