"""Engine adapter registry: one row per engine family, the only place that knows how a family runs.

Presets are data (argv, env, assets, pins); this table says how hyperloom builds, places, waits for and stops the
binary a preset launches. Each family keeps its own engine (SPEC §16; main 01:0x): the modularity lives here.

Fields:
- binary / targets: the serve executable and the ninja targets the node builds (only these are copied to a release).
- topology: ``replicas`` = one process per node, one single GPU turn each; ``pair`` = TP2 rank 0 / rank 1 in one
  PAIR turn (gpu-turn-pair.sh), with the production layout re-checked after acquiring it, and the router sends to
  rank 0 as its single replica.
- ready: the node readiness adapter (hyperloom_lease.adapter_ready), always combined with ``health_path`` = 200.
- drain: ``sigterm`` (no HTTP drain), ``admin-drain`` (POST /admin/drain on rank 0, then SIGTERM), ``leader-first``
  (SIGTERM rank 0 first; it sends the shutdown block that ends rank 1, which is then signalled).
- bind / key_file: ``lan`` engines bind the node's private address and authenticate generation with a key file;
  ``loopback`` engines have no key support, bind 127.0.0.1 and are reachable only through a router on rank 0's
  machine (which authenticates its own LAN listener, SPEC §2.7).
- trellis: how the strix-trellis component is built and linked (None, ``ds41`` or ``minimax``).
- label: GPU-turn label prefix (always ``hl-``; SPEC §16.4).
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .config import PRESET_NAMES, ConfigError, preset as load_preset, read_json
from .paths import repo_root


@dataclass(frozen=True)
class Adapter:
    family: str
    binary: str
    targets: tuple
    topology: str
    ready: str
    health_path: str
    drain: str
    bind: str
    key_file: bool
    trellis: str | None
    label: str
    layout_required: str | None
    capabilities: tuple


FAMILIES = {
    "qwen-replicas": Adapter("qwen-replicas", "strix-hip-decode-token", ("strix-hip-decode-token",), "replicas",
                             "qwen-stderr", "/health", "sigterm", "lan", True, None, "hl-qwen", None,
                             ("http-serve", "key-file", "metrics", "api-status")),
    "ds41-pair": Adapter("ds41-pair", "strix-deepseek41-serve", ("strix-deepseek41-serve",), "pair",
                         "ds41-json", "/health", "admin-drain", "lan", True, "ds41", "hl-ds41", "1",
                         ("http-serve", "key-file", "metrics", "api-status", "admin-drain", "import-state")),
    "minimax-pair": Adapter("minimax-pair", "strix-minimax-serve", ("strix-minimax-serve",), "pair",
                            "minimax-stdout", "/health", "leader-first", "loopback", False, "minimax", "hl-mm", "1",
                            ("http-serve", "metrics")),
}

# PRESET_NAMES (from config) lists every launchable preset; the RTX prefill service preset is a separate schema.
SHA = re.compile(r"[0-9a-f]{40}")


def adapter(family):
    try:
        return FAMILIES[family]
    except KeyError as exc:
        raise ConfigError(f"unknown engine family: {family}") from exc


def family_of(data, name):
    return data.get("family", name)


def for_preset(name):
    data = load_preset(name)
    return adapter(family_of(data, name)), data


def members(settings, family):
    """The profile's machines in launch order (pair: rank 0, rank 1)."""
    if adapter(family).topology == "replicas":
        return list(settings["nodes"])
    return [settings["rank0"], settings["rank1"]]


def router_members(family, mids):
    return list(mids) if adapter(family).topology == "replicas" else list(mids[:1])


def lock():
    return read_json(repo_root() / "versions.lock")


def components(data, versions=None):
    """versions.lock's default pins, overridden by the preset's own component pins. A pin is an immutable full SHA,
    or (for a source that only exists in a local repository) a short ``commit_prefix`` that an install resolves to a
    full SHA from ``--source`` and records in the release manifest."""
    versions = versions if versions is not None else lock()
    result = {name: {"repo": row["repo"], "commit": row["commit"]} for name, row in versions["components"].items()}
    for name, row in (data.get("components") or {}).items():
        result[name] = dict(row)
    return result


def check_components(data, where="preset"):
    rows = data.get("components") or {}
    if not isinstance(rows, dict):
        raise ConfigError(f"{where}.components: expected object")
    for name, row in rows.items():
        if not isinstance(row, dict) or not isinstance(row.get("repo"), str) or row["repo"].startswith("-"):
            raise ConfigError(f"{where}.components.{name}: expected repo and commit")
        commit, prefix = row.get("commit"), row.get("commit_prefix")
        if set(row) - {"repo", "commit", "commit_prefix", "note"}:
            raise ConfigError(f"{where}.components.{name}: unknown keys")
        if commit is not None and not (isinstance(commit, str) and SHA.fullmatch(commit)):
            raise ConfigError(f"{where}.components.{name}.commit: expected a full SHA")
        if commit is None and not (isinstance(prefix, str) and re.fullmatch(r"[0-9a-f]{7,39}", prefix)):
            raise ConfigError(f"{where}.components.{name}: a full commit or a commit_prefix is required")
    engine = components(data)["engine"]
    if engine.get("commit") != data["engine_sha"]:
        raise ConfigError(f"{where}: engine_sha must equal the preset's engine pin")


def recipe_options(data, family):
    """Build options beyond versions.lock's shared HIP configure. Empty for the original presets, so their
    recipe (and release id) is unchanged."""
    build = data.get("build") or {}
    allowed = {"configure", "trellis_api", "trellis_configure"}
    if set(build) - allowed:
        raise ConfigError("preset.build: unknown keys")
    result = {}
    if "configure" in build:
        result["configure"] = [str(flag) for flag in build["configure"]]
    trellis = adapter(family).trellis
    if trellis == "minimax":
        result["trellis_mode"] = "minimax"
    if "trellis_api" in build:
        if trellis != "ds41" or build["trellis_api"] not in ("v12", "tp03", "splitk04", "dense_r1"):
            raise ConfigError("preset.build.trellis_api: DeepSeek trellis adapters only (v12, tp03, splitk04, dense_r1)")
        result["trellis_api"] = build["trellis_api"]
    if "trellis_configure" in build:
        flags = build["trellis_configure"]
        if trellis is None or not isinstance(flags, list) or not all(
                isinstance(f, str) and re.fullmatch(r"-D[A-Z0-9_]+=[A-Za-z0-9_.]+", f) for f in flags):
            raise ConfigError("preset.build.trellis_configure: expected -DNAME=VALUE flags")
        result["trellis_configure"] = list(flags)
    return result
