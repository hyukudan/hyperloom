# Redaction

The portal (`hyperloom.web.api.redact`: documents, job messages, audits, log lines) and the node's log search
(`node/hyperloom_logsearch.py`) mask credentials with one set of rules, `hyperloom/redaction.py`. The node keeps a
verbatim copy of the rules block; `tests/test_redaction.py` asserts the copies are identical, runs one case table
against every implementation and measures that redaction time stays linear (64 KB worst cases included).

Text is read as written and, for string literals written with escapes (JSON `"..."`, a Python repr `'...'`), also
decoded, up to 4 layers deep: what the rules hide in the decoded text is hidden in the literal too. Hiding in a
decoded layer only adds to what the text itself hides.

Each path also replays its own rules of 2a7c803 (the base): the node replays the base node's line rules, the portal
the base portal's text rules (and, for argument lists, its rule that hides the argument after `--api-key`,
`--handoff-api-key`, `--password` or `--token`). The replay applies that path's rules in order, as the base did, and
maps each substitution back to the original text; those spans are hidden with the rest. So no secret the base hid
on a path shows on it, and a path never hides more because of the other path's base rules. The node's base rules
are written in a form that runs in linear time; a test checks that they give the verbatim rules' substitutions.

`tests/test_redaction_corpus.py` runs a fixed corpus (`tests/fixtures/redaction/`: every case so far and a seeded
sample of generated JSON, repr and shell encodings, kept values, unpaired quotes and argument lists) against frozen
copies of earlier rules: no secret that the base rules or the previous rules hid is shown, and nothing more is hidden
than the previous rules hid, except the entries `allowlist.json` names with the change that explains them.

## Known limits

Each row is a form the rules deliberately leave as described.

| Limit | Minimal example | Result |
|---|---|---|
| The node redacts one log line at a time; a command continued with a backslash is seen without its first line | `curl https://x \` then `  -u a:pw` | the second line shows `a:pw` |
| A token option keeps a number, in an argument list and in portal text (the node hides it, as its base did) | `--hf-token 123456789012`, `["--hf-token", "123456789012"]` | unchanged |
| `--user name:group` is hidden like `name:password` | `docker run --user root:root img` | `--user [redacted]` |
| An argument list that only mentions mysql hides a later glued `-p` value | `["echo", "mysql", "-pretty"]` | `-p[redacted]` |
| A bare `hf_` or GitHub token word is hidden only with 30+ token characters or when it calls itself a secret | `hf_hub_download` | unchanged |
| A shell-quoted value holding a double quote and a blank, inside a single-quoted command, as written, JSON-encoded or in a repr | `ssh host 'export HF_TOKEN='"'"'"q r x'"'"''` | `r x` shown |
| A client certificate's password is hidden for curl only | `wget --certificate c.pem:pw` | unchanged |
| An apostrophe that opens prose (`it's`) can pair with the opening quote of a later value, which then shows its words after the first blank | `a_key it's db_pass 'hunter2 pw'` | `pw'` shown |
| A shell-escaped blank joins the next command into a value | `sshpass -p=a\ {sshpass -p 'x"y'` | `'x"y'` shown |
| A kept value (None, a count, a uid:gid) written inside a repr of JSON is hidden, and so is the closing quote after it; in one more layer of encoding it stays hidden | `'{"m": "api_key=None"}'` | `api_key=[redacted]}'` |
| The portal shows a password given to sshpass after its command's own options, as its base did (the node hides it, as its base did) | `sshpass ssh -p S3cret` | unchanged on the portal |
| In text, only the first password glued to `-p` in a mysql command is hidden (an argument list hides each) | `mysql -pA1 -pB2` | `mysql -p[redacted] -pB2` |
| `--user` followed by a value that starts with `-` is read as an option, not a value, in text and argument lists (as in 2a7c803) | `wget --user -alice:x` | unchanged |

## Hidden by a path's base replay

These forms were readable at 65f2fe5. Each is hidden again where that path's base hid it.

Hidden on the node only. The base node rules hid the value after an environment-style name (`max_tokens 16`,
`key_cache shape`), any value after `Bearer`, `Basic`, `Digest`, `Token` or `Negotiate`, any `key`/`token`/`pass`
name's `=` or `:` value, a `--...token...` option's value, and the word after `mysql ... -p` or `sshpass ... -p`.
The portal keeps all of them.

| Text | Node result |
|---|---|
| `MAX_TOKENS 5`, `max_tokens 16` | `MAX_TOKENS [redacted]`, `max_tokens [redacted]` |
| `budget bound violated by the endpoint: max_tokens 16, reported 21 completion tokens` | `max_tokens [redacted] reported ...` |
| `--hf-token 123456789012`, `--max-num-batched-tokens 8192`, `--prompt-tokens 9000` | `--hf-token [redacted]`, ... |
| `max_tokens=512`, `SamplingParams(n=1, max_tokens=512, temperature=0.7)`, `pad_token_id=-1`, `attn_mask_key=None` | `max_tokens=[redacted]`, ... |
| `token_ids: [1, 2, 3]`, `token-identical: 2/3 (first pass)`, `tokenizer=/models/qwen` | `token_ids: [redacted], 2, 3]`, ... |
| `--tokenizer /models/qwen` | `--tokenizer [redacted]` |
| `key_cache shape [32, 8192]`, `tokenizer.ggml.bos_token_id u32 = 151643` | `key_cache [redacted] [32, 8192]`, ... |
| `Token counters are reset`, `Token ledger`, `basic setup done` | `Token [redacted] are reset`, ... |
| `needs imported token counts`, `the token 12345678 was sampled`, `token ids`, `generated-token counters` | `token [redacted]`, ... |
| `sshpass -e ssh -p 2222 host`, `sshpass -f pwfile ssh -p 2222 host`, `mysql -h db -P3306 shop` | `-p [redacted]`, `-P[redacted]` |
| a placement message ending in `(the job was not started)` after a snake-case name or `-p` value | `[redacted] [redacted] job was not started)` |

Hidden on every path. Both bases hid these: the base portal took any value after `token:` or `token=` (also
inside a longer name) and anything after `Bearer`. The base node did the same.

| Text | Result |
|---|---|
| `token: 151643`, `token: \"None\" ok` | `token: [redacted]`, ... |
| `pad_token: <|endoftext|>`, `pad_token: </s>`, `bos_token: <s>`, `unk_token: <unk>`, `eos_token: <|im_end|>` | `pad_token: [redacted]`, ... |
| `SamplingParams(token=None)` | `SamplingParams(token=[redacted]` |
| `curl -H "Authorization: Bearer [redacted]" -d "x y" https://example.invalid` | portal: `Bearer [redacted] -d ...` |
| argument lists: the argument after `--api-key`, `--handoff-api-key`, `--password` or `--token`, kept values included | `["--token", "512"]` gives `["--token", "[redacted]"]` |

## Follow-ups

- Read shell-quoted commands nested in a quoted command (`'"'"'`) as a shell would, so a value holding a double
  quote and a blank is hidden whole.
- Redact `\`-continued commands across lines on the node (the portal reads whole texts).
- Certificate options of tools other than curl (`wget --certificate`, `httpie --cert`).
- Tell a value's trailing parenthesised message from a parenthesised secret (`(the job ...)` vs `(x)`); on the
  node, the base replay hides `(the` as its base did.
- Decide whether the node keeps the base node's hiding of counts and token names (`max_tokens 16`,
  `token_ids: [1, 2, 3]`), and whether the portal keeps the base portal's hiding of special tokens
  (`pad_token: <|endoftext|>`); either needs the base-parity bar relaxed for that form.
- Keep a prose apostrophe (`it's`) from pairing with a later value's opening quote.
- Read a shell-escaped blank (`\ `) as part of one word without joining the next command into it.
- Hide every glued `-p` value of a mysql command in text, not only the first.
- Tell a `--user` value that starts with `-` (`-alice:x`) from the next option.
- Keep a kept value's closing quote in a repr of JSON (`'{"m": "api_key=None"}'` keeps `None"}'`).
