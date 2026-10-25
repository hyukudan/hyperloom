"""A/B runner with exactness receipts (F26, contract C12).

Two predeployed arms (two targets; the same target twice is an A/A control) run the same seeded greedy corpus
interleaved ABA x3: three rounds, each an A pass, a B pass and an A pass over the whole corpus (A runs six passes,
B three). The runner never deploys or switches anything.

The receipt ``hyperloom/ab-receipt-v1`` is self-contained and verifiable offline (``verify``):

- ``arms``: per arm the address (scheme://host:port, no credentials), model, tokenizer id with its source
  (``reported`` / ``declared`` / ``unavailable``: the key is omitted when unknown), build (binary sha256) and
  recipe digest and router policy, each with its source. A model or tokenizer id that the portal's redaction would
  alter is carried hex-encoded as ``model_hex`` / ``vocab_hex``.
- ``corpus``: seed, prompt set, ``stop_policy``, ``corpus_digest`` (sha256 of the canonical bodies without
  ``model``) and per case ``body_hex`` (hex of the canonical UTF-8 JSON body without ``model``) and
  ``request_sha256`` per arm (sha256 of the canonical body with that arm's model).
- ``trial_order``: the nine planned passes ``{round, pass, arm}``; ``trials``: one per pass and case with its
  outcome (``ok``, ``no-ids``, ``error``), finish reason, completion count, client timings and the complete token
  ids (a list; ``gz:b64:`` + base64 of gzip JSON when longer than 256 ids and the encoding survives the portal's
  redaction unchanged). A trial without ids has no ``token_ids`` key.
- ``comparison``: per case ``equal`` / ``diverged`` (with the first divergent index) / ``unavailable``, from the
  first pass of each arm; ``determinism``: per arm the cases whose every repeat equals its first pass.
- ``speed``: per round the matched cases (all three trials succeeded with usage and output), each pass's end-to-end
  rate = sum(completion) / sum(last_output) [client, measured from the request write], and the delta
  B / mean(flanking A) - 1.
- ``summary``: identical count over all cases, the claim and its scope, determinism counts, the speed deltas.

"token-identical: n/N" is claimed only when both arms report the same model id and the same known tokenizer id.
"""

import base64
import gzip
import hashlib
import json
import random
import statistics
import time
import uuid

from hyperloom.bench import data, runs, store as bench_store, targets
from hyperloom.web.api import redact


SCHEMA = "hyperloom/ab-receipt-v1"
OWN = ("arms", "tokenizers", "prompt_set", "cases", "max_tokens", "seed", "token_budget", "trace", "run_id", "planned_tokens")
SETS = ("structured", "prose", "code", "json")
ORDER = [{"round": r, "pass": 3 * r + i, "arm": arm} for r in range(3) for i, arm in enumerate("aba")]
LONG_IDS = 256


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(data_bytes):
    return hashlib.sha256(data_bytes).hexdigest()


def corpus(prompt_set, cases, seed):
    prompts = list(data("prompts.json")["sets"][prompt_set])
    random.Random(seed).shuffle(prompts)
    return [[{"role": "user", "content": prompts[i % len(prompts)]}] for i in range(cases)]


def case_body(messages, max_tokens):
    """The arm-independent request body of a case (greedy, streamed with usage and token ids)."""
    return {"messages": messages, "max_tokens": max_tokens, "temperature": 0, "stream": True,
            "stream_options": {"include_usage": True}, "return_token_ids": True}


HEX_KEYS = {"model": "model_hex", "tokenizer_id": "vocab_hex"}


def transportable(identity):
    """Strings the verifier needs (model, tokenizer id) are kept as they are only when the portal's redaction leaves
    them unchanged, else hex-encoded under a key redaction never treats as sensitive (``model_hex``, ``vocab_hex``);
    other strings are redacted once here (redaction is idempotent)."""
    result = {}
    for key, value in identity.items():
        if key in HEX_KEYS and isinstance(value, str) and redact({key: value}) != {key: value}:
            result[HEX_KEYS[key]] = value.encode("utf-8").hex()
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


def identity_value(arm, key):
    if arm.get(HEX_KEYS[key]) is not None:
        return bytes.fromhex(arm[HEX_KEYS[key]]).decode("utf-8")
    return arm.get(key)


def encode_ids(ids):
    if len(ids) <= LONG_IDS:
        return ids
    text = "gz:b64:" + base64.b64encode(gzip.compress(json.dumps(ids, separators=(",", ":")).encode("ascii"), 9, mtime=0)).decode("ascii")
    return text if redact({"token_ids": text}) == {"token_ids": text} else ids


def decode_ids(value):
    if isinstance(value, list):
        if all(type(v) is int and v >= 0 for v in value):
            return value
        raise ValueError("token ids must be non-negative integers")
    if isinstance(value, str) and value.startswith("gz:b64:"):
        ids = json.loads(gzip.decompress(base64.b64decode(value[7:], validate=True)))
        return decode_ids(ids)
    raise ValueError("token ids must be a list or gz:b64 text")


def first_divergence(a, b):
    for index, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return index
    return None if len(a) == len(b) else min(len(a), len(b))


# -- validation ---------------------------------------------------------------------------------------------------
def _arm(api, value):
    parsed = targets.parse(value)
    row = targets.describe(api, parsed)
    return parsed, row


def validate(api, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("JSON object required")
    unknown = set(arguments) - set(OWN)
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)) + " (A/B runs are greedy: no sampling or thinking switch)")
    arms = arguments.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"a", "b"}:
        raise ValueError('arms must be {"a": target, "b": target}')
    normalised = {}
    for name in ("a", "b"):
        parsed, _ = _arm(api, arms[name])
        normalised[name] = {"target": targets.as_argument(parsed), "target_digest": targets.digest(api, parsed)}
    declared = arguments.get("tokenizers") or {}
    if not isinstance(declared, dict) or set(declared) - {"a", "b"} or any(
            not isinstance(v, str) or not 1 <= len(v) <= 200 or any(c.isspace() for c in v) for v in declared.values()):
        raise ValueError("tokenizers: optional declared tokenizer ids per arm (no whitespace, at most 200 characters)")
    prompt_set = arguments.get("prompt_set", "prose")
    if prompt_set not in SETS:
        raise ValueError("prompt_set must be one of " + ", ".join(SETS))
    args = {"arms": normalised, "tokenizers": dict(declared), "prompt_set": prompt_set,
            "cases": runs._int(arguments, "cases", 1, 64, 8), "max_tokens": runs._int(arguments, "max_tokens", 8, 1024, 64),
            "seed": runs._int(arguments, "seed", 0, 2 ** 31 - 1, 1),
            "token_budget": runs._int(arguments, "token_budget", 1000, runs.MAX_BUDGET, runs.DEFAULT_BUDGET),
            "trace": runs._bool(arguments, "trace"), "run_id": uuid.uuid4().hex[:16],
            "sampling": None, "thinking": None}
    bodies = [case_body(m, args["max_tokens"]) for m in corpus(prompt_set, args["cases"], args["seed"])]
    planned = sum(runs.request_bound({**b, "model": "m" * 200}) for b in bodies) * len(ORDER)
    return runs.check_plan(args, planned)


def resources(api, arguments):
    found = set()
    for arm in arguments["arms"].values():
        found |= targets.resources(api, targets.parse(arm["target"]))
    return found


# -- execution ----------------------------------------------------------------------------------------------------
def _trial(run, arm, endpoint, context, body, timeout, round_index, pass_index, case):
    request = dict(body, model=arm["model"])
    reply = run.chat(request, timeout, endpoint=endpoint, context_tokens=context)
    ids = reply.token_ids if reply.ok else None
    trial = {"round": round_index, "pass": pass_index, "arm": arm["name"], "case": case,
             "outcome": "ok" if reply.ok and ids is not None else ("no-ids" if reply.ok else "error"),
             "finish_reason": reply.finish_reason, "completion_tokens": reply.completion_tokens,
             "ttft_s": runs.rounded(reply.ttft_s, 4), "last_output_s": runs.rounded(reply.last_output_s, 4),
             "end_s": runs.rounded(reply.end_s, 4)}
    if not reply.ok:
        trial["error"] = redact((reply.error or f"HTTP {reply.status}")[:200])
    if trial["finish_reason"] is not None:
        trial["finish_reason"] = redact(str(trial["finish_reason"])[:40])
    if ids is not None:
        trial["token_ids"] = encode_ids(ids)
    return trial


def execute(api, arguments, ctx, store=None):
    """The A/B job (portal and CLI): resolves both arms, runs ABA x3 and stores the receipt."""
    store = store or bench_store.opened(api)
    bench_store.mark_stale(store)
    resolved, probed = {}, {}
    try:
        for name in ("a", "b"):
            arm = arguments["arms"][name]
            resolved[name] = targets.resolve(api, targets.parse(arm["target"]), arm["target_digest"])
            probed[name] = targets.probe_model(resolved[name], ctx.cancelled)
    except BaseException as exc:
        cancelled = ctx.cancelled()
        bench_store.write(store, {"id": arguments["run_id"], "kind": "ab",
                                  "target_key": targets.key_of(targets.parse(arguments["arms"]["a"]["target"])),
                                  "target_label": "A/B", "state": "cancelled" if cancelled else "failed",
                                  "started": time.time(), "finished": time.time(), "model": None, "recipe_digest": None,
                                  "summary": {"kind": "ab", "reason": (ctx.reason if cancelled else f"{type(exc).__name__}: {exc}")[:300]},
                                  "detail": None})
        if cancelled:
            return None
        raise
    a_model, a_context, a_tokenizer = probed["a"]
    run = runs.Run(api, "ab", arguments, ctx, resolved["a"], a_model, resolved["a"].context_tokens or a_context, a_tokenizer, store)
    run.resolved = targets.Resolved(resolved["a"].key, f"{resolved['a'].label} vs {resolved['b'].label}", "ab",
                                    resolved["a"].endpoint, None, {}, run.context_tokens, resolved["a"].identity)
    arms = {}
    for name in ("a", "b"):
        model, context, tokenizer = probed[name]
        identity = transportable(runs.identity(resolved[name], model, tokenizer, arguments["tokenizers"].get(name)))
        arms[name] = {"name": name, "model": model, "context": resolved[name].context_tokens or context,
                      "endpoint": resolved[name].endpoint, "identity": identity}
    run.summary["families"] = [resolved["a"].family, resolved["b"].family]
    run.live(force=True)

    return runs.finish_with(run, ctx, lambda run: passes(run, arms, arguments))


def passes(run, arms, arguments):
    """ABA x3 over the corpus; fills run.detail["receipt"] and the summary. ``arms``: {a, b} with name, model,
    context, endpoint and identity."""
    messages = corpus(arguments["prompt_set"], arguments["cases"], arguments["seed"])
    bodies = [case_body(m, arguments["max_tokens"]) for m in messages]
    timeout = 120 + arguments["max_tokens"] * 0.5
    trials = []
    run.detail.update(receipt=None, trials=trials)       # kept (and stored live) if the run stops before the end
    for step in ORDER:
        for case, body in enumerate(bodies):
            if run.cancelled():
                return
            arm = arms[step["arm"]]
            run.progress(f"round {step['round'] + 1}/3 · pass {step['arm'].upper()} · case {case + 1}/{len(bodies)}")
            trials.append(_trial(run, arm, arm["endpoint"], arm["context"], body, timeout, step["round"], step["pass"], case))
            run.summary["trials_done"] = len(trials)
            run.live()
    receipt = build(arms, arguments, bodies, trials)
    run.detail.pop("trials")
    run.detail["receipt"] = receipt
    run.summary.update(receipt["summary"], cases=len(bodies), prompt_set=arguments["prompt_set"],
                       max_tokens=arguments["max_tokens"])


# -- receipt ------------------------------------------------------------------------------------------------------
def build(arms, arguments, bodies, trials):
    cases = []
    for body in bodies:
        entry = {"body_hex": canonical(body).hex(),
                 "request_sha256": {name: sha(canonical(dict(body, model=arms[name]["model"]))) for name in ("a", "b")}}
        cases.append(entry)
    receipt = {"schema": SCHEMA, "created_at": round(time.time(), 3),
               "arms": {name: arms[name]["identity"] for name in ("a", "b")},
               "corpus": {"seed": arguments["seed"], "prompt_set": arguments["prompt_set"], "cases": cases,
                          "corpus_digest": sha(canonical([json.loads(bytes.fromhex(c["body_hex"])) for c in cases])),
                          "stop_policy": {"max_tokens": arguments["max_tokens"], "stop_sequences": [],
                                          "rule": "end of sequence or max_tokens"}},
               "trial_order": ORDER, "trials": trials}
    receipt.update(derive(receipt))
    if redact(receipt) != receipt:
        raise ValueError("internal: the receipt would not survive the portal's redaction unchanged")
    return receipt


def _ids(trial):
    if trial.get("outcome") != "ok" or "token_ids" not in trial:
        return None
    return decode_ids(trial["token_ids"])


def _rate(trials):
    seconds = sum(t["last_output_s"] for t in trials)
    return sum(t["completion_tokens"] for t in trials) / seconds if seconds > 0 else None


def _usable(trial):
    return (trial is not None and trial.get("outcome") in ("ok", "no-ids") and type(trial.get("completion_tokens")) is int
            and trial["completion_tokens"] >= 1 and isinstance(trial.get("last_output_s"), (int, float))
            and trial["last_output_s"] > 0)


def derive(receipt):
    """Everything computed from the trials: comparison, determinism, speed, summary (also what ``verify`` checks)."""
    arms, order = receipt["arms"], receipt["trial_order"]
    count = len(receipt["corpus"]["cases"])
    by = {(t["pass"], t["case"]): t for t in receipt["trials"]}
    first = {arm: next(step["pass"] for step in order if step["arm"] == arm) for arm in ("a", "b")}
    comparison, identical = [], 0
    for case in range(count):
        a, b = by.get((first["a"], case)), by.get((first["b"], case))
        ids_a, ids_b = (_ids(a) if a else None), (_ids(b) if b else None)
        if ids_a is None or ids_b is None:
            missing = [arm.upper() for arm, ids in (("a", ids_a), ("b", ids_b)) if ids is None]
            comparison.append({"case": case, "result": "unavailable", "why": "no token ids from " + " and ".join(missing)})
            continue
        index = first_divergence(ids_a, ids_b)
        if index is None:
            identical += 1
            comparison.append({"case": case, "result": "equal", "length": len(ids_a)})
        else:
            comparison.append({"case": case, "result": "diverged", "first_divergence": index,
                               "lengths": [len(ids_a), len(ids_b)]})
    determinism = {}
    for arm in ("a", "b"):
        passes = [step["pass"] for step in order if step["arm"] == arm]
        stable = 0
        for case in range(count):
            arrays = [_ids(by[(p, case)]) if (p, case) in by else None for p in passes]
            if arrays[0] is not None and all(x == arrays[0] for x in arrays):
                stable += 1
        determinism[arm] = {"deterministic_cases": stable, "cases": count, "passes": len(passes)}
    speed = []
    for round_index in sorted({step["round"] for step in order}):
        steps = [step for step in order if step["round"] == round_index]
        matched = [case for case in range(count) if all(_usable(by.get((s["pass"], case))) for s in steps)]
        rates = [(_rate([by[(s["pass"], c)] for c in matched]) if matched else None) for s in steps]
        entry = {"round": round_index, "matched_cases": matched,
                 "rates": [None if r is None else round(r, 6) for r in rates]}
        a_rates = [r for s, r in zip(steps, rates) if s["arm"] == "a" and r is not None]
        b_rates = [r for s, r in zip(steps, rates) if s["arm"] == "b" and r is not None]
        if matched and len(a_rates) == 2 and len(b_rates) == 1:
            entry["delta_b_vs_a"] = round(b_rates[0] / statistics.mean(a_rates) - 1, 6)
        else:
            entry["why"] = "no case completed in all three passes of this round"
        speed.append(entry)
    deltas = [s["delta_b_vs_a"] for s in speed if "delta_b_vs_a" in s]
    same_model = identity_value(arms["a"], "model") == identity_value(arms["b"], "model")
    tokenizers = [identity_value(arms[x], "tokenizer_id") for x in ("a", "b")]
    with_ids = sum(1 for c in comparison if c["result"] != "unavailable")
    if with_ids == 0:
        claim = "token ids unavailable: no identity claim"
    elif not same_model:
        claim = "withheld: the arms serve different models"
    elif None in tokenizers:
        claim = "withheld: tokenizer id not known for " + " and ".join(x.upper() for x, t in zip("ab", tokenizers) if t is None)
    elif tokenizers[0] != tokenizers[1]:
        claim = "withheld: the arms use different tokenizers"
    else:
        claim = f"token-identical: {identical}/{count} (first pass of each arm)"
    summary = {"identical": identical, "cases": count, "compared": with_ids, "claim": claim,
               "deterministic": {arm: f"{determinism[arm]['deterministic_cases']}/{count}" for arm in ("a", "b")},
               "speed_deltas_b_vs_a": deltas,
               "speed_delta_median": round(statistics.median(deltas), 6) if deltas else None,
               "speed_delta_range": [min(deltas), max(deltas)] if deltas else None}
    return {"comparison": comparison, "determinism": determinism, "speed": speed, "summary": summary}


def verify(receipt):
    """Recompute every equality and derived figure of a receipt; returns {ok, problems, summary}. Malformed input is
    reported as a problem, never raised."""
    try:
        return _verify(receipt)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError, OSError, EOFError) as exc:
        return {"ok": False, "problems": [f"malformed receipt: {type(exc).__name__}: {exc}"[:300]], "summary": None}


def _verify(receipt):
    problems = []

    def problem(text):
        if len(problems) < 200:
            problems.append(text)
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        return {"ok": False, "problems": ["not a " + SCHEMA + " document"], "summary": None}
    try:
        arms, corpus_part, order, trials = receipt["arms"], receipt["corpus"], receipt["trial_order"], receipt["trials"]
        cases = corpus_part["cases"]
    except (KeyError, TypeError):
        return {"ok": False, "problems": ["missing arms, corpus, trial_order or trials"], "summary": None}
    if order != ORDER:
        problem("trial_order is not ABA x3")
    for name in ("a", "b"):
        try:
            if not isinstance(arms.get(name), dict) or not isinstance(identity_value(arms[name], "model"), str):
                problem(f"arm {name}: model missing")
            identity_value(arms[name], "tokenizer_id")
        except (ValueError, UnicodeDecodeError):
            problem(f"arm {name}: malformed hex identity")
    bodies = []
    for index, case in enumerate(cases):
        try:
            raw = bytes.fromhex(case["body_hex"])
            body = json.loads(raw)
        except (KeyError, ValueError, TypeError):
            problem(f"case {index}: body_hex is not hex of a JSON body")
            bodies.append(None)
            continue
        if canonical(body) != raw:
            problem(f"case {index}: body is not in canonical form")
        if "model" in body:
            problem(f"case {index}: the arm-independent body names a model")
        if body.get("max_tokens") != corpus_part.get("stop_policy", {}).get("max_tokens") or body.get("stop"):
            problem(f"case {index}: body differs from the stop policy")
        if body.get("temperature") != 0:
            problem(f"case {index}: body is not greedy")
        for name in ("a", "b"):
            try:
                model = identity_value(arms.get(name) or {}, "model")
            except (ValueError, UnicodeDecodeError):
                model = None
            if sha(canonical(dict(body, model=model))) != (case.get("request_sha256") or {}).get(name):
                problem(f"case {index}: request_sha256 of arm {name} does not match its body")
        bodies.append(body)
    if None not in bodies and sha(canonical(bodies)) != corpus_part.get("corpus_digest"):
        problem("corpus_digest does not match the case bodies")
    if not isinstance(arms, dict) or not isinstance(cases, list) or not isinstance(trials, list):
        return {"ok": False, "problems": ["arms, cases or trials have the wrong type"], "summary": None}
    seen = {}
    for trial in trials:
        if not isinstance(trial, dict):
            problem("a trial is not an object")
            continue
        if trial.get("case") not in range(len(cases)):
            problem(f"trial pass {trial.get('pass')} names case {trial.get('case')}, outside the corpus")
        key = (trial.get("pass"), trial.get("case"))
        if key in seen:
            problem(f"trial pass {key[0]} case {key[1]} appears twice")
        seen[key] = trial
        step = ORDER[trial["pass"]] if isinstance(trial.get("pass"), int) and 0 <= trial["pass"] < len(ORDER) else None
        if step is None or trial.get("arm") != step["arm"] or trial.get("round") != step["round"]:
            problem(f"trial pass {key[0]} case {key[1]}: arm or round does not follow trial_order")
        if trial.get("outcome") not in ("ok", "no-ids", "error"):
            problem(f"trial pass {key[0]} case {key[1]}: unknown outcome")
        if trial.get("outcome") == "ok":
            try:
                ids = decode_ids(trial["token_ids"])
            except (KeyError, ValueError, TypeError, OSError, EOFError):
                problem(f"trial pass {key[0]} case {key[1]}: token ids missing or malformed")
            else:
                if type(trial.get("completion_tokens")) is not int or len(ids) != trial["completion_tokens"]:
                    problem(f"trial pass {key[0]} case {key[1]}: {len(ids)} token ids for "
                            f"{trial.get('completion_tokens')} reported completion tokens")
        elif "token_ids" in trial:
            problem(f"trial pass {key[0]} case {key[1]}: ids on a trial whose outcome is {trial.get('outcome')}")
    for step in ORDER:
        for case in range(len(cases)):
            if (step["pass"], case) not in seen:
                problem(f"trial pass {step['pass']} case {case} missing")
    if problems:
        return {"ok": False, "problems": problems, "summary": None}
    derived = derive(receipt)
    for key in ("comparison", "determinism", "speed", "summary"):
        if derived[key] != receipt.get(key):
            problem(f"{key} does not match what the trials give")
    return {"ok": not problems, "problems": problems, "summary": derived["summary"]}


def share(summary):
    claim = str(summary.get("claim") or "")
    deterministic = summary.get("deterministic") if isinstance(summary.get("deterministic"), dict) else {}
    return {"identical": summary.get("identical"), "cases": summary.get("cases"), "compared": summary.get("compared"),
            "claim": ("token-identical (first pass of each arm)" if claim.startswith("token-identical")
                      else "identity claim withheld" if claim.startswith("withheld") else "token ids unavailable"),
            "deterministic_a": deterministic.get("a", "").replace("/", " of "),
            "deterministic_b": deterministic.get("b", "").replace("/", " of "),
            "speed_delta_median": summary.get("speed_delta_median"), "speed_delta_range": summary.get("speed_delta_range"),
            "max_tokens": summary.get("max_tokens"), "prompt_set": summary.get("prompt_set")}
