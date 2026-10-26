"""CPU-only counter identity regressions."""

from unittest import mock
import ctypes as C

import pytest

from hyperloom import gpu_counters


NVML_EXPORTS = ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2",
                "nvmlDeviceGetUUID", "nvmlDeviceGetHandleByPciBusId_v2",
                "nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2")


CUDA_EXPORTS = ("cuInit", "cuDeviceGetCount", "cuDeviceGet", "cuDeviceGetLuid", "cuDeviceGetPCIBusId")


def test_15_supported_exports_map_luids_by_pci_not_ordinal(monkeypatch):
    nvml = mock.Mock(spec_set=NVML_EXPORTS)
    cuda = mock.Mock(spec_set=CUDA_EXPORTS)
    nvml.nvmlInit_v2.return_value = nvml.nvmlShutdown.return_value = 0
    def count(out):
        out._obj.value = 2
        return 0
    def device(out, ordinal):
        out._obj.value = ordinal + 10
        return 0
    def pci(out, size, dev):
        out.value = f"{0:04x}:{dev.value:02x}:{0:02x}.{0}".encode()
        return 0
    def handle(pci, out):
        out._obj.value = int(pci[5:7], 16)
        return 0
    def identity(handle, out, size):
        out.value = f"gpu-test-{handle.value}".encode()
        return 0
    def luid(out, mask, dev):
        C.memmove(out, dev.value.to_bytes(4, "little") + (9).to_bytes(4, "little"), 8)
        mask._obj.value = 1
        return 0
    nvml.nvmlDeviceGetUUID.side_effect = identity
    nvml.nvmlDeviceGetHandleByPciBusId_v2.side_effect = handle
    cuda.cuInit.return_value = 0
    cuda.cuDeviceGetCount.side_effect = count
    cuda.cuDeviceGet.side_effect = device
    cuda.cuDeviceGetPCIBusId.side_effect = pci
    cuda.cuDeviceGetLuid.side_effect = luid
    monkeypatch.setattr(C, "CDLL", lambda _: nvml)
    monkeypatch.setattr(C, "WinDLL", lambda _: cuda, raising=False)
    observer = gpu_counters.Nvml()
    # Reversed NVML inventory, unrelated to CUDA ordinals.
    monkeypatch.setattr(observer, "devices", lambda: [("gpu-test-11", C.c_void_p(11)),
                                                       ("gpu-test-10", C.c_void_p(10))])
    assert observer.luids() == {(9, 10): "gpu-test-10", (9, 11): "gpu-test-11"}
    cuda.cuInit.assert_called_once_with(0)
    assert cuda.cuDeviceGetLuid.argtypes == [C.POINTER(C.c_char), C.POINTER(C.c_uint), C.c_int]
    assert cuda.cuDeviceGetPCIBusId.argtypes == [C.POINTER(C.c_char), C.c_int, C.c_int]
    assert nvml.nvmlDeviceGetHandleByPciBusId_v2.argtypes == [C.c_char_p, C.POINTER(C.c_void_p)]
    for failure in ("duplicate", "missing", "zero-mask", "api"):
        if failure == "duplicate":
            cuda.cuDeviceGet.side_effect = lambda out, ordinal: device(out, 0)
        elif failure == "missing":
            cuda.cuDeviceGet.side_effect = device
            cuda.cuDeviceGetCount.side_effect = lambda out: (setattr(out._obj, "value", 1) or 0)
        elif failure == "zero-mask":
            cuda.cuDeviceGetCount.side_effect = count
            cuda.cuDeviceGetLuid.side_effect = lambda out, mask, dev: 0
        else:
            cuda.cuInit.return_value = 3
        with pytest.raises(gpu_counters.CounterError):
            observer.luids()
