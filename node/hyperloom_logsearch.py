"""Bounded, read-only search over hyperloom and configured lab log files.

Python 3.10+, standard library only. Exposes VERBS and run(verb, payload) -> dict (node extension contract).

Roots come from the payload (the install root's ``log/`` directory and configured lab directories). A root is
refused when it is a filesystem root, the home directory itself, a system directory, a UNC/device path, an
alternate data stream or a link. Directories are walked by descriptor without following links (POSIX: every
directory and file is opened relative to its parent with O_NOFOLLOW and must be a directory or a regular file by
fstat, so a swapped ancestor, a FIFO or a device is never read); credential directories (.ssh, .gnupg, secrets,
keys, an install root's etc), every dot directory and dot file (.env, .netrc...) and the payload's exclusions are
pruned during the walk.

Matching is per line on lines capped at 4 KiB, newest files first, within one deadline, a directory-entry budget, a
scanned-bytes budget, a file budget and a result budget; whole lines are redacted before they are cut for display.
A regular expression (a restricted subset: no backreference, lookaround or quantified group) always runs in a
short-lived worker process that is killed and reaped at the deadline, so no pattern can stall the node.
"""

from collections import deque
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import stat
import subprocess
import sys
import threading
import time

VERBS = {"logs-search"}
SCHEMA = "hyperloom/node-v1"
LINE_CAP = 4096
SHOW_CAP = 400
MAX_PATTERN = 128
MAX_ROOTS = 16
MAX_EXCLUDES = 16
MAX_DEPTH = 6
MAX_ENTRIES = 50_000
MAX_CANDIDATES = 20_000
MAX_WORKER_OUTPUT = 1 << 20
LIMITS = {"deadline_s": (0.5, 5.0, 5.0), "max_bytes": (1, 64 << 20, 64 << 20), "max_files": (1, 2000, 2000),
          "max_results": (1, 500, 200), "context": (0, 2, 2)}
OUTPUT_BUDGET = 200 * 1024
EXTENSIONS = {"", ".log", ".txt", ".out", ".err", ".jsonl", ".json", ".md", ".csv"}
SKIP_NAME = re.compile(r"key|secret|token|passw|credential|\.pem$|\.p12$", re.I)
PRUNE_DIRS = {".ssh", ".gnupg", "secrets", "keys", ".hyperloom-boot", ".aws", ".kube", ".docker"}
DENY_POSIX = ("/proc", "/sys", "/dev", "/boot", "/etc", "/run", "/var/lib", "/root")
LEVELS = {"error": re.compile(r"ERROR|FATAL|POISON|Traceback|panic|failed", re.I),
          "warning": re.compile(r"ERROR|FATAL|POISON|Traceback|panic|failed|WARN", re.I)}
KEYS = ("text", "regex", "ignore_case", "root", "roots", "exclude", "since", "until", "process", "level", *LIMITS)
# Redaction: the repository's one set of rules (hyperloom/redaction.py); this tool is deployed as standalone
# files, so the block below is a verbatim copy (tests/test_redaction.py asserts they are identical). Lines are
# redacted one at a time: a command continued with a backslash on the next line is seen without its first line.
# --- shared redaction rules (node/hyperloom_logsearch.py keeps a verbatim copy) ---
# Credentials after an authorization scheme, any name that carries key/secret/token/password/credential/auth
# (AWS_SECRET_ACCESS_KEY=, "db.password": ...), the value after a password word ("password x"), option forms, URL
# user information, the value after an environment-style name, clients that take a password after -p, curl's user
# options, any tool's --user name:password and bare Hugging Face tokens. Every rule reads the original text and
# finds where a value starts; one shell-word reader (_shell_word) reads every value, and the hidden spans are
# merged, so no rule's output can hide a credential from another (`curl -u a:password -U b:c`: the password-word
# rule cannot take -U from curl's scan).
REDACTED = "[redacted]"
# Linear forms: a name is matched as a whole run from the position where the run starts ((?=(run))\N takes the
# run once and never gives it back, as an atomic group would); a lookahead checks that the run carries a
# credential word (group cred). A name whose only such word is token (max_tokens, --max-num-batched-tokens,
# token_ids, "token-identical", pad_token) keeps a number, a list of numbers or a special token as its value
# (max_tokens=512, token: 151643, token-identical: 2/3, token_ids: [1, 2], pad_token: <|endoftext|> stay; any other
# value is hidden); tokenizer names are not credentials. Any name keeps None, null, true or false. An authorization
# scheme word stays readable (Authorization: Bearer [redacted]). These checks read the value without its quotes
# (--user "1000:1000" is a uid:gid as 1000:1000 is).
_PLAIN = r"(?:None|null|true|false|True|False)"
_COUNT = r"(?:\[?[0-9]+(?:[./][0-9]+)*|<[|\w.-]+>)"
_KEPT_PLAIN = re.compile(_PLAIN + r"[)\],;]*")
_KEPT_COUNT = re.compile(r"(?:" + _PLAIN + "|" + _COUNT + r")[)\],;]*")
_UID_GID = re.compile(r"[0-9]+:[0-9]+")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^:]*")       # C:\certs\client.pem: a certificate without a password
_CREDENTIAL_LOOK = re.compile(r"[0-9+/=]|.[A-Z]")
_QUOTES = re.compile(r"""['"]""")
_NESTED_BEARER = re.compile(r"(?i)\bbearer\s+\S")
_DOUBLE = r'''"(?:[^"\\\n]|\\.)*"'''
_DOUBLE_STRING = re.compile(_DOUBLE)
_NOT_OPTION = r"(?!-[A-Za-z](?:\s|$)|--[A-Za-z])"   # a blank-separated value is not the next option (-U, --next)
_START = r"(?!\[redacted\])(?=\S)"                 # where a value starts (one already hidden is not read again)
# A shell word: quoted parts (a backslash escapes inside double quotes and outside quotes) and unquoted
# characters, concatenated (-u'alice:foo'bar and api_key=a' two words' are one word each); a quote with no closing
# quote on its line is an ordinary character; a backslash before a newline ends the word; a pair of escaped
# double quotes is a quoted part too, at depths of 1, 3 and 7 backslashes (\"two words\" in JSON written inside a
# string: {\"password\": \"x y\"}; a quote with more backslashes is inside it: \"a\\\"b c\"). A value inside a quoted
# string that opens and closes at a word boundary (-H "X-Api-Key: abc", '{"password": "x y"}', bash -c "curl -u
# a:b") ends where that string ends; a string glued to more text (' ... a:b'c d') is not such a string, so the
# value pairs its own quotes. Each value is read once: a reader never goes back over text it has read.
_WORD = r"""(?:'[^'\n]*'|""" + _DOUBLE + r"""|\\.|[^\s'"\\]|['"\\])+"""
_BOUNDARY = " \t\n\r\f\v=:([{,"
_BOUNDARY_AFTER = " \t\n\r\f\v,;)]}"
_LEXEME = re.compile(r"""[^'"\\]+|\\[\s\S]?""")
_BACKSLASHES = re.compile(r"\\+")
# Every quote of a text with the backslashes right before it is found once (_QUOTE_RUN), and each kind of closing
# quote a value may ask for gets one sorted list of positions (_closes), so finding where a quoted part ends never
# reads the text again. A plain apostrophe closes at the next apostrophe; a plain double quote at the next one not
# escaped (an even run). Escaped double quotes open on a run c of 1, 3 or 7 backslashes (\"...\" in JSON written
# inside a string; \\\"...\\\" when that is written inside a string again) and close, first, at a quote whose run r
# has r mod 2(c+1) == c, as that encoding writes it (at depth 1, \\\\\" ends a value that ends in a backslash;
# \\\" is a quote inside the value), else at any odd run of at least c, else at the next escaped quote. An escaped
# apostrophe (a repr, a repr of a repr) opens only where a value starts and closes the same way, before a
# boundary (api_key=\'two words\'; \'a b\\\' ends in a backslash; \'rock \\\'n\\\' roll\' holds apostrophes; while
# it\'s fine stays one escape). A close is on the same line, before the end of an enclosing string.
_QUOTE_RUN = re.compile(r"""(?<!\\)\\*['"]""")
_NEWLINE = re.compile(r"\n")
_CLOSE_BOUNDARY = " \t\n\r\f\v,;)]}'\""


def _strings(text):
    """(starts, ends, cache): the quoted strings of ``text`` that open at a word boundary, in order (read once,
    left to right, as a shell reads them; a quote with no closing quote on its line is an ordinary character), and
    a cache for the closing quotes the value reader looks up (_closes)."""
    starts, ends, failed, pos, size = [], [], {"'": -1, '"': -1}, 0, len(text)
    while pos < size:
        quote = text[pos]
        if quote not in "'\"":
            pos = _LEXEME.match(text, pos).end()
            continue
        end = -1
        if pos >= failed[quote]:
            if quote == "'":
                end = text.find("'", pos + 1)
                end = end if end != -1 and "\n" not in text[pos + 1:end] else -1
            else:
                found = _DOUBLE_STRING.match(text, pos)
                end = found.end() - 1 if found else -1
            if end == -1:           # no closing quote on this line: none for a later quote of this kind either
                line = text.find("\n", pos)
                failed[quote] = size if line == -1 else line
        if end == -1:
            pos += 1
            continue
        if (pos == 0 or text[pos - 1] in _BOUNDARY) and (end + 1 == size or text[end + 1] in _BOUNDARY_AFTER):
            starts.append(pos)
            ends.append(end)
        pos = end + 1
    return starts, ends, {}


def _first_after(positions, at):
    """The index of the first of the sorted ``positions`` after ``at``."""
    low, high = 0, len(positions)
    while low < high:
        middle = (low + high) // 2
        if positions[middle] <= at:
            low = middle + 1
        else:
            high = middle
    return low


def _closes(text, cache, key):
    """The sorted positions of the quotes that close a quoted part of this kind: key is (quote, "plain"), (quote,
    tier, opening run) with tier 1 (the encoding's own run) or 2 (any odd run at least as long), or (quote, "any")."""
    if key in cache:
        return cache[key]
    if "quotes" not in cache:
        size = len(text)
        cache["quotes"] = [(found.end() - 1, found.end() - 1 - found.start(), text[found.end() - 1],
                            found.end() == size or text[found.end()] in _CLOSE_BOUNDARY)
                           for found in _QUOTE_RUN.finditer(text)]
        cache["lines"] = [found.start() for found in _NEWLINE.finditer(text)]
    quote, tier = key[0], key[1]
    depth = key[2] if len(key) > 2 else 0
    cache[key] = [at for at, run, char, boundary in cache["quotes"] if char == quote and (
        (quote == "'" or run % 2 == 0) if tier == "plain" else run % 2 == 1 and (
            tier == "any" or (quote == '"' or boundary) and (
                run % (2 * depth + 2) == depth if tier == 1 else run >= depth)))]
    return cache[key]


def _close(text, cache, keys, at, limit):
    """The first quote after ``at`` that closes a part of the first of ``keys`` that has one on the same line before
    ``limit``; -1 if none."""
    _closes(text, cache, ("'", "plain"))
    lines = cache["lines"]
    line = _first_after(lines, at)
    end = min(limit, lines[line] if line < len(lines) else len(text))
    for key in keys:
        positions = _closes(text, cache, key)
        index = _first_after(positions, at)
        if index < len(positions) and positions[index] < end:
            return positions[index]
    return -1


def _shell_word(text, pos, stops="", strings=None):
    """(end, value): the shell word that starts at ``pos`` (also ending at any character of ``stops``: an
    assignment's value ends at , ; }) and its text without quotes or escapes. ``strings``: _strings(text), shared by
    every value of the text (it keeps the positions of the closing quotes)."""
    starts, ends, cache = _strings(text) if strings is None else strings
    limit, enclosing = len(text), ""
    low = _first_after(starts, pos - 1)  # the last string that opens before pos
    if low and pos < ends[low - 1]:
        limit, enclosing = ends[low - 1], text[starts[low - 1]]
    value, at, size = [], pos, len(text)
    while at < size:
        char = text[at]
        if at >= limit:                 # the enclosing string ends
            break
        if char.isspace() or char in stops or char == enclosing or text[at:at + 2] == "\\\n":
            break
        if char == "\\":
            run = _BACKSLASHES.match(text, at, limit).end() - at
            quote = text[at + run:at + run + 1]
            end = -1
            if run in (1, 3, 7) and (quote == '"' or quote == "'" and at == pos):
                end = _close(text, cache, ((quote, 1, run), (quote, 2, run), (quote, "any")), at + run, limit)
            elif run % 2 and (quote == '"' or quote == "'" and at == pos):
                end = _close(text, cache, ((quote, "any"),), at + run, limit)
            if end != -1:
                content = end                   # the value ends before the closing quote's backslashes
                while content > at + run + 1 and text[content - 1] == "\\":
                    content -= 1
                value.append(text[at + run + 1:content])
                at = end + 1
                continue
            value.append(text[at + 1:min(at + run + run % 2, limit)])
            at = min(at + run + run % 2, limit)
            continue
        if char in "'\"":
            end = _close(text, cache, ((char, "plain"),), at, limit)
            if end != -1:
                value.append(text[at + 1:end])
                at = end + 1
                continue
        value.append(char)
        at += 1
    return min(at, limit, size), "".join(value)


def _unquoted(value):
    """``value`` without the quotes around it, its backslashes kept (a Windows path)."""
    return value[1:-1] if len(value) > 1 and value[0] in "'\"" and value[-1] == value[0] else value


def _keep_plain(match, value):
    return _KEPT_PLAIN.fullmatch(value) is not None


def _keep_token_value(match, value):
    """A name with a credential word keeps only None/null/true/false; a token name also keeps a count."""
    named = match.groupdict()
    credential = named.get("cred") or named.get("cred2")
    return (_KEPT_PLAIN if credential else _KEPT_COUNT).fullmatch(value) is not None


def _keep_scheme_value(match, value):
    """After a scheme word elsewhere, Bearer's value is hidden; another scheme's only when it looks like a
    credential (8+ characters, not only digits, with a digit, +, / or =, or an upper-case letter after the first):
    "Token counters", "basic setup" and "the token 12345678" stay readable. A value that holds a Bearer and its
    value (Token 'Bearer abc') is hidden: the Bearer inside it is not searched for again."""
    return not match.group("bearer") and not _NESTED_BEARER.search(value) and not (
        len(value) >= 8 and not value.isdigit() and _CREDENTIAL_LOOK.search(value))


def _keep_user(match, value):
    """--user keeps a value without a colon and a numeric uid:gid (docker run --user 1000:1000)."""
    return ":" not in value or _UID_GID.fullmatch(value) is not None


def _keep_nothing(match, value):
    return False


# (pattern, what ends a value besides a blank, which values stay): the pattern ends where the value starts; a
# pattern with a group v names its value itself.
SECRET_PATTERNS = (
    # An authorization header's credentials whatever they look like; after a scheme word elsewhere, see
    # _keep_scheme_value.
    (re.compile(r"""(?i)\b(?:proxy-)?authorization["']?\s*[:=]\s*["']?(?:Bearer|Basic|Digest|Token|Negotiate)\s+"""
                + _START), "", _keep_nothing),
    (re.compile(r"""(?i)\b(?:(?P<bearer>Bearer)|Basic|Digest|Token|Negotiate)\s+""" + _START), "",
     _keep_scheme_value),
    (re.compile(r'''(?ix)(?<![\w.-])(?:(?<!\\)\\*["'])?(?:(?=[\w.-]*?(?P<cred>key|secret|pass|pwd|credential|auth))'''
                r'''|(?=[\w.-]*?token(?!iz)))(?=([\w.-]*))\2(?:\\*["'])?\s*[:=]\s*'''
                r'''(?!(?:Bearer|Basic|Digest|Token|Negotiate)\s+\[redacted\])'''
                r'''(?:(?:Bearer|Basic|Digest|Token|Negotiate)\s+(?=[^\s,;}:=]))?''' + _START), ",;}",
     _keep_token_value),
    # An option whose name carries a credential word (--hf-token x, --db-password=x, --oauth2-bearer x), also
    # glued after other characters of the word (x--api-key x): matched from the start of the word, up to its
    # first --.
    (re.compile(r"(?i)(?<![\w-])\w*(?:-\w+)*--(?:(?=[\w-]*?(?P<cred>key|secret|passw(?:or)?d|credential|bearer|(?<![a-z])pass(?:phrase)?(?![\w-])))"
                r"|(?=[\w-]*?token(?!iz)))(?=([\w-]*))\2(?:\s+|=)" + _START), "", _keep_token_value),
    # The value after a password word (not an option that follows it: `-u a:password -U b:c`).
    (re.compile(r"(?i)\b(?:passw(?:or)?d|passwd|passphrase|pwd)\s+(?![:=])" + _NOT_OPTION + _START), "",
     _keep_plain),
    # URL user information (also an empty user name: redis://:password@host).
    (re.compile(r"(?i)(?<![a-z0-9+.-])(?=[a-z0-9+.-]*?(?<!\w)[a-z])(?=([a-z0-9+.-]*))\1://"
                r"(?P<v>[^/\s:@]*:[^/\s@]+)@"), "", _keep_nothing),
    # An environment-style name and its value separated by blanks only (`HF_TOKEN hf_x`, `export DB_PASS s3`): the
    # name is upper case, or snake case ending with the credential word (hf_token, db_pass), so prose such as
    # "512 tokens in 3.1 s" or "key_cache shape" stays readable. A snake-case name inside a value another rule hides
    # is that value, not a name, before a parenthesis (HF_TOKEN hf_probe_secret (the job...): "(the" stays).
    (re.compile(r"\b(?:(?:(?=[A-Z0-9_]*?(?P<cred>KEY|SECRET|PASS|PWD|CREDENTIALS?|AUTH))|(?=[A-Z0-9_]*?TOKEN(?!IZ)))"
                r"[A-Z0-9]+(?:_[A-Z0-9]+)*"
                r"|(?P<lower>(?i:(?:(?=[a-z0-9_]*?(?P<cred2>key|secret|pass|pwd|credentials?|auth))"
                r"|(?=[a-z0-9_]*?token(?!iz)))"
                r"[a-z0-9]+(?:_[a-z0-9]+)*_[a-z0-9]*(?:key|secret|token|pass(?:wd|word)?|pwd|credentials?|auth)s?)))"
                r"\s+(?![:=])" + _NOT_OPTION + _START), "", _keep_token_value),
    # Any other tool's `--user name:password` (a numeric uid:gid, as in `docker run --user 1000:1000`, stays; a
    # --user followed by another option, as in `systemd-run --user --setenv=A=/x:/y`, takes no value; the option
    # name may be quoted: wget "--user" a:b).
    (re.compile(r"""(?:(?P<q>["'])--(?:proxy-)?user(?P=q)\s+(?!-)|--(?:proxy-)?user(?:\s+(?!-)|=))""" + _START), "",
     _keep_user),
    # A bare Hugging Face access token (no name before it): hf_ and 30 or more token characters, or an hf_ word
    # that calls itself a secret (hf_probe_secret); library and model names such as hf_transfer, hf_hub_download
    # or hf_token stay readable. A bare GitHub token: ghp_, gho_, ghu_, ghs_, ghr_ or github_pat_ and 30 or more
    # characters, or such a word that calls itself a secret (ghs_runner, gho_offset stay).
    (re.compile(r"(?P<v>\bhf_(?:[A-Za-z0-9]{30,}|(?=\w*?(?i:secret))[A-Za-z0-9_]+)\b"
                r"|\b(?:gh[pousr]|github_pat)_(?:[A-Za-z0-9_]{30,}|(?=\w*?(?i:secret))[A-Za-z0-9_]+)\b)"), "",
     _keep_nothing),
)
# Clients that take a password after -p: mysql and mariadb tools glued (`mysql -pX`, `mariadb-dump -p'x y'`; -P is
# the port), anywhere later on the line; sshpass among its own options (`sshpass -p X`, `sshpass -f file` has none,
# so the wrapped command's -p, ssh's port, stays). (client, password option, what a miss skips: the rest of the
# line, or sshpass's options, taken once and never re-read from each client word inside them); sshpass's flags
# may be grouped (-vp X).
_SSHPASS_OPTIONS = r"(?:\s+(?:-[fdP](?:\s+|(?=\S))" + _WORD + r"|-[ehvV]+(?!\S)))*"
GLUED_PASSWORD = (
    (re.compile(r"(?i)\b(?:mysql\w*|mariadb[\w-]*)\b"),
     re.compile(r"[^\n]*?\s-p" + _START), re.compile(r"[^\n]*")),
    (re.compile(r"(?i)\bsshpass\b"),
     re.compile(r"(?=(" + _SSHPASS_OPTIONS + r"))\1\s+-[ehvV]*p\s*" + _START),
     re.compile(r"(?=(" + _SSHPASS_OPTIONS + r"))\1")),
)
# curl's user and password options (-u user:pw, -su user:pw, -uuser:pw, --user, --user=, the proxy's -U and
# --proxy-user, also quoted: "-u" "a:b", "--user=a:b"; a client certificate's password: -E cert:pw, --cert
# cert:pw, the whole value when it holds a colon that is not a Windows drive's), every one of them (also after --next), within the same
# command: up to a newline (a backslash continues the line), a pipe, `;`, `&&` or a background `&` outside quotes (a
# backslash-escaped character is not a separator; a value glued to a separator stays whole: `-u a:b;c` hides
# `a:b;c`). An unpaired quote is an ordinary character. Only option letters that take no argument can come before
# u/U in one word (-sSu; -XPUT is -X PUT). The executable is matched in any case (CURL.EXE) and may be quoted, with
# a path ("/usr/bin/curl", "C:\Program Files\curl\curl.exe"): the scan starts at its opening quote, so its closing
# quote is not read as the start of a quoted argument.
CURL_COMMAND = re.compile(r"""(?i)(?:(?P<q>["'])(?:[^"'\n]*[\\/])?)?\bcurl(?:\.exe)?\b(?(q)(?P=q))"""
                          r"""(?:'[^'\n]*'|""" + _DOUBLE + r"""|\\[\s\S]|&(?![&\s]|$)|[^\n|;&'"\\]|['"\\])*""")
CURL_FLAGS = r"[sSfLkvqiIOJRgGnNpBMljaZV#:0-9]"
CURL_OPTION = re.compile(r"""(?<=\s)(?P<o>(?P<q>["'])(?:-""" + CURL_FLAGS + r"""*(?:[uU]|(?P<e>E))"""
                         r"""|--(?:proxy-)?(?:user|(?P<c>cert)))(?P=q)\s+"""
                         r"""|["']?(?:-""" + CURL_FLAGS + r"""*(?:[uU]|(?P<e2>E))\s*"""
                         r"""|--(?:proxy-)?(?:user|(?P<c2>cert))(?:\s+|=)))""" + _START
                         + r"""|'[^'\n]*'|""" + _DOUBLE + r"""|\\.""")


def _values(text, strings):
    """(start, end, name): every value the rules and the -p clients hide; name, when set, is where a snake-case
    name ends (its last character): that value is hidden only if the name is not inside another hidden value. A
    snake-case name's own value (name -1) is not such a value: db_pass a_key (x hides x."""
    for pattern, stops, keep in SECRET_PATTERNS:
        pos, read = 0, 0
        while True:
            found = pattern.search(text, pos)
            if found is None:
                break
            if "v" in pattern.groupindex:
                yield (*found.span("v"), None)
                pos = found.end()
                continue
            # The search goes on from where the value starts, so a value that is itself a name (--api-key
            # --password x, Token Bearer x) is read as one too; a value that starts inside one already read is not
            # read again (each character is read once: --key=--key=--key=...).
            pos = found.end()
            if pos < read:
                continue
            end, value = _shell_word(text, pos, stops, strings)
            # A snake-case name before a parenthesis ("hf_probe_secret (the job ...)") may be another value's
            # end: its value is hidden only if the name is not inside another hidden value, and is not taken as
            # read, so a name inside it is still read on its own.
            conditional = found.groupdict().get("lower") and text[pos] == "(" and not _QUOTES.search(text, pos, end)
            if end > pos and not keep(found, value):
                yield pos, end, found.end("lower") - 1 if conditional else -1 if found.groupdict().get("lower") else None
            read = pos if conditional else end
    for client, option, miss in GLUED_PASSWORD:
        pos = 0
        while True:
            found = client.search(text, pos)
            if found is None:
                break
            value = option.match(text, found.end())
            if value is None:
                pos = max(miss.match(text, found.end()).end(), found.end())
                continue
            end = _shell_word(text, value.end(), "", strings)[0]
            yield value.end(), end, None
            pos = max(end, value.end())


def _curl_users(text, strings):
    pos, last = 0, 0
    while True:
        command = CURL_COMMAND.search(text, pos)
        if command is None:
            break
        for option in CURL_OPTION.finditer(text, command.start(), command.end()):
            if option.group("o") is None or option.start() < last:
                continue
            certificate = option.group("e") or option.group("c") or option.group("e2") or option.group("c2")
            end = _shell_word(text, option.end(), "", strings)[0]      # it may go on past the command
            raw = _unquoted(text[option.end():end])     # a certificate path as written (C:\certs\client.pem)
            hidden = end > option.end() and (not certificate or ":" in raw and not _WINDOWS_PATH.fullmatch(raw))
            if hidden:
                yield option.end(), end
            if hidden or end > option.end() and text[option.end()] != "-":
                last = end              # read, hidden or not (a certificate without a password): not read again;
                #                         a certificate kept that looks like an option (--cert --next) is one
        pos = command.end()             # a value that runs on past it may hide the next command's start


def redact_text(text):
    """``text`` with every credential the shared rules recognise replaced by [redacted] (overlapping or touching
    hidden spans become one)."""
    strings = _strings(text)
    found = [*_values(text, strings), *((start, end, None) for start, end in _curl_users(text, strings))]
    spans = sorted((start, end) for start, end, name in found if name is None or name == -1)
    union, ends = [], []                # the hidden spans merged, to look a name up in them
    for start, end in sorted((start, end) for start, end, name in found if name is None):
        if union and start <= ends[-1]:
            ends[-1] = max(ends[-1], end)
        else:
            union.append(start)
            ends.append(end)
    for start, end, name in found:
        if name is not None and name != -1:
            low, high = 0, len(union)
            while low < high:           # the last merged span that starts at or before the name
                middle = (low + high) // 2
                if union[middle] <= name:
                    low = middle + 1
                else:
                    high = middle
            if not (low and name < ends[low - 1]):
                spans.append((start, end))
    parts, last = [], 0
    for start, end in sorted(spans):
        if end <= start:
            continue
        if start <= last and parts:
            last = max(last, end)       # within or touching the previous hidden span
            continue
        parts += [text[last:start], REDACTED]
        last = end
    return "".join(parts) + text[last:]
# --- end of shared redaction rules ---


FD_WALK = (os.open in os.supports_dir_fd and os.scandir in os.supports_fd and hasattr(os, "O_DIRECTORY")
           and hasattr(os, "O_NOFOLLOW"))
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def redact(line):
    return redact_text(line)


def show(line):
    """A result line for display: redacted whole, then cut."""
    return redact(line)[:SHOW_CAP]


def check_regex(pattern):
    """Refuse what the restricted subset excludes; returns the compiled pattern."""
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"regex longer than {MAX_PATTERN} characters")
    if re.search(r"\\[1-9]|\(\?P=|\(\?<?[=!]", pattern):
        raise ValueError("regex backreferences and lookarounds are not accepted")
    escaped, in_class = False, False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == ")" and pattern[index + 1:index + 2] in ("*", "+", "?", "{"):
            raise ValueError("a quantified group is not accepted (quantify single characters or classes)")
    try:
        return re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as exc:      # a{4294967296}: OverflowError, not re.error
        raise ValueError("invalid regex: " + str(exc)) from None


def _number(payload, key):
    low, high, default = LIMITS[key]
    value = payload.get(key, default)
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{key} must be a number in {low}..{high}")
    return value


def options(payload):
    """Validated search options (raises ValueError)."""
    unknown = set(payload) - set(KEYS)
    if unknown:
        raise ValueError("unknown payload keys: " + ", ".join(sorted(unknown)))
    if ("text" in payload) == ("regex" in payload):
        raise ValueError("give exactly one of text or regex")
    needle = payload.get("text", payload.get("regex"))
    if not isinstance(needle, str) or not needle or len(needle) > MAX_PATTERN or any(ord(c) < 32 for c in needle):
        raise ValueError(f"the search text must be 1..{MAX_PATTERN} printable characters")
    ignore = payload.get("ignore_case", False)
    if type(ignore) is not bool:
        raise ValueError("ignore_case must be boolean")
    if "regex" in payload:
        check_regex(needle)
    opts = {"text": payload.get("text"), "regex": payload.get("regex"), "ignore_case": ignore,
            **{key: _number(payload, key) for key in LIMITS}}
    for key in ("since", "until"):
        value = payload.get(key)
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1e11):
            raise ValueError(key + " must be an epoch time")
        opts[key] = value
    process = payload.get("process")
    if process is not None and (not isinstance(process, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", process)):
        raise ValueError("process must be 1..64 characters [A-Za-z0-9._-]")
    opts["process"] = process
    level = payload.get("level", "any")
    if level not in ("any", "warning", "error"):
        raise ValueError("level must be any, warning or error")
    opts["level"] = level
    roots = []
    if payload.get("root") is not None:
        if not isinstance(payload["root"], str):
            raise ValueError("root must be a path")
        roots.append(["hyperloom log", {"install_root": payload["root"]}])
    extra = payload.get("roots", [])
    if not isinstance(extra, list) or len(extra) > MAX_ROOTS:
        raise ValueError(f"roots must be a list of at most {MAX_ROOTS} directories")
    for item in extra:
        roots.append([item, item])
    if not roots:
        raise ValueError("no root to search")
    excludes = payload.get("exclude", [])
    if not isinstance(excludes, list) or len(excludes) > MAX_EXCLUDES or any(
            not isinstance(e, str) or not e or len(e) > 1024 or not Path(e).is_absolute() for e in excludes):
        raise ValueError(f"exclude must be a list of at most {MAX_EXCLUDES} absolute paths")
    opts["roots"], opts["exclude"] = roots, excludes
    return opts


def install_log(value):
    """The log directory of a marked hyperloom install root, as the configured path (read-only check: nothing is
    created; the configured path stays the authority, it is never replaced by a resolved one)."""
    root = checked_root(value)
    marker = root / ".hyperloom-root"
    if os.path.islink(marker) or not marker.is_file():
        raise ValueError("install root is not a marked hyperloom root")
    return str(Path(value) / "log")


def _reparse(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE)


def protected(real):
    """Why a resolved directory must not be searched (as a root or anywhere below one), or None."""
    if real.parent == real:
        return "a filesystem root cannot be searched"
    if os.name == "nt":
        for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            item = os.environ.get(key)
            if item:
                item = Path(os.path.realpath(item))
                if real == item or real.is_relative_to(item):
                    return "system directories cannot be searched"
    else:
        posix = real.as_posix()
        if any(posix == d or posix.startswith(d + "/") for d in DENY_POSIX):
            return "system directories cannot be searched"
    if real == Path(os.path.realpath(Path.home())):
        return "the whole home directory cannot be searched; configure a log directory"
    if any(part.lower() in PRUNE_DIRS for part in real.parts):
        return "credential directories cannot be searched"
    for ancestor in (real, *real.parents):
        relative = [p.lower() for p in real.parts[len(ancestor.parts):]]
        if relative[:1] == ["etc"] and (ancestor / ".hyperloom-root").is_file():
            return "an install root's etc directory cannot be searched"
    return None


def checked_root(value):
    """An absolute, existing, link-free directory outside system and credential directories (resolved)."""
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError("a root must be an absolute path")
    if os.name == "nt":
        if value.startswith(("\\\\", "//")) or ":" in value[2:] or not re.match(r"[A-Za-z]:[\\/]", value):
            raise ValueError("a root must be a local drive path (no UNC, device or stream path)")
    path = Path(value)
    if not path.is_absolute() or ".." in PurePath(value).parts:
        raise ValueError("a root must be an absolute path without '..'")
    for ancestor in (path, *path.parents):
        if _reparse(ancestor) and ancestor.parent != ancestor:
            raise ValueError("a root must not be or lie under a symbolic link or reparse point")
    real = Path(os.path.realpath(path))
    if os.path.normcase(str(real)) != os.path.normcase(os.path.abspath(value)):
        raise ValueError("a root must not resolve elsewhere (a link or junction in its path)")
    reason = protected(real)
    if reason:
        raise ValueError(reason)
    return Path(os.path.abspath(value))


class Budget:
    def __init__(self, opts, deadline):
        self.opts, self.deadline = opts, deadline
        self.entries = 0

    def expired(self):
        return time.monotonic() >= self.deadline


def _wanted(name, opts):
    # Dot files (.env, .netrc, .bash_history...) are never searched.
    if name.startswith(".") or SKIP_NAME.search(name) or PurePosixPath(name).suffix.lower() not in EXTENSIONS:
        return False
    return not opts["process"] or opts["process"].lower() in name.lower()


def _in_window(info, opts):
    if opts["since"] is not None and info.st_mtime < opts["since"]:
        return False
    # a file written long after the window still holds older lines; only one untouched since then is skipped
    return opts["until"] is None or info.st_mtime <= opts["until"] + 86400


def _excluded(real_dir, excludes):
    return any(real_dir == e or real_dir.is_relative_to(e) for e in excludes)


def walk(label, root, opts, budget, report, found):
    """Append (label, root, parts, mtime, size) of candidate files under one checked root."""
    excludes = [Path(os.path.realpath(e)) for e in opts["exclude"]]
    if _excluded(root, excludes):
        report["roots"].append({"root": label, "error": "excluded"})
        return
    stack = [(root, (), 0)]
    while stack:
        directory, parts, depth = stack.pop()
        if FD_WALK:
            try:
                fd = open_dir(root, parts)
            except OSError:
                continue
            target = fd
        else:
            fd, target = None, directory
        try:
            with os.scandir(target) as entries:
                rows = []
                for entry in entries:
                    budget.entries += 1
                    if budget.entries > MAX_ENTRIES or budget.expired():
                        report["truncated"].append("entries" if budget.entries > MAX_ENTRIES else "deadline")
                        return
                    rows.append(entry)
                marked = any(e.name.lower() == ".hyperloom-root" and e.is_file(follow_symlinks=False) for e in rows)
                for entry in rows:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if (depth + 1 > MAX_DEPTH or entry.name.lower() in PRUNE_DIRS or entry.name.startswith(".")
                                or (marked and entry.name.lower() == "etc")
                                or SKIP_NAME.search(entry.name)):
                            continue
                        child = directory / entry.name
                        if not _excluded(child, excludes) and protected(child) is None:
                            stack.append((child, parts + (entry.name,), depth + 1))
                    elif stat.S_ISREG(info.st_mode) and _wanted(entry.name, opts) and _in_window(info, opts):
                        found.append((label, root, parts + (entry.name,), info.st_mtime, info.st_size))
                        if len(found) >= MAX_CANDIDATES:
                            report["truncated"].append("candidates")
                            return
        except OSError:
            continue
        finally:
            if fd is not None:
                os.close(fd)


def open_dir(root, parts):
    """POSIX: a descriptor of root/parts opened from "/" one component at a time with O_DIRECTORY|O_NOFOLLOW, so no
    component of the path (the root's ancestors included) can be a link."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*Path(root).parts[1:], *parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def _windows_handle(path, directory=False):
    """Windows: (handle, final path, attributes) of a path opened without following a reparse point."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]

    class Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
                    ("written", wintypes.FILETIME), ("serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                    ("index_low", wintypes.DWORD)]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    flags = 0x00200000 | (0x02000000 if directory else 0)       # OPEN_REPARSE_POINT (| BACKUP_SEMANTICS for dirs)
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, flags, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError("cannot open " + str(path))
    try:
        info = Info()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError("cannot read file information")
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel.GetFinalPathNameByHandleW(handle, buffer, 1024, 0):
            raise OSError("cannot read the final path")
        final = buffer.value[4:] if buffer.value.startswith("\\\\?\\") else buffer.value
        return handle, Path(final), info.attributes
    except OSError:
        kernel.CloseHandle(handle)
        raise


def _windows_close(handle):
    import ctypes
    ctypes.WinDLL("kernel32").CloseHandle(handle)


def windows_root(root):
    """Windows: the final path of the opened root directory, which must be the configured root itself (normalized
    case): a junction or link anywhere in its ancestors makes them differ. The protected-path policy is applied to
    that final path."""
    top, final, attributes = _windows_handle(root, directory=True)
    _windows_close(top)
    if attributes & REPARSE or not attributes & 0x10:
        raise OSError("the root is not a plain directory")
    if os.path.normcase(str(final)).rstrip("\\") != os.path.normcase(os.path.abspath(str(root))).rstrip("\\"):
        raise OSError("the root resolves elsewhere (a link or junction in its path)")
    reason = protected(Path(final))
    if reason:
        raise OSError(reason)
    return final


def open_file(root, parts):
    """A binary stream of a regular file root/parts reached without following any link: POSIX by descriptors from "/",
    Windows by a handle whose own final path must lie inside the root's (checked on the opened object)."""
    if FD_WALK:
        parent = open_dir(root, parts[:-1])
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular file")
            os.set_blocking(fd, True)
            return os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            raise
    if os.name == "nt":
        import msvcrt
        expected = Path(root).joinpath(*parts)
        handle, final, attributes = _windows_handle(expected)
        # The opened file's own final path must be exactly the walked one: a junction or link swapped in anywhere
        # (inside the root too) makes them differ, so a redirected open never reaches a pruned directory.
        if attributes & (REPARSE | 0x10) or os.path.normcase(str(final)) != os.path.normcase(str(expected)):
            _windows_close(handle)
            raise OSError("not the walked regular file")
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")
    raise OSError("no safe way to open files on this platform")


def candidates(opts, report, budget):
    found = []
    for label, value in opts["roots"]:
        try:
            configured = install_log(value["install_root"]) if isinstance(value, dict) else value
            root = checked_root(configured)
            if os.name == "nt":
                # The configured path is the authority: the opened root must be it, and the walk uses that path.
                root = windows_root(configured)
            if not root.is_dir():
                raise ValueError("not a directory")
        except (ValueError, OSError) as exc:
            report["roots"].append({"root": label, "error": str(exc)[:200]})
            continue
        report["roots"].append({"root": label, "error": None})
        walk(label, root, opts, budget, report, found)
        if report["truncated"]:
            break
    found.sort(key=lambda row: row[3], reverse=True)
    return found


def matcher(opts):
    if opts["regex"] is not None:
        compiled = re.compile(opts["regex"], re.I if opts["ignore_case"] else 0)
        return lambda line: compiled.search(line) is not None
    needle = opts["text"]
    if opts["ignore_case"]:
        needle = needle.casefold()
        return lambda line: needle in line.casefold()
    return lambda line: needle in line


class Replay:
    """readline() over an already read (and charged) head, then the rest of the file."""

    def __init__(self, head, stream):
        self.head, self.pos, self.stream = head, 0, stream

    def pending(self):
        return len(self.head) - self.pos

    def readline(self, limit):
        out = b""
        if self.pos < len(self.head):
            stop = self.head.find(b"\n", self.pos, self.pos + limit)
            take = (stop + 1 - self.pos) if stop != -1 else min(limit, len(self.head) - self.pos)
            out = self.head[self.pos:self.pos + take]
            self.pos += take
            if out.endswith(b"\n") or len(out) >= limit or self.pos < len(self.head):
                return out
        return out + self.stream.readline(limit - len(out)) if limit > len(out) else out


def unread_on_disk(stream):
    """Whether bytes remain in the file beyond what was read (without reading them)."""
    try:
        return os.fstat(stream.fileno()).st_size > stream.tell()
    except OSError:
        return True


def lines_of(source, room, budget):
    """Yield (text, bytes read from disk) per line, capped at LINE_CAP and at the remaining allowance (replayed head
    bytes cost nothing); the rest of a longer line is consumed (bounded by the budget and the deadline), not matched."""
    while True:
        allowance = room() + source.pending()
        if allowance <= 0:
            return
        before = source.pending()
        raw = source.readline(min(LINE_CAP, allowance))
        if not raw:
            return
        fresh = max(0, len(raw) - before)
        if not raw.endswith(b"\n"):
            while fresh < room() and not budget.expired():
                rest = source.readline(min(65536, max(1, room() - fresh)))
                fresh += len(rest)
                if not rest or rest.endswith(b"\n"):
                    break
        yield raw.rstrip(b"\r\n").decode("utf-8", "replace"), fresh


def search(opts, emit, deadline):
    """Scan the roots; emit(result) for each match; returns the report (counts, truncation, errors)."""
    report = {"files": 0, "bytes": 0, "results": 0, "truncated": [], "roots": [], "skipped_binary": 0}
    budget = Budget(opts, deadline)
    match = matcher(opts)
    level = LEVELS.get(opts["level"])
    context = int(opts["context"])
    room = lambda: opts["max_bytes"] - report["bytes"]      # noqa: E731
    for label, root, parts, mtime, size in candidates(opts, report, budget):
        stop = "deadline" if budget.expired() else "files" if report["files"] >= opts["max_files"] else \
            "bytes" if room() <= 0 else None
        if stop:
            report["truncated"].append(stop)
            break
        report["files"] += 1          # every attempted file counts, binary or unreadable ones too
        try:
            stream = open_file(root, parts)
        except OSError:
            continue
        with stream:
            head = stream.read(min(4096, room()))
            report["bytes"] += len(head)          # every byte read from disk is charged once
            if b"\0" in head:
                report["skipped_binary"] += 1
                if room() <= 0 and unread_on_disk(stream):
                    report["truncated"].append("bytes")
                    break
                continue
            source = Replay(head, stream)
            before, pending, number = deque(maxlen=context), [], 0
            for text, fresh in lines_of(source, room, budget):
                number += 1
                report["bytes"] += fresh
                for item in pending:
                    item["after"].append(show(text))
                for item in [i for i in pending if len(i["after"]) >= context]:
                    pending.remove(item)
                    emit(item)
                if (not level or level.search(text)) and match(text):
                    if report["results"] >= opts["max_results"]:
                        stop = "results"
                    else:
                        report["results"] += 1
                        item = {"root": label, "file": "/".join(parts), "line": number, "mtime": mtime,
                                "text": show(text), "cut": len(redact(text)) > SHOW_CAP,
                                "before": [show(b) for b in before], "after": []}
                        (pending.append if context else emit)(item)
                before.append(text)
                # bytes already read (the replayed probe) are always processed; the budget stops only new reads
                stop = stop or ("deadline" if budget.expired() else "bytes" if room() <= 0 and not source.pending()
                                and unread_on_disk(stream) else None)
                if stop:
                    break
            for item in pending:
                emit(item)
            if stop is None and room() <= 0 and unread_on_disk(stream):
                stop = "bytes"
        if stop:
            report["truncated"].append(stop)
            break
    report["truncated"] = sorted(set(report["truncated"]))
    return report


def run_search(opts):
    """In process for literal text; in a worker process (killed and reaped at the deadline) for a regex."""
    started = time.monotonic()
    deadline = started + opts["deadline_s"]
    results = []
    report = search(opts, results.append, deadline) if opts["regex"] is None else _run_worker(opts, results, deadline)
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    return results, report


def _no_window():
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def _run_worker(opts, results, deadline):
    child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
    lines, size, errors = [], [0], bytearray()

    def read():
        try:
            for raw in child.stdout:
                size[0] += len(raw)
                if size[0] > MAX_WORKER_OUTPUT:
                    return
                lines.append(raw)
        except (OSError, ValueError):
            pass

    def read_errors():
        try:
            while chunk := child.stderr.read(4096):
                errors.extend(chunk[:max(0, 8192 - len(errors))])
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read, daemon=True)
    error_reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    error_reader.start()
    try:
        child.stdin.write(json.dumps(opts).encode("utf-8"))
        child.stdin.close()
    except OSError:
        pass
    reader.join(max(0.0, deadline - time.monotonic()) + 0.25)
    stopped = reader.is_alive() or size[0] > MAX_WORKER_OUTPUT
    if child.poll() is None:
        child.kill()
    child.wait()
    reader.join(2)
    error_reader.join(2)
    child.stdout.close()
    child.stderr.close()
    report = None
    for raw in lines:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("type") == "result" and len(results) < opts["max_results"]:
            results.append(row["item"])
        elif row.get("type") == "report":
            report = row["report"]
    if report is not None and not stopped:
        return report
    partial = {"files": None, "bytes": None, "results": len(results), "truncated": [], "roots": [], "skipped_binary": None}
    if stopped:
        partial["truncated"] = ["deadline"]
        partial["why"] = "the regex worker was stopped at the deadline; results found so far are listed"
    else:
        last = errors.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no report"]
        partial["truncated"] = ["worker"]
        partial["why"] = "the regex worker ended without a report (exit " + str(child.returncode) + "): " + redact(last[0])[:200]
    return partial


def worker_main():
    """Worker: options as JSON on stdin; results and the final report as JSON lines on stdout."""
    opts = json.loads(sys.stdin.buffer.read(1 << 20).decode("utf-8"))
    deadline = time.monotonic() + float(opts["deadline_s"])

    def emit(item):
        sys.stdout.write(json.dumps({"type": "result", "item": item}, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    report = search(opts, emit, deadline)
    sys.stdout.write(json.dumps({"type": "report", "report": report}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


def fit(results, report):
    """Keep the whole serialized reply under the output budget (the report first, then results in order: newest
    files were scanned first, so the tail goes)."""
    for row in report.get("roots", []):
        row["root"] = str(row["root"])[:1024]
        if row.get("error"):
            row["error"] = str(row["error"])[:200]
    room = OUTPUT_BUDGET - len(json.dumps(report, ensure_ascii=True)) - 512
    kept = []
    for item in results:
        room -= len(json.dumps(item, ensure_ascii=True)) + 2
        if room < 0:
            report["truncated"] = sorted(set(report["truncated"]) | {"output"})
            break
        kept.append(item)
    return kept


def run(verb, payload):
    if verb not in VERBS:
        raise ValueError("unknown verb " + verb)
    try:
        opts = options(payload)
    except ValueError as exc:
        return {"schema": SCHEMA, "verb": verb, "error": str(exc)}
    results, report = run_search(opts)
    return {"schema": SCHEMA, "verb": verb, "results": fit(results, report), "report": report}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(worker_main())
