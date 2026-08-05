"""Verify a local Engraphis distribution set against immutable PyPI files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


class ArtifactMismatch(RuntimeError):
    """A published filename or digest conflicts with the candidate artifact set."""


class ArtifactIncomplete(RuntimeError):
    """The published set is valid so far but does not contain every candidate file."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep release metadata reads pinned to the configured PyPI origin."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def local_artifacts(directory: Path) -> dict[str, str]:
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
    return result


def pypi_artifacts(version: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
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
    local = local_artifacts(directory)
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
    count = verify(
        args.dist, args.version, exact=not args.allow_subset,
        retries=args.retries, delay=args.delay,
    )
    print("verified %d immutable PyPI artifact(s)" % count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
