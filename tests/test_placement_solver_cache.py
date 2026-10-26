"""CPU-only solver cache regressions."""

from copy import deepcopy
from pathlib import Path
from unittest import mock
import json

import pytest

from hyperloom.recipes import content_cache, measurements, reader, solver

from tests.placement_fixtures import identity


def test_12_solver_snapshots_matching_measurements(tmp_path, identity):
    from tests.test_placement_solver import SolverTests
    helper = SolverTests()
    helper.setUp()
    try:
        bundle = json.loads(helper.bundle.read_bytes())
        key = measurements.key(identity, "600", "a" * 64, "store")
        bundle["measurement_keys"] = {"anchor": key}
        bundle["probe"]["devices"] = [dict(id=identity, uuid=identity, driver="600")]
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        measured = measurements.median([dict(h2d_mbps=1, d2h_mbps=2, h2d_lat_ns=3)] * 9,
                                        measurements.XFER_METHOD)
        measurements.Cache(helper.root).save(key, "xfer", measured)
        result = helper.solve()
        record = content_cache._read(helper.root / "solver-plans" / (result["selection"] + ".json"), "PL_E_STALE")
        assert record["snapshot"]["request"]["probe"]["probe_xfer"]["anchor"]["h2d_mbps"] == 1
    finally:
        helper.doCleanups()


def test_12_production_solver_only_forwards_current_cache_hits_and_freezes_them(identity):
    from tests.test_placement_solver import SolverTests
    helper = SolverTests()
    helper.setUp()
    try:
        identity_key = measurements.key(identity, "600", "a" * 64, "store")
        measured = measurements.median([dict(h2d_mbps=10, d2h_mbps=20, h2d_lat_ns=30)] * 9,
                                        measurements.XFER_METHOD)
        cache = measurements.Cache(helper.root)
        cache.save(identity_key, "xfer", measured)
        bundle = json.loads(helper.bundle.read_bytes())
        bundle.update(measurement_keys={"anchor": identity_key})
        bundle["probe"].update(devices=[dict(uuid=identity, driver="600")], file_store="store",
                               xfer_sha256={"anchor": "a" * 64}, probe_xfer={"anchor": {"stale": 1}})
        captured = []
        original_run = helper.backend.core.run
        def run(argv, **kwargs):
            captured.append((json.loads(Path(argv[argv.index("--probe") + 1]).read_bytes()),
                             json.loads(Path(argv[argv.index("--inputs") + 1]).read_bytes())))
            return original_run(argv, **kwargs)
        helper.backend.core.run = run
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        with mock.patch.object(measurements, "acquire", side_effect=AssertionError("no measurement during solve")):
            result = helper.solve()
            assert captured[-1][0]["probe_xfer"]["anchor"]["h2d_mbps"] == 10
            assert captured[-1][1]["choices"][0]["probe_xfer"] == captured[-1][0]["probe_xfer"]
            for field in ("uuid", "driver", "xfer", "store"):
                changed = deepcopy(bundle)
                if field in ("uuid", "driver"):
                    changed["probe"]["devices"][0][field] = "changed"
                elif field == "xfer":
                    changed["probe"]["xfer_sha256"]["anchor"] = "b" * 64
                else:
                    changed["probe"]["file_store"] = "new-store"
                helper.bundle.write_bytes(json.dumps(changed).encode())
                helper.solve()
                assert captured[-1][0]["probe_xfer"] == {}
                assert captured[-1][1]["choices"][0]["probe_xfer"] == {}
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        cache.save(identity_key, "xfer", measurements.median([dict(h2d_mbps=11, d2h_mbps=22, h2d_lat_ns=33)] * 9,
                                                             measurements.XFER_METHOD))
        with pytest.raises(reader.RecipeError, match="measurements changed"):
            solver.pick(helper.cluster, result["selection"], "plain", "picked", backend=helper.backend)
    finally:
        helper.doCleanups()
