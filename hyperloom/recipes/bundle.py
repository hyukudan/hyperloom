"""`hyperloom recipe export` / `hyperloom recipe import`: a recipe and its layers as one portable, data-only bundle.

A bundle holds the recipe text and the chosen overrides layers of that recipe, each with its sha256; nothing
machine-specific (no hardware facts, disk receipts, paths, hosts or device ids: tuning profiles, which carry one
machine's identity, are not bundled) and nothing that runs. Import checks the whole bundle first (structure, limits,
digests, the recipe resolved with every layer through the same reader as `save`), then writes into the user
directories only, under the writers' lock with save's compare-and-swap rules (service.publish_bundle)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .reader import RecipeError

SCHEMA = "hyperloom/recipe-bundle-v1"
MAX_BUNDLE_BYTES = 500 * 1024      # JSON-escaped inside the portal's request it still fits its 1 MiB body
MAX_LAYERS = 16
KEYS = {"schema", "recipe", "layers"}
RECIPE_KEYS = {"id", "name", "format", "text", "sha256"}
LAYER_KEYS = {"name", "format", "text", "sha256"}


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format(text):
    return "json" if text.lstrip().startswith("{") else "toml"


def export(cluster, ref, layer_names=()):
    """The bundle of recipe ``ref`` (an id or a file) with the named user overrides layers (file names in the layers
    directory). Everything is checked on the exact bytes that go into the bundle, as import checks them: each file is
    read once; a tuning profile, a layer of another recipe or version, an id import would refuse, or a size above
    import's limits is refused here."""
    from . import core, reader, service, session
    path = service.locate(ref, cluster)
    rid = service.identify(path, cluster) or Path(path).stem
    try:
        service.check_id(rid)                       # the id import will check
    except RecipeError as exc:
        raise RecipeError(f"{exc}: save the recipe under an id first (hyperloom recipe save ID --from FILE)") from exc
    text = session.read_text(path)
    recipe = reader.parse(text, Path(path).name)
    known = {r["name"] for r in service.layer_files()}
    backend = core.backend(cluster)
    layers, stems = [], set()
    for name in dict.fromkeys(layer_names):
        if name not in known:
            raise service.NotFound(f'unknown layer "{name}" (layers: {", ".join(sorted(known)) or "none"})')
        service.check_id(Path(name).stem, "layer name")              # the names import will check
        if Path(name).stem.casefold() in stems:
            raise RecipeError(f'layer "{Path(name).stem}" twice (one .toml and one .json?): choose one')
        stems.add(Path(name).stem.casefold())
        layer_text = session.read_text(service.layers_dir() / name)          # the only read of this layer
        schema = reader.legacy_id(reader.load_text(layer_text, name).get("schema"))
        if schema != reader.OVERRIDES_SCHEMA:
            raise RecipeError(f"layer {name}: a tuning profile carries one machine's identity and is not bundled")
        service.check_layer_document(name, layer_text, text, Path(path).name, backend)
        layers.append({"name": Path(name).stem, "format": _format(layer_text), "text": layer_text,
                       "sha256": _sha256(layer_text)})
    if len(layers) > MAX_LAYERS:
        raise RecipeError(f"a bundle holds at most {MAX_LAYERS} layers")
    if layers:
        service._resolve_text(text, Path(path).name, backend=backend,
                              extra=[(layer["name"] + "." + layer["format"], layer["text"]) for layer in layers])
    bundle = {"schema": SCHEMA,
              "recipe": {"id": rid, "name": recipe["name"], "format": _format(text), "text": text,
                         "sha256": _sha256(text)},
              "layers": layers}
    for entry in [bundle["recipe"], *layers]:
        if len(entry["text"].encode("utf-8")) > service.MAX_SAVE_BYTES:
            raise RecipeError(f'{entry.get("id") or entry["name"]}: larger than {service.MAX_SAVE_BYTES // 1024} KiB, '
                              "which import refuses")
    # The largest form it is written in must be importable: `recipe export` prints indent 2, ASCII-escaped, and on
    # Windows standard output each newline becomes two bytes; --out and the portal write less.
    printed = json.dumps(bundle, indent=2)
    if len(printed.encode("utf-8")) + 2 * (printed.count("\n") + 1) > MAX_BUNDLE_BYTES:
        raise RecipeError(f"the bundle would be larger than {MAX_BUNDLE_BYTES // 1024} KiB, which import refuses")
    return bundle


def read(data):
    """A bundle from its bytes: size, JSON, exact keys, types, limits and every digest checked; RecipeError otherwise."""
    from . import service
    if len(data) > MAX_BUNDLE_BYTES:
        raise RecipeError(f"bundle larger than {MAX_BUNDLE_BYTES // 1024} KiB")
    try:
        bundle = json.loads(data.decode("utf-8"), parse_constant=lambda name: _refuse(f"bundle: {name} is not JSON"))
    except (UnicodeError, ValueError) as exc:
        raise RecipeError(f"bundle: not UTF-8 JSON ({exc})") from exc
    if not isinstance(bundle, dict) or set(bundle) != KEYS:
        raise RecipeError(f"bundle: expected exactly the keys {', '.join(sorted(KEYS))}")
    if bundle["schema"] != SCHEMA:
        raise RecipeError(f"bundle: schema must be {SCHEMA}")
    recipe, layers = bundle["recipe"], bundle["layers"]
    if not isinstance(recipe, dict) or set(recipe) != RECIPE_KEYS:
        raise RecipeError(f"bundle recipe: expected exactly the keys {', '.join(sorted(RECIPE_KEYS))}")
    if not isinstance(layers, list) or len(layers) > MAX_LAYERS:
        raise RecipeError(f"bundle layers: an array of at most {MAX_LAYERS}")
    _entry(recipe, "recipe")
    service.check_id(recipe["id"])
    seen = set()
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or set(layer) != LAYER_KEYS:
            raise RecipeError(f"bundle layer {index}: expected exactly the keys {', '.join(sorted(LAYER_KEYS))}")
        _entry(layer, f"layer {index}")
        service.check_id(layer["name"], "layer name")
        if layer["name"].casefold() in seen:
            raise RecipeError(f'bundle: layer "{layer["name"]}" twice')
        seen.add(layer["name"].casefold())
    return bundle


def _refuse(message):
    raise RecipeError(message)


def _entry(entry, what):
    from .service import MAX_SAVE_BYTES
    text = entry["text"]
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_SAVE_BYTES:
        raise RecipeError(f"bundle {what}: text must be a nonempty document of at most {MAX_SAVE_BYTES // 1024} KiB")
    if entry["format"] != _format(text):
        raise RecipeError(f"bundle {what}: format {entry['format']!r} does not match its text")
    if entry["sha256"] != _sha256(text):
        raise RecipeError(f"bundle {what}: sha256 does not match its text")
    if not isinstance(entry["name"], str):
        raise RecipeError(f"bundle {what}: name must be a string")


def import_(cluster, data, *, recipe_id=None, replace=None):
    """Write a bundle into the user directories. The recipe is written under ``recipe_id`` (default: the bundle's id)
    unless the catalog already has a recipe with exactly that text (then only the layers are written); ``replace`` =
    the sha256 of the user recipe being replaced. Every check runs first (the recipe resolved with all its layers,
    each layer checked as save-layer checks it), so a refusal writes nothing; running the same import again after an
    interrupted one completes it (a layer file that already holds the same text is kept)."""
    from . import core, reader, service
    bundle = read(data)
    recipe = bundle["recipe"]
    recipe_id = recipe_id or recipe["id"]
    service.check_id(recipe_id)
    for layer in bundle["layers"]:
        schema = reader.legacy_id(reader.load_text(layer["text"], layer["name"]).get("schema"))
        if schema != reader.OVERRIDES_SCHEMA:
            raise RecipeError(f"bundle layer {layer['name']}: only overrides layers are bundled (a tuning profile "
                              "carries one machine's identity)")
    backend = core.backend(cluster)
    recipe_file = "bundle." + recipe["format"]
    resolved = service._resolve_text(recipe["text"], recipe_file, backend=backend,
                                     extra=[(layer["name"] + "." + layer["format"], layer["text"])
                                            for layer in bundle["layers"]])
    if resolved.recipe["name"] != recipe["name"]:
        raise RecipeError(f'bundle recipe: its text is named "{resolved.recipe["name"]}", not "{recipe["name"]}"')
    for layer in bundle["layers"]:
        service.check_layer_document(layer["name"], layer["text"], recipe["text"], recipe_file, backend)
    out = service.publish_bundle(cluster, recipe_id, recipe["text"],
                                 [(layer["name"], layer["text"]) for layer in bundle["layers"]], replace=replace)
    out.update(reader=backend.label)
    return out
