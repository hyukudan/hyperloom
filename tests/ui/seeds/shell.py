"""Portal shell fixtures for the browser smoke (no telemetry history).

- Shell scenarios: while ``<fixture dir>/shell-scenario.json`` exists, the documents named in it (``health/summary``,
  ``overview``) answer its content (the string ``"fail"`` makes that request fail, ``"missing"`` answers 404 as when no feature
  registers it, a ``delay_s`` field delays the answer); otherwise they answer what the registered feature answers, or 404 when no
  feature registers them. The walkthrough writes and removes the file, so its assertions do not depend on which
  other features are installed.
- While ``<fixture dir>/no-sw`` exists, the static assets manifest omits ``sw.js`` (the portal then answers 404 for
  ``/sw.js``, as a portal that no longer serves the worker would).
"""

import json
from pathlib import Path
import time


SHELL_DOCUMENTS = ("health/summary", "overview")


def patches(directory, now):
    from hyperloom.web import features, server
    directory = Path(directory)
    real_load, real_manifest = features.load, server.manifest

    def scenario():
        path = directory / "shell-scenario.json"
        for _ in range(40):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (PermissionError, ValueError):
                time.sleep(0.025)           # being replaced by the walkthrough: read it again
        return {}

    def load(modules=features.MODULES):
        registry = real_load(modules)
        for name in SHELL_DOCUMENTS:
            real = registry.gets.get(name)

            def handler(api, query, name=name, real=real):
                current = scenario()
                if name in current:
                    if current[name] == "fail":
                        raise RuntimeError("fixture: " + name + " is failing")
                    if current[name] == "missing":
                        raise LookupError("unknown endpoint")      # as if no feature registered it
                    answer = dict(current[name])
                    delay = answer.pop("delay_s", 0)
                    if delay:
                        time.sleep(min(float(delay), 20))     # a slow producer
                    return api.document(name.replace("/", "-"), **answer)
                if real is not None:
                    return real(api, query)
                raise LookupError("unknown endpoint")
            registry.gets[name] = handler
        return registry

    def manifest(static_dir, folder):
        names = real_manifest(static_dir, folder)
        if folder == "assets" and (directory / "no-sw").exists():
            return tuple(name for name in names if name != "sw.js")
        return names

    return [(features, "load", load), (server, "manifest", manifest)]
