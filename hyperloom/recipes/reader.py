"""Stub reader for strix-stream recipe-v1 (the core's final schema, strix-stream docs/recipes.md): strict parse,
layered resolution (profile/overrides), explain trace and admission-time route selection.

The core library (`ss-recipe`, strix-stream) is the source of truth for these rules. This module mirrors the parts
hyperloom needs so the offline tools and the tests run without it; parity tests hold the two together, and
`up --recipe` uses the core once it exists (design wave4/recipes/orch/design.md §9.1).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import tomllib


SCHEMA = "strix-stream/recipe-v1"
PROFILE_SCHEMA = "strix-stream/recipe-profile-v1"
OVERRIDES_SCHEMA = "strix-stream/recipe-overrides-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_GIB = 1048576.0
MAX_INDEX = 4096          # layer indices and list entries (a range never expands past it)
EXACT_KEYS = ("exact", "exactness", "bit_exact")
NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
ID = re.compile(r"[a-z0-9_-]{1,32}")
SHA = re.compile(r"[0-9a-f]{64}")
GIB_BYTES = 1 << 30


class RecipeError(ValueError):
    pass


class StaleProfile(RecipeError):
    """A profile that does not fit the current recipe/hardware: refused; the run continues untuned."""


# --- field tables: name -> (kind, default) -----------------------------------------------------------------------

REQUIRED = object()
DERIVED = object()   # absent means "derived by the core from the descriptor" (workspace_gib)


def _float(low, high=MAX_GIB, low_open=False):
    return ("float", low, high, low_open)


def _int(low, high=2 ** 63 - 1):
    return ("int", low, high)


def _enum(*values):
    return ("enum",) + values


LINK_REQ = {"kind": (_enum("usb4", "ethernet", "pcie", "other"), "other"), "gbps_min": (_float(0), 0.0)}
DEVICE = {"id": ("id", REQUIRED), "kind": (_enum("cuda", "hip", "cpu"), REQUIRED), "count": (_int(1, 64), 1),
          "name": ("str", None), "memory_min_gib": (_float(0, low_open=True), REQUIRED),
          "host_ram_min_gib": (_float(0), 0.0), "disk_read_gbytes_ps_min": (_float(0), 0.0),
          "h2d_gbytes_ps_min": (_float(0), 0.0), "internal_link": ("internal_link", None)}
LINK = {"id": ("id", REQUIRED), "between": ("between", REQUIRED), **LINK_REQ}
LIMITS = {"host_free_ram_floor_gib": (_float(0), 8.0), "host_free_commit_floor_gib": (_float(0), 0.0)}
WHEN = {"prompt_tokens_min": (_int(0), 0), "prompt_tokens_max": (_int(0), None),
        "batch_min": (_int(1), 1), "batch_max": (_int(1), None)}
BUDGET = {"device_gib": (_float(0, low_open=True), None), "host_pinned_gib": (_float(0), 0.0),
          "workspace_gib": (_float(0), DERIVED), "max_context_tokens": (_int(1, 2 ** 40), REQUIRED),
          "max_batch": (_int(1, 65536), REQUIRED),
          "margin_gib": (_float(0, 64), 2.0), "margin_fraction": (_float(0, 0.5), 0.03),
          "dynamic_slots_per_layer": (_int(0, 65536), 0), "ring_slots": (_int(0, 65536), 0),
          "pinned_backing": ("bool", False), "tier_mode": (_enum("inclusive", "exclusive"), "inclusive")}
OPTIONS = {"policy": (_enum("lru", "scored"), "lru"), "lookahead_units": (_int(0, 64), 2),
           "reader_threads": (_int(1, 64), 2), "staging_buffers": (_int(2, 64), 4),
           "staging_mib": (_int(1, 16384), 256), "copy_piece_mib": (_int(1, 4096), 64),
           "score_window": (_int(1, 4096), 16), "pin_planned": ("bool", True), "fill_residents": ("bool", True),
           "direct_io": ("bool", True), "verify_hash": ("bool", False), "group_units": (_int(0, 65536), 0),
           "spec_persistent": ("bool", False)}
ENGINE = {"prefetch_layers": (_int(0, 8), 1), "chunk_tokens": (_int(0, 1048576), 0),
          "routed_only_max_tokens": (_int(0, 1048576), 0), "idle_preload_layers": (_int(0, 64), 0),
          "max_copies_per_token": (_int(0, 65536), 0)}
PLACEMENT = {**BUDGET, **OPTIONS, **ENGINE}
UNITS = {"id": ("id", REQUIRED), "class": (_enum("moe", "blob"), REQUIRED), "layers": ("layers", None),
         "rank": (_enum("model", "list", "frequency"), "model"), "list": ("list", None),
         "resident": ("count", "fill"), "pinned": ("count", 0), "rest": (_enum("disk", "pinned", "peer"), "disk"),
         "peer": ("id", None)}
ROUTE_KEYS = {"id", "phase", "devices", "execute", "handoff_route", "links", "when", "fallback", "placement",
              "enabled"}
TOP_KEYS = {"schema", "name", "description", "generated_by", "pin", "match", "limits", "routes"}
MATCH_KEYS = {"model", "model_sha256", "engine", "devices", "links"}

BUDGET_FIELDS = ("device_gib", "host_pinned_gib", "workspace_gib", "max_context_tokens", "max_batch", "margin_gib",
                 "margin_fraction", "dynamic_slots_per_layer", "ring_slots", "pinned_backing", "staging_buffers",
                 "staging_mib", "tier_mode")
# §6.2: the only settable leaves besides limits, when, fallback and placement fields.
SETTABLE_DEVICE = ("name", "memory_min_gib", "host_ram_min_gib", "disk_read_gbytes_ps_min", "h2d_gbytes_ps_min")
SETTABLE_UNITS = ("layers", "rank", "list", "resident", "pinned")


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise RecipeError(f"{where or 'recipe'}: expected a table")
    for key in obj:
        if key.lower() in EXACT_KEYS:
            raise RecipeError(f"{where + '.' if where else ''}{key}: exactness is not a recipe option "
                              "(every placement is bit-identical)")
        if key.startswith("x-"):
            continue
        if key not in allowed:
            raise RecipeError(f'{where or "recipe"}: unknown key "{key}"')


def _value(kind, value, where):
    """One typed field value; raises naming the path."""
    tag = kind[0] if isinstance(kind, tuple) else kind
    if tag == "str":
        if not isinstance(value, str):
            raise RecipeError(f"{where}: expected a string")
        return value
    if tag == "id":
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise RecipeError(f"{where}: expected an id [a-z0-9_-]{{1,32}}")
        return value
    if tag == "bool":
        if type(value) is not bool:
            raise RecipeError(f"{where}: expected a boolean")
        return value
    if tag == "enum":
        if value not in kind[1:]:
            raise RecipeError(f"{where}: expected one of {', '.join(kind[1:])}")
        return value
    if tag == "int":
        if type(value) is not int or not kind[1] <= value <= kind[2]:
            raise RecipeError(f"{where}: expected an integer {kind[1]}..{kind[2]}")
        return value
    if tag == "float":
        _, low, high, low_open = kind
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RecipeError(f"{where}: expected a finite number")
        if value < low or (low_open and value == low) or value > high:
            raise RecipeError(f"{where}: expected a number {'>' if low_open else '>='} {low:g} and <= {high:g}")
        return float(value)
    if tag == "count":
        if value == "fill" or (type(value) is int and value >= 0):
            return value
        raise RecipeError(f'{where}: expected an integer >= 0 or "fill"')
    if tag == "list":
        if not isinstance(value, list) or len(value) > MAX_INDEX or any(type(v) is not int or v < 0 for v in value):
            raise RecipeError(f"{where}: expected an array of at most {MAX_INDEX} integers >= 0")
        if len(set(value)) != len(value):
            raise RecipeError(f"{where}: entries must be distinct")
        return list(value)
    if tag == "layers":
        if not isinstance(value, list) or len(value) > MAX_INDEX:
            raise RecipeError(f"{where}: expected an array of at most {MAX_INDEX} layer indices and [first, last] ranges")
        out, covered = [], set()
        for i, item in enumerate(value):
            if type(item) is int and 0 <= item < MAX_INDEX:
                span = {item}
                out.append(item)
            elif (isinstance(item, list) and len(item) == 2 and all(type(v) is int and 0 <= v < MAX_INDEX for v in item)
                  and item[0] <= item[1]):
                span = set(range(item[0], item[1] + 1))
                out.append(list(item))
            else:
                raise RecipeError(f"{where}[{i}]: expected a layer index < {MAX_INDEX} or [first, last] with "
                                  "first <= last")
            if span & covered:
                raise RecipeError(f"{where}[{i}]: overlaps an earlier entry")
            covered |= span
        return out
    if tag == "between":
        if (not isinstance(value, list) or len(value) != 2 or any(not isinstance(v, str) for v in value)
                or value[0] == value[1]):
            raise RecipeError(f"{where}: expected 2 distinct set ids")
        return list(value)
    if tag == "internal_link":
        _check_keys(value, set(LINK_REQ), where)
        return _fields(value, LINK_REQ, where)
    raise AssertionError(tag)


def _fields(obj, table, where):
    """Typed values of a table's fields; absent keys take their default (None/DERIVED = absent)."""
    out = {}
    for key, (kind, default) in table.items():
        if key in obj:
            out[key] = _value(kind, obj[key], f"{where}.{key}")
        elif default is REQUIRED:
            raise RecipeError(f'{where}: missing key "{key}"')
        else:
            out[key] = None if default is DERIVED else default
    return out


def _items(obj, key, where, required=False):
    """An array of tables, each paired with its error path (by id when it has a valid one)."""
    prefix = f"{where}.{key}" if where else key
    if key not in obj:
        if required:
            raise RecipeError(f'{where or "recipe"}: missing key "{key}"')
        return []
    value = obj[key]
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise RecipeError(f"{prefix}: expected an array of tables")
    out, seen = [], set()
    for i, item in enumerate(value):
        ident = item.get("id")
        valid = isinstance(ident, str) and ID.fullmatch(ident)
        if valid and ident in seen:
            raise RecipeError(f'{prefix}: duplicate id "{ident}"')
        seen.add(ident)
        out.append((item, f"{prefix}.{ident}" if valid else f"{prefix}[{i}]"))
    return out


def _check_tree(value, where):
    if isinstance(value, dict):
        for key, item in value.items():
            _check_tree(item, f"{where}.{key}" if where else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_tree(item, f"{where}[{i}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise RecipeError(f"{where or 'recipe'}: inf and nan are not recipe values")
    elif not isinstance(value, (str, int, float, bool)):
        raise RecipeError(f"{where or 'recipe'}: dates and times are not recipe values")


def load_text(text, origin="recipe"):
    """A document tree from TOML or JSON text (JSON when the first non-blank is `{`)."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise RecipeError(f"{origin}: larger than 16 MiB")
    try:
        if text.lstrip().startswith("{"):
            def no_constants(name):
                raise ValueError(f"{name} is not a number")

            def unique(pairs):
                out = {}
                for key, value in pairs:
                    if key in out:
                        raise ValueError(f'duplicate key "{key}"')
                    out[key] = value
                return out
            tree = json.loads(text, parse_constant=no_constants, object_pairs_hook=unique)
            if not isinstance(tree, dict):
                raise RecipeError(f"{origin}: expected a JSON object")
        else:
            tree = tomllib.loads(text)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, RecipeError):
            raise
        raise RecipeError(f"{origin}: not valid TOML or JSON: {exc}") from exc
    _check_tree(tree, "")
    return tree


# --- recipe ------------------------------------------------------------------------------------------------------

def _placement(obj, route, where):
    _check_keys(obj, set(PLACEMENT) | {"units"}, where)
    out = _fields(obj, PLACEMENT, where)
    groups = []
    for raw, gwhere in _items(obj, "units", where):
        _check_keys(raw, set(UNITS), gwhere)
        group = _fields(raw, UNITS, gwhere)
        if (group["rank"] == "list") != (group["list"] is not None):
            raise RecipeError(f'{gwhere}.list: required exactly when rank = "list"')
        if (group["rest"] == "peer") != (group["peer"] is not None):
            raise RecipeError(f'{gwhere}.peer: required exactly when rest = "peer"')
        if group["rest"] == "peer" and route["execute"] != "hetero":
            raise RecipeError(f'{gwhere}.rest: "peer" needs a hetero route')
        if group["rest"] == "pinned" and group["pinned"] == "fill":
            raise RecipeError(f'{gwhere}.rest: "pinned" with pinned = "fill" leaves the pinned tier unbounded')
        groups.append(group)
    out["units"] = groups
    return out


def parse(text, origin="recipe"):
    """A validated recipe as a normalized dict (defaults filled; ``_written`` = paths the file sets)."""
    tree = load_text(text, origin)
    _check_keys(tree, TOP_KEYS, "")
    for key in ("schema", "name", "match", "routes"):
        if key not in tree:
            raise RecipeError(f'recipe: missing key "{key}"')
    if tree["schema"] != SCHEMA:
        raise RecipeError(f"schema: expected {SCHEMA}")
    if not isinstance(tree["name"], str) or not NAME.fullmatch(tree["name"]):
        raise RecipeError("name: expected [A-Za-z0-9._-]{1,64}")
    recipe = {"schema": SCHEMA, "name": tree["name"],
              "description": _value("str", tree.get("description", ""), "description"),
              "generated_by": _value(_enum("human", "autotuner"), tree.get("generated_by", "human"), "generated_by")}
    pins = tree.get("pin", [])
    if not isinstance(pins, list) or any(not isinstance(p, str) for p in pins) or len(set(pins)) != len(pins):
        raise RecipeError("pin: expected an array of distinct paths")

    match = tree["match"]
    _check_keys(match, MATCH_KEYS, "match")
    if not isinstance(match.get("model"), str) or not match["model"]:
        raise RecipeError("match.model: expected a non-empty string")
    if "model_sha256" in match and (not isinstance(match["model_sha256"], str) or not SHA.fullmatch(match["model_sha256"])):
        raise RecipeError("match.model_sha256: expected 64 lowercase hex digits")
    devices = []
    for raw, where in _items(match, "devices", "match", required=True):
        _check_keys(raw, set(DEVICE), where)
        device = _fields(raw, DEVICE, where)
        if device["h2d_gbytes_ps_min"] > 0 and device["kind"] != "cuda":
            raise RecipeError(f"{where}.h2d_gbytes_ps_min: cuda sets only")
        devices.append(device)
    if not devices:
        raise RecipeError("match.devices: at least one device set")
    by_id = {d["id"]: d for d in devices}
    links = []
    for raw, where in _items(match, "links", "match"):
        _check_keys(raw, set(LINK), where)
        link = _fields(raw, LINK, where)
        for end in link["between"]:
            if end not in by_id:
                raise RecipeError(f'{where}.between: unknown set "{end}"')
        links.append(link)
    recipe["match"] = {"model": match["model"], "model_sha256": match.get("model_sha256"),
                       "engine": _value("str", match["engine"], "match.engine") if "engine" in match else None,
                       "devices": devices, "links": links}
    limits = tree.get("limits", {})
    _check_keys(limits, set(LIMITS), "limits")
    recipe["limits"] = _fields(limits, LIMITS, "limits")

    routes = []
    for raw, where in _items(tree, "routes", "", required=True):
        _check_keys(raw, ROUTE_KEYS, where)
        for key in ("id", "phase", "devices", "execute"):
            if key not in raw:
                raise RecipeError(f'{where}: missing key "{key}"')
        route = {"id": _value("id", raw["id"], f"{where}.id"),
                 "phase": _value(_enum("prefill", "decode"), raw["phase"], f"{where}.phase"),
                 "execute": _value(_enum("single", "tp", "replicas", "hetero"), raw["execute"], f"{where}.execute")}
        sets = raw["devices"]
        if (not isinstance(sets, list) or not sets or any(not isinstance(s, str) for s in sets)
                or len(set(sets)) != len(sets)):
            raise RecipeError(f"{where}.devices: expected >= 1 distinct set ids")
        for s in sets:
            if s not in by_id:
                raise RecipeError(f'{where}.devices: unknown set "{s}"')
        route["devices"] = list(sets)
        route["handoff_route"] = (_value("id", raw["handoff_route"], f"{where}.handoff_route")
                                  if "handoff_route" in raw else None)
        if route["handoff_route"] is not None and route["phase"] != "prefill":
            raise RecipeError(f"{where}.handoff_route: prefill routes only")
        rlinks = raw.get("links", [])
        if (not isinstance(rlinks, list) or any(not isinstance(l, str) for l in rlinks)
                or len(set(rlinks)) != len(rlinks)):
            raise RecipeError(f"{where}.links: expected distinct link ids")
        for l in rlinks:
            if l not in {x["id"] for x in links}:
                raise RecipeError(f'{where}.links: unknown link "{l}"')
        route["links"] = list(rlinks)
        when = raw.get("when", {})
        _check_keys(when, set(WHEN), f"{where}.when")
        route["when"] = _fields(when, WHEN, f"{where}.when")
        fallback = raw.get("fallback")
        route["fallback"] = None if fallback is None else _value("id", fallback, f"{where}.fallback")
        route["enabled"] = _value("bool", raw.get("enabled", True), f"{where}.enabled")
        counts = [by_id[s]["count"] for s in sets]
        execute = route["execute"]
        if ((execute == "single" and (len(sets) != 1 or counts[0] != 1))
                or (execute in ("tp", "replicas") and (len(sets) != 1 or counts[0] < 2))
                or (execute == "hetero" and len(sets) < 2)):
            raise RecipeError(f"{where}.execute: {execute} does not fit sets {', '.join(sets)} (counts {counts})")
        placements = raw.get("placement", {})
        _check_keys(placements, set(sets), f"{where}.placement")
        route["placement"] = {d: _placement(p, route, f"{where}.placement.{d}")
                              for d, p in placements.items() if not d.startswith("x-")}
        for d, placement in route["placement"].items():
            if placement["device_gib"] is None:
                placement["device_gib"] = by_id[d]["memory_min_gib"]   # the default, made explicit as the core does
        routes.append(route)
    recipe["routes"] = routes
    recipe["pin"] = list(pins)
    recipe["_written"] = sorted(_written_paths(tree))
    check(recipe)
    for i, pin in enumerate(pins):
        if not any(p == pin or p.startswith(pin + ".") for p in settable_paths(recipe)):
            raise RecipeError(f'pin[{i}]: "{pin}" is not a settable path or a segment prefix of one')
    return recipe


def when_empty(route):
    w = route["when"]
    return (w["prompt_tokens_min"] == 0 and w["prompt_tokens_max"] is None and w["batch_min"] == 1
            and w["batch_max"] is None)


def check(recipe):
    """Cross-field rules over a whole document (recipe or effective)."""
    devices = {d["id"]: d for d in recipe["match"]["devices"]}
    links = {l["id"]: l for l in recipe["match"]["links"]}
    routes = recipe["routes"]
    by_id = {}
    for route in routes:
        if route["id"] in by_id:
            raise RecipeError(f'routes: duplicate id "{route["id"]}"')
        by_id[route["id"]] = route
    if not any(r["phase"] == "decode" for r in routes):
        raise RecipeError("routes: no decode route")
    for phase in ("prefill", "decode"):
        own = [r for r in routes if r["phase"] == phase]
        if own and not when_empty(own[-1]):
            raise RecipeError(f'routes: the last {phase} route ("{own[-1]["id"]}") must have an empty `when` (catch-all)')
    for route in routes:
        where = f"routes.{route['id']}"
        w = route["when"]
        if w["prompt_tokens_max"] is not None and w["prompt_tokens_max"] < w["prompt_tokens_min"]:
            raise RecipeError(f"{where}.when.prompt_tokens_max: below prompt_tokens_min")
        if w["batch_max"] is not None and w["batch_max"] < w["batch_min"]:
            raise RecipeError(f"{where}.when.batch_max: below batch_min")
        handoff = route["handoff_route"]
        if handoff is not None and (handoff not in by_id or by_id[handoff]["phase"] != "decode"):
            raise RecipeError(f'{where}.handoff_route: "{handoff}" is not a decode route')
        fallback = route["fallback"]
        if fallback is not None:
            if fallback not in by_id:
                raise RecipeError(f'{where}.fallback: unknown route "{fallback}"')
            if fallback == route["id"] or by_id[fallback]["phase"] != route["phase"]:
                raise RecipeError(f"{where}.fallback: expected another route of the same phase")
            seen, step = {route["id"]}, fallback
            while step is not None:
                if step in seen:
                    raise RecipeError(f'{where}.fallback: cycle through "{step}"')
                seen.add(step)
                step = by_id[step]["fallback"]
        sets = list(dict.fromkeys(route["devices"] + (by_id[handoff]["devices"] if handoff else [])))
        if len(sets) > 1:
            edges = {(a, b) for l in route["links"] for a, b in (links[l]["between"], links[l]["between"][::-1])}
            reached, todo = {sets[0]}, [sets[0]]
            while todo:
                here = todo.pop()
                for other in sets:
                    if other not in reached and (here, other) in edges:
                        reached.add(other)
                        todo.append(other)
            if reached != set(sets):
                raise RecipeError(f"{where}.links: must connect {', '.join(sets)}")
        for set_id in route["devices"]:
            if set_id not in route["placement"]:
                raise RecipeError(f"{where}.placement.{set_id}: required (max_context_tokens, max_batch)")
        for set_id, placement in route["placement"].items():
            pwhere = f"{where}.placement.{set_id}"
            dev = devices[set_id]
            device_gib = dev["memory_min_gib"] if placement["device_gib"] is None else placement["device_gib"]
            if placement["device_gib"] is not None and placement["device_gib"] > dev["memory_min_gib"]:
                raise RecipeError(f"{pwhere}.device_gib: {placement['device_gib']:g} exceeds match.devices.{set_id}."
                                  f"memory_min_gib {dev['memory_min_gib']:g}")
            if dev["host_ram_min_gib"] > 0 and placement["host_pinned_gib"] > dev["host_ram_min_gib"]:
                raise RecipeError(f"{pwhere}.host_pinned_gib: {placement['host_pinned_gib']:g} exceeds "
                                  f"match.devices.{set_id}.host_ram_min_gib {dev['host_ram_min_gib']:g}")
            if placement["workspace_gib"] is not None and placement["workspace_gib"] >= device_gib:
                raise RecipeError(f"{pwhere}.workspace_gib: must be below device_gib ({device_gib:g})")
            order = route["devices"]
            for group in placement["units"]:
                gwhere = f"{pwhere}.units.{group['id']}"
                if group["rest"] == "peer" and (group["peer"] not in order
                                                or order.index(group["peer"]) <= order.index(set_id)):
                    raise RecipeError(f"{gwhere}.peer: must be a set after {set_id} in the route's devices")
                if (group["pinned"] != 0 or group["rest"] == "pinned") and placement["host_pinned_gib"] <= 0:
                    raise RecipeError(f"{pwhere}.host_pinned_gib: must be > 0 when a unit group uses the pinned tier")


# --- paths, layers ------------------------------------------------------------------------------------------------

def settable_paths(recipe):
    """Every settable leaf (§6.2) of this recipe; placement paths exist for every set of a route."""
    paths = ["limits.host_free_ram_floor_gib", "limits.host_free_commit_floor_gib"]
    for d in recipe["match"]["devices"]:
        paths += [f"match.devices.{d['id']}.{k}" for k in SETTABLE_DEVICE]
    paths += [f"match.links.{l['id']}.gbps_min" for l in recipe["match"]["links"]]
    for route in recipe["routes"]:
        base = f"routes.{route['id']}"
        paths += [base + ".fallback", base + ".enabled"]
        paths += [f"{base}.when.{k}" for k in WHEN]
        for set_id in route["devices"]:
            paths += [f"{base}.placement.{set_id}.{k}" for k in PLACEMENT]
            for group in route["placement"].get(set_id, {}).get("units", []):
                paths += [f"{base}.placement.{set_id}.units.{group['id']}.{k}" for k in SETTABLE_UNITS]
    return paths


def _written_paths(tree):
    written = {f"limits.{k}" for k in tree.get("limits", {})}
    for kind in ("devices", "links"):
        for item in tree["match"].get(kind, []):
            written |= {f"match.{kind}.{item.get('id')}.{k}" for k in item}
    for route in tree["routes"]:
        base = f"routes.{route.get('id')}"
        written |= {f"{base}.{k}" for k in ("fallback", "enabled") if k in route}
        written |= {f"{base}.when.{k}" for k in route.get("when", {})}
        for set_id, placement in route.get("placement", {}).items():
            if set_id.startswith("x-"):
                continue
            written |= {f"{base}.placement.{set_id}.{k}" for k in placement if k != "units"}
            for group in placement.get("units", []):
                written |= {f"{base}.placement.{set_id}.units.{group.get('id')}.{k}" for k in group}
    return written


def _locate(recipe, path):
    """(container dict, field, kind) for a settable path; raises naming why it is not settable."""
    if path not in settable_paths(recipe):
        raise RecipeError(f'"{path}": not a settable path (§6.2)')
    parts = path.split(".")
    if parts[0] == "limits":
        return recipe["limits"], parts[1], LIMITS[parts[1]][0]
    if parts[0] == "match":
        table = DEVICE if parts[1] == "devices" else LINK
        item = next(x for x in recipe["match"][parts[1]] if x["id"] == parts[2])
        return item, parts[3], table[parts[3]][0]
    route = next(r for r in recipe["routes"] if r["id"] == parts[1])
    if len(parts) == 3:
        return route, parts[2], "bool" if parts[2] == "enabled" else "id"
    if parts[2] == "when":
        return route["when"], parts[3], WHEN[parts[3]][0]
    placement = route["placement"][parts[3]]
    if len(parts) == 5:
        return placement, parts[4], PLACEMENT[parts[4]][0]
    group = next(g for g in placement["units"] if g["id"] == parts[5])
    return group, parts[6], UNITS[parts[6]][0]


def parse_layer(text, origin, recipe, recipe_sha256, probe=None):
    """A profile or overrides document checked against this recipe. A profile is checked against the identity
    (``probe``: the probe report with ``binding``, ``workload`` and ``descriptors``); one that does not fit raises
    StaleProfile with the reason."""
    tree = load_text(text, origin)
    schema = tree.get("schema")
    if schema == PROFILE_SCHEMA:
        _check_keys(tree, {"schema", "recipe", "recipe_sha256", "source", "workload", "key", "probe", "max_drift",
                           "set"}, "")
    elif schema == OVERRIDES_SCHEMA:
        _check_keys(tree, {"schema", "recipe", "recipe_sha256", "source", "set"}, "")
    else:
        raise RecipeError(f"{origin}: schema: expected {PROFILE_SCHEMA} or {OVERRIDES_SCHEMA}")
    if tree.get("recipe") != recipe["name"]:
        raise RecipeError(f'{origin}: recipe: is for "{tree.get("recipe")}", not "{recipe["name"]}"')
    sets = tree.get("set")
    if not isinstance(sets, dict):
        raise RecipeError(f'{origin}: missing table "set"')
    layer = {"kind": "profile" if schema == PROFILE_SCHEMA else "override", "origin": origin,
             "source": tree.get("source", ""), "set": list(sets.items())}
    sha = tree.get("recipe_sha256")
    if schema == PROFILE_SCHEMA:
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise RecipeError(f"{origin}: recipe_sha256: required (64 lowercase hex)")
        if sha != recipe_sha256:
            raise StaleProfile(f"{origin}: profile stale: tuned on another version of the recipe file")
        _check_profile_key(tree, origin, probe, recipe)
    elif sha is not None and sha != recipe_sha256:
        raise RecipeError(f"{origin}: recipe_sha256: written for another version of the recipe file")
    return layer


def _check_profile_key(tree, origin, probe, recipe):
    key, measured = tree.get("key"), tree.get("probe")
    sets = [d["id"] for d in recipe["match"]["devices"]]
    gpu_sets = [d["id"] for d in recipe["match"]["devices"] if d["kind"] in ("cuda", "hip")]
    cuda_sets = [d["id"] for d in recipe["match"]["devices"] if d["kind"] == "cuda"]
    if not isinstance(tree.get("workload"), str) or not tree["workload"]:
        raise RecipeError(f"{origin}: workload: required")
    if not isinstance(key, dict) or not isinstance(measured, dict):
        raise RecipeError(f"{origin}: key and probe: required")
    _check_keys(key, {"engine_sha256", "model_sha256", "devices", "drivers", "descriptors"}, f"{origin}: key")
    for name in ("engine_sha256", "model_sha256"):
        if not isinstance(key.get(name), str) or not SHA.fullmatch(key[name]):
            raise RecipeError(f"{origin}: key.{name}: required (64 lowercase hex)")
    for name, needed in (("devices", sets), ("drivers", gpu_sets), ("descriptors", sets)):
        if not isinstance(key.get(name), dict) or set(key[name]) != set(needed):
            raise RecipeError(f"{origin}: key.{name}: one entry for every set ({', '.join(needed)})")
    for set_id in sets:
        values = measured.get(set_id)
        need = {"disk_read_gbytes_ps"} | ({"h2d_gbytes_ps"} if set_id in cuda_sets else set())
        if not isinstance(values, dict) or not need <= set(values):
            raise RecipeError(f"{origin}: probe.{set_id}: needs {', '.join(sorted(need))}")
    if probe is None:
        raise StaleProfile(f"{origin}: profile stale: no identity (probe report) to check it against")
    if tree["workload"] != probe.get("workload"):
        raise StaleProfile(f"{origin}: profile stale: tuned for workload {tree['workload']}, now {probe.get('workload')}")
    for set_id, shas in key["descriptors"].items():
        if list(shas) != list((probe.get("descriptors") or {}).get(set_id, [])):
            raise StaleProfile(f"{origin}: profile stale: key.descriptors.{set_id} differs from the descriptors in use")
    drift = _value(_float(0, 1), tree.get("max_drift", 0.10), f"{origin}: max_drift")
    for name in ("engine_sha256", "model_sha256"):
        if name in key and key[name] != probe.get(name):
            raise StaleProfile(f"{origin}: profile stale: key.{name} differs from the probe")
    binding = probe.get("binding", {})
    for set_id, ids in key.get("devices", {}).items():
        if [m["id"] for m in binding.get(set_id, [])] != list(ids):
            raise StaleProfile(f"{origin}: profile stale: key.devices.{set_id} differs from the binding")
    by_id = {d["id"]: d for d in probe.get("devices", [])}
    for set_id, driver in key.get("drivers", {}).items():
        for member in binding.get(set_id, []):
            got = by_id.get(member["id"], {}).get("driver")
            if got != driver:
                raise StaleProfile(f"{origin}: profile stale: key.drivers.{set_id} is {driver}, probed {got}")
    for set_id, values in measured.items():
        for name, tuned in values.items():
            current = [by_id.get(m["id"], {}).get(name) for m in binding.get(set_id, [])]
            if not current or any(v is None for v in current):
                raise StaleProfile(f"{origin}: profile stale: probe.{set_id}.{name} not measured now")
            now = min(current)
            if abs(now - tuned) > drift * tuned:
                raise StaleProfile(f"{origin}: profile stale: probe.{set_id}.{name} {tuned:g} at tuning, {now:g} now")


def parse_set(text):
    """`path=value` from the command line; the value is TOML value text (a bare word is a string)."""
    if "=" not in text:
        raise RecipeError(f'--set "{text}": expected path=value')
    path, raw = (part.strip() for part in text.split("=", 1))
    try:
        value = tomllib.loads("v = " + raw)["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    return path, value


class Effective:
    """The effective recipe, its explain trace and the layers that were refused (stale profiles)."""

    def __init__(self, recipe, sha256, origin):
        self.recipe = recipe
        self.sha256 = sha256
        self.origin = origin
        self.trace = {}
        self.refused = []
        self.untuned = False

    def to_json(self):
        doc = deepcopy(self.recipe)
        doc.pop("_written", None)
        return doc

    def canonical_sha256(self):
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve(text, origin="recipe", profile=None, overrides=(), sets=(), probe=None):
    """recipe + at most one profile ((text, origin)) + overrides files ([(text, origin)]) + --set entries ->
    Effective. Each layer applies atomically; a stale profile is recorded in ``refused`` and the run is untuned."""
    recipe = parse(text, origin)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    eff = Effective(recipe, sha, origin)
    for path in settable_paths(recipe):
        eff.trace[path] = {"layer": "recipe" if path in recipe["_written"] else "default", "origin": origin,
                           "shadowed": []}
    layers = []
    if profile is not None:
        try:
            layers.append(parse_layer(profile[0], profile[1], recipe, sha, probe))
        except StaleProfile as exc:
            eff.refused.append(str(exc))
            eff.untuned = True
    for body, name in overrides:
        layers.append(parse_layer(body, name, recipe, sha))
    if sets:
        layers.append({"kind": "override", "origin": "--set", "source": "", "set": list(sets)})
    profile_placements = set()
    budget_overridden = []
    for layer in layers:
        candidate, trace = deepcopy(eff.recipe), deepcopy(eff.trace)
        if layer["kind"] == "profile":
            profile_placements = {".".join(path.split(".")[:4]) for path, _ in layer["set"]
                                  if path.startswith("routes.") and ".placement." in path}
            for path in budget_overridden:
                if path.startswith("match.") or ".".join(path.split(".")[:4]) in profile_placements:
                    raise RecipeError(f"{layer['origin']}: profile added after an override of {path}: drop the "
                                      "profile or retune")
        for path, value in layer["set"]:
            if layer["kind"] == "override" and (path.startswith("match.") or (
                    ".placement." in path and path.split(".")[-1] in BUDGET_FIELDS and len(path.split(".")) == 5)):
                applied = any(item["kind"] == "profile" for item in layers)
                if applied and (path.startswith("match.") or ".".join(path.split(".")[:4]) in profile_placements):
                    raise RecipeError(f"{layer['origin']}: {path} changes a budget the profile was tuned with: drop "
                                      "the profile or retune")
                budget_overridden.append(path)
            container, field, kind = _locate(candidate, path)
            entry = trace.setdefault(path, {"layer": "default", "origin": origin, "shadowed": []})
            pin = next((p for p in recipe["pin"] if path == p or path.startswith(p + ".")), None)
            checked = _value(kind, value, path)
            if layer["kind"] == "profile" and pin is not None:
                entry["shadowed"].append({"value": checked, "layer": "profile", "origin": layer["origin"],
                                          "why": f"pinned by recipe: {pin}"})
                continue
            entry["shadowed"].append({"value": container.get(field), "layer": entry["layer"],
                                      "origin": entry["origin"], "why": "overridden"})
            container[field] = checked
            entry.update(layer=layer["kind"],
                         origin=layer["origin"] + (f" ({layer['source']})" if layer["source"] else ""))
        if layer["origin"] == "--set":
            for path, _ in layer["set"]:
                if path.endswith((".rank", ".list")) and ".units." in path:
                    other = path[: path.rindex(".")] + (".list" if path.endswith(".rank") else ".rank")
                    if other not in {q for q, _ in layer["set"]} and (path.endswith(".list") or value_of(candidate, path) == "list"):
                        raise RecipeError(f"--set {path}: rank = \"list\" and its list change together (one overrides file)")
        try:
            check(candidate)
        except RecipeError as exc:
            raise RecipeError(f"{layer['origin']}: refused, the effective recipe would be invalid: {exc}") from exc
        eff.recipe, eff.trace = candidate, trace
    return eff


def value_of(recipe, path):
    container, field, _ = _locate(deepcopy(recipe), path)
    return container.get(field)


def explain(eff):
    """`strix-stream/recipe-explain-v1`-shaped: every settable leaf with value, layer, origin and shadowed values."""
    rows = []
    shadow = deepcopy(eff.recipe)
    for path in sorted(settable_paths(eff.recipe)):
        container, field, _ = _locate(shadow, path)
        entry = eff.trace.get(path, {"layer": "default", "origin": eff.origin, "shadowed": []})
        rows.append({"path": path, "value": container.get(field), "layer": entry["layer"], "origin": entry["origin"],
                     "shadowed": entry["shadowed"]})
    return {"schema": "strix-stream/recipe-explain-v1", "recipe": eff.recipe["name"], "fields": rows,
            "refused_layers": list(eff.refused), "untuned": eff.untuned}


# --- admission-time route selection (§7) -------------------------------------------------------------------------

def when_holds(when, tokens, batch):
    return (tokens >= when["prompt_tokens_min"]
            and (when["prompt_tokens_max"] is None or tokens <= when["prompt_tokens_max"])
            and batch >= when["batch_min"] and (when["batch_max"] is None or batch <= when["batch_max"]))


def placement_defaults():
    """The schema default of every optional placement field (required ones absent)."""
    out = {k: (None if d is DERIVED else d) for k, (_, d) in PLACEMENT.items() if d is not REQUIRED}
    out["units"] = []
    return out


def counting_requirements(recipe_sha, route, state):
    """The requirements that count for a route (§7.3): current effective recipe, the member's bound device, the
    descriptor in use. Returns ({(set, member): requirement}, why not compiled or None)."""
    binding, descriptors = state.get("binding", {}), state.get("descriptors", {})
    found = {}
    for req in state.get("requirements", []):
        if req.get("route") != route["id"]:
            continue
        set_id, member = req.get("set"), req.get("member")
        members = binding.get(set_id, [])
        if not isinstance(member, int) or not 0 <= member < len(members):
            continue
        in_use = (descriptors.get(set_id) or [None] * len(members))
        if (req.get("effective_sha256") == recipe_sha and req.get("id") == members[member]["id"]
                and member < len(in_use) and req.get("descriptor_sha256") == in_use[member]):
            if (set_id, member) in found:
                return found, f"route {route['id']}: two requirements for {set_id}[{member}]"
            found[(set_id, member)] = req
    for set_id in route["devices"]:
        for member in range(len(binding.get(set_id, []))):
            if (set_id, member) not in found:
                return found, f"route {route['id']}: not compiled for this recipe ({set_id}[{member}])"
    return found, None


def _running_credit(recipe_sha, route, found, state):
    plan_ids = sorted(req["plan_id"] for req in found.values())
    for entry in state.get("running", []):
        if (isinstance(entry, dict) and entry.get("route") == route["id"]
                and entry.get("effective_sha256") == recipe_sha and sorted(entry.get("plan_ids", [])) == plan_ids):
            return True
    return False


def admissible(recipe, route, state, by_id, recipe_sha):
    """(None, charge per host in bytes) when the route (with its handoff route) can be admitted, else (why, {}).
    A route credited as running is charged 0 but every host it uses still keeps the floors (§7.3)."""
    binding = state.get("binding", {})
    devices, links, hosts = state.get("devices", {}), state.get("links", {}), state.get("hosts", {})
    sets_by_id = {d["id"]: d for d in recipe["match"]["devices"]}
    if not route["enabled"]:
        return "disabled", {}
    involved = [route] + ([by_id[route["handoff_route"]]] if route["handoff_route"] else [])
    context, batch = state.get("context_tokens", state.get("prompt_tokens", 0)), state.get("batch", 1)
    if route["handoff_route"]:
        handoff = by_id[route["handoff_route"]]
        if not handoff["enabled"]:
            return f'handoff route "{handoff["id"]}" is disabled', {}
        if not when_holds(handoff["when"], state.get("prompt_tokens", 0), batch):
            return f'handoff route "{handoff["id"]}": when does not hold', {}
    charge, commit, used = {}, {}, set()
    for r in involved:
        for set_id in r["devices"]:
            members = binding.get(set_id)
            if not members:
                return f'set "{set_id}" is not bound', {}
            for member in members:
                info = devices.get(member["id"])
                if not info or not info.get("available", False):
                    return (f'set "{set_id}" member {member["id"]} unavailable '
                            f'({(info or {}).get("why", "missing from the state")})'), {}
                used.add(member["host"])
            if sets_by_id[set_id]["internal_link"] is not None and len({m["host"] for m in members}) >= 2:
                info = links.get(f"{set_id}.internal")
                if not info or not info.get("up", False):
                    return f'link "{set_id}.internal" down ({(info or {}).get("why", "missing from the state")})', {}
            placement = r["placement"][set_id]
            if context > placement["max_context_tokens"]:
                return (f"route {r['id']}: context {context} > max_context_tokens {placement['max_context_tokens']} "
                        f"on {set_id}"), {}
            if batch > placement["max_batch"]:
                return f"route {r['id']}: batch {batch} > max_batch {placement['max_batch']} on {set_id}", {}
        for link in r["links"]:
            info = links.get(link)
            if not info or not info.get("up", False):
                return f'link "{link}" down ({(info or {}).get("why", "missing from the state")})', {}
        found, why = counting_requirements(recipe_sha, r, state)
        if why is not None:
            return why, {}
        for (set_id, member), req in found.items():
            if req.get("disk_bytes", 0) > 0 and not devices.get(req["id"], {}).get("disk_available", False):
                return f'set "{set_id}" member {req["id"]}: disk unavailable', {}
        if _running_credit(recipe_sha, r, found, state):
            continue
        for (set_id, member), req in found.items():
            host = binding[set_id][member]["host"]
            charge[host] = charge.get(host, 0) + int(req["host_bytes"])
            commit[host] = commit.get(host, 0) + int(req.get("commit_bytes", req["host_bytes"]))
    floor, commit_floor = recipe["limits"]["host_free_ram_floor_gib"], recipe["limits"]["host_free_commit_floor_gib"]
    for host in sorted(used):
        free = hosts.get(host, {}).get("free_ram_gib")
        if free is None:
            return f"host {host}: free RAM unknown", {}
        if free - charge.get(host, 0) / GIB_BYTES < floor:
            return (f"host {host}: free RAM {free:.1f} GiB - charge {charge.get(host, 0) / GIB_BYTES:.1f} GiB < floor "
                    f"{floor:g} GiB"), {}
        if commit_floor > 0:
            free_commit = hosts.get(host, {}).get("free_commit_gib")
            if free_commit is None:
                return f"host {host}: free commit unknown", {}
            if free_commit - commit.get(host, 0) / GIB_BYTES < commit_floor:
                return (f"host {host}: free commit {free_commit:.1f} GiB - charge {commit.get(host, 0) / GIB_BYTES:.1f} "
                        f"GiB < commit floor {commit_floor:g} GiB"), {}
    return None, {h: charge.get(h, 0) for h in sorted(used) if charge.get(h, 0)}


def select(recipe, state, recipe_sha):
    """§7: `strix-stream/recipe-select-v1` {route, handoff_route, charge, trace[{route, verdict, why}]}; raises
    RecipeError when no route can be admitted (the message lists every visited route and why). ``recipe_sha`` is the
    effective recipe's sha256 the requirements and running credits must name."""
    phase, tokens, batch = state["phase"], state.get("prompt_tokens", 0), state.get("batch", 1)
    by_id = {r["id"]: r for r in recipe["routes"]}

    def result(route, charge, trace):
        return {"schema": "strix-stream/recipe-select-v1", "recipe": recipe["name"], "effective_sha256": recipe_sha,
                "route": route["id"], "handoff_route": route["handoff_route"], "charge": charge, "trace": trace}
    if state.get("session_route") is not None:
        route = by_id.get(state["session_route"])
        why, charge = ("unknown route", {}) if route is None else admissible(recipe, route, state, by_id, recipe_sha)
        if why is not None:
            raise RecipeError(f'session route "{state["session_route"]}" cannot be admitted ({why})')
        return result(route, charge, [{"route": route["id"], "verdict": "admitted", "why": None}])
    visited, trace = set(), []
    for route in (r for r in recipe["routes"] if r["phase"] == phase):
        if route["id"] in visited:
            continue
        visited.add(route["id"])
        if not route["enabled"]:
            trace.append({"route": route["id"], "verdict": "disabled", "why": None})
            continue
        if not when_holds(route["when"], tokens, batch):
            trace.append({"route": route["id"], "verdict": "no match", "why": None})
            continue
        step = route
        while step is not None:
            why, charge = admissible(recipe, step, state, by_id, recipe_sha)
            if why is None:
                trace.append({"route": step["id"], "verdict": "admitted", "why": None})
                return result(step, charge, trace)
            trace.append({"route": step["id"], "verdict": "not admitted", "why": why})
            target = by_id.get(step["fallback"]) if step["fallback"] else None
            step = None
            if target is not None and target["id"] not in visited:
                visited.add(target["id"])
                if not target["enabled"]:
                    trace.append({"route": target["id"], "verdict": "disabled", "why": "fallback target"})
                elif when_holds(target["when"], tokens, batch):
                    step = target
                else:
                    trace.append({"route": target["id"], "verdict": "no match", "why": "fallback target"})
    reasons = "; ".join(f"{t['route']}: {t['why'] or t['verdict']}" for t in trace)
    raise RecipeError(f"no admissible {phase} route ({reasons or 'no route'})")
