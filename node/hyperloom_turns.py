"""Read-only view of the lab GPU-turn queue (holder, live tickets, recent turns).

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).
"""

VERBS = {"turns"}


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    return {"schema": "hyperloom/node-v1", "verb": verb, "error": "not implemented in this node tool"}
