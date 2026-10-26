"""CPU-only spool regressions."""

from types import SimpleNamespace
from unittest import mock
import sys

import pytest

from hyperloom import rtx
from hyperloom.recipes import content_cache, reader, runtime


def test_5_native_replica_spool_round_trip(tmp_path):
    from hyperloom import replica_spool
    from tests import test_placement_render, test_rtx
    from tests.replica_fake import FakeReplica
    data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION)
    files = SimpleNamespace(read=lambda: {"engine": data["engine"], "devices": []},
                            argv=lambda: ["--placement", "plan"])
    with pytest.raises(reader.RecipeError, match="PL_E_CAPABILITY.*placement"):
        runtime.render(files, binary="replica", argv=["model"], data=data)
    spool = replica_spool.Client(tmp_path, alive=lambda: True)
    for name in ("queue", "run", "done", "lock"):
        (tmp_path / name).mkdir()
    (tmp_path / "ready.json").write_text('{"event":"ready","load_id":7}', encoding="utf-8")
    assert spool.ready()["load_id"] == 7
    def worker():
        request = next((tmp_path / "queue").glob("*.req"))
        args = request.read_text().splitlines()
        request.rename(tmp_path / "run" / request.name)
        content_cache._write(tmp_path / "done" / (request.stem + ".json"),
                             dict(name=request.stem, rc=0, args=args))
        (tmp_path / "run" / request.name).rename(tmp_path / "done" / request.name)
    with mock.patch("hyperloom.replica_spool.time.sleep", side_effect=lambda _: worker()):
        assert spool.request(["--ping"])["args"] == ["--ping"]
        assert spool.request(["--phases", "chats0", "--chat-steps", "2"])["rc"] == 0
        assert spool.request(["--quit"])["args"] == ["--quit"]
    with pytest.raises(ValueError):
        spool.request(["--phases\n--quit"])
    fixture = test_placement_render.PlacementRenderTests()
    fixture.setUp()
    helper = test_rtx.HelperTests()
    helper.setUp()
    try:
        fixture.document["devices"][0].update(role="anchor", budget_bytes=10 * rtx.GIB)
        fixture.document["devices"] = fixture.document["devices"][:1]
        fixture.write()
        from tests.test_recipes_grants import NOW, window
        policy = dict(grants=[dict(uuid=fixture.uuids[0], vram_cap_gib=10, approval="test", window=window(),
                                  gate=[sys.executable, "fake-gate"])],
                      caps_gib={fixture.uuids[0].lower(): 10}, caps_bytes={fixture.uuids[0].lower(): 10 * rtx.GIB},
                      host_pinned_gib=0, host_pageable_gib=0,
                      free_commit_floor_gib=4)
        cluster = SimpleNamespace(machines={"local": SimpleNamespace(access="local", os="windows",
                                                                       install_root=str(tmp_path))})
        request = runtime.Request("local", fixture.files, policy, argv=tuple(fixture.argv))
        with mock.patch.object(runtime, "preset", return_value=fixture.data), mock.patch.object(rtx.time, "time", return_value=NOW):
            spec = rtx.render_placement_service(cluster, request, api_key="0" * 64)
        assert spec.argv[1] == str(fixture.root / "model") and spec.replica_spool
        assert not {"--host", "--port", "--api-key"} & set(spec.argv)
        assert spec.argv.count("--placement") == spec.argv.count("--content-manifest") == spec.argv.count("--receipts") == 1
        # Exercise the real helper protocol; only process/GPU supervision is faked.
        spec = test_rtx._binary_spec(argv=[sys.executable, *spec.argv[1:]], replica_spool=True)
        FakeReplica.instances = []
        thread = helper.run_helper(helper.write(spec), child_factory=FakeReplica,
            health=mock.Mock(side_effect=AssertionError("native replica has no HTTP health")))
        try:
            assert helper.wait_state("ready")["ready"]["event"] == "ready"
            client = replica_spool.Client(helper.directory / "spool", alive=FakeReplica.instances[0].alive)
            assert client.request(["--not-runtime"])["rc"] == 2
            assert client.request(["--phases", "chats0"])["rc"] == 0
        finally:
            (helper.directory / "stop").touch()
            thread.join(5)
        assert not thread.is_alive()
        assert ["--ping"] in FakeReplica.instances[0].requests
        assert ["--quit"] in FakeReplica.instances[0].requests
    finally:
        fixture.doCleanups()
        helper.doCleanups()
