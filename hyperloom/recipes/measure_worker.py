"""Internal transfer child. The parent releases stdin only under its grant, lock, gate and watchdog.

No CUDA DLL is loaded before that release. No engine or model file is opened here.
"""
import ctypes as C
from contextlib import ExitStack
import json
import os
import signal
import sys
import time
import uuid

from .measurements import RUNS, XFER_METHOD


def transfer(request, *, library=None, timer=time.perf_counter_ns):
    load = C.WinDLL if os.name == "nt" else C.CDLL
    dll = library or load("nvcuda.dll" if os.name == "nt" else "libcuda.so.1")

    def call(name, types, *args):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = types, C.c_int
        status = fn(*args)
        if status:
            raise RuntimeError(f"CUDA transfer {name} failed ({status})")

    ptr, devptr = C.c_void_p, C.c_uint64
    call("cuInit", [C.c_uint], 0)
    count = C.c_int()
    call("cuDeviceGetCount", [C.POINTER(C.c_int)], C.byref(count))
    selected = None
    for index in range(count.value):
        ident = (C.c_ubyte * 16)()
        call("cuDeviceGetUuid", [ptr, C.c_int], C.byref(ident), index)
        if str(uuid.UUID(bytes=bytes(ident))) == request["uuid"].removeprefix("GPU-").lower():
            selected = index
    if selected is None:
        raise RuntimeError("granted CUDA UUID is not visible")
    with ExitStack() as stack:
        context = ptr()
        call("cuCtxCreate_v2", [C.POINTER(ptr), C.c_uint, C.c_int], C.byref(context), 0, selected)
        stack.callback(call, "cuCtxDestroy_v2", [ptr], context)
        host, device = ptr(), devptr()
        size = XFER_METHOD["bytes"]
        call("cuMemHostAlloc", [C.POINTER(ptr), C.c_size_t, C.c_uint], C.byref(host), size, 0)
        stack.callback(call, "cuMemFreeHost", [ptr], host)
        C.memset(host, 0, size)
        call("cuMemAlloc_v2", [C.POINTER(devptr), C.c_size_t], C.byref(device), size)
        stack.callback(call, "cuMemFree_v2", [devptr], device)

        def copy(name, types, a, b, length):
            call("cuCtxSynchronize", [])
            start = timer()
            call(name, types, a, b, length)
            call("cuCtxSynchronize", [])
            return max(1, timer() - start)

        h2d = [devptr, ptr, C.c_size_t]
        d2h = [ptr, devptr, C.c_size_t]
        # Materialize both copy paths before the nine recorded runs.
        copy("cuMemcpyHtoD_v2", h2d, device, host, size)
        copy("cuMemcpyDtoH_v2", d2h, host, device, size)
        samples = []
        for _ in range(RUNS):
            samples.append({"h2d_mbps": size * 1000 / copy("cuMemcpyHtoD_v2", h2d, device, host, size),
                            "d2h_mbps": size * 1000 / copy("cuMemcpyDtoH_v2", d2h, host, device, size),
                            "h2d_lat_ns": copy("cuMemcpyHtoD_v2", h2d, device, host, XFER_METHOD["latency_bytes"])})
        return samples


def main():
    request = json.loads(sys.stdin.readline())
    if os.name != "nt":
        # Install the parent-death signal before CUDA, then close the parent-death race with getppid.
        libc = C.CDLL(None)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0):  # PR_SET_PDEATHSIG (Linux)
            raise RuntimeError("cannot supervise transfer worker lifetime")
    if os.getppid() != request["parent_pid"] or time.time() >= request["deadline"]:
        raise RuntimeError("transfer parent or deadline is no longer valid")
    print(json.dumps(transfer(request), allow_nan=False))


if __name__ == "__main__":
    main()
