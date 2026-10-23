"""Stand-in for the CUDA prefill service: same argv shape, stdout events and endpoints; no GPU."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys

argv = sys.argv[1:]
port = int(argv[argv.index("--port") + 1])
key = argv[argv.index("--api-key") + 1]
served = 0


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "busy": False, "queued": 0, "served": served, "max_positions": 32768})
        else:
            self.reply(404, {"error": "unknown"})

    def do_POST(self):
        global served
        if self.headers.get("Authorization") != "Bearer " + key:
            return self.reply(401, {"error": "unauthorized"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        served += 1
        self.reply(200, {"tokens": len(body["tokens"]), "sha256": "0" * 64, "next_token": 1,
                         "fast": {"dense": False, "moe": False, "attention": False}})

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
print(json.dumps({"event": "loaded", "env_device": os.environ.get("CUDA_VISIBLE_DEVICES")}), flush=True)
print(json.dumps({"event": "READY", "http": f"127.0.0.1:{port}"}), flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
print(json.dumps({"event": "STOPPED"}), flush=True)
