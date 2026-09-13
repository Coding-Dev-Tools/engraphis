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
Every seed and worker engine explicitly requires `sqlite_durability="durable"`:
WAL with `synchronous=FULL`. The report records this policy and each ready worker's
effective PRAGMAs. A different effective policy fails startup. This is a software
durability configuration, not hardware power-failure validation. Historical
artifacts retain their original policy and bytes; they are not relabeled.

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

Freeze the two reference hosts before any primary run. On each selected machine,
`python -m eval.engine_capacity --host-identity` prints a read-only hardware
inventory, its SHA-256, an observed host-identity hash and the acceptance-policy
hash. The host hash covers the local hostname, OS and architecture; it omits the
raw hostname and is not hardware attestation. Assemble the observations into one
manifest, retaining its exact bytes for every cell and aggregation:

```json
{
  "schema": "engraphis-capacity-reference-hosts/v1",
  "policy_sha256": "ACCEPTANCE_POLICY_SHA256",
  "hosts": {
    "laptop16": {"host_identity_sha256": "LAPTOP_HOST_SHA256", "hardware_sha256": "LAPTOP_HARDWARE_SHA256"},
    "shared32": {"host_identity_sha256": "SHARED_HOST_SHA256", "hardware_sha256": "SHARED_HARDWARE_SHA256"}
  }
}
```

The placeholders must be replaced by the observed 64-character digests. The two
profiles require distinct host identities. A RAM range alone does not establish
the selected reference host. A run without the prebound manifest remains a
diagnostic and cannot later gain acceptance by attaching a manifest afterward.

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
python -m eval.engine_capacity --run-cell cell.json --reference-hosts reference-hosts.json --model-dir EXISTING_LOCAL_MODEL --model-sha256 FROZEN_DIRECTORY_DIGEST --output cell-evidence.json
```

The runner sets offline model-library modes, disables extractors, uses the explicit
local model selector and requires exact backends. It does not install, download,
provision, or invoke an answer model. Each repeat uses a disposable directory and
never accepts an existing user database. The configuration deadline must exceed
the arrival schedule. Worker startup and workload each have that deadline;
synchronous seeding currently has no hard interruption deadline. Seed/startup
errors, deadline failures and worker teardown failures remain in the report, with
all scheduled operations preserved. No missing operation receives an invented
latency. The command fails if any repetition is incomplete, including a teardown
failure after otherwise correct operations.

## Measurement and identity

- **Queue-inclusive latency:** parent scheduled arrival through parent receipt,
  including dispatch lag, IPC, queuing, engine operation and explicit canonical
  verification. Report operation time, verification time and the remaining
  queue/IPC time separately. Do not call the remainder pure database-lock wait.
- **Startup:** each worker's engine-open time, separate from seeding and queued
  operations. Processes/connections are fresh; OS page cache is warm. This is not
  a cold-disk result.
- **Phases:** this capacity runner retains the content-free `engine_recall`
  duration when provided by diagnostics. Its operation records do not yet copy
  the individual stage timings now available in factory `eval.performance` mode.
  Never infer those timings from the aggregate.
- **Memory:** sampled simultaneous RSS sum for the runner and its descendant
  processes starts before seeding and continues through populated-engine startup,
  workload, worker shutdown and temporary-database cleanup. A sampler thread
  requests observations every 50 ms; explicit phase boundaries also sample.
  `resource_observations.version=2` records attempts, successful/unavailable sample
  counts, observed peak, first/last attempt times and maximum attempt gap separately
  for seeding, startup, workload and teardown. The aggregate observed peak includes
  all four phases. Missing observations stay null. A phase is assigned at sample
  start; collection may cross its boundary. Shared pages can be counted more than
  once, inaccessible/exited descendants can be missed, and retained allocator
  memory contributes to later phases. GPU memory and unsampled transient peaks
  remain unmeasured. These observations cannot certify an allocation ceiling.
- **Outstanding work:** a content-free time series requests a sample each second
  and at phase boundaries after offered load begins. It records scheduled-due,
  submitted and parent-received counts, plus scheduled-outstanding, dispatch-pending
  and submitted-unreceived counts. The scheduled-outstanding count includes work
  awaiting dispatch, IPC, executing operations and canonical verification. It is
  not an instrumented database-lock queue. On failure, the offered count freezes
  when workload observation ends; teardown does not invent future arrivals.
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

The backlog assessment is deliberately finite and predeclared. For a positive
offered rate, it uses the active arrival interval (ending before post-arrival
drain), requiring at least ten seconds and at least two distinct sampled times in
each of five equal windows. Sustained growth is observed only when each successive
window's mean outstanding count increases and the last-minus-first mean exceeds
`max(1, offered_operations_per_second)`. Report the window means, sample counts,
observed slope and execution-complete status. A burst, short run or sparse series
reports the assessment as unavailable. No observed growth is not a general queue
stability result; `general_capacity_proof` always remains false. Sampling delays
and this rule can miss shorter or intermittent overload.

## Acceptance and outstanding execution

The existing [release criteria](RELEASE_READINESS.md) require queue-inclusive recall
p95 at 100k memories to be at most 1s for four agents on the 16 GiB reference host,
and at most 2s for sixteen agents on the 32 GiB reference host. Check every fresh
repetition for both backends and both workloads. Report tails in the other cells
without inventing additional limits. All cells require sampled lifecycle RSS below
75% of observed reference RAM, including startup, and no sustained backlog growth
under the declared finite-load rule. The runner binds these criteria, their policy
hash, the reference manifest and producer/validator source versions before execution.
Freeze any additional task-quality or disk limits separately; the runner does not
choose product SLOs after seeing measurements.
Require zero integrity violations; preserve failures, timeouts and unmatched
cells. Compare changes on identical seeds/model/hardware with randomized paired
order and repeat-level paired intervals. Do not use 2,000 correlated operations as
2,000 independent experimental repetitions. Report per-category tails; 20 erase
operations per repeat do not support a stable erase p99 claim on their own.

A strict complete-matrix aggregator now validates the primary matrix and reports
repeat-blocked 95% intervals. Lifecycle RSS sampling now includes startup, but a
strict cold-cache protocol, true transient allocation peaks, production workload calibration, restore drills,
same-subject contention cases, actual 10k/100k/native runs and the independent
agent corpus remain separate work. The runner intentionally never sets the
target-capacity or matrix-complete flags to true. Defaults change only after the
full evidence and the independent quality gates support that decision.

## Complete-matrix aggregation

```console
python -m eval.capacity_matrix --inputs CELL_01.json CELL_02.json OTHER_CELL_FILES --reference-hosts reference-hosts.json --require-acceptance --output matrix.json
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
RSS peak with observed physical RAM. Acceptance additionally requires the prebound
reference-host match, unchanged observed host identity, WAL/FULL in every worker,
all lifecycle phases observed, internally consistent RSS counts/peaks, and clean
completion without lost operations or failed teardown. The validator recomputes
backlog growth from the saved series and cross-checks received counters against
individual operation wall times. Missing/old observation versions, unavailable
startup samples, incomplete series and missing reference bindings cannot pass.

`protocol_observations_pass` records whether the complete matrix satisfies these
checks. `capacity_acceptance_pass`, `responsiveness_gate_pass` and
`resource_stability_gate_pass` additionally require observed-mode artifacts;
`--require-acceptance` fails unless `capacity_acceptance_pass` is true. Report-only
aggregation can still inspect old or partial observations without promoting them.
Malformed or contradictory observations fail validation. No primary acceptance
result is asserted by adding this validator.

These are sampled, finite-load acceptance observations. They cannot certify
unsampled transient/GPU peaks, general queue stability or measurement authenticity.
Independent task quality and publication remain separate, so `target_capacity_verified`,
`publication_ready` and measurement authenticity remain false. `--fixture` accepts
only explicitly synthetic test artifacts and preserves that label; it can exercise
structural observation checks but never pass real acceptance. Normal mode rejects
those fixtures. Neither aggregation mode executes a benchmark.
