"""Quality suite: deterministic items, scorers, exact McNemar p-values, the paired interval and pairing rules."""

import math
import unittest

from hyperloom.bench import quality


class SuiteTests(unittest.TestCase):
    def test_suite_is_deterministic_and_context_aware(self):
        first, second = quality.suite(None), quality.suite(None)
        self.assertEqual(quality.suite_digest(first), quality.suite_digest(second))
        self.assertEqual(len({i["id"] for i in first}), len(first))
        self.assertEqual(sum(1 for i in first if i["category"] == "long-context recall"), 2)
        self.assertEqual(sum(1 for i in quality.suite(200000) if i["category"] == "long-context recall"), 6)
        self.assertEqual(sum(1 for i in quality.suite(16384) if i["category"] == "long-context recall"), 2)
        recall = next(i for i in quality.suite(200000) if i["id"] == "recall-128k-1")
        self.assertGreater(len(recall["messages"][0]["content"]), 128 * 1024 * 2)
        self.assertIn(recall["scorer"]["answers"][0], recall["messages"][0]["content"])

    def test_generated_items_have_correct_answers(self):
        items = {i["id"]: i for i in quality.suite(None)}
        arith = items["arith-01"]["messages"][0]["content"]
        value = None
        for sentence in arith.split(". "):
            words = sentence.replace(".", "").split()
            if words[:2] == ["Start", "with"]:
                value = int(words[2])
            elif words[:1] == ["Add"]:
                value += int(words[1])
            elif words[:1] == ["Subtract"]:
                value -= int(words[1])
            elif words[:2] == ["Multiply", "by"]:
                value *= int(words[2])
        self.assertEqual(str(value), items["arith-01"]["scorer"]["answers"][0])


class ScorerTests(unittest.TestCase):
    def test_answers(self):
        scorer = {"kind": "answer", "answers": ["Canberra"]}
        for reply in ("Canberra", "canberra.", "**Canberra**", "The answer is Canberra", "Answer: Canberra\n", "<think>maybe Sydney</think>Canberra"):
            self.assertTrue(quality.score(scorer, reply), reply)
        for reply in ("Sydney", "Canberra or Sydney", ""):
            self.assertFalse(quality.score(scorer, reply), reply)
        self.assertTrue(quality.score({"kind": "answer", "answers": ["180"]}, "180.0"))

    def test_formats(self):
        cases = [({"kind": "uppercase"}, "IT RAINS.", "It rains."), ({"kind": "lowercase"}, "quiet trees.", "Quiet trees."),
                 ({"kind": "prefixed_lines", "count": 3, "prefix": "- "}, "- a\n- b\n- c", "- a\n- b"),
                 ({"kind": "word_count", "count": 5}, "One two three four five.", "One two three four."),
                 ({"kind": "json_keys", "keys": ["age", "name"]}, '```json\n{"name": "A", "age": 3}\n```', '{"name": "A"}'),
                 ({"kind": "json_int_array", "count": 3, "greater_than": 10}, "[11, 12, 13]", "[11, 12, 9]"),
                 ({"kind": "ends_with", "word": "DONE"}, "We left. DONE", "DONE we left"),
                 ({"kind": "starts_with", "word": "Certainly"}, "Certainly, red.", "Sure, red."),
                 ({"kind": "no_letter", "letter": "e", "min_words": 6}, "A cat sat on a warm rug", "The cat sat on a warm rug"),
                 ({"kind": "no_char", "char": ",", "min_sentences": 2}, "Snow falls. It is cold.", "Snow, falls. Cold."),
                 ({"kind": "numbered", "count": 2}, "1. cat\n2. dog", "1. cat\n3. dog"),
                 ({"kind": "quoted_word"}, '"carrot"', "carrot"),
                 ({"kind": "max_chars", "max": 10, "min": 2}, "Deep sea", "The deep blue ocean"),
                 ({"kind": "exact", "text": "blue blue"}, "blue blue", "blue blue blue")]
        for scorer, good, bad in cases:
            with self.subTest(kind=scorer["kind"]):
                self.assertTrue(quality.score(scorer, good))
                self.assertFalse(quality.score(scorer, bad))


class StatisticsTests(unittest.TestCase):
    def test_exact_mcnemar(self):
        self.assertEqual(quality.mcnemar_p(0, 0), 1.0)
        self.assertAlmostEqual(quality.mcnemar_p(0, 5), 2 / 32)
        self.assertAlmostEqual(quality.mcnemar_p(1, 9), 2 * 11 / 1024)
        self.assertEqual(quality.mcnemar_p(4, 4), 1.0)
        self.assertAlmostEqual(quality.mcnemar_p(2, 10), 2 * sum(math.comb(12, k) for k in range(3)) / 2 ** 12)
        self.assertEqual(quality.mcnemar_p(10, 2), quality.mcnemar_p(2, 10))

    def test_interval(self):
        low, high = quality.paired_interval(1, 9, 70)
        self.assertLess(low, high)
        self.assertLess(high, 0)
        self.assertIsNone(quality.paired_interval(0, 0, 0))
        low, high = quality.paired_interval(0, 0, 5)
        self.assertLess(low, 0)
        self.assertGreater(high, 0)
        self.assertGreaterEqual(quality.paired_interval(40, 0, 40)[0], -1)
        self.assertLessEqual(quality.paired_interval(40, 0, 40)[1], 1)

    def test_compare_pairs_only_identical_items_and_requests(self):
        def result(item_id, passed, request="r", digest="d", status="scored", reply="x"):
            return {"id": item_id, "item_digest": digest, "request_digest": request, "status": status, "passed": passed,
                    "reply_sha256": reply, "category": "short QA"}
        this = [result("a", True), result("b", True, reply="y"), result("c", False), result("d", True, request="other"),
                result("e", None, status="error"), result("f", True)]
        other = [result("a", True), result("b", False), result("c", True), result("d", True), result("e", True),
                 result("f", True, digest="changed")]
        compared = quality.compare(this, other)
        self.assertEqual(compared["paired"], 3)
        self.assertEqual((compared["only_this"], compared["only_other"]), (1, 1))
        self.assertEqual(compared["identical_replies"], 2)
        self.assertEqual(compared["exclusions"], {"request differs (generation settings)": 1, "not scored in both runs": 1,
                                                  "item differs (prompt, answer or scorer)": 1})
        self.assertEqual(compared["verdict"], "no statistically significant difference detected")
        self.assertEqual(quality.compare([], [])["verdict"], "no paired items: nothing to compare")


if __name__ == "__main__":
    unittest.main()
