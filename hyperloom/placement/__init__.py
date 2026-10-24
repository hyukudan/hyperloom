"""Per-family VRAM memory models for the placement planner (hyperloom/vramplan.py).

`plan(engine, model, inputs, **params)` is the only entry point the rest of hyperloom uses: a service preset names
its placement engine as data, so rtx.py stays model-agnostic.
"""
from __future__ import annotations

from .. import vramplan

ENGINES = ("minimax-rtx-prefill", "qwen-rtx-prefill", "ds41-rtx-prefill", "ds41-rtx-front", "ds41-rtx-hot",
           "ds41-node", "desc-v1")


def plan(engine, model, inputs, **params):
    if engine == "minimax-rtx-prefill":
        from . import minimax
        return vramplan.plan(minimax.model(model, **params), inputs)
    if engine == "qwen-rtx-prefill":
        from . import qwen
        return vramplan.plan_first(qwen.model(model, **params), inputs)
    if engine == "ds41-rtx-prefill":
        from . import ds41
        if inputs.host_gib is None:
            raise vramplan.PlanError("ds41-rtx-prefill: host_gib is required (the pinned host tier)")
        return ds41.predict_prefill(model, vram_cap_gib=inputs.vram_cap_gib, host_gib=inputs.host_gib,
                                    margin_gib=inputs.margin_gib, margin_fraction=inputs.margin_fraction, **params)
    if engine == "ds41-rtx-front":
        from . import ds41
        return vramplan.plan(ds41.front_model(model, **params), inputs)
    if engine == "ds41-rtx-hot":
        from . import ds41
        return vramplan.plan(ds41.hot_model(model, **params), inputs)
    if engine == "ds41-node":
        from . import ds41
        return vramplan.plan(ds41.node_model(model, **params), inputs)
    if engine == "desc-v1":
        from . import desc
        return vramplan.plan(desc.model(model, **params), inputs)
    raise vramplan.PlanError(f"unknown placement engine {engine}; known: {', '.join(ENGINES)}")
