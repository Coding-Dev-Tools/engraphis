"""Pin every installed distribution except an unpublished candidate.

The candidate itself is audited against OSV. PyPI's version-specific JSON API
cannot audit a version that has not been published yet, so its separate audit
uses this exact installed set with only the validated candidate excluded. It
fails if another installed distribution cannot be audited through PyPI.
"""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import sys
from pathlib import Path


def _canonical_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError(f"invalid installed distribution name: {name!r}")
    return re.sub(r"[-_.]+", "-", name).lower()


def published_pins(
    site_packages: Path, *, candidate_name: str, candidate_version: str
) -> list[str]:
    """Return every installed distribution except the exact candidate as pins."""
    if not site_packages.is_dir():
        raise ValueError(f"installed site-packages directory is missing: {site_packages}")
    candidate = _canonical_name(candidate_name)
    seen: dict[str, str] = {}
    for distribution in metadata.distributions(path=[str(site_packages)]):
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            raise ValueError("installed distribution is missing a name or version")
        normalized = _canonical_name(name)
        if not re.fullmatch(r"[0-9][A-Za-z0-9!+._-]*", version):
            raise ValueError(f"invalid installed version for {name!r}: {version!r}")
        if normalized in seen:
            raise ValueError(f"duplicate installed distribution: {normalized}")
        seen[normalized] = version

    metadata_entries = [
        *site_packages.glob("*.dist-info"),
        *site_packages.glob("*.egg-info"),
    ]
    if len(metadata_entries) != len(seen):
        raise ValueError("installed distribution metadata was skipped or is ambiguous")

    if candidate not in seen:
        raise ValueError(f"candidate distribution is missing: {candidate_name}")
    if seen[candidate] != candidate_version:
        raise ValueError(
            f"installed {candidate_name} version {seen[candidate]} does not match "
            f"source version {candidate_version}"
        )
    del seen[candidate]
    if not seen:
        raise ValueError("no installed published distributions remain to audit")
    return [f"{name}=={version}" for name, version in sorted(seen.items())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_packages", type=Path)
    args = parser.parse_args()
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10 runner fallback
            import tomli as tomllib

        with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)["project"]
        pins = published_pins(
            args.site_packages,
            candidate_name=project["name"],
            candidate_version=project["version"],
        )
    except (OSError, KeyError, ValueError) as error:
        print(f"installed distribution audit cannot proceed: {error}", file=sys.stderr)
        return 1
    print("\n".join(pins))
    print(
        f"PyPI audit pins {len(pins)} installed distributions; "
        f"OSV audits them and {project['name']}=={project['version']}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
