"""Fidelity against a pinned native replica reference, through its serve spool only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid

from . import gpu_counters, replica_spool
from .recipes import runtime


def _end(row, steps):
    tokens = row.get("toks")
    if (not isinstance(tokens, list) or len(tokens) != steps + 1
            or any(type(t) is not int or not 0 <= t <= 0xffffffff for t in tokens)
            or type(row.get("position")) is not int or row["position"] < 0
            or type(row.get("next_token")) is not int or not 0 <= row["next_token"] <= 0xffffffff
            or not isinstance(row.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{1,16}", row["digest"])):
        raise ValueError("invalid replica reference tokens or canonical state")
    return {key: row[key] for key in ("toks", "position", "next_token", "digest")}


def _oracle(directory, service):
    from .rtx import ServiceError
    try:
        root = Path(directory).resolve()
        raw = (root / "replica-oracle.json").read_bytes()
        document = json.loads(raw)
        data = runtime.preset()
        runtime.qualified(data)
        if (hashlib.sha256(raw).hexdigest() != (data.get("fidelity") or {}).get("oracle_sha256")
                or document["schema"] != "hyperloom/replica-oracle-v1"
                or document["engine_sha"] != data["engine_sha"]
                or document["engine_sha"] != service.get("engine_sha")):
            raise ValueError("reference and running release differ")
        paths = {}
        for name in ("state", "sequences", "expected"):
            pin = document["files"][name]
            path = (root / pin["file"]).resolve()
            if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != pin["sha256"]:
                raise ValueError("reference file identity differs: " + name)
            paths[name] = path
        for name in ("chat_prompt", "chat_steps"):
            if type(document[name]) is not int or not 1 <= document[name] <= 0xffffffff:
                raise ValueError("positive " + name + " required")
        chats = document["chats"]
        if (not isinstance(chats, list) or not chats or len(set(chats)) != len(chats)
                or any(type(c) is not int or c < 0 for c in chats)):
            raise ValueError("nonempty distinct chat indices required")
        sequences = [line.split("\t", 1)[1] for line in paths["sequences"].read_text().splitlines() if "\t" in line]
        for chat in chats:
            ids = [int(token) for token in sequences[chat].split(",")]
            if len(ids) < document["chat_prompt"] or any(t < 0 or t > 0xffffffff for t in ids):
                raise ValueError("reference prompt is too short or invalid")
        expected = {}
        for line in paths["expected"].read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("invalid reference event")
            if row.get("event") != "chat_end":
                continue
            key = row["key"]
            if key in expected:
                raise ValueError("duplicate reference tokens")
            expected[key] = _end(row, document["chat_steps"])
        if set(expected) != {f"plain:0#0-{c}" for c in chats}:
            raise ValueError("reference must contain exactly every selected chats0 arm")
        return document, paths, expected
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ServiceError("replica oracle unavailable or invalid: " + str(exc)
                           + "; obtain the release-bound native reference bundle from the engine owner") from exc


def fidelity_gate(service, prompts):
    from .rtx import ServiceError, visibility
    oracle, paths, expected = _oracle(prompts, service)
    policy = service.get("placement_supervision") or {}
    touched = {d["uuid"].lower() for d in (service.get("placement") or {}).get("devices", [])}
    if (not touched or touched != {u.lower() for u in policy.get("caps_gib", {})}
            or not service.get("pid") or not service.get("spool")):
        raise ServiceError("replica fidelity needs an owned spool, pid and supervision for every touched device")
    rows, samples, observer = [], [], None
    result = {"gate": "replica-greedy-tokens-v1", "pass": False, "rows": rows,
              "visibility": {"only_granted": False, "samples": samples}, "engine_sha": oracle["engine_sha"],
              "quality_gate": None, "quality_gate_why": "exact native token fidelity; model quality qualification is separate"}

    def seen():
        sample = visibility(service, observer=observer)
        samples.append(sample)
        # Zero bytes cannot prove visibility on a touched device. Device totals are never substituted.
        if not sample["only_granted"] or not touched <= set(sample["visible_on"]):
            raise ServiceError("replica is not attributable on exactly its touched devices")
        result["visibility"].update(sample)

    def alive():
        try:
            state = json.loads((Path(service["spool"]).parent / "state.json").read_text(encoding="utf-8"))
            return state.get("state") == "ready" and state.get("pid") == service["pid"]
        except (OSError, ValueError):
            return False

    try:
        observer = gpu_counters.Observer()
        seen()
        client = replica_spool.Client(service["spool"], alive=alive)
        ready = client.ready()
        load_id = (service.get("ready") or {}).get("load_id")
        if (not ready or type(load_id) is not int or ready.get("load_id") != load_id
                or ready.get("pid") != service["pid"] or not client.health()):
            raise ServiceError("replica spool readiness/ping does not match the owned load")
        # Placement owns drafter selection; its admitted lines survive rendering and service-record persistence.
        speculative = any(line.get("component") == "drafter"
                          for device in service["placement"]["devices"] for line in device.get("lines", []))
        phases = "chats0,schats0" if speculative else "chats0"
        # The native worker flushes but retains its JSON stream until the next request. Keep its evidence under
        # the owned runtime directory: deleting it here can fail on Windows while that stream is still open.
        output = Path(service["spool"]).resolve() / ("fidelity-" + uuid.uuid4().hex + ".jsonl")
        result["evidence"] = str(output)
        reply = client.request(["--import-state", str(paths["state"]), "--seqs-file", str(paths["sequences"]),
                                "--chats", ",".join(map(str, oracle["chats"])),
                                "--chat-prompt", str(oracle["chat_prompt"]), "--chat-steps", str(oracle["chat_steps"]),
                                "--phases", phases, "--qual", "--json", str(output)])
        seen()
        if (reply.get("rc") != 0 or reply.get("fatal") is not False or reply.get("failures") != 0
                or reply.get("load_id") != load_id):
            raise ServiceError("replica rejected or failed its native fidelity request")
        wanted = dict(expected)
        if speculative:
            wanted.update({key.replace("plain:0", "spec:schats0", 1): tokens for key, tokens in expected.items()})
        found = set()
        for line in output.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ServiceError("invalid replica event")
            if row.get("pass") is False or row.get("event") == "error":
                raise ServiceError("replica reported a failed native gate")
            if row.get("event") != "chat_end":
                continue
            key = row.get("key")
            if key not in wanted or key in found:
                raise ServiceError("unexpected or duplicate replica token arm")
            found.add(key)
            equal = _end(row, oracle["chat_steps"]) == wanted[key]
            rows.append({"key": key, "tokens": len(wanted[key]["toks"]), "equal": equal})
        if found != wanted.keys() or not rows or not all(row["equal"] for row in rows):
            raise ServiceError("replica tokens differ from the complete native reference")
        if not client.health():
            raise ServiceError("replica failed its post-gate ping")
        result["pass"] = True
    except (OSError, ValueError, RuntimeError) as exc:
        result["why"] = str(exc)
    finally:
        if observer is not None:
            observer.close()
    return result
