"""hyperloom bench CLI: the same arguments as the portal, command-line previews, a run with its own hold, history,
share, receipt export and verification."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))

import support  # noqa: E402

from hyperloom import admission, cli  # noqa: E402
from hyperloom.bench import commands  # noqa: E402
from hyperloom.cliext import bench as bench_cli  # noqa: E402


def parse(argv):
    parser = argparse.ArgumentParser(prog="hyperloom")
    sub = parser.add_subparsers(dest="command")
    bench_cli.add(sub)
    return parser.parse_args(argv)


class FormsTests(unittest.TestCase):
    def test_arguments_and_command_line_round_trip(self):
        cases = {
            "decode": ["--profile", "qwen", "--levels", "1,2,8", "--max-tokens", "64", "--prompt-set", "code", "--trace"],
            "prefill": ["--target", "fake", "--sizes", "1024,4096", "--trials", "3", "--max-context", "32768"],
            "ab": ["--a-profile", "qwen", "--b-target", "fake", "--b-tokenizer", "tok-1", "--cases", "4", "--seed", "9"],
            "quality": ["--profile", "qwen", "--categories", "qa,recall", "--concurrency", "2"],
            "tools": ["--target", "fake", "--preset", "hard", "--repeats", "3", "--temperature", "0.5", "--sample-seed", "4"],
            "demo": ["--profile", "qwen", "--text", "one prompt", "--text", "another prompt", "--max-tokens", "100"],
        }
        for kind, argv in cases.items():
            with self.subTest(kind=kind):
                arguments = commands.arguments_from(kind, parse(["bench", kind, *argv]))
                line = commands.command_line(kind, arguments)
                again = commands.arguments_from(kind, parse(["bench", kind, *__import__("shlex").split(line)[3:]]))
                self.assertEqual(again, arguments)
        decoded = commands.arguments_from("quality", parse(["bench", "quality", "--profile", "q", "--categories", "qa,recall"]))
        self.assertEqual(decoded["categories"], ["short QA", "long-context recall"])
        tools = commands.arguments_from("tools", parse(["bench", "tools", "--target", "fake", "--temperature", "0.5"]))
        self.assertEqual(tools["sampling"], {"temperature": 0.5})
        with self.assertRaises(ValueError):
            commands.arguments_from("quality", parse(["bench", "quality", "--profile", "q", "--categories", "poetry"]))


class CLIRunTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.0005)
        self.db = admission.Admission(":memory:")
        self.addCleanup(self.db.close)
        for patcher in (mock.patch.object(bench_cli, "_api", lambda: self.bench.api),
                        mock.patch.object(admission, "for_config", lambda path: self.db)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_run_history_share_receipt_verify(self):
        code, out, _ = self.main(["bench", "decode", "--profile", "qwen", "--levels", "1,2", "--max-tokens", "16", "--quiet"])
        self.assertEqual(code, 0, out)
        run_id = json.loads(out)["run_id"]
        self.assertEqual(self.db.holds(), [])
        code, out, _ = self.main(["bench", "runs", "list", "--kind", "decode"])
        self.assertEqual([r["id"] for r in json.loads(out)], [run_id])
        code, out, _ = self.main(["bench", "share", run_id])
        self.assertIn("hyperloom bench · decode", out)
        self.assertNotIn("127.0.0.1", out)
        code, out, err = self.main(["bench", "ab", "--a-profile", "qwen", "--b-target", "fake", "--cases", "2", "--max-tokens", "8", "--quiet"])
        self.assertEqual(code, 0, out + err)
        ab_id = json.loads(out)["run_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "receipt.json")
            self.assertEqual(self.main(["bench", "receipt", ab_id, "--out", path])[0], 0)
            code, out, _ = self.main(["bench", "verify-receipt", path])
            self.assertEqual(code, 0, out)
            self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(self.main(["bench", "receipt", run_id, "--out", "x.json"])[0], 2)
        code, out, _ = self.main(["bench", "compare", run_id, ab_id])
        self.assertEqual(code, 2)
        self.assertEqual(self.main(["bench", "runs", "clear"])[0], 2)
        code, out, _ = self.main(["bench", "runs", "clear", "--kind", "decode", "--yes"])
        self.assertEqual(json.loads(out), {"removed": 1})

    def test_refusals_have_their_own_exit_codes(self):
        hold = self.db.acquire("core", {"halo-1"}, label="up qwen")
        code, _, err = self.main(["bench", "decode", "--profile", "qwen", "--levels", "1", "--quiet"])
        self.assertEqual(code, 3)
        self.assertIn("deploy, start or install is running", err)
        self.db.release(hold)
        code, _, err = self.main(["bench", "decode", "--profile", "qwen", "--levels", "0"])
        self.assertEqual(code, 2)
        code, _, err = self.main(["bench", "decode", "--profile", "other"])
        self.assertEqual(code, 3)
        self.assertIn("start it first", err)

    def test_targets_commands(self):
        code, out, _ = self.main(["bench", "targets", "add", "lab-box", "--scheme", "https", "--host", "halo-2", "--port", "8443",
                                  "--machines", "halo-2", "--sampling"])
        self.assertEqual(code, 0, out)
        code, out, _ = self.main(["bench", "targets", "list"])
        rows = {t["key"]: t for t in json.loads(out)["targets"]}
        self.assertEqual(rows["target:lab-box"]["machines"], ["halo-2"])
        self.assertTrue(rows["target:lab-box"]["sampling"])
        self.assertEqual(self.main(["bench", "targets", "capabilities", "qwen", "--thinking", "on"])[0], 0)
        self.assertEqual(self.main(["bench", "targets", "remove", "lab-box"])[0], 0)
        self.assertEqual(self.main(["bench", "targets", "remove", "lab-box"])[0], 2)
        code, out, _ = self.main(["bench", "guard", "--target", "profile:qwen"])
        self.assertTrue(json.loads(out)["preview"]["admitted"])


if __name__ == "__main__":
    unittest.main()
