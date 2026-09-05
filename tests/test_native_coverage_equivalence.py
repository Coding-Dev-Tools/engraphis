"""Cardinality excludes extras only after full per-ID/content verification."""
import numpy as np
import pytest

pytest.importorskip("sqlite_vec")

from engraphis.backends.vector_sqlitevec import (  # noqa: E402
    SqliteVecVectorIndex, _native_mirror_covers_canonical,
)
from engraphis.core.interfaces import MemoryRecord  # noqa: E402
from engraphis.core.store import Store  # noqa: E402
from eval.native_coverage_scaling import _legacy_reverse_scan, run_comparison  # noqa: E402


@pytest.mark.parametrize("mutation,expected", [
    ("complete", True), ("missing", False), ("extra", False),
    ("zero_stale", False), ("zero_clean", True), ("wrong_dimension", False),
    ("stale_vector", False), ("orphan", False), ("malformed", False),
    ("balanced_missing_extra", False),
])
def test_count_equivalence_preserves_full_mirror_integrity(mutation, expected):
    store = Store(":memory:")
    try:
        index = SqliteVecVectorIndex(store, dim=4)
        wid = store.get_or_create_workspace("coverage")
        vectors = np.eye(4, dtype=np.float32)[:3]
        mids = ["mem_coverage_a", "mem_coverage_b", "mem_coverage_c"]
        for mid, vector in zip(mids, vectors):
            store.add_memory(MemoryRecord(id=mid, content="coverage evidence", workspace_id=wid))
            store.put_vector(mid, vector)
        index.upsert(mids, vectors)
        if mutation in ("missing", "balanced_missing_extra"):
            index.delete([mids[0]])
        if mutation in ("extra", "balanced_missing_extra"):
            index.upsert(["mem_extra"], vectors[:1])
        if mutation in ("zero_stale", "zero_clean"):
            store.put_vector(mids[0], np.zeros(4, dtype=np.float32))
            if mutation == "zero_clean":
                index.delete([mids[0]])
        if mutation == "wrong_dimension":
            store.put_vector(mids[0], np.ones(3, dtype=np.float32))
        if mutation == "stale_vector":
            index.upsert([mids[0]], vectors[1:2])
        if mutation == "orphan":
            store.conn.execute("DELETE FROM memories WHERE id=?", (mids[0],))
        if mutation == "malformed":
            store.conn.execute("UPDATE mem_vectors SET vector=? WHERE id=?", (b"x", mids[0]))
        store.conn.commit()
        with store.read_snapshot():
            assert _legacy_reverse_scan(store.conn, 4) is expected
            assert _native_mirror_covers_canonical(store.conn, 4) is expected
    finally:
        store.close()


def test_native_coverage_ablation_executes_both_methods():
    report = run_comparison([3, 7], dim=4, batch_size=2)
    assert len(report["metrics"]["cells"]) == 4
    assert all(cell["coverage_verified"] for cell in report["metrics"]["cells"])
    assert report["metrics"]["source_stable"]
