# File-backed engine capacity protocol

`python -m eval.engine_capacity` prints the frozen version-one measurement matrix
without creating a database or calling a model. This protocol complements the
existing vector-index microbenchmarks; it does not replace coding-task acceptance.
The runner currently emits individual cell artifacts, not a publication decision.

## Primary matrix and sampling

| Axis | Declared values |
| --- | --- |
| Hardware | Personal 16 GiB laptop; shared 32 GiB host |
| Starting live memories | 10,000; 100,000 |
| Actual backend | NumPy; sqlite-vec, with fallback forbidden |
| Independent engine processes | 1; 4; 16 |
| Workload | Recall; mixed 80% recall / 15% remember / 4% correct / 1% erase |
| Repetitions | Five fresh databases and process groups per cell |
| Operations | 2,000 per repetition, including failures |
| Offered arrival rate | One operation per agent per second: 1, 4 or 16 total |

This is **48 primary cells, 240 repetitions and 480,000 scheduled operations**.
One million starting memories is a separate stress track. Additional repetitions,
arrival rates or datasets must be labeled as additional experiments and cannot
replace missing primary cells. The specified arrival schedule alone takes about
58 hours across the entire primary matrix, before seeding, startup or overhead.
The user has authorized local benchmarking and ordinary tests. Actual availability
of the selected hardware and existing local model constrains execution; paid
evaluations retain their separate approval boundary.

Each cell uses SQLite on a real local file, populated through
`remember_with_resolution`. Seed time includes actual embedding, resolution and
index maintenance. Forty repositories share the workspace when the dataset is
large enough. A repeat retains one file-backed database while each worker opens
its own engine in a newly spawned process. No connection is inherited or shared
as a Python object. Recall keeps ordinary reinforcement and enables diagnostics.

The generated workload contains explicit keyed retention facts and deterministic
operation schedules. Read targets are disjoint from correction/erasure targets;
this provides an unambiguous correctness check while writes compete for the same
database. New writes must be additions; corrections must close the old fact and
create the expected live content; erased memories must be absent. These are
synthetic integrity checks, not a realistic distribution of coding tasks or a
test of simultaneous contradictory writes to the same subject.

## Safe plumbing smoke

```console
python -m eval.engine_capacity --smoke --concurrency 4 --output capacity-smoke.json
python -m pytest -o addopts='' tests/test_engine_capacity.py tests/test_coding_acceptance.py -q
```

The smoke has 16 generated starting memories, 100 operations and one repetition.
It uses the dependency-light hashing embedder and a burst arrival schedule. It
must always report `target_capacity_verified=false`,
`primary_matrix_complete=false` and `independent_task_quality=false`. Native smoke
is explicit (`--backend sqlite-vec`); a missing native backend is an error, not an
invitation to install it or substitute NumPy. An existing output file is not
silently overwritten: the shared benchmark writer verifies or rejects it.

## Running one primary cell

On the selected hardware with an available pinned local model, save a Cell
configuration such as:

```json
{
  "size": 100000,
  "concurrency": 16,
  "operations": 2000,
  "repeats": 5,
  "backend": "numpy",
  "workload": "mixed",
  "hardware": "shared32",
  "dimension": 384,
  "token_budget": 1500,
  "seed": 20260905,
  "arrival_rate": 16,
  "timeout_s": 7200,
  "smoke": false
}
```

The dimension must match the selected real model; 384 above is an example, not a
model selection. Full cells require a pre-existing semantic model directory,
its exact digest, and `psutil`. The digest is SHA-256 of `canonical_json` for the
mapping of relative POSIX file names to file SHA-256 values, sorted by name.
Symlinks are rejected. Freeze and review that inventory before execution.

```console
python -m eval.engine_capacity --run-cell cell.json --model-dir EXISTING_LOCAL_MODEL --model-sha256 FROZEN_DIRECTORY_DIGEST --output cell-evidence.json
```

The runner sets offline model-library modes, disables extractors, uses the explicit
local model selector and requires exact backends. It does not install, download,
provision, or invoke an answer model. Each repeat uses a disposable directory and
never accepts an existing user database. The configuration deadline must exceed
the arrival schedule; deadline failures remain in the denominator.

## Measurement and identity

- **Queue-inclusive latency:** parent scheduled arrival through parent receipt,
  including dispatch lag, IPC, queuing, engine operation and explicit canonical
  verification. Report operation time, verification time and the remaining
  queue/IPC time separately. Do not call the remainder pure database-lock wait.
- **Startup:** each worker's engine-open time, separate from seeding and queued
  operations. Processes/connections are fresh; OS page cache is warm. This is not
  a cold-disk result.
- **Phases:** retain the content-free `engine_recall` duration when provided by
  diagnostics. Embedding, candidate discovery, ranking and packing are not yet
  independently timed. Never infer those timings from the aggregate.
- **Memory:** sampled simultaneous RSS sum for the runner and its descendant
  processes during operations. Report sample count and observed peak. Shared
  pages can be counted more than once; transient peaks and seeding/startup memory
  are not measured. This is insufficient by itself to certify a RAM ceiling.
- **Storage:** final database/WAL/shared-memory sizes after worker shutdown.
  Checkpointing can change these values; they are not peak disk usage.
- **Evidence:** use the existing benchmark envelope and immutable JSON/SHA-256
  writer. Include HEAD and dirty-state identity, source bytes before/after,
  generator/configuration/schedule identities, local model digest, requested and
  actual backend/dimension/capability, package versions, hardware/RAM, Python,
  SQLite, BLAS environment and named regex context counter revision. Raw source
  text and full retrieval traces are not written into result records.

Each scheduled operation has an outcome even if a worker fails. Missing timing
observations stay absent, not zero. Report measured counts and failures alongside
per-operation and aggregate p50/p95/p99, received-operation throughput and all five
repeat results. Source or model-byte drift invalidates the run. A host matching the declared
RAM range is a reported observation, not proof of representative hardware.

## Acceptance and outstanding execution

Freeze task-quality limits and latency/RAM/disk SLOs for each target before running
the matrix. The runner does not choose product SLOs after seeing measurements.
Require zero integrity violations; preserve failures, timeouts and unmatched
cells. Compare changes on identical seeds/model/hardware with randomized paired
order and repeat-level paired intervals. Do not use 2,000 correlated operations as
2,000 independent experimental repetitions. Report per-category tails; 20 erase
operations per repeat do not support a stable erase p99 claim on their own.

A strict complete-matrix aggregator now validates the primary matrix and reports
repeat-blocked 95% intervals. The strict cold-cache and startup/peak-memory
protocol, production workload calibration, restore drills,
same-subject contention cases, actual 10k/100k/native runs and the independent
agent corpus remain separate work. The runner intentionally never sets the
target-capacity or matrix-complete flags to true. Defaults change only after the
full evidence and the independent quality gates support that decision.

## Complete-matrix aggregation

```console
python -m eval.capacity_matrix --inputs CELL_01.json CELL_02.json OTHER_CELL_FILES --output matrix.json
```

Supply exactly 48 cell files and their original SHA-256 sidecars. The aggregator
requires five distinct numbered repetitions per cell, unique execution IDs across
all 240 repetitions, and all 2,000 scheduled outcomes in each repetition. Older
artifacts without execution identities cannot establish this contract by having
new IDs attached after the run. Source/model changes, backend fallback, altered
tokenizer/configuration, mixed software environments within a hardware profile,
changed workload schedules, record/summary disagreement and duplicate cells fail
validation. Model/software/source identities must agree across the matrix.

Timeouts and failed workers retain all scheduled outcomes; absent timings remain
absent and failed. `matrix_structurally_complete` describes the artifact structure,
while `all_measurements_complete` separately describes the observed executions.
Each cell reports operation counts, failures, per-repeat p50/p95/p99 and a 95%
bootstrap interval for the mean of five repetition means. The resampling unit is
the fresh database/process repetition, not each operation. Five blocks provide
exploratory uncertainty; identical blocks are explicitly degenerate. No interval
pretends to certify the sparsely sampled erasure p99.

Hardware gates recompute the declared RAM-profile match and compare every sampled
RSS peak with observed physical RAM. Passing these observations cannot certify
startup/transient peak memory or a latency SLO. The aggregator consequently keeps
`target_capacity_verified`, `publication_ready` and measurement authenticity false.
Its `--fixture` mode accepts only explicitly synthetic test artifacts and preserves
that label; normal mode rejects those fixtures. Neither mode executes a benchmark.
