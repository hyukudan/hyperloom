"""Optional non-lab mode (off by default): engines as systemd --user units on the nodes.

Only for profiles whose every machine sets ``lab_mode: false``; lab machines start GPU work through
leases only. The units wrap the same node supervise with no turn and no deadline. The router is not
part of a unit: start it with ``hyperloom router start`` (see docs/non-lab-mode.md).
"""

from copy import deepcopy
import time

from . import remote, render
from .orchestrate import OrchestrationError, _call, _client, _node_name, _profile, _url, prelaunch


OPERATIONS = ("install", "start", "stop", "status", "remove")
# Above the unit's TimeoutStopSec (90 s): the node only queues the stop (systemctl --no-block); this side waits.
STOP_WAIT_S = 120
STOPPED = {"inactive", "failed"}


def _machines(cluster, profile):
    settings, kind, mids = _profile(cluster, profile)
    if kind != "qwen-replicas":
        raise OrchestrationError("non-lab service mode supports qwen-replicas only; the DeepSeek pair needs a "
                                 "peer turn and stays lease-only")
    lab = [mid for mid in mids if cluster.machines[mid].lab_mode]
    if lab:
        raise OrchestrationError("non-lab service mode refused: lab_mode is true (the default) on "
                                 + ", ".join(lab) + "; lab machines run GPU work through leases only")
    return settings, kind, mids


def _processes(cluster, profile, settings, kind, mids, clients):
    """Run the same pre-launch checks and key push as up(), then render the processes for the unit specs."""
    prepared = prelaunch(cluster, profile, settings, kind, clients, mids)
    selected = deepcopy(cluster)
    selected.profiles[profile]["release_id"] = prepared["release_id"]
    selected.profiles[profile]["api_key_files"] = paths = prepared["node_paths"]
    result = {}
    for spec in render.render_processes(selected, profile):
        machine = cluster.machines[spec.machine_id]
        port = int(spec.argv[spec.argv.index("--http-port") + 1])
        result[spec.machine_id] = {"name": _node_name(spec.machine_id), "machine": "local", "argv": spec.argv,
                                   "env": spec.env, "ready": spec.ready, "key_file": paths[spec.machine_id],
                                   "health_url": _url({"address": machine.http_address, "port": port}, "/health"),
                                   "ready_timeout_s": 600}
    return prepared["release_id"], result


def _node_call(nodes, client, mid, verb, payload):
    """One node's reply, or its error: a failing node never hides the others' results."""
    try:
        reply = _call(client, verb, payload)
    except (OrchestrationError, remote.RemoteError, OSError, ValueError) as exc:
        nodes[mid] = {"error": str(exc)}
        return None
    nodes[mid] = {key: value for key, value in reply.items() if key not in ("schema", "verb")}
    return reply


def _wait_stopped(cluster, profile, clients, nodes, stop_wait_s, poll_s):
    pending = [mid for mid, row in nodes.items() if "error" not in row]
    limit = time.monotonic() + stop_wait_s
    while pending:
        for mid in list(pending):
            payload = {"root": cluster.machines[mid].install_root, "profile": profile}
            try:
                state = (_call(clients[mid], "service-status", payload).get("systemd") or {}).get("ActiveState")
            except (OrchestrationError, remote.RemoteError, OSError, ValueError) as exc:
                nodes[mid]["error"] = "stop requested; status failed: " + str(exc)
                pending.remove(mid)
                continue
            nodes[mid]["active_state"] = state
            if state in STOPPED:
                pending.remove(mid)
        if not pending:
            break
        if time.monotonic() >= limit:
            for mid in pending:
                nodes[mid]["error"] = f"unit still {nodes[mid].get('active_state')} after {stop_wait_s} s"
            break
        time.sleep(poll_s)


def run(cluster, operation, profile, *, enable=False, stop_wait_s=STOP_WAIT_S, poll_s=2.0):
    """Apply one operation on every profile node; per-node results (or errors) are all reported, ok is False
    when any node failed."""
    if operation not in OPERATIONS:
        raise ValueError("unknown service operation: " + operation)
    settings, kind, mids = _machines(cluster, profile)
    clients = {mid: _client(cluster.machines[mid]) for mid in mids}
    result = {"profile": profile, "mode": "non-lab", "nodes": {}}
    if operation == "install":
        release, processes = _processes(cluster, profile, settings, kind, mids, clients)
        result["release_id"] = release
    for mid in mids:
        payload = {"root": cluster.machines[mid].install_root, "profile": profile}
        if operation == "install":
            payload.update(processes=[processes[mid]], enable=enable)
        _node_call(result["nodes"], clients[mid], mid, "service-" + operation, payload)
    if operation == "stop":
        _wait_stopped(cluster, profile, clients, result["nodes"], stop_wait_s, poll_s)
    result["ok"] = not any("error" in row for row in result["nodes"].values())
    return result
