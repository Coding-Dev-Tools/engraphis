import threading

import numpy as np
import pytest

from engraphis.core import store as store_module
from engraphis.core.interfaces import MemoryRecord, SearchFilter
from engraphis.core.store import Store


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7])
def test_adaptive_scan_returns_exact_sorted_bounded_batches(count, monkeypatch):
    store = Store(":memory:")
    try:
        wid = store.get_or_create_workspace("snapshot-plan")
        repo = store.get_or_create_repo(wid, "selected")
        other = store.get_or_create_repo(wid, "other")
        for number in reversed(range(9)):
            store.add_memory(MemoryRecord(
                id=f"mem_order_{number}", content="vector scan evidence", workspace_id=wid,
                repo_id=repo if number < count else other,
                embedding=np.array([number + 1, 1, 0, 0], dtype=np.float32),
            ))
        monkeypatch.setattr(store_module, "VECTOR_SCAN_BATCH", 2)
        flt = SearchFilter(workspace_id=wid, repo_id=repo)
        batches = list(store.iter_vector_matrices(flt, dim=4))
        assert all(1 <= len(mids) <= 2 and matrix.shape == (len(mids), 4)
                   for mids, matrix in batches)
        mids = [mid for ids, _ in batches for mid in ids]
        assert mids == [f"mem_order_{number}" for number in range(count)]
        expected = dict(store.iter_vectors(flt, dim=4))
        for ids, matrix in batches:
            for mid, vector in zip(ids, matrix):
                np.testing.assert_array_equal(vector, expected[mid])
        assert not store.conn.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("count", [1, 5])
def test_early_generator_close_releases_snapshot_for_waiting_writer(count, monkeypatch):
    store = Store(":memory:")
    worker = None
    stream = None
    try:
        wid = store.get_or_create_workspace("snapshot-close")
        for number in range(count):
            store.add_memory(MemoryRecord(
                id=f"mem_snapshot_{number}", content="snapshot evidence", workspace_id=wid,
                embedding=np.array([1, number + 1, 0, 0], dtype=np.float32),
            ))
        monkeypatch.setattr(store_module, "VECTOR_SCAN_BATCH", 2)
        stream = iter(store.iter_vector_matrices(dim=4))
        next(stream)
        started, finished = threading.Event(), threading.Event()
        errors = []

        def write():
            started.set()
            try:
                store.put_vector("mem_snapshot_0", np.array([0, 1, 0, 0], dtype=np.float32))
                store.conn.commit()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=write)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        stream.close()
        assert finished.wait(2)
        assert not errors
        assert not store.conn.in_transaction
    finally:
        if stream is not None:
            stream.close()
        if worker is not None:
            worker.join(timeout=2)
        store.close()
