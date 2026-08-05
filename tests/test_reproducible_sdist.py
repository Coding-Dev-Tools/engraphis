"""Release guard for deterministic source-distribution metadata."""
from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from scripts.normalize_sdist import NormalizationError, normalize_sdist, source_date_epoch


def _archive(path: Path, *, gzip_mtime: int, member_mtime: int, unsafe: bool = False) -> None:
    with path.open("wb") as raw, \
            gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=gzip_mtime,
            ) as compressed, \
            tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
        directory = tarfile.TarInfo("engraphis-1.0.0/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = member_mtime
        directory.uid = 1000
        directory.gid = 1000
        directory.uname = "builder"
        directory.gname = "builder"
        archive.addfile(directory)

        name = "engraphis-1.0.0/../escape" if unsafe else "engraphis-1.0.0/module.py"
        payload = b"VALUE = 1\n"
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        member.mode = 0o644
        member.mtime = member_mtime
        member.uid = 1000
        member.gid = 1000
        member.uname = "builder"
        member.gname = "builder"
        archive.addfile(member, io.BytesIO(payload))


def test_normalized_sdists_are_byte_identical_and_idempotent(tmp_path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _archive(first, gzip_mtime=100, member_mtime=200)
    _archive(second, gzip_mtime=300, member_mtime=400)
    epoch = 1_700_000_000

    first_digest = normalize_sdist(first, epoch=epoch)
    second_digest = normalize_sdist(second, epoch=epoch)

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest
    assert int.from_bytes(first.read_bytes()[4:8], "little") == epoch
    unchanged = first.read_bytes()
    assert normalize_sdist(first, epoch=epoch) == first_digest
    assert first.read_bytes() == unchanged
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.mtime == epoch for member in members)
        assert all(member.uid == member.gid == 0 for member in members)
        assert all(not member.uname and not member.gname for member in members)
        assert archive.extractfile("engraphis-1.0.0/module.py").read() == b"VALUE = 1\n"


def test_normalizer_rejects_unsafe_members_without_replacing_archive(tmp_path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    _archive(archive, gzip_mtime=100, member_mtime=200, unsafe=True)
    before = archive.read_bytes()

    with pytest.raises(NormalizationError, match="unsafe member path"):
        normalize_sdist(archive, epoch=1_700_000_000)

    assert archive.read_bytes() == before


@pytest.mark.parametrize("value", ["", "invalid", "-1", str(1 << 32)])
def test_source_date_epoch_is_strict(value) -> None:
    with pytest.raises(NormalizationError):
        source_date_epoch(value)
