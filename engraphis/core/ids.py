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
from typing import Optional

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U
_MAX_TIMESTAMP_MS = 1 << 48


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
    "device": "dev",
    "receipt": "rcpt",
    "vault": "vlt",
    "source": "src",
}


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid(timestamp_ms: Optional[int] = None) -> str:
    """Return a 26-char, lexicographically sortable ULID."""
    if timestamp_ms is None:
        ts = int(time.time() * 1000)
    elif isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise ValueError("timestamp_ms must be an integer in range [0, 2**48)")
    else:
        ts = timestamp_ms
    if not 0 <= ts < _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp_ms must be an integer in range [0, 2**48)")
    rand = secrets.randbits(80)
    return _encode(ts, 10) + _encode(rand, 16)


def new_id(kind: str, *, allow_unsafe: bool = False, unsafe: bool | None = None) -> str:
    """Return a prefixed id, e.g. ``new_id("memory") -> 'mem_01J...'``.

    Known kinds (the keys of ``PREFIXES``) always work. Unknown kinds raise
    ``ValueError`` unless explicitly opted out with ``allow_unsafe=True`` (or
    the ``unsafe=True`` alias), in which case the kind itself is used as the
    prefix for forward compatibility.
    """
    if unsafe is not None:
        allow_unsafe = allow_unsafe or bool(unsafe)
    if kind not in PREFIXES:
        if not allow_unsafe:
            raise ValueError(
                f"unknown id kind {kind!r} (expected one of {sorted(PREFIXES)}; "
                "pass allow_unsafe=True to use it as a literal prefix)"
            )
        return f"{kind}_{ulid()}"
    return f"{PREFIXES[kind]}_{ulid()}"


def assert_id_kind(value: str, kind: str) -> str:
    """Return *value* if it carries the prefix for *kind*, else raise."""
    expected = PREFIXES[kind]
    if not isinstance(value, str) or not value.startswith(expected + "_"):
        raise ValueError(f"id for kind {kind!r} must start with {expected + '_'!r}")
    return value
