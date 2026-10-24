"""The strix-stream recipe core through its CLI (`ss-recipe`, JSON on stdout), and the two backends hyperloom
evaluates recipes with: the core (authoritative) and the stub reader (offline diagnostics, tests).

Both give one resolved document that every later step uses: the core's effective recipe is re-read by the stub
parser into hyperloom's in-memory shape (a disagreement is a refusal), its effective sha256 is the core's, and match
and select are the core's own."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from ..winproc import no_window
from . import reader
from .reader import RecipeError

TIMEOUT_S = 60
MAX_OUTPUT = 4 << 20        # bytes of stdout or stderr read from one core call


def _executable(candidate):
    """The candidate as an absolute path to an executable program, else None: on Windows only an .exe (a .bat or .cmd
    would run through cmd.exe, where a --set value could inject commands)."""
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file():
        return None
    if os.name == "nt":
        return str(path) if path.suffix.lower() == ".exe" else None
    return str(path) if os.access(path, os.X_OK) else None


def locate(cluster=None):
    """The ss-recipe executable: cluster `pc.recipe_core`, else HYPERLOOM_SS_RECIPE, else the absolute PATH entries
    (never the current directory); None when absent. A configured path that is not usable is None, not a fallback."""
    configured = (cluster.raw.get("pc", {}).get("recipe_core") if cluster is not None else None)
    for candidate in (configured, os.environ.get("HYPERLOOM_SS_RECIPE")):
        if candidate:
            return _executable(candidate)
    name = "ss-recipe.exe" if os.name == "nt" else "ss-recipe"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).is_absolute():
            found = _executable(Path(entry) / name)
            if found:
                return found
    return None


def _bounded(stream, what):
    data = stream.read(MAX_OUTPUT + 1)
    if len(data) > MAX_OUTPUT:
        raise RecipeError(f"ss-recipe {what} exceeds {MAX_OUTPUT >> 20} MiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecipeError(f"ss-recipe {what} is not UTF-8") from exc


class Core:
    def __init__(self, executable, run=subprocess.run):
        self.executable = executable
        self.run = run

    def call(self, args, inputs=None):
        """JSON object on stdout of one ss-recipe call; exit 1 raises RecipeError with the core's message. Output is
        read through files, at most MAX_OUTPUT bytes each, as UTF-8."""
        inputs = inputs or {}
        with tempfile.TemporaryDirectory(prefix="hl-recipe-") as directory:
            for name, value in inputs.items():
                (Path(directory) / name).write_text(value if isinstance(value, str) else json.dumps(value),
                                                    encoding="utf-8")
            # Input names in args stand for the temporary files written above.
            argv = [self.executable, *[str(Path(directory) / a) if a in inputs else a for a in args]]
            with open(Path(directory) / ".stdout", "w+b") as stdout, open(Path(directory) / ".stderr", "w+b") as stderr:
                try:
                    out = self.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=TIMEOUT_S,
                                   **no_window())
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RecipeError(f"ss-recipe unavailable: {exc}") from exc
                stdout.seek(0)
                stderr.seek(0)
                text, err = _bounded(stdout, "output"), _bounded(stderr, "error output")
        message = err.strip().removeprefix("ss-recipe: ")
        if out.returncode == 1:
            raise RecipeError(message or "ss-recipe refused")
        if out.returncode != 0:
            raise RecipeError(f"ss-recipe usage error: {message}")
        try:
            result = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RecipeError(f"ss-recipe printed no JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise RecipeError(f"ss-recipe printed {type(result).__name__}, not a JSON object")
        return result


def _set_text(value):
    """A --set value as TOML value text (JSON scalars and arrays are valid TOML)."""
    return json.dumps(value)


@dataclass
class Resolved:
    """The effective recipe every later step uses, with its explain trace."""
    recipe: dict
    file_sha256: str
    trace: dict
    untuned: bool = False
    refused: list = field(default_factory=list)

    def to_json(self):
        doc = json.loads(json.dumps(self.recipe))
        doc.pop("_written", None)
        return doc


class StubBackend:
    label = "stub (offline diagnostics)"
    authoritative = False

    def resolve(self, path, text, layers, identity):
        def read(layer):
            try:
                return Path(layer).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise RecipeError(f"{layer}: cannot read ({exc})") from exc
        profile = (read(layers.profile), str(layers.profile)) if layers.profile else None
        overrides = [(read(p), str(p)) for p in layers.overrides]
        eff = reader.resolve(text, Path(path).name, profile=profile, overrides=overrides, sets=layers.sets,
                             probe=identity)
        return Resolved(eff.recipe, eff.sha256, reader.explain(eff), eff.untuned, eff.refused)

    def effective_sha256(self, path, layers, resolved, report):
        return hashlib.sha256(json.dumps(resolved.to_json(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def select(self, path, layers, resolved, state, effective_sha):
        return reader.select(resolved.recipe, state, effective_sha)


class CoreBackend:
    authoritative = True

    def __init__(self, core):
        self.core = core
        self.label = f"core {core.executable}"
        self._profile_dropped = False

    def _layer_args(self, layers, inputs, identity):
        args = []
        if layers.profile and not self._profile_dropped:
            inputs["identity.json"] = identity or {}
            args += ["--profile", str(layers.profile), "--identity", "identity.json"]
        for p in layers.overrides:
            args += ["--overrides", str(p)]
        for path, value in layers.sets:
            args += ["--set", f"{path}={_set_text(value)}"]
        return args

    def resolve(self, path, text, layers, identity):
        self._profile_dropped, refused = False, []
        while True:
            inputs = {}
            args = self._layer_args(layers, inputs, identity)
            try:
                effective = self.core.call(["validate", str(path), *args], inputs)
                explain = self.core.call(["explain", str(path), *args, "--json"], inputs)
                break
            except RecipeError as exc:
                if layers.profile and not self._profile_dropped and "profile stale" in str(exc):
                    # The core refuses a stale profile; the run goes on with the recipe alone, untuned (§6.1).
                    refused.append(str(exc))
                    self._profile_dropped = True
                    continue
                raise
        try:
            recipe = reader.parse(json.dumps(effective), Path(path).name)
        except RecipeError as exc:
            raise RecipeError(f"the core's effective recipe does not read back in hyperloom: {exc}") from exc
        fields = explain.get("fields", [])
        if not isinstance(fields, list) or any(not isinstance(f, dict) or not isinstance(f.get("path"), str)
                                               or not isinstance(f.get("shadowed", []), list)
                                               or any(not isinstance(x, dict) for x in f.get("shadowed", []))
                                               for f in fields):
            raise RecipeError("the core's explain output has an unexpected shape")
        rows = [{"path": f["path"], "value": f.get("value"), "layer": f.get("from"), "origin": f.get("origin"),
                 "shadowed": [{"value": s.get("value"), "layer": s.get("from"), "origin": s.get("origin"),
                               "why": s.get("reason")} for s in f.get("shadowed", [])]}
                for f in fields]
        trace = {"schema": "strix-stream/recipe-explain-v1", "recipe": recipe["name"], "fields": rows,
                 "refused_layers": refused, "untuned": bool(refused)}
        return Resolved(recipe, hashlib.sha256(text.encode("utf-8")).hexdigest(), trace, bool(refused), refused)

    def match(self, path, layers, report, identity):
        """(problems, binding, effective_sha256) from the core's hardware check."""
        inputs = {"probe.json": report}
        args = self._layer_args(layers, inputs, identity)
        try:
            result = self.core.call(["match", str(path), "probe.json", *args], inputs)
        except RecipeError as exc:
            lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
            # One problem per unmet requirement, as the stub reports them (the core's header line ends with ":").
            details = [line for line in lines if not line.endswith(":")]
            return details or lines, None, None
        binding, sha = result.get("binding"), result.get("effective_sha256")
        if not isinstance(binding, dict) or not isinstance(sha, str):
            raise RecipeError("the core's match output has no binding or effective_sha256")
        return [], binding, sha

    def select(self, path, layers, resolved, state, effective_sha, identity=None):
        inputs = {"state.json": state}
        args = self._layer_args(layers, inputs, identity)
        result = self.core.call(["select", str(path), "state.json", *args], inputs)
        if not isinstance(result.get("trace", []), list) or not (result.get("route") is None
                                                                 or isinstance(result.get("route"), str)):
            raise RecipeError("the core's select output has an unexpected shape")
        return result


def backend(cluster=None):
    """The core when it is installed, else the stub."""
    executable = locate(cluster)
    return CoreBackend(Core(executable)) if executable else StubBackend()
