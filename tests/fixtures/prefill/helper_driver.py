"""Runs rtx.service_run in its own process (as `hyperloom _prefill-run` does) with GPU queries stubbed out."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hyperloom import procs, rtx  # noqa: E402


class NoGpuDog(rtx.Watchdog):
    def __init__(self, *args, **kw):
        super().__init__(*args, apps=lambda: [], used=lambda uuid: 0, **kw)


rtx.service_run(sys.argv[1], watchdog_factory=NoGpuDog, verify=lambda uuid: {"uuid": uuid},
                lock_factory=lambda uuid: procs.InstanceLock(Path(sys.argv[1]).parent / "device.lock"))
