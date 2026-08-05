"""Normalize source-distribution metadata for byte-reproducible release builds."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import os
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Optional


_MAX_GZIP_EPOCH = (1 << 32) - 1


class NormalizationError(ValueError):
    """Raised when an archive cannot be normalized safely."""


def source_date_epoch(value: Optional[str] = None) -> int:
    """Return a gzip-compatible SOURCE_DATE_EPOCH."""

    raw = os.environ.get("SOURCE_DATE_EPOCH", "") if value is None else value
    try:
        epoch = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise NormalizationError("SOURCE_DATE_EPOCH must be an integer") from exc
    if not 0 <= epoch <= _MAX_GZIP_EPOCH:
        raise NormalizationError(
            "SOURCE_DATE_EPOCH must be between 0 and %d" % _MAX_GZIP_EPOCH
        )
    return epoch


def _safe_member(member: tarfile.TarInfo, archive: Path) -> None:
    name = member.name.replace("\\", "/")
    # Tar directory members conventionally end in exactly one slash. Remove that
    # representation-only suffix before checking components; empty interior
    # components and traversal markers remain unsafe.
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    if (
        not name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise NormalizationError(
            "%s has an unsafe member path: %r" % (archive.name, member.name)
        )
    if not (member.isfile() or member.isdir()):
        raise NormalizationError(
            "%s has an unsupported member type: %r" % (archive.name, member.name)
        )


def normalize_sdist(path: Path, *, epoch: int) -> str:
    """Atomically normalize one ``.tar.gz`` archive and return its SHA-256."""

    path = Path(path)
    if not path.is_file() or not path.name.endswith(".tar.gz"):
        raise NormalizationError("expected an existing .tar.gz source distribution")
    if not 0 <= epoch <= _MAX_GZIP_EPOCH:
        raise NormalizationError("normalization epoch is outside the gzip timestamp range")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(path, "r:gz") as source, \
                temporary.open("wb") as raw_output, \
                gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=epoch,
                ) as compressed, \
                tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
                encoding="utf-8",
                ) as target:
            members = source.getmembers()
            for original in members:
                _safe_member(original, path)
            for original in sorted(members, key=lambda item: item.name):
                member = copy.copy(original)
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = epoch
                member.pax_headers = {}
                payload = source.extractfile(original) if original.isfile() else None
                try:
                    target.addfile(member, payload)
                finally:
                    if payload is not None:
                        payload.close()
        try:
            os.chmod(temporary, 0o644)
        except OSError:
            pass
        os.replace(temporary, path)
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise NormalizationError("%s could not be normalized" % path.name) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument(
        "--source-date-epoch",
        help="integer timestamp; defaults to SOURCE_DATE_EPOCH",
    )
    args = parser.parse_args(argv)
    try:
        epoch = source_date_epoch(args.source_date_epoch)
        for archive in args.archives:
            digest = normalize_sdist(archive, epoch=epoch)
            print("normalized %s sha256=%s" % (archive.name, digest))
    except NormalizationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
