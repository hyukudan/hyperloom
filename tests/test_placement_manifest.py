"""CPU-only manifest regressions."""

from types import SimpleNamespace
from unittest import mock
import json
import subprocess

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom.recipes import commands, content_cache, core, measurements, reader

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
    def capture(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "capture_output" not in kwargs
        assert "text" not in kwargs
        for channel in ("stdout", "stderr"):
            assert kwargs[channel].fileno() >= 0
            assert "b" in kwargs[channel].mode
        kwargs["stdout"].write(json.dumps(document).encode("utf-8"))
        return SimpleNamespace(returncode=0)
    run = mock.Mock(side_effect=capture)
    def runner(paths, permitted):
        return measurements.manifest(None, paths, permitted, clock=lambda: NOW, run=run)
    result = measurements.acquire(None, "manifest", runtime=tmp_path, files=[tmp_path / "model"], window=approval,
                                  clock=lambda: NOW, manifest_runner=runner)
    assert run.call_args.args[0][1:3] == ["manifest", "--full"]
    assert run.call_args.kwargs["timeout"] == 600
    assert (tmp_path / "content-manifests" / (result["manifest_sha256"] + ".json")).exists()
    run.side_effect = subprocess.TimeoutExpired("manifest", 600)
    with pytest.raises(reader.RecipeError, match="deadline"):
        measurements.manifest(None, [tmp_path / "model"], approval, clock=lambda: NOW, run=run)


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("invalid", ["oversize", "encoding"])
def test_manifest_output_is_bounded_and_strict_utf8(tmp_path, monkeypatch, channel, invalid):
    approval = dict(window(), approval="test")
    monkeypatch.setattr(core, "locate", lambda _: "fake ts-recipe")
    monkeypatch.setattr(content_cache, "MAX_JSON_BYTES", 64)
    def run(argv, **kwargs):
        kwargs[channel].write(b"x" * 65 if invalid == "oversize" else b"\xff")
        return SimpleNamespace(returncode=0)
    reason = "exceeds" if invalid == "oversize" else "not UTF-8"
    with pytest.raises(reader.RecipeError, match="ts-recipe .*" + reason):
        measurements.manifest(None, [tmp_path / "model"], approval, clock=lambda: NOW, run=run)


def test_manifest_utf8_output_and_refusal_text(tmp_path, files, monkeypatch):
    artifacts, _ = files
    document = json.loads(artifacts.manifest.read_bytes())
    document["note"] = "caf\u00e9"
    approval = dict(window(), approval="test")
    monkeypatch.setattr(core, "locate", lambda _: "fake ts-recipe")
    for rc in (0, 1):
        def run(argv, **kwargs):
            kwargs["stdout"].write(json.dumps(document, ensure_ascii=False).encode("utf-8"))
            kwargs["stderr"].write("ts-recipe: refus\u00e9\n".encode("utf-8"))
            return SimpleNamespace(returncode=rc)
        operation = lambda: measurements.manifest(None, [tmp_path / "model"], approval, clock=lambda: NOW, run=run)
        if rc:
            with pytest.raises(reader.RecipeError, match="^ts-recipe: refus\u00e9$"):
                operation()
        else:
            assert operation() == document
