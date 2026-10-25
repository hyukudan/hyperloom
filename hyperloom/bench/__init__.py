"""Benchmarks and evaluation against served chat-completions-compatible endpoints (F24-F32).

Every run kind (decode, prefill, ab, quality, tools, demo) is a portal job admitted through the lab-wide admission
database (contract C11) or a CLI run holding the same kind of admission; runs never deploy, switch a router
policy or take a GPU turn. Modules:

- ``client``: stdlib streaming client (SSE, no redirects, bounded, cancellable).
- ``targets``: running profiles' routers and the allow-listed ad-hoc endpoints; resources; identity.
- ``runs``: the run lifecycle (validation, token budget reservations, live state, CLI context and hold).
- ``store``: runs persisted in the telemetry store (retention, listing).
- ``decode``, ``prefill``, ``ab`` (exactness receipts, C12), ``quality``, ``tools``, ``demo``: the kinds.
- ``history``: comparisons and the share summary; ``guard``: the read-only admission view.
"""

import json
from pathlib import Path


DATA = Path(__file__).with_name("data")


def data(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))
