"""Read-only host and USB4 link telemetry (CPU/sensors, network, disks, GPU processes; USB4 sysfs, published layout, interrupt counts, kernel evidence). Never opens a stream device.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).
"""

VERBS = {"host", "usb4"}


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return {"schema": "hyperloom/node-v1", "verb": verb, "error": "not implemented in this node tool"}
