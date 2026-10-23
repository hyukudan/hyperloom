"""Marked node installs and detached, low-priority build/hash jobs (Python 3.10+)."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid


VERBS = {"install", "job", "releases", "activate", "uninstall", "verify-model", "push-release"}
RUNTIME_ENV = {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"}
TERMINAL = {"stopped", "failed", "aborted", "cancelled", "canceled", "finished"}


def text(value):
    if (not isinstance(value, str) or not value
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)):
        raise ValueError("expected nonempty text without control characters")
    return value


def absolute(value):
    value = text(value)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("expected an absolute path without parent traversal")
    return path


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", text(value)):
        raise ValueError("invalid identifier")
    return value


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def release_id(manifest):
    return hashlib.sha256(canonical(manifest)).hexdigest()[:12]


def read_json(path):
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("state file too large")
    return json.loads(data)


def atomic_json(path, obj):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(canonical(obj) + b"\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inside(root, *parts):
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("path escapes marked root")
    return path


def overlap(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def checked_root(payload, create=False):
    requested = absolute(payload.get("root"))
    if requested.is_symlink():
        raise ValueError("install root must not be a symlink")
    root = requested.resolve()
    home = Path.home().resolve()
    if home.is_relative_to(root) or root == Path(root.anchor):
        raise ValueError("install root is or contains HOME or a filesystem root")
    policy_path = inside(root, "etc", "install-policy.json")
    policy = read_json(policy_path) if policy_path.is_file() else {}
    protected = list(policy.get("asset_paths", [])) + list(payload.get("asset_paths", []))
    lab = payload.get("lab_dir") or policy.get("lab_dir")
    if lab is not None:
        protected.append(lab)
    for value in protected:
        if overlap(root, absolute(value).resolve()):
            raise ValueError("install root overlaps lab or configured asset path")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("install root overlaps a git repository")
    if root.exists():
        # Do not follow links when searching for a repository inside a proposed root.
        for directory, dirs, files in os.walk(root, followlinks=False):
            relative = Path(directory).relative_to(root)
            # Fetched sources are expected repositories in an already marked install.
            if relative.parts and relative.parts[0] == "src" and (root / ".hyperloom-root").is_file():
                dirs[:] = []
                continue
            if ".git" in dirs or ".git" in files:
                raise ValueError("install root contains a git repository")
    marker = inside(root, ".hyperloom-root")
    if marker.is_symlink():
        raise ValueError("invalid root marker")
    if not marker.exists():
        if not create:
            raise ValueError("unmarked install root")
        if root.exists() and any(root.iterdir()):
            raise ValueError("refusing nonempty unmarked install root")
        root.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="ascii") as stream:
            stream.write(uuid.uuid4().hex + "\n")
    if not re.fullmatch(r"[0-9a-f]{32}", marker.read_text(encoding="ascii").strip()):
        raise ValueError("invalid root marker")
    for name in ("etc", "run", "log", "src", "releases"):
        inside(root, name).mkdir(exist_ok=True)
    if create:
        atomic_json(policy_path, {"lab_dir": lab, "asset_paths": sorted(set(protected) - {lab})})
    return root


def lab_idle(lab):
    if lab is None:
        return
    holder = absolute(lab) / ".gpu-turn" / "holder"
    try:
        line = holder.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    if line.startswith("PAIR"):
        raise ValueError("builds and model hashing are refused during a PAIR turn")


def active_leases(root, profile=None):
    result = []
    for path in inside(root, "run").glob("lease-*.json"):
        state = read_json(inside(root, "run", path.name))
        if state.get("state") in TERMINAL:
            continue
        recorded_profile = state.get("profile", state.get("spec", {}).get("profile"))
        # Older lease records have no profile: fail closed rather than switch live code.
        if profile is None or recorded_profile is None or recorded_profile == profile:
            result.append(state)
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release(root, rid):
    if not re.fullmatch(r"[0-9a-f]{12}", text(rid)):
        raise ValueError("release_id must be 12 lowercase hex characters")
    directory = inside(root, "releases", rid)
    manifest = read_json(inside(root, "releases", rid, "MANIFEST.json"))
    if release_id(manifest) != rid:
        raise ValueError("manifest digest does not match release id")
    binaries = manifest.get("binaries")
    if not isinstance(binaries, dict) or not binaries:
        raise ValueError("release has no binary hashes")
    for name, digest in binaries.items():
        identifier(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid binary digest")
        binary = inside(root, "releases", rid, "bin", name)
        if binary.is_symlink() or sha256(binary) != digest:
            raise ValueError("binary sha256 mismatch")
    if not all(manifest.get("capabilities", {}).get(name, {}).get("ok") for name in binaries):
        raise ValueError("release capability check missing or failed")
    return directory, manifest


def static_capabilities(binary, flags):
    data = Path(binary).read_bytes()
    if data[:4] != b"\x7fELF":
        return {"ok": False, "method": "static", "missing": ["<not an ELF executable>"]}
    missing = [flag for flag in flags if flag.encode("utf-8") not in data]
    return {"ok": not missing, "method": "static", "checked": len(flags), "missing": missing}


def command(argv, **kwargs):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, check=True, **kwargs)


def version(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, check=False)
        return {"returncode": result.returncode,
                "text": (result.stdout + result.stderr).decode("utf-8", "replace")[:16384].strip()}
    except (OSError, subprocess.TimeoutExpired):
        return {"returncode": None, "text": "unavailable"}


def envelope():
    if shutil.which("systemd-run"):
        try:
            command(["systemd-run", "--user", "--scope", "-p", "MemoryMax=16G", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return ["systemd-run", "--user", "--scope", "-p", "MemoryMax=16G", "nice", "-n", "19"]
        except (OSError, subprocess.SubprocessError):
            pass
    return ["nice", "-n", "19"]


def launch_job(root, kind, payload):
    if os.name != "posix":
        raise ValueError("node background jobs require POSIX")
    job = uuid.uuid4().hex
    spec = inside(root, "run", "job-" + job + ".spec.json")
    state_path = inside(root, "run", "job-" + job + ".json")
    log = inside(root, "log", "job-" + job + ".log")
    prefix = envelope() if kind == "install" else ["nice", "-n", "19"]
    if kind == "verify-model" and shutil.which("ionice"):
        prefix += ["ionice", "-c3"]
    atomic_json(spec, {"root": str(root), "kind": kind, "payload": payload,
                       "job": job, "limited": prefix[0] != "systemd-run"})
    atomic_json(state_path, {"job": job, "kind": kind, "state": "queued", "log": str(log)})
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(["nohup", *prefix, sys.executable, str(Path(__file__).resolve()),
                                      "--job", str(spec)], stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output, start_new_session=True,
                                     close_fds=True)
    except OSError as exc:
        atomic_json(state_path, {"job": job, "state": "failed", "error": str(exc)})
        raise
    threading.Thread(target=child.wait, daemon=True).start()
    # Do not rewrite queued state here: the worker may already have finished.
    return {"job": job, "pid": child.pid, "log": str(log)}


def validate_install(payload):
    components = payload.get("components")
    if not isinstance(components, dict) or "engine" not in components:
        raise ValueError("components.engine is required")
    for name, item in components.items():
        identifier(name)
        if not isinstance(item, dict) or set(item) != {"repo", "commit"}:
            raise ValueError("component requires repo and commit")
        if not re.fullmatch(r"[0-9a-f]{40}", text(item["commit"])):
            raise ValueError("component commit must be an immutable full SHA")
        if text(item["repo"]).startswith("-"):
            raise ValueError("invalid component repository")
    recipe = payload.get("recipe")
    if (not isinstance(recipe, dict) or not {"configure", "targets", "ctest_regex", "trellis"} <= set(recipe)
            or set(recipe) - {"configure", "targets", "ctest_regex", "trellis", "required_flags"}):
        raise ValueError("recipe requires configure, targets, ctest_regex and trellis (optional required_flags)")
    flags = recipe.get("required_flags", {})
    if not isinstance(flags, dict) or any(
            not isinstance(v, list) or not all(isinstance(f, str) and f.startswith("--") and len(f) < 128 for f in v)
            for v in flags.values()):
        raise ValueError("required_flags must map targets to lists of --flags")
    if not isinstance(recipe["configure"], list):
        raise ValueError("configure must be an argv list")
    for arg in recipe["configure"]:
        text(arg)
    if not isinstance(recipe["targets"], list) or not recipe["targets"]:
        raise ValueError("targets must be a nonempty list")
    for target in recipe["targets"]:
        identifier(target)
    if len(set(recipe["targets"])) != len(recipe["targets"]):
        raise ValueError("duplicate build targets")
    text(recipe["ctest_regex"])
    if type(recipe["trellis"]) is not bool:
        raise ValueError("trellis must be boolean")
    if recipe["trellis"] and "strix-trellis" not in components:
        raise ValueError("trellis source component is required")


def fetch_sources(root, components):
    """Fetch pinned sources, including the router's unbuilt Python package."""
    sources = {}
    for name, item in sorted(components.items()):
        identifier(name)
        if not re.fullmatch(r"[0-9a-f]{40}", text(item["commit"])):
            raise ValueError("component commit must be an immutable full SHA")
        if text(item["repo"]).startswith("-"):
            raise ValueError("invalid component repository")
        source = inside(root, "src", name + "-" + item["commit"])
        source.mkdir(exist_ok=True)
        if not (source / ".git").exists():
            command(["git", "init", str(source)])
        # Recover cleanly after an interrupted init/remote setup.
        command(["git", "-C", str(source), "config", "remote.origin.url", item["repo"]])
        command(["git", "-C", str(source), "fetch", "--depth", "1", "origin", item["commit"]])
        command(["git", "-C", str(source), "checkout", "--force", "FETCH_HEAD"])
        actual = command(["git", "-C", str(source), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
        if actual != item["commit"]:
            raise ValueError("fetched source commit mismatch")
        sources[name] = source
    return sources


def build(root, payload, job):
    lab_idle(payload.get("lab_dir"))
    sources = fetch_sources(root, payload["components"])
    staging = inside(root, "releases", ".build-" + job)
    staging.mkdir()
    output = staging / "bin"
    recipe = payload["recipe"]
    env = dict(os.environ, **RUNTIME_ENV)
    env["HYPERLOOM_CONFIGURE"] = "\n".join(recipe["configure"])
    env["HYPERLOOM_CTEST_REGEX"] = recipe["ctest_regex"]
    try:
        command(["bash", str(Path(__file__).with_name("build-node.sh")), str(sources["engine"]),
                 str(sources["strix-trellis"]) if recipe["trellis"] else "-",
                 str(inside(root, "run", "build-" + job)), str(output), *recipe["targets"]], env=env)
        hashes, capabilities = {}, {}
        for target in recipe["targets"]:
            binary = inside(root, "releases", staging.name, "bin", target)
            if binary.is_symlink():
                raise ValueError("release binaries must be regular files")
            hashes[target] = sha256(binary)
            # Static check only: engine binaries are never executed outside a GPU lease (a serve binary
            # given an unknown flag can open GPU or USB4 devices). The preset's flags must be present as
            # strings in the binary.
            capabilities[target] = static_capabilities(binary, recipe.get("required_flags", {}).get(target, []))
            if not capabilities[target]["ok"]:
                raise ValueError("capability check failed: " + target + " missing " +
                                 ",".join(capabilities[target]["missing"][:8]))
        node = Path(__file__).with_name("hyperloom-node").read_text(encoding="utf-8")
        node_version = re.search(r'^VERSION = "([^"]+)"', node, re.MULTILINE).group(1)
        rocm_file = Path("/opt/rocm/core-10.1/.info/version")
        manifest = {"schema": "hyperloom/release-v1", "components": payload["components"],
                    "recipe": recipe, "binaries": hashes, "capabilities": capabilities,
                    "hyperloom_node_version": node_version, "runtime_env": RUNTIME_ENV,
                    "toolchains": {"amdclang++": version([shutil.which("amdclang++") or
                                                           "/opt/rocm/core-10.1/bin/amdclang++", "--version"]),
                                   "cmake": version(["cmake", "--version"]),
                                   "ninja": version(["ninja", "--version"]),
                                   "rocm": rocm_file.read_text().strip() if rocm_file.is_file() else "unavailable",
                                   "rocm_package": version(["dpkg-query", "-W", "-f=${Version}", "rocm-core"])}}
        rid = release_id(manifest)
        atomic_json(staging / "MANIFEST.json", manifest)
        destination = inside(root, "releases", rid)
        if destination.exists():
            release(root, rid)
        else:
            staging.rename(destination)
        return {"release_id": rid}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def job_main(spec_path):
    spec = read_json(absolute(spec_path))
    root = checked_root({"root": spec["root"]})
    job = spec["job"]
    identifier(job)
    path = inside(root, "run", "job-" + job + ".json")
    state = {"job": job, "kind": spec["kind"], "pid": os.getpid(), "state": "running",
             "started": time.time(), "log": str(inside(root, "log", "job-" + job + ".log"))}
    atomic_json(path, state)
    try:
        if spec["limited"]:
            import resource
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            cap = 16 * 1024 ** 3
            if hard != resource.RLIM_INFINITY:
                cap = min(cap, hard)
            resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
        if spec["kind"] == "install":
            state.update(build(root, spec["payload"], job))
        else:
            payload = spec["payload"]
            lab_idle(payload.get("lab_dir"))
            model = absolute(payload["path"])
            if model.stat().st_size != payload["bytes"]:
                raise ValueError("model size mismatch")
            digest = sha256(model)
            if digest != payload["sha256"]:
                raise ValueError("model sha256 mismatch")
            state.update(sha256=digest, verified=True)
        state["state"] = "succeeded"
    except Exception as exc:
        state.update(state="failed", error=str(exc))
    finally:
        state["finished"] = time.time()
        atomic_json(path, state)
        absolute(spec_path).unlink(missing_ok=True)
    return 0 if state["state"] == "succeeded" else 1


def push_release(root, payload):
    directory, manifest = release(root, payload.get("release_id"))
    peer = text(payload.get("peer_ssh"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", peer) or peer.count("@") > 1:
        raise ValueError("invalid peer ssh alias")
    peer_root = text(payload.get("peer_root"))
    if not PurePosixPath(peer_root).is_absolute() or ".." in PurePosixPath(peer_root).parts:
        raise ValueError("peer_root must be absolute without parent traversal")
    peer_root = str(PurePosixPath(peer_root))
    rid = directory.name
    staging = str(PurePosixPath(peer_root) / "releases" / (".incoming-" + uuid.uuid4().hex))
    q = shlex.quote
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "--", peer]
    releases = str(PurePosixPath(peer_root) / "releases")
    destination = releases + "/" + rid
    expected = {"MANIFEST.json": sha256(directory / "MANIFEST.json"),
                **{"bin/" + n: d for n, d in manifest["binaries"].items()}}

    def verify_peer(path):
        for name, digest in expected.items():
            result = command([*ssh, "sha256sum -- " + q(path + "/" + name)],
                             capture_output=True, text=True, timeout=30)
            if not result.stdout.split() or result.stdout.split()[0] != digest:
                raise ValueError("peer binary sha256 mismatch")

    # Bootstrap already shipped this module: use the same root guards on a fresh peer.
    peer_policy = {"root": peer_root, "lab_dir": payload.get("peer_lab_dir"),
                   "asset_paths": payload.get("peer_asset_paths", [])}
    prepare = ("import sys; from pathlib import Path; "
               "sys.path.insert(0, str(Path.home()/'.hyperloom-boot')); "
               "import hyperloom_install; root=hyperloom_install.checked_root(" + repr(peer_policy) + ", create=True); "
               "hyperloom_install.fetch_sources(root, " + repr({name: row for name, row in
                   manifest.get("components", {}).items() if name == "tensormeld"}) + ")")
    command([*ssh, "python3 -c " + q(prepare)], timeout=600)
    # Reject remote symlink ancestors again before writing the unique staging directory.
    guard = (f"test -f {q(peer_root + '/.hyperloom-root')} && "
             f"test \"$(readlink -f -- {q(peer_root)})\" = {q(peer_root)} && "
             f"test ! -L {q(releases)} && test ! -L {q(destination)} && "
             f"mkdir -p -- {q(releases)} && "
             f"if test -d {q(destination)}; then printf existing; else mkdir -- {q(staging)}; fi")
    prepared = command([*ssh, guard], capture_output=True, text=True, timeout=30)
    if prepared.stdout.strip() == "existing":
        verify_peer(destination)
        return {"release_id": rid, "verified": True}
    try:
        if shutil.which("rsync"):
            command(["rsync", "-a", "-s", "-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes",
                     "--", str(directory) + "/", peer + ":" + staging + "/"], timeout=600)
        else:
            with subprocess.Popen(["tar", "-C", str(directory), "-cf", "-", "."],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as archive:
                try:
                    subprocess.run([*ssh, "tar -C " + q(staging) + " -xf -"], stdin=archive.stdout,
                                   check=True, timeout=600)
                    archive.stdout.close()
                    archive.wait(timeout=5)
                finally:
                    archive.stdout.close()
                    if archive.poll() is None:
                        archive.terminate()
                    archive.wait(timeout=5)
                if archive.returncode:
                    raise ValueError("release archive failed")
        verify_peer(staging)
        command([*ssh, f"test ! -e {q(destination)} && mv -- {q(staging)} {q(destination)}"], timeout=30)
        return {"release_id": rid, "verified": True}
    finally:
        # The generated staging path was checked against the peer root above.
        command([*ssh, "rm -rf -- " + q(staging)], timeout=30)


def run(verb, payload):
    result = {"schema": "hyperloom/node-v1", "verb": verb}
    try:
        if verb not in VERBS or not isinstance(payload, dict):
            raise ValueError("unsupported install request")
        if verb == "verify-model":
            model = absolute(payload.get("path"))
            size = payload.get("bytes")
            if type(size) is not int or size < 0 or model.stat().st_size != size:
                raise ValueError("model size mismatch")
            digest = payload.get("sha256")
            if digest is None:
                return {**result, "verified": True, "bytes": size}
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid model sha256")
            if "root" not in payload:
                raise ValueError("root is required for a background hashing job")
        if verb == "install":
            validate_install(payload)
            lab_idle(payload.get("lab_dir"))
        root = checked_root(payload, create=verb == "install")
        if verb in {"install", "verify-model"}:
            policy = read_json(root / "etc" / "install-policy.json")
            payload = {**payload, "lab_dir": payload.get("lab_dir") or policy.get("lab_dir")}
            lab_idle(payload.get("lab_dir"))
            result.update(launch_job(root, verb, payload))
        elif verb == "job":
            job = identifier(payload.get("job"))
            state = read_json(inside(root, "run", "job-" + job + ".json"))
            # A failed job is a successful query, not a protocol failure (exit 2).
            # Keep its diagnostic out of the response envelope's error field.
            if "error" in state:
                state["reason"] = state.pop("error")
            result.update(state)
            log = inside(root, "log", "job-" + job + ".log")
            result["tail"] = ""
            if log.exists():
                with log.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 65536))
                    result["tail"] = stream.read(65536).decode("utf-8", errors="ignore")
        elif verb == "releases":
            active = {}
            for path in (root / "etc").glob("active-*.json"):
                state = read_json(inside(root, "etc", path.name))
                active[path.name[7:-5]] = state["release_id"]
            rows = []
            for directory in sorted((root / "releases").iterdir()):
                if re.fullmatch(r"[0-9a-f]{12}", directory.name):
                    _, manifest = release(root, directory.name)
                    rows.append({"release_id": directory.name, "manifest": manifest,
                                 "active_profiles": [p for p, rid in active.items() if rid == directory.name]})
            result.update(releases=rows, active=active)
        elif verb == "activate":
            profile = identifier(payload.get("profile"))
            if active_leases(root, profile):
                raise ValueError("cannot activate while a lease for this profile is running")
            _, manifest = release(root, payload.get("release_id"))
            atomic_json(inside(root, "etc", "active-" + profile + ".json"),
                        {"profile": profile, "release_id": release_id(manifest)})
            result.update(profile=profile, release_id=release_id(manifest))
        elif verb == "push-release":
            result.update(push_release(root, payload))
        elif verb == "uninstall":
            if active_leases(root):
                raise ValueError("cannot uninstall while a lease is running")
            if any(read_json(p).get("state") in {"queued", "running"}
                   for p in (root / "run").glob("job-*.json") if not p.name.endswith(".spec.json")):
                raise ValueError("cannot uninstall while a job is running")
            keep = payload.get("keep_logs", False)
            if type(keep) is not bool:
                raise ValueError("keep_logs must be boolean")
            # rmtree unlinks symlinks; it never follows them into assets or other roots.
            if keep:
                for path in root.iterdir():
                    if path.name in {"log", ".hyperloom-root"}:
                        continue
                    if path.is_symlink() or not path.is_dir():
                        path.unlink()
                    else:
                        shutil.rmtree(path)
            else:
                shutil.rmtree(root)
            result.update(uninstalled=True, kept_logs=keep)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        raise SystemExit(job_main(sys.argv[2]))
    raise SystemExit("internal job entry point only")
