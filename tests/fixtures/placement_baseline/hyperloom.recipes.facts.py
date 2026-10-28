# Frozen function sources from main 8539c28; executed with the current module globals.

def gather(cluster, ctx, *, call, gpus=None, memory=None, pro_busy=None, cache=None, measure_disks=False,
           measurer=None, now=None):
    """(inventory, facts, availability, notes). ``call(machine_id, verb)`` runs a node verb and returns its result or
    raises; any failure is an unavailable fact with its reason, never an invented value."""
    now = time.time() if now is None else now
    gpus, memory = gpus or probe.nvidia_gpus, memory or probe.host_memory
    pro_busy, measurer = pro_busy or rtx.pro_busy, measurer or probe.read_bandwidth
    notes = []
    try:
        inventory = {"cuda": gpus()}
    except probe.ProbeError as exc:
        inventory = {"cuda": []}
        notes.append(f"nvidia-smi: {exc}")
    pc_mem = memory()
    devices, avail_devices, links, avail_links = {}, {}, [], {}
    hosts = {ctx.pc: {"free_ram_gib": pc_mem["free_gib"], "free_commit_gib": pc_mem["free_commit_gib"]}}
    pc_assets = cluster.assets.get(ctx.pc, {})
    for gpu in inventory["cuda"]:
        row = {"name": gpu["name"], "memory_gib": gpu["memory_gib"], "host_ram_gib": pc_mem["total_gib"]}
        if ctx.service is not None and gpu["uuid"] == ctx.service["uuid"]:
            asset = pc_assets.get(ctx.service.get("model_asset") or "")
            if asset is not None and cache is not None:
                path = asset.get("control_path", asset["path"])
                disk = probe.disk_fact(cache, ctx.pc, path, probe.file_stat(path), measure_disks,
                                       lambda: measurer(path))
                if disk["value"] is not None:
                    row["disk_read_gbytes_ps"] = disk["value"]
                else:
                    notes.append(f"{ctx.pc} disk: {disk['why']}")
            busy = None
            try:
                busy = pro_busy(gpu["uuid"])
            except rtx.ServiceError as exc:
                busy = str(exc)
            avail_devices[gpu["uuid"]] = ({"available": True} if busy is None else
                                          {"available": False, "why": "RTX PRO busy: " + busy})
            reason = _reservation_problem(cluster, ctx, now)
            if reason is not None and avail_devices[gpu["uuid"]]["available"]:
                avail_devices[gpu["uuid"]] = {"available": False, "why": reason}
        else:
            avail_devices[gpu["uuid"]] = {"available": False, "why": "not the profile's prefill device"}
        devices[gpu["uuid"]] = row
    lan, usb4 = {}, {}
    for mid in ctx.nodes:
        try:
            gpu_result = call(mid, "gpu")
            doctor_result = call(mid, "doctor")
        except Exception as exc:  # any remote failure is the node being unavailable
            devices[mid] = {"name": None, "memory_gib": None}
            avail_devices[mid] = {"available": False, "why": f"node unreachable: {exc}"}
            hosts[mid] = {"free_ram_gib": None}
            lan[mid] = {"up": False, "why": f"node unreachable: {exc}"}
            continue
        node = probe.node_device(gpu_result)
        devices[mid] = {"name": node["name"]["value"], "memory_gib": _gtt(gpu_result),
                        "host_ram_gib": node["host_ram_gib"]["value"]}
        model = cluster.assets.get(mid, {}).get(FAMILY_MODELS_BY_ID.get(ctx.model, ""), {}).get("path")
        evidence = cache.evidence(mid, model, now) if cache is not None and model else None
        if evidence is not None:
            devices[mid]["disk_read_gbytes_ps"] = evidence["disk_read_gbytes_ps"]
        avail_devices[mid] = {"available": True}
        hosts[mid] = {"free_ram_gib": node["host_free_ram_gib"]["value"]}
        lan_fact, usb4_fact = probe.node_links(doctor_result)
        lan[mid] = {"up": lan_fact["up"], "why": lan_fact.get("why"), "gbps": lan_fact["value"]}
        usb4[mid] = usb4_fact
    for mid in ctx.nodes:
        gbps = lan.get(mid, {}).get("gbps")
        if gbps is not None:   # a link whose speed is unknown is not reported (the core then refuses it), never guessed
            links.append({"kind": "ethernet", "hosts": [ctx.pc, mid], "gbps": gbps})
    if len(ctx.nodes) == 2:
        a, b = ctx.nodes
        # USB4 bandwidth is not probed here: 0 Gb/s satisfies only a gbps_min of 0 (design §3).
        links.append({"kind": "usb4", "hosts": [a, b], "gbps": 0.0})
        notes.append("USB4 bandwidth not probed: reported as 0 Gb/s (only gbps_min = 0 passes)")
    facts = {"model": ctx.model, "devices": devices, "links": links,
             "node_runtime": node_runtime(cluster, ctx, cache)}
    availability = {"devices": avail_devices, "hosts": hosts, "lan": lan, "usb4": usb4}
    return inventory, facts, availability, notes
