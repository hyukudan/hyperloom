"""Frozen redaction rules of 2a7c803 (the base): the node tool's line rules and the portal's rules for text and
argument lists, copied verbatim for the corpus test."""

import re

SECRET_PATTERNS = (
    (re.compile(r"(?i)\b((?:Bearer|Basic|Digest|Token|Negotiate)\s+)[A-Za-z0-9+/=._~:-]+"), r"\1[redacted]"),
    (re.compile(r'''(?ix)(["']?[\w.-]*(?:key|secret|token|pass|pwd|credential|auth)[\w.-]*["']?\s*[:=]\s*)'''
                r'''(?:"[^"]*"|'[^']*'|[^\s,;}]+)'''), r"\1[redacted]"),
    (re.compile(r"(?i)(--[\w-]*(?:key|secret|token|passw(?:or)?d|credential)[\w-]*(?:\s+|=))(?!\[redacted\])\S+"),
     r"\1[redacted]"),
    (re.compile(r"(?i)\b(passw(?:or)?d|passwd|passphrase|pwd)(\s+)(?!\[redacted\])(?![:=])\S+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
    # An environment-style name and its value separated by blanks only (`HF_TOKEN hf_x`, `export DB_PASS s3`): the
    # name is upper case or snake case, so prose such as "512 tokens in 3.1 s" stays readable.
    (re.compile(r"\b((?:[A-Z0-9]+_)*[A-Z0-9]*(?:KEY|SECRET|TOKEN|PASS|PWD|CREDENTIALS?|AUTH)[A-Z0-9]*(?:_[A-Z0-9]+)*"
                r"|(?i:[a-z0-9]+(?:_[a-z0-9]+)*_[a-z0-9]*(?:key|secret|token|pass|pwd|credentials?|auth)[a-z0-9]*"
                r"(?:_[a-z0-9]+)*|[a-z0-9]*(?:key|secret|token|pass|pwd|credentials?|auth)[a-z0-9]*(?:_[a-z0-9]+)+))"
                r"(\s+)(?![:=])(?!\[redacted\])\S+"), r"\1\2[redacted]"),
    # Clients that take the password glued to a short option (`mysql -pX`, `sshpass -pX`, `sshpass -p X`).
    (re.compile(r"(?i)\b((?:mysql\w*|mariadb[\w-]*)\b[^\n]*?\s-p)(?!\[redacted\])(?=\S)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(sshpass\b[^\n]*?\s-p\s*)(?!\[redacted\])\S+"), r"\1[redacted]"),
)


def node_redact(line):
    for pattern, replacement in SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def portal_redact(value, text=True):
    redact = portal_redact
    if isinstance(value, (list, tuple)):
        result, hide = [], False
        for item in value:
            result.append("[redacted]" if hide else redact(item, text))
            hide = isinstance(item, str) and item in ("--api-key", "--handoff-api-key", "--password", "--token")
        return result
    if isinstance(value, str) and text:
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[redacted]", value)
        return re.sub(r'''(?ix)((?:["']?(?:api[_-]?key|handoff[_-]?api[_-]?key|password|secret|token)["']?)\s*[:=]\s*|--(?:api-key|handoff-api-key|password|token)\s+)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                      r"\1[redacted]", value)
    return value


# The portal's text substitutions above, in order, as (pattern, replacement) pairs (for the corpus metric).
PORTAL_TEXT_PATTERNS = (
    (re.compile(r"(?i)(Bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(r'''(?ix)((?:["']?(?:api[_-]?key|handoff[_-]?api[_-]?key|password|secret|token)["']?)\s*[:=]\s*|--(?:api-key|handoff-api-key|password|token)\s+)(?:"[^"]*"|'[^']*'|[^\s,;}]+)'''), r"\1[redacted]"),
)
