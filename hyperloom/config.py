"""Strict, secret-free cluster configuration (all paths belong to their host)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Machine:
    id: str
    os: str
    access: str
    http_address: str
    ssh_target: str | None
    ssh_port: int
    install_root: str
    lab_mode: bool
    devices: list[dict]
    lab_dir: str | None = None
    peer_ssh: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Cluster:
    machines: dict[str, Machine]
    assets: dict
    profiles: dict
    router: dict
    pc: dict
    raw: dict


MAX_CONFIG_BYTES = 256 * 1024
PRESETS = Path(__file__).with_name("presets")


def record(value, where, required=(), optional=()):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{where}: expected object")
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ConfigError(f"{where}: missing keys {sorted(missing)}; unknown keys {sorted(extra)}")
    return value


def text(value, where):
    if not isinstance(value, str) or not value or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value):
        raise ConfigError(f"{where}: expected nonempty text without control characters")
    return value


def integer(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where}: expected integer {low}..{high}")
    return value


def number(value, where, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ConfigError(f"{where}: expected finite number {low}..{high}")


def boolean(value, where):
    if type(value) is not bool:
        raise ConfigError(f"{where}: expected boolean")
    return value


def identifier(value, where):
    text(value, where)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ConfigError(f"{where}: invalid identifier")
    return value


def absolute(value, where, os_name=None):
    text(value, where)
    valid = (PureWindowsPath(value).is_absolute() if os_name == "windows" else
             PurePosixPath(value).is_absolute() if os_name == "linux" else
             PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
    if not valid:
        raise ConfigError(f"{where}: expected absolute path")
    return value


def address(value, where):
    text(value, where)
    if "%" in value:
        raise ConfigError(f"{where}: scoped addresses are not supported")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: expected numeric private or loopback IP") from exc
    # Only native IPv6 ULA/loopback addresses are plainly private. In particular,
    # mapped/compatible addresses and reserved space vary across Python versions.
    native_v6 = ip.version != 6 or ip == ipaddress.IPv6Address("::1") or ip in ipaddress.IPv6Network("fc00::/7")
    if (not native_v6 or ip.is_unspecified or ip.is_multicast or ip.is_link_local
            or (ip.is_reserved and not ip.is_loopback) or not (ip.is_private or ip.is_loopback)):
        raise ConfigError(f"{where}: expected private or loopback IP")
    return str(ip)


def listen(value, where):
    text(value, where)
    try:
        host, port = value.rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not port.isascii() or not port.isdecimal():
            raise ValueError()
        return {"address": address(host, where), "port": integer(int(port), where, 1, 65535)}
    except ValueError as exc:
        raise ConfigError(f"{where}: expected IP:port (bracket IPv6)") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ConfigError(f"invalid JSON constant: {value}")


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigError("JSON exceeds 256 KiB")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ConfigError(f"cannot read JSON: {exc}") from exc


# Every launchable preset; the engine family of each (its "family" key, else its name) is in engines.FAMILIES.
PRESET_NAMES = ("qwen-replicas", "ds41-pair", "ds41-pair-long", "qwen-replicas-w5", "ds41-pair-w5", "minimax-pair")


def preset(name):
    if name not in PRESET_NAMES:
        raise ConfigError(f"unknown preset: {name}")
    return read_json(PRESETS / f"{name}.json")


def family(name):
    """The engine profile a preset launches (a variant such as ds41-pair-long runs as ds41-pair)."""
    return preset(name).get("family", name)


def validate_overrides(overrides, data):
    record(overrides, "overrides", optional=data["knobs"])
    for key, value in overrides.items():
        rule = data["knobs"][key]
        integer(value, f"overrides.{key}", rule["min"], rule["max"])
        if "values" in rule and value not in rule["values"]:
            raise ConfigError(f"overrides.{key}: value is not qualified by this preset")


def _curve(value, where):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ConfigError(f"{where}: expected 1..64 curve points")
    seen = set()
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ConfigError(f"{where}: expected [tokens, seconds]")
        integer(point[0], where, 0, 1 << 24)
        number(point[1], where, 0, 999999)
        if point[0] in seen:
            raise ConfigError(f"{where}: duplicate token counts")
        seen.add(point[0])


def _router(raw, machines):
    r = record(raw, "router", optional=("machine", "listen", "api_key_file", "replica_api_key_files",
               "policy", "balance_active_decodes", "max_inflight", "limits", "node_profile",
               "decode_s_per_token", "cache"))
    host = identifier(r.get("machine", "local"), "router.machine")
    if host != "local" and host not in machines:
        raise ConfigError("router.machine: unknown machine")
    if "listen" in r:
        listen(r["listen"], "router.listen")
    if "api_key_file" in r:
        absolute(r["api_key_file"], "router.api_key_file")
    keys = record(r.get("replica_api_key_files", {}), "router.replica_api_key_files", optional=machines)
    for value in keys.values():
        absolute(value, "router.replica_api_key_files")
    if r.get("policy", "nodes") not in ("nodes", "auto"):
        raise ConfigError("router.policy: expected nodes or auto")
    if "balance_active_decodes" in r:
        boolean(r["balance_active_decodes"], "router.balance_active_decodes")
    integer(r.get("max_inflight", 8), "router.max_inflight", 1, 64)
    limits = record(r.get("limits", {}), "router.limits", optional=("connect_timeout_s", "read_timeout_s",
                    "queue_timeout_s", "max_waiting", "max_connections", "max_request_bytes"))
    for key, value in limits.items():
        low, high = {"connect_timeout_s": (0.05, 120), "read_timeout_s": (1, 86400),
                     "queue_timeout_s": (0, 86400), "max_waiting": (0, 4096),
                     "max_connections": (1, 4096), "max_request_bytes": (1, 256 * 1024 * 1024)}[key]
        (number if key.endswith("_s") else integer)(value, f"router.limits.{key}", low, high)
    if "node_profile" in r:
        p = record(r["node_profile"], "router.node_profile", ("prefill_curve",), ("restore_s",))
        _curve(p["prefill_curve"], "router.node_profile.prefill_curve")
        number(p.get("restore_s", 0.01), "router.node_profile.restore_s", 0, 59.999)
    if "decode_s_per_token" in r:
        _curve(r["decode_s_per_token"], "router.decode_s_per_token")
    cache = record(r.get("cache", {}), "router.cache", optional=("block_tokens", "max_entries"))
    for key, value in cache.items():
        integer(value, f"router.cache.{key}", 16 if key == "block_tokens" else 1,
                (1 << 20) if key == "block_tokens" else (1 << 22))


def parse(obj: dict) -> Cluster:
    obj = deepcopy(obj)
    record(obj, "cluster", ("schema", "machines", "assets", "profiles"), ("router", "pc"))
    if obj["schema"] != "hyperloom/cluster-v1":
        raise ConfigError("cluster.schema: expected hyperloom/cluster-v1")
    rows = obj["machines"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ConfigError("machines: expected 1..64 machines")
    machines = {}
    local_count = device_count = 0
    for m in rows:
        record(m, "machine", ("id", "os", "access", "http_address", "install_root"),
               ("ssh_target", "ssh_port", "lab_mode", "devices", "lab_dir", "peer_ssh"))
        mid = identifier(m["id"], "machine.id")
        if mid in machines:
            raise ConfigError("duplicate machine id")
        if m["os"] not in ("linux", "windows") or m["access"] not in ("local", "ssh", "agent"):
            raise ConfigError("machine: expected linux/windows and local/ssh/agent")
        local_count += m["access"] == "local"
        target = m.get("ssh_target")
        if m["access"] == "ssh" and target is None:
            raise ConfigError("ssh machine requires ssh_target")
        if target is not None:
            text(target, "ssh_target")
            if target.startswith("-") or any(c.isspace() for c in target):
                raise ConfigError("ssh_target: no leading dash or whitespace")
        devices = m.get("devices", [])
        if not isinstance(devices, list):
            raise ConfigError("devices: expected list")
        ids = set()
        for device in devices:
            record(device, "device", ("id",), ("type", "uuid", "excluded"))
            did = identifier(device["id"], "device.id")
            if did in ids:
                raise ConfigError("duplicate device id")
            ids.add(did)
            for key in ("type", "uuid"):
                if key in device:
                    text(device[key], f"device.{key}")
            if "excluded" in device:
                boolean(device["excluded"], "device.excluded")
        device_count += len(devices)
        lab = m.get("lab_dir")
        if "lab_dir" in m:
            absolute(lab, "machine.lab_dir", "linux")
            if ".." in PurePosixPath(lab).parts or "\\" in lab:
                raise ConfigError("machine.lab_dir: expected absolute POSIX path without '..'")
        peers = m.get("peer_ssh", {})
        record(peers, "machine.peer_ssh", optional=peers if isinstance(peers, dict) else ())
        for peer, alias in peers.items():
            identifier(peer, "machine.peer_ssh machine id")
            text(alias, "machine.peer_ssh alias")
            if (alias.startswith("-") or not re.fullmatch(r"[\w.@:\[\]-]+", alias, flags=re.ASCII)
                    or alias.count("@") > 1):
                raise ConfigError("machine.peer_ssh: invalid SSH alias")
        machines[mid] = Machine(mid, m["os"], m["access"], address(m["http_address"], "http_address"),
                                target, integer(m.get("ssh_port", 22), "ssh_port", 1, 65535),
                                absolute(m["install_root"], "install_root", m["os"]),
                                boolean(m.get("lab_mode", True), "lab_mode"), devices, lab, peers)
    for machine in machines.values():
        if any(peer not in machines for peer in machine.peer_ssh):
            raise ConfigError("machine.peer_ssh: unknown machine id")
    if local_count > 1 or device_count > 128:
        raise ConfigError("cluster: at most one local machine and 128 devices")
    assets = record(obj["assets"], "assets", optional=machines)
    for mid, entries in assets.items():
        record(entries, f"assets.{mid}", optional=entries if isinstance(entries, dict) else ())
        for name, asset in entries.items():
            identifier(name, "asset name")
            record(asset, "asset", ("path", "bytes", "sha256"), ("control_path",))
            absolute(asset["path"], "asset.path", machines[mid].os)
            if "control_path" in asset:
                absolute(asset["control_path"], "asset.control_path")
            integer(asset["bytes"], "asset.bytes", 0, (1 << 63) - 1)
            if not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
                raise ConfigError("asset.sha256: expected lowercase SHA-256")
    profiles = record(obj["profiles"], "profiles", optional=obj["profiles"] if isinstance(obj["profiles"], dict) else ())
    for name, p in profiles.items():
        identifier(name, "profile name")
        record(p, "profile", optional=("preset", "nodes", "rank0", "rank1", "port", "receipt", "overrides", "api_key_files",
                                       "release_id", "prefill_service", "advanced"))
        kind = p.get("preset", name)
        data = preset(kind)
        from . import engines
        engine = engines.adapter(family(kind))
        engines.check_components(data, f"preset {kind}")
        if "receipt" in p and p["receipt"] != data["receipt"]:
            raise ConfigError("profile.receipt: does not match qualified preset")
        if "release_id" in p and (not isinstance(p["release_id"], str) or not re.fullmatch(r"[0-9a-f]{12}", p["release_id"])):
            raise ConfigError("profile.release_id: expected 12 lowercase hex characters")
        if engine.topology == "replicas":
            if "rank0" in p or "rank1" in p:
                raise ConfigError("replica profile uses nodes")
            mids = p.get("nodes")
            if not isinstance(mids, list) or not 1 <= len(mids) <= 64 or any(not isinstance(x, str) for x in mids):
                raise ConfigError("profile.nodes: expected 1..64 machine ids")
        else:
            if "nodes" in p or "rank0" not in p or "rank1" not in p:
                raise ConfigError("pair profile requires rank0 and rank1")
            mids = [p["rank0"], p["rank1"]]
            if any(not isinstance(x, str) for x in mids):
                raise ConfigError("pair ranks: expected machine ids")
        if len(set(mids)) != len(mids) or any(mid not in machines for mid in mids):
            raise ConfigError("profile: duplicate or unknown machine")
        if any(machines[mid].os != "linux" for mid in mids):
            raise ConfigError("Phase 0 engine presets require Linux workers")
        integer(p.get("port", data["port"]), "profile.port", 1, 65535)
        keys = record(p.get("api_key_files", {}), "profile.api_key_files", optional=mids if engine.key_file else ())
        for mid, value in keys.items():
            absolute(value, "profile.api_key_files", machines[mid].os)
        validate_overrides(p.get("overrides", {}), data)
        if "prefill_service" in p:
            from . import rtx
            service = rtx.parse_profile_service(p["prefill_service"], machines)
            if family(kind) not in service["decode_presets"]:
                raise ConfigError(f"profile.prefill_service: {service['name']} does not hand off to {kind}")
        from . import knobs
        knobs.validate(p.get("advanced", {}), data)
    pc = record(obj.get("pc", {}), "pc", optional=("router_listen", "ui_listen", "cuda"))
    for key in ("router_listen", "ui_listen"):
        if key in pc:
            endpoint = listen(pc[key], f"pc.{key}")
            if key == "ui_listen" and endpoint["address"] != "127.0.0.1":
                raise ConfigError("UI must listen on 127.0.0.1")
    if "cuda" in pc:
        cuda = record(pc["cuda"], "pc.cuda", optional=("pro_uuid", "reservation"))
        if "pro_uuid" in cuda:
            text(cuda["pro_uuid"], "pc.cuda.pro_uuid")
        if cuda.get("reservation") is not None:
            from . import rtx
            rtx.parse_reservation(cuda["reservation"])
    router = obj.get("router", {})
    _router(router, machines)
    return Cluster(machines, assets, profiles, router, pc, obj)


def load(path) -> Cluster:
    return parse(read_json(path))
