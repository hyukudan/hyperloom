"""Offline audit of the pinned replica parser and the documented pure-render inventory.

Run with --source PATH/TO/REPLICA to verify the native source; --write refreshes the documentation table only.
The checked-in snapshot is deliberately not refreshed automatically on a source mismatch.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hyperloom.recipes import replica_args, runtime


SNAPSHOT = ROOT / "tests/fixtures/replica-parser-a919c257.json"
DOCUMENT = ROOT / "docs/placement-render.md"
START = "<!-- replica-parser-table:start -->"
END = "<!-- replica-parser-table:end -->"
PARSER_HELPERS = (
    "src/deepseek41_attention_state.cpp",  # imported state, including its in-file read/hash helpers
    "src/deepseek41_model.cpp",  # pinned_file() used by the receipt check
    "src/ds41_dspark_driver.cpp",  # prior/table/cost configuration
    "src/ds41_dspark_vocab.cpp",  # vocabulary spec (placement-owned, still fingerprinted)
    "src/cuda/ds41_attention.cu",  # argument-time dispatch setters
    "src/cuda/ds41_core_norm.cu",
    "src/cuda/ds41_core_mma.cu",
    "src/cuda/ds41_router_v2.cu",
    "src/cuda/ds41_tier_site.cu",  # known row/predict limits and external-loader boundary
)


def fingerprints(source):
    """Hash whole parsing helpers and their recursive local quoted includes, normalized to LF.

    New calls/includes in any of these files invalidate the pin too: review must extend this explicit helper
    inventory when a new out-of-line callee is introduced. External headers are recorded, never silently read
    from another checkout, CUDA installation or an unpinned external placement loader.
    """
    source = source.resolve()
    pending = [source / name for name in ("apps/cuda_ds41_replica.cu", "apps/cuda_ds41_replica_serve.hpp",
                                         *PARSER_HELPERS)]
    files, external = {}, set()
    while pending:
        path = pending.pop().resolve()
        name = path.relative_to(source).as_posix()
        if name in files:
            continue
        text = path.read_text(encoding="utf-8")
        files[name] = hashlib.sha256(text.encode()).hexdigest()
        for include in re.findall(r'^\s*#include\s*"([^"]+)"', text, re.MULTILINE):
            candidates = [(path.parent / include).resolve(), (source / "include" / include).resolve()]
            local = next((p for p in candidates if p.is_relative_to(source) and p.is_file()), None)
            if local is None:
                external.add(include)
            else:
                pending.append(local)
    return dict(sorted(files.items())), sorted(external)


def inventory(source):
    text = (source / "apps/cuda_ds41_replica.cu").read_text(encoding="utf-8")
    parser = text.split("const auto parse_args =", 1)[1].split("const std::vector<std::string> launch_args", 1)[0]
    options = {}
    for option, branch in re.findall(r'if \(a == "(--[^"]+)"\)([^\n]*)', parser):
        options[option] = ("u32" if "u32()" in branch or "std::stoul(" in branch else
                           "float" if "std::stof(" in branch else "double" if "std::stod(" in branch else
                           "long" if "std::stol(" in branch else "value" if "next()" in branch else "flag")
    files, external = fingerprints(source)
    option_lines = {option: text[:text.index('a == "' + option + '"')].count("\n") + 1 for option in options}
    serve = (source / "apps/cuda_ds41_replica_serve.hpp").read_text(encoding="utf-8")
    env = serve.split("inline bool is_request_env", 1)[1].split("\n}", 1)[0]
    return dict(revision="a919c257c511b2ae4688737f2127e02eecee2255", files=files, external_includes=external,
                options=options, option_lines=option_lines,
                request_env=sorted(re.findall(r'name == "([^"]+)"', env)))


def table(snapshot):
    assert snapshot["revision"] == replica_args.AUDITED_REVISION, "runtime/native parser revision drift"
    options = snapshot["options"]
    owned = runtime.SIZING | runtime.SUGAR
    accepted = replica_args.VALUES | replica_args.FLAGS
    assert accepted == options.keys() - owned, "native/render inventory drift"
    assert replica_args.REQUEST_ENV == set(snapshot["request_env"]), "native request environment drift"
    for option in accepted:
        assert (option in replica_args.FLAGS) == (options[option] == "flag"), option
        assert (option in replica_args.UINTS) == (options[option] == "u32"), option
        assert replica_args.FLOATS.get(option) == ({"float": 32, "double": 64}.get(options[option])), option
    lines = [START, "| Native option | Source at a919c257 | Native conversion | Pure rendering rule |", "|---|---|---|---|"]
    for option, kind in sorted(options.items()):
        if option in owned:
            rule = "Placement-owned; always PL_E_CONFLICT (including repeated / equals spellings)."
        elif option in replica_args.UINTS:
            rule = "U32; no additional parser range."
            if option == "--steps":
                rule = "U32; steps + 16 <= INT_MAX; >= 64 for exact/pressure/wforce/spec phases."
            elif option in ("--chat-prompt", "--chat-steps"):
                rule = "U32; chat-prompt + chat-steps + 16 <= INT_MAX when chats are selected."
        elif option in replica_args.FLOATS:
            rule = "F32." if kind == "float" else "F64; no additional parser range."
        elif option in replica_args.ENUMS:
            rule = "One of: " + ", ".join(sorted(replica_args.ENUMS[option])) + "."
        elif option in replica_args.INPUTS and option != "--controller-prior":
            rule = "Readable regular input file; contents engine-owned."
        elif option in replica_args.OUTPUTS:
            rule = "Writable regular output or writable existing parent; never created by rendering; actual open/rename engine-owned."
        elif option in replica_args.FLAGS:
            rule = "Flag; takes no value."
        else:
            rule = {
                "--window": "controller, probe, or fixed:U32.",
                "--controller-prior": "none, builtin[:CAP], or readable FILE[:CAP]; decimal CAP uses U32.",
                "--controller-costs": "none or 6/7 comma-separated F64 costs; first 6 > 0 after float(ms / 1000); requires prior != none.",
                "--seqmod": "First two nonempty colon-separated fields use U32; extra fields ignored, as native.",
                "--chats": "Each nonempty comma-separated field uses U32; file-dependent index bound engine-owned.",
                "--phases": "Native grammar; every K <= 64; admitted max_rows x K <= 64 and K <= max(cached --predict, 8) when known; per-occurrence required inputs; smoke refused for full-model placement.",
                "--env": "NAME=VALUE; NAME is " + " or ".join(sorted(replica_args.REQUEST_ENV)) + "; arbitrary/empty VALUE.",
                "--probe-phase": "Opaque comma-separated phase filter; native imposes no enum or numeric syntax.",
                "--serve": "Spool path; helper-owned in service rendering.",
            }[option]
        if option in ("--serve-idle", "--serve-max"):
            rule += " Helper-owned in service rendering."
        lines.append(f"| `{option}` | apps/cuda_ds41_replica.cu:{snapshot['option_lines'][option]} | {kind} | {rule} |")
    return "\n".join([*lines, END])


def check_document():
    expected = table(json.loads(SNAPSHOT.read_text(encoding="utf-8")))
    actual = DOCUMENT.read_text(encoding="utf-8")
    assert START in actual and actual.split(START, 1)[1].split(END, 1)[0] == expected.split(START, 1)[1].split(END, 1)[0], "replica parser documentation drift"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if args.source:
        assert inventory(args.source) == snapshot, "replica source changed: review parser rules and explicitly repin the snapshot"
    if args.write:
        text = DOCUMENT.read_text(encoding="utf-8")
        before, rest = text.split(START, 1)
        _, after = rest.split(END, 1)
        DOCUMENT.write_text(before + table(snapshot) + after, encoding="utf-8", newline="\n")
    check_document()
    print(f"{len(snapshot['options'])} native options / {len(snapshot['files'])} source files audited; "
          "parser snapshot and documentation agree")


if __name__ == "__main__":
    main()
