"""The redaction corpus (tests/fixtures/redaction/corpus.jsonl): every case of the shared table, the review cases and
a seeded sample of generated text (JSON, repr and shell encodings of credentials, kept values, unpaired quotes) and
argument lists. Against the frozen rules of 2a7c803 and of 65f2fe5 (copies in the fixture), the rules show no secret
either hid; against 65f2fe5 they hide no other character, except in the entries allowlist.json names, each with the
change that explains it, and every allowlisted entry still needs its reason."""

import importlib.util
import json
from pathlib import Path
import unittest

from hyperloom.web.api import redact
from tests.redaction_corpus_metric import hides_more, shows

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "redaction"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


node = load(ROOT / "node" / "hyperloom_logsearch.py", "hyperloom_logsearch_corpus_under_test")
BASE = load(FIXTURE / "rules_2a7c803.py", "redaction_rules_2a7c803")
PREVIOUS = load(FIXTURE / "rules_65f2fe5.py", "redaction_rules_65f2fe5")


def outputs(entry, portal, node_redact):
    value = entry["input"]
    if entry["kind"] == "text":
        return {"portal": portal(value), "node": node_redact(value)}
    return {"portal": portal(value), "recipe": portal(value, text=False)}


class CorpusTests(unittest.TestCase):
    def test_no_new_leak_and_no_unexplained_redaction(self):
        allow = json.loads((FIXTURE / "allowlist.json").read_text(encoding="utf-8"))
        corpus = [json.loads(line) for line in (FIXTURE / "corpus.jsonl").open(encoding="utf-8")]
        self.assertGreater(len(corpus), 3000)
        leaks, extra, stale = [], [], []
        for entry in corpus:
            new = outputs(entry, redact, node.redact)
            previous = outputs(entry, PREVIOUS.portal_redact, PREVIOUS.node_redact)
            base = outputs(entry, BASE.portal_redact, BASE.node_redact)
            for path in new:
                for name, old in (("2a7c803", base), ("65f2fe5", previous)):
                    shown = shows(entry, new[path], old[path])
                    if shown:
                        leaks.append((name, path, entry["id"], entry["input"], shown))
                reason = allow["entries"].get(str(entry["id"]), {}).get(path)
                more = hides_more(entry, new[path], previous[path])
                if more and reason not in allow["reasons"]:
                    extra.append((path, entry["id"], entry["input"], new[path], previous[path]))
                elif reason is not None and not more:
                    stale.append((path, entry["id"], reason))
        self.assertEqual(leaks, [])
        self.assertEqual(extra, [])
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
