"""Explicit placement measurements. This entry point is never used by launch or planning."""
import json
from pathlib import Path

from hyperloom import cli


def add(commands):
    parser = commands.add_parser("probe", help="explicit, operator-approved placement measurements")
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("xfer", "store", "manifest"):
        mode.add_argument("--" + name, action="store_true")
    parser.add_argument("--uuid", help="configured, non-excluded GPU UUID (required for --xfer)")
    parser.add_argument("--xfer-sha256", help="the current Tierstream xfer-v1 SHA (never computed by Hyperloom)")
    parser.add_argument("--store-id", help="the current Tierstream store identity")
    parser.add_argument("--file", type=Path, action="append", default=[], help="explicit file to read")
    parser.add_argument("--window", type=Path, help="operator approval JSON: approval, starts_at, expires_at")
    parser.add_argument("--config", type=Path, default=cli.config_path())
    parser.set_defaults(ext_run=run)


def run(args):
    from hyperloom.recipes import commands, service
    with service.configured(args.config):
        print(json.dumps(commands.measure_command(cli._cluster(args.config), args), indent=2, allow_nan=False))
    return 0
