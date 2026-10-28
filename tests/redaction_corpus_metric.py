"""The corpus metric (tests/test_redaction_corpus.py and the full-corpus tooling): which characters of an input a
redacted output hides, found by aligning the two, and whether one output shows a secret or hides more than another."""

import difflib
import json


def hidden(value, output):
    """The positions of ``value`` (a text, or an argument list: (item, position)) that ``output`` hides."""
    if isinstance(value, str):
        if not isinstance(output, str):
            return set(range(len(value)))
        # Each [redacted] becomes one character no input holds, so a hidden "red" is not aligned with its letters.
        matcher = difflib.SequenceMatcher(None, value, output.replace("[redacted]", "\x00"), autojunk=False)
        return {at for tag, start, end, _, _ in matcher.get_opcodes() if tag != "equal" for at in range(start, end)}
    return {(index, at) for index, item in enumerate(value) if isinstance(item, str)
            for at in hidden(item, output[index] if index < len(output) else "")}


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def shows(entry, new, old):
    """The secrets (markers) ``new`` shows that ``old`` hid."""
    return [marker for marker in entry["markers"] if marker in text(new) and marker not in text(old)]


def hides_more(entry, new, old):
    """``new`` hides a character ``old`` showed, outside the secrets, where ``old`` showed no secret."""
    if any(marker in text(old) for marker in entry["markers"]):
        return False
    value = entry["input"]
    secret = set()
    for marker in entry["markers"]:
        for index, item in enumerate([value] if isinstance(value, str) else value):
            at = item.find(marker) if isinstance(item, str) else -1
            while at != -1:
                secret |= {at + k if isinstance(value, str) else (index, at + k) for k in range(len(marker))}
                at = item.find(marker, at + 1)
    return bool(hidden(value, new) - hidden(value, old) - secret)
