"""Translate frozen presets to argv/env and TensorMeld router objects."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib
import ipaddress
from pathlib import Path, PurePosixPath
import re
import sys

from . import engines, knobs
from .config import ConfigError, family, listen, preset, validate_overrides
from .paths import repo_root


@dataclass(frozen=True)
class ProcessSpec:
    machine_id: str
    role: str
    argv: list[str]
    env: dict[str, str]
    ready: str


def _profile(cluster, name):
    if name not in cluster.profiles:
        raise ConfigError(f"unknown profile: {name}")
    p = cluster.profiles[name]
    kind = p.get("preset", name)
    data = preset(kind)
    validate_overrides(p.get("overrides", {}), data)
    knobs.validate(p.get("advanced", {}), data)
    mids = engines.members(p, family(kind))
    return p, kind, data, mids


def port_flag(data):
    """The argv flag that carries the serve port ({port}): --http-port, or --port for the MiniMax serve."""
    return data["argv"][data["argv"].index("{port}") - 1]


def _port(profile, data):
    return profile.get("overrides", {}).get(port_flag(data), profile.get("port", data["port"]))


def _tokens(asset):
    path = Path(asset.get("control_path", asset["path"]))
    try:
        with path.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
    except OSError as exc:
        raise ConfigError("calibration_tokens: unavailable; configure a control_path on the control machine") from exc
    if len(raw) > 256 * 1024 or len(raw) != asset["bytes"] or hashlib.sha256(raw).hexdigest() != asset["sha256"]:
        raise ConfigError("calibration_tokens: size or sha256 mismatch")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ConfigError("calibration_tokens: expected UTF-8 token IDs") from exc
    if not value or not re.fullmatch(r"[0-9]+(?:[\s,]+[0-9]+)*", value):
        raise ConfigError("calibration_tokens: expected nonnegative integer IDs separated by whitespace or commas")
    ids = re.split(r"[\s,]+", value)
    if len(ids) > 32768 or any(len(x) > 10 or int(x) > 2147483647 for x in ids):
        raise ConfigError("calibration_tokens: token count or ID out of bounds")
    return [str(int(x)) for x in ids]


def diagnostic_env(data, names, values):
    """Non-production diagnostic env from the preset's verified table; never replaces production env."""
    table = data.get("diagnostics", {})
    # Advanced knob env belongs to the launch, not to an observation switch.
    knob_env = {knob["target"] for knob in data.get("advanced", {}).values() if knob["kind"] == "env"}
    env = {}
    for name in names:
        if name not in table:
            raise ConfigError(f"unknown diagnostic for this preset: {name}")
        for key, value in table[name]["env"].items():
            if key in data["env"] or key in env or key in knob_env:
                raise ConfigError(f"diagnostic {name}: {key} is already set")
            env[key] = value.format_map(values)
    return env


def render_processes(cluster, profile_name, *, diagnostics=(), run=None) -> list[ProcessSpec]:
    """Production render; diagnostics (names from the preset table) add observation-only env under <root>/log."""
    profile, kind, data, mids = _profile(cluster, profile_name)
    engine = engines.adapter(family(kind))
    replicas = engine.topology == "replicas"
    if diagnostics and (run is None or not re.fullmatch(r"[0-9a-f]{8,32}", run)):
        raise ConfigError("diagnostic runs need a hex run id")
    if "release_id" not in profile:
        raise ConfigError("profile.release_id: required to render an immutable release executable path")
    result = []
    for rank, mid in enumerate(mids):
        machine = cluster.machines[mid]
        assets = cluster.assets.get(mid, {})
        missing = set(data["assets"]) - assets.keys()
        if missing:
            raise ConfigError(f"{mid}: missing assets {sorted(missing)}")
        values = {name: item["path"] for name, item in assets.items()}
        if engine.key_file:
            try:
                values["key_file"] = profile["api_key_files"][mid]
            except KeyError as exc:
                raise ConfigError(f"{mid}: missing node-local api_key_files path") from exc
        values.update(binary=str(PurePosixPath(machine.install_root) / "releases" / profile["release_id"] / "bin" / data["binary"]),
                      address=machine.http_address if engine.bind == "lan" else "127.0.0.1",
                      port=str(_port(profile, data)), rank=str(rank))
        if "pins" in data:
            # These pins are part of the frozen numerical identity, not capacity settings.
            pins = data["pins"]
            if (assets["sidecar"]["sha256"] != pins["sidecar_sha256"]
                    or assets["sidecar"]["bytes"] != pins["sidecar_bytes"]):
                raise ConfigError("sidecar: manifest does not match the qualified preset")
            values["golden_sha256"] = assets["golden"]["sha256"]
            if values["golden_sha256"] != pins["golden_sha256"]:
                raise ConfigError("golden: manifest does not match the qualified preset")
        tokens = _tokens(assets["calibration_tokens"]) if "{tokens}" in data["argv"] else []
        argv = []
        for token in data["argv"]:
            if token == "{tokens}":
                argv.extend(tokens)
            else:
                argv.append(token.format_map(values))
        for flag, value in profile.get("overrides", {}).items():
            if flag in argv:
                argv[argv.index(flag) + 1] = str(value)
            else:
                argv.extend((flag, str(value)))
        env = {key: value.format_map(values) for key, value in data["env"].items()}
        knobs.apply(argv, env, profile, data)
        if diagnostics:
            env.update(diagnostic_env(data, diagnostics, {
                "log_dir": str(PurePosixPath(machine.install_root) / "log"), "run": run,
                "role": "replica-" + str(rank) if replicas else f"rank{rank}"}))
        result.append(ProcessSpec(mid, "replica" if replicas else f"rank{rank}", argv, env, engine.ready))
    return result


def launch_summary(cluster, profile_name) -> dict:
    """Whether a launch is the qualified production line, and which advanced knobs differ."""
    profile, kind, data, _ = _profile(cluster, profile_name)
    return dict(knobs.summary(profile, data), receipt=data["receipt"], engine_sha=data["engine_sha"],
                family=family(kind), preset=kind)


def render_router(cluster, profile_name) -> dict:
    profile, kind, data, mids = _profile(cluster, profile_name)
    engine = engines.adapter(family(kind))
    settings = cluster.router
    host = settings.get("machine", "local")
    default_listen = cluster.pc.get("router_listen", "127.0.0.1:18600")
    if host != "local":
        address = cluster.machines[host].http_address
        default_listen = f"[{address}]:18600" if ":" in address else f"{address}:18600"
    endpoint = listen(settings.get("listen", default_listen), "router.listen")
    obj = {"schema": "tensormeld/replica-router-v1", "listen": endpoint, "replicas": [],
           "balance_active_decodes": settings.get("balance_active_decodes", True)}
    if "api_key_file" in settings:
        obj["api_key_file"] = settings["api_key_file"]
    if not ipaddress.ip_address(endpoint["address"]).is_loopback and "api_key_file" not in obj:
        raise ConfigError("non-loopback router requires a router-host-local api_key_file")
    keys = settings.get("replica_api_key_files", {})
    for mid in engines.router_members(family(kind), mids):
        replica = {"id": mid, "node": mid, "address": cluster.machines[mid].http_address,
                   "port": _port(profile, data), "max_inflight": settings.get("max_inflight", 8)}
        if engine.bind == "loopback":
            # No key support in the engine: it binds 127.0.0.1, so only a router on the same machine reaches it.
            if host != mid:
                raise ConfigError(f"{family(kind)} engines bind loopback only: run the router on {mid}")
            replica["address"] = "127.0.0.1"
        if engine.key_file:
            if mid not in keys:
                raise ConfigError(f"{mid}: missing router-host-local replica_api_key_files path")
            replica["api_key_file"] = keys[mid]
        obj["replicas"].append(replica)
    prefill = {"policy": settings.get("policy", "nodes")}
    if "prefill_service" in profile:
        # Phase 3a: the service listens on the control machine's loopback, so the router runs there too.
        from . import rtx
        if host != "local":
            raise ConfigError("the RTX prefill service is loopback-only: run the router locally")
        sid = profile["prefill_service"]["machine"]
        if sid not in keys:
            raise ConfigError(f"{sid}: missing router-host-local prefill service key file")
        prefill = rtx.router_prefill(cluster, profile_name, keys[sid])
    for key in ("node_profile", "decode_s_per_token", "cache"):
        if key in prefill:
            continue  # the service preset's measured curves for this decode profile
        if key in settings:
            prefill[key] = deepcopy(settings[key])
        elif key in data.get("router", {}):
            prefill[key] = deepcopy(data["router"][key])
    if prefill["policy"] == "auto" and "node_profile" not in prefill:
        raise ConfigError("auto policy requires a measured node_profile")
    obj["prefill"] = prefill
    if "limits" in settings:
        obj["limits"] = deepcopy(settings["limits"])
    return obj


def router_policies():
    """The prefill policies the pinned TensorMeld router parser accepts (recipes need "routes")."""
    source = str(repo_root() / "third_party" / "tensormeld" / "src")
    sys.path.insert(0, source)
    try:
        return tuple(importlib.import_module("tensormeld.replica_router").PREFILL_POLICIES)
    finally:
        sys.path.remove(source)


def validate_router(obj):
    """Run the pinned parser, including its real key-file checks; no network or GPU."""
    source = str(repo_root() / "third_party" / "tensormeld" / "src")
    sys.path.insert(0, source)
    try:
        parser = importlib.import_module("tensormeld.replica_router")
        return parser.RouterConfig.parse(obj)
    finally:
        sys.path.remove(source)
