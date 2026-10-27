"""Packaged free-text redaction for placement audit errors and process logs.

Keep these rules aligned with the standalone node log-search tool; the shared
corpus in test_placement_redaction checks drift without a runtime dependency.
"""
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


def redact(line):
    for pattern, replacement in SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line
