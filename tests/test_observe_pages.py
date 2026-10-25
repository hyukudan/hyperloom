"""The observability pages rendered (with a minimal page shell) against documents the collectors produced from
partial engine answers: missing counts are shown as unavailable with why, never as a fabricated value."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hyperloom.web.features import observe
from tests.fixtures.observe.fakes import HERE, NODE1, Env, fixture

NODE = shutil.which("node")


def render(page, docs, storage=None):
    answer = subprocess.run([NODE, str(HERE / "render.js")], input=json.dumps({"page": page, "docs": docs,
                                                                              "storage": storage}),
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    if answer.returncode:
        raise AssertionError(answer.stderr)
    return answer.stdout


class Collected(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(self.tmp.name).__enter__()
        self.env.launch("qwen", [("halo-1", NODE1, 18501)])
        self.env.router()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()


@unittest.skipUnless(NODE, "needs node to render the pages")
class PartialCountsTests(Collected):
    def collected(self, **speculative):
        status = fixture("engine-status-c1.json")
        for key, value in speculative.items():
            if value is None:
                status["speculative"].pop(key, None)
            else:
                status["speculative"][key] = value
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        return (observe.speculative_document(self.env.api, {}), observe.engines_document(self.env.api, {}))

    def test_missing_accepted_count(self):
        spec, engines = self.collected(accepted=None, proposed=10)
        row = spec["engines"][0]
        self.assertIsNone(row["acceptance"])
        self.assertIsNone(row["accepted_per_cycle"])
        self.assertIn("without an accepted count", row["why"]["acceptance"])
        page = render("speculative", {"observe/speculative": spec})
        self.assertIn("without an accepted count", page)
        self.assertNotIn("null", page)
        panel = render("engine-panel", {"observe/engines": engines})
        self.assertNotIn("null", panel)
        self.assertNotIn("0.00", panel)
        self.assertIn("without an accepted count", panel)
        self.assertIn("no accepted count", panel)

    def test_partial_pool_report(self):
        status = fixture("engine-status-c1.json")
        slots = status.get("slots") if isinstance(status.get("slots"), dict) else {}
        for key in [k for k in slots if "pool" in k or "free" in k or k.endswith("_bytes")]:
            slots.pop(key)
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        engines = observe.engines_document(self.env.api, {})
        row = engines["engines"][0]
        row["slots"] = {**(row.get("slots") or {}), "pool_positions": 1024, "pool_bytes": None, "pool_largest_free": None}
        panel = render("engine-panel", {"observe/engines": engines})
        self.assertRegex(panel, r"1[,.]?024 positions · size not reported · largest free not reported")
        self.assertNotIn("null", panel)

    def test_missing_proposed_count(self):
        spec, engines = self.collected(proposed=None)
        self.assertIn("without a proposed count", spec["engines"][0]["why"]["acceptance"])
        for page, docs in (("speculative", {"observe/speculative": spec}), ("engine-panel", {"observe/engines": engines})):
            self.assertNotIn("null", render(page, docs))

    def test_complete_counts(self):
        spec, engines = self.collected()
        page = render("speculative", {"observe/speculative": spec})
        self.assertIn("84 / 115", page)
        self.assertIn("84 / 115", render("engine-panel", {"observe/engines": engines}))


@unittest.skipUnless(NODE, "needs node to render the pages")
class DisagreementTests(Collected):
    """A count the engine reports itself is shown as reported, with the C8 disagreement beside it."""

    def test_reported_computed_count_disagrees(self):
        status = fixture("engine-status-c1.json")
        status["requests"][1]["computed_tokens"] = 809
        status["observed"]["computed_tokens"] = 809
        status["totals"]["computed_tokens"] = 5711
        self.env.engine(NODE1, 18501, status)
        self.env.collect()
        engines = observe.engines_document(self.env.api, {})
        panel = render("engine-panel", {"observe/engines": engines})
        self.assertIn("prompt - cached - imported = 808", panel)
        accounting = engines["engines"][0]["prompt_accounting"]
        if accounting["lifetime"].get("why", {}).get("computed_tokens"):
            self.assertIn(accounting["lifetime"]["why"]["computed_tokens"], panel.replace("&#39;", "'"))
        request = observe.request_document(self.env.api, "aabbccddeeff0011", {})
        self.assertIn("808", request["request"]["engine"]["why"]["computed_tokens"])
        listing = observe.requests_document(self.env.api, {"limit": "50"})
        page = render("timeline", {"observe/requests?limit=50": listing,
                                   "observe/requests/aabbccddeeff0011": request}, {"selected": "aabbccddeeff0011"})
        self.assertIn("engine reports 809 computed tokens; prompt - cached - imported = 808", page)


class LatencyCoverageTests(Collected):
    """The latency statistics name the share of completed requests they cover when the per-view bound cuts them."""

    def test_cut_across_profiles_is_disclosed(self):
        self.env.collect()
        store = self.env.store
        columns = [row[1] for row in store.execute("PRAGMA table_info(observe_requests)")]
        copied = ", ".join("'other'" if c == "profile" else c for c in columns)
        store.execute(f"INSERT INTO observe_requests ({', '.join(columns)}) SELECT {copied} FROM observe_requests")
        eligible = store.execute("SELECT COUNT(*) FROM observe_requests WHERE state='done'")[0][0]
        self.assertGreater(eligible, 3)
        whole = observe.latency_document(self.env.api, {})
        self.assertEqual((whole["coverage"]["eligible"], whole["coverage"]["cut"]), (eligible, False))
        self.assertNotIn("coverage", whole["why"])
        with mock.patch.object(observe, "MAX_ROWS", 3):
            cut = observe.latency_document(self.env.api, {})
        self.assertEqual((cut["coverage"]["selected"], cut["coverage"]["eligible"], cut["coverage"]["cut"]), (3, eligible, True))
        self.assertIn(f"the newest 3 of {eligible}", cut["why"]["coverage"])
        self.assertEqual(sum(g["requests"] for g in cut["groups"]), 3)
        if NODE:
            page = render("latency", {"observe/latency?by=model&range=3600": cut})
            self.assertIn(f"the newest 3 of {eligible} completed requests", page)
            self.assertIn("partial", page)

    def test_published_scopes_are_kept(self):
        self.assertEqual(observe._scope({"since": "observed (submitted after arming)"}),
                         "cumulative since the engine was armed (observed (submitted after arming))")
        self.assertEqual(observe._scope({"since": "armed"}), "cumulative since the engine was armed")
        self.assertEqual(observe._scope({"since": "process start"}), "cumulative since the engine process started")
        self.assertEqual(observe._scope({"since": "last reset"}), "cumulative (last reset)")
        self.assertEqual(observe._scope({"since": "unknown"}), "cumulative (scope not reported)")


if __name__ == "__main__":
    unittest.main()
