"""DXGI/D3DKMT ABI and cleanup with CPU-only COM objects; no driver is loaded."""
import ctypes as C
import unittest
from unittest import mock

from hyperloom import windows_adapters


class AdapterTests(unittest.TestCase):
    def test_pci_luid_inventory_and_failure_cleanup(self):
        table = (C.c_void_p * 13)(*range(100, 113))
        factory, adapter = (C.c_void_p * 1)(C.addressof(table)), (C.c_void_p * 1)(C.addressof(table))
        dxgi, gdi = mock.Mock(), mock.Mock()
        events = []
        def create(iid, out):
            self.assertEqual(C.string_at(iid, 16).hex(), "78ae0a776ff2ba4da829253c83d1b387")
            out._obj.value = C.addressof(factory)
            return 0
        def enum(this, index, out):
            if index:
                return 0x887A0002
            out._obj.value = C.addressof(adapter)
            return 0
        def desc(this, out):
            value = out._obj
            value.vendor, value.luid.low, value.luid.high = 0x10de, 10, 9
            return 0
        def opened(out):
            self.assertEqual((out._obj.luid.high, out._obj.luid.low), (9, 10))
            out._obj.handle = 42
            events.append("open")
            return 0
        def query(out):
            value = out._obj
            self.assertEqual((value.handle, value.kind, value.size), (42, 6, 12))
            address = C.cast(value.data, C.POINTER(C.c_uint32))
            address[0], address[1], address[2] = 7, 2, 1
            return 0
        functions = {102: lambda this: events.append("release") or 0, 112: enum, 110: desc}
        dxgi.CreateDXGIFactory1.side_effect = create
        gdi.D3DKMTOpenAdapterFromLuid.side_effect = opened
        gdi.D3DKMTQueryAdapterInfo.side_effect = query
        gdi.D3DKMTCloseAdapter.side_effect = lambda _: events.append("close") or 0
        def load(name):
            self.assertIn(name, ("dxgi.dll", "gdi32.dll"))
            return dxgi if name == "dxgi.dll" else gdi
        with mock.patch.object(C, "WinDLL", side_effect=load, create=True), \
                mock.patch.object(C, "WINFUNCTYPE", side_effect=lambda *a: lambda address: functions[address], create=True):
            self.assertEqual(windows_adapters.dxgi_adapters(), [(b"0000:07:02.1", (9, 10))])
            self.assertEqual(events, ["open", "close", "release", "release"])
            events.clear()
            gdi.D3DKMTQueryAdapterInfo.side_effect = lambda _: -1
            with self.assertRaises(OSError):
                windows_adapters.dxgi_adapters()
            self.assertEqual(events, ["open", "close", "release", "release"])
