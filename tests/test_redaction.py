"""One set of redaction rules (hyperloom/redaction.py): the node's log search keeps a verbatim copy (it is deployed
as standalone files); one case table runs against every implementation, and redaction time stays linear."""

import importlib.util
import json
from pathlib import Path
import re
import time
import unittest

from hyperloom import redaction
from hyperloom.web.api import redact

ROOT = Path(__file__).resolve().parent.parent
NODE_FILE = ROOT / "node" / "hyperloom_logsearch.py"
spec = importlib.util.spec_from_file_location("hyperloom_logsearch_redaction_under_test", NODE_FILE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)

HF = "hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567"
# A line and the secret(s) that must not survive in it.
SECRETS = {
    # names and option forms
    "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENG": "wJalrXUtnFEMIK7MDENG",
    "login with password hunter3 now": "hunter3",
    '{"db.password": "s3cr3t-value"}': "s3cr3t-value",
    "db.password: 123456": "123456",
    "api_key=12345": "12345",
    "run --hf-token tk_123456 --other": "tk_123456",
    "run --db-password=Opt10nPw": "Opt10nPw",
    "x--api-key Gl8edOpt": "Gl8edOpt",
    "GITHUB_TOKEN: ghp_abcdef123": "ghp_abcdef123",
    "X-Auth-Token: abcdefghij": "abcdefghij",
    "client_secret='quoted value'": "quoted value",
    "HF_TOKEN=hf_xYz123": "hf_xYz123",
    "HF_TOKEN hf_xYz123": "hf_xYz123",
    "export DB_PASSWORD  s3cr3tval": "s3cr3tval",
    "hf_token\thf_lowercase9": "hf_lowercase9",
    "PASS=x9secret": "x9secret",
    "db_pass: plainpw7": "plainpw7",
    "max_tokens=notanumber": "notanumber",
    "MAX_TOKENS 16,leak3d": "leak3d",
    "--max-tokens 16;leak4d": "leak4d",
    "HF_TOKEN 1000:1000secret": "1000secret",
    # authorization schemes
    "Authorization: Basic dXNlcjpwYXNz": "dXNlcjpwYXNz",
    "authorization: basic lowercaseonly": "lowercaseonly",
    "Authorization: Bearer abc.def.ghi": "abc.def.ghi",
    "sent Bearer abc!def": "abc!def",
    "using Token ghp_abc123xyz": "ghp_abc123xyz",
    # URL user information
    "fetch https://user:pa55word@example.invalid/x": "pa55word",
    # clients that take a password after -p
    "mysql -u root -pXyzzy42 shop": "Xyzzy42",
    "mysqldump -h db -pAnother7 --all": "Another7",
    "mariadb-dump -pMaria9 db": "Maria9",
    "sshpass -p Sp4ce sshx": "Sp4ce",
    "sshpass -pGlu3d ssh host": "Glu3d",
    # bare Hugging Face tokens
    "download with " + HF + " ok": HF,
    "token is hf_abcdefghijklmnopqrstuvwxyzabcdefgh": "hf_abcdefghijklmnopqrstuvwxyzabcdefgh",
    # curl's user options: every form, every occurrence, wherever they are in the command
    "curl -u alice:Wonder7land https://example.invalid/x": "Wonder7land",
    "curl -sSu bob:Pa55-w0rd -o out.json https://example.invalid": "Pa55-w0rd",
    "curl -su bob:Pa55-w1rd https://example.invalid": "Pa55-w1rd",
    "curl --user carol:hunter4 https://example.invalid": "hunter4",
    "curl --user=dave:Sw0rdfish https://example.invalid": "Sw0rdfish",
    "curl -ueve:Gl0bal https://example.invalid": "Gl0bal",
    "curl -x http://proxy.invalid -U frank:Pr0xyPw https://example.invalid": "Pr0xyPw",
    "curl --proxy-user gil:Pr0xy2 https://example.invalid": "Pr0xy2",
    "curl -u 'gina:two words' https://example.invalid": "two words",
    "sudo curl -u hal:Sud0pw https://example.invalid": "Sud0pw",
    "sudo -H -u root /usr/bin/curl -u ida:Wr4pped https://example.invalid": "Wr4pped",
    "wget --user hank:C0mbined https://example.invalid": "C0mbined",
    "curl -H 'Cookie: a=1; b=2' -u ivy:C00kieJar https://example.invalid": "C00kieJar",
    'curl -H "Accept: a|b" -u jo:P1peLine https://example.invalid': "P1peLine",
    "curl -u alice:FirstSecret -U bob:SecondSecret https://example.invalid": ("FirstSecret", "SecondSecret"),
    "curl -u kim:N3xtOne https://a.invalid --next -u lee:N3xtTwo https://b.invalid": ("N3xtOne", "N3xtTwo"),
    "curl https://example.invalid/" + "a" * 5000 + " -u max:L0ngPrefix": "L0ngPrefix",
    "curl https://example.invalid/?a=1&b=2 -u ned:Amp3rsand": "Amp3rsand",
    "curl -u olga:Semi;colon https://example.invalid": ("Semi", "colon"),
    "curl https://example.invalid \\\n  -u pat:C0ntinued": "C0ntinued",
    "curl's -u quinn:Ap0strophe": "Ap0strophe",
    "curl -#u wes:H4sh https://example.invalid": "H4sh",
    # shell words: escapes inside double quotes, concatenated quoted and unquoted parts
    'curl -H "X: \\"a;b\\"" -u xia:SuperSecret https://example.invalid': "SuperSecret",
    "curl -H a\\;b -u yan:Esc4ped https://example.invalid": "Esc4ped",
    "curl -u'zoe:foo'bar https://example.invalid": ("foo", "bar"),
    'curl -u "amy:in\\"side" https://example.invalid': "side",
    # rules combined on one line: a command's options are read before a name rule can take one as a value
    "curl -u alice:password -U bob:SecondSecret https://example.invalid": ("alice:password", "SecondSecret"),
    "curl --user ben:passwd --proxy-user cat:Pr0xyThird https://example.invalid": ("ben:passwd", "Pr0xyThird"),
    "export DB_PASSWORD=Fir5t && curl -u dan:Sec0nd -U eli:Th1rd https://example.invalid": ("Fir5t", "Sec0nd", "Th1rd"),
    "mysql -u root -ppassword -e 'select 1' && curl --user fay:Mix3d https://example.invalid": ("-ppassword", "Mix3d"),
    "HF_TOKEN " + HF + " curl -u gus:Aft3r https://example.invalid": (HF, "Aft3r"),
    "sshpass -p token ssh host; curl -u hal:Tok3nPw https://example.invalid": ("-p token", "Tok3nPw"),
    "command -v curl >/dev/null&&curl -u rae:Gl0edAnd https://example.invalid": "Gl0edAnd",
    "type curl;curl -u sam:Gl0edSemi https://example.invalid": "Gl0edSemi",
    "curl -sS https://a.invalid|curl -u tim:Gl0edPipe -T - https://b.invalid": "Gl0edPipe",
    "curl -u uma:F1rst;curl -u vic:S3cond https://example.invalid": ("F1rst", "S3cond"),
    # glued and quoted passwords, empty URL user names
    "mysql -h db -P3306 -uroot -pP0rtFirst shop": "P0rtFirst",
    "mysqldump -P3307 -pDump3d --all-databases": "Dump3d",
    "mysql -p'hunter 2' shop": "hunter 2",
    "sshpass -P assword: -p Pr0mpt ssh host": "Pr0mpt",
    "sshpass -e ssh host; sshpass -p S3condCmd ssh host": "S3condCmd",
    'app --password "correct horse battery staple"': "horse battery staple",
    "PASSWORD 'two words'": "two words",
    "redis://:s3cretpw@cache:6379/0": "s3cretpw",
}
# Text that must stay exactly as it is.
READABLE = (
    "generated 512 tokens in 3.1 s", "keyboard input ignored", "pass 2 of 3 finished", "mysql connection pool size 8",
    "hf_transfer enabled, hf_hub_download retried, hf_qwen3moe loaded", "HF_HUB_ENABLE_HF_TRANSFER=1",
    "docker run --user 1000:1000 img", "curl -sS https://example.invalid | python3 -u -",
    "systemd-run --user --scope -p MemoryMax=16G", "curl -sS https://example.invalid/health",
    "python -u serve.py --port 8000",
    "curl -sS https://example.invalid && python -u serve.py",
    "curl -sS https://example.invalid || python -u serve.py",
    "curl -sS https://example.invalid && systemd-run --user --scope true",
    "curl -sS https://example.invalid & docker run --user 1000:1000 img",
    "curl -sS https://example.invalid; docker run --user 1000:1000 img",
    "max_tokens=512", "max_tokens 16", "MAX_TOKENS 5", "--max-num-batched-tokens 8192", "--prompt-tokens 9000",
    "SamplingParams(n=1, max_tokens=512, temperature=0.7)", "token: 151643", "token-identical: 2/3 (first pass)",
    "budget bound violated by the endpoint: max_tokens 16, reported 21 completion tokens",
    "--max-tokens=512, --max-num-batched-tokens 8192;",
    "Token counters are reset", "needs imported token counts", "basic setup done",
    "--tokenizer /models/qwen", "tokenizer=/models/qwen",
    "curl -XPUT https://example.invalid", "curl -sSL -X PUT https://example.invalid",
    "sshpass -e ssh -p 2222 host", "sshpass -f pwfile ssh -p 2222 host", "mysql -h db -P3306 shop",
    "tokenizer.ggml.bos_token_id u32 = 151643", "key_cache shape [32, 8192]", "token_ids: [1, 2, 3]",
    "pad_token: <|endoftext|>", "attn_mask_key=None", "SamplingParams(token=None)", "the token 12345678 was sampled",
    "--hf-token 123456789012",
    "systemd-run --user --setenv=PYTHONPATH=/foo:/bar python3 -u app.py",
    "systemd-run --user --property=MemoryMax=16G --setenv=A=b:c true",
)
IMPLEMENTATIONS = {
    "shared": redaction.redact_text,
    "node": node.redact,
    "portal text": redact,
    "portal document": lambda line: redact({"stderr": line, "lines": [line]})["lines"][0],
}


class SharedRulesTests(unittest.TestCase):
    def test_the_node_copy_is_identical(self):
        marker, end = "# --- shared redaction rules", "# --- end of shared redaction rules ---\n"

        def block(path):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(marker), 1, path)
            return text[text.index(marker):text.index(end) + len(end)]
        self.assertEqual(block(NODE_FILE), block(ROOT / "hyperloom" / "redaction.py"))
        # The compiled rules too: pattern source and flags (each names the value it hides: group v), and the code.
        compiled = lambda p: (p.pattern, p.flags, tuple(p.groupindex))
        self.assertEqual([compiled(p) for p in node.SECRET_PATTERNS], [compiled(p) for p in redaction.SECRET_PATTERNS])
        self.assertEqual([tuple(map(compiled, rule)) for rule in node.GLUED_PASSWORD],
                         [tuple(map(compiled, rule)) for rule in redaction.GLUED_PASSWORD])
        for name in ("CURL_COMMAND", "CURL_OPTION"):
            self.assertEqual(compiled(getattr(node, name)), compiled(getattr(redaction, name)), name)
        for name in ("_glued_password", "_curl_users", "redact_text"):
            self.assertEqual(getattr(node, name).__code__.co_code, getattr(redaction, name).__code__.co_code, name)
        # The node tool stays self-contained: it never imports the hyperloom package.
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+hyperloom\b", NODE_FILE.read_text(encoding="utf-8"), re.M))

    def test_every_implementation_masks_the_secrets_and_keeps_the_text(self):
        for name, implementation in IMPLEMENTATIONS.items():
            for line, secrets in SECRETS.items():
                with self.subTest(implementation=name, line=line[:80]):
                    result = implementation(line)
                    self.assertIn("[redacted]", result)
                    for secret in (secrets,) if isinstance(secrets, str) else secrets:
                        self.assertNotIn(secret, result)
            for text in READABLE:
                with self.subTest(implementation=name, text=text):
                    self.assertEqual(implementation(text), text)

    def test_what_stays_around_a_redacted_value(self):
        for name, implementation in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertEqual(implementation("curl -u a:b -U c:d https://x && python -u serve.py"),
                                 "curl -u [redacted] -U [redacted] https://x && python -u serve.py")
                self.assertEqual(implementation("mysql -u root -pXyz shop"), "mysql -u root -p[redacted] shop")
                self.assertEqual(implementation("export HF_TOKEN=" + HF), "export HF_TOKEN=[redacted]")
                self.assertEqual(implementation("Authorization: Bearer abc.def"), "Authorization: Bearer [redacted]")

    def test_argument_lists(self):
        cases = [
            (["sudo", "-H", "-u", "root", "/usr/bin/curl", "-u", "alice:Secret", "https://x"],
             ["sudo", "-H", "-u", "root", "/usr/bin/curl", "-u", "[redacted]", "https://x"]),
            (["curl", "-ubruce:Wonder7land", "-sSUbob:Pr0xy", "https://x", "--next", "--user", "c:d", "https://y"],
             ["curl", "-u[redacted]", "-sSU[redacted]", "https://x", "--next", "--user", "[redacted]", "https://y"]),
            (["curl", "-sS", "https://x", "&&", "python3", "-u", "serve.py", "a:b"],
             ["curl", "-sS", "https://x", "&&", "python3", "-u", "serve.py", "a:b"]),
            (["docker", "run", "--user", "1000:1000", "--user", "kim:pw", "img"],
             ["docker", "run", "--user", "1000:1000", "--user", "[redacted]", "img"]),
            (["systemd-run", "--user", "--scope", "true"], ["systemd-run", "--user", "--scope", "true"]),
            (["mysql", "-uroot", "-pXyzzy42", "shop"], ["mysql", "-uroot", "-p[redacted]", "shop"]),
            (["sshpass", "-p", "Sp4ce", "ssh", "-p", "2222", "host"],
             ["sshpass", "-p", "[redacted]", "ssh", "-p", "2222", "host"]),
            (["vllm", "serve", "--max-num-batched-tokens", "8192", "--tokenizer", "/m", "--hf-token", "tk_1x",
              "--api-key", "k", "--password", "p"],
             ["vllm", "serve", "--max-num-batched-tokens", "8192", "--tokenizer", "/m", "--hf-token", "[redacted]",
              "--api-key", "[redacted]", "--password", "[redacted]"]),
            (["sshpass", "-f", "pwfile", "ssh", "-p", "2222", "host"],
             ["sshpass", "-f", "pwfile", "ssh", "-p", "2222", "host"]),
            (["sshpass", "-P", "assword:", "-p", "Pr0mpt", "ssh", "-p", "22"],
             ["sshpass", "-P", "assword:", "-p", "[redacted]", "ssh", "-p", "22"]),
            (["curl", "-XPUT", "-u", "a:b", "https://x"], ["curl", "-XPUT", "-u", "[redacted]", "https://x"]),
            (["mysql", "-P3306", "-pXyz", "db"], ["mysql", "-P3306", "-p[redacted]", "db"]),
            (["systemd-run", "--user", "--setenv=PYTHONPATH=/foo:/bar", "python3", "-u", "app.py"],
             ["systemd-run", "--user", "--setenv=PYTHONPATH=/foo:/bar", "python3", "-u", "app.py"]),
            (["C:\\tools\\curl.exe", "-#u", "a:b", "-Zuc:d"],
             ["C:\\tools\\curl.exe", "-#u", "[redacted]", "-Zu[redacted]"]),
            (["C:\\mysql\\bin\\mysql.exe", "-pS3"], ["C:\\mysql\\bin\\mysql.exe", "-p[redacted]"]),
            (["vllm", "--hf-token", "123456789012", "--max-tokens", "<|endoftext|>"],
             ["vllm", "--hf-token", "123456789012", "--max-tokens", "<|endoftext|>"]),
            (["env", "HF_HUB_ENABLE_HF_TRANSFER=1", "HF_TOKEN=" + HF, HF],
             ["env", "HF_HUB_ENABLE_HF_TRANSFER=1", "HF_TOKEN=[redacted]", "[redacted]"]),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(redaction.redact_argv(argv), expected)
                self.assertEqual(redact(argv), expected)
                self.assertEqual(redact({"argv": argv})["argv"], expected)
        self.assertEqual(redaction.redact_argv([1, None, "x"]), [1, None, "x"])       # non-text items kept
        self.assertEqual(redact("curl -u x:y " + HF, text=False), "curl -u x:y " + HF)     # documents' prose

    def test_redaction_time_grows_linearly(self):
        # Lines built to make a backtracking rule re-scan: long runs of name characters with and without a
        # credential word, repeated separators, options, clients, quotes and URL schemes.
        units = ("a", "=", "token", "keyw", "TOKEN_", "pass_", "--key", "--", "a.", "a://", "curl ", "curl -",
                 "curl '", "curl -u ", "'x", '"', "mysql ", "sshpass ", "--user=", "--user=x", " -p", "Token ",
                 "Bearer ", "hf_", "\\\n", "x--key", "key=", "A_", "_", "sshpass -f '", "sshpass -d ",
                 "sshpass\n -f ", 'sshpass -P "', "sshpass -e ", "mysql -P3 ", "a_b_", "pad_token: <", "redis://:",
                 '"\\"', "'\\'\"", '\\"', 'curl "\\', "x\\ ", "curl -u'a'", "password -U ")
        for name, implementation in (("shared", redaction.redact_text), ("node", node.redact)):
            for unit in units:
                self.assert_linear(name, unit, lambda size: implementation((unit * (size // len(unit) + 1))[:size]))
        # Argument lists: every item of a portal list goes through them; long items whose suffix fails a pattern.
        for name, implementation in (("shared", redaction.redact_argv), ("portal", redact)):
            for unit in ("--key", "--token", "-u", "-s", "/mysql", "mariadb-", "--", "a:", "-p"):
                def run(size, unit=unit):
                    item = (unit * (size // len(unit) + 1))[:size] + "!"
                    implementation(["curl", item, "-u", item, "--user", item, "sshpass", "-p", item, "mysql", item])
                self.assert_linear(name, unit, run)

    def assert_linear(self, name, unit, run):
        timings = []
        for size in (8000, 32000):
            best = float("inf")
            for _ in range(3):
                started = time.perf_counter()
                run(size)
                best = min(best, time.perf_counter() - started)
            timings.append(best)
        with self.subTest(implementation=name, unit=unit, timings=timings):
            # 4x the input: well under 8x the time (a quadratic rule takes 16x), above a noise floor.
            self.assertLess(timings[1], max(8 * timings[0], 0.25))
            self.assertLess(timings[1], 2.0)


if __name__ == "__main__":
    unittest.main()
