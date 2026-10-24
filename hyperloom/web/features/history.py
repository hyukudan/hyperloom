"""Metrics history (F4): the series in the telemetry store and their buckets.

GET history                       -> {series: [names], collectors: {name: state}}
GET history/<series>?range=3600   -> {series, kind, tier, step, start, end, lines: [{labels, points}]}
    optional: start=<epoch> end=<epoch> tier=raw|m1|m15 and label filters l.<label>=<value>
Points are [t, avg, max, min, n, sum]; missing buckets are absent (charts draw gaps, never zeros).
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


def index(api, query):
    store = telemetry.store_for(api)
    return api.document("history", series=store.names(), collectors=telemetry.status(api),
                        why={} if telemetry.status(api) is not None else {"collectors": "samplers not running in this process"})


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
    store = telemetry.store_for(api)
    lines = store.query(name, start, end, labels=labels, tier=tier)
    if not lines:
        return api.document("history-series", series=name, kind=None, tier=None, step=None, start=start, end=end,
                            lines=[], why={"lines": "no samples recorded for this series in the range"})
    return api.document("history-series", series=name, kind=lines[0]["kind"], tier=lines[0]["tier"],
                        step=lines[0]["step"], start=start, end=end,
                        lines=[{"labels": line["labels"], "points": line["points"]} for line in lines])


def register(reg):
    reg.get("history", index)
    reg.get_prefix("history/", series)
