"""Synthetic per-test identities, grants and cached placement files."""

import hashlib
import json
import sys

import pytest

from hyperloom.recipes import content_cache, runtime
from hyperloom import rtx

from tests.test_recipes_grants import window


def grant(identity, cap=90):
    return dict(uuid=identity, vram_cap_gib=cap, approval="test", window=window(), gate=[sys.executable, "fake"])


@pytest.fixture
def identity():
    import uuid
    return "GPU-" + str(uuid.uuid4())


@pytest.fixture
def files(tmp_path, identity):
    model = tmp_path / "model"
    model.write_bytes(b"model")
    stat = model.stat()
    manifest = {"schema": content_cache.MANIFEST_SCHEMA, "files": [dict(path=str(model), size=stat.st_size,
        mtime_ns=stat.st_mtime_ns, sha256="a" * 64, header_sha256="b" * 64)]}
    receipts = b'{"schema":"tierstream/receipts-v1"}\n'
    document = dict(schema="tierstream/placement-v1", engine="strix-cuda-ds41-replica",
                    receipts_sha256=hashlib.sha256(receipts).hexdigest(),
                    devices=[dict(kind="cuda", uuid=identity, pci=f"{0:04x}:{1:02x}:{0:02x}.{0}",
                                  role="anchor", budget_bytes=10 * rtx.GIB)])
    result = runtime.Files(*(tmp_path / n for n in ("placement.json", "manifest.json", "receipts.json")))
    result.placement.write_bytes(json.dumps(document).encode())
    result.manifest.write_bytes(json.dumps(manifest).encode())
    result.receipts.write_bytes(receipts)
    return result, document
