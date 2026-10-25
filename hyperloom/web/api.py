"""Portal documents and CLI operations.

Wire contract: every GET document has schema ``hyperloom/ui-<screen>-v1``,
server_time (Unix seconds), poll_interval_s=2 and a ``why`` map keyed by missing
field names. Collections: devices.machines/discovered, topology.links,
models.models/proposals, profiles.profiles, stats.engines/recent_requests,
logs.entries/failures, settings.trusted_machines. Missing sources are null.
POST arguments use CLI names (hyphens become underscores); replies contain
job/id, state, action, arguments, result, error. Poll /jobs or /jobs/<id>.
GET logs accepts profile/process/tail. POST cancel accepts job or profile.
Recipes (web/recipes.py, docs/api.md): GET recipes, recipes/<id>, recipes/active, recipes/routes; synchronous POST
recipes/{validate,explain,check,diff,pick,select,plan} and recipes/{save,delete,save-layer,delete-layer,save-receipt};
jobs recipes/probe and recipes/deploy. Each one runs the `hyperloom recipe ...` command line.
"""

from collections import deque
from copy import deepcopy
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import ProxyHandler, Request, build_opener

from hyperloom import __version__, admission, cli, config, engines, nodegate, orchestrate, remote, render, secrets
from hyperloom.adapters import engine, router
from hyperloom.paths import repo_root
from hyperloom.stats import Window
from hyperloom.winproc import no_window
from hyperloom.web import recipes as recipe_web
from hyperloom.web import features as feature_registry


# Token counts (a recipe's prompt_tokens_min, max_copies_per_token) are measurements; a key that also names a secret
# (password, secret, authorization, api key, private key) is redacted whatever its value.
MEASUREMENT = re.compile(r"(?i)[a-z0-9_]*(?:tokens|per_token)[a-z0-9_]*")
SECRET_NAME = re.compile(r"(?i)password|secret|authorization|api[_-]?key|private[_-]?key")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")      # profile and machine ids (orchestrate._identifier)
SCREENS = frozenset(("devices", "topology", "models", "profiles", "stats", "logs", "settings"))
MUTATIONS = frozenset(("up", "down", "install", "hosts/pin", "doctor", "keys/rotate", "cancel")) | recipe_web.JOBS
POLL_INTERVAL = 2
CAPABILITIES = {
    "up": {"enabled": True, "why": "Qwen replicas; any model with a recipe"},
    "down": {"enabled": True}, "install": {"enabled": True},
    "hosts/pin": {"enabled": True}, "doctor": {"enabled": True},
    "keys/rotate": {"enabled": True, "why": "engine keys; restart required"},
    "pair": {"enabled": False, "why": "later phase · TensorMeld agent · TM-R10..R13"},
    "discover": {"enabled": False, "why": "later phase · TM-R13"},
    "revoke": {"enabled": False, "why": "later phase · TensorMeld agent"},
    "placement": {"enabled": False, "why": "later phase 2.5 · R17 / TM-R15"},
    "link_probe": {"enabled": False, "why": "later phase 2"},
    "step_breakdown": {"enabled": False, "why": "needs R16 / E-C5"},
    "usb4_telemetry": {"enabled": False, "why": "needs R18"},
}


class APIError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class JobFailed(Exception):
    """Raised by a feature job whose failure is definite (or whose outcome is unresolved): the job ends "failed"
    even when a cancel was requested meanwhile, and `outcome` (a short word such as "unresolved", matching
    JOB_OUTCOME; anything else is recorded as "invalid") is kept on the job and its audit line, so the job, the
    audit log and the feature's own records agree."""

    def __init__(self, message, outcome=None):
        super().__init__(message)
        self.outcome = outcome


JOB_OUTCOME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
LOOPBACK_UNAVAILABLE = "unavailable: engine bound to loopback on its node"
OWNER_LEASE_S = 120.0       # a portal-started benchmark is abandoned when its page stops renewing it this long
HOLD_RENEW_S = 30.0


class JobContext:
    """Passed to feature jobs: ctx(text) reports progress; ctx.cancelled() is true after a cancel request, a stop
    of an overlapping profile (from any portal or CLI process, through the shared admission database), the
    deadline, or, for a job started from a portal page, when that page stopped renewing it (abandoned).
    ctx.reason says which; ctx.deadline is a monotonic time."""

    def __init__(self, deadline_s, admission_db=None, hold=None, owned=False):
        self.deadline = time.monotonic() + deadline_s
        self.reason = None
        self.report = lambda text: None
        self.hold = hold
        self._admission = admission_db
        self._event = threading.Event()
        self._owner_expires = time.monotonic() + OWNER_LEASE_S if owned else None
        self._checked = 0.0

    def __call__(self, text):
        self.report(text)

    def cancel(self, reason):
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def renew_owner(self):
        if self._owner_expires is not None:
            self._owner_expires = time.monotonic() + OWNER_LEASE_S

    def owner_fresh(self):
        return self._owner_expires is None or time.monotonic() < self._owner_expires

    def cancelled(self):
        if not self._event.is_set():
            now = time.monotonic()
            if now >= self.deadline:
                self.cancel("deadline reached")
            elif not self.owner_fresh():
                self.cancel("abandoned: the portal page that started it stopped renewing it")
            elif self._admission is not None and self.hold and now - self._checked >= 1.0:
                self._checked = now
                try:
                    reason = self._admission.cancelled(self.hold)
                except Exception:  # noqa: BLE001 - unreadable now: checked again at the next call
                    reason = None
                if reason:
                    self.cancel(reason)
        return self._event.is_set()

    def wait(self, seconds):
        """Sleep up to `seconds`, waking early on cancellation; True when cancelled."""
        end = time.monotonic() + max(0.0, seconds)
        while not self.cancelled():
            left = min(end, self.deadline) - time.monotonic()
            if left <= 0:
                return self.cancelled()
            self._event.wait(min(left, 1.0))
        return True


def _binds_loopback(profile, status, cluster):
    """True when the profile's engine binds 127.0.0.1 on its node (no LAN listener to poll): the launched release's
    bind (a key-file release binds the LAN), else its family's."""
    launch = (status or {}).get("launch") or {}
    family, preset = launch.get("family"), launch.get("preset")
    try:
        if preset is None and family is None and cluster is not None and profile in cluster.profiles:
            preset = cluster.profiles[profile].get("preset", profile)
        if isinstance(preset, str):
            try:
                return engines.release_adapter(preset).bind == "loopback"
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if family is None:
                family = config.family(preset)
        return family is not None and engines.adapter(family).bind == "loopback"
    except (OSError, ValueError, KeyError):
        return False


# Keys that hold token counts, token-id arrays or tokenizer identities (data, not credentials). Anything else whose
# name looks like a credential ("token", "access_token", "csrf_token", api keys, passwords...) is redacted whatever
# its type.
DATA_TOKEN_KEY = re.compile(r"(?:[a-z0-9_]*_)?tokens(?:_[a-z0-9_]+)?|(?:[a-z0-9_]*_)?token_(?:ids|count|counts|budget)"
                            r"|tokenizer(?:_[a-z0-9_]+)?", re.I)
COMPRESSED_IDS = re.compile(r"gz:b64:[A-Za-z0-9+/]+={0,2}")


def _token_data(key, item):
    if not DATA_TOKEN_KEY.fullmatch(key) or re.search(r"password|secret|authorization|api[_-]?key|private|bearer|csrf", key, re.I):
        return False
    number = lambda v: type(v) in (int, float)
    if key.lower().startswith("tokenizer"):
        return isinstance(item, str) and len(item) <= 200 and not re.search(r"(?i)bearer\s|\s", item)
    return (number(item) or (isinstance(item, list) and all(type(v) is int for v in item))
            or (isinstance(item, dict) and bool(item)          # a counter's accounting: numbers, flags, unknowns
                and all(number(v) or v is None or type(v) is bool for v in item.values()))
            or (isinstance(item, str) and key.lower().endswith("token_ids") and bool(COMPRESSED_IDS.fullmatch(item))))


def redact(value, text=True):
    """Redact structured secrets and command/log representations of them. ``text=False``: keys and argv only, free
    text kept as it is (recipe documents: their prose would otherwise be rewritten, "per token: an upper bound")."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("why", "series") and isinstance(item, dict):
                # A map keyed by field or series names (why a field has no value; a series' history link): its
                # strings go through the text rules (a credential in them is still redacted); a field name such as
                # "computed_tokens" is not a secret. Other values are redacted as usual.
                result[key] = {k: redact(v, text) if isinstance(v, str) else redact({k: v}, text)[k]
                               for k, v in item.items()}
                continue
            metadata = key == "secrets" and isinstance(item, dict) and item in (
                {"storage": "DPAPI", "values": "never shown"},
                {"storage": "0600 files", "values": "never shown"})
            sensitive = re.search(r"password|secret|token|authorization|api[_-]?key|private[_-]?key", key, re.I)
            counts = sensitive and _token_data(key, item)
            credential = re.search(r"password|secret|authorization|api[_-]?key|private[_-]?key|bearer|csrf", key, re.I)
            # Token counts are measurements (a recipe's prompt_tokens_min, max_copies_per_token), not secrets.
            number = item is None or isinstance(item, (int, float)) and not isinstance(item, bool)
            measurement = number and MEASUREMENT.fullmatch(key) and not SECRET_NAME.search(key) and not credential
            # A *_tokens key keeps a number only (never a string); token-id arrays and the like are `counts`.
            exempt = key.endswith(("_file", "_path")) or (number and key.endswith("_tokens") and not credential)
            result[key] = "[redacted]" if sensitive and not counts and not measurement and not metadata and \
                not exempt else redact(item, text)
        return result
    if isinstance(value, (list, tuple)):
        result, hide = [], False
        for item in value:
            result.append("[redacted]" if hide else redact(item, text))
            hide = isinstance(item, str) and item in ("--api-key", "--handoff-api-key", "--password", "--token")
        return result
    if isinstance(value, str) and text:
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[redacted]", value)
        return re.sub(r'''(?ix)((?:["']?(?:api[_-]?key|handoff[_-]?api[_-]?key|password|secret|token)["']?)\s*[:=]\s*|--(?:api-key|handoff-api-key|password|token)\s+)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                      r"\1[redacted]", value)
    return value


def _value(check):
    return check.get("value") if isinstance(check, dict) and check.get("status") == "ok" else None


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def _missing(row, fields, why="source unavailable"):
    reasons = row.setdefault("why", {})
    for field in fields:
        if row.setdefault(field, None) is None:
            reasons.setdefault(field, why)
        else:
            reasons.pop(field, None)
    return row


def _lease(row, now):
    result = dict(row)
    deadline = row.get("deadline")
    result["remaining_s"] = max(0, deadline - now) if _finite(deadline) else None
    result["cap_s"] = (row.get("approval") or {}).get("lease_s", 1200)
    result["drain_margin_s"] = 60
    return _missing(result, ("label", "deadline", "remaining_s", "approval"), "lease field not reported")


def _os_name(value):
    if isinstance(value, dict):
        return value.get("PRETTY_NAME") or " ".join(str(value[k]) for k in ("system", "release") if value.get(k)) or None
    if isinstance(value, str):
        match = re.search(r'^PRETTY_NAME=(.*)$', value, re.M)
        return match[1].strip().strip('\"\'') if match else value.splitlines()[0]
    return None


def _rocm(doctor):
    packages = _value(doctor.get("rocm_package_version"))
    versions = []
    if isinstance(packages, dict):
        versions = [str(v) for k, v in packages.items() if re.match(r"^(amdrocm|rocm-core)", k)]
    elif isinstance(packages, str):
        versions = [packages]
    for version in versions:
        match = re.search(r"(?:^|:)(\d+\.\d+\.\d+)", version)
        if match:
            return "ROCm " + match[1]
    return None


def _nic_name(name):
    # Some probe providers already decoded UTF-8 as the Windows ANSI codepage.
    # Repair only a reversible UTF-8 sequence, preserving valid native names.
    for encoding in ("cp1252", "latin1"):
        if not any(marker in name for marker in ("Ã", "Â", "â")):
            break
        try:
            return name.encode(encoding).decode("utf-8")
        except UnicodeError:
            pass
    return name


def _nics(interfaces):
    if interfaces is None:
        return None
    result = []
    for interface in interfaces:
        name = _nic_name(interface.get("name", interface.get("Name", "")))
        state = str(interface.get("state", interface.get("Status", ""))).lower()
        if state != "up" or interface.get("physical") is False or re.search(
                r"loopback|^lo$|virtual|vethernet|hyper-v|bluetooth|pseudo|docker|veth|bridge|^br-|^virbr|^tun|^tap|vmnet|wsl", name, re.I):
            continue
        rate = interface.get("negotiated_mbps")
        if rate is None and isinstance(interface.get("LinkSpeed"), str):
            match = re.fullmatch(r"([\d.]+)\s*([GM])bps", interface["LinkSpeed"], re.I)
            if match:
                rate = float(match[1]) * (1000 if match[2].upper() == "G" else 1)
        speed = f"{rate / 1000:g} Gb/s" if _finite(rate) and rate >= 1000 else f"{rate:g} Mb/s" if _finite(rate) and rate > 0 else "speed not reported"
        result.append({**interface, "name": name, "summary": f"{name} · {speed}"})
    return result


def _usb4(doctor):
    usb = doctor.get("usb4", {})
    describe = _value(usb.get("describe")) or {}
    layout = _value(usb.get("layout"))
    if not (_value(usb.get("module")) or describe or layout or _value(usb.get("devices"))):
        return None
    topology = describe.get("topology") or (layout.get("topology", {}) if isinstance(layout, dict) else {})
    streams = [v for k, v in describe.items() if isinstance(v, dict) and re.fullmatch(r".+/data\d+", k)]
    if not streams and isinstance(layout, dict):
        streams = [v for k, v in layout.items() if isinstance(v, dict) and re.fullmatch(r".+/data\d+", k)]
    reported = _value(usb.get("streams")) or {}
    openable = (sum(v["openable"] for v in streams) if streams and all(type(v.get("openable")) is bool for v in streams)
                else len(reported.get("openable", [])) if not streams and reported.get("count") else None)
    warnings = list(topology.get("warnings") or [])
    cables = []
    for cable in topology.get("cables", []):
        link = cable.get("link") or {}
        cid = cable.get("id", "?")
        cable_warnings = [w for w in warnings if re.search(r"\bcable\s+" + re.escape(cid) + r"\b", str(w), re.I)]
        if link.get("full_speed") is False and not cable_warnings:
            cable_warnings.append(f"Cable {cid} trained below full speed")
        if cable.get("state") in ("down", "missing", "error"):
            cable_warnings.append(f"Cable {cid} {cable['state']}")
        speed = link.get("gbps")
        summary = f"Cable {cid} · {speed:g} Gb/s" if _finite(speed) else f"Cable {cid} · speed not reported"
        cables.append({"id": cid, "summary": summary, "gbps": speed,
                       "peer": (cable.get("remote") or {}).get("device_name"),
                       "streams_openable": cable.get("streams_openable"),
                       "health": "warning" if cable_warnings else cable.get("state", "unknown"), "warnings": cable_warnings})
        warnings.extend(w for w in cable_warnings if w not in warnings)
    if openable is None and cables and all(type(c.get("streams_openable")) is int for c in cables):
        openable = sum(c["streams_openable"] for c in cables)
    if openable is not None and openable < 4:
        warnings.append(f"USB4 missing stream · {openable}/4 streams openable")
    host = bool(streams) and all((v.get("config") or {}).get("gpu_direct") == 0 for v in streams)
    number = layout if isinstance(layout, (str, int)) else layout.get("layout") if isinstance(layout, dict) else None
    if number is None and host:
        number = 1
    summary = f"{openable}/4 streams openable" if openable is not None else "stream openability not reported"
    if number is not None:
        summary += f" · layout {number}" + (" (host)" if host else "")
    return {"summary": summary, "layout": number, "openable": openable, "cables": cables, "host": topology.get("host"),
            "driver_loaded": _value(usb.get("module")) is True, "warnings": list(dict.fromkeys(warnings))}


def _accelerator(raw, doctor, gpu, declared):
    amd = "vram_total_bytes" in raw
    vram, gtt = _value(raw.get("vram_total_bytes")), _value(raw.get("gtt_total_bytes"))
    # Large GTT with a small carve-out identifies the UMA sysfs accounting.
    unified = amd and _finite(vram) and _finite(gtt) and 0 < vram <= 1024 ** 3 and gtt > vram
    targets = _value(doctor.get("gfx_targets")) or []
    target = raw.get("gfx_target") or (targets[0].get("target") if len(targets) == 1 else None)
    name = raw.get("name") or _value(raw.get("product_name"))
    if not name and target == "gfx1151":
        name = "Radeon 8060S"
    if amd and target:
        name = f"{name} · {target}" if name else target
    row = {"id": raw.get("id"), "name": raw.get("name"), "source": "hyperloom-node gpu" if amd else "tensormeld probe",
           "memory_used_bytes": _value(raw.get("vram_used_bytes")) if amd else None,
           "memory_total_bytes": _value(raw.get("vram_total_bytes")) if amd else raw.get("reported_memory_bytes"),
           "memory_free_bytes": None if amd else raw.get("reported_free_bytes"),
           "utilization_pct": _value(raw.get("busy_percent")) if amd else raw.get("utilization_pct"),
           "driver": raw.get("driver_version"), "runtime": _rocm(doctor) if amd else raw.get("cuda_version"),
           "kernel": _value(gpu.get("kernel")), "usb4_driver": _value(doctor.get("usb4", {}).get("module")),
           "layout": _value(doctor.get("usb4", {}).get("layout")), "raw": raw, "why": {}}
    row.update(name=name, memory_kind="unified · GPU-visible" if unified else "VRAM")
    if unified:
        used = [_value(raw.get(field)) for field in ("vram_used_bytes", "gtt_used_bytes")]
        row["memory_total_bytes"] = vram + gtt
        row["memory_used_bytes"] = sum(used) if all(_finite(v) for v in used) else None
    for field in ("temperature_c", "power_w"):
        if not amd:
            row[field] = raw.get(field)
            continue
        sensors = _value(raw.get(field))
        readings = [_value(value) for value in sensors.values()] if isinstance(sensors, dict) else []
        row[field] = readings[0] if len(readings) == 1 else None
        if len(readings) > 1:
            row["why"][field] = "multiple hwmon sensors; see raw readings"
    if _finite(row["memory_total_bytes"]) and _finite(row["memory_used_bytes"]):
        row["memory_free_bytes"] = row["memory_total_bytes"] - row["memory_used_bytes"]
    if not amd and _finite(row["memory_total_bytes"]) and _finite(row["memory_free_bytes"]):
        row["memory_used_bytes"] = row["memory_total_bytes"] - row["memory_free_bytes"]
    policy = next((device for device in declared if device.get("id") == raw.get("id") and raw.get("id")), {})
    row["excluded"] = policy.get("excluded", False) or "4090" in (row["name"] or "")
    row["policy_reason"] = policy.get("reason") or ("RTX 4090 is excluded by policy" if row["excluded"] else None)
    return _missing(row, ("id", "name", "memory_used_bytes", "memory_total_bytes", "memory_free_bytes", "utilization_pct",
                          "temperature_c", "power_w", "driver", "runtime", "kernel", "usb4_driver", "layout", "policy_reason"))


def _http(endpoint, suffix, text=False):
    address = endpoint["address"]
    host = f"[{address}]" if ":" in address else address
    request = Request(f"http://{host}:{endpoint['port']}{suffix}")
    with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("telemetry response exceeds 1 MiB")
    return data.decode("utf-8") if text else json.loads(data)


class API:
    def __init__(self, config_path=None, *, orchestrator=None, remote_module=None,
                 clock=time.time, audit_path=None):
        self.config_path = Path(config_path) if config_path else cli.config_path()
        self.orchestrate = orchestrator or orchestrate
        # With the real orchestrator, up/down/install run as detached CLI processes so they finish (and
        # clean up) even if the portal process exits or is killed mid-job.
        self.detached = False  # set by serve(); tests keep the in-process path
        self.remote = remote_module or remote
        self.clock = clock
        self.audit_path = Path(audit_path) if audit_path else cli.config_path().parent / "audit.jsonl"
        self._poll_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._cached = None
        self._expires = 0
        self._jobs = {}
        self._active = None
        self._seen = {}
        self._windows = {}
        self._history = deque(maxlen=1800)
        self._counters = {}
        self._recent = {}
        self._features_running = {}     # job id -> JobContext of every running feature job
        self.admission_db = None        # tests inject one; else the configuration's shared database (C11)
        self._holds = {}                # job id -> admission hold id (core, stop and admitted feature jobs)
        self._heartbeat = None
        self._heartbeat_lock = threading.Lock()     # a hold is published only once the renewal thread runs
        self.recipes = recipe_web.Runner()

    def _recipe_loader(self, path):
        return self.orchestrate.load_cluster(path, config)

    def recipe_get(self, endpoint, query):
        """GET recipes, recipes/<id>, recipes/active, recipes/routes: (status, document); the CLI's own parser and
        commands.run, in process (they only read files)."""
        if endpoint in recipe_web.POSTS or endpoint in recipe_web.JOBS:
            raise APIError(endpoint + " requires POST", 405)
        from hyperloom.recipes import service
        with service.configured(self.config_path):
            try:
                operation, argv = recipe_web.read_request(endpoint, query, config=self.config_path)
            except ValueError as exc:
                raise APIError(str(exc)) from exc
            status, document = recipe_web.document(operation, argv, recipe_web.in_process(argv, self._recipe_loader),
                                                   now=self.clock())
        return status, recipe_web.redacted(document, redact)

    def recipe_post(self, action, arguments, cancelled=lambda: False):
        """POST recipes/<query or write>: (status, document) of the CLI child; writes are audited."""
        from hyperloom.recipes import service
        try:
            with service.configured(self.config_path):
                request = recipe_web.Request(action, arguments, config=self.config_path)
        except ValueError as exc:
            if action in recipe_web.WRITES:
                self._audit(action, recipe_web.audit_arguments(arguments), {"state": "refused", "error": str(exc)})
            raise APIError(str(exc)) from exc
        try:
            try:
                result = self.recipes.run(request.argv, stdin=request.stdin, deadline_s=request.deadline_s,
                                          cancelled=cancelled)
            except recipe_web.Busy as exc:
                raise APIError(str(exc), 503) from exc
            status, document = recipe_web.document(request.operation, request.command_line_argv(), result,
                                                   now=self.clock(), cli_files=request.cli_files)
        finally:
            request.close()
        if action in recipe_web.WRITES:
            self._audit(action, recipe_web.audit_arguments(arguments),
                        {"state": "succeeded" if document["ok"] else "refused", "exit_code": document["exit_code"],
                         "error": document["error"], "result": document["result"]})
        return status, recipe_web.redacted(document, redact)

    def _cluster(self):
        return self.orchestrate.load_cluster(self.config_path, config)

    def _document(self, screen, **fields):
        return {"schema": f"hyperloom/ui-{screen}-v1", "server_time": self.clock(),
                "poll_interval_s": POLL_INTERVAL, "why": {}, **fields}

    def _node(self, machine, verb, payload=None):
        payload = payload or {}
        if machine.access == "ssh":
            # One node call per machine at a time across portal processes and the CLI (reentrant for a holder).
            with nodegate.hold(self.config_path, machine.id, wait=15.0):
                return self.remote.Remote(machine, self.orchestrate._known_hosts()).call(verb, payload, timeout=10)
        if machine.access != "local":
            raise ValueError("later phase · TensorMeld agent")
        result = subprocess.run([str(Path(sys.executable).absolute()),
                                 str(repo_root() / "node" / "hyperloom-node"), verb],
                                input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=15,
                                **no_window())
        if result.returncode:
            raise ValueError("local " + verb + " failed")
        return json.loads(result.stdout)

    def _probe(self, machine):
        # There is no remote probe verb in the node protocol at this pin.
        if machine.access != "local":
            return None
        result = subprocess.run([str(Path(sys.executable).absolute()), "-m", "tensormeld", "probe"],
                                env={**cli._router_env(), "PYTHONIOENCODING": "utf-8"},
                                text=True, encoding="utf-8", capture_output=True, timeout=15, **no_window())
        if result.returncode:
            raise ValueError("tensormeld probe unavailable")
        data = json.loads(result.stdout)
        if data.get("schema_version") != 1:
            raise ValueError("unknown probe schema")
        for interface in data.get("interfaces", []):
            interface["name"] = _nic_name(interface.get("name", ""))
        if os.name == "nt":
            try:
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                    "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
                    text=True, encoding="utf-8", capture_output=True, timeout=5, **no_window())
                if result.returncode == 0 and result.stdout.strip():
                    data["cpu_model"] = result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        # nvidia-smi is a passive status query; never create a GPU context.
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw",
                                     "--format=csv,noheader,nounits"], capture_output=True, text=True, encoding="utf-8", timeout=5,
                                    **no_window())
            readings = list(csv.reader(result.stdout.splitlines())) if result.returncode == 0 else []
            devices = [d for d in data.get("devices", []) if d.get("vendor") == "nvidia"]
            for device in devices:
                matches = [r for r in readings if len(r) == 7 and (r[0].strip() == device.get("uuid", device.get("id")) or r[1].strip() == device.get("name"))]
                if len(matches) != 1:
                    continue
                values = [v.strip() for v in matches[0]]
                device["id"] = values[0]
                for key, index, scale in (("utilization_pct", 2, 1), ("reported_memory_bytes", 3, 1024 ** 2),
                                           ("temperature_c", 5, 1), ("power_w", 6, 1)):
                    try:
                        value = float(values[index]) * scale
                        if math.isfinite(value):
                            device[key] = value
                    except ValueError:
                        pass
                try:
                    device["reported_free_bytes"] = device["reported_memory_bytes"] - float(values[4]) * 1024 ** 2
                except (ValueError, KeyError):
                    pass
        except (OSError, subprocess.SubprocessError):
            pass
        return data

    def _inventory(self, cluster, statuses):
        machines = []
        for mid, machine in cluster.machines.items():
            row = {"id": mid, "name": mid, "declared_os": machine.os, "access": machine.access,
                   "declared_devices": machine.devices, "os": None, "cpu_model": None,
                   "ram": None, "nics": None, "disk_free_bytes": None, "accelerators": None,
                   "doctor": None, "gpu": None, "probe": None, "roles": [], "leases": [],
                   "last_seen": self._seen.get(mid), "health": "offline", "why": {}}
            for verb in ("doctor", "gpu"):
                try:
                    value = self._node(machine, verb)
                    if not isinstance(value, dict):
                        raise ValueError("node response must be an object")
                    if value.get("error"):
                        raise ValueError(value["error"])
                    if value.get("schema") != "hyperloom/node-v1":
                        raise ValueError("unknown node schema")
                    row[verb] = value
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    row["why"][verb] = str(exc)
            try:
                row["probe"] = self._probe(machine)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                row["why"]["probe"] = str(exc)
            if row["doctor"] or row["gpu"] or row["probe"]:
                row["health"] = "online"
                self._seen[mid] = row["last_seen"] = self.clock()
            elif any("unknown host key" in why for why in row["why"].values()):
                row["health"] = "unpaired"
            doctor = (row["doctor"] or {}).get("checks", {})
            gpu = (row["gpu"] or {}).get("checks", {})
            probe = row["probe"] or {}
            row.update(os=_os_name(_value(doctor.get("os")) or probe.get("os")),
                       cpu_model=_value(doctor.get("cpu_model")) or probe.get("cpu_model"), ram=_value(gpu.get("ram")),
                       nics=_nics(probe.get("interfaces", _value(doctor.get("interfaces")))), disk_free_bytes=(_value(doctor.get("disk")) or {}).get("free_bytes"),
                       accelerators=_value(gpu.get("amd")))
            if row["ram"] is None:
                row["ram"] = probe.get("host_memory")
            if probe.get("devices"):
                # Probe's AMD records are presence-only; prefer the sysfs records
                # rather than presenting the same UMA device twice.
                native_amd = row["accelerators"] is not None
                row["accelerators"] = (row["accelerators"] or []) + [
                    device for device in probe["devices"] if device.get("vendor") != "amd" or not native_amd]
            if row["accelerators"] is not None:
                row["accelerators"] = [_accelerator(device, doctor, gpu, machine.devices) for device in row["accelerators"]]
            row["devices"] = row["accelerators"]
            row["ram_bytes"] = (row["ram"] or {}).get("total_bytes")
            ram_free = (row["ram"] or {}).get("available_bytes")
            row["ram_summary"] = f"{ram_free / 1e9:.1f} GB free of {row['ram_bytes'] / 1e9:.1f} GB" if _finite(ram_free) and _finite(row["ram_bytes"]) else None
            row["nics_summary"] = [nic["summary"] for nic in row["nics"]] if row["nics"] is not None else None
            row["rocm"] = _rocm(doctor)
            row["rocm_packages"] = _value(doctor.get("rocm_package_version"))
            row["usb4"] = _usb4(doctor)
            kernel = _value(gpu.get("kernel"))
            row["kernel_summary"] = (kernel or "") + (" · USB4 stream driver loaded" if row["usb4"] and row["usb4"]["driver_loaded"] else "") or None
            row["cpu"] = row["cpu_model"]
            row["warnings"] = row["usb4"]["warnings"] if row["usb4"] else []
            if row["warnings"] and row["health"] == "online":
                row["health"] = "degraded"
            if machine.access == "local":
                row["roles"].append({"role": "portal"})
            for name, status in statuses.items():
                node = (status.get("nodes") or {}).get(mid, {})
                for lease in node.get("leases", []):
                    if lease.get("state") in ("stopped", "failed", "aborted", "cancelled"):
                        continue
                    row["leases"].append(_lease(lease, self.clock()))
                    for proc in lease.get("processes", []):
                        row["roles"].append({"profile": name, "role": proc.get("name")})
            row["lease"] = row["leases"][0] if row["leases"] else None
            for field in ("os", "cpu_model", "ram", "nics", "disk_free_bytes", "accelerators", "probe", "last_seen"):
                if row[field] is None:
                    row["why"].setdefault(field, "machine offline" if row["health"] == "offline" else
                                           "needs TM-R14" if field == "cpu_model" else "source unavailable")
            machines.append(_missing(row, ("devices", "ram_bytes", "cpu", "release", "component_shas", "lease")))
        return machines

    def _profiles(self, cluster, statuses):
        rows = []
        for name, declared in cluster.profiles.items():
            status = statuses.get(name, {"profile": name, "state": "stopped"})
            preset_name = declared.get("preset", name)
            try:
                family = config.family(preset_name)
            except (OSError, ValueError, KeyError):
                family = None
            row = {"id": name, "name": name, "preset": preset_name, "family": family,
                   "configuration": declared, "status": status,
                   "state": status.get("state"), "processes": [], "leases": [],
                   "effective_configuration": None, "nodes": declared.get("nodes") or [declared[k] for k in ("rank0", "rank1") if k in declared],
                   "devices": None, "router": cluster.router.get("machine", "local"), "identity": None,
                   "receipt": declared.get("receipt"), "release": declared.get("release_id"),
                   "started_at": None, "history": self._action_log(), "why": {}}
            nodes = status.get("nodes") or {}
            states = []
            for mid, node in nodes.items():
                if node.get("error"):
                    states.append("degraded")
                for lease in node.get("leases", []):
                    if lease.get("profile") and lease["profile"] != name:
                        continue
                    row["leases"].append({"machine": mid, **_lease(lease, self.clock())})
                    states.append(lease.get("state"))
                    for process in lease.get("processes", []):
                        proc = {**process, "machine": mid, "lease": lease.get("lease"),
                                "state": process.get("state") or (("ready" if process.get("ready") else "starting")
                                          if lease.get("state") in ("ready", "starting", "acquired") else lease.get("state")),
                                "binary_sha256": process.get("binary_sha256", process.get("exe_sha256")),
                                "ready_after_s": process.get("ready_after_s"),
                                "reason": process.get("reason", lease.get("reason")), "endpoint": process.get("endpoint")}
                        row["processes"].append(_missing(proc, ("state", "binary_sha256", "ready_after_s", "endpoint", "reason")))
            if row["state"] is None and not status.get("error"):
                row["state"] = ("degraded" if any(s in ("failed", "aborted", "degraded") for s in states)
                                else "running" if states and all(s == "ready" for s in states)
                                else "starting" if any(s in ("queued", "acquired", "starting") for s in states)
                                else "stopped")
            if status.get("error"):
                row["state"] = None
                row["why"]["state"] = status["error"]
            row["started_at"] = min((item["start_time"] for item in row["leases"] + row["processes"] if _finite(item.get("start_time"))), default=None)
            owned = {}
            try:
                owned = self.orchestrate._read(self.orchestrate._state_path(name))
                # Ownership records are authoritative for router placement.
                row["launch"] = redact({key: owned[key] for key in ("router", "leases", "release_id") if key in owned})
                row["router"] = (owned.get("router") or {}).get("machine")
                row["nodes"] = owned.get("nodes") or [mid for mid in owned.get("machines", {}) if mid in row["nodes"]]
                row["release"] = owned.get("release_id") or row["release"]
                row["started_at"] = owned.get("started_at") or row["started_at"]
                row["devices"] = owned.get("devices") or None
                local_router = owned.get("router") or {}
                if local_router.get("machine") == "local":
                    try:
                        directory = Path(local_router["directory"])
                        actual = self.orchestrate._read(directory / "router.json")
                        row["effective_configuration"] = [{"setting": "router", "value": redact(actual), "source": "rendered router.json"}]
                        local_state = self.orchestrate._read(directory / "state.json")
                        row["processes"].append(_missing({"name": "router", "machine": "local", "state": local_state.get("state"),
                                                           "endpoint": local_router.get("endpoint"), "pid": local_state.get("pid")},
                                                          ("ready_after_s", "binary_sha256")))
                    except (OSError, ValueError, KeyError):
                        row["why"]["effective_configuration"] = "rendered router file unavailable"
            except (OSError, ValueError, KeyError):
                row["launch"] = None
                row["why"]["launch"] = "no owned launch"
            if not row["devices"]:
                row["devices"] = [f"{mid}:{device['id']}" for mid in row["nodes"] for device in cluster.machines[mid].devices if not device.get("excluded")]
            specs = owned.get("process_specs") or []
            if not specs:
                # Older launch records have no argv/env. Render the configured
                # profile with the same pure renderer as the CLI, and label it.
                try:
                    selected = deepcopy(cluster)
                    if row["release"]:
                        selected.profiles[name]["release_id"] = row["release"]
                    if engines.adapter(config.family(declared.get("preset", name))).topology == "replicas":
                        selected.profiles[name]["nodes"] = row["nodes"]
                    specs = [asdict(spec) for spec in render.render_processes(selected, name)]
                except (OSError, ValueError, KeyError):
                    pass
            effective = row["effective_configuration"] or []
            for spec in specs:
                source = "saved launch argv/env" if owned.get("process_specs") else "configured profile render"
                for key in ("argv", "env"):
                    effective.append({"setting": f"{spec.get('machine_id', spec.get('machine', ''))} · {spec.get('role', spec.get('name', 'engine'))} · {key}",
                                      "value": redact(spec.get(key)), "source": source})
            if owned.get("rendered_router"):
                effective.append({"setting": "router", "value": redact(owned["rendered_router"]), "source": "saved launch render"})
            row["effective_configuration"] = effective or None
            row["effective_config"] = row["effective_configuration"]
            try:
                preset = config.preset(declared.get("preset", name))
                if row["receipt"] is None:
                    row["receipt"] = preset.get("receipt")
                identities = []
                for spec in specs:
                    identity = (spec.get("env") or {}).get("STRIX_TRELLIS_IDENTITY")
                    if identity and identity not in identities:
                        identities.append(identity)
                row["identity"] = {"receipt": row["receipt"], "engine_sha": preset.get("engine_sha")}
                if identities:
                    row["identity"]["trellis"] = identities
                row["parameters"] = []
                for flag, rule in preset["knobs"].items():
                    index = preset["argv"].index(flag) if flag in preset["argv"] else -1
                    production = preset["argv"][index + 1] if index >= 0 else "engine default"
                    if production == "{port}":
                        production = declared.get("port", preset["port"])
                    elif isinstance(production, str) and re.search(r"\{\w+\}", production):
                        production = "profile value"
                    if isinstance(production, str) and production.isdecimal():
                        production = int(production)
                    row["parameters"].append(_missing({"id": flag, "production": production,
                        "value": declared.get("overrides", {}).get(flag, production), "qualified": rule}, ("production", "value")))
            except (OSError, ValueError, KeyError):
                row["parameters"] = None
            row["why"]["parameter_save"] = "configuration save endpoint unavailable in the Phase 1b task contract"
            _missing(row, ("effective_config", "effective_configuration"), "rendered launch inputs unavailable; needs E-C2 / E-C3 for component provenance")
            rows.append(_missing(row, ("nodes", "devices", "router", "identity", "receipt", "release", "started_at", "parameters")))
        return rows

    def _statistics(self, statuses, cluster=None):
        engines, routers, recent = [], [], []
        now = self.clock()
        for profile, status in statuses.items():
            loopback = _binds_loopback(profile, status, cluster)
            raw_router = status.get("router")
            if not isinstance(raw_router, dict) or raw_router.get("error"):
                continue
            try:
                parsed_router = asdict(router.parse_status(raw_router))
                parsed_router["prefill_placements"] = (parsed_router["prefill"] or {}).get("placements")
                placements = parsed_router["prefill_placements"]
                previous = self._counters.get(profile + "/placements")
                parsed_router["prefill_placement_window"] = None
                if placements and all(_finite(value) for value in placements.values()):
                    if previous and all(placements[key] >= previous[1][key] for key in placements):
                        parsed_router["prefill_placement_window"] = {"window_s": now - previous[0],
                            "counts": {key: placements[key] - previous[1][key] for key in placements}}
                    self._counters[profile + "/placements"] = (now, placements)
                if parsed_router["prefill_placement_window"] is None:
                    parsed_router["why"]["prefill_placement_window"] = "requires two counter samples without reset"
                if parsed_router["prefill_placements"] is None:
                    parsed_router["why"]["prefill_placements"] = "router placements unavailable"
                routers.append({"profile": profile, **parsed_router})
            except ValueError:
                continue
            for replica in parsed_router["replicas"] or []:
                if not replica or not replica.get("endpoint"):
                    continue
                rid = profile + "/" + str(replica["id"])
                item = {"id": rid, "profile": profile, "status": None, "metrics": None,
                        "throughput": None, "ttft_p50": None, "ttft_p95": None,
                        "draft_acceptance": None, "usb4_exchange_mean_us": None, "why": {}}
                if loopback:
                    # The router on rank 0 reaches the engine on its node's 127.0.0.1; probing that address here
                    # would hit the control machine instead.
                    for key in ("status", "metrics", "throughput", "ttft_p50", "ttft_p95", "draft_acceptance",
                                "usb4_exchange_mean_us"):
                        item["why"][key] = LOOPBACK_UNAVAILABLE
                    engines.append(item)
                    continue
                window = self._windows.setdefault(rid, Window(clock=self.clock))
                try:
                    from urllib.parse import urlsplit
                    url = urlsplit(replica["endpoint"] if "://" in replica["endpoint"] else "http://" + replica["endpoint"])
                    endpoint = {"address": url.hostname, "port": url.port}
                    metrics = engine.parse_metrics(_http(endpoint, "/metrics", text=True))
                    item["metrics"] = asdict(metrics)
                    # Keep the baseline for ten seconds, even though the UI polls every two.
                    baseline = self._counters.get(rid)
                    counter = metrics.generated_tokens
                    if baseline is None or now - baseline[0] >= 10 or counter is None or (
                            baseline[1] is not None and counter < baseline[1]):
                        item["throughput"] = asdict(window.rate("generated_tokens", counter, now))
                        item["throughput"]["label"] = engine.THROUGHPUT_LABEL
                        item["throughput"]["window_s"] = now - baseline[0] if baseline else None
                        self._counters[rid] = (now, counter, item["throughput"])
                    else:
                        item["throughput"] = baseline[2]
                    parsed = engine.parse_status(_http(endpoint, "/api/status"))
                    item["status"] = asdict(parsed)
                    window.add_requests(parsed.requests, now)
                    item["ttft_p50"] = asdict(window.quantile("engine_ttft_ms", .5, now))
                    item["ttft_p95"] = asdict(window.quantile("engine_ttft_ms", .95, now))
                    # Route requires router correlation (R7), even if the engine supplies a route string.
                    retained = self._recent.setdefault(rid, {})
                    if item["throughput"].get("reset"):
                        retained.clear()
                    for request in parsed.requests or []:
                        if request:
                            if request.get("id") and _finite(request.get("time")):
                                retained.setdefault(request["id"], request)
                    retained = {request["id"]: request for request in sorted(retained.values(), key=lambda r: r["time"])[-50:]
                                if now - 600 <= request["time"] <= now}
                    self._recent[rid] = retained
                    accepted = proposed = 0
                    draft_known = False
                    for request in retained.values():
                        recent.append({**request, "engine": rid, "route": None, "why": {"route": "needs R7"}})
                        if _finite(request.get("mtp_accepted")) and _finite(request.get("mtp_proposed")):
                            accepted += request["mtp_accepted"]
                            proposed += request["mtp_proposed"]
                            draft_known = True
                    if draft_known and proposed:
                        item["draft_acceptance"] = {"value": accepted / proposed, "accepted": accepted,
                                                    "proposed": proposed, "label": "accepted / proposed · last 50 completed requests or 10 min"}
                    group = parsed.group or {}
                    exchanges, seconds = group.get("exchanges"), group.get("exchange_seconds")
                    previous = self._counters.get(rid + "/exchange")
                    if _finite(exchanges) and _finite(seconds):
                        self._counters[rid + "/exchange"] = (exchanges, seconds)
                        if previous and exchanges > previous[0] and seconds >= previous[1]:
                            item["usb4_exchange_mean_us"] = (seconds - previous[1]) * 1e6 / (exchanges - previous[0])
                except (OSError, ValueError, RuntimeError) as exc:
                    item["why"]["status"] = str(exc)
                    self._counters.pop(rid, None)
                    window.rate("generated_tokens", None, now)
                for key in ("status", "metrics", "throughput", "ttft_p50", "ttft_p95", "draft_acceptance", "usb4_exchange_mean_us"):
                    if item[key] is None:
                        item["why"].setdefault(key, "requires counter samples" if "exchange" in key else "source unavailable")
                engines.append(item)
        document = self._document("stats", engines=engines, routers=routers,
                                  recent_requests=sorted(recent, key=lambda row: row.get("time") or 0, reverse=True)[:50],
                                  step_breakdown=None, usb4_per_cable=None, client_ttft=None)
        document["why"].update(step_breakdown="needs R16 / E-C5", usb4_per_cable="needs R18", client_ttft="bench receipt unavailable")
        self._history.append({"time": now, "engines": [{key: item[key] for key in ("id", "throughput", "ttft_p50", "ttft_p95")} for item in engines]})
        document["history"] = list(self._history)
        return document

    def _snapshot(self):
        documents = {name: self._document(name) for name in SCREENS}
        try:
            cluster = self._cluster()
        except (OSError, ValueError, RuntimeError) as exc:
            for name, fields in {"devices": ("machines", "discovered"), "topology": ("links",),
                                 "models": ("models", "proposals"), "profiles": ("profiles",),
                                 "stats": ("engines", "recent_requests"), "logs": ("entries", "failures"),
                                 "settings": ("trusted_machines", "configuration")}.items():
                for field in fields:
                    documents[name][field] = None
                    documents[name]["why"][field] = "configuration unavailable: " + str(exc)
            documents["settings"]["capabilities"] = {**CAPABILITIES, **feature_registry.registry().capabilities}
            return documents
        status_error = None
        try:
            statuses = self.orchestrate.status()
        except (OSError, ValueError, RuntimeError) as exc:
            status_error = str(exc)
            statuses = {name: {"error": status_error} for name in cluster.profiles}
            documents["profiles"]["why"]["status"] = str(exc)
        machines = self._inventory(cluster, statuses)
        if status_error:
            for machine in machines:
                machine["roles"] = machine["leases"] = None
                machine["why"].update(roles=status_error, leases=status_error)
        documents["devices"].update(machines=machines, discovered=None)
        headroom = [device.get("memory_free_bytes") for machine in machines for device in machine["accelerators"] or [] if not device["excluded"]]
        all_memory = bool(headroom) and all(_finite(value) for value in headroom) and all(
            row["accelerators"] is not None and row["health"] != "offline" for row in machines)
        documents["devices"]["summary"] = _missing({
            "online": sum(machine["health"] in ("online", "degraded") for machine in machines),
            "accelerators_in_use": None, "memory_free_bytes": sum(headroom) if all_memory else None,
            "minimum_headroom_bytes": min(headroom) if all_memory else None,
            "links_healthy": None, "links_summary": None, "excluded_reason": "RTX 4090 excluded by policy"},
            ("accelerators_in_use", "memory_free_bytes", "minimum_headroom_bytes", "links_healthy", "links_summary"))
        if all(machine["roles"] is not None and machine["accelerators"] is not None for machine in machines):
            documents["devices"]["summary"]["accelerators_in_use"] = sum(
                not device["excluded"] for machine in machines if any(role.get("role") not in ("portal", "router")
                for role in machine["roles"]) for device in machine["accelerators"])
        cable_states = [cable for machine in machines if machine["usb4"] for cable in machine["usb4"]["cables"]]
        if cable_states:
            healthy = sum(cable["health"] in ("up", "ok") for cable in cable_states)
            documents["devices"]["summary"].update(links_healthy=f"{healthy}/{len(cable_states)} up",
                links_summary="USB4 cable observations · " + ("warnings reported" if healthy < len(cable_states) else "no warnings"))
        documents["devices"]["why"]["discovered"] = "later phase · TM-R13"
        usb_machines = [row for row in machines if row["usb4"]]
        links = []
        for machine in machines:
            for nic in machine["nics"] or []:
                rate = nic.get("negotiated_mbps")
                links.append({"id": machine["id"] + ":" + nic["name"], "from": machine["id"], "to": "lan",
                              "name": machine["id"] + " · " + nic["name"],
                              "kind": "Wi-Fi" if re.search(r"wi.?fi|wireless|^wl", nic["name"], re.I) else "Ethernet",
                              "gbps": rate / 1000 if _finite(rate) and rate > 0 else None,
                              "health": "up", "source": "interface status", "measured_at": self.clock(),
                              "bytes_per_s": None, "rtt_us_p50": None, "rtt_us_p99": None,
                              "why": {"bytes_per_s": "link-probe receipt unavailable", "rtt_us_p50": "link-probe receipt unavailable",
                                      "rtt_us_p99": "link-probe receipt unavailable"}})
        for machine in usb_machines:
            for cable in machine["usb4"]["cables"]:
                peer = next((m["id"] for m in usb_machines if cable["peer"] and cable["peer"] in (m["id"], m["usb4"].get("host"))), None)
                links.append({**cable, "from": machine["id"], "to": peer, "kind": "USB4", "source": "driver status",
                              "name": f"{machine['id']} ↔ {peer or 'peer'} · cable {cable['id']}",
                              "measured_at": self.clock(), "bytes_per_s": None, "rtt_us_p50": None, "rtt_us_p99": None,
                              "why": {"bytes_per_s": "needs R18", "rtt_us_p50": "needs R18", "rtt_us_p99": "needs R18"}})
        documents["topology"].update(machines=machines, links=links, usb4_per_cable=None,
                                    usb4=[{"machine": row["id"], **row["usb4"]} for row in usb_machines])
        documents["topology"]["why"].update(links="link-probe receipt unavailable; later phase 2", usb4_per_cable="needs R18")
        documents["topology"]["layout"] = [{"machine": row["id"], "value": row["usb4"]["summary"]} for row in usb_machines]
        for key in ("accelerators_in_use", "links_healthy", "links_summary"):
            if documents["devices"]["summary"][key] is not None:
                documents["devices"]["summary"]["why"].pop(key, None)
        # Declared asset bytes/hashes are identities, never evidence of presence.
        documents["models"].update(models=[{"machine": mid, "id": name, "manifest": asset,
                                            "bytes": asset.get("bytes"), "sha256": asset.get("sha256"),
                                            "revision": asset.get("source_revision"), "quant": None,
                                            "presence": None, "why": {"presence": "verify-model result unavailable"}}
                                           for mid, assets in cluster.assets.items() for name, asset in assets.items()], proposals=None)
        for asset in documents["models"]["models"]:
            # verify-model can launch a hashing job. Polls use strictly read-only
            # verbs and never claim verification from the manifest alone.
            asset["verification"] = asset["presence"]
            _missing(asset, ("verification", "revision", "quant"), "verification or manifest field unavailable")
        documents["models"]["why"]["proposals"] = "later phase 2.5 · R17 / TM-R15"
        documents["profiles"]["profiles"] = self._profiles(cluster, statuses)
        documents["stats"] = self._statistics(statuses, cluster)
        for profile in documents["profiles"]["profiles"]:
            trellis = list(dict.fromkeys(((item.get("status") or {}).get("memory") or {}).get("trellis_identity")
                                        for item in documents["stats"]["engines"] if item["profile"] == profile["id"]
                                        and ((item.get("status") or {}).get("memory") or {}).get("trellis_identity")))
            if trellis:
                profile["identity"] = {**(profile.get("identity") or {}), "trellis": trellis}
            for machine in machines:
                if machine["id"] in (profile["nodes"] or []) and profile["release"]:
                    machine["release"] = profile["release"]
                    machine["why"].pop("release", None)
                location = profile["router"]
                if profile["state"] in ("running", "starting") and (machine["id"] == location or location == "local" and machine["access"] == "local"):
                    machine["roles"].append({"profile": profile["id"], "role": "router"})
        if status_error:
            for field in ("engines", "routers", "recent_requests"):
                documents["stats"][field] = None
                documents["stats"]["why"][field] = status_error
        documents["logs"] = self._logs(statuses)
        trusted = []
        try:
            lines = Path(self.orchestrate._known_hosts()).read_text(encoding="utf-8").splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) == 3 and not line.startswith("#"):
                    key = self.remote._hostkey(parts[1], parts[2])
                    aliases = parts[0].split(",")
                    mid = next((mid for mid, machine in cluster.machines.items()
                                if (mid if machine.ssh_port == 22 else f"[{mid}]:{machine.ssh_port}") in aliases), parts[0])
                    trusted.append({"machine": mid, "algorithm": key.algorithm,
                                    "fingerprint": key.sha256_fingerprint, "paired_at": None,
                                    "why": {"paired_at": "pin timestamp unavailable"}})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            documents["settings"]["why"]["trusted_machines"] = str(exc)
            trusted = None
        documents["settings"].update(trusted_machines=trusted, configuration=redact(cluster.raw), version=__version__,
                                       config_path=str(self.config_path), capabilities={**CAPABILITIES, **feature_registry.registry().capabilities},
                                       secrets={"storage": "DPAPI" if os.name == "nt" else "0600 files", "values": "never shown"},
                                       features={"loaded": list(feature_registry.registry().loaded),
                                                 "failed": dict(feature_registry.registry().failed)},
                                       lab_rules={"label_prefix": "hl-", "turn_cap_s": 1200, "drain_margin_s": 60,
                                                  "usb4_layout": 1, "excluded": "RTX 4090", "extended_turn": "explicit approver ACK required"})
        return documents

    def _logs(self, statuses):
        actions = self._action_log()
        entries = [{**action, "process": "portal", "message": json.dumps(action)} for action in actions]
        failures = [{"when": action["when"], "process": "portal", "event": "failed",
                     "reason": action["result"].get("error"), "bundle": action}
                    for action in actions if isinstance(action.get("result"), dict) and action["result"].get("state") == "failed"]
        reasons = {}
        for profile, status in statuses.items():
            processes = list((status.get("nodes") or {}).keys())
            if status.get("router"):
                processes.append("router")
            for process in processes:
                try:
                    logs = self.orchestrate.logs(profile, process, 100)
                    for stream in ("stdout", "stderr"):
                        for line in str(logs.get(stream) or "").splitlines():
                            message = redact(line)
                            event = next((event for event in ("POISONED", "READY", "STOPPED", "WARN") if event in message), None)
                            entries.append({"profile": profile, "process": process, "stream": stream,
                                            "message": message, "event": event, "when": None,
                                            "why": {"when": "timestamp unavailable unless present in captured output"}})
                except (OSError, ValueError, RuntimeError) as exc:
                    reasons[profile + "/" + process] = str(exc)
        return self._document("logs", entries=entries[-1000:], failures=failures,
                              why=reasons, failure_scope="portal failures from the persistent audit log")

    def read(self, screen, query=None):
        query = query or {}
        if screen == "jobs":
            with self._job_lock:
                return redact(self._document("jobs", jobs=[dict(job) for job in self._jobs.values()], active=self._active))
        if screen.startswith("jobs/"):
            with self._job_lock:
                job = self._jobs.get(screen[5:])
                if job is None:
                    raise APIError("unknown job", 404)
                return redact(dict(job))
        if screen not in SCREENS:
            handler, rest = feature_registry.registry().find_get(screen)
            if handler is None:
                raise APIError("unknown endpoint", 404)
            return redact(self._feature_call(handler, *(() if rest is None else (rest,)), query))
        # One shared snapshot for every screen and every concurrent tab.
        with self._poll_lock:
            if self._cached is None or self.clock() >= self._expires:
                self._cached = self._snapshot()
                self._expires = self.clock() + POLL_INTERVAL
            document = self._cached[screen]
            if screen == "logs" and query.get("profile"):
                key = (query["profile"], query.get("process", "router"), int(query.get("tail", 100)))
                if not 1 <= key[2] <= 65536:
                    raise APIError("tail must be in 1..65536")
                cache_key = "logs/" + repr(key)
                if cache_key not in self._cached:
                    self._cached[cache_key] = self._document("logs", entries=redact(self.orchestrate.logs(*key)), failures=None,
                                                           why={"failures": "persistent process failure index unavailable"})
                document = self._cached[cache_key]
            return redact(document)

    # Feature modules (hyperloom/web/features): helpers they use and the dispatch of their endpoints.
    def document(self, name, **fields):
        return self._document(name, **fields)

    def snapshot(self):
        """The shared 2 s snapshot of the core screens (the same one every core GET reads)."""
        with self._poll_lock:
            if self._cached is None or self.clock() >= self._expires:
                self._cached = self._snapshot()
                self._expires = self.clock() + POLL_INTERVAL
            return self._cached

    def cached_snapshot(self):
        """The last core snapshot if one exists (never builds one: no node calls); None otherwise."""
        with self._poll_lock:
            return self._cached

    def cluster(self):
        return self._cluster()

    def node(self, machine, verb, payload=None):
        """Run a node verb on a configured machine (id or Machine)."""
        if isinstance(machine, str):
            machines = self._cluster().machines
            if not isinstance(machines, dict):
                machines = {m.id: m for m in machines}
            if machine not in machines:
                raise LookupError("unknown machine " + machine)
            machine = machines[machine]
        return self._node(machine, verb, payload)

    def audit(self, action, arguments, result):
        self._audit(action, arguments, result)

    def _feature_call(self, handler, *args):
        try:
            return handler(self, *args)
        except APIError:
            raise
        except LookupError as exc:
            raise APIError(str(exc).strip("'\""), 404) from exc
        except PermissionError as exc:
            raise APIError(str(exc), 409) from exc
        except ValueError as exc:
            raise APIError(str(exc)) from exc

    def feature_post(self, action, arguments):
        """Synchronous feature POST: (status, document)."""
        registry = feature_registry.registry()
        handler = registry.posts.get(action)
        if handler is None:
            raise APIError("unknown mutation", 404)
        try:
            document = self._feature_call(handler, arguments)
        except APIError as exc:
            if action in registry.audited:
                self._audit(action, arguments, {"state": "refused", "error": str(exc)})
            raise
        if action in registry.audited:
            self._audit(action, arguments, {"state": "succeeded", "result": document})
        return 200, redact(document)

    def _admission(self):
        if self.admission_db is None:
            self.admission_db = admission.for_config(self.config_path)
        return self.admission_db

    def _resources_of_profile(self, profile):
        """Resource ids a profile occupies (its nodes, its prefill service's machine and its router); everything
        ("*") when unknown."""
        try:
            return admission.profile_resources(self._cluster(), profile, self.orchestrate)
        except Exception:
            return {"*"}

    def _renew_holds(self):
        """Heartbeat: renew the admission hold of every job of this process until the job releases it after its
        own cleanup (cancelled or abandoned jobs included: their resources stay owned while they clean up).
        Abandonment is detected by the job context, separately; it cancels the job, it never drops the hold."""
        while True:
            time.sleep(HOLD_RENEW_S)
            with self._job_lock:
                holds = dict(self._holds)
                contexts = dict(self._features_running)
            for jid, hold in holds.items():
                try:
                    context = contexts.get(jid)
                    if context is not None:
                        context.cancelled()     # notices abandonment / stops / deadline promptly
                except Exception:  # noqa: BLE001 - one failing check never stops the renewals
                    pass
                try:
                    self._admission().renew(hold)
                except Exception:
                    pass

    def _hold(self, jid, kind, resources, label):
        """Register an admission hold for a job (raises APIError 409 when refused)."""
        try:
            hold = self._admission().acquire(kind, resources, label=label)
        except admission.Refused as exc:
            raise APIError(str(exc), 409) from exc
        try:
            with self._heartbeat_lock:
                if self._heartbeat is None:
                    heartbeat = threading.Thread(target=self._renew_holds, name="hyperloom-admission", daemon=True)
                    heartbeat.start()           # raises: nothing published, the next hold tries again
                    self._heartbeat = heartbeat
                with self._job_lock:
                    self._holds[jid] = hold
        except Exception:
            try:
                self._admission().release(hold)
            except Exception:  # noqa: BLE001 - its lease then lapses: nobody renews it
                pass
            raise
        return hold

    def _unhold(self, jid):
        with self._job_lock:
            hold = self._holds.pop(jid, None)
        if hold:
            try:
                self._admission().release(hold)
            except Exception:
                pass

    def renew_jobs(self, arguments):
        """POST jobs/renew {jobs: [id, ...]}: the portal page that started these jobs is still open."""
        ids = arguments.get("jobs") if isinstance(arguments, dict) and set(arguments) == {"jobs"} else None
        if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(i, str) for i in ids):
            raise APIError("jobs must be a list of at most 32 job ids")
        renewed, finished = [], []
        with self._job_lock:
            for jid in ids:
                context = self._features_running.get(jid)
                if context is not None:
                    context.renew_owner()
                    renewed.append(jid)
                else:
                    finished.append(jid)    # unknown or no longer running: the page may stop renewing it
        return 200, {"renewed": renewed, "not_running": finished}

    def _feature_job(self, action, arguments):
        spec = feature_registry.registry().jobs[action]
        validated = None     # the validated arguments: only these are ever rolled back (never the client's own)
        try:
            if spec.validate is not None:
                arguments = validated = self._feature_call(spec.validate, arguments)
            resources = set()
            if spec.admission or (spec.exclusive and spec.resources is not None):
                resources = {str(r) for r in self._feature_call(spec.resources, arguments)}
                if not resources:
                    raise APIError("an admitted job must name the resources it uses")
            elif spec.exclusive:
                resources = {"*"}
        except APIError as exc:
            self._rollback(spec, validated)
            self._audit(action, arguments, {"state": "refused", "error": str(exc)})
            raise
        except Exception:
            self._rollback(spec, validated)
            raise
        jid = uuid.uuid4().hex
        with self._job_lock:
            refusal = None
            if spec.exclusive and self._active:
                refusal = "another mutating job is running"
            elif spec.admission and self._active:
                # The slot may be reserved by a job not yet listed (its admission is being decided).
                running = (self._jobs.get(self._active) or {}).get("action") or "starting"
                refusal = "a deploy, stop or install job is running (" + str(running) + ")"
            if not refusal and spec.exclusive:
                self._active = jid      # reserve the slot while the shared admission decides
        if refusal:
            self._rollback(spec, validated)
            self._audit(action, arguments, {"state": "refused", "error": refusal})
            raise APIError(refusal, 409)
        hold = None
        try:
            if spec.admission or spec.exclusive:
                hold = self._hold(jid, "bench" if spec.admission else "core", resources, action + " " + jid[:8])
            context = JobContext(spec.deadline_s, self._admission() if hold else None, hold,
                                 owned=bool(spec.admission))
        except Exception as exc:
            self._unhold(jid)
            with self._job_lock:
                if self._active == jid:
                    self._active = None
            self._rollback(spec, validated)
            error = exc if isinstance(exc, APIError) else APIError(f"admission unavailable: {exc}", 503)
            self._audit(action, arguments, {"state": "refused", "error": str(error)})
            raise error from exc
        with self._job_lock:
            job = {"job": jid, "id": jid, "action": action, "arguments": redact(arguments),
                   "state": "running", "started_at": self.clock(), "finished_at": None,
                   "result": None, "error": None, "progress": "starting",
                   "resources": sorted(resources) if hold else None,
                   "renew": "POST jobs/renew while the page is open" if spec.admission else None}
            accepted = self._accepted(action, arguments, jid)
            if accepted is None:
                self._jobs[jid] = job
                self._features_running[jid] = context
            for old in list(self._jobs):
                if len(self._jobs) <= 100:
                    break
                if self._jobs[old]["state"] != "running":
                    del self._jobs[old]
            response = dict(job)
        if accepted is not None:
            self._rollback(spec, validated)
            self._unaccept(jid, accepted)

        def progress(text):
            with self._job_lock:
                job["progress"] = redact(str(text))[:500]
        context.report = progress

        def run():
            try:
                result = spec.run(self, arguments, context)
                update = {"state": "cancelled" if context.cancelled() and result is None else "succeeded",
                          "result": redact(result), "error": redact(context.reason) if context.cancelled() else None}
            except JobFailed as exc:
                update = {"state": "failed", "result": None, "error": redact(str(exc))}
                if exc.outcome is not None:
                    outcome = exc.outcome if isinstance(exc.outcome, str) else ""
                    update["outcome"] = outcome if JOB_OUTCOME.fullmatch(outcome) else "invalid"
            except Exception as exc:
                update = {"state": "cancelled" if context.cancelled() else "failed", "result": None,
                          "error": redact(str(exc))}
            # The hold is released only after the job's own cleanup returned.
            self._unhold(jid)
            try:
                self._audit(action, arguments, {"job": jid, **update})
            except OSError as exc:
                update = {"state": "failed", "result": update["result"],
                          "error": redact("audit write failed: " + str(exc)),
                          **({"outcome": update["outcome"]} if "outcome" in update else {})}
            with self._job_lock:
                job.update(update, finished_at=self.clock(), progress="finished")
                if self._active == jid:
                    self._active = None
                self._features_running.pop(jid, None)
        self._start_worker(run, "hyperloom-feature-" + jid[:8], jid, job, action, arguments,
                           lambda: self._rollback(spec, validated))
        return response

    def _action_log(self):
        try:
            with self.audit_path.open("rb") as stream:
                stream.seek(0, 2)
                offset = max(0, stream.tell() - 65536)
                stream.seek(offset)
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            if offset:
                lines = lines[1:]
            return [redact(json.loads(line)) for line in lines[-100:] if line.strip()]
        except FileNotFoundError:
            return []

    def _audit(self, action, arguments, result):
        entry = {"when": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 "action": action, "arguments": redact(arguments), "result": redact(result)}
        with self._audit_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(entry, ensure_ascii=True, allow_nan=False) + "\n")

    def _arguments(self, action, arguments):
        if action in recipe_web.JOBS:
            return self._recipe_job_arguments(action, arguments)
        allowed = {"up": {"profile", "config", "router", "node", "device", "deadline", "approval_ref", "approval_lease", "approval_end",
                          "recipe", "recipe_set", "recipe_workload", "recipe_overrides", "recipe_profile", "recipe_inputs"},
                   "down": {"profile", "force"}, "install": {"profile", "config", "build_on"},
                   "hosts/pin": {"machine", "config", "fingerprint"}, "doctor": {"machine", "config"},
                   "keys/rotate": {"name"}, "cancel": {"job", "profile"}}
        if action not in allowed:
            raise APIError("unknown mutation", 404)
        if not isinstance(arguments, dict) or set(arguments) - allowed[action]:
            raise APIError("unknown arguments")
        arguments = dict(arguments)
        required = {"up": None if arguments.get("recipe") or arguments.get("recipe_inputs") else "profile",
                    "down": "profile", "install": "profile", "hosts/pin": "machine", "keys/rotate": "name"}.get(action)
        if required and (not isinstance(arguments.get(required), str) or not arguments[required]):
            raise APIError(required + " is required")
        for key, value in arguments.items():
            if key in ("node", "device", "recipe_set", "recipe_overrides"):
                if not isinstance(value, list) or not value or len(value) > 256 or any(
                        not isinstance(item, str) or not item or item.startswith("-") for item in value):
                    raise APIError(key + " must be a nonempty array of strings")
            elif key == "force":
                if type(value) is not bool:
                    raise APIError("force must be boolean")
            elif key in ("deadline", "approval_lease"):
                if type(value) is not int or value <= 0:
                    raise APIError(key + " must be a positive integer")
            elif not isinstance(value, str) or not value or value.startswith("-"):
                # Every string becomes a CLI argument: one starting with '-' would be read as an option.
                raise APIError(key + " must be a nonempty string not starting with '-'")
            elif key in ("profile", "machine", "build_on", "router") and not IDENTIFIER.fullmatch(value):
                raise APIError(f"{key} must be an id ([A-Za-z0-9][A-Za-z0-9_.-]{{0,63}})")
        if "config" in arguments:
            # Every mutation runs on the portal's own configuration: a configuration file is never named by the
            # client (any local user can read the CSRF token from the page; a named file could send node keys to the
            # machines it lists, or pin and probe them).
            if Path(arguments["config"]).absolute() != Path(self.config_path).absolute():
                raise APIError(f"{action} runs on the portal's configuration: drop config")
            del arguments["config"]
        if action == "hosts/pin" and not arguments.get("fingerprint"):
            raise APIError("fingerprint is required; compare on the machine")
        if action == "cancel" and not (arguments.get("job") or arguments.get("profile")):
            raise APIError("cancel requires job or profile")
        if action == "cancel" and arguments.get("job") and arguments.get("profile"):
            # The job names what is stopped (and so the stop's admission resources): never a second profile.
            raise APIError("cancel takes job or profile, not both")
        if action == "cancel" and arguments.get("job"):
            with self._job_lock:
                job = self._jobs.get(arguments["job"])
                job = dict(job) if job else None
                feature = arguments["job"] in self._features_running     # a running feature job: its context
            launch = bool(job) and job["action"] in ("up", "recipes/deploy")
            if job and job["state"] != "running" and (launch or job["action"] in feature_registry.registry().jobs):
                # A known job that finished (a launch, or a feature job whose context is gone): 409 at once.
                raise APIError(f"job {job['job']} has already {job['state']}" +
                               (": use down with a profile" if launch else ""), 409)
            if not job or (not launch and not feature):
                raise APIError("only an up or a recipes/deploy job can be cancelled; use down with a profile")
        if action == "up" and (arguments.get("recipe") or arguments.get("recipe_inputs")):
            try:
                self._recipe_up_argv(arguments)
                if not arguments.get("profile"):
                    # Bound now, so the job names the profile it launches (its cancel stops that profile).
                    profile = self._recipe_up_profile(arguments)
                    if profile:
                        arguments["profile"] = profile
            except (ValueError, OSError) as exc:
                raise APIError(str(exc)) from exc
        return arguments

    def _recipe_up_profile(self, args):
        """The profile an up with a recipe and no profile launches: the snapshot's, or the only profile serving the
        recipe's model (as `hyperloom up --recipe ID` decides it); None for auto (the CLI asks for a profile)."""
        from hyperloom.recipes import service
        path = Path(self.config_path)
        with service.configured(path):
            if args.get("recipe_inputs"):
                return service.load_inputs(args["recipe_inputs"])[1]["intent"]["profile"]
            if args.get("recipe") == "auto":
                return None
            cluster = self.orchestrate.load_cluster(path, config)
            return service.deployment_profile(cluster, service.locate(args["recipe"], cluster, for_up=True))

    def _recipe_job_arguments(self, action, arguments):
        from hyperloom.recipes import service
        try:
            with service.configured(self.config_path):
                if action == "recipes/probe":
                    service.argv("probe", arguments, config=self.config_path)
                elif "_job" in arguments:
                    raise ValueError("unknown arguments: _job")
                else:
                    self._deploy_argv(arguments)
        except ValueError as exc:
            raise APIError(str(exc)) from exc
        return dict(arguments)

    def _deploy_argv(self, args, job=None):
        from hyperloom.recipes import service
        allowed = {"inputs", "stop", "approval_ref", "approval_lease", "approval_end", "_job"}   # _job: set by mutate
        if not isinstance(args, dict) or set(args) - allowed:
            raise ValueError("unknown arguments")
        argv = service.argv("deploy", {**{k: args[k] for k in ("inputs", "stop") if k in args},
                                       **({"job": job} if job else {})}, config=self.config_path)
        for key, flag in (("approval_ref", "--approval-ref"), ("approval_lease", "--approval-lease"),
                          ("approval_end", "--approval-end")):
            if key in args:
                if key == "approval_lease" and (type(args[key]) is not int or args[key] <= 0):
                    raise ValueError("approval_lease must be a positive integer")
                if key != "approval_lease" and (not isinstance(args[key], str) or not args[key] or args[key].startswith("-")):
                    raise ValueError(key + " must be a nonempty string")
                argv[-2:-2] = [flag, str(args[key])]
        return argv

    def _recipe_up_argv(self, args):
        """The `hyperloom up` argv of an API up with a recipe (layer files are names in the user layers directory)."""
        from hyperloom.recipes import service
        with service.configured(Path(self.config_path)):    # the layers of the config up uses
            return self._recipe_up_argv_in(args, service)

    def _recipe_up_argv_in(self, args, service):
        argv = ["up"] + ([args["profile"]] if args.get("profile") else [])
        argv += ["--config", str(Path(self.config_path)), "--router", args.get("router", "local"),
                 "--deadline", str(args.get("deadline", 1140))]
        # Every accepted argument goes to the CLI, which refuses the combinations it does not allow.
        for key, flag in (("node", "--node"), ("device", "--device")):
            if args.get(key):
                argv += [flag, *args[key]]
        # Recipe references are ids (or auto) and snapshot digests: never a path from the client.
        if args.get("recipe_inputs"):
            if not service.DIGEST.fullmatch(str(args["recipe_inputs"])):
                raise ValueError("recipe_inputs must be a 64-hex snapshot digest")
            argv += ["--recipe-inputs", args["recipe_inputs"]]
        if args.get("recipe"):
            argv += ["--recipe", service.recipe_ref("recipe", args["recipe"], auto=True)]
        for item in args.get("recipe_set") or []:
            argv += ["--recipe-set", item]
        for item in args.get("recipe_overrides") or []:
            argv += ["--recipe-overrides", str(service._named("recipe_overrides", item, service.layers_dir(),
                                                              service._suffix_of(item)))]
        if args.get("recipe_profile"):
            argv += ["--recipe-profile", str(service._named("recipe_profile", args["recipe_profile"],
                                                            service.layers_dir(), service._suffix_of(args["recipe_profile"])))]
        if args.get("recipe_workload"):
            argv += ["--recipe-workload", args["recipe_workload"]]
        for key, flag in (("approval_ref", "--approval-ref"), ("approval_lease", "--approval-lease"),
                          ("approval_end", "--approval-end")):
            if key in args:
                argv += [flag, str(args[key])]
        if args.get("_job"):
            argv += ["--job", args["_job"]]            # set by mutate: the job's cancel marker
        return argv

    def _cli_argv(self, action, args):
        if action == "up" and (args.get("recipe") or args.get("recipe_inputs")):
            return [sys.executable, "-m", "hyperloom", *self._recipe_up_argv(args)]
        if action == "recipes/deploy":
            return [sys.executable, "-m", "hyperloom", *self._deploy_argv(args, job=args.get("_job"))]
        argv = [sys.executable, "-m", "hyperloom", action, args["profile"]]
        if action in ("up", "install"):
            argv += ["--config", str(Path(self.config_path))]
        if action == "install" and args.get("build_on"):
            argv += ["--build-on", args["build_on"]]
        if action == "down" and args.get("force"):
            argv += ["--force"]
        if action == "up":
            argv += ["--router", args.get("router", "local"), "--deadline", str(args.get("deadline", 1140))]
            if args.get("node"):
                argv += ["--node", *args["node"]]
            if args.get("device"):
                argv += ["--device", *args["device"]]
            for key, flag in (("approval_ref", "--approval-ref"), ("approval_lease", "--approval-lease"),
                              ("approval_end", "--approval-end")):
                if key in args:
                    argv += [flag, str(args[key])]
            if args.get("_job"):
                argv += ["--job", args["_job"]]        # set by mutate: the job's cancel marker
        return argv

    def _run_detached(self, action, args):
        """Run one CLI mutation in its own detached process and return its JSON result."""
        # Own hidden console (CREATE_NO_WINDOW), not a console-less detached process: every ssh/scp such a process
        # starts would open a visible window. Break away from the portal's job so the CLI job
        # survives the portal.
        flags = {"start_new_session": True} if os.name != "nt" else no_window(
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
        argv = self._cli_argv(action, args)
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **flags)
        except PermissionError:
            # The portal runs inside a job object that forbids breakaway; detach without it.
            flags["creationflags"] &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **flags)
        out, err = child.communicate()
        if child.returncode != 0:
            # A recipe launch's refusal is the CLI's whole explanation; other actions keep their short tail.
            tail = 16384 if action == "recipes/deploy" or (action == "up" and (
                args.get("recipe") or args.get("recipe_inputs"))) else 400
            raise APIError((err or out).decode("utf-8", "replace").strip()[-tail:] or f"{action} failed", 500)
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError:
            return {"output": out.decode("utf-8", "replace")[-2000:]}

    def _execute(self, action, args):
        if action in ("down", "cancel") and not args.get("job"):
            # Any stop (detached or in process) while a switch runs cancels the switch too: it would otherwise go
            # on and launch. So does a stop of the profile an up (with a recipe or without) is launching: up would
            # otherwise clear the profile's stop marker when it takes the profile. Only their markers: this stop
            # names its own profile, and a launch's target is stopped only when that is the profile named here (by
            # this stop itself).
            with self._job_lock:
                switches = [dict(j) for j in self._jobs.values() if j["state"] == "running" and (
                    j["action"] == "recipes/deploy" or j["action"] == "up"
                    and j["arguments"].get("profile") == args.get("profile"))]
            cancels = []
            for switch in switches:
                try:
                    cancels.append(self._cancel_deploy(switch, stop_target=False))
                except (APIError, OSError, ValueError) as exc:         # the stop itself still runs
                    cancels.append({"cancelled": switch["job"], "error": str(exc)})
            result = self._execute_action(action, args)
            return dict(result, cancelled_switches=cancels) if cancels and isinstance(result, dict) else result
        return self._execute_action(action, args)

    def _execute_action(self, action, args):
        recipe_up = action == "up" and bool(args.get("recipe") or args.get("recipe_inputs"))
        if action == "recipes/probe":
            from hyperloom.recipes import service
            with service.configured(self.config_path):
                argv = service.argv("probe", args, config=self.config_path)
            status, document = recipe_web.document("probe", argv, self.recipes.run(
                argv, deadline_s=recipe_web.HARDWARE_DEADLINE_S), now=self.clock())
            if not document["ok"]:
                raise APIError(document["error"] or "probe failed", status)
            return document
        if action == "recipes/deploy" and self.detached:
            return self._run_detached(action, args)
        if action == "recipes/deploy" or recipe_up:
            # In process (tests): the CLI's parser and the very function the CLI runs.
            argv = (self._deploy_argv(args, job=args.get("_job")) if action == "recipes/deploy"
                    else self._recipe_up_argv(args))
            parsed = cli.build_parser(recipe_web.RaisingParser).parse_args(argv)
            if self.detached:
                return self._run_detached(action, args)
            if action == "up":
                return cli.up_command(parsed, orchestrate=self.orchestrate, load_cluster=self._recipe_loader)
            from hyperloom.recipes import commands, service
            with service.configured(parsed.config):
                return service.deploy(parsed.inputs, parsed.stop, orchestrate=self.orchestrate,
                                      approval=commands._approval(parsed), job=parsed.job)
        if self.detached and action in ("up", "down", "install"):
            if action in ("up", "install"):
                cluster = self.orchestrate.load_cluster(Path(self.config_path), config)
                _, kind, _ = self.orchestrate._profile(cluster, args["profile"])
                if engines.adapter(kind).topology != "replicas":
                    raise APIError("later phase · only replica profiles are available in Phase 1b")
            return self._run_detached(action, args)
        if action == "cancel":
            profile = args.get("profile")
            if args.get("job"):
                with self._job_lock:
                    job = self._jobs.get(args["job"])
                    context = self._features_running.get(args["job"])
                    job = dict(job) if job else None
                if context is not None:
                    context.cancel("cancelled by request")
                    return {"cancelled": args["job"]}
                if job and job["state"] != "running" and job["action"] in feature_registry.registry().jobs:
                    raise APIError(f"job {job['job']} has already {job['state']}", 409)
                if not job or job["action"] not in ("up", "recipes/deploy"):
                    raise APIError("only an up or a recipes/deploy job can be cancelled; use down with a profile")
                if job["state"] != "running":
                    raise APIError(f"job {job['job']} has already {job['state']}: use down with a profile", 409)
                # Every up (with a recipe or without) and every deploy: only what the job did is undone.
                return self._cancel_deploy(job)
            return self.orchestrate.down(profile)
        if action == "down":
            return self.orchestrate.down(args["profile"], force=args.get("force", False))
        if action == "keys/rotate":
            secrets.rotate(args["name"])
            return {"name": args["name"], "rotated": True, "restart_required": True}
        if action == "doctor" and "machine" not in args:
            from types import SimpleNamespace
            return self._node(SimpleNamespace(access="local"), "doctor")
        cluster = self.orchestrate.load_cluster(Path(self.config_path), config)
        if action in ("up", "install"):
            _, kind, mids = self.orchestrate._profile(cluster, args["profile"])
            if engines.adapter(kind).topology != "replicas":
                raise APIError("later phase · only replica profiles are available in Phase 1b")
        if action == "install":
            release = self.orchestrate.install(cluster, args.get("build_on") or mids[0],
                                               list(engines.adapter(kind).targets), profile=args["profile"])
            return {"release_id": release}
        if action == "up":
            supplied = [key in args for key in ("approval_ref", "approval_lease", "approval_end")]
            approval = None
            if any(supplied):
                if not all(supplied):
                    raise APIError("approval requires approval_ref, approval_lease, approval_end")
                end = datetime.fromisoformat(args["approval_end"].replace("Z", "+00:00"))
                if end.tzinfo is None:
                    raise APIError("approval_end must include a timezone")
                approval = {"ref": args["approval_ref"], "lease_s": args["approval_lease"], "end_epoch": end.timestamp()}
            launch = lambda cancelled=None: self.orchestrate.up(  # noqa: E731
                cluster, args["profile"], router=args.get("router", "local"), nodes=args.get("node"),
                devices=args.get("device"), deadline_s=args.get("deadline", 1140), approval=approval,
                **({"cancelled": cancelled} if cancelled else {}))
            if not args.get("_job"):
                return launch()
            # The job's cancel marker is checked right after up owns the profile (the hand-over), as `up --job`
            # does: a cancel before it starts nothing and never stops a profile that was running already.
            from hyperloom.recipes import service
            with service.configured(self.config_path), \
                    service.launch_progress(args["_job"], "up", args["profile"]) as progress:
                return launch(progress.handoff)
        mid = args.get("machine")
        if action == "doctor":
            if mid not in cluster.machines:
                raise APIError("unknown machine: " + mid)
            return self._node(cluster.machines[mid], "doctor")
        if mid not in cluster.machines:
            raise APIError("unknown machine: " + mid)
        machine = cluster.machines[mid]
        candidates = self.remote.keyscan(machine.http_address, machine.ssh_port)
        match = next((key for key in candidates if key.sha256_fingerprint == args["fingerprint"]), None)
        if match is None:
            raise APIError("no scanned host key has that fingerprint; nothing pinned")
        self.remote.KnownHosts(self.orchestrate._known_hosts()).pin(mid, machine.http_address, machine.ssh_port, match)
        return {"machine": mid, "pinned": match.sha256_fingerprint, "algorithm": match.algorithm}

    def _cancel_deploy(self, job, *, stop_target=True):
        """Cancel a launch job (a switch, or an up with a recipe or without), undoing only what it did. The marker is
        written first; then the job's progress says how far it got. Before the hand-over (checks, stopping) the
        marker is all: the deploy stops at its next step and up, at the hand-over, starts nothing; the profiles it
        already stopped stay stopped (the answer names them), and a profile that was running before the job (an up
        refuses it) is never stopped. Once up owns the target ("launching", "done"), the target is stopped
        (``stop_target``), as it is the profile this job started."""
        from hyperloom.recipes import service
        with service.configured(self.config_path):
            marker = service.cancel_marker(job["job"])
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError as exc:
                raise APIError(f"the cancel marker could not be written ({exc}): nothing was cancelled; stop the "
                               "profile with down", 500) from exc
            try:
                progress = service.read_progress(job["job"]) or {"phase": "not started", "profile": None,
                                                                 "stopped": [], "stopping": None}
            except service.RecipeError as exc:
                # Unknown progress: never reported as "nothing to undo". The marker holds the deploy before its next
                # step and up at the hand-over; a launch already under way is the caller's to stop.
                raise APIError(f"{exc}. The cancel marker is written: the deploy stops before its next step and "
                               f"starts nothing after it; if its launch had begun, stop that profile with down", 500)
        down = None
        if stop_target and service.launch_began(progress) and progress.get("profile"):
            down = self.orchestrate.down(progress["profile"])
        return {"cancelled": job["job"], "phase": progress.get("phase"), "profile": progress.get("profile"),
                "stopped_by_deploy": progress.get("stopped") or [], "stopping": progress.get("stopping"),
                "down": down, "summary": service.cancel_summary(progress, down=down)}

    def _rollback(self, spec, arguments):
        """A validated feature job that does not start: its validation's reservations are undone."""
        if spec.rollback is not None and isinstance(arguments, dict):    # None: nothing was validated
            try:
                self._feature_call(spec.rollback, arguments)
            except Exception:  # noqa: BLE001 - the refusal is the answer; a failing rollback must not replace it
                pass

    def _accepted(self, action, arguments, jid):
        """Write a job's "accepted" audit entry (under the job lock); the error when it could not be written: the job
        does not start, and the caller releases what it reserved (``_unaccept``) once the lock is released."""
        try:
            self._audit(action, arguments, {"job": jid, "state": "accepted"})
            return None
        except Exception as exc:  # noqa: BLE001 - re-raised by _unaccept after the cleanup
            if self._active == jid:
                self._active = None
            return exc

    def _unaccept(self, jid, error):
        """A job whose acceptance could not be audited: its admission hold is released, nothing runs."""
        self._unhold(jid)
        raise APIError("audit write failed: " + str(error) + " (the job was not started)", 503) from error

    def mutate(self, action, arguments):
        if action not in MUTATIONS and action in feature_registry.registry().jobs:
            return self._feature_job(action, arguments)
        target = None       # a cancel's job, read once: whether the cancel is a stop never changes afterwards
        if action == "cancel" and isinstance(arguments, dict) and isinstance(arguments.get("job"), str):
            with self._job_lock:
                listed = self._jobs.get(arguments["job"])
                target = None if listed is None else (dict(listed), arguments["job"] in self._features_running or
                                                      listed["action"] in feature_registry.registry().jobs)
        try:
            arguments = self._arguments(action, arguments)
        except APIError as exc:
            self._audit(action, arguments, {"state": "refused", "error": str(exc)})
            raise
        priority = action in ("down", "cancel")
        occupying = action in ("up", "install", "recipes/deploy", "recipes/probe")
        # A stop: down, and every cancel that is not a feature job's (a profile's cancel and an up's or a deploy's
        # cancel stop a profile, or may).
        stopping, profile = action == "down", arguments.get("profile")
        if action == "cancel":
            # A feature job's cancel (running, finished or since evicted) only cancels its context: never a stop.
            job, feature = target if target is not None else (None, False)
            if arguments.get("job") and target is None:
                feature = True          # not listed when read: nothing to stop (the cancel itself answers why)
            if job is not None and not profile:
                profile = job["arguments"].get("profile")
            stopping = not feature
        resources = set()
        if occupying or stopping:
            resources = self._resources_of_profile(profile) if isinstance(profile, str) and profile else {"*"}
            if action == "up" and arguments.get("recipe_inputs"):
                resources = {"*"}       # a snapshot launches with the placement it was planned on: hold everything
        jid = uuid.uuid4().hex
        with self._job_lock:
            if self._active and not priority:
                self._audit(action, arguments, {"state": "refused", "error": "another mutating job is running"})
                raise APIError("another mutating job is running", 409)
            if not priority:
                self._active = jid      # reserved while the shared admission decides
        if occupying or stopping:
            # A stop is never refused: registering it cancels overlapping benchmarks (in every process) and its
            # hold keeps new ones out until it finishes. A deploy is refused while a benchmark holds its resources.
            try:
                self._hold(jid, "stop" if stopping else "core", resources, action + " " + str(profile or ""))
            except Exception as exc:
                with self._job_lock:
                    if self._active == jid:
                        self._active = None
                error = exc if isinstance(exc, APIError) else APIError(f"admission unavailable: {exc}", 503)
                self._audit(action, arguments, {"state": "refused", "error": str(error)})
                raise error from exc
            if stopping:
                with self._job_lock:
                    contexts = [self._features_running[j] for j in self._holds if j in self._features_running]
                for context in contexts:
                    try:
                        context.cancelled()     # observe the stop's cancellation now, not at the next poll
                    except Exception:  # noqa: BLE001 - the benchmark sees it at its own next check
                        pass
        with self._job_lock:
            job = {"job": jid, "id": jid, "action": action, "arguments": redact(arguments),
                   "state": "running", "started_at": self.clock(), "finished_at": None,
                   "result": None, "error": None, "progress": "calling CLI service"}
            accepted = self._accepted(action, arguments, jid)
            if accepted is None:
                self._jobs[jid] = job
            # Keep completed jobs bounded; never evict an operation still running.
            for old in list(self._jobs):
                if len(self._jobs) <= 100:
                    break
                if self._jobs[old]["state"] != "running":
                    del self._jobs[old]
            response = dict(job)
        if accepted is not None:
            self._unaccept(jid, accepted)
        if action in ("recipes/deploy", "up"):
            arguments = dict(arguments, _job=jid)    # the launch's cancel marker is named by its job
        def run():
            try:
                result = self._execute(action, arguments)
                update = {"state": "succeeded", "result": redact(result), "error": None}
            except Exception as exc:
                update = {"state": "failed", "result": None, "error": redact(str(exc))}
            self._unhold(jid)
            try:
                self._audit(action, arguments, {"job": jid, **update})
            except OSError as exc:
                update = {"state": "failed", "result": update["result"], "error": redact("audit write failed: " + str(exc))}
            with self._job_lock:
                job.update(update, finished_at=self.clock(), progress="finished")
                if self._active == jid:
                    self._active = None
            with self._poll_lock:
                self._expires = 0
        self._start_worker(run, "hyperloom-ui-" + jid[:8], jid, job, action, arguments)
        return response

    def _start_worker(self, run, name, jid, job, action, arguments, rollback=lambda: None):
        """Start an accepted job's worker; when no thread can be started, the job fails at once and everything it
        reserved (hold, slot, feature context, validation) is released, answered 503."""
        try:
            threading.Thread(target=run, name=name, daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
            self._unhold(jid)
            with self._job_lock:
                job.update(state="failed", error=redact("the job could not start: " + str(exc)),
                           finished_at=self.clock(), progress="finished")
                if self._active == jid:
                    self._active = None
                self._features_running.pop(jid, None)
            rollback()
            try:
                self._audit(action, arguments, {"job": jid, "state": "failed", "error": "the job could not start"})
            except OSError:
                pass
            raise APIError("the job could not start: " + str(exc), 503) from exc
