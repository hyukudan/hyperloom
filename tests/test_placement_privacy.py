"""Every tracked recipe/profile/receipt/fixture passes the same portable-data rule, without exceptions."""
import ast
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PHYSICAL = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|"
                      r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]\b")
PRIVATE_KEYS = frozenset(("uuid", "pci", "pci_path", "hostname", "host_id", "user", "username"))
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9-]*>")
_PLACEHOLDER_PATH = PLACEHOLDER.pattern + r"(?:[\\/]" + PLACEHOLDER.pattern + r")*"
# Only complete paths with generic roots and placeholder-only suffixes are portable.
_PORTABLE_PATH = (r"(?:/(?:home|root|tmp)(?:/" + _PLACEHOLDER_PATH + r")?|/(?:" + _PLACEHOLDER_PATH + r")?|"
                  r"(?:[Cc]:[\\/](?:(?i:users)(?:[\\/]" + _PLACEHOLDER_PATH + r")?|" + _PLACEHOLDER_PATH + r")?)|"
                  r"(?:(?i:%USERPROFILE%)|~)(?:[\\/]" + _PLACEHOLDER_PATH + r")?)[\\/]?"
                  r"(?=$|[\s`\"'(),;\]}])")
ABSOLUTE = re.compile(r"(?:^|[\s\"'=,;()\[\]{}`])(?!" + _PORTABLE_PATH + r")(?:[A-Za-z]:[\\/]|/(?!/)[\w.-]+/)")
ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)|"
                     r"(?<![\w:])(?:[0-9a-fA-F]*:){2,}[0-9a-fA-F:.]*(?:%[\w.~-]+)?(?![\w:])")
LOCAL_IDENTITY = re.compile(r"(?!" + _PORTABLE_PATH + r")(?i:[a-z]:[\\/](?:users[\\/]|tmp[\\/]|python\d+\b)|"
                            r"/(?:mnt/[a-z]/|home/[\w.-]+/)|"
                            r"\b(?:claude|codex)[a-z0-9]*(?:[-_][a-z0-9]+)*\b|\b(?:sol|opus)(?:[-_][a-z0-9]+)*\b)")


def addresses(value):
    for match in ADDRESS.finditer(value):
        candidate = match[0].rstrip(".")
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        yield candidate


def private(value, key=""):
    if isinstance(value, dict):
        return any(next(addresses(k), None) or (k in PRIVATE_KEYS and v not in (None, "", [], {}) and
                    not (isinstance(v, str) and PLACEHOLDER.fullmatch(v))) or private(v, k)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(private(v, key) for v in value)
    if isinstance(value, str):
        if key == "host" and value and not PLACEHOLDER.fullmatch(value):
            return True
        if PHYSICAL.search(value) or ABSOLUTE.search(value) or next(addresses(value), None):
            return True
        for variable in ("USERNAME", "COMPUTERNAME"):
            local = os.environ.get(variable, "")
            if len(local) >= 4 and local.casefold() in value.casefold():
                return True
    return False


def artifacts():
    paths = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                           cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    for name in sorted(set(paths)):
        path = Path(name)
        if ((ROOT / path).is_file() and path.suffix.lower() in (".json", ".toml", ".csv", ".jsonl", ".log", ".txt")
                and (name.startswith("tests/fixtures/") or
                     any(part in ("recipes", "profiles", "receipts", "run") for part in path.parts))):
            yield name, ROOT / path


def findings():
    found = {}
    for name, path in artifacts():
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        try:
            doc = json.loads(text) if path.suffix == ".json" else tomllib.loads(text) if path.suffix == ".toml" else text
        except ValueError:
            doc = text
        if private(doc):
            found[name] = hashlib.sha256(raw).hexdigest()
    for path in [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("tests/**/*.md"),
                 *ROOT.glob("hyperloom/**/*.py"), *ROOT.glob("hyperloom/**/*.json"),
                 *ROOT.glob("hyperloom/recipes/examples/**/*.toml")]:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        values = [text] if path.suffix != ".py" else [node.value for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        if any(LOCAL_IDENTITY.search(value) for value in values):
            found[path.relative_to(ROOT).as_posix()] = hashlib.sha256(raw).hexdigest()
    return found


class PrivacyTests(unittest.TestCase):
    def test_generic_roots_with_only_placeholder_segments_are_portable(self):
        for value in ("/", "/home", "/home/<user>", "/root", "/tmp", "/tmp/<runtime>",
                      "/<root>/<model>", "/home/<user>/<model>/", "C:\\", "C:\\Users",
                      "C:\\Users\\<user>", "C:/Users/<user>/<model>", "c:/users/<user>",
                      "%USERPROFILE%", "%USERPROFILE%/<model>", "~", "~/<model>"):
            for text in (value, f"path '{value}',", f"``{value}``"):
                with self.subTest(text=text):
                    self.assertIsNone(ABSOLUTE.search(text))
                    self.assertIsNone(LOCAL_IDENTITY.search(text))
                    self.assertFalse(private(text))

    def test_download_home_examples_are_portable(self):
        from hyperloom.downloads import too_wide
        self.assertIsNone(ABSOLUTE.search(too_wide.__doc__))
        self.assertIsNone(LOCAL_IDENTITY.search(too_wide.__doc__))
        self.assertFalse(private(too_wide.__doc__))

    def test_portable_path_rule_does_not_hide_real_paths_or_identities(self):
        for value in ("/home/alice/x", "C:\\Users\\bob", "D:/tmp/x", "/mnt/d/tmp",
                      "/tmp/alice", "/root/alice", "/home/<user>/model", "C:/Users/<user>/model",
                      "C:/<user>/model", "D:/<user>", "/home/<User>/x", "/home/<user>.json",
                      "/home/<user>-alice", "/home/<user>!alice", "/home/<user>?alice",
                      "C:/<user>;D:/models/qwen", "PATH=C:\\Users\\<user>;D:\\lab\\bin",
                      "(/home/<user>)(/srv/alice/m)", "`/tmp/<runtime>`/srv/alice/x", "C:/;D:/hf/models",
                      "C:/Users/<Alice>", "C:\\Users\\<BOB>"):
            with self.subTest(value=value):
                self.assertTrue(private(value))
                self.assertTrue(private(f"'/home/<user>', '{value}'"))
        for value in ("/home/alice/x", "C:\\Users\\bob", "D:/tmp/x", "/mnt/d/tmp",
                      "claude-hl-5", "codex-red", "sol-worker", "opus-worker"):
            with self.subTest(value=value):
                self.assertIsNotNone(LOCAL_IDENTITY.search(value))
                self.assertIsNotNone(LOCAL_IDENTITY.search(f"C:/Users/<user>, {value}"))

    def test_tracked_artifacts_add_no_identity_or_path_bearing_data(self):
        self.assertEqual(findings(), {}, "artifact carries UUID, PCI, IP, host, user or absolute path; use portable placeholders")

    def test_strict_rule_catches_identity_paths_and_host_user_fields(self):
        import uuid
        for value in ({"uuid": "GPU-" + str(uuid.uuid4())}, {"pci": f"{0:04x}:{1:02x}:{0:02x}.{0}"},
                      {"host": "synthetic-host"}, {"username": "operator"},
                      str(ROOT / "local-runtime"), {"model": "/private/model/file"}):
            with self.subTest(kind=type(value).__name__):
                self.assertTrue(private(value))
        self.assertFalse(private({"device_name": "portable class", "arch": "sm_test", "samples": [{"bytes": 42}],
                                  "file": "<runtime>/placement.json", "host_pinned_gib": 74}))
