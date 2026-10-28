"""Portable artifact data, with one path-and-hash exemption for the reviewed adversarial redaction corpus."""
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
import unicodedata
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# Main 8539c28's adversarial corpus deliberately contains synthetic private-looking strings.
# Any byte change removes the exemption and requires another privacy review.
EXEMPTIONS = {"tests/fixtures/redaction/corpus.jsonl":
              "d46b87708f5d14f344214c86563c93158e8e1e39f7b2cb098ab9259da2d8a8aa"}
PHYSICAL = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|"
                      r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]\b")
PRIVATE_KEYS = frozenset(("uuid", "pci", "pci_path", "hostname", "host_id", "user", "username"))
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9-]*>")
_PLACEHOLDER_PATH = PLACEHOLDER.pattern + r"(?:[\\/]" + PLACEHOLDER.pattern + r")*"
# Only complete paths with generic roots and placeholder-only suffixes are portable.
_PORTABLE_PATH = re.compile(r"(?:/(?:home|root|tmp)(?:/" + _PLACEHOLDER_PATH + r")?|/(?:" + _PLACEHOLDER_PATH + r")?|"
                  r"(?:[Cc]:[\\/](?:(?i:users)(?:[\\/]" + _PLACEHOLDER_PATH + r")?|" + _PLACEHOLDER_PATH + r")?)|"
                  r"(?:%\w+%|\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\}|env:[A-Za-z_]\w*)|~)(?:[\\/]" + _PLACEHOLDER_PATH + r")?)[\\/]?")
# A URI's :// introduces its authority, while the third slash in file:/// starts a path.
# Consume placeholder segments as units so their angle brackets do not become shell delimiters.
# An environment variable ($VAR, ${VAR}, $env:VAR, %VAR%) starts a path when a separator follows it; it is portable
# only when every following segment is a <placeholder>. A named home (~alice) is a path when a separator follows it
# or when it is a whole (quoted) value, so prose such as "x ~approx" is not. A drive-relative path (D:lab\model)
# needs a separator after its first segment, so "a:b" is not one; a slash after % is a ratio (50%/60%).
ABSOLUTE = re.compile(r"(?<!\w)(?:[A-Za-z]:(?=[\\/]|[\w.-]+[\\/]|[\w-]+\.[\w.-]+)|\\\\[\w.-]+\\|(?<!%)(?<!:/)/(?!/)|"
                      r"(?:%\w+%|\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\}|env:[A-Za-z_]\w*))(?=[\\/])|"
                      r"~(?=[\\/\s`\"'(),;\[\]{}<>|]|$)|~[A-Za-z_][\w.-]*(?=[\\/])|\\(?=[\w<])|"
                      r"(?:(?<![\s\S])|(?<=[`\"'=]))~[A-Za-z_][\w.-]*(?=$|[`\"']))"
                      r"(?:<[^<>\s]*>|[^\s`\"'(),;\[\]{}<>|])*")
# Public HTTP resource URLs are portable references, rather than filesystem paths; their hosts are checked below.
URI_PATH = re.compile(r"\b(?!(?i:https?)://)[a-zA-Z][a-zA-Z0-9+.-]*://(?:<[^<>\s]*>|[^/\s`\"'<>;|])+(?P<path>/"
                      r"(?:<[^<>\s]*>|[^\s`\"'(),;\[\]{}<>|])*)")
# Such a URI also names a host (smb://alice-host/share): only an empty host, localhost, a <placeholder> or a
# reserved example name (RFC 2606) is portable.
URI_AUTHORITY = re.compile(r"\b(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?:[^/\s`\"'<>;|@]*@)?"
                           r"(?P<host>\[[^\]\s]*\]|<[^<>\s]*>|[^/\\\s`\"'<>;|:]*)")
PORTABLE_HOST = re.compile(r"|(?i:localhost)|" + PLACEHOLDER.pattern +
                           r"|(?i:(?:[\w-]+\.)*(?:example(?:\.(?:com|net|org))?|invalid|test))")
# Slash and backslash look-alikes that NFKC keeps (division and fraction slash, big solidus, set minus, ...).
LOOK_ALIKES = str.maketrans({"∕": "/", "⁄": "/", "⧸": "/", "／": "/", "╱": "/",
                             "⧵": "\\", "⧹": "\\", "∖": "\\", "＼": "\\", "╲": "\\",
                             "﹨": "\\", "\u27cb": "/", "\u27cd": "\\", "\U0001f67c": "/",
                             "\U0001f67d": "\\", "\u0338": "/"})
ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)|"
                     r"(?<![\w:])(?:[0-9a-fA-F]*:){2,}[0-9a-fA-F:.]*(?:%[\w.~-]+)?(?![\w:])")
LOCAL_IDENTITY = re.compile(r"(?!" + _PORTABLE_PATH.pattern + r"(?=$|[\s`\"'(),;\]}]))(?i:[a-z]:[\\/](?:users[\\/]|tmp[\\/]|python\d+\b)|"
                            r"/(?:mnt/[a-z]/|home/[\w.-]+/)|"
                            r"\b(?:claude|codex)[a-z0-9]*(?:[-_][a-z0-9]+)*\b|\b(?:sol|opus)(?:[-_][a-z0-9]+)*\b)")


def private_paths(value):
    value = unicodedata.normalize("NFKC", value).translate(LOOK_ALIKES)
    for match in URI_AUTHORITY.finditer(value):
        host = match["host"]
        # HTTP references may use public DNS names, but single-label/LAN names, local suffixes and IPs are private.
        public_http = (match["scheme"].lower() in ("http", "https") and
                       re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}", host) and
                       not host.lower().endswith((".local", ".localhost", ".lan", ".internal", ".home", ".home.arpa",
                                                  ".localdomain", ".corp", ".intranet", ".private")))
        if not PORTABLE_HOST.fullmatch(host) and not public_http:
            yield match[0]
    for match in URI_PATH.finditer(value):
        if not _PORTABLE_PATH.fullmatch(match["path"]):
            yield match["path"]
    for match in ABSOLUTE.finditer(value):
        if re.fullmatch(r"/[A-Za-z]{1,12}", match[0]) and value[:match.start()].endswith("$"):
            continue  # Currency/unit prose such as $/hour is not a variable path.
        # A slash following a placeholder continues a relative portable fixture path.
        if match[0].startswith(("/", "\\")) and re.search(PLACEHOLDER.pattern + r"$", value[:match.start()]):
            continue
        if match[0].startswith("/") and re.search(r"(?:^|[^\w.])\.\.?$", value[:match.start()]):
            continue
        if not _PORTABLE_PATH.fullmatch(match[0]):
            yield match[0]


def addresses(value):
    for match in ADDRESS.finditer(value):
        candidate = match[0].rstrip(".")
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        yield candidate


def private_text(value):
    """Whether one string, a value or a mapping key, carries an identity, a private path or an address."""
    if (PHYSICAL.search(value) or LOCAL_IDENTITY.search(value) or
            next(private_paths(value), None) or next(addresses(value), None)):
        return True
    for variable in ("USERNAME", "COMPUTERNAME"):
        local = os.environ.get(variable, "")
        if len(local) >= 4 and local.casefold() in value.casefold():
            return True
    return False


def private(value, key=""):
    if isinstance(value, dict):
        # Keys are data too ({"/srv/alice/model": ...}): each one gets every check a string value gets.
        return any((isinstance(k, str) and private_text(k)) or (k in PRIVATE_KEYS and v not in (None, "", [], {}) and
                    not (isinstance(v, str) and PLACEHOLDER.fullmatch(v))) or private(v, k)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(private(v, key) for v in value)
    if isinstance(value, str):
        if key == "host" and value and not PLACEHOLDER.fullmatch(value):
            return True
        return private_text(value)
    return False


def artifacts():
    if (ROOT / ".git").exists():
        paths = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                               cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    else:
        # Source archives have no index: inspect every candidate file rather than skipping the privacy gate.
        # Gitlinks belong to independent repositories and are not files in the parent's index.
        submodules = {line.split("=", 1)[1].strip() for line in
                      (ROOT / ".gitmodules").read_text(encoding="utf-8").splitlines()
                      if re.match(r"\s*path\s*=", line)}
        paths = []
        for directory, dirs, files in os.walk(ROOT):
            relative = Path(directory).relative_to(ROOT)
            dirs[:] = [name for name in dirs if (relative / name).as_posix() not in submodules]
            paths.extend((relative / name).as_posix() for name in files)
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
        if EXEMPTIONS.get(name) == hashlib.sha256(raw).hexdigest():
            continue
        text = raw.decode("utf-8")
        try:
            doc = (json.loads(text) if path.suffix == ".json" else
                   [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == ".jsonl" else
                   tomllib.loads(text) if path.suffix == ".toml" else text)
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
    def test_only_reviewed_main_corpus_bytes_are_exempt(self):
        name = "tests/fixtures/redaction/corpus.jsonl"
        digest = "d46b87708f5d14f344214c86563c93158e8e1e39f7b2cb098ab9259da2d8a8aa"
        self.assertEqual(EXEMPTIONS, {name: digest})
        path = ROOT / name
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        with mock.patch(__name__ + ".artifacts", return_value=[(name, path)]), \
                mock.patch.object(Path, "glob", return_value=[]):
            self.assertEqual(findings(), {})
            with mock.patch.object(Path, "read_bytes", return_value=raw + b"\n"):
                self.assertIn(name, findings())
        # Matching bytes at a different path do not inherit the exemption either.
        with mock.patch(__name__ + ".artifacts", return_value=[("tests/fixtures/copied-corpus.jsonl", path)]), \
                mock.patch.object(Path, "glob", return_value=[]):
            self.assertIn("tests/fixtures/copied-corpus.jsonl", findings())

    def test_generic_roots_with_only_placeholder_segments_are_portable(self):
        for value in ("/", "/home", "/home/<user>", "/root", "/tmp", "/tmp/<runtime>",
                      "/<root>/<model>", "/home/<user>/<model>/", "C:\\", "C:\\Users",
                      "C:\\Users\\<user>", "C:/Users/<user>/<model>", "c:/users/<user>",
                      "%USERPROFILE%", "%USERPROFILE%/<model>", "~", "~/<model>",
                      "$HOME/<model>", "${HOME}/<model>", "$env:USERPROFILE\\<model>"):
            for text in (value, f"path '{value}',", f"``{value}``"):
                with self.subTest(text=text):
                    self.assertEqual(list(private_paths(text)), [])
                    self.assertIsNone(LOCAL_IDENTITY.search(text))
                    self.assertFalse(private(text))

    def test_download_home_examples_are_portable(self):
        from hyperloom.downloads import too_wide
        self.assertEqual(list(private_paths(too_wide.__doc__)), [])
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

    def test_each_path_after_uri_or_shell_prefix_is_checked_independently(self):
        for prefix in ("C:/Users/<user>;file://", "echo /home/<user>;cat<", "cat>", "cat|", "path:",
                       "custom+scheme:///", "="):
            with self.subTest(prefix=prefix):
                self.assertTrue(private(prefix + "/srv/lab/alice/model"))
                self.assertTrue(private(prefix + "D:/lab/model"))
        for value in ("file:///home/<user>", "cat</home/<user>", "cat>/tmp/<runtime>",
                      "custom+scheme:///<root>/<model>", "<runtime>/placement.json",
                      "https://example.invalid/project", "http://example.invalid/project", "~117 GiB"):
            with self.subTest(value=value):
                self.assertFalse(private(value))

    def test_tracked_json_injections_after_uri_and_redirection_are_found(self):
        for value in ("C:/Users/<user>;file:///srv/lab/alice/model",
                      "echo /home/<user>;cat</srv/lab/alice/model",
                      "C:/Users/<user>;file://localhost/srv/lab/alice/model",
                      "smb://host/share/model", "custom+scheme://host/srv/lab/model",
                      "\\\\server\\share\\model", "\\\\.\\pipe\\name", "~alice", "~alice/model",
                      "$HOME/alice/model", "${HOME}/alice/model", "$env:USERPROFILE\\alice",
                      "%APPDATA%\\model", "D:lab\\model", "C:\uff3clab", "\uff0fsrv", "\u2215srv",
                      "smb://alice-host/<root>", "file://alice-host/<root>", "nfs://10.0.0.7/<root>",
                      "smb://<host>/alice/model", "file://<host>/alice/model", "D:lab.model",
                      "$MODEL_DIR/model", "$MODEL_DIR\\alice\\model", "${MODEL_DIR}/alice", "%MODEL_DIR%\\alice",
                      "$USERPROFILE/alice", "$XDG_CONFIG_HOME/x", "$TMPDIR/alice",
                      *(separator + "srv" + separator + "alice" for separator in
                        map(chr, (0x2215, 0x2044, 0x29f8, 0xff0f, 0x2571, 0x29f5, 0x29f9, 0x2216,
                                  0xff3c, 0x2572, 0xfe68, 0x27cb, 0x27cd, 0x1f67c, 0x1f67d, 0x0338))),
                      "http://alice-pc:8080/x", "https://alice-pc/x", "http://alice.local/x",
                      "https://nas.lan/x", "https://nas.internal/x", "https://nas.home.arpa/x",
                      "https://nas.localdomain/x", "http://nas.corp/x", "https://nas.intranet/x", "https://nas.private/x",
                      "$/srv/alice", "x$/Users/alice/m", "costs $/srv/alice/models",
                      "$/hour/model", "$/hour\\model", "$/hour/", "$/abcdefghijklm", "$/GB2",
                      *("C:/Users/<user>;" + prefix + "-worker" for prefix in (
                          "clau" + "de", "co" + "dex", "s" + "ol", "op" + "us"))):
            with self.subTest(value=value):
                self.assertIn(self.fixture_name, self.injected_findings({"nested": [value]}))

    fixture = ROOT / "tests/fixtures/render/placement.json"
    fixture_name = fixture.relative_to(ROOT).as_posix()

    def injected_findings(self, probe):
        """findings() over the tracked fixture with ``probe`` added in memory (the file is not changed)."""
        original = Path.read_bytes
        def injected(path):
            if path == self.fixture:
                document = json.loads(original(path))
                document["privacy_probe"] = probe
                return json.dumps(document).encode("utf-8")
            return original(path)
        with mock.patch.object(Path, "read_bytes", injected), \
                mock.patch(__name__ + ".artifacts", return_value=[(self.fixture_name, self.fixture)]), \
                mock.patch.object(Path, "glob", return_value=[]):
            return findings()

    def test_tracked_json_key_injections_are_found(self):
        self.assertEqual(self.injected_findings({"<runtime>": "portable"}), {})
        for key in ("co" + "dex-worker", "/srv/alice/model", "C:\\Users\\alice", "10.0.0.7",
                    "0000:01:00.0", "${MODEL_DIR}/alice", "%MODEL_DIR%\\alice",
                    "GPU-" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, "smb://alice-host/<root>", "$MODEL_DIR/alice"):
            for probe in ({key: "portable"}, {"nested": [{key: "portable"}]}):
                with self.subTest(probe=probe):
                    self.assertIn(self.fixture_name, self.injected_findings(probe))

    def test_portable_variables_hosts_and_prose_are_not_paths(self):
        for value in ("$MODEL_DIR/<model>", "${MODEL_DIR}\\<model>", "%MODEL_DIR%\\<model>", "$env:LOCALAPPDATA\\<runtime>",
                      "smb://<host>/<root>", "file://localhost/<root>", "file:///<root>", "nfs://nas.example/<root>",
                      "a:b", "ratio a:b of c", "x ~approx 3", "about ~approx", "50%/60%", "load 50%/60% of 100%",
                      "costs $5/hour", "$/hour", "costs $/hour", "$/month", "$/GB", "$/abcdefghijkl",
                      "https://github.com/project/repo", "\u29f8"):
            with self.subTest(value=value):
                self.assertFalse(private(value))
                self.assertEqual(self.injected_findings({"nested": [value]}), {})
                self.assertEqual(self.injected_findings({value: "portable"}), {})
        for value in ("~alice", "'~alice'", "home=~alice", "~alice/model"):
            with self.subTest(value=value):
                self.assertTrue(private(value))

    def test_strict_rule_catches_identity_paths_and_host_user_fields(self):
        import uuid
        for value in ({"uuid": "GPU-" + str(uuid.uuid4())}, {"pci": f"{0:04x}:{1:02x}:{0:02x}.{0}"},
                      {"host": "synthetic-host"}, {"username": "operator"},
                      str(ROOT / "local-runtime"), {"model": "/private/model/file"}):
            with self.subTest(kind=type(value).__name__):
                self.assertTrue(private(value))
        self.assertFalse(private({"device_name": "portable class", "arch": "sm_test", "samples": [{"bytes": 42}],
                                  "file": "<runtime>/placement.json", "host_pinned_gib": 74}))
