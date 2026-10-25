"""Fleet telemetry and the machine detail document (F2, F3; contracts C4, C5, C6).

Collectors (registered here, run by the portal's samplers):
    fleet-nodes    every 5 s: one ``host`` node verb per ssh machine and round (never two in flight for one machine);
                   the payload carries the CPU baseline of the previous answer and the loopback engines of that node
                   (C4: rank 0 / router members only), so one call per machine and round covers everything.
    fleet-control  every 2 s: the control machine itself (C6): nvidia-smi for its cards, memory and commit, CPU,
                   disks and network through read-only system calls (Linux: the node verb's own code in process).

Both record the shared ``node_*`` series (gauges and states directly; byte counters as deltas through the store's
persisted counter baselines, keyed by machine, series and label, with the boot id as the source instance) and keep
the latest answer of each machine in the store document namespace ``fleet/host`` (key: machine id). Loopback engine
answers go to ``fleet/engine`` (key ``<machine>:<port>``). Documents and the CLI read those; a GET never calls a node.

GET  machines/<id>       detail document (GPU hero, memory domains, processes, CPU, network, disks, engines, events,
                         and the history endpoints the page charts)
GET  fleet/thresholds    memory headroom thresholds (F2) and their defaults
POST fleet/thresholds/set {unified|rtx|commit: {amber_gib, red_gib}} (partial) or {reset: true}; audited
"""

import csv
import importlib.util
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from hyperloom import procs, secrets, telemetry
from hyperloom.adapters import engine as engine_adapter
from hyperloom.paths import repo_root
from hyperloom.telemetry.store import BASELINES as BASELINE_NAMESPACE
from hyperloom.winproc import no_window


NODE_INTERVAL = 5.0
CONTROL_INTERVAL = 2.0
ROUND_S = 12.0              # the transport's own timeout is 10 s
COUNTER_GAP_S = 30.0
NVIDIA_TIMEOUT_S = 5.0
NVIDIA_MAX_BYTES = 64 * 1024
GIB = 1024 ** 3
HOSTS, ENGINES, PREFS, THRESHOLDS_KEY = "fleet/host", "fleet/engine", "prefs", "fleet/thresholds"
# Status before and after the metrics read: the counter is accounted only when both name the same engine.
ENGINE_PATHS = ["/health", "/api/status?detail=1", "/metrics", "/api/status"]
CPU_CHIPS = ("k10temp", "coretemp", "zenpower", "cpu_thermal")
THROTTLE_LEGEND = {0: "none", 1: "power cap", 2: "thermal", 3: "other"}
UP_LEGEND = {0: "down", 1: "up"}

# name -> (kind, unit, legend)
SERIES = {
    "node_up": ("state", None, UP_LEGEND),
    "node_cpu_busy_percent": ("gauge", "%", None),
    "node_load1": ("gauge", None, None),
    "node_mem_available_bytes": ("gauge", "bytes", None),
    "node_mem_total_bytes": ("gauge", "bytes", None),
    "node_commit_available_bytes": ("gauge", "bytes", None),
    "node_gpu_busy_percent": ("gauge", "%", None),
    "node_gpu_temp_celsius": ("gauge", "celsius", None),
    "node_gpu_power_watts": ("gauge", "W", None),
    "node_gpu_power_limit_watts": ("gauge", "W", None),
    "node_gpu_clock_mhz": ("gauge", "MHz", None),
    "node_gpu_throttled": ("state", None, THROTTLE_LEGEND),
    "node_gpu_mem_used_bytes": ("gauge", "bytes", None),
    "node_gpu_mem_total_bytes": ("gauge", "bytes", None),
    "node_cpu_temp_celsius": ("gauge", "celsius", None),
    "node_cpu_power_watts": ("gauge", "W", None),
    "node_sensor_temp_celsius": ("gauge", "celsius", None),
    "node_disk_used_bytes": ("gauge", "bytes", None),
    "node_disk_total_bytes": ("gauge", "bytes", None),
    "node_disk_read_bytes": ("delta", "bytes", None),
    "node_disk_write_bytes": ("delta", "bytes", None),
    "node_net_rx_bytes": ("delta", "bytes", None),
    "node_net_tx_bytes": ("delta", "bytes", None),
}
DEFAULT_THRESHOLDS = {"unified": {"amber_gib": 8.0, "red_gib": 4.0},
                      "rtx": {"amber_gib": 2.0, "red_gib": 1.0},
                      "commit": {"amber_gib": 8.0, "red_gib": None}}
THRESHOLD_LABELS = {"unified": "unified node memory (MemAvailable and GPU-visible free)",
                    "rtx": "RTX card VRAM free", "commit": "Windows commit headroom"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _value(check):
    return check.get("value") if isinstance(check, dict) and check.get("status") == "ok" else None


def _why(check, fallback="not reported"):
    return check.get("why", fallback) if isinstance(check, dict) and check.get("status") != "ok" else fallback


# -- per-portal state ---------------------------------------------------------------------------------------------
class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.machine_locks = {}     # machine id -> Lock held while its node call is in flight
        self.cpu = {}               # machine id -> cpu baseline from its last answer
        self.windows = {}           # control-machine counters between rounds (CPU times)
        self.described = set()      # id(store) already described


_states = {}
_states_lock = threading.Lock()


def _state(api):
    with _states_lock:
        state = getattr(api, "_fleet_state", None)
        if state is None:
            state = _State()
            try:
                api._fleet_state = state
            except AttributeError:
                state = _states.setdefault(id(api), state)
        return state


def describe(api, store, state=None):
    state = state or _state(api)
    if id(store) in state.described:
        return
    for name, (kind, unit, legend) in SERIES.items():
        interval = CONTROL_INTERVAL if name == "node_commit_available_bytes" else NODE_INTERVAL
        store.describe(name, kind, unit=unit, interval=interval, legend=legend)
    store.ensure_table(RATES_DDL)
    state.described.add(id(store))


# -- launch records: roles and loopback engines (local files only) ------------------------------------------------
def launch_records(api):
    """{profile: launch record} of the profiles this control machine launched (local reads, no ssh)."""
    records = {}
    try:
        paths = sorted(secrets.runtime_dir().glob("up-*.json"))
    except OSError:
        return records
    for path in paths[:32]:
        try:
            record = api.orchestrate._read(path)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records[str(record.get("profile") or path.stem[3:])] = record
    return records


def _loopback(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except (TypeError, ValueError):
        return False


def loopback_engines(records):
    """{machine: [{profile, port, replica}]}: replicas the rendered router reaches on their own node's loopback (C4: only the
    router members, i.e. rank 0 of a pair, serve HTTP; followers are never polled)."""
    found = {}
    for profile, record in records.items():
        router = record.get("rendered_router") if isinstance(record, dict) else None
        for replica in (router or {}).get("replicas") or []:
            node, port = replica.get("node") or replica.get("id"), replica.get("port")
            if _loopback(replica.get("address")) and isinstance(node, str) and type(port) is int and 1024 <= port <= 65535:
                rows = found.setdefault(node, [])
                if all(row["port"] != port for row in rows) and len(rows) < 4:
                    started = record.get("started_at")
                    rows.append({"profile": profile, "port": port, "replica": str(replica.get("id") or node),
                                 "launch": f"{started:.3f}" if _finite(started) else None})
    return found


def roles(records, control_id):
    """{machine: [{profile, role}]} from the launch records: rank 0 / rank 1 / replica / prefill service / router."""
    found = {}
    for profile, record in records.items():
        family = ((record.get("launch") or {}).get("family")) if isinstance(record, dict) else None
        nodes = [n for n in record.get("nodes") or [] if isinstance(n, str)]
        pair = len({spec.get("role") for spec in record.get("process_specs") or [] if isinstance(spec, dict)} & {"rank0", "rank1"}) > 0
        if family:
            try:
                from hyperloom import engines
                pair = engines.adapter(family).topology == "pair"
            except (ValueError, KeyError):
                pass
        for index, node in enumerate(nodes):
            role = (f"rank {index}" if index < 2 else "member") if pair else "replica"
            found.setdefault(node, []).append({"profile": profile, "role": role, "of": nodes if pair else None})
        service = record.get("prefill_service")
        if isinstance(service, dict) and isinstance(service.get("machine"), str):
            found.setdefault(service["machine"], []).append({"profile": profile, "role": "prefill service"})
        router = record.get("router")
        if isinstance(router, dict):
            where = router.get("machine")
            where = control_id if where == "local" else where
            if isinstance(where, str):
                found.setdefault(where, []).append({"profile": profile, "role": "router"})
    return found


# -- node answers -> points ---------------------------------------------------------------------------------------
def _sensor(chip, label):
    return f"{chip or 'hwmon'}/{label}"


def sensor_names(chips):
    """{hwmon: sensor prefix}, deterministic per machine: the chip name when it occurs once; "<chip>@<device>" when
    the name repeats (the device link name is stable across boots, hwmon numbers are not); "<chip>@<hwmon>" when the
    device link is absent or itself repeats. The last form is not stable across boots: such sensors are shown in
    the documents but never recorded as series (see stable_prefix)."""
    names = {}
    for chip in chips:
        names.setdefault(chip.get("name") or "hwmon", []).append(chip)
    prefixes = {}
    for name, group in names.items():
        devices = [chip.get("device") for chip in group]
        for chip in group:
            device = chip.get("device")
            if len(group) == 1:
                prefixes[chip.get("hwmon")] = name
            elif device and devices.count(device) == 1:
                prefixes[chip.get("hwmon")] = f"{name}@{device}"
            else:
                prefixes[chip.get("hwmon")] = f"{name}@{chip.get('hwmon')}"
    return prefixes


def stable_prefix(prefix):
    return "@hwmon" not in prefix


def sensor_labels(entries):
    """[(entry, label)]: the sensor label, suffixed with its input name when two inputs of one chip share it."""
    labels = [str(entry.get("sensor")) for entry in entries]
    return [(entry, label if labels.count(label) == 1 else f"{label}#{entry.get('input') or index}")
            for index, (entry, label) in enumerate(zip(entries, labels))]


def host_points(machine, host):
    """(gauges, counters) from one ``host`` answer. gauges: [(name, labels, value[, kind])]; counters:
    [(name, labels, value)] (raw cumulative byte counters, converted to deltas by the caller)."""
    checks = (host or {}).get("checks") or {}
    gauges, counters = [], []
    base = {"machine": machine}

    def add(name, value, **labels):
        if _finite(value):
            gauges.append((name, {**base, **labels}, value))

    cpu = _value(checks.get("cpu")) or {}
    add("node_cpu_busy_percent", cpu.get("busy_percent"))
    load = _value(checks.get("load"))
    if isinstance(load, list) and load:
        add("node_load1", load[0])
    memory = _value(checks.get("memory")) or {}
    add("node_mem_available_bytes", memory.get("MemAvailable"))
    add("node_mem_total_bytes", memory.get("MemTotal"))
    gpu_hwmons = set()
    for card in _value(checks.get("gpus")) or []:
        device = card.get("card")
        if not isinstance(device, str):
            continue
        gpu_hwmons.add(card.get("hwmon"))
        add("node_gpu_busy_percent", card.get("busy_percent"), device=device)
        for temp, label in sensor_labels(card.get("temps") or []):
            add("node_gpu_temp_celsius", temp.get("celsius"), device=device, sensor=_sensor("amdgpu", label))
        add("node_gpu_power_watts", card.get("power_w"), device=device, sensor=str(card.get("power_sensor") or "power1"))
        add("node_gpu_power_limit_watts", card.get("power_cap_w"), device=device)
        add("node_gpu_clock_mhz", card.get("sclk_mhz"), device=device)
        for domain in ("vram", "gtt"):
            add("node_gpu_mem_used_bytes", card.get(domain + "_used_bytes"), device=device, domain=domain)
            add("node_gpu_mem_total_bytes", card.get(domain + "_total_bytes"), device=device, domain=domain)
    chips = [chip for chip in _value(checks.get("sensors")) or []
             if chip.get("hwmon") not in gpu_hwmons and chip.get("name") != "amdgpu"]
    prefixes = sensor_names(chips)
    for chip in chips:
        cpu_chip = chip.get("name") in CPU_CHIPS
        name = "node_cpu_temp_celsius" if cpu_chip else "node_sensor_temp_celsius"
        if not stable_prefix(prefixes[chip.get("hwmon")]):
            continue
        for temp, label in sensor_labels(chip.get("temps") or []):
            add(name, temp.get("celsius"), sensor=_sensor(prefixes[chip.get("hwmon")], label))
        if cpu_chip:
            powers = [p for p in chip.get("power") or [] if p.get("field") in ("average", "input")]
            averaged = {p.get("input") for p in powers if p.get("field") == "average"}
            powers = [p for p in powers if p.get("field") == "average" or p.get("input") not in averaged]
            for power, label in sensor_labels(powers):
                add("node_cpu_power_watts", power.get("watts"), sensor=_sensor(prefixes[chip.get("hwmon")], label))
    for disk in _value(checks.get("disks")) or []:
        add("node_disk_used_bytes", disk.get("used_bytes"), mount=disk.get("mount"))
        add("node_disk_total_bytes", disk.get("total_bytes"), mount=disk.get("mount"))
    for row in _value(checks.get("diskstats")) or []:
        for field, name in (("read_bytes", "node_disk_read_bytes"), ("write_bytes", "node_disk_write_bytes")):
            if _finite(row.get(field)):
                counters.append((name, {**base, "device": row["device"]}, row[field]))
    for row in _value(checks.get("net")) or []:
        for field, name in (("rx_bytes", "node_net_rx_bytes"), ("tx_bytes", "node_net_tx_bytes")):
            if _finite(row.get(field)):
                counters.append((name, {**base, "iface": row["iface"]}, row[field]))
    return gauges, counters


RATES_DDL = ("CREATE TABLE IF NOT EXISTS fleet_rates (key TEXT PRIMARY KEY, machine TEXT NOT NULL, instance TEXT NOT NULL, "
             "ts REAL NOT NULL, rate REAL NOT NULL, window_s REAL NOT NULL)")
RATE_SQL = ("INSERT INTO fleet_rates(key, machine, instance, ts, rate, window_s) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) "
            "DO UPDATE SET machine=excluded.machine, instance=excluded.instance, ts=excluded.ts, rate=excluded.rate, "
            "window_s=excluded.window_s")


def counter_key(name, labels):
    return name + "|" + "/".join(f"{k}={labels[k]}" for k in sorted(labels) if k != "machine")


def account(store, key, machine, instance, value, ts, rate_key, points=()):
    """Advance one counter's persisted baseline (C3) and, in the same transaction, record its delta points and its
    rate over the real interval since the previous baseline sample (table fleet_rates, bound to the instance).
    Callers hold the machine's gate, the only writer of that machine's keys in any process, so the previous
    baseline read here is the one counter_delta replaces. A new baseline (first sample, reset, new instance)
    removes the key's rate. Returns the delta."""
    before = store.get(BASELINE_NAMESPACE, key) or {}
    if before.get("instance") != str(instance) or not _finite(before.get("value")) or value < before["value"]:
        # This sample starts a new baseline: the old rate goes first, so an interruption can never leave it readable.
        store.execute("DELETE FROM fleet_rates WHERE key=?", (rate_key,))

    def apply(delta, info):
        writes = {"points": [(name, labels, delta, "delta") for name, labels in points]}
        previous = before.get("ts")
        if before.get("instance") == str(instance) and _finite(previous) and ts > previous:
            writes["sql"] = [(RATE_SQL, (rate_key, machine, str(instance), ts, delta / (ts - previous), ts - previous))]
        return writes

    delta, info = store.counter_delta(key, instance, value, ts, max_gap=COUNTER_GAP_S, apply=apply)
    if delta is None and not info.get("stale"):
        store.execute("DELETE FROM fleet_rates WHERE key=?", (rate_key,))
    return delta


def record_counters(store, counters, instance, ts):
    """Byte counters -> deltas through the store's persisted baselines (one transaction each; C3)."""
    recorded = 0
    for name, labels, value in counters:
        key = "fleet/" + labels["machine"] + "/" + counter_key(name, labels)
        delta = account(store, key, labels["machine"], instance, value, ts,
                        labels["machine"] + "|" + counter_key(name, labels), points=[(name, labels)])
        recorded += delta is not None
    return recorded


def rates(store, machine, now, max_age):
    """{rate key: (rate, window_s, instance)} committed with the counters of this machine, at most max_age old.
    Readers use a rate only when its instance is the current instance of the source."""
    if store is None:
        return {}
    try:
        rows = store.execute("SELECT key, instance, ts, rate, window_s FROM fleet_rates WHERE machine=?", (machine,))
    except Exception:       # table not created in this store yet
        return {}
    return {key: (rate, window, instance) for key, instance, ts, rate, window in rows if now - ts <= max_age}




def engine_identity(status):
    """The engine's own identity of its process lifetime: its reported instance id, else its reported start time.
    Nothing is inferred from an uptime (an inferred start cannot tell a replacement from the same process)."""
    if not isinstance(status, dict):
        return None
    # Kept exactly: an identity is never truncated or rounded (that could join two processes); one that does not
    # fit is refused instead.
    value = status.get("instance_id")
    if isinstance(value, str) and value:
        return "id:" + value if len(value) <= 128 and value.isprintable() else None
    for key in ("start_time", "started_at"):
        if _finite(status.get(key)):
            return f"{key}:{status[key]!r}"
    return None


def _instance(status, launch):
    """The counter instance (C3): the launch plus the engine's own identity; None when the engine reports neither an
    instance id nor its start time (its counter is then not accounted)."""
    identity = engine_identity(status)
    return None if identity is None or launch is None else f"{launch}/{identity}"


def bracketed(before, after):
    """True when the status reads before and after the metrics read report the same engine identity, so the
    counter between them belongs to that process."""
    first, second = engine_identity(before), engine_identity(after)
    return first is not None and first == second


def engine_entry(previous, machine, target, answer, now):
    """Parsed loopback engine answer (C4): per-path outcomes, status, metrics, instance and a preview rate."""
    paths = (answer or {}).get("paths") or {}
    row = {"ts": now, "machine": machine, "profile": target["profile"], "port": target["port"],
           "replica": target.get("replica"),
           "outcomes": {path: ("ok" if (paths.get(path) or {}).get("status") == "ok"
                               else (paths.get(path) or {}).get("why", "not answered")) for path in ENGINE_PATHS},
           "health": None, "status": None, "metrics": None, "metrics_text": None, "instance": None,
           "generated_tokens": None,
           "throughput": None, "why": {}}
    row["health"] = "ok" if row["outcomes"]["/health"] == "ok" else None
    if row["health"] is None:
        row["why"]["health"] = row["outcomes"]["/health"]
    status = paths.get("/api/status?detail=1") or {}
    if status.get("status") == "ok":
        try:
            parsed = json.loads(status["value"]["body"])
            row["status"] = parsed if isinstance(parsed, dict) else None
        except (ValueError, KeyError, TypeError):
            pass
        if row["status"] is None:
            row["why"]["status"] = "engine status is not a JSON object"
    else:
        row["why"]["status"] = row["outcomes"]["/api/status?detail=1"]
    metrics = paths.get("/metrics") or {}
    if metrics.get("status") == "ok":
        try:
            parsed = engine_adapter.parse_metrics(metrics["value"]["body"])
            row["metrics"] = {k: v for k, v in vars(parsed).items() if k not in ("why", "throughput_label")}
            row["metrics_text"] = metrics["value"]["body"][:256 * 1024]
        except (ValueError, KeyError, TypeError) as exc:
            row["why"]["metrics"] = "engine metrics unreadable: " + str(exc)[:100]
    else:
        row["why"]["metrics"] = row["outcomes"]["/metrics"]
    row["launch"] = target.get("launch")
    row["instance"] = _instance(row["status"], row["launch"])
    second = paths.get("/api/status") or {}
    try:
        after = json.loads(second["value"]["body"]) if second.get("status") == "ok" else None
    except (ValueError, KeyError, TypeError):
        after = None
    if row["instance"] is not None and not bracketed(row["status"], after):
        row["instance"] = None
        row["why"]["instance_bracket"] = ("the status read after the metrics did not report the same engine identity; "
                                         "this sample's counter is not accounted")
    row["uptime_seconds"] = (row["status"] or {}).get("uptime_seconds")
    row["generated_tokens"] = (row["metrics"] or {}).get("generated_tokens")
    if row["instance"] is None and "instance_bracket" not in row["why"]:
        row["why"]["instance"] = ("the engine reports neither an instance id nor its start time; its counter is not "
                                  "accounted and its throughput is unavailable")
    row["why"]["throughput"] = "requires two counter samples of the same engine instance"
    return row


def engine_key(machine, profile, port):
    return f"{machine}:{profile}:{port}"


# -- one ssh call per machine, across threads and processes -------------------------------------------------------
def gate_path(api, machine_id):
    """The machine's node-call gate: an OS file lock any caller (this package, the CLI, other packages) can take."""
    return Path(api.config_path).parent / "node-locks" / (machine_id + ".lock")


def node_gate(api, machine_id):
    """Public form of the gate for other callers: a release function, or None while another call holds it."""
    return _try_lock(api, _state(api), machine_id)


def _try_lock(api, state, machine_id):
    """Both the in-process lock and the machine's OS file lock (shared with the CLI): a release function, or None
    when either is held (that machine is skipped, never called twice)."""
    with state.lock:
        local = state.machine_locks.setdefault(machine_id, threading.Lock())
    if not local.acquire(blocking=False):
        return None
    try:
        handle = procs.InstanceLock(gate_path(api, machine_id)).acquire()
    except (procs.ProcessError, OSError):
        local.release()
        return None

    def release():
        try:
            handle.close()
        finally:
            local.release()
    return release


def _call_node(api, machine, payload):
    """One host call (patched by the browser fixtures)."""
    return api.node(machine, "host", payload)


def _node_answer(api, machine, payload):
    try:
        answer = _call_node(api, machine, payload)
        if not isinstance(answer, dict) or answer.get("schema") != "hyperloom/node-v1":
            raise ValueError("unexpected node answer")
        if answer.get("error"):
            raise ValueError(str(answer["error"])[:300])
        return answer, None
    except Exception as exc:     # every failure is an outcome of this machine, never of the round
        return None, f"{type(exc).__name__}: {exc}"[:300]


def _entry(store, machine_id, access, answer, error, now, *, engines=()):
    """The acquired result of one machine: points, counters, rates and the document to persist (no writes)."""
    previous = store.get(HOSTS, machine_id) if store is not None else None
    result = {"machine": machine_id, "access": access, "gauges": [], "counters": [], "engines": [], "instance": None}
    if answer is None:
        result["gauges"].append(("node_up", {"machine": machine_id}, 0))
        result["doc"] = {**{k: (previous or {}).get(k) for k in ("host", "last_ok", "instance")},
                         "ts": (previous or {}).get("ts"), "failed_at": now, "ok": False, "error": error,
                         "access": access, "failures": int((previous or {}).get("failures") or 0) + 1}
        return result
    checks = answer.get("checks") or {}
    instance = _value(checks.get("boot_id"))
    if instance is None:
        uptime = _value(checks.get("uptime_s"))
        instance = f"boot-{int((now - uptime) // 60)}" if _finite(uptime) else None
    gauges, counters = host_points(machine_id, answer)
    result["gauges"] = [("node_up", {"machine": machine_id}, 1), *gauges]
    result["counters"] = counters
    result["instance"] = instance
    result["doc"] = {"ts": now, "ok": True, "error": None, "access": access, "host": answer, "last_ok": now,
                     "failures": 0, "instance": instance}
    for target, row in engines:
        key = engine_key(machine_id, target["profile"], target["port"])
        result["engines"].append((key, engine_entry(store.get(ENGINES, key) if store is not None else None,
                                                    machine_id, target, row, now)))
    result["doc"]["engines"] = [key for key, _ in result["engines"]]
    return result


def collect_nodes(api, now):
    """Acquire one round of the fleet-nodes collector: every ssh machine's answer (one call each, never two in
    flight for a machine), converted to points; nothing is written (see persist)."""
    store = telemetry.existing_store(api)
    state = _state(api)
    cluster = api.cluster()
    targets = loopback_engines(launch_records(api))
    machines = [m for m in cluster.machines.values() if m.access == "ssh"]
    answers, threads, skipped = {}, [], {}

    def work(machine, release, payload):
        try:
            answers[machine.id] = _node_answer(api, machine, payload)
        finally:
            release()

    for machine in machines:
        release = _try_lock(api, state, machine.id)
        if release is None:
            skipped[machine.id] = "previous node call still in flight"
            continue
        payload = {}
        if machine.id in state.cpu:
            payload["cpu"] = state.cpu[machine.id]
        if targets.get(machine.id):
            payload["engine_status"] = [{"port": t["port"], "paths": list(ENGINE_PATHS)} for t in targets[machine.id]]
        thread = threading.Thread(target=work, args=(machine, release, payload), daemon=True,
                                  name="hyperloom-fleet-" + machine.id)
        threads.append(thread)
        thread.start()
    deadline = time.monotonic() + ROUND_S
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    results = []
    for machine in machines:
        if machine.id in skipped or machine.id not in answers:
            results.append({"machine": machine.id, "access": "ssh", "skipped": skipped.get(
                machine.id, "node call did not finish within the round"), "gauges": [], "counters": [], "engines": []})
            continue
        answer, error = answers[machine.id]
        checks = (answer or {}).get("checks") or {}
        cpu = _value(checks.get("cpu")) or {}
        boot = _value(checks.get("boot_id"))
        if boot and type(cpu.get("total")) is int and type(cpu.get("idle")) is int:
            state.cpu[machine.id] = {"boot_id": boot, "total": cpu["total"], "idle": cpu["idle"]}
        rows = _value(checks.get("engine_status")) or []
        results.append(_entry(store, machine.id, "ssh", answer, error, now,
                              engines=list(zip(targets.get(machine.id) or [], rows))))
    return {"ts": now, "collector": "fleet-nodes", "machines": results,
            "engine_keys": sorted(engine_key(m, t["profile"], t["port"]) for m, rows in targets.items() for t in rows)}


def persist(api, store, round_, *, counters_due=True):
    """Write one acquired round: descriptors, the per-machine documents, counter deltas through the persisted
    baselines (C3) and the loopback engine documents. Returns the gauge/state points for the sampler (deltas are
    committed here, never returned)."""
    state = _state(api)
    describe(api, store, state)
    now = round_["ts"]
    points = []
    for result in round_["machines"]:
        if "skipped" in result:
            previous = store.get(HOSTS, result["machine"]) or {}
            store.put(HOSTS, result["machine"], dict(previous, skipped=int(previous.get("skipped") or 0) + 1,
                                                     last_skip=now, skip_why=result["skipped"]))
            continue
        points.extend(result["gauges"])
        release = _try_lock(api, state, result["machine"])
        if release is None:
            # Another recorder holds this machine: its counters wait for the next round (never accounted twice).
            result["doc"]["accounting"] = "skipped: another recorder holds this machine"
        else:
            try:
                if counters_due and result["counters"] and result.get("instance"):
                    record_counters(store, result["counters"], result["instance"], now)
                for key, row in result["engines"]:
                    if row.get("instance") and _finite(row.get("generated_tokens")):
                        account(store, "fleet/engine/" + key, result["machine"], row["instance"],
                                row["generated_tokens"], now, "engine|" + key)
                    else:
                        store.execute("DELETE FROM fleet_rates WHERE key=?", ("engine|" + key,))
            finally:
                release()
        for key, row in result["engines"]:
            store.put(ENGINES, key, row)
        store.put(HOSTS, result["machine"], result["doc"])
    if "engine_keys" in round_:
        current = set(round_["engine_keys"])
        for key, _, _ in store.items(ENGINES):
            if key not in current:
                store.delete(ENGINES, key)       # the engine stopped or its port now belongs to another launch
                store.execute("DELETE FROM fleet_rates WHERE key=?", ("engine|" + key,))
    return points


def sample_nodes(api, now):
    """The fleet-nodes collector."""
    return persist(api, telemetry.open_store(api), collect_nodes(api, now))


# -- control machine (C6) -----------------------------------------------------------------------------------------
NVIDIA_QUERY = ("index,uuid,name,power.draw,power.limit,temperature.gpu,utilization.gpu,memory.used,memory.total,"
                "clocks.sm,pstate,clocks_throttle_reasons.active,pcie.link.gen.current,pcie.link.width.current")
NVIDIA_FIELDS = ("index", "uuid", "name", "power_w", "power_limit_w", "temperature_c", "busy_percent", "memory_used_mib",
                 "memory_total_mib", "clock_mhz", "pstate", "throttle_reasons", "pcie_gen", "pcie_width")
THERMAL_BITS, POWER_BITS, IDLE_BIT = 0x8 | 0x20 | 0x40, 0x4 | 0x80, 0x1
REASONS = {0x1: "idle", 0x2: "application clocks setting", 0x4: "software power cap", 0x8: "hardware slowdown",
           0x10: "sync boost", 0x20: "software thermal slowdown", 0x40: "hardware thermal slowdown",
           0x80: "hardware power brake", 0x100: "display clock setting"}


def throttle_state(mask):
    """(code, reasons) of an active clock-event bitmask: 2 thermal, 1 power cap, 3 other, 0 none or idle."""
    if mask is None:
        return None, None
    reasons = [REASONS.get(1 << bit, f"reason bit {1 << bit:#x}") for bit in range(64) if mask >> bit & 1]
    if mask & THERMAL_BITS:
        return 2, reasons
    if mask & POWER_BITS:
        return 1, reasons
    if mask & ~IDLE_BIT:
        return 3, reasons
    return 0, reasons


def _cell(text):
    text = text.strip()
    return None if not text or text.startswith("[") or text in ("N/A", "Unknown Error") else text


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_nvidia(gpus_csv, apps_csv=""):
    cards = []
    for row in csv.reader(io.StringIO(gpus_csv)):
        if len(row) != len(NVIDIA_FIELDS):
            continue
        values = dict(zip(NVIDIA_FIELDS, (_cell(v) for v in row)))
        uuid = values["uuid"]
        if not uuid or not re.fullmatch(r"GPU-[0-9a-fA-F-]{8,64}", uuid):
            continue
        card = {"index": int(values["index"]) if (values["index"] or "").isdigit() else None,
                "uuid": uuid, "device": uuid[:12], "name": values["name"], "pstate": values["pstate"]}
        for key in ("power_w", "power_limit_w", "temperature_c", "busy_percent", "clock_mhz", "pcie_gen", "pcie_width"):
            card[key] = _number(values[key])
        for key in ("memory_used", "memory_total"):
            mib = _number(values[key + "_mib"])
            card[key + "_bytes"] = None if mib is None else int(mib * 1024 * 1024)
        mask = None
        if values["throttle_reasons"]:
            try:
                mask = int(values["throttle_reasons"], 16)
            except ValueError:
                mask = None
        card["throttle"], card["throttle_reasons"] = throttle_state(mask)
        card["processes"] = []
        card["why"] = {key: "not reported by nvidia-smi" for key, value in card.items() if value is None}
        cards.append(card)
    by_uuid = {card["uuid"]: card for card in cards}
    for row in csv.reader(io.StringIO(apps_csv or "")):
        if len(row) != 4:
            continue
        uuid, pid, name, used = (_cell(v) for v in row)
        card = by_uuid.get(uuid or "")
        if card is None or not (pid or "").isdigit() or len(card["processes"]) >= 64:
            continue
        mib = _number(used)
        # Only the executable's file name: full paths name users and folders.
        base = re.split(r"[\\/]", name or "")[-1][:80] or None
        card["processes"].append({"pid": int(pid), "comm": base,
                                  "vram_bytes": None if mib is None else int(mib * 1024 * 1024)})
    return cards


def _nvidia_run(arguments):
    executable = shutil.which("nvidia-smi")
    if executable is None:
        raise FileNotFoundError("nvidia-smi not found")
    result = subprocess.run([executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=NVIDIA_TIMEOUT_S, check=False, **no_window())
    if result.returncode:
        raise RuntimeError(f"nvidia-smi exited with {result.returncode}")
    if len(result.stdout) > NVIDIA_MAX_BYTES:
        raise RuntimeError("nvidia-smi output exceeds 64 KiB")
    return result.stdout.decode("utf-8", "replace")


def nvidia_sample(run=_nvidia_run):
    """Passive nvidia-smi queries for every card (never creates a GPU context)."""
    try:
        gpus = run(["--query-gpu=" + NVIDIA_QUERY, "--format=csv,noheader,nounits"])
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"status": "unavailable", "value": None, "why": "nvidia-smi: " + str(exc)[:200]}
    try:
        apps = run(["--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    except (OSError, subprocess.SubprocessError, RuntimeError):
        apps = ""
    cards = parse_nvidia(gpus, apps)
    return {"status": "ok", "value": cards} if cards else {"status": "unavailable", "value": None,
                                                           "why": "nvidia-smi reported no cards"}


def nvidia_points(machine, cards):
    points = []
    for card in cards or []:
        labels = {"machine": machine, "device": card["device"]}

        def add(name, value, **extra):
            if _finite(value):
                points.append((name, {**labels, **extra}, value))

        add("node_gpu_busy_percent", card.get("busy_percent"))
        add("node_gpu_temp_celsius", card.get("temperature_c"), sensor="gpu")
        add("node_gpu_power_watts", card.get("power_w"), sensor="board")
        add("node_gpu_power_limit_watts", card.get("power_limit_w"))
        add("node_gpu_clock_mhz", card.get("clock_mhz"))
        add("node_gpu_throttled", card.get("throttle"))
        add("node_gpu_mem_used_bytes", card.get("memory_used_bytes"), domain="vram")
        add("node_gpu_mem_total_bytes", card.get("memory_total_bytes"), domain="vram")
    return points


class Windows:
    """Read-only Windows system calls for the control machine (ctypes; no WMI, no elevation)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        try:
            self.iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        except OSError:
            self.iphlpapi = None

    def memory(self):
        ctypes = self.ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        *[(name, ctypes.c_ulonglong) for name in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]]

        class PERFORMANCE_INFORMATION(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), *[(name, ctypes.c_size_t) for name in (
                "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
                "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")],
                ("HandleCount", ctypes.c_ulong), ("ProcessCount", ctypes.c_ulong), ("ThreadCount", ctypes.c_ulong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        info = PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
        if not self.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            raise OSError("GetPerformanceInfo failed")
        page = info.PageSize
        return {"MemTotal": status.ullTotalPhys, "MemAvailable": status.ullAvailPhys,
                "CommitTotal": info.CommitTotal * page, "CommitLimit": info.CommitLimit * page,
                "CommitAvailable": (info.CommitLimit - info.CommitTotal) * page}

    def cpu_times(self):
        ctypes = self.ctypes
        FILETIME = self.wintypes.FILETIME
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not self.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetSystemTimes failed")
        value = lambda t: t.dwHighDateTime << 32 | t.dwLowDateTime
        return value(idle), value(kernel) + value(user)       # kernel time includes idle time

    def uptime(self):
        self.kernel32.GetTickCount64.restype = self.ctypes.c_ulonglong
        return self.kernel32.GetTickCount64() / 1000.0

    def cores(self):
        return os.cpu_count()

    def disks(self):
        mask = self.kernel32.GetLogicalDrives()
        rows = []
        for index in range(26):
            if not mask >> index & 1:
                continue
            root = chr(65 + index) + ":\\"
            if self.kernel32.GetDriveTypeW(self.ctypes.c_wchar_p(root)) != 3:     # DRIVE_FIXED
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            rows.append({"mount": root[:2], "fstype": None, "total_bytes": usage.total,
                         "used_bytes": usage.used, "free_bytes": usage.free})
        return rows

    def interfaces(self):
        if self.iphlpapi is None:
            raise OSError("iphlpapi unavailable")
        ctypes = self.ctypes

        class MIB_IF_ROW2(ctypes.Structure):
            _fields_ = [("InterfaceLuid", ctypes.c_ulonglong), ("InterfaceIndex", ctypes.c_ulong),
                        ("InterfaceGuid", ctypes.c_ubyte * 16), ("Alias", ctypes.c_wchar * 257),
                        ("Description", ctypes.c_wchar * 257), ("PhysicalAddressLength", ctypes.c_ulong),
                        ("PhysicalAddress", ctypes.c_ubyte * 32), ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
                        ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("TunnelType", ctypes.c_int),
                        ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int), ("AccessType", ctypes.c_int),
                        ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte), ("OperStatus", ctypes.c_int),
                        ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
                        ("NetworkGuid", ctypes.c_ubyte * 16), ("ConnectionType", ctypes.c_int),
                        ("TransmitLinkSpeed", ctypes.c_ulonglong), ("ReceiveLinkSpeed", ctypes.c_ulonglong),
                        *[(name, ctypes.c_ulonglong) for name in (
                            "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors", "InUnknownProtos",
                            "InUcastOctets", "InMulticastOctets", "InBroadcastOctets", "OutOctets", "OutUcastPkts",
                            "OutNUcastPkts", "OutDiscards", "OutErrors", "OutUcastOctets", "OutMulticastOctets",
                            "OutBroadcastOctets", "OutQLen")]]

        if ctypes.sizeof(MIB_IF_ROW2) != 1352:
            raise OSError("unexpected MIB_IF_ROW2 layout")

        class MIB_IF_TABLE2(ctypes.Structure):
            _fields_ = [("NumEntries", ctypes.c_ulong), ("Table", MIB_IF_ROW2 * 1)]

        table = ctypes.POINTER(MIB_IF_TABLE2)()
        if self.iphlpapi.GetIfTable2(ctypes.byref(table)) != 0:
            raise OSError("GetIfTable2 failed")
        try:
            count = min(table.contents.NumEntries, 256)
            rows = ctypes.cast(ctypes.addressof(table.contents.Table), ctypes.POINTER(MIB_IF_ROW2 * count)).contents
            found = []
            for row in rows:
                hardware = bool(row.Flags & 1)
                if not hardware or row.Type == 24:          # software and loopback interfaces are skipped
                    continue
                found.append({"iface": row.Alias[:64], "luid": row.InterfaceLuid, "rx_bytes": row.InOctets,
                              "tx_bytes": row.OutOctets, "physical": True,
                              "operstate": "up" if row.OperStatus == 1 else "down",
                              "link_mbps": row.ReceiveLinkSpeed / 1e6 if row.ReceiveLinkSpeed < 2 ** 63 else None})
            return found
        finally:
            self.iphlpapi.FreeMibTable(table)


def _ok(value):
    return {"status": "ok", "value": value}


def _unavailable(why):
    return {"status": "unavailable", "value": None, "why": why}


def windows_checks(system, state):
    """A ``host``-shaped answer for a Windows control machine."""
    checks = {}

    def section(name, function):
        try:
            checks[name] = _ok(function())
        except Exception as exc:
            checks[name] = _unavailable(f"{name}: {type(exc).__name__}: {exc}"[:200])

    section("uptime_s", system.uptime)
    section("memory", system.memory)
    try:
        idle, total = system.cpu_times()
        previous = state.windows.get("cpu")
        state.windows["cpu"] = (idle, total)
        if previous and total > previous[1] and idle >= previous[0]:
            span = total - previous[1]
            busy = max(0.0, min(100.0, 100.0 * (span - (idle - previous[0])) / span))
            checks["cpu"] = _ok({"busy_percent": busy, "cores": system.cores()})
        else:
            checks["cpu"] = _ok({"busy_percent": None, "cores": system.cores(),
                                 "why": {"busy_percent": "requires two samples of the system CPU times"}})
    except Exception as exc:
        checks["cpu"] = _unavailable(f"cpu: {type(exc).__name__}")
    checks["load"] = _unavailable("Windows has no load average")
    checks["sensors"] = _unavailable("no CPU sensor readable without elevated access")
    section("disks", system.disks)
    checks["diskstats"] = _unavailable("per-disk counters are not read on Windows")
    section("net", system.interfaces)
    checks["gpus"] = _unavailable("no AMD GPU on this machine")
    checks["gpu_processes"] = _unavailable("see the processes of each NVIDIA card")
    checks["listeners"] = _unavailable("not read on the control machine")
    return {"schema": "hyperloom/node-v1", "verb": "host", "checks": checks}


def _node_module():
    path = repo_root() / "node" / "hyperloom_observe.py"
    spec = importlib.util.spec_from_file_location("hyperloom_node_observe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def control_machine(cluster):
    return next((m for m in cluster.machines.values() if m.access == "local"), None)


def _control_host(state):
    """The control machine's own answer (patched by the browser fixtures)."""
    if os.name == "nt":
        system = state.windows.get("system")
        if system is None:
            system = state.windows["system"] = Windows()
        return windows_checks(system, state)
    module = state.windows.get("module") or _node_module()
    state.windows["module"] = module
    answer = module.collect({"cpu": state.windows["cpu"]} if "cpu" in state.windows else {})
    cpu = _value(answer["checks"].get("cpu")) or {}
    boot = _value(answer["checks"].get("boot_id"))
    if boot and type(cpu.get("total")) is int:
        state.windows["cpu"] = {"boot_id": boot, "total": cpu["total"], "idle": cpu["idle"]}
    return answer


def _control_nvidia():
    """nvidia-smi of the control machine (patched by the browser fixtures)."""
    return nvidia_sample()


def collect_control(api, now):
    """Acquire one round of the fleet-control collector (C6); nothing is written (see persist)."""
    store = telemetry.existing_store(api)
    state = _state(api)
    machine = control_machine(api.cluster())
    if machine is None:
        return {"ts": now, "collector": "fleet-control", "machines": [], "why": "no machine with local access configured"}
    release = _try_lock(api, state, machine.id)
    if release is None:
        return {"ts": now, "collector": "fleet-control", "machines": [{
            "machine": machine.id, "access": "local", "skipped": "previous control sample still running",
            "gauges": [], "counters": [], "engines": []}]}
    try:
        try:
            answer, error = _control_host(state), None
        except Exception as exc:
            answer, error = None, f"{type(exc).__name__}: {exc}"[:300]
        nvidia = _control_nvidia()
    finally:
        release()
    if answer is not None:
        answer.setdefault("checks", {})["nvidia"] = nvidia
    result = _entry(store, machine.id, "local", answer, error, now)
    if answer is None:
        # The portal process itself is up; only its own system reads failed.
        result["gauges"] = [("node_up", {"machine": machine.id}, 1)]
    else:
        memory = _value(answer["checks"].get("memory")) or {}
        if _finite(memory.get("CommitAvailable")):
            result["gauges"].append(("node_commit_available_bytes", {"machine": machine.id}, memory["CommitAvailable"]))
    result["gauges"].extend(nvidia_points(machine.id, _value(nvidia)))
    return {"ts": now, "collector": "fleet-control", "machines": [result]}


def sample_control(api, now):
    """The fleet-control collector. Its byte counters are committed at most once per node interval, so every delta
    series is sampled at no less than its described interval (coverage never overstated)."""
    store = telemetry.open_store(api)
    state = _state(api)
    round_ = collect_control(api, now)
    last = state.windows.get("counters_committed")
    due = last is None or now - last >= NODE_INTERVAL
    if due and any(r.get("counters") for r in round_["machines"]):
        state.windows["counters_committed"] = now
    return persist(api, store, round_, counters_due=due)


# -- thresholds (F2) ----------------------------------------------------------------------------------------------
def thresholds(store):
    saved = store.get(PREFS, THRESHOLDS_KEY) if store is not None else None
    merged = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    for group, values in (saved or {}).items():
        if group in merged and isinstance(values, dict):
            merged[group].update({k: v for k, v in values.items() if k in ("amber_gib", "red_gib")})
    return merged


def validate_thresholds(arguments, current):
    """Partial update of the thresholds; the fully merged result is validated (or {"reset": true})."""
    if not isinstance(arguments, dict) or not arguments:
        raise ValueError("thresholds must be a nonempty object")
    if "reset" in arguments:
        if arguments != {"reset": True}:
            raise ValueError('reset takes only {"reset": true}')
        return json.loads(json.dumps(DEFAULT_THRESHOLDS))
    merged = json.loads(json.dumps(current))
    for group, values in arguments.items():
        if group not in DEFAULT_THRESHOLDS:
            raise ValueError("unknown threshold group: " + str(group)[:40])
        if not isinstance(values, dict) or not values or set(values) - {"amber_gib", "red_gib"}:
            raise ValueError(group + " takes amber_gib and/or red_gib")
        for key, value in values.items():
            if value is None:
                if key == "red_gib" and group == "commit":
                    merged[group][key] = None
                    continue
                raise ValueError(f"{group}.{key} must be a number (only commit.red_gib may be null)")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1024:
                raise ValueError(f"{group}.{key} must be a number of GiB in 0..1024")
            merged[group][key] = float(value)
    for group, rule in merged.items():
        if rule.get("amber_gib") is None:
            raise ValueError(f"{group}.amber_gib is required")
        if rule.get("red_gib") is None and group != "commit":
            raise ValueError(f"{group}.red_gib is required")
        if rule.get("red_gib") is not None and rule["red_gib"] > rule["amber_gib"]:
            raise ValueError(f"{group}: red_gib must not exceed amber_gib")
    return merged


def get_thresholds(api, query):
    if query:
        raise ValueError("fleet/thresholds takes no query arguments")
    store = telemetry.existing_store(api)
    why = {} if store is not None else {"thresholds": "settings store not open in this portal process; defaults shown"}
    return api.document("fleet-thresholds", thresholds=thresholds(store), defaults=DEFAULT_THRESHOLDS,
                        labels=THRESHOLD_LABELS, why=why)


def set_thresholds(api, arguments):
    """POST fleet/thresholds/set (and `hyperloom telemetry thresholds`): validated, then saved."""
    store = telemetry.open_store(api)
    merged = validate_thresholds(arguments, thresholds(store))
    store.put(PREFS, THRESHOLDS_KEY, merged)
    return {"thresholds": merged, "defaults": DEFAULT_THRESHOLDS}


def level(available, group, limits):
    """red when available is strictly under red, amber when strictly under amber, else ok."""
    if group is None:
        return "info"
    if not _finite(available):
        return "unknown"
    rule = limits.get(group) or {}
    if rule.get("red_gib") is not None and available < rule["red_gib"] * GIB:
        return "red"
    if rule.get("amber_gib") is not None and available < rule["amber_gib"] * GIB:
        return "amber"
    return "ok"


RANK = {"red": 3, "amber": 2, "ok": 0}


def excluded_card(card, declared):
    ids = {d.get("id") for d in declared or () if isinstance(d, dict) and d.get("excluded")}
    return card.get("uuid") in ids or card.get("device") in ids or "4090" in (card.get("name") or "")


def memory_domains(machine, entry, limits, fresh=True):
    """F2: memory per accounting domain, never merged, with levels; (domains, limiting, why)."""
    host = (entry or {}).get("host") or {}
    checks = host.get("checks") or {}
    domains = []
    memory = _value(checks.get("memory")) or {}
    windows = "CommitLimit" in memory or machine.os == "windows"
    total, available = memory.get("MemTotal"), memory.get("MemAvailable")
    system = {"domain": "system", "device": None, "total_bytes": total, "available_bytes": available,
              "used_bytes": total - available if _finite(total) and _finite(available) else None,
              "label": "System memory · available physical" if windows else "System memory · MemAvailable",
              "threshold": None if windows else "unified", "observer": "control machine" if windows else "node"}
    if not memory:
        system["why"] = _why(checks.get("memory"), "no answer from this machine yet")
    domains.append(system)
    if windows:
        domains.append({"domain": "commit", "label": "Windows commit headroom", "device": None,
                        "total_bytes": memory.get("CommitLimit"), "available_bytes": memory.get("CommitAvailable"),
                        "used_bytes": memory.get("CommitTotal"), "threshold": "commit", "observer": "control machine",
                        **({} if memory else {"why": system["why"]})})
    for card in _value(checks.get("gpus")) or []:
        parts = {k: card.get(k) for k in ("vram_used_bytes", "vram_total_bytes", "gtt_used_bytes", "gtt_total_bytes",
                                          "vis_vram_used_bytes", "vis_vram_total_bytes")}
        known = all(_finite(parts[k]) for k in ("vram_used_bytes", "vram_total_bytes", "gtt_used_bytes", "gtt_total_bytes"))
        used = parts["vram_used_bytes"] + parts["gtt_used_bytes"] if known else None
        size = parts["vram_total_bytes"] + parts["gtt_total_bytes"] if known else None
        domains.append({"domain": "gpu-visible", "label": f"GPU-visible memory (VRAM + GTT) · {card.get('card')}",
                        "device": card.get("card"), "total_bytes": size, "used_bytes": used,
                        "available_bytes": size - used if known else None, "threshold": "unified", "observer": "node",
                        "parts": parts, **({} if known else {"why": "VRAM or GTT counters not reported"})})
    for card in _value(checks.get("nvidia")) or []:
        used, size = card.get("memory_used_bytes"), card.get("memory_total_bytes")
        known = _finite(used) and _finite(size)
        excluded = excluded_card(card, machine.devices)
        domains.append({"domain": "vram", "label": f"VRAM · {card.get('name') or card.get('device')}",
                        "device": card.get("device"), "total_bytes": size, "used_bytes": used,
                        "available_bytes": size - used if known else None, "threshold": "rtx",
                        "observer": "control machine (nvidia-smi)", "excluded": excluded,
                        **({"excluded_reason": "excluded by policy; never the limiting domain"} if excluded else {}),
                        **({} if known else {"why": "memory not reported by nvidia-smi"})})
    for domain in domains:
        domain["level"] = level(domain.get("available_bytes"), domain.get("threshold"), limits) if fresh else (
            "info" if domain.get("threshold") is None else "unknown")
        domain["stale"] = not fresh
    if not fresh:
        return domains, None, "the machine's last answer is stale; levels are not assigned"
    candidates = [d for d in domains if d["level"] in RANK and not d.get("excluded")]
    if not candidates:
        return domains, None, "no domain with a threshold reported its memory"

    def tightness(d):
        # Highest level first; then the smallest headroom relative to its amber threshold (a zero amber threshold
        # never warns, so such a domain sorts after every domain with a real threshold, by its free bytes).
        amber = (limits.get(d["threshold"]) or {}).get("amber_gib")
        ratio = d["available_bytes"] / (amber * GIB) if amber else math.inf
        return (-RANK[d["level"]], ratio, d["available_bytes"])

    best = min(candidates, key=tightness)
    return domains, {k: best.get(k) for k in ("domain", "device", "label", "level", "available_bytes", "total_bytes")}, None


# -- documents (F1, F3) -------------------------------------------------------------------------------------------
HISTORY_SERIES = ("node_gpu_busy_percent", "node_gpu_power_watts", "node_gpu_power_limit_watts", "node_gpu_temp_celsius",
                  "node_cpu_busy_percent")


SNAPSHOT_MAX_AGE_S = 30.0


def cached_snapshot(api):
    """The core snapshot only if it is already cached and at most 30 s old: a GET here never refreshes it (that
    would call the nodes). Returns (snapshot or None, why)."""
    cached = getattr(api, "_cached", None)
    if not isinstance(cached, dict):
        return None, "portal snapshot not computed yet (it is computed while a core page is open)"
    stamp = (cached.get("devices") or {}).get("server_time")
    if not _finite(stamp) or api.clock() - stamp > SNAPSHOT_MAX_AGE_S:
        return None, "portal snapshot older than 30 s (no core page open)"
    return cached, None


def status_of(entry, now, interval):
    """(status, why): online / stale / offline / unknown. C10: no success within 3 intervals = stale."""
    if not entry:
        return "unknown", "no collection for this machine yet (samplers not running or first round pending)"
    last_ok = entry.get("last_ok")
    if not _finite(last_ok):
        return "offline", entry.get("error") or "never answered"
    age = now - last_ok
    if not entry.get("ok"):
        return ("offline" if age > 3 * interval else "stale"), entry.get("error") or "last call failed"
    if age > 3 * interval:
        return "stale", (entry.get("skip_why") or "no answer") + f"; last answer {int(age)} s ago"
    return "online", None


def fresh_engines(store, machine_id, now, records):
    """Loopback engine documents of this machine that are current (key in the launch records, < 3 intervals old)."""
    if store is None:
        return {}
    keys = {engine_key(m, t["profile"], t["port"]) for m, rows in loopback_engines(records).items() for t in rows}
    found = {}
    for key, _, value in store.items(ENGINES, limit=64):
        if key in keys and value.get("machine") == machine_id and _finite(value.get("ts")) and now - value["ts"] <= 3 * NODE_INTERVAL:
            found[value.get("profile")] = (key, value)
    return found


def _series_latest(store, name, labels, max_age):
    rows = store.latest(name, labels, max_age=max_age) if store is not None and store.descriptor(name) else []
    return rows[0] if rows else None


def lan_engine(store, profile, replica, now):
    """A LAN engine as the observability collector recorded it (shared engine_* series, read with the engine key
    from the control machine, C4); None when that collector has not recorded it."""
    if store is None:
        return None
    labels = {"profile": profile, "replica": replica}
    up = _series_latest(store, "engine_up", labels, 3 * NODE_INTERVAL)
    running = _series_latest(store, "engine_queue_running", labels, 3 * NODE_INTERVAL)
    waiting = _series_latest(store, "engine_queue_waiting", labels, 3 * NODE_INTERVAL)
    if up is None and running is None and waiting is None:
        return None
    rate = None
    descriptor = store.descriptor("engine_generated_tokens")
    if descriptor and _finite(descriptor.get("interval_s")):
        result = store.query("engine_generated_tokens", now - 60, now, labels=labels, tier="raw")
        points = [p for line in result["lines"] for p in line["points"]]
        if points:
            rate = sum(p[5] for p in points) / (len(points) * descriptor["interval_s"])
    model = next((row["labels"].get("model") for row in (up, running, waiting) if row and row["labels"].get("model")), None)
    return {"model": model, "running": running and running["value"], "queued": waiting and waiting["value"], "rate": rate}


def engine_rows(store, machine_id, records, control_id, now):
    """Engines and services on a machine: profile, role, model, context, queue, completed rate. A distributed
    engine is counted once, on rank 0 (rank 1 names its rank 0)."""
    rows = []
    loops = fresh_engines(store, machine_id, now, records)
    machine_rates = rates(store, machine_id, now, 3 * NODE_INTERVAL)
    for role in roles(records, control_id).get(machine_id, []):
        profile = role["profile"]
        row = {"profile": profile, "role": role["role"], "model": None, "context_length": None, "running": None,
               "queued": None, "throughput": None, "memory": None, "observer": None, "why": {}}
        fields = ("model", "context_length", "running", "queued", "throughput")
        if role["role"] == "rank 1":
            leader = (role.get("of") or [None])[0]
            row["follower_of"] = leader
            row["why"].update({k: f"rank 1: served and counted on rank 0 ({leader})" for k in fields})
            rows.append(row)
            continue
        if role["role"] in ("router", "prefill service"):
            row["why"].update({k: "reported on the engine's own machine" for k in fields})
            rows.append(row)
            continue
        if profile in loops:
            key, loop = loops[profile]
            row["observer"] = "engine via node loopback (C4)"
            status = loop.get("status") or {}
            metrics = loop.get("metrics") or {}
            row["model"] = status.get("model")
            row["context_length"] = status.get("context_tokens") or (status.get("slots") or {}).get("context_tokens")
            queue = status.get("queue") or {}
            row["running"] = queue.get("running", metrics.get("running"))
            row["queued"] = queue.get("queued", metrics.get("waiting"))
            row["memory"] = status.get("memory") if isinstance(status.get("memory"), dict) else None
            committed = machine_rates.get("engine|" + key)
            if committed and committed[2] == loop.get("instance"):
                row["throughput"], row["throughput_window_s"] = committed[0], committed[1]
                row["throughput_label"] = "completed tok/s · engine counter between two collections"
            reason = loop.get("why") or {}
            for name in fields:
                if row[name] is None:
                    row["why"][name] = reason.get("throughput" if name == "throughput" else "status") or "engine not reporting"
        else:
            lan = lan_engine(store, profile, machine_id, now)
            if lan is None:
                row["why"].update({k: "needs the engine collector of the observability feature (read with the engine key)"
                                   for k in fields})
            else:
                row["observer"] = "engine via control machine (observability collector)"
                row["model"], row["running"], row["queued"], row["throughput"] = lan["model"], lan["running"], lan["queued"], lan["rate"]
                if row["throughput"] is not None:
                    row["throughput_label"] = "completed tok/s · engine counter, last minute"
                for name in fields:
                    if row[name] is None:
                        row["why"][name] = "not reported by the engine collector"
        rows.append(row)
    return rows


# Disjoint allocations an engine reports (summed); every other *_bytes figure (device free/total, per-slot sizes,
# pools that overlap the state) is shown but never summed.
ALLOCATIONS = ("weights_bytes", "state_bytes", "workspace_bytes", "trellis_bytes", "cache_bytes", "draft_bytes")


def engine_report(domains, engines):
    """The engines' own memory reports: their disjoint allocations as bar segments; what no report covers is
    'unattributed', never inferred."""
    reports = []
    for engine in engines:
        memory = engine.get("memory") or {}
        parts = {k: memory[k] for k in ALLOCATIONS if _finite(memory.get(k))}
        other = {k: v for k, v in memory.items() if k.endswith("_bytes") and k not in ALLOCATIONS and _finite(v)}
        if parts:
            reports.append({"profile": engine["profile"], "parts": parts, "reported_bytes": sum(parts.values()),
                            "other": other, "other_label": "capacities, free space and per-slot sizes (not allocations, not summed)"})
    if not reports:
        return {"reports": [], "unattributed_bytes": None, "why": "no engine on this machine reports its memory"}
    candidates = [d for d in domains if d["domain"] in ("gpu-visible", "vram")]
    reported = sum(r["reported_bytes"] for r in reports)
    if len(candidates) != 1 or not _finite(candidates[0].get("used_bytes")):
        return {"reports": reports, "unattributed_bytes": None,
                "why": "the reports do not name their device and this machine has " + (
                    "several GPU domains" if len(candidates) > 1 else "no GPU domain with a used figure")}
    gpu = candidates[0]
    totals = [r["other"].get("device_total_bytes") for r in reports]
    if not all(_finite(total) and _finite(gpu.get("total_bytes")) and abs(total - gpu["total_bytes"]) <= 0.02 * gpu["total_bytes"]
               for total in totals):
        return {"reports": reports, "domain": gpu["domain"], "unattributed_bytes": None,
                "why": "the engine report does not identify its memory domain (its device total does not match "
                       + gpu["label"] + "); nothing is inferred"}
    if reported > gpu["used_bytes"]:
        return {"reports": reports, "domain": gpu and gpu["domain"], "unattributed_bytes": None,
                "why": "the reports do not fit the GPU domain's used bytes; nothing is inferred"}
    return {"reports": reports, "domain": gpu["domain"], "device": gpu.get("device"), "used_bytes": gpu["used_bytes"],
            "unattributed_bytes": gpu["used_bytes"] - reported,
            "label": "used in this GPU domain but covered by no engine report"}


def gpu_cards(checks, domains):
    gpus = []
    for card in _value(checks.get("gpus")) or []:
        why = dict(card.get("why") or {})
        why["throttle"] = "the AMD driver reports no throttle state in its sysfs text files"
        if card.get("power_cap_w") is None:
            why["power_limit_w"] = why.pop("power_cap_w", "no readable power cap for this card")
        gpus.append({"kind": "amd", "device": card.get("card"), "name": "AMD GPU · " + str(card.get("card")),
                     "busy_percent": card.get("busy_percent"),
                     "temperatures": [{"sensor": "amdgpu/" + str(t.get("sensor")), "celsius": t.get("celsius")}
                                      for t in card.get("temps") or []],
                     "power_w": card.get("power_w"), "power_sensor": card.get("power_sensor"),
                     "power_limit_w": card.get("power_cap_w"), "clock_mhz": card.get("sclk_mhz"), "throttle": None,
                     "observer": "node (sysfs)", "why": why})
    for card in _value(checks.get("nvidia")) or []:
        gpus.append({"kind": "nvidia", "device": card["device"], "name": card.get("name"), "index": card.get("index"),
                     "busy_percent": card.get("busy_percent"),
                     "temperatures": [{"sensor": "gpu", "celsius": card.get("temperature_c")}],
                     "power_w": card.get("power_w"), "power_sensor": "board", "power_limit_w": card.get("power_limit_w"),
                     "clock_mhz": card.get("clock_mhz"), "throttle": THROTTLE_LEGEND.get(card.get("throttle")),
                     "throttle_reasons": card.get("throttle_reasons"), "pstate": card.get("pstate"),
                     "pcie": {"gen": card.get("pcie_gen"), "width": card.get("pcie_width")},
                     "processes": card.get("processes"), "observer": "control machine (nvidia-smi)",
                     "excluded": next((bool(d.get("excluded")) for d in domains if d.get("device") == card["device"]), False),
                     "why": card.get("why") or {}})
    return gpus


def machine_view(api, machine, store, now, records, control_id):
    """Everything both documents show about one machine (no node call, no store write)."""
    entry = store.get(HOSTS, machine.id) if store is not None else None
    interval = CONTROL_INTERVAL if machine.access == "local" else NODE_INTERVAL
    status, status_why = status_of(entry, now, interval)
    limits = thresholds(store)
    domains, limiting, limiting_why = memory_domains(machine, entry, limits, fresh=status == "online")
    checks = ((entry or {}).get("host") or {}).get("checks") or {}
    engines = engine_rows(store, machine.id, records, control_id, now)
    return {"entry": entry, "checks": checks, "status": status, "status_why": status_why, "interval": interval,
            "limits": limits, "domains": domains, "limiting": limiting, "limiting_why": limiting_why,
            "engines": engines, "gpus": gpu_cards(checks, domains)}


def machine_document(api, rest, query):
    if query:
        raise ValueError("machines/<id> takes no query arguments")
    cluster = api.cluster()
    machine = cluster.machines.get(rest) if isinstance(rest, str) else None
    if machine is None:
        raise LookupError("unknown machine")
    store = telemetry.existing_store(api)
    now = api.clock()
    control = control_machine(cluster)
    view = machine_view(api, machine, store, now, launch_records(api), control.id if control else None)
    entry, checks = view["entry"], view["checks"]
    why = {}
    if store is None:
        why["host"] = "telemetry store not open in this portal process (samplers disabled)"
    elif not entry:
        why["host"] = "no collection for this machine yet"
    nvidia = checks.get("nvidia")
    if nvidia is not None and _value(nvidia) is None:
        why["nvidia"] = _why(nvidia)
    if not view["gpus"]:
        why["gpus"] = _why(checks.get("gpus"), "") or _why(nvidia, "") or "no GPU reported by this machine"
    processes = _value(checks.get("gpu_processes"))
    if processes is None and _value(nvidia):
        processes = [{"device": card["device"], **p} for card in _value(nvidia) for p in card.get("processes") or []]
    if processes is None:
        why["processes"] = _why(checks.get("gpu_processes"), "no GPU process list on this machine")
    cpu = _value(checks.get("cpu")) or {}
    chips = [c for c in _value(checks.get("sensors")) or [] if c.get("name") != "amdgpu"]
    prefixes = sensor_names(chips)
    temps = [{"sensor": _sensor(prefixes[chip.get("hwmon")], label), "celsius": temp.get("celsius"),
              "kind": "cpu" if chip.get("name") in CPU_CHIPS else "other"}
             for chip in chips for temp, label in sensor_labels(chip.get("temps") or [])]
    load = _value(checks.get("load"))
    cpu_doc = {"busy_percent": cpu.get("busy_percent"), "cores": cpu.get("cores"), "load": load,
               "temperatures": temps, "why": {}}
    if cpu_doc["busy_percent"] is None:
        cpu_doc["why"]["busy_percent"] = (cpu.get("why") or {}).get("busy_percent") or _why(checks.get("cpu"))
    if load is None:
        cpu_doc["why"]["load"] = _why(checks.get("load"))
    if not temps:
        cpu_doc["why"]["temperatures"] = _why(checks.get("sensors"), "no temperature sensors reported")
    committed = rates(store, machine.id, now, 3 * NODE_INTERVAL + view["interval"])

    def rate(name, label):
        found = committed.get(machine.id + "|" + name + "|" + label)
        return found[0] if found and found[2] == str((entry or {}).get("instance")) else None

    net = []
    for row in _value(checks.get("net")) or []:
        label = "iface=" + str(row.get("iface"))
        rx, tx = rate("node_net_rx_bytes", label), rate("node_net_tx_bytes", label)
        net.append({"iface": row.get("iface"), "physical": row.get("physical"), "operstate": row.get("operstate"),
                    "rx_bytes_per_s": rx, "tx_bytes_per_s": tx,
                    "why": {k: "requires two committed counter samples of the same boot" for k, v in (("rx_bytes_per_s", rx), ("tx_bytes_per_s", tx)) if v is None}})
    if not net:
        why["net"] = _why(checks.get("net"), "no interfaces reported")
    disks = [{k: row.get(k) for k in ("mount", "fstype", "total_bytes", "used_bytes", "free_bytes")}
             for row in _value(checks.get("disks")) or []]
    if not disks:
        why["disks"] = _why(checks.get("disks"), "no filesystems reported")
    disk_io = []
    for row in _value(checks.get("diskstats")) or []:
        label = "device=" + str(row.get("device"))
        read, write = rate("node_disk_read_bytes", label), rate("node_disk_write_bytes", label)
        disk_io.append({"device": row.get("device"), "read_bytes_per_s": read, "write_bytes_per_s": write,
                        "why": {k: "requires two committed counter samples of the same boot" for k, v in (("read_bytes_per_s", read), ("write_bytes_per_s", write)) if v is None}})
    if not disk_io:
        why["disk_io"] = _why(checks.get("diskstats"), "no disk counters reported")
    events = store.events(machine=machine.id, limit=20) if store is not None else None
    if events is None:
        why["events"] = "telemetry store not open in this portal process"
    history = {name: f"history/{name}?l.machine={machine.id}" for name in HISTORY_SERIES}
    return api.document(
        "machine", machine={"id": machine.id, "os": machine.os, "access": machine.access, "control": machine.access == "local"},
        status=view["status"], status_why=view["status_why"], last_ok=(entry or {}).get("last_ok"),
        uptime_s=_value(checks.get("uptime_s")), gpus=view["gpus"],
        memory={"domains": view["domains"], "limiting": view["limiting"], "limiting_why": view["limiting_why"],
                "engine_report": engine_report(view["domains"], view["engines"])},
        processes=processes, cpu=cpu_doc, net=net, disks=disks, disk_io=disk_io, engines=view["engines"], events=events,
        history=history, thresholds=view["limits"], interval_s=view["interval"],
        machines=[m for m in cluster.machines], why=why)


def register(reg):
    reg.collector("fleet-nodes", sample_nodes, interval=NODE_INTERVAL)
    reg.collector("fleet-control", sample_control, interval=CONTROL_INTERVAL)
    reg.get_prefix("machines/", machine_document)
    reg.get("fleet/thresholds", get_thresholds)
    reg.post("fleet/thresholds/set", set_thresholds, audited=True)
