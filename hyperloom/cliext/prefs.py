"""``hyperloom prefs``: the portal preferences (F52) from the command line, through the same validation and the same
write as the portal's ``POST prefs/save``.

    hyperloom prefs show                       JSON: values (defaults filled) and which fields are saved
    hyperloom prefs set refresh_s=5 theme=dark hidden_panels=logs,topology timezone=UTC
    hyperloom prefs set language=en            a default value removes the saved row

The store is the portal's (``<config dir>/telemetry.sqlite3``); ``show`` never creates it.
"""

import json
import sys


def _store_path():
    from hyperloom import cli
    return cli.config_path().parent / "telemetry.sqlite3"


def parse(pairs):
    """``key=value`` words -> the JSON object the portal would send (numbers and lists typed like its controls)."""
    values = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError("expected key=value, got " + repr(pair))
        if key in values:
            raise ValueError("field given twice: " + key)
        if key == "refresh_s":
            try:
                values[key] = int(value)
            except ValueError:
                raise ValueError("refresh_s must be a whole number of seconds") from None
        elif key == "hidden_panels":
            values[key] = [item for item in value.split(",") if item]
        else:
            values[key] = value
    return values


def show(args):
    from hyperloom.telemetry.store import Store
    from hyperloom.web.features import prefs
    path = _store_path()
    store = Store(path) if path.is_file() else None
    try:
        values, saved = prefs.read(store)
    finally:
        if store is not None:
            store.close()
    print(json.dumps({"values": values, "saved": saved, **({} if path.is_file() else {"why": prefs.NO_STORE})},
                     indent=2, sort_keys=True))
    return 0


def set_values(args):
    from hyperloom.telemetry.store import Store
    from hyperloom.web.features import prefs
    try:
        values = prefs.validate(parse(args.pairs))
    except ValueError as exc:
        print("hyperloom prefs: " + str(exc), file=sys.stderr)
        return 2
    store = Store(_store_path())
    try:
        prefs.save(store, values)
        current, saved = prefs.read(store)
    finally:
        store.close()
    print(json.dumps({"values": current, "saved": saved}, indent=2, sort_keys=True))
    return 0


def add(commands):
    parser = commands.add_parser("prefs", help="portal preferences (refresh rate, units, time zone, theme, language)")
    sub = parser.add_subparsers(dest="prefs_command", required=True)
    sub.add_parser("show", help="print the preferences as JSON").set_defaults(ext_run=show)
    setter = sub.add_parser("set", help="change preferences: key=value ...")
    setter.add_argument("pairs", nargs="+", metavar="key=value")
    setter.set_defaults(ext_run=set_values)
