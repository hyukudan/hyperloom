"""One set of redaction rules for free text: the portal's ``redact`` (documents, job messages, audits, log lines)
and the node's log search (``node/hyperloom_logsearch.py``) mask the same credentials.

The node tool is deployed as standalone files (``hyperloom nodes update`` copies the files the node CLI's
EXTENSIONS tuple names), so it keeps a verbatim copy of the block between the two ``shared redaction rules``
markers below; tests/test_redaction.py asserts the copies are identical and runs one case table against both.

Every rule runs in time linear in the text's length (tests/test_redaction.py measures it on long adversarial
lines): a name is matched once from where it starts, never re-scanned from each of its characters, and a value is
read once.

Known limits, chosen on purpose: the node redacts one log line at a time, so a command continued with a backslash
on the next line (`curl https://x \\` then `-u a:b`) is seen without its curl; a lowercase name with the credential
word inside it (db_password_prod) hides a value after = or : only, so prose such as "key_cache shape" stays; a
blank-separated value that starts with -- is the next option (password --verbose); a token option keeps a number
(--hf-token 123456789012); `--user name:group` is hidden as a name:password would be; an argument list that only
mentions mysql hides a later glued -p value; a snake-case name that is itself the hidden value of another name is
not read as a name (`api_key= db_pass x` shows x), so a message after a value such as hf_probe_secret stays
readable; a bare hf_ word is hidden only when it is 30+ token characters or calls itself a secret.
"""

import re

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
_CREDENTIAL_LOOK = re.compile(r"[0-9+/=]|.[A-Z]")
_NESTED_BEARER = re.compile(r"(?i)\bbearer\s+\S")
_DOUBLE = r'''"(?:[^"\\\n]|\\.)*"'''
_DOUBLE_STRING = re.compile(_DOUBLE)
_NOT_OPTION = r"(?!-[A-Za-z](?:\s|$)|--[A-Za-z])"   # a blank-separated value is not the next option (-U, --next)
_START = r"(?!\[redacted\])(?=\S)"                 # where a value starts (one already hidden is not read again)
# A shell word: quoted parts (a backslash escapes inside double quotes and outside quotes) and unquoted
# characters, concatenated (-u'alice:foo'bar and api_key=a' two words' are one word each); a quote with no closing
# quote on its line is an ordinary character; a backslash before a newline ends the word; an escaped quote pair
# is a quoted part too (\"two words\" in JSON written inside a string: {\"password\": \"x y\"}). A value inside a quoted
# string that opens and closes at a word boundary (-H "X-Api-Key: abc", '{"password": "x y"}', bash -c "curl -u
# a:b") ends where that string ends; a string glued to more text (' ... a:b'c d') is not such a string, so the
# value pairs its own quotes. Each value is read once: a reader never goes back over text it has read.
_WORD = r"""(?:'[^'\n]*'|""" + _DOUBLE + r"""|\\.|[^\s'"\\]|['"\\])+"""
_BOUNDARY = " \t\n\r\f\v=:([{,"
_BOUNDARY_AFTER = " \t\n\r\f\v,;)]}"
_LEXEME = re.compile(r"""[^'"\\]+|\\[\s\S]?""")


def _strings(text):
    """(starts, ends): the quoted strings of ``text`` that open at a word boundary, in order (read once, left to
    right, as a shell reads them; a quote with no closing quote on its line is an ordinary character)."""
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
    return starts, ends


def _shell_word(text, pos, stops="", strings=None):
    """(end, value): the shell word that starts at ``pos`` (also ending at any character of ``stops``: an
    assignment's value ends at , ; }) and its text without quotes or escapes. ``strings``: _strings(text)."""
    starts, ends = _strings(text) if strings is None else strings
    limit, enclosing = len(text), ""
    low, high = 0, len(starts)
    while low < high:                   # the last string that opens before pos
        middle = (low + high) // 2
        if starts[middle] < pos:
            low = middle + 1
        else:
            high = middle
    if low and pos < ends[low - 1]:
        limit, enclosing = ends[low - 1], text[starts[low - 1]]
    value, at, size = [], pos, len(text)
    failed = {"'": -1, '"': -1, "\\'": -1, '\\"': -1}
    while at < size:
        char = text[at]
        if at >= limit:                 # the enclosing string ends
            break
        if char.isspace() or char in stops or char == enclosing or text[at:at + 2] == "\\\n":
            break
        if char == "\\":
            quote = text[at + 1:at + 2]
            if quote in ("'", '"') and at >= failed["\\" + quote]:
                end = text.find("\\" + quote, at + 2, limit)
                if end != -1 and "\n" not in text[at + 2:end]:
                    value.append(text[at + 2:end])
                    at = end + 2
                    continue
                line = text.find("\n", at, limit)  # no closing escaped quote: none for a later one either
                failed["\\" + quote] = limit if line == -1 else line
            value.append(text[at + 1:min(at + 2, limit)])
            at = min(at + 2, limit)
            continue
        if char in "'\"" and at >= failed[char]:
            if char == "'":
                end = text.find("'", at + 1, limit)
                end = end if end != -1 and "\n" not in text[at + 1:end] else -1
            else:
                found = _DOUBLE_STRING.match(text, at, limit)
                end = found.end() - 1 if found else -1
            if end != -1:
                value.append(text[at + 1:end])
                at = end + 1
                continue
            line = text.find("\n", at, limit)       # no closing quote: none for a later one on this line either
            failed[char] = limit if line == -1 else line
        value.append(char)
        at += 1
    return min(at, limit, size), "".join(value)


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
    (re.compile(r'''(?ix)(?<![\w.-])(?:\\?["'])?(?:(?=[\w.-]*?(?P<cred>key|secret|pass|pwd|credential|auth))'''
                r'''|(?=[\w.-]*?token(?!iz)))(?=([\w.-]*))\2(?:\\?["'])?\s*[:=]\s*'''
                r'''(?!(?:Bearer|Basic|Digest|Token|Negotiate)\s+\[redacted\])'''
                r'''(?:(?:Bearer|Basic|Digest|Token|Negotiate)\s+(?=[^\s,;}]))?''' + _START), ",;}",
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
    # is that value, not a name (HF_TOKEN hf_probe_secret (the job...): "(the" stays).
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
    # or hf_token stay readable. A bare GitHub token: ghp_, gho_, ghu_, ghs_, ghr_ or github_pat_ and its characters.
    (re.compile(r"(?P<v>\bhf_(?:[A-Za-z0-9]{30,}|(?=\w*?(?i:secret))[A-Za-z0-9_]+)\b"
                r"|\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]+)"), "", _keep_nothing),
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
# --proxy-user, also quoted: "-u" "a:b", "--user=a:b"), every one of them (also after --next), within the same
# command: up to a newline (a backslash continues the line), a pipe, `;`, `&&` or a background `&` outside quotes (a
# backslash-escaped character is not a separator; a value glued to a separator stays whole: `-u a:b;c` hides
# `a:b;c`). An unpaired quote is an ordinary character. Only option letters that take no argument can come before
# u/U in one word (-sSu; -XPUT is -X PUT). The executable is matched in any case (CURL.EXE) and may be quoted, with
# a path ("/usr/bin/curl", "C:\Program Files\curl\curl.exe"): the scan starts at its opening quote, so its closing
# quote is not read as the start of a quoted argument.
CURL_COMMAND = re.compile(r"""(?i)(?:(?P<q>["'])(?:[^"'\n]*[\\/])?)?\bcurl(?:\.exe)?\b(?(q)(?P=q))"""
                          r"""(?:'[^'\n]*'|""" + _DOUBLE + r"""|\\[\s\S]|&(?![&\s]|$)|[^\n|;&'"\\]|['"\\])*""")
CURL_FLAGS = r"[sSfLkvqiIOJRgGnNpBMljaZV#:0-9]"
CURL_OPTION = re.compile(r"""(?<=\s)(?P<o>(?P<q>["'])(?:-""" + CURL_FLAGS + r"""*[uU]|--(?:proxy-)?user)(?P=q)\s+"""
                         r"""|["']?(?:-""" + CURL_FLAGS + r"""*[uU]\s*|--(?:proxy-)?user(?:\s+|=)))""" + _START
                         + r"""|'[^'\n]*'|""" + _DOUBLE + r"""|\\.""")


def _values(text, strings):
    """(start, end, name): every value the rules and the -p clients hide; name is where a snake-case name
    ends (its last character), a value only if that name is not inside another hidden value."""
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
            read, value = _shell_word(text, pos, stops, strings)
            if read > pos and not keep(found, value):
                yield pos, read, found.end("lower") - 1 if found.groupdict().get("lower") else None
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
            end = _shell_word(text, option.end(), "", strings)[0]      # its value may go on past the command
            if end > option.end():
                yield option.end(), end
                last = end
        pos = command.end()             # a value that runs on past it may hide the next command's start


def redact_text(text):
    """``text`` with every credential the shared rules recognise replaced by [redacted] (overlapping or touching
    hidden spans become one)."""
    strings = _strings(text)
    found = [*_values(text, strings), *((start, end, None) for start, end in _curl_users(text, strings))]
    spans = sorted((start, end) for start, end, name in found if name is None)
    union, ends = [], []                # the hidden spans merged, to look a name up in them
    for start, end in spans:
        if union and start <= ends[-1]:
            ends[-1] = max(ends[-1], end)
        else:
            union.append(start)
            ends.append(end)
    for start, end, name in found:
        if name is not None:
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


# Argument lists (the portal's argv documents and recipe results; the node searches lines only). A credential
# option hides the next argument or its glued value (--api-key x, --db-password=x, --oauth2-bearer x; a token
# option such as --hf-token or --max-num-batched-tokens hides it unless it is a number, as the text rules do);
# --user/--proxy-user hide a following or glued name:password (a uid:gid such as 1000:1000, or another option,
# stays). Clients: within a command (the arguments between two shell separators), every argument that names
# curl, mysql/mariadb or sshpass (its file name without quotes, in any case, .exe allowed) turns that client on
# for the rest of the command, and nothing turns it off: no table of wrappers decides where the command is (sudo
# -n, ssh host, docker exec c, A=1 all reach curl). A URL (https://x/mysql) or a value that was hidden
# (-u a:pw/mysql) names no client. curl: -u/-U values, separate or glued (-ualice:pw); mysql/mariadb: the password
# glued to -p; sshpass: its -p value among its own options (the command it runs keeps its own -p, ssh's port). A
# separator right after an option is that option's value (curl -o ';' -u a:b), not the end of the command.
# Every pattern here is matched once per argument, without backtracking into a failing suffix: an option name is
# a whole --word, then searched for its credential word; a program is a whole file name.
ARGV_OPTION = re.compile(r"(--[\w-]*)(?:(=)(.*))?", re.S)
ARGV_CREDENTIAL_WORD = re.compile(r"(?i)key|secret|passw(?:or)?d|credential|bearer|(?<![a-z])pass(?:phrase)?(?![\w-])")
ARGV_TOKEN_WORD = re.compile(r"(?i)token(?!iz)")
ARGV_CLIENT = re.compile(r"(?i)(curl|mysql\w*|mariadb[\w-]*|sshpass)(?:\.exe)?")
ARGV_PATH = re.compile(r"[\\/]")
ARGV_CURL_USER = re.compile(r"-" + CURL_FLAGS + r"*[uU]")
ARGV_GLUED = {"curl": re.compile(r"(-" + CURL_FLAGS + r"*[uU])[^\s:]*:.*", re.S),
              "mysql": re.compile(r"(-p)(?!\[redacted\]).+", re.S),
              "sshpass": re.compile(r"(-[ehvV]*p)(?!\[redacted\]).+", re.S)}
ARGV_SSHPASS_PASSWORD = re.compile(r"-[ehvV]*p")
ARGV_SEPARATORS = {"|", "||", "&&", ";", "&"}


def _program(item):
    """The program an argument names (its file name, without quotes), lower case."""
    return ARGV_PATH.split(item.strip("\"'"))[-1].strip("\"'").lower()


def _argv_hidden(kind, item):
    """Whether the value ``item`` of an option of this kind is hidden (read as the text rules read a value)."""
    value = _shell_word(item, 0, "", ([], []))[1] if item[:1] in "'\"" else item
    if kind == "user":
        return not item.startswith("-") and not _keep_user(None, value)
    if kind == "count":
        return _KEPT_COUNT.fullmatch(value) is None
    return kind == "credential"


def redact_argv(value, each=None):
    """An argument list with its credentials replaced; ``each`` redacts one argument (default: the text rules for a
    string, anything else kept)."""
    if each is None:
        each = lambda item: redact_text(item) if isinstance(item, str) else item
    result, hide, clients, sshpass, option = [], None, set(), False, False
    for item in value:
        if not isinstance(item, str):
            result.append(each(item))
            hide, option = None, False
            continue
        if item in ARGV_SEPARATORS and not option:
            result.append(item)
            hide, clients, sshpass = None, set(), False
            continue
        hidden = hide is not None and _argv_hidden(hide, item)
        named = ARGV_OPTION.fullmatch(item)
        kind = None
        if named is not None:
            name = named.group(1)
            kind = ("credential" if ARGV_CREDENTIAL_WORD.search(name) else "user" if name in (
                "--user", "--proxy-user") else "count" if ARGV_TOKEN_WORD.search(name) else None)
        glued = None
        if not hidden:
            glued = next((found for client, found in ((c, ARGV_GLUED[c].fullmatch(item)) for c in (
                "curl", "mysql", "sshpass")) if found and client in clients and (client != "sshpass" or sshpass)), None)
        if hidden:
            shown = REDACTED
        elif glued is not None:
            shown = glued.group(1) + REDACTED
        elif kind and named.group(3) and _argv_hidden(kind, _shell_word(named.group(3), 0, "", ([], []))[1]):
            shown = named.group(1) + "=" + REDACTED
        else:
            shown = each(item)
        result.append(shown)
        changed = shown != item
        argument = hide == "argument"
        # sshpass's own options end at its password or at the first argument that is not an option's value.
        if sshpass and (changed and hide is not None or glued is not None or not argument and not item.startswith("-")):
            sshpass = False
        if not changed and "://" not in item:
            found = ARGV_CLIENT.fullmatch(_program(item))
            if found is not None:
                client = found.group(1).lower()
                client = client if client in ("curl", "sshpass") else "mysql"
                clients.add(client)
                sshpass = sshpass or client == "sshpass"
        option = item.startswith("-") and not changed and named is not None and named.group(2) is None or (
            item.startswith("-") and not changed and len(item) == 2)
        if kind and named.group(2) is None:
            hide = kind
        elif sshpass and ARGV_SSHPASS_PASSWORD.fullmatch(item):
            hide = "credential"
        elif "curl" in clients and ARGV_CURL_USER.fullmatch(item):
            hide = "user"
        elif sshpass and item in ("-f", "-d", "-P"):
            hide = "argument"           # its file, descriptor or prompt: still among sshpass's options
        else:
            hide = None
    return result
