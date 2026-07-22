from __future__ import annotations

import hashlib

import pytest

from scripts.verify_release_artifacts import (
    ArtifactIncomplete,
    ArtifactMismatch,
    local_artifacts,
    validate_artifacts,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_verified_pypi_subset_can_be_safely_resumed(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-cp311-cp311-win_amd64.whl"
    sdist = tmp_path / "engraphis-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    local = local_artifacts(tmp_path)

    validate_artifacts(local, {wheel.name: _digest(b"wheel")}, exact=False)
    with pytest.raises(ArtifactIncomplete):
        validate_artifacts(local, {wheel.name: _digest(b"wheel")}, exact=True)
    validate_artifacts(local, local, exact=True)


def test_pypi_duplicate_name_is_skipped_only_when_digest_matches(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate")
    local = local_artifacts(tmp_path)

    with pytest.raises(ArtifactMismatch, match="digest conflicts"):
        validate_artifacts(local, {wheel.name: _digest(b"different")}, exact=False)


def test_unexpected_published_filename_fails_closed(tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate")
    local = local_artifacts(tmp_path)

    with pytest.raises(ArtifactMismatch, match="outside the candidate set"):
        validate_artifacts(
            local, {"engraphis-1.0.0-malicious.whl": _digest(b"candidate")},
            exact=False,
        )
