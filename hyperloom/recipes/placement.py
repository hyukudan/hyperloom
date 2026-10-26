"""CPU placement orchestration over cached inputs. Tierstream is the only placement planner.

The diagnostic reader remains usable for editing recipes, but cannot place or solve. Missing/incompatible core
binaries, missing JSON output and PL_E_* refusals are terminal: there is no Python arithmetic or stub fallback.
This module neither probes hardware nor launches an engine.
"""
from . import core, session
from .reader import RecipeError


def _backend(cluster, backend):
    backend = backend if backend is not None else core.backend(cluster)
    if not isinstance(backend, core.CoreBackend):
        raise RecipeError("placement requires a compatible ts-recipe core (pc.recipe_core, HYPERLOOM_TS_RECIPE or PATH)")
    return backend


def place(path, *, route, probe, components, cluster=None, layers=None, backend=None, **kwargs):
    """Return the core's placement JSON and explanation from one invocation."""
    return _backend(cluster, backend).place(path, layers or session.Layers(), route=route, probe=probe,
                                           components=components, **kwargs)


def solve(path, *, route, probe, inputs, cluster=None, layers=None, backend=None, **kwargs):
    """Return the core's solver rows and table from one invocation."""
    return _backend(cluster, backend).solve(path, layers or session.Layers(), route=route, probe=probe,
                                           inputs=inputs, **kwargs)
