# Redaction

The portal (`hyperloom.web.api.redact`: documents, job messages, audits, log lines) and the node's log search
(`node/hyperloom_logsearch.py`) mask credentials with one set of rules, `hyperloom/redaction.py`. The node keeps a
verbatim copy of the rules block; `tests/test_redaction.py` asserts the copies are identical, runs one case table
against every implementation and measures that redaction time stays linear (64 KB worst cases included).

Text is read as written and, for string literals written with escapes (JSON `"..."`, a Python repr `'...'`), also
decoded, up to 4 layers deep: what the rules hide in the decoded text is hidden in the literal too. Hiding in a
decoded layer only adds to what the text itself hides.

`tests/test_redaction_corpus.py` runs a fixed corpus (`tests/fixtures/redaction/`: every case so far and a seeded
sample of generated JSON, repr and shell encodings, kept values, unpaired quotes and argument lists) against frozen
copies of earlier rules: no secret that the base rules or the previous rules hid is shown, and nothing more is hidden
than the previous rules hid, except the entries `allowlist.json` names with the change that explains them.

## Known limits

Each row is a form the rules deliberately leave as described.

| Limit | Minimal example | Result |
|---|---|---|
| The node redacts one log line at a time; a command continued with a backslash is seen without its first line | `curl https://x \` then `  -u a:pw` | the second line shows `a:pw` |
| A token option keeps a number | `--hf-token 123456789012` | unchanged |
| `--user name:group` is hidden like `name:password` | `docker run --user root:root img` | `--user [redacted]` |
| An argument list that only mentions mysql hides a later glued `-p` value | `["echo", "mysql", "-pretty"]` | `-p[redacted]` |
| A snake-case name that is another value's end is not a name when a parenthesis follows it (the message after a value stays readable) | `api_key= db_pass (x)` | `api_key= [redacted] (x)` |
| A bare `hf_` or GitHub token word is hidden only with 30+ token characters or when it calls itself a secret | `hf_hub_download` | unchanged |
| A shell-quoted value holding a double quote and a blank, inside a single-quoted command, as written, JSON-encoded or in a repr | `ssh host 'export HF_TOKEN='"'"'"q r x'"'"''` | `r x` shown |
| A client certificate's password is hidden for curl only | `wget --certificate c.pem:pw` | unchanged |
| An apostrophe that opens prose (`it's`) can pair with the opening quote of a later value, which then shows its words after the first blank | `a_key it's db_pass 'hunter2 pw'` | `pw'` shown |
| A shell-escaped blank joins the next command into a value | `sshpass -p=a\ {sshpass -p 'x"y'` | `'x"y'` shown |
| A kept value (None, a count, a uid:gid) written inside a repr of JSON is hidden, and so is the closing quote after it; in one more layer of encoding it stays hidden | `'{"m": "api_key=None"}'` | `api_key=[redacted]}'` |

## Follow-ups

- Read shell-quoted commands nested in a quoted command (`'"'"'`) as a shell would, so a value holding a double
  quote and a blank is hidden whole.
- Redact `\`-continued commands across lines on the node (the portal reads whole texts).
- Certificate options of tools other than curl (`wget --certificate`, `httpie --cert`).
- Tell a value's trailing parenthesised message from a parenthesised secret (`(the job ...)` vs `(x)`).
- Keep a prose apostrophe (`it's`) from pairing with a later value's opening quote.
- Read a shell-escaped blank (`\ `) as part of one word without joining the next command into it.
- Keep a kept value's closing quote in a repr of JSON (`'{"m": "api_key=None"}'` keeps `None"}'`).
