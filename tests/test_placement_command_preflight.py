"""CPU-only command preflight regressions."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock
import json

import pytest

from hyperloom import admission, cli, orchestrate, config
from hyperloom.config import parse
from hyperloom.recipes import launch, preflight, service, session

from tests import test_recipes, test_rtx
from tests.recipe_fixtures import isolated
from tests.test_web_recipes import Portal, QWEN


@pytest.mark.parametrize("entry", ["hold-up", "up", "hold-inputs", "hold-deploy", "deploy"])
@pytest.mark.parametrize("invalid", ["incomplete", "timezone", "expired", "short-lease", "blank-ref",
                                    "deadline", "long-deadline", "queue"])
def test_4_command_refusals_precede_admission_progress_snapshot_and_pins(tmp_path, monkeypatch, entry, invalid):
    path = tmp_path / "recipe.json"
    path.write_bytes(json.dumps(test_recipes.PlacementSchemaTests.document()).encode())
    options = launch.Options(str(path))
    intent = dict(service.DEFAULT_INTENT, profile="new")
    approval = None
    flags = []
    if invalid in ("deadline", "long-deadline", "queue"):
        key = "queue_timeout_s" if invalid == "queue" else "deadline_s"
        intent[key] = 1 if invalid == "queue" else (0 if invalid == "deadline" else 1200)
        flags = ["--queue-timeout" if invalid == "queue" else "--deadline", str(intent[key])]
    elif invalid == "incomplete":
        flags = ["--approval-ref", "review"]
        approval = {"ref": "review"}
    else:
        ref = " " if invalid == "blank-ref" else "review"
        lease = 1 if invalid == "short-lease" else 1300
        end = "2000-01-01T00:00:00Z" if invalid == "expired" else "2099-01-01T00:00:00Z"
        if invalid == "timezone":
            end = end[:-1]
        flags = ["--approval-ref", ref, "--approval-lease", str(lease), "--approval-end", end]
        approval = dict(ref=ref, lease_s=lease, end_epoch=0 if invalid == "expired" else 4070908800)
        if invalid == "timezone":
            approval["end_epoch"] = float("nan")  # Direct API equivalent: an invalid end.
    monkeypatch.setattr(service, "options_from_inputs", lambda *a: (None, intent, options))
    monkeypatch.setattr(preflight, "before_options", lambda *a, **kw: None)
    monkeypatch.setattr(service, "_states", lambda: {})
    monkeypatch.setattr(cli, "_cluster", lambda _: None)
    fake = mock.Mock()
    with ExitStack() as stack:
        writes = [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                  for module, name in [(admission, "for_config"), (service, "launch_progress"),
                                       (service, "acquire_pin"), (service, "pinned"), (service, "launch_inputs")]]
        if entry == "deploy":
            operation = lambda: service.deploy("a" * 64, orchestrate=fake, approval=approval)
        else:
            if entry == "hold-deploy":
                argv = ["recipe", "deploy", "a" * 64]
                flags = [] if invalid in ("deadline", "long-deadline", "queue") else flags
            elif entry == "hold-inputs":
                argv = ["up", "new", "--recipe-inputs", "a" * 64]
            else:
                argv = ["up", "new", "--recipe", str(path)]
            args = cli.build_parser().parse_args([*argv, *flags, "--config", str(tmp_path / "config.json")])
            operation = (lambda: cli.up_command(args, orchestrate=fake, load_cluster=lambda _: None)) if entry == "up" \
                else (lambda: cli._admission_hold(args))
        message = "queue timeout" if invalid == "queue" else ("deadline" if "deadline" in invalid else "approval")
        with pytest.raises(ValueError, match=message):
            operation()
        for write in writes:
            write.assert_not_called()
        fake.down.assert_not_called()
        fake.up.assert_not_called()


SNAPSHOT_FLAGS = [["--recipe", "other"], ["--recipe-profile", "profile.json"],
                  ["--recipe-overrides", "overrides.json"], ["--recipe-set", "a=1"],
                  ["--recipe-dir", "recipes"], ["--recipe-workload", "default"], ["--recipe-workload", ""],
                  ["--node", "halo-1"], ["--device", "workstation:rtx-pro"],
                  ["--diagnostic", "unknown"], ["--vram-cap-gib", "0"], ["--router", "halo-1"]]


COMMAND_FLAGS = [(["--node", "unknown"], "unknown machine"),
                 (["--node", "halo-1", "halo-1"], "distinct machines"),
                 (["--node", "halo-2", "halo-1"], "rank order"),
                 (["--diagnostic", "unknown"], "unknown diagnostic"),
                 (["--device", "bad"], "unknown machine or device"),
                 (["--device", "workstation:rtx-4090"], "excluded"),
                 (["--device", "halo-1:missing"], "unsupported profile device"),
                 (["--vram-cap-gib", "90"], "both set the RTX cap"),
                 (["--router", "unknown"], "router locally"),
                 (["--job", "not-hex"], "expected hex")]


COMMAND_CASES = [(entry, flags, "planned snapshot") for entry in ("hold-inputs", "inputs")
                 for flags in SNAPSHOT_FLAGS]


COMMAND_CASES += [(entry, flags, message) for entry in ("hold-up", "up") for flags, message in COMMAND_FLAGS]


COMMAND_CASES += [(entry, ["--job", "not-hex"], "expected hex")
                  for entry in ("hold-deploy", "deploy")]


COMMAND_CASES += [(entry, [], "not the reviewed") for entry in ("hold-deploy", "deploy")]


COMMAND_CASES += [(entry, [], "no snapshot") for entry in ("hold-deploy", "deploy", "hold-inputs", "inputs")]


COMMAND_CASES += [(entry, [], "expected a 64-hex digest") for entry in ("hold-inputs", "inputs")]


COMMAND_CASES += [(entry, [], "no such recipe") for entry in ("hold-up", "up")]


COMMAND_CASES += [(entry, [], "saved running state")
                  for entry in ("hold-up", "up", "hold-inputs", "inputs", "hold-plain", "plain")]


PLAIN_FLAGS = [(flags, "loopback-only" if flags[0] == "--router" else message)
               for flags, message in COMMAND_FLAGS if flags[0] != "--vram-cap-gib"]


PLAIN_FLAGS += [(["--vram-cap-gib", value], "vram-cap-gib") for value in ("nan", "0", "91")]


PLAIN_FLAGS += [(["--recipe-profile", "profile.json"], "need --recipe")]


PLAIN_FLAGS += [(["--recipe-workload", ""], "need --recipe")]


COMMAND_CASES += [(entry, flags, message) for entry in ("hold-plain", "plain") for flags, message in PLAIN_FLAGS]


@pytest.mark.parametrize("entry,flags,message", COMMAND_CASES)
def test_2_option_refusals_precede_holds_publication_pins_and_progress(tmp_path, monkeypatch, entry, flags, message):
    cluster = parse(test_rtx.rtx_example())
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(test_recipes.PlacementSchemaTests.document()), encoding="utf-8")
    options = launch.Options(str(path))
    intent = dict(service.DEFAULT_INTENT, profile="ds41-rtx")
    monkeypatch.setattr(cli, "_cluster", lambda _: cluster)
    def from_inputs(*args):
        if message == "no snapshot":
            raise service.NotFound("no snapshot (it may have expired: plan again)")
        if not args[0]:
            raise service.RecipeError("expected a 64-hex digest")
        return cluster, intent, options
    monkeypatch.setattr(service, "options_from_inputs", from_inputs)
    monkeypatch.setattr(service, "deployment_profile", lambda *a: "ds41-rtx")
    def locate(ref, *args, **kwargs):
        if not ref:
            raise service.NotFound("no such recipe")
        return path
    monkeypatch.setattr(service, "locate", locate)
    monkeypatch.setattr(service, "_states", lambda: {"old": {}} if message == "not the reviewed" else {})
    monkeypatch.setattr(orchestrate, "_state_path", lambda _: tmp_path / "up.json")
    if message == "saved running state":
        (tmp_path / "up.json").write_text("{}", encoding="utf-8")
    prepare = mock.Mock(return_value=SimpleNamespace(placement_request=object()))
    monkeypatch.setattr(launch, "prepare", prepare)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    fake = mock.Mock()
    with service.configured(tmp_path / "config.json"), ExitStack() as stack:
        writes = [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                  for module, name in [(admission, "for_config"), (service, "launch_inputs"),
                                       (service, "acquire_pin"), (service, "pinned"), (service, "launch_progress")]]
        if entry == "deploy":
            operation = lambda: service.deploy("a" * 64, job="not-hex" if flags else None, orchestrate=fake)
        else:
            argv = (["recipe", "deploy", "a" * 64] if entry == "hold-deploy" else
                    ["up", "ds41-rtx"] if "plain" in entry else
                    ["up", "ds41-rtx", "--recipe-inputs" if "inputs" in entry else "--recipe",
                     "" if message in ("expected a 64-hex digest", "no such recipe") else
                     "a" * 64 if "inputs" in entry else str(path)])
            args = cli.build_parser().parse_args([*argv, *flags, "--config", str(tmp_path / "config.json")])
            operation = (lambda: cli._admission_hold(args)) if entry.startswith("hold-") else (
                lambda: cli.up_command(args, orchestrate=fake, load_cluster=lambda _: cluster))
        with pytest.raises((ValueError, RuntimeError), match=message):
            operation()
        for write in writes:
            write.assert_not_called()
        fake.up.assert_not_called()
        fake.down.assert_not_called()
        prepare.assert_not_called()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_product_legacy_deploy_preflight_refuses_with_400_before_job_or_hold():
    with isolated() as env:
        cluster = config.load(env.config)
        recipe = service.locate(QWEN, cluster)
        digest = service.launch_inputs(cluster, recipe, session.Layers(),
                                       intent=dict(service.DEFAULT_INTENT, profile="qwen-replicas"))
        root, manifest = service.load_inputs(digest)
        saved = next(e for e in manifest["entries"] if e["role"] == "recipe")
        # A real legacy launch snapshot, corrupted after preview. Preflight must verify it before admission.
        (root / saved["name"]).write_text("changed after preview", encoding="utf-8")
        portal = Portal(env)
        try:
            # A legacy recipe's missing snapshot used to become an asynchronous job failure.
            status, body = portal.request("POST", "recipes/deploy", {"inputs": "0" * 64})
            assert status == 400
            assert set(body) == {"error"}
            assert "recipe preflight refused:" in body["error"]
            assert "no snapshot" in body["error"]
            status, body = portal.request("POST", "recipes/deploy", {"inputs": digest})
            assert status == 400
            assert set(body) == {"error"}
            assert "recipe preflight refused:" in body["error"]
            status, body = portal.request("POST", "up", {"profile": "qwen-replicas", "recipe_inputs": "0" * 64,
                                                        "node": ["halo-1"]})
            assert status == 400
            assert set(body) == {"error"}
            assert "recipe preflight refused:" in body["error"]
            assert portal.api._jobs == {}
            assert portal.api._active is None
            assert not (env.config.parent / "run" / "admission.sqlite3").exists()
            assert not (service.runtime_dir() / "recipe-inputs" / "pins").exists()
        finally:
            portal.close()
