"""CPU-only counter identity regressions."""

from unittest import mock
import ctypes as C

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import gpu_counters


NVML_EXPORTS = ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2",
                "nvmlDeviceGetUUID", "nvmlDeviceGetHandleByPciBusId_v2",
                "nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2")


def test_15_supported_exports_map_luids_by_pci_not_ordinal(monkeypatch):
    nvml = mock.Mock(spec_set=NVML_EXPORTS)
    nvml.nvmlInit_v2.return_value = nvml.nvmlShutdown.return_value = 0
    def handle(pci, out):
        out._obj.value = int(pci[5:7], 16)
        return 0
    def identity(handle, out, size):
        out.value = f"gpu-test-{handle.value}".encode()
        return 0
    nvml.nvmlDeviceGetUUID.side_effect = identity
    nvml.nvmlDeviceGetHandleByPciBusId_v2.side_effect = handle
    monkeypatch.setattr(C, "CDLL", lambda _: nvml)
    monkeypatch.setattr(C, "WinDLL", mock.Mock(side_effect=AssertionError("CUDA driver loaded")), raising=False)
    observer = gpu_counters.Nvml()
    # Reversed NVML inventory, unrelated to DXGI ordinals.
    monkeypatch.setattr(observer, "devices", lambda: [("gpu-test-11", C.c_void_p(11)),
                                                       ("gpu-test-10", C.c_void_p(10))])
    adapters = [(f"{0:04x}:{bus:02x}:{0:02x}.{0}".encode(), (9, bus)) for bus in (10, 11)]
    monkeypatch.setattr(gpu_counters, "dxgi_adapters", lambda: adapters)
    assert observer.luids() == {(9, 10): "gpu-test-10", (9, 11): "gpu-test-11"}
    C.WinDLL.assert_not_called()
    assert nvml.nvmlDeviceGetHandleByPciBusId_v2.argtypes == [C.c_char_p, C.POINTER(C.c_void_p)]
    for bad in ([adapters[0]] * 2, adapters[:1], [(adapters[0][0], (0, 0)), adapters[1]],
                [(b"0000:0c:00.0", (9, 12)), adapters[1]]):
        monkeypatch.setattr(gpu_counters, "dxgi_adapters", lambda: bad)
        with pytest.raises(gpu_counters.CounterError):
            observer.luids()
    monkeypatch.setattr(gpu_counters, "dxgi_adapters", mock.Mock(side_effect=OSError("unavailable")))
    with pytest.raises(gpu_counters.CounterError, match="unavailable"):
        observer.luids()
