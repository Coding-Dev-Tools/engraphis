"""Engraphis — self-hosted AI memory system."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("engraphis")
except PackageNotFoundError:  # source tree without an installed distribution
    # Keep in step with [project] version in pyproject.toml — tests/test_packaging.py
    # pins the two together so a release cannot ship them out of sync.
    __version__ = "0.9.9"
