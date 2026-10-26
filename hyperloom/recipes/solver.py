"""Operator-selected Tierstream rows, snapshotted locally and saved only through the core's pick/re-plan API."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import commands, content_cache, core, placement, reader, service, session
from .reader import RecipeError

SCHEMA = "hyperloom/solver-selection-v1"


def rows(document):
    """Display all core rows in their original order, including refusals and unpredicted estimates."""
    if document.get("schema") != "tierstream/solver-v1" or not isinstance(document.get("rows"), list):
        raise RecipeError("PL_E_SCHEMA expected tierstream/solver-v1 rows")
    result = []
    for row in document["rows"]:
        placed = row.get("placement") or {}
        refusal = placed.get("refusal")
        prediction = row.get("prediction") or {}
        requires = row.get("requires") or []
        result.append(dict(deepcopy(row), id=row.get("id") or (row.get("choice") or {}).get("id"), refusal=refusal,
                           estimates=prediction.get("estimates", row.get("estimates", [])),
                           tag=prediction.get("tag", "unpredicted"),
                           saveable=not refusal and not requires,
                           save_refused=("row refused" if refusal else "requires: " + ", ".join(requires)
                                         if requires else None)))
    return result


def _bundle(name):
    service.check_id(name, "solver input bundle")
    return commands.runtime_dir() / "solver-inputs" / (name + ".json")


def _materialize(value, sources, *, directory):
    """Freeze JSON document references before planning. Never read models or acquire descriptors/measurements."""
    if isinstance(value, dict):
        return {k: {lang: _materialize(doc, sources, directory=directory) for lang, doc in v.items()}
            if k == "usage_lang" and isinstance(v, dict) else
            _materialize(v, sources, directory=directory) if k in (
            "components", "desc", "context", "usage", "costs", "curves") else
            [_materialize(v_, sources, directory=directory) for v_ in v] if k == "choices" and isinstance(v, list)
            else deepcopy(v) for k, v in value.items()}
    if isinstance(value, str):
        path = Path(value)
        path = path if path.is_absolute() else directory / path
        return _materialize(_read_source(path, sources), sources, directory=path.parent)
    return deepcopy(value)


def _read_source(path, sources):
    raw = Path(path).read_bytes()
    if len(raw) > content_cache.MAX_JSON_BYTES:
        raise RecipeError("PL_E_SCHEMA solver JSON input too large")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise RecipeError("PL_E_SCHEMA solver JSON input must be an object")
    sources[str(Path(path).resolve())] = hashlib.sha256(raw).hexdigest()
    return doc


def _capture(cluster, ref, route, bundle, layers):
    path = service.locate(ref, cluster)
    bundle_path = _bundle(bundle)
    recipe_bytes = path.read_bytes()
    sources = {str(path.resolve()): hashlib.sha256(recipe_bytes).hexdigest()}
    request = _read_source(bundle_path, sources)
    if set(request) - {"probe", "inputs", "desc", "context", "identity"} or not {"probe", "inputs"} <= request.keys():
        raise RecipeError("PL_E_SCHEMA solver input bundle needs probe and inputs")
    for key in ("probe", "inputs", "desc", "context", "identity"):
        if key in request:
            request[key] = _materialize(request[key], sources, directory=bundle_path.parent)
    # Cache references are resolved here, not reopened in a later pick against another manifest/configuration.
    cached_components = []
    for choice in request["inputs"].get("choices", []):
        if content_cache.is_reference(choice.get("components")):
            component_ref, selected = choice["components"], choice.pop("selected_content", None)
            choice["components"] = commands.components_store().load(component_ref, selected=selected)
            cached_components.append({"reference": component_ref, "selected": selected, "document": choice["components"]})
    layer_docs = []
    for role, paths in (("profile", [layers.profile] if layers.profile else []), ("overrides", layers.overrides)):
        for layer in paths:
            layer = Path(layer)
            data = layer.read_bytes()
            sources[str(layer.resolve())] = hashlib.sha256(data).hexdigest()
            layer_docs.append({"role": role, "text": data.decode("utf-8"), "suffix": layer.suffix})
    return {"schema": SCHEMA, "recipe": recipe_bytes.decode("utf-8"), "recipe_ref": str(ref), "route": route,
            "request": request, "sources": sources, "layers": layer_docs, "sets": layers.sets,
            "workload": layers.workload, "config_sha256": service.config_sha256(),
            "cached_components": cached_components}


def _check(snapshot):
    if snapshot.get("config_sha256") != service.config_sha256():
        raise RecipeError("PL_E_STALE solver configuration changed; solve again")
    for path, expected in snapshot["sources"].items():
        try:
            current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            raise RecipeError("PL_E_STALE solver inputs unavailable; solve again") from exc
        if current != expected:
            raise RecipeError("PL_E_STALE solver inputs changed; solve again")
    for cached in snapshot["cached_components"]:
        if commands.components_store().load(cached["reference"], selected=cached["selected"]) != cached["document"]:
            raise RecipeError("PL_E_STALE solver components changed; solve again")


def _run(snapshot, backend, *, pick=None):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="hl-solver-") as temporary:
        directory = Path(temporary)
        path = directory / "recipe.toml"
        path.write_bytes(snapshot["recipe"].encode("utf-8"))
        layers = session.Layers(sets=snapshot["sets"], workload=snapshot["workload"])
        for index, layer in enumerate(snapshot["layers"]):
            target = directory / (str(index) + layer["suffix"])
            target.write_bytes(layer["text"].encode("utf-8"))
            if layer["role"] == "profile":
                layers.profile = target
            else:
                layers.overrides.append(target)
        request = deepcopy(snapshot["request"])
        return placement.solve(path, route=snapshot["route"], layers=layers, backend=backend,
                               **request, **({"pick": pick} if pick is not None else {}))


def solve(cluster, ref, route, bundle, *, layers=None, backend=None):
    backend = placement._backend(cluster, backend)
    snapshot = _capture(cluster, ref, route, bundle, layers or session.Layers())
    result = _run(snapshot, backend)
    shown = rows(result.document)
    _check(snapshot)
    digest = content_cache._sha(snapshot)
    record = {"snapshot": snapshot, "result": result.document, "table": result.text}
    content_cache._write(commands.runtime_dir() / "solver-plans" / (digest + ".json"), record)
    return {"selection": digest, "solver": result.document, "rows": shown, "table": result.text,
            "saved": False, "note": "Choose a row explicitly; no assist or grant is enabled by solving."}


def pick(cluster, digest, row_id, name, *, backend=None, replace=None):
    """Tierstream emits the layer after its semantic re-plan check; save those exact bytes, never synthesize quotas."""
    service.check_id(name, "layer name")
    if not content_cache._digest(digest) or not isinstance(row_id, str) or not row_id:
        raise RecipeError("PL_E_SCHEMA selection digest and row id are required")
    backend = placement._backend(cluster, backend)
    record = content_cache._read(commands.runtime_dir() / "solver-plans" / (digest + ".json"), "PL_E_STALE")
    snapshot = record["snapshot"]
    if content_cache._sha(snapshot) != digest:
        raise RecipeError("PL_E_STALE solver snapshot identity changed")
    candidates = [r for r in rows(record["result"]) if r.get("id") == row_id]
    if len(candidates) != 1 or not candidates[0]["saveable"]:
        raise RecipeError("PL_E_CAPABILITY unknown, refused or hypothetical row cannot be saved")
    with service.runtime_lock():
        _check(snapshot)
        picked = _run(snapshot, backend, pick=row_id)
        if picked.document != record["result"]:
            raise RecipeError("PL_E_STALE solver result changed; solve again")
        emitted = picked.overrides
        if not isinstance(emitted, bytes) or not emitted or len(emitted) > service.MAX_SAVE_BYTES or b"\r" in emitted:
            raise RecipeError("PL_E_SCHEMA core emitted no valid LF overrides")
        text = emitted.decode("utf-8")
        layer = reader.load_text(text, "picked overrides")
        if not isinstance(layer.get("set"), dict) or not layer["set"]:
            raise RecipeError("PL_E_SCHEMA picked overrides have no settings")
        # Solving may not enable assists, change budgets or redirect components, even with an incompatible core.
        route = next((r for r in reader.parse(snapshot["recipe"])["routes"] if r["id"] == snapshot["route"]), None)
        if route is None:
            raise RecipeError("PL_E_SCHEMA picked route is missing")
        base = "routes." + snapshot["route"]
        allowed = {base + ".max_rows", base + ".max_sequences", base + ".components.arena.units",
                   base + ".components.expert_file.units"}
        allowed.update(base + ".placement." + sid + ".max_context_tokens" for sid in route["devices"])
        for field in layer["set"]:
            if field not in allowed:
                raise RecipeError("PL_E_CONFLICT picked row may not change assists, budgets or component destinations")
        _check(snapshot)
        saved = service.save_layer(cluster, name, text=text, replace=replace, backend=backend)
        return dict(saved, selection=digest, row=row_id, replan_checked=True)
