"""DXGI adapter LUIDs joined to PCI addresses through read-only D3DKMT queries; no CUDA driver."""
import ctypes as C
import uuid


def dxgi_adapters():
    class Luid(C.Structure):
        _fields_ = [("low", C.c_uint32), ("high", C.c_int32)]

    class Description(C.Structure):
        _fields_ = [("name", C.c_wchar * 128), ("vendor", C.c_uint32), ("device", C.c_uint32),
                    ("subsystem", C.c_uint32), ("revision", C.c_uint32), ("video", C.c_size_t),
                    ("system", C.c_size_t), ("shared", C.c_size_t), ("luid", Luid), ("flags", C.c_uint32)]

    class Open(C.Structure):
        _fields_ = [("luid", Luid), ("handle", C.c_uint32)]

    class Address(C.Structure):
        _fields_ = [("bus", C.c_uint32), ("device", C.c_uint32), ("function", C.c_uint32)]

    class Query(C.Structure):
        _fields_ = [("handle", C.c_uint32), ("kind", C.c_uint32), ("data", C.c_void_p), ("size", C.c_uint32)]

    class Close(C.Structure):
        _fields_ = [("handle", C.c_uint32)]

    def method(pointer, index, *args):
        table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(C.c_int32, C.c_void_p, *args)(table[index])

    def check(status):
        if status:
            raise OSError("DXGI/D3DKMT adapter inventory unavailable")

    dxgi, gdi = C.WinDLL("dxgi.dll"), C.WinDLL("gdi32.dll")
    dxgi.CreateDXGIFactory1.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    dxgi.CreateDXGIFactory1.restype = C.c_int32
    for name, struct in (("D3DKMTOpenAdapterFromLuid", Open), ("D3DKMTQueryAdapterInfo", Query),
                         ("D3DKMTCloseAdapter", Close)):
        fn = getattr(gdi, name)
        fn.argtypes, fn.restype = [C.POINTER(struct)], C.c_int32
    iid = (C.c_ubyte * 16).from_buffer_copy(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    factory = C.c_void_p()
    check(dxgi.CreateDXGIFactory1(C.byref(iid), C.byref(factory)))
    result = []
    try:
        for index in range(256):
            adapter = C.c_void_p()
            status = method(factory, 12, C.c_uint32, C.POINTER(C.c_void_p))(factory, index, C.byref(adapter))
            if status & 0xffffffff == 0x887A0002:  # DXGI_ERROR_NOT_FOUND
                return result
            check(status)
            try:
                desc = Description()
                check(method(adapter, 10, C.POINTER(Description))(adapter, C.byref(desc)))
                if desc.vendor != 0x10de or desc.flags & 2:  # NVIDIA hardware only, no software adapter.
                    continue
                opened = Open(desc.luid)
                check(gdi.D3DKMTOpenAdapterFromLuid(C.byref(opened)))
                try:
                    address = Address()
                    query = Query(opened.handle, 6, C.addressof(address), C.sizeof(address))  # KMTQAITYPE_ADAPTERADDRESS
                    check(gdi.D3DKMTQueryAdapterInfo(C.byref(query)))
                    if address.bus > 255 or address.device > 31 or address.function > 7:
                        raise OSError("invalid adapter PCI address")
                    # D3DKMT exposes no PCI domain; this Windows adapter mapping assumes domain zero.
                    pci = f"0000:{address.bus:02x}:{address.device:02x}.{address.function}".encode("ascii")
                    result.append((pci, (desc.luid.high & 0xffffffff, desc.luid.low)))
                finally:
                    check(gdi.D3DKMTCloseAdapter(C.byref(Close(opened.handle))))
            finally:
                method(adapter, 2)(adapter)
        raise OSError("adapter inventory exceeds limit")
    finally:
        method(factory, 2)(factory)
