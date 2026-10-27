"""USB4 link health (F14): trained link speed per direction, stream state, ring interrupts, kernel evidence and
the engine-measured exchange per step.

Collector ``usb4`` (every 10 s) calls the read-only node verb ``usb4`` on every Linux machine; every 60 s the call
also reads the kernel journal after the cursor stored for the node's current boot. Series (contract C3):

    usb4_link_gbps{machine, router, direction}         gauge  Gb/s   per-lane speed x lanes
    usb4_link_lanes{machine, router, direction}        gauge  lanes
    usb4_stream_attached{machine, stream}              state  0 detached / 1 attached (live service binding only)
    usb4_ring_irqs{machine, domain, nhi, ring, vector} delta  interrupts per host-interface vector
    usb4_kernel_evidence{machine, kind}                delta  kernel lines of that kind

Counters (interrupt counts, evidence totals per boot) go through ``store.counter_delta``; an interrupt vector's
rate row is written in the same transaction as its baseline. Per machine the store keeps ``usb4/node`` (the last
summary and the state of every source), ``usb4/journal`` (boot, cursor, scan generation, totals) and, per engine,
``usb4/exchange`` (the last group counters). GET ``usb4`` reads only those; it never calls a node and never
refreshes the portal snapshot. POST ``usb4/refresh`` asks the sampler for a collection now.
"""

import math
import threading

from hyperloom import nodegate, nodepoll, pollbudget, telemetry
from hyperloom.telemetry.store import BASELINES


INTERVAL_S = pollbudget.CYCLE_S
JOURNAL_EVERY_S = 60.0
FULL_LANE_GBPS, FULL_LANES = 20.0, 2
FULL_RATE_GBPS = FULL_LANE_GBPS * FULL_LANES
SNAPSHOT_MAX_AGE_S = 30.0
EXCHANGE_MAX_AGE_S = 120.0
EXCHANGE_STALE_S = 3 * INTERVAL_S       # three collector intervals without a new observation
MAX_PARALLEL = 8
JOIN_S = 15.0
NODE_NS, JOURNAL_NS, EXCHANGE_NS = "usb4/node", "usb4/journal", "usb4/exchange"
KINDS = ("reset_pending", "host_reset", "quarantine", "ring_leaked", "contexts_retained", "hop_deactivation_failed",
         "close_not_sent", "teardown")
KIND_LABELS = {"reset_pending": "host interface reset pending", "host_reset": "host interface reset",
               "quarantine": "GPU context quarantine", "ring_leaked": "ring leaked by an abandoned owner",
               "contexts_retained": "GPU contexts retained", "hop_deactivation_failed": "hop deactivation failed",
               "close_not_sent": "CLOSE not sent to the peer", "teardown": "other teardown messages"}
SOURCES = {"links": INTERVAL_S, "layout": INTERVAL_S, "live": INTERVAL_S, "parameters": INTERVAL_S,
           "interrupts": INTERVAL_S, "journal": JOURNAL_EVERY_S}
RATE_TABLE = ("CREATE TABLE IF NOT EXISTS usb4_irq_rate (machine TEXT NOT NULL, nhi TEXT NOT NULL, "
              "vector INTEGER NOT NULL, ts REAL NOT NULL, gap REAL NOT NULL, delta REAL NOT NULL, irq INTEGER NOT NULL, "
              "uncertain INTEGER NOT NULL, PRIMARY KEY(machine, nhi, vector))")
RATE_UPSERT = ("INSERT INTO usb4_irq_rate(machine, nhi, vector, ts, gap, delta, irq, uncertain) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(machine, nhi, vector) DO UPDATE SET ts=excluded.ts, "
               "gap=excluded.gap, delta=excluded.delta, irq=excluded.irq, uncertain=excluded.uncertain")
NO_STORE = "history store not open in this portal process (samplers disabled)"


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _unavailable(why):
    return {"status": "unavailable", "why": why}


# -- node call --------------------------------------------------------------------------------------------------
def machines(api):
    """The machines that can carry USB4 links: Linux nodes reached over ssh or locally."""
    return [m for m in api.cluster().machines.values() if m.os == "linux" and m.access in ("ssh", "local")]


def fetch(api, machine, payload):
    """One read-only node call (the browser fixture replaces this with recorded node outputs)."""
    return api.node(machine, "usb4", payload)


_inflight = set()          # (id(api), machine id) while a usb4 call of that portal to that machine runs
_inflight_lock = threading.Lock()
SKIPPED = "skipped"


def _gate(api, machine):
    """The machine's node gate taken now or not at all (never waits: a busy machine is skipped this round, so no
    thread queues on the gate and the core and the command line are never starved). A release function, or None."""
    config_path = getattr(api, "config_path", None)
    if machine.access != "ssh" or config_path is None:
        return lambda: None
    return nodegate.try_hold(config_path, machine.id)


def _calls(api, targets, payloads, now=None):
    """{machine id: (result, error)} with at most MAX_PARALLEL calls at a time, each joined within JOIN_S. A machine
    whose previous call still runs, or whose node gate another call holds, answers (None, SKIPPED + why) without a
    call: its previous report stays."""
    out = {}
    lock = threading.Lock()
    every = list(targets)

    def one(machine, release, key):
        try:
            if machine.access == "ssh" and getattr(api, "config_path", None) is not None:
                nodegate.adopt(api.config_path, machine.id)    # this thread makes the call: api.node re-enters
            result, error = fetch(api, machine, payloads[machine.id]), None
            if not isinstance(result, dict):
                result, error = None, "node returned no USB4 report"
            elif result.get("error"):
                result, error = None, str(result["error"])[:300]
        except Exception as exc:          # one unreachable node must not hide the others
            result, error = None, f"{type(exc).__name__}: {exc}"[:300]
        finally:
            release()
            with _inflight_lock:
                _inflight.discard(key)
        with lock:
            out[machine.id] = (result, error)

    poll = nodepoll.of(api)
    if poll is not None:
        # The portal's node poll reads the ssh nodes (one session per cycle for every reader): their latest report.
        for machine in [m for m in targets if m.access == "ssh"]:
            result, error, _ = poll.read(machine.id, "usb4", "usb4", payloads[machine.id], now=now, reader="usb4")
            if result is None and (error == nodepoll.NOT_YET or error.startswith(("paused", "no new"))):
                out[machine.id] = (None, f"{SKIPPED}: {error}"[:300])
            elif result is None or result.get("error"):
                out[machine.id] = (None, str(error or result.get("error"))[:300])
            else:
                out[machine.id] = (result, None)
        targets = [m for m in targets if m.access != "ssh"]
    for start in range(0, len(targets), MAX_PARALLEL):
        batch = []
        for machine in targets[start:start + MAX_PARALLEL]:
            key = (id(api), machine.id)
            with _inflight_lock:
                if key in _inflight:
                    out[machine.id] = (None, SKIPPED + ": the previous call to this machine is still running")
                    continue
                _inflight.add(key)
            release = None
            try:
                if machine.access == "ssh":
                    why = pollbudget.for_api(api).node(machine.id, "usb4", now=now)   # paced; paused in a turn
                    if why is not None:
                        raise LookupError(why)
                release = _gate(api, machine)
                if release is None:
                    if machine.access == "ssh":
                        pollbudget.for_api(api).refund(machine.id, "usb4")   # no call: the grant is given back
                    raise LookupError("another node call holds this machine")
                thread = threading.Thread(target=one, args=(machine, release, key), daemon=True,
                                          name="hyperloom-usb4-" + machine.id)
                thread.start()
            except Exception as exc:  # noqa: BLE001 - busy gate, or no worker: given back, skipped this round
                if release is not None:
                    release()
                with _inflight_lock:
                    _inflight.discard(key)
                out[machine.id] = (None, f"{SKIPPED}: {exc}"[:300])
                continue
            batch.append(thread)
        for thread in batch:
            thread.join(JOIN_S)
    with lock:
        return {m.id: out.get(m.id, (None, "node call did not return within %g s" % JOIN_S)) for m in every}


# -- summary (shared with the CLI) -------------------------------------------------------------------------------
def _direction(value):
    value = value if isinstance(value, dict) else {}
    keep = ("gbps_per_lane", "lanes", "gbps", "full_rate", "raw_speed", "raw_lanes")
    return {key: value.get(key) for key in keep}


def summarise(result, now, previous=None):
    """Portal/CLI view of one node report. `previous` is the last summary of the same machine (for changes of
    link speed within one boot)."""
    previous = previous or {}
    same_boot = bool(result.get("boot_id")) and previous.get("boot_id") == result.get("boot_id")
    summary = {"boot_id": result.get("boot_id"), "kernel": result.get("kernel"), "collected_at": now,
               "node_time": result.get("collected_at"), "domains": result.get("domains") or {}}
    links = result.get("links")
    if isinstance(links, list):
        before = {row.get("router"): row for row in previous.get("links") or [] if isinstance(row, dict)}
        rows = []
        for link in links:
            if not isinstance(link, dict):
                continue
            row = {key: link.get(key) for key in ("router", "domain", "nhi", "usb4_port", "peer", "full_rate",
                                                  "below_full_rate")}
            row["rx"], row["tx"] = _direction(link.get("rx")), _direction(link.get("tx"))
            old = before.get(row["router"]) if same_boot else None
            row["changed"] = old.get("changed") if old else None
            if old and (old["rx"].get("gbps"), old["tx"].get("gbps")) != (row["rx"]["gbps"], row["tx"]["gbps"]):
                row["changed"] = {"at": now, "from": {"rx": old["rx"].get("gbps"), "tx": old["tx"].get("gbps")},
                                  "to": {"rx": row["rx"]["gbps"], "tx": row["tx"]["gbps"]}}
            rows.append(row)
        summary["links"] = rows
        summary["links_why"] = (result.get("why") or {}).get("links")
    else:
        summary["links"] = None
        summary["links_why"] = (links or {}).get("why", "links not reported") if isinstance(links, dict) else "links not reported"
    layout = result.get("layout") if isinstance(result.get("layout"), dict) else _unavailable("layout not reported")
    if layout.get("status") == "ok":
        live_links = {row["router"]: row for row in summary["links"] or []}
        cables = []
        for cable in layout.get("cables") or []:
            cable = dict(cable)
            live = live_links.get(cable.get("router"))
            live_gbps = None
            if live and _finite(live["rx"].get("gbps")) and _finite(live["tx"].get("gbps")):
                live_gbps = min(live["rx"]["gbps"], live["tx"]["gbps"])
            cable["live_gbps"] = live_gbps
            cable["layout_differs"] = (live_gbps is not None and _finite(cable.get("gbps"))
                                       and cable["gbps"] != live_gbps)
            cables.append(cable)
        summary["layout"] = {"status": "ok", "generated_at": layout.get("generated_at"),
                             "modified_at": layout.get("modified_at"), "version": layout.get("version"),
                             "warnings": layout.get("warnings") or [], "live": layout.get("live")}
        summary["streams"] = layout.get("streams") or []
        summary["cables"] = cables
    else:
        summary["layout"] = {"status": "unavailable", "why": layout.get("why", "layout not reported")}
        summary["streams"] = None
        summary["cables"] = None
    parameters = result.get("parameters")
    summary["parameters"] = parameters if isinstance(parameters, dict) else _unavailable("parameters not reported")
    interrupts = result.get("interrupts") if isinstance(result.get("interrupts"), dict) else _unavailable("not reported")
    summary["interrupts"] = ({"status": "ok", "vectors": interrupts.get("vectors") or [], "note": interrupts.get("note")}
                             if interrupts.get("status") == "ok" else interrupts)
    journal = result.get("journal")
    summary["journal_read"] = journal if isinstance(journal, dict) else _unavailable("journal not reported")
    return summary


def nhi_rates(vectors, rates, now, previous=None):
    """[{nhi, domain, ring, vectors, irqs, count, rate_per_s, uncertain, why}] per host interface and ring class.

    `rates` maps (nhi, vector) -> (ts, gap, delta, irq, uncertain) rows written with the vector's baseline.
    `previous` is the summary of the previous sample ({"collected_at", "interrupts": {"vectors"}}). A class has a
    rate only when its membership (vector and IRQ numbers) is the same as at the previous sample, that sample is
    the start of every member's interval (one common window ending now) and every member counted a delta."""
    previous = previous or {}
    before_ts = previous.get("collected_at")
    before = {}
    for vector in ((previous.get("interrupts") or {}).get("vectors") or []):
        before.setdefault((vector.get("nhi"), vector.get("ring")), set()).add((vector.get("vector"), vector.get("irq")))
    groups = {}
    for vector in vectors:
        groups.setdefault((vector["nhi"], vector["ring"]), []).append(vector)
    out = []
    for (nhi, ring), members in sorted(groups.items()):
        found = [rates.get((nhi, v["vector"])) for v in members]
        membership = {(v["vector"], v["irq"]) for v in members}
        rate, why, uncertain = None, None, False
        if not _finite(before_ts) or (nhi, ring) not in before:
            why = "needs two samples (first sample of these vectors)"
        elif before[(nhi, ring)] != membership:
            why = "a ring was opened or closed since the previous sample"
        elif any(row is None or row[0] != now or row[3] != v["irq"] for row, v in zip(found, members)):
            why = "a vector was reset or replaced since the previous sample"
        elif any(abs((now - row[1]) - before_ts) > 1e-6 for row in found) or found[0][1] <= 0:
            why = "the previous sample of these vectors was not recorded (no common interval)"
        else:
            rate = sum(row[2] for row in found) / found[0][1]
            uncertain = any(row[4] for row in found)
        out.append({"nhi": nhi, "domain": members[0].get("domain"), "ring": ring, "vectors": len(members),
                    "irqs": [v["irq"] for v in members], "count": sum(v["count"] for v in members),
                    "rate_per_s": rate, "uncertain": uncertain,
                    "why": why or ("spread over a gap longer than three intervals" if uncertain else None)})
    return out


# -- accounting --------------------------------------------------------------------------------------------------
def _prepare(store):
    store.describe("usb4_link_gbps", "gauge", unit="Gb/s", interval=INTERVAL_S)
    store.describe("usb4_link_lanes", "gauge", unit="lanes", interval=INTERVAL_S)
    store.describe("usb4_stream_attached", "state", interval=INTERVAL_S, legend={"0": "detached", "1": "attached"})
    store.describe("usb4_ring_irqs", "delta", unit="interrupts", interval=INTERVAL_S)
    store.describe("usb4_kernel_evidence", "delta", unit="lines", interval=JOURNAL_EVERY_S)
    store.ensure_table(RATE_TABLE)


def _gauges(machine, summary):
    points = []
    for link in summary.get("links") or []:
        for direction in ("rx", "tx"):
            labels = {"machine": machine, "router": link["router"], "direction": direction}
            if _finite(link[direction].get("gbps")):
                points.append(("usb4_link_gbps", labels, link[direction]["gbps"], "gauge"))
            if _finite(link[direction].get("lanes")):
                points.append(("usb4_link_lanes", labels, link[direction]["lanes"], "gauge"))
    for stream in summary.get("streams") or []:
        attached = (stream.get("live") or {}).get("attached_now")
        if type(attached) is bool:           # live observation only; the published flag is never a sample
            points.append(("usb4_stream_attached", {"machine": machine, "stream": stream["id"]}, int(attached), "state"))
    return points


def _account_irqs(store, machine, boot, vectors, now, previous):
    """Per vector: counter_delta advances its baseline and, for a counted delta, records the delta point and the
    vector's rate row in the same transaction. Each vector gets its outcome (counted, baseline, reset)."""
    for vector in vectors:
        key = f"usb4/irq/{machine}/{vector['nhi']}/{vector['vector']}"
        base = store.get(BASELINES, key)      # only this collector writes these keys, one run in flight
        labels = {"machine": machine, "domain": "unknown" if vector.get("domain") is None else str(vector["domain"]),
                  "nhi": vector["nhi"], "ring": vector["ring"], "vector": str(vector["vector"])}

        def apply(delta, info, base=base, vector=vector, labels=labels):
            gap = now - base["ts"]
            return {"points": [("usb4_ring_irqs", labels, delta, "delta")],
                    "sql": [(RATE_UPSERT, (machine, vector["nhi"], vector["vector"], now, gap, delta, vector["irq"],
                                           int(bool(info.get("uncertain")))))]}

        delta, info = store.counter_delta(key, f"{boot}/{vector['irq']}", vector["count"], now,
                                          max_gap=3 * INTERVAL_S, apply=apply)
        vector["outcome"] = ("counted" if delta is not None else "stale" if info.get("stale") else
                             "baseline" if info.get("unobserved_s") is None else "reset")
        if info.get("unobserved_s"):
            vector["unobserved_s"] = info["unobserved_s"]
    rows = store.execute("SELECT nhi, vector, ts, gap, delta, irq, uncertain FROM usb4_irq_rate WHERE machine=?",
                         (machine,))
    return nhi_rates(vectors, {(n, v): (t, g, d, i, u) for n, v, t, g, d, i, u in rows}, now, previous)


def _account_journal(store, machine, read, now, boot=None):
    """Fold one journal read into the stored per-boot state; returns the state document. `boot` is the boot id
    the node reported in this collection: when it differs from the stored one and this collection did not read
    the new boot's journal, the stored totals are retired (they describe the previous boot) and the next
    collection scans the new boot."""
    doc = store.get(JOURNAL_NS, machine) or {}
    status = read.get("status")
    if boot and doc.get("boot_id") and doc["boot_id"] != boot and status != "ok":
        doc = {"status": "unavailable", "attempted_at": None, "boot_id": None,
               "why": "the node booted again; the kernel journal of this boot is read at the next collection"
               + ("" if status == "skipped" else " (last read: %s)" % read.get("why")),
               "previous_boot": {"boot_id": doc["boot_id"], "totals": doc.get("totals"), "read_at": doc.get("read_at")}}
        store.put(JOURNAL_NS, machine, doc)
        return doc
    if status == "skipped":
        return doc
    if status != "ok":
        doc.update(status="unavailable", why=read.get("why"), attempted_at=now)
        store.put(JOURNAL_NS, machine, doc)
        return doc
    boot = read["boot_id"]
    counts = {kind: int((read.get("counts") or {}).get(kind) or 0) for kind in KINDS}
    same = doc.get("boot_id") == boot and isinstance(doc.get("totals"), dict)
    if read.get("mode") == "follow" and same:
        generation = doc.get("generation") or 1
        totals = {kind: int(doc["totals"].get(kind) or 0) + counts[kind] for kind in KINDS}
        complete = bool(doc.get("complete", True)) and bool(read.get("complete"))
        incomplete_why = "; ".join(r for r in (doc.get("incomplete_why"), read.get("incomplete_why")) if r) or None
        recent = ((doc.get("recent") or []) + (read.get("recent") or []))[-20:]
        last_new_at = now if read.get("matched") else doc.get("last_new_at")
    else:
        # A scan of this boot from scratch: a new generation that replaces the totals (never adds to them).
        generation = (doc.get("generation") or 0) + 1 if same else 1
        totals = counts
        complete = bool(read.get("complete")) and read.get("mode") != "follow"
        incomplete_why = (read.get("incomplete_why") if not read.get("complete") else
                          "the stored journal totals were missing: counted from the stored cursor only"
                          if read.get("mode") == "follow" else None)
        recent = read.get("recent") or []
        last_new_at = doc.get("last_new_at") if same else None
    instance = f"{boot}/{generation}"
    for kind in KINDS:
        store.counter_delta(f"usb4/evidence/{machine}/{kind}", instance, totals[kind], now,
                            apply=lambda delta, info, kind=kind: {"points": [
                                ("usb4_kernel_evidence", {"machine": machine, "kind": kind}, delta, "delta")]})
    doc = {"status": "ok", "why": None, "boot_id": boot, "cursor": read.get("cursor"), "generation": generation,
           "totals": totals, "complete": complete, "incomplete_why": incomplete_why, "more": bool(read.get("more")),
           "recent": recent, "last_new_at": last_new_at, "attempted_at": now, "read_at": now,
           "mode": read.get("mode"), "scanned_from": read.get("scanned_from") if read.get("mode") != "follow"
           else doc.get("scanned_from")}
    store.put(JOURNAL_NS, machine, doc)
    return doc


def _sources(previous, attempted, now, error=None):
    """Per-source outcome: status, why, last_attempt, last_ok, interval_s."""
    out = {name: dict(value) for name, value in (previous or {}).items()}
    for name, part in attempted.items():
        row = out.setdefault(name, {"last_ok": None})
        ok = error is None and isinstance(part, (dict, list)) and (isinstance(part, list) or part.get("status") == "ok")
        row.update(status="ok" if ok else "unavailable", interval_s=SOURCES[name], last_attempt=now,
                   why=None if ok else error or (part.get("why") if isinstance(part, dict) else None) or "not reported")
        if ok:
            row["last_ok"] = now
    return out


# -- exchange (engine-measured) ----------------------------------------------------------------------------------
def _cached_stats(api, now):
    """The stats document of the portal's last shared snapshot when it is recent; never refreshes it (a refresh
    would run node inspections)."""
    cached = getattr(api, "_cached", None)
    stats = cached.get("stats") if isinstance(cached, dict) else None
    when = stats.get("server_time") if isinstance(stats, dict) else None
    if not _finite(when) or now - when > SNAPSHOT_MAX_AGE_S:
        return None
    return stats


def _ratio(numerator, denominator):
    return numerator / denominator if _finite(numerator) and _finite(denominator) and denominator > 0 else None


def _members(profile):
    """Machine ids of a profile: replica profiles list nodes, pair profiles name rank0 and rank1."""
    if not isinstance(profile, dict):
        return None
    if isinstance(profile.get("nodes"), list):
        return [m for m in profile["nodes"] if isinstance(m, str)]
    ranks = [profile.get(key) for key in ("rank0", "rank1") if isinstance(profile.get(key), str)]
    return ranks or None


RATIOS = {"seconds_per_step": ("exchange_seconds", "steps"), "seconds_per_exchange": ("exchange_seconds", "exchanges"),
          "bytes_per_step": ("bytes_sent", "steps"), "bytes_per_exchange": ("bytes_sent", "exchanges")}


def _exchange(api, store, now):
    """Engine group counters from the cached snapshot -> per-engine exchange view (one `put` per engine holding
    the counters, the group identity and the observation time; the next observation divides the differences).

    A window exists only between two observations of the same engine instance and the same group: uptime
    reported by both and advanced by the elapsed time, group id and epoch reported and equal. Each ratio then
    needs its own two counters present, not decreasing, and a positive denominator."""
    stats = _cached_stats(api, now)
    if stats is None:
        return
    observed = stats["server_time"]
    profiles = getattr(api.cluster(), "profiles", {}) or {}
    for item in stats.get("engines") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        last = store.get(EXCHANGE_NS, item["id"]) or {}
        if last.get("observed_at") == observed:
            continue                      # the same snapshot again: nothing new was measured
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        group = status.get("group") if isinstance(status.get("group"), dict) else {}
        batch = status.get("batch") if isinstance(status.get("batch"), dict) else {}
        counters = {"exchanges": group.get("exchanges"), "exchange_seconds": group.get("exchange_seconds"),
                    "bytes_sent": group.get("bytes_sent"), "steps": batch.get("steps")}
        counters = {k: v if _finite(v) else None for k, v in counters.items()}
        identity = {key: group.get(key) for key in ("group_id", "epoch", "rank", "world", "transport")}
        profile = item.get("profile")
        view = {"engine": item["id"], "profile": profile, "machines": _members(profiles.get(profile)),
                "group": identity, "observer": "engine (rank %s)" % identity["rank"] if identity["rank"] is not None
                else "engine", "observed_at": observed, "uptime_s": status.get("uptime_seconds"), "why": {}}
        if not group:
            view["why"]["group"] = (item.get("why") or {}).get("status") or "engine reports no group counters"
        previous = last.get("counters") or {}
        span = observed - last["observed_at"] if _finite(last.get("observed_at")) else None
        uptime, old_uptime = status.get("uptime_seconds"), last.get("uptime_s")
        reasons = []
        if span is None or span <= 0:
            reasons.append("needs two observations of the engine")
        else:
            if not (_finite(uptime) and _finite(old_uptime)):
                reasons.append("engine uptime not reported: the engine instance cannot be confirmed")
            elif abs((uptime - old_uptime) - span) > max(5.0, 0.5 * span):
                reasons.append("engine uptime does not match the elapsed time (restart)")
            if identity["group_id"] is None or identity["epoch"] is None:
                reasons.append("group id or epoch not reported: the group cannot be confirmed")
            elif last.get("group") != identity:
                reasons.append("the engine group changed (id, epoch, rank or transport) between observations")
        window = None
        if not reasons:
            window = {"span_s": span}
            delta = {}
            for name in counters:
                if counters[name] is not None and _finite(previous.get(name)) and counters[name] >= previous[name]:
                    delta[name] = counters[name] - previous[name]
            window["steps"], window["exchanges"] = delta.get("steps"), delta.get("exchanges")
            for ratio, (top, bottom) in RATIOS.items():
                value = _ratio(delta.get(top), delta.get(bottom))
                window[ratio] = value
                if value is None:
                    view["why"][ratio] = (f"{top} or {bottom} missing or went down" if top not in delta or bottom not in delta
                                          else f"no {bottom} in the window (idle)")
        elif group:
            view["why"]["window"] = "; ".join(reasons)
        view["window"] = window
        # Lifetime ratios only between counters of one origin (the group's own counters); batch steps count
        # from engine start, the group counters from the group's formation, so no lifetime per-step value.
        view["lifetime"] = {"seconds_per_exchange": _ratio(counters["exchange_seconds"], counters["exchanges"]),
                            "bytes_per_exchange": _ratio(counters["bytes_sent"], counters["exchanges"]),
                            "label": "since the group formed (group counters)"} if group else None
        store.put(EXCHANGE_NS, item["id"], {"observed_at": observed, "counters": counters, "group": identity,
                                            "uptime_s": uptime, "view": view})


def _series_per_step(store, now):
    """Fresh samples of the observability series usb4_exchange_seconds_per_step, when that package records it."""
    descriptor = store.descriptor("usb4_exchange_seconds_per_step")
    if not descriptor:
        return []
    age = 3 * (descriptor.get("interval_s") or INTERVAL_S)
    return [{"labels": row["labels"], "ts": row["ts"], "seconds_per_step": row["value"]}
            for row in store.latest("usb4_exchange_seconds_per_step", max_age=age)]


# -- collector ---------------------------------------------------------------------------------------------------
def sample(api, now):
    store = telemetry.open_store(api)
    _prepare(store)
    targets = machines(api)
    payloads = {}
    for machine in targets:
        journal = store.get(JOURNAL_NS, machine.id) or {}
        attempted = journal.get("attempted_at")
        due = not _finite(attempted) or now - attempted >= JOURNAL_EVERY_S or journal.get("more")
        payloads[machine.id] = {"journal": {"boot_id": journal.get("boot_id"), "cursor": journal.get("cursor")}} if due else {}
    results = _calls(api, targets, payloads, now)
    points = []
    for machine in targets:
        result, error = results[machine.id]
        previous = store.get(NODE_NS, machine.id) or {}
        if error is not None and error.startswith(SKIPPED):
            # Not called this round: the previous report stays as it is, with the reason.
            previous["skipped"], previous["skipped_at"] = error, now
            store.put(NODE_NS, machine.id, previous)
            continue
        if error is not None:
            parts = {name: None for name in SOURCES if name != "journal" or payloads[machine.id]}
            previous["sources"] = _sources(previous.get("sources"), parts, now, error=error)
            previous["error"], previous["error_at"] = error, now
            store.put(NODE_NS, machine.id, previous)
            continue
        summary = summarise(result, now, previous.get("summary"))
        if not payloads[machine.id]:
            summary["journal_read"] = {"status": "skipped", "why": "not requested in this call"}
        layout = result.get("layout") if isinstance(result.get("layout"), dict) else {}
        links_failed = (result.get("why") or {}).get("links_failed")
        attempted = {"links": _unavailable(links_failed) if links_failed else result.get("links"),
                     "layout": result.get("layout"),
                     "live": layout.get("live") or _unavailable(layout.get("why") or "no published layout to check"),
                     "parameters": result.get("parameters"), "interrupts": result.get("interrupts")}
        if summary["journal_read"].get("status") != "skipped":
            attempted["journal"] = summary["journal_read"]
        if summary["interrupts"].get("status") == "ok" and summary["boot_id"]:
            summary["interrupts"]["nhis"] = _account_irqs(store, machine.id, summary["boot_id"],
                                                          summary["interrupts"]["vectors"], now, previous.get("summary"))
        _account_journal(store, machine.id, summary.pop("journal_read"), now, summary["boot_id"])
        points.extend(_gauges(machine.id, summary))
        store.put(NODE_NS, machine.id, {"summary": summary, "sources": _sources(previous.get("sources"), attempted, now),
                                        "error": None, "error_at": None})
    try:
        _exchange(api, store, now)
    except (TypeError, ValueError, AttributeError):
        pass                              # the exchange view is optional; the link data above is recorded
    return points


# -- GET / POST --------------------------------------------------------------------------------------------------
def _stale(sources, now):
    out = {}
    for name, row in (sources or {}).items():
        last = row.get("last_ok")
        limit = 3 * (row.get("interval_s") or INTERVAL_S)
        out[name] = (not _finite(last)) or now - last > limit
    return out


def _machine_view(store, machine, now):
    node = store.get(NODE_NS, machine.id) if store is not None else None
    journal = store.get(JOURNAL_NS, machine.id) if store is not None else None
    if store is None:
        return {"id": machine.id, "summary": None, "sources": None, "journal": None, "why": {"summary": NO_STORE}}
    if not node:
        return {"id": machine.id, "summary": None, "sources": None, "journal": journal,
                "why": {"summary": "not collected yet (the usb4 collector runs every 10 s)"}}
    summary = node.get("summary")
    view = {"id": machine.id, "summary": summary, "sources": node.get("sources"),
            "stale": _stale(node.get("sources"), now), "error": node.get("error"), "error_at": node.get("error_at"),
            "journal": journal, "why": {}}
    if summary is None:
        view["why"]["summary"] = node.get("error") or "no successful collection yet"
    else:
        view["age_s"] = now - summary["collected_at"]
    return view


def document(api, query):
    if query:
        raise ValueError("usb4 takes no query arguments")
    now = api.clock()
    store = telemetry.existing_store(api)
    targets = machines(api)
    exchange, why = [], {}
    if store is None:
        why["machines"] = NO_STORE
        why["exchange"] = NO_STORE
    else:
        for _key, _ts, value in store.items(EXCHANGE_NS):
            view = dict(value.get("view") or {})
            view["age_s"] = now - value["observed_at"] if _finite(value.get("observed_at")) else None
            if view["age_s"] is None or view["age_s"] <= EXCHANGE_MAX_AGE_S:
                view["stale"] = view["age_s"] is None or view["age_s"] > EXCHANGE_STALE_S
                if view["stale"]:
                    view["why"] = dict(view.get("why") or {}, stale="no new engine observation for more than %d s; "
                                       "values are as of their observation time" % EXCHANGE_STALE_S)
                exchange.append(view)
        if not exchange:
            why["exchange"] = ("engine status not sampled in the last %d s (the shared snapshot refreshes while a portal "
                               "page is open) or no engine is running" % EXCHANGE_MAX_AGE_S)
    status = telemetry.status(api)
    return api.document("usb4", machines=[_machine_view(store, m, now) for m in targets],
                        exchange=sorted(exchange, key=lambda row: str(row.get("engine"))),
                        exchange_series=_series_per_step(store, now) if store is not None else None,
                        full_rate={"gbps_per_lane": FULL_LANE_GBPS, "lanes": FULL_LANES, "gbps": FULL_RATE_GBPS},
                        kinds=KIND_LABELS, collector=(status or {}).get("usb4"),
                        samplers=status is not None, why=why)


def refresh(api, arguments):
    if arguments:
        raise ValueError("usb4/refresh takes no arguments")
    telemetry.refresh(api, "usb4")
    return {"requested": True, "note": "a collection runs now unless one is already running (then that one counts)"}


def register(reg):
    reg.collector("usb4", sample, interval=INTERVAL_S)
    reg.get("usb4", document)
    reg.post("usb4/refresh", refresh)
    reg.capability("usb4", True)
