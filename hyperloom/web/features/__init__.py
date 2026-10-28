"""Portal feature registry.

Each feature is a module in this package (listed in MODULES) with a ``register(registry)`` function. A feature
adds GET documents, synchronous POST queries/writes and background jobs without touching the portal core:

    def register(reg):
        reg.get("overview", overview)                      # GET /hyperloom/api/overview -> overview(api, query)
        reg.get_prefix("history/", history)                # GET history/<rest> -> history(api, rest, query)
        reg.post("bench/compare", compare)                 # synchronous POST -> compare(api, arguments)
        reg.post("alerts/test", test, audited=True)        # synchronous, written to the audit log
        reg.job("bench/run", run, validate=check,          # 202 + job; run(api, arguments, ctx) in a thread
                admission="bench", resources=targets, deadline_s=1800)
        reg.collector("gpu", sample, interval=2)           # telemetry: sample(api, now) -> [(name, labels, value[, kind])]
        reg.capability("bench", True, "why")               # shown in settings.capabilities

A module registers into a private staging registry; only when ``register`` returns without error and every name is
valid and free is it merged, so a failing feature leaves nothing behind. Core names (the core screens, core
mutations, jobs, recipes) are reserved. Prefix GETs match the longest prefix; an exact GET wins over a prefix.

GET handlers return a document; use ``api.document(name, **fields)`` for the common envelope. Handlers raise
``ValueError`` (400), ``LookupError`` (404) or ``PermissionError`` (409). Every document and job passes through
the portal's redaction. Jobs receive a context ``ctx``: ``ctx(text)`` reports progress, ``ctx.cancelled()`` is
true after a cancel request, a priority stop of an overlapping profile, or the job's deadline; ``ctx.deadline``
is the monotonic deadline. Long jobs must poll ``ctx.cancelled()`` and clean up.

Admission (contract C11): a job with ``admission="bench"`` declares ``resources(api, arguments) -> set`` of
resource ids (machine ids, devices, ``router``); it is refused while a core mutating job runs, while another
admitted job holds an overlapping resource, or when the lab-wide cap is reached; core deploy/start/install jobs
are refused while an admitted job holds an overlapping resource, and a stop (``down``) cancels them.
"""

from dataclasses import dataclass, field
import importlib
import re
import threading


MODULES = ("overview", "machines", "history", "observe", "ledger", "endpoints", "usb4", "energy", "health", "alerts",
           "events", "bench", "planner", "turns", "library", "logsearch", "ops", "integrations", "prefs")
NAME = re.compile(r"[a-z][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*/?")
RESERVED = frozenset(("devices", "topology", "models", "profiles", "stats", "logs", "settings", "jobs", "up", "down",
                      "install", "doctor", "cancel", "hosts", "keys", "recipes"))
ADMISSIONS = ("bench",)
DEFAULT_DEADLINE_S = 3600


@dataclass
class Job:
    run: object
    validate: object = None
    exclusive: bool = False     # True: takes the portal's single mutating-job slot (deploy-like actions)
    admission: str = None       # "bench": shared admission controller (C11)
    resources: object = None    # fn(api, arguments) -> set of resource ids (required with admission)
    deadline_s: float = DEFAULT_DEADLINE_S
    rollback: object = None     # fn(api, arguments): undo what validate reserved when the job is not started


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

    def _taken(self, name):
        return name in self.gets or name in self.posts or name in self.jobs or name in self.prefixes

    def _name(self, name, prefix=False):
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.endswith("/") != prefix:
            raise ValueError("invalid feature endpoint name: " + repr(name))
        if name.split("/", 1)[0].rstrip("/") in RESERVED:
            raise ValueError("feature endpoint name is reserved by the portal core: " + name)
        if self._taken(name):
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

    def job(self, name, run, validate=None, exclusive=False, admission=None, resources=None,
            deadline_s=DEFAULT_DEADLINE_S, rollback=None):
        if admission is not None and (admission not in ADMISSIONS or resources is None or exclusive):
            raise ValueError("an admitted job names its admission class and resources and is not exclusive")
        if not 1 <= float(deadline_s) <= 24 * 3600:
            raise ValueError("job deadline must be in 1 s..24 h")
        self.jobs[self._name(name)] = Job(run, validate, exclusive, admission, resources, float(deadline_s), rollback)

    def collector(self, name, sample, interval=2.0):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in self.collectors:
            raise ValueError("collector name invalid or registered twice: " + repr(name))
        if not 0.5 <= float(interval) <= 3600:
            raise ValueError("collector interval must be in 0.5..3600 s")
        self.collectors[name] = (sample, float(interval))

    def capability(self, name, enabled, why=None):
        if name in self.capabilities:
            raise ValueError("capability registered twice: " + name)
        self.capabilities[name] = {"enabled": bool(enabled), **({"why": why} if why else {})}

    def merge(self, other):
        """Merge a staged registry; nothing is merged when any name conflicts."""
        for name in [*other.gets, *other.prefixes, *other.posts, *other.jobs]:
            if self._taken(name):
                raise ValueError("feature endpoint registered twice: " + name)
        for name in other.collectors:
            if name in self.collectors:
                raise ValueError("collector registered twice: " + name)
        for name in other.capabilities:
            if name in self.capabilities:
                raise ValueError("capability registered twice: " + name)
        self.gets.update(other.gets)
        self.prefixes.update(other.prefixes)
        self.posts.update(other.posts)
        self.audited.update(other.audited)
        self.jobs.update(other.jobs)
        self.collectors.update(other.collectors)
        self.capabilities.update(other.capabilities)

    def find_get(self, endpoint):
        """(handler, rest) for a GET endpoint, or (None, None): exact first, then the longest prefix."""
        if endpoint in self.gets:
            return self.gets[endpoint], None
        for prefix in sorted(self.prefixes, key=len, reverse=True):
            if endpoint.startswith(prefix) and len(endpoint) > len(prefix):
                return self.prefixes[prefix], endpoint[len(prefix):]
        return None, None


def load(modules=MODULES):
    """Import and register every available feature module; a failing one is reported and leaves nothing behind."""
    registry = Registry()
    for name in modules:
        full = __name__ + "." + name
        try:
            module = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name == full:
                continue
            registry.failed[name] = str(exc)
            continue
        except Exception as exc:  # a broken feature must not take the portal down
            registry.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        staged = Registry()
        try:
            module.register(staged)
            registry.merge(staged)
            registry.loaded.append(name)
        except Exception as exc:
            registry.failed[name] = f"{type(exc).__name__}: {exc}"
    return registry


_registry = None
_generation = 0             # bumped by every reset(): a load started before it never replaces what it installed
_lock = threading.Lock()


def registry():
    """The process registry, loaded on first use. Concurrent first uses (request threads) share one registry: a load
    that finishes after another one, or after a reset(), does not replace the installed registry."""
    global _registry
    with _lock:
        if _registry is not None:
            return _registry
        generation = _generation
    loaded = load()             # outside the lock: feature imports may start threads that read the registry
    with _lock:
        if _registry is None and _generation == generation:
            _registry = loaded
        return _registry if _registry is not None else loaded


def reset(value=None):
    """Tests: install a registry (or reload the default one on next use)."""
    global _registry, _generation
    with _lock:
        _registry = value
        _generation += 1
