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


def secret_positions(entry):
    """{marker: the positions of its characters in the input} (every occurrence)."""
    value, found = entry["input"], {}
    for marker in entry["markers"]:
        positions = found.setdefault(marker, set())
        for index, item in enumerate([value] if isinstance(value, str) else value):
            at = item.find(marker) if isinstance(item, str) else -1
            while at != -1:
                positions |= {at + k if isinstance(value, str) else (index, at + k) for k in range(len(marker))}
                at = item.find(marker, at + 1)
    return found


def shows(entry, new, old):
    """The secrets (markers) of which ``new`` shows a character that ``old`` hid (a partial exposure counts)."""
    new_hidden, old_hidden = hidden(entry["input"], new), hidden(entry["input"], old)
    return [marker for marker, positions in secret_positions(entry).items()
            if (positions & old_hidden) - new_hidden]


def hides_more(entry, new, old):
    """``new`` hides a character ``old`` showed, outside the secrets, where ``old`` hid every secret character."""
    secrets = secret_positions(entry)
    old_hidden = hidden(entry["input"], old)
    if any(positions - old_hidden for positions in secrets.values()):
        return False
    secret = set().union(*secrets.values()) if secrets else set()
    return bool(hidden(entry["input"], new) - old_hidden - secret)
