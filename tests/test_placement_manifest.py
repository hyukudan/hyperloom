"""CPU-only manifest regressions."""

from types import SimpleNamespace
from unittest import mock
import json

import pytest

from hyperloom.recipes import commands, core, measurements, reader

from tests.placement_fixtures import files, identity
from tests.test_recipes_grants import NOW, window


def test_08_manifest_command_connects_core_runner(tmp_path):
    args = SimpleNamespace(xfer=False, store=False, uuid=None, xfer_sha256=None, store_id=None,
                           file=["model"], window={})
    with mock.patch.object(commands, "runtime_dir", return_value=tmp_path), \
            mock.patch.object(measurements, "acquire", return_value={}) as acquire:
        commands.measure_command(SimpleNamespace(), args)
    assert callable(acquire.call_args.kwargs.get("manifest_runner"))


def test_08_manifest_full_read_deadline_and_persistence(tmp_path, files, monkeypatch):
    artifacts, _ = files
    approval = {k: v for k, v in window().items() if k != "kind"}
    approval["approval"] = "test"
    document = json.loads(artifacts.manifest.read_bytes())
    monkeypatch.setattr(core, "locate", lambda _: "fake ts-recipe")
    run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr=""))
    def runner(paths, permitted):
        return measurements.manifest(None, paths, permitted, clock=lambda: NOW, run=run)
    result = measurements.acquire(None, "manifest", runtime=tmp_path, files=[tmp_path / "model"], window=approval,
                                  clock=lambda: NOW, manifest_runner=runner)
    assert run.call_args.args[0][1:3] == ["manifest", "--full"]
    assert run.call_args.kwargs["timeout"] == 600
    assert (tmp_path / "content-manifests" / (result["manifest_sha256"] + ".json")).exists()
    import subprocess
    run.side_effect = subprocess.TimeoutExpired("manifest", 600)
    with pytest.raises(reader.RecipeError, match="deadline"):
        measurements.manifest(None, [tmp_path / "model"], approval, clock=lambda: NOW, run=run)
