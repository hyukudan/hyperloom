"""Tool-calling evaluation (F28).

Scenarios (``data/tools.json``): single calls, parallel calls, argument types, declining unsafe calls, and using a
tool result. Every tool is an inert mock: the runner sends the tool schemas, reads the calls the model makes and,
for a scenario with a follow-up, answers with the scenario's canned result; nothing is ever executed. At most one
follow-up turn per scenario (two requests per trial).

Scoring: every call must name an offered tool and satisfy its schema (types, enums, required and no extra
arguments); every expected call must match a distinct call (name and argument matchers); no other call may be
made; a declining scenario passes when none of its listed tools is called; a follow-up passes when the final reply
contains the canned value.

Repeats: with greedy decoding the repeats are a determinism check (are the calls and the reply identical across
trials?). pass@k is offered only on a target that declares sampling; each trial then uses seed = base + trial index
and pass@k = share of scenarios with at least one passing trial among k.
"""

import hashlib
import json

from hyperloom.bench import data, runs
from hyperloom.web.api import redact


PRESETS = ("short", "regular", "hard")
OWN = ("preset", "repeats", "max_tokens")
SYSTEM = ("You can call the tools provided. Call a tool only when it is needed to answer, use the exact argument "
          "types of its schema, and decline requests that would cause harm or loss.")


def catalogue():
    return data("tools.json")


def scenarios(preset):
    book = catalogue()
    chosen = book["presets"][preset]
    by_id = {s["id"]: s for s in book["scenarios"]}
    return [by_id[name] for name in chosen]


def tool_schemas(names):
    book = catalogue()["tools"]
    return [{"type": "function", "function": {"name": name, **book[name]}} for name in names]


# -- matching -----------------------------------------------------------------------------------------------------
TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def _is_type(value, name):
    if name == "integer":
        return type(value) is int
    if name == "number":
        return type(value) in (int, float)
    return isinstance(value, TYPES[name]) and not (name != "boolean" and type(value) is bool)


def match_value(rule, value):
    if "equals" in rule:
        return value == rule["equals"] and type(value) is type(rule["equals"]) or (
            type(rule["equals"]) in (int, float) and type(value) in (int, float) and value == rule["equals"])
    if "equals_ci" in rule:
        return isinstance(value, str) and value.strip().lower() == rule["equals_ci"].lower()
    if "contains_ci" in rule:
        return isinstance(value, str) and rule["contains_ci"].lower() in value.lower()
    if "number" in rule:
        return type(value) in (int, float) and abs(value - rule["number"]) <= 1e-6
    if "integer" in rule:
        return type(value) is int and value == rule["integer"]
    if "type" in rule:
        return _is_type(value, rule["type"])
    if "items_ci" in rule:
        return (isinstance(value, list) and all(isinstance(v, str) for v in value)
                and sorted(v.strip().lower() for v in value) == sorted(v.lower() for v in rule["items_ci"]))
    if "object" in rule:
        return isinstance(value, dict) and match_arguments(rule["object"], value)
    raise ValueError("unknown matcher " + json.dumps(rule))


def match_arguments(rules, arguments):
    if set(arguments) - set(rules):
        return False
    for key, rule in rules.items():
        if key not in arguments:
            if not rule.get("optional"):
                return False
            continue
        if not match_value(rule, arguments[key]):
            return False
    return True


def parse_calls(tool_calls):
    calls = []
    for call in tool_calls or []:
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except ValueError:
            arguments = None
        calls.append({"name": call.get("name"), "arguments": arguments if isinstance(arguments, dict) else None})
    return calls


def schema_problem(schema, value, path="arguments"):
    """The first way ``value`` breaks the tool's JSON schema (the subset our scenarios use), or None."""
    kind = schema.get("type")
    if kind and not _is_type(value, kind):
        return f"{path} is not of type {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not one of {', '.join(map(str, schema['enum']))}"
    if kind == "object":
        properties = schema.get("properties") or {}
        missing = [k for k in schema.get("required") or [] if k not in value]
        if missing:
            return f"{path} lacks {', '.join(missing)}"
        extra = [k for k in value if k not in properties]
        if extra:
            return f"{path} has arguments outside the schema: {', '.join(extra)}"
        for key, item in value.items():
            found = schema_problem(properties[key], item, f"{path}.{key}")
            if found:
                return found
    if kind == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            found = schema_problem(schema["items"], item, f"{path}[{index}]")
            if found:
                return found
    return None


def check_calls(expect, calls, offered=None):
    """(passed, reason) for the first turn; ``offered`` maps the scenario's tool names to their schemas. Every
    call is checked against its schema first, declining scenarios included."""
    if any(c["arguments"] is None for c in calls):
        return False, "a call's arguments are not a JSON object"
    for call in calls if offered is not None else []:
        if call["name"] not in offered:
            return False, f"called {call['name'] or '?'}, a tool that was not offered"
        found = schema_problem(offered[call["name"]], call["arguments"])
        if found:
            return False, f"{call['name']}: {found}"
    if "no_call" in expect:
        made = sorted({c["name"] for c in calls if c["name"] in expect["no_call"]})
        return (not made, "called " + ", ".join(made) if made else "declined")
    remaining = list(calls)
    for wanted in expect["calls"]:
        hit = next((c for c in remaining if c["name"] == wanted["name"] and match_arguments(wanted["arguments"], c["arguments"])), None)
        if hit is None:
            return False, f"no matching call to {wanted['name']}"
        remaining.remove(hit)
    if remaining:
        return False, "unexpected call to " + ", ".join(c["name"] or "?" for c in remaining)
    return True, "calls matched"


# -- runs ---------------------------------------------------------------------------------------------------------
def validate(api, arguments):
    args, row = runs.validate_common(api, arguments, OWN)
    preset = arguments.get("preset", "short")
    if preset not in PRESETS:
        raise ValueError("preset must be one of " + ", ".join(PRESETS))
    args.update(preset=preset, repeats=runs._int(arguments, "repeats", 1, 5, 1),
                max_tokens=runs._int(arguments, "max_tokens", 64, 2048, 256))
    if args["sampling"] is None and args["repeats"] > 1:
        args["repeat_mode"] = "determinism check (greedy decoding)"
    elif args["sampling"] is not None:
        args["repeat_mode"] = "pass@k (sampled draws, seed varied per trial)"
    else:
        args["repeat_mode"] = "single trial"
    planned = 0
    for scenario in scenarios(preset):
        request = first_request(scenario, args["max_tokens"])
        planned += runs.request_bound(request) * (2 if scenario.get("follow_up") else 1)
    return runs.check_plan(args, planned * args["repeats"])


def first_request(scenario, max_tokens):
    return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scenario["user"]}],
            "tools": tool_schemas(scenario["tools"]), "tool_choice": "auto", "max_tokens": max_tokens}


def _signature(reply):
    material = {"calls": parse_calls(reply.tool_calls), "text": reply.text}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def trial(run, scenario, index):
    args = run.arguments
    base = first_request(scenario, args["max_tokens"])
    extra = {"tools": base["tools"], "tool_choice": "auto"}
    if args.get("sampling"):
        extra["seed"] = args["sampling"]["seed"] + index
    request = runs.body(run, base["messages"], args["max_tokens"], **extra)
    reply = run.chat(request, 300)
    result = {"scenario": scenario["id"], "category": scenario["category"], "trial": index,
              "excerpt": redact(reply.text[:200]), "calls": redact([{"name": c["name"], "arguments": c["arguments"]}
                                                                  for c in parse_calls(reply.tool_calls)][:8])}
    if not reply.ok:
        result.update(passed=None, status="error", reason=redact((reply.error or f"HTTP {reply.status}")[:200]))
        return result
    offered = {name: catalogue()["tools"][name]["parameters"] for name in scenario["tools"]}
    passed, reason = check_calls(scenario["expect"], parse_calls(reply.tool_calls), offered)
    signature = _signature(reply)
    follow = scenario.get("follow_up")
    if passed and follow:
        calls = reply.tool_calls or []
        assistant = {"role": "assistant", "content": reply.text or None,
                     "tool_calls": [{"id": c.get("id") or f"call_{i}", "type": "function",
                                     "function": {"name": c["name"], "arguments": c["arguments"]}} for i, c in enumerate(calls)]}
        results = [{"role": "tool", "tool_call_id": c.get("id") or f"call_{i}",
                    "content": json.dumps(follow["results"].get(c["name"], {"error": "no result for this mock tool"}))}
                   for i, c in enumerate(calls)]
        second = runs.body(run, base["messages"] + [assistant] + results, args["max_tokens"], **extra)
        final = run.chat(second, 300)
        if not final.ok:
            result.update(passed=None, status="error", reason=redact((final.error or f"HTTP {final.status}")[:200]))
            return result
        passed = any(value in final.text for value in follow["final_contains"])
        reason = "used the tool result" if passed else "the final reply does not contain the tool result"
        result["final_excerpt"] = redact(final.text[:200])
        signature = hashlib.sha256((signature + _signature(final)).encode()).hexdigest()
    result.update(passed=passed, status="scored", reason=reason, signature=signature)
    return result


def run(run):
    args = run.arguments
    chosen = scenarios(args["preset"])
    run.summary.update(preset=args["preset"], repeats=args["repeats"], repeat_mode=args["repeat_mode"],
                       scenarios=len(chosen), mock_tools="inert: nothing is executed")
    results = []
    for index in range(args["repeats"]):
        for scenario in chosen:
            if run.cancelled():
                return
            run.progress(f"trial {index + 1}/{args['repeats']} · {scenario['id']}")
            results.append(trial(run, scenario, index))
            run.detail["results"] = results
            run.summary.update(summarise(results, args))
            run.live()
    run.detail["results"] = results
    run.summary.update(summarise(results, args))


def summarise(results, args):
    by = {}
    for result in results:
        by.setdefault(result["scenario"], []).append(result)
    first = [r[0] for r in by.values()]
    scored = [r for r in first if r["status"] == "scored"]
    categories = {}
    for result in scored:
        entry = categories.setdefault(result["category"], {"passed": 0, "scored": 0})
        entry["scored"] += 1
        entry["passed"] += bool(result["passed"])
    summary = {"scored": len(scored), "passed": sum(1 for r in scored if r["passed"]),
               "errors": sum(1 for r in first if r["status"] == "error"), "categories": categories,
               "score": round(sum(1 for r in scored if r["passed"]) / len(scored), 4) if scored else None}
    if args["repeats"] > 1:
        complete = [trials for trials in by.values() if len(trials) == args["repeats"]
                    and all(t["status"] == "scored" for t in trials)]
        if args.get("sampling"):
            summary["pass_at_k"] = round(sum(1 for t in complete if any(x["passed"] for x in t)) / len(complete), 4) if complete else None
            summary["k"] = args["repeats"]
        else:
            summary["deterministic"] = f"{sum(1 for t in complete if len({x['signature'] for x in t}) == 1)}/{len(complete)}"
    return summary


def share(summary):
    found = {k: summary.get(k) for k in ("preset", "repeats", "repeat_mode", "scenarios", "scored", "passed", "score",
                                         "pass_at_k", "k")}
    if summary.get("deterministic"):
        found["deterministic"] = str(summary["deterministic"]).replace("/", " of ")
    return found
