"""Read-only host telemetry: CPU and sensors, memory domains, network, disks, GPU processes, listeners and the
read-only engine status path for loopback engines.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).
"""

VERBS = {"host"}


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return {"schema": "hyperloom/node-v1", "verb": verb, "error": "not implemented in this node tool"}
