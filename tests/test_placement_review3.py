"""CPU-only reproductions for the four residual round-2 review findings."""
import ctypes as C
import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from hyperloom import gpu_counters
from hyperloom.recipes import commands, content_cache, core, reader, runtime, session
from tests import test_recipes
from tests.test_placement_privacy import private


# Verified against CUDA 13.0 headers and the installed DLL export tables, without loading either DLL.
NVML_EXPORTS = ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2",
                "nvmlDeviceGetUUID", "nvmlDeviceGetHandleByPciBusId_v2",
                "nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2")
CUDA_EXPORTS = ("cuInit", "cuDeviceGetCount", "cuDeviceGet", "cuDeviceGetLuid", "cuDeviceGetPCIBusId")


@pytest.mark.parametrize("address", ["192.0.2.10", "127.0.0.1", "::1", "2001:db8::1",
                                     "fe80::abcd%test", "::ffff:192.0.2.1",
                                     "2001:0db8:0000:0000:0000:0000:0000:0001"])
def test_11_addresses_are_private_in_values_keys_and_endpoints(address):
    for value in (address, f"address {address}.", {"endpoint": f"http://[{address}]:8096/v1"},
                  {address: {"bytes": 42}}):
        assert private(value)


def test_11_address_placeholders_round_trip_original_bytes():
    from tests.portable_fixtures import restore, sanitize
    raw = '"http://192.0.2.10:8096", "192.0.2.1", "[2001:db8::1]", "[::1]"\n'
    portable = sanitize(raw)
    assert not private(portable)
    assert restore(portable).encode() == raw.encode()


def test_15_supported_exports_map_luids_by_pci_not_ordinal(monkeypatch):
    nvml = mock.Mock(spec_set=NVML_EXPORTS)
    cuda = mock.Mock(spec_set=CUDA_EXPORTS)
    nvml.nvmlInit_v2.return_value = nvml.nvmlShutdown.return_value = 0
    def count(out):
        out._obj.value = 2
        return 0
    def device(out, ordinal):
        out._obj.value = ordinal + 10
        return 0
    def pci(out, size, dev):
        out.value = f"{0:04x}:{dev.value:02x}:{0:02x}.{0}".encode()
        return 0
    def handle(pci, out):
        out._obj.value = int(pci[5:7], 16)
        return 0
    def identity(handle, out, size):
        out.value = f"gpu-test-{handle.value}".encode()
        return 0
    def luid(out, mask, dev):
        C.memmove(out, dev.value.to_bytes(4, "little") + (9).to_bytes(4, "little"), 8)
        mask._obj.value = 1
        return 0
    nvml.nvmlDeviceGetUUID.side_effect = identity
    nvml.nvmlDeviceGetHandleByPciBusId_v2.side_effect = handle
    cuda.cuInit.return_value = 0
    cuda.cuDeviceGetCount.side_effect = count
    cuda.cuDeviceGet.side_effect = device
    cuda.cuDeviceGetPCIBusId.side_effect = pci
    cuda.cuDeviceGetLuid.side_effect = luid
    monkeypatch.setattr(C, "CDLL", lambda _: nvml)
    monkeypatch.setattr(C, "WinDLL", lambda _: cuda, raising=False)
    observer = gpu_counters.Nvml()
    # Reversed NVML inventory, unrelated to CUDA ordinals.
    monkeypatch.setattr(observer, "devices", lambda: [("gpu-test-11", C.c_void_p(11)),
                                                       ("gpu-test-10", C.c_void_p(10))])
    assert observer.luids() == {(9, 10): "gpu-test-10", (9, 11): "gpu-test-11"}
    cuda.cuInit.assert_called_once_with(0)
    assert cuda.cuDeviceGetLuid.argtypes == [C.POINTER(C.c_char), C.POINTER(C.c_uint), C.c_int]
    assert cuda.cuDeviceGetPCIBusId.argtypes == [C.POINTER(C.c_char), C.c_int, C.c_int]
    assert nvml.nvmlDeviceGetHandleByPciBusId_v2.argtypes == [C.c_char_p, C.POINTER(C.c_void_p)]
    for failure in ("duplicate", "missing", "zero-mask", "api"):
        if failure == "duplicate":
            cuda.cuDeviceGet.side_effect = lambda out, ordinal: device(out, 0)
        elif failure == "missing":
            cuda.cuDeviceGet.side_effect = device
            cuda.cuDeviceGetCount.side_effect = lambda out: (setattr(out._obj, "value", 1) or 0)
        elif failure == "zero-mask":
            cuda.cuDeviceGetCount.side_effect = count
            cuda.cuDeviceGetLuid.side_effect = lambda out, mask, dev: 0
        else:
            cuda.cuInit.return_value = 3
        with pytest.raises(gpu_counters.CounterError):
            observer.luids()


@pytest.mark.parametrize("layer_kind", ["sets", "overrides", "profile", "stale-profile", "legacy"])
@pytest.mark.parametrize("selection", ["direct", "auto"])
def test_16_effective_layer_selects_placement_and_policy(tmp_path, monkeypatch, layer_kind, selection):
    doc = test_recipes.PlacementSchemaTests.document()
    route = doc["routes"][0]
    for key in ("assist", "components", "max_rows", "max_sequences"):
        route.pop(key, None)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    sets = [("routes.decode.components.drafter.device", "anchor")]
    layers = session.Layers(sets=sets)
    if layer_kind == "overrides":
        override = tmp_path / "override.json"
        override.write_text(json.dumps({"schema": reader.OVERRIDES_SCHEMA, "recipe": doc["name"],
                                        "set": dict(sets)}), encoding="utf-8")
        layers = session.Layers(overrides=[override])
    binding = {"anchor": [dict(id="synthetic-anchor", host="local")], "aux": []}
    report = dict(engine_sha256="a" * 64, model_sha256="b" * 64, devices=[dict(
        id="synthetic-anchor", driver="test", disk_read_gbytes_ps=10, h2d_gbytes_ps=20)])
    descriptors = {"anchor": ["c" * 64]}
    if layer_kind in ("profile", "stale-profile"):
        profile = dict(schema=reader.PROFILE_SCHEMA, recipe=doc["name"], workload="default",
            recipe_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), key=dict(
                engine_sha256=report["engine_sha256"], model_sha256=report["model_sha256"],
                devices={"anchor": ["synthetic-anchor"], "aux": {"unbound": True}},
                drivers={"anchor": "test", "aux": {"unbound": True}}, descriptors=descriptors),
            probe={"anchor": dict(disk_read_gbytes_ps=10, h2d_gbytes_ps=20), "aux": {"unbound": True}},
            set=dict(sets))
        if layer_kind == "stale-profile":
            profile["key"]["engine_sha256"] = "d" * 64
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        layers = session.Layers(profile=profile_path)
    elif layer_kind == "legacy":
        layers = session.Layers(sets=[("routes.decode.placement.anchor.device_gib", 89)])
    backend = core.CoreBackend(core.Core("fake"))
    backend.resolve = core.StubBackend().resolve
    backend.match = mock.Mock(return_value=([], binding, "a" * 64))
    ctx = SimpleNamespace(pc="local", profile="decode", model="synthetic")
    monkeypatch.setattr(session.facts, "context", lambda *args: ctx)
    legacy_bind = mock.Mock(return_value={"binding": binding})
    if layer_kind == "profile":
        legacy_bind.side_effect = AssertionError("CUDA-only profile activation must not use legacy HIP binding")
    monkeypatch.setattr(session.apply, "bind", legacy_bind)
    monkeypatch.setattr(session.devices, "candidates", lambda *args: report)
    monkeypatch.setattr(session.apply, "probe_report", lambda *args, **kw: report)
    monkeypatch.setattr(session.apply, "descriptors", lambda *args: descriptors)
    monkeypatch.setattr(session.facts, "availability", lambda *args: {})
    monkeypatch.setattr(session.apply, "deploy", lambda *args, **kw: {})
    source = mock.Mock(return_value=({}, {}, {}, []))
    source.placement = False
    observed = []
    source.side_effect = lambda _: (observed.append(source.placement) or ({}, {}, {}, []))
    chosen = SimpleNamespace(ok=True, effective=SimpleNamespace(recipe=reader.parse(json.dumps(doc))))
    placement = mock.Mock(return_value=chosen)
    monkeypatch.setattr(session, "_placement_evaluation", placement)
    if selection == "auto":
        result, _ = session.pick(None, "decode", [tmp_path], layers, source, backend)
    else:
        result = session.evaluate(None, "decode", path, layers, source, backend)
    if layer_kind in ("stale-profile", "legacy"):
        placement.assert_not_called()
        assert observed == [False]
        assert result.decision == {}
        assert result.effective.untuned == (layer_kind == "stale-profile")
        return
    assert result is chosen
    assert observed == ([False, True] if layer_kind == "profile" else [True])
    assert reader.placement_mode(placement.call_args.args[5]["routes"][0])


def test_17_corrupt_executable_refuses_in_pure_decision(tmp_path, monkeypatch):
    data = dict(runtime.preset(), engine_sha="1" * 40)
    binary = tmp_path / "bin" / (data["binary"] + ".exe")
    binary.parent.mkdir()
    good = b"CPU-only engine fixture"
    digest = hashlib.sha256(good).hexdigest()
    binary.write_bytes(good)
    content_cache._write(binary.parent.parent / "MANIFEST.json", {
        "engine_commit": data["engine_sha"], "binaries": {binary.name: digest}})
    recipe = reader.parse(json.dumps(test_recipes.PlacementSchemaTests.document()))
    plan = dict(engine=data["engine"], effective_sha256="a" * 64, route="decode", devices=[],
                build_sha256=digest, receipts_sha256="b" * 64)
    cache = SimpleNamespace(record=lambda *a, **kw: dict(reference=dict(build_sha256=digest,
        receipts_sha256="b" * 64), manifest_sha256="c" * 64, selected={"model": "model"}),
        manifests=SimpleNamespace(directory=tmp_path))
    monkeypatch.setattr(runtime, "preset", lambda: data)
    monkeypatch.setattr(runtime, "executable", lambda *args: binary)
    monkeypatch.setattr(runtime, "render", lambda *args, **kw: None)
    monkeypatch.setattr(commands, "components_store", lambda: cache)
    monkeypatch.setattr(commands, "runtime_dir", lambda: tmp_path)
    admitted = dict(inputs={"routes": {"decode": dict(components={}, selected_content={}, receipts="receipts")}},
                    policies={"decode": {}})
    def decide():
        return runtime.decision(None, recipe, SimpleNamespace(pc="local"), {}, "a" * 64,
                                {"decode": plan}, admitted)
    assert decide()["placement"]
    before = set(tmp_path.rglob("*"))
    binary.write_bytes(b"X" * len(good))
    with pytest.raises(reader.RecipeError, match="PL_E_STALE.*binary"):
        decide()
    assert set(tmp_path.rglob("*")) == before
