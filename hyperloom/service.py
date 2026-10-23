"""Optional non-lab mode (off by default): engines as systemd --user units on the nodes.

Only for profiles whose every machine sets ``lab_mode: false``; lab machines start GPU work through
leases only. The units wrap the same node supervise with no turn and no deadline. The router is not
part of a unit: start it with ``hyperloom router start`` (see docs/non-lab-mode.md).
"""

from copy import deepcopy

from . import orchestrate, render, secrets
from .orchestrate import OrchestrationError, _call, _client, _node_name, _profile, _put, _release, _secret_name, _url


OPERATIONS = ("install", "start", "stop", "status", "remove")


def _machines(cluster, profile):
    settings, kind, mids = _profile(cluster, profile)
    if kind != "qwen-replicas":
        raise OrchestrationError("non-lab service mode supports qwen-replicas only; the DeepSeek pair needs a "
                                 "peer turn and stays lease-only")
    lab = [mid for mid in mids if cluster.machines[mid].lab_mode]
    if lab:
        raise OrchestrationError("non-lab service mode refused: lab_mode is true (the default) on "
                                 + ", ".join(lab) + "; lab machines run GPU work through leases only")
    return settings, mids


def _processes(cluster, profile, settings, mids, clients):
    """Render the profile like up() does, with the same per-node keys, for the unit specs."""
    releases = {mid: _release(clients[mid], cluster.machines[mid], profile, settings.get("release_id"))
                for mid in mids}
    if len(set(releases.values())) != 1:
        raise OrchestrationError("profile nodes must run the same release id")
    selected = deepcopy(cluster)
    selected.profiles[profile]["release_id"] = releases[mids[0]]
    paths = {mid: _put(clients[mid], cluster.machines[mid], _secret_name(profile, mid),
                       secrets.ensure(_secret_name(profile, mid))) for mid in mids}
    selected.profiles[profile]["api_key_files"] = paths
    result = {}
    for spec in render.render_processes(selected, profile):
        machine = cluster.machines[spec.machine_id]
        port = int(spec.argv[spec.argv.index("--http-port") + 1])
        result[spec.machine_id] = {"name": _node_name(spec.machine_id), "machine": "local", "argv": spec.argv,
                                   "env": spec.env, "ready": spec.ready, "key_file": paths[spec.machine_id],
                                   "health_url": _url({"address": machine.http_address, "port": port}, "/health"),
                                   "ready_timeout_s": 600}
    return releases[mids[0]], result


def run(cluster, operation, profile, *, enable=False):
    if operation not in OPERATIONS:
        raise ValueError("unknown service operation: " + operation)
    settings, mids = _machines(cluster, profile)
    clients = {mid: _client(cluster.machines[mid]) for mid in mids}
    result = {"profile": profile, "mode": "non-lab", "nodes": {}}
    if operation == "install":
        release, processes = _processes(cluster, profile, settings, mids, clients)
        result["release_id"] = release
    for mid in mids:
        payload = {"root": cluster.machines[mid].install_root, "profile": profile}
        if operation == "install":
            payload.update(processes=[processes[mid]], enable=enable)
        reply = _call(clients[mid], "service-" + operation, payload)
        result["nodes"][mid] = {key: value for key, value in reply.items() if key not in ("schema", "verb")}
    return result
