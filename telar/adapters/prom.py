"""Parse Prometheus text samples into (name, sorted label pairs) keys."""

import re

from . import AdapterError


_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\\n]|\\[\\"n])*)"\s*')
_VALUE = r"(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]?Inf|NaN)"
_TAIL = re.compile(r"\s+(" + _VALUE + r")(?:\s+[+-]?\d+)?\s*")


def _labels(text):
    labels = {}
    pos = 0
    if not text.strip():
        return ()
    while pos < len(text):
        match = _LABEL.match(text, pos)
        if not match:
            raise AdapterError("invalid metric labels")
        name, value = match.groups()
        if name in labels:
            raise AdapterError("duplicate metric label")
        labels[name] = re.sub(r'\\([\\"n])', lambda m: "\n" if m[1] == "n" else m[1], value)
        pos = match.end()
        if pos == len(text):
            break
        if text[pos] != ",":
            raise AdapterError("invalid metric label separator")
        pos += 1
        if not text[pos:].strip():
            break
    return tuple(sorted(labels.items()))


def parse(text):
    """Parse the components' text exposition; reject malformed/duplicate samples.

    HELP/TYPE comments and optional millisecond timestamps are accepted.
    IEEE NaN/Inf are preserved here; the engine adapter marks them unavailable.
    """
    if not isinstance(text, str):
        raise AdapterError("metrics must be text")
    samples = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            match = _NAME.match(line)
            if not match:
                raise AdapterError("invalid metric name")
            name, pos = match[0], match.end()
            labels = ()
            if pos < len(line) and line[pos] == "{":
                # A closing brace inside a quoted label is ordinary text.
                end, quoted, escaped = pos + 1, False, False
                while end < len(line):
                    char = line[end]
                    if escaped:
                        escaped = False
                    elif quoted and char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = not quoted
                    elif char == "}" and not quoted:
                        break
                    end += 1
                if end == len(line):
                    raise AdapterError("unclosed metric labels")
                labels = _labels(line[pos + 1:end])
                pos = end + 1
            tail = _TAIL.fullmatch(line[pos:])
            if not tail:
                raise AdapterError("invalid metric value")
            key = name, labels
            if key in samples:
                raise AdapterError("duplicate metric sample")
            samples[key] = float(tail[1])
        except AdapterError as exc:
            raise AdapterError(f"metrics line {number}: {exc}") from exc
    return samples
