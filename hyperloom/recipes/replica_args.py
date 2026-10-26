"""Pure startup checks for cuda_ds41_replica.cu at a919c257; no engine or model-body reads.

Placement supplies capacities. Native input files and request controls still need valid argv and readable inputs
before admission. Keep this inventory aligned with the published replica when enabling its placement capability.
"""
from pathlib import Path
import re

from .reader import RecipeError


INPUTS = frozenset(("--import-state", "--receipt", "--ref", "--seqs-file", "--routes",
                    "--residency-seed", "--winpf-prior", "--controller-prior"))
VALUES = INPUTS | frozenset(("--steps", "--phases", "--seqmod", "--chats", "--chat-prompt", "--chat-steps",
    "--json", "--timeline", "--verify", "--warm", "--pressure-free", "--pressure-recent", "--prefetch-layer",
    "--prefetch-token", "--window", "--controller-scope", "--draft-trace", "--controller-save",
    "--controller-costs", "--controller-explore", "--probe-phase", "--winpf-l1-cap", "--winpf-pmin",
    "--winpf-prior-cap", "--winpf-prior-pmin", "--winpf-revert", "--wb-alt", "--serve", "--serve-idle",
    "--serve-max", "--residency-save", "--reset-cache", "--env"))
FLAGS = frozenset(("--controller-prior-alt", "--controller-learn", "--flush-ready",
                   "--no-flush-ready", "--wb-defer", "--no-wb-defer", "--serve-keep-on-fail", "--qual"))
DEFAULT_PHASES = "exact0,pressure,exact8,chats8,chats0"


def _available(value, option):
    try:
        path = Path(value)
        if not path.is_file():
            raise OSError("not a regular file")
        # stat alone cannot establish readability. Open without reading model/state contents or hashing them.
        with path.open("rb"):
            pass
    except (OSError, ValueError) as exc:
        raise RecipeError("PL_E_STALE replica input is missing or unreadable: " + option) from exc


def validate(argv):
    """Validate native arity, required launch inputs and every input occurrence, without publishing anything."""
    if (not argv or any(not isinstance(a, str) or not a or "\x00" in a or "\n" in a or "\r" in a for a in argv)
            or argv[0].startswith("--")):
        raise RecipeError("PL_E_SCHEMA replica requires the positional model before options and nonempty argv tokens")
    inputs = [("model", argv[0])]
    options = {}
    index = 1
    while index < len(argv):
        option = argv[index]
        if option in FLAGS:
            index += 1
            continue
        if option not in VALUES:
            raise RecipeError("PL_E_SCHEMA unknown replica option (use separate option values): " + option)
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise RecipeError("PL_E_SCHEMA missing value of " + option)
        value = argv[index + 1]
        options[option] = value
        if option in INPUTS:
            inputs.append((option, value))
        index += 2

    def require(option):
        if option not in options:
            raise RecipeError("PL_E_SCHEMA replica requires " + option)

    require("--import-state")
    # Placement uses the full model; --layers is placement-owned, so the smoke-only receipt exemption cannot apply.
    require("--receipt")
    phases = options.get("--phases", DEFAULT_PHASES).split(",")
    for phase in phases:
        if not re.fullmatch(r"pressure[0-9]*|(?:exact|csmoke|gsmoke|smokep?)[0-9]+"
                            r"|(?:wforcep?|spec|wsmokep?)[0-9]+w?|(?:chats|dchats)[0-9]+(?:b[0-9]+)?"
                            r"|schats[0-9]+(?:b[0-9]+)?(?:f[1-5])?w?", phase):
            raise RecipeError("PL_E_SCHEMA unknown replica phase " + phase)
        if phase.startswith(("exact", "pressure", "wforce", "spec")):
            require("--ref")
        if phase.startswith(("chats", "dchats", "schats")):
            require("--chats")
            require("--seqs-file")
        if phase.startswith(("spec", "schats")):
            require("--draft")
        if phase.startswith(("smoke", "wsmoke", "csmoke", "gsmoke")):
            raise RecipeError("PL_E_SCHEMA replica smoke phases require a partial model; placement owns --layers")
    for option, value in inputs:
        if option == "--controller-prior":
            # Native FILE[:CAP] parsing must preserve a Windows drive colon. Only these two values are built-ins.
            if value == "none":
                continue
            path, colon, cap = value.rpartition(":")
            if colon and len(path) > 1 and cap.isascii() and cap.isdigit():
                value = path
            if value == "builtin":
                continue
        _available(value, option)
