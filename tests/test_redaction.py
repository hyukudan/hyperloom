"""One set of redaction rules (hyperloom/redaction.py): the node's log search keeps a verbatim copy (it is deployed
as standalone files); one case table runs against every implementation, and redaction time stays linear."""

import importlib.util
import json
from pathlib import Path
import re
import time
import unittest

from hyperloom import redaction
from hyperloom.web import recipes as recipe_web
from hyperloom.web.api import redact

ROOT = Path(__file__).resolve().parent.parent
NODE_FILE = ROOT / "node" / "hyperloom_logsearch.py"
spec = importlib.util.spec_from_file_location("hyperloom_logsearch_redaction_under_test", NODE_FILE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)

HF = "hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567"
# A line and the secret(s) that must not survive in it.
SECRETS = {
    # names and option forms
    "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENG": "wJalrXUtnFEMIK7MDENG",
    "login with password hunter3 now": "hunter3",
    '{"db.password": "s3cr3t-value"}': "s3cr3t-value",
    "db.password: 123456": "123456",
    "api_key=12345": "12345",
    "run --hf-token tk_123456 --other": "tk_123456",
    "run --db-password=Opt10nPw": "Opt10nPw",
    "x--api-key Gl8edOpt": "Gl8edOpt",
    "GITHUB_TOKEN: ghp_abcdef123": "ghp_abcdef123",
    "X-Auth-Token: abcdefghij": "abcdefghij",
    "client_secret='quoted value'": "quoted value",
    "HF_TOKEN=hf_xYz123": "hf_xYz123",
    "HF_TOKEN hf_xYz123": "hf_xYz123",
    "export DB_PASSWORD  s3cr3tval": "s3cr3tval",
    "hf_token\thf_lowercase9": "hf_lowercase9",
    "PASS=x9secret": "x9secret",
    "db_pass: plainpw7": "plainpw7",
    "max_tokens=notanumber": "notanumber",
    "MAX_TOKENS 16,leak3d": "leak3d",
    "--max-tokens 16;leak4d": "leak4d",
    "HF_TOKEN 1000:1000secret": "1000secret",
    # authorization schemes
    "Authorization: Basic dXNlcjpwYXNz": "dXNlcjpwYXNz",
    "authorization: basic lowercaseonly": "lowercaseonly",
    "Authorization: Bearer abc.def.ghi": "abc.def.ghi",
    "sent Bearer abc!def": "abc!def",
    "using Token ghp_abc123xyz": "ghp_abc123xyz",
    # URL user information
    "fetch https://user:pa55word@example.invalid/x": "pa55word",
    # clients that take a password after -p
    "mysql -u root -pXyzzy42 shop": "Xyzzy42",
    "mysqldump -h db -pAnother7 --all": "Another7",
    "mariadb-dump -pMaria9 db": "Maria9",
    "sshpass -p Sp4ce sshx": "Sp4ce",
    "sshpass -pGlu3d ssh host": "Glu3d",
    # bare Hugging Face tokens
    "download with " + HF + " ok": HF,
    "token is hf_abcdefghijklmnopqrstuvwxyzabcdefgh": "hf_abcdefghijklmnopqrstuvwxyzabcdefgh",
    # curl's user options: every form, every occurrence, wherever they are in the command
    "curl -u alice:Wonder7land https://example.invalid/x": "Wonder7land",
    "curl -sSu bob:Pa55-w0rd -o out.json https://example.invalid": "Pa55-w0rd",
    "curl -su bob:Pa55-w1rd https://example.invalid": "Pa55-w1rd",
    "curl --user carol:hunter4 https://example.invalid": "hunter4",
    "curl --user=dave:Sw0rdfish https://example.invalid": "Sw0rdfish",
    "curl -ueve:Gl0bal https://example.invalid": "Gl0bal",
    "curl -x http://proxy.invalid -U frank:Pr0xyPw https://example.invalid": "Pr0xyPw",
    "curl --proxy-user gil:Pr0xy2 https://example.invalid": "Pr0xy2",
    "curl -u 'gina:two words' https://example.invalid": "two words",
    "sudo curl -u hal:Sud0pw https://example.invalid": "Sud0pw",
    "sudo -H -u root /usr/bin/curl -u ida:Wr4pped https://example.invalid": "Wr4pped",
    "wget --user hank:C0mbined https://example.invalid": "C0mbined",
    "curl -H 'Cookie: a=1; b=2' -u ivy:C00kieJar https://example.invalid": "C00kieJar",
    'curl -H "Accept: a|b" -u jo:P1peLine https://example.invalid': "P1peLine",
    "curl -u alice:FirstSecret -U bob:SecondSecret https://example.invalid": ("FirstSecret", "SecondSecret"),
    "curl -u kim:N3xtOne https://a.invalid --next -u lee:N3xtTwo https://b.invalid": ("N3xtOne", "N3xtTwo"),
    "curl https://example.invalid/" + "a" * 5000 + " -u max:L0ngPrefix": "L0ngPrefix",
    "curl https://example.invalid/?a=1&b=2 -u ned:Amp3rsand": "Amp3rsand",
    "curl -u olga:Semi;colon https://example.invalid": ("Semi", "colon"),
    "curl https://example.invalid \\\n  -u pat:C0ntinued": "C0ntinued",
    "curl's -u quinn:Ap0strophe": "Ap0strophe",
    "curl -#u wes:H4sh https://example.invalid": "H4sh",
    # shell words: escapes inside double quotes, concatenated quoted and unquoted parts
    'curl -H "X: \\"a;b\\"" -u xia:SuperSecret https://example.invalid': "SuperSecret",
    "curl -H a\\;b -u yan:Esc4ped https://example.invalid": "Esc4ped",
    "curl -u'zoe:foo'bar https://example.invalid": ("foo", "bar"),
    'curl -u "amy:in\\"side" https://example.invalid': "side",
    # rules combined on one line: a command's options are read before a name rule can take one as a value
    "curl -u alice:password -U bob:SecondSecret https://example.invalid": ("alice:password", "SecondSecret"),
    "curl --user ben:passwd --proxy-user cat:Pr0xyThird https://example.invalid": ("ben:passwd", "Pr0xyThird"),
    "export DB_PASSWORD=Fir5t && curl -u dan:Sec0nd -U eli:Th1rd https://example.invalid": ("Fir5t", "Sec0nd", "Th1rd"),
    "mysql -u root -ppassword -e 'select 1' && curl --user fay:Mix3d https://example.invalid": ("-ppassword", "Mix3d"),
    "HF_TOKEN " + HF + " curl -u gus:Aft3r https://example.invalid": (HF, "Aft3r"),
    "sshpass -p token ssh host; curl -u hal:Tok3nPw https://example.invalid": ("-p token", "Tok3nPw"),
    # a quoted executable, also a Windows path with blanks
    'sudo "/usr/bin/curl" -u "ivo:Qu0tedExe" https://example.invalid': "Qu0tedExe",
    "'curl' -u jan:Qu0ted2 https://example.invalid": "Qu0ted2",
    '"C:\\Program Files\\curl\\bin\\curl.exe" -u kai:W1nPath https://example.invalid': "W1nPath",
    "& 'C:\\tools\\curl.exe' --user lia:W1nPs https://example.invalid": "W1nPs",
    # every value rule reads a whole shell word
    "api_key='foo'bar": ("foo", "bar"),
    'api_key="in\\"side"': "side",
    '{"password": "two words", "user": "x"}': "two words",
    "wget --user 'mia:two words' https://example.invalid": ("two", "words"),
    "tool --user=nia:'gl ued' x": ("gl", "ued"),
    "export DB_PASSWORD 'sp aced'": ("sp", "aced"),
    "Authorization: Bearer 'qu0ted.t0ken'": "qu0ted.t0ken",
    "sent Bearer \"qu0ted.t0ken2\"": "qu0ted.t0ken2",
    "command -v curl >/dev/null&&curl -u rae:Gl0edAnd https://example.invalid": "Gl0edAnd",
    "type curl;curl -u sam:Gl0edSemi https://example.invalid": "Gl0edSemi",
    "curl -sS https://a.invalid|curl -u tim:Gl0edPipe -T - https://b.invalid": "Gl0edPipe",
    "curl -u uma:F1rst;curl -u vic:S3cond https://example.invalid": ("F1rst", "S3cond"),
    # glued and quoted passwords, empty URL user names
    "mysql -h db -P3306 -uroot -pP0rtFirst shop": "P0rtFirst",
    "mysqldump -P3307 -pDump3d --all-databases": "Dump3d",
    "mysql -p'hunter 2' shop": "hunter 2",
    "sshpass -P assword: -p Pr0mpt ssh host": "Pr0mpt",
    "sshpass -e ssh host; sshpass -p S3condCmd ssh host": "S3condCmd",
    'app --password "correct horse battery staple"': "horse battery staple",
    "PASSWORD 'two words'": "two words",
    "redis://:s3cretpw@cache:6379/0": "s3cretpw",
    # one shell-word reader for every value: a quoted part that starts with a blank, scheme values, escaped blanks
    "curl -u alice:' SecretSuffix' https://example.invalid": "SecretSuffix",
    "api_key=a' SecretSuffix'": "SecretSuffix",
    "Bearer 'foo'bar": ("foo", "bar"),
    'Bearer "in\\"side"': "side",
    "Bearer ab,Zq7Xw9Kp": "Zq7Xw9Kp",
    "Authorization: Bearer ab;Zq7Xw9Kp": "Zq7Xw9Kp",
    "Bearer a'Zq7Xw9Kp": "Zq7Xw9Kp",
    "Bearer 'two words'": ("two", "words"),
    'Bearer "abc def"': ("abc", "def"),
    "wget --user a\\ b:pass\\ word https://example.invalid": ("pass", "word"),
    'bash -c "curl -u alice:\' Nested\'"': "Nested",
    "it's api_key=a' Apostr0phe'": "Apostr0phe",
    # curl in any case, its quoted option names and its bearer option
    "CURL.EXE -u abe:UpperExe https://example.invalid": "UpperExe",
    "Curl -u abe:M1xedCase https://example.invalid": "M1xedCase",
    'curl "-u" "abe:Qu0tedOpt" https://example.invalid': "Qu0tedOpt",
    '"/usr/bin/curl" "-u" "abe:Qu0tedBoth" https://example.invalid': "Qu0tedBoth",
    'curl "--user=abe:Qu0tedGlued" https://example.invalid': "Qu0tedGlued",
    "curl --oauth2-bearer Oauth2Tok https://example.invalid": "Oauth2Tok",
    # JSON written inside a string: names and values in escaped quotes
    '{"body": "{\\"api_key\\": \\"sk-abc123\\"}"}': "sk-abc123",
    '{"msg": "{\\"password\\":\\"hunter2\\"}"}': "hunter2",
    'curl -d "{\\"password\\": \\"correct horse\\"}" https://example.invalid': ("correct", "horse"),
    'kwargs={\\"access_token\\": \\"ya29.abc\\"}': "ya29.abc",
    'X="password=\\"Sec two\\""': ("Sec", "two"),
    # a Bearer inside another scheme's value; other tools' quoted --user, grouped sshpass flags, --pass
    "Token 'Bearer abcdefghijkl'": "abcdefghijkl",
    "using token 'Bearer abcdefgh.ijkl-mnop'": "abcdefgh.ijkl-mnop",
    'wget "--user" abe:Qu0tedWget https://example.invalid': "Qu0tedWget",
    "sshpass -vp Gr0uped ssh host": "Gr0uped",
    "curl --pass Phr4se -E cert.pem https://example.invalid": "Phr4se",
    "ghp_abcdef0123456789abcdef0123456789abcd": "ghp_abcdef0123456789abcdef0123456789abcd",
    # JSON escaped twice, a quote inside an escaped string, a name that is another value's end
    '{"log": "{\\"body\\": \\"{\\\\\\"password\\\\\\": \\\\\\"hunter2 Tw1ce\\\\\\"}\\"}"}': ("hunter2", "Tw1ce"),
    '{\\\\\\"api_key\\\\\\": \\\\\\"sk-abc123\\\\\\"}': "sk-abc123",
    '{\\"password\\":\\"a\\\\\\"b SECc\\"}': "SECc",
    "api_key= db_pass 'HF_TOKEN SECx'": "SECx",
    'password db_pass "z hf_key SECa"': "SECa",
    "hf_probe_secret 'HF_TOKEN SECz'": "SECz",
    "Bearer db_pass 'HF_TOKEN SECu'": "SECu",
    "api_key= db_pass Sec0nd": "Sec0nd",
    "hf_probe_secret (HF_TOKEN SECp)": "SECp",
    # a client certificate's password
    "curl -E cert.pem:CertPw https://example.invalid": "CertPw",
    "curl --cert 'cert.pem:two words' https://example.invalid": ("two", "words"),
    # a value in escaped quotes that ends in a backslash or holds a quote, in JSON written inside a string, once or
    # twice: the next field is still read
    **{json.dumps({"log": json.dumps(record)}): secret for record, secret in (
        ({"password": "C:\\dir\\", "api_key": "a b SEC2"}, "SEC2"),
        ({"password": "pw\\", "msg": "ok", "token": "a SEC3"}, "SEC3"),
        ({"client_secret": "x\\", "access_token": "ya29 SEC4"}, "SEC4"),
        ({"password": "a\\\\", "api_key": "b SEC7"}, "SEC7"),
        ({"password": 'q"x y', "api_key": "c SEC8"}, ("SEC8", "x y")))},
    **{json.dumps({"l": json.dumps({"log": json.dumps(record)})}): secret for record, secret in (
        ({"password": "C:\\dir\\", "api_key": "a b SEC2"}, "SEC2"),
        ({"password": 'q"x y', "api_key": "c SEC8"}, ("SEC8", "x y")))},
    # a value in escaped apostrophes
    "X='password=\\'Sec two\\''": ("Sec", "two"),
    "{'body': '{\\'api_key\\': \\'sk abc\\'}'}": ("sk", "abc"),
    "api_key=\\'SECa SECb\\' tail": ("SECa", "SECb"),
    "api_key:\\'SEC2X SEC3X\\'": ("SEC2X", "SEC3X"),
    # ... that ends in a backslash, and a repr of a repr
    repr("password='SECa SECb\\' \"x\""): ("SECa", "SECb"),
    "X='password=\\'Sec two\\\\\\''": ("Sec", "two"),
    "api_key=\\'SECa SECb\\\\\\' tail": ("SECa", "SECb"),
    repr("curl -u 'u:SECa SECb\\' -H \"X: y\""): ("SECa", "SECb"),
    repr("Authorization: Bearer 'SECa SECb\\' \"x\""): ("SECa", "SECb"),
    repr({"body": repr("password='SECa SECb' \"x\"")}): ("SECa", "SECb"),
    # a repr of a repr whose value holds its own apostrophes
    repr({"body": repr({"password": "rock 'n' SEC1 roll \"x\""})}): ("SEC1", "roll"),
    repr({"body": repr({"password": "Xy7'\"kSEC2 next", "user": "bob"})}): ("SEC2", "next"),
    repr(repr({"password": "p' q\"SEC4"})): "SEC4",
    repr({"body": repr({"token": "ab'; SEC6\""})}): "SEC6",
    repr({"body": repr({"password": "x', SEC7\""})}): "SEC7",
    # a snake-case name's own value is not another value's end
    "db_pass a_key (SEC1X": "SEC1X",
    "x_secret a_key (SEC2X)": "SEC2X",
    "sshpass -h hf_probe_secret a_key (SEC1X": "SEC1X",
    # a curl command written inside a JSON string, its separators in escaped quotes; a repr value's own apostrophe;
    # a value in escaped quotes that ends in a backslash
    '{"cmd":"curl -H \\"Cookie: a;b\\" -u alice:Secret1"}': "Secret1",
    '{"cmd":"curl -H \\"X: a|b\\" -u alice:Secret2"}': "Secret2",
    json.dumps({"cmd": 'curl -H "Cookie: a;b" -u alice:Secret3 https://example.invalid'}): "Secret3",
    repr({"password": "a'\" SECsuffix"}): "SECsuffix",
    json.dumps('token "hunter2\\" ok'): "hunter2",
    json.dumps({"msg": 'token "hunter2\\\\" ok'}): "hunter2",
    # a close shorter than its opening keeps the value's characters; escaped option names and headers in JSON; a
    # repr of JSON whose value holds an apostrophe
    "token=" + "\\" * 7 + '"512SECRET\\" ok': "SECRET",
    "--user " + "\\" * 7 + '"1000:1000SECRT\\" x': "SECRT",
    json.dumps({"cmd": 'curl "-u" "alice:OptSecret"'}): "OptSecret",
    json.dumps({"cmd": r'curl -H "X: \"a;b\"" -u alice:HeaderSecret'}): "HeaderSecret",
    json.dumps({"cmd": r'curl -H "X: \"a|b\"" -u alice:HeaderSecret2'}): "HeaderSecret2",
    repr({"body": json.dumps({"password": "a' SecretSuffix"})}): "SecretSuffix",
    repr(json.dumps({"password": "a' SecretSuffix2"})): "SecretSuffix2",
    # a leak-closing pass never makes another rule's value readable
    "xBearer db_pass (s3cretpw)": "s3cretpw",
    "app.kBearer db_pass (s3cretpw)": "s3cretpw",
    "password --x'a_key (hunter2 s3cretpw)": "hunter2",
    "api_key1 it's fine; db_pass 'hunter2 s3cretpw'": ("hunter2", "s3cretpw"),
    "--password --it's password 'hunter2 s3cretpw'": ("hunter2", "s3cretpw"),
    "HF_TOKEN --it's; HF_TOKEN 'hunter2 s3cretpw'": ("hunter2", "s3cretpw"),
    "password --certa:X1pw": "X1pw",
    "-uBearer a:X2pw": "X2pw",
    "HF_TOKENcurl a:X3pw": "X3pw",
    "hf_tokencurl --X4pw": "X4pw",
    "api_keyfile --X4pw": "X4pw",
    "db_passwordx\t--X4pw": "X4pw",
    "HF_TOKENcurl --X4pw": "X4pw",
    json.dumps(json.dumps("Bearer abc\nSecret42")): "Secret42",
    # a quoted value with an escaped quote in it, a value glued to an earlier [redacted]
    'secret: " S3cret \\" x': "S3cret",
    "api_key= [redacted]S3cret": "S3cret",
    # decoded string literals: the last layer, and a literal whose own decoding stops, are read
    json.dumps(json.dumps(json.dumps(r'"\"password=Secret42\""'))): "Secret42",
    repr("\"curl -H 'Cookie: a;b' -u alice:pw9 h\""): "pw9",
    repr("\"curl -u alice:'pw 9' h\""): ("pw", "9'"),
    '{"url": "\\"https:\\/\\/alice:pw9@h\\/x\\""}': "pw9",
    '"\\"p\\u0061ssword hunter4\\""': "hunter4",
    '"\\"\\u0063url -u alice:pw10 h\\""': "pw10",
    '"\\"HF\\u005fTOKEN hunter5\\""': "hunter5",
    "'\\'Bear\\x65r hunter6\\''": "hunter6",
    # a scheme word that is itself a name with a value
    "api_key: Token : Sch3meName": "Sch3meName",
}
# What the node hides as 2a7c803's node rules did (its base replay); the portal's base showed these, and so does it.
NODE_SECRETS = {
    "db_password_prod ProdSecret42": "ProdSecret42",
    "db_password_prod (hunter2 pw)": "hunter2",
    "secret_db ProdSecret43": "ProdSecret43",
    "Token hunter2": "hunter2",
    "Basic hunter3": "hunter3",
    "auth_header hunter2": "hunter2",
    "x_auth_y Abc123": "Abc123",
    "oauth_cfg S3c": "S3c",
    "sshpass ssh -p S3cret": "S3cret",
    "sshpass x -p S3cret": "S3cret",
    "sshpass -HQ1Z -pQ2Z": "Q2Z",
    "password: my_secret_pw db_password_prod hunter2": "hunter2",
    "api_key: abc_key_1 key_cache hunter2": "hunter2",
    "export HF_TOKEN=x_key_y key_cache S3": "S3",
    "password key_cache key_cache val": "val",
    "Token key_cache key_cache val": "val",
}
# Forms other components rely on (synthetic values), each in the messages that carry it: the exact masked text.
PLACEMENT_FORMS = {
    "HF_TOKEN hf_probe_secret": "HF_TOKEN [redacted]",
    'HF_TOKEN "two word secret"': "HF_TOKEN [redacted]",
    "PASS=pass_probe_secret": "PASS=[redacted]",
    'PASS="two word secret"': "PASS=[redacted]",
    'password="two word secret"': "password=[redacted]",
    'password "two word secret"': "password [redacted]",
    '--password "two word secret"': "--password [redacted]",
    "https://:url_probe_secret@example.invalid": "https://[redacted]@example.invalid",
    "curl -u user:curl_probe_secret": "curl -u [redacted]",
    "mysql -pmysql_probe_secret": "mysql -p[redacted]",
    'mysql -p"two word secret"': "mysql -p[redacted]",
    "sshpass -p ssh_probe_secret": "sshpass -p [redacted]",
    'sshpass -p "two word secret"': "sshpass -p [redacted]",
    "sshpass -p 'two word secret'": "sshpass -p [redacted]",
    "hf_probe_secret": "[redacted]",
    "ghp_probe_secret": "[redacted]",
}
PLACEMENT_MESSAGES = ("{}", "admission unavailable: {}", "audit write failed: {}",
                      "audit write failed: {} (the job was not started)", "the job could not start: {}")
# Text that must stay exactly as it is.
READABLE = (
    "generated 512 tokens in 3.1 s", "keyboard input ignored", "pass 2 of 3 finished", "mysql connection pool size 8",
    "hf_transfer enabled, hf_hub_download retried, hf_qwen3moe loaded", "HF_HUB_ENABLE_HF_TRANSFER=1",
    "docker run --user 1000:1000 img", "curl -sS https://example.invalid | python3 -u -",
    "systemd-run --user --scope -p MemoryMax=16G", "curl -sS https://example.invalid/health",
    "python -u serve.py --port 8000",
    "curl -sS https://example.invalid && python -u serve.py",
    "curl -sS https://example.invalid || python -u serve.py",
    "curl -sS https://example.invalid && systemd-run --user --scope true",
    "curl -sS https://example.invalid & docker run --user 1000:1000 img",
    "curl -sS https://example.invalid; docker run --user 1000:1000 img",
    "curl -XPUT https://example.invalid", "curl -sSL -X PUT https://example.invalid",
    "systemd-run --user --setenv=PYTHONPATH=/foo:/bar python3 -u app.py",
    "systemd-run --user --property=MemoryMax=16G --setenv=A=b:c true",
    'docker run --user "1000:1000" img', "docker run --user '1000:1000' img",
    "curl's manual page",
    "ghs_runner ok", "ghr_ready", "gho_offset=3", "ghu_users table",
    "curl -E cert.pem https://example.invalid", "curl.exe -E C:\\certs\\client.pem https://example.invalid",
    'curl --cert "C:\\Program Files\\certs\\c.pem" https://example.invalid',
    "curl --cert 'C:\\certs\\c.pem' https://example.invalid", 'curl --cert="C:\\certs\\c.pem" https://example.invalid',
    # kept values in escaped quotes: JSON written inside a string, a repr
    json.dumps({"body": json.dumps({"max_tokens": "512", "msg": "ok"})}),
    json.dumps(json.dumps({"api_key": "None"})),
    '{"body": "{\\"token\\": null, \\"max_new_tokens\\": \\"1024\\"}"}',
    json.dumps({"cmd": 'docker run --user "1000:1000" img'}),
    json.dumps({"cmd": r'curl --cert "C:\certs\client.pem" https://example.invalid'}),
    "curl -E 'C:\\certs\\' -H 'X-Trace: 1' https://example.invalid",
)
# Readable on the portal; the node hides a value in each, as 2a7c803's node rules did (its base replay).
# docs/redaction.md lists them.
READABLE_ON_THE_PORTAL = (
    "max_tokens 16", "MAX_TOKENS 5",
    "budget bound violated by the endpoint: max_tokens 16, reported 21 completion tokens",
    "Token counters are reset", "needs imported token counts", "basic setup done", "Token ledger",
    "generated-token counters", "token ids", "tokenizer.ggml.bos_token_id u32 = 151643", "key_cache shape [32, 8192]",
    "the token 12345678 was sampled", "--hf-token 123456789012",
    "max_tokens=512", "--max-num-batched-tokens 8192", "--prompt-tokens 9000",
    "SamplingParams(n=1, max_tokens=512, temperature=0.7)", "token-identical: 2/3 (first pass)",
    "--max-tokens=512, --max-num-batched-tokens 8192;", "--tokenizer /models/qwen", "tokenizer=/models/qwen",
    "sshpass -e ssh -p 2222 host", "sshpass -f pwfile ssh -p 2222 host", "mysql -h db -P3306 shop",
    "token_ids: [1, 2, 3]", "attn_mask_key=None", "max_tokens=\\'512\\' then", 'max_tokens=\\"512\\" then',
    "pad_token_id=-1",
)
# 65f2fe5 kept these; both 2a7c803's node rules and its portal rules hid a value in each (the portal's took any
# token: or token= value, and anything after Bearer), so every path hides it again. docs/redaction.md lists them.
HIDDEN_AS_BOTH_BASES_DID = (
    "token: 151643", "pad_token: <|endoftext|>", "SamplingParams(token=None)",
    'curl -H "Authorization: Bearer [redacted]" -d "x y" https://example.invalid', 'token: \\"None\\" ok',
    "pad_token: </s>", "bos_token: <s>", "unk_token: <unk>", "eos_token: <|im_end|>",
    "eos_token: <\uff5cend\u2581of\u2581sentence\uff5c>",
)
IMPLEMENTATIONS = {
    "shared": redaction.redact_text,
    "node": node.redact,
    "portal text": redact,
    "portal document": lambda line: redact({"stderr": line, "lines": [line]})["lines"][0],
}
PORTAL = {name: implementation for name, implementation in IMPLEMENTATIONS.items() if name != "node"}
# The placement messages on the node where its base hid the parenthesis after the value (a snake-case or upper case
# name, or -p, before a blank): the exact masked text it gives.
NODE_PLACEMENT = {
    "audit write failed: curl -u user:curl_probe_secret (the job was not started)":
        "audit write failed: curl -u [redacted] [redacted] job was not started)",
    "audit write failed: mysql -pmysql_probe_secret (the job was not started)":
        "audit write failed: mysql -p[redacted] [redacted] job was not started)",
    "audit write failed: sshpass -p ssh_probe_secret (the job was not started)":
        "audit write failed: sshpass -p [redacted] [redacted] job was not started)",
    "audit write failed: hf_probe_secret (the job was not started)":
        "audit write failed: [redacted] [redacted] job was not started)",
    "audit write failed: ghp_probe_secret (the job was not started)":
        "audit write failed: [redacted] [redacted] job was not started)",
}


class SharedRulesTests(unittest.TestCase):
    def test_the_node_copy_is_identical(self):
        marker, end = "# --- shared redaction rules", "# --- end of shared redaction rules ---\n"

        def block(path):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(marker), 1, path)
            return text[text.index(marker):text.index(end) + len(end)]
        self.assertEqual(block(NODE_FILE), block(ROOT / "hyperloom" / "redaction.py"))
        # The compiled rules too: pattern source, flags and groups, what ends each value and which values stay,
        # and the code of every function of the block.
        compiled = lambda p: (p.pattern, p.flags, tuple(p.groupindex))
        rule = lambda r: (compiled(r[0]), r[1], r[2].__code__.co_code)
        self.assertEqual([rule(r) for r in node.SECRET_PATTERNS], [rule(r) for r in redaction.SECRET_PATTERNS])
        self.assertEqual([tuple(map(compiled, rule)) for rule in node.GLUED_PASSWORD],
                         [tuple(map(compiled, rule)) for rule in redaction.GLUED_PASSWORD])
        for name in ("CURL_COMMAND", "CURL_OPTION", "_LEXEME", "_DOUBLE_STRING", "_KEPT_PLAIN", "_KEPT_COUNT"):
            self.assertEqual(compiled(getattr(node, name)), compiled(getattr(redaction, name)), name)
        functions = re.findall(r"^def (\w+)", block(NODE_FILE), re.M)
        self.assertIn("_shell_word", functions)
        for name in functions:
            self.assertEqual(getattr(node, name).__code__.co_code, getattr(redaction, name).__code__.co_code, name)
        # The node tool stays self-contained: it never imports the hyperloom package.
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+hyperloom\b", NODE_FILE.read_text(encoding="utf-8"), re.M))

    def test_every_implementation_masks_the_secrets_and_keeps_the_text(self):
        for name, implementation in IMPLEMENTATIONS.items():
            for line, secrets in SECRETS.items():
                with self.subTest(implementation=name, line=line[:80]):
                    result = implementation(line)
                    self.assertIn("[redacted]", result)
                    for secret in (secrets,) if isinstance(secrets, str) else secrets:
                        self.assertNotIn(secret, result)
            for text in READABLE:
                with self.subTest(implementation=name, text=text):
                    self.assertEqual(implementation(text), text)
            for text in READABLE_ON_THE_PORTAL:
                with self.subTest(implementation=name, portal_readable=text):
                    if name == "node":
                        self.assertIn("[redacted]", implementation(text))
                    else:
                        self.assertEqual(implementation(text), text)
            for text in HIDDEN_AS_BOTH_BASES_DID:
                with self.subTest(implementation=name, rehidden=text):
                    self.assertIn("[redacted]", implementation(text))
        for line, secret in NODE_SECRETS.items():
            with self.subTest(node=line):
                self.assertIn("[redacted]", node.redact(line))
                self.assertNotIn(secret, node.redact(line))

    def test_the_forms_other_components_rely_on(self):
        for name, implementation in IMPLEMENTATIONS.items():
            for form, masked in PLACEMENT_FORMS.items():
                for message in PLACEMENT_MESSAGES:
                    line = message.format(form)
                    with self.subTest(implementation=name, line=line):
                        expected = NODE_PLACEMENT.get(line, message.format(masked)) if name == "node" else \
                            message.format(masked)
                        self.assertEqual(implementation(line), expected)

    def test_what_stays_around_a_redacted_value(self):
        for name, implementation in IMPLEMENTATIONS.items():
            with self.subTest(implementation=name):
                self.assertEqual(implementation("password=it\\'s fine and that\\'s all"),
                                 "password=[redacted] fine and that\\'s all")
                self.assertEqual(implementation("api_key= db_pass (x)"), "api_key= [redacted] (x)")
                self.assertEqual(implementation("HF_TOKEN hf_probe_secret (the job was not started)"),
                                 "HF_TOKEN [redacted] (the job was not started)")
                self.assertEqual(implementation("export HF_TOKEN_PATH='C:\\hf\\' && python run.py --model 'Qwen/Qwen3'"),
                                 "export HF_TOKEN_PATH=[redacted] && python run.py --model 'Qwen/Qwen3'")
                self.assertEqual(implementation("app --password 'p\\' --user 'bob' --verbose"),
                                 "app --password [redacted] --user 'bob' --verbose")
                self.assertEqual(implementation("curl -u a:b -U c:d https://x && python -u serve.py"),
                                 "curl -u [redacted] -U [redacted] https://x && python -u serve.py")
                self.assertEqual(implementation("mysql -u root -pXyz shop"), "mysql -u root -p[redacted] shop")
                self.assertEqual(implementation("export HF_TOKEN=" + HF), "export HF_TOKEN=[redacted]")
                # The node's base also read Authorization: as a name and hid the scheme word.
                self.assertEqual(implementation("Authorization: Bearer abc.def"), "Authorization: [redacted] [redacted]"
                                 if name == "node" else "Authorization: Bearer [redacted]")

    def test_argument_lists(self):
        cases = [
            (["sudo", "-H", "-u", "root", "/usr/bin/curl", "-u", "alice:Secret", "https://x"],
             ["sudo", "-H", "-u", "root", "/usr/bin/curl", "-u", "[redacted]", "https://x"]),
            (["curl", "-ubruce:Wonder7land", "-sSUbob:Pr0xy", "https://x", "--next", "--user", "c:d", "https://y"],
             ["curl", "-u[redacted]", "-sSU[redacted]", "https://x", "--next", "--user", "[redacted]", "https://y"]),
            (["curl", "-sS", "https://x", "&&", "python3", "-u", "serve.py", "a:b"],
             ["curl", "-sS", "https://x", "&&", "python3", "-u", "serve.py", "a:b"]),
            (["docker", "run", "--user", "1000:1000", "--user", "kim:pw", "img"],
             ["docker", "run", "--user", "1000:1000", "--user", "[redacted]", "img"]),
            (["systemd-run", "--user", "--scope", "true"], ["systemd-run", "--user", "--scope", "true"]),
            (["mysql", "-uroot", "-pXyzzy42", "shop"], ["mysql", "-uroot", "-p[redacted]", "shop"]),
            (["sshpass", "-p", "Sp4ce", "ssh", "-p", "2222", "host"],
             ["sshpass", "-p", "[redacted]", "ssh", "-p", "2222", "host"]),
            (["vllm", "serve", "--max-num-batched-tokens", "8192", "--tokenizer", "/m", "--hf-token", "tk_1x",
              "--api-key", "k", "--password", "p"],
             ["vllm", "serve", "--max-num-batched-tokens", "8192", "--tokenizer", "/m", "--hf-token", "[redacted]",
              "--api-key", "[redacted]", "--password", "[redacted]"]),
            (["sshpass", "-f", "pwfile", "ssh", "-p", "2222", "host"],
             ["sshpass", "-f", "pwfile", "ssh", "-p", "2222", "host"]),
            (["sshpass", "-P", "assword:", "-p", "Pr0mpt", "ssh", "-p", "22"],
             ["sshpass", "-P", "assword:", "-p", "[redacted]", "ssh", "-p", "22"]),
            (["curl", "-XPUT", "-u", "a:b", "https://x"], ["curl", "-XPUT", "-u", "[redacted]", "https://x"]),
            (["mysql", "-P3306", "-pXyz", "db"], ["mysql", "-P3306", "-p[redacted]", "db"]),
            # Only the command (after a wrapper and its options, or a separator) chooses the client.
            (["curl", "https://example.invalid/mysql", "-u", "alice:Secret"],
             ["curl", "https://example.invalid/mysql", "-u", "[redacted]"]),
            (["curl", "-u", "a:pw/mysql", "-U", "b:Second"], ["curl", "-u", "[redacted]", "-U", "[redacted]"]),
            (["sudo", '"/usr/bin/curl"', "-u", "a:b"], ["sudo", '"/usr/bin/curl"', "-u", "[redacted]"]),
            (["C:\\Program Files\\curl\\curl.exe", "-u", "a:b"],
             ["C:\\Program Files\\curl\\curl.exe", "-u", "[redacted]"]),
            (["env", "A=1", "-u", "X", "timeout", "10", "nice", "-n", "5", "curl", "-u", "a:b"],
             ["env", "A=1", "-u", "X", "timeout", "10", "nice", "-n", "5", "curl", "-u", "[redacted]"]),
            (["systemd-run", "--user", "--scope", "-p", "MemoryMax=16G", "curl", "-u", "a:b"],
             ["systemd-run", "--user", "--scope", "-p", "MemoryMax=16G", "curl", "-u", "[redacted]"]),
            (["sshpass", "-p", "pw", "curl", "-u", "a:b"], ["sshpass", "-p", "[redacted]", "curl", "-u", "[redacted]"]),
            (["make", "&&", "mysql", "-pS3"], ["make", "&&", "mysql", "-p[redacted]"]),
            # Any argument that names a client turns it on until the next separator (an argument list that only
            # mentions mysql loses a glued -p value: hiding too much, never too little).
            (["echo", "mysql", "-pretty"], ["echo", "mysql", "-p[redacted]"]),
            (["sudo", "-n", "curl", "-u", "alice:Secret"], ["sudo", "-n", "curl", "-u", "[redacted]"]),
            (["sudo", "--user", "root", "curl", "-u", "a:S"], ["sudo", "--user", "root", "curl", "-u", "[redacted]"]),
            (["env", "--unset", "X", "curl", "-u", "a:S"], ["env", "--unset", "X", "curl", "-u", "[redacted]"]),
            (["sudo", "-iu", "root", "curl", "-u", "a:S"], ["sudo", "-iu", "root", "curl", "-u", "[redacted]"]),
            (["ssh", "host", "curl", "-u", "a:S"], ["ssh", "host", "curl", "-u", "[redacted]"]),
            (["sshpass", "-p", "pw", "ssh", "h", "curl", "-u", "a:S"],
             ["sshpass", "-p", "[redacted]", "ssh", "h", "curl", "-u", "[redacted]"]),
            (["docker", "exec", "c", "curl", "-u", "a:S"], ["docker", "exec", "c", "curl", "-u", "[redacted]"]),
            (["kubectl", "exec", "p", "--", "curl", "-u", "a:S"],
             ["kubectl", "exec", "p", "--", "curl", "-u", "[redacted]"]),
            (["A=1", "curl", "-u", "a:S"], ["A=1", "curl", "-u", "[redacted]"]),
            (["timeout", "--signal", "KILL", "10", "curl", "-u", "a:S"],
             ["timeout", "--signal", "KILL", "10", "curl", "-u", "[redacted]"]),
            (["xargs", "-I", "{}", "curl", "-u", "a:S", "{}"], ["xargs", "-I", "{}", "curl", "-u", "[redacted]", "{}"]),
            (["echo", "x;", "curl", "-u", "a:S"], ["echo", "x;", "curl", "-u", "[redacted]"]),
            (["CURL.EXE", "-u", "a:S"], ["CURL.EXE", "-u", "[redacted]"]),
            # A separator that is an option's value does not end the command.
            (["curl", "-o", ";", "-u", "a:S"], ["curl", "-o", ";", "-u", "[redacted]"]),
            (["curl", "-d", "&&", "-u", "a:S"], ["curl", "-d", "&&", "-u", "[redacted]"]),
            # Glued long options and their values read as the text rules read them.
            (["curl", "--user=alice:Secret", "--proxy-user=bob:ProxySecret"],
             ["curl", "--user=[redacted]", "--proxy-user=[redacted]"]),
            (["app", "--api-key=k1", "--max-tokens=512", "--hf-token=tk_x", "--user=1000:1000"],
             ["app", "--api-key=[redacted]", "--max-tokens=512", "--hf-token=[redacted]", "--user=1000:1000"]),
            (["curl", "--oauth2-bearer", "S"], ["curl", "--oauth2-bearer", "[redacted]"]),
            (["curl", "--pass", "S", "--bypass", "x"], ["curl", "--pass", "[redacted]", "--bypass", "x"]),
            (["curl", "-E", "cert:Sec", "-Ec:S", "--cert=c.pem:S", "--cert", "C:\\c.pem", "-E", "c.pem"],
             ["curl", "-E", "[redacted]", "-E[redacted]", "--cert=[redacted]", "--cert", "C:\\c.pem", "-E", "c.pem"]),
            (["curl", "--cert=C:\\c.pem", "-EC:\\c.pem", "--cert", '"C:\\c.pem"', "-Ec.pem:S"],
             ["curl", "--cert=C:\\c.pem", "-EC:\\c.pem", "--cert", '"C:\\c.pem"', "-E[redacted]"]),
            # An argument is one whole value, blanks included; a credential option keeps None/null/true/false.
            (["curl", "--user=alice smith:Secret", "-ualice smith:Secret"],
             ["curl", "--user=[redacted]", "-u[redacted]"]),
            # After --api-key, --handoff-api-key, --password or --token the next argument is hidden whatever it is, as
            # 2a7c803's portal did, and so is password=null (its text rule); another credential option keeps None.
            (["app", "--api-key", "None", "--password=null", "--password", "Secret", "--db-password", "None"],
             ["app", "--api-key", "[redacted]", "--password=[redacted]", "--password", "[redacted]", "--db-password",
              "None"]),
            (["app", "--token", "512", "--max-tokens", "512", "--hf-token", "123456789012", "--token", ";", "x"],
             ["app", "--token", "[redacted]", "--max-tokens", "512", "--hf-token", "123456789012", "--token",
              "[redacted]", "x"]),
            # A quoted word at the start of an argument does not make the rest of it readable.
            (["app", "--password", "'None' ArgSecret", "--password", "None"],
             ["app", "--password", "[redacted]", "--password", "[redacted]"]),
            (["curl", "--user='alice' smith:ArgSecret", "--hf-token=123 SECX"],
             ["curl", "--user=[redacted]", "--hf-token=[redacted]"]),
            # An option name that keeps literal quotes.
            (["wget", '"--user"', "a:Sec"], ["wget", '"--user"', "[redacted]"]),
            (["curl", "'-u'", "a:S", "'--api-key=x'"], ["curl", "'-u'", "[redacted]", "'--api-key=[redacted]'"]),
            (["sshpass", "-vp", "Sec", "ssh", "-p", "22"], ["sshpass", "-vp", "[redacted]", "ssh", "-p", "22"]),
            (["sshpass", "-vpSec", "ssh", "-p", "22"], ["sshpass", "-vp[redacted]", "ssh", "-p", "22"]),
            (["docker", "run", "--user", '"1000:1000"', "--user", "'kim:pw'", "img"],
             ["docker", "run", "--user", '"1000:1000"', "--user", "[redacted]", "img"]),
            (["systemd-run", "--user", "--setenv=PYTHONPATH=/foo:/bar", "python3", "-u", "app.py"],
             ["systemd-run", "--user", "--setenv=PYTHONPATH=/foo:/bar", "python3", "-u", "app.py"]),
            (["C:\\tools\\curl.exe", "-#u", "a:b", "-Zuc:d"],
             ["C:\\tools\\curl.exe", "-#u", "[redacted]", "-Zu[redacted]"]),
            (["C:\\mysql\\bin\\mysql.exe", "-pS3"], ["C:\\mysql\\bin\\mysql.exe", "-p[redacted]"]),
            (["vllm", "--hf-token", "123456789012", "--max-tokens", "<|endoftext|>"],
             ["vllm", "--hf-token", "123456789012", "--max-tokens", "<|endoftext|>"]),
            (["env", "HF_HUB_ENABLE_HF_TRANSFER=1", "HF_TOKEN=" + HF, HF],
             ["env", "HF_HUB_ENABLE_HF_TRANSFER=1", "HF_TOKEN=[redacted]", "[redacted]"]),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(redaction.redact_argv(argv), expected)
                self.assertEqual(redact(argv), expected)
                self.assertEqual(redact({"argv": argv})["argv"], expected)
                # Recipe results: keys and argv only (text=False), so the argv rules alone must hide them (all but a
                # bare token after an environment name and password=null, which only the text rules hide, at
                # 2a7c803 too).
                recipe = [item if item == "--password=null" else shown for item, shown in zip(argv, expected)]
                if "HF_TOKEN=" + HF not in argv:
                    self.assertEqual(recipe_web.redacted({"result": {"argv": argv}}, redact)["result"]["argv"], recipe)
        self.assertEqual(redaction.redact_argv([1, None, "x"]), [1, None, "x"])       # non-text items kept
        self.assertEqual(redact("curl -u x:y " + HF, text=False), "curl -u x:y " + HF)     # documents' prose

    def test_redaction_time_grows_linearly(self):
        # Lines built to make a backtracking rule re-scan: long runs of name characters with and without a
        # credential word, repeated separators, options, clients, quotes and URL schemes.
        units = ("a", "=", "token", "keyw", "TOKEN_", "pass_", "--key", "--", "a.", "a://", "curl ", "curl -",
                 "curl '", "curl -u ", "'x", '"', "mysql ", "sshpass ", "--user=", "--user=x", " -p", "Token ",
                 "Bearer ", "hf_", "\\\n", "x--key", "key=", "A_", "_", "sshpass -f '", "sshpass -d ",
                 "sshpass\n -f ", 'sshpass -P "', "sshpass -e ", "mysql -P3 ", "a_b_", "pad_token: <", "redis://:",
                 '"\\"', "'\\'\"", '\\"', 'curl "\\', "x\\ ", "curl -u'a'", "password -U ", 'key="', "key='x",
                 'key="\\', "key=a'", 'key=a"b ', "--user '", "--user a'", '"/curl', "'curl\"", '"C:\\x\\', "Bearer '",
                 " '", "a' ", "x='", "\"'", "Bearer a'", "-u a:' ", "\" -u ", "'-u' ", "it's ", "=\"", "CURL -u ",
                 "--oauth2-bearer ", '"\\', "\\'", ": '", "=' x'", '\\"', 'key=\\"', '{\\"key\\": \\"',
                 "Token 'Bearer ", "x_secret ", "hf_secret ", "ghp_", "sshpass -vp ", '"--user" ', "\\\\\\\"",
                 '\\\\\\"k\\\\\\":\\\\\\"', "a_secret (", "a_key (x a_key ", "curl -E ", "\\\\",
                 '\\\\\\\\\\"', 'k=\\"x\\\\\\\\\\" ', "k=\\'", "k=\\'x ", "a_key a_key (",
                 "--password \\'", "Bearer \\'", "password=\\'x ", "api_key: \\\\\\'", "token: \\'\\\\\\' ",
                 "curl --cert=\\'", "password=\\'a b ",
                 # string literals nested in string literals (decoded and read again, 4 layers deep)
                 json.dumps({"m": json.dumps({"password": "a b"})}), repr(json.dumps({"token": "x\\"})),
                 json.dumps(json.dumps(json.dumps('"'))), '"\\"\\\\\\"')
        # Values whose closing quote is far away: one close at the end of the line, or none on many lines.
        tails = (("curl -E \\'", "\\'"), ("curl --cert=\\'", "\\'"), ("curl -E \\'", "\\\\\\'"), ("curl -E \\\"", "\\\""),
                 ("--password \\'a\n", ""), ("--password \\'a\\'b\n", ""), ("api_key: \\\\\\'a\n", ""),
                 ("password=\\'a b\n", ""), ("a='\n", "'"), ("password=\\\"x\n", "\\\""))
        for unit, tail in tails:
            for name, implementation in (("shared", redaction.redact_text), ("portal", redact)):
                self.assert_linear(name, unit + tail, lambda size, unit=unit, tail=tail, run=implementation: run(
                    (unit * (size // len(unit) + 1))[:size] + tail))
            # The node reads one line at a time.
            self.assert_linear("node", unit + tail, lambda size, unit=unit, tail=tail: node.redact(
                ((unit * (size // len(unit) + 1))[:size] + tail).replace("\n", " ")))
        for name, implementation in (("shared", redaction.redact_text), ("node", node.redact)):
            for unit in units:
                self.assert_linear(name, unit, lambda size: implementation((unit * (size // len(unit) + 1))[:size]))
        # Argument lists: every item of a portal list goes through them; long items whose suffix fails a pattern.
        for name, implementation in (("shared", redaction.redact_argv), ("portal", redact)):
            for unit in ("--key", "--token", "-u", "-s", "/mysql", "mariadb-", "--", "a:", "-p", "--user=a:",
                         "--api-key=", "--max-tokens=1", "'", '"', "://curl", "curl.exe", "-E", "\"--user\"",
                         "'"):
                def run(size, unit=unit):
                    item = (unit * (size // len(unit) + 1))[:size] + "!"
                    implementation(["curl", item, "-u", item, "--user", item, "sshpass", "-p", item, "mysql", item])
                self.assert_linear(name, unit, run)

    def test_64_kb_worst_cases(self):
        # One 64 KB line (or text, for the portal) per worst case found so far, string literals nested in string
        # literals among them (each decoded layer is read once more): each stays well under a second.
        # Credential-dense prose JSON-encoded 4 times over: every layer is decoded.
        dense = ("a_key 'it's' db_pass (x) password --x xBearer a hf_tokens b Token c " * 962) + "\n"
        wrapped = prose = dense
        for _ in range(4):
            dense, wrapped, prose = json.dumps(dense), json.dumps({"m": wrapped}), "x " + json.dumps(prose) + " y"
        # Every layer of the forms in an object or in prose runs every pass (about 0.6-0.8 s on a loaded machine;
        # linear, see test_redaction_time_grows_linearly): the bound leaves room for a slow machine.
        for name, implementation in (("shared", redaction.redact_text), ("node", node.redact), ("portal", redact)):
            for unit, line in (("dense, 4 layers", dense), ("dense in objects, 4 layers", wrapped),
                               ("dense in prose, 4 layers", prose)):
                with self.subTest(implementation=name, unit=unit):
                    started = time.perf_counter()
                    implementation(line)
                    self.assertLess(time.perf_counter() - started, 2.5)
        nested = json.dumps({"log": json.dumps({"password": "a b\\", "api_key": "x"})})
        units = ("--password \\'", "curl -E \\'", "Bearer \\'", "\\\\\\\"", "'\\'\"", nested, repr(nested),
                 json.dumps(repr(nested)), '"\\"' * 3, "'\\\\'", "a_key (", "--key=", "x\\\"y ", "{\\\"k\\\": \\\"")
        for unit in units:
            line = (unit * (65536 // len(unit) + 1))[:65536]
            for name, implementation in (("shared", redaction.redact_text), ("node", node.redact),
                                         ("portal", redact)):
                with self.subTest(implementation=name, unit=unit[:40]):
                    started = time.perf_counter()
                    implementation(line if name != "portal" else line.replace(" ", "\n", 64))
                    self.assertLess(time.perf_counter() - started, 1.0)

    def assert_linear(self, name, unit, run):
        timings = []
        for size in (8000, 32000):
            best = float("inf")
            for _ in range(3):
                started = time.perf_counter()
                run(size)
                best = min(best, time.perf_counter() - started)
            timings.append(best)
        with self.subTest(implementation=name, unit=unit, timings=timings):
            # 4x the input: well under 8x the time (a quadratic rule takes 16x), above a noise floor.
            self.assertLess(timings[1], max(8 * timings[0], 0.25))
            self.assertLess(timings[1], 2.0)


if __name__ == "__main__":
    unittest.main()
