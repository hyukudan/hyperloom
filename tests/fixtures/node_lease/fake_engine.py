"""Synthetic engine events and loopback health; no component or GPU code."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--adapter", choices=("qwen", "ds41", "silent"), default="qwen")
parser.add_argument("--events", required=True)
parser.add_argument("--pid-file", required=True)
parser.add_argument("--exit-after", type=float)
parser.add_argument("--grandchild", action="store_true")
parser.add_argument("--http-api-key-file")
parser.add_argument("--rank", type=int, default=0)
args = parser.parse_args()


def event(name):
    with Path(args.events).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": name, "pid": os.getpid()}) + "\n")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        valid = self.path == "/admin/drain"
        if args.http_api_key_file:
            valid = valid and self.headers.get("Authorization") == "Bearer " + Path(args.http_api_key_file).read_text().strip()
        event("drain" if valid else "bad-drain")
        self.send_response(200 if valid else 403)
        self.end_headers()

    def log_message(self, *_):
        pass


server = None
if args.rank == 0:
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
child = None
if args.grandchild:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(args.pid_file).write_text(json.dumps({"pid": os.getpid(), "child": child.pid if child else None,
                                        "env": dict(os.environ)}))
event("start")
if args.adapter == "qwen":
    print(f"strix serve: listening on 127.0.0.1:{args.port} model synthetic", file=sys.stderr, flush=True)
elif args.adapter == "ds41":
    print(json.dumps({"event": "READY", "rank": args.rank}), flush=True)
stopped = threading.Event()


def stop(*_):
    event("term")
    stopped.set()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGHUP, stop)
stopped.wait(args.exit_after)
if child:
    child.terminate()
    child.wait()
if server is not None:
    server.shutdown()
event("exit")
