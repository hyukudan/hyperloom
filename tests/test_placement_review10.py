"""Round 10 item 3: complete argv refusal sweep, before admission, with CPU fakes."""
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hyperloom import admission, orchestrate, procs, rtx
from hyperloom.recipes import launch, measurements, preflight, runtime, service, session
from hyperloom.recipes.reader import RecipeError
from tests import test_placement_render
from tests.test_recipes_grants import NOW, window


# Native K is the first digit run, not the optional b budget or f draft count.
PHASES = ("chats65", "chats4294967295", "pressure65", "exact65", "dchats65b0",
          "chats65b0", "pressure4294967295", "wforce65", "wforcep65w", "spec65w",
          "schats65b0f5w", "smoke65", "smokep65", "wsmoke65", "wsmokep65w", "csmoke65", "gsmoke65")
OUTPUTS = ("--json", "--timeline", "--draft-trace", "--controller-save", "--residency-save")
CASES = ([(phase, {}, False) for phase in PHASES]
         + [("chats11", {"max_rows": 6}, False), ("chats9b64", {"max_rows": 8}, False),
            ("pressure33", {"max_rows": 2}, False), ("chats64", {"max_rows": 2}, False),
            ("chats0", {"max_rows": 9}, False), ("chats0", {"max_rows": 0}, False),
            ("wforce0", {"max_rows": 1}, False), ("schats0", {"max_rows": 7}, False),
            ("chats0", {"max_context_tokens": 239}, False),
            ("chats64", {"max_rows": 1}, True), ("chats10b4294967295", {"max_rows": 6}, True),
            ("chats8", {"max_rows": 8}, True), ("chats0", {"max_context_tokens": 240}, True),
            ("chats9", {"max_rows": 1, "allocation_flags": {"--predict": 8}}, False),
            ("chats0", {"max_rows": 2, "allocation_flags": {"--predict": 33}}, False),
            ("chats32", {"max_rows": 2, "allocation_flags": {"--predict": 32}}, True),
            ("exact0", {"argv": ["--steps", "63"]}, False),
            ("chats0", {"argv": ["--controller-prior", "none", "--controller-costs", "1,2,3,4,5,6"]}, False),
            ("chats0", {"argv": ["--controller-prior", "none", "--controller-costs", "none"]}, True),
            ("exact0", {"max_context_tokens": 79}, False),
            ("exact0", {"max_context_tokens": 240}, True),
            ("chats0", {"argv": ["--verify", ""]}, False),
            ("chats0", {"argv": ["--verify", "1\x00"]}, False),
            ("chats0", {"argv": ["--verify", "1\n"]}, False),
            ("chats0", {"argv": ["--verify", "1\r"]}, False),
            ("chats0", {"argv": ["--seqmod", "5"]}, False),
            ("chats0", {"argv": ["--seqmod", "5:0:ignored"]}, True)]
         + [(phase, {}, False) for phase in ("smoke0", "smokep0", "wsmoke0", "wsmokep0", "csmoke0",
            "gsmoke0", "spec0", "schats0", "unknown", "chats", "chats0b", "schats0f0", "schats0f6",
            "schats0f", "exact0w", "chats0w", "pressure0w", "chats0b4294967296")]
         + [("chats0", {"argv": [option, bad, option, "<valid-output>"]}, False)
            for option in OUTPUTS for bad in ("<missing-output>", "<output-directory>")]
         + [("chats0", {"argv": [option, "<valid-output>"]}, True) for option in OUTPUTS]
         + [("chats0", {"argv": [option, "<valid-output>"], "deny_output": True, "existing_output": exists}, False)
            for option in OUTPUTS for exists in (False, True)]
         + [("chats0", {"argv": ["--residency-save", "<valid-output>"], "atomic_directory": True}, False)]
         + [("chats0", {"argv": tokens, "code": "PL_E_CONFLICT"}, False) for tokens in
            (["--cache", "other", "--cache", "tierstream"], ["--cache", "tierstream"], ["--cache=tierstream"])]
         + [pytest.param("chats" + "0" * 5000, {"max_rows": 8}, True, id="leading-zero-K"),
            pytest.param("chats" + "0" * 5000 + "65", {"max_rows": 1}, False, id="leading-zero-invalid-K")])


@pytest.mark.parametrize("phase,config,valid", CASES)
@pytest.mark.parametrize("repeat", (False, True))
def test_3_phase_capacities_before_admission(tmp_path, monkeypatch, phase, config, valid, repeat):
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        argv = list(fixture.argv)
        index = argv.index("--phases")
        del argv[index:index + 2]
        argv.extend(("--ref", str(fixture.root / "import-state"), "--phases", phase))
        if repeat:
            argv.extend(("--phases", "chats0"))
        config = dict(config)
        output = fixture.root / "future-output"
        if config.pop("existing_output", False):
            output.write_bytes(b"preserve existing output")
        if config.pop("atomic_directory", False):
            Path(str(output) + ".tmp").mkdir()
        code = config.pop("code", "PL_E_SCHEMA")
        argv.extend(token.replace("<valid-output>", str(output))
                    .replace("<missing-output>", str(fixture.root / "absent" / "future-output"))
                    .replace("<output-directory>", str(fixture.root)) for token in config.pop("argv", []))
        if config.pop("deny_output", False):
            import os
            access = os.access
            monkeypatch.setattr(os, "access", lambda path, mode: False if Path(path) in (output, output.parent)
                                else access(path, mode))
        allocation_flags = config.pop("allocation_flags", None)
        fixture.document["config"] = config
        fixture.document["devices"] = fixture.document["devices"][:1]
        fixture.document["devices"][0].update(role="anchor", budget_bytes=10 * rtx.GIB)
        fixture.write()
        policy = dict(grants=[dict(uuid=fixture.uuids[0], vram_cap_gib=10, approval="test", window=window(),
                                  gate=[str(Path(__file__).resolve()), "fake"])],
                      caps_gib={fixture.uuids[0].lower(): 10}, host_pinned_gib=0, host_pageable_gib=0,
                      free_commit_floor_gib=4)
        decision = dict(placement=True, machine="local", argv=argv, supervision=policy,
                        files=dict(placement=str(fixture.files.placement), manifest=str(fixture.files.manifest),
                                   receipts=str(fixture.files.receipts), manifest_sha256=None, document=fixture.document,
                                   allocation_flags=allocation_flags))
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
                return launch.prepare(cluster, "decode", launch.Options("fake-placement.json"), router="local",
                                      vram_cap_gib=None, state_dir=tmp_path, source=lambda _: None)
            if valid:
                assert prepare().placement_request.argv == tuple(argv)
            else:
                match = "bad native value of --phases" if phase in PHASES else code
                with pytest.raises(RecipeError, match=match):
                    prepare()
            for call in forbidden:
                call.assert_not_called()
        assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        assert list(tmp_path.iterdir()) == []
    finally:
        fixture.doCleanups()


def test_3_fingerprint_covers_helpers_and_transitive_headers(tmp_path):
    from tools.check_replica_args import inventory, PARSER_HELPERS

    for name in ("apps/cuda_ds41_replica.cu", "apps/cuda_ds41_replica_serve.hpp", *PARSER_HELPERS):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// synthetic parser dependency\n", encoding="utf-8", newline="\n")
    main = tmp_path / "apps/cuda_ds41_replica.cu"
    main.write_text('#include "strix/first.hpp"\nconst auto parse_args = x;\n'
                    'if (a == "--phases") phases_arg = next();\n'
                    'const std::vector<std::string> launch_args;\n', encoding="utf-8", newline="\n")
    serve = tmp_path / "apps/cuda_ds41_replica_serve.hpp"
    serve.write_text('inline bool is_request_env() { return name == "ENV";\n}\n', encoding="utf-8", newline="\n")
    first, second = (tmp_path / "include/strix" / name for name in ("first.hpp", "second.hpp"))
    first.parent.mkdir(parents=True)
    first.write_text('#include "second.hpp"\n', encoding="utf-8", newline="\n")
    second.write_text("// nested parser rule\n", encoding="utf-8", newline="\n")
    original = inventory(tmp_path)
    # A rule change in any helper/header must invalidate the same pinned fingerprint as a main-parser edit.
    for name in original["files"]:
        path = tmp_path / name
        content = path.read_bytes()
        path.write_bytes(content + b"// changed refusal\n")
        assert inventory(tmp_path) != original, name
        path.write_bytes(content)
    second.write_bytes(second.read_bytes().replace(b"\n", b"\r\n"))
    assert inventory(tmp_path) == original  # the audit is deliberately LF-normalized
    second.unlink()
    assert inventory(tmp_path) != original


@pytest.mark.parametrize("flags,phase,rows,valid", [
    ({"--predict": 8}, "chats9", 1, False), ({"--predict": 2}, "chats8", 1, True),
    ({"--predict": 9}, "chats9", 1, True), ({"--predict": 33}, "chats0", 2, False),
    ({"--predict": 32}, "chats32", 2, True), ({}, "chats9", 1, False)])
def test_3_cached_predict_limit_survives_decision_serialization(monkeypatch, flags, phase, rows, valid):
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        fixture.document["config"] = dict(max_rows=rows)
        fixture.write()
        files = dict(placement=str(fixture.files.placement), manifest=str(fixture.files.manifest),
                     receipts=str(fixture.files.receipts), manifest_sha256=None, document=fixture.document,
                     allocation_flags=flags)
        decision = json.loads(json.dumps(dict(machine="local", files=files, supervision={}, argv=[
            *fixture.argv, "--phases", phase])))
        monkeypatch.setattr(runtime, "preset", lambda: fixture.data)
        request = runtime.request(decision)
        if valid:
            runtime.render(request.files, binary="fake", argv=request.argv)
        else:
            with pytest.raises(RecipeError, match="PL_E_SCHEMA.*(?:phases|predict)"):
                runtime.render(request.files, binary="fake", argv=request.argv)
    finally:
        fixture.doCleanups()


def test_3_decision_uses_the_admitted_allocation_reference(monkeypatch):
    from hyperloom.recipes import commands, content_cache

    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    try:
        rid, sha, build = "decode", "a" * 64, "b" * 64
        plan = fixture.document
        plan.update(route=rid, effective_sha256=sha, build_sha256=build, config=dict(max_rows=1))
        for device in plan["devices"]:
            device["host"] = "local"
        manifest_sha = content_cache._sha(fixture.manifest)
        (fixture.root / (manifest_sha + ".json")).write_bytes(fixture.files.manifest.read_bytes())
        reference = dict(build_sha256=build, receipts_sha256=plan["receipts_sha256"], flags={"--predict": 8})
        record = dict(reference=reference, manifest_sha256=manifest_sha, selected=dict(model=fixture.argv[0]))
        cache = mock.Mock(manifests=SimpleNamespace(directory=fixture.root))
        cache.record.return_value = record
        argv = [*fixture.argv[1:], "--phases", "chats9"]
        admitted = dict(inputs=dict(routes={rid: dict(components=reference, receipts=str(fixture.files.receipts),
                                                    selected_content=record["selected"], argv=argv)}))
        recipe = dict(routes=[dict(id=rid, phase="decode", components=dict(arena=dict(units=1)))])
        monkeypatch.setattr(runtime, "preset", lambda: fixture.data)
        monkeypatch.setattr(commands, "components_store", lambda: cache)
        monkeypatch.setattr(commands, "runtime_dir", lambda: fixture.root / "unpublished")
        monkeypatch.setattr(runtime, "executable", lambda *a: Path("fake"))
        before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        with mock.patch.object(runtime.Files, "publish", side_effect=AssertionError("publish")), \
                mock.patch.object(procs, "LocalChild", side_effect=AssertionError("child")), \
                pytest.raises(RecipeError, match="PL_E_SCHEMA.*cached --predict"):
            runtime.decision(None, recipe, SimpleNamespace(pc="local"), None, sha, {rid: plan}, admitted)
        cache.record.assert_called_once_with(reference, selected=record["selected"])
        assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        assert not (fixture.root / "unpublished").exists()
    finally:
        fixture.doCleanups()
