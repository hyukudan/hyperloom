"""Portal preferences (F52): display and data settings of the portal shell.

GET prefs        -> {values, saved, defaults, choices, timezones_available, local_zone, related, why}
GET prefs/timezones -> {names: the IANA zones this process accepts, or null without a zone database}
POST prefs/save  -> any subset of the fields below; validated, written in one transaction, audited; returns the
                    same document as GET.

Values live in the telemetry store, docs namespace ``prefs/ui``, one key per field, so another feature reads one
key (the ledger reads ``timezone``). A field at its default has no row: a reader that finds no row uses the
default (``timezone`` absent = the control machine's local zone). ``related`` points at settings other features
own (energy price, alert channels, MQTT, retention, collector intervals, headroom thresholds): it says where they
live and whether that feature is registered, it copies none of their data.
"""

import json
import os
import re

from hyperloom import telemetry


NAMESPACE = "prefs/ui"
DEFAULTS = {"refresh_s": 2, "temperature_unit": "C", "timezone": "local", "language": "en", "theme": "system",
            "density": "comfortable", "hidden_panels": []}
CHOICES = {"refresh_s": [1, 2, 5, 10, 30], "temperature_unit": ["C", "F"], "language": ["en", "es"],
           "theme": ["system", "light", "dark", "black"], "density": ["comfortable", "compact"]}
ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")
PANEL = re.compile(r"[a-z][a-z0-9-]{0,40}")
MAX_PANELS = 64
NO_STORE = "preferences store not open in this portal process; defaults shown"
# (id, label, page, endpoint, feature module): settings owned by other features.
RELATED = (
    ("energy-price", "Energy price and currency", "energy", "energy/price", "energy"),
    ("thermal-thresholds", "Thermal thresholds", "energy", "energy/thresholds", "energy"),
    ("headroom-thresholds", "Memory headroom thresholds", "overview", "fleet/thresholds", "overview"),
    ("alert-channels", "Alert channels", "alerts", "alerts/channels", "alerts"),
    ("mqtt", "MQTT publishing", "integrations", "integrations/mqtt", "integrations"),
    ("retention", "Activity log retention", "events", "events/clear", "events"),
    ("intervals", "Collector poll intervals", "ops", "telemetry/interval", "ops"),
)


def zone_database():
    """True when this process can load named zones (Windows needs the optional tzdata package)."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Madrid")
        return True
    except Exception:
        return False


def _zone(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("timezone must be a string of at most 64 characters")
    if value in ("local", "UTC"):
        return value
    if not ZONE.fullmatch(value) or ".." in value:
        raise ValueError("timezone must be local, UTC or an IANA zone name")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
    except Exception:
        if not zone_database():
            raise ValueError("this portal process has no time zone database; only local and UTC can be chosen") from None
        raise ValueError("unknown time zone " + value) from None
    return value


def validate(arguments):
    """The validated subset of fields in ``arguments`` (ValueError on anything else)."""
    if not isinstance(arguments, dict) or not arguments:
        raise ValueError("preferences: a JSON object with at least one field is required")
    unknown = sorted(set(arguments) - set(DEFAULTS))
    if unknown:
        raise ValueError("unknown preference fields: " + ", ".join(unknown))
    values = {}
    for key, value in arguments.items():
        if key == "refresh_s":
            if isinstance(value, bool) or not isinstance(value, int) or value not in CHOICES[key]:
                raise ValueError("refresh_s must be one of " + ", ".join(map(str, CHOICES[key])))
        elif key == "timezone":
            value = _zone(value)
        elif key == "hidden_panels":
            if not isinstance(value, list) or len(value) > MAX_PANELS:
                raise ValueError(f"hidden_panels must be a list of at most {MAX_PANELS} page ids")
            if any(not isinstance(item, str) or not PANEL.fullmatch(item) for item in value):
                raise ValueError("hidden_panels entries must be page ids")
            if "settings" in value:
                raise ValueError("the settings page cannot be hidden")
            value = sorted(set(value))
        elif not isinstance(value, str) or value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of " + ", ".join(CHOICES[key]))
        values[key] = value
    return values


def read(store):
    """(values with defaults filled, saved flags) from a store (or None)."""
    values, saved = dict(DEFAULTS), {key: False for key in DEFAULTS}
    if store is None:
        return values, saved
    for key in DEFAULTS:
        value = store.get(NAMESPACE, key)
        if value is None:
            continue
        try:
            value = validate({key: value})[key]
        except ValueError:
            continue                     # a row this version cannot use reads as the default
        values[key], saved[key] = value, True
    return values, saved


def save(store, values):
    """Write validated values in one transaction: a default deletes its row."""
    statements = []
    for key, value in values.items():
        if value == DEFAULTS[key]:
            statements.append(("DELETE FROM docs WHERE namespace=? AND key=?", (NAMESPACE, key)))
        else:
            statements.append(("INSERT INTO docs(namespace, key, ts, value) VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
                               "DO UPDATE SET ts=excluded.ts, value=excluded.value",
                               (NAMESPACE, key, store.clock(), json.dumps(value, ensure_ascii=True, sort_keys=True))))
    store.transaction(statements)


def related():
    from hyperloom.web import features
    registry = features.registry()
    rows = []
    for ident, label, page, endpoint, module in RELATED:
        available = (endpoint in registry.posts or endpoint in registry.jobs or endpoint in registry.gets)
        row = {"id": ident, "label": label, "page": page, "endpoint": endpoint, "available": available}
        if not available:
            row["why"] = "needs the " + module + " feature"
        rows.append(row)
    return rows


def local_iana():
    """The control machine's IANA zone name when the platform states one (``TZ``, or the target of
    /etc/localtime), else None (Windows names its zones differently)."""
    candidates = []
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        candidates.append(tz)
    try:
        target = os.path.realpath("/etc/localtime").replace("\\", "/")
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
    except (OSError, ValueError):
        pass
    for name in candidates:
        if name == "UTC" or (ZONE.fullmatch(name) and ".." not in name):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(name)
                return name
            except Exception:
                continue
    return None


def local_zone(now):
    """The control machine's zone at ``now``: {name (the platform's label), utc_offset_s, iana (or None)}."""
    from datetime import datetime
    moment = datetime.fromtimestamp(now).astimezone()
    return {"name": moment.tzname() or "local", "utc_offset_s": int(moment.utcoffset().total_seconds()),
            "iana": local_iana()}


def zone_names():
    """The IANA names this portal process accepts (sorted), or None without a zone database."""
    if not zone_database():
        return None
    try:
        from zoneinfo import available_timezones
        return sorted(name for name in available_timezones() if ZONE.fullmatch(name) and len(name) <= 64)
    except Exception:
        return None


def document(api, store):
    values, saved = read(store)
    why = {} if store is not None else {"values": NO_STORE}
    zones = zone_database()
    if not zones:
        why["timezone"] = "no time zone database in this portal process; local and UTC only"
    return api.document("prefs", values=values, saved=saved, defaults=DEFAULTS, choices=CHOICES,
                        timezones_available=zones, local_zone=local_zone(api.clock()), related=related(), why=why)


def get(api, query):
    if query:
        raise ValueError("prefs takes no query arguments")
    return document(api, telemetry.existing_store(api))


def timezones(api, query):
    if query:
        raise ValueError("prefs/timezones takes no query arguments")
    names = zone_names()
    return api.document("prefs-timezones", names=names,
                        why={} if names is not None else {"names": "no time zone database in this portal process"})


def post(api, arguments):
    values = validate(arguments)
    store = telemetry.open_store(api)
    save(store, values)
    return document(api, store)


def register(reg):
    reg.get("prefs", get)
    reg.get("prefs/timezones", timezones)
    reg.post("prefs/save", post, audited=True)
