"""Census reader (F12, C9), RTX prefill panel and batch-run stats (F13), configured-directory reads."""

import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

from hyperloom.web import features
from hyperloom.web.api import APIError
from hyperloom.web.features import ledger
from tests.test_ledger_collector import make_api, state


FIXTURES = Path(__file__).parent / "fixtures" / "ledger"


def fresh(api, name):
    """The document after one run of the `ledger-files` collector (the GET reads its snapshot)."""
    ledger.collect_files(api, api.clock())
    return getattr(ledger, name)(api, {})
COMPLETE = {"machines": ["halo-1", "halo-2"], "devices": [], "link_mode": "usb4gpu", "prompt_tokens": 43,
            "context_tokens": 1000, "batch_rows": 1, "spec_mode": "off", "spec_depth": {"na": "no speculation"},
            "precision": "q4", "recipe_digest": {"na": "no recipe"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.api, self.clock = make_api()
        self.addCleanup(self.api._directory.cleanup)
        features.reset(features.load(("ledger",)))
        self.addCleanup(features.reset, None)
        self.root = Path(self.api._directory.name)

    def configure(self, name, files=()):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        for source in files:
            shutil.copy(FIXTURES / source, directory / source)
        key = {"census": "census_dir", "runs": "logs_dir"}[name]
        self.api.feature_post("ledger/settings", {key: str(directory)})
        return directory


class CensusTests(Base):
    def live(self, **launch):
        ds = state("ds", "ds41-pair", (("halo-1", 18080),))
        ds["nodes"] = ["halo-1", "halo-2"]
        ds["launch"].update({"engine_sha": "cab238d6" + "0" * 32, **launch})
        return [ds]

    def census(self, states=()):
        with mock.patch.object(ledger, "_states", lambda api: list(states)), \
                mock.patch.object(self.api, "snapshot", lambda: {"profiles": {"profiles": []}}):
            return fresh(self.api, "census")

    def test_unconfigured_directory_is_unavailable(self):
        doc = self.census()
        self.assertIsNone(doc["artifacts"])
        self.assertIn("not configured", doc["why"]["artifacts"])

    def test_the_producer_output_is_read_with_its_tags(self):
        self.configure("census", ["census-qwen.json", "census-v1-ds.json"])
        doc = self.census()
        artifacts = {a["engine"]: a for a in doc["artifacts"]}
        qwen = artifacts["Qwen"]
        self.assertFalse(qwen["schema_declared"])
        self.assertIn("schema field absent", qwen["why"]["schema"])
        first = qwen["cells"][0]
        self.assertEqual((first["cell"], first["ceiling_tag"], first["wall_tag"], first["ceiling_tok_s"]),
                         ("code1k MTP decode", "E", "M", 86.0))
        window = qwen["cells"][1]
        self.assertEqual((window["bytes_tag"], window["ceiling_tag"]), ("E (inventory arithmetic)", None))
        self.assertEqual(window["why"]["ceiling_tag"], "tag not given")
        nd = artifacts["DS"]["cells"][2]
        self.assertEqual((nd["ceiling_tag"], nd["pct_ceiling"]), ("ND", None))
        self.assertEqual(nd["why"]["pct_ceiling"], "withheld by the census (not computed for this cell)")
        self.assertEqual(first["match"]["why"], "no running profile of this engine")

    def test_matching_states_each_dimension_and_never_claims_a_live_value_without_workload_evidence(self):
        directory = self.configure("census", ["census-v1-ds.json"])
        data = json.loads((directory / "census-v1-ds.json").read_text(encoding="utf-8"))
        data["cells"][0]["dims"] = dict(COMPLETE)
        data["cells"][1]["dims"] = {"machines": ["halo-1", "halo-2"]}
        (directory / "census-v1-ds.json").write_text(json.dumps(data), encoding="utf-8")
        doc = self.census(self.live())
        cells = doc["artifacts"][0]["cells"]
        match = cells[0]["match"]
        self.assertEqual((match["profile"], match["build"], match["comparable"]), ("ds", "engine commit", False))
        self.assertEqual(match["dims"]["machines"], "matches")
        self.assertEqual(match["dims"]["context_tokens"], "not observable live")
        self.assertTrue(match["dims"]["recipe_digest"].startswith("declared not applicable"))
        self.assertIn("not observable at this engine pin", match["why"])
        self.assertIn("cell does not declare: devices", cells[1]["match"]["why"])
        self.assertNotIn("live_tok_s", match)
        self.assertIn("not compared with a cell", doc["live"]["ds"]["label"])

    def test_build_and_dimension_mismatches_are_named(self):
        directory = self.configure("census", ["census-v1-ds.json"])
        data = json.loads((directory / "census-v1-ds.json").read_text(encoding="utf-8"))
        data["cells"][0]["dims"] = {**COMPLETE, "machines": ["halo-1"]}
        (directory / "census-v1-ds.json").write_text(json.dumps(data), encoding="utf-8")
        doc = self.census(self.live())
        self.assertIn("machines differs", doc["artifacts"][0]["cells"][0]["match"]["why"])
        other = self.live(engine_sha="f" * 40)
        doc = self.census(other)
        self.assertIn("running build differs", doc["artifacts"][0]["cells"][0]["match"]["why"])

    def test_short_identity_prefixes_never_match(self):
        directory = self.configure("census", ["census-v1-ds.json"])
        data = json.loads((directory / "census-v1-ds.json").read_text(encoding="utf-8"))
        data["sha"], data["binsha"] = "cab", ""
        data["cells"][0]["dims"] = dict(COMPLETE)
        (directory / "census-v1-ds.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("running build differs", self.census(self.live())["artifacts"][0]["cells"][0]["match"]["why"])

    def test_invalid_and_oversized_files_are_rejected_with_reasons(self):
        directory = self.configure("census")
        (directory / "bad.json").write_text("{", encoding="utf-8")
        (directory / "other.json").write_text(json.dumps({"schema": "census-v2", "engine": "DS", "sha": "x", "cells": []}),
                                             encoding="utf-8")
        (directory / "big.json").write_bytes(b" " * (ledger.CENSUS_BYTES + 1))
        doc = self.census()
        reasons = {r["file"]: r["why"] for r in doc["rejected"]}
        self.assertEqual(reasons["bad.json"], "not JSON")
        self.assertIn("not census-v1", reasons["other.json"])
        self.assertIn("larger than", reasons["big.json"])


class DirectoryTests(Base):
    def test_file_replaced_between_listing_and_open_is_refused(self):
        directory = self.configure("census", ["census-qwen.json"])
        setting = ledger._setting(self.api, "census_dir")
        with ledger.Directory(setting) as d:
            (name, listed), = d.entries((".json",), 10)[0]
            os.replace(directory / name, directory / "moved.json")
            (directory / name).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                d.open(name, listed)

    def test_directory_swap_while_reading_cannot_redirect_the_read(self):
        directory = self.configure("census", ["census-qwen.json"])
        original = (directory / "census-qwen.json").read_bytes()
        setting = ledger._setting(self.api, "census_dir")
        with ledger.Directory(setting) as d:
            (name, listed), = d.entries((".json",), 10)[0]
            try:
                os.rename(directory, self.root / "census-away")
            except PermissionError:
                # Windows: the held directory (and its ancestors) cannot be renamed during the read.
                self.assertEqual(os.name, "nt")
                with d.open(name, listed) as handle:
                    self.assertEqual(handle.read(), original)
                return
            # POSIX: the read goes through the directory descriptor, not the path.
            directory.mkdir()
            (directory / name).write_text("{}", encoding="utf-8")
            with d.open(name, listed) as handle:
                self.assertEqual(handle.read(), original)

    def test_replaced_directory_is_refused(self):
        directory = self.configure("census", ["census-qwen.json"])
        os.rename(directory, self.root / "census-old")
        directory.mkdir()
        shutil.copy(FIXTURES / "census-qwen.json", directory)
        doc = fresh(self.api, "census")
        self.assertIsNone(doc["artifacts"])
        self.assertIn("replaced since it was saved", doc["why"]["artifacts"])

    def test_a_link_is_refused_at_save_and_when_swapped_in(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links not permitted here")
        with self.assertRaises(APIError):
            self.api.feature_post("ledger/settings", {"census_dir": str(link)})
        directory = self.configure("census")
        os.rename(directory, self.root / "census-old")
        os.symlink(target, directory, target_is_directory=True)
        doc = fresh(self.api, "census")
        self.assertIsNone(doc["artifacts"])

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_a_junction_is_refused_at_save_and_when_swapped_in(self):
        import _winapi
        target = self.root / "target"
        target.mkdir()
        _winapi.CreateJunction(str(target), str(self.root / "junction"))
        with self.assertRaises(APIError):
            self.api.feature_post("ledger/settings", {"census_dir": str(self.root / "junction")})
        directory = self.configure("census")
        os.rename(directory, self.root / "census-old")
        _winapi.CreateJunction(str(target), str(directory))
        shutil.copy(FIXTURES / "census-qwen.json", target)
        doc = fresh(self.api, "census")
        self.assertIsNone(doc["artifacts"])

    def test_listing_skips_links_and_other_suffixes(self):
        directory = self.configure("runs", ["rtx-run.log"])
        (directory / "notes.md").write_text("x", encoding="utf-8")
        (directory / "sub.log").mkdir()
        runs, why, _ = ledger.list_runs(ledger._setting(self.api, "logs_dir"))
        self.assertEqual([r["name"] for r in runs], ["rtx-run.log"])


class RtxTests(Base):
    def test_batch_run_stats_are_parsed_by_listed_id(self):
        self.configure("runs", ["rtx-run.log"])
        with mock.patch.object(ledger, "_states", lambda api: []):
            doc = fresh(self.api, "rtx")
        self.assertEqual(len(doc["runs"]), 1)
        self.assertIn("no batch run parsed yet", doc["why"]["last"])
        status, result = self.api.feature_post("ledger/rtx-parse", {"id": doc["runs"][0]["id"]})
        stats = result["stats"]
        # The last of the two stats lines in the run log (strix-stream/stats-v1 as written by the run).
        self.assertEqual((stats["total"]["hits"], stats["total"]["routed"], stats["total"]["bytes_pinned"]),
                         (31, 60, 48695859712))
        self.assertAlmostEqual(stats["hit_rate"], 31 / 60)
        self.assertAlmostEqual(stats["wait_s"], 4.6195557)
        self.assertEqual((len(stats["layers"]), stats["sources_direct"], stats["backend"]), (60, 60, "cuda"))
        self.assertIsNone(stats["spec_used_share"])
        self.assertEqual(stats["why"]["spec_used_share"], "no speculative reads issued")
        self.assertIn("feature off", stats["why"]["readahead"])
        with mock.patch.object(ledger, "_states", lambda api: []):
            self.assertEqual(fresh(self.api, "rtx")["last"]["stats"]["total"]["hits"], 31)
        audit = [json.loads(line)["action"] for line in self.api.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("ledger/rtx-parse", audit)

    def test_unknown_or_malformed_ids_and_missing_lines(self):
        directory = self.configure("runs")
        for bad in ({"id": "../x"}, {"id": "0" * 15}, {"path": "x"}):
            with self.assertRaises(APIError):
                self.api.feature_post("ledger/rtx-parse", bad)
        with self.assertRaises(APIError) as caught:
            self.api.feature_post("ledger/rtx-parse", {"id": "0" * 16})
        self.assertEqual(caught.exception.status, 404)
        (directory / "plain.log").write_text("mm-rtx-prefill: total 1 s\n", encoding="utf-8")
        run_id = ledger.list_runs(ledger._setting(self.api, "logs_dir"))[0][0]["id"]
        with self.assertRaises(APIError) as caught:
            self.api.feature_post("ledger/rtx-parse", {"id": run_id})
        self.assertIn("no `ss stats` line", str(caught.exception))
        (directory / "plain.log").write_text('mm-rtx-prefill: ss stats {"schema": "other"}\n', encoding="utf-8")
        run_id = ledger.list_runs(ledger._setting(self.api, "logs_dir"))[0][0]["id"]
        with self.assertRaises(APIError) as caught:
            self.api.feature_post("ledger/rtx-parse", {"id": run_id})
        self.assertIn("not strix-stream/stats-v1", str(caught.exception))

    def test_service_without_provider_counters_is_unavailable(self):
        profile = state("ds", "ds41-pair")
        profile["prefill_service"] = {"port": 18500}

        def fetch(address, port, path, **kwargs):
            if path == "/health":
                return {"status": "ok", "busy": False, "queued": 0, "served": 3}
            raise OSError("404")
        with mock.patch.object(ledger, "_states", lambda api: [profile]), mock.patch.object(ledger, "_fetch", fetch):
            doc = fresh(self.api, "rtx")
        service = doc["services"][0]
        self.assertEqual(service["health"]["served"], 3)
        self.assertEqual(service["why"]["provider"], ledger.SERVICE_NEEDS)

    def test_service_provider_counters_and_stream_stats(self):
        profile = state("ds", "ds41-pair")
        profile["prefill_service"] = {"port": 18500}
        line = (FIXTURES / "rtx-run.log").read_text(encoding="utf-8").splitlines()
        stream = json.loads([l for l in line if "ss stats" in l][0].split("ss stats ", 1)[1])
        status = {"provider": {"name": "expert streamer", "counters": {"hits": 10, "misses": {"value": 2},
                                                                         "bytes_streamed": {"value": 4096, "unit": "bytes"},
                                                                         "bad": "x", "flag": True}},
                  "strix_stream": stream}
        with mock.patch.object(ledger, "_states", lambda api: [profile]), \
                mock.patch.object(ledger, "_fetch", lambda a, p, path, **k: {"status": "ok"} if path == "/health" else status):
            service = fresh(self.api, "rtx")["services"][0]
        self.assertEqual([c["name"] for c in service["provider"]["counters"]], ["hits", "misses", "bytes_streamed"])
        self.assertEqual(service["provider"]["counters"][2]["unit"], "bytes")
        self.assertEqual(service["strix_stream"]["total"]["hits"], 32)


if __name__ == "__main__":
    unittest.main()
