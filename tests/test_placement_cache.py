"""CPU-only cache regressions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
import json
import os

import pytest

from hyperloom.recipes import content_cache, descriptors


def test_04_legacy_mtime_cache_loads_unchanged(tmp_path):
    import subprocess
    # Execute the actual merge-base writer, including its JSON formatting and integer-second stat identity.
    namespace = dict(__name__="hyperloom.recipes.baseline_descriptors", __package__="hyperloom.recipes")
    baseline = subprocess.run(["git", "show", "ddc5640:hyperloom/recipes/descriptors.py"],
                              check=True, capture_output=True, text=True).stdout
    exec(compile(baseline, "ddc5640:descriptors.py", "exec"), namespace)
    model = tmp_path / "model"
    model.write_bytes(b"model")
    os.utime(model, ns=(1700000000250000000, 1700000000250000000))
    legacy = namespace["Store"](tmp_path / "cache")
    store = descriptors.Store(tmp_path / "cache")
    with mock.patch.object(descriptors.modelfiles, "read", return_value=({"legacy": True}, [])):
        legacy.save(model)
        original = store._path(model).read_bytes()
        assert store.load(model) == ({"legacy": True}, [])
        store.save(model)
        assert store._path(model).read_bytes() == original
    # Baseline deliberately treats same-second changes as unchanged; the next second invalidates it.
    os.utime(model, ns=(1700000000750000000, 1700000000750000000))
    assert store.load(model) == ({"legacy": True}, [])
    os.utime(model, ns=(1700000001250000000, 1700000001250000000))
    with pytest.raises(descriptors.DescriptorError, match="changed"):
        store.load(model)


def test_04_strict_placement_headers_have_a_separate_versioned_namespace(tmp_path):
    model = tmp_path / "model"
    model.write_bytes(b"model")
    legacy = descriptors.Store(tmp_path / "cache")
    strict = descriptors.PlacementStore(tmp_path / "cache")
    with mock.patch.object(descriptors.modelfiles, "read", return_value=({}, [])):
        legacy.save(model)
        strict.save(model)
    assert legacy._path(model) != strict._path(model)
    old = content_cache._read(legacy._path(model), "PL_E_STALE")
    precise = content_cache._read(strict._path(model), "PL_E_STALE")
    assert "mtime" in old["files"][0] and "schema" not in old
    assert precise["schema"] == strict.schema and "mtime_ns" in precise["files"][0]
    stat = model.stat()
    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100))
    with pytest.raises(descriptors.DescriptorError, match="changed"):
        strict.load(model)


def test_13_concurrent_cache_publication_is_atomic_and_cleans_temps(tmp_path):
    path = tmp_path / "cache.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: content_cache._write(path, {"value": i}), range(64)))
    assert type(json.loads(path.read_bytes())["value"]) is int
    assert list(tmp_path.iterdir()) == [path]


def test_13_failed_publication_cleans_unique_temporary_file(tmp_path):
    path = tmp_path / "cache.json"
    content_cache._write(path, {"old": True})
    with mock.patch.object(Path, "replace", side_effect=OSError("publication failed")), pytest.raises(OSError):
        content_cache._write(path, {"new": True})
    assert json.loads(path.read_bytes()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]
