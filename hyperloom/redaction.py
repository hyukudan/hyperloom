"""One set of redaction rules for free text: the portal's ``redact`` (documents, job messages, audits, log lines)
and the node's log search (``node/hyperloom_logsearch.py``) mask the same credentials.

The node tool is deployed as standalone files (``hyperloom nodes update`` copies the files the node CLI's
EXTENSIONS tuple names), so it keeps a verbatim copy of the block between the two ``shared redaction rules``
markers below; tests/test_redaction.py asserts the copies are identical and runs one case table against both.

Every rule runs in time linear in the text's length (tests/test_redaction.py measures it on long adversarial
lines): a name is matched once from where it starts, never re-scanned from each of its characters.
"""

import re

# --- shared redaction rules (node/hyperloom_logsearch.py keeps a verbatim copy) ---
# Credentials after an authorization scheme, any name that carries key/secret/token/password/credential/auth
# (AWS_SECRET_ACCESS_KEY=, "db.password": ...), the value after a password word ("password x"), option forms, URL
# user information, the value after an environment-style name, clients that take a password after -p, curl's user
# options, any tool's --user name:password and bare Hugging Face tokens. Every rule reads the original text and
# names the value it hides (group v); the hidden spans are merged, so no rule's output can hide a credential from
# another (`curl -u a:password -U b:c`: the password-word rule cannot take -U from curl's scan).
REDACTED = "[redacted]"
# Linear forms: a name is matched as a whole run from the position where the run starts ((?=(run))\N takes the
# run once and never gives it back, as an atomic group would); a lookahead checks that the run carries a
# credential word. A name whose only such word is token (max_tokens, --max-num-batched-tokens, token_ids,
# "token-identical", pad_token) keeps a number, a list of numbers or a special token as its value (max_tokens=512,
# token: 151643, token-identical: 2/3, token_ids: [1, 2], pad_token: <|endoftext|> stay; any other value is
# hidden); tokenizer names are not credentials. Any name keeps None, null, true or false. An authorization scheme
# word stays readable (Authorization: Bearer [redacted]). A quoted value is hidden whole.
_PLAIN = r"(?:None|null|true|false|True|False)"
_COUNT = r"(?:\[?[0-9]+(?:[./][0-9]+)*|<[|\w.-]+>)"
_END = r"(?=[)\]]*(?:[\s,;}]|$))"              # the end of a name=value value
_END_WORD = r"(?=[)\],;]*(?:\s|$))"            # the end of a blank-separated value
# A shell word: quoted parts (a backslash escapes inside double quotes) and unquoted characters, concatenated
# (-u'alice:foo'bar is one word); an unpaired quote is an ordinary character.
_DOUBLE = r'''"(?:[^"\\\n]|\\.)*"'''
_WORD = r"""(?:""" + _DOUBLE + r"""|'[^'\n]*'|\\.|[^\s'"\\]|['"\\])+"""
_VALUE = r"(?P<v>" + _WORD + r")"
_NOT_OPTION = r"(?!-[A-Za-z](?:\s|$)|--[A-Za-z])"   # a blank-separated value is not the next option (-U, --next)
SECRET_PATTERNS = (
    # An authorization header's credentials whatever they look like; after a scheme word elsewhere, Bearer's value
    # and a value that looks like a credential (8+ characters, not only digits, with a digit, +, / or =, or an
    # upper-case letter after the first): "Token counters", "basic setup" and "the token 12345678" stay readable.
    re.compile(r"""(?i)\b(?:proxy-)?authorization["']?\s*[:=]\s*["']?(?:Bearer|Basic|Digest|Token|Negotiate)\s+"""
               r"""(?!\[redacted\])(?P<v>[^\s"',;]+)"""),
    re.compile(r"""(?i)\b(?:Bearer\s+|(?:Basic|Digest|Token|Negotiate)\s+(?=[^\s"',;]{8})(?![0-9]+(?![^\s"',;]))"""
               r"""(?=[^\s"',;]*?(?:[0-9+/=]|(?<![\s"',;])(?-i:[A-Z]))))(?!\[redacted\])(?P<v>[^\s"',;]+)"""),
    re.compile(r'''(?ix)(?<![\w.-])["']?(?:(?=[\w.-]*?(key|secret|pass|pwd|credential|auth))'''
               r'''|(?=[\w.-]*?token(?!iz)))(?=([\w.-]*))\2["']?\s*[:=]\s*'''
               r'''(?:(?:Bearer|Basic|Digest|Token|Negotiate)\s+(?=[^\s,;}]))?'''
               r'''(?!''' + _PLAIN + _END + r''')(?(1)|(?!''' + _COUNT + _END + r'''))'''
               r'''(?P<v>"[^"]*"|'[^']*'|[^\s,;}]+)'''),
    # An option whose name carries a credential word (--hf-token x, --db-password=x), also glued after other
    # characters of the word (x--api-key x): matched from the start of the word, up to its first --.
    re.compile(r"(?i)(?<![\w-])\w*(?:-\w+)*--(?:(?=[\w-]*?(key|secret|passw(?:or)?d|credential))"
               r"|(?=[\w-]*?token(?!iz)))(?=([\w-]*))\2(?:\s+|=)"
               r"(?!" + _PLAIN + _END_WORD + r")(?(1)|(?!" + _COUNT + _END_WORD + r"))(?!\[redacted\])" + _VALUE),
    # The value after a password word (not an option that follows it: `-u a:password -U b:c`).
    re.compile(r"(?i)\b(?:passw(?:or)?d|passwd|passphrase|pwd)\s+(?!\[redacted\])(?![:=])" + _NOT_OPTION + _VALUE),
    # URL user information (also an empty user name: redis://:password@host).
    re.compile(r"(?i)(?<![a-z0-9+.-])(?=[a-z0-9+.-]*?(?<!\w)[a-z])(?=([a-z0-9+.-]*))\1://"
               r"(?P<v>[^/\s:@]*:[^/\s@]+)@"),
    # An environment-style name and its value separated by blanks only (`HF_TOKEN hf_x`, `export DB_PASS s3`): the
    # name is upper case, or snake case ending with the credential word (hf_token, db_pass), so prose such as
    # "512 tokens in 3.1 s" or "key_cache shape" stays readable.
    re.compile(r"\b(?:(?:(?=[A-Z0-9_]*?(KEY|SECRET|PASS|PWD|CREDENTIALS?|AUTH))|(?=[A-Z0-9_]*?TOKEN(?!IZ)))"
               r"[A-Z0-9]+(?:_[A-Z0-9]+)*"
               r"|(?i:(?:(?=[a-z0-9_]*?(key|secret|pass|pwd|credentials?|auth))|(?=[a-z0-9_]*?token(?!iz)))"
               r"[a-z0-9]+(?:_[a-z0-9]+)*_[a-z0-9]*(?:key|secret|token|pass(?:wd|word)?|pwd|credentials?|auth)s?))"
               r"\s+(?!" + _PLAIN + _END_WORD + r")(?(1)|(?(2)|(?!" + _COUNT + _END_WORD + r")))"
               r"(?![:=])(?!\[redacted\])" + _NOT_OPTION + _VALUE),
    # Any other tool's `--user name:password` (a numeric uid:gid, as in `docker run --user 1000:1000`, stays; a
    # --user followed by another option, as in `systemd-run --user --setenv=A=/x:/y`, takes no value). The value is
    # looked for up to the next such option, so a run of options is read once.
    re.compile(r"--(?:proxy-)?user(?:\s+(?!-)|=)(?!\[redacted\])(?![0-9]+:[0-9]+(?:\s|$))"
               r"(?P<v>(?:(?!--(?:proxy-)?user(?:\s|=))[^\s:])*:\S+)"),
    # A bare Hugging Face access token (no name before it): hf_ and 30 or more token characters (library and model
    # names such as hf_transfer or hf_hub_download stay readable).
    re.compile(r"(?P<v>\bhf_[A-Za-z0-9]{30,}\b)"),
)
# Clients that take a password after -p: mysql and mariadb tools glued (`mysql -pX`, `mariadb-dump -p'x y'`; -P is
# the port), anywhere later on the line; sshpass among its own options (`sshpass -p X`, `sshpass -f file` has none,
# so the wrapped command's -p, ssh's port, stays). (client, password option, what a miss skips: the rest of the
# line, or sshpass's options, taken once and never re-read from each client word inside them)
_SSHPASS_OPTIONS = r"(?:\s+(?:-[fdP](?:\s+|(?=\S))" + _WORD + r"|-[ehvV]+))*"
GLUED_PASSWORD = (
    (re.compile(r"(?i)\b(?:mysql\w*|mariadb[\w-]*)\b"),
     re.compile(r"[^\n]*?\s-p(?!\[redacted\])(?=\S)" + _VALUE), re.compile(r"[^\n]*")),
    (re.compile(r"(?i)\bsshpass\b"),
     re.compile(r"(?=(" + _SSHPASS_OPTIONS + r"))\1\s+-p\s*(?!\[redacted\])" + _VALUE),
     re.compile(r"(?=(" + _SSHPASS_OPTIONS + r"))\1")),
)
# curl's user and password options (-u user:pw, -su user:pw, -uuser:pw, --user, --user=, the proxy's -U and
# --proxy-user), every one of them (also after --next), within the same command: up to a newline (a backslash
# continues the line), a pipe, `;`, `&&` or a background `&` outside quotes (a backslash-escaped character is not a
# separator; a value glued to a separator stays whole: `-u a:b;c` hides `a:b;c`; a value is a whole shell word,
# -u'a:b'c). An unpaired quote is an ordinary character. Only option letters that take no argument can come before
# u/U in one word (-sSu; -XPUT is -X PUT).
CURL_COMMAND = re.compile(r"""\bcurl\b(?:'[^'\n]*'|""" + _DOUBLE + r"""|\\[\s\S]|&(?![&\s]|$)|[^\n|;&'"\\]|['"\\])*""")
CURL_FLAGS = r"[sSfLkvqiIOJRgGnNpBMljaZV#:0-9]"
CURL_OPTION = re.compile(r"""'[^'\n]*'|""" + _DOUBLE + r"""|\\.|(?<=\s)(-""" + CURL_FLAGS + r"""*[uU]\s*"""
                         r"""|--(?:proxy-)?user(?:\s+|=))(?!\[redacted\])""" + _VALUE)


def _glued_password(text):
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
            yield value.span("v")
            pos = value.end()


def _curl_users(text):
    pos, last = 0, 0
    while True:
        command = CURL_COMMAND.search(text, pos)
        if command is None:
            break
        for option in CURL_OPTION.finditer(text, command.start(), command.end()):
            if option.group(1) is None or option.start() < last:
                continue
            option = CURL_OPTION.match(text, option.start())       # its value may go on past the command
            if option is None or option.group(1) is None:
                continue
            yield option.span("v")
            last = option.end()
        pos = command.end()             # a value that runs on past it may hide the next command's start


def redact_text(text):
    """``text`` with every credential the shared rules recognise replaced by [redacted] (overlapping or touching
    hidden spans become one)."""
    spans = [match.span("v") for pattern in SECRET_PATTERNS for match in pattern.finditer(text)]
    spans += [*_glued_password(text), *_curl_users(text)]
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


# Argument lists (the portal's argv documents; the node searches lines only). A credential option hides the next
# argument (--api-key x, --db-password x; a token option such as --hf-token or --max-num-batched-tokens hides it
# unless it is a number, as the text rules do); --user/--proxy-user hide a following name:password (a uid:gid such
# as 1000:1000 stays); after a client found anywhere in the list (behind sudo, env or a wrapper too) up to a shell
# separator: curl's -u/-U value, separate or glued (-ualice:pw), the password glued to -p for mysql/mariadb, and
# sshpass's -p value among its own options (the wrapped command's -p, ssh's port, stays).
# Every pattern here is matched once per argument, without backtracking into a failing suffix: an option name is
# a whole --word, then searched for its credential word; a client is a whole file name.
ARGV_OPTION = re.compile(r"--[\w-]*")
ARGV_CREDENTIAL_WORD = re.compile(r"(?i)key|secret|passw(?:or)?d|credential")
ARGV_TOKEN_WORD = re.compile(r"(?i)token(?!iz)")
ARGV_KEPT = re.compile(r"\[?[0-9]+(?:[./][0-9]+)*\]?|<[|\w.-]+>|" + _PLAIN)
ARGV_CLIENT = re.compile(r"(?i)(curl|mysql\w*|mariadb[\w-]*|sshpass)(?:\.exe)?")
ARGV_PATH = re.compile(r"[\\/]")
ARGV_GLUED = {"curl": re.compile(r"(-" + CURL_FLAGS + r"*[uU])[^\s:]*:.*", re.S),
              "mysql": re.compile(r"(-p)(?!\[redacted\]).+", re.S),
              "sshpass": re.compile(r"(-p)(?!\[redacted\]).+", re.S)}
ARGV_SEPARATORS = {"|", "||", "&&", ";", "&"}


def redact_argv(value, each=None):
    """An argument list with its credentials replaced; ``each`` redacts one argument (default: the text rules for a
    string, anything else kept)."""
    if each is None:
        each = lambda item: redact_text(item) if isinstance(item, str) else item
    result, hide, client = [], False, None
    for item in value:
        if not isinstance(item, str):
            result.append(each(item))
            hide = False
            continue
        # After a user option, a name:password is hidden; a uid:gid or another option (--setenv=A=/x:/y) stays.
        hidden = hide is True or (hide == "user" and ":" in item and not item.startswith("-")
                                  and not re.fullmatch(r"[0-9]+:[0-9]+", item)) or (
            hide == "count" and not ARGV_KEPT.fullmatch(item))
        glued = client is not None and ARGV_GLUED[client].fullmatch(item)
        result.append(REDACTED if hidden else glued.group(1) + REDACTED if glued else each(item))
        found = ARGV_CLIENT.fullmatch(ARGV_PATH.split(item)[-1])
        if client == "sshpass" and (hidden or glued or hide != "argument" and not item.startswith("-")):
            client = None               # past sshpass's own options: the wrapped command (ssh -p 2222)
        if item in ARGV_SEPARATORS:
            client = None
        elif found:
            name = found.group(1).lower()
            client = "curl" if name == "curl" else "sshpass" if name == "sshpass" else "mysql"
        option = ARGV_OPTION.fullmatch(item) is not None
        if option and ARGV_CREDENTIAL_WORD.search(item) or client == "sshpass" and item == "-p":
            hide = True
        elif item in ("--user", "--proxy-user") or client == "curl" and re.fullmatch("-" + CURL_FLAGS + "*[uU]", item):
            hide = "user"
        elif option and ARGV_TOKEN_WORD.search(item):
            hide = "count"
        elif client == "sshpass" and item in ("-f", "-d", "-P"):
            hide = "argument"           # its file, descriptor or prompt: still among sshpass's options
        else:
            hide = False
    return result
