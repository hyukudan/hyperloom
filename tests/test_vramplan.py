"""VRAM placement planner: header readers, the planner, and each family's memory model."""
import gzip
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from hyperloom import modelfiles, vramplan
from hyperloom.placement import ds41, minimax, qwen
from hyperloom.vramplan import GIB, Inputs, Model, PlanError, Unit

FIXTURES = Path(__file__).parent / "fixtures" / "vramplan"


def write_gguf(path, tensors, alignment=32):
    """A GGUF v3 header with (name, dims, ggml type) tensors and zero-filled data at aligned offsets."""
    def string(s):
        b = s.encode()
        return struct.pack("<Q", len(b)) + b
    head = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), 1)
    head += string("general.alignment") + struct.pack("<I", 4) + struct.pack("<I", alignment)
    offset, infos = 0, b""
    sizes = []
    for name, dims, kind in tensors:
        _, block, per = modelfiles.GGML_TYPES[kind]
        n = 1
        for d in dims:
            n *= d
        size = n // block * per
        infos += string(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims)
        infos += struct.pack("<IQ", kind, offset)
        sizes.append(size)
        offset += -(-size // alignment) * alignment
    data = head + infos
    data += b"\0" * (-len(data) % alignment)
    Path(path).write_bytes(data + b"\0" * offset)
    return sizes


def write_safetensors(path, tensors):
    header, offset = {}, 0
    for name, dtype, shape, size in tensors:
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    raw = json.dumps(header).encode()
    Path(path).write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * offset)


class ReaderTests(unittest.TestCase):
    def test_gguf_sizes_from_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.gguf"
            sizes = write_gguf(path, [("a", [256, 4], 10), ("b", [32, 3], 39), ("c", [7], 0)])
            meta, tensors = modelfiles.read(path)
        self.assertEqual(meta["general.alignment"], 32)
        self.assertEqual([t.bytes for t in tensors], sizes)
        self.assertEqual(sizes, [4 * 84, 3 * 17, 28])  # Q2_K 84 B/256, MXFP4 17 B/32, F32
        self.assertEqual([t.dtype for t in tensors], ["Q2_K", "MXFP4", "F32"])

    def test_gguf_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.gguf"
            path.write_bytes(b"NOPE" + b"\0" * 64)
            with self.assertRaises(modelfiles.ModelFileError):
                modelfiles.read(path)

    def test_split_gguf_reads_every_shard(self):
        with tempfile.TemporaryDirectory() as d:
            write_gguf(Path(d) / "m-00001-of-00002.gguf", [("a", [32], 0)])
            write_gguf(Path(d) / "m-00002-of-00002.gguf", [("b", [64], 0)])
            _, tensors = modelfiles.read(Path(d) / "m-00002-of-00002.gguf")
        self.assertEqual([(t.name, t.bytes) for t in tensors], [("a", 128), ("b", 256)])

    def test_gguf_metadata_arrays_are_skipped_and_nesting_refused(self):
        def string(s):
            b = s.encode()
            return struct.pack("<Q", len(b)) + b
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.gguf"
            vocab = struct.pack("<IQ", 8, 2) + string("a") + string("bc")
            path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + string("tokens") + struct.pack("<I", 9) + vocab)
            meta, _ = modelfiles.read(path)
            self.assertEqual(meta["tokens"], {"array": 2})
            path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + string("x") + struct.pack("<IIQ", 9, 9, 1))
            with self.assertRaises(modelfiles.ModelFileError):
                modelfiles.read(path)

    def test_gguf_alignment_must_be_a_power_of_two(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in (0, 24):
                path = Path(d) / f"a{bad}.gguf"
                write_gguf(path, [("a", [32], 0)], alignment=32)
                raw = bytearray(path.read_bytes())
                at = raw.index(b"general.alignment") + len(b"general.alignment") + 4
                raw[at:at + 4] = struct.pack("<I", bad)
                path.write_bytes(bytes(raw))
                with self.assertRaises(modelfiles.ModelFileError):
                    modelfiles.read(path)

    def test_safetensors_offsets_are_validated(self):
        with tempfile.TemporaryDirectory() as d:
            for entry in ({"dtype": "F32", "shape": [1], "data_offsets": [100, 0]},
                          {"dtype": "F32", "shape": [1], "data_offsets": [0, 10 ** 9]},
                          ["not", "a", "dict"]):
                raw = json.dumps({"x": entry}).encode()
                path = Path(d) / "a.safetensors"
                path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 16)
                with self.assertRaises(modelfiles.ModelFileError):
                    modelfiles.read(path)

    def test_split_gguf_needs_the_exact_shard_pattern(self):
        with tempfile.TemporaryDirectory() as d:
            write_gguf(Path(d) / "m-00001-of-00002.gguf", [("a", [32], 0)])
            write_gguf(Path(d) / "m-00002-of-00002.gguf", [("b", [64], 0)])
            write_gguf(Path(d) / "m-00001-of-00003.gguf", [("c", [64], 0)])  # another split, same prefix
            write_gguf(Path(d) / "m-extra-of-x.gguf", [("d", [64], 0)])
            _, tensors = modelfiles.read(Path(d) / "m-00001-of-00002.gguf")
            self.assertEqual([t.name for t in tensors], ["a", "b"])
            _, tensors = modelfiles.read(Path(d) / "m-extra-of-x.gguf")
            self.assertEqual([t.name for t in tensors], ["d"])

    def test_safetensors_directory(self):
        with tempfile.TemporaryDirectory() as d:
            write_safetensors(Path(d) / "a.safetensors", [("x", "BF16", [4, 2], 16), ("y", "F32", [3], 12)])
            _, tensors = modelfiles.read(d)
        self.assertEqual([(t.name, t.bytes, t.shape) for t in tensors], [("x", 16, (4, 2)), ("y", 12, (3,))])


def toy(order="prefix", stream=None, units=10, size=GIB, host="pinned"):
    return Model(engine="toy", fixed=[("context", GIB // 2)],
                 units=[Unit(f"u:{i}", size, benefit=float(units - i), host=host) for i in range(units)],
                 order=order, flags=lambda keys, plan: {"--resident": len(keys)}, stream=stream)


class PlannerTests(unittest.TestCase):
    def test_prefix_fills_up_to_cap_minus_margin(self):
        p = vramplan.plan(toy(), Inputs(8, margin_gib=1, margin_fraction=0))
        self.assertEqual(p.resident, [f"u:{i}" for i in range(6)])  # 8 - 1 margin - 0.5 fixed = 6.5
        self.assertEqual(p.flags, {"--resident": 6})
        self.assertLessEqual(p.peak_bytes, 7 * GIB)
        self.assertEqual(p.pinned_bytes, 4 * GIB)

    def test_stream_slots_are_reserved_only_when_something_streams(self):
        model = toy(stream=lambda streamed: 2 * max(u.bytes for u in streamed))
        p = vramplan.plan(model, Inputs(8, margin_gib=1, margin_fraction=0))
        self.assertEqual(len(p.resident), 4)  # 6.5 room - 2 slots
        self.assertEqual(p.stream_bytes, 2 * GIB)
        full = vramplan.plan(model, Inputs(12, margin_gib=1, margin_fraction=0))
        self.assertEqual((len(full.resident), full.stream_bytes), (10, 0))

    def test_three_tiers_spill_from_pinned_to_nvme(self):
        model = toy(host="pinned|disk")  # 10 units of 1 GiB, benefit 10..1
        p = vramplan.plan(model, Inputs(8, host_gib=2, margin_gib=1, margin_fraction=0))
        self.assertEqual(len(p.resident), 6)
        self.assertEqual(p.tiers, {"u:6": "pinned", "u:7": "pinned", "u:8": "disk", "u:9": "disk"})
        self.assertEqual((p.pinned_bytes, p.disk_bytes), (2 * GIB, 2 * GIB))
        self.assertEqual(p.to_dict()["tier_units"], {"disk": 2, "pinned": 2})
        unlimited = vramplan.plan(model, Inputs(8, margin_gib=1, margin_fraction=0))
        self.assertEqual(set(unlimited.tiers.values()), {"pinned"})

    def test_pinned_only_units_take_the_host_budget_first(self):
        model = toy(host="pinned|disk")
        model.units[9] = Unit("u:9", GIB, 0.0, host="pinned")
        p = vramplan.plan(model, Inputs(8, host_gib=2, margin_gib=1, margin_fraction=0))
        self.assertEqual(p.tiers, {"u:6": "pinned", "u:7": "disk", "u:8": "disk", "u:9": "pinned"})

    def test_ranked_takes_best_benefit_per_byte(self):
        model = toy(order="ranked")
        model.units = [Unit("a", GIB, 1.0), Unit("b", GIB, 3.0), Unit("c", GIB, 3.0), Unit("d", 2 * GIB, 4.0)]
        p = vramplan.plan(model, Inputs(3.5, margin_gib=1, margin_fraction=0))
        self.assertEqual(p.resident, ["b", "c"])  # ties keep model order; d is 2 B/byte

    def test_fixed_fits_but_fixed_plus_slots_does_not(self):
        for order in ("prefix", "ranked"):
            model = toy(order=order, stream=lambda streamed: 2 * GIB)
            with self.assertRaises(PlanError):  # room 1.5 GiB: no unit, and the 2 GiB of slots do not fit either
                vramplan.plan(model, Inputs(3, margin_gib=1, margin_fraction=0))

    def test_check_peak(self):
        vramplan.check_peak("x", 7 * GIB, 8 * GIB, GIB)
        with self.assertRaises(PlanError):
            vramplan.check_peak("x", 7 * GIB + 1, 8 * GIB, GIB)

    def test_fixed_above_cap_is_refused(self):
        with self.assertRaises(PlanError):
            vramplan.plan(toy(), Inputs(1, margin_gib=1))

    def test_host_budget_is_enforced(self):
        with self.assertRaises(PlanError):
            vramplan.plan(toy(), Inputs(4, host_gib=2, margin_gib=1, margin_fraction=0))
        p = vramplan.plan(toy(host="disk"), Inputs(4, host_gib=2, margin_gib=1, margin_fraction=0))
        self.assertEqual((p.pinned_bytes, p.disk_bytes), (0, 8 * GIB))

    def test_deterministic_and_monotonic_in_the_cap(self):
        counts = []
        for cap in range(3, 14):
            a = vramplan.plan(toy(), Inputs(cap))
            b = vramplan.plan(toy(), Inputs(cap))
            self.assertEqual(a.to_dict(), b.to_dict())
            counts.append(len(a.resident))
        self.assertEqual(counts, sorted(counts))

    def test_explain_names_flags_and_peak(self):
        text = vramplan.explain(vramplan.plan(toy(), Inputs(8)))
        self.assertIn("--resident", text)
        self.assertIn("predicted peak", text)

    def test_plan_first_reports_every_refusal(self):
        small, big = toy(units=1), toy(units=1)
        big.fixed = [("context", 100 * GIB)]
        self.assertIs(vramplan.plan_first([big, small], Inputs(8)).engine, "toy")
        with self.assertRaises(PlanError) as ctx:
            vramplan.plan_first([big, big], Inputs(8))
        self.assertEqual(str(ctx.exception).count("exceeds"), 2)


def minimax_index():
    with gzip.open(FIXTURES / "minimax-header-subset.json.gz") as f:
        rows = json.load(f)["tensors"]
    return {n: modelfiles.Tensor(n, b, "x", d, tuple(s)) for n, b, d, s in rows}


class MiniMaxTests(unittest.TestCase):
    def test_blob_bytes_equal_the_engines_builder(self):
        # mm-rtx-prefill 736d1361 build_layer over the real header: dense 237,834,240 B, MoE 2,864,462,336 B.
        index = minimax_index()
        self.assertEqual(minimax.layer_bytes(index, 0), 237834240)
        self.assertEqual(minimax.layer_bytes(index, 3), 2864462336)
        self.assertEqual(minimax.layer_bytes(index, 59), 2864462336)  # model.norm instead of the next input norm

    def test_kv_state_per_row(self):
        run = minimax.runtime_bytes(8193)
        raw = 8192 * minimax.LAYERS * (1024 + 1024 + 256)  # K, V, index keys
        self.assertLessEqual(raw, run["kv_state"])
        self.assertLess(run["kv_state"] - raw, 3 * minimax.CUDA_GRANULE)

    def _model(self, positions=8192, layers=60):
        index = minimax_index()
        sizes = [minimax.layer_bytes(index, 0 if L < 3 else 3) for L in range(layers)]
        with mock.patch.object(minimax, "layer_bytes", side_effect=lambda idx, L: sizes[L]), \
                mock.patch.object(minimax.modelfiles, "read", return_value=({}, [
                    modelfiles.Tensor(f"{minimax.P}layers.{L}.x", 1, "f", "F32", (1,)) for L in range(60)])), \
                mock.patch.object(minimax.modelfiles, "fingerprint", return_value=[]):
            return minimax.model("D:/models/mm", positions=positions, layers=layers)

    def test_resident_is_a_layer_prefix_and_counts_dense_layers(self):
        p90 = vramplan.plan(self._model(), Inputs(90))
        p95 = vramplan.plan(self._model(), Inputs(95))
        self.assertEqual(p90.resident, [f"layer:{i}" for i in range(p90.flags["--resident"])])
        self.assertEqual((p90.flags, p95.flags), ({"--resident": 31}, {"--resident": 33}))
        self.assertLess(p95.peak_bytes, (95 - 2.85) * GIB)
        self.assertEqual(p90.stream_bytes, 2 * minimax._granule(2864462336))

    def test_tiny_caps_are_refused_not_over_planned(self):
        # review of d5c3e0f: caps 8/9/10 GiB gave --resident 0 with a predicted peak above the cap
        for cap in (8, 9, 10):
            with self.assertRaises(PlanError):
                vramplan.plan(self._model(), Inputs(cap))

    def test_low_cap_needs_more_pinned_host_than_the_pc_has(self):
        with self.assertRaises(PlanError):
            vramplan.plan(self._model(), Inputs(48, host_gib=80))
        partial = vramplan.plan(self._model(layers=30), Inputs(48, host_gib=80))
        self.assertEqual(partial.flags["--layers"], 30)
        self.assertEqual(partial.flags["--resident"] + len(partial.streamed), 30)


class QwenTests(unittest.TestCase):
    def _gguf(self, d):
        tensors = [("token_embd.weight", [2560, 1024], 8), ("output.weight", [2560, 4096], 8),
                   ("per_layer_token_embd.weight", [2560, 4096], 8)]
        tensors += [(f"blk.{L}.ffn.weight", [2560, 2560], 8) for L in range(qwen.LAYERS)]
        path = Path(d) / "q.gguf"
        write_gguf(path, tensors)
        return path

    def test_everything_but_head_and_ple_is_on_the_device(self):
        with tempfile.TemporaryDirectory() as d:
            options = qwen.model(self._gguf(d), positions=32768)
        weights = options[0].source["weights_bytes"]
        per = 2560 * 2560 // 32 * 34
        self.assertEqual(weights, qwen.LAYERS * qwen._alloc(per) + qwen._alloc(2560 * 1024 // 32 * 34)
                         + 2 * qwen.LAYERS * qwen._alloc(qwen.HC_PACKED))
        self.assertEqual(options[0].host_fixed[0], ("ple_table_mapped", 2560 * 4096 // 32 * 34))

    def test_cap_picks_the_largest_block_that_fits(self):
        with tempfile.TemporaryDirectory() as d:
            options = qwen.model(self._gguf(d), positions=32768)
        need = {o.flags([], None)["--blocks"]: sum(b for _, b in o.fixed) for o in options}
        self.assertEqual(list(need), ["1024,2048,4096,8192", "1024,2048,4096", "1024,2048", "1024"])
        self.assertEqual(list(need.values()), sorted(need.values(), reverse=True))
        big = vramplan.plan_first(options, Inputs(64))
        self.assertEqual(big.flags["--blocks"], "1024,2048,4096,8192")
        tight = (need["1024,2048"] + need["1024,2048,4096"]) / 2
        cap = (tight + 2 * GIB) / GIB
        self.assertEqual(vramplan.plan_first(options, Inputs(cap, margin_fraction=0)).flags["--blocks"], "1024,2048")
        with self.assertRaises(PlanError):
            vramplan.plan_first(options, Inputs(2))

    def test_workspaces_grow_with_block_and_positions(self):
        self.assertLess(qwen.workspace_bytes(4096, 32768, True), qwen.workspace_bytes(8192, 32768, True))
        self.assertLess(qwen.state_bytes(8192, False), qwen.state_bytes(32768, False))


DS41 = json.loads((FIXTURES / "ds41-sizes.json").read_text())


class DeepSeekTests(unittest.TestCase):
    def test_sizes_from_the_header(self):
        self.assertEqual(DS41["expert_bytes"], 9953280)  # IQ2_XXS gate/up 3,041,280 B + Q2_K down 3,870,720 B
        self.assertEqual(DS41["layer_experts"], [384 * 9953280] * 40)
        self.assertAlmostEqual((sum(DS41["layer_dense"]) + DS41["globals"]) / 1e9, 10.06, places=2)  # measured 10.07

    def test_prefill_replays_the_measured_engine_plan(self):
        # TTFT exact run, full VRAM, 32K, --host-gib 74: layers 0-19 stream every expert, 20-39 keep 334..383, slots
        # 7.64 GB, experts 71.5-71.9 GB (ds41-ttft-report.md; hetero/w2/e2e).
        p = ds41.predict_prefill("m.gguf", vram_cap_gib=200, positions=32768, host_gib=74, sz=DS41)
        resident = p.source["resident_per_layer"]
        self.assertEqual(resident[:20], [0] * 20)
        self.assertEqual((resident[20], resident[39]), (334, 383))
        self.assertAlmostEqual(p.stream_bytes / 1e9, 7.64, places=2)
        self.assertTrue(71.4e9 < p.resident_bytes < 71.9e9)

    def test_prefill_refuses_a_cap_below_its_own_floor(self):
        with self.assertRaises(PlanError):
            ds41.predict_prefill("m.gguf", vram_cap_gib=16, positions=32768, host_gib=74, sz=DS41)

    def test_prefill_engine_cap_leaves_room_for_the_context(self):
        p = ds41.predict_prefill("m.gguf", vram_cap_gib=48, positions=8192, host_gib=74, sz=DS41)
        self.assertEqual(p.flags["--vram-cap-gib"], "45")  # 48 - margin 2 - context 1
        self.assertLessEqual(p.peak_bytes, 46 * GIB)
        self.assertEqual(int(p.flags["--host-gib"]), 74)
        self.assertGreater(p.disk_bytes, 0)  # what does not fit the host budget streams from the file
        self.assertEqual(set(p.tiers.values()), {"pinned", "disk"})

    def test_host_gib_never_exceeds_what_streams(self):
        p = ds41.predict_prefill("m.gguf", vram_cap_gib=200, positions=512, host_gib=74, sz=DS41)
        need = sum((384 - r) * 9953280 for r in p.source["resident_per_layer"])
        self.assertLessEqual(int(p.flags["--host-gib"]) * GIB, need + GIB)

    def test_cone_rows(self):
        rows = ds41.cone_rows(32768)
        self.assertEqual(rows[:20], [32768.0] * 20)
        self.assertEqual(rows, sorted(rows, reverse=True)[:20] + sorted(rows[20:], reverse=True))
        self.assertLess(rows[39], 128)

    def test_plan_streamed_matches_the_engine_contract(self):
        s = ds41.plan_streamed(10000, 2, [1.0] * 40)
        mx = max(s)
        self.assertLessEqual(sum(384 - v for v in s) + 2 * mx, 10000)
        with self.assertRaises(PlanError):
            ds41.plan_streamed(100, 2, [1.0] * 40)

    def test_front_is_a_layer_prefix_calibrated_at_k22(self):
        model = ds41.front_model("m.gguf", sz=DS41)
        exact = vramplan.plan(model, Inputs(96.0e9 / GIB + 0.01, margin_gib=0, margin_fraction=0))
        self.assertEqual(exact.flags, {"--cuts": 22, "--layers": 22, "--tiers": "D" * 22})
        self.assertLess(vramplan.plan(model, Inputs(48)).flags["--cuts"], 22)

    def test_hot_experts_by_routing_frequency(self):
        with tempfile.TemporaryDirectory() as d:
            routes = Path(d) / "r.txt"
            routes.write_text("# seq a rows 2\n0:5,7 1:3\n0:7 1:3,9\n", encoding="ascii")
            counts = ds41.route_counts(str(routes))
        self.assertEqual(counts[(0, 7)], 2)
        self.assertEqual(counts[(1, 3)], 2)
        model = ds41.hot_model("m.gguf", routes=counts, reserve_gib=0, sz=DS41)
        p = vramplan.plan(model, Inputs(3 * 9953280 / GIB + 1e-6, margin_gib=0, margin_fraction=0))
        self.assertEqual(p.resident, ["0:5", "0:7", "1:3"])  # 2, 2, then 1: 0:5 beats 1:9 (ties by layer)
        data = ds41.placement_json(p, model_file="m.gguf", budget_gib=1.0, reserve_gib=0, servers=["halo-1", "halo-2"])
        self.assertEqual([s["node"] for s in data["devices"][3]["servers"]], ["halo-1", "halo-2"])
        self.assertEqual(data["model"]["expert_bytes"], 9953280)
        with self.assertRaises(PlanError):
            ds41.placement_json(p, model_file="m.gguf", budget_gib=1.0, servers=["halo-1"])
        self.assertEqual(data["layers"][0]["rtx-pro"], [5, 7])
        self.assertEqual(data["devices"][0]["experts"], 3)
        self.assertLessEqual(3 * 9953280, data["devices"][0]["expert_gib"] * GIB)

    def test_hot_counts_match_the_w1_fit_at_the_same_reserve(self):
        model = ds41.hot_model("m.gguf", routes={}, sz=DS41)
        counts = {cap: len(vramplan.plan(model, Inputs(cap, margin_gib=0, margin_fraction=0)).resident)
                  for cap in (60, 70, 80, 90)}
        self.assertEqual(counts, {60: 5442, 70: 6521, 80: 7600, 90: 8678})  # hetero/w1-report.md


class NodeTests(unittest.TestCase):
    ROUTES = {(0, 1): 50, (0, 200): 40, (1, 300): 30, (2, 5): 20}

    def test_pair_node_keeps_its_half_and_adds_the_hottest_of_the_other(self):
        model = ds41.node_model("m.gguf", routes=self.ROUTES, mode="pair-rank0", fixed_gib=10, sz=DS41)
        half = 40 * 192 * 9953280
        cap = (10 * GIB + half + 2 * 9953280) / GIB + 1e-6
        p = vramplan.plan(model, Inputs(cap, margin_gib=0, margin_fraction=0))
        self.assertEqual(p.resident, ["0:200", "1:300"])  # the other half's experts, hottest first
        self.assertEqual(p.flags, {"extra_experts": 2})
        self.assertEqual(set(p.tiers.values()), {"pair"})  # computed by the peer, never fetched
        self.assertEqual(p.disk_bytes, 0)

    def test_pair_node_can_stream_the_rest_from_local_nvme(self):
        model = ds41.node_model("m.gguf", routes=self.ROUTES, mode="pair-rank1", fixed_gib=10, rest="disk", sz=DS41)
        p = vramplan.plan(model, Inputs((10 * GIB + 40 * 192 * 9953280) / GIB + 0.01, margin_gib=0, margin_fraction=0))
        self.assertEqual(set(p.tiers.values()), {"disk"})
        self.assertEqual(p.pinned_bytes, 0)

    def test_replica_node_streams_the_rest_from_nvme(self):
        model = ds41.node_model("m.gguf", routes=self.ROUTES, mode="replica", fixed_gib=10, sz=DS41)
        p = vramplan.plan(model, Inputs((10 * GIB + 3 * 9953280) / GIB + 1e-6, margin_gib=0, margin_fraction=0))
        self.assertEqual(p.resident, ["0:1", "0:200", "1:300"])
        self.assertEqual(p.disk_bytes, (40 * 384 - 3) * 9953280)

    def test_fixed_gib_must_be_finite_and_non_negative(self):
        for bad in (-50, float("nan"), float("inf"), "10"):
            with self.subTest(fixed=bad), self.assertRaises(PlanError):
                ds41.node_model("m.gguf", routes=self.ROUTES, mode="replica", fixed_gib=bad, sz=DS41)

    def test_unknown_mode(self):
        with self.assertRaises(PlanError):
            ds41.node_model("m.gguf", routes={}, mode="tp4", fixed_gib=10, sz=DS41)


GOLDEN = FIXTURES / "golden"


class DescriptorTests(unittest.TestCase):
    """desc-v1 descriptors planned here must give the same tiers and totals as strix-stream's C++ ss_plan_make
    (expected values recorded from ss-plan.exe built from 8a4ee8a: 4 KiB pinned billing, unconditional host check)."""

    @staticmethod
    def desc(name):
        with gzip.open(GOLDEN / f"{name}.desc.json.gz") as f:
            return json.load(f)

    @staticmethod
    def refusal(text):
        if "nothing fits" in text or "exceeds" in text:
            return "nothing-fits"
        return "host" if "pinned on the host" in text else text

    def test_same_plans_as_strix_stream(self):
        from hyperloom.placement import desc as D
        cases = json.loads((GOLDEN / "ss-plan-expected.json").read_text())["cases"]
        self.assertEqual(len(cases), 34)
        self.assertEqual({c["expected"].get("refused") for c in cases}, {None, "nothing-fits", "host"})
        for case in cases:
            with self.subTest(descriptor=case["descriptor"], cap=case["cap_gib"], host=case["host_gib"],
                              ring=case.get("ring_slots", 0)):
                d, want = self.desc(case["descriptor"]), case["expected"]
                inputs = Inputs(case["cap_gib"], host_gib=case["host_gib"])
                model = D.model(d, ring_slots=case.get("ring_slots", 0))
                if "refused" in want:
                    with self.assertRaises(PlanError) as ctx:
                        vramplan.plan(model, inputs)
                    self.assertEqual(self.refusal(str(ctx.exception)), want["refused"])
                    continue
                p = vramplan.plan(model, inputs)
                got = D.plan_v1(d, p)
                self.assertEqual(got["tiers"], want["tiers"])
                self.assertEqual((p.peak_bytes, p.pinned_bytes, p.disk_bytes),
                                 (want["peak_bytes"], want["pinned_bytes"], want["disk_bytes"]))

    def test_dynamic_slots_same_tiers_conservative_peak(self):
        from hyperloom.placement import desc as D
        for case in json.loads((GOLDEN / "ss-plan-expected.json").read_text())["dynamic_cases"]:
            with self.subTest(cap=case["cap_gib"]):
                d = self.desc(case["descriptor"])
                model = D.model(d, dynamic_slots_per_layer=case["dynamic_slots"])
                inputs = Inputs(case["cap_gib"], host_gib=case["host_gib"])
                if "refused" in case["expected"]:
                    with self.assertRaises(PlanError) as ctx:
                        vramplan.plan(model, inputs)
                    self.assertEqual(self.refusal(str(ctx.exception)), case["expected"]["refused"])
                    continue
                p = vramplan.plan(model, inputs)
                self.assertEqual(D.plan_v1(d, p)["tiers"], case["expected"]["tiers"])
                self.assertGreaterEqual(p.peak_bytes, case["expected"]["peak_bytes"])

    def test_non_streamable_units_billed_at_the_pool_stride(self):
        from hyperloom.placement import desc as D
        d = self.desc("mixed-pinned-disk")
        fixed = dict(D.model(d).fixed)
        self.assertEqual(fixed["non-streamable units (pool stride)"], 6 * (-(-70_000_001 // 4096) * 4096))

    def test_malformed_descriptors_are_plan_errors(self):
        from hyperloom.placement import desc as D
        base = self.desc("mixed-pinned-disk")
        layer2 = base["layers"][2]
        bad_top = [{"fixed_device": [{"name": "x", "bytes": -100 * GIB}]}, {"host_fixed": [{"name": "x", "bytes": -1}]},
                   {"unit_align": -256}, {"unit_align": 0}, {"unit_align": 3}, {"ring_slots": 1 << 21},
                   {"fixed_device": [{"name": "x", "bytes": True}]}, {"state_bytes_per_position": None},
                   {"surprise": 1}, {"layers": []}, {"layers": "no"}, {"sources": [{"path": 3}]}]
        for change in bad_top:
            with self.subTest(top=sorted(change)), self.assertRaises(PlanError):
                D.load(dict(base, **change))
        bad_layer = [{"fixed_bytes": -1}, {"n_units": 10 ** 9}, {"n_units": True}, {"top_k": 0},
                     {"benefit": [float("inf")] + [1] * 9}, {"benefit": [1] * 3}, {"parts": ["not a dict"]},
                     {"parts": [dict(layer2["parts"][0], format="Q9_Z")]},
                     {"parts": [dict(layer2["parts"][0], source=5)]},
                     {"parts": [dict(layer2["parts"][0], stride=1)]}, {"hash": ["xyz"] * 10}, {"streamable": "yes"},
                     {"kind": "dense"}, {"unit_bytes": 1}, {"index": 7}]
        for change in bad_layer:
            layers = list(base["layers"])
            layers[2] = dict(layer2, **change)
            with self.subTest(layer=sorted(change)), self.assertRaises(PlanError):
                D.load(dict(base, layers=layers))
        self.assertEqual(D.load(dict(base, **{"x-note": "free"}))["x-note"], "free")

    def test_descriptor_files_are_bounded_and_clean(self):
        from hyperloom.placement import desc as D
        with tempfile.TemporaryDirectory() as d:
            deep = Path(d) / "deep.json"
            deep.write_text("[" * 100000 + "]" * 100000)
            junk = Path(d) / "junk.json"
            junk.write_text("{not json")
            for path in (deep, junk, Path(d) / "missing.json"):
                with self.subTest(path=path.name), self.assertRaises(PlanError):
                    D.load(path)
            big = Path(d) / "big.json"
            big.write_text(json.dumps(self.desc("moe-ranked")))
            with mock.patch.object(D, "MAX_BYTES", 100), self.assertRaises(PlanError):
                D.load(big)
        with self.assertRaises(PlanError):
            D.model(self.desc("moe-ranked"), benefit=[float("nan")] * 48)
        with self.assertRaises(PlanError):
            D.model(self.desc("moe-ranked"), ring_slots=-1)

    def test_descriptor_rules_match_strix_stream(self):
        from hyperloom.placement import desc as D
        base = self.desc("moe-ranked")
        for change in ({"schema": "x"}, {"order": "random"}, {"tiers": "ssd"}):
            with self.subTest(change=change), self.assertRaises(PlanError):
                D.load(dict(base, **change))
        for change in ({"top_k": 0}, {"parts": []}, {"unit_bytes": 1}, {"kind": "dense"}):
            layer = dict(base["layers"][0], **change)
            with self.subTest(layer_change=sorted(change)), self.assertRaises(PlanError):
                D.load(dict(base, layers=[layer] + base["layers"][1:]))

    def test_plan_v1_letters_cover_every_unit(self):
        from hyperloom.placement import desc as D
        d = self.desc("blob-prefix")
        p = vramplan.plan(D.model(d), Inputs(48, host_gib=63))
        rows = D.plan_v1(d, p)["tiers"]
        self.assertEqual(len(rows), 60)
        self.assertTrue(all(len(r) == 1 and r in "RPD" for r in rows))
        self.assertEqual("".join(rows).rstrip("PD"), "R" * len(p.resident))  # a prefix


class RegistryTests(unittest.TestCase):
    def test_unknown_engine(self):
        from hyperloom import placement
        with self.assertRaises(PlanError):
            placement.plan("nope", "m", Inputs(8))

    def test_cli_lists_every_engine(self):
        from hyperloom import cli, placement
        self.assertEqual(cli.VRAMPLAN_ENGINES, placement.ENGINES)

    def test_cli_refuses_flags_that_do_not_apply(self):
        import contextlib
        import io
        from hyperloom import cli
        for argv in (["vramplan", "ds41-node", "--model", "m.gguf", "--vram-cap-gib", "8", "--layers", "3"],
                     ["vramplan", "ds41-node", "--model", "m.gguf", "--vram-cap-gib", "8",
                      "--placement-out", "p.json"]):
            err = io.StringIO()
            with self.subTest(argv=argv[-2]), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(argv), 1)
            self.assertIn(argv[-2], err.getvalue())

    def test_ds41_prefill_needs_a_host_budget(self):
        from hyperloom import placement
        with self.assertRaises(PlanError):
            placement.plan("ds41-rtx-prefill", "m", Inputs(48), positions=8192)


if __name__ == "__main__":
    unittest.main()
