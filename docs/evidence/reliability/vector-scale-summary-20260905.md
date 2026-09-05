# File-backed scale evidence, 2026-09-05

Both corrected matrices completed all six corpus/concurrency cells, all exact-result parity checks, mixed writes, and durable reopen checks. The native mirror rebuild also passed. This is synthetic scoped index/storage evidence; it does not establish semantic recall quality or coding-agent task success.

**Capacity limits remain.** At 100k total records, 16-worker p50 latency is 8.633s for NumPy and 5.296s for sqlite-vec. Native reopen takes 35.132s and a full mirror rebuild takes 61.313s. These results do not establish the 100k concurrent agent operating target; no backend/ranking/grounding default was changed.

**Scope tradeoff.** The selected first-sorted-batch strategy measured 235.479ms at a 5% eligible scope versus 132.031ms for repeated scope-driven batches (about 78% slower in this single comparison). It removes the severe unordered-probe cliff at 2.1% (55.942ms versus 240.465ms), and reduces the initial candidate scan cost at 25% (496.087ms versus 1326.383ms) and 100% (1084.019ms versus 16185.526ms). These are descriptive method comparisons on one corpus/process, with cache/order effects; they are not release speedups or SLO confidence bounds.

**Provenance boundary.** The slow complete NumPy comparison is our initial bounded-matrix candidate. Released/base code materialized its matrix once; the initial candidate latency must not be described as released Engraphis performance. The FTS ablation forces the former deletion method on otherwise current code, and the native verification ablation preserves the former reverse-scan oracle. Neither is a historical release benchmark.

**Protocol.** File-backed disposable SQLite, 10k/100k total records, 256-dimensional normalized PCG64 synthetic vectors, seed 20260731, four synthetic repository scopes, 25% eligible for the timed searches, k=10, 16 distinct queries, one warmup pass and two timed passes (32 search samples/cell), concurrency 1/4/16, four mixed writes per concurrency, and batches of 500 writes. Mixed phases use 16 reads and four single-record commits per cell. No model/tokenizer runs: precomputed vectors only. NumPy and native runs were sequential, with team CPU-heavy gates paused and the observed unrelated pytest process allowed to exit before timing.

Host: Intel Core i7-10700KF at 3.80GHz, 16 logical CPUs, 34,221,301,760 bytes RAM, AMD64, SQLite 3.49.1; sqlite-vec 0.1.9 was loaded from an isolated install. OPENBLAS_NUM_THREADS, OMP_NUM_THREADS and MKL_NUM_THREADS were all 1. Background host activity was not controlled as a laboratory experiment.

**Complete search cells.** Latency includes execution and shared-store lock waits, excludes executor queue time; throughput includes queue time. RSS is current resident memory after a cell. Peak RSS is cumulative for the process, not an isolated per-cell peak.

| Backend | Records | Workers | p50 ms | p95 ms | p99 ms | Searches/s | RSS MiB | Process peak MiB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| NumPy | 10000 | 1 | 58.316 | 80.819 | 85.610 | 15.997 | 50.79 | 55.24 |
| NumPy | 10000 | 4 | 268.058 | 393.975 | 416.295 | 13.748 | 50.52 | 55.71 |
| NumPy | 10000 | 16 | 1042.093 | 1099.690 | 1107.065 | 15.236 | 51.71 | 57.01 |
| NumPy | 100000 | 1 | 495.837 | 542.234 | 550.786 | 2.024 | 51.29 | 61.76 |
| NumPy | 100000 | 4 | 2340.836 | 2500.777 | 2525.383 | 1.738 | 51.19 | 61.78 |
| NumPy | 100000 | 16 | 8632.554 | 8806.378 | 8905.387 | 1.811 | 51.88 | 62.94 |
| sqlite-vec | 10000 | 1 | 39.591 | 56.319 | 57.810 | 23.790 | 49.23 | 53.61 |
| sqlite-vec | 10000 | 4 | 156.052 | 199.244 | 207.034 | 24.832 | 49.39 | 53.61 |
| sqlite-vec | 10000 | 16 | 610.213 | 792.479 | 806.125 | 23.990 | 48.88 | 53.61 |
| sqlite-vec | 100000 | 1 | 357.100 | 382.293 | 387.018 | 2.808 | 50.99 | 59.86 |
| sqlite-vec | 100000 | 4 | 1487.517 | 1703.431 | 1736.983 | 2.619 | 51.11 | 59.86 |
| sqlite-vec | 100000 | 16 | 5296.068 | 6475.360 | 7147.615 | 2.841 | 49.91 | 59.86 |

**Storage and durability.** Population uses the real canonical Store/FTS/vector write path, bypassing extraction, embedding, resolution and full MemoryEngine writes. Native population includes publication verification. Reopen uses a new connection; the operating-system disk cache was not flushed. Disk is DB+WAL+SHM after final reopen, before closing the disposable store.

| Backend | Records | Initial open ms | Reopen ms | Population s | Rebuild s | Final disk MiB | Durable memories/vectors |
|---|---:|---:|---:|---:|---:|---:|---:|
| NumPy | 10000 | 28.210 | 14.707 | 2.879 | not applicable | 21.52 | 10012 / 10012 |
| NumPy | 100000 | 26.410 | 14.481 | 31.189 | not applicable | 209.70 | 100012 / 100012 |
| sqlite-vec | 10000 | 38.671 | 849.240 | 5.483 | 1.691 | 32.25 | 10012 / 10012 |
| sqlite-vec | 100000 | 34.038 | 35132.382 | 104.001 | 61.313 | 314.48 | 100012 / 100012 |

**Mixed cells.** Each has one writer and the listed reader concurrency. The corpus grows by four committed records after each cell; raw JSON records the exact starting count. Four write samples are descriptive only.

| Backend | Initial records | Readers | Read p50 ms | Read p95 ms | Write p50 ms | Write p95 ms | Committed writes |
|---|---:|---:|---:|---:|---:|---:|---:|
| NumPy | 10000 | 1 | 61.010 | 66.503 | 57.568 | 60.726 | 4 |
| NumPy | 10004 | 4 | 230.828 | 250.557 | 227.260 | 237.154 | 4 |
| NumPy | 10008 | 16 | 528.255 | 961.893 | 250.665 | 525.618 | 4 |
| NumPy | 100000 | 1 | 640.612 | 689.457 | 584.104 | 590.260 | 4 |
| NumPy | 100004 | 4 | 2271.882 | 2609.469 | 2316.833 | 2553.175 | 4 |
| NumPy | 100008 | 16 | 4356.733 | 7828.967 | 1778.842 | 4475.820 | 4 |
| sqlite-vec | 10000 | 1 | 36.373 | 49.005 | 6.143 | 11.074 | 4 |
| sqlite-vec | 10004 | 4 | 155.163 | 206.627 | 5.902 | 38.469 | 4 |
| sqlite-vec | 10008 | 16 | 574.131 | 657.220 | 51.917 | 113.089 | 4 |
| sqlite-vec | 100000 | 1 | 393.290 | 511.251 | 91.027 | 129.928 | 4 |
| sqlite-vec | 100004 | 4 | 1568.690 | 1876.138 | 136.675 | 514.114 | 4 |
| sqlite-vec | 100008 | 16 | 6687.569 | 6919.419 | 627.436 | 1279.725 | 4 |

**Measured changes and retained baselines.**

| Artifact | Status and interpretation | SHA-256 |
|---|---|---|
| [vector-scale-numpy-corrected-20260905.json](vector-scale-numpy-corrected-20260905.json) | Complete final NumPy matrix | `ab4ba40faad58b7ecd3aff96a50ac45742f78f670ed79bed32b1802162d54e6b` |
| [vector-scale-sqlite-vec-corrected-20260905.json](vector-scale-sqlite-vec-corrected-20260905.json) | Complete final sqlite-vec 0.1.9 matrix | `3cae847fcff2a1c0c5c680b2b050912ff2eb35e488a55be10f23832a4e6c787c` |
| [vector-scale-numpy-20260905.json](vector-scale-numpy-20260905.json) | Complete initial bounded candidate; not released/base performance | `049d22200d0e088f03f42a13c8a0ae4031c3c9c3c12113c3f6196855e2f0d479` |
| [vector-scale-incomplete-baseline-20260905.json](vector-scale-incomplete-baseline-20260905.json) | Incomplete FTS baseline: about 396s, confirmed 40k/100k population checkpoint; no recovered latency samples | `45c4f61518012e35e09b6d274dec3015d00d4948f2b8279c0f56760dec080544` |
| [vector-scale-native-incomplete-20260905.json](vector-scale-native-incomplete-20260905.json) | Incomplete initial native coverage run: about 430s, six emitted read-parity checkpoints; 100k mixed/rebuild/reopen unfinished and no recovered latency samples | `1b73068e071f4e39b644522a25512282fe986fda9ad5724ad44369eb25a3ceb1` |
| [fts-insert-counterfactual-20260905.json](fts-insert-counterfactual-20260905.json) | Same-current-code deletion ablation: 10k population 16.650s forced former delete vs 2.495s corrected insert | `b0a4603e983246790682ee5db4b5d5fab65b8ffb72dce002c85049f5112bff16` |
| [native-coverage-counterfactual-20260905.json](native-coverage-counterfactual-20260905.json) | Same-store verification ablation: 10k reverse scan 2.029s vs full-content verification plus cardinality 0.889s | `df0eec12c370c5ee06ec2125e5e8b275ef6c654dab820f25427211cede3faa17` |
| [vector-scan-plan-20260905.json](vector-scan-plan-20260905.json) | Scoped JOIN vs vector-first selectivity comparison | `bb4f15d60d89e9e153456fb238acc3ea13fec68cfc673fdffb922a8f4e6ed63b` |
| [vector-scan-adaptive-20260905.json](vector-scan-adaptive-20260905.json) | Four scan candidates at 0.1/1/2.1/5/25/100%; selected scoped_first_sorted_batch | `a421a8e73180104f0f5612780dfc929b678083e59494beab66a6bb621ebcc6e7` |

The two final `.checkpoint.json` files contain completed receipts matching their canonical artifact hashes. If a future run is interrupted, checkpoints atomically retain actual completed cells and timings with config/source identity and status `incomplete`. No missing native baseline numbers were reconstructed.

**Final source identity.** Both corrected reports have `source_stable=true`, identical before/after hashes, the same generated corpus/query identities, and matching result-ID hashes for every corresponding cell. Current file hashes were checked again after completion.

Git revision: `c37ba0eb18408fe500cd70cd73fb5dcd7679b89c` (dirty working tree). Measured tracked-source diff SHA-256: `b5de7d748eb33d21b2ccbed2d84b81216f40e479eea82ecccc4a1c747177b1fe`.

| Measured source | SHA-256 |
|---|---|
| `engraphis/backends/vector_numpy.py` | `c598831bea547824cfe08844816fa79857d3617cb0631238f95dad955a424f72` |
| `engraphis/backends/vector_sqlitevec.py` | `6148e14ceaacc19239b64c642a3fba0e98797c78cec356210157afadf475a08b` |
| `engraphis/core/interfaces.py` | `5f0ace8f64472100f31b591b0b3822e7fb21eae84813d69325bcb4b9108d4d22` |
| `engraphis/core/schema.py` | `99f3647602e2659035d5977e9d9814b378e58c2849483821e6e01d777b668686` |
| `engraphis/core/store.py` | `007272ec42011faaae25bdbaf8905cf0d2945bf6f451b60aa7a90fff0550debd` |
| `engraphis/core/vector_search.py` | `75800052e573e9af01c6fd098eaf8647c3c45cd7eb2601dae05d42359e19b234` |
| `eval/benchmark.py` | `ed828e3548e8fb05ffafe9479791e545e56504421ff88676e4bf4466d6beaf91` |
| `eval/vector_scale.py` | `3f9c327d9eca1a857512ddc208e972933be1a7d4fa7fa0017aca0cbf8fe7bb6d` |
| `eval/vector_scale_storage.py` | `bda0c1139596eb9232cd75c826fc941101b6116bae32ad7895525d43f9d35484` |

**Replay commands.** Use fresh output names because artifacts and existing checkpoints are not overwritten. The optional native dependency must resolve to sqlite-vec 0.1.9; the command below reuses the isolated Windows install prepared for this run.

```powershell
$env:ENGRAPHIS_EXTRACTOR='none'
$env:PYTHONIOENCODING='utf-8'
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
$env:PYTHONPATH=Join-Path $env:TEMP 'engraphis-review-sqlitevec-c37ba0e'
python -m eval.vector_scale --file-backed --backend numpy --sizes 10000,100000 --dim 256 --queries 16 --iterations 2 --warmups 1 --k 10 --seed 20260731 --concurrencies 1,4,16 --mixed-writes 4 --batch-size 500 --tenants 4 --progress --output docs/evidence/reliability/vector-scale-numpy-replay.json
python -m eval.vector_scale --file-backed --backend sqlite-vec --sizes 10000,100000 --dim 256 --queries 16 --iterations 2 --warmups 1 --k 10 --seed 20260731 --concurrencies 1,4,16 --mixed-writes 4 --batch-size 500 --tenants 4 --progress --output docs/evidence/reliability/vector-scale-sqlite-vec-replay.json
python -m eval.fts_insert_scaling --sizes 1000,5000,10000 --dim 256 --batch-size 500 --seed 20260731 --output docs/evidence/reliability/fts-insert-replay.json
python -m eval.native_coverage_scaling --sizes 1000,5000,10000 --dim 256 --batch-size 500 --seed 20260731 --output docs/evidence/reliability/native-coverage-replay.json
python -m eval.vector_scan_plan --sizes 100000 --dim 256 --batch-size 500 --seed 20260731 --output docs/evidence/reliability/vector-scan-replay.json
```

The current scan diagnostic includes the selected production iterator; its earlier saved `adaptive_snapshot` comparison was the unordered-probe candidate. Replaying the final source therefore does not recreate that abandoned candidate. The selected `scoped_first_sorted_batch` method remains explicit in the diagnostic.

**Validation and open evidence.** Final focused storage/sync/NumPy/native/FTS/snapshot/checkpoint/ablation gate: 494 passed, 3 skipped. Ruff, targeted Pyright and diff whitespace checks passed. The final full public gate passed 4,736 tests with 37 skipped on stable production/test source; see [validation.json](validation.json) and [the source receipt](public-complete-source-final.json). No descendants, paid model calls, servers, commits or pushes were used by this worker.

One million records is supported as opt-in input but was not run. Independent process repetitions, process contention, semantic embeddings, extraction/resolution latency, full hybrid recall/context packing, tenant workload distributions beyond the diagnostic scopes, and real coding-agent task outcomes remain unmeasured. Percentiles from 32 search samples and four mixed-write samples do not establish tail-SLO confidence. The improvements and remaining limits should guide the next evaluation phase, without treating these synthetic timings as product-quality or release claims.

**Remaining temporary artifacts.** Automatic approval review rejected the optional PowerShell action `Remove-Item -LiteralPath <resolved benchmark directory> -Recurse -Force` targeting `C:\Users\jomie\AppData\Local\Temp\egr-scale-1624f50x` and `C:\Users\jomie\AppData\Local\Temp\egr-scale-vxkh5zh0`. The only stated reason was `blocked by policy`. No deletion ran. Both directories remain untouched; cleanup was not retried. Canonical evidence artifacts are unaffected.
