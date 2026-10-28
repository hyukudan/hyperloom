# Frozen function sources from main 8539c28; executed with the current module globals.

def probe_report(recipe, bound, facts):
    """The core's probe report (§8.2) with exactly the bound devices, so the core's binding can only pick them.
    ``facts``: {"model", "model_sha256", "devices": {probe id: {kind, name, memory_gib, host_ram_gib,
    disk_read_gbytes_ps, driver}}, "links": [{kind, hosts, gbps}]} (capability values only)."""
    devices = []
    for device in recipe["match"]["devices"]:
        for member in bound["binding"][device["id"]]:
            item = facts["devices"].get(member["id"], {})
            row = {"id": member["id"], "kind": device["kind"], "name": item.get("name"),
                   "memory_gib": item.get("memory_gib"), "host": member["host"]}
            for key in ("host_ram_gib", "disk_read_gbytes_ps", "driver"):
                if item.get(key) is not None:
                    row[key] = item[key]
            devices.append(row)
    report = {"model": facts["model"], "devices": devices, "links": deepcopy(facts.get("links", []))}
    if facts.get("model_sha256"):
        report["model_sha256"] = facts["model_sha256"]
    return report
