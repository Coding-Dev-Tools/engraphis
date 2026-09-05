"""Compare scoped keyset scan query plans without changing ranking or locking."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tempfile
import time

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store, VECTOR_SCAN_BATCH
from eval.benchmark import report_envelope, write_canonical_artifact
from eval.vector_scale import _normalized_random, parse_sizes
from eval.vector_scale_storage import _ROOT, _SOURCES, _hardware, _insert, _source_snapshot


def _digest_batch(digest, mids, matrix):
    for mid, vector in zip(mids, matrix):
        digest.update(mid.encode())
        digest.update(vector.tobytes())


def scan(store, flt, dim, *, vector_first, sorted_first=False):
    where, params = store._where(flt, False, alias="m")
    where.extend(("v.dim=?", "length(v.vector)=?", "v.id>?"))
    params.extend((dim, dim * 4))
    join = "CROSS JOIN" if vector_first else "JOIN"
    sql = (f"SELECT v.id,v.vector FROM mem_vectors v {join} memories m ON m.id=v.id WHERE "
           + " AND ".join(where) + " ORDER BY v.id LIMIT ?")
    plan = [str(row[3]) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN " + sql, (*params, "", VECTOR_SCAN_BATCH),
    ).fetchall()]
    after_id, count, digest = "", 0, hashlib.sha256()
    started = time.perf_counter()
    with store.read_snapshot():
        while True:
            selected_sql = sql.replace("CROSS JOIN", "JOIN") if sorted_first and not after_id else sql
            rows = store.conn.fetchall(selected_sql, (*params, after_id, VECTOR_SCAN_BATCH))
            mids = [str(row["id"]) for row in rows]
            payload = b"".join(row["vector"] for row in rows)
            matrix = np.frombuffer(payload, dtype=np.float32).reshape(len(mids), dim)
            _digest_batch(digest, mids, matrix)
            count += len(rows)
            if len(rows) < VECTOR_SCAN_BATCH:
                break
            after_id = str(rows[-1]["id"])
    return {"elapsed_seconds": time.perf_counter() - started,
            "rows": count, "result_sha256": digest.hexdigest(), "query_plan": plan}


def adaptive_scan(store, flt, dim):
    count, digest = 0, hashlib.sha256()
    started = time.perf_counter()
    for mids, matrix in store.iter_vector_matrices(flt, dim=dim):
        _digest_batch(digest, mids, matrix)
        count += len(mids)
    return {"elapsed_seconds": time.perf_counter() - started,
            "rows": count, "result_sha256": digest.hexdigest(),
            "query_plan": ["production first scoped sorted batch; later vector-primary-key batches"]}


def run_comparison(sizes, *, dim=256, batch_size=500, seed=20260731):
    sizes = parse_sizes(",".join(map(str, sizes)))
    if min(dim, batch_size) < 1:
        raise ValueError("dimension and batch_size must be positive")
    before, cells = _source_snapshot(), []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="egr-plan-") as folder:
            store = Store(str(Path(folder) / "corpus.db"))
            try:
                index = NumpyVectorIndex(store, dim=dim)
                workspace_id = store.get_or_create_workspace("scan-plan")
                scope_ids = [store.get_or_create_repo(workspace_id, f"scope-{i}") for i in range(6)]
                # One corpus supports narrow, moderate and broad filter probes.
                repo_ids = ([scope_ids[0]] + [scope_ids[1]] * 10 + [scope_ids[2]] * 21
                            + [scope_ids[3]] * 50 + [scope_ids[4]] * 250 + [scope_ids[5]] * 668)
                rng = np.random.default_rng(seed)
                for offset in range(0, size, batch_size):
                    stop = min(size, offset + batch_size)
                    vectors = _normalized_random(rng, stop - offset, dim)
                    _insert(store, index, range(offset, stop), vectors, workspace_id, repo_ids)
                for percent, repo_id in ((0.1, scope_ids[0]), (1, scope_ids[1]),
                                         (2.1, scope_ids[2]), (5, scope_ids[3]),
                                         (25, scope_ids[4]), (100, None)):
                    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
                    pair = []
                    for strategy, vector_first in (("planner_selected_join", False),
                                                   ("vector_primary_key_first", True)):
                        result = scan(store, flt, dim, vector_first=vector_first)
                        pair.append(result)
                        cells.append({"strategy": strategy, "corpus_size": size,
                                      "target_scope_percent": percent, **result})
                    result = adaptive_scan(store, flt, dim)
                    pair.append(result)
                    cells.append({"strategy": "adaptive_snapshot", "corpus_size": size,
                                  "target_scope_percent": percent, **result})
                    result = scan(store, flt, dim, vector_first=True, sorted_first=True)
                    pair.append(result)
                    cells.append({"strategy": "scoped_first_sorted_batch", "corpus_size": size,
                                  "target_scope_percent": percent, **result})
                    if len({entry["result_sha256"] for entry in pair}) != 1:
                        raise RuntimeError("join-order optimization changed filtered vectors")
            finally:
                store.close()
    after = _source_snapshot()
    return report_envelope(
        suite="vector-scan-plan/counterfactual-v1", dataset_path=Path(__file__),
        config={"sizes": sizes, "dimension": dim, "batch_size": batch_size, "seed": seed,
                "target_scope_percentages": [0.1, 1, 2.1, 5, 25, 100]},
        records=[{"question_id": f"{cell['strategy']}-{cell['corpus_size']}-{cell['target_scope_percent']}",
                  "category": "scoped_vector_scan"} for cell in cells],
        metrics={"cells": cells, "hardware": _hardware(), "source_before": before,
                 "source_after": after, "source_stable": before == after,
                 "measurement_scope": "same scoped bounded vector scan; only join order differs",
                 "unmeasured": ["historical release behavior", "independent process repetitions",
                                "external contention", "end-to-end recall"]},
        source_paths=[_ROOT / name for name in _SOURCES] + [Path(__file__)],
        command=["python", "-m", "eval.vector_scan_plan", "--sizes", ",".join(map(str, sizes)),
                 "--dim", str(dim), "--batch-size", str(batch_size), "--seed", str(seed)],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,100000")
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
