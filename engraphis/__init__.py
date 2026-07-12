"""Engraphis — self-hosted AI memory system."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("engraphis")
except PackageNotFoundError:  # source tree without an installed distribution
    __version__ = "0.2.0"
