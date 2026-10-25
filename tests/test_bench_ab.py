"""A/B receipts (C12): ABA x3 order, per-case comparison, claim rules, redaction-safe transport, offline verification
(tampering of arrays, bodies, digests and summaries is detected) and the verify-receipt CLI."""

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))

import support  # noqa: E402
from fake_server import FakeServer  # noqa: E402

from hyperloom.bench import ab, targets  # noqa: E402
from hyperloom.cliext import bench as bench_cli  # noqa: E402
from hyperloom.web.api import redact  # noqa: E402


def arms(model_a="m-1", model_b="m-1", tokenizer_a="tok-1", tokenizer_b="tok-1"):
    def identity(name, model, tokenizer):
        found = {"url": "http://halo-1:18600", "label": name, "model": model, "binary_sha256": None, "build_source": "unavailable"}
        if tokenizer:
            found.update(tokenizer_id=tokenizer, tokenizer_source="declared")
        else:
            found["tokenizer_source"] = "unavailable"
        return ab.transportable(found)
    return {"a": {"name": "a", "model": model_a, "identity": identity("A", model_a, tokenizer_a)},
            "b": {"name": "b", "model": model_b, "identity": identity("B", model_b, tokenizer_b)}}


ARGS = {"seed": 3, "prompt_set": "code", "max_tokens": 16, "cases": 3}


def trials(cases=3, length=6, diverge=None, fail=None, no_ids=(), long=False):
    rows = []
    for step in ab.ORDER:
        for case in range(cases):
            ids = list(range(case * 100, case * 100 + (300 if long else length)))
            if diverge and step["arm"] == "b" and case == diverge[0]:
                ids[diverge[1]] += 1
            trial = {"round": step["round"], "pass": step["pass"], "arm": step["arm"], "case": case, "outcome": "ok",
                     "finish_reason": "length", "completion_tokens": len(ids), "ttft_s": 0.05,
                     "last_output_s": 0.5 if step["arm"] == "a" else 0.4, "end_s": 0.6}
            if fail == (step["pass"], case):
                trial.update(outcome="error", error="HTTP 503", completion_tokens=None, last_output_s=None)
            elif step["arm"] in no_ids:
                trial["outcome"] = "no-ids"
            else:
                trial["token_ids"] = ab.encode_ids(ids)
            rows.append(trial)
    return rows


def receipt(**kwargs):
    bodies = [ab.case_body(m, ARGS["max_tokens"]) for m in ab.corpus(ARGS["prompt_set"], ARGS["cases"], ARGS["seed"])]
    arm_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k.startswith(("model_", "tokenizer_"))}
    return ab.build(arms(**arm_kwargs), ARGS, bodies, trials(**kwargs))


class ReceiptTests(unittest.TestCase):
    def test_order_is_aba_three_times(self):
        self.assertEqual("".join(step["arm"] for step in ab.ORDER), "aba" * 3)
        self.assertEqual([step["pass"] for step in ab.ORDER], list(range(9)))

    def test_identical_arms_claim_and_verify(self):
        built = receipt()
        self.assertEqual(built["summary"]["claim"], "token-identical: 3/3 (first pass of each arm)")
        self.assertEqual(built["summary"]["deterministic"], {"a": "3/3", "b": "3/3"})
        self.assertEqual(len(built["speed"]), 3)
        self.assertAlmostEqual(built["speed"][0]["delta_b_vs_a"], 0.5 / 0.4 - 1, places=6)
        result = ab.verify(built)
        self.assertTrue(result["ok"], result["problems"])

    def test_divergence_index(self):
        built = receipt(diverge=(1, 4))
        self.assertEqual(built["comparison"][1], {"case": 1, "result": "diverged", "first_divergence": 4, "lengths": [6, 6]})
        self.assertEqual(built["summary"]["claim"], "token-identical: 2/3 (first pass of each arm)")
        self.assertEqual(built["summary"]["deterministic"]["b"], "3/3")
        self.assertTrue(ab.verify(built)["ok"])

    def test_claim_withheld_or_unavailable(self):
        self.assertIn("different models", receipt(model_b="m-2")["summary"]["claim"])
        self.assertIn("tokenizer id not known for B", receipt(tokenizer_b=None)["summary"]["claim"])
        self.assertIn("different tokenizers", receipt(tokenizer_b="tok-2")["summary"]["claim"])
        none = receipt(no_ids=("a", "b"))
        self.assertEqual(none["summary"]["claim"], "token ids unavailable: no identity claim")
        self.assertEqual(none["summary"]["compared"], 0)
        self.assertTrue(ab.verify(none)["ok"])

    def test_failed_trial_counts_in_the_denominator_and_speed_matching(self):
        built = receipt(fail=(1, 2))      # B's first pass of case 2 failed
        self.assertEqual(built["comparison"][2]["result"], "unavailable")
        self.assertEqual(built["summary"]["identical"], 2)
        self.assertEqual(built["summary"]["cases"], 3)
        self.assertEqual(built["speed"][0]["matched_cases"], [0, 1])
        self.assertNotIn("token_ids", built["trials"][5])
        self.assertTrue(ab.verify(built)["ok"])

    def test_long_arrays_are_compressed_and_round_trip(self):
        built = receipt(long=True)
        self.assertTrue(built["trials"][0]["token_ids"].startswith("gz:b64:"))
        self.assertEqual(ab.decode_ids(built["trials"][0]["token_ids"]), list(range(300)))
        self.assertTrue(ab.verify(json.loads(json.dumps(redact(built))))["ok"])

    def test_identities_redaction_would_alter_are_carried_as_hex(self):
        tricky_model, tricky_tokenizer = "model token: x", "tok" * 66 + "ab"
        built = receipt(model_a=tricky_model, model_b=tricky_model, tokenizer_a="token:sample", tokenizer_b="token:sample")
        self.assertEqual(redact(built), built)
        self.assertIn("model_hex", built["arms"]["a"])
        self.assertIn("vocab_hex", built["arms"]["a"])
        self.assertTrue(built["summary"]["claim"].startswith("token-identical"))
        self.assertTrue(ab.verify(json.loads(json.dumps(redact(built))))["ok"])
        long_ok = receipt(tokenizer_a=tricky_tokenizer, tokenizer_b=tricky_tokenizer)
        self.assertEqual(long_ok["arms"]["a"]["tokenizer_id"], tricky_tokenizer)
        multibyte = receipt(model_a="modèle-ü", model_b="modèle-ü", tokenizer_a="vocabulário", tokenizer_b="vocabulário")
        self.assertTrue(ab.verify(json.loads(json.dumps(redact(multibyte))))["ok"])

    def test_tampering_is_detected(self):
        built = receipt(diverge=(0, 2))
        cases = {
            "array": lambda r: r["trials"][1]["token_ids"].__setitem__(0, 999),
            "first divergence": lambda r: r["comparison"][0].__setitem__("first_divergence", 3),
            "summary": lambda r: r["summary"].__setitem__("identical", 3),
            "claim": lambda r: r["summary"].__setitem__("claim", "token-identical: 3/3 (first pass of each arm)"),
            "body": lambda r: r["corpus"]["cases"][0].__setitem__("body_hex", ab.canonical(
                {**json.loads(bytes.fromhex(r["corpus"]["cases"][0]["body_hex"])), "messages": [{"role": "user", "content": "x"}]}).hex()),
            "digest": lambda r: r["corpus"].__setitem__("corpus_digest", "0" * 64),
            "request sha": lambda r: r["corpus"]["cases"][1]["request_sha256"].__setitem__("b", "1" * 64),
            "model": lambda r: r["arms"]["b"].__setitem__("model", "other"),
            "missing trial": lambda r: r["trials"].pop(),
            "order": lambda r: r["trial_order"].reverse(),
            "speed": lambda r: r["speed"][1].__setitem__("delta_b_vs_a", 0.9),
            "ids on error": lambda r: r["trials"][0].__setitem__("outcome", "error"),
            "stop policy": lambda r: r["corpus"]["stop_policy"].__setitem__("max_tokens", 17),
            "short array": lambda r: r["trials"][3].__setitem__("token_ids", []),
            "count mismatch": lambda r: r["trials"][3].__setitem__("completion_tokens", 7),
            "extra case": lambda r: r["trials"].append({**r["trials"][0], "case": 999}),
        }
        for name, tamper in cases.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(built)
                tamper(changed)
                result = ab.verify(changed)
                self.assertFalse(result["ok"], name)
                self.assertTrue(result["problems"])
        self.assertFalse(ab.verify({"schema": "something else"})["ok"])
        for broken in ({"schema": ab.SCHEMA, "arms": [], "corpus": {"cases": []}, "trial_order": ab.ORDER, "trials": []},
                       {**built, "trials": [None]}, {**built, "corpus": None}):
            self.assertFalse(ab.verify(broken)["ok"])


class PortalRoundTripTests(unittest.TestCase):
    def test_ab_run_receipt_downloads_and_verifies_offline(self):
        bench = support.Bench(self, token_delay=0.0002)
        with FakeServer(diverge_at=3, diverge_match="lighthouse") as other:
            targets.save(bench.api, {"id": "other", "scheme": "http", "host": "127.0.0.1", "port": other.port, "machines": ["halo-2"]})
            record, run = bench.run("bench/ab", {"arms": {"a": {"profile": "qwen"}, "b": {"target": "other"}}, "cases": 8,
                                                 "max_tokens": 8, "tokenizers": {"a": "tok-1", "b": "tok-1"}}, 60)
        self.assertEqual(record["state"], "succeeded", record["error"])
        self.assertEqual(record["resources"], ["halo-1", "halo-2", "router"])
        document = bench.api.read("bench/receipt/" + run["id"])
        receipt_doc = document["receipt"]
        self.assertEqual(receipt_doc["arms"]["a"]["tokenizer_source"], "declared")
        self.assertEqual(receipt_doc["arms"]["a"]["binary_sha256"], ["b" * 64])
        self.assertEqual(receipt_doc["arms"]["a"]["router_policy"], "nodes")
        self.assertNotIn(support.KEY, json.dumps(document))
        diverged = [c for c in receipt_doc["comparison"] if c["result"] == "diverged"]
        self.assertEqual([c["first_divergence"] for c in diverged], [3])
        self.assertEqual(receipt_doc["summary"]["claim"], "token-identical: 7/8 (first pass of each arm)")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = bench_cli._verify(type("Args", (), {"file": str(path)})())
            self.assertEqual(code, 0, out.getvalue())
            document["receipt"]["trials"][0]["token_ids"][0] += 1
            path.write_text(json.dumps(document), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(bench_cli._verify(type("Args", (), {"file": str(path)})()), 1)
        self.assertEqual(len([r for r in bench.server.requests if r["method"] == "POST"]), 6 * 8)


if __name__ == "__main__":
    unittest.main()
