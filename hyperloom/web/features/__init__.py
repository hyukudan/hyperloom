"""Portal feature registry.

Each feature is a module in this package (listed in MODULES) with a ``register(registry)`` function. A feature
adds GET documents, synchronous POST queries/writes and background jobs without touching the portal core:

    def register(reg):
        reg.get("overview", overview)                      # GET /hyperloom/api/overview -> overview(api, query)
        reg.get_prefix("history/", history)                # GET history/<rest> -> history(api, rest, query)
        reg.post("bench/compare", compare)                 # synchronous POST -> compare(api, arguments)
        reg.post("alerts/test", test, audited=True)        # synchronous, written to the audit log
        reg.job("bench/run", run, validate=check)          # 202 + job; run(api, arguments, progress) in a thread
        reg.collector("gpu", sample, interval=2)           # telemetry: sample(api, now) -> [(name, labels, value)]
        reg.capability("bench", True, "why")               # shown in settings.capabilities

GET handlers return a document; use ``api.document(name, **fields)`` for the common envelope. Handlers raise
``ValueError`` for a bad request (400), ``LookupError`` for an unknown item (404) and ``PermissionError`` for
a refused action (409). Every document and job result passes through the portal's redaction.
"""

from dataclasses import dataclass, field
import importlib
import re


MODULES = ("overview", "machines", "history", "observe", "ledger", "endpoints", "usb4", "energy", "health", "alerts",
           "events", "bench",
           "planner", "turns", "library", "logsearch", "ops", "integrations", "prefs")
NAME = re.compile(r"[a-z][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*/?")


@dataclass
class Job:
    run: object
    validate: object = None
    exclusive: bool = False     # True: takes the portal's single mutating-job slot (deploy-like actions)


@dataclass
class Registry:
    gets: dict = field(default_factory=dict)
    prefixes: dict = field(default_factory=dict)
    posts: dict = field(default_factory=dict)
    audited: set = field(default_factory=set)
    jobs: dict = field(default_factory=dict)
    collectors: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    loaded: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)

    def _name(self, name, prefix=False):
        if not NAME.fullmatch(name) or name.endswith("/") != prefix:
            raise ValueError("invalid feature endpoint name: " + name)
        if name in self.gets or name in self.posts or name in self.jobs or name in self.prefixes:
            raise ValueError("feature endpoint registered twice: " + name)
        return name

    def get(self, name, handler):
        self.gets[self._name(name)] = handler

    def get_prefix(self, prefix, handler):
        self.prefixes[self._name(prefix, prefix=True)] = handler

    def post(self, name, handler, audited=False):
        self.posts[self._name(name)] = handler
        if audited:
            self.audited.add(name)

    def job(self, name, run, validate=None, exclusive=False):
        self.jobs[self._name(name)] = Job(run, validate, exclusive)

    def collector(self, name, sample, interval=2.0):
        if name in self.collectors:
            raise ValueError("collector registered twice: " + name)
        if not 0.5 <= float(interval) <= 3600:
            raise ValueError("collector interval must be in 0.5..3600 s")
        self.collectors[name] = (sample, float(interval))

    def capability(self, name, enabled, why=None):
        self.capabilities[name] = {"enabled": bool(enabled), **({"why": why} if why else {})}

    def find_get(self, endpoint):
        """(handler, rest) for a GET endpoint, or (None, None)."""
        if endpoint in self.gets:
            return self.gets[endpoint], None
        for prefix, handler in self.prefixes.items():
            if endpoint.startswith(prefix) and len(endpoint) > len(prefix):
                return handler, endpoint[len(prefix):]
        return None, None


def load(modules=MODULES):
    """Import every available feature module; one that fails to import is reported, never fatal."""
    registry = Registry()
    for name in modules:
        try:
            module = importlib.import_module(__name__ + "." + name)
        except ModuleNotFoundError as exc:
            if exc.name == __name__ + "." + name:
                continue
            registry.failed[name] = str(exc)
            continue
        except Exception as exc:  # a broken feature must not take the portal down
            registry.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        try:
            module.register(registry)
            registry.loaded.append(name)
        except Exception as exc:
            registry.failed[name] = f"{type(exc).__name__}: {exc}"
    return registry


_registry = None


def registry():
    global _registry
    if _registry is None:
        _registry = load()
    return _registry


def reset(value=None):
    """Tests: install a registry (or reload the default one on next use)."""
    global _registry
    _registry = value
