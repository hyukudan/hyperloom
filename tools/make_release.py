#!/usr/bin/env python3
"""Build the hyperloom release archives from a clean checkout (stdlib only).

The release is the product tree at HEAD plus the pinned TensorMeld source (the
router runs from it), with RELEASE.json listing the sha256 of every file. The
engine and strix-trellis are built on the nodes from their pinned SHAs; add
--node-sources to carry their source archives for machines without GitHub.

    python tools/make_release.py [--out dist] [--wheels] [--node-sources]

Writes <name>.zip (Windows), <name>.tar.gz (Linux) and SHA256SUMS, where
<name> is hyperloom-<version>-g<12-hex commit>. Archives are reproducible:
sorted entries, the commit time as every mtime, fixed modes.
"""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "hyperloom/release-v1"
COMPONENTS = ("tensormeld", "engine", "strix-trellis")
# Executable inside the release (git tracks no mode bits on Windows checkouts).
EXECUTABLE = {"node/hyperloom-node", "node/build-node.sh", "tools/make_release.py", "tools/ci-docker.sh"}


def git(*args, cwd=ROOT, binary=False):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else result.stdout.decode().strip()


def version():
    match = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("pyproject.toml has no version")
    return match[1]


def pins():
    """Gitlink SHAs at HEAD, which versions.lock must agree with."""
    lock = json.loads((ROOT / "versions.lock").read_text(encoding="utf-8"))["components"]
    result = {}
    for line in git("ls-tree", "HEAD", "third_party/").splitlines():
        mode, kind, sha, path = line.split(None, 3)
        name = PurePosixPath(path).name
        if kind == "commit" and name in COMPONENTS:
            if lock[name]["commit"] != sha:
                raise SystemExit(f"versions.lock {name} {lock[name]['commit']} != gitlink {sha}")
            result[name] = sha
    if set(result) != set(COMPONENTS):
        raise SystemExit("missing component gitlinks: " + ", ".join(sorted(set(COMPONENTS) - set(result))))
    return result


def extract(archive, destination, prefix=""):
    """Unpack a git archive tar stream, refusing links and paths outside the tree."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isdir() or member.name == "pax_global_header":
                continue
            path = PurePosixPath(prefix) / member.name
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"unsupported archive entry: {member.name}")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


def collect(tree):
    files = {}
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        files[path.relative_to(tree).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def write_archives(tree, name, out, stamp):
    entries = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file())
    zipped = out / (name + ".zip")
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in entries:
            info = zipfile.ZipInfo(f"{name}/{rel}", date_time=time.gmtime(stamp)[:6])
            info.external_attr = (0o100755 if rel in EXECUTABLE else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, tree.joinpath(rel).read_bytes())
    tarred = out / (name + ".tar.gz")
    with open(tarred, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=stamp) as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for rel in entries:
                data = tree.joinpath(rel).read_bytes()
                info = tarfile.TarInfo(f"{name}/{rel}")
                info.size, info.mtime = len(data), stamp
                info.mode = 0o755 if rel in EXECUTABLE else 0o644
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    return [zipped, tarred]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--wheels", action="store_true",
                        help="bundle setuptools and wheel for an offline pip install (needs PyPI now)")
    parser.add_argument("--node-sources", action="store_true",
                        help="add git archives of the pinned engine and strix-trellis under sources/")
    parser.add_argument("--allow-dirty", action="store_true", help="for testing only; the name gets +dirty")
    args = parser.parse_args(argv)
    dirty = git("status", "--porcelain", "--untracked-files=no", "--ignore-submodules=dirty")
    if dirty and not args.allow_dirty:
        raise SystemExit("the checkout has uncommitted changes:\n" + dirty)
    commit = git("rev-parse", "HEAD")
    components = pins()
    name = f"hyperloom-{version()}-g{commit[:12]}" + ("-dirty" if dirty else "")
    stamp = int(git("show", "-s", "--format=%ct", "HEAD"))
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary) / name
        tree.mkdir()
        extract(git("archive", "--format=tar", "HEAD", binary=True), tree)
        source = ROOT / "third_party" / "tensormeld"
        if not (source / ".git").exists():
            raise SystemExit("run: git submodule update --init --depth 1 third_party/tensormeld")
        extract(git("archive", "--format=tar", components["tensormeld"], cwd=source, binary=True),
                tree, "third_party/tensormeld")
        if args.node_sources:
            for component in ("engine", "strix-trellis"):
                checkout = ROOT / "third_party" / component
                if not (checkout / ".git").exists():
                    raise SystemExit(f"run: git submodule update --init third_party/{component}")
                data = git("archive", "--format=tar.gz", f"--prefix={component}-{components[component]}/",
                           components[component], cwd=checkout, binary=True)
                target = tree / "sources" / f"{component}-{components[component]}.tar.gz"
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(data)
        if args.wheels:
            wheels = tree / "wheels"
            subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                            "--dest", str(wheels), "setuptools>=68", "wheel"], check=True)
        manifest = {"schema": SCHEMA, "name": name, "version": version(), "commit": commit,
                    "components": components, "files": collect(tree)}
        (tree / "RELEASE.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outputs = write_archives(tree, name, args.out, stamp)
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in outputs)
    (args.out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    print(json.dumps({"name": name, "archives": [str(p) for p in outputs],
                      "files": len(manifest["files"]), "sha256sums": str(args.out / "SHA256SUMS")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
