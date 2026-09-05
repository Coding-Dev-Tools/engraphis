"""File-backed counterfactual: force the former FTS delete versus new-row insertion.

Everything else uses the same current Store and NumPy backend. This isolates the
delete change; it is not a historical release benchmark or end-to-end write test.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import time

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.store import Store
from eval.benchmark import report_envelope, write_canonical_artifact
from eval.vector_scale import _normalized_random, parse_sizes
from eval.vector_scale_storage import (
    _ROOT, _SOURCES, _disk, _hardware, _insert, _source_snapshot,
)


def run_comparison(sizes, *, dim=256, batch_size=500, seed=20260731):
    sizes = parse_sizes(",".join(map(str, sizes)))
    if min(dim, batch_size) < 1:
        raise ValueError("dimension and batch_size must be positive")
    before = _source_snapshot()
    cells = []
    for strategy in ("forced_legacy_delete", "new_row_insert"):
        for size in sizes:
            with tempfile.TemporaryDirectory(prefix="egr-fts-") as folder:
                path = Path(folder) / "corpus.db"
                store = Store(str(path))
                try:
                    if not store.has_fts5:
                        raise RuntimeError("FTS5 required for the unindexed-column comparison")
                    workspace_id = store.get_or_create_workspace("fts-scaling")
                    repo_ids = [store.get_or_create_repo(workspace_id, f"scope-{i}")
                                for i in range(4)]
                    index = NumpyVectorIndex(store, dim=dim)
                    if strategy == "forced_legacy_delete":
                        original = store._fts_upsert

                        def force_delete(mid, title, content, keywords, **_kwargs):
                            return original(mid, title, content, keywords,
                                            replace_existing=True)

                        store._fts_upsert = force_delete
                    rng = np.random.default_rng(seed)
                    started = time.perf_counter()
                    for offset in range(0, size, batch_size):
                        stop = min(size, offset + batch_size)
                        vectors = _normalized_random(rng, stop - offset, dim)
                        _insert(store, index, range(offset, stop), vectors, workspace_id, repo_ids)
                    elapsed = time.perf_counter() - started
                    rows = {
                        name: int(store.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                        for name in ("memories", "mem_vectors", "mem_fts")
                    }
                    if set(rows.values()) != {size}:
                        raise RuntimeError("canonical/vector/FTS cardinality mismatch")
                    cells.append({"strategy": strategy, "corpus_size": size,
                                  "elapsed_seconds": elapsed, "records_per_second": size / elapsed,
                                  "verified_row_counts": rows, "disk": _disk(path)})
                finally:
                    store.close()
    after = _source_snapshot()
    return report_envelope(
        suite="fts-new-insert-scaling/counterfactual-v1", dataset_path=Path(__file__),
        config={"sizes": sizes, "dimension": dim, "batch_size": batch_size, "seed": seed},
        records=[{"question_id": f"{cell['strategy']}-{cell['corpus_size']}",
                  "category": "canonical_storage_population"} for cell in cells],
        metrics={"cells": cells, "hardware": _hardware(), "source_before": before,
                 "source_after": after, "source_stable": before == after,
                 "measurement_scope": "current synthetic Store+NumPy writes, forcing the former FTS deletion in one arm",
                 "unmeasured": ["historical release behavior", "embedding or resolution latency",
                                "independent process repetitions", "external contention"]},
        source_paths=[_ROOT / name for name in _SOURCES] + [Path(__file__)],
        models={"embedding": {"identity": "none; precomputed synthetic vectors"},
                "vector_backend": {"identity": "NumpyVectorIndex"}},
        command=["python", "-m", "eval.fts_insert_scaling", "--sizes", ",".join(map(str, sizes)),
                 "--dim", str(dim), "--batch-size", str(batch_size), "--seed", str(seed)],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,5000,10000")
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260731)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = run_comparison(parse_sizes(args.sizes), dim=args.dim,
                            batch_size=args.batch_size, seed=args.seed)
    print(write_canonical_artifact(report, args.output))
    return 0 if report["metrics"]["source_stable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
