"""Pure startup checks for cuda_ds41_replica.cu at a919c257; no engine or model-body reads.

Placement supplies capacities. Native input files and request controls still need valid argv and readable inputs
before admission. Keep this inventory aligned with the published replica when enabling its placement capability.
"""
from pathlib import Path
from decimal import Decimal
from fractions import Fraction
import math
import os
import re
import struct

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
OUTPUTS = frozenset(("--json", "--timeline", "--draft-trace", "--controller-save", "--residency-save"))
DEFAULT_PHASES = "exact0,pressure,exact8,chats8,chats0"
UINTS = frozenset(("--steps", "--chat-prompt", "--chat-steps", "--verify", "--warm", "--pressure-free",
    "--pressure-recent", "--prefetch-layer", "--prefetch-token", "--controller-explore", "--winpf-l1-cap",
    "--winpf-prior-cap", "--winpf-revert", "--wb-alt", "--serve-max"))
FLOATS = {"--winpf-pmin": 32, "--winpf-prior-pmin": 32, "--serve-idle": 64}
ENUMS = {"--controller-scope": frozenset(("run", "chat")), "--reset-cache": frozenset(("warm", "open"))}
REQUEST_ENV = frozenset(("STRIX_DS41_CUDA_ENGRAM_CHECK", "STRIX_DS41_REPLICA_WINDOW_ENGRAM_PREFETCH"))
# The published adapter is Windows: unsigned long (std::stoul) is 32 bits, even on Win64.
# These conversions intentionally consume prefixes, just like std::sto* with no end-position argument.
_INTEGER = re.compile(r"[ \t\v\f\r\n]*([+-]?)([0-9]+)")
_REAL = re.compile(r"[ \t\v\f\r\n]*([+-]?(?:infinity|inf|nan(?:\([a-z0-9_]*\))?"
                   r"|0x(?:[0-9a-f]+(?:\.[0-9a-f]*)?|\.[0-9a-f]+)(?:p[+-]?[0-9]+)?"
                   r"|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?))", re.IGNORECASE | re.ASCII)


def _bad(option):
    raise RecipeError("PL_E_SCHEMA bad native value of " + option)


def _uint(value, option, bits=32):
    match = _INTEGER.match(value)
    if match is None:
        _bad(option)
    # Bound the string before int(): Python's decimal digit limit must not leak out of preflight.
    digits = match[2].lstrip("0") or "0"
    if len(digits) > len(str((1 << bits) - 1)) or int(digits) > (1 << bits) - 1:
        _bad(option)
    return (-int(digits) if match[1] == "-" else int(digits)) % (1 << bits)


def _f32(value):
    try:
        return struct.unpack("f", struct.pack("f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _magnitude(token):
    if not token.startswith("0x"):
        return Fraction(Decimal(token))
    mantissa, separator, exponent = token[2:].partition("p")
    whole, _, fractional = mantissa.partition(".")
    power = int(exponent.lstrip("+-").lstrip("0") or "0") if separator else 0
    if exponent.startswith("-"):
        power = -power
    return Fraction(int(whole + fractional, 16), 16 ** len(fractional)) * Fraction(2) ** power


def _real(value, option, bits=64):
    match = _REAL.match(value)
    if match is None:
        _bad(option)
    token = match[1].lower().lstrip("+-")
    if token.startswith(("inf", "nan")):
        return math.nan if token.startswith("nan") else math.copysign(math.inf, -1 if match[1][0] == "-" else 1)
    try:
        number = float.fromhex(match[1]) if token.startswith("0x") else float(match[1])
    except (ValueError, OverflowError):
        _bad(option)
    if bits == 32 and abs(number) in (2.0 ** -150, 2.0 ** 128 - 2.0 ** 103):
        # Avoid double rounding at float's zero/infinity boundaries: strtof rounds the original text directly.
        exact = _magnitude(token)
        if exact <= 2.0 ** -150 or exact >= 2.0 ** 128 - 2.0 ** 103:
            _bad(option)
        return math.copysign(2.0 ** -149 if abs(number) == 2.0 ** -150 else (2 - 2.0 ** -23) * 2.0 ** 127,
                             number)
    number = _f32(number) if bits == 32 else number
    # Windows strtof/strtod accept representable subnormals; ERANGE covers overflow and underflow to zero.
    mantissa = token[2:].split("p", 1)[0] if token.startswith("0x") else token.split("e", 1)[0]
    nonzero = any(c in ("123456789abcdef" if token.startswith("0x") else "123456789") for c in mantissa)
    if math.isinf(number) or (number == 0 and nonzero):
        _bad(option)
    return number


def _split(value, separator=","):
    return [part for part in value.split(separator) if part]  # native split() skips empty fields


def _prior_path(value):
    path, colon, cap = value.rpartition(":")
    if colon and len(path) > 1 and cap and cap.isascii() and cap.isdigit():
        _uint(cap, "--controller-prior")
        return path
    return value


def _value(option, value):
    if option in UINTS:
        _uint(value, option)
    elif option in FLOATS:
        _real(value, option, FLOATS[option])
    elif option in ENUMS:
        if value not in ENUMS[option]:
            _bad(option)
    elif option == "--window":
        if value.startswith("fixed:"):
            _uint(value[6:], option)
        elif value not in ("controller", "probe"):
            _bad(option)
    elif option == "--env":
        name, equal, _ = value.partition("=")
        if not equal or name not in REQUEST_ENV:
            _bad(option)
    elif option == "--chats":
        for chat in _split(value):
            _uint(chat, option)
    elif option == "--seqmod":
        fields = _split(value, ":")
        if len(fields) < 2:
            _bad(option)
        for field in fields[:2]:
            _uint(field, option)
    elif option == "--controller-prior":
        _prior_path(value)
    elif option == "--controller-costs" and value != "none":
        costs = _split(value)
        if len(costs) not in (6, 7):
            _bad(option)
        for i, cost in enumerate(costs):
            number = _real(cost, option)
            if i < 6 and not _f32(number * 1e-3) > 0:
                _bad(option)


def _phases(value):
    phases = _split(value)
    for phase in phases:
        if not re.fullmatch(r"pressure[0-9]*|(?:exact|csmoke|gsmoke|smokep?)[0-9]+"
                            r"|(?:wforcep?|spec|wsmokep?)[0-9]+w?|(?:chats|dchats)[0-9]+(?:b[0-9]+)?"
                            r"|schats[0-9]+(?:b[0-9]+)?(?:f[1-5])?w?", phase):
            _bad("--phases")
        for digits in re.findall(r"[0-9]+", phase):
            _uint(digits, "--phases")
        # Every valid native row count is at least one. Check even overwritten --phases occurrences.
        first = re.search(r"[0-9]+", phase)
        if first is not None and _uint(first[0], "--phases") > 64:
            _bad("--phases")
    return phases


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


def _output_available(value, option):
    # Native opens these files without making parents. Check known failures without creating/truncating outputs;
    # the eventual open/rename remains authoritative for ACLs, sharing violations and filesystem races.
    try:
        paths = [Path(value).resolve()]
        if option == "--residency-save":
            paths.append(Path(value + ".tmp").resolve())
            if not os.access(paths[0].parent, os.W_OK):
                raise OSError("output parent is not writable")
        for path in paths:
            if path.exists():
                if not path.is_file() or not os.access(path, os.W_OK):
                    raise OSError("output is not a writable regular file")
            elif not path.parent.is_dir() or not os.access(path.parent, os.W_OK):
                raise OSError("output parent is missing or not writable")
    except (OSError, ValueError) as exc:
        raise RecipeError("PL_E_SCHEMA unavailable native output: " + option) from exc


def _capacities(phases, config, allocation_flags):
    """Use admitted capacities when available; never derive allocation flags from request argv."""
    if not isinstance(config, dict):
        _bad("placement config")
    rows = config.get("max_rows")
    positions = config.get("max_context_tokens")
    for name, value in (("--max-rows", rows), ("--max-positions", positions)):
        if value is not None and (type(value) is not int or not 0 < value <= 0xffffffff):
            _bad(name)
    window = any(p.startswith(("wforce", "spec", "schats", "wsmoke", "gsmoke", "csmoke")) for p in phases)
    draft = any(p.startswith(("spec", "schats")) for p in phases)
    if rows is not None and (rows > 8 or (window and rows < 2) or (draft and rows > 6)):
        _bad("--max-rows for --phases")
    predict = None
    if allocation_flags is not None:
        if not isinstance(allocation_flags, dict):
            _bad("cached allocation flags")
        # The components reference binds all allocation-affecting flags; absent --predict uses native default 8.
        predict = max(8, _uint(str(allocation_flags.get("--predict", 8)), "cached --predict"))
        if (rows or 1) * predict > 64:
            _bad("--max-rows x cached --predict")
    for phase in phases:
        digits = re.search(r"[0-9]+", phase)
        k = _uint(digits[0], "--phases") if digits else 0
        if (rows or 1) * k > 64 or (predict is not None and k > predict):
            _bad("--phases K / --max-rows / cached --predict")
    return positions


def validate(argv, *, config=None, allocation_flags=None):
    """Validate native arity, required launch inputs and every input occurrence, without publishing anything."""
    if (not argv or any(not isinstance(a, str) or not a or "\x00" in a or "\n" in a or "\r" in a for a in argv)
            or argv[0].startswith("--")):
        raise RecipeError("PL_E_SCHEMA replica requires the positional model before options and nonempty argv tokens")
    inputs = [("model", argv[0])]
    options, occurrences = {}, {}
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
        _value(option, value)
        if option in OUTPUTS:
            _output_available(value, option)
        if option == "--phases":
            _phases(value)
        options[option] = value
        occurrences.setdefault(option, []).append(value)
        if option in INPUTS:
            inputs.append((option, value))
        index += 2

    def require(option):
        if option not in options:
            raise RecipeError("PL_E_SCHEMA replica requires " + option)

    require("--import-state")
    # Placement uses the full model; --layers is placement-owned, so the smoke-only receipt exemption cannot apply.
    require("--receipt")
    # Do not let a later occurrence hide invalid syntax, ranges, or requirements of an earlier one.
    phases = [phase for value in occurrences.get("--phases", [DEFAULT_PHASES]) for phase in _phases(value)]
    positions = _capacities(phases, {} if config is None else config, allocation_flags)
    for phase in phases:
        if phase.startswith(("exact", "pressure", "wforce", "spec")):
            require("--ref")
        if phase.startswith(("chats", "dchats", "schats")):
            require("--chats")
            require("--seqs-file")
        if phase.startswith(("spec", "schats")):
            require("--draft")
        if phase.startswith(("smoke", "wsmoke", "csmoke", "gsmoke")):
            raise RecipeError("PL_E_SCHEMA replica smoke phases require a partial model; placement owns --layers")
    steps = [_uint(v, "--steps") for v in occurrences.get("--steps", ["64"])]
    if max(steps) + 16 > 0x7fffffff or (any(p.startswith(("exact", "pressure", "wforce", "spec")) for p in phases)
                                          and min(steps) < 64):
        _bad("--steps")
    if any(p.startswith(("chats", "dchats", "schats")) for p in phases):
        prompt = max(_uint(v, "--chat-prompt") for v in occurrences.get("--chat-prompt", ["96"]))
        count = max(_uint(v, "--chat-steps") for v in occurrences.get("--chat-steps", ["128"]))
        if prompt + count + 16 > 0x7fffffff:
            _bad("--chat-prompt / --chat-steps")
    else:
        prompt = count = 0
    # The imported position is engine-owned, but even position zero cannot fit a larger request.
    if positions is not None and max(max(steps) + 16, prompt + count + 16) > positions:
        _bad("--max-positions below what --phases need")
    if (any(v != "none" for v in occurrences.get("--controller-costs", []))
            and "none" in occurrences.get("--controller-prior", [])):
        _bad("--controller-costs needs --controller-prior")
    for option, value in inputs:
        if option == "--controller-prior":
            # Native FILE[:CAP] parsing must preserve a Windows drive colon. Only these two values are built-ins.
            if value == "none":
                continue
            value = _prior_path(value)
            if value == "builtin":
                continue
        _available(value, option)
