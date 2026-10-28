"""The redaction corpus (tests/fixtures/redaction/corpus.jsonl): every case of the shared table, the review cases and
a seeded sample of generated text (JSON, repr and shell encodings of credentials, kept values, unpaired quotes) and
argument lists. The characters each set of rules hides are taken from its spans (tests/redaction_corpus_metric.py).
Against the frozen rules of 2a7c803 and of 65f2fe5 (copies in the fixture), the rules show no secret character either
hid; against 65f2fe5 they hide no other character, except in the entries allowlist.json names, each with the change
that explains it, and every allowlisted entry still needs its reason."""

import importlib.util
import json
from pathlib import Path
import unittest

from hyperloom import redaction
from hyperloom.web.api import redact
from tests.redaction_corpus_metric import Rules, hides_more, merged, secret_positions, shows, substitution_hidden

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "redaction"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


node = load(ROOT / "node" / "hyperloom_logsearch.py", "hyperloom_logsearch_corpus_under_test")
base = load(FIXTURE / "rules_2a7c803.py", "redaction_rules_2a7c803")
previous = load(FIXTURE / "rules_65f2fe5.py", "redaction_rules_65f2fe5")
HEAD = Rules(lambda text: merged(redaction._hidden(text)), lambda text: merged(node._hidden(text)),
             lambda value, text: redact(value, text=text))
PREVIOUS = Rules(lambda text: merged(previous.hidden_spans(text)), lambda text: merged(previous.hidden_spans(text)),
                 lambda value, text: previous.portal_redact(value, text=text))
BASE = Rules(lambda text: substitution_hidden(base.PORTAL_TEXT_PATTERNS, text),
             lambda text: substitution_hidden(base.SECRET_PATTERNS, text),
             lambda value, text: base.portal_redact(value, text=text))


class CorpusTests(unittest.TestCase):
    def test_no_new_leak_and_no_unexplained_redaction(self):
        allow = json.loads((FIXTURE / "allowlist.json").read_text(encoding="utf-8"))
        corpus = [json.loads(line) for line in (FIXTURE / "corpus.jsonl").open(encoding="utf-8")]
        self.assertGreater(len(corpus), 3000)
        leaks, extra, stale = [], [], []
        for entry in corpus:
            new, was, first = HEAD.hidden(entry), PREVIOUS.hidden(entry), BASE.hidden(entry)
            for path in new:
                for name, old in (("2a7c803", first), ("65f2fe5", was)):
                    shown = shows(entry, new[path], old[path])
                    if shown:
                        leaks.append((name, path, entry["id"], entry["input"], shown))
                reason = allow["entries"].get(str(entry["id"]), {}).get(path)
                more = hides_more(entry, new[path], was[path])
                if more and reason not in allow["reasons"]:
                    extra.append((path, entry["id"], entry["input"]))
                elif reason is not None and not more:
                    stale.append((path, entry["id"], reason))
        self.assertEqual(leaks, [])
        self.assertEqual(extra, [])
        self.assertEqual(stale, [])

    def test_the_metric_sees_one_shown_character(self):
        # Any one secret character that the head's spans no longer hide is a leak against the head itself.
        corpus = [json.loads(line) for line in (FIXTURE / "corpus.jsonl").open(encoding="utf-8")]
        checked = 0
        for entry in corpus[:400]:
            new = HEAD.hidden(entry)
            for path, positions in new.items():
                for marker, characters in __import__("tests.redaction_corpus_metric", fromlist=["x"]) \
                        .secret_positions(entry).items():
                    for at in sorted(characters & positions)[:3]:
                        self.assertIn(marker, shows(entry, positions - {at}, positions))
                        checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
