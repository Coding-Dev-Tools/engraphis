"""Engraphis — self-hosted AI memory system."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

_SOURCE_VERSION = "1.7.1"

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
    __version__ = "1.7.1"


def _default_memory_engine_factory(**kwargs):
    from engraphis.factory import create_memory_engine as factory

    return factory(**kwargs)


def create_memory_engine(*args, **kwargs):
    """Public lazy wrapper around the v2 outer composition root."""
    from engraphis.factory import create_memory_engine as factory

    return factory(*args, **kwargs)


from engraphis.core.engine import (  # noqa: E402
    MemoryEngine,
    configure_engine_factory,
)

configure_engine_factory(_default_memory_engine_factory)

__all__ = ["MemoryEngine", "create_memory_engine", "__version__"]
