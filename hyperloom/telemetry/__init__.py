"""Telemetry: the persistent store and the background samplers that feed it.

The portal process opens one store at ``<config dir>/telemetry.sqlite3`` when it starts (``open_store``); GET
handlers only read it through ``existing_store`` (a GET never creates files). Collectors are registered by
portal features (``reg.collector(name, sample, interval)``). ``start(api)`` runs one daemon thread per collector
plus a maintenance thread (retention). A collector returns ``[(series, labels, value[, kind]), ...]``; it must
bound its own work (node verbs carry their own deadlines); its failures are counted, never raised. Runs of one
collector never overlap: a refresh requested while it runs is coalesced into that run.
"""

import threading
import time

from .store import Baselines, KINDS, MAX_POINTS, Store, TIERS, canonical_labels

__all__ = ["Baselines", "KINDS", "MAX_POINTS", "Store", "TIERS", "canonical_labels", "open_store", "existing_store",
           "store_for", "Sampler", "start", "stop", "status", "refresh", "set_interval"]

MIN_INTERVAL, MAX_INTERVAL = 0.5, 3600.0
_lock = threading.Lock()
_stores = {}
_samplers = {}


def _path(api):
    return str(api.config_path.parent / "telemetry.sqlite3")


def open_store(api):
    """Open (creating if needed) the store of this portal; called at portal start and by jobs/collectors."""
    existing = getattr(api, "telemetry", None)
    if existing is not None:
        return existing
    path = _path(api)
    with _lock:
        if path not in _stores:
            _stores[path] = Store(path, clock=getattr(api, "clock", time.time))
        store = _stores[path]
    api.telemetry = store
    return store


def existing_store(api):
    """The store if this portal opened it (GET handlers: read-only, never creates it), else None."""
    return getattr(api, "telemetry", None)


store_for = open_store


class Sampler:
    """One thread per collector; each runs `sample(api, now)` every `interval` seconds, never overlapping."""

    def __init__(self, api, store, collectors, clock=time.time):
        self.api, self.store, self.clock = api, store, clock
        self.collectors = {name: (spec[0], float(spec[1])) for name, spec in collectors.items()}
        self.state = {name: {"interval_s": spec[1], "last_ok": None, "last_error": None, "error": None,
                             "points": 0, "failures": 0, "running_since": None, "coalesced": 0}
                      for name, spec in self.collectors.items()}
        self._running = {name: threading.Lock() for name in self.collectors}
        self._wake = {name: threading.Event() for name in self.collectors}
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []

    def run_once(self, name):
        """Run one collection now; returns False (coalesced) when a run of this collector is in progress."""
        if not self._running[name].acquire(blocking=False):
            with self._state_lock:
                self.state[name]["coalesced"] += 1
            return False
        try:
            sample, interval = self.collectors[name]
            now = self.clock()
            with self._state_lock:
                self.state[name]["running_since"] = now
            try:
                points = sample(self.api, now) or []
                count = self.store.record(points, ts=now, interval=interval)
                with self._state_lock:
                    self.state[name].update(last_ok=now, points=count, error=None)
            except Exception as exc:  # a failing source never stops the others
                with self._state_lock:
                    self.state[name].update(last_error=now, error=f"{type(exc).__name__}: {exc}"[:300],
                                            failures=self.state[name]["failures"] + 1)
            finally:
                with self._state_lock:
                    self.state[name]["running_since"] = None
            return True
        finally:
            self._running[name].release()

    def refresh(self, name):
        """Ask for a collection now (coalesced with a run in progress)."""
        if name not in self.collectors:
            raise LookupError("unknown collector " + name)
        # A request that arrives while this collector runs is absorbed by that run (no second collection).
        if self._running[name].locked():
            with self._state_lock:
                self.state[name]["coalesced"] += 1
            return False
        self._wake[name].set()
        return True

    def set_interval(self, name, interval):
        if name not in self.collectors:
            raise LookupError("unknown collector " + name)
        interval = float(interval)
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            raise ValueError(f"interval must be in {MIN_INTERVAL}..{MAX_INTERVAL} s")
        self.collectors[name] = (self.collectors[name][0], interval)
        with self._state_lock:
            self.state[name]["interval_s"] = interval
        self._wake[name].set()

    def snapshot(self):
        with self._state_lock:
            return {name: dict(value) for name, value in self.state.items()}

    def _loop(self, name):
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once(name)
            wait = max(0.2, self.collectors[name][1] - (time.monotonic() - started))
            self._wake[name].wait(wait)
            self._wake[name].clear()

    def _maintain(self):
        while not self._stop.wait(300):
            try:
                self.store.prune()
            except Exception:
                pass

    def start(self):
        for name in self.collectors:
            self._threads.append(threading.Thread(target=self._loop, args=(name,), name="hyperloom-sampler-" + name, daemon=True))
        self._threads.append(threading.Thread(target=self._maintain, name="hyperloom-telemetry-maintenance", daemon=True))
        for thread in self._threads:
            thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        for event in self._wake.values():
            event.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))


def start(api, registry=None):
    """Open the store and start the samplers of every registered collector for this portal (idempotent)."""
    if registry is None:
        from hyperloom.web import features
        registry = features.registry()
    with _lock:
        sampler = _samplers.get(id(api))
        if sampler is None:
            store = getattr(api, "telemetry", None)
            if store is None:
                path = _path(api)
                store = _stores.get(path) or Store(path, clock=getattr(api, "clock", time.time))
                _stores[path] = store
                api.telemetry = store
            sampler = Sampler(api, store, registry.collectors)
            _samplers[id(api)] = sampler
            api.sampler = sampler
            sampler.start()
    return sampler


def stop(api):
    with _lock:
        sampler = _samplers.pop(id(api), None)
    if sampler is not None:
        sampler.stop()


def status(api):
    sampler = getattr(api, "sampler", None)
    return None if sampler is None else sampler.snapshot()


def refresh(api, name):
    sampler = getattr(api, "sampler", None)
    if sampler is None:
        raise PermissionError("samplers are not running in this portal process")
    sampler.refresh(name)


def set_interval(api, name, interval):
    sampler = getattr(api, "sampler", None)
    if sampler is None:
        raise PermissionError("samplers are not running in this portal process")
    sampler.set_interval(name, interval)
