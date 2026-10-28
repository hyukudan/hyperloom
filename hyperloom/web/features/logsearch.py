"""Logs search (F37): bounded, read-only search over hyperloom-managed logs and configured lab directories on
every machine, by literal text or a restricted regex.

GET  logsearch                    machines, their searchable roots (ids; paths come from our own configuration and
                                  preferences only), limits and the regex rules; never opens a file
POST logsearch/query {text|regex, ignore_case?, machines?, roots?, window_s? | since?/until?, process?, level?,
                      context?, max_results?}       synchronous, read-only
POST logsearch/roots {machine, roots: [abs dirs]}   (audited) extra roots of a machine, each inside a root the
                                  configuration gives it (install root, lab directory, model roots)

Nodes run the `logs-search` verb (deadline 5 s, scanned bytes <= 64 MiB, files <= 2000, results <= 500 with
2 context lines, secrets redacted, a regex in a worker process killed at the deadline). The control machine runs
the same search in a local node tool process over the router and prefill service directories of owned launches
and its configured roots. Clients never send paths: they choose root ids from the GET document.
"""

import concurrent.futures
import threading
from pathlib import PurePosixPath, PureWindowsPath
import re
import time

from hyperloom import secrets
from hyperloom.web.features import ops

NAMESPACE = "logsearch"
MAX_ROOTS = 16
MAX_RESULTS = 500
MACHINE_DEADLINE_S = 5.0
QUERY_DEADLINE_S = 20.0     # a machine whose call has not returned by then is reported as not answering
WINDOWS_S = (60, 30 * 86400)
REGEX_RULES = ("at most 128 characters", "no backreferences or lookarounds", "no quantified group: quantify single "
               "characters or classes", "matched per line on lines cut at 4 KiB")
QUERY_KEYS = ("text", "regex", "ignore_case", "machines", "roots", "window_s", "since", "until", "process", "level",
              "context", "max_results")


def configured(api, store=None):
    return dict(ops.pref(api, NAMESPACE, "roots", {}, store=store) or {})


def owned_directories(api):
    """Router and prefill service directories of owned launches on the control machine (from the launch records of the
    portal's configuration)."""
    found = []
    try:
        from hyperloom import orchestrate
        states = orchestrate.launch_states(api.config_path, read=api.orchestrate._read)
    except OSError:
        states = {}
    for profile, state in list(states.items())[:32]:
        for kind in ("router", "prefill_service"):
            entry = state.get(kind) or {}
            directory = entry.get("directory")
            if directory and (kind == "prefill_service" or entry.get("machine") == "local"):
                found.append((f"{profile} {kind.replace('_', ' ')}", str(directory)))
    return found


def roots_of(api, machine, store=None):
    """[(id, label, path or None)] of a machine: the hyperloom log of its install root (nodes), the owned launch
    directories (control machine) and the configured lab roots."""
    rows = []
    if machine.access == "ssh":
        rows.append(("hyperloom", "hyperloom log (install root)", None))
    elif machine.access == "local":
        for index, (label, path) in enumerate(owned_directories(api)):
            rows.append((f"launch-{index}", label, path))
    try:
        bases = allowed_bases(api, machine)
    except Exception:  # noqa: BLE001 - an unreadable configuration: no extra root
        bases = []
    for index, path in enumerate(configured(api, store).get(machine.id, [])):
        if isinstance(path, str) and _inside(path, machine.os, bases):   # an earlier release's root elsewhere: unused
            rows.append((f"root-{index}", path, path))
    return rows


def allowed_bases(api, machine):
    """The roots the configuration gives a machine (its install root, its lab directory, its model roots): an extra
    log root must lie inside one of them."""
    from hyperloom import downloads
    bases = [machine.install_root] + ([machine.lab_dir] if getattr(machine, "lab_dir", None) else [])
    bases = [base for base in bases if not downloads.too_wide(machine, base)]
    return bases + [base for base in downloads.configured_bases(api.cluster(), machine) if base not in bases]


def _inside(path, system, bases):
    flavour = PureWindowsPath if system == "windows" else PurePosixPath
    parts = [p.lower() for p in flavour(path).parts] if system == "windows" else list(flavour(path).parts)
    for base in bases:
        head = [p.lower() for p in flavour(base).parts] if system == "windows" else list(flavour(base).parts)
        if parts[:len(head)] == head and not any(part.startswith(".") for part in parts[len(head):]):
            return True
    return False


def document(api, query):
    if query:
        raise ValueError("logsearch takes no parameters")
    cluster = api.cluster()
    machines = []
    for mid, machine in cluster.machines.items():
        row = {"machine": mid, "os": machine.os, "access": machine.access, "roots": [], "why": {}}
        if machine.access not in ("ssh", "local"):
            row["why"]["roots"] = "agent access is not supported"
        else:
            row["roots"] = [{"id": rid, "label": label} for rid, label, _ in roots_of(api, machine)]
            row["configured"] = configured(api).get(mid, [])
            if not row["roots"]:
                row["why"]["roots"] = "nothing to search yet: no owned launch and no configured root"
        machines.append(row)
    why = {}
    if ops.store_for_read(api) is None:
        why["configured"] = "configured roots are kept in the portal's store (not open in this process)"
    return api.document("logsearch", machines=machines, why=why, regex_rules=list(REGEX_RULES),
                        limits={"deadline_s": MACHINE_DEADLINE_S, "max_bytes": 64 << 20, "max_files": 2000,
                                "max_results": MAX_RESULTS, "context": 2, "line_bytes": 4096},
                        levels=["any", "warning", "error"])


def _number(arguments, key, low, high, kind=(int, float)):
    value = arguments[key]
    if type(value) not in kind or not low <= value <= high:
        raise ValueError(f"{key} must be a number in {low}..{high}")
    return value


def build(api, arguments):
    """(per-machine plans [(machine, payload, root labels)], shared options) from a query (raises ValueError)."""
    ops.strict(arguments, QUERY_KEYS)
    if ("text" in arguments) == ("regex" in arguments):
        raise ValueError("give exactly one of text or regex")
    needle = arguments.get("text", arguments.get("regex"))
    if not isinstance(needle, str) or not 1 <= len(needle) <= 128 or any(ord(c) < 32 for c in needle):
        raise ValueError("the search text must be 1..128 printable characters")
    base = {"text" if "text" in arguments else "regex": needle}
    if "ignore_case" in arguments:
        if type(arguments["ignore_case"]) is not bool:
            raise ValueError("ignore_case must be boolean")
        base["ignore_case"] = arguments["ignore_case"]
    if "window_s" in arguments:
        if "since" in arguments or "until" in arguments:
            raise ValueError("give window_s or since/until, not both")
        base["since"] = api.clock() - _number(arguments, "window_s", *WINDOWS_S)
    for key in ("since", "until"):
        if key in arguments:
            base[key] = _number(arguments, key, 0, 1e11)
    if "process" in arguments:
        if not isinstance(arguments["process"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", arguments["process"]):
            raise ValueError("process must be 1..64 characters [A-Za-z0-9._-]")
        base["process"] = arguments["process"]
    if "level" in arguments:
        if arguments["level"] not in ("any", "warning", "error"):
            raise ValueError("level must be any, warning or error")
        base["level"] = arguments["level"]
    if "context" in arguments:
        base["context"] = _number(arguments, "context", 0, 2, (int,))
    limit = _number(arguments, "max_results", 1, MAX_RESULTS, (int,)) if "max_results" in arguments else 200
    cluster = api.cluster()
    wanted = arguments.get("machines")
    if wanted is not None and (not isinstance(wanted, list) or not wanted or len(wanted) > 64
                               or any(not isinstance(m, str) for m in wanted)):
        raise ValueError("machines must be a non-empty list of machine ids")
    chosen = list(cluster.machines) if wanted is None else list(dict.fromkeys(wanted))
    for mid in chosen:
        ops.machine_of(api, mid, cluster)
    root_ids = arguments.get("roots")
    if root_ids is not None and (not isinstance(root_ids, list) or len(root_ids) > 64
                                 or any(not isinstance(r, str) for r in root_ids)):
        raise ValueError("roots must be a list of root ids")
    plans = []
    for mid in chosen:
        machine = cluster.machines[mid]
        if machine.access not in ("ssh", "local"):
            continue
        roots = [r for r in roots_of(api, machine) if root_ids is None or r[0] in root_ids or f"{mid}:{r[0]}" in root_ids]
        if not roots:
            continue
        payload = dict(base, max_results=limit, deadline_s=MACHINE_DEADLINE_S - 0.5)
        if any(rid == "hyperloom" for rid, _, _ in roots):
            payload["root"] = machine.install_root
        payload["roots"] = [path for rid, _, path in roots if path is not None]
        if machine.access == "local":
            payload["exclude"] = [str(secrets._directory())]
        plans.append((machine, payload, {path or "hyperloom log": label for _, label, path in roots}))
    if not plans:
        raise ValueError("nothing to search on the chosen machines (no root)")
    return plans, limit


_busy_lock = threading.Lock()
_busy = set()          # (id(api), machine id) with a search call in flight (also after a query gave up waiting)


def _search(api, machine, payload):
    key = (id(api), machine.id)
    with _busy_lock:
        if key in _busy:
            raise PermissionError("a search of this machine is still running; try again when it finishes")
        _busy.add(key)
    try:
        reply = ops._node_call(api, machine, "logs-search", payload)
    finally:
        with _busy_lock:
            _busy.discard(key)
    if not isinstance(reply, dict) or reply.get("schema") != "hyperloom/node-v1":
        raise ValueError("unexpected reply from the node tool")
    if reply.get("error"):
        raise ValueError(reply["error"])
    return reply


def query(api, arguments):
    """Run a search on every chosen machine in parallel (one call per machine, each with its own deadline)."""
    plans, limit = build(api, arguments)
    started = time.monotonic()
    results, machines = [], []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(plans)))
    try:
        futures = {pool.submit(_search, api, machine, payload): (machine, labels) for machine, payload, labels in plans}
        done, waiting = concurrent.futures.wait(futures, timeout=QUERY_DEADLINE_S)
        for future, (machine, labels) in futures.items():
            row = {"machine": machine.id, "error": None, "report": None}
            if future in waiting:
                row["error"] = f"no answer within {QUERY_DEADLINE_S:g} s"
            else:
                try:
                    reply = future.result()
                    row["report"] = reply.get("report")
                    for item in reply.get("results") or []:
                        results.append({**item, "machine": machine.id, "root": labels.get(item.get("root"), item.get("root"))})
                except Exception as exc:
                    row["error"] = str(exc)[:300]
            machines.append(row)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    machines.sort(key=lambda r: r["machine"])
    results.sort(key=lambda r: (-(r.get("mtime") or 0), r["machine"], r.get("file") or "", r.get("line") or 0))
    truncated = len(results) > limit
    return api.document("logsearch-results", query={k: v for k, v in arguments.items() if k in QUERY_KEYS},
                        results=results[:limit], machines=machines, truncated=truncated,
                        elapsed_s=round(time.monotonic() - started, 3),
                        why={} if results else {"results": "no line matched in the searched files"})


def _check_root(path, system):
    ops.check_path(path, "root", windows=system == "windows")
    parts = (PureWindowsPath(path) if system == "windows" else PurePosixPath(path)).parts
    if len(parts) < 2:
        raise ValueError("a filesystem root cannot be searched")
    lowered = [p.lower() for p in parts]
    if any(p in (".ssh", ".gnupg", "secrets", "keys", ".hyperloom-boot") for p in lowered):
        raise ValueError("credential directories cannot be searched")
    if system != "windows" and parts[1] in ("proc", "sys", "dev", "boot", "etc", "run", "root"):
        raise ValueError("system directories cannot be searched")
    return path


def save_roots(api, arguments):
    ops.strict(arguments, ("machine", "roots"), ("machine", "roots"))
    machine = ops.machine_of(api, arguments["machine"])
    roots = arguments["roots"]
    if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
        raise ValueError(f"roots must be a list of at most {MAX_ROOTS} absolute directories")
    if roots:
        from hyperloom import downloads
        downloads.learn_home(machine)       # the node's home is never a base, wherever it is
    bases = allowed_bases(api, machine)
    clean = []
    for path in roots:
        _check_root(path, machine.os)
        if not _inside(path, machine.os, bases):
            raise ValueError("a log root must lie inside a root the configuration gives this machine ("
                             + ", ".join(bases) + ") and outside hidden directories")
        if path not in clean:
            clean.append(path)
    store = ops.store_for_write(api)
    every = configured(api, store)
    if clean:
        every[machine.id] = clean
    else:
        every.pop(machine.id, None)
    ops.set_pref(api, NAMESPACE, "roots", every)
    return {"machine": machine.id, "roots": clean,
            "note": "each node checks the roots again (links, system and credential directories are refused)"}


def register(reg):
    reg.get("logsearch", document)
    reg.post("logsearch/query", query)
    reg.post("logsearch/roots", save_roots, audited=True)
