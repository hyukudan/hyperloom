import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_release", ROOT / "tools" / "make_release.py")
make_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_release)


def _tar(**members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return data.getvalue()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_extract_refuses_traversal_and_links(self):
        with self.assertRaises(SystemExit):
            make_release.extract(_tar(**{"../escape": b"x"}), self.directory)
        link = io.BytesIO()
        with tarfile.open(fileobj=link, mode="w") as archive:
            info = tarfile.TarInfo("link")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(info)
        with self.assertRaises(SystemExit):
            make_release.extract(link.getvalue(), self.directory)
        make_release.extract(_tar(**{"a/b.txt": b"ok"}), self.directory, "third_party/x")
        self.assertEqual((self.directory / "third_party/x/a/b.txt").read_bytes(), b"ok")

    def test_archives_are_reproducible_with_fixed_modes(self):
        tree = self.directory / "tree"
        (tree / "node").mkdir(parents=True)
        (tree / "node" / "hyperloom-node").write_bytes(b"#!/usr/bin/env python3\n")
        (tree / "README.md").write_bytes(b"readme\n")
        digests = []
        for run in ("one", "two"):
            out = self.directory / run
            out.mkdir()
            paths = make_release.write_archives(tree, "hyperloom-0-g000000000000", out, 1700000000)
            digests.append([hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
        self.assertEqual(digests[0], digests[1])
        with tarfile.open(self.directory / "one" / "hyperloom-0-g000000000000.tar.gz") as archive:
            modes = {m.name: m.mode for m in archive.getmembers()}
        self.assertEqual(modes["hyperloom-0-g000000000000/node/hyperloom-node"], 0o755)
        self.assertEqual(modes["hyperloom-0-g000000000000/README.md"], 0o644)

    def test_git_archive_line_endings_do_not_depend_on_local_config(self):
        repo = self.directory / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True).stdout

        git("init", "-q")
        (repo / ".gitattributes").write_bytes(b"* text=auto\n")
        (repo / "a.txt").write_bytes(b"one\ntwo\n")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
        git("config", "core.autocrlf", "true")
        self.assertIn(b"one\r\n", git("archive", "--format=tar", "HEAD"))
        with tarfile.open(fileobj=io.BytesIO(git(*make_release.ARCHIVE, "--format=tar", "HEAD"))) as archive:
            self.assertEqual(archive.extractfile("a.txt").read(), b"one\ntwo\n")

    def test_allow_dirty_is_named_after_head(self):
        source = (ROOT / "tools" / "make_release.py").read_text(encoding="utf-8")
        self.assertNotIn('"-dirty"', source)
        self.assertIn("NOT included", source)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer")
    def test_installer_verifies_release_before_any_change(self):
        release = self.directory / "hyperloom-0.1.0-g0123456789ab"
        (release / "windows").mkdir(parents=True)
        shutil.copyfile(ROOT / "windows" / "install.ps1", release / "windows" / "install.ps1")
        (release / "README.md").write_text("readme\n", encoding="utf-8")
        (release / "wheels").mkdir()
        (release / "wheels" / "w-1-py3-none-any.whl").write_bytes(b"wheel")
        files = {p.relative_to(release).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in release.rglob("*") if p.is_file()}
        (release / "RELEASE.json").write_text(json.dumps(
            {"schema": "hyperloom/release-v1", "name": release.name, "version": "0.1.0",
             "commit": "0123456789ab" * 3 + "0123", "components": {}, "files": files}), encoding="utf-8")
        local, roaming = self.directory / "local app", self.directory / "roaming app"
        env = dict(os.environ, LOCALAPPDATA=str(local), APPDATA=str(roaming))

        def run():
            return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                   str(release / "windows" / "install.ps1"), "-WhatIf"],
                                  env=env, capture_output=True, text=True, timeout=60)

        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified 3 files", result.stdout)
        self.assertIn(str(local / "hyperloom" / "venvs" / release.name), result.stdout)
        self.assertIn("-P -m hyperloom ui", result.stdout)
        # pip installs from the verified copy, wheels included, never from the extracted archive.
        line = next(x for x in result.stdout.splitlines() if x.startswith("Pip arguments: "))
        pip = json.loads(line[len("Pip arguments: "):])
        copy = local / "hyperloom" / "releases" / release.name
        self.assertEqual(pip[pip.index("--find-links") + 1], str(copy / "wheels"))
        self.assertEqual(pip[-1], str(copy))
        (release / "wheels" / "w-1-py3-none-any.whl").write_bytes(b"tampered")
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 mismatch: wheels/w-1-py3-none-any.whl", result.stdout + result.stderr)
        (release / "wheels" / "w-1-py3-none-any.whl").write_bytes(b"wheel")
        (release / "README.md").write_text("tampered\n", encoding="utf-8")
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 mismatch: README.md", result.stdout + result.stderr)
        (release / "README.md").write_text("readme\n", encoding="utf-8")
        (release / "extra.py").write_text("", encoding="utf-8")
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("File not in RELEASE.json: extra.py", result.stdout + result.stderr)
        self.assertFalse(local.exists())
        self.assertFalse(roaming.exists())


if __name__ == "__main__":
    unittest.main()
