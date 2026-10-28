"""CPU-only G6 oracle generator; see fixtures/render/README.md."""
from __future__ import annotations

if __package__:
    from .portable_fixtures import read_fixture, sanitize
else:
    from portable_fixtures import read_fixture, sanitize
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


ORACLE = "eb9ae96"
CAPS = (None, 90, 8)


def capture(root):
    # The caller starts a fresh interpreter: imports must come from the requested checkout.
    sys.path.insert(0, str(root))
    from hyperloom import config, engines, orchestrate, render, rtx
    from hyperloom.placement import ds41

    fixtures = root / "tests/fixtures"
    obj = json.loads((root / "examples/cluster.example.json").read_text(encoding="utf-8"))
    for mid in ("halo-1", "halo-2"):
        obj["assets"][mid]["calibration_tokens"]["control_path"] = str(fixtures / "render/calibration.tokens")
        obj["assets"][mid]["mm_model"] = {"path": "/models/minimax", "bytes": 0, "sha256": "0" * 64}
    keys = {mid: "/opt/hyperloom/secrets/engine.key" for mid in ("halo-1", "halo-2")}
    presets = {}
    for path in sorted((root / "hyperloom/presets").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") == "hyperloom/prefill-service-v1":
            continue
        if "argv" not in raw:
            continue
        name = path.stem
        data = config.preset(name)
        adapter = engines.adapter(config.family(name))
        settings = {"preset": name, "release_id": "0" * 12}
        settings.update({"nodes": ["halo-1", "halo-2"]} if adapter.topology == "replicas"
                        else {"rank0": "halo-1", "rank1": "halo-2"})
        if adapter.key_file:
            settings["api_key_files"] = keys
        obj["profiles"][name] = settings
        presets[name] = data

    # A fixed, synthetic operator reservation and model header: no clock, model reads or GPU probes.
    uuid = "GPU-00000000-0000-0000-0000-000000000001"
    workstation = next(m for m in obj["machines"] if m["id"] == "workstation")
    workstation["devices"] = [{"id": "rtx-pro", "type": "nvidia", "uuid": uuid}]
    obj["pc"]["cuda"] = {"pro_uuid": uuid, "reservation": {
        "owner": "operator", "approval": "synthetic PC-BIG", "expires_at": "2033-05-18T03:48:20Z",
        "vram_cap_gib": 90, "host_gib": 72, "free_commit_floor_gib": 4}}
    obj["assets"]["workstation"] = {"ds41_model": {"path": "C:/hyperloom/assets/ds41.gguf",
                                                   "bytes": 0, "sha256": "0" * 64}}
    obj["profiles"]["ds41-rtx"] = dict(obj["profiles"]["ds41-pair-long"],
        prefill_service={"preset": "rtx-prefill-ds41", "machine": "workstation", "device": "rtx-pro"})
    sizes = json.loads(read_fixture(fixtures / "vramplan/ds41-sizes.json"))
    cluster = config.parse(obj)
    rows = []
    for name in sorted(cluster.profiles):
        kind = cluster.profiles[name].get("preset", name)
        data = config.preset(kind)
        adapter = engines.adapter(config.family(kind))
        identity = {"release_id": cluster.profiles[name]["release_id"], "engine_sha": data["engine_sha"],
                    "binary": data["binary"], "components": engines.components(data),
                    "options": engines.recipe_options(data, config.family(kind)),
                    "default_configure": engines.lock()["toolchains"]["hip_configure"],
                    "targets": list(adapter.targets), "trellis": adapter.trellis is not None,
                    "required_flags": orchestrate._required_flags(kind, list(adapter.targets))}
        for cap in CAPS:
            row = {"profile": name, "vram_cap_gib": cap, "release": identity}
            try:
                router = "halo-1" if config.family(kind) == "minimax-pair" else "local"
                if cap is not None and "prefill_service" not in cluster.profiles[name]:
                    # Exercise up's real preflight refusal, before any lease or transport.
                    with patch.object(orchestrate, "_state_path", side_effect=AssertionError("unexpected launch state")), \
                            patch.object(orchestrate, "_client", side_effect=AssertionError("unexpected transport")):
                        orchestrate.up(cluster, name, router=router, vram_cap_gib=cap)
                    raise AssertionError("cap unexpectedly accepted without a service")
                row["processes"] = [asdict(p) for p in render.render_processes(cluster, name)]
                if "prefill_service" in cluster.profiles[name]:
                    with patch.dict(os.environ, {}, clear=True), patch.object(ds41, "sizes", return_value=sizes):
                        spec = rtx.render_service(cluster, name, api_key="a" * 64, handoff_api_key="b" * 64,
                                                  free_physical_gib=100, now=2000000000, vram_cap_gib=cap)
                    service = rtx.service_preset("rtx-prefill-ds41")
                    row["service"] = {"argv": spec.argv, "env": spec.env, "engine_sha": service["engine_sha"],
                                      "binary": service["binary"], "binary_sha256": spec.binary_sha256}
            except (ValueError, RuntimeError) as exc:
                row = {"profile": name, "vram_cap_gib": cap, "release": identity,
                       "refusal": {"type": type(exc).__name__, "message": str(exc)}}
            rows.append(row)
    return {"oracle": ORACLE, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw", action="store_true", help="local byte comparison only; never commit this output")
    args = parser.parse_args()
    result = capture(args.source.resolve())
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    args.output.write_text(text if args.raw else sanitize(text), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
