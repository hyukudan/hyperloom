"""Local client for cuda_ds41_replica_serve.hpp's file protocol. No sockets or engine HTTP assumptions."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import uuid


class SpoolError(OSError):
    pass


class Client:
    def __init__(self, directory, *, alive):
        self.directory = Path(directory)
        self.alive = alive
        self._serial = threading.Lock()
        self._load_id = None

    def ready(self):
        if not self.alive() or not (self.directory / "lock").is_dir():
            return None
        try:
            event = json.loads((self.directory / "ready.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(event, dict) or event.get("event") != "ready"
                or type(event.get("load_id")) is not int):
            return None
        if self._load_id is not None and event["load_id"] != self._load_id:
            return None
        self._load_id = event["load_id"]
        return event

    def request(self, args, *, timeout=900):
        """One argument per LF line, published by rename; return the worker's done/NAME.json verbatim.

        Callers supply the replica's runtime options, including --json for request output. rc 2 is a request
        rejection, not loss of worker health. The single worker is serialized, including control requests.
        """
        if (not isinstance(args, (list, tuple)) or not args or any(
                not isinstance(a, str) or not a or a.startswith("#") or any(c in a for c in "\r\n\0")
                for a in args)):
            raise ValueError("spool arguments must be nonempty single lines")
        with self._serial:
            return self._request(args, timeout)

    def _request(self, args, timeout):
        if self.ready() is None:
            raise SpoolError("replica spool is not ready")
        name = uuid.uuid4().hex
        queued = self.directory / "queue" / (name + ".req")
        temporary = queued.with_suffix(".tmp")
        response = self.directory / "done" / (name + ".json")
        try:
            temporary.write_bytes(("\n".join(args) + "\n").encode("utf-8"))
            temporary.replace(queued)
            end = time.monotonic() + timeout
            while True:
                try:
                    result = json.loads(response.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    result = None
                if result is not None:
                    if (not isinstance(result, dict) or result.get("name") != name
                            or type(result.get("rc")) is not int):
                        raise SpoolError("invalid replica spool response")
                    return result
                if not self.alive():
                    raise SpoolError("replica exited before its spool response")
                if time.monotonic() >= end:
                    # Never retry a running request: it may have changed the worker's state.
                    raise SpoolError("replica spool response timed out")
                time.sleep(.05)
        finally:
            temporary.unlink(missing_ok=True)
            queued.unlink(missing_ok=True)

    def health(self):
        if self.ready() is None:
            return False
        if any((self.directory / "run").glob("*.req")):
            return self.alive()
        if not self._serial.acquire(blocking=False):
            return self.alive()  # a request already owns the single worker; never queue pings behind it
        try:
            result = self._request(["--ping"], 2)
            return result["rc"] == 0 and result.get("load_id") == self._load_id
        finally:
            self._serial.release()

    def shutdown(self, timeout=5):
        return self.request(["--quit"], timeout=timeout)
