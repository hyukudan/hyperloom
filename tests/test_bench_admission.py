"""Benchmark admission (C11) through the portal: one run per resource set, the lab cap, deploys refused while a
benchmark holds the resources, stops cancel benchmarks, CLI holds."""

from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))

import support  # noqa: E402
from fake_server import FakeServer  # noqa: E402

from hyperloom import admission  # noqa: E402
from hyperloom.bench import runs, targets  # noqa: E402
from hyperloom.web.api import APIError  # noqa: E402


SLOW = {"levels": [1], "max_tokens": 400}


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.05)

    def finish(self, job):
        self.bench.api.mutate("cancel", {"job": job["id"]})
        return self.bench.wait(job, 20)

    def test_one_run_per_resource_set(self):
        first = self.bench.start("bench/decode", {"target": {"profile": "qwen"}, **SLOW})
        with self.assertRaises(APIError) as caught:
            self.bench.start("bench/tools", {"target": {"profile": "qwen"}})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("held by bench/decode", str(caught.exception))
        guard = self.bench.api.read("bench/guard", {"target": "profile:qwen"})
        self.assertFalse(guard["preview"]["admitted"])
        self.assertEqual(guard["running"][0]["run_id"], first["arguments"]["run_id"])
        other = self.bench.start("bench/decode", {"target": {"target": "fake"}, **SLOW})       # disjoint: admitted
        with FakeServer() as third:
            targets.save(self.bench.api, {"id": "third", "scheme": "http", "host": "127.0.0.1", "port": third.port, "machines": ["halo-2"]})
            with self.assertRaises(APIError) as caught:
                self.bench.start("bench/decode", {"target": {"target": "third"}, **SLOW})
            self.assertIn("at most 2", str(caught.exception))
        self.assertEqual(self.finish(first)["state"], "cancelled")
        self.assertEqual(self.finish(other)["state"], "cancelled")

    def test_undeclared_target_holds_everything(self):
        targets.save(self.bench.api, {"id": "loose", "scheme": "http", "host": "127.0.0.1", "port": self.bench.server.port})
        job = self.bench.start("bench/decode", {"target": {"target": "loose"}, **SLOW})
        self.assertEqual(job["resources"], ["*"])
        with self.assertRaises(APIError):
            self.bench.start("bench/decode", {"target": {"target": "fake"}, **SLOW})
        self.finish(job)

    def test_deploy_is_refused_while_a_benchmark_holds_its_resources(self):
        job = self.bench.start("bench/decode", {"target": {"profile": "qwen"}, **SLOW})
        self.bench.api._arguments = lambda action, arguments: arguments
        with self.assertRaises(APIError) as caught:
            self.bench.api.mutate("up", {"profile": "qwen"})
        self.assertIn("benchmark or demo holds these resources", str(caught.exception))
        self.finish(job)

    def test_stop_cancels_the_benchmark(self):
        job = self.bench.start("bench/decode", {"target": {"profile": "qwen"}, **SLOW})
        time.sleep(0.3)
        self.bench.api.admission_db.acquire("stop", {"halo-1", "router"}, label="down qwen")
        record = self.bench.wait(job, 20)
        self.assertEqual(record["state"], "cancelled")
        self.assertIn("stopped", record["error"])
        run = self.bench.api.read("bench/runs/" + job["arguments"]["run_id"])["run"]
        self.assertEqual(run["state"], "cancelled")

    def test_cli_run_takes_and_releases_its_own_hold(self):
        db = admission.Admission(":memory:")
        self.addCleanup(db.close)
        with mock.patch.object(admission, "for_config", lambda path: db):
            with runs.hold(self.bench.api.config_path, {"halo-1", "router"}, "cli bench/decode x") as (held, hold_id):
                self.assertEqual([h["label"] for h in db.holds()], ["cli bench/decode x"])
                with self.assertRaises(admission.Refused):
                    db.acquire("core", {"halo-1"}, label="up")
                context = runs.CLIContext(30, hold_id, held)
                db.acquire("stop", {"router"}, label="down")
                time.sleep(1.1)
                self.assertTrue(context.cancelled())
                self.assertIn("stopped", context.reason)
            self.assertEqual([h["kind"] for h in db.holds()], ["stop"])


class ReadHoldsTests(unittest.TestCase):
    """GET documents read the admission holds without writing: no purge, no database created."""

    def test_reads_live_holds_without_purging(self):
        now = [1000.0]
        db = admission.Admission(":memory:", clock=lambda: now[0])
        live = db.acquire("bench", {"m1"}, label="live")
        db.acquire("stop", {"m2"}, label="old", lease_s=1)
        now[0] += 10
        statements = []
        db._db.set_trace_callback(statements.append)
        self.assertEqual([h["id"] for h in admission.read_holds("cfg.json", db)], [live])
        self.assertFalse([s for s in statements if not s.lstrip().upper().startswith("SELECT")], statements)
        self.assertEqual(db._db.execute("SELECT COUNT(*) FROM holds").fetchone()[0], 2)    # the expired one stays

    def test_no_database_is_created(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("os.environ", {"HYPERLOOM_ADMISSION_DB": ""}):
            config = Path(directory, "cluster.json")
            self.assertEqual(admission.read_holds(config), [])
            self.assertFalse(Path(directory, "run").exists())

    def test_ops_and_health_documents_read_without_writing(self):
        from hyperloom.web.features import health, ops
        db = admission.Admission(":memory:")
        db.acquire("bench", {"m1"}, label="bench m1")
        db.acquire("stop", {"m2"}, label="down p")
        api = mock.Mock(config_path="cfg.json", admission_db=db)
        statements = []
        db._db.set_trace_callback(statements.append)
        self.assertEqual(ops.bench_holds(api, {"m1"}), ["bench m1"])
        context = health.Context(api, None, 0.0, None)
        self.assertEqual(context.stop_resources(), [({"m2"}, "down p")])
        self.assertFalse([s for s in statements if not s.lstrip().upper().startswith("SELECT")], statements)


class GuardAndWorkerTests(unittest.TestCase):
    def test_the_preview_counts_a_cancelled_benchmark_until_it_is_released(self):
        from hyperloom.bench import guard
        holds = [{"id": "b1", "kind": "bench", "resources": ["m1"], "label": "bench m1", "cancelled": "stopped"}]
        self.assertIn("held by bench m1", guard.refusal(holds, ["m1"]))
        db = admission.Admission(":memory:")
        db.acquire("bench", {"m1"}, label="bench m1")
        db.release(db.acquire("stop", {"m1"}, label="down p"))       # the stop finished; the bench cleans up
        with self.assertRaises(admission.Refused):
            db.acquire("bench", {"m1"})                              # the preview agrees with the admission

    def test_a_worker_that_cannot_start_ends_the_run_after_the_started_ones(self):
        import threading
        finished, gate = [], threading.Event()

        def work(item):
            gate.wait(5)
            finished.append(item)
        real_start, calls = threading.Thread.start, []

        def start(thread):
            calls.append(thread)
            if len(calls) == 2:
                raise RuntimeError("can't start new thread")
            real_start(thread)
        with mock.patch.object(threading.Thread, "start", start):
            releaser = threading.Thread(target=lambda: (time.sleep(0.2), gate.set()))
            real_start(releaser)
            with self.assertRaises(RuntimeError):
                runs.parallel(work, [1, 2, 3, 4], 3)
        self.assertEqual(finished, [1])          # the started worker finished its item before parallel returned;
        releaser.join(5)                         # no further item was started


if __name__ == "__main__":
    unittest.main()
