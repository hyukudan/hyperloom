"""Quality suite with paired comparison (F27).

Suite ``hyperloom-quality-v1``: fixed items from ``data/quality.json`` (short QA with exact answers, instruction
following with checkable formats) plus items generated from the suite seed (arithmetic chains, state tracking, and
long-context recall of a planted fact at about 8k / 32k / 128k tokens where the context allows). Every scorer is
deterministic. Each item result keeps a 200-character excerpt, ``reply_sha256`` (the complete content text as
received, reasoning excluded), ``item_digest`` (prompt, expected answer, scorer and its parameters) and
``request_digest`` (the canonical request body without ``model``).

Comparison pairs the items of two runs with equal ``item_id``, ``item_digest`` and ``request_digest`` that both runs
scored; it reports the paired n, exclusions by reason, both scores on the paired population, identical replies, the
discordant counts b (passed only in this run) and c (passed only in the other), the exact two-sided McNemar p-value
p = min(1, 2 * sum_{k<=min(b,c)} C(b+c, k) / 2^(b+c)) (1 when b + c = 0), the paired difference d = (b - c) / n and
an approximate 95 % interval (one pseudo-count added to each discordant cell).
"""

import hashlib
import json
import math
import random
import re

from hyperloom.bench import data, runs
from hyperloom.web.api import redact


SUITE = "hyperloom-quality-v1"
SEED = 20261025
CATEGORIES = ("short QA", "arithmetic chains", "state tracking", "instruction following", "long-context recall")
OWN = ("categories", "max_tokens", "concurrency")
ANSWER_ONLY = "Answer the question. Reply with only the answer, without explanation."
RECALL_SIZES = (8192, 32768, 131072)
CHARS_PER_NOMINAL_TOKEN = 3
WORDS = ("river", "copper", "lantern", "meadow", "orbit", "pepper", "quartz", "saddle", "tundra", "velvet", "walnut",
         "harbor", "glacier", "falcon", "ember", "dune", "cobalt", "birch", "anchor", "marble", "nimbus", "prairie")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _item(item_id, category, messages, max_tokens, scorer):
    item = {"id": item_id, "category": category, "messages": messages, "max_tokens": max_tokens, "scorer": scorer}
    item["item_digest"] = _digest({"messages": messages, "scorer": scorer})
    return item


def _arithmetic(rng, index):
    value = rng.randint(2, 20)
    steps = [f"Start with {value}."]
    for _ in range(rng.randint(3, 5)):
        op = rng.choice(("add", "subtract", "multiply"))
        if op == "add":
            n = rng.randint(2, 30)
            value += n
            steps.append(f"Add {n}.")
        elif op == "subtract" and value > 5:
            n = rng.randint(1, min(20, value - 1))
            value -= n
            steps.append(f"Subtract {n}.")
        else:
            n = rng.randint(2, 5)
            value *= n
            steps.append(f"Multiply by {n}.")
    prompt = " ".join(steps) + " What number do you get? Reply with only the number."
    return _item(f"arith-{index:02d}", "arithmetic chains", [{"role": "user", "content": prompt}], 32,
                 {"kind": "answer", "answers": [str(value)]})


def _state(rng, index):
    cups = ["left", "middle", "right"]
    ball = rng.choice(cups)
    lines = [f"Three cups stand in a row at the places left, middle and right. A ball is under the cup at the {ball} place."]
    for _ in range(rng.randint(3, 6)):
        a, b = rng.sample(cups, 2)
        lines.append(f"Swap the cups at {a} and {b}.")
        if ball == a:
            ball = b
        elif ball == b:
            ball = a
    lines.append("Each swap exchanges the places of two cups, and the ball moves with its cup; the places keep their "
                 "names left, middle and right. At which place is the ball now? Reply with only left, middle or right.")
    return _item(f"state-{index:02d}", "state tracking", [{"role": "user", "content": " ".join(lines)}], 32,
                 {"kind": "answer", "answers": [ball]})


def _recall(rng, size, index):
    project, code = rng.choice(WORDS), str(rng.randint(1000, 9999))
    chars = size * CHARS_PER_NOMINAL_TOKEN
    sentences, total = [], 0
    filler = random.Random(f"{SEED}-{size}-{index}")
    while total < chars:
        sentence = " ".join(filler.choice(WORDS) for _ in range(filler.randint(6, 12))).capitalize() + ". "
        sentences.append(sentence)
        total += len(sentence)
    depth = (0.3, 0.7)[index % 2]
    sentences.insert(int(len(sentences) * depth), f"The vault code for project {project} is {code}. ")
    prompt = ("Read the notes, then answer the question after them.\n\n" + "".join(sentences)
              + f"\n\nWhat is the vault code for project {project}? Reply with only the number.")
    return _item(f"recall-{size // 1024}k-{index + 1}", "long-context recall", [{"role": "user", "content": prompt}], 32,
                 {"kind": "answer", "answers": [code]})


def suite(context_limit=None, categories=CATEGORIES):
    """The items of the suite for a context limit (recall sizes that do not fit are left out; unknown limit: 8k)."""
    fixed = data("quality.json")
    rng = random.Random(SEED)
    items = []
    if "short QA" in categories:
        for qa in fixed["qa"]:
            items.append(_item(qa["id"], "short QA", [{"role": "system", "content": ANSWER_ONLY},
                                                       {"role": "user", "content": qa["question"]}], 32,
                               {"kind": "answer", "answers": qa["answers"]}))
    arith = [_arithmetic(rng, i + 1) for i in range(15)]
    state = [_state(rng, i + 1) for i in range(15)]
    if "arithmetic chains" in categories:
        items.extend(arith)
    if "state tracking" in categories:
        items.extend(state)
    if "instruction following" in categories:
        for inst in fixed["instructions"]:
            scorer = {k: v for k, v in inst.items() if k not in ("id", "prompt")}
            scorer["kind"] = scorer.pop("check")
            items.append(_item(inst["id"], "instruction following", [{"role": "user", "content": inst["prompt"]}], 128, scorer))
    if "long-context recall" in categories:
        for size in RECALL_SIZES:
            if (context_limit is None and size != RECALL_SIZES[0]) or (context_limit is not None and size + 256 > context_limit):
                continue
            recall_rng = random.Random(f"{SEED}-recall-{size}")
            items.extend(_recall(recall_rng, size, i) for i in range(2))
    return items


def suite_digest(items):
    return _digest([[item["id"], item["item_digest"]] for item in items])


# -- scoring ------------------------------------------------------------------------------------------------------
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def content(text):
    return THINK.sub("", text or "").strip()


def normalise(text):
    text = text.strip().strip("*_`\"' ").strip()
    text = re.sub(r"\s+", " ", text).lower()
    return text.rstrip(".!").strip()


def _number(text):
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def candidates(text):
    lines = [line for line in text.splitlines() if line.strip()]
    found = [text] + lines[:1] + lines[-1:]
    match = re.search(r"answer\s*(?:is|:)\s*(.+)", text, re.I)
    if match:
        found.append(match[1].splitlines()[0])
    return [normalise(c) for c in found]


def _answer(text, answers):
    wanted = {normalise(a) for a in answers}
    numbers = {_number(a) for a in wanted} - {None}
    for candidate in candidates(text):
        if candidate in wanted:
            return True
        if numbers and _number(candidate) in numbers:
            return True
    return False


def _strip_fence(text):
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text.strip(), re.S)
    return match[1] if match else text.strip()


def score(scorer, reply):
    """True / False for a reply's content text under a scorer."""
    text = content(reply)
    kind = scorer["kind"]
    words = re.findall(r"[A-Za-z0-9']+", text)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if kind == "answer":
        return _answer(text, scorer["answers"])
    if kind == "uppercase":
        letters = [c for c in text if c.isalpha()]
        return bool(letters) and all(c.isupper() for c in letters)
    if kind == "lowercase":
        letters = [c for c in text if c.isalpha()]
        return bool(letters) and all(c.islower() for c in letters)
    if kind == "prefixed_lines":
        return len(lines) == scorer["count"] and all(line.startswith(scorer["prefix"]) and line[len(scorer["prefix"]):].strip()
                                                    for line in lines)
    if kind == "word_count":
        return len(words) == scorer["count"]
    if kind == "json_keys":
        try:
            value = json.loads(_strip_fence(text))
        except ValueError:
            return False
        return isinstance(value, dict) and sorted(value) == sorted(scorer["keys"])
    if kind == "json_int_array":
        try:
            value = json.loads(_strip_fence(text))
        except ValueError:
            return False
        return (isinstance(value, list) and len(value) == scorer["count"]
                and all(type(v) is int and v > scorer["greater_than"] for v in value))
    if kind == "ends_with":
        return bool(words) and words[-1] == scorer["word"]
    if kind == "starts_with":
        return bool(words) and words[0] == scorer["word"]
    if kind == "no_letter":
        return scorer["letter"].lower() not in text.lower() and len(words) >= scorer["min_words"]
    if kind == "no_char":
        sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
        return scorer["char"] not in text and len(sentences) >= scorer["min_sentences"]
    if kind == "numbered":
        return (len(lines) == scorer["count"]
                and all(re.fullmatch(rf"{i + 1}\.\s+\S.*", line.strip()) for i, line in enumerate(lines)))
    if kind == "quoted_word":
        return bool(re.fullmatch(r'"[A-Za-z][A-Za-z-]*"', text.strip()))
    if kind == "max_chars":
        return scorer["min"] <= len(text.strip()) <= scorer["max"]
    if kind == "exact":
        return text.strip() == scorer["text"]
    raise ValueError("unknown scorer " + kind)


# -- runs ---------------------------------------------------------------------------------------------------------
def validate(api, arguments):
    args, row = runs.validate_common(api, arguments, OWN)
    categories = arguments.get("categories", list(CATEGORIES))
    if not isinstance(categories, list) or not categories or any(c not in CATEGORIES for c in categories):
        raise ValueError("categories: a non-empty list of " + ", ".join(CATEGORIES))
    max_tokens = arguments.get("max_tokens")
    if max_tokens is not None:
        max_tokens = runs._int(arguments, "max_tokens", 16, 4096)
    args.update(categories=[c for c in CATEGORIES if c in categories], max_tokens=max_tokens,
                concurrency=runs._int(arguments, "concurrency", 1, 8, 4))
    items = suite(row.get("context_tokens") or 131072, args["categories"])
    planned = sum(runs.request_bound({"model": "m" * 200, "messages": i["messages"],
                                      "max_tokens": max_tokens or i["max_tokens"]}) for i in items)
    return runs.check_plan(args, planned)


def request_for(run, item):
    return runs.body(run, item["messages"], run.arguments.get("max_tokens") or item["max_tokens"])


def request_digest(request):
    return _digest({k: v for k, v in request.items() if k != "model"})


def run(run):
    args = run.arguments
    items = suite(run.context_tokens, args["categories"])
    run.summary.update(suite=SUITE, suite_digest=suite_digest(items), items=len(items), categories={},
                       scorers="deterministic", context_limit=run.context_tokens,
                       recall_note=None if run.context_tokens else "context limit not reported: recall runs at 8k only")
    results = [None] * len(items)
    done = {"n": 0}

    def work(index):
        item = items[index]
        request = request_for(run, item)
        reply = run.chat(request, 300)
        text = content(reply.text)
        result = {"id": item["id"], "category": item["category"], "item_digest": item["item_digest"],
                  "request_digest": request_digest(request), "reply_sha256": hashlib.sha256(reply.text.encode("utf-8")).hexdigest(),
                  "excerpt": redact(text[:200]), "finish_reason": reply.finish_reason}
        if reply.ok:
            result.update(status="scored", passed=score(item["scorer"], reply.text))
        else:
            result.update(status="error", passed=None, error=redact((reply.error or f"HTTP {reply.status}")[:200]))
            result.pop("reply_sha256")
        results[index] = result
        done["n"] += 1
        run.progress(f"{done['n']}/{len(items)} items")
        run.detail["items"] = [r for r in results if r]
        run.summary["categories"] = tally(run.detail["items"])
        run.live()
        return result
    runs.parallel(work, list(range(len(items))), args["concurrency"])
    run.detail["items"] = [r for r in results if r]
    run.summary["categories"] = tally(run.detail["items"])
    scored = [r for r in run.detail["items"] if r["status"] == "scored"]
    run.summary.update(scored=len(scored), passed=sum(1 for r in scored if r["passed"]),
                       errors=sum(1 for r in run.detail["items"] if r["status"] == "error"),
                       score=round(sum(1 for r in scored if r["passed"]) / len(scored), 4) if scored else None)


def tally(results):
    table = {}
    for result in results:
        entry = table.setdefault(result["category"], {"passed": 0, "scored": 0, "errors": 0})
        if result["status"] == "scored":
            entry["scored"] += 1
            entry["passed"] += bool(result["passed"])
        else:
            entry["errors"] += 1
    for entry in table.values():
        entry["score"] = round(entry["passed"] / entry["scored"], 4) if entry["scored"] else None
    return table


# -- comparison ---------------------------------------------------------------------------------------------------
def mcnemar_p(b, c):
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def paired_interval(b, c, n):
    if n <= 0:
        return None
    p12, p21 = (b + 1) / (n + 2), (c + 1) / (n + 2)
    d = p12 - p21
    se = math.sqrt(max(0.0, (p12 + p21 - d * d) / (n + 2)))
    return [max(-1.0, round(d - 1.96 * se, 4)), min(1.0, round(d + 1.96 * se, 4))]


def compare(this, other):
    """Paired comparison of two quality runs' item results (this run minus the other)."""
    mine = {r["id"]: r for r in this}
    theirs = {r["id"]: r for r in other}
    exclusions = {}

    def exclude(reason):
        exclusions[reason] = exclusions.get(reason, 0) + 1
    pairs = []
    for item_id in sorted(set(mine) | set(theirs)):
        a, b = mine.get(item_id), theirs.get(item_id)
        if a is None or b is None:
            exclude("item in one run only")
        elif a["item_digest"] != b["item_digest"]:
            exclude("item differs (prompt, answer or scorer)")
        elif a["request_digest"] != b["request_digest"]:
            exclude("request differs (generation settings)")
        elif a["status"] != "scored" or b["status"] != "scored":
            exclude("not scored in both runs")
        else:
            pairs.append((a, b))
    n = len(pairs)
    only_this = sum(1 for a, b in pairs if a["passed"] and not b["passed"])
    only_other = sum(1 for a, b in pairs if b["passed"] and not a["passed"])
    p = mcnemar_p(only_this, only_other)
    result = {"paired": n, "exclusions": exclusions,
              "this_score": round(sum(a["passed"] for a, _ in pairs) / n, 4) if n else None,
              "other_score": round(sum(b["passed"] for _, b in pairs) / n, 4) if n else None,
              "identical_replies": sum(1 for a, b in pairs if a.get("reply_sha256") == b.get("reply_sha256")),
              "only_this": only_this, "only_other": only_other, "both": sum(1 for a, b in pairs if a["passed"] and b["passed"]),
              "neither": sum(1 for a, b in pairs if not a["passed"] and not b["passed"]),
              "mcnemar_p": round(p, 6), "difference": round((only_this - only_other) / n, 4) if n else None,
              "interval_95": paired_interval(only_this, only_other, n),
              "interval_basis": "approximate (one pseudo-count added to each discordant cell)"}
    if n == 0:
        result["verdict"] = "no paired items: nothing to compare"
    elif p >= 0.05:
        result["verdict"] = "no statistically significant difference detected"
    else:
        result["verdict"] = f"significant difference (p = {p:.4g})"
    return result


def share(summary):
    return {"suite": summary.get("suite"), "items": summary.get("items"), "scored": summary.get("scored"),
            "passed": summary.get("passed"), "score": summary.get("score"),
            "categories": {k: {"passed": v.get("passed"), "scored": v.get("scored"), "score": v.get("score")}
                           for k, v in (summary.get("categories") or {}).items() if k in CATEGORIES}}
