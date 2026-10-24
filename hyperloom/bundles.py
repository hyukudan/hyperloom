"""Unpublished component sources: a pinned commit that exists only in a local repository, shipped as a git bundle.

``hyperloom install PROFILE --source NAME=DIR[@COMMIT]`` bundles exactly one ref (``refs/hyperloom/source`` at the
pinned commit, with its history) from DIR, copies it staged and renamed into ``<install root>/incoming/<commit>.bundle``
on the build node, and the node fetches that bundle instead of the remote. The source repository is only read: the
ref lives in a throwaway repository that borrows DIR's objects (``objects/info/alternates``), so DIR gains no ref,
object or config. Without ``--source`` a component is fetched from its remote by commit, as before.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .winproc import no_window


REF = "refs/hyperloom/source"


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    directory: Path
    commit: str | None


@dataclass(frozen=True)
class Bundle:
    name: str
    commit: str
    path: Path
    sha256: str
    bytes: int


def parse(spec):
    """NAME=DIR[@COMMIT]; DIR may itself contain '@' only when a COMMIT suffix is given."""
    if not isinstance(spec, str) or "=" not in spec:
        raise BundleError("--source expects NAME=DIR[@COMMIT]")
    name, rest = spec.split("=", 1)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise BundleError("--source: invalid component name: " + name)
    commit = None
    head, sep, tail = rest.rpartition("@")
    if sep and re.fullmatch(r"[0-9a-f]{7,40}", tail):
        rest, commit = head, tail
    if not rest:
        raise BundleError("--source: missing directory for " + name)
    return Source(name, Path(rest), commit)


def parse_all(specs):
    result = {}
    for spec in specs or ():
        source = parse(spec)
        if source.name in result:
            raise BundleError("--source given twice for " + source.name)
        result[source.name] = source
    return result


def _git(args, cwd=None, timeout=600):
    git = shutil.which("git")
    if git is None:
        raise BundleError("git is required on the control machine for --source")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        # Outside any repository by default: list-heads must not pick up the caller's working directory.
        result = subprocess.run([git, *args], cwd=cwd or tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True,
                                timeout=timeout, env=env, check=False, **no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleError("git failed: " + str(exc)) from exc
    if result.returncode:
        raise BundleError("git " + args[0] + " failed: " + (result.stderr or result.stdout).strip()[-300:])
    return result.stdout.strip()


def resolve(source, pin):
    """The full commit of SOURCE that equals PIN ({commit} or {commit_prefix}); refuses anything else."""
    if not source.directory.is_dir():
        raise BundleError(f"--source {source.name}: not a directory")
    wanted = pin.get("commit") or source.commit or pin.get("commit_prefix")
    if not wanted:
        raise BundleError(f"--source {source.name}: no pinned commit")
    commit = _git(["-C", str(source.directory), "rev-parse", "--verify", "--quiet", wanted + "^{commit}"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BundleError(f"--source {source.name}: commit {wanted} not found in the local repository")
    if source.commit and not commit.startswith(source.commit):
        raise BundleError(f"--source {source.name}: @{source.commit} does not match the preset's pin {wanted}")
    if pin.get("commit") and commit != pin["commit"]:
        raise BundleError(f"--source {source.name}: {commit} is not the preset's pin {pin['commit']}")
    if not pin.get("commit") and not commit.startswith(pin.get("commit_prefix", "")):
        raise BundleError(f"--source {source.name}: {commit} does not match the pinned prefix {pin.get('commit_prefix')}")
    return commit


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def heads(path):
    rows = [line.split() for line in _git(["bundle", "list-heads", str(path)]).splitlines() if line.strip()]
    if any(len(row) != 2 for row in rows):
        raise BundleError("malformed bundle head list")
    return {ref: commit for commit, ref in rows}


def create(source, commit, directory):
    """Write DIRECTORY/<commit>.bundle with exactly REF at COMMIT; the source repository is never written."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BundleError("bundle commit must be a full SHA")
    common = Path(_git(["-C", str(source.directory), "rev-parse", "--git-common-dir"]))
    if not common.is_absolute():
        common = (source.directory / common).resolve()
    objects = common / "objects"
    if not objects.is_dir():
        raise BundleError(f"--source {source.name}: repository objects not found")
    out = Path(directory) / (commit + ".bundle")
    with tempfile.TemporaryDirectory(prefix="hyperloom-bundle-") as scratch:
        borrow = Path(scratch) / "borrow.git"
        _git(["init", "--quiet", "--bare", str(borrow)])
        (borrow / "objects" / "info" / "alternates").write_text(objects.as_posix() + "\n", encoding="utf-8", newline="\n")
        # Depth 1, like the remote fetch (git fetch --depth 1 origin <commit>): the commit is marked shallow, so the
        # bundle carries that commit and its tree only. This also works from a shallow source checkout, and the node
        # marks the same commit shallow before it fetches the bundle.
        (borrow / "shallow").write_text(commit + "\n", encoding="ascii", newline="\n")
        _git(["-C", str(borrow), "update-ref", REF, commit])
        _git(["-C", str(borrow), "bundle", "create", str(out), REF], timeout=3600)
    if heads(out) != {REF: commit}:
        out.unlink(missing_ok=True)
        raise BundleError("bundle does not carry exactly " + REF + " at " + commit)
    return Bundle(source.name, commit, out, _digest(out), out.stat().st_size)
