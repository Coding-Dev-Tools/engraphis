"""Bounded memory browsing using the canonical store scope and temporal rules.

Cursors bind the query, time anchors, ordering and persisted scope revisions.
Only relevant committed changes require a refresh; connections may change freely.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import asdict, replace
from typing import Any, Callable, ContextManager, Optional, Protocol, cast

from .interfaces import SearchFilter


class BrowseStore(Protocol):
    conn: Any

    def _where(self, flt: Optional[SearchFilter], include_invalid: bool,
               alias: str = "") -> tuple[list[str], list[Any]]: ...


class BrowseCursorStale(ValueError):
    """The scoped listing changed since the previous page."""


def _revision(store: BrowseStore, flt: SearchFilter) -> str:
    identity = store.conn.execute("SELECT identity FROM browse_state WHERE singleton=1").fetchone()
    if identity is None:
        raise RuntimeError("memory browse state is incomplete; reopen with a writable Store")
    digest = hashlib.sha256(str(identity[0]).encode())
    # A formerly excluded record may enter the query after a mutation, so revisions
    # cover scope/type buckets rather than only currently visible/searchable rows.
    where, params = store._where(replace(flt, modified_since=None), include_invalid=True)
    predicate = " AND ".join(where) or "1"
    rows = store.conn.execute(
        "SELECT workspace_id,repo_id,session_id,scope,mtype,revision "
        "FROM browse_scope_revisions WHERE " + predicate
        + " ORDER BY workspace_id,repo_id,session_id,scope,mtype", params,
    )
    # Scope cardinality may grow with sessions. Stream this small revision ledger;
    # no memory prose or vectors enter the digest or the cursor.
    for row in rows:
        digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
    return digest.hexdigest()


def _cursor_number(value: Any) -> bool:
    # JSON integers are unbounded; SQLite parameters are signed 64-bit integers.
    # Reject oversized integers before float conversion or database binding.
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (not isinstance(value, int) or -(1 << 63) <= value < (1 << 63))
            and math.isfinite(value))


def _decode(cursor: str) -> dict[str, Any]:
    try:
        if len(cursor) > 4096:
            raise ValueError
        value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_",
                                           validate=True))
        if not isinstance(value, dict) or type(value.get("v")) is not int \
                or value["v"] not in (1, 2):
            raise ValueError
        anchors = value["anchors"]
        if not isinstance(anchors, list) or len(anchors) != 2 \
                or any(not _cursor_number(x) for x in anchors):
            raise ValueError
        position = value["position"]
        if not isinstance(position, list) or len(position) != 4:
            raise ValueError
        if position[0] not in (0, 1) or any(not _cursor_number(x) for x in position[:3]) \
                or not isinstance(position[3], str) or len(position[3]) > 200:
            raise ValueError
        if value["v"] == 2:
            revision = value["revision"]
            if not isinstance(revision, str) or len(revision) != 64 \
                    or any(ch not in "0123456789abcdef" for ch in revision):
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid memory cursor") from exc


def browse_memories(store: BrowseStore, flt: SearchFilter, *, q: str = "",
                    limit: int = 200, cursor: str = "", timeout: float = 5.0) -> dict[str, Any]:
    """Browse through an independent bounded reader when the Store supports one."""
    borrow = getattr(store, "borrow_read_snapshot", None)
    if callable(borrow):
        create_snapshot = cast(Callable[..., ContextManager[BrowseStore]], borrow)
        with create_snapshot(timeout=timeout) as reader:
            return _browse_memories(reader, flt, q=q, limit=limit, cursor=cursor)
    return _browse_memories(store, flt, q=q, limit=limit, cursor=cursor)


def _browse_memories(store: BrowseStore, flt: SearchFilter, *, q: str = "",
                     limit: int = 200, cursor: str = "") -> dict[str, Any]:
    """Return a consistent bounded page and exact filtered count without embeddings."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if not isinstance(q, str) or len(q) > 10_000:
        raise ValueError("invalid memory search")
    if not isinstance(cursor, str):
        raise ValueError("invalid memory cursor")
    identity = hashlib.sha256(json.dumps(
        {"filter": asdict(flt), "q": q}, sort_keys=True, default=str,
        separators=(",", ":"),
    ).encode()).hexdigest()
    previous = _decode(cursor) if cursor else None
    if previous and previous.get("query") != identity:
        raise ValueError("memory cursor does not match the query")
    if previous and previous["v"] == 1:
        raise BrowseCursorStale("memory cursor upgraded; restart from the first page")
    now = time.time()
    anchors = (previous["anchors"] if previous else [
        flt.valid_at if flt.valid_at is not None else flt.as_of if flt.as_of is not None else now,
        flt.known_at if flt.known_at is not None else now,
    ])
    anchored = replace(flt, as_of=None, valid_at=anchors[0], known_at=anchors[1])
    conn = store.conn
    owns = not conn.transaction_owned_by_current_thread()
    try:
        if owns:
            conn.execute("BEGIN")
        revision = _revision(store, anchored)
        if previous and previous.get("revision") != revision:
            raise BrowseCursorStale("memory listing changed; restart from the first page")
        where, params = store._where(anchored, include_invalid=False)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
                         "OR summary LIKE ? ESCAPE '\\')")
            params.extend(["%" + escaped + "%"] * 3)
        predicate = " AND ".join(where) or "1"
        total = int(conn.execute("SELECT COUNT(*) FROM memories WHERE " + predicate,
                                 params).fetchone()[0])
        order = "(sort_order IS NULL), COALESCE(sort_order,0), -COALESCE(valid_from,0), id"
        if previous:
            predicate += " AND (" + order + ") > (?,?,?,?)"
            params.extend(previous["position"])
        rows = conn.execute(
            "SELECT * FROM memories WHERE " + predicate + " ORDER BY " + order + " LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        page = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            sort = last["sort_order"]
            recent = last["valid_from"]
            payload = {"v": 2, "query": identity, "revision": revision, "anchors": anchors,
                       "position": [int(sort is None), sort if sort is not None else 0,
                                    -(recent if recent is not None else 0), last["id"]]}
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                payload, separators=(",", ":"), allow_nan=False,
            ).encode()).decode()
        return {"rows": page, "total_count": total, "next_cursor": next_cursor,
                "valid_at": anchors[0], "known_at": anchors[1]}
    finally:
        if owns and conn.transaction_owned_by_current_thread():
            conn.rollback()
