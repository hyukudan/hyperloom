"""CLI extensions: one module per feature family adds its own subcommands.

Each module in MODULES defines ``add(commands)`` (``commands`` is the top-level subparsers object) and sets
``ext_run=<function(args) -> exit code>`` on its parsers with ``set_defaults``. The portal API builds the same
argv for the same functions, so the CLI and the portal share one code path.
"""

import sys


MODULES = ("bench", "telemetry", "health", "turns", "library", "logsearch", "integrations")


def add_all(commands):
    for name in MODULES:
        full = __name__ + "." + name
        try:
            __import__(full)    # not importlib.import_module: tests patch that to simulate missing modules
        except ModuleNotFoundError as exc:
            if exc.name == full:
                continue
            raise
        sys.modules[full].add(commands)
