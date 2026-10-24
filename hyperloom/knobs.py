"""Per-pin qualified advanced knobs (SPEC §3.1): data from the preset, never invented here.

A preset's ``advanced`` table names switches that its pinned engine documents with a
qualification record. The production value renders nothing new (it is the code default or
already in the production line); any other value is a named, visible override and marks
the launch as not the production line.
"""
from __future__ import annotations

import json
import re

from .config import ConfigError, read_json, record
from .paths import repo_root


KINDS = ("argv", "argv-equals", "env")
# Identity, transport, layout, numerics, endpoints and assets are never knobs.
RESERVED_ARGV = re.compile(r"--(rank|transport|prefill-path|http-bind|http-port|http-api-key-file|http-serve"
                           r"|context|unqualified-context|numerical-profile|prefill-numerical-profile"
                           r"|optimization-profile|residency|execution-mode|dspark|dspark-vocab|mtp-sidecar.*"
                           r"|mtp-golden.*|mtp-target-.*|mtp-source-.*|mtp-converter-.*|mtp-norm-.*"
                           r"|mtp-conversion|mtp-.*-rounding|mtp-draft-vocab|mtp-draft-profile)")
ENV_NAME = re.compile(r"STRIX_[A-Z0-9_]{1,64}")
RESERVED_ENV = re.compile(r"STRIX_(DIST_|DS41_(OVERLAY|CB4|TRELLIS)|.*(TRANSPORT|LAYOUT|USB4|DEVICE|VISIBLE)).*")


def engine_pin():
    return read_json(repo_root() / "versions.lock")["components"]["engine"]["commit"]


def _scalar(value, where):
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value)):
        return value
    raise ConfigError(f"{where}: expected integer or short token")


def check_table(data):
    """Validate a preset's advanced table against its own production line."""
    table = record(data.get("advanced", {}), "preset.advanced",
                   optional=data.get("advanced", {}) if isinstance(data.get("advanced"), dict) else ())
    for name, knob in table.items():
        where = f"preset.advanced.{name}"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name) or name.startswith("-"):
            raise ConfigError(f"{where}: invalid knob name")
        record(knob, where, ("kind", "target", "values", "production", "qualification"), ("requires", "conflicts", "effect"))
        target, kind = knob["target"], knob["kind"]
        if kind not in KINDS:
            raise ConfigError(f"{where}: unknown kind")
        if kind == "env":
            if not ENV_NAME.fullmatch(target) or RESERVED_ENV.fullmatch(target) or target in data["env"]:
                raise ConfigError(f"{where}: env target is reserved or part of the production identity")
        else:
            if not re.fullmatch(r"--[a-z0-9][a-z0-9-]{0,63}", target) or RESERVED_ARGV.fullmatch(target):
                raise ConfigError(f"{where}: argv target is reserved")
            if target in data["knobs"] or any(t == target or t.startswith(target + "=") for t in data["argv"]):
                raise ConfigError(f"{where}: target is already set by the production line or a basic knob")
        values = knob["values"]
        if not isinstance(values, list) or not 2 <= len(values) <= 16:
            raise ConfigError(f"{where}: expected 2..16 qualified values")
        values = [_scalar(v, where) for v in values]
        if len(set(map(json.dumps, values))) != len(values) or knob["production"] not in values:
            raise ConfigError(f"{where}: values must be distinct and include the production value")
        q = record(knob["qualification"], f"{where}.qualification", ("receipt", "engine_sha", "evidence"))
        for key in ("receipt", "evidence"):
            if not isinstance(q[key], str) or not q[key].strip():
                raise ConfigError(f"{where}.qualification.{key}: required")
        if q["engine_sha"] != data["engine_sha"]:
            raise ConfigError(f"{where}: qualified at another engine pin")
        for relation in ("requires", "conflicts"):
            rows = record(knob.get(relation, {}), f"{where}.{relation}", optional=table)
            if name in rows:
                raise ConfigError(f"{where}.{relation}: refers to itself")
            for other, allowed in rows.items():
                if not isinstance(allowed, list) or not allowed or any(v not in table[other]["values"] for v in allowed):
                    raise ConfigError(f"{where}.{relation}.{other}: expected qualified values")
    return table


def validate(chosen, data):
    """Validate a profile's ``advanced`` selection; returns {name: value}."""
    table = check_table(data)
    chosen = record(chosen, "profile.advanced", optional=table)
    if chosen and data["engine_sha"] != engine_pin():
        raise ConfigError("profile.advanced: preset is not qualified at the pinned engine; advanced knobs refused")
    for name, value in chosen.items():
        if type(value) is bool or value not in table[name]["values"]:
            raise ConfigError(f"profile.advanced.{name}: value is not qualified at this pin")
    effective = {name: chosen.get(name, knob["production"]) for name, knob in table.items()}
    for name, value in chosen.items():
        for other, allowed in table[name].get("requires", {}).items():
            if effective[other] not in allowed:
                raise ConfigError(f"profile.advanced.{name}: requires {other} in {allowed}")
        for other, banned in table[name].get("conflicts", {}).items():
            if effective[other] in banned:
                raise ConfigError(f"profile.advanced.{name}: conflicts with {other}={effective[other]}")
    return chosen


def summary(profile, data):
    """Visible description of a launch: production is False when any knob leaves its production value."""
    table = data.get("advanced", {})
    changed = {name: value for name, value in profile.get("advanced", {}).items()
               if value != table[name]["production"]}
    return {"production": not changed, "advanced": changed,
            "qualification": {name: table[name]["qualification"] for name in changed}}


def apply(argv, env, profile, data):
    """Add the non-production advanced values to one rendered process (in table order)."""
    chosen = profile.get("advanced", {})
    for name, knob in data.get("advanced", {}).items():
        if name not in chosen or chosen[name] == knob["production"]:
            continue
        value = str(chosen[name])
        if knob["kind"] == "env":
            env[knob["target"]] = value
        elif knob["kind"] == "argv-equals":
            argv.append(knob["target"] + "=" + value)
        else:
            argv.extend((knob["target"], value))
