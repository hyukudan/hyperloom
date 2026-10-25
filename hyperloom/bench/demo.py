"""Live streaming demo (F31): up to 32 parallel prompts in terminal-style tiles.

One stream per tile, admitted like a benchmark. While a tile streams it shows stream events per second (labelled:
events are not tokens) and its peak; when the final usage arrives it shows the end-to-end tok/s (measured: engine
completion tokens / time from request write to last output) and the client decode estimate. The aggregate
sparkline is the sum of the tiles' events per second, sampled every 0.5 s. Reasoning text is kept apart (folded in
the page). The job writes the session to its run row (text tails of 6 KiB, reasoning tails of 3 KiB); the last 20
sessions per target reopen read-only.
"""

import collections
import json
import threading
import time

from hyperloom.bench import data, runs
from hyperloom.web.api import redact


OWN = ("prompt_set", "count", "texts", "max_tokens")
SETS = ("structured", "prose", "code", "json")
MAX_TILES = 32
MAX_TEXT = 4000
MAX_BODY = 56 * 1024
TEXT_TAIL, REASONING_TAIL = 6144, 3072
SAMPLES = 1200


def validate(api, arguments):
    args, _ = runs.validate_common(api, arguments, OWN)
    texts = arguments.get("texts")
    if texts is not None:
        if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_TILES
                or any(not isinstance(t, str) or not 1 <= len(t) <= MAX_TEXT for t in texts)):
            raise ValueError(f"texts: 1..{MAX_TILES} prompts of 1..{MAX_TEXT} characters")
        prompts = list(texts)
        args.update(texts=prompts, prompt_set=None, count=len(prompts))
    else:
        prompt_set = arguments.get("prompt_set", "prose")
        if prompt_set not in SETS:
            raise ValueError("prompt_set must be one of " + ", ".join(SETS))
        count = runs._int(arguments, "count", 1, MAX_TILES, 8)
        items = data("prompts.json")["sets"][prompt_set]
        prompts = [items[i % len(items)] for i in range(count)]
        args.update(texts=None, prompt_set=prompt_set, count=count)
    if len(json.dumps(arguments, ensure_ascii=True).encode()) > MAX_BODY:
        raise ValueError("prompts exceed 56 KiB in total")
    args["max_tokens"] = runs._int(arguments, "max_tokens", 16, 2048, 512)
    args["prompts"] = prompts
    planned = sum(runs.request_bound({"model": "m" * 200, "messages": [{"role": "user", "content": p}],
                                      "max_tokens": args["max_tokens"]}) for p in prompts)
    return runs.check_plan(args, planned)


class Tile:
    def __init__(self, index, prompt):
        self.index, self.prompt = index, prompt
        self.text, self.reasoning = "", ""
        self.state = "waiting"
        self.events = collections.deque()
        self.count = 0
        self.peak = 0.0
        self.final = None
        self.lock = threading.Lock()

    def delta(self, piece, thought, now):
        with self.lock:
            self.state = "streaming"
            self.text = (self.text + piece)[-TEXT_TAIL:]
            self.reasoning = (self.reasoning + thought)[-REASONING_TAIL:]
            self.count += 1
            clock = time.monotonic()
            self.events.append(clock)
            self._trim(clock)
            self.peak = max(self.peak, float(len(self.events)))

    def _trim(self, clock):
        while self.events and clock - self.events[0] > 1.0:
            self.events.popleft()

    def rate(self):
        with self.lock:
            self._trim(time.monotonic())
            return float(len(self.events)) if self.state == "streaming" else 0.0

    def view(self):
        with self.lock:
            self._trim(time.monotonic())
            view = {"index": self.index, "prompt": redact(self.prompt[:300]), "state": self.state, "text": redact(self.text),
                    "reasoning": redact(self.reasoning), "events": self.count,
                    "events_per_s": float(len(self.events)) if self.state == "streaming" else None,
                    "peak_events_per_s": self.peak or None}
            if self.final:
                view.update(self.final)
            return view


def run(run):
    args = run.arguments
    tiles = [Tile(i, p) for i, p in enumerate(args["prompts"])]
    samples = []
    stop = threading.Event()
    run.summary.update(tiles=len(tiles), max_tokens=args["max_tokens"],
                       labels={"events_per_s": "stream events per second (not tokens)",
                               "e2e_rate": runs.RATE_BASIS["e2e_rate"], "client_rate": runs.RATE_BASIS["client_rate"]})
    started = time.monotonic()

    def publish(force=False):
        run.detail["tiles"] = [tile.view() for tile in tiles]
        run.detail["aggregate"] = samples[-SAMPLES:]
        run.summary["aggregate_events_per_s_peak"] = max((s[1] for s in samples), default=None)
        run.summary["streaming"] = sum(1 for t in tiles if t.state == "streaming")
        run.summary["done"] = sum(1 for t in tiles if t.state in ("done", "failed"))
        run.live(force=force)

    def ticker():
        while not stop.wait(0.5):
            samples.append([round(time.monotonic() - started, 2), sum(tile.rate() for tile in tiles)])
            publish()
    thread = threading.Thread(target=ticker, name="hyperloom-bench-demo", daemon=True)
    thread.start()

    def stream(tile):
        request = runs.body(run, [{"role": "user", "content": tile.prompt}], args["max_tokens"])
        reply = run.chat(request, 120 + args["max_tokens"] * 0.5, on_delta=tile.delta)
        figures = runs.measure(reply)
        with tile.lock:
            tile.state = "done" if reply.ok else ("cancelled" if reply.cancelled else "failed")
            tile.final = {"completion_tokens": figures["completion_tokens"], "ttft_s": figures["ttft_s"],
                          "e2e_rate": figures["e2e_rate"], "client_rate": figures["client_rate"],
                          "error": figures["error"], "why": figures["why"]}
        return figures
    try:
        runs.parallel(stream, tiles, len(tiles))
    finally:
        stop.set()
        thread.join(2.0)
        publish(force=True)
    done = [t.final for t in tiles if t.final and t.final["e2e_rate"] is not None]
    run.summary.update(completed=len(done), total_completion_tokens=sum(t["completion_tokens"] for t in done) if done else None,
                       e2e_rate_p50=runs.percentile([t["e2e_rate"] for t in done], 50))


def share(summary):
    return {k: summary.get(k) for k in ("tiles", "max_tokens", "completed", "e2e_rate_p50", "aggregate_events_per_s_peak")}
