"""Bounded in-memory statistics. Use one Window per component instance.

Rates compare consecutive counter samples. Requests use the engine's Unix
``time`` field (request creation time, not client timing), newest first or last.
"""

from dataclasses import dataclass
import math
import time

from .adapters.engine import TTFT_LABEL


def _number(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Rate:
    value: float | None
    reset: bool = False
    why: str | None = None
    label: str = "completed counter units/s over the sampling window"


@dataclass(frozen=True)
class Quantile:
    value: float | None
    n: int
    label: str
    why: str | None = None


class Window:
    def __init__(self, max_requests=50, max_age_s=600, clock=time.time):
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        if not _number(max_age_s) or max_age_s == 0:
            raise ValueError("max_age_s must be positive and finite")
        self.max_requests = max_requests
        self.max_age_s = max_age_s
        self.clock = clock
        self._counters = {}
        self._requests = {}

    def _now(self, now):
        now = self.clock() if now is None else now
        if not _number(now):
            raise ValueError("sample time must be nonnegative and finite")
        return now

    def clear(self):
        """Discard the old instance's baselines and request history."""
        self._counters.clear()
        self._requests.clear()

    def rate(self, name, value, now=None) -> Rate:
        """Counter units per second; decreases restart the baseline.

        A reset clears requests too, so reused ids cannot mix engine instances.
        Feed counters before requests when processing one poll. Missing samples
        break the baseline; duplicate/backward sample times do not replace it.
        """
        now = self._now(now)
        if not _number(value):
            self._counters.pop(name, None)
            return Rate(None, why="counter unavailable")
        previous = self._counters.get(name)
        if previous is not None and now <= previous[0]:
            return Rate(None, why="sample time did not advance")
        self._counters[name] = now, value
        if previous is None:
            return Rate(None, why="requires two counter samples")
        if value < previous[1]:
            self.clear()
            self._counters[name] = now, value
            return Rate(None, reset=True, why="counter decreased; component restarted")
        return Rate((value - previous[1]) / (now - previous[0]))

    def _prune(self, now):
        recent = [(key, request) for key, request in self._requests.items()
                  if now - self.max_age_s <= request["time"] <= now]
        # Id breaks timestamp ties deterministically across overlapping polls.
        recent.sort(key=lambda item: (item[1]["time"], item[0]))
        self._requests = dict(recent[-self.max_requests:])

    def add_requests(self, requests, now=None):
        """Merge completed request records by id and return the retained count.

        Malformed records and records without an id/time are unavailable samples.
        Existing ids are immutable; polling them again does not refresh their age.
        """
        now = self._now(now)
        self._prune(now)
        for request in requests or ():
            if not isinstance(request, dict):
                continue
            key, stamp = request.get("id"), request.get("time")
            if (not isinstance(key, str) or not key or not _number(stamp)
                    or not now - self.max_age_s <= stamp <= now):
                continue
            self._requests.setdefault(key, dict(request))
        self._prune(now)
        return len(self._requests)

    def quantile(self, field, q, now=None) -> Quantile:
        """Linear interpolation at (n-1)*q, for q in [0, 1]; n counts valid values."""
        if not _number(q) or q > 1:
            raise ValueError("q must be between zero and one")
        self._prune(self._now(now))
        label = TTFT_LABEL if field == "engine_ttft_ms" else field
        values = []
        for request in self._requests.values():
            value = request.get(field)
            if field == "engine_ttft_ms":
                queue, prefill = request.get("queue_ms"), request.get("prefill_ms")
                value = queue + prefill if _number(queue) and _number(prefill) else None
            if _number(value):
                values.append(value)
        values.sort()
        n = len(values)
        if not n:
            return Quantile(None, 0, label, "no available request samples")
        position = (n - 1) * q
        lo, hi = math.floor(position), math.ceil(position)
        value = values[lo] + (values[hi] - values[lo]) * (position - lo)
        return Quantile(value, n, label)
