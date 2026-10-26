"""CPU-only argv values regressions."""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hyperloom import admission, orchestrate, procs, rtx
from hyperloom.recipes import launch, measurements, preflight, runtime, service, session
from hyperloom.recipes.reader import RecipeError

from tests import test_placement_render
from tests.test_recipes_grants import NOW, window
from tools.check_replica_args import check_document


U32 = ("--steps", "--verify", "--warm", "--pressure-free", "--pressure-recent", "--prefetch-layer",
       "--prefetch-token", "--chat-prompt", "--chat-steps", "--controller-explore", "--winpf-l1-cap",
       "--winpf-prior-cap", "--winpf-revert", "--wb-alt", "--serve-max")


FLOATS = ("--winpf-pmin", "--winpf-prior-pmin", "--serve-idle")


BAD = ([(o, v) for o in U32 for v in ("nonsense", "4294967296", "-4294967296")]
       + [(o, v) for o in FLOATS for v in ("nonsense", "1e999", "1e-999")]
       + [("--winpf-pmin", "3.5e38"), ("--winpf-prior-pmin", "1e-46"),
          ("--winpf-pmin", "0x1p-150"), ("--winpf-pmin", "0x1.ffffffp127")]
       + [("--window", "unknown"), ("--window", "fixed:nonsense"), ("--window", "fixed:4294967296"),
          ("--controller-scope", "unknown"), ("--reset-cache", "unknown"),
          ("--env", "CUDA_VISIBLE_DEVICES=bad"), ("--env", "STRIX_DS41_CUDA_ENGRAM_CHECK"),
          ("--env", "=1"), ("--env", "strix_ds41_cuda_engram_check=1"),
          ("--controller-costs", "1,2"), ("--controller-costs", "1,2,3,4,5,nonsense"),
          ("--controller-costs", "1,2,3,4,5,0"), ("--controller-costs", "1,2,3,4,5,nan"),
          ("--controller-prior", "builtin:4294967296"), ("--chats", "nonsense"),
          ("--seqmod", "nonsense:0"), ("--seqmod", "5:nonsense"),
          ("--steps", "2147483632"), ("--chat-prompt", "2147483504"),
          ("--chat-steps", "2147483536"), ("--phases", "chats99999999999999999999"),
          ("--phases", "chats0b4294967296"), ("--phases", "exact0"), ("--phases", "unknown")])


GOOD = ([(o, "0suffix") for o in U32 if o != "--steps"]
        + [("--steps", "64suffix"), ("--verify", " +42tail"), ("--verify", "-1"),
           ("--winpf-pmin", "-2.5tail"), ("--winpf-prior-pmin", "nan(payload)"),
           ("--winpf-pmin", "1e-40"), ("--winpf-pmin", "7.006492321624086e-46"),
           ("--winpf-pmin", "3.40282356779733661637539395458142568447e38"),
           ("--winpf-pmin", "0x1.8p+1tail"), ("--serve-idle", "inf"),
           ("--window", "fixed:0suffix"), ("--window", "probe"), ("--controller-scope", "chat"),
           ("--reset-cache", "open"), ("--controller-costs", "none"),
           ("--controller-costs", "1,2,3,4,5,6,-7"), ("--controller-prior", "builtin:0"),
           ("--env", "STRIX_DS41_CUDA_ENGRAM_CHECK="),
           ("--env", "STRIX_DS41_REPLICA_WINDOW_ENGRAM_PREFETCH=anything=else")])


ARG_CASES = ([(o, v, False, repeat) for o, v in BAD for repeat in (False, True)]
             + [(o, v, True, False) for o, v in GOOD])


@pytest.mark.parametrize("option,value,valid,repeat", ARG_CASES)
def test_2_native_values_are_checked_before_admission(tmp_path, monkeypatch, option, value, valid, repeat):
    check_document()
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        argv = list(fixture.argv)
        # The overwritten occurrence must still be validated. For files, the later value is readable.
        later = (argv[argv.index(option) + 1] if option in argv else
                 "controller" if option == "--window" else "run" if option == "--controller-scope" else
                 "warm" if option == "--reset-cache" else "STRIX_DS41_CUDA_ENGRAM_CHECK=1" if option == "--env" else
                 "none" if option in ("--controller-prior", "--controller-costs") else "5:0" if option == "--seqmod" else "64")
        argv.extend((option, value))
        if repeat:
            argv.extend((option, later))
        for i, device in enumerate(fixture.document["devices"]):
            device.update(role="anchor" if i == 0 else "assist", budget_bytes=10 * rtx.GIB)
        policy = dict(grants=[dict(uuid=u, vram_cap_gib=10, approval="test", window=window(),
                                  gate=[str(Path(__file__).resolve()), "fake"]) for u in fixture.uuids],
                      caps_gib={u.lower(): 10 for u in fixture.uuids},
                      caps_bytes={u.lower(): 10 * rtx.GIB for u in fixture.uuids}, host_pinned_gib=0, host_pageable_gib=0,
                      free_commit_floor_gib=4)
        decision = dict(placement=True, machine="local", argv=argv, supervision=policy,
                        files=dict(placement=str(fixture.files.placement), manifest=str(fixture.files.manifest),
                                   receipts=str(fixture.files.receipts), manifest_sha256=None, document=fixture.document))
        evaluation = SimpleNamespace(problems=[], refusal=None, decision=decision, effective_sha256="a" * 64,
                                     effective=SimpleNamespace(untuned=False))
        cluster = SimpleNamespace(machines={"local": SimpleNamespace(access="local", os="windows",
                                                                    install_root=str(fixture.root))})
        before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        with ExitStack() as stack:
            monkeypatch.setattr(runtime, "preset", lambda: fixture.data)
            monkeypatch.setattr(rtx.time, "time", lambda: NOW)
            stack.enter_context(mock.patch.object(launch, "_backend", return_value=(object(), True)))
            stack.enter_context(mock.patch.object(session, "evaluate", return_value=evaluation))
            stack.enter_context(mock.patch.object(preflight, "includes", return_value=True))
            forbidden = [stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                         for module, name in ((admission, "for_config"), (service, "acquire_pin"),
                         (service, "launch_inputs"), (service, "launch_progress"), (orchestrate, "_write"),
                         (orchestrate, "_call"), (orchestrate, "_detached"), (procs, "LocalChild"),
                         (procs, "InstanceLock"), (rtx, "prepare_service"), (rtx, "run_gate"),
                         (measurements, "acquire"), (runtime.Files, "publish"))]
            def prepare():
                # The service helper owns these controls; exercise their native syntax at the lower pure renderer.
                if option in ("--serve-max", "--serve-idle"):
                    runtime.render(fixture.files, binary="fake", argv=argv)
                    return SimpleNamespace(placement_request=SimpleNamespace(argv=tuple(argv)))
                return launch.prepare(cluster, "decode", launch.Options("fake-placement.json"), router="local",
                                      vram_cap_gib=None, state_dir=tmp_path, source=lambda _: None)
            if valid:
                assert prepare().placement_request.argv == tuple(argv)
            else:
                with pytest.raises(RecipeError, match="PL_E_SCHEMA"):
                    prepare()
            for call in forbidden:
                call.assert_not_called()
        assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        assert list(tmp_path.iterdir()) == []
    finally:
        fixture.doCleanups()
