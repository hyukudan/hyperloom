"""Benchmark targets: a running profile's router, or an allow-listed ad-hoc lab endpoint (F32).

A target argument is ``{"profile": "<name>"}`` or ``{"target": "<allow-list id>"}``. Listing and validation never
touch the network; the endpoint is resolved (router address, key from the secret store) only when a run starts,
inside its job. The allow-list lives in the telemetry store document ``prefs`` / ``bench/targets`` and is edited
only through ``save``/``remove`` (portal POSTs, audited, and the CLI); what a profile's engine honours beyond
greedy decoding (a thinking switch) is declared in ``prefs`` / ``bench/profile-capabilities``.

Hosts: an ad-hoc target is a lab endpoint only: a configured machine id, or a numeric private or loopback IP (the
same rule as the endpoint targets); names are never resolved and public addresses are refused.

Keys: an ad-hoc target carries no key. The only key a benchmark ever sends is the one the server binds to a profile
target (its router's own client key, sent only to the router address of this configuration's launch); a client
never names a secret, so no stored key can be sent to a host a client chose. A stored entry of an earlier release
that names a key no longer validates and is not offered.

Resources (contract C11): a profile occupies its machines and the router (the same rule the portal core applies to
deploys and stops). An ad-hoc target occupies, as ``up`` would, every configured machine its address is (a machine
id, its configured address, or loopback for the control machine; the router too on the router's port), plus the
machines its entry declares (``machines``: configured ids, every machine an endpoint forwards to). ``"external"``
(resource ``host:<address>``) is accepted only for an address that is no configured machine. Without a machine of
its own and without a declaration it occupies everything (``*``), so an unknown placement never runs beside another
benchmark and any stop cancels it. A validated run freezes its target and resources (``digest``); the job refuses
to start when either changed.
"""

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import re

from hyperloom import config, secrets, telemetry

from . import client


PREFS_NAMESPACE, PREFS_KEY, CAPS_KEY = "prefs", "bench/targets", "bench/profile-capabilities"
TARGET_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MACHINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROUTER_DEFAULT_PORT = 18600
HOST_WHY = "host must be a configured machine id or a numeric private or loopback IP (names are not resolved; " \
           "public addresses are not lab endpoints)"
KEY_WHY = "an ad-hoc target carries no key: a keyed endpoint is benchmarked as its profile (the server binds " \
          "the router's own key)"
MAX_TARGETS = 64
GREEDY_WHY = "our engines decode greedily; sampling parameters are not honoured"
THINKING_WHY = "the endpoint does not declare a thinking switch"
NO_IDS_WHY = "unavailable · this engine family does not return token ids yet"
NO_IDS_FAMILIES = ("minimax-pair",)
IDS_WHY = "requested on every request; checked per response"


def _host(value):
    """A machine id (kept as written) or the canonical spelling of a private or loopback IP literal."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(HOST_WHY)
    literal = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return config.address(literal, "host")
    except ValueError:
        pass
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        if MACHINE_ID.fullmatch(value) and not value.replace(".", "").isdigit():
            return value                              # a machine id: checked against the configuration on use
    raise ValueError(HOST_WHY)


def address_of(cluster, host):
    """The IP literal a target host stands for (raises ValueError for an unknown machine id)."""
    if host in cluster.machines:
        return config.address(cluster.machines[host].http_address, "host")
    try:
        return config.address(host, "host")
    except ValueError:
        raise ValueError(HOST_WHY + ": " + host + " is not a configured machine") from None


def own_machines(cluster, host, port):
    """The configured machines a target's address is, as ``up`` holds them: the machine named, every machine whose
    configured address it is, the control machine (local access) for loopback; and the router on its own port."""
    address = address_of(cluster, host)
    loopback = ipaddress.ip_address(address).is_loopback
    found = set()
    for mid, machine in cluster.machines.items():
        try:
            same = config.address(machine.http_address, "host") == address
        except ValueError:
            same = False
        if mid == host or same or (loopback and machine.access == "local"):
            found.add(mid)
    router = cluster.router or {}
    router_machine = router.get("machine", "local")
    try:
        router_port = config.listen(router["listen"], "router.listen")["port"] if router.get("listen") \
            else ROUTER_DEFAULT_PORT
    except (ValueError, KeyError):
        router_port = ROUTER_DEFAULT_PORT
    at_router = router_machine in found or (router_machine == "local" and any(
        cluster.machines[m].access == "local" for m in found))
    if found and at_router and port == router_port:
        found.add("router")
    return found


def check_entry(entry):
    """A validated allow-list entry (raises ValueError)."""
    allowed = {"id", "label", "scheme", "host", "port", "machines", "sampling", "thinking", "context_tokens"}
    if isinstance(entry, dict) and "key" in entry:
        raise ValueError(KEY_WHY)
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise ValueError("target fields: " + ", ".join(sorted(allowed)))
    if not isinstance(entry.get("id"), str) or not TARGET_ID.fullmatch(entry["id"]):
        raise ValueError("target id: lowercase letters, digits and '-', starting with a letter (at most 40)")
    if entry.get("scheme") not in ("http", "https"):
        raise ValueError("scheme must be http or https")
    port = entry.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer in 1..65535")
    label = entry.get("label", entry["id"])
    if not isinstance(label, str) or not 1 <= len(label) <= 60 or re.search(r"[\x00-\x1f<>]", label):
        raise ValueError("label: 1..60 printable characters")
    context = entry.get("context_tokens")
    if context is not None and (type(context) is not int or not 1024 <= context <= 4_194_304):
        raise ValueError("context_tokens must be an integer in 1024..4194304")
    for flag in ("sampling", "thinking"):
        if type(entry.get(flag, False)) is not bool:
            raise ValueError(flag + " must be true or false")
    machines = entry.get("machines")
    if machines is not None and machines != "external" and (
            not isinstance(machines, list) or not 1 <= len(machines) <= 32
            or any(not isinstance(m, str) or not MACHINE_ID.fullmatch(m) for m in machines)):
        raise ValueError('machines must be a list of configured machine ids or "external"')
    if isinstance(machines, list):
        machines = sorted(set(machines))
    return {"id": entry["id"], "label": label, "scheme": entry["scheme"], "host": _host(entry.get("host")), "port": port,
            "machines": machines, "sampling": entry.get("sampling", False),
            "thinking": entry.get("thinking", False), "context_tokens": context}


def allow_list(store):
    if store is None:
        return None
    value = store.get(PREFS_NAMESPACE, PREFS_KEY) or {}
    entries = []
    for entry in value.get("targets") or []:
        try:
            entries.append(check_entry(entry))
        except ValueError:
            continue    # a hand-edited entry that no longer validates is not offered
    return entries


def save(api, arguments):
    """POST bench/targets/save: add or replace an allow-list entry (a lab host; "external" only for an address that
    is no configured machine)."""
    entry = check_entry(arguments)
    cluster = api.cluster()
    own = own_machines(cluster, entry["host"], entry["port"])
    if isinstance(entry["machines"], list):
        unknown = [m for m in entry["machines"] if m not in cluster.machines]
        if unknown:
            raise ValueError("not configured: " + ", ".join(unknown) + " (name configured machines, or external)")
    elif entry["machines"] == "external" and own:
        raise ValueError(f"{entry['host']} is the configured machine " + ", ".join(sorted(own - {"router"}))
                         + ": a benchmark there holds it like up (declare it, or leave machines out)")
    store = telemetry.open_store(api)
    entries = [e for e in allow_list(store) if e["id"] != entry["id"]]
    if len(entries) >= MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} ad-hoc targets")
    entries.append(entry)
    store.put(PREFS_NAMESPACE, PREFS_KEY, {"targets": sorted(entries, key=lambda e: e["id"])})
    return {"saved": entry["id"], "targets": len(entries)}


def remove(api, arguments):
    """POST bench/targets/remove {id}."""
    if not isinstance(arguments, dict) or set(arguments) != {"id"} or not isinstance(arguments["id"], str):
        raise ValueError("remove takes {id}")
    store = telemetry.open_store(api)
    entries = allow_list(store)
    kept = [e for e in entries if e["id"] != arguments["id"]]
    if len(kept) == len(entries):
        raise LookupError("no ad-hoc target " + arguments["id"])
    store.put(PREFS_NAMESPACE, PREFS_KEY, {"targets": kept})
    return {"removed": arguments["id"], "targets": len(kept)}


def profile_capabilities(store):
    value = (store.get(PREFS_NAMESPACE, CAPS_KEY) or {}) if store is not None else {}
    profiles = value.get("profiles") if isinstance(value.get("profiles"), dict) else {}
    return {name: {"thinking": entry.get("thinking") is True} for name, entry in profiles.items()
            if isinstance(name, str) and isinstance(entry, dict)}


def save_capabilities(api, arguments):
    """POST bench/targets/capabilities {profile, thinking}: what a profile's engine honours (package prefs)."""
    if (not isinstance(arguments, dict) or set(arguments) != {"profile", "thinking"}
            or type(arguments["thinking"]) is not bool or not isinstance(arguments["profile"], str)):
        raise ValueError("capabilities take {profile, thinking: true|false}")
    if arguments["profile"] not in api.cluster().profiles:
        raise LookupError("unknown profile " + arguments["profile"])
    store = telemetry.open_store(api)
    current = profile_capabilities(store)
    current[arguments["profile"]] = {"thinking": arguments["thinking"]}
    store.put(PREFS_NAMESPACE, CAPS_KEY, {"profiles": current})
    return {"profile": arguments["profile"], "thinking": arguments["thinking"]}


def _family_capabilities(family, declared):
    ids = NO_IDS_WHY if family in NO_IDS_FAMILIES else None
    thinking = bool((declared or {}).get("thinking"))
    return {"sampling": False, "sampling_why": GREEDY_WHY, "thinking": thinking,
            "thinking_why": None if thinking else THINKING_WHY,
            "ids": ids is None, "ids_why": ids or IDS_WHY}


def _profile_rows(api):
    try:
        rows = api.snapshot()["profiles"].get("profiles") or []
    except Exception:
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def listing(api):
    """Targets offered to the pages (no network): running profiles and the allow-list."""
    rows, why = [], {}
    store = telemetry.existing_store(api)
    declared = profile_capabilities(store)
    for row in _profile_rows(api):
        name = row["id"]
        state = row.get("state")
        caps = _family_capabilities(row.get("family"), declared.get(name))
        blocker = None if state == "running" else f"profile {name} is {state or 'in an unknown state'}; start it first"
        rows.append({"key": "profile:" + name, "kind": "profile", "id": name, "label": name, "family": row.get("family"),
                     "state": state, "blocker": blocker, "context_tokens": None, **caps})
    entries = allow_list(store)
    if entries is None:
        why["adhoc"] = "history store not open in this portal process"
    for entry in entries or []:
        rows.append({"key": "target:" + entry["id"], "kind": "adhoc", "id": entry["id"], "label": entry["label"],
                     "family": None, "state": "allow-listed", "blocker": None,
                     "address": f"{entry['scheme']}://{entry['host']}:{entry['port']}",
                     "key_ref": None, "machines": entry["machines"], "context_tokens": entry["context_tokens"],
                     "sampling": entry["sampling"], "sampling_why": None if entry["sampling"] else "not declared for this target",
                     "thinking": entry["thinking"], "thinking_why": None if entry["thinking"] else THINKING_WHY,
                     "ids": True, "ids_why": IDS_WHY})
    return rows, why


def parse(value):
    """Normalise a target argument to ("profile"|"target", id)."""
    if isinstance(value, dict) and set(value) == {"profile"} and isinstance(value["profile"], str):
        if not PROFILE_NAME.fullmatch(value["profile"]):
            raise ValueError("invalid profile name")
        return ("profile", value["profile"])
    if isinstance(value, dict) and set(value) == {"target"} and isinstance(value["target"], str):
        if not TARGET_ID.fullmatch(value["target"]):
            raise ValueError("invalid target id")
        return ("target", value["target"])
    raise ValueError('target must be {"profile": name} or {"target": id}')


def as_argument(parsed):
    return {parsed[0]: parsed[1]}


def key_of(parsed):
    return parsed[0] + ":" + parsed[1]


def describe(api, parsed):
    """The listing row of a target (raises LookupError / PermissionError when it cannot run now)."""
    rows, _ = listing(api)
    for row in rows:
        if row["key"] == key_of(parsed):
            if row["blocker"]:
                raise PermissionError(row["blocker"])
            return row
    raise LookupError(("profile " if parsed[0] == "profile" else "ad-hoc target ") + parsed[1] + " is not offered")


def _entry(api, target_id, readonly=False):
    store = telemetry.existing_store(api)
    if store is None and readonly:
        raise LookupError("history store not open in this portal process")
    store = store or telemetry.open_store(api)
    entry = next((e for e in allow_list(store) if e["id"] == target_id), None)
    if entry is None:
        raise LookupError("ad-hoc target " + target_id + " is not allow-listed")
    return entry


def resources(api, parsed, readonly=False, entry=None):
    """The physical resources a target occupies (C11). Unknown placement is conservative: everything."""
    if parsed[0] == "profile":
        return set(api._resources_of_profile(parsed[1]))
    entry = entry or _entry(api, parsed[1], readonly)
    try:
        cluster = api.cluster()
        own = own_machines(cluster, entry["host"], entry["port"])
    except Exception:
        return {"*"}
    if entry["machines"] == "external":
        # Only an address that is no configured machine; one that became a machine holds it, like up.
        return {"host:" + address_of(cluster, entry["host"])} if not own else own
    if isinstance(entry["machines"], list):
        if not all(m in cluster.machines for m in entry["machines"]):
            return {"*"}
        return own | set(entry["machines"])
    return own or {"*"}


def snapshot(api, parsed):
    """One read of what a target is now: (material, digest). The material (the profile's router record, or the
    allow-list entry, plus the resource set) is what a run then uses; the digest freezes it."""
    if parsed[0] == "profile":
        try:
            located = api.orchestrate.endpoint(parsed[1], config_path=api.config_path)   # this portal's launch only
        except Exception as exc:
            raise PermissionError(f"profile {parsed[1]} has no running router: {exc}") from None
        material = {"profile": parsed[1], "url": located.get("url"), "key_name": located.get("key_name"),
                    "resources": sorted(resources(api, parsed))}
    else:
        entry = _entry(api, parsed[1])
        material = {"entry": entry, "resources": sorted(resources(api, parsed, entry=entry))}
    return material, hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def digest(api, parsed):
    """Freezes what a validated run will talk to: the allow-list entry, or the profile's router record, and the
    resources it occupies."""
    return snapshot(api, parsed)[1]


@dataclass
class Resolved:
    key: str
    label: str
    kind: str
    endpoint: client.Endpoint = field(repr=False)
    family: str = None
    capabilities: dict = field(default_factory=dict)
    context_tokens: int = None
    identity: dict = field(default_factory=dict)


def _split_url(url):
    match = re.fullmatch(r"(https?)://(\[[^\]]+\]|[^:/\[\]]+):(\d+)/?", url or "")
    if not match:
        raise ValueError("router address is not scheme://host:port")
    return match[1], match[2].strip("[]"), int(match[3])


def _source(value, source):
    return source if value else "unavailable"


def resolve(api, parsed, frozen=None):
    """Resolve at run start (inside the job): endpoint with its key and the identity known without the network.
    With ``frozen`` (the validated target digest) a target that changed since validation is refused."""
    row = describe(api, parsed)
    material, found = snapshot(api, parsed)       # checked and used: the same single read
    if frozen is not None and found != frozen:
        raise PermissionError("the target or its resources changed after this run was accepted; start it again")
    if parsed[0] == "profile":
        located = material
        scheme, host, port = _split_url(located.get("url"))
        key = secrets.get(located["key_name"]) if located.get("key_name") else None
        endpoint = client.Endpoint(scheme, host, port, key)
        profile = next((r for r in _profile_rows(api) if r["id"] == parsed[1]), {})
        shas = sorted({p.get("binary_sha256") for p in profile.get("processes") or [] if isinstance(p, dict)
                       and isinstance(p.get("binary_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", p["binary_sha256"])})
        status = profile.get("status") if isinstance(profile.get("status"), dict) else {}
        recipe = status.get("recipe") if isinstance(status.get("recipe"), dict) else {}
        router = status.get("router") if isinstance(status.get("router"), dict) else {}
        prefill = router.get("prefill") if isinstance(router.get("prefill"), dict) else {}
        recipe_digest = next((recipe.get(k) for k in ("effective_sha256", "sha256")
                              if isinstance(recipe.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", recipe.get(k))), None)
        policy = prefill.get("policy") if isinstance(prefill.get("policy"), str) else None
        identity = {"family": profile.get("family"), "release": profile.get("release") if isinstance(profile.get("release"), str) else None,
                    "binary_sha256": shas or None, "build_source": "launch-record" if shas else "unavailable",
                    "recipe_digest": recipe_digest, "recipe_source": _source(recipe_digest, "launch-record"),
                    "router_policy": policy, "router_policy_source": _source(policy, "router-status")}
        return Resolved(key_of(parsed), row["label"], "profile", endpoint, row.get("family"), row, None, identity)
    entry = material["entry"]
    # Keyless, at the configured address of the machine named or the IP literal saved (never a resolved name).
    endpoint = client.Endpoint(entry["scheme"], address_of(api.cluster(), entry["host"]), entry["port"], None)
    identity = {"family": None, "release": None, "binary_sha256": None, "build_source": "unavailable",
                "recipe_digest": None, "recipe_source": "unavailable", "router_policy": None,
                "router_policy_source": "unavailable"}
    return Resolved(key_of(parsed), entry["label"], "adhoc", endpoint, None, row, entry["context_tokens"], identity)


def probe_model(resolved, cancelled, wanted=None):
    """GET /v1/models at run start: (model id, reported context length or None, tokenizer id or None)."""
    data = client.get_json(resolved.endpoint, "/v1/models", timeout=10, cancelled=cancelled)
    models = [m for m in (data.get("data") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)] \
        if isinstance(data, dict) else []
    if not models:
        raise client.ClientError("the endpoint lists no models")
    chosen = next((m for m in models if m["id"] == wanted), None) if wanted else models[0]
    if chosen is None:
        raise LookupError(f"model {wanted} is not served by this target")
    context = next((chosen.get(k) for k in ("max_model_len", "context_length", "context_tokens")
                    if type(chosen.get(k)) is int and chosen.get(k) > 0), None)
    tokenizer = next((chosen.get(k) for k in ("tokenizer_id", "tokenizer")
                      if isinstance(chosen.get(k), str) and 1 <= len(chosen.get(k)) <= 1000), None)
    return chosen["id"], context, tokenizer
