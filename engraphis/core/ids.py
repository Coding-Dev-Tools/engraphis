"""ULID-style identifiers — time-sortable, dependency-free.

A ULID is a 26-char Crockford-base32 string: 48 bits of millisecond timestamp
followed by 80 bits of randomness. Because the timestamp is the high-order part,
plain lexicographic sorting of ids is also chronological — useful for cursors,
debugging, and stable ordering without a separate created_at lookup.

Prefixed ids (``mem_...``, ``repo_...``) make logs and traces self-describing.
"""
from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U

# Canonical prefixes for each entity kind.
PREFIXES = {
    "workspace": "ws",
    "repo": "repo",
    "session": "ses",
    "memory": "mem",
    "entity": "ent",
    "edge": "edg",
    "symbol": "sym",
    "event": "evt",
    "job": "job",
    "audit": "aud",
}


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid(timestamp_ms: int | None = None) -> str:
    """Return a 26-char, lexicographically sortable ULID."""
    ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    rand = secrets.randbits(80)
    return _encode(ts, 10) + _encode(rand, 16)


def new_id(kind: str) -> str:
    """Return a prefixed id, e.g. ``new_id("memory") -> 'mem_01J...'``.

    Unknown kinds fall back to using the kind itself as the prefix, so callers
    are never blocked by a missing entry in ``PREFIXES``.
    """
    prefix = PREFIXES.get(kind, kind)
    return f"{prefix}_{ulid()}"
