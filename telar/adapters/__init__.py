"""Read-only adapters for the pinned components' telemetry.

Absent, null, or invalid values are None; ``why`` maps field paths to reasons.
Unknown status schemas raise AdapterError. Extra fields are ignored.
"""

import math


class AdapterError(ValueError):
    """Unsupported or malformed telemetry document."""


def check_schema(obj, expected):
    if not isinstance(obj, dict) or obj.get("schema") != expected:
        raise AdapterError(f"expected schema {expected}")


class Reader:
    def __init__(self, obj):
        self.obj = obj
        self.why = {}

    def get(self, path, kind):
        value = self.obj
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                self.why[path] = "missing from component response"
                return None
            value = value[key]
        if value is None:
            self.why[path] = "component reported null"
            return None
        if kind in (int, float):
            valid = (type(value) is int if kind is int else type(value) in (int, float))
            try:
                valid = valid and value >= 0 and math.isfinite(value)
            except OverflowError:
                valid = False
        else:
            valid = isinstance(value, kind)
        if not valid:
            self.why[path] = f"expected {kind.__name__}"
            return None
        return value

    def record(self, path, fields):
        if self.get(path, dict) is None:
            return None
        return {name: self.get(f"{path}.{name}", kind) for name, kind in fields.items()}

    def records(self, path, fields):
        values = self.get(path, list)
        if values is None:
            return None
        result = []
        for index, value in enumerate(values):
            child = Reader({"item": value})
            result.append(child.record("item", fields))
            self.why.update({f"{path}[{index}]" + key[4:]: reason
                             for key, reason in child.why.items()})
        return result
