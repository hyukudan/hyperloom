"""The redaction corpus (tests/fixtures/redaction/corpus.jsonl): every case of the shared table, the review cases and
a seeded sample of generated text (JSON, repr and shell encodings of credentials, kept values, unpaired quotes) and
argument lists. Against the frozen rules of 2a7c803 and of 65f2fe5 (copies in the fixture), the rules show no secret
either hid; against 65f2fe5 they hide nothing more, except the entries allowlist.json names, each with the change
that explains it."""

import importlib.util
import json
from pathlib import Path
import unittest

from hyperloom.web.api import redact

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


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def hides_more(new, old, markers):
    """``new`` hides more than ``old`` (more [redacted], or as many and a shorter text) where ``old`` showed no
    secret."""
    new, old = text(new), text(old)
    return new != old and not any(marker in old for marker in markers) and (
        new.count("[redacted]") > old.count("[redacted]") or
        new.count("[redacted]") == old.count("[redacted]") > 0 and len(new) < len(old))


class CorpusTests(unittest.TestCase):
    def test_no_new_leak_and_no_unexplained_redaction(self):
        allow = json.loads((FIXTURE / "allowlist.json").read_text(encoding="utf-8"))
        corpus = [json.loads(line) for line in (FIXTURE / "corpus.jsonl").open(encoding="utf-8")]
        self.assertGreater(len(corpus), 3000)
        leaks, extra = [], []
        for entry in corpus:
            new = outputs(entry, redact, node.redact)
            for name, rules in (("2a7c803", BASE), ("65f2fe5", PREVIOUS)):
                old = outputs(entry, rules.portal_redact, rules.node_redact)
                for path in new:
                    shown = [m for m in entry["markers"] if m in text(new[path]) and m not in text(old[path])]
                    if shown:
                        leaks.append((name, path, entry["id"], entry["input"], shown))
                    if name == "65f2fe5" and hides_more(new[path], old[path], entry["markers"]):
                        reason = allow["entries"].get(str(entry["id"]), {}).get(path)
                        if reason not in allow["reasons"]:
                            extra.append((path, entry["id"], entry["input"], text(new[path]), text(old[path])))
        self.assertEqual(leaks, [])
        self.assertEqual(extra, [])


if __name__ == "__main__":
    unittest.main()
