"""One evaluation of a recipe for a profile: resolve, bind, match, admit, decide. Shared by `hyperloom recipe ...`
and `hyperloom up --recipe`; nothing here starts a process or reads a model file."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from . import apply, facts, reader
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
    effective: reader.Effective
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
        return {"recipe": recipe["name"], "file": self.path, "reader": self.reader, "sha256": self.effective.sha256,
                "effective_sha256": self.effective.canonical_sha256(), "profile": self.context.profile,
                "untuned": self.effective.untuned, "refused_layers": self.effective.refused,
                "binding": self.bound["binding"], "roles": self.bound["roles"], "match": self.problems,
                "availability": {"devices": self.availability["devices"], "links": self.availability["links"],
                                 "hosts": self.availability["hosts"]},
                "deploy": self.decision, "refused": self.refusal, "notes": self.notes,
                "limitations": ["one decode configuration per run (decode routes are chosen at up)",
                                "prefill routes are chosen per request by the router (prompt length; no batch)"]}


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RecipeError(f"{path}: {exc}") from exc


def resolve(path, layers, report=None):
    text = read_text(path)
    profile = (read_text(layers.profile), str(layers.profile)) if layers.profile else None
    overrides = [(read_text(p), str(p)) for p in layers.overrides]
    return reader.resolve(text, Path(path).name, profile=profile, overrides=overrides, sets=layers.sets, probe=report)


def evaluate(cluster, profile, path, layers, source, reader_label="stub (offline diagnostics)"):
    """``source(ctx)`` -> (inventory, facts, raw availability, notes): live probes or a saved facts file."""
    ctx = facts.context(cluster, profile)
    base = reader.resolve(read_text(path), Path(path).name)  # structure only: binding never depends on layers
    inventory, gathered, raw, notes = source(ctx)
    bound = apply.bind(base.recipe, ctx, inventory)
    report = apply.probe_report(base.recipe, bound, gathered)
    identity = dict(report, binding=bound["binding"], workload=layers.workload,
                    descriptors=apply.descriptors(bound, ctx))
    eff = resolve(path, layers, identity)
    problems = apply.match(eff.recipe, report, bound)
    availability = facts.availability(eff.recipe, bound, raw)
    decision, refusal = None, None
    if not problems:
        try:
            decision = apply.deploy(eff.recipe, ctx, bound, availability, eff.canonical_sha256())
        except RecipeError as exc:
            refusal = str(exc)
    return Evaluation(str(path), reader_label, eff, ctx, bound, report, problems, availability, decision, refusal,
                      list(notes))


def saved_source(path):
    """A facts source from `hyperloom recipe probe --out FILE`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != FACTS_SCHEMA:
        raise RecipeError(f"{path}: expected {FACTS_SCHEMA}")

    def source(ctx):
        if data.get("profile") != ctx.profile:
            raise RecipeError(f"{path}: facts of profile {data.get('profile')}, not {ctx.profile}")
        return data["inventory"], data["facts"], data["raw"], data.get("notes", []) + [f"facts from {path}"]
    return source


def facts_record(profile, inventory, gathered, raw, notes):
    return {"schema": FACTS_SCHEMA, "profile": profile, "inventory": inventory, "facts": gathered, "raw": raw,
            "notes": notes}


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
    """Every recipe file in the directories (sorted), parsed or with its error; duplicate names are errors."""
    rows, names = [], {}
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.suffix in SUFFIXES and p.is_file()):
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
    return rows


def specificity(recipe):
    """How much hardware a recipe pins down: the count of non-default minimums in `match`."""
    count = 0
    for device in recipe["match"]["devices"]:
        count += bool(device["name"]) + (device["memory_min_gib"] > 0) + (device["host_ram_min_gib"] > 0)
        count += (device["disk_read_gbytes_ps_min"] > 0) + (device["internal_link"] is not None)
    count += sum(1 + (link["gbps_min"] > 0) for link in recipe["match"]["links"])
    return count


def pick(cluster, profile, directories, layers, source):
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
                evaluation = evaluate(cluster, profile, row["path"], layers, once)
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
