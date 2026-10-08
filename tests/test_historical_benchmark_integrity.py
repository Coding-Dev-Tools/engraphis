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


def test_retained_v142_client_checksum_resolves_the_original_payload():
    """The renamed client snapshot must keep its original bytes and valid sidecar."""
    root = Path(__file__).resolve().parents[1]
    artifact = root / (
        "docs/benchmark-evidence/offline-fixtures-v142-client-9449d94b7e80.json"
    )
    expected = "9449d94b7e8085ac6030102a4e00ba109dc81e90602a1b1acc832f6675d61754"
    sidecar = artifact.with_suffix(".json.sha256")
    digest, filename = sidecar.read_text("ascii").split()
    assert filename == artifact.name
    assert sidecar.parent / filename == artifact
    assert digest == hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
