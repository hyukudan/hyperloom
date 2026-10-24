"""Telemetry: the persistent store and the background samplers that feed it.

The portal process owns one store at ``<config dir>/telemetry.sqlite3`` (``store_for(api)``). Collectors are
registered by portal features (``reg.collector(name, sample, interval)``); ``start(api)`` runs one daemon thread
per collector. A collector returns ``[(series, labels, value), ...]``; its failures are counted, never raised.
"""

import threading
import time

from .store import Baselines, KINDS, Store, TIERS, canonical_labels

__all__ = ["Baselines", "KINDS", "Store", "TIERS", "canonical_labels", "store_for", "Sampler", "start", "status"]

_lock = threading.Lock()
_stores = {}
_samplers = {}


def store_for(api):
    """The store of this portal (tests set ``api.telemetry`` to an in-memory Store)."""
    existing = getattr(api, "telemetry", None)
    if existing is not None:
        return existing
    path = str(api.config_path.parent / "telemetry.sqlite3")
    with _lock:
        if path not in _stores:
            _stores[path] = Store(path, clock=getattr(api, "clock", time.time))
        store = _stores[path]
    api.telemetry = store
    return store


class Sampler:
    """One thread per collector; each runs `sample(api, now)` every `interval` seconds."""

    def __init__(self, api, store, collectors, clock=time.time):
        self.api, self.store, self.clock = api, store, clock
        self.collectors = dict(collectors)
        self.state = {name: {"interval_s": spec[1], "last_ok": None, "last_error": None, "error": None,
                             "points": 0, "failures": 0} for name, spec in self.collectors.items()}
        self._stop = threading.Event()
        self._threads = []

    def run_once(self, name):
        sample, _ = self.collectors[name]
        now = self.clock()
        try:
            points = sample(self.api, now) or []
            count = self.store.record(points, ts=now)
            self.state[name].update(last_ok=now, points=count, error=None)
        except Exception as exc:  # a failing source never stops the others
            self.state[name].update(last_error=now, error=f"{type(exc).__name__}: {exc}"[:300],
                                    failures=self.state[name]["failures"] + 1)

    def _loop(self, name):
        interval = self.collectors[name][1]
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once(name)
            self._stop.wait(max(0.2, interval - (time.monotonic() - started)))

    def start(self):
        for name in self.collectors:
            thread = threading.Thread(target=self._loop, args=(name,), name="hyperloom-sampler-" + name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self):
        self._stop.set()


def start(api, registry=None):
    """Start the samplers of every registered collector for this portal (idempotent)."""
    if registry is None:
        from hyperloom.web import features
        registry = features.registry()
    key = id(api)
    with _lock:
        if key in _samplers:
            return _samplers[key]
    sampler = Sampler(api, store_for(api), registry.collectors).start()
    with _lock:
        _samplers[key] = sampler
    api.sampler = sampler
    return sampler


def status(api):
    sampler = getattr(api, "sampler", None)
    return None if sampler is None else {name: dict(value) for name, value in sampler.state.items()}
