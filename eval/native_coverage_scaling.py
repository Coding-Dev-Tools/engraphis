"""Isolate native coverage verification; the legacy oracle is frozen pre-optimization.

Both arms verify canonical contents. Only reverse native traversal versus exact
cardinality differs; this is a method ablation, not a historical release result.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import time

import numpy as np

from engraphis.backends.vector_sqlitevec import (
    SqliteVecVectorIndex, _COVERAGE_BATCH_SIZE, _expected_native_vector,
    _native_mirror_covers_canonical, _native_vector_matches,
)
from engraphis.core.store import Store
from eval.benchmark import report_envelope, write_canonical_artifact
from eval.vector_scale import _normalized_random, parse_sizes
from eval.vector_scale_storage import _ROOT, _SOURCES, _hardware, _insert, _source_snapshot


def _legacy_reverse_scan(conn, dimension: int) -> bool:
    """Whether vec0 exactly mirrors every same-dimension canonical vector.

    Both scans are keyset-paginated and all counterpart lookups stay below SQLite's
    conservative variable limit. The caller supplies the transaction: writable callers
    hold ``BEGIN IMMEDIATE`` while publishing, and read-only callers hold one snapshot.
    """
    after_id = ""
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
        after_id = ids[-1]
        if len(canonical_rows) < _COVERAGE_BATCH_SIZE:
            break

    # The forward scan proves that nothing canonical is missing or stale. This reverse
    # scan rejects orphaned native rows and rows whose canonical vector became zero or
    # changed dimension after another backend wrote the portable mirror.
    after_id = ""
    while True:
        native_rows = conn.execute(
            "SELECT id, embedding FROM mem_vec_ann "
            "WHERE id>? ORDER BY id LIMIT ?",
            (after_id, _COVERAGE_BATCH_SIZE),
        ).fetchall()
        if not native_rows:
            break
        ids = [str(row["id"]) for row in native_rows]
        marks = ",".join("?" for _ in ids)
        canonical_rows = conn.execute(
            "SELECT v.id, v.vector FROM mem_vectors v "
            "JOIN memories m ON m.id=v.id "
            f"WHERE v.dim=? AND v.id IN ({marks})",
            (dimension, *ids),
        ).fetchall()
        canonical = {str(row["id"]): row["vector"] for row in canonical_rows}
        for row in native_rows:
            memory_id = str(row["id"])
            valid, expected = _expected_native_vector(
                canonical.get(memory_id), dimension,
            )
            if (
                not valid
                or expected is None
                or not _native_vector_matches(
                    row["embedding"], expected, dimension,
                )
            ):
                return False
        after_id = ids[-1]
        if len(native_rows) < _COVERAGE_BATCH_SIZE:
            break
    return True


def run_comparison(sizes, *, dim=256, batch_size=500, seed=20260731):
    sizes = parse_sizes(",".join(map(str, sizes)))
    if min(dim, batch_size) < 1:
        raise ValueError("dimension and batch_size must be positive")
    before = _source_snapshot()
    cells = []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="egr-cover-") as folder:
            store = Store(str(Path(folder) / "corpus.db"))
            try:
                index = SqliteVecVectorIndex(store, dim=dim)
                workspace_id = store.get_or_create_workspace("coverage-scaling")
                repo_ids = [store.get_or_create_repo(workspace_id, "scope0")]
                rng = np.random.default_rng(seed)
                for offset in range(0, size, batch_size):
                    stop = min(size, offset + batch_size)
                    vectors = _normalized_random(rng, stop - offset, dim)
                    _insert(store, index, range(offset, stop), vectors, workspace_id, repo_ids)
                for strategy, verify in (("legacy_reverse_scan", _legacy_reverse_scan),
                                         ("verified_cardinality", _native_mirror_covers_canonical)):
                    with store.read_snapshot():
                        started = time.perf_counter()
                        covered = verify(store.conn, dim)
                        elapsed = time.perf_counter() - started
                    if not covered:
                        raise RuntimeError("coverage method rejected the identical complete mirror")
                    cells.append({"strategy": strategy, "corpus_size": size,
                                  "elapsed_seconds": elapsed, "coverage_verified": covered})
            finally:
                store.close()
    after = _source_snapshot()
    return report_envelope(
        suite="native-coverage-scaling/counterfactual-v1", dataset_path=Path(__file__),
        config={"sizes": sizes, "dimension": dim, "batch_size": batch_size, "seed": seed},
        records=[{"question_id": f"{cell['strategy']}-{cell['corpus_size']}",
                  "category": "native_coverage_verification"} for cell in cells],
        metrics={"cells": cells, "hardware": _hardware(), "source_before": before,
                 "source_after": after, "source_stable": before == after,
                 "measurement_scope": "same current canonical/native store; only the verification traversal differs",
                 "unmeasured": ["historical release behavior", "independent process repetitions",
                                "external contention", "end-to-end recall"]},
        source_paths=[_ROOT / name for name in _SOURCES] + [Path(__file__)],
        command=["python", "-m", "eval.native_coverage_scaling", "--sizes", ",".join(map(str, sizes)),
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
