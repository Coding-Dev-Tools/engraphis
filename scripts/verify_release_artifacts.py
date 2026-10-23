"""Verify a local Engraphis distribution set against immutable PyPI files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


#: The published distribution name.  The PyPI metadata URL is already bound to it,
#: so the local candidate filenames are bound to it as well.
DISTRIBUTION_NAME = "engraphis"

#: A release version is a stable semantic version; nothing else may be published.
_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")


class ArtifactMismatch(RuntimeError):
    """A published filename or digest conflicts with the candidate artifact set."""


class ArtifactIncomplete(RuntimeError):
    """The published set is valid so far but does not contain every candidate file."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep release metadata reads pinned to the configured PyPI origin."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def expected_member_names(version: str) -> tuple[str, str]:
    """Return the exact source-distribution name and the wheel name prefix.

    The wheel build tag varies with the interpreter and platform, so the wheel
    is bound by its ``<name>-<version>-`` prefix while the source distribution
    is bound by its complete name.
    """
    if not _VERSION.fullmatch(version):
        raise ArtifactMismatch("release version must be stable semantic version syntax")
    return (
        f"{DISTRIBUTION_NAME}-{version}.tar.gz",
        f"{DISTRIBUTION_NAME}-{version}-",
    )


def local_artifacts(directory: Path, version: str | None = None) -> dict[str, str]:
    files = sorted(
        path for path in Path(directory).iterdir()
        if path.is_file() or path.is_symlink()
    )
    if not files:
        raise ArtifactMismatch("the local distribution set is empty")
    result = {}
    for path in files:
        if path.is_symlink() or not path.is_file() or not (
            path.name.endswith(".whl") or path.name.endswith(".tar.gz")
        ):
            raise ArtifactMismatch("the distribution set contains an unsafe non-package file")
        if path.name in result:
            raise ArtifactMismatch("the distribution set contains duplicate filenames")
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if version is not None:
        _require_release_member_names(result, version)
    return result


def _require_release_member_names(local: dict[str, str], version: str) -> None:
    """Bind the candidate filenames to the exact release they claim to be.

    Without this binding the pre-publication check accepts any ``*.whl`` and
    ``*.tar.gz`` pair whenever the release is not on PyPI yet: the published
    set is empty, so nothing conflicts, and a stale or mis-versioned artifact
    would still be uploaded by the publish step.  The filenames are the only
    local statement of the version, so they are verified here.
    """
    sdist_name, wheel_prefix = expected_member_names(version)
    sdists = sorted(name for name in local if name.endswith(".tar.gz"))
    wheels = sorted(name for name in local if name.endswith(".whl"))
    if sdists != [sdist_name]:
        raise ArtifactMismatch(
            "the distribution set must contain exactly the source distribution "
            f"{sdist_name!r}, found {sdists or 'none'}"
        )
    if len(wheels) != 1 or not wheels[0].startswith(wheel_prefix):
        raise ArtifactMismatch(
            "the distribution set must contain exactly one wheel named "
            f"{wheel_prefix}*.whl, found {wheels or 'none'}"
        )


def pypi_artifacts(version: str) -> dict[str, str]:
    if not _VERSION.fullmatch(version):
        raise ArtifactMismatch("release version must be stable semantic version syntax")
    url = "https://pypi.org/pypi/engraphis/%s/json" % quote(version, safe="")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=30) as response:
            metadata = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise ArtifactMismatch("PyPI metadata request failed") from None
    except (OSError, ValueError, json.JSONDecodeError):
        raise ArtifactMismatch("PyPI metadata response was unavailable or malformed") from None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("urls"), list):
        raise ArtifactMismatch("PyPI returned malformed artifact metadata")
    result = {}
    for item in metadata["urls"]:
        if not isinstance(item, dict):
            raise ArtifactMismatch("PyPI returned malformed artifact metadata")
        filename = item.get("filename")
        digest = (item.get("digests") or {}).get("sha256")
        if (not isinstance(filename, str) or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ArtifactMismatch("PyPI returned malformed artifact metadata")
        if filename in result:
            raise ArtifactMismatch("PyPI returned a duplicate artifact filename")
        result[filename] = digest
    return result


def validate_artifacts(local: dict[str, str], published: dict[str, str], *,
                       exact: bool) -> None:
    unexpected = set(published) - set(local)
    if unexpected:
        raise ArtifactMismatch("PyPI contains filenames outside the candidate set")
    mismatched = [name for name, digest in published.items() if local[name] != digest]
    if mismatched:
        raise ArtifactMismatch("a published PyPI artifact digest conflicts with the candidate")
    missing = set(local) - set(published)
    if exact and missing:
        raise ArtifactIncomplete("PyPI has not published the complete candidate set")


def verify(directory: Path, version: str, *, exact: bool, retries: int = 1,
           delay: float = 0.0) -> int:
    local = local_artifacts(directory, version=version)
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        published = pypi_artifacts(version)
        try:
            validate_artifacts(local, published, exact=exact)
            return len(published)
        except ArtifactIncomplete:
            if attempt + 1 >= attempts:
                raise
            time.sleep(max(0.0, float(delay)))
    raise AssertionError("unreachable")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--allow-subset", action="store_true")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        count = verify(
            args.dist, args.version, exact=not args.allow_subset,
            retries=args.retries, delay=args.delay,
        )
    except (ArtifactIncomplete, ArtifactMismatch, OSError) as exc:
        # The publication gate fails closed: an unusable candidate set is a
        # release-stopping error, never a warning that lets a partial upload
        # continue.
        print("release artifact verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified %d immutable PyPI artifact(s)" % count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
