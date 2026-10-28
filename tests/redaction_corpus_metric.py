"""The corpus metric (tests/test_redaction_corpus.py and the full-corpus tooling): which characters of an input each
set of rules hides, taken from the spans the rules hide (never by comparing texts), and whether one set shows a
secret character another hid or hides a character another showed.

A set of rules is given as an adapter: ``hidden(entry)`` -> {path: positions}. Text paths are "portal" and "node";
an argument list's are "portal" (its items through the text rules) and "recipe" (keys and argument lists only).
Positions are character indexes of the text, or (item, index) pairs of an argument list."""

REDACTED = "[redacted]"


def merged(spans):
    return {at for start, end in spans for at in range(start, end)}


def argv_hidden(value, output, text_hidden):
    """The positions an argument-list redaction hid: a whole item, the value glued to an option ('--user=[redacted]',
    "'--api-key=[redacted]'", -u[redacted]) or what the text rules hid in an item (text_hidden(item), or None)."""
    positions = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            continue
        shown = output[index] if index < len(output) else ""
        if shown == item:
            continue
        if shown == REDACTED:
            positions |= {(index, at) for at in range(len(item))}
            continue
        if text_hidden is not None:
            own = text_hidden(item)
            if render(item, own) == shown:          # what the text rules hid in the item
                positions |= {(index, at) for at in own}
                continue
        cut = shown.find(REDACTED)               # a value glued to an option: prefix [redacted] suffix
        prefix, suffix = shown[:cut], shown[cut + len(REDACTED):]
        if cut != -1 and item.startswith(prefix) and item.endswith(suffix) and len(prefix) + len(suffix) <= len(item):
            positions |= {(index, at) for at in range(len(prefix), len(item) - len(suffix))}
        else:
            positions |= {(index, at) for at in range(len(item))}
    return positions


def render(text, positions):
    """``text`` with each run of hidden positions replaced by [redacted]."""
    parts, last = [], 0
    for at in sorted(positions):
        if at < last:
            continue
        end = at
        while end in positions:
            end += 1
        parts += [text[last:at], REDACTED]
        last = end
    return "".join(parts) + text[last:]


class Rules:
    """An adapter over rules that expose their spans: text_hidden(text) -> positions for the portal and the node,
    argv(value, each) -> the redacted list (each: the text redaction of one item, or None to keep it)."""

    def __init__(self, portal_text, node_text, argv):
        self.portal_text, self.node_text, self.argv = portal_text, node_text, argv

    def hidden(self, entry):
        value = entry["input"]
        if entry["kind"] == "text":
            return {"portal": self.portal_text(value), "node": self.node_text(value)}
        portal = self.argv(value, True)
        recipe = self.argv(value, False)
        return {"portal": argv_hidden(value, portal, self.portal_text), "recipe": argv_hidden(value, recipe, None)}


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
    """The secrets (markers) of which ``new`` (positions) shows a character that ``old`` hid."""
    return [marker for marker, positions in secret_positions(entry).items() if (positions & old) - new]


def hides_more(entry, new, old):
    """``new`` hides a character ``old`` showed, outside the secrets, where ``old`` hid every secret character."""
    secrets = secret_positions(entry)
    if any(positions - old for positions in secrets.values()):
        return False
    secret = set().union(*secrets.values()) if secrets else set()
    return bool(new - old - secret)


def substitution_hidden(patterns, text):
    """The positions of ``text`` that applying ``patterns`` ((pattern, replacement) pairs, in order, as re.sub does)
    hides: the characters of each match that the replacement does not keep (its groups, and a literal it ends with
    that the match ends with too, as the base rules' URL '@')."""
    origin, current, hidden = list(range(len(text))), text, set()
    for pattern, replacement in patterns:
        out, mapped, last = [], [], 0
        for found in pattern.finditer(current):
            out.append(current[last:found.start()])
            mapped += origin[last:found.start()]
            kept = set()
            for piece in _pieces(replacement):
                if isinstance(piece, int):
                    start, end = found.span(piece)
                    if start != -1:
                        out.append(current[start:end])
                        mapped += origin[start:end]
                        kept |= set(range(start, end))
                elif piece != REDACTED and current[found.end() - len(piece):found.end()] == piece:
                    out.append(piece)
                    mapped += origin[found.end() - len(piece):found.end()]
                    kept |= set(range(found.end() - len(piece), found.end()))
                else:
                    out.append(piece)
                    mapped += [None] * len(piece)
            hidden |= {origin[at] for at in range(found.start(), found.end())
                       if at not in kept and origin[at] is not None}
            last = found.end()
        out.append(current[last:])
        mapped += origin[last:]
        current, origin = "".join(out), mapped
    return hidden


def _pieces(replacement):
    """A replacement template as group numbers and literal texts, [redacted] apart (\\1[redacted]@ -> [1,
    "[redacted]", "@"])."""
    pieces, at = [], 0
    while at < len(replacement):
        if replacement[at] == "\\" and replacement[at + 1:at + 2].isdigit():
            pieces.append(int(replacement[at + 1]))
            at += 2
            continue
        end = replacement.find("\\", at + 1)
        end = len(replacement) if end == -1 else end
        literal = replacement[at:end]           # [redacted] apart from what follows it: [redacted]@ keeps "@"
        while REDACTED in literal and literal != REDACTED:
            cut = literal.index(REDACTED)
            pieces += [piece for piece in (literal[:cut], REDACTED) if piece]
            literal = literal[cut + len(REDACTED):]
        if literal:
            pieces.append(literal)
        at = end
    return pieces
