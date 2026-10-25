"""The life of one benchmark run, shared by every kind (portal job or CLI).

``validate_common`` checks the arguments every kind shares and adds a ``run_id`` (so the page can poll the run as
soon as its job is accepted). ``execute`` resolves the target inside the job (router address and key, then
``GET /v1/models``), writes the run row, calls the kind's ``run(run)`` and finalises the row as succeeded, failed,
cancelled or "budget reached". ``Run.cancelled()`` is true on the job's cancellation (cancel request, stop of an
overlapping profile, deadline, abandoned page) and once the counted usage reaches the token budget; streams poll
it through their watchdog and close.

The CLI runs the same ``execute`` with ``CLIContext`` and takes its own admission hold (``hold``).
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
import signal
import sys
import threading
import time
import uuid

from hyperloom import admission
from hyperloom.web.api import redact

from . import client, store as bench_store, targets


DEFAULT_BUDGET = 400_000
MAX_BUDGET = 16_000_000
LIVE_INTERVAL_S = 0.5
DEADLINES = {"decode": 1800, "prefill": 3600, "ab": 3600, "quality": 3600, "tools": 1800, "demo": 900}
COMMON = ("target", "model", "token_budget", "trace", "sampling", "thinking", "run_id", "planned_tokens", "target_digest")


class BudgetReached(Exception):
    pass


def request_bound(request, context_tokens=None):
    """The generated tokens a request may consume: its ``max_tokens``, the protocol's own ceiling, enforced by the
    endpoint. (The token budget counts generated tokens; prompt tokens are fixed by the run's plan.)"""
    return int(request.get("max_tokens") or 0)


def check_plan(arguments, planned):
    """Refuse a plan whose generated tokens (the sum of its max_tokens) exceed the token budget."""
    if planned > arguments["token_budget"]:
        raise ValueError(f"this run can generate up to {planned} tokens, above its token budget of "
                         f"{arguments['token_budget']}; raise token_budget or shrink the run")
    arguments["planned_tokens"] = planned
    return arguments


def _int(arguments, key, low, high, default=None):
    value = arguments.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer in {low}..{high}")
    return value


def _bool(arguments, key, default=False):
    value = arguments.get(key, default)
    if type(value) is not bool:
        raise ValueError(key + " must be true or false")
    return value


def check_sampling(value, row):
    """None (greedy) or {temperature, top_p, seed}; refused unless the target honours sampling."""
    if value is None:
        return None
    if not row.get("sampling"):
        raise ValueError("sampling is not available on this target: " + (row.get("sampling_why") or "not honoured"))
    if not isinstance(value, dict) or set(value) - {"temperature", "top_p", "seed"}:
        raise ValueError("sampling takes temperature, top_p and seed")
    temperature, top_p, seed = value.get("temperature", 0.7), value.get("top_p", 1.0), value.get("seed", 1)
    if type(temperature) not in (int, float) or not 0 < temperature <= 2:
        raise ValueError("sampling.temperature must be in (0, 2]")
    if type(top_p) not in (int, float) or not 0 < top_p <= 1:
        raise ValueError("sampling.top_p must be in (0, 1]")
    if type(seed) is not int or not 0 <= seed < 2 ** 31:
        raise ValueError("sampling.seed must be an integer in 0..2^31-1")
    return {"temperature": float(temperature), "top_p": float(top_p), "seed": seed}


def validate_common(api, arguments, own_keys):
    """Shared validation: returns (normalised arguments, target row)."""
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = set(arguments) - set(COMMON) - set(own_keys)
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)))
    parsed = targets.parse(arguments.get("target"))
    row = targets.describe(api, parsed)
    model = arguments.get("model")
    if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 200):
        raise ValueError("model must be a model id")
    thinking = arguments.get("thinking")
    if thinking is not None:
        if type(thinking) is not bool:
            raise ValueError("thinking must be true, false or absent")
        if not row.get("thinking"):
            raise ValueError("thinking cannot be switched on this target: " + (row.get("thinking_why") or "not declared"))
    normalised = {"target": targets.as_argument(parsed), "model": model,
                  "token_budget": _int(arguments, "token_budget", 1000, MAX_BUDGET, DEFAULT_BUDGET),
                  "trace": _bool(arguments, "trace"), "sampling": check_sampling(arguments.get("sampling"), row),
                  "thinking": thinking, "run_id": uuid.uuid4().hex[:16],
                  "target_digest": targets.digest(api, parsed)}
    return normalised, row


def body(run, messages, max_tokens, **extra):
    """The request body every kind sends (greedy unless the target honours sampling and the run asked for it)."""
    request = {"model": run.model, "messages": messages, "max_tokens": max_tokens, "stream": True,
               "stream_options": {"include_usage": True}, "return_token_ids": True}
    if run.arguments.get("sampling"):
        request.update(run.arguments["sampling"])
    else:
        request["temperature"] = 0
    if run.arguments.get("thinking") is not None:
        request["chat_template_kwargs"] = {"enable_thinking": run.arguments["thinking"]}
    request.update(extra)
    return request


def percentile(values, q):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return None
    rank = max(1, math.ceil(q / 100 * len(values)))
    return values[rank - 1]


def rounded(value, digits=3):
    return None if value is None else round(value, digits)


RATE_BASIS = {"e2e_rate": "measured: completion tokens / (last output - request write), client",
              "client_rate": "estimated: (completion tokens - 1) / (last output - first output), client; the first "
                             "output event may carry more than one token",
              "engine_rate": "engine-reported: (completion tokens - 1) / (decode_ms / 1000)"}


def measure(reply):
    """One reply's figures, each with its observer: client times (s, from the request write), engine usage and
    timings, router headers. Rates are None with a reason when their inputs are missing (bases: RATE_BASIS)."""
    completion = reply.completion_tokens
    row = {"ok": reply.ok, "status": reply.status, "error": reply.error, "ttft_s": rounded(reply.ttft_s, 4),
           "last_output_s": rounded(reply.last_output_s, 4), "end_s": rounded(reply.end_s, 4),
           "prompt_tokens": reply.prompt_tokens, "completion_tokens": completion, "cached_tokens": reply.cached_tokens,
           "events": reply.content_events, "finish_reason": reply.finish_reason,
           "replica": reply.headers.get("x-tensormeld-replica"), "prefill_place": reply.headers.get("x-tensormeld-prefill"),
           "client_rate": None, "engine_rate": None, "why": {}}
    if completion is not None and completion >= 1 and reply.last_output_s and reply.last_output_s > 0:
        row["e2e_rate"] = round(completion / reply.last_output_s, 3)
    else:
        row["e2e_rate"] = None
        row["why"]["e2e_rate"] = "no usage or no output"
    if completion is None:
        row["why"]["client_rate"] = row["why"]["engine_rate"] = "the endpoint reported no usage (stream events are not tokens)"
    elif completion < 2:
        row["why"]["client_rate"] = row["why"]["engine_rate"] = "fewer than two generated tokens"
    else:
        span = (reply.last_output_s or 0) - (reply.ttft_s or 0)
        if reply.ttft_s is not None and reply.last_output_s is not None and span > 0:
            row["client_rate"] = round((completion - 1) / span, 3)
        else:
            row["why"]["client_rate"] = "all output arrived in one event (no measurable interval)"
        decode_ms = reply.engine_ms("decode_ms")
        if decode_ms and decode_ms > 0:
            row["engine_rate"] = round((completion - 1) / (decode_ms / 1000.0), 3)
        else:
            row["why"]["engine_rate"] = "the engine reported no decode time"
    if not reply.ok:
        row["why"]["ok"] = reply.error or f"HTTP {reply.status}"
    if row["error"]:
        row["error"] = redact(row["error"])
    return row


class Run:
    def __init__(self, api, kind, arguments, ctx, resolved, model, context_tokens, tokenizer, store):
        self.api, self.kind, self.arguments, self.ctx = api, kind, arguments, ctx
        self.id = arguments["run_id"]
        self.resolved, self.model, self.context_tokens, self.tokenizer = resolved, model, context_tokens, tokenizer
        self.store = store
        self.budget = arguments["token_budget"]
        self.used = 0              # generated tokens settled against the budget
        self.prompt_used = 0       # prompt tokens reported by the engine (counted, not budgeted)
        self.reserved = 0
        self.requests = 0
        self.started = time.time()
        self.summary, self.detail = {}, {}
        self.traces = [] if arguments.get("trace") else None
        self.budget_hit = False
        self.violation = None
        self._lock = threading.Lock()
        self._written = 0.0
        self._sequence = 0

    # -- cancellation and budget ---------------------------------------------------------------------------------
    def cancelled(self):
        return self.violation is not None or self.ctx.cancelled()

    def reserve(self, request, context_tokens=None):
        """Reserve the request's max_tokens before sending it; BudgetReached when it does not fit."""
        bound = request_bound(request, context_tokens)
        with self._lock:
            if self.used + self.reserved + bound > self.budget:
                self.budget_hit = True
                raise BudgetReached()
            self.reserved += bound
        return bound

    def settle(self, bound, reply, max_tokens):
        """Replace the reservation by the engine's completion count (without usage the reservation stays charged).
        A reply with more completion tokens than its max_tokens stops the run (protocol violation)."""
        prompt, completion = reply.prompt_tokens, reply.completion_tokens
        with self._lock:
            self.reserved -= bound
            self.used += completion if completion is not None else bound
            self.prompt_used += prompt or 0
            self.requests += 1
            if self.violation is None and completion is not None and completion > max_tokens:
                self.violation = (f"budget bound violated by the endpoint: max_tokens {max_tokens}, "
                                  f"reported {completion} completion tokens")

    def request_id(self):
        with self._lock:
            self._sequence += 1
            return f"hlb-{self.id}-{self._sequence}"

    def chat(self, request, timeout, on_delta=None, endpoint=None, context_tokens=None):
        """Send one request (reserved against the budget first) to the run's endpoint (or ``endpoint`` with its own
        ``context_tokens``)."""
        if self.ctx.cancelled():
            return client.Reply(error="cancelled", cancelled=True)
        bound = self.reserve(request, context_tokens if endpoint is not None else self.context_tokens)
        request_id = self.request_id()
        deadline = getattr(self.ctx, "deadline", None)
        if deadline is not None:
            timeout = max(1.0, min(timeout, deadline - time.monotonic()))
        try:
            reply = client.chat(endpoint or self.resolved.endpoint, request, timeout=timeout, cancelled=self.cancelled,
                                request_id=request_id, on_delta=on_delta)
        except BaseException:
            with self._lock:
                self.reserved -= bound
                self.used += bound
            raise
        self.settle(bound, reply, int(request.get("max_tokens") or 0))
        if self.traces is not None:
            canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            with self._lock:
                if len(self.traces) < 2000:
                    self.traces.append({"request_id": request_id, "body_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                                        "router_route_id": reply.headers.get("x-tensormeld-route-id"),
                                        "router_request_id": reply.headers.get("x-tensormeld-request-id"),
                                        "status": reply.status, "error": reply.error, "event_times_s": reply.event_times})
        return reply

    # -- persistence -------------------------------------------------------------------------------------------
    def _row(self, state, finished=None):
        detail = dict(self.detail)
        if self.traces is not None:
            detail["traces"] = self.traces
        summary = {"kind": self.kind, "model": self.model, "target": self.resolved.label, "requests": self.requests,
                   "tokens_used": self.used, "token_budget": self.budget, "progress": self.ctx_progress,
                   "budget_counts": "generated tokens (each request reserves its max_tokens)",
                   "prompt_tokens_used": self.prompt_used,
                   "family": self.resolved.family, **self.summary}
        return {"id": self.id, "kind": self.kind, "target_key": self.resolved.key, "target_label": self.resolved.label,
                "state": state, "started": self.started, "finished": finished, "model": self.model,
                "recipe_digest": self.resolved.identity.get("recipe_digest"), "summary": summary, "detail": detail}

    ctx_progress = "starting"

    def progress(self, text):
        self.ctx_progress = str(text)[:300]
        self.ctx(text)

    def live(self, force=False):
        now = time.monotonic()
        if force or now - self._written >= LIVE_INTERVAL_S:
            self._written = now
            with self._lock:
                row = self._row("running")
            bench_store.write(self.store, row)

    def finish(self, state, reason=None):
        with self._lock:
            if reason:
                self.summary["reason"] = str(reason)[:300]
            row = self._row(state, time.time())
        bench_store.write(self.store, row)
        bench_store.prune(self.store, self.kind, self.resolved.key)

    def identity(self, declared_tokenizer=None):
        return identity(self.resolved, self.model, self.tokenizer, declared_tokenizer)


def identity(resolved, model, tokenizer, declared_tokenizer=None):
    """A target's identity with provenance (no credentials: the URL is scheme://host:port only). An unknown
    tokenizer id is omitted (its source says "unavailable"), so the document survives redaction unchanged."""
    found = {"url": resolved.endpoint.url, "label": resolved.label, "model": model, **resolved.identity}
    if tokenizer:
        found.update(tokenizer_id=tokenizer, tokenizer_source="reported")
    elif declared_tokenizer:
        found.update(tokenizer_id=declared_tokenizer, tokenizer_source="declared")
    else:
        found["tokenizer_source"] = "unavailable"
    return found


def execute(api, kind, arguments, ctx, runner, store=None):
    """Run one benchmark of ``kind`` with ``runner(run)`` (fills run.summary / run.detail). Returns the job result
    (None after a cancellation)."""
    store = store or bench_store.opened(api)
    bench_store.mark_stale(store)
    parsed = targets.parse(arguments["target"])
    try:
        resolved = targets.resolve(api, parsed, arguments.get("target_digest"))
        model, context, tokenizer = targets.probe_model(resolved, ctx.cancelled, arguments.get("model"))
    except BaseException as exc:
        cancelled = ctx.cancelled()
        bench_store.write(store, {"id": arguments["run_id"], "kind": kind, "target_key": targets.key_of(parsed),
                                  "target_label": parsed[1], "state": "cancelled" if cancelled else "failed",
                                  "started": time.time(), "finished": time.time(), "model": None, "recipe_digest": None,
                                  "summary": {"kind": kind, "reason": (ctx.reason if cancelled else
                                              f"{type(exc).__name__}: {exc}")[:300]}, "detail": None})
        if cancelled:
            return None
        raise
    context = resolved.context_tokens or context
    run = Run(api, kind, arguments, ctx, resolved, model, context, tokenizer, store)
    run.live(force=True)
    return finish_with(run, ctx, runner)


def finish_with(run, ctx, runner):
    """Run ``runner(run)`` and finalise the row: succeeded, budget reached, cancelled (returns None) or failed."""
    kind = run.kind
    try:
        runner(run)
        if run.violation:
            raise BudgetReached()
    except BudgetReached:
        if run.violation:
            run.finish("budget bound violated", run.violation)
            return {"run_id": run.id, "kind": kind, "state": "budget bound violated"}
        run.finish("budget reached", f"the token budget ({run.budget}) would be exceeded after {run.used} tokens")
        return {"run_id": run.id, "kind": kind, "state": "budget reached"}
    except BaseException as exc:
        if run.violation:
            run.finish("budget bound violated", run.violation)
            return {"run_id": run.id, "kind": kind, "state": "budget bound violated"}
        if ctx.cancelled():
            run.finish("cancelled", ctx.reason or "cancelled")
            return None
        run.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    if ctx.cancelled():
        run.finish("cancelled", ctx.reason or "cancelled")
        return None
    if run.budget_hit:
        run.finish("budget reached", f"the token budget ({run.budget}) would be exceeded after {run.used} tokens")
        return {"run_id": run.id, "kind": kind, "state": "budget reached"}
    run.finish("succeeded")
    return {"run_id": run.id, "kind": kind, "state": "succeeded"}


def parallel(work, items, limit):
    """Run work(item) for every item with at most ``limit`` threads; results in item order."""
    results = [None] * len(items)
    errors = []
    lock = threading.Lock()
    queue = list(enumerate(items))

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                index, item = queue.pop(0)
            try:
                results[index] = work(item)
            except BaseException as exc:      # surfaced after every worker returned
                with lock:
                    errors.append(exc)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(limit, len(items))))]
    started = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except BaseException:
        # No further item starts; the workers already running finish theirs before the run (and its admission
        # hold) ends.
        with lock:
            queue.clear()
        for thread in started:
            thread.join()
        raise
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


# -- CLI -------------------------------------------------------------------------------------------------------
class CLIContext:
    """The job context of a CLI run: Ctrl-C cancels; the deadline and the admission hold (a stop of an overlapping
    profile from the portal or another CLI cancels it) are checked too."""

    def __init__(self, deadline_s, hold_id=None, admission_db=None, quiet=False):
        self.deadline = time.monotonic() + deadline_s
        self.reason = None
        self.hold, self._admission = hold_id, admission_db
        self._event = threading.Event()
        self._checked = 0.0
        self.quiet = quiet

    def __call__(self, text):
        if not self.quiet:
            print("bench: " + str(text), file=sys.stderr, flush=True)

    def cancel(self, reason):
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def cancelled(self):
        if not self._event.is_set():
            now = time.monotonic()
            if now >= self.deadline:
                self.cancel("deadline reached")
            elif self._admission is not None and self.hold and now - self._checked >= 1.0:
                self._checked = now
                reason = self._admission.cancelled(self.hold)
                if reason:
                    self.cancel(reason)
        return self._event.is_set()

    def wait(self, seconds):
        end = time.monotonic() + max(0.0, seconds)
        while not self.cancelled():
            left = min(end, self.deadline) - time.monotonic()
            if left <= 0:
                return self.cancelled()
            self._event.wait(min(left, 0.5))
        return True


@contextmanager
def hold(config_path, resources, label):
    """A CLI run's admission hold (C11): acquired or Refused, renewed every 30 s, released at the end."""
    db = admission.for_config(config_path)
    hold_id = db.acquire("bench", resources, label=label)
    stop = threading.Event()

    def renew():
        while not stop.wait(30):
            try:
                db.renew(hold_id)
            except Exception:
                pass
    thread = threading.Thread(target=renew, name="hyperloom-bench-hold", daemon=True)
    thread.start()
    try:
        yield db, hold_id
    finally:
        stop.set()
        thread.join(5)
        db.release(hold_id)


@contextmanager
def interrupt_cancels(context):
    """Ctrl-C (and SIGTERM where available) cancels the CLI run instead of killing it mid-stream."""
    previous = {}

    def handler(signum, frame):
        context.cancel("interrupted")
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None and threading.current_thread() is threading.main_thread():
            try:
                previous[number] = signal.signal(number, handler)
            except (ValueError, OSError):
                pass
    try:
        yield
    finally:
        for number, value in previous.items():
            signal.signal(number, value)


def pid():
    return os.getpid()
