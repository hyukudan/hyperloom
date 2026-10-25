"""Logs search (F37): the bounded `logs-search` node verb (roots, links, budgets, context, redaction, regex
refusals and the worker deadline) and the portal's query/roots endpoints."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from hyperloom.web.features import ops
from tests.test_ops_turns import APIFixture

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hyperloom_logsearch_under_test", ROOT / "node" / "hyperloom_logsearch.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class Tree:
    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "work"
        self.base.mkdir()
        self.root = self.base / "runs"
        (self.root / "a").mkdir(parents=True)
        (self.root / "a" / "run.log").write_text("".join(f"line {i} value\n" for i in range(1, 201)))
        (self.root / "b.txt").write_text("alpha\nERROR beta Bearer abc.def api_key=hunter2\ngamma\n")
        (self.root / "image.log").write_bytes(b"\0\x01binary ERROR")
        (self.root / "keys").mkdir()
        (self.root / "keys" / "x.log").write_text("ERROR inside a credential directory\n")
        (self.root / "secret-notes.log").write_text("ERROR sensitive file name\n")


class NodeSearchTests(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self)

    def run_search(self, **payload):
        payload.setdefault("roots", [str(self.tree.root)])
        return node.run("logs-search", payload)

    def test_literal_context_and_redaction(self):
        result = self.run_search(text="ERROR", context=1)
        files = sorted(r["file"] for r in result["results"])
        self.assertEqual(files, ["b.txt"])           # binary, credential directory and secret-named files are skipped
        hit = result["results"][0]
        self.assertEqual((hit["before"], hit["after"]), (["alpha"], ["gamma"]))
        self.assertNotIn("hunter2", json.dumps(result))
        self.assertNotIn("abc.def", json.dumps(result))
        self.assertEqual(result["report"]["skipped_binary"], 1)

    def test_case_level_process_and_limits(self):
        self.assertEqual(len(self.run_search(text="LINE 1", ignore_case=True, max_results=5)["results"]), 5)
        self.assertEqual(self.run_search(text="LINE 1")["results"], [])
        only = self.run_search(text="e", level="error")
        self.assertEqual({r["file"] for r in only["results"]}, {"b.txt"})
        self.assertEqual({r["file"] for r in self.run_search(text="line", process="run")["results"]}, {"a/run.log"})
        capped = self.run_search(text="line", max_results=3)
        self.assertIn("results", capped["report"]["truncated"])
        tiny = self.run_search(text="line", max_bytes=100)
        self.assertIn("bytes", tiny["report"]["truncated"])

    def test_time_window_uses_mtime(self):
        old = time.time() - 10 * 86400
        os.utime(self.tree.root / "b.txt", (old, old))
        self.assertEqual(self.run_search(text="alpha", since=time.time() - 86400)["results"], [])
        self.assertEqual(len(self.run_search(text="alpha")["results"]), 1)

    def test_long_lines_are_cut_and_consumed(self):
        (self.tree.root / "long.log").write_text("x" * 50000 + " needle-late\nneedle-next\n")
        result = self.run_search(text="needle")
        self.assertEqual([r["text"] for r in result["results"]], ["needle-next"])   # matched per 4 KiB line prefix

    def test_refused_payloads_and_roots(self):
        for payload in ({"text": "a", "regex": "a"}, {"text": ""}, {"text": "a", "unknown": 1}, {"text": "a", "level": "x"},
                        {"text": "a", "deadline_s": 60}, {"text": "a", "roots": "x"}, {"text": "a" * 200}):
            payload.setdefault("roots", [str(self.tree.root)])
            self.assertIn("error", node.run("logs-search", payload), payload)
        for root in ("relative/dir", str(Path(self.tree.root).anchor), str(Path.home())):
            report = self.run_search(text="a", roots=[root])["report"]
            self.assertTrue(report["roots"][0]["error"], root)
        report = self.run_search(text="a", roots=[str(self.tree.root / "keys")])["report"]
        self.assertIn("credential", report["roots"][0]["error"])

    def test_protected_descendants_are_pruned(self):
        install = self.tree.base / "install"
        (install / "etc").mkdir(parents=True)
        (install / "log").mkdir()
        (install / ".hyperloom-root").write_text("x\n")
        (install / "etc" / "notes.log").write_text("ERROR in etc\n")
        (install / "log" / "lease-x.log").write_text("ERROR in log\n")
        result = node.run("logs-search", {"text": "ERROR", "roots": [str(install)]})
        self.assertEqual([r["file"] for r in result["results"]], ["log/lease-x.log"])
        result = node.run("logs-search", {"text": "ERROR", "root": str(install)})
        self.assertEqual([(r["root"], r["file"]) for r in result["results"]], [("hyperloom log", "lease-x.log")])
        unmarked = node.run("logs-search", {"text": "ERROR", "root": str(self.tree.root)})
        self.assertIn("marked", unmarked["report"]["roots"][0]["error"])
        excluded = node.run("logs-search", {"text": "line", "roots": [str(self.tree.root)], "exclude": [str(self.tree.root / "a")]})
        self.assertFalse([r for r in excluded["results"] if r["file"].startswith("a/")])

    def test_links_are_not_followed(self):
        outside = self.tree.base / "outside"
        outside.mkdir()
        (outside / "o.log").write_text("ERROR outside\n")
        try:
            os.symlink(outside, self.tree.root / "link", target_is_directory=True)
            os.symlink(outside / "o.log", self.tree.root / "f.log")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = self.run_search(text="outside")
        self.assertEqual(result["results"], [])
        refused = self.run_search(text="outside", roots=[str(self.tree.root / "link")])
        self.assertTrue(refused["report"]["roots"][0]["error"])

    def test_windows_roots_through_a_junction_are_refused(self):
        if os.name != "nt":
            self.skipTest("Windows junctions")
        import _winapi
        junction = self.tree.base / "via-junction"
        _winapi.CreateJunction(str(self.tree.base), str(junction))
        refused = self.run_search(text="alpha", roots=[str(junction / "runs")])
        self.assertTrue(refused["report"]["roots"][0]["error"])
        self.assertEqual(refused["results"], [])

    def test_regex_subset_and_worker_deadline(self):
        for pattern in ("(ab)+", r"(a)\1", "(?=x)", "(a|b)*", "a" * 129):
            self.assertIn("error", self.run_search(regex=pattern), pattern)
        ok = self.run_search(regex=r"line 1[0-9]{2} ", max_results=5)
        self.assertEqual(len(ok["results"]), 5)
        (self.tree.root / "slow.log").write_text(("a" * 4000 + "\n") * 50)
        started = time.monotonic()
        stalled = self.run_search(regex="a*a*a*a*a*a*a*b", deadline_s=1)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertIn("deadline", stalled["report"]["truncated"])

    def test_budgets_count_every_read(self):
        for index in range(20):
            (self.tree.root / f"bin-{index}.log").write_bytes(b"\0" * 8000)
        result = self.run_search(text="x", max_files=1, max_bytes=1)
        self.assertLessEqual(result["report"]["files"], 1)
        self.assertLessEqual(result["report"]["bytes"], 1)
        self.assertTrue(set(result["report"]["truncated"]) & {"files", "bytes"})

    def test_probe_bytes_are_charged_once_and_still_matched(self):
        small = self.tree.base / "small"
        small.mkdir()
        (small / "a.log").write_bytes(b"needle\n")
        for limit, truncated in ((6, ["bytes"]), (7, []), (8, [])):
            result = node.run("logs-search", {"text": "needle", "roots": [str(small)], "max_bytes": limit})
            self.assertEqual((len(result["results"]), result["report"]["truncated"]), (1, truncated), limit)
            self.assertLessEqual(result["report"]["bytes"], limit)

    def test_replayed_bytes_are_always_processed(self):
        small = self.tree.base / "multi"
        small.mkdir()
        (small / "a.log").write_bytes(b"first\nneedle\n")
        for limit, truncated in ((12, ["bytes"]), (13, []), (14, [])):
            result = node.run("logs-search", {"text": "needle", "roots": [str(small)], "max_bytes": limit, "context": 1})
            self.assertEqual([(r["text"], r["before"]) for r in result["results"]], [("needle", ["first"])], limit)
            self.assertEqual(result["report"]["truncated"], truncated, limit)
        (small / "b.log").write_bytes(b"\0binary")
        result = node.run("logs-search", {"text": "zz", "roots": [str(small)], "max_bytes": 100})
        self.assertEqual((result["report"]["skipped_binary"], result["report"]["truncated"]), (1, []))

    def test_roots_are_the_configured_paths(self):
        install = self.tree.base / "install"
        (install / "log").mkdir(parents=True)
        (install / ".hyperloom-root").write_text("x\n")
        self.assertEqual(node.install_log(str(install)), str(install / "log"))
        if hasattr(os, "symlink"):
            try:
                os.symlink(install, self.tree.base / "install-link", target_is_directory=True)
            except OSError:
                return
            with self.assertRaises(ValueError):
                node.install_log(str(self.tree.base / "install-link"))

    def test_the_whole_reply_fits_the_output_limit(self):
        deep = self.tree.base / ("d" * 200) / ("e" * 200) / ("f" * 200)
        deep.mkdir(parents=True)
        (deep / "big.log").write_text("".join(f"match {'z' * 380} {i}\n" for i in range(500)))
        roots = [str(deep)] * 3 + [str(self.tree.base / ("missing" + "q" * 900) / str(i)) for i in range(12)]
        result = node.run("logs-search", {"text": "match", "roots": roots, "max_results": 500, "context": 2})
        self.assertLessEqual(len(json.dumps(result)), node.OUTPUT_BUDGET + 1024)
        self.assertIn("output", result["report"]["truncated"])

    def test_windows_files_reached_through_a_junction_are_refused(self):
        if os.name != "nt":
            self.skipTest("Windows junctions")
        import _winapi
        (self.tree.root / "Secrets").mkdir(exist_ok=True)
        (self.tree.root / "Secrets" / "z.log").write_text("ERROR behind a junction\n")
        _winapi.CreateJunction(str(self.tree.root / "Secrets"), str(self.tree.root / "plain"))
        self.assertEqual(self.run_search(text="junction")["results"], [])
        with self.assertRaises(OSError):
            node.open_file(self.tree.root, ("plain", "z.log"))

    def test_credential_names_are_pruned_whatever_their_case(self):
        (self.tree.root / "Secrets").mkdir()
        (self.tree.root / "Secrets" / "x.log").write_text("ERROR mixed case\n")
        (self.tree.root / ".SSH").mkdir()
        (self.tree.root / ".SSH" / "y.log").write_text("ERROR upper case\n")
        self.assertEqual(self.run_search(text="case")["results"], [])
        self.assertIsNotNone(node.protected(Path(os.path.realpath(self.tree.root / ".SSH"))))

    def test_output_budget(self):
        (self.tree.root / "big.log").write_text("".join(f"match {'y' * 380} {i}\n" for i in range(600)))
        result = self.run_search(text="match", max_results=500, context=2)
        self.assertLessEqual(len(json.dumps(result["results"])), node.OUTPUT_BUDGET)
        self.assertIn("output", result["report"]["truncated"])


class PortalSearchTests(APIFixture, unittest.TestCase):
    def setUp(self):
        self.api = self.make_api()
        self.tree = Tree(self)
        self.calls = []

        def fake(api, machine, verb, payload):
            self.calls.append((machine.id, verb, payload))
            local = dict(payload)
            local.pop("root", None)
            local["roots"] = [str(self.tree.root)]
            local.pop("exclude", None)
            return node.run(verb, local)
        patcher = mock.patch.object(ops, "_node_call", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_document_lists_root_ids_not_client_paths(self):
        doc = self.api.read("logsearch", {})
        halo = next(m for m in doc["machines"] if m["machine"] == "halo-1")
        self.assertEqual([r["id"] for r in halo["roots"]], ["hyperloom"])
        with self.assertRaises(Exception):
            self.api.read("logsearch", {"path": "/etc"})

    def test_query_fans_out_and_merges(self):
        status, doc = self.api.feature_post("logsearch/query", {"text": "ERROR", "machines": ["halo-1", "halo-2"],
                                                                "window_s": 3600, "context": 1})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["machine"] for r in doc["results"]), ["halo-1", "halo-2"])
        self.assertEqual(sorted(m["machine"] for m in doc["machines"]), ["halo-1", "halo-2"])
        payload = self.calls[0][2]
        self.assertEqual((payload["root"], payload["context"], payload["deadline_s"]), ("/opt/hl", 1, 4.5))
        self.assertNotIn("hunter2", json.dumps(doc))
        for bad in ({"text": "x", "machines": ["nope"]}, {"text": "x", "window_s": 5}, {"text": "x", "path": "/etc"},
                    {"text": "x", "roots": "x"}, {"regex": "x", "text": "y"}):
            with self.assertRaises(Exception, msg=bad):
                self.api.feature_post("logsearch/query", bad)

    def test_roots_are_validated_and_audited(self):
        for roots in (["relative"], ["/srv/x/.ssh"], ["/etc/x"], ["/"], ["/a"] * 17):
            with self.assertRaises(Exception, msg=roots):
                self.api.feature_post("logsearch/roots", {"machine": "halo-1", "roots": roots})
        self.api.feature_post("logsearch/roots", {"machine": "halo-1", "roots": ["/srv/lab/runs", "/srv/lab/runs"]})
        doc = self.api.read("logsearch", {})
        halo = next(m for m in doc["machines"] if m["machine"] == "halo-1")
        self.assertEqual(halo["configured"], ["/srv/lab/runs"])
        self.assertEqual([r["id"] for r in halo["roots"]], ["hyperloom", "root-0"])
        _, result = self.api.feature_post("logsearch/query", {"text": "alpha", "machines": ["halo-1"], "roots": ["root-0"]})
        self.assertEqual(self.calls[-1][2]["roots"], ["/srv/lab/runs"])
        self.assertNotIn("root", self.calls[-1][2])
        audit = [json.loads(line)["action"] for line in (Path(self.dir.name) / "audit.jsonl").read_text().splitlines()]
        self.assertIn("logsearch/roots", audit)

    def test_overlapping_searches_are_coalesced_per_machine(self):
        import threading as threads
        gate, entered = threads.Event(), threads.Event()

        def slow(api, machine, verb, payload):
            entered.set()
            gate.wait(5)
            return {"schema": "hyperloom/node-v1", "verb": verb, "results": [], "report": {"files": 0}}
        with mock.patch.object(ops, "_node_call", slow):
            first = threads.Thread(target=self.api.feature_post, args=("logsearch/query", {"text": "x", "machines": ["halo-1"]}))
            first.start()
            entered.wait(5)
            _, second = self.api.feature_post("logsearch/query", {"text": "x", "machines": ["halo-1", "halo-1"]})
            gate.set()
            first.join()
        self.assertEqual(len(second["machines"]), 1)
        self.assertIn("still running", second["machines"][0]["error"])

    def test_cli_runs_the_same_query(self):
        from hyperloom.cliext import logsearch as cli
        args = mock.Mock(pattern="ERROR", regex=False, ignore_case=False, machine=["halo-1"], root=None, since="1h",
                         process=None, level=None, context=None, max_results=None, json=True)
        with mock.patch("hyperloom.cliext.ops.cli_api", return_value=self.api), mock.patch("builtins.print") as printed:
            self.assertEqual(cli.search(args), 0)
        self.assertEqual(json.loads(printed.call_args[0][0])["schema"], "hyperloom/ui-logsearch-results-v1")


if __name__ == "__main__":
    unittest.main()
