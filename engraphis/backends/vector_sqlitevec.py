"""sqlite-vec native exact-KNN backend + factory.

Replaces Python/NumPy scan orchestration with an embedded native vector index in the
same SQLite file — preserving the local-first, single-file story. If the
``sqlite-vec`` extension is not installable in the current environment, the
factory transparently falls back to ``NumpyVectorIndex`` (so nothing breaks),
which is exactly what happens in restricted CI sandboxes.

Stable ``vec0`` performs exact KNN; it is not a sublinear ANN algorithm.

Note: sqlite-vec cannot apply Engraphis' bi-temporal/workspace filter inside the vec0
MATCH directly, so ``search`` expands the KNN window until it has enough visible hits.
"""
from __future__ import annotations

import importlib
import logging
import re
import sys
from numbers import Integral
from typing import Optional

import numpy as np

from engraphis.backends.embedder_deterministic import MAX_EMBEDDING_DIM
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import SearchFilter, VectorIndex
from engraphis.core.store import IN_CLAUSE_CHUNK, Store

logger = logging.getLogger("engraphis")

_INDEX_FORMAT_VERSION = 3
# Visibility checks are plain IN-chunks over canonical ids (identical semantics at
# any chunk size); match the store's IN_CLAUSE_CHUNK so a widening round costs
# len(unchecked)/500 round-trips instead of len(unchecked)/8.
_VISIBILITY_BATCH_SIZE = IN_CLAUSE_CHUNK
_COVERAGE_BATCH_SIZE = 500
_DELETE_BATCH_SIZE = 500
_COVERAGE_RTOL = 1e-6
_COVERAGE_ATOL = 1e-7


def _cosine_from_l2(distance: float) -> float:
    """Convert Euclidean distance between unit vectors back to cosine similarity."""
    return max(-1.0, min(1.0, 1.0 - (float(distance) ** 2) / 2.0))


def _validated_dimension(dim: int) -> int:
    """Return a bounded integer safe to interpolate into sqlite-vec DDL."""
    if isinstance(dim, bool) or not isinstance(dim, Integral):
        raise ValueError("embedding dimension must be a positive integer")
    dimension = int(dim)
    if not 1 <= dimension <= MAX_EMBEDDING_DIM:
        raise ValueError(
            f"embedding dimension must be between 1 and {MAX_EMBEDDING_DIM}"
        )
    return dimension


def _validated_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, Integral) or int(k) < 0:
        raise ValueError("k must be a non-negative integer")
    return int(k)


def _vector_batch(vecs: np.ndarray, dim: int) -> np.ndarray:
    try:
        values = np.asarray(vecs, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("vectors must be a finite float32 array") from exc
    if values.ndim != 2 or values.shape[1] != dim:
        actual = values.shape[1] if values.ndim == 2 else "?"
        raise ValueError(
            f"vector dimension {actual} does not match the index dimension {dim}"
        )
    if not np.isfinite(values).all():
        raise ValueError("vectors must contain only finite values")
    return values


def _vector_query(vec: np.ndarray, dim: int) -> np.ndarray:
    try:
        values = np.asarray(vec, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("query vector must be a finite 1-D float32 array") from exc
    if values.ndim != 1 or values.shape[0] != dim:
        actual = values.shape[0] if values.ndim == 1 else "?"
        raise ValueError(
            f"query dimension {actual} does not match the index dimension {dim}"
        )
    if not np.isfinite(values).all():
        raise ValueError("query vector must contain only finite values")
    return values


def _expected_native_vector(
    value: object, dimension: int,
) -> tuple[bool, Optional[np.ndarray]]:
    """Return whether a canonical blob is valid and its expected vec0 vector.

    Zero vectors deliberately have no native row: both backends define a zero query or
    candidate as contributing no cosine hit. Every other canonical vector is normalized
    exactly as :meth:`SqliteVecVectorIndex.upsert` normalizes it before comparison.
    """
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return False, None
    try:
        vector = np.frombuffer(bytes(value), dtype=np.float32)
    except (TypeError, ValueError, BufferError):
        return False, None
    if vector.shape != (dimension,) or not np.isfinite(vector).all():
        return False, None
    normalized = vector.astype(np.float64, copy=True)
    with np.errstate(over="ignore", invalid="ignore"):
        norm = float(np.linalg.norm(normalized))
    if not np.isfinite(norm):
        return False, None
    if norm == 0:
        return True, None
    normalized /= norm
    return True, normalized.astype(np.float32)


def _native_vector_matches(
    value: object, expected: np.ndarray, dimension: int,
) -> bool:
    """Compare finite vec0 output while allowing float32 normalization roundoff."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return False
    try:
        actual = np.frombuffer(value, dtype=np.float32)
    except (TypeError, ValueError, BufferError):
        return False
    return bool(
        actual.shape == (dimension,)
        and np.isfinite(actual).all()
        and np.allclose(
            actual, expected, rtol=_COVERAGE_RTOL, atol=_COVERAGE_ATOL,
        )
    )


def _native_mirror_covers_canonical(conn, dimension: int) -> bool:
    """Whether vec0 exactly mirrors every same-dimension canonical vector.

    The scan is keyset-paginated and all counterpart lookups stay below SQLite's
    conservative variable limit. The caller supplies the transaction: writable callers
    hold ``BEGIN IMMEDIATE`` while publishing, and read-only callers hold one snapshot.
    """
    after_id = ""
    expected_native_count = 0
    while True:
        canonical_rows = conn.execute(
            "SELECT v.id, v.vector FROM mem_vectors v "
            "JOIN memories m ON m.id=v.id "
            "WHERE v.dim=? AND v.id>? ORDER BY v.id LIMIT ?",
            (dimension, after_id, _COVERAGE_BATCH_SIZE),
        ).fetchall()
        if not canonical_rows:
            break
        ids = [str(row["id"]) for row in canonical_rows]
        marks = ",".join("?" for _ in ids)
        native_rows = conn.execute(
            f"SELECT id, embedding FROM mem_vec_ann WHERE id IN ({marks})", ids,
        ).fetchall()
        native = {str(row["id"]): row["embedding"] for row in native_rows}
        for row in canonical_rows:
            memory_id = str(row["id"])
            valid, expected = _expected_native_vector(row["vector"], dimension)
            if not valid:
                return False
            if expected is None:
                if memory_id in native:
                    return False
            elif not _native_vector_matches(
                native.get(memory_id), expected, dimension,
            ):
                return False
            else:
                expected_native_count += 1
        after_id = ids[-1]
        if len(canonical_rows) < _COVERAGE_BATCH_SIZE:
            break

    # Every expected nonzero ID and its full vector content was verified above.
    # vec0 IDs are unique, so matching total cardinality now excludes every extra
    # orphan/zero/wrong-dimension row. Repeated ORDER BY id on vec0 is not an indexed
    # range scan and needlessly sorts the full native table for each reverse batch.
    native_total = conn.execute("SELECT COUNT(*) AS n FROM mem_vec_ann").fetchone()
    return native_total is not None and int(native_total["n"]) == expected_native_count


def _native_index_status(conn, dimension: int):
    """Return the live vec0 table row and whether its persisted state is current."""
    existing = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='mem_vec_ann'"
    ).fetchone()
    state_table = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='mem_vec_ann_state'"
    ).fetchone()
    state = (
        conn.execute(
            "SELECT format_version, dimension FROM mem_vec_ann_state "
            "WHERE singleton=1"
        ).fetchone()
        if state_table is not None
        else None
    )
    declared_dimension = None
    if existing and existing["sql"]:
        match = re.search(
            r"FLOAT\s*\[\s*(\d+)\s*\]", existing["sql"], re.IGNORECASE
        )
        if match:
            declared_dimension = int(match.group(1))
    current = bool(
        existing
        and declared_dimension == dimension
        and state
        and int(state["format_version"]) == _INDEX_FORMAT_VERSION
        and int(state["dimension"]) == dimension
    )
    if current:
        current = _native_mirror_covers_canonical(conn, dimension)
    return existing, current


_READ_ONLY_STALE_ERROR = (
    "read-only sqlite-vec index is unavailable or stale; open the database writable "
    "once to rebuild it, or use vector_backend='numpy'"
)




class SqliteVecVectorIndex:
    """Native exact KNN over embeddings using the sqlite-vec extension."""

    shares_store_vector_table = False
    shares_store_transaction = True

    def __init__(self, store: Store, dim: int) -> None:
        dimension = _validated_dimension(dim)
        # sqlite-vec is a loadable SQLite extension. SQLCipher ships a different
        # SQLite build, and loading both native libraries into one interpreter has
        # caused hard crashes rather than a normal Python exception. An `auto`
        # request below can safely use NumPy instead; an explicit sqlite-vec
        # request gets this actionable error before any unsafe native call.
        if any(
            name == "sqlcipher3" or name.startswith("sqlcipher3.")
            for name in sys.modules
        ):
            raise RuntimeError(
                "sqlite-vec cannot share a process with SQLCipher; use "
                "vector_backend='numpy' or run the accelerated backend in a fresh process"
            )
        sqlite_vec = importlib.import_module("sqlite_vec")  # lazy optional extension
        self.store = store
        self.dim = dimension
        conn = store.conn
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            # Never leave extension loading enabled on a shared connection, including
            # when the optional native load fails.
            conn.enable_load_extension(False)

        if store.read_only:
            # Loading the extension only registers SQL functions. Never run DDL or
            # update backend state against an immutable inspection Store.
            owns_transaction = not conn.transaction_owned_by_current_thread()
            try:
                if owns_transaction:
                    conn.execute("BEGIN")
                _, current = _native_index_status(conn, dimension)
                self._verified_generation = store.vector_generation()
            except Exception:
                raise RuntimeError(_READ_ONLY_STALE_ERROR) from None
            finally:
                if owns_transaction and conn.transaction_owned_by_current_thread():
                    conn.rollback()
            if not current:
                raise RuntimeError(_READ_ONLY_STALE_ERROR)
            self.requires_rebuild = False
            return

        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mem_vec_ann_state ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "format_version INTEGER NOT NULL, dimension INTEGER NOT NULL)"
            )
            existing, current = _native_index_status(conn, dimension)
            self._verified_generation = store.vector_generation() if current else -1
            # The composition root can inspect this capability before replaying the
            # canonical mem_vectors mirror after a table creation or format change.
            self.requires_rebuild = not current
            if existing and not current:
                # vec0 rows are a disposable mirror of canonical mem_vectors. Recreate
                # on format/dimension changes; engine startup hydrates only after the
                # canonical embedding-space gate is ready.
                conn.execute("DROP TABLE mem_vec_ann")
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec_ann USING vec0("
                f"id TEXT PRIMARY KEY, embedding FLOAT[{dimension}])"
            )
            # DDL is not readiness: persist an incomplete marker until the engine has
            # replayed every canonical row and calls ``mark_rebuild_complete``. A crash
            # in that window must make read-only startup reject or fall back.
            persisted_version = _INDEX_FORMAT_VERSION if current else 0
            conn.execute(
                "INSERT INTO mem_vec_ann_state("
                "singleton, format_version, dimension) VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "format_version=excluded.format_version, dimension=excluded.dimension",
                (persisted_version, dimension),
            )
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def can_skip_hydration(self) -> bool:
        """Skip replay only when no canonical mutation followed verified coverage."""
        with self.store.read_snapshot():
            return (not self.requires_rebuild
                    and self._verified_generation == self.store.vector_generation())

    def mark_rebuild_complete(self) -> None:
        """Publish native readiness only after the canonical mirror is fully hydrated."""
        if self.store.read_only:
            raise RuntimeError("read-only sqlite-vec indexes cannot publish rebuild state")
        conn = self.store.conn
        if conn.transaction_owned_by_current_thread():
            raise RuntimeError(
                "sqlite-vec rebuild completion requires its own transaction"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not _native_mirror_covers_canonical(conn, self.dim):
                raise RuntimeError(
                    "sqlite-vec rebuild is incomplete; native mirror coverage differs "
                    "from canonical vectors"
                )
            updated = conn.execute(
                "UPDATE mem_vec_ann_state SET format_version=? "
                "WHERE singleton=1 AND dimension=?",
                (_INDEX_FORMAT_VERSION, self.dim),
            )
            if updated.rowcount != 1:
                raise RuntimeError("sqlite-vec rebuild state is missing or stale")
            generation = self.store.vector_generation()
            conn.commit()
        except BaseException:
            if conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise
        self.requires_rebuild = False
        self._verified_generation = generation

    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None,
               *, commit: bool = True) -> None:
        values = _vector_batch(vecs, self.dim)
        try:
            count = len(ids)
        except TypeError as exc:
            raise ValueError("ids must be a sequence matching the vector batch") from exc
        if count != values.shape[0]:
            raise ValueError(
                f"ids length {count} does not match vector batch size {values.shape[0]}"
            )
        if any(not isinstance(mid, str) or not mid for mid in ids):
            raise ValueError("ids must contain non-empty strings")
        if meta is not None and len(meta) != count:
            raise ValueError("meta length must match the vector batch")
        if not count:
            return
        normalized = values.astype(np.float64, copy=True)
        with np.errstate(over="ignore", invalid="ignore"):
            norms = np.linalg.norm(normalized, axis=1)
        if not np.isfinite(norms).all():
            raise ValueError("vector norm must be finite")
        nonzero = norms > 0
        normalized[nonzero] /= norms[nonzero, None]
        normalized = normalized.astype(np.float32)

        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            # ``vec0`` virtual tables do not implement SQLite's conflict-resolution
            # algorithms consistently: INSERT OR REPLACE can still raise a UNIQUE
            # constraint error when a persisted row is hydrated after reopening a
            # database. Delete the batch first, then insert the replacement rows in
            # the same transaction so restart hydration remains idempotent and
            # failures roll back to the previous index state.
            for offset in range(0, count, _DELETE_BATCH_SIZE):
                batch = ids[offset:offset + _DELETE_BATCH_SIZE]
                marks = ",".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", batch
                )
            for mid, vector, keep in zip(ids, normalized, nonzero):
                if not keep:
                    continue
                conn.execute(
                    "INSERT INTO mem_vec_ann(id, embedding) VALUES (?, ?)",
                    (mid, vector.tobytes()),
                )
            if commit and owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def delete(self, ids: list[str], *, commit: bool = True) -> None:
        if not ids:
            return
        conn = self.store.conn
        owns_transaction = not conn.transaction_owned_by_current_thread()
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for offset in range(0, len(ids), _DELETE_BATCH_SIZE):
                batch = ids[offset:offset + _DELETE_BATCH_SIZE]
                marks = ",".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM mem_vec_ann WHERE id IN ({marks})", batch
                )
            if commit and owns_transaction and conn.transaction_owned_by_current_thread():
                conn.commit()
        except BaseException:
            if owns_transaction and conn.transaction_owned_by_current_thread():
                conn.rollback()
            raise

    def search(
        self,
        vec: np.ndarray,
        k: int,
        *,
        filter: Optional[SearchFilter] = None,
    ) -> list[tuple[str, float]]:
        k = _validated_k(k)
        if k == 0:
            return []
        v = _vector_query(vec, self.dim)
        with np.errstate(over="ignore", invalid="ignore"):
            norm = float(np.linalg.norm(v))
        if not np.isfinite(norm):
            raise ValueError("query vector norm must be finite")
        if norm == 0:
            return []
        v = v / norm
        total_row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM mem_vec_ann"
        ).fetchone()
        total = int(total_row["n"]) if total_row is not None else 0
        if total == 0:
            return []
        # Fetch one look-ahead row so the common unique-distance case can prove the
        # kth boundary complete without issuing another native KNN query.
        limit = min(k + 1, total)
        visibility: dict[str, bool] = {}
        while True:
            # The KNN cap uses vec0's explicit `k = ?` constraint, NOT `LIMIT ?`.
            rows = self.store.conn.execute(
                "SELECT id, distance FROM mem_vec_ann WHERE embedding MATCH ? "
                "AND k = ? ORDER BY distance",
                (v.tobytes(), int(limit)),
            ).fetchall()
            # Match NumPy's live-record contract even for direct callers that omit a
            # filter; orphaned, closed, and future ANN rows must never leak. Ask Store
            # for IDs only so widening never hydrates large memory bodies.
            effective_filter = filter if filter is not None else SearchFilter()
            unchecked = [
                row["id"] for row in rows if row["id"] not in visibility
            ]
            for start in range(0, len(unchecked), _VISIBILITY_BATCH_SIZE):
                batch = unchecked[start:start + _VISIBILITY_BATCH_SIZE]
                visible_ids = self.store.visible_memory_ids(
                    batch, effective_filter
                )
                visibility.update(
                    (memory_id, memory_id in visible_ids) for memory_id in batch
                )
            eligible = [
                row for row in rows if visibility.get(row["id"], False)
            ]
            eligible.sort(key=lambda row: (float(row["distance"]), str(row["id"])))

            # vec0 may choose an unspecified subset when equal-distance rows straddle
            # its k boundary. Widen until the raw boundary is strictly farther than
            # the kth visible row; then the complete tie group is present and the
            # memory-id secondary order is deterministic across backends and runs.
            exhausted = len(rows) < limit or limit >= total
            enough = len(eligible) >= k
            boundary_complete = (
                enough
                and (
                    exhausted
                    or float(rows[-1]["distance"]) > float(eligible[k - 1]["distance"])
                )
            )
            if boundary_complete or exhausted:
                selected = eligible[:k]
                return [
                    (row["id"], _cosine_from_l2(row["distance"]))
                    for row in selected
                ]
            # Filtered search widens geometrically until k visible hits are found.
            limit = total if limit * 2 >= total // 4 else limit * 2



def get_vector_index(store: Store, *, dim: int = 384, prefer: str = "auto") -> VectorIndex:
    """Return a sqlite-vec index if available, else the NumPy reference index.

    prefer: "auto" (try sqlite-vec, fall back), "sqlite-vec" (require it),
            or "numpy" (force the reference index).
    """
    dimension = _validated_dimension(dim)
    if prefer not in {"auto", "sqlite-vec", "numpy"}:
        raise ValueError("prefer must be one of: auto, sqlite-vec, numpy")
    if prefer == "numpy":
        return NumpyVectorIndex(store, dim=dimension)
    try:
        return SqliteVecVectorIndex(store, dimension)
    except Exception as exc:
        if prefer == "sqlite-vec":
            raise
        logger.warning(
            "sqlite-vec vector index unavailable (%s); falling back to NumpyVectorIndex",
            type(exc).__name__,
        )
        return NumpyVectorIndex(store, dim=dimension)
