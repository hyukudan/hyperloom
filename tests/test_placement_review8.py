"""Review 8 item 5: native startup refusals precede admission, using CPU fixtures only."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hyperloom import admission, orchestrate, procs, rtx
from hyperloom.recipes import launch, measurements, preflight, runtime, service, session
from hyperloom.recipes.reader import RecipeError
from tests import test_placement_render
from tests.test_recipes_grants import NOW, window


# Keep this inventory independent of runtime.SIZING: these native controls must never override a plan.
CAPACITY = ("--max-positions", "--max-rows", "--arena-gib", "--spares", "--host-spares",
            "--pressure-spares", "--predict", "--layers", "--host-margin-gib", "--max-pinned-gib",
            "--engram", "--engram-cache-mb", "--draft", "--draft-device", "--draft-vocab", "--draft-keep",
            "--draft-keep-from", "--warmup-graphs")
DISPATCH = ("--timing", "--stream-hashes", "--chat-logits", "--records-inline", "--route-stats", "--test-hooks",
            "--fuse-hc", "--fuse-shared",
            "--router-v2", "--router-pf", "--router-pf0", "--pdl", "--pdl-np", "--pdl-regs",
            "--shared-fork", "--qakv-s1", "--qakv-auto", "--attn-fork", "--ob-expand", "--moe-expand",
            "--ob-one", "--attn1", "--attn-h1", "--attn-pf", "--hc-cluster", "--hc-partial5",
            "--moe-v1b3", "--dsrtxk-off", "--fuse-off")
INPUTS = ("model", "--import-state", "--receipt", "--ref", "--seqs-file", "--routes",
          "--residency-seed", "--winpf-prior", "--controller-prior")
VALUES = ("--import-state", "--receipt", "--ref", "--seqs-file", "--routes", "--seqmod",
          "--phases", "--chats", "--steps", "--chat-prompt", "--chat-steps", "--json", "--timeline",
          "--verify", "--warm", "--pressure-free", "--pressure-recent", "--prefetch-layer", "--prefetch-token",
          "--window", "--controller-scope", "--controller-prior", "--controller-save", "--controller-costs",
          "--controller-explore", "--probe-phase", "--draft-trace", "--winpf-prior", "--winpf-l1-cap",
          "--winpf-pmin", "--winpf-prior-cap", "--winpf-prior-pmin", "--winpf-revert", "--wb-alt",
          "--residency-save", "--reset-cache", "--env")
CASES = ([('valid', 'none')] + [("required", option) for option in
         ("model", "--import-state", "--receipt", "--ref", "--seqs-file", "--chats", "--draft")]
         + [(fault, option) for fault in ("missing-value", "next-option", "equals-value") for option in VALUES]
         + [(fault, option) for fault in ("missing-file", "unreadable", "directory") for option in INPUTS]
         + [(fault, option) for fault in ("conflict", "equals-conflict") for option in CAPACITY + DISPATCH]
         + [("unknown", "--unknown-native-option"), ("duplicate-file", "--import-state"),
            ("builtin", "--controller-prior"), ("default-phases", "--ref")])


@pytest.mark.parametrize("fault,option", CASES, ids=[f"{fault}:{option}" for fault, option in CASES])
def test_5_native_startup_is_validated_before_admission(tmp_path, monkeypatch, fault, option):
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        paths = {}
        for name in INPUTS:
            path = fixture.root / ("native-" + name.lstrip("-")) if name != "model" else fixture.root / "model"
            if name != "model":
                path.write_bytes(b"synthetic native input")
            paths[name] = path
        argv = [str(paths["model"])]
        for name in INPUTS[1:]:
            argv.extend((name, str(paths[name]) + (":64" if name == "--controller-prior" else "")))
        argv.extend(("--phases", "exact0,chats0", "--chats", "0"))
        expected = "PL_E_SCHEMA"
        if fault == "required":
            if option == "model":
                argv.pop(0)
            elif option == "--draft":
                argv[argv.index("--phases") + 1] = "spec0"
            else:
                index = argv.index(option)
                del argv[index:index + 2]
        elif fault in ("missing-value", "next-option", "equals-value"):
            if option in argv:
                index = argv.index(option)
                del argv[index:index + 2]
            argv.extend([option] if fault == "missing-value" else
                        [option, "--qual"] if fault == "next-option" else [option + "=value"])
        elif fault in ("missing-file", "directory", "unreadable"):
            expected = "PL_E_STALE"
            if fault != "unreadable":
                paths[option].unlink()
                if fault == "directory":
                    paths[option].mkdir()
            else:
                original = Path.open
                def open_file(path, *args, **kwargs):
                    if path == paths[option]:
                        raise PermissionError("synthetic unreadable input")
                    return original(path, *args, **kwargs)
                monkeypatch.setattr(Path, "open", open_file)
        elif fault in ("conflict", "equals-conflict"):
            expected = "PL_E_CONFLICT"
            argv.extend([option, "1"] if fault == "conflict" else [option + "=1"])
        elif fault == "unknown":
            argv.append(option)
        elif fault == "duplicate-file":
            expected = "PL_E_STALE"
            argv[1:1] = [option, str(fixture.root / "absent-state")]
        elif fault == "builtin":
            argv[argv.index(option) + 1] = "builtin:64"
        elif fault == "default-phases":
            for name in ("--phases", "--ref"):
                index = argv.index(name)
                del argv[index:index + 2]

        fixture.document["devices"] = fixture.document["devices"][:1]
        fixture.document["devices"][0].update(role="anchor", budget_bytes=10 * rtx.GIB)
        fixture.write()
        policy = dict(grants=[dict(uuid=fixture.uuids[0], vram_cap_gib=10, approval="test", window=window(),
                                  gate=[str(Path(__file__).resolve()), "fake"])],
                      caps_gib={fixture.uuids[0].lower(): 10}, host_pinned_gib=0, host_pageable_gib=0,
                      free_commit_floor_gib=4)
        files = fixture.files
        decision = dict(placement=True, machine="local", argv=argv, supervision=policy,
                        files=dict(placement=str(files.placement), manifest=str(files.manifest),
                                   receipts=str(files.receipts), manifest_sha256=None, document=fixture.document))
        evaluation = SimpleNamespace(problems=[], refusal=None, decision=decision, effective_sha256="a" * 64,
                                     effective=SimpleNamespace(untuned=False))
        cluster = SimpleNamespace(machines={"local": SimpleNamespace(access="local", os="windows",
                                                                    install_root=str(tmp_path))})
        before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file() and
                  not (fault == "unreadable" and p == paths[option])}
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
                return launch.prepare(cluster, "decode", launch.Options("fake-placement.json"), router="local",
                                      vram_cap_gib=None, state_dir=tmp_path, source=lambda _: None)
            if fault in ("valid", "builtin"):
                assert prepare().placement_request.argv == tuple(argv)
            else:
                with pytest.raises(RecipeError, match=expected):
                    prepare()
            for call in forbidden:
                call.assert_not_called()
        assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file() and
                          not (fault == "unreadable" and p == paths[option])}
        assert list(tmp_path.iterdir()) == []
    finally:
        fixture.doCleanups()
