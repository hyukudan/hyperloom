"""Scripted replies for the fake server: a quality responder that answers the suite (except chosen items), a tool
responder that makes the expected calls, and a demo responder with reasoning. Test data only."""

import json

from hyperloom.bench import quality, tools


INSTRUCTION_REPLIES = {
    "inst-01": "RAIN FALLS SOFTLY ON THE OLD TIN ROOF.", "inst-02": "- apple\n- pear\n- plum",
    "inst-03": "Silver lantern of the night", "inst-04": '{"name": "Ada", "age": 36}',
    "inst-05": "The night train left the station on time DONE", "inst-06": "A cat sat on a warm rug all day",
    "inst-07": "Certainly, blue is a fine colour.", "inst-08": "1. cat\n2. dog\n3. owl\n4. fox",
    "inst-09": "the forest is quiet and green.", "inst-10": "Winter brings snow. The lake freezes over.",
    "inst-11": '"carrot"', "inst-12": "Deep, blue and always moving.", "inst-13": "* Walk daily\n* Stretch often",
    "inst-14": "[12, 40, 99]", "inst-15": "blue blue blue blue",
}


def quality_responder(miss=(), context_limit=None):
    """Answers every item of the suite correctly except the ids in ``miss`` (which get a wrong reply)."""
    by_prompt = {}
    for item in quality.suite(context_limit or 200000):
        if item["scorer"]["kind"] == "answer":
            reply = item["scorer"]["answers"][0]
        else:
            reply = INSTRUCTION_REPLIES[item["id"]]
        by_prompt[item["messages"][-1]["content"]] = (item["id"], reply)

    def respond(body):
        found = by_prompt.get(body["messages"][-1]["content"])
        if found is None:
            return None
        item_id, reply = found
        return {"text": "I am not sure." if item_id in miss else reply}
    return respond


CALLS = {
    "single-weather": [("get_weather", {"city": "Lisbon", "unit": "celsius"})],
    "single-order": [("lookup_order", {"order_id": "A-1029"})],
    "single-notes": [("search_notes", {"query": "budget"})],
    "types-thermostat": [("set_thermostat", {"temperature": 21.5, "mode": "heat"})],
    "types-event": [("create_event", {"title": "Team offsite", "date": "2026-11-12", "attendees": ["Ana", "Luis"], "all_day": True})],
    "types-cart": [("add_to_cart", {"item": {"sku": "SKU-778", "quantity": 3}})],
    "types-currency": [("convert_currency", {"amount": 250.75, "from_currency": "EUR", "to_currency": "USD"})],
    "parallel-weather": [("get_weather", {"city": "Paris"}), ("get_weather", {"city": "Rome"})],
    "parallel-mixed": [("get_weather", {"city": "Oslo"}), ("lookup_order", {"order_id": "B-5512"})],
    "parallel-three": [("lookup_order", {"order_id": "C-1"}), ("lookup_order", {"order_id": "C-2"}), ("lookup_order", {"order_id": "C-3"})],
    "follow-weather": [("get_weather", {"city": "Madrid", "unit": "celsius"})],
    "follow-currency": [("convert_currency", {"amount": 100, "from_currency": "GBP", "to_currency": "EUR"})],
    "follow-order": [("lookup_order", {"order_id": "D-404"})],
}


def tools_responder(wrong=()):
    """Makes the expected calls (declining unsafe ones; using tool results); scenarios in ``wrong`` get a bad call."""
    by_user = {s["user"]: s for s in tools.catalogue()["scenarios"]}

    def respond(body):
        messages = body["messages"]
        scenario = by_user.get(next((m["content"] for m in messages if m["role"] == "user"), ""))
        if scenario is None:
            return None
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            value = next((str(v) for k, v in result.items() if k in ("temperature_c", "amount", "arrival")), "")
            return {"text": f"The answer is {value}."}
        if scenario["id"].startswith("decline"):
            return {"text": "I will not do that: it could cause harm."}
        calls = CALLS[scenario["id"]]
        if scenario["id"] in wrong:
            calls = [(calls[0][0], {"unexpected": True})]
        return {"text": "", "tool_calls": [{"id": f"call_{i}", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(arguments)}}
                                           for i, (name, arguments) in enumerate(calls)]}
    return respond


def demo_responder():
    def respond(body):
        prompt = body["messages"][-1]["content"]
        words = prompt.split()
        text = " ".join(f"{w.lower().strip('.,')}" for w in words * 3)[:600]
        return {"text": text, "reasoning": "Plan: answer in a calm voice, keep it short." if len(prompt) % 2 else ""}
    return respond
