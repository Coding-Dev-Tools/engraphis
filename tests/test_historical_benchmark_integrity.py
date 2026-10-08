"""Keep retained benchmark artifacts at their advertised byte identities."""
from __future__ import annotations

import hashlib
from pathlib import Path


def test_retained_v149_bytes_match_the_advertised_checksum():
    root = Path(__file__).resolve().parents[1]
    artifact = root / "docs/benchmark-evidence/offline-fixtures-v149.json"
    payload = artifact.read_bytes()
    expected = "d5d36c55c4303d77b161137521dd31f77f39b7a0c9e2fed3ddb63e303b12cc6d"
    assert payload.endswith(b"\n")
    assert hashlib.sha256(payload).hexdigest() == expected
    assert artifact.with_suffix(".json.sha256").read_text("ascii") == (
        f"{expected}  {artifact.name}\n"
    )
