"""Metrics history (F4): the series in the telemetry store and their buckets.

GET history                       -> {series: [names], collectors: {name: state}}
GET history/<series>?range=3600   -> {series, kind, unit, interval_s, legend, value, tier, step, start, end,
                                      lines: [{labels, points}], truncated}
    optional: start=<epoch> end=<epoch> tier=raw|m1|m15 and label filters l.<label>=<value>
Points are [t, avg, max, min, n, sum, last, coverage]; ``value`` names the field to plot for the kind (gauge: avg,
delta: sum, state: last). Missing buckets are absent (charts draw gaps, never zeros). A GET never creates the
store: without one (samplers not started in this process) the document says why.
"""

import math
import re

from hyperloom import telemetry


SERIES = re.compile(r"[a-z][a-z0-9_]{0,63}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_RANGE = 31 * 86400


def _number(query, key, default=None):
    if key not in query:
        return default
    try:
        value = float(query[key])
    except ValueError:
        raise ValueError(key + " must be a number") from None
    if not math.isfinite(value):
        raise ValueError(key + " must be finite")
    return value


VALUE = {"gauge": "avg", "delta": "sum", "state": "last"}
NO_STORE = "metrics history not open in this portal process (samplers disabled)"


def index(api, query):
    store = telemetry.existing_store(api)
    status = telemetry.status(api)
    why = {}
    if store is None:
        why["series"] = NO_STORE
    if status is None:
        why["collectors"] = "samplers not running in this process"
    return api.document("history", series=None if store is None else store.names(), collectors=status, why=why)


def series(api, name, query):
    if not SERIES.fullmatch(name):
        raise ValueError("invalid series name")
    unknown = [key for key in query if key not in ("range", "start", "end", "tier") and not key.startswith("l.")]
    if unknown:
        raise ValueError("unknown query arguments: " + ", ".join(sorted(unknown)))
    labels = {}
    for key, value in query.items():
        if key.startswith("l."):
            if not LABEL.fullmatch(key[2:]) or len(value) > 128:
                raise ValueError("invalid label filter " + key)
            labels[key[2:]] = value
    now = api.clock()
    end = _number(query, "end", now)
    span = _number(query, "range", 3600)
    start = _number(query, "start", end - span)
    if end < start or end - start > MAX_RANGE:
        raise ValueError("range must be positive and at most 31 days")
    tier = query.get("tier")
    store = telemetry.existing_store(api)
    if store is None:
        return api.document("history-series", series=name, kind=None, value=None, tier=None, step=None, start=start,
                            end=end, lines=None, truncated=[], why={"lines": NO_STORE})
    result = store.query(name, start, end, labels=labels, tier=tier)
    descriptor = result["descriptor"] or {}
    why = {} if result["lines"] else {"lines": "no samples recorded for this series in the range"}
    if result["truncated"]:
        why["truncated"] = "oldest buckets beyond the point limit were cut for the listed lines"
    return api.document("history-series", series=name, kind=descriptor.get("kind"), unit=descriptor.get("unit"),
                        interval_s=descriptor.get("interval_s"), legend=descriptor.get("legend"),
                        value=VALUE.get(descriptor.get("kind")), tier=result["tier"], step=result["step"],
                        start=start, end=end, lines=result["lines"], truncated=result["truncated"], why=why)


def register(reg):
    reg.get("history", index)
    reg.get_prefix("history/", series)
