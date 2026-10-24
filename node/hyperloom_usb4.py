"""Read-only USB4 link telemetry: thunderbolt sysfs link speed/lanes, the published stream layout, ring interrupt
counts and kernel evidence lines. Never opens a stream device and never writes configfs.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).
"""

VERBS = {"usb4"}


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return {"schema": "hyperloom/node-v1", "verb": verb, "error": "not implemented in this node tool"}
