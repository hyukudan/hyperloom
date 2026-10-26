"""Phase 3a: a resident prefill service on the control machine's RTX PRO, owned like the local router.

The service preset (presets/rtx-prefill-*.json, schema hyperloom/prefill-service-v1) is model-agnostic data: the
binary and argv template, the decode presets whose import route it hands states to, its measured router profile, and
its fidelity oracle. A new model is a new preset file; nothing here names one.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import re
import subprocess
import sys
import threading
import time
import uuid

from . import placement as placements, procs, secrets, vramplan
from .config import ConfigError, family, identifier, integer, number, preset, read_json, record, text
from .paths import repo_root
from .winproc import no_window, retry_sharing

SCHEMA = "hyperloom/prefill-service-v1"
PRESETS = Path(__file__).with_name("presets")
GIB = 1 << 30
WATCH_S = 2.0
MEMORY_S = 0.5
PRO_IDLE_MIB = 2048  # display and driver use only; more means another job holds the PRO
MAX_WINDOW_S = 1200  # lab PC-BIG window unless main approves a longer one
SMI_TIMEOUT_S = 5
GATE_TIMEOUT_S = 60
PHYSICAL_FLOOR_GIB = 2.0  # below this much available RAM the PC is thrashing; the service stops before the desktop does
PHYSICAL_STRIKES = 4  # consecutive memory samples (0.5 s apart) under the floor
_SECRET = "<redacted>"


class ServiceError(RuntimeError):
    pass


def service_preset(name):
    if not isinstance(name, str) or not re.fullmatch(r"rtx-prefill-[a-z0-9-]{1,40}", name):
        raise ConfigError(f"unknown prefill service preset: {name}")
    path = PRESETS / f"{name}.json"
    if not path.is_file():
        raise ConfigError(f"unknown prefill service preset: {name}")
    data = read_json(path)
    if data.get("schema") != SCHEMA or data.get("name") != name:
        raise ConfigError(f"{name}: expected schema {SCHEMA}")
    for flag in data["secret_flags"]:
        if flag not in data["argv"]:
            raise ConfigError(f"{name}: secret flag {flag} not in argv")
    return data


def parse_reservation(value, where="pc.cuda.reservation"):
    r = record(value, where, ("owner", "approval", "vram_cap_gib", "host_gib", "free_commit_floor_gib"),
               ("extended_approval", "expires_at", "window_s", "gate"))
    text(r["owner"], f"{where}.owner")
    if "extended_approval" in r:
        text(r["extended_approval"], f"{where}.extended_approval")
    text(r["approval"], f"{where}.approval")
    # A fixed end (expires_at), or a window that opens when the service or build starts (window_s): a queued
    # pair turn can start hours after the operator granted the PC-BIG slot.
    if ("expires_at" in r) == ("window_s" in r):
        raise ConfigError(f"{where}: give exactly one of expires_at or window_s")
    if "expires_at" in r:
        expires(r["expires_at"], f"{where}.expires_at")
    else:
        integer(r["window_s"], f"{where}.window_s", 60,
                86400 if "extended_approval" in r else MAX_WINDOW_S)
    number(r["vram_cap_gib"], f"{where}.vram_cap_gib", 1, 1024)
    number(r["host_gib"], f"{where}.host_gib", 0, 4096)
    number(r["free_commit_floor_gib"], f"{where}.free_commit_floor_gib", 1, 4096)
    if "gate" in r:
        gate(r["gate"], f"{where}.gate")
    return r


def gate(value, where):
    """The operator's gate: a command (absolute executable + arguments) run right before the service touches the
    device. Exit 0 lets the start go on; anything else refuses it. Lets a shared PC keep its own device schedule
    (another job may hold the device while it is still loading, which no memory reading shows yet)."""
    if not isinstance(value, list) or not 1 <= len(value) <= 32 or not all(isinstance(a, str) for a in value):
        raise ConfigError(f"{where}: expected a list of 1..32 strings")
    for argument in value:
        if not argument or len(argument) > 4096 or "\0" in argument:
            raise ConfigError(f"{where}: empty, over-long or NUL argument")
    if not (PureWindowsPath(value[0]).is_absolute() or value[0].startswith("/")):
        raise ConfigError(f"{where}: the executable must be an absolute path")
    return value


def run_gate(argv, run=subprocess.run):
    """None when the gate lets the start go on, else why it refused (its first output line)."""
    try:
        result = run(argv, capture_output=True, timeout=GATE_TIMEOUT_S, stdin=subprocess.DEVNULL, **no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"gate did not run: {type(exc).__name__}"
    if result.returncode == 0:
        return None
    output = (result.stdout or b"") + (result.stderr or b"")
    first = output[:4096].decode("utf-8", "replace").strip().splitlines()
    return f"gate refused (rc {result.returncode}): {first[0][:200] if first else 'no output'}"


def expires(value, where):
    text(value, where)
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ConfigError(f"{where}: expected UTC time YYYY-MM-DDTHH:MM:SSZ") from exc
    return moment.timestamp()


def reservation_end(reservation, now):
    """End of the PC-BIG window: the fixed expires_at, or now + window_s when the window opens at start."""
    if "window_s" in reservation:
        return now + reservation["window_s"]
    return expires(reservation["expires_at"], "pc.cuda.reservation.expires_at")


def parse_profile_service(value, machines, where="profile.prefill_service"):
    """The profile's prefill service: which preset, on which local machine and NVIDIA device."""
    s = record(value, where, ("preset", "machine", "device"), ("port", "positions"))
    data = service_preset(s["preset"])
    machine = machines.get(identifier(s["machine"], f"{where}.machine"))
    if machine is None or machine.access != "local" or machine.os != "windows":
        raise ConfigError(f"{where}.machine: the service runs on the local Windows machine")
    device = next((d for d in machine.devices if d["id"] == s["device"]), None)
    if device is None or device.get("excluded", False) or device.get("type") != "nvidia" or "uuid" not in device:
        raise ConfigError(f"{where}.device: expected a non-excluded nvidia device with a uuid")
    if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", device["uuid"]):
        raise ConfigError(f"{where}.device: expected an NVIDIA GPU-<uuid>")
    integer(s.get("port", data["port"]), f"{where}.port", 1, 65535)
    if s.get("positions", data["positions"]["default"]) not in data["positions"]["values"]:
        raise ConfigError(f"{where}.positions: not qualified by {s['preset']}")
    return data


@dataclass(frozen=True)
class ServiceSpec:
    preset: str
    machine_id: str
    pro_uuid: str
    port: int
    argv: list[str]
    env: dict[str, str]
    redacted: list[str]
    deadline: float
    vram_cap_gib: float
    free_commit_floor_gib: float
    ready_timeout_s: int
    consistency: dict
    binary_sha256: str | None = None
    placement: dict | None = None
    gate: list | None = None
    placement_supervision: dict | None = None
    replica_spool: bool = False
    engine_sha: str | None = None


def redact(argv, flags):
    out = list(argv)
    for i, token in enumerate(out[:-1]):
        if token in flags:
            out[i + 1] = _SECRET
    return out


def _service(cluster, profile_name):
    profile = cluster.profiles[profile_name]
    if "prefill_service" not in profile:
        raise ConfigError(f"{profile_name}: no prefill_service")
    s = profile["prefill_service"]
    data = service_preset(s["preset"])
    kind = family(profile.get("preset", profile_name))
    if kind not in data["decode_presets"]:
        raise ConfigError(f"{s['preset']} does not hand off to {kind} profiles")
    if preset(profile.get("preset", profile_name))["engine_sha"] != data["engine_sha"]:
        # The handoff imports the service's state into the decode pair: one engine pin on both sides.
        raise ConfigError(f"{s['preset']} is qualified at engine {data['engine_sha'][:8]}, not at this profile's pin")
    return s, data, kind


def _decode_flag(cluster, profile_name, flag, default):
    """A decode server flag as it will launch: profile override, else the preset's argv, else the engine default."""
    profile = cluster.profiles[profile_name]
    if flag in profile.get("overrides", {}):
        return profile["overrides"][flag]
    argv = preset(profile.get("preset", profile_name))["argv"]
    return int(argv[argv.index(flag) + 1]) if flag in argv else default


def check_context(data, positions, decode_context, reserve_tokens):
    """An offloaded prompt must import (position + 1 < context) and leave its output reserve in the decode slot. The
    router sends no reserve (TensorMeld R9), so the decode server reserves its default max tokens."""
    longest = decode_context - reserve_tokens - 1
    if longest < 8192:
        raise ConfigError(f"decode context {decode_context} with reserve {reserve_tokens} offloads at most "
                          f"{longest} tokens; Phase 3a needs at least 8192")
    result = {"service_positions": positions, "decode_context": decode_context, "reserve_tokens": reserve_tokens,
              "longest_offload": min(positions, longest)}
    if positions > longest:
        # The router has no per-service length cap: a longer prompt is prefilled, its import is refused (413) and the
        # router falls back to a local prefill. Recorded here and in the docs until the router can bound it.
        result["note"] = f"prompts of {longest + 1}..{positions} tokens fail the import and fall back to local prefill"
    return result


def host_gib(data, reservation, free_physical_gib):
    rule = data["host_gib"]
    return max(0, min(rule["max"], math.floor(reservation["host_gib"]),
                      math.floor(free_physical_gib - rule["physical_reserve_gib"])))


def effective_cap(reservation, requested):
    """The VRAM cap of this start: `up --vram-cap-gib` lowers the operator's grant (or the 96 GiB default without a
    reservation), never raises it."""
    granted = reservation["vram_cap_gib"] if reservation else 96
    if requested is None:
        return granted
    number(requested, "--vram-cap-gib", 1, 1024)
    if requested > granted:
        where = "the reservation's" if reservation else "the default"
        raise ConfigError(f"--vram-cap-gib {requested:g} is above {where} {granted:g} GiB")
    return requested


# Service engines whose --host-gib is whole GiB (placement/ds41.py emits it as an integer).
WHOLE_GIB_HOST = ("ds41-rtx-prefill",)


def exact_host(plan, host):
    """A recipe's host budget reaches the engine exactly: the plan's --host-gib (its own lower estimate) is replaced
    by the budget the recipe set and admission charged."""
    if "--host-gib" in plan.flags:
        plan.flags["--host-gib"] = str(int(host)) if float(host).is_integer() else f"{host:g}"


def plan_placement(data, model_path, cap, host, positions):
    """The preset's placement engine planned for this cap (None when the preset has no placement block)."""
    rule = data.get("placement")
    if rule is None:
        return None
    inputs = vramplan.Inputs(cap, host_gib=host, **rule.get("margin", {}))
    try:
        plan = placements.plan(rule["engine"], model_path, inputs, positions=positions, **rule.get("params", {}))
    except (vramplan.PlanError, OSError, ValueError) as exc:
        raise ConfigError(f"{data['name']}: no placement at {cap:g} GiB: {exc}") from exc
    return plan


def expand_placement(data, plan):
    """The `{placement}` token: the plan's flags when the cap was given, else the preset's unplanned tokens (the
    launch line qualified before planning existed)."""
    out = []
    for token in data["argv"]:
        if token != "{placement}":
            out.append(token)
        elif "placement" not in data:
            raise ConfigError("argv has {placement} but the preset has no placement block")
        else:
            out += plan.argv() if plan is not None else list(data["placement"]["unplanned_argv"])
    return out


def render_service(cluster, profile_name, *, api_key, handoff_api_key, free_physical_gib, now=None, vram_cap_gib=None,
                   host_gib_exact=None, placement_request=None):
    if placement_request is not None:
        if vram_cap_gib is not None or host_gib_exact is not None:
            raise ConfigError("PL_E_CONFLICT placement cannot be combined with legacy sizing flags")
        return render_placement_service(cluster, placement_request, api_key=api_key)
    s, data, kind = _service(cluster, profile_name)
    decode = data["decode_profiles"][kind]
    rule = decode["context"]
    positions = s.get("positions", data["positions"]["default"])
    consistency = check_context(data, positions, _decode_flag(cluster, profile_name, rule["flag"], rule["default"]),
                                _decode_flag(cluster, profile_name, rule["reserve_flag"], rule["reserve_default"]))
    replicas = len(cluster.profiles[profile_name].get("nodes", [None]))
    if replicas > decode["max_decode_replicas"]:
        # One --handoff-api-key per service until the router maps decode keys per replica (TensorMeld R9).
        raise ConfigError(f"{data['name']} hands off to at most {decode['max_decode_replicas']} decode replica(s)")
    machine = cluster.machines[s["machine"]]
    device = next(d for d in machine.devices if d["id"] == s["device"])
    cuda = cluster.pc.get("cuda", {})
    if "pro_uuid" in cuda and cuda["pro_uuid"] != device["uuid"]:
        raise ConfigError("pc.cuda.pro_uuid differs from the profile's device uuid")
    reservation = cuda.get("reservation")
    now = time.time() if now is None else now
    if reservation is None:
        if machine.lab_mode:
            raise ConfigError("lab mode: the RTX service needs an operator-granted pc.cuda.reservation")
        deadline = math.inf
    else:
        deadline = reservation_end(reservation, now)
        if deadline <= now + 60:
            raise ConfigError("pc.cuda.reservation has expired (or ends within 60 s)")
        if deadline > now + MAX_WINDOW_S and "extended_approval" not in reservation:
            raise ConfigError("pc.cuda.reservation: PC-BIG windows are at most 20 min without an extended_approval")
    for value in (api_key, handoff_api_key):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ConfigError("service keys must be generated 32-byte hex keys")
    assets = cluster.assets.get(s["machine"], {})
    missing = set(data["assets"]) - assets.keys()
    if missing:
        raise ConfigError(f"{s['machine']}: missing assets {sorted(missing)}")
    for name in data["assets"]:
        # Size on every start, like the nodes' assets (a full hash is the assets job's, outside measurement windows).
        if assets[name]["bytes"]:
            try:
                size = os.stat(assets[name]["path"]).st_size
            except OSError as exc:
                raise ConfigError(f"{s['machine']}: asset {name} unavailable: {exc}") from exc
            if size != assets[name]["bytes"]:
                raise ConfigError(f"{s['machine']}: asset {name} is {size} bytes, the manifest says {assets[name]['bytes']}")
    binary = PureWindowsPath(machine.install_root) / "cuda" / data["engine_sha"][:12] / "bin" / (data["binary"] + ".exe")
    floor = reservation["free_commit_floor_gib"] if reservation else 4
    cap = effective_cap(reservation, vram_cap_gib)
    host = host_gib(data, reservation or {"host_gib": 4096}, free_physical_gib)
    if host_gib_exact is not None:
        # A recipe's pinned host budget is used exactly or refused, never clamped (recipes design 9.7).
        if host_gib_exact > host:
            raise ConfigError(f"recipe host_pinned_gib {host_gib_exact:g} is above what this start allows ({host:g} "
                              "GiB: the preset's maximum, the reservation and free RAM)")
        host = host_gib_exact
    values = {name: item["path"] for name, item in assets.items()}
    # Planned only when the operator asks for a cap (`up --vram-cap-gib`), on every such start; without it the
    # qualified launch line is unchanged (lead decision, review of d5c3e0f).
    plan = None
    if vram_cap_gib is not None:
        plan = plan_placement(data, values.get(data.get("placement", {}).get("model_asset")), cap, host, positions)
        if plan is not None and host_gib_exact is not None:
            exact_host(plan, host_gib_exact)
    values.update(binary=str(binary), port=str(s.get("port", data["port"])),
                  positions=str(positions),
                  vram_cap_gib=f"{cap:g}", host_gib=f"{host:g}",
                  commit_floor_gib=f"{floor + 10:g}", api_key=api_key, handoff_api_key=handoff_api_key,
                  pro_uuid=device["uuid"])
    argv = [token.format_map(values) for token in expand_placement(data, plan)]
    env = dict(child_env(os.environ), **{k: v.format_map(values) for k, v in data["env"].items()})
    manifest = Path(binary).parent.parent / "MANIFEST.json"
    sha = None
    if manifest.is_file():
        sha = read_json(manifest).get("binaries", {}).get(binary.name)
    return ServiceSpec(data["name"], s["machine"], device["uuid"], int(values["port"]), argv, env,
                       redact(argv, data["secret_flags"]), deadline, cap, floor, data["ready"]["timeout_s"], consistency,
                       sha, None if plan is None else dict(plan.to_dict(), explain=vramplan.explain(plan)),
                       (reservation or {}).get("gate"))


def render_placement_service(cluster, request, *, api_key):
    """Opt-in RTX-alone decode renderer. The shipped unpublished pin refuses before rendering any argv."""
    from .recipes import runtime
    from .recipes.grants import uuid_key
    data = runtime.preset()
    runtime.qualified(data)
    machine = cluster.machines[request.machine]
    if machine.access != "local" or machine.os != "windows":
        raise ConfigError("PL_E_CAPABILITY placement decode adapter requires the local Windows machine")
    if not re.fullmatch(r"[0-9a-f]{64}", api_key):
        raise ConfigError("placement service key must be a generated 32-byte hex key")
    if any(a.split("=", 1)[0] in {"--serve", "--serve-idle", "--serve-max", "--host", "--port", "--api-key"}
           for a in request.argv):
        raise ConfigError("PL_E_CONFLICT replica spool lifecycle belongs to the helper; HTTP options are unsupported")
    binary = PureWindowsPath(machine.install_root) / "cuda" / data["engine_sha"][:12] / "bin" / (data["binary"] + ".exe")
    argv, explicit, document = runtime.render(request.files, binary=binary,
        argv=[*request.argv, "--serve", "{replica_spool}"], data=data)
    policy = request.supervision
    touched = {uuid_key(d["uuid"]) for d in document["devices"]}
    if touched != set(policy.get("caps_gib", {})):
        raise ConfigError("PL_E_GRANT supervision must cover exactly the placement's touched UUIDs")
    granted = {uuid_key(g["uuid"]): g for g in policy.get("grants", [])}
    if touched != granted.keys():
        raise ConfigError("PL_E_GRANT every touched device needs an explicit grant")
    from . import placement_supervision
    placement_supervision.validate(policy, now=time.time(), starting=False)
    windows = [g["window"] for g in granted.values()]
    if policy.get("host_grant") is not None:
        windows.append(policy["host_grant"]["window"])
    deadline = min(expires(w["expires_at"], "placement grant expires_at") for w in windows)
    if policy.get("pc_big_deadline") is not None:
        deadline = min(deadline, policy["pc_big_deadline"])
    anchor = next((d for d in document["devices"] if d.get("role") == "anchor"), None)
    if anchor is None:
        raise ConfigError("PL_E_SCHEMA placement has no anchor")
    budgets = {uuid_key(d["uuid"]): d.get("budget_bytes") for d in document["devices"]}
    if any(type(b) is not int or b <= 0 for b in budgets.values()):
        raise ConfigError("PL_E_SCHEMA placement requires integer device budgets")
    if "caps_bytes" not in policy:
        policy["caps_bytes"] = budgets
    for device in document["devices"]:
        if device.get("budget_bytes") != policy["caps_bytes"].get(uuid_key(device["uuid"])):
            raise ConfigError("PL_E_GRANT supervised cap differs from placement budget")
    manifest = Path(binary).parent.parent / "MANIFEST.json"
    sha = read_json(manifest).get("binaries", {}).get(binary.name) if manifest.is_file() else None
    return ServiceSpec(data["name"], request.machine, anchor["uuid"], request.port, argv,
                       dict(child_env(os.environ), **explicit), redact(argv, data["secret_flags"]),
                       deadline, policy["caps_gib"][uuid_key(anchor["uuid"])],
                       policy.get("free_commit_floor_gib", 4), data["ready_timeout_s"], {},
                       sha, document, placement_supervision=policy, replica_spool=True, engine_sha=data["engine_sha"])


# Device selection and behaviour switches of the CUDA runtime are never inherited (the preset sets its own);
# toolkit paths such as CUDA_PATH are kept.
_ENV_DENY = {"CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "CUDA_LAUNCH_BLOCKING", "CUDA_MODULE_LOADING",
             "CUDA_DEVICE_MAX_CONNECTIONS", "CUDA_FORCE_PTX_JIT", "CUDA_DISABLE_PTX_JIT", "CUDA_AUTO_BOOST",
             "CUDA_ENABLE_COREDUMP_ON_EXCEPTION", "NVIDIA_VISIBLE_DEVICES", "NVIDIA_TF32_OVERRIDE"}
_ENV_DENY_PREFIX = ("PYTHON", "CUDA_MPS_", "CUDA_CACHE_", "CUDA_COREDUMP", "STRIX_")


def child_env(environ):
    return {k: v for k, v in environ.items()
            if k.upper() not in _ENV_DENY and not k.upper().startswith(_ENV_DENY_PREFIX)}


def router_service(cluster, profile_name, key_file):
    """The TensorMeld prefill.services entry (the router must run on the same machine: loopback only)."""
    s, data, _ = _service(cluster, profile_name)
    return {"id": "prefill-" + s["device"], "node": s["machine"], "address": "127.0.0.1",
            "port": s.get("port", data["port"]), "max_inflight": 1, "api_key_file": key_file,
            "profile": dict(data["profile"])}


def router_prefill(cluster, profile_name, key_file):
    """policy auto with the measured service and decode profiles."""
    _, data, kind = _service(cluster, profile_name)
    decode = data["decode_profiles"][kind]
    return {"policy": "auto", "services": [router_service(cluster, profile_name, key_file)],
            "node_profile": decode["node_profile"], "decode_s_per_token": decode["decode_s_per_token"],
            "handoff_path": data["handoff_path"]}


# --- device checks (nvidia-smi, passive queries) -------------------------------------------------------------

def _smi(args, run=subprocess.run):
    try:
        out = run(["nvidia-smi", *args], capture_output=True, text=True, timeout=SMI_TIMEOUT_S, **no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"nvidia-smi unavailable: {exc}") from exc
    if out.returncode != 0:
        raise ServiceError("nvidia-smi failed: " + (out.stderr or out.stdout).strip()[:300])
    return [[cell.strip() for cell in line.split(",")] for line in out.stdout.splitlines() if line.strip()]


def verify_pro(pro_uuid, run=subprocess.run, require_idle=True):
    """The UUID must resolve to exactly one RTX PRO 6000 before anything starts on it."""
    rows = _smi(["--query-gpu=uuid,name,pci.bus_id,memory.total", "--format=csv,noheader,nounits", "-i", pro_uuid], run)
    if len(rows) != 1 or rows[0][0] != pro_uuid or "RTX PRO 6000" not in rows[0][1]:
        raise ServiceError(f"{pro_uuid} is not one RTX PRO 6000: {rows}")
    # One PC-BIG at a time across all users of the PRO: refuse a busy device rather than share it.
    busy = pro_busy(pro_uuid, run)
    if busy and require_idle:
        raise ServiceError("RTX PRO busy: " + busy)
    return {"uuid": rows[0][0], "name": rows[0][1], "pci_bus_id": rows[0][2], "memory_total_mib": int(rows[0][3]),
            "busy": busy}


def verify_device(device_uuid, run=subprocess.run):
    """Placement identity/idle check: any explicitly granted CUDA device, without a model-name restriction."""
    rows = _smi(["--query-gpu=uuid,name,pci.bus_id,memory.total", "--format=csv,noheader,nounits", "-i", device_uuid], run)
    if len(rows) != 1 or rows[0][0].lower() != device_uuid.lower() or not rows[0][3].isdigit():
        raise ServiceError("placement device identity or physical capacity unavailable")
    busy = pro_busy(rows[0][0], run)
    if busy:
        raise ServiceError("placement device busy: " + busy)
    return {"uuid": rows[0][0], "name": rows[0][1], "pci_bus_id": rows[0][2],
            "memory_total_mib": int(rows[0][3]), "busy": None}


def pro_busy(pro_uuid, run=subprocess.run):
    """None when the PRO is idle, else what holds it. Under WDDM the driver lists desktop processes (a GPU-accelerated
    UI, a tray app) on every GPU with memory [N/A]; those alone are not a job. A listed process with real memory above
    idle is, and so is device memory above idle (a real job under WDDM shows up there even when its row is [N/A])."""
    others = [pid for gpu, pid, used in compute_apps(run) if gpu == pro_uuid and used is not None and used > PRO_IDLE_MIB]
    if others:
        return f"compute processes {sorted(others)}"
    used = device_used_mib(pro_uuid, run)
    if used > PRO_IDLE_MIB:
        return f"{used} MiB in use (idle is <= {PRO_IDLE_MIB} MiB)"
    return None


def compute_apps(run=subprocess.run):
    """(gpu uuid, pid, used MiB or None) for every compute process on every NVIDIA GPU."""
    rows = _smi(["--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"], run)
    result = []
    for row in rows:
        if len(row) == 3 and row[1].isdigit():
            result.append((row[0], int(row[1]), int(row[2]) if row[2].isdigit() else None))
    return result


def device_used_mib(pro_uuid, run=subprocess.run):
    rows = _smi(["--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", pro_uuid], run)
    return int(rows[0][0])


def memory_status():
    """Free physical memory and free commit, in GiB."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Status(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
                        ("total_page", ctypes.c_uint64), ("avail_page", ctypes.c_uint64),
                        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
                        ("avail_extended", ctypes.c_uint64)]
        status = Status()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError()
        return {"free_physical_gib": status.avail_phys / GIB, "free_commit_gib": status.avail_page / GIB}
    info = {}
    with open("/proc/meminfo", encoding="ascii") as stream:
        for line in stream:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
    commit = info["CommitLimit"] - info["Committed_AS"]
    return {"free_physical_gib": info["MemAvailable"] / GIB, "free_commit_gib": commit / GIB}


class Watchdog:
    """The lab PC-BIG policy for one PID. Memory every 0.5 s, the GPU every 2 s: kill the process below the free-commit
    floor or the available-RAM floor, above the VRAM cap, on any GPU other than the PRO, or at the deadline. It fails closed: an error inside the
    watchdog kills the process too. The rest of the PC keeps working."""

    def __init__(self, spec, pid, kill, log, *, memory=memory_status, apps=compute_apps, used=device_used_mib,
                 clock=time.time, interval=WATCH_S, memory_interval=MEMORY_S, gpu=True):
        self.spec, self.pid, self.kill, self.log = spec, pid, kill, log
        self.memory, self.apps, self.used, self.clock = memory, apps, used, clock
        self.interval, self.memory_interval, self.gpu = interval, memory_interval, gpu
        self.reason = None
        self.baseline_mib = None
        self.smi_failures = 0
        self.physical_strikes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.gpu:
            try:
                self.baseline_mib = self.used(self.spec.pro_uuid)
            except ServiceError:
                self.baseline_mib = None
        self._thread.start()
        return self

    def alive(self):
        return self._thread.is_alive()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10)

    def check_memory(self):
        memory = self.memory()
        if memory["free_commit_gib"] < self.spec.free_commit_floor_gib:
            return f"free commit {memory['free_commit_gib']:.1f} GiB below {self.spec.free_commit_floor_gib:g}", memory
        self.physical_strikes = self.physical_strikes + 1 if memory["free_physical_gib"] < PHYSICAL_FLOOR_GIB else 0
        if self.physical_strikes >= PHYSICAL_STRIKES:
            return (f"available RAM {memory['free_physical_gib']:.1f} GiB below {PHYSICAL_FLOOR_GIB:g} "
                    f"for {self.physical_strikes} samples"), memory
        if self.clock() >= self.spec.deadline:
            return "deadline", memory
        return None, memory

    def check(self):
        """One full sample (memory and GPU); returns the kill reason or None."""
        reason, memory = self.check_memory()
        line = {"t": round(self.clock(), 1), "pid": self.pid, **{k: round(v, 2) for k, v in memory.items()}}
        if reason is None and self.gpu:
            try:
                mine = [(gpu, used) for gpu, pid, used in self.apps() if pid == self.pid]
                other = sorted({gpu for gpu, _ in mine if gpu != self.spec.pro_uuid})
                if other:
                    reason = "process visible on another GPU: " + ",".join(other)
                own = [used for gpu, used in mine if gpu == self.spec.pro_uuid]
                if own and own[0] is not None:
                    line["vram_mib"] = own[0]
                elif self.baseline_mib is not None:
                    line["vram_mib"] = max(0, self.used(self.spec.pro_uuid) - self.baseline_mib)
                elif reason is None:
                    reason = "VRAM use cannot be measured (no per-process value and no device baseline)"
                if reason is None and line.get("vram_mib", 0) > self.spec.vram_cap_gib * 1024:
                    reason = f"VRAM {line['vram_mib']} MiB above the {self.spec.vram_cap_gib:g} GiB cap"
                self.smi_failures = 0
            except ServiceError as exc:
                # A transient nvidia-smi failure is logged; a persistent one would silently disable the GPU checks.
                self.smi_failures += 1
                line["smi"] = str(exc)[:200]
                if self.smi_failures >= SMI_FAILURES_MAX:
                    reason = f"nvidia-smi failed {self.smi_failures} times in a row: GPU checks unavailable"
        if reason:
            line["kill"] = reason
        self.log(line)
        return reason

    def _run(self):
        next_full = 0.0
        while not self._stop.is_set():
            try:
                if time.monotonic() >= next_full:
                    reason = self.check()
                    next_full = time.monotonic() + self.interval
                else:
                    reason = self.check_memory()[0]
            except Exception as exc:  # noqa: BLE001 - fail closed
                reason = f"watchdog error: {type(exc).__name__}: {exc}"
            if reason:
                self.reason = reason
                try:
                    self.log({"t": round(self.clock(), 1), "pid": self.pid, "kill": reason})
                except Exception:  # noqa: BLE001 - the kill matters more than its log line
                    pass
                try:
                    self.kill()
                except Exception:  # noqa: BLE001 - the helper sees self.reason and stops the Job anyway
                    pass
                return
            self._stop.wait(self.memory_interval)


# --- the owned service (a detached CLI helper holds the Job Object, like the local router) ------------------

SMI_FAILURES_MAX = 3


def _scrub(event, secrets):
    """Remove secret values (keys from the argv) from an engine event before it is stored in state."""
    text = json.dumps(event)
    for secret in secrets:
        if secret:
            text = text.replace(json.dumps(secret)[1:-1], _SECRET)
    return json.loads(text)


_retry = retry_sharing  # Windows: a reader holding state.json briefly makes replace/read fail


def _write(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    try:
        _retry(lambda: os.replace(tmp, path))
    finally:
        tmp.unlink(missing_ok=True)


def _read(path):
    return _retry(lambda: json.loads(Path(path).read_text(encoding="utf-8")))


def _ready_event(path):
    try:
        with open(path, "rb") as stream:
            for line in stream:
                if line.startswith(b"{") and b'"event"' in line:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "READY":
                        return event
    except OSError:
        pass
    return None


def _health(port):
    from urllib.request import ProxyHandler, build_opener
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        return json.loads(response.read(65536))


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def device_lock(pro_uuid):
    """One heavy PC job (service or CUDA build) per device at a time, across every profile on this machine."""
    return procs.InstanceLock(device_lock_path(pro_uuid))


def device_lock_path(pro_uuid):
    return secrets.runtime_dir() / f"pcbig-{re.sub(r'[^A-Za-z0-9-]', '', pro_uuid)}.lock"


def _owner_alive(owner):
    """The controller's ownership document still exists and still names this service (B1: a controller that died
    before recording the service must not leave it running)."""
    if not owner:
        return True
    try:
        state = _read(owner["state_path"])
    except (OSError, ValueError):
        return False
    return state.get("token") == owner["token"]


HEALTH_STRIKES = 3
HEALTH_INTERVAL_S = 10
OWNER_INTERVAL_S = 2


def service_run(spec_path, *, child_factory=procs.LocalChild, watchdog_factory=Watchdog, verify=verify_pro,
                health=_health, lock_factory=device_lock, owner_check=_owner_alive, gate_runner=run_gate,
                memory=memory_status, observer_factory=None):
    """Persistent helper: owns the CUDA child (Job Object), its watchdog and the plaintext keys in its spec.
    Its lifetime lock (helper.lock) tells stop_service whether it is alive; every start step re-checks the stop file."""
    path = Path(spec_path)
    directory = path.parent
    lifetime = procs.InstanceLock(directory / "helper.lock").acquire()
    child = watchdog = device = spool = events = None
    spec = None
    devices = ExitStack()
    observer = None
    reason = "stopped"
    redacted = []

    def log(line):
        events.write(json.dumps(line) + "\n")
        events.flush()

    def stopping():
        return (directory / "stop").exists() or not owner_check(owner)

    errors, failed = [], []

    def cleanup_step(name, action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
            failed.append(name)
            # Error text may contain keys from a failed child command; record only the step and type.
            line = {"cleanup": name, "error": type(exc).__name__}
            try:
                log(line)
            except BaseException:
                try:
                    print(json.dumps(line), file=sys.stderr, flush=True)
                except BaseException:
                    pass  # Neither log destination may interrupt containment or lock release.

    owner = None
    try:
        events = (directory / "watchdog.jsonl").open("a", encoding="utf-8")
        raw = _read(path)
        path.unlink()  # the only copy of the literal keys on disk, now in memory only
        spec = ServiceSpec(**raw["spec"])
        if spec.replica_spool:
            from .replica_spool import Client
            # A new helper owns a new spool: a previous worker's ready.json can never admit this start.
            spec = ServiceSpec(**dict(spec.__dict__, argv=[str(directory / "spool") if a == "{replica_spool}"
                                                          else a for a in spec.argv]))
        owner, redacted = raw.get("owner"), spec.redacted
        if stopping():
            reason = "stop requested before start"
            return
        if spec.placement_supervision is not None:
            from . import gpu_counters, placement_supervision
            placement_supervision.validate(spec.placement_supervision, now=time.time(), starting=True, memory=memory())
            info = placement_supervision.acquire(spec.placement_supervision, devices, lock_factory=lock_factory,
                                                  gate_runner=gate_runner,
                                                  verify=verify_device if verify is verify_pro else verify,
                                                  log=log, now=time.time(),
                                                  release=lambda lock: cleanup_step("device", lock.close))
            # Availability is tested before any engine is started. Counters may enumerate GPUs but never run work.
            observer = (observer_factory or gpu_counters.Observer)()
            sample = observer.sample(os.getpid())
            _, caps = placement_supervision.validate(spec.placement_supervision, now=time.time(), starting=True)
            if not caps.keys() <= sample.keys():
                raise ServiceError("granted GPU missing from attributable counters")
            if spec.pro_uuid.lower() not in caps:
                raise ServiceError("placement anchor has no grant")
            for item in info:
                cap_bytes = spec.placement_supervision.get("caps_bytes", {}).get(item["uuid"].lower())
                if (item.get("memory_total_mib") is not None
                        and cap_bytes is not None and cap_bytes > item["memory_total_mib"] * (1 << 20)):
                    raise ServiceError("placement cap exceeds device physical capacity")
            # Gate commands can take time; recheck every deadline and the physical/commit floors before spawn.
            placement_supervision.validate(spec.placement_supervision, now=time.time(), starting=True, memory=memory())
        else:
            device = lock_factory(spec.pro_uuid).acquire()
            if spec.gate:
                refused = gate_runner(spec.gate)
                log({"t": round(time.time(), 1), "gate": refused or "open"})
                if refused:
                    raise ServiceError(refused)
            info = verify(spec.pro_uuid)
        if not spec.binary_sha256:
            raise ServiceError("service binary has no release manifest (hyperloom prefill build)")
        if _sha256(spec.argv[0]) != spec.binary_sha256:
            raise ServiceError("service binary does not match its release manifest")
        _write(directory / "state.json", {"state": "loading", "device": info, "argv": redacted})
        if stopping():
            reason = "stop requested before start"
            return
        child = child_factory(spec.argv, spec.env, directory, "service", port=("127.0.0.1", spec.port))
        child.start()
        if spec.replica_spool:
            spool = Client(directory / "spool", alive=child.alive)
        # The watchdog only terminates the process; this loop sees it and does the orderly stop below.
        if spec.placement_supervision is not None:
            watchdog = placement_supervision.Watchdog(spec, child.process.pid, child.process.kill, log,
                                                       observer=observer, memory=memory)
            observer = None  # watchdog now owns the observer, including cleanup on start failure
            watchdog.start()
        else:
            watchdog = watchdog_factory(spec, child.process.pid, child.process.kill, log).start()
        end = time.monotonic() + spec.ready_timeout_s
        ready, strikes, next_health, next_owner = None, 0, 0.0, 0.0
        while True:
            now = time.monotonic()
            if watchdog.reason:
                reason = "watchdog: " + watchdog.reason
                break
            if not watchdog.alive():
                reason = "watchdog stopped unexpectedly"
                break
            if not child.alive():
                reason = f"service exited ({child.process.returncode})"
                break
            if (directory / "stop").exists():
                break
            if now >= next_owner:
                if not owner_check(owner):
                    reason = "controller ownership gone"
                    break
                next_owner = now + OWNER_INTERVAL_S
            if time.time() >= spec.deadline:
                reason = "deadline"
                break
            if ready is None:
                if now >= end:
                    reason = "readiness timed out"
                    break
                event = spool.ready() if spool is not None else _ready_event(directory / "service.stdout.log")
                if event is not None:
                    try:
                        ok = spool.health() if spool is not None else health(spec.port).get("status") == "ok"
                        if ok:
                            ready = _scrub(event, [a for a, b in zip(spec.argv, redacted) if a != b])
                            _write(directory / "state.json", {"state": "ready", "pid": child.process.pid,
                                                              "device": info, "ready": ready, "argv": redacted})
                            next_health = now + HEALTH_INTERVAL_S
                    except (OSError, ValueError):
                        pass
            elif now >= next_health:
                try:
                    ok = spool.health() if spool is not None else health(spec.port).get("status") == "ok"
                except (OSError, ValueError):
                    ok = False
                strikes = 0 if ok else strikes + 1
                if strikes >= HEALTH_STRIKES:
                    reason = "health failed"
                    break
                next_health = now + HEALTH_INTERVAL_S
            time.sleep(0.2)
    except Exception as exc:  # noqa: BLE001 - recorded for the controller
        reason = f"{type(exc).__name__}: {exc}"
    finally:
        def shutdown_spool():
            if spool is not None and child is not None and child.alive():
                spool.shutdown(timeout=5)

        def stopped():
            state = {"state": "stopped", "reason": reason, "argv": redacted}
            if failed:
                state["cleanup_errors"] = list(failed)
            _write(directory / "state.json", state)

        # LIFO: the lifetime lock is released only after every other cleanup has been attempted. ExitStack also
        # unwinds every per-device lock if another close fails. Guard each callback so logging and error propagation
        # cannot skip child termination, terminal state, or any remaining release.
        with ExitStack() as cleanup:
            cleanup.callback(cleanup_step, "lifetime", lifetime.close)
            cleanup.callback(cleanup_step, "devices", devices.close)
            if device is not None:
                cleanup.callback(cleanup_step, "device", device.close)
            cleanup.callback(cleanup_step, "state", stopped)
            if events is not None:
                cleanup.callback(cleanup_step, "events", events.close)
            if observer is not None:
                cleanup.callback(cleanup_step, "observer", observer.close)
            if child is not None:
                cleanup.callback(cleanup_step, "child", lambda: child.stop(10))
                cleanup.callback(cleanup_step, "spool", shutdown_spool)
            if watchdog is not None:
                cleanup.callback(cleanup_step, "watchdog", watchdog.stop)
            cleanup.callback(cleanup_step, "spec", lambda: path.unlink(missing_ok=True))
        if errors:
            if spec is None or spec.placement_supervision is None:
                raise errors[0]
            raise BaseExceptionGroup("service cleanup failed", errors)


def prepare_service(spec):
    """The helper's directory and the record the controller persists BEFORE anything is launched (B1)."""
    directory = secrets.runtime_dir() / ("prefill-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {"machine": spec.machine_id, "directory": str(directory), "port": spec.port, "preset": spec.preset,
            "device": spec.pro_uuid, "argv": spec.redacted, "vram_cap_gib": spec.vram_cap_gib,
            "placement": spec.placement}
    if spec.placement_supervision is not None:
        record["placement_supervision"] = spec.placement_supervision
    if spec.replica_spool:
        record["spool"] = str(directory / "spool")
        record["engine_sha"] = spec.engine_sha
    return record


def launch_service(record, spec, *, detached, owner=None, timeout_s=None, check=lambda: None):
    """Write the helper's private spec, start the helper and wait for the service's readiness. `owner` names the
    controller's state file and token: the helper stops by itself when they disappear."""
    directory = Path(record["directory"])
    spec_path = directory / "spec.json"
    secrets._write(spec_path, json.dumps({"spec": dict(spec.__dict__), "owner": owner}).encode("utf-8"))
    try:
        helper = detached([str(Path(sys.executable).absolute()), "-m", "hyperloom", "_prefill-run", str(spec_path)],
                          directory / "controller.log")
        end = time.monotonic() + (timeout_s or spec.ready_timeout_s + 30)
        while time.monotonic() < end:
            check()
            state_path = directory / "state.json"
            if state_path.exists():
                state = _read(state_path)
                if state["state"] == "ready":
                    record.update(pid=state["pid"], ready=state["ready"])
                    return record
                if state["state"] == "stopped":
                    raise ServiceError("prefill service stopped: " + state.get("reason", "?"))
            if helper.poll() is not None:
                raise ServiceError("prefill helper exited; see " + str(directory / "controller.log"))
            time.sleep(0.2)
        raise ServiceError("prefill service readiness timed out")
    except BaseException:
        stop_service(record)
        raise
    finally:
        spec_path.unlink(missing_ok=True)


def start_service(spec, *, detached, owner=None, timeout_s=None, check=lambda: None):
    return launch_service(prepare_service(spec), spec, detached=detached, owner=owner, timeout_s=timeout_s, check=check)


def stop_service(record, timeout_s=60):
    """Ask the helper to stop and wait for it. A helper that is not running (never started, or dead: its Job Object
    already ended the service) cannot start later either: it checks the stop file after taking its lifetime lock."""
    directory = Path(record["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop").touch()
    state_path = directory / "state.json"
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        lock = procs.InstanceLock(directory / "helper.lock")
        try:
            lock.acquire()
        except procs.ProcessError:
            time.sleep(0.2)
            continue
        try:
            (directory / "spec.json").unlink(missing_ok=True)
            state = _read(state_path) if state_path.exists() else {}
            if state.get("state") != "stopped":
                state = {"state": "stopped", "reason": "helper gone"}
                _write(state_path, state)
            return state
        finally:
            lock.close()
    raise ServiceError("prefill service did not confirm stop")


# --- operator commands: preview, device check, CUDA build, fidelity gate, placement check --------------------

def render_preview(cluster, profile_name, vram_cap_gib=None, *, placement_request=None):
    """The service line as it would start now (keys redacted), with the context consistency it was checked for and
    the placement planned for the cap."""
    spec = render_service(cluster, profile_name, api_key="0" * 64, handoff_api_key="0" * 64,
                          free_physical_gib=(0 if placement_request is not None else memory_status()["free_physical_gib"]),
                          vram_cap_gib=vram_cap_gib, placement_request=placement_request)
    return {"argv": spec.redacted, "cuda_env": {k: spec.env[k] for k in ("CUDA_DEVICE_ORDER", "CUDA_VISIBLE_DEVICES")},
            "deadline": spec.deadline if math.isfinite(spec.deadline) else None, "consistency": spec.consistency,
            "binary_sha256": spec.binary_sha256, "vram_cap_gib": spec.vram_cap_gib, "placement": spec.placement}


def _release_dir(cluster, profile_name):
    s, data, _ = _service(cluster, profile_name)
    return Path(cluster.machines[s["machine"]].install_root) / "cuda" / data["engine_sha"][:12], data


BUILD_MAX_S = 4 * 3600


def build(cluster, profile_name, source, *, child_factory=procs.LocalChild, watchdog_factory=Watchdog,
          lock_factory=device_lock, poll_s=1.0, gate_runner=run_gate):
    """CUDA build: heavy PC work (SPEC 5.7). In lab mode only inside a current reservation, under its free-commit
    watchdog and deadline, in a Job Object (cmake, ninja, cl and nvcc die with it). Builds into a staging directory
    and publishes it only when the release directory does not exist yet: a release is never rebuilt in place."""
    s, _, _ = _service(cluster, profile_name)
    machine = cluster.machines[s["machine"]]
    device = next(d for d in machine.devices if d["id"] == s["device"])
    reservation = cluster.pc.get("cuda", {}).get("reservation")
    deadline = time.time() + BUILD_MAX_S
    if reservation is not None:
        deadline = min(deadline, reservation_end(reservation, time.time()))
    if machine.lab_mode and (reservation is None or deadline <= time.time() + 60):
        raise ServiceError("lab mode: the CUDA build needs a current pc.cuda.reservation")
    if source is None:
        raise ServiceError("build needs --source <engine checkout at the pinned commit>")
    output, data = _release_dir(cluster, profile_name)
    if output.exists():
        raise ServiceError(f"{output} already exists; a release is never rebuilt in place")
    staging = output.with_name(output.name + ".staging-" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True)
    script = repo_root() / "windows" / "build-cuda-front.ps1"
    argv = [str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Source", str(source), "-Output", str(staging), "-Commit", data["engine_sha"],
            "-ConfigureJson", json.dumps(data["build"]["configure"]), "-TargetsJson", json.dumps(data["build"]["targets"])]
    floor = reservation["free_commit_floor_gib"] if reservation else 4
    limits = SimpleNamespace(pro_uuid=device["uuid"], free_commit_floor_gib=floor, deadline=deadline, vram_cap_gib=0)
    lock = lock_factory(device["uuid"]).acquire()
    child = dog = None
    try:
        refused = gate_runner(reservation["gate"]) if reservation and reservation.get("gate") else None
        if refused:
            raise ServiceError(refused)
        # Windows PowerShell 5.1 must not inherit another PowerShell's module path (its own cmdlets then fail to load).
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
        child = child_factory(argv, env, staging, "build")
        child.start()
        with (staging / "watchdog.jsonl").open("a", encoding="utf-8") as events:
            dog = watchdog_factory(limits, child.process.pid, child.process.kill,
                                   lambda line: (events.write(json.dumps(line) + "\n"), events.flush()), gpu=False).start()
            while child.alive() and dog.reason is None and dog.alive():
                time.sleep(poll_s)
            reason = dog.reason or (None if dog.alive() else "watchdog stopped unexpectedly")
            dog.stop()
        code = child.process.wait()
        if reason:
            raise ServiceError(f"CUDA build stopped: {reason}; see {staging}")
        if code != 0:
            raise ServiceError(f"CUDA build failed ({code}); see {staging / 'build.stdout.log'}")
        manifest = read_json(staging / "MANIFEST.json")
    finally:
        if dog is not None:
            dog.stop()
        if child is not None:
            child.stop(5)
        lock.close()
    os.replace(staging, output)  # after the log drains have closed their files
    return manifest


def prompt_ids(directory, entry):
    """The oracle prompt: a reference file's token ids repeated cyclically to the length, checked by its sha256."""
    import hashlib
    raw = (Path(directory) / entry["source"]).read_text(encoding="utf-8")
    ids = [int(x) for x in re.split(r"[\s,\[\]]+", raw) if x]
    if not ids:
        raise ServiceError(f"{entry['source']}: no token ids")
    ids = [ids[i % len(ids)] for i in range(entry["tokens"])]
    if hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest() != entry["ids_sha256"]:
        raise ServiceError(f"{entry['source']}: token ids differ from the oracle's prompt at {entry['tokens']}")
    return ids


def _request(address, port, method, path, body, key, timeout=900):
    import http.client
    conn = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, json.loads(resp.read(64 << 20) or b"null")
    finally:
        conn.close()


def _running(profile_name):
    from . import orchestrate
    state = orchestrate._read(orchestrate._state_path(profile_name))
    service = state.get("prefill_service") or {}
    if not service.get("port"):
        raise ServiceError(f"{profile_name}: no running prefill service")
    return state, service


def _keys(profile_name, suffix):
    from . import orchestrate
    return secrets.ensure(orchestrate._secret_name(profile_name, suffix))


def gpu_memory(run=subprocess.run):
    """uuid -> used MiB for every NVIDIA GPU (device-level evidence when WDDM lists no per-process rows)."""
    return {row[0]: int(row[1]) for row in _smi(["--query-gpu=uuid,memory.used", "--format=csv,noheader,nounits"], run)}


def visibility(service, apps=compute_apps, memory=gpu_memory, *, observer=None):
    """Acceptance 1. True/False from per-process rows; None (unverified) when the driver lists none, with the
    device memory of every GPU as the evidence to read beside the service's PRO-pinned environment."""
    if service.get("placement_supervision") is not None:
        from . import gpu_counters
        owned = observer is None
        observer = observer or gpu_counters.Observer()
        try:
            sample = observer.sample(service["pid"])
            allowed = {u.lower() for u in service["placement_supervision"]["caps_gib"]}
            if not allowed <= sample.keys() or any(type(v) is not int or v < 0 for v in sample.values()):
                raise gpu_counters.CounterError("attributable GPU counters unavailable")
            gpus = sorted(u for u, used in sample.items() if used > 0)
            return {"only_granted": set(gpus) <= allowed, "visible_on": gpus, "vram_bytes": sample}
        finally:
            if owned:
                observer.close()
    gpus = sorted({gpu for gpu, pid, _ in apps() if pid == service.get("pid")})
    verdict = None if not gpus else gpus == [service["device"]]
    return {"only_pro": verdict, "visible_on": gpus, "device_memory_mib": memory(),
            "why": None if gpus else "no per-process rows from the driver (WDDM); see device_memory_mib"}


def fidelity_gate(cluster, profile_name, prompts, request=_request, apps=compute_apps, memory=gpu_memory,
                  service=None, key=None):
    """Select the owned native replica gate or the unchanged legacy prefill oracle."""
    active = service
    if active is None:
        try:
            _, active = _running(profile_name)
        except (OSError, ValueError, RuntimeError, KeyError):
            _service(cluster, profile_name)  # Legacy preset errors precede missing/not-running state errors.
            raise
    if active.get("spool") or active.get("placement_supervision") is not None:
        from . import replica_gate
        return replica_gate.fidelity_gate(active, prompts)
    return _legacy_fidelity_gate(cluster, profile_name, prompts, request, apps, memory, service, key)


def _legacy_fidelity_gate(cluster, profile_name, prompts, request=_request, apps=compute_apps, memory=gpu_memory,
                          service=None, key=None):
    """The preset's named oracle against a service (the profile's running one by default): state sha256, next token,
    logits FNV and state size at every oracle length (no handoff), plus the GPUs the process is visible on."""
    _, data, _ = _service(cluster, profile_name)
    if service is None:
        _, service = _running(profile_name)
        key = _keys(profile_name, "prefill")
    rows, ok = [], True
    for entry in data["fidelity"]["oracle"]:
        body = {"tokens": prompt_ids(prompts, entry), "session": f"gate-{entry['tokens']}"}
        status, _, reply = request("127.0.0.1", service["port"], "POST", "/v1/prefill", body, key)
        reply = reply if isinstance(reply, dict) else {}
        got = {k: reply.get(k) for k in ("sha256", "next_token", "logits_fnv", "state_bytes")}
        want = {"sha256": entry["state_sha256"], "next_token": entry["next_token"], "logits_fnv": entry["logits_fnv"],
                "state_bytes": entry["state_bytes"]}
        equal = status == 200 and got == want and not any((reply.get("fast") or {}).values())
        ok &= equal
        rows.append({"tokens": entry["tokens"], "status": status, "equal": equal, "got": got,
                     "seconds": reply.get("seconds")})
    seen = visibility(service, apps, memory) if service.get("pid") else {"only_pro": None, "why": "attached: no pid"}
    return {"gate": data["fidelity"]["name"], "pass": ok and seen.get("only_granted", seen.get("only_pro")) is not False,
            "rows": rows,
            "visibility": seen, "quality_gate": data["fidelity"]["quality_gate"],
            "quality_gate_why": data["fidelity"]["quality_gate_why"]}


_FILLER = ("The loom carries one thread across the warp, and every pass adds a row that the next pass must respect. "
           "Weavers count rows, check tension, and keep the pattern even from the first edge to the last. ")


def _text_of(call, tokens):
    """Deterministic English text of at most `tokens` tokens by the decode server's own tokenizer (via the router)."""
    words = (_FILLER * (tokens // 20 + 8)).split(" ")
    low, high = 1, len(words)
    while low < high:  # the longest prefix at or below the target
        mid = (low + high + 1) // 2
        status, _, reply = call("POST", "/tokenize", {"prompt": " ".join(words[:mid])})
        if status != 200:
            raise ServiceError(f"/tokenize answered {status}")
        if len(reply["tokens"]) <= tokens:
            low = mid
        else:
            high = mid - 1
    return " ".join(words[:low])


def placement_check(cluster, profile_name, request=_request, health=_health, long_tokens=8000, short_tokens=64):
    """Acceptance 2-3: a long prompt is placed offload, imported and decoded with no fallback; a short one stays local."""
    state, service = _running(profile_name)
    if not state.get("router"):
        raise ServiceError(f"{profile_name}: router not running")
    endpoint, key = state["router"]["endpoint"], _keys(profile_name, "router")

    def call(method, path, body):
        return request(endpoint["address"], endpoint["port"], method, path, body, key)

    def counts():
        # The router reports its prefill placements under /api/status -> prefill -> placements; a missing block is an
        # error, not zero counts (W3 read a wrong key and saw every delta as 0).
        placements = ((call("GET", "/api/status", None)[2] or {}).get("prefill") or {}).get("placements")
        if not isinstance(placements, dict):
            raise ServiceError("router status has no prefill placements")
        return placements

    rows = []
    for kind, tokens in (("long", long_tokens), ("short", short_tokens)):
        prompt = _text_of(call, tokens)
        before, served = counts(), health(service["port"]).get("served") or 0
        status, headers, reply = call("POST", "/v1/completions",
                                      {"prompt": prompt, "max_tokens": 16, "temperature": 0, "stream": False})
        after = counts()
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in ("local", "offload", "fallback", "cache")}
        served = (health(service["port"]).get("served") or 0) - served
        note = headers.get("x-tensormeld-prefill", "")
        if kind == "long":
            want = note.startswith("offload") and delta["offload"] == 1 and delta["fallback"] == 0 and served == 1
        else:
            want = note.startswith("local") and delta["offload"] == 0 and served == 0
        rows.append({"prompt": kind, "target_tokens": tokens, "status": status, "placement": note,
                     "router_counts": delta, "service_served": served,
                     "completion_tokens": (((reply if isinstance(reply, dict) else {}).get("usage")) or {}).get("completion_tokens"),
                     "pass": status == 200 and want})
    return {"pass": all(r["pass"] for r in rows), "rows": rows}


def check(cluster, profile_name):
    """Passive: config, reservation, device identity, memory and the built release. Starts nothing on the GPU."""
    s, _, _ = _service(cluster, profile_name)
    device = next(d for d in cluster.machines[s["machine"]].devices if d["id"] == s["device"])
    try:
        preview = render_preview(cluster, profile_name)
    except ConfigError as exc:
        preview = {"blocked": str(exc)}
    output, _ = _release_dir(cluster, profile_name)
    manifest = output / "MANIFEST.json"
    return {"device": verify_pro(device["uuid"], require_idle=False), "memory": memory_status(),
            "service": preview, "release": read_json(manifest) if manifest.exists() else None}


def standalone_gate(cluster, profile_name, prompts, *, detached, start=start_service, stop=stop_service, gate=None,
                    vram_cap_gib=None):
    """Acceptance 1 and 4 without the pair: launch the service exactly as `up` does (same render, helper, Job Object
    and watchdog; the handoff key is a throwaway, nothing is handed off), run the oracle, stop it."""
    import secrets as random
    gate = gate or fidelity_gate
    key = random.token_hex(32)
    spec = render_service(cluster, profile_name, api_key=key, handoff_api_key=random.token_hex(32),
                          free_physical_gib=memory_status()["free_physical_gib"], vram_cap_gib=vram_cap_gib)
    record = start(spec, detached=detached)
    try:
        result = gate(cluster, profile_name, prompts, service=record, key=key)
        result["launch"] = {"argv": spec.redacted, "ready": record.get("ready"), "binary_sha256": spec.binary_sha256,
                            "vram_cap_gib": spec.vram_cap_gib, "placement": spec.placement}
        return result
    finally:
        stop(record)


def plan_command(cluster, profile_name, vram_cap_gib=None):
    """Passive: the placement `up` would start with at this cap, and the flags it becomes."""
    preview = render_preview(cluster, profile_name, vram_cap_gib=vram_cap_gib)
    return {"vram_cap_gib": preview["vram_cap_gib"], "argv": preview["argv"], "placement": preview["placement"]}


def command(operation, cluster, profile_name, *, prompts=None, source=None, attach=None, standalone=False,
            vram_cap_gib=None):
    if profile_name not in cluster.profiles:
        raise ConfigError(f"unknown profile: {profile_name}")
    if operation == "plan":
        return plan_command(cluster, profile_name, vram_cap_gib)
    if operation == "check":
        return check(cluster, profile_name)
    if operation == "build":
        return build(cluster, profile_name, source)
    if operation == "gate":
        if prompts is None:
            raise ServiceError("gate needs --prompts <directory of the oracle's reference prompt files>")
        if attach is not None:  # a service launched some other way (the lab's own line), for launch-equivalence
            return fidelity_gate(cluster, profile_name, prompts, service={"port": attach, "device": None}, key="")
        if standalone:
            from .orchestrate import _detached
            return standalone_gate(cluster, profile_name, prompts, detached=_detached, vram_cap_gib=vram_cap_gib)
        return fidelity_gate(cluster, profile_name, prompts)
    return placement_check(cluster, profile_name)
