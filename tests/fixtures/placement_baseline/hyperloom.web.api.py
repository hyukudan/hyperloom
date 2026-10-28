# Frozen function sources from main 8539c28; executed with the current module globals.

def _accelerator(raw, doctor, gpu, declared):
    amd = "vram_total_bytes" in raw
    vram, gtt = _value(raw.get("vram_total_bytes")), _value(raw.get("gtt_total_bytes"))
    # Large GTT with a small carve-out identifies the UMA sysfs accounting.
    unified = amd and _finite(vram) and _finite(gtt) and 0 < vram <= 1024 ** 3 and gtt > vram
    targets = _value(doctor.get("gfx_targets")) or []
    target = raw.get("gfx_target") or (targets[0].get("target") if len(targets) == 1 else None)
    name = raw.get("name") or _value(raw.get("product_name"))
    if not name and target == "gfx1151":
        name = "Radeon 8060S"
    if amd and target:
        name = f"{name} · {target}" if name else target
    row = {"id": raw.get("id"), "name": raw.get("name"), "source": "hyperloom-node gpu" if amd else "tensormeld probe",
           "memory_used_bytes": _value(raw.get("vram_used_bytes")) if amd else None,
           "memory_total_bytes": _value(raw.get("vram_total_bytes")) if amd else raw.get("reported_memory_bytes"),
           "memory_free_bytes": None if amd else raw.get("reported_free_bytes"),
           "utilization_pct": _value(raw.get("busy_percent")) if amd else raw.get("utilization_pct"),
           "driver": raw.get("driver_version"), "runtime": _rocm(doctor) if amd else raw.get("cuda_version"),
           "kernel": _value(gpu.get("kernel")), "usb4_driver": _value(doctor.get("usb4", {}).get("module")),
           "layout": _value(doctor.get("usb4", {}).get("layout")), "raw": raw, "why": {}}
    row.update(name=name, memory_kind="unified · GPU-visible" if unified else "VRAM")
    if unified:
        used = [_value(raw.get(field)) for field in ("vram_used_bytes", "gtt_used_bytes")]
        row["memory_total_bytes"] = vram + gtt
        row["memory_used_bytes"] = sum(used) if all(_finite(v) for v in used) else None
    for field in ("temperature_c", "power_w"):
        if not amd:
            row[field] = raw.get(field)
            continue
        sensors = _value(raw.get(field))
        readings = [_value(value) for value in sensors.values()] if isinstance(sensors, dict) else []
        row[field] = readings[0] if len(readings) == 1 else None
        if len(readings) > 1:
            row["why"][field] = "multiple hwmon sensors; see raw readings"
    if _finite(row["memory_total_bytes"]) and _finite(row["memory_used_bytes"]):
        row["memory_free_bytes"] = row["memory_total_bytes"] - row["memory_used_bytes"]
    if not amd and _finite(row["memory_total_bytes"]) and _finite(row["memory_free_bytes"]):
        row["memory_used_bytes"] = row["memory_total_bytes"] - row["memory_free_bytes"]
    policy = next((device for device in declared if device.get("id") == raw.get("id") and raw.get("id")), {})
    row["excluded"] = policy.get("excluded", False) or "4090" in (row["name"] or "")
    row["policy_reason"] = policy.get("reason") or ("RTX 4090 is excluded by policy" if row["excluded"] else None)
    return _missing(row, ("id", "name", "memory_used_bytes", "memory_total_bytes", "memory_free_bytes", "utilization_pct",
                          "temperature_c", "power_w", "driver", "runtime", "kernel", "usb4_driver", "layout", "policy_reason"))


def _snapshot(self):
        documents = {name: self._document(name) for name in SCREENS}
        try:
            cluster = self._cluster()
        except (OSError, ValueError, RuntimeError) as exc:
            for name, fields in {"devices": ("machines", "discovered"), "topology": ("links",),
                                 "models": ("models", "proposals"), "profiles": ("profiles",),
                                 "stats": ("engines", "recent_requests"), "logs": ("entries", "failures"),
                                 "settings": ("trusted_machines", "configuration")}.items():
                for field in fields:
                    documents[name][field] = None
                    documents[name]["why"][field] = "configuration unavailable: " + str(exc)
            documents["settings"]["capabilities"] = {**CAPABILITIES, **feature_registry.registry().capabilities}
            return documents
        statuses, status_error, fresh = self._statuses()       # this configuration's launches only, paced
        if status_error is not None:
            statuses = {name: {"error": status_error} for name in cluster.profiles}
            documents["profiles"]["why"]["status"] = status_error
        machines = self._inventory(cluster, statuses)
        if status_error:
            for machine in machines:
                machine["roles"] = machine["leases"] = None
                machine["why"].update(roles=status_error, leases=status_error)
        documents["devices"].update(machines=machines, discovered=None)
        headroom = [device.get("memory_free_bytes") for machine in machines for device in machine["accelerators"] or [] if not device["excluded"]]
        all_memory = bool(headroom) and all(_finite(value) for value in headroom) and all(
            row["accelerators"] is not None and row["health"] != "offline" for row in machines)
        documents["devices"]["summary"] = _missing({
            "online": sum(machine["health"] in ("online", "degraded") for machine in machines),
            "accelerators_in_use": None, "memory_free_bytes": sum(headroom) if all_memory else None,
            "minimum_headroom_bytes": min(headroom) if all_memory else None,
            "links_healthy": None, "links_summary": None, "excluded_reason": "RTX 4090 excluded by policy"},
            ("accelerators_in_use", "memory_free_bytes", "minimum_headroom_bytes", "links_healthy", "links_summary"))
        if all(machine["roles"] is not None and machine["accelerators"] is not None for machine in machines):
            documents["devices"]["summary"]["accelerators_in_use"] = sum(
                not device["excluded"] for machine in machines if any(role.get("role") not in ("portal", "router")
                for role in machine["roles"]) for device in machine["accelerators"])
        cable_states = [cable for machine in machines if machine["usb4"] for cable in machine["usb4"]["cables"]]
        if cable_states:
            healthy = sum(cable["health"] in ("up", "ok") for cable in cable_states)
            documents["devices"]["summary"].update(links_healthy=f"{healthy}/{len(cable_states)} up",
                links_summary="USB4 cable observations · " + ("warnings reported" if healthy < len(cable_states) else "no warnings"))
        documents["devices"]["why"]["discovered"] = "later phase · TM-R13"
        usb_machines = [row for row in machines if row["usb4"]]
        links = []
        for machine in machines:
            for nic in machine["nics"] or []:
                rate = nic.get("negotiated_mbps")
                links.append({"id": machine["id"] + ":" + nic["name"], "from": machine["id"], "to": "lan",
                              "name": machine["id"] + " · " + nic["name"],
                              "kind": "Wi-Fi" if re.search(r"wi.?fi|wireless|^wl", nic["name"], re.I) else "Ethernet",
                              "gbps": rate / 1000 if _finite(rate) and rate > 0 else None,
                              "health": "up", "source": "interface status", "measured_at": self.clock(),
                              "bytes_per_s": None, "rtt_us_p50": None, "rtt_us_p99": None,
                              "why": {"bytes_per_s": "link-probe receipt unavailable", "rtt_us_p50": "link-probe receipt unavailable",
                                      "rtt_us_p99": "link-probe receipt unavailable"}})
        for machine in usb_machines:
            for cable in machine["usb4"]["cables"]:
                peer = next((m["id"] for m in usb_machines if cable["peer"] and cable["peer"] in (m["id"], m["usb4"].get("host"))), None)
                links.append({**cable, "from": machine["id"], "to": peer, "kind": "USB4", "source": "driver status",
                              "name": f"{machine['id']} ↔ {peer or 'peer'} · cable {cable['id']}",
                              "measured_at": self.clock(), "bytes_per_s": None, "rtt_us_p50": None, "rtt_us_p99": None,
                              "why": {"bytes_per_s": "needs R18", "rtt_us_p50": "needs R18", "rtt_us_p99": "needs R18"}})
        documents["topology"].update(machines=machines, links=links, usb4_per_cable=None,
                                    usb4=[{"machine": row["id"], **row["usb4"]} for row in usb_machines])
        documents["topology"]["why"].update(links="link-probe receipt unavailable; later phase 2", usb4_per_cable="needs R18")
        documents["topology"]["layout"] = [{"machine": row["id"], "value": row["usb4"]["summary"]} for row in usb_machines]
        for key in ("accelerators_in_use", "links_healthy", "links_summary"):
            if documents["devices"]["summary"][key] is not None:
                documents["devices"]["summary"]["why"].pop(key, None)
        # Declared asset bytes/hashes are identities, never evidence of presence.
        documents["models"].update(models=[{"machine": mid, "id": name, "manifest": asset,
                                            "bytes": asset.get("bytes"), "sha256": asset.get("sha256"),
                                            "revision": asset.get("source_revision"), "quant": None,
                                            "presence": None, "why": {"presence": "verify-model result unavailable"}}
                                           for mid, assets in cluster.assets.items() for name, asset in assets.items()], proposals=None)
        for asset in documents["models"]["models"]:
            # verify-model can launch a hashing job. Polls use strictly read-only
            # verbs and never claim verification from the manifest alone.
            asset["verification"] = asset["presence"]
            _missing(asset, ("verification", "revision", "quant"), "verification or manifest field unavailable")
        documents["models"]["why"]["proposals"] = "later phase 2.5 · R17 / TM-R15"
        documents["profiles"]["profiles"] = self._profiles(cluster, statuses)
        if fresh or self._stats_cache is None:
            # Engine and router reads follow the launch status: at most once per cycle (none for an engine whose
            # node a GPU turn of another holder holds).
            self._stats_cache = self._statistics(statuses, cluster)
        documents["stats"] = deepcopy(self._stats_cache)
        for profile in documents["profiles"]["profiles"]:
            trellis = list(dict.fromkeys(((item.get("status") or {}).get("memory") or {}).get("trellis_identity")
                                        for item in documents["stats"]["engines"] if item["profile"] == profile["id"]
                                        and ((item.get("status") or {}).get("memory") or {}).get("trellis_identity")))
            if trellis:
                profile["identity"] = {**(profile.get("identity") or {}), "trellis": trellis}
            for machine in machines:
                if machine["id"] in (profile["nodes"] or []) and profile["release"]:
                    machine["release"] = profile["release"]
                    machine["why"].pop("release", None)
                location = profile["router"]
                if profile["state"] in ("running", "starting") and (machine["id"] == location or location == "local" and machine["access"] == "local"):
                    machine["roles"].append({"profile": profile["id"], "role": "router"})
        if status_error:
            for field in ("engines", "routers", "recent_requests"):
                documents["stats"][field] = None
                documents["stats"]["why"][field] = status_error
        documents["logs"] = self._logs(statuses)
        trusted = []
        try:
            lines = Path(self.orchestrate._known_hosts()).read_text(encoding="utf-8").splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) == 3 and not line.startswith("#"):
                    key = self.remote._hostkey(parts[1], parts[2])
                    aliases = parts[0].split(",")
                    mid = next((mid for mid, machine in cluster.machines.items()
                                if (mid if machine.ssh_port == 22 else f"[{mid}]:{machine.ssh_port}") in aliases), parts[0])
                    trusted.append({"machine": mid, "algorithm": key.algorithm,
                                    "fingerprint": key.sha256_fingerprint, "paired_at": None,
                                    "why": {"paired_at": "pin timestamp unavailable"}})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            documents["settings"]["why"]["trusted_machines"] = str(exc)
            trusted = None
        documents["settings"].update(trusted_machines=trusted, configuration=redact(cluster.raw), version=__version__,
                                       config_path=str(self.config_path), capabilities={**CAPABILITIES, **feature_registry.registry().capabilities},
                                       secrets={"storage": "DPAPI" if os.name == "nt" else "0600 files", "values": "never shown"},
                                       features={"loaded": list(feature_registry.registry().loaded),
                                                 "failed": dict(feature_registry.registry().failed)},
                                       lab_rules={"label_prefix": "hl-", "turn_cap_s": 1200, "drain_margin_s": 60,
                                                  "usb4_layout": 1, "excluded": "RTX 4090", "extended_turn": "explicit approver ACK required"})
        return documents


def _logs(self, statuses):
        actions = self._action_log()
        entries = [{**action, "process": "portal", "message": json.dumps(action)} for action in actions]
        failures = [{"when": action["when"], "process": "portal", "event": "failed",
                     "reason": action["result"].get("error"), "bundle": action}
                    for action in actions if isinstance(action.get("result"), dict) and action["result"].get("state") == "failed"]
        reasons = {}
        budget = pollbudget.for_api(self)
        poll = nodepoll.of(self)
        for profile, status in statuses.items():
            processes = list((status.get("nodes") or {}).keys())
            if status.get("router"):
                processes.append("router")
            for process in processes:
                # A node's log (and a router hosted on a node) is one ssh session: it takes a budget grant; a paced
                # read keeps the last entries, a held turn reads nothing.
                host = status.get("router_machine") if process == "router" else process
                why = None
                if host not in (None, "local"):
                    why = (poll.paused(host) if poll is not None
                           else budget.node(host, f"logs-{profile}-{process}", now=self.clock()))
                if why is not None:
                    if why.startswith("paused"):
                        reasons[profile + "/" + process] = why + "; not read"
                    else:
                        entries.extend(self._last_logs.get((profile, process), []))
                    continue
                mark = len(entries)
                try:
                    logs = self.orchestrate.logs(profile, process, 100, config_path=self.config_path,
                                                 **({"read": nodepoll.reader(self)} if poll else {}))
                    for stream in ("stdout", "stderr"):
                        for line in str(logs.get(stream) or "").splitlines():
                            message = redact(line)
                            event = next((event for event in ("POISONED", "READY", "STOPPED", "WARN") if event in message), None)
                            entries.append({"profile": profile, "process": process, "stream": stream,
                                            "message": message, "event": event, "when": None,
                                            "why": {"when": "timestamp unavailable unless present in captured output"}})
                    self._last_logs[(profile, process)] = entries[mark:]
                except nodepoll.NotRead as exc:
                    entries.extend(self._last_logs.get((profile, process), []))
                    if (profile, process) not in self._last_logs:
                        reasons[profile + "/" + process] = str(exc)
                except (OSError, ValueError, RuntimeError) as exc:
                    reasons[profile + "/" + process] = str(exc)
        return self._document("logs", entries=entries[-1000:], failures=failures,
                              why=reasons, failure_scope="portal failures from the persistent audit log")


def _process_logs(self, key):
        """One process's log for the Logs page (refreshed by the page): a node's log is one ssh session, so it takes
        the polling budget's grant of that node like every other read (once per 30 s per process and tail, spaced
        with every other poll of the node); a paced read keeps the last answer, a held GPU turn reads nothing."""
        profile, process, tail = key
        try:
            host = orchestrate.log_machine(profile, process, read=self.orchestrate._read)
        except Exception:  # noqa: BLE001 - no readable launch state: the process names its machine, or nothing
            host = None
        if host is None:
            try:
                host = process if process in self._cluster().machines else None
            except Exception:  # noqa: BLE001
                host = None
        why = None
        poll = nodepoll.of(self) if host != "local" else None
        if host != "local":
            why = ((poll.paused(host) if poll is not None
                    else pollbudget.for_api(self).node(host, f"page-logs-{profile}-{process}-{tail}", now=self.clock()))
                   if host else "the machine of this process is not known: not read")
        if why is None:
            try:
                entries = self.orchestrate.logs(*key, config_path=self.config_path,
                                                **({"read": nodepoll.reader(self)} if poll is not None else {}))
            except nodepoll.NotRead as exc:
                why = str(exc)
            else:
                document = self._document("logs", entries=redact(entries), failures=None,
                                          why={"failures": "persistent process failure index unavailable"})
                self._last_process_logs[key] = document
                return document
        last = self._last_process_logs.get(key)
        if last is not None and not why.startswith("paused"):
            return last
        return self._document("logs", entries=[], failures=None,
                              why={"entries": why + ("; not read" if why.startswith("paused") else ""),
                                   "failures": "persistent process failure index unavailable"})


def mutate(self, action, arguments):
        if action not in MUTATIONS and action in feature_registry.registry().jobs:
            return self._feature_job(action, arguments)
        target = None       # a cancel's job, read once: whether the cancel is a stop never changes afterwards
        if action == "cancel" and isinstance(arguments, dict) and isinstance(arguments.get("job"), str):
            with self._job_lock:
                listed = self._jobs.get(arguments["job"])
                target = None if listed is None else (dict(listed), arguments["job"] in self._features_running or
                                                      listed["action"] in feature_registry.registry().jobs)
        try:
            arguments = self._arguments(action, arguments)
        except APIError as exc:
            self._audit(action, arguments, {"state": "refused", "error": str(exc)})
            raise
        priority = action in ("down", "cancel")
        occupying = action in ("up", "install", "recipes/deploy", "recipes/probe")
        # A stop: down, and every cancel that is not a feature job's (a profile's cancel and an up's or a deploy's
        # cancel stop a profile, or may).
        stopping, profile = action == "down", arguments.get("profile")
        if action == "cancel":
            # A feature job's cancel (running, finished or since evicted) only cancels its context: never a stop.
            job, feature = target if target is not None else (None, False)
            if arguments.get("job") and target is None:
                feature = True          # not listed when read: nothing to stop (the cancel itself answers why)
            if job is not None and not profile:
                profile = job["arguments"].get("profile")
            stopping = not feature
        resources = set()
        if occupying or stopping:
            resources = self._resources_of_profile(profile, arguments.get("router") if action == "up" else None) \
                if isinstance(profile, str) and profile else {"*"}
            if action == "up" and arguments.get("recipe_inputs"):
                resources = {"*"}       # a snapshot launches with the placement it was planned on: hold everything
        jid = uuid.uuid4().hex
        with self._job_lock:
            if self._active and not priority:
                self._audit(action, arguments, {"state": "refused", "error": "another mutating job is running"})
                raise APIError("another mutating job is running", 409)
            if not priority:
                self._active = jid      # reserved while the shared admission decides
        unheld = None       # a stop that goes ahead without the shared admission: why
        if occupying or stopping:
            # A stop is never refused: registering it cancels overlapping benchmarks (in every process) and its
            # hold keeps new ones out until it finishes. A deploy is refused while a benchmark holds its resources.
            try:
                self._hold(jid, "stop" if stopping else "core", resources, action + " " + str(profile or ""))
            except Exception as exc:
                if stopping and not isinstance(exc, APIError):
                    # The admission database is unavailable: a stop still goes ahead (it never depends on it). The
                    # benchmarks of this process that overlap are cancelled here; other processes' see it no more.
                    unheld = f"admission unavailable ({type(exc).__name__}: {exc}); stopped without its hold"
                    with self._job_lock:
                        contexts = [self._features_running[j] for j, held in self._hold_resources.items()
                                    if j in self._features_running and admission._overlap(held, resources)]
                    for context in contexts:
                        try:
                            context.cancel("stopped: " + action + " " + str(profile or ""))
                        except Exception:  # noqa: BLE001 - it sees the stop at its own next check, or ends
                            pass
                else:
                    with self._job_lock:
                        if self._active == jid:
                            self._active = None
                    error = exc if isinstance(exc, APIError) else APIError(f"admission unavailable: {exc}", 503)
                    self._audit(action, arguments, {"state": "refused", "error": str(error)})
                    raise error from exc
            if stopping and unheld is None:
                with self._job_lock:
                    contexts = [self._features_running[j] for j in self._holds if j in self._features_running]
                for context in contexts:
                    try:
                        context.cancelled()     # observe the stop's cancellation now, not at the next poll
                    except Exception:  # noqa: BLE001 - the benchmark sees it at its own next check
                        pass
        with self._job_lock:
            job = {"job": jid, "id": jid, "action": action, "arguments": redact(arguments),
                   "state": "running", "started_at": self.clock(), "finished_at": None,
                   "result": None, "error": None, "progress": "calling CLI service"}
            if unheld is not None:
                job["warning"] = unheld
            accepted = self._accepted(action, arguments, jid)
            if accepted is None:
                self._jobs[jid] = job
            # Keep completed jobs bounded; never evict an operation still running.
            for old in list(self._jobs):
                if len(self._jobs) <= 100:
                    break
                if self._jobs[old]["state"] != "running":
                    del self._jobs[old]
            response = dict(job)
        if accepted is not None:
            self._unaccept(jid, accepted)
        if action in ("recipes/deploy", "up"):
            arguments = dict(arguments, _job=jid)    # the launch's cancel marker is named by its job
        def run():
            try:
                result = self._execute(action, arguments)
                update = {"state": "succeeded", "result": redact(result), "error": None}
            except Exception as exc:
                update = {"state": "failed", "result": None, "error": redact(str(exc))}
            self._unhold(jid)
            try:
                self._audit(action, arguments, {"job": jid, **update, **({"warning": unheld} if unheld else {})})
            except OSError as exc:
                update = {"state": "failed", "result": update["result"], "error": redact("audit write failed: " + str(exc))}
            with self._job_lock:
                job.update(update, finished_at=self.clock(), progress="finished")
                if self._active == jid:
                    self._active = None
            with self._poll_lock:
                self._expires = 0
                self._status_stale = True
        self._start_worker(run, "hyperloom-ui-" + jid[:8], jid, job, action, arguments)
        return response
