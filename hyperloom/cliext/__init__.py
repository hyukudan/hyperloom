"""CLI extensions: one module per feature family adds its own subcommands.

Each module in MODULES defines ``add(commands)`` (``commands`` is the top-level subparsers object) and sets
``ext_run=<function(args) -> exit code>`` on its parsers with ``set_defaults``. The portal API builds the same
argv for the same functions, so the CLI and the portal share one code path.
"""

import argparse
import sys


MODULES = ("bench", "telemetry", "observe", "ledger", "usb4", "energy", "health", "alerts", "turns", "library", "logsearch",
           "ops", "planner", "endpoints", "integrations")


def add_all(commands):
    """Add every available CLI family; one whose import or parser setup fails is skipped whole (it is first
    built against a scratch parser, so a failure never leaves half-registered commands behind)."""
    for name in MODULES:
        full = __name__ + "." + name
        try:
            __import__(full)    # not importlib.import_module: tests patch that to simulate missing modules
        except ModuleNotFoundError as exc:
            if exc.name == full:
                continue
            print(f"hyperloom: CLI extension {name} unavailable ({exc})", file=sys.stderr)
            continue
        except Exception as exc:
            print(f"hyperloom: CLI extension {name} unavailable ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        module = sys.modules[full]
        try:
            # Built once, in isolation, then grafted: a failure anywhere leaves the real parser untouched.
            scratch = argparse.ArgumentParser(prog="hyperloom", add_help=False).add_subparsers(dest="command")
            scratch._prog_prefix = commands._prog_prefix
            module.add(scratch)
            taken = sorted(set(scratch.choices) & set(commands.choices))
            if taken:
                raise ValueError("command names already used: " + ", ".join(taken))
        except Exception as exc:
            print(f"hyperloom: CLI extension {name} unavailable ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        commands._name_parser_map.update(scratch._name_parser_map)
        commands._choices_actions.extend(scratch._choices_actions)
