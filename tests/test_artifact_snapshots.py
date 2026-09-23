"""Artifact decisions and receipts refer to the bytes that passed verification."""
import hashlib
import json
from pathlib import Path

import pytest

from eval.benchmark import read_artifact_snapshot


def test_snapshot_remains_bound_when_the_file_is_replaced_after_read(tmp_path, monkeypatch):
    path = tmp_path / "artifact.json"
    original = b'{"observed": 7}'
    digest = hashlib.sha256(original).hexdigest()
    path.write_bytes(original)
    path.with_suffix(".json.sha256").write_text(digest)
    read_bytes = Path.read_bytes
    reads = []

    def replace(path_to_read):
        payload = read_bytes(path_to_read)
        if path_to_read == path:
            reads.append(path)
            replacement = path.with_suffix(".replacement")
            replacement.write_text('{"observed": 999}')
            replacement.replace(path)
        return payload

    monkeypatch.setattr(Path, "read_bytes", replace)
    assert read_artifact_snapshot(path) == ({"observed": 7}, digest)
    assert len(reads) == 1
    assert json.loads(path.read_text()) == {"observed": 999}


@pytest.mark.parametrize("checksum", [None, "", "\n", "0" * 64])
def test_snapshot_rejects_missing_empty_or_wrong_checksums(tmp_path, checksum):
    path = tmp_path / "artifact.json"
    path.write_text('{"observed": 7}')
    if checksum is not None:
        path.with_suffix(".json.sha256").write_text(checksum)
    with pytest.raises(ValueError, match="checksum"):
        read_artifact_snapshot(path)


def test_snapshot_requires_an_object_after_checksum_verification(tmp_path):
    path = tmp_path / "artifact.json"
    payload = b'[]'
    path.write_bytes(payload)
    path.with_suffix(".json.sha256").write_text(hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError, match="JSON object"):
        read_artifact_snapshot(path)
