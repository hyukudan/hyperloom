"""The redaction corpus (tests/fixtures/redaction/corpus.jsonl): every case of the shared table, the review cases and
a seeded sample of generated text (JSON, repr and shell encodings of credentials, kept values, unpaired quotes) and
argument lists. The characters each set of rules hides are taken from its spans (tests/redaction_corpus_metric.py).
Against the frozen rules of 2a7c803 and of 65f2fe5 (copies in the fixture), the rules show no secret character either
hid; against 65f2fe5 they hide no other character, except in the entries allowlist.json names, each with the change
that explains it, and every allowlisted entry still needs its reason. Each path hides at least what its own base
rules hid (their substitutions replayed), over the corpus and a seeded fuzz, and the node's linear base rules give
the verbatim rules' substitutions."""

import importlib.util
import json
from pathlib import Path
import random
import re
import unittest

from hyperloom import redaction
from hyperloom.web.api import redact
from tests.redaction_corpus_metric import (Rules, argv_hidden, hides_more, merged, render, secret_positions, shows,
                                             substitution_hidden)

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
HEAD = Rules(lambda text: merged(redaction._hidden(text, 0, redaction.BASE_PORTAL_RULES)),
             lambda text: merged(node._hidden(text, 0, node.BASE_NODE_RULES)),
             lambda value, text: redact(value, text=text))
PREVIOUS = Rules(lambda text: merged(previous.hidden_spans(text)), lambda text: merged(previous.hidden_spans(text)),
                 lambda value, text: previous.portal_redact(value, text=text))
BASE = Rules(lambda text: substitution_hidden(base.PORTAL_TEXT_PATTERNS, text),
             lambda text: substitution_hidden(base.SECRET_PATTERNS, text),
             lambda value, text: base.portal_redact(value, text=text))


def load_corpus():
    with (FIXTURE / "corpus.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class CorpusTests(unittest.TestCase):
    def test_no_new_leak_and_no_unexplained_redaction(self):
        allow = json.loads((FIXTURE / "allowlist.json").read_text(encoding="utf-8"))
        corpus = load_corpus()
        self.assertGreater(len(corpus), 3000)
        leaks, extra, stale, unfaithful = [], [], [], []
        for entry in corpus:
            new, was, first = HEAD.hidden(entry), PREVIOUS.hidden(entry), BASE.hidden(entry)
            if entry["kind"] == "text":       # the spans measured are what the public functions hide
                value = entry["input"]
                for name, public in (("shared", redaction.redact_text(value)), ("node", node.redact(value)),
                                     ("portal", redact(value))):
                    if render(value, new["node" if name == "node" else "portal"]) != public:
                        unfaithful.append((name, entry["id"], value))
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
        self.assertEqual(unfaithful, [])
        self.assertEqual(leaks, [])
        self.assertEqual(extra, [])
        self.assertEqual(stale, [])

    def test_the_metric_sees_one_shown_character(self):
        # Any one secret character that the head's spans no longer hide is a leak against the head itself.
        corpus = load_corpus()
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


# A seeded fuzz of short texts built from the pieces the rules read (names, schemes, options, quotes, clients).
PIECES = ("a", "A", "_", "-", "--", ".", "+", ":", "=", "//", "@", " ", "  ", "\n", "\t", '"', "'", ",", ";", "}", "(",
          "\\", "key", "KEY", "token", "TOKEN", "pass", "pwd", "auth", "secret", "credential", "CREDENTIALS",
          "password", "passphrase", "mysql", "mariadb", "sshpass", "-p", " -p", "Bearer ", "Token ", "Basic ",
          "https://", "1", "x", "S3c", "[redacted]", "api_key", "--api-key", "--token", "--password ", "curl -u ")


def fuzz(count, seed=20261028):
    rng = random.Random(seed)
    return ["".join(rng.choice(PIECES) for _ in range(rng.randint(1, 12))) for _ in range(count)]


def texts():
    return [entry["input"] for entry in load_corpus() if entry["kind"] == "text"] + fuzz(20000)


def parity_misses(inputs, portal_rules, node_rules, alone=False):
    """The (path, text) pairs where the head (``alone``: its base replay only) shows a character that path's base
    rules (verbatim) hid."""
    misses = []
    hidden = (lambda module, text, rules: merged(module._replay(text, rules))) if alone else (
        lambda module, text, rules: merged(module._hidden(text, 0, rules)))
    for text in inputs:
        for path, mine, theirs in (("portal", hidden(redaction, text, portal_rules), base.PORTAL_TEXT_PATTERNS),
                                   ("node", hidden(node, text, node_rules), base.SECRET_PATTERNS)):
            if substitution_hidden(theirs, text) - mine:
                misses.append((path, text))
    return misses


class BaseParityTests(unittest.TestCase):
    def test_each_path_hides_what_its_base_hid(self):
        inputs = texts()
        self.assertEqual(parity_misses(inputs, redaction.BASE_PORTAL_RULES, node.BASE_NODE_RULES), [])
        for value in fuzz(3000, 7):     # argument lists: the base portal's list rule and its text rules per item
            items = value.split(" ")
            own = lambda item: merged(redaction._hidden(item, 0, redaction.BASE_PORTAL_RULES))
            mine = argv_hidden(items, redact(items), own)
            theirs = argv_hidden(items, base.portal_redact(items),
                                 lambda item: substitution_hidden(base.PORTAL_TEXT_PATTERNS, item))
            self.assertEqual(theirs - mine, set(), items)

    def test_the_parity_test_sees_a_missing_base_rule(self):
        # Mutants: each base rule dropped from the replay, which then shows a character its base hid somewhere in
        # the corpus or the fuzz; and the node's credential words narrowed (auth taken out), which the whole head
        # then shows too (auth_header hunter2).
        inputs = texts()
        portal, node_rules = redaction.BASE_PORTAL_RULES, node.BASE_NODE_RULES
        mutants = [(portal[:i] + portal[i + 1:], node_rules) for i in range(len(portal))]
        mutants += [(portal, node_rules[:i] + node_rules[i + 1:]) for i in range(len(node_rules))]
        for index, (portal_rules, node_mutant) in enumerate(mutants):
            with self.subTest(dropped=index):
                self.assertNotEqual(parity_misses(inputs, portal_rules, node_mutant, alone=True), [])
        narrowed = tuple((re.compile(pattern.pattern.replace("|auth", "").replace("|AUTH", "")), replacement)
                         for pattern, replacement in node_rules)
        self.assertNotEqual(parity_misses(inputs, portal, narrowed), [])

    def test_the_node_base_rules_are_the_verbatim_ones(self):
        def chain(rules, text):
            steps = []
            for pattern, replacement in rules:
                if "keep" in pattern.groupindex:
                    text = pattern.sub(lambda found: found.group(0) if found.group("keep") is not None
                                       else found.expand(replacement), text)
                else:
                    text = pattern.sub(replacement, text)
                steps.append(text)
            return steps
        self.assertEqual(len(node.BASE_NODE_RULES), len(base.SECRET_PATTERNS))
        for text in texts():
            self.assertEqual(chain(node.BASE_NODE_RULES, text), chain(base.SECRET_PATTERNS, text), text)
        self.assertEqual([(p.pattern, p.flags, r) for p, r in redaction.BASE_PORTAL_RULES],
                         [(p.pattern, p.flags, r) for p, r in base.PORTAL_TEXT_PATTERNS])



if __name__ == "__main__":
    unittest.main()
