"""The Tierstream recipe core through its CLI (`ts-recipe`, with legacy `ss-recipe` discovery), and two backends
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
# strix-stream is renamed tierstream: its core is `ts-recipe`, and `ss-recipe` is still found (the new name first).
NAMES = ("ts-recipe", "ss-recipe")
ENV = ("HYPERLOOM_TS_RECIPE", "HYPERLOOM_SS_RECIPE")


def _executable(candidate):
    """The candidate as an absolute path to an executable program, else None: on Windows only an .exe (a .bat or .cmd
    would run through cmd.exe, where a --set value could inject commands)."""
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file():
        return None
    if os.name == "nt":
        return str(path) if path.suffix.lower() == ".exe" else None
    return str(path) if os.access(path, os.X_OK) else None


def _names():
    return [name + (".exe" if os.name == "nt" else "") for name in NAMES]


def locate(cluster=None):
    """The recipe core executable (ts-recipe, else ss-recipe): cluster `pc.recipe_core`, else HYPERLOOM_TS_RECIPE, else
    HYPERLOOM_SS_RECIPE, else the absolute PATH entries (never the current directory); None when absent. A configured
    path that is not usable is None, not a fallback; a configured path that does not exist (the core was renamed)
    is looked up as its sibling ts-recipe / ss-recipe in the same directory."""
    configured = (cluster.raw.get("pc", {}).get("recipe_core") if cluster is not None else None)
    if configured:
        path = Path(configured)
        if path.is_absolute() and not path.exists():
            for name in _names():
                found = _executable(path.with_name(name))
                if found:
                    return found
        return _executable(configured)
    for variable in ENV:
        candidate = os.environ.get(variable)
        if candidate:
            return _executable(candidate)
    entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry and Path(entry).is_absolute()]
    for name in _names():
        for entry in entries:
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


@dataclass(frozen=True)
class PlacementOutput:
    """The same core invocation's machine-readable result and human-readable stdout."""
    document: dict
    text: str
    overrides: bytes | None = None


class Core:
    def __init__(self, executable, run=subprocess.run):
        self.executable = executable
        self.run = run

    def call(self, args, inputs=None):
        """A core JSON document. Placement always uses --json-out, even without a text switch."""
        return self._call(args, inputs, with_text=False)

    def placement(self, args, inputs=None):
        """JSON plus --explain/--table stdout; never attempt to parse text as JSON or fall back."""
        if not args or args[0] not in ("place", "solve"):
            raise RecipeError("placement output requires ts-recipe place or solve")
        return self._call(args, inputs, with_text=True)

    def _call(self, args, inputs, with_text):
        inputs = inputs or {}
        placing = bool(args and args[0] in ("place", "solve"))
        label = "ts-recipe" if placing else "ss-recipe"
        with tempfile.TemporaryDirectory(prefix="hl-recipe-") as directory:
            for name, value in inputs.items():
                (Path(directory) / name).write_bytes((value if isinstance(value, str) else json.dumps(value)).encode("utf-8"))
            # Input names in args stand for the temporary files written above.
            argv = [str(self.executable), *[str(Path(directory) / a) if a in inputs else str(a) for a in args]]
            overrides_path = None
            if placing and "--emit-overrides" in argv:
                index = argv.index("--emit-overrides") + 1
                overrides_path = Path(directory) / "picked-overrides.toml"
                argv[index] = str(overrides_path)
            json_path = Path(directory) / "placement-output.json"
            if placing:
                argv += ["--json-out", str(json_path)]
            with open(Path(directory) / ".stdout", "w+b") as stdout, open(Path(directory) / ".stderr", "w+b") as stderr:
                try:
                    out = self.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=TIMEOUT_S,
                                   **no_window())
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RecipeError(f"{label} unavailable: {exc}") from exc
                stdout.seek(0)
                stderr.seek(0)
                text, err = _bounded(stdout, "output"), _bounded(stderr, "error output")
            if placing and out.returncode != 0:
                # Keep the core's PL_E_* diagnostics, including prefixes, details and indentation, verbatim.
                raise RecipeError(err.rstrip("\r\n") or "ts-recipe refused")
            message = err.strip()
            for prefix in (name + ": " for name in NAMES):
                if message.startswith(prefix):
                    message = message[len(prefix):]
                    break
            if out.returncode == 1:
                raise RecipeError(message or "ss-recipe refused")
            if out.returncode != 0:
                raise RecipeError(f"{label} usage error: {message}")
            try:
                if placing:
                    with json_path.open("rb") as stream:
                        doc = json.loads(_bounded(stream, "placement JSON output"))
                else:
                    doc = json.loads(text)
            except (OSError, ValueError, RecursionError) as exc:
                message = f"{label} printed no valid JSON" if placing else "ss-recipe printed no JSON"
                raise RecipeError(f"{message}: {exc}") from exc
            if not isinstance(doc, dict):
                raise RecipeError(f"{label} printed {type(doc).__name__}, not a JSON object")
            overrides = None
            if overrides_path is not None:
                try:
                    with overrides_path.open("rb") as stream:
                        overrides = stream.read(MAX_OUTPUT + 1)
                    if len(overrides) > MAX_OUTPUT:
                        raise RecipeError("ts-recipe picked overrides exceeds output limit")
                except OSError as exc:
                    raise RecipeError("ts-recipe emitted no picked overrides after re-plan") from exc
            return PlacementOutput(doc, text, overrides) if with_text else doc


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
                return Path(layer).read_bytes().decode("utf-8")     # no newline translation, like the recipe
            except (OSError, UnicodeError) as exc:
                raise RecipeError(f"{layer}: cannot read ({exc})") from exc
        profile = (read(layers.profile), str(layers.profile)) if layers.profile else None
        overrides = [(read(p), str(p)) for p in layers.overrides]
        eff = reader.resolve(text, Path(path).name, profile=profile, overrides=overrides, sets=layers.sets,
                             probe=identity)
        return Resolved(eff.recipe, eff.sha256, reader.explain(eff), eff.untuned, eff.refused)

    def effective_sha256(self, path, layers, resolved, report):
        return reader.canonical_sha256(resolved.to_json())

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

    def _placement(self, command, path, layers, route, documents, identity, text_flag, pick=None):
        inputs = {}
        args = [command, str(path), "--route", route]
        for name, value in documents.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                filename = f"placement-{name}.json"
                inputs[filename] = value
                args += [f"--{name}", filename]
            else:
                args += [f"--{name}", str(value)]
        args += self._layer_args(layers, inputs, identity)
        if pick is not None:
            if command != "solve" or not isinstance(pick, str) or not pick or pick.startswith("-"):
                raise RecipeError("PL_E_SCHEMA solve pick requires a row id")
            args += ["--pick", pick, "--emit-overrides", "picked-overrides.toml"]
        if text_flag:
            args.append(text_flag)
        return self.core.placement(args, inputs)

    def place(self, path, layers, *, route, probe, components, desc=None, usage=None, context=None,
              identity=None, explain=True):
        """Plan from cached inputs; Tierstream owns binding, validation, arithmetic and refusals."""
        return self._placement("place", path, layers, route,
                               {"probe": probe, "components": components, "desc": desc, "usage": usage,
                                "context": context}, identity, "--explain" if explain else None)

    def solve(self, path, layers, *, route, probe, inputs, desc=None, context=None, identity=None, table=True, pick=None):
        """Solve from cached configuration documents; keep both the core's rows and its display text."""
        return self._placement("solve", path, layers, route,
                               {"probe": probe, "inputs": inputs, "desc": desc, "context": context},
                               identity, "--table" if table else None, pick=pick)


def backend(cluster=None):
    """The core when it is installed, else the stub."""
    executable = locate(cluster)
    return CoreBackend(Core(executable)) if executable else StubBackend()
