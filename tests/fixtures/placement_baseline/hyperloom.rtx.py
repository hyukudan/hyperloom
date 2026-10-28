# Frozen function sources from main 8539c28; executed with the current module globals.

def fidelity_gate(cluster, profile_name, prompts, request=_request, apps=compute_apps, memory=gpu_memory,
                  service=None, key=None):
    """The preset's named oracle against a service (the profile's running one by default): state sha256, next token,
    logits FNV and state size at every oracle length (no handoff), plus the GPUs the process is visible on."""
    _, data, _ = _service(cluster, profile_name)
    if service is None:
        _, service = _running(profile_name)
        key = _keys(profile_name, "prefill")
    rows, ok = [], True
    for entry in data["fidelity"]["oracle"]:
        body = {"tokens": prompt_ids(prompts, entry), "session": f"gate-{entry['tokens']}"}
        status, _, reply = request("127.0.0.1", service["port"], "POST", "/v1/prefill", body, key)
        reply = reply if isinstance(reply, dict) else {}
        got = {k: reply.get(k) for k in ("sha256", "next_token", "logits_fnv", "state_bytes")}
        want = {"sha256": entry["state_sha256"], "next_token": entry["next_token"], "logits_fnv": entry["logits_fnv"],
                "state_bytes": entry["state_bytes"]}
        equal = status == 200 and got == want and not any((reply.get("fast") or {}).values())
        ok &= equal
        rows.append({"tokens": entry["tokens"], "status": status, "equal": equal, "got": got,
                     "seconds": reply.get("seconds")})
    seen = visibility(service, apps, memory) if service.get("pid") else {"only_pro": None, "why": "attached: no pid"}
    return {"gate": data["fidelity"]["name"], "pass": ok and seen["only_pro"] is not False, "rows": rows,
            "visibility": seen, "quality_gate": data["fidelity"]["quality_gate"],
            "quality_gate_why": data["fidelity"]["quality_gate_why"]}
