"""The strix-stream recipe core through its CLI (`ss-recipe`, JSON on stdout). hyperloom never re-implements a rule
the core owns once the core is present; until then the stub reader answers, labelled as such."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ..winproc import no_window
from .reader import RecipeError

TIMEOUT_S = 60


def locate(cluster=None):
    """The ss-recipe executable: cluster `pc.recipe_core`, else HYPERLOOM_SS_RECIPE, else PATH; None when absent."""
    configured = (cluster.raw.get("pc", {}).get("recipe_core") if cluster is not None else None)
    for candidate in (configured, os.environ.get("HYPERLOOM_SS_RECIPE")):
        if candidate:
            return candidate if Path(candidate).is_file() else None
    return shutil.which("ss-recipe")


class Core:
    def __init__(self, executable, run=subprocess.run):
        self.executable = executable
        self.run = run

    def _call(self, args, inputs=None):
        inputs = inputs or {}
        with tempfile.TemporaryDirectory(prefix="hl-recipe-") as directory:
            for name, value in inputs.items():
                (Path(directory) / name).write_text(json.dumps(value), encoding="utf-8")
            # Input names in args stand for the temporary files written above.
            argv = [self.executable, *[str(Path(directory) / a) if a in inputs else a for a in args]]
            try:
                out = self.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, **no_window())
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RecipeError(f"ss-recipe unavailable: {exc}") from exc
        if out.returncode == 1:
            raise RecipeError(out.stderr.strip() or "ss-recipe refused")
        if out.returncode != 0:
            raise RecipeError(f"ss-recipe usage error: {out.stderr.strip()}")
        try:
            return json.loads(out.stdout)
        except ValueError as exc:
            raise RecipeError(f"ss-recipe printed no JSON: {exc}") from exc

    @staticmethod
    def layers(profile=None, probe=None, overrides=(), sets=()):
        args = []
        if profile:
            args += ["--profile", profile]
            if probe:
                args += ["--probe", "probe.json"]
        for path in overrides:
            args += ["--overrides", path]
        for path, value in sets:
            args += ["--set", f"{path}={json.dumps(value)}"]
        return args

    def explain(self, recipe, profile=None, probe=None, overrides=(), sets=()):
        return self._call(["explain", recipe, *self.layers(profile, probe, overrides, sets), "--json"],
                          {"probe.json": probe} if probe else None)

    def match(self, recipe, report):
        return self._call(["match", recipe, "probe.json"], {"probe.json": report})

    def select(self, recipe, state, profile=None, overrides=(), sets=()):
        return self._call(["select", recipe, "state.json", *self.layers(profile, None, overrides, sets)],
                          {"state.json": state})
