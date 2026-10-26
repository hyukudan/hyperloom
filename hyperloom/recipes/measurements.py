"""Explicit acquisitions and passive local measurement lookup. A cache miss never starts work.

Tierstream owns the xfer-v1 hash. This module accepts that identity rather than duplicating its arithmetic.
GPU work lives in an owned child, blocked on stdin until locks, gates and attributable watchdog are ready.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

from .. import gpu_counters, placement_supervision, procs, rtx
from ..winproc import no_window
from . import content_cache, grants, probe
from .reader import RecipeError

RUNS = 9
XFER_METHOD = {"aggregation": "median", "runs": RUNS, "copies": "pinned", "bytes": 64 << 20,
               "latency_bytes": 4096, "timing": "synchronized-host-monotonic", "readers": 1}
STORE_METHOD = {"aggregation": "median", "runs": RUNS, "io": "direct", "read_bytes": probe.DISK_CHUNK,
                "readers": 1, "max_bytes_per_run": probe.DISK_PROBE_DEFAULT_BYTES}


def key(uuid, driver, xfer_sha256, store_id):
    if (not all(isinstance(v, str) and v and v != "unknown" for v in (uuid, driver, store_id))
            or not content_cache._digest(xfer_sha256)):
        raise RecipeError("PL_E_STALE measurements need UUID, driver, xfer-v1 SHA and store id")
    return {"uuid": grants.uuid_key(uuid), "driver": driver, "xfer_sha256": xfer_sha256, "store_id": store_id}


class Cache:
    def __init__(self, runtime):
        self.directory = Path(runtime) / "measurements"

    def save(self, identity, mode, result):
        identity = key(**identity)
        content_cache._write(self.directory / (content_cache._sha([identity, mode]) + ".json"),
                             {"key": identity, "mode": mode, "result": result})

    def lookup(self, identity, mode):
        """Read JSON only. Identity changes produce an unpredicted row, never a refresh."""
        try:
            identity = key(**identity)
            row = content_cache._read(self.directory / (content_cache._sha([identity, mode]) + ".json"), "PL_E_STALE")
            result = row["result"]
            provenance = result["provenance"]
            method = XFER_METHOD if mode == "xfer" else STORE_METHOD
            if provenance["method"] != method or median(provenance["samples"], method) != result:
                row = {}
        except (RecipeError, KeyError, TypeError):
            row = {}
        if row.get("key") != identity or row.get("mode") != mode:
            return {"status": "unpredicted", "why": "no measurement for the current identity"}
        return {"status": "measured", **row["result"]}


def approved(window, *, now):
    if not isinstance(window, dict) or not isinstance(window.get("approval"), str) or not window["approval"].strip():
        raise RecipeError("PL_E_GRANT store/manifest reads need an operator-approved window")
    interval = {"starts_at": window.get("starts_at"), "expires_at": window.get("expires_at"), "kind": "device"}
    grants.parse_window(interval, "probe.window")
    if not grants._active(interval, now):
        raise RecipeError("PL_E_GRANT probe window is not open or has expired")
    return rtx.expires(interval["expires_at"], "probe.window.expires_at")


def gpu_grant(cluster, row, *, now):
    cuda = cluster.pc.get("cuda", {})
    entries = grants.parse_grants(cuda.get("grants", []))
    grant = next((g for g in entries if grants.uuid_key(g["uuid"]) == grants.uuid_key(row["uuid"])), None)
    if grant is None or not grants._active(grant["window"], now, starting=True) or grant["vram_cap_gib"] <= 0:
        raise RecipeError("PL_E_GRANT measurement requires a current grant for this UUID (lab mode included)")
    windows = [g["window"] for g in entries]
    if cuda.get("host_grant"):
        windows.append(grants.parse_host_grant(cuda["host_grant"])["window"])
    # The child has a hard 60 s deadline: do not let a known PC-BIG window start during that turn either.
    big = any(w["kind"] == "PC-BIG" and rtx.expires(w["starts_at"], "probe window") < now + 60
              and rtx.expires(w["expires_at"], "probe window") > now for w in windows)
    reservation = cuda.get("reservation")
    if reservation:
        big = big or "window_s" in reservation or rtx.reservation_end(reservation, now) > now
    if "4090" in row["name"] and big:
        raise RecipeError("PL_E_GRANT no 4090 measurement during a PC-BIG window")
    return grant


def median(samples, method):
    if len(samples) != RUNS or not samples or any(set(s) != set(samples[0]) for s in samples):
        raise RecipeError("PL_E_STALE measurement needs nine runs of the same method")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for s in samples for v in s.values()):
        raise RecipeError("PL_E_STALE invalid measurement sample")
    return {"values": {k: statistics.median(s[k] for s in samples) for k in samples[0]},
            "provenance": {"method": method, "samples": samples}}


def _gpu(row, grant, *, clock=time.time, lock_factory=rtx.device_lock, gate_runner=rtx.run_gate,
         observer_factory=gpu_counters.Observer, watchdog_factory=placement_supervision.Watchdog,
         popen=subprocess.Popen, job_factory=None):
    policy = {"grants": [grant], "caps_gib": {grant["uuid"]: grant["vram_cap_gib"]},
              "caps_bytes": {grants.uuid_key(grant["uuid"]): int(grant["vram_cap_gib"] * rtx.GIB)},
              "host_pinned_gib": 0, "host_pageable_gib": 0, "free_commit_floor_gib": 4}
    end = min(clock() + 60, rtx.expires(grant["window"]["expires_at"], "probe grant"))
    spec = SimpleNamespace(placement_supervision=policy, deadline=end, free_commit_floor_gib=4)
    with ExitStack() as stack:
        observer = observer_factory()
        owned = [True]
        stack.callback(lambda: observer.close() if owned[0] else None)
        sample = observer.sample(os.getpid())
        if (grants.uuid_key(row["uuid"]) not in sample
                or any(type(v) is not int or v < 0 for v in sample.values())):
            raise RecipeError("PL_E_CAPABILITY measurement observer cannot attribute the selected GPU")
        placement_supervision.validate(policy, now=clock(), starting=True, memory=rtx.memory_status())
        placement_supervision.acquire(policy, stack, lock_factory=lock_factory, gate_runner=gate_runner,
                                      verify=lambda u: _idle(u), log=lambda _: None, now=clock())
        placement_supervision.validate(policy, now=clock(), starting=True)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=row["uuid"], CUDA_DEVICE_ORDER="PCI_BUS_ID")
        job = (job_factory or procs._WindowsJob)() if os.name == "nt" or job_factory else None
        if job is not None:
            stack.callback(job.close)
        child = popen([sys.executable, "-m", "hyperloom.recipes.measure_worker"],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, env=env, **no_window())
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
        stack.callback(cleanup)
        if job is not None:
            job.assign(child)  # Worker is still blocked on stdin; parent death closes the Job.
        dog = watchdog_factory(spec, child.pid, child.kill, lambda _: None, observer=observer, clock=clock)
        owned[0] = False  # Watchdog.start closes its observer on failure too.
        dog.start()
        # Watchdog owns the observer from here; clean up even when communicate/JSON validation fails.
        try:
            request = {"uuid": row["uuid"], "parent_pid": os.getpid(), "deadline": end}
            out, error = child.communicate(json.dumps(request) + "\n", timeout=max(.01, end - clock()))
            reason = dog.reason or dog.check()
            if reason or child.returncode:
                raise RecipeError("PL_E_GRANT probe stopped: " + (reason or error.strip()))
            return median(json.loads(out), XFER_METHOD)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
            dog.stop()


def _idle(uuid):
    busy = rtx.pro_busy(uuid)
    if busy:
        raise RecipeError("PL_E_BUSY measurement device is busy: " + busy)


def _store(path, window, *, clock=time.time, measure=probe.read_bandwidth):
    samples = []
    for _ in range(RUNS):
        remaining = approved(window, now=clock()) - clock()
        result = measure(path, max_s=min(probe.DISK_PROBE_MAX_S, remaining))
        if result["method"] != "direct":
            raise RecipeError("PL_E_STALE buffered store reads cannot predict FILE performance")
        approved(window, now=clock())
        samples.append({"file_read_mbps": result["gbytes_ps"] * 1000})
    return median(samples, STORE_METHOD)


def manifest(cluster, files, window, *, clock=time.time, run=subprocess.run):
    """Full-read CPU acquisition owned by ts-recipe, bounded by the operator's approved deadline."""
    from . import core
    end = approved(window, now=clock())
    executable = core.locate(cluster)
    if executable is None:
        raise RecipeError("PL_E_CAPABILITY probe --manifest requires ts-recipe")
    argv = [str(executable), "manifest", "--full", *[str(Path(p).resolve()) for p in files]]
    remaining = end - clock()
    if remaining <= 0:
        raise RecipeError("PL_E_GRANT manifest window has expired")
    try:
        result = run(argv, capture_output=True, text=True, timeout=remaining, **no_window())
    except subprocess.TimeoutExpired as exc:
        raise RecipeError("PL_E_GRANT manifest stopped at the approved window deadline") from exc
    except OSError as exc:
        raise RecipeError("PL_E_CAPABILITY ts-recipe manifest unavailable") from exc
    approved(window, now=clock())
    if result.returncode:
        raise RecipeError(result.stderr.rstrip() or "PL_E_STALE ts-recipe manifest refused")
    try:
        if len(result.stdout.encode("utf-8")) > content_cache.MAX_JSON_BYTES:
            raise ValueError("manifest too large")
        document = json.loads(result.stdout)
        entries = content_cache.ManifestStore._entries(document)
        if set(entries) != {os.path.normcase(str(Path(p).resolve())) for p in files}:
            raise ValueError("manifest differs from requested files")
    except (ValueError, TypeError) as exc:
        raise RecipeError("PL_E_STALE invalid ts-recipe manifest output") from exc
    return document


def acquire(cluster, mode, *, runtime, uuid=None, xfer_sha256=None, store_id=None, files=(), window=None,
            clock=time.time, inventory=None, gpu_runner=_gpu, store_runner=_store,
            manifest_runner=None):
    """Explicit top-level command only. All authorization checks precede any child or model read."""
    if mode not in ("xfer", "store", "manifest"):
        raise RecipeError("unknown measurement")
    approval = None
    if mode != "xfer":
        if window is None:
            approved(None, now=clock())
        approval = content_cache._read(window, "PL_E_GRANT") if isinstance(window, (str, Path)) else window
        approved(approval, now=clock())
        if not files or (mode == "store" and len(files) != 1):
            raise RecipeError("probe needs explicit --file paths (exactly one for --store)")
    if mode == "manifest":
        # The core owns header/table boundaries and the manifest schema; no substitute hash algorithm here.
        if manifest_runner is None:
            raise RecipeError("PL_E_CAPABILITY ts-recipe manifest integration requires a qualified manifest CLI")
        document = manifest_runner(files, approval)
        approved(approval, now=clock())
        return {"manifest_sha256": content_cache.ManifestStore(runtime).save(document)}
    if mode == "xfer":
        configured = [d for m in cluster.machines.values() if m.access == "local" for d in m.devices
                      if not d.get("excluded", False) and uuid and grants.uuid_key(d.get("uuid", "")) == grants.uuid_key(uuid)]
        if not configured:
            raise RecipeError("PL_E_UNKNOWN_DEVICE probe UUID must be configured and non-excluded")
        # Refuse missing grants before even querying inventory.
        gpu_grant(cluster, {"uuid": uuid, "name": ""}, now=clock())
        rows = [r for r in (inventory() if inventory is not None else probe.nvidia_gpus(placement=True))
                if grants.uuid_key(r["uuid"]) == grants.uuid_key(uuid)]
        if len(rows) != 1:
            raise RecipeError("PL_E_UNKNOWN_DEVICE measurement UUID is absent or ambiguous")
        row = rows[0]
        grant = gpu_grant(cluster, row, now=clock())
        identity = key(row["uuid"], row.get("driver"), xfer_sha256, store_id)
        result = gpu_runner(row, grant)
        gpu_grant(cluster, row, now=clock())
    else:
        # Store figures are host-only; the explicit sentinel keeps GPU and store identities disjoint.
        identity = key("host-store", "none", xfer_sha256, store_id)
        result = store_runner(files[0], approval)
        approved(approval, now=clock())
    Cache(runtime).save(identity, mode, result)
    return {"key": identity, **result}
