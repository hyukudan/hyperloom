"""One evaluation of a recipe for a profile: resolve, bind, match, admit, decide. Shared by `hyperloom recipe ...`
and `hyperloom up --recipe`; nothing here starts a process or reads a model file."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from pathlib import Path

from . import apply, core, devices, facts, grants, reader
from .reader import RecipeError

SUFFIXES = (".toml", ".json")
FACTS_SCHEMA = "hyperloom/recipe-facts-v1"


@dataclass
class Layers:
    profile: Path | None = None
    overrides: list = field(default_factory=list)
    sets: list = field(default_factory=list)
    workload: str = "default"     # the workload class this run serves; a profile tuned for another one is stale


@dataclass
class Evaluation:
    path: str
    reader: str
    effective: core.Resolved
    effective_sha256: str
    context: apply.Context
    bound: dict
    report: dict
    problems: list
    availability: dict
    decision: dict | None
    refusal: str | None
    notes: list

    @property
    def ok(self):
        return not self.problems and self.refusal is None

    def summary(self):
        recipe = self.effective.recipe
        return {"recipe": recipe["name"], "file": self.path, "reader": self.reader,
                "sha256": self.effective.file_sha256, "effective_sha256": self.effective_sha256,
                "profile": self.context.profile,
                "untuned": self.effective.untuned, "refused_layers": self.effective.refused,
                "binding": self.bound["binding"], "roles": self.bound["roles"], "match": self.problems,
                "availability": {"devices": self.availability["devices"], "links": self.availability["links"],
                                 "hosts": self.availability["hosts"]},
                "deploy": self.decision, "refused": self.refusal, "notes": self.notes,
                "limitations": ["one decode configuration per run (decode routes are chosen at up)",
                                "prefill routes are chosen per request by the router (prompt length; no batch)"]}


def read_text(path):
    """A recipe file as text, decoded without newline translation: the text hashes to the raw file's sha256, which
    profile and overrides layers bind to (a CRLF file keeps its CRLF)."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RecipeError(f"{path}: {exc}") from exc


def resolve(path, layers, identity=None, backend=None):
    """The effective recipe of a file with its layers (the core's when installed, else the stub's)."""
    return (backend or core.StubBackend()).resolve(path, read_text(path), layers, identity)


def evaluate(cluster, profile, path, layers, source, backend=None):
    """``source(ctx)`` -> (inventory, facts, raw availability, notes): live probes or a saved facts file. One resolved
    document (the backend's) is used for binding, match, admission and the decision."""
    backend = backend or core.StubBackend()
    ctx = facts.context(cluster, profile)
    base = resolve(path, Layers(), backend=backend).recipe   # structure only: binding never depends on layers
    inventory, gathered, raw, notes = source(ctx)
    ctx.limits = dict(ctx.limits or {}, node_runtime=facts.current_runtime(cluster, ctx, gathered.get("node_runtime")))
    if any(reader.placement_mode(r) for r in base["routes"]):
        return _placement_evaluation(cluster, path, layers, backend, ctx, base, inventory, gathered, raw, notes)
    bound = apply.bind(base, ctx, inventory)
    report = apply.probe_report(base, bound, gathered)
    identity = dict(report, binding=bound["binding"], workload=layers.workload,
                    descriptors=apply.descriptors(bound, ctx))
    resolved = resolve(path, layers, identity, backend)
    recipe = resolved.recipe
    if isinstance(backend, core.CoreBackend):
        problems, binding, effective_sha = backend.match(path, layers, report, identity)
        if not problems and binding != bound["binding"]:
            problems = [f"the core bound {binding}, hyperloom launches {bound['binding']}"]
    else:
        problems = apply.match(recipe, report, bound)
        effective_sha = backend.effective_sha256(path, layers, resolved, report)
    availability = facts.availability(recipe, bound, raw)
    decision, refusal = None, None
    if not problems:
        def select(state):
            return backend.select(path, layers, resolved, state, effective_sha, identity) \
                if isinstance(backend, core.CoreBackend) else reader.select(recipe, state, effective_sha)
        try:
            decision = apply.deploy(recipe, ctx, bound, availability, effective_sha, select=select)
        except RecipeError as exc:
            refusal = str(exc)
    return Evaluation(str(path), backend.label, resolved, effective_sha or "", ctx, bound, report, problems,
                      availability, decision, refusal, list(notes))


def _placement_evaluation(cluster, path, layers, backend, ctx, base, inventory, gathered, raw, notes):
    if not isinstance(backend, core.CoreBackend):
        raise RecipeError("placement binding requires a compatible ts-recipe core")
    report = apply.probe_report(base, None, gathered, cluster=cluster, ctx=ctx, inventory=inventory)
    problems, binding, effective_sha = backend.match(path, Layers(), report, None)
    bound = devices.bound(base, ctx, binding or {d["id"]: [] for d in base["match"]["devices"]})
    identity = dict(report, binding=bound["binding"], workload=layers.workload,
                    descriptors=apply.descriptors(bound, ctx))
    resolved = resolve(path, layers, identity, backend)
    if not problems:
        problems, binding, effective_sha = backend.match(path, layers, report, identity)
        if not problems:
            bound = devices.bound(resolved.recipe, ctx, binding)
    availability = facts.availability(resolved.recipe, bound, raw)
    refusal = None
    if not problems:
        try:
            used = grants.check(cluster, resolved.recipe, bound["binding"], report, raw["hosts"])
            from . import sysmem
            for row in report["devices"]:
                if grants.uuid_key(row.get("uuid", "")) in used:
                    sysmem.require(raw.get("sysmem_policy", "unknown"), placement=True, platform=row.get("platform"))
            # Placement launch stays unavailable until its adapter and supervision are complete.
            apply.refuse_unsupported(resolved.recipe)
        except RecipeError as exc:
            refusal = str(exc)
    return Evaluation(str(path), backend.label, resolved, effective_sha or "", ctx, bound, report, problems,
                      availability, None, refusal, list(notes))


def saved_source(path):
    """A facts source from `hyperloom recipe probe --out FILE`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != FACTS_SCHEMA:
        raise RecipeError(f"{path}: expected {FACTS_SCHEMA}")

    def source(ctx):
        if data.get("profile") != ctx.profile:
            raise RecipeError(f"{path}: facts of profile {data.get('profile')}, not {ctx.profile}")
        observed = data.get("observed_at")
        when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(observed)) if type(observed) in (int, float)
                else "at an unrecorded time")
        return data["inventory"], data["facts"], data["raw"], data.get("notes", []) + [
            f"facts from {path}, observed {when} (saved, not live)"]
    return source


def facts_record(profile, inventory, gathered, raw, notes):
    return {"schema": FACTS_SCHEMA, "profile": profile, "inventory": inventory, "facts": gathered, "raw": raw,
            "notes": notes, "observed_at": time.time()}


def simulate(evaluation, tokens, generation=None):
    """Which prefill route a request of ``tokens`` (plus ``generation`` tokens of budget) would get with the evaluated
    availability (core §7)."""
    decision = evaluation.decision
    if decision is None:
        raise RecipeError("nothing is deployed: " + (evaluation.refusal or "; ".join(evaluation.problems)))
    generation = (evaluation.context.limits or {}).get("default_generation_tokens", 1024) if generation is None else generation
    state = apply.admission_state(evaluation.bound, evaluation.availability, evaluation.context,
                                  running=decision["running"], requirements=decision["requirements"], tokens=tokens,
                                  context=tokens + generation)
    return reader.select(evaluation.effective.recipe, state, decision["effective_sha256"])


def diff(a, b):
    """Field-level differences of two effective recipes (structure and values), route order included."""
    left, right = _flatten(a.to_json()), _flatten(b.to_json())
    rows = []
    for key in sorted(set(left) | set(right)):
        if left.get(key, _MISSING) != right.get(key, _MISSING):
            rows.append({"path": key, "a": left.get(key, "<absent>"), "b": right.get(key, "<absent>")})
    order = ([r["id"] for r in a.recipe["routes"]], [r["id"] for r in b.recipe["routes"]])
    return {"same": not rows and order[0] == order[1], "fields": rows,
            "route_order": None if order[0] == order[1] else {"a": order[0], "b": order[1]}}


_MISSING = object()


def _flatten(doc, prefix=""):
    out = {}
    if isinstance(doc, dict):
        for key, value in doc.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(doc, list) and doc and all(isinstance(v, dict) and "id" in v for v in doc):
        for value in doc:
            out.update(_flatten(value, f"{prefix}.{value['id']}"))
    else:
        out[prefix] = doc
    return out


def candidates(directories):
    """Every recipe file in the directories (sorted), parsed or with its error; duplicate names and duplicate ids
    (file stems, case-folded: the catalog's recipe ids) are errors on every row that has them."""
    rows, names, stems = [], {}, {}
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.suffix in SUFFIXES and p.is_file()):
            stems.setdefault(path.stem.casefold(), []).append(str(path))
            try:
                recipe = reader.parse(read_text(path), path.name)
                rows.append({"path": str(path), "name": recipe["name"], "model": recipe["match"]["model"],
                             "recipe": recipe, "error": None})
                names.setdefault(recipe["name"], []).append(str(path))
            except RecipeError as exc:
                rows.append({"path": str(path), "name": None, "model": None, "recipe": None, "error": str(exc)})
    for name, paths in names.items():
        if len(paths) > 1:
            for row in rows:
                if row["name"] == name:
                    row["error"] = f'duplicate recipe name "{name}" in {", ".join(paths)}'
    for stem, paths in stems.items():
        if len(paths) > 1:
            for row in rows:
                if row["path"] in paths:
                    row["error"] = f'duplicate recipe id "{stem}" in {", ".join(paths)}'
    return rows


def specificity(recipe):
    """How much hardware a recipe pins down: the count of non-default minimums in `match`."""
    count = 0
    for device in recipe["match"]["devices"]:
        count += bool(device["name"]) + (device["memory_min_gib"] > 0) + (device["host_ram_min_gib"] > 0)
        count += (device["disk_read_gbytes_ps_min"] > 0) + (device["internal_link"] is not None)
    count += sum(1 + (link["gbps_min"] > 0) for link in recipe["match"]["links"])
    return count


def pick(cluster, profile, directories, layers, source, backend=None):
    """Auto-pick: candidates for the profile's model that match and deploy; the most specific wins, ties by name.
    Returns (chosen Evaluation or None, rows for every candidate with why it was or was not chosen)."""
    ctx = facts.context(cluster, profile)
    cached = {}

    def once(c):
        if "v" not in cached:
            cached["v"] = source(c)
        return cached["v"]
    rows, passing = [], []
    for row in candidates(directories):
        out = {"path": row["path"], "name": row["name"]}
        if row["error"]:
            out["result"] = "invalid: " + row["error"]
        elif row["model"] != ctx.model:
            out["result"] = f'other model ("{row["model"]}")'
        else:
            try:
                evaluation = evaluate(cluster, profile, row["path"], layers, once, backend)
            except RecipeError as exc:
                out["result"] = "refused: " + str(exc)
            else:
                if evaluation.ok:
                    out["result"] = "passes"
                    out["specificity"] = specificity(evaluation.effective.recipe)
                    passing.append((-out["specificity"], row["name"], evaluation))
                else:
                    out["result"] = "refused: " + "; ".join(evaluation.problems or [evaluation.refusal])
        rows.append(out)
    if not passing:
        return None, rows
    passing.sort(key=lambda item: (item[0], item[1]))
    return passing[0][2], rows


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
