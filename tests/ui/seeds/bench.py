"""Fixture benchmark runs for the browser smoke: the recorded runs of tests/fixtures/bench/seed-runs.json (made by
tests/fixtures/bench/record_seed.py with the real runners against the fake server) written into the fixture
portal's store with times shifted to now, plus one allow-listed ad-hoc target. Test data only."""

from tests.portable_fixtures import read_fixture
import json
from pathlib import Path


def seed(store, now):
    from hyperloom.bench import store as bench_store, targets
    recorded = json.loads(read_fixture(Path(__file__).resolve().parents[2] / "fixtures" / "bench" / "seed-runs.json"))
    store.put(targets.PREFS_NAMESPACE, targets.PREFS_KEY, {"targets": recorded["targets"]})
    for run in recorded["runs"]:
        run = dict(run)
        started = now - run.pop("age_s")
        run.update(started=started, finished=started + run.pop("duration_s"))
        bench_store.write(store, run)
