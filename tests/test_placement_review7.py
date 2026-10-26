"""Round 7: native replica fidelity and synchronous legacy launch refusals, CPU fakes only."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import uuid

import pytest

from hyperloom import config, replica_spool, rtx
from hyperloom.recipes import runtime, service, session
from tests.recipe_fixtures import isolated
from tests.test_web_recipes import Portal, QWEN


@pytest.mark.parametrize("fault", [None, "tokens", "missing-arm", "duplicate-arm", "short", "rc", "fatal",
                                  "load", "ping", "missing-device", "zero-device", "foreign-device", "counter",
                                  "stale-output", "oracle-pin", "no-oracle", "unpublished-oracle", "release",
                                  "digest", "position", "next-token", "plain", "running"])
def test_3_native_fidelity_uses_pinned_replica_tokens_and_every_device(tmp_path, monkeypatch, fault):
    ids = ["GPU-" + str(uuid.uuid4()) for _ in range(3)]
    keys = [u.lower() for u in ids]
    directory = tmp_path / "spool"
    for name in ("queue", "run", "done", "lock"):
        (directory / name).mkdir(parents=True)
    (directory / "ready.json").write_text('{"event":"ready","load_id":7,"pid":42}', encoding="utf-8")
    (tmp_path / "state.json").write_text('{"state":"ready","pid":42}', encoding="utf-8")
    oracle = {"schema": "hyperloom/replica-oracle-v1", "engine_sha": "1" * 40,
              "chat_prompt": 2, "chat_steps": 2, "chats": [0], "files": {}}
    for name, content in {"state": b"synthetic state", "sequences": b"fixture\t1,2\n",
                          "expected": b'{"event":"chat_end","key":"plain:0#0-0","toks":[3,4,5],"position":4,"next_token":5,"digest":"abc"}\n'}.items():
        path = tmp_path / (name + ".txt")
        path.write_bytes(content)
        oracle["files"][name] = {"file": path.name, "sha256": hashlib.sha256(content).hexdigest()}
    if fault == "oracle-pin":
        oracle["files"]["expected"]["sha256"] = "0" * 64
    if fault != "no-oracle":
        (tmp_path / "replica-oracle.json").write_text(json.dumps(oracle), encoding="utf-8")
    service = {"pid": 42, "spool": str(directory), "ready": {"load_id": 7}, "engine_sha": "1" * 40,
               "argv": ["replica"],
               "placement": {"devices": [{"uuid": u, "lines": [{"component": "drafter"}]} for u in ids[:2]]},
               "placement_supervision": {"caps_gib": dict.fromkeys(keys[:2], 10)}}
    oracle_sha = hashlib.sha256((tmp_path / "replica-oracle.json").read_bytes()).hexdigest() if fault != "no-oracle" else "0" * 64
    monkeypatch.setattr(runtime, "preset", lambda: dict(runtime_preset, engine_sha="1" * 40,
                       fidelity=None if fault == "unpublished-oracle" else {"oracle_sha256": oracle_sha}))
    if fault == "release":
        service["engine_sha"] = "2" * 40
    if fault == "plain":
        for device in service["placement"]["devices"]:
            device["lines"] = []
    native_class = replica_spool.Client
    clients = []
    def client(*args, **kwargs):
        native = mock.Mock(wraps=native_class(*args, **kwargs))
        clients.append(native)
        return native
    monkeypatch.setattr(replica_spool, "Client", client)
    def request(args):
        if args == ["--ping"]:
            return {"rc": 1 if fault == "ping" else 0, "load_id": 7}
        assert "--seqs-file" in args and "--import-state" in args
        assert args[args.index("--phases") + 1] == ("chats0" if fault == "plain" else "chats0,schats0")
        rows = [{"event": "chat_end", "key": key, "toks": [3, 4, 5], "position": 4, "next_token": 5, "digest": "abc"}
                for key in ("plain:0#0-0", "spec:schats0#0-0")]
        if fault == "plain":
            rows.pop()
        if fault in ("digest", "position", "next-token"):
            rows[1][fault.replace("-", "_")] = "def" if fault == "digest" else 999
        if fault == "tokens":
            rows[1]["toks"][1] = 99
        if fault == "short":
            rows[1]["toks"].pop()
        if fault == "missing-arm":
            rows.pop()
        if fault == "duplicate-arm":
            rows.append(rows[0])
        if fault != "stale-output":
            Path(args[args.index("--json") + 1]).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return {"rc": 1 if fault == "rc" else 0, "fatal": fault == "fatal",
                "failures": 0, "load_id": 8 if fault == "load" else 7}
    def worker(_):
        # The actual client publishes LF argv by rename; fake only the native worker, never HTTP.
        queued = next((directory / "queue").glob("*.req"))
        raw = queued.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        args = raw.decode().splitlines()
        running = directory / "run" / queued.name
        queued.rename(running)
        reply = dict(request(args), name=queued.stem)
        temporary = directory / "done" / (queued.stem + ".tmp")
        temporary.write_text(json.dumps(reply), encoding="utf-8")
        temporary.rename(temporary.with_suffix(".json"))
        running.rename(directory / "done" / queued.name)
    monkeypatch.setattr(replica_spool.time, "sleep", worker)
    sample = dict.fromkeys(keys[:2], 1024)
    if fault == "missing-device":
        sample.pop(keys[1])
    if fault == "zero-device":
        sample[keys[1]] = 0
    if fault == "foreign-device":
        sample[keys[2]] = 1
    observer = mock.Mock()
    observer.sample.side_effect = OSError("unattributable") if fault == "counter" else lambda _: sample
    monkeypatch.setattr("hyperloom.gpu_counters.Observer", lambda: observer)
    http = mock.Mock(side_effect=AssertionError("placement must never use HTTP"))
    monkeypatch.setattr(rtx, "_service", mock.Mock(side_effect=AssertionError("legacy oracle")))
    monkeypatch.setattr(rtx, "_running", lambda _: ({}, service))
    try:
        result = rtx.fidelity_gate(SimpleNamespace(), "placement", tmp_path,
                                   service=None if fault == "running" else service, request=http)
    except rtx.ServiceError as exc:
        assert fault in ("oracle-pin", "no-oracle", "unpublished-oracle", "release")
        assert "oracle" in str(exc)
        assert not clients
    else:
        assert result["pass"] is (fault in (None, "plain", "running")), result
        if fault in (None, "plain", "running"):
            assert result["visibility"]["visible_on"] == sorted(keys[:2])
            assert len(result["rows"]) == (1 if fault == "plain" else 2)
            assert observer.sample.call_count >= 2
            assert Path(result["evidence"]).is_file()
        observer.close.assert_called_once()
    http.assert_not_called()


runtime_preset = runtime.preset()


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
            assert "no snapshot" in body["error"]
            status, body = portal.request("POST", "recipes/deploy", {"inputs": digest})
            assert status == 400
            assert set(body) == {"error"}
            assert "changed" in body["error"]
            status, body = portal.request("POST", "up", {"profile": "qwen-replicas", "recipe_inputs": "0" * 64,
                                                        "node": ["halo-1"]})
            assert status == 400
            assert set(body) == {"error"}
            assert "planned snapshot as it is" in body["error"]
            assert portal.api._jobs == {}
            assert portal.api._active is None
            assert not (env.config.parent / "run" / "admission.sqlite3").exists()
            assert not (service.runtime_dir() / "recipe-inputs" / "pins").exists()
        finally:
            portal.close()
