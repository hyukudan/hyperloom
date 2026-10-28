"""Shared fixtures for the recipe catalog/service and portal recipe tests: a cluster configuration file the CLI loads,
saved hardware facts (no machine is contacted) and an isolated configuration/runtime directory."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from hyperloom.recipes import service, session
from tests.test_rtx import PRO, rtx_example

NODE = {"name": "AMD Radeon 8060S", "memory_gib": 120.0, "host_ram_gib": 125.0, "disk_read_gbytes_ps": 6.5}


def cluster_document():
    obj = rtx_example()
    obj["profiles"]["mm"] = {"preset": "minimax-pair", "rank0": "halo-1", "rank1": "halo-2", "port": 18601,
                             "release_id": "0" * 12}
    return obj


def facts(model, profile, **availability):
    """A `hyperloom recipe probe --out` record for the example machines (synthetic: no machine is contacted)."""
    inventory = {"cuda": [{"uuid": PRO, "name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6}]}
    gathered = {"model": model,
                "devices": {PRO: {"name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6, "host_ram_gib": 127.7,
                                  "disk_read_gbytes_ps": 6.5},
                            "halo-1": dict(NODE), "halo-2": dict(NODE)},
                "links": [{"kind": "ethernet", "hosts": ["workstation", "halo-1"], "gbps": 2.5},
                          {"kind": "ethernet", "hosts": ["workstation", "halo-2"], "gbps": 2.5},
                          {"kind": "usb4", "hosts": ["halo-1", "halo-2"], "gbps": 40.0}]}
    raw = {"devices": {PRO: {"available": True}, "halo-1": {"available": True}, "halo-2": {"available": True}},
           "hosts": {"workstation": {"free_ram_gib": 100.0, "free_commit_gib": 150.0},
                     "halo-1": {"free_ram_gib": 124.0}, "halo-2": {"free_ram_gib": 124.0}},
           "lan": {"halo-1": {"up": True}, "halo-2": {"up": True}},
           "usb4": {"halo-1": {"up": True}, "halo-2": {"up": True}}}
    raw.update(availability)
    return session.facts_record(profile, inventory, gathered, raw, [])


class Isolated:
    """A temporary configuration directory (cluster.json, recipes/, run/) that both this process and CLI children
    use: APPDATA/LOCALAPPDATA/HOME point into it, and the stub reader is selected unless a core is configured."""

    def __init__(self, core=None):
        # The recipe core when HYPERLOOM_TS_RECIPE (else HYPERLOOM_SS_RECIPE) names it, as core.locate reads them (the
        # same tests then run on the core), else the stub.
        if core is None:
            named = os.environ.get("HYPERLOOM_TS_RECIPE") or os.environ.get("HYPERLOOM_SS_RECIPE")
            core = bool(named) and Path(named).is_file()
        self.temporary = tempfile.TemporaryDirectory(prefix="hl-recipes-test-")
        self.root = Path(self.temporary.name)
        env = {"APPDATA": str(self.root / "appdata"), "LOCALAPPDATA": str(self.root / "local"),
               "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "home" / ".config")}
        env.update(HYPERLOOM_RECIPE_STUB="1")      # `up --recipe` on the stub is allowed (non-production) in tests
        if not core:
            env.update(HYPERLOOM_TS_RECIPE="", HYPERLOOM_SS_RECIPE="")
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        # The route table goes through the router's own parser when a TensorMeld with routes is reachable (the pinned
        # one, or HYPERLOOM_TM_SRC), else it is accepted: the same rule as the recipe tests.
        from hyperloom import render
        from tests.test_recipes import _check_table
        self.route_check = mock.patch.object(render, "check_route_table", _check_table)
        self.route_check.start()
        from hyperloom.cli import config_path
        self.config = config_path()
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(cluster_document()), encoding="utf-8")
        service.use_config(self.config)

    def save_facts(self, name, model, profile, **availability):
        directory = service.facts_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(facts(model, profile, **availability)), encoding="utf-8")
        return path

    def close(self):
        service.use_config(None)
        self.route_check.stop()
        self.patch.stop()
        self.temporary.cleanup()


@contextmanager
def isolated(core=None):
    env = Isolated(core)
    try:
        yield env
    finally:
        env.close()


def builtin_text(recipe_id):
    return (service.BUILTIN_DIR / f"{recipe_id}.toml").read_text(encoding="utf-8")


def copy_of(recipe_id, name):
    """A builtin recipe's text under another recipe name (a user recipe that does not collide)."""
    text = builtin_text(recipe_id)
    old = next(line for line in text.splitlines() if line.startswith("name = "))
    return text.replace(old, f'name = "{name}"', 1)


__all__ = ["PRO", "cluster_document", "facts", "isolated", "builtin_text", "copy_of", "deepcopy"]
