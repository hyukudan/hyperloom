"""Every recipe operation, once. The command line (`hyperloom recipe ...`, `hyperloom up --recipe`) calls these
functions through ``commands.run``; the portal API runs that same command line (hyperloom/web/recipes.py builds the
argv with ``argv`` and runs the CLI), so a request and its command line give the same answer.

Recipe ids are file stems (``qwen3.8-flash-next.lab``). Sources: the built-in examples (vendored strix-stream recipes,
read-only; ``pending/`` ones wait for something and never launch), the operator's configured directories
(``pc.recipe_dirs``) and the user directory (``<config dir>/recipes``), the only one ``save``/``delete`` write.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
import threading
import time
import uuid

from ..winproc import retry_sharing
from .reader import RecipeError

BUILTIN_DIR = Path(__file__).with_name("examples")
BUILTIN_MANIFEST = Path(__file__).with_name("builtin.json")      # source commit + sha256 of every built-in file
PENDING_DIR = BUILTIN_DIR / "pending"
SUFFIXES = (".toml", ".json")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# Ids that would shadow a portal endpoint (GET recipes/<id>) or read as a CLI keyword.
RESERVED = frozenset(("active", "routes", "probe", "validate", "explain", "check", "diff", "select", "plan", "save",
                      "delete", "list", "show", "pick", "auto", "layers", "save-layer", "delete-layer"))
DEVICES = re.compile(r"(?i)(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?")   # Windows names
MAX_SAVE_BYTES = 256 * 1024
MAX_FILES = 512                  # recipe files read per directory by the catalog (the portal's GETs are bounded)
MAX_FILE_BYTES = 1024 * 1024     # a recipe file larger than this is listed as an error, not read
OVERRIDES_SCHEMA = "strix-stream/recipe-overrides-v1"
# Known x-predicted metrics: (label, unit, kind). Unknown metrics are shown as they are, never dropped.
METRICS = {"ttft_s": ("TTFT", "s", "speed"), "decode_tok_s": ("decode", "tok/s", "speed"),
           "decode_agg_tok_s": ("decode, aggregate", "tok/s", "speed"),
           "serve_wall_agg_tok_s": ("serve, wall aggregate", "tok/s", "speed"),
           "decode_tok_s_with_refill_mode": ("decode with refill mode", "tok/s", "speed"),
           "vram_gib": ("VRAM", "GiB", "memory"), "pinned_gib": ("pinned host RAM", "GiB", "memory"),
           "device_peak_gib_per_rank": ("device peak per rank", "GiB", "memory"),
           "top1_delta_pts": ("top-1 vs Q8", "pts", "quality"), "kl_ratio_vs_q8": ("KL vs Q8", "x", "quality")}
_thread_lock = threading.RLock()


class NotFound(RecipeError):
    """No recipe (or layer file) with that id: the CLI exits 3, the portal answers 404."""


class Conflict(RecipeError):
    """A write against a file that changed, or an id/name already taken: the CLI exits 4, the portal answers 409."""


# --- places ------------------------------------------------------------------------------------------------------

# The cluster configuration in use (`--config`), per call: a context variable, so a portal request (or an in-process
# job with its own configuration) never changes the directories another thread is using.
_config = contextvars.ContextVar("hyperloom_recipe_config", default=None)


def use_config(path):
    """Every recipe directory derives from the cluster configuration in use (`--config`), not the default one. Sets it
    for the current thread's context only (the CLI's main thread; tests)."""
    _config.set(Path(path).absolute() if path else None)


@contextmanager
def configured(path):
    """The configuration in use for the duration of a block (this thread only), restored afterwards."""
    token = _config.set(Path(path).absolute() if path else None)
    try:
        yield
    finally:
        _config.reset(token)


def config_file():
    from ..cli import config_path
    return Path(_config.get() or config_path())


def config_dir():
    return config_file().parent


def user_dir():
    return config_dir() / "recipes"


def layers_dir():
    """Overrides and tuning-profile files the portal may name (it never takes a path from the browser)."""
    return user_dir() / "layers"


def runtime_dir():
    return config_dir() / "run"


def facts_dir():
    return runtime_dir() / "facts"


def receipts_dir():
    return runtime_dir() / "receipts"


def _key(path):
    """One spelling per file or directory: resolved, case-folded where the file system folds case."""
    return os.path.normcase(os.path.realpath(path))


def directories(cluster=None, extra=()):
    """(source, directory) in precedence order, aliases removed (a directory listed twice keeps its first source)."""
    configured = list(extra) + list(((cluster.raw.get("pc") or {}).get("recipe_dirs") or []) if cluster else [])
    rows = [("builtin", BUILTIN_DIR), ("builtin", PENDING_DIR)] + [("configured", Path(d)) for d in configured] + \
        [("user", user_dir())]
    seen, out = set(), []
    for source, directory in rows:
        if _key(directory) not in seen:
            seen.add(_key(directory))
            out.append((source, Path(directory)))
    return out


def candidate_dirs(cluster=None, extra=()):
    """Where pick, select and `up --recipe auto` look: every source except the pending built-ins."""
    return [d for _, d in directories(cluster, extra) if _key(d) != _key(PENDING_DIR)]


def pending(path):
    """True for a file of the built-in pending directory, however it is named."""
    return _key(Path(path).parent) == _key(PENDING_DIR)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# --- catalog -----------------------------------------------------------------------------------------------------

def _row(source, path):
    from . import reader
    if path.stat().st_size > MAX_FILE_BYTES:
        return {"id": path.stem, "name": None, "model": None, "source": source, "writable": source == "user",
                "path": str(path), "sha256": None, "status": source, "blocker": None, "status_text": None,
                "description": None, "error": f"larger than {MAX_FILE_BYTES // 1024} KiB (not read)", "tradeoffs": [],
                "headline": {}, "derived_from": None}
    data = path.read_bytes()
    is_pending = pending(path)
    row = {"id": path.stem, "name": None, "model": None, "source": source, "writable": source == "user",
           "path": str(path), "sha256": _sha256(data), "status": "pending" if is_pending else source, "blocker": None,
           "status_text": None, "description": None, "error": None, "tradeoffs": [], "headline": {},
           "derived_from": None}
    try:
        text = data.decode("utf-8")
        tree = reader.load_text(text, path.name)
        recipe = reader.parse(text, path.name)
    except (UnicodeError, RecipeError) as exc:
        row["error"] = str(exc)
        return row
    derived = tree.get("x-derived-from") if isinstance(tree.get("x-derived-from"), dict) else None
    rows = tradeoffs(tree)
    if derived is not None:
        # A saved copy has no predictions of its own: its base's are shown as such (not re-measured).
        rows = [dict(r, inherited=True) for r in tradeoffs({"x-predicted": derived.get("base_predictions") or []})]
    row.update(name=recipe["name"], model=recipe["match"]["model"], description=recipe.get("description") or None,
               tradeoffs=rows, headline=headline(rows, recipe), derived_from=derived)
    if isinstance(tree.get("x-status"), str):
        row["status_text"] = tree["x-status"]
        if is_pending:
            row["blocker"] = tree["x-status"].removeprefix("pending:").strip()
    return row


def catalog(cluster=None, extra=()):
    """Every recipe file of every source. A recipe id (case-folded) or a recipe name that more than one file has is an
    error on every one of those rows, as `pick` refuses duplicates: no row silently wins."""
    rows = []
    for source, directory in directories(cluster, extra):
        if directory.is_dir():
            files = _recipe_files(directory)
            rows += [_row(source, p) for p in files[:MAX_FILES]]
            if len(files) > MAX_FILES:
                rows.append({"id": f"({len(files) - MAX_FILES} more in {directory})", "overflow": True, "name": None,
                             "model": None, "source": source, "writable": False, "path": str(directory),
                             "sha256": None, "status": source,
                             "error": f"more than {MAX_FILES} recipe files in {directory}: the rest are not listed",
                             "tradeoffs": [], "headline": {}, "blocker": None, "status_text": None,
                             "description": None, "derived_from": None})
    for key, label in ((lambda r: r["id"].casefold(), "recipe id"), (lambda r: r["name"], "recipe name")):
        groups = {}
        for row in rows:
            if not row.get("overflow") and (row["name"] is not None or label == "recipe id"):
                groups.setdefault(key(row), []).append(row)
        for value, same in groups.items():
            if len(same) > 1:
                for row in same:
                    row["error"] = f'duplicate {label} "{value}" in {", ".join(r["path"] for r in same)}'
    for row in rows:
        if row["id"].lower() in RESERVED and not row["error"]:
            row["error"] = f'recipe id "{row["id"]}" is reserved'
        elif not row.get("overflow") and not row["error"] and not is_recipe_id(row["id"]):
            row["error"] = f'recipe id "{row["id"]}" is not an id (letters, digits, ".", "_", "-", no file suffix)'
    return rows


def tradeoffs(tree):
    """The recipe's ``[[x-predicted]]`` rows, normalized (label, unit, kind), in file order."""
    out = []
    for item in tree.get("x-predicted") or []:
        if not isinstance(item, dict) or "metric" not in item or "value" not in item:
            continue
        label, unit, kind = METRICS.get(item["metric"], (item["metric"], "", "other"))
        out.append({"route": item.get("route"), "metric": item["metric"], "label": label, "unit": unit, "kind": kind,
                    "value": item["value"], "at_prompt_tokens": item.get("at_prompt_tokens"),
                    "batch": item.get("batch"), "concurrency": item.get("concurrency"),
                    "basis": item.get("basis"), "scope": item.get("scope"), "evidence": item.get("evidence")})
    return out


def headline(rows, recipe):
    """The few numbers a profile card shows: the best TTFT at 1K / 8K / 32K, the single-request decode rate, the peak
    memory and the quality cost, each with its basis; only predictions of enabled routes (a disabled route does not
    run)."""
    enabled = {r["id"] for r in recipe["routes"] if r["enabled"]}
    out = {}

    def keep(key, row, better):
        if key not in out or better(row["value"], out[key]["value"]):
            out[key] = {k: row.get(k) for k in ("value", "unit", "basis", "route", "label", "inherited")}
    for row in rows:
        if not isinstance(row["value"], (int, float)) or (row["route"] is not None and row["route"] not in enabled):
            continue
        if row["metric"] == "ttft_s" and row["at_prompt_tokens"] in (1024, 8192, 32768):
            keep(f"ttft_{row['at_prompt_tokens'] // 1024}k_s", row, lambda a, b: a < b)
        elif row["metric"] in ("decode_tok_s", "serve_wall_agg_tok_s") and (row["batch"] or row["concurrency"] or 1) == 1:
            keep("decode_tok_s", row, lambda a, b: a > b)
        elif row["kind"] == "memory":
            keep("memory_gib", row, lambda a, b: a > b)
        elif row["kind"] == "quality":
            keep(row["metric"], row, lambda a, b: abs(a) > abs(b))
    return out


def layer_files():
    """The overrides / tuning-profile files of the user layers directory, with what they apply to."""
    from . import reader
    out = []
    directory = layers_dir()
    if directory.is_dir():
        for path in _recipe_files(directory)[:MAX_FILES]:
            if path.stat().st_size > MAX_FILE_BYTES:
                out.append({"name": path.name, "error": f"larger than {MAX_FILE_BYTES // 1024} KiB (not read)"})
                continue
            data = path.read_bytes()
            row = {"name": path.name, "sha256": _sha256(data), "schema": None, "recipe": None, "set": None,
                   "error": None}
            try:
                tree = reader.load_text(data.decode("utf-8"), path.name)
                row.update(schema=tree.get("schema"), recipe=tree.get("recipe"), set=tree.get("set"),
                           recipe_sha256=tree.get("recipe_sha256"))
            except (UnicodeError, RecipeError) as exc:
                row["error"] = str(exc)
            out.append(row)
    return out


def receipt_files():
    """The stored disk-read receipts (for `probe --evidence NODE=NAME`)."""
    out = []
    directory = receipts_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json"))[:MAX_FILES]:
            row = {"name": path.stem, "sha256": None, "host": None, "path": None, "disk_read_gbytes_ps": None,
                   "error": None}
            try:
                data = path.read_bytes()[: 64 * 1024 + 1]
                receipt = json.loads(data)
                row.update(sha256=_sha256(data), **{k: receipt.get(k) for k in ("host", "path", "disk_read_gbytes_ps")})
            except (OSError, ValueError, AttributeError) as exc:
                row["error"] = str(exc)
            out.append(row)
    return out


def find(ref, cluster=None, extra=()):
    """The catalog row of a recipe id; NotFound names the ids that exist."""
    rows = catalog(cluster, extra)
    same = [r for r in rows if r["id"].casefold() == ref.casefold()]
    if not same:
        known = sorted(r["id"] for r in rows if not r["error"])
        raise NotFound(f'unknown recipe "{ref}" (known: {", ".join(known) or "none"})')
    if same[0]["error"]:
        raise RecipeError(f"recipe {ref}: {same[0]['error']}")
    return same[0]


def is_recipe_id(ref):
    """True for a reference that is a recipe id: [A-Za-z0-9][A-Za-z0-9._-]{0,63} without a .toml/.json suffix. It has
    no separator, drive, stream (':') or file suffix, so it is never read as a file name."""
    return isinstance(ref, str) and bool(ID.fullmatch(ref)) and not ref.casefold().endswith(SUFFIXES)


def is_file_ref(ref):
    """True for a reference the CLI reads as a FILE: it has a directory part or a .toml/.json name (any case), and
    its name does not end in '.' or ' ' (Windows would drop those and open another file)."""
    ref = str(ref)
    name = ref.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name != name.rstrip(". ") or "\0" in ref:
        return False
    return "/" in ref or "\\" in ref or name.casefold().endswith(SUFFIXES)


def locate(ref, cluster=None, *, for_up=False, extra=()):
    """A recipe reference -> its file. A recipe id is looked up in the catalog only: the file system is never asked
    about it (a file of the working directory with that name is not read). A FILE reference (``is_file_ref``: a path
    or a .toml/.json name) is the CLI's FILE argument; anything else is refused without touching the file system. The
    portal passes ids only (``argv`` refuses the rest). ``for_up``: a pending built-in is refused, whether it is named
    by id or by path."""
    ref = str(ref)
    if is_recipe_id(ref):
        path = Path(find(ref, cluster, extra)["path"])
    elif is_file_ref(ref) and Path(ref).is_file():
        path = Path(ref)
    else:
        raise NotFound(f"{ref}: no such recipe file or id (a file needs a .toml/.json name or a directory part)")
    if for_up and pending(path):
        raise RecipeError(f"recipe {path.stem} is pending, it does not launch: {_blocker(path)}")
    return path


def _blocker(path):
    try:
        return _row("builtin", Path(path))["blocker"] or "see its header"
    except OSError:
        return "see its header"


def identify(path, cluster=None):
    """The catalog id of a recipe file, or None (a file outside the catalog, or an id in error)."""
    if not path:
        return None
    key = _key(path)
    return next((r["id"] for r in catalog(cluster) if _key(r["path"]) == key and not r["error"]), None)


# --- deployment profiles -----------------------------------------------------------------------------------------

def model_profiles(cluster, model):
    """Deployment profiles whose engine family serves ``model`` (the recipe's match.model)."""
    from .. import render
    from . import facts
    out = []
    for name, settings in (cluster.profiles.items() if cluster is not None else ()):
        try:
            family = render.family(settings.get("preset", name))
        except (OSError, ValueError, KeyError):
            continue
        if facts.FAMILY_MODELS.get(family, (None,))[0] == model:
            out.append(name)
    return out


def models(cluster):
    """{model id: [deployment profiles]} for every engine family with a recipe model id."""
    from . import facts
    return {model: model_profiles(cluster, model) for model, _ in facts.FAMILY_MODELS.values()}


def deployment_profile(cluster, path, profile=None):
    """The profile ``up --recipe`` runs: the given one, else the only profile serving the recipe's model."""
    if profile:
        return profile
    from . import reader, session
    model = reader.parse(session.read_text(path), Path(path).name)["match"]["model"]
    found = model_profiles(cluster, model)
    if len(found) != 1:
        raise RecipeError(f'recipe {Path(path).name} is for "{model}": name the profile to run it with '
                          f'({", ".join(found) if found else "no configured profile serves this model"})')
    return found[0]


# --- snapshots: every input read once ----------------------------------------------------------------------------

INPUTS_SCHEMA = "hyperloom/recipe-inputs-v1"


def _copy_inputs(inputs, root):
    """Copy [(role, path)] into root, one subdirectory per role and index (basenames kept, so two inputs with the same
    basename never meet), and return [{role, original, path, sha256}] for the copies. Each file is read once."""
    out = []
    for index, (role, path) in enumerate(inputs):
        data = Path(path).read_bytes()
        target = Path(root) / f"{index:02d}-{role}"
        target.mkdir(parents=True, exist_ok=True)
        (target / Path(path).name).write_bytes(data)
        out.append({"role": role, "original": str(path), "path": str(target / Path(path).name),
                    "sha256": _sha256(data)})
    return out


def _manifest_digest(manifest):
    return _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))


DEFAULT_INTENT = {"router": "local", "deadline_s": 1140, "queue_timeout_s": 3600}
KEEP_SNAPSHOT_S = 24 * 3600
PIN_S = 6 * 3600              # a launch pins its snapshot while it runs; a pin older than this is a crash leftover
MAX_SNAPSHOTS = 200


def config_path_in_use():
    return config_file()


def launch_inputs(cluster, recipe_path, layers, candidates=(), *, intent, pin=False):
    """Everything a launch depends on, each file read ONCE and published into a content-addressed directory
    `<config dir>/run/recipe-inputs/<digest>/`: `recipe/` (or `candidates/dNN/` for auto), `profile/`, `overrides/NN/`,
    `config/` (the cluster configuration's bytes) and `manifest.json` (ordered, role-labelled entries with original path,
    catalog id, pending flag and sha256, plus the --set entries, the workload and the launch intent: profile, router,
    deadline, queue timeout). digest = sha256 of the canonical manifest. A launch reads only these copies, so a save or
    a configuration edit during it cannot mix versions; a published directory is never rewritten. Returns the digest."""
    entries = [("recipe", Path(recipe_path), f"recipe/{Path(recipe_path).name}")] if recipe_path else []
    for index, directory in enumerate(candidates):
        if Path(directory).is_dir():
            entries += [("candidate", p, f"candidates/d{index:02d}/{p.name}") for p in _recipe_files(directory)]
    if layers.profile:
        entries.append(("profile", Path(layers.profile), f"profile/{Path(layers.profile).name}"))
    entries += [("overrides", Path(p), f"overrides/{i:02d}/{Path(p).name}") for i, p in enumerate(layers.overrides)]
    entries.append(("config", config_path_in_use(), f"config/{config_path_in_use().name}"))
    blobs = [p.read_bytes() for _, p, _ in entries]
    ids_ = {_key(r["path"]): r["id"] for r in catalog(cluster) if not r["error"]}
    manifest = {"schema": INPUTS_SCHEMA, "candidates": len(candidates),
                "entries": [{"role": role, "original": str(original), "id": ids_.get(_key(original)), "name": name,
                             "pending": pending(original), "sha256": _sha256(data), "bytes": len(data)}
                            for (role, original, name), data in zip(entries, blobs)],
                "set": [[path, value] for path, value in layers.sets], "workload": layers.workload,
                "intent": dict(DEFAULT_INTENT, **intent)}
    digest = _manifest_digest(manifest)
    with runtime_lock():
        root = runtime_dir() / "recipe-inputs" / digest
        if not root.is_dir():
            root.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root.parent))
            for index in range(len(candidates)):
                (staging / "candidates" / f"d{index:02d}").mkdir(parents=True)
            for entry, data in zip(manifest["entries"], blobs):
                (staging / entry["name"]).parent.mkdir(parents=True, exist_ok=True)
                (staging / entry["name"]).write_bytes(data)
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

            def publish():
                try:
                    os.replace(staging, root)
                except OSError:
                    if not root.is_dir():
                        raise                                # a sharing violation is retried; anything else is told
                    shutil.rmtree(staging, ignore_errors=True)   # the same inputs were published first: verified below
            try:
                retry_sharing(publish)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        else:
            os.utime(root)                                   # recently planned again: kept by the retention rule
        held = _new_pin(digest) if pin else None             # pinned before anything can prune it
        _prune_snapshots(keep=digest)
    try:
        load_inputs(digest)
    except BaseException:
        release_pin(held)
        raise
    return (digest, held) if pin else digest


def _prune_snapshots(keep):
    """Retention (caller holds the runtime lock): keep snapshots referenced by a launch state, younger than a day, and
    at most MAX_SNAPSHOTS in all; remove the rest, oldest first."""
    directory = runtime_dir() / "recipe-inputs"
    referenced = {keep} | {((state.get("recipe") or {}).get("inputs_digest")) for state in _states().values()}
    now = time.time()
    pins = directory / "pins"
    referenced |= {p.name.split(".")[0] for p in (pins.iterdir() if pins.is_dir() else ())
                   if now - p.stat().st_mtime < PIN_S}
    rows = sorted((p.stat().st_mtime, p) for p in directory.iterdir()
                  if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name))
    excess = len(rows) - MAX_SNAPSHOTS
    for mtime, path in rows:
        if path.name in referenced:
            continue
        if now - mtime > KEEP_SNAPSHOT_S or excess > 0:
            shutil.rmtree(path, ignore_errors=True)
            excess -= 1


def _new_pin(digest):
    """A holder's own pin on a snapshot (caller holds the runtime lock, as pruning does)."""
    if not DIGEST.fullmatch(str(digest)):
        raise RecipeError(f"recipe inputs {digest!r}: expected a 64-hex digest")
    if not (runtime_dir() / "recipe-inputs" / digest).is_dir():
        raise NotFound(f"recipe inputs {digest}: no snapshot (it may have expired: plan again)")
    pins = runtime_dir() / "recipe-inputs" / "pins"
    pins.mkdir(parents=True, exist_ok=True)
    pin = pins / f"{digest}.{uuid.uuid4().hex}"
    pin.write_text(str(os.getpid()), encoding="utf-8")
    return pin


def acquire_pin(digest):
    """Keep a snapshot from being pruned while a launch (deploy or `up --recipe*`) uses it. Each holder has its own pin
    file, taken and released under the runtime lock (the lock pruning holds); once the launch state records the digest,
    that reference keeps it."""
    with runtime_lock():
        return _new_pin(digest)


def release_pin(pin):
    if pin is not None:
        with runtime_lock():
            Path(pin).unlink(missing_ok=True)


@contextmanager
def pinned(digest):
    pin = acquire_pin(digest)
    try:
        yield
    finally:
        release_pin(pin)


def cancel_marker(job):
    """The file whose existence cancels the deploy run with this job id (the portal's cancel writes it)."""
    if not re.fullmatch(r"[0-9a-f]{8,64}", str(job)):
        raise RecipeError(f"deploy job id {job!r}: expected hex")
    return runtime_dir() / "deploy-cancel" / str(job)


def deploy_progress(job):
    """The progress file of a portal launch job (a deploy, or an up with a recipe), next to its cancel marker: its
    phase (checks, stopping, launching once `up` owns the target profile, done, failed), the target profile and the
    profiles it has stopped. A cancel reads it after writing the marker, to undo only what the job did."""
    return cancel_marker(job).with_name(f"{job}.json")


KEEP_PROGRESS_S = 24 * 3600


def read_progress(job):
    """The progress a deploy recorded, or None when it has recorded none yet. A file that exists but cannot be read
    or parsed raises (RecipeError): a cancel must not take it for "nothing recorded"."""
    try:
        data = retry_sharing(lambda: deploy_progress(job).read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecipeError(f"deploy {job}: its progress cannot be read ({exc})") from exc
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError as exc:                                # UnicodeDecodeError included
        raise RecipeError(f"deploy {job}: its progress is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("phase"), str):
        raise RecipeError(f"deploy {job}: its progress has no phase")
    return value


def _write_progress(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    try:
        retry_sharing(lambda: os.replace(temporary, path))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _prune_progress(directory):
    """Progress files and markers of deploys older than a day (a marker written after its deploy ended stays)."""
    now = time.time()
    for path in (directory.iterdir() if directory.is_dir() else ()):
        try:
            if now - path.stat().st_mtime > KEEP_PROGRESS_S:
                path.unlink()
        except OSError:
            pass


def stop_outcome(result):
    """How a `down` answered: "stopped", or "stop requested" (its launch was still running and stops at its next
    check), as orchestrate.down reports it."""
    state = result.get("state") if isinstance(result, dict) else None
    return "stop requested" if state == "stop-requested" else "stopped" if state in (None, "stopped") else str(state)


def _had(progress):
    stopped = progress.get("stopped") or []
    states = progress.get("stop_states") or {}
    what = progress.get("what") or "deploy"
    if stopped:
        had = (f"this {what} had stopped " + ", ".join(f"{p} ({states.get(p, 'stopped')})" for p in stopped)
               + ": they stay stopped (run up to start them again)")
    else:
        had = f"this {what} had stopped nothing"
    if progress.get("stopping"):
        had += (f"; it was stopping {progress['stopping']} (or, meeting this cancel first, left it running: the "
                f"{what}'s own error says which)")
    return had


def launch_began(progress):
    """True once the job's `up` owned the target and went past the hand-over (it may have failed since: a failed
    launch can keep its ownership when its cleanup fails, so its profile is the cancel's to stop)."""
    progress = progress or {}
    return progress.get("phase") in ("launching", "done") or (progress.get("phase") == "failed"
                                                              and bool(progress.get("launched")))


def cancel_summary(progress, *, down=None):
    """One sentence: what a cancelled job had changed and what the cancel undid."""
    progress = progress or {}
    had, profile = _had(progress), progress.get("profile")
    ended = " (the launch had then failed)" if progress.get("phase") == "failed" else ""
    if down is not None:
        return f"the launch of {profile} had begun{ended}: {profile}: {stop_outcome(down)}; {had}"
    if launch_began(progress):
        return f"the launch of {profile} had begun{ended} and was not stopped by this request; {had}"
    if progress.get("phase") == "failed":
        return f"the {progress.get('what') or 'deploy'} had already ended with an error before its launch; {had}"
    return f"cancelled before the launch: nothing was started; {had}"


def load_inputs(digest):
    """(root, manifest) of a published snapshot, every file checked against the manifest."""
    if not DIGEST.fullmatch(str(digest)):
        raise RecipeError(f"recipe inputs {digest!r}: expected a 64-hex digest")
    root = runtime_dir() / "recipe-inputs" / digest
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotFound(f"recipe inputs {digest}: no snapshot (it may have expired: plan again)") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != INPUTS_SCHEMA or _manifest_digest(manifest) != digest:
        raise RecipeError(f"recipe inputs {digest}: the manifest does not match its digest")
    for entry in manifest["entries"]:
        name = Path(entry["name"])
        if name.is_absolute() or ".." in name.parts:
            raise RecipeError(f"recipe inputs {digest}: {entry['name']} is not inside the snapshot")
        try:
            same = _sha256((root / name).read_bytes()) == entry["sha256"]
        except OSError:
            same = False
        if not same:
            raise RecipeError(f"recipe inputs {digest}: {entry['name']} changed since the snapshot")
    return root, manifest


def options_from_inputs(digest, profile=None, *, for_up=True):
    """(cluster, intent, launch.Options) that read only the snapshot ``digest``: the cluster comes from the snapshot's
    configuration copy, the profile and launch arguments from its intent (`up --recipe-inputs`, the portal's deploy)."""
    from .. import orchestrate
    from . import launch, session
    root, manifest = load_inputs(digest)
    entries, intent = manifest["entries"], manifest["intent"]
    if profile is not None and profile != intent["profile"]:
        raise RecipeError(f"recipe inputs {digest[:12]} were planned for profile {intent['profile']}, not {profile}")
    recipe = next((e for e in entries if e["role"] == "recipe"), None)
    if for_up and recipe is not None and recipe["pending"]:
        raise RecipeError(f"recipe {Path(recipe['original']).stem} is pending, it does not launch")
    config = next(e for e in entries if e["role"] == "config")
    cluster = orchestrate.load_cluster(root / config["name"])
    layers = session.Layers(profile=next((root / e["name"] for e in entries if e["role"] == "profile"), None),
                            overrides=[root / e["name"] for e in entries if e["role"] == "overrides"],
                            sets=[tuple(s) for s in manifest["set"]], workload=manifest["workload"])
    inputs = [dict(e, path=str(root / e["name"])) for e in entries if e["role"] != "config"]
    if recipe is None:
        dirs = [root / "candidates" / f"d{i:02d}" for i in range(manifest["candidates"])]
        options = launch.Options("auto", layers, dirs, inputs=inputs, exact_dirs=True, digest=digest)
    else:
        options = launch.Options(str(root / recipe["name"]), layers, [], inputs=inputs, digest=digest)
    return cluster, intent, options


def _recipe_files(directory, limit=None):
    """The recipe files of a directory, sorted; at most ``limit`` (the portal's bound) when given."""
    files = sorted(p for p in Path(directory).iterdir() if p.suffix in SUFFIXES and p.is_file())
    return files if limit is None else files[:limit]


@contextmanager
def snapshot_args(args, names=("file", "a", "b", "recipe_profile", "recipe_overrides")):
    """Copy the files a read-only operation reads into a private temporary directory and point ``args`` at the copies;
    yields a function that maps copy paths in a result back to the originals. Nothing to copy (list, show, active,
    routes: the portal's GETs) creates nothing."""
    if not any(getattr(args, name, None) for name in names):
        yield lambda result: result
        return
    with tempfile.TemporaryDirectory(prefix="hl-recipe-") as directory:
        mapping = {}
        for name in names:
            value = getattr(args, name, None)
            if isinstance(value, Path) and value.is_file():
                copy = _copy_inputs([(name, value)], Path(directory) / name)[0]
                mapping[copy["path"]] = copy["original"]
                setattr(args, name, Path(copy["path"]))
            elif isinstance(value, list) and value:
                copies = _copy_inputs([(name, p) for p in value], Path(directory) / name)
                mapping.update({c["path"]: c["original"] for c in copies})
                setattr(args, name, [Path(c["path"]) for c in copies])
        yield lambda result: rename(result, mapping)


def snapshot_dirs(directories_, root):
    """Copies of the candidate directories (their recipe files only) under root, for pick/select."""
    out, mapping = [], {}
    for index, directory in enumerate(directories_):
        target = Path(root) / f"d{index:02d}"
        target.mkdir(parents=True)
        if Path(directory).is_dir():
            for path in _recipe_files(directory):
                (target / path.name).write_bytes(path.read_bytes())
                mapping[str(target / path.name)] = str(path)
        out.append(target)
    return out, mapping


def rename(value, mapping):
    if isinstance(value, str):
        for copy, original in mapping.items():
            value = value.replace(copy, original)
        return value
    if isinstance(value, dict):
        return {k: rename(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename(v, mapping) for v in value]
    return value


def up_options(cluster, recipe, profile=None, *, layers=None, dirs=(), intent=None):
    """(cluster, intent, launch.Options) for ``up --recipe REF`` (a file, a recipe id or auto): everything is
    snapshotted first and the launch reads only the snapshot (its configuration copy included)."""
    from . import session
    layers = layers or session.Layers()
    if recipe == "auto":
        if not profile:
            raise RecipeError("up --recipe auto needs the profile to run")
        path, candidates = None, candidate_dirs(cluster, dirs)
    else:
        path, candidates = locate(recipe, cluster, for_up=True, extra=dirs), ()
        profile = deployment_profile(cluster, path, profile)
    digest, pin = launch_inputs(cluster, path, layers, candidates, intent=dict(intent or {}, profile=profile), pin=True)
    try:
        cluster, intent, options = options_from_inputs(digest, profile)
    except BaseException:
        release_pin(pin)
        raise
    options.pin = pin                                         # released by the caller once up returns
    return cluster, intent, options


# --- read operations ---------------------------------------------------------------------------------------------

def list_recipes(cluster, model=None):
    rows = [r for r in catalog(cluster) if model is None or r["model"] == model]
    return {"recipes": rows, "models": models(cluster), "layers": layer_files(), "receipts": receipt_files(),
            "user_dir": str(user_dir()),
            "layers_dir": str(layers_dir()), "facts": sorted(p.stem for p in facts_dir().glob("*.json")),
            "builtin": json.loads(BUILTIN_MANIFEST.read_text(encoding="utf-8"))["source"]}


def _kind(kind):
    if isinstance(kind, tuple):
        if kind[0] == "float":
            return {"type": "number", "min": kind[1], "max": kind[2], "min_exclusive": kind[3]}
        if kind[0] == "int":
            return {"type": "integer", "min": kind[1], "max": kind[2]}
        if kind[0] == "enum":
            return {"type": "enum", "values": list(kind[1:])}
    return {"bool": {"type": "boolean"}, "id": {"type": "route"}, "str": {"type": "string"},
            "count": {"type": "count", "values": ["fill"], "min": 0},
            "layers": {"type": "layers"}, "list": {"type": "list"}}.get(kind, {"type": str(kind)})


def fields(recipe):
    """Every settable path with its value (as written or defaulted), its type and range, for the editor's hints; the
    reader (core) stays the judge."""
    from . import reader
    shadow = deepcopy(recipe)
    routes_ = [r["id"] for r in recipe["routes"]]
    out = []
    for path in reader.settable_paths(recipe):
        container, field, kind = reader._locate(shadow, path)
        row = {"path": path, "value": container.get(field), "written": path in recipe["_written"], **_kind(kind)}
        if row["type"] == "route":
            row.update(type="enum", values=[None] + [r for r in routes_ if r != path.split(".")[1]])
        parts = path.split(".")
        row["group"] = (f"route {parts[1]}" + (f" · {parts[3]}" if len(parts) > 4 else "") if parts[0] == "routes"
                        else "limits" if parts[0] == "limits" else f"match · {parts[2]}")
        row["pinned"] = any(path == p or path.startswith(p + ".") for p in recipe["pin"])
        out.append(row)
    return out


def show(cluster, ref):
    """One recipe: its catalog row, source text, structure and settable fields."""
    from . import reader, session
    path = locate(ref, cluster)
    row = next((r for r in catalog(cluster) if _key(r["path"]) == _key(path)), None) or \
        {"id": None, "path": str(path), "source": "file", "writable": False, "sha256": session.digest(path)}
    text = session.read_text(path)
    recipe = reader.parse(text, path.name)
    structure = {"match": recipe["match"], "limits": recipe["limits"], "pin": recipe["pin"],
                 "routes": [{k: r[k] for k in ("id", "phase", "devices", "execute", "when", "fallback",
                                               "handoff_route", "links", "enabled")} for r in recipe["routes"]]}
    return dict(row, text=text, structure=structure, fields=fields(recipe))


def ids(rows, cluster, mapping=None):
    """Add the catalog id to rows that carry a file path (pick/select candidates)."""
    by_key = {_key(r["path"]): r["id"] for r in catalog(cluster) if not r["error"]}
    for row in rows:
        path = (mapping or {}).get(row.get("path"), row.get("path"))
        row["path"] = path
        row["id"] = by_key.get(_key(path)) if path else None
    return rows


def select(cluster, model, profile=None, *, layers=None, source_for=None, backend=None):
    """The recipe `up --recipe auto` would run for MODEL on each deployment profile serving it (or the one named), with
    every candidate's result: passes, invalid, or refused with the reasons."""
    from . import commands, core, session
    serving = model_profiles(cluster, model)
    if profile and profile not in serving:
        raise RecipeError(f'profile {profile} does not serve "{model}" (profiles serving it: {", ".join(serving) or "none"})')
    profiles = [profile] if profile else serving
    if not profiles:
        raise RecipeError(f'no configured profile serves "{model}"')
    backend = backend or core.backend(cluster)
    out = []
    with tempfile.TemporaryDirectory(prefix="hl-recipe-select-") as directory:
        dirs, mapping = snapshot_dirs(candidate_dirs(cluster), directory)
        for name in profiles:
            source = source_for(name) if source_for else commands.live_source(cluster)
            chosen, rows = session.pick(cluster, name, dirs, layers or session.Layers(), source, backend)
            rows = rename(ids([r for r in rows if not r["result"].startswith("other model")], cluster, mapping), mapping)
            out.append({"profile": name, "chosen": ids([{"path": chosen.path}], cluster, mapping)[0]["id"] if chosen else None,
                        "deploy": rename(chosen.summary()["deploy"], mapping) if chosen else None,
                        "candidates": rows})
    return {"model": model, "profiles": out, "reader": backend.label}


CHECKED_AT_START = ["the GPU turn / lease and its approval",
                    "prelaunch: the node tool, assets, release bundle and engine binaries on every machine",
                    "the PC reservation at start",
                    "the RTX PRO idle and free RAM/commit right before and after the service starts",
                    "every node's free-RAM floor after its engine loads"]


UP_CONDITIONS = ("conditional preflight: the profile, node and router checks and the recipe decision + RTX plan pass on "
                 "this snapshot; the checks in checked_at_start run when up starts and can still refuse it")


def config_sha256():
    """The sha256 of the cluster configuration file in use (a deploy refuses when it changed since the plan)."""
    try:
        return _sha256(config_file().read_bytes())
    except OSError:
        return None


def plan(cluster, ref, profile, *, layers=None, source=None, inputs=None):
    """Launch eligibility and preview: what `up PROFILE --recipe REF` decides before any lease (orchestrate's launch
    checks, then a dry run of launch.prepare on a snapshot of the inputs: match, decision, the RTX service planned at
    the recipe's budget, the router's route table). ``up.ok`` only when every check passes; otherwise the refusal, and
    the evaluation and planner preview are still given under ``preview`` (labelled, not deployable). ``inputs`` = the
    snapshot digest the answer is about (deploy launches exactly that snapshot)."""
    from .. import orchestrate
    from . import commands, core, launch, session
    layers = layers or session.Layers()
    if inputs is None:
        recipe = None if ref == "auto" else locate(ref, cluster)
        inputs = launch_inputs(cluster, recipe, layers, candidate_dirs(cluster) if recipe is None else (),
                               intent={"profile": profile})
    root, manifest = load_inputs(inputs)
    originals = {str(root / e["name"]): e["original"] for e in manifest["entries"]}
    entry = next((e for e in manifest["entries"] if e["role"] == "recipe"), None)
    source = source or commands.live_source(cluster)
    out = {"profile": profile, "recipe": (entry["id"] if entry else "auto"),
           "file": entry["original"] if entry else "auto", "inputs": inputs, "intent": manifest["intent"],
           "checked_at_start": CHECKED_AT_START}
    refusal = None
    try:
        cluster, _, options = options_from_inputs(inputs, profile)
        orchestrate.check_launch(cluster, profile, "local", recipe=True)
    except (OSError, ValueError, KeyError) as exc:
        refusal = str(exc)
    if refusal is None:
        with tempfile.TemporaryDirectory(prefix="hl-recipe-plan-") as directory:
            try:
                prepared = launch.prepare(cluster, profile, options, router="local", vram_cap_gib=None,
                                          state_dir=directory, source=source)
            except RecipeError as exc:
                refusal = str(exc)
            else:
                record = prepared.record()
                record["route_table"]["state_file"] = None          # a dry run has no state file
                out.update(up={"ok": True, "production": prepared.production, "conditions": UP_CONDITIONS},
                           evaluation=record,
                           service=record["service"], route_table=_table(prepared.table))
                return rename(out, originals)
    out["up"] = {"ok": False, "refused": refusal, "conditions": UP_CONDITIONS}
    if entry is None:
        return rename(out, originals)
    snap = session.Layers(profile=next((root / e["name"] for e in manifest["entries"] if e["role"] == "profile"), None),
                          overrides=[root / e["name"] for e in manifest["entries"] if e["role"] == "overrides"],
                          sets=list(layers.sets), workload=layers.workload)
    evaluation = session.evaluate(cluster, profile, root / entry["name"], snap, source, core.backend(cluster))
    preview = {"label": "preview only: up would refuse (see up.refused)", "evaluation": evaluation.summary()}
    if evaluation.ok:
        service = dict(evaluation.decision["service"], plan=None)
        if service["start"]:
            cap, host = launch._service_budget(evaluation)
            service.update(vram_cap_gib=cap, host_gib=host)
            service["plan"], why = launch.plan_service(cluster, profile, cap, host)
            if why is not None:
                service.update(start=False, why="planner: " + why)
        preview["service"] = service
    out["preview"] = preview
    return rename(out, originals)


def _table(table):
    return {k: v for k, v in table.items() if k != "state"}


class _Progress:
    """A launch job's progress (``deploy_progress``) and its cancel marker. Order matters: the job records
    "launching" BEFORE it looks at the marker at the hand-over, and a cancel writes the marker BEFORE it reads the
    progress, so either the launch sees the marker and starts nothing, or the cancel sees "launching" and stops the
    profile."""

    def __init__(self, job, what="deploy"):
        self.what = what
        self.marker = cancel_marker(job) if job else None
        self.path = deploy_progress(job) if job else None
        self.state = {"phase": "checks", "what": what, "profile": None, "stopped": [], "stopping": None}
        self.handoff_refused = False
        if self.path is not None:
            _prune_progress(self.path.parent)

    def record(self, **changes):
        self.state.update(changes)
        if self.path is not None:
            _write_progress(self.path, self.state)

    def record_best_effort(self, **changes):
        try:
            self.record(**changes)
        except OSError:
            pass

    def cancelled(self):
        return self.marker is not None and self.marker.exists()

    def check(self):
        if self.cancelled():
            raise RecipeError(f"{self.what} cancelled: " + cancel_summary(self.state))

    def before_stop(self, name):
        """Right before the job stops ``name``: "stopping NAME" is recorded FIRST, then the marker is checked (the
        hand-over's order). A cancel either wrote its marker before this record (the job then stops nothing more) or
        reads "stopping NAME" (its answer names the stop under way)."""
        self.record(phase="stopping", stopping=name)
        if self.cancelled():
            self.state["stopping"] = None          # met the marker first: NAME is left as it is
            self.check()

    def handoff(self):
        """up calls this once, right after it owns the target profile and before it starts anything."""
        self.record(phase="launching")
        self.handoff_refused = self.cancelled()
        return self.handoff_refused


@contextmanager
def launch_progress(job, what, profile=None):
    """The progress and cancel marker of a portal launch job around its work (no job: nothing is recorded). Checks
    the marker first; records "failed" (with whether the launch had begun) or "done" at the end, best effort."""
    progress = _Progress(job, what)
    try:
        progress.record(profile=profile)
        progress.check()
        yield progress
    except BaseException:
        launched = progress.state["phase"] == "launching" and not progress.handoff_refused
        progress.record_best_effort(phase="failed", stopping=None, launched=launched)
        raise
    else:
        # Best effort: "launching", already recorded, makes a later cancel stop the target all the same.
        progress.record_best_effort(phase="done", stopping=None)
    finally:
        if progress.marker is not None:
            try:
                retry_sharing(lambda: progress.marker.unlink(missing_ok=True))
            except OSError:
                pass        # the job's own id: a marker left behind cancels nothing else (pruned after a day)


def deploy(digest, stop=(), *, source=None, orchestrate=None, approval=None, job=None):
    """The switch as one operation: launch exactly the planned snapshot ``digest`` after stopping the reviewed running
    profiles ``stop``. Everything that can refuse deterministically is checked BEFORE anything is stopped: the snapshot
    (manifest digest and every file), the running set (must equal ``stop``), the profile/node/router checks and the
    recipe decision + RTX plan on fresh facts. Then each listed profile is stopped and `up` runs on the snapshot (its
    configuration copy, its launch intent).

    ``job``: the portal's job id. Its cancel marker stops the deploy before the next step; up checks it once more right
    after it owns the profile (the hand-over) and then starts nothing. The progress file says how far the deploy got,
    so a cancel undoes only what the deploy did (a cancelled deploy's error says what it had stopped)."""
    from .. import orchestrate as default_orchestrate
    orchestrate = orchestrate or default_orchestrate
    with launch_progress(job, "deploy") as progress:
        return _deploy(digest, stop, source, orchestrate, approval, progress)


def _deploy(digest, stop, source, orchestrate, approval, progress):
    with pinned(digest):                                      # held from before the first read to the end of up
        return _deploy_pinned(digest, stop, source, orchestrate, approval, progress)


def _deploy_pinned(digest, stop, source, orchestrate, approval, progress):
    from .. import orchestrate as default_orchestrate
    from . import commands, launch
    check = progress.check
    cluster, intent, options = options_from_inputs(digest)
    profile = intent["profile"]
    progress.record(profile=profile)
    # The launch arguments' own checks (deadline, approval, queue timeout) before anything is stopped.
    default_orchestrate._approval(intent["deadline_s"], approval)
    if type(intent["queue_timeout_s"]) is not int or not 60 <= intent["queue_timeout_s"] <= default_orchestrate.MAX_QUEUE_S:
        raise RecipeError("the snapshot's queue timeout is out of range")
    running = sorted(_states())
    if sorted(set(stop)) != running:
        raise Conflict(f"the running profiles are {', '.join(running) or 'none'}, not the reviewed "
                       f"{', '.join(sorted(set(stop))) or 'none'}: review the switch again")
    orchestrate.check_launch(cluster, profile, intent["router"], recipe=True)
    check()
    with tempfile.TemporaryDirectory(prefix="hl-recipe-deploy-") as directory:
        launch.prepare(cluster, profile, options, router=intent["router"], vram_cap_gib=None, state_dir=directory,
                       source=source or commands.live_source(cluster))
    stopped = []
    for name in sorted(set(stop)):
        progress.before_stop(name)
        stopped.append({"profile": name, "down": orchestrate.down(name)})
        progress.record(stopping=None, stopped=progress.state["stopped"] + [name],
                        stop_states=dict(progress.state.get("stop_states") or {},
                                         **{name: stop_outcome(stopped[-1]["down"])}))
    check()
    # The hand-over is checked again by up right after it takes ownership of the profile (no gap: a later cancel
    # sees "launching" and stops the launching profile through its own stop marker).
    try:
        result = orchestrate.up(cluster, profile, router=intent["router"], deadline_s=intent["deadline_s"],
                                queue_timeout_s=intent["queue_timeout_s"], approval=approval, recipe=options,
                                cancelled=progress.handoff)
    except (default_orchestrate.OrchestrationError, OSError, ValueError) as exc:
        if not progress.cancelled():
            raise
        if progress.handoff_refused or progress.state["phase"] != "launching":
            raise RecipeError("deploy cancelled at the launch hand-over: nothing was started; "
                              + _had(progress.state)) from exc
        raise RecipeError(f"deploy cancelled after the launch began: the cancel stops {profile} (its answer says how); "
                          + _had(progress.state)) from exc
    return {"inputs": digest, "profile": profile, "stopped": stopped, "up": result}


# --- the running deployment --------------------------------------------------------------------------------------

def _states(profile=None):
    """{profile: launch state} of the profiles hyperloom launched (their state files; no remote call)."""
    from .. import orchestrate
    paths = [orchestrate._state_path(profile)] if profile else \
        sorted(orchestrate._state_path("x").parent.glob("up-*.json"))
    out = {}
    for path in paths:
        try:
            state = orchestrate._read(path)
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and isinstance(state.get("profile"), str) and state["profile"] == (profile or state["profile"]):
            out[state["profile"]] = state
    return out


def active(cluster, profile=None, *, states=None):
    """The recipe each running profile was launched with, as launched (the launch record: effective recipe, explain
    trace, layers, inputs), and whether each original input still has the bytes it had."""
    out = []
    for name, state in (states if states is not None else _states(profile)).items():
        record = state.get("recipe")
        row = {"profile": name, "phase": state.get("phase"), "started_at": state.get("started_at"),
               "production": state.get("production"), "recipe": None}
        if record is None:
            row["why"] = "launched without a recipe"
            out.append(row)
            continue
        inputs = []
        for item in record.get("inputs") or []:
            try:
                now = _sha256(Path(item["original"]).read_bytes())
                change = "unchanged" if now == item["sha256"] else "changed since the launch"
            except OSError:
                change = "deleted since the launch"
            inputs.append(dict(item, now=change))
        row.update(recipe=next((i["id"] for i in inputs if i["role"] == "recipe" and i.get("id")), None)
                   or record.get("recipe"), record={k: v for k, v in record.items() if k not in ("effective", "explain")},
                   inputs=inputs, effective=record.get("effective"), explain=record.get("explain"))
        if record.get("effective") is None:
            row["why"] = "this launch recorded no effective recipe (launched before hyperloom kept it)"
        out.append(row)
    return {"deployments": out}


def routes(cluster, profile=None, *, states=None, router_status=None):
    """Route statistics of each recipe deployment: the router's prefill_routes (admitted per route at selection time,
    refused, failed hand-offs, the last refusal's trace; `recent` per-request rows when the router keeps them), split
    into RTX (offload) and node routes by the RECORDED launch decision, and the route state now."""
    from .. import orchestrate
    fetch = router_status or orchestrate._http_json
    out = []
    for name, state in (states if states is not None else _states(profile)).items():
        record = state.get("recipe")
        if record is None:
            continue
        places = {p["id"]: ("rtx" if p.get("place") == "offload" else "nodes")
                  for p in ((record.get("deploy") or {}).get("prefill") or [])}
        table = record.get("route_table") or {}
        row = {"profile": name, "recipe": record.get("recipe"), "effective_sha256": table.get("effective_sha256"),
               "routes": [{"id": r["id"], "enabled": r["enabled"], "on": places.get(r["id"], "nodes")}
                          for r in table.get("routes", [])],
               "service": record.get("service"), "stats": None, "totals": None, "recent": None, "state": None,
               "why": {}}
        endpoint = (state.get("router") or {}).get("endpoint")
        if not endpoint:
            row["why"]["stats"] = "no local router endpoint recorded"
        else:
            try:
                status = fetch(endpoint)
                stats = status.get("prefill_routes") if isinstance(status, dict) else None
                if not isinstance(stats, dict):
                    raise ValueError("the router reports no prefill_routes")
                row["stats"] = stats
            except (OSError, ValueError, orchestrate.OrchestrationError) as exc:
                row["why"]["stats"] = f"router status unavailable: {exc}"
        stats = row["stats"]
        if stats is not None:
            if stats.get("effective_sha256") != row["effective_sha256"]:
                row["why"]["identity"] = "the router serves another route table than this launch recorded"
            admitted = stats.get("admitted") if isinstance(stats.get("admitted"), dict) else {}
            totals = {"rtx": 0, "nodes": 0}
            for route in row["routes"]:
                count = admitted.get(route["id"], 0)
                route["admitted"] = count if type(count) is int else None
                totals[route["on"]] += count if type(count) is int else 0
            row["totals"] = dict(totals, refused=stats.get("refused"), handoff_failed=stats.get("handoff_failed"))
            if isinstance(stats.get("recent"), list):
                row["recent"] = [dict(r, on=places.get(r.get("route"), "nodes")) if isinstance(r, dict) else r
                                 for r in stats["recent"]]
            else:
                row["why"]["recent"] = "per-request log unavailable: needs the router's request log (UI-SPEC R7)"
        state_file = table.get("state_file")
        if state_file:
            try:
                data = json.loads(Path(state_file).read_text(encoding="utf-8"))
                at = data.get("at")
                row["state"] = dict(data, age_s=round(time.time() - at, 1) if type(at) in (int, float) else None)
            except (OSError, ValueError, AttributeError):
                row["why"]["state"] = "route state file unreadable (the router fails closed)"
        out.append(row)
    return {"deployments": out}


# --- writes: the user directory only, one cross-process transaction -----------------------------------------------

def check_id(recipe_id, what="recipe id"):
    if not is_recipe_id(recipe_id) or recipe_id.endswith(".") or DEVICES.fullmatch(recipe_id) \
            or recipe_id.lower() in RESERVED:
        raise RecipeError(f"{what} {recipe_id!r}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}, not ending in '.', "
                          "'.toml' or '.json', not a reserved or device name")
    return recipe_id


def _plain_dir(path):
    """Create the directory if needed; refuse a symlink, junction or other reparse point."""
    Path(path).mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) \
            or not stat.S_ISDIR(info.st_mode):
        raise RecipeError(f"{path}: the user recipe directory must be a plain directory (not a link or junction)")
    return Path(os.path.realpath(path))


def _check_overlap(cluster, root):
    """The user directory must not be, contain or sit inside a built-in or configured recipe directory."""
    mine = _key(root)
    for source, directory in directories(cluster):
        if source == "user":
            continue
        other = _key(directory)
        if mine == other or mine.startswith(other + os.sep) or other.startswith(mine + os.sep):
            raise RecipeError(f"the user recipe directory {root} overlaps the {source} directory {directory}: "
                              "nothing is written there")


@contextmanager
def _file_lock(path):
    """An exclusive OS lock on ``path`` (msvcrt / fcntl), across threads and processes."""
    with _thread_lock:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)        # retries for ~10 s, then OSError
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _transaction(cluster, *, layers=False):
    """The one lock every recipe/layer writer (CLI or portal, any process) holds across its checks and its write."""
    root = _plain_dir(user_dir())
    _check_overlap(cluster, root)
    target = _plain_dir(root / "layers") if layers else root
    with _file_lock(root / ".lock"):
        yield target


def _existing(root, name):
    """The file of ``root`` that holds this id (either suffix, any case), or None; two of them is a conflict."""
    found = [p for p in root.iterdir() if p.suffix in SUFFIXES and p.stem.casefold() == name.casefold()]
    if len(found) > 1:
        raise Conflict(f"{', '.join(str(p) for p in found)} hold the same id: remove one first")
    if found and (found[0].is_symlink() or not found[0].is_file()):
        raise RecipeError(f"{found[0]}: not a plain file")
    return found[0] if found else None


def _publish(root, name, data, current):
    """Write data to root/name through an exclusive temporary file, then remove the replaced file if it had another
    spelling."""
    if current is not None and current.suffix == Path(name).suffix:
        name = current.name            # same id, other case: keep the file's own spelling (one file, not two)
    handle, temporary = tempfile.mkstemp(prefix=".hl-", suffix=".part", dir=root)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        target = root / name
        retry_sharing(lambda: os.replace(temporary, target))     # Windows: a reader holding the file for a moment
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if current is not None and current.name != name and current.exists() and not os.path.samefile(current, target):
        retry_sharing(current.unlink)
    if _key(target.parent) != _key(root):
        raise RecipeError(f"{target}: written outside {root}")
    return target


def _check_replace(current, replace, what):
    if current is not None:
        if replace is None:
            raise Conflict(f"{what} exists: pass its sha256 to replace it")
        if _sha256(current.read_bytes()) != replace:
            raise Conflict(f"{what} changed since it was read (sha256 differs): reload it")
    elif replace is not None:
        raise Conflict(f"{what} does not exist (nothing to replace)")


def _overrides_layer(recipe_name, base_text, sets, source):
    return json.dumps({"schema": OVERRIDES_SCHEMA, "recipe": recipe_name, "recipe_sha256": _sha256(base_text.encode("utf-8")),
                       "source": source, "set": {path: value for path, value in sets}}, indent=1) + "\n"


def _resolve_text(text, name, layers=None, backend=None, extra=()):
    """Resolve recipe text (and layer texts [(name, text)]) through the backend, from private copies."""
    from . import session
    with tempfile.TemporaryDirectory(prefix="hl-recipe-check-") as directory:
        path = Path(directory) / name
        path.write_bytes(text.encode("utf-8"))
        files = []
        for index, (layer_name, layer_text) in enumerate(extra):
            files.append(Path(directory) / f"{index:02d}-{layer_name}")
            files[-1].write_bytes(layer_text.encode("utf-8"))
        layers = layers or session.Layers(overrides=files)
        return session.resolve(path, layers, backend=backend)


def derive(base_text, base_file, new_name, sets, base_id, backend):
    """A saved copy of a base recipe with field edits: the edits are one overrides layer; base + that layer is resolved
    first (the CLI's refusals apply, a rank/list pair is accepted in one layer); the values are then written into the
    base's own document tree (x- keys kept, predictions moved to x-derived-from as the base's) and the copy must
    resolve to exactly base + layer. Returns (JSON text, resolved copy)."""
    from . import reader
    base = reader.parse(base_text, base_file)
    sets = list(sets)
    layer = _overrides_layer(base["name"], base_text, sets, "hyperloom recipe save --base")
    expected = _resolve_text(base_text, base_file, backend=backend, extra=[("edits.json", layer)]).to_json()
    tree = reader.load_text(base_text, base_file)
    for path, value in sets:
        _tree_node(tree, path)[path.split(".")[-1]] = value
    tree["name"] = new_name
    tree["x-derived-from"] = {"recipe": base_id or base["name"], "name": base["name"],
                              "sha256": _sha256(base_text.encode("utf-8")), "set": [[p, v] for p, v in sets],
                              "base_predictions": tree.pop("x-predicted", []),
                              "note": "a copy has its own name and sha256: the base's tuning profiles do not apply, "
                                      "and the base's predictions were not re-measured for these values"}
    text = json.dumps(tree, indent=1, ensure_ascii=False) + "\n"
    copy = _resolve_text(text, "copy.json", backend=backend)
    got = copy.to_json()
    expected.update(name=got["name"])
    if got != expected:
        differs = sorted(k for k in set(got) | set(expected) if got.get(k) != expected.get(k))
        raise RecipeError(f"the copy of {base['name']} would not resolve like the base with these values "
                          f"(differs in: {', '.join(differs)})")
    return text, copy


def _tree_node(tree, path):
    parts = path.split(".")

    def item(items, ident):
        return next(x for x in items if isinstance(x, dict) and x.get("id") == ident)
    if parts[0] == "limits":
        return tree.setdefault("limits", {})
    if parts[0] == "match":
        return item(tree["match"][parts[1]], parts[2])
    route = item(tree["routes"], parts[1])
    if len(parts) == 3:
        return route
    if parts[2] == "when":
        return route.setdefault("when", {})
    placement = route.setdefault("placement", {}).setdefault(parts[3], {})
    if len(parts) == 5:
        return placement
    return item(placement["units"], parts[5])


def save(cluster, recipe_id, *, text=None, base=None, base_sha256=None, sets=(), name=None, replace=None,
         backend=None):
    """Write a user recipe after the CLI's validation (the core when installed): ``text`` (TOML or JSON), or ``base``
    (a recipe id or file, at the version ``base_sha256``) + ``sets`` (a derived copy, JSON). ``replace`` = the sha256 of
    the user file being replaced. Never writes outside the user directory or over another source."""
    from . import core, reader, session
    check_id(recipe_id)
    backend = backend or core.backend(cluster)
    if (text is None) == (base is None):
        raise RecipeError("save needs either the recipe text (--from) or a base recipe (--base)")
    if base is not None:
        base_path = locate(base, cluster)
        base_text = session.read_text(base_path)
        if base_sha256 is None or _sha256(base_text.encode("utf-8")) != base_sha256:
            raise Conflict(f"base recipe {base} is not at the version edited (--base-sha256 {base_sha256}): reload it")
        text, resolved = derive(base_text, base_path.name, name or recipe_id, sets, identify(base_path, cluster),
                                backend)
    else:
        if sets or name or base_sha256:
            raise RecipeError("--set, --name and --base-sha256 go with --base")
        resolved = _resolve_text(text, "draft" + (".json" if text.lstrip().startswith("{") else ".toml"),
                                 backend=backend)
    data = text.encode("utf-8")
    if len(data) > MAX_SAVE_BYTES:
        raise RecipeError(f"recipe larger than {MAX_SAVE_BYTES // 1024} KiB")
    suffix = ".json" if text.lstrip().startswith("{") else ".toml"
    new_name = resolved.recipe["name"]
    if not isinstance(new_name, str) or not reader.NAME.fullmatch(new_name):
        raise RecipeError(f"recipe name {new_name!r}: expected {reader.NAME.pattern}")
    with _transaction(cluster) as root:
        current = _existing(root, recipe_id)
        for row in catalog(cluster):
            if current is not None and _key(row["path"]) == _key(current):
                continue
            if row["id"].casefold() == recipe_id.casefold():
                raise Conflict(f'recipe id "{recipe_id}" is taken by {row["source"]} {row["path"]}')
            if row["name"] == new_name:
                raise Conflict(f'recipe name "{new_name}" is taken by {row["id"]} ({row["source"]}): give it another name')
        _check_replace(current, replace, f"user recipe {recipe_id}")
        target = _publish(root, recipe_id + suffix, data, current)
    return {"id": recipe_id, "name": new_name, "path": str(target), "sha256": _sha256(data),
            "replaced": current is not None, "reader": backend.label}


def delete(cluster, recipe_id, sha256):
    """Remove a user recipe at the version read; any other source is refused."""
    check_id(recipe_id)
    with _transaction(cluster) as root:
        current = _existing(root, recipe_id)
        if current is None:
            row = next((r for r in catalog(cluster) if r["id"].casefold() == recipe_id.casefold()), None)
            if row is not None:
                raise RecipeError(f"recipe {recipe_id} is {row['source']} ({row['path']}): only user recipes are deleted")
            raise NotFound(f"no user recipe {recipe_id}")
        _check_replace(current, sha256, f"user recipe {recipe_id}")
        retry_sharing(current.unlink)
    return {"id": recipe_id, "deleted": str(current)}


def save_layer(cluster, layer_name, recipe_ref=None, sets=(), *, text=None, replace=None, backend=None):
    """Write a layer into the user layers directory: an overrides layer built from ``sets`` for ``recipe_ref``
    (strix-stream/recipe-overrides-v1, bound to the recipe's name and sha256), or a given ``text`` (an overrides or a
    tuning-profile document, e.g. the autotuner's output). Checked first like the CLI uses it: an overrides layer is
    resolved with its recipe; a tuning profile is checked for schema, recipe and sha (its staleness against the hardware
    is judged where it is used, as with `--recipe-profile`)."""
    from . import core, reader, session
    check_id(layer_name, "layer name")
    backend = backend or core.backend(cluster)
    if (text is None) == (recipe_ref is None):
        raise RecipeError("save-layer needs --recipe with --set, or --from with a layer document")
    if text is None:
        path = locate(recipe_ref, cluster)
        recipe_text = session.read_text(path)
        recipe = reader.parse(recipe_text, path.name)
        text = _overrides_layer(recipe["name"], recipe_text, list(sets), "hyperloom recipe save-layer")
        _resolve_text(recipe_text, path.name, backend=backend, extra=[(layer_name + ".json", text)])
    else:
        if sets:
            raise RecipeError("--set builds an overrides layer for --recipe; --from takes a whole layer document")
        tree = reader.load_text(text, layer_name)
        if tree.get("schema") not in (reader.PROFILE_SCHEMA, reader.OVERRIDES_SCHEMA):
            raise RecipeError(f"layer {layer_name}: schema must be {reader.PROFILE_SCHEMA} or {reader.OVERRIDES_SCHEMA}")
        row = next((r for r in catalog(cluster) if r["name"] == tree.get("recipe") and not r["error"]), None)
        if row is None:
            raise RecipeError(f'layer {layer_name}: no recipe named "{tree.get("recipe")}" in the catalog')
        recipe_text = session.read_text(row["path"])
        recipe = reader.parse(recipe_text, Path(row["path"]).name)
        if tree["schema"] == reader.OVERRIDES_SCHEMA:
            _resolve_text(recipe_text, Path(row["path"]).name, backend=backend, extra=[(layer_name + ".layer", text)])
        else:
            try:
                reader.parse_layer(text, layer_name, recipe, _sha256(recipe_text.encode("utf-8")), probe=None)
            except reader.StaleProfile as exc:
                if "tuned on another version" in str(exc):
                    raise
                # Hardware staleness is judged where the profile is used (no hardware here).
    data = text.encode("utf-8")
    if len(data) > MAX_SAVE_BYTES:
        raise RecipeError(f"layer larger than {MAX_SAVE_BYTES // 1024} KiB")
    suffix = ".json" if text.lstrip().startswith("{") else ".toml"
    with _transaction(cluster, layers=True) as root:
        current = _existing(root, layer_name)
        _check_replace(current, replace, f"layer {layer_name}")
        target = _publish(root, layer_name + suffix, data, current)
    return {"name": target.name, "recipe": recipe["name"], "path": str(target), "sha256": _sha256(data),
            "replaced": current is not None}


def save_receipt(cluster, name, text, *, replace=None):
    """Store a node disk-read receipt (JSON with the keys `probe --evidence` reads) as <config dir>/run/receipts/NAME.json,
    for `probe --evidence NODE=NAME`; its content is checked against the node and model where it is used."""
    from . import facts
    check_id(name, "receipt name")
    try:
        receipt = json.loads(text)
    except ValueError as exc:
        raise RecipeError(f"receipt {name}: not JSON ({exc})") from exc
    if not isinstance(receipt, dict) or not facts.EVIDENCE_SCHEMA_KEYS <= receipt.keys():
        raise RecipeError(f"receipt {name}: needs {', '.join(sorted(facts.EVIDENCE_SCHEMA_KEYS))}")
    data = text.encode("utf-8")
    if len(data) > 64 * 1024:
        raise RecipeError(f"receipt {name}: larger than 64 KiB")
    with runtime_lock():
        root = _plain_dir(receipts_dir())
        _check_overlap(cluster, root)
        current = _existing(root, name)
        _check_replace(current, replace, f"receipt {name}")
        target = _publish(root, name + ".json", data, current)
    return {"name": target.name, "path": str(target), "sha256": _sha256(data), "replaced": current is not None}


@contextmanager
def runtime_lock():
    """The lock every writer of the runtime recipe files holds: probes (descriptor store, disk cache, evidence) and
    receipts, in the CLI and the portal alike."""
    root = _plain_dir(runtime_dir())
    with _file_lock(root / ".probe.lock"):
        yield root


def delete_layer(cluster, layer_name, sha256):
    check_id(Path(layer_name).stem if Path(layer_name).suffix in SUFFIXES else layer_name, "layer name")
    with _transaction(cluster, layers=True) as root:
        current = _existing(root, Path(layer_name).stem if Path(layer_name).suffix in SUFFIXES else layer_name)
        if current is None:
            raise NotFound(f"no layer {layer_name} in {root}")
        _check_replace(current, sha256, f"layer {layer_name}")
        retry_sharing(current.unlink)
    return {"name": current.name, "deleted": str(current)}


# --- portal request -> command line ------------------------------------------------------------------------------

# JSON argument -> (flag, kind). Positionals in order; "?" = optional. Kinds: string, strings, int, flag, recipe (a
# recipe id, never a path), profile(s) and names of files in hyperloom's own directories (layer, layers, facts, out,
# receipts) - never paths from the browser.
LAYER_ARGS = {"set": ("--recipe-set", "strings"), "workload": ("--recipe-workload", "string"),
              "overrides": ("--recipe-overrides", "layers"), "tuning_profile": ("--recipe-profile", "layer")}
HARDWARE = {"facts": ("--facts", "facts")}
OPERATIONS = {
    "list": ((), {"model": ("--model", "string")}),
    "show": (("recipe",), {}),
    "active": (("profile?",), {}),
    "routes": (("profile?",), {}),
    "validate": (("recipe", "profile?"), {**LAYER_ARGS, **HARDWARE}),
    "explain": (("recipe", "profile?"), {**LAYER_ARGS, **HARDWARE, "all": ("--all", "flag"),
                                         "prompt_tokens": ("--prompt-tokens", "int"),
                                         "unavailable": ("--unavailable", "strings"),
                                         "link_down": ("--link-down", "strings"),
                                         "free_ram": ("--free-ram", "strings")}),
    "diff": (("a", "b"), {"a_set": ("--a-set", "strings"), "b_set": ("--b-set", "strings")}),
    "pick": (("profile",), {**LAYER_ARGS, **HARDWARE}),
    "select": (("model",), {**LAYER_ARGS, **HARDWARE, "profile": ("--profile", "profile")}),
    "plan": (("recipe", "profile"), {**LAYER_ARGS, **HARDWARE}),
    "probe": (("profile",), {"disks": ("--disks", "flag"), "descriptors": ("--descriptors", "flag"),
                             "evidence": ("--evidence", "receipts"), "out": ("--out", "out")}),
    "save": (("id",), {"base": ("--base", "recipe"), "base_sha256": ("--base-sha256", "string"),
                       "set": ("--set", "strings"), "name": ("--name", "string"),
                       "replace": ("--replace", "string")}),
    "delete": (("id",), {"sha256": ("--sha256", "string")}),
    "save-layer": (("name",), {"recipe": ("--recipe", "recipe"), "set": ("--set", "strings"),
                               "replace": ("--replace", "string")}),
    "save-receipt": (("name",), {"replace": ("--replace", "string")}),
    "delete-layer": (("name",), {"sha256": ("--sha256", "string")}),
    "deploy": (("inputs",), {"stop": ("--stop", "profiles"), "job": ("--job", "string")}),
}
# Positionals that name a recipe (an id; "auto" where the CLI takes it), a profile or a snapshot digest.
POSITIONAL_KINDS = {"recipe": "recipe", "a": "recipe", "b": "recipe", "profile": "profile", "inputs": "digest"}
AUTO = frozenset(("plan",))
# Positionals that may be "-": the recipe text comes on stdin (the portal's draft checks and text saves).
STDIN = {"validate": "recipe", "explain": "recipe", "save": "text", "save-layer": "text", "save-receipt": "text"}


def _string(key, value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value or value.startswith("-"):
        raise ValueError(f"{key} must be a nonempty string (at most 4096 characters, not starting with '-')")
    return value


def recipe_ref(key, value, *, auto=False):
    """A recipe reference from the portal: a catalog id (or "auto" where allowed), never a path: no '/', '\\', ':',
    drive, stream, '..' component or file suffix (the CLI then looks it up in the catalog only)."""
    value = _string(key, value)
    if (auto and value == "auto") or is_recipe_id(value):
        return value
    raise ValueError(f"{key} {value!r}: a recipe id ([A-Za-z0-9][A-Za-z0-9._-]{{0,63}}, no .toml/.json suffix"
                     f"{', or auto' if auto else ''}), not a path")


def _profile(key, value):
    value = _string(key, value)
    if not ID.fullmatch(value):
        raise ValueError(f"{key} {value!r}: a profile name ([A-Za-z0-9][A-Za-z0-9._-]{{0,63}})")
    return value


def _positional(operation, key, value):
    kind = POSITIONAL_KINDS.get(key)
    if kind == "recipe":
        return recipe_ref(key, value, auto=operation in AUTO)
    if kind == "profile":
        return _profile(key, value)
    if kind == "digest":
        value = _string(key, value)
        if not DIGEST.fullmatch(value):
            raise ValueError(f"{key} must be a 64-hex snapshot digest")
        return value
    return _string(key, value)


def _named(key, value, directory, suffix=".json"):
    value = _string(key, value)
    stem = value[: -len(suffix)] if value.endswith(suffix) else value
    if not ID.fullmatch(stem) or Path(value).name != value or DEVICES.fullmatch(stem):
        raise ValueError(f"{key} {value!r}: a file name inside {directory} (not a device name such as CON or NUL)")
    return Path(directory) / (stem + suffix)


def argv(operation, arguments, *, config, stdin=False):
    """The `hyperloom recipe ...` argv equivalent to a portal request (validated). ``stdin``: the recipe text goes on
    standard input (positional "-")."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown recipe operation {operation}")
    positionals, options = OPERATIONS[operation]
    allowed = {p.rstrip("?") for p in positionals} | set(options)
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    if set(arguments) - allowed:
        raise ValueError(f"unknown arguments: {', '.join(sorted(set(arguments) - allowed))}")
    out = ["recipe", operation]
    for name in positionals:
        key = name.rstrip("?")
        if stdin and STDIN.get(operation) == key:
            if key in arguments:
                raise ValueError(f"{key} and the text are exclusive")
            out.append("-")
        elif arguments.get(key) is not None:
            out.append(_positional(operation, key, arguments[key]))
        elif not name.endswith("?"):
            raise ValueError(f"{key} is required")
    if stdin and operation in ("save", "save-layer", "save-receipt"):
        out += ["--from", "-"]
    for key, (flag, kind) in options.items():
        value = arguments.get(key)
        if value is None:
            continue
        if kind == "flag":
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
            out += [flag] if value else []
        elif kind == "int":
            if type(value) is not int or not 0 <= value <= 2 ** 40:
                raise ValueError(f"{key} must be an integer 0..2^40")
            out += [flag, str(value)]
        elif kind == "string":
            out += [flag, _string(key, value)]
        elif kind == "recipe":
            out += [flag, recipe_ref(key, value)]
        elif kind == "profile":
            out += [flag, _profile(key, value)]
        elif kind in ("strings", "layers", "receipts", "profiles"):
            if not isinstance(value, list) or len(value) > 256:
                raise ValueError(f"{key} must be an array of at most 256 strings")
            for item in value:
                if kind == "layers":
                    item = str(_named(key, item, layers_dir(), _suffix_of(item)))
                elif kind == "profiles":
                    item = _profile(key, item)
                elif kind == "receipts":
                    node, _, receipt = _string(key, item).partition("=")
                    item = f"{node}={_named(key, receipt, receipts_dir(), _suffix_of(receipt))}"
                out += [flag, _string(key, item)]
        elif kind == "layer":
            out += [flag, str(_named(key, value, layers_dir(), _suffix_of(value)))]
        elif kind in ("facts", "out"):
            out += [flag, str(_named(key, value, facts_dir()))]
    return out + ["--config", str(config)]


def _suffix_of(name):
    return ".toml" if isinstance(name, str) and name.endswith(".toml") else ".json"


def command_line(arguments):
    """An argv as one shell line (POSIX quoting), as docs/api.md shows it."""
    return " ".join(shlex.quote(str(a)) for a in ["hyperloom", *arguments])
