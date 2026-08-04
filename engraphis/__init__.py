"""Engraphis — self-hosted AI memory system."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

_SOURCE_VERSION = "1.4.5"

try:
    __version__ = _dist_version("engraphis")
    # Editable checkouts can retain stale dist-info until their next reinstall.  The
    # checked-in source version is authoritative for this runtime and must not advertise
    # the prior MCP contract merely because metadata has not been refreshed yet.
    if __version__ != _SOURCE_VERSION:
        __version__ = _SOURCE_VERSION
except PackageNotFoundError:  # source tree without an installed distribution
    # Keep in step with [project] version in pyproject.toml — tests/test_packaging.py
    # pins the two together so a release cannot ship them out of sync.
    __version__ = "1.4.5"
