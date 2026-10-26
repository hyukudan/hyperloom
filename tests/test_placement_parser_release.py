"""The pure Python argv mirror belongs to one audited native revision."""

from pathlib import Path
import json

import pytest

from hyperloom.recipes import runtime
from hyperloom.recipes.reader import RecipeError


@pytest.mark.parametrize("matching", [False, True])
def test_published_revision_must_match_parser_audit(matching):
    snapshot = json.loads((Path(__file__).parent / "fixtures/replica-parser-a919c257.json").read_bytes())
    data = dict(runtime.preset(), engine_sha=snapshot["revision"] if matching else "f" * 40)
    if matching:
        runtime.qualified(data)
    else:
        with pytest.raises(RecipeError, match="PL_E_CAPABILITY.*parser"):
            runtime.qualified(data)


def test_audit_tool_binds_snapshot_revision_to_runtime():
    from tools import check_replica_args
    snapshot = json.loads(check_replica_args.SNAPSHOT.read_bytes())
    check_replica_args.table(snapshot)
    snapshot["revision"] = "f" * 40
    with pytest.raises(AssertionError, match="revision drift"):
        check_replica_args.table(snapshot)


def test_3_fingerprint_covers_helpers_and_transitive_headers(tmp_path):
    from tools.check_replica_args import inventory, PARSER_HELPERS

    for name in ("apps/cuda_ds41_replica.cu", "apps/cuda_ds41_replica_serve.hpp", *PARSER_HELPERS):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// synthetic parser dependency\n", encoding="utf-8", newline="\n")
    main = tmp_path / "apps/cuda_ds41_replica.cu"
    main.write_text('#include "strix/first.hpp"\nconst auto parse_args = x;\n'
                    'if (a == "--phases") phases_arg = next();\n'
                    'const std::vector<std::string> launch_args;\n', encoding="utf-8", newline="\n")
    serve = tmp_path / "apps/cuda_ds41_replica_serve.hpp"
    serve.write_text('inline bool is_request_env() { return name == "ENV";\n}\n', encoding="utf-8", newline="\n")
    first, second = (tmp_path / "include/strix" / name for name in ("first.hpp", "second.hpp"))
    first.parent.mkdir(parents=True)
    first.write_text('#include "second.hpp"\n', encoding="utf-8", newline="\n")
    second.write_text("// nested parser rule\n", encoding="utf-8", newline="\n")
    original = inventory(tmp_path)
    # A rule change in any helper/header must invalidate the same pinned fingerprint as a main-parser edit.
    for name in original["files"]:
        path = tmp_path / name
        content = path.read_bytes()
        path.write_bytes(content + b"// changed refusal\n")
        assert inventory(tmp_path) != original, name
        path.write_bytes(content)
    second.write_bytes(second.read_bytes().replace(b"\n", b"\r\n"))
    assert inventory(tmp_path) == original  # the audit is deliberately LF-normalized
    second.unlink()
    assert inventory(tmp_path) != original
