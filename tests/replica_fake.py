"""CPU-only fake of the replica's positional argv and serve spool (never the legacy HTTP fake)."""
import json
from pathlib import Path
import threading
import time
from unittest import mock

from hyperloom.recipes import content_cache


class FakeReplica:
    instances = []

    def __init__(self, argv, env, directory, name, port=None):
        assert len(argv) > 2 and not argv[1].startswith("--")
        assert not {"--host", "--port", "--api-key"} & set(argv)
        self.argv, self.env, self.directory = argv, env, Path(directory)
        self.spool = Path(argv[argv.index("--serve") + 1])
        self.process = mock.Mock(pid=4242, returncode=None)
        self.stopped = False
        self.requests = []
        self.thread = None
        self.instances.append(self)

    def start(self):
        for name in ("queue", "run", "done", "lock"):
            (self.spool / name).mkdir(parents=True)
        content_cache._write(self.spool / "ready.json", dict(event="ready", load_id=42, pid=4242))
        self.thread = threading.Thread(target=self._run)
        self.thread.start()
        return self

    def _run(self):
        try:
            while not self.stopped:
                for request in sorted((self.spool / "queue").glob("*.req")):
                    running = self.spool / "run" / request.name
                    request.rename(running)
                    args = running.read_text().splitlines()
                    self.requests.append(args)
                    rc = 2 if "--not-runtime" in args else 0
                    content_cache._write(self.spool / "done" / (request.stem + ".json"),
                        dict(name=request.stem, rc=rc, load_id=42, args=args))
                    running.rename(self.spool / "done" / request.name)
                    if args == ["--quit"]:
                        return
                time.sleep(.005)
        finally:
            (self.spool / "lock").rmdir()

    def alive(self):
        return not self.stopped

    def stop(self, grace_s=5):
        self.stopped = True
        if self.thread is not None:
            self.thread.join(grace_s)
