"""Placement-only supervision. Operator grants never open or extend a PC-BIG window."""
from __future__ import annotations

import math

from . import gpu_counters, rtx
from .recipes import grants
from .config import number


def validate(policy, *, now, starting=False, memory=None):
    """Validate an exact cap for each granted UUID and the aggregate pinned/pageable host charge."""
    entries = grants.parse_grants(policy["grants"])
    big = policy.get("pc_big_deadline")
    if big is not None and now + (60 if starting else 0) >= big:
        raise rtx.ServiceError("placement turn reaches a PC-BIG window")
    by_uuid = {grants.uuid_key(g["uuid"]): g for g in entries}
    caps = {grants.uuid_key(k): v for k, v in policy["caps_gib"].items()}
    if not by_uuid or len(caps) != len(policy["caps_gib"]) or caps.keys() != by_uuid.keys():
        raise rtx.ServiceError("placement requires one cap per granted UUID")
    byte_caps = policy.get("caps_bytes")
    if (not isinstance(byte_caps, dict) or byte_caps.keys() != caps.keys() or
            any(type(v) is not int or v <= 0 for v in byte_caps.values())):
        raise rtx.ServiceError("placement requires integer byte caps for every touched GPU")
    for uuid, grant in by_uuid.items():
        cap = caps[uuid]
        number(cap, "placement cap", 0, 1024)
        if cap <= 0 or cap > grant["vram_cap_gib"]:
            raise rtx.ServiceError("placement cap exceeds grant or is disabled")
        # Tierstream recipe.cpp gib_bytes, used by place.cpp for budget_bytes: round half up, not round().
        cap_bytes = math.floor(cap * rtx.GIB + 0.5)
        grant_bytes = math.floor(grant["vram_cap_gib"] * rtx.GIB + 0.5)
        if byte_caps[uuid] > cap_bytes or byte_caps[uuid] > grant_bytes:
            raise rtx.ServiceError("placement byte cap exceeds GiB cap or grant")
        if not grants._active(grant["window"], now, starting=starting):
            raise rtx.ServiceError("placement device grant is not open or has expired")
    pinned, pageable = policy["host_pinned_gib"], policy["host_pageable_gib"]
    number(pinned, "placement pinned host", 0, grants.HOST_MAX_GIB)
    number(pageable, "placement pageable host", 0, 4096)
    charge = pinned + pageable
    host = policy.get("host_grant")
    if host is not None:
        grants.parse_host_grant(host)
        if not grants._active(host["window"], now, starting=starting):
            raise rtx.ServiceError("placement host grant is not open or has expired")
    if charge and (host is None or charge > host["host_gib"]):
        raise rtx.ServiceError("placement pinned + pageable exceeds host grant")
    if memory is not None and starting:
        free, commit = memory["free_physical_gib"], memory["free_commit_gib"]
        floor = policy["free_commit_floor_gib"]
        number(floor, "placement commit floor", 0, 4096)
        if not all(math.isfinite(v) for v in (free, commit)):
            raise rtx.ServiceError("placement host memory counters unavailable")
        if free * rtx.GIB < grants.HOST_START_BYTES or charge > free:
            raise rtx.ServiceError("placement requires MemAvailable >= 82 GB and the host charge")
        if commit - charge < floor:
            raise rtx.ServiceError("placement host charge would cross free commit floor")
    return by_uuid, caps


def acquire(policy, stack, *, lock_factory, gate_runner, verify, log, now, release=None):
    """The caller owns the ExitStack before acquisition: any lock/gate failure rolls back every held lock."""
    entries, _ = validate(policy, now=now, starting=True)
    info = []
    for uuid in sorted(entries):
        lock = lock_factory(uuid).acquire()
        if release is None:
            stack.callback(lock.close)
        else:
            stack.callback(release, lock)
    for uuid in sorted(entries):
        refused = gate_runner(entries[uuid]["gate"])
        log({"gate": refused or "open", "uuid": uuid})
        if refused:
            raise rtx.ServiceError(refused)
        info.append(verify(entries[uuid]["uuid"]))
    return info


class Watchdog(rtx.Watchdog):
    """No baseline subtraction, including WDDM N/A. Observer failure immediately stops the owned process."""

    def __init__(self, spec, pid, kill, log, *, observer=None, **kwargs):
        super().__init__(spec, pid, kill, log, **kwargs)
        self.policy = spec.placement_supervision
        self.observer = observer
        self._cleanup_errors = []

    def start(self):
        if self.observer is None:
            self.observer = gpu_counters.Observer()
        # Sample before starting the thread; never call the legacy start's device-total baseline.
        try:
            self.observer.sample(self.pid)
            self._thread.start()
        except BaseException as exc:
            observer = self.observer
            self.observer = None
            try:
                observer.close()
            except BaseException as close_error:
                # The helper records start failures and then calls stop. Keep shutdown errors for that guarded
                # teardown too, so a failed first child sample cannot swallow them or close the observer twice.
                self._cleanup_errors.append(close_error)
                raise BaseExceptionGroup("placement watchdog start and cleanup failed", [exc, close_error])
            raise
        return self

    def stop(self):
        errors, self._cleanup_errors = self._cleanup_errors, []
        try:
            super().stop()
        except BaseException as exc:
            errors.append(exc)
        observer = self.observer
        self.observer = None
        if observer is not None:
            try:
                observer.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("placement watchdog cleanup failed", errors)

    def check_memory(self):
        reason, memory = super().check_memory()
        if reason is None:
            try:
                validate(self.policy, now=self.clock())
                if memory["free_commit_gib"] < self.policy["free_commit_floor_gib"]:
                    reason = "placement free commit below floor"
                if not all(math.isfinite(v) for v in memory.values()):
                    reason = "placement host memory counters unavailable"
            except (ValueError, RuntimeError) as exc:
                reason = str(exc)
        return reason, memory

    def check(self):
        reason, memory = self.check_memory()
        line = {"t": round(self.clock(), 1), "pid": self.pid, **memory}
        if reason is None:
            try:
                _, caps = validate(self.policy, now=self.clock())
                sample = self.observer.sample(self.pid)
                line["vram_bytes"] = sample
                if not caps.keys() <= sample.keys():
                    raise gpu_counters.CounterError("granted GPU missing from attributable counters")
                for uuid, used in sample.items():
                    if type(used) is not int or used < 0:
                        raise gpu_counters.CounterError("per-process GPU memory is N/A")
                    if uuid not in caps and used > 0:
                        reason = "process active on an ungranted GPU"
                        break
                    limit = self.policy.get("caps_bytes", {}).get(uuid)
                    if limit is None and uuid in caps:
                        # Explicit microbenchmarks have grant caps, without a Tierstream placement document.
                        if getattr(self.spec, "placement", None) is not None:
                            raise gpu_counters.CounterError("placement integer budget unavailable")
                        limit = caps[uuid] * rtx.GIB
                    if uuid in caps and used > limit:
                        reason = "per-process VRAM exceeds placement cap"
                        break
            except gpu_counters.CounterError as exc:
                reason = str(exc)
        if reason:
            line["kill"] = reason
        self.log(line)
        return reason
