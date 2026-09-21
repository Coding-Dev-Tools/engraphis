# Benchmark expansion execution

This campaign separates local fixture evidence, implementation-authored coding outcomes,
external retrieval diagnostics, official reader scores, and operational capacity. None of these
labels is interchangeable. The August screenshot is traced in
[BENCHMARK_CHANGE_COVERAGE.md](BENCHMARK_CHANGE_COVERAGE.md); original artifacts are retained.

The campaign uses GPT-5.6 Luna with medium reasoning through **Codex OAuth only** for coding
readers and peer extraction. The native CLI reuses its existing ChatGPT sign-in; credential
files are never read or copied by the runner. API-key accounts and inherited gateway settings
are rejected. Its five core arms remain `no_memory`, `full_history`, `lexical`, `dense`, and
`hybrid`. The companion adds `dense_lexical`, Mem0 OSS, and Graphiti. Peers use their open source
packages and isolated local stores, not their managed services. Unsupported adapter capabilities
are explicit unscored attempts, not fabricated failures or product quality conclusions.

## Environment and freeze

Use a separate Python 3.12 environment. The tested Windows package versions are in
[`benchmark-requirements-windows-py312.txt`](../eval/configs/benchmark-requirements-windows-py312.txt),
with the observed distribution inventory in
[`benchmark-environment-windows-py312.json`](../eval/configs/benchmark-environment-windows-py312.json).
This is an exact version lock for this platform, not a cross-platform wheel-hash lock. Optional
packages are confined to the evaluation environment; the NumPy-only core dependency contract is unchanged.

```powershell
uv venv .private-eval/benchmark-20260915/venv --python 3.12 --seed
$py = '.private-eval/benchmark-20260915/venv/Scripts/python.exe'
uv pip install --python $py -r eval/configs/benchmark-requirements-windows-py312.txt
uv pip install --python $py --no-deps -e .
& $py -m eval.coding_corpus --verify
```

The 400 scenarios have 40 family labels and the frozen 80/80/240 split. They are generated from
shared templates. Family counts therefore do not imply 40 independent real repositories.
The corpus is `implementation_team`, and cannot satisfy independent-human acceptance or
leadership gates. Answer keys and oracle sources are verified by SHA-256 and never supplied
as reader context. Candidate code runs inside the declared Docker image; oracle expected
values remain outside that container. Only declared source files can be edited.

Prepare into new paths after all relevant code and dependency changes are finished:

```powershell
& $py -m eval.benchmark_campaign --prepare `
  --manifest .private-eval/benchmark-20260915/campaign.json `
  --companion .private-eval/benchmark-20260915/comparisons.json `
  --dependency-lock eval/configs/benchmark-environment-windows-py312.json
```

The original, retired API-route inputs are preserved at
[`benchmark-campaign-20260916.json`](../eval/configs/benchmark-campaign-20260916.json) and
[`benchmark-comparisons-20260916.json`](../eval/configs/benchmark-comparisons-20260916.json).
Their source-byte bindings cover the earlier implementation on base `ca790261`; the base
commit alone does not reproduce those changes. The OAuth-only runner rejects this transport.
The original [OAuth scored run](../eval/configs/benchmark-campaign-oauth-v2-20260916.json)
binds source commit `7370e148d7793aeb632bc92494528376ec35267e` and campaign
`59560230f21a371a03de13aab2c055ab49d56ac47c5fbcd0a05835220c93b7d4`.
The preceding OAuth manifest is retained with a pre-dispatch failure: its call identifier was
rejected before any reservation or generation. The successor includes the corrected identifier
and public record envelope. Preparation itself creates no approval.

To reproduce that pilot, use an isolated checkout at that exact source commit and copy
the frozen OAuth successor manifest and checksum into it from this review package. Later result
and documentation commits are deliberately separate from the measured source revision. Restore
the declared dependency and model bytes, verify the existing native ChatGPT OAuth sign-in, and
prepare a new private approval/results location for a separately authorized replication. To
inspect the existing run, retain its exact original approval, results directory and source checkout.
It is stopped at an uncertain call, so ordinary resume deliberately stops there. Do not replay
its completed or uncertain calls. The explicitly bound continuation below handles only eligible
cells that were never attempted.

The manifest binds reader instructions, corpus bytes, model revisions, dependency versions,
source bytes, repository revision, stage membership, context budgets and repetitions. A source
edit invalidates execution; create a new campaign rather than modifying a frozen manifest.
The public corpus deliberately remains accessible to inspection, so its holdout is a procedural
selection boundary, not a secret or independent evaluation service.

## Spending and recovery

Every model-using stage needs separate owner authorization of its generated proposal. See
[BENCHMARK_STAGE_BUDGETS.md](BENCHMARK_STAGE_BUDGETS.md). No authorization receipt is created by
preparation. The `--approval` file must bind the campaign, stage, durable journal location,
approved prices and limits, an authorization reference, and a bounded UTC validity window.
This receipt is an operator attestation; its hash proves integrity, not the identity or consent
of the owner. Do not turn a proposed budget into an approval without the owner's instruction.

Use the same arguments as preparation, replace `--prepare` with `--execute --stage <name>`,
and add `--approval <reviewed-private-receipt.json> --results <private-results-directory>`.
The runner reserves the full API-price usage proxy before dispatch, disables native request
and stream retries, and verifies the account type, effective model, reasoning effort and usage.
Codex has no exposed provider hard output cap here: output limits are observed-usage acceptance
thresholds. Subscription usage is not reported as API spending. The per-stage journals form
the campaign reservation record; they do not implement an additional shared cash ceiling across separately approved
stages. Input, extraction, correction and reader calls all consume that stage's reservation.
The journal stores exact completed response text privately for byte-identical recovery.

The core pilot's retrospective eligibility mask excludes all arms/budgets of its invalid
long-document fixture. A continuation binds that mask, the original ledger, checkpoint set,
approval and public result before dispatching only previously unattempted eligible cells.
Original terminal errors are preserved. The continuation's lower remaining allowance counts
every original reservation, including unknown usage, and cannot expand the approved stage.
Both parent and child directories are locked against concurrent campaign dispatch. The
600-second continuation deadline is a separate transport cohort from the original 180 seconds;
it cannot be hidden inside an equivalent-latency claim.

The [frozen continuation manifest](../eval/configs/benchmark-campaign-continuation-20260916.json)
binds source revision `cc25ac5b59e3b5e04a79ee09b8d7454d01cb31c1` and campaign
`79996b53272a76b2836d4835dada4be321dd26d1820ee2d260f4eadc92b14cad`.
The parent directory's durable `continuation-allocation.json` binds this exact child ledger,
eligible cell set and allowance. Moving execution to a second results directory cannot obtain
a fresh allowance. Parent evidence, approval validity and source bytes are checked before
every initial or corrective generation. An interrupted child marker also prevents replay.

Run the authorized continuation from its frozen source checkout, preserving the original
private approval, ledger and checkpoints at their bound locations:

```powershell
& $py -m eval.campaign_continuation `
  --parent-manifest eval/configs/benchmark-campaign-oauth-v2-20260916.json `
  --companion eval/configs/benchmark-comparisons-20260916.json `
  --eligibility eval/configs/benchmark-core-pilot-eligibility-20260916.json `
  --audit-artifact docs/benchmark-evidence/core-pilot-corpus-validity-20260916.json `
  --public-artifact docs/benchmark-evidence/core-pilot-oauth-v2-20260916.json `
  --parent-approval .private-eval/benchmark-20260915/core-pilot-oauth-v2-approval.json `
  --parent-results .private-eval/benchmark-20260915/core-pilot-oauth-v2 `
  --child-results .private-eval/benchmark-20260915/core-pilot-oauth-continuation `
  --dependency-lock eval/configs/benchmark-environment-windows-py312.json `
  --execute --public-output docs/benchmark-evidence/core-pilot-combined-20260916.json
```

Omit `--execute` and `--public-output` for the read-only eligibility and allowance preview.
This command is recovery of the existing run, not authorization for a replication. A completed
eligible subset still returns exit code 2 when the original raw experiment retains an error.
Read `valid_missing_attempts`, raw statuses and exclusions separately before interpreting it.

The retained continuation itself is now stopped at a terminal native failed turn: 75 cells
completed, one errored, and 14 were never attempted. Re-running the command preserves that stop;
it does not skip the error or retry the failed call. A different child directory is rejected by
the allocation receipt. Further execution needs a new reviewed recovery artifact that preserves
both cohorts, charges every reservation and selects only genuinely unattempted cells. The
existing core allowance is not exhausted; no additional account funding is implied.

Each reader gets an ephemeral native thread with environment access, MCP servers, plugins,
agent delegation and tools disabled. Inherited global Codex instructions remain present;
their bytes are frozen and common across arms. They are included in reported model usage.
Native model rerouting, unexpected tools, compaction and missing usage invalidate the attempt.
Codex version and instruction drift stop execution before a generation. The host's persistent
login and API environment are left unchanged; gateway variables are removed only from children.

A completed attempt is never dispatched again. An interrupted reservation is uncertain and
requires reconciliation with provider usage; the runner does not automatically retry it.
Errors and missing attempts remain visible. Do not delete journals, move an approved run to a
new location, or silently replace an error with a successful retry. Archive a rejected attempt
and obtain approval for any replay or additional stage. Continuing never-attempted cells within
the already approved pilot requires a bound recovery receipt, not a second authorization.

After execution, `python -m scripts.audit_coding_oauth_run --manifest <manifest.json>
--report <public-report.json> --results <private-results> --private-inventory <new-private.json>
--output <new-public-audit.json>` joins checkpoint, ledger and native journal evidence without
calling a model. The inventory hashes are taken after execution; they do not prove dispatch-time
sealing. Native thread-start model identity and absence of rerouting are checked, but final-turn
model metadata was not captured. An incomplete audit remains `BLOCKED` and preserves its causes.
The continuation cohort report retains the full 150-cell stage contract, so its audit also
reports cells intentionally covered by the parent or excluded by the eligibility mask. Use
the combined report for total coverage. The stopped correction additionally exposed a partial
checkpoint gap: native ledgers retain 212 completed calls across both cohorts, while row-level
usage contains 211. The audit records the difference without rewriting the failed checkpoint.

Validation selection is derived from actual checksummed validation outcomes, with no missing
attempts, errors, or critical violations and fully scored candidate outcomes. Unsupported
companion cells remain explicitly counted. Freeze selection before held-out work:

```powershell
& $py -m eval.benchmark_campaign --manifest <campaign.json> --companion <comparisons.json> `
  --dependency-lock eval/configs/benchmark-environment-windows-py312.json `
  --results <private-results-directory> --freeze-selection --selection-receipt <new-receipt.json>
```

The held-out stage requires that exact receipt and revalidates its source and checkpoint
bindings. Any tuning after inspecting holdout results requires a new holdout. Broader ranking
or default changes still require the existing acceptance gates. No engine default is promoted
by campaign preparation, project-authored intervals, or a green offline suite.

## Metrics and matched inputs

- Candidate recall scores all retrieved memories. Packed recall scores only source IDs actually
  represented in the context; answer-token evidence coverage uses the actual excerpts.
- Task success is the deterministic repository oracle result. It is independent of citation
  validity, required-evidence retention, answer-token overlap, and abstention.
- Required-source citation agreement is not entailment grading. Prose completeness and semantic
  citation support remain ungraded until a separately validated grader is executed; the runner
  leaves them absent rather than promoting lexical overlap to answer quality.
- Evidence tokens use the common RegexTokenCounter. Serialized JSON proxy measurements, actual
  Smart/Classic wrapper responses, complete model inputs and provider-reported usage have separate
  fields and boundaries. Wrapper journeys do not claim network/stdio transport capture.
- Paired descriptive 95% intervals resample repository-family means. Shared templates limit their
  independence; missing/unscored pairs are reported. Non-inferiority remains `indeterminate` when
  the evidence cannot support the existing one-percentage-point margin.

Full-history is the eligible chronological history within the same scope and time boundaries;
it is explicitly unsupported when it cannot fit a matched context ceiling. It is never silently
truncated or given a larger input allowance. The reader, source files, allowed edits, prompt,
oracle, correction limit and model remain identical across arms.

## External diagnostics

Use the exact source versions and checksums in
[`external-sources-20260915.json`](../eval/configs/external-sources-20260915.json). Data files and
per-case checkpoints stay private. The LoCoMo v2 repair manifest preserves the original three
declared fixes and declares one repeated gold-ID deduplication. The LongMemEval repair manifest
declares twelve empty non-answer turn omissions. Both fail on source drift or unused repairs;
neither drops a question. Original datasets and earlier repair manifests remain unchanged.

```powershell
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
& $py -m eval.external --dataset <locomo10.json> --format locomo --canonical --no-resolve `
  --embed-model sentence-transformers/all-MiniLM-L6-v2 `
  --embed-revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41 `
  --locomo-repair-manifest eval/datasets/locomo10_repair_manifest_v2.json `
  --checkpoint-dir <private-locomo-checkpoints> --artifact <new-public-safe-report.json>
& $py -m eval.external --dataset <longmemeval_s_cleaned.json> --format longmemeval --canonical --no-resolve `
  --embed-model sentence-transformers/all-MiniLM-L6-v2 `
  --embed-revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41 `
  --longmemeval-repair-manifest eval/datasets/longmemeval_s_cleaned_repair_manifest.json `
  --checkpoint-dir <private-longmemeval-checkpoints> --artifact <new-public-safe-report.json>
```

These commands measure full-source retrieval diagnostics at k=10 and a 1,500-token evidence
budget. `--no-resolve` keeps annotated dialogues distinct; it is recorded and is not the default
product write behavior. No QA reader or evaluator is called. A completed case resumes without
work; interrupted local-only cases require explicit `--restart-interrupted`, with the earlier
attempt retained. Do not run these alongside capacity or other performance measurements.

The per-directory runner lock is held by the operating system before the checkpoint manifest
is read or created. Its protocol marker file remains in the directory after the process exits;
the operating system releases ownership even after an abrupt exit. A second live runner is
rejected. Keep the lock file in place, and use `--restart-interrupted --checkpoint-dir <directory>`
to explicitly rerun an incomplete local case while retaining its earlier start and retry receipts.
Legacy PID-only, existing empty, or unrecognized lock files remain blocked for inspection; they
are not treated as evidence that a previous owner is dead. An exit before the first marker byte
is written can leave an empty file; use a new directory in that case. A recognized nonempty
marker prefix can be completed under the OS lock. The source-binding rule below applies to upgrades.

Checkpoint v2 also binds the actual embedder fingerprint and runtime package versions, and
revalidates question coverage for cached cases. A changed source, model, or environment requires
a new checkpoint directory; preserve earlier directories as historical evidence. Unscored
retrieval categories remain `null` in the diagnostic rather than becoming zero-score results.

Official LongMemEval-V2 has its own pinned Python 3.11 checkout/environment, prescribed Qwen
reader/embedding, default GPT-5.2 judge and 30 generated configurations. Its resource feasibility
and compute proposal are in [LONGMEMEVAL_V2_FEASIBILITY.md](LONGMEMEVAL_V2_FEASIBILITY.md).
The additional benchmark formats and prerequisites are listed in
[ADDITIONAL_BENCHMARK_DIAGNOSTICS.md](ADDITIONAL_BENCHMARK_DIAGNOSTICS.md).

## Capacity and user journeys

Run both backends' smoke cells before executing the 24-cell current-host campaign. Freeze the
local model directory SHA and observed host; run only one measured workload at a time.

```powershell
& $py -m eval.engine_capacity --smoke --backend numpy --concurrency 4 --output <numpy-smoke.json>
& $py -m eval.engine_capacity --smoke --backend sqlite-vec --concurrency 4 --output <native-smoke.json>
& $py -m eval.local_capacity_campaign --prepare --manifest <local-capacity.json> `
  --hardware shared32 --model-dir <immutable-local-model> --model-sha256 <directory-sha256>
& $py -m eval.local_capacity_campaign --manifest <local-capacity.json> `
  --execute --results <private-capacity-results>
```

The current-host plan contains 240,000 operations, 120 fresh repetitions, and about 29.17 hours
of scheduled arrivals before seeding/startup/teardown. Interrupted cells are not counted as
complete or silently replayed. A complete one-host result is still only half of the two-host
48-cell acceptance protocol. Resource monitoring, queue-inclusive latency and integrity checks
remain mandatory even when throughput looks favorable.

`python -m eval.user_journeys` executes seven small functional journeys covering imports,
Smart/Classic responses, session context, code bridges, concurrent corrections, index repair,
and erasure/restart. See [USER_JOURNEY_BENCHMARKS.md](USER_JOURNEY_BENCHMARKS.md) for each boundary.
They establish functional evidence and do not replace capacity or coding outcome measurements.
Retain a public source-bound artifact with
`python -m scripts.export_user_journey_evidence --output <new-artifact.json>`.

## Durable serial local queue

`eval.local_benchmark_queue` runs only allowlisted local diagnostic/capacity modules. A frozen
manifest binds producer bytes and every declared input file; changed inputs stop dispatch.
It verifies an existing upstream diagnostic before proceeding, runs one job at a time, and
retains per-job logs, started reservations, terminal checkpoints and `status.json` privately.
Failed or interrupted jobs require reconciliation; they are never automatically replayed.
Closing the app does not intentionally terminate a separately launched queue process, but
Windows shutdown or process termination interrupts it and is not reported as completion.

```powershell
& $py -m eval.local_benchmark_queue --manifest <new-local-queue.json> --prepare-from <job-spec.json>
& $py -m eval.local_benchmark_queue --manifest <local-queue.json> --execute --results <private-queue-directory>
```

The original run used
[`benchmark-local-queue-20260916.json`](../eval/configs/benchmark-local-queue-20260916.json),
launched as a hidden Windows process at 2026-09-16 03:04 UTC. The two backend smokes, full
LoCoMo/LongMemEval baselines and LoCoMo k=20 comparison completed before this queue; they are
frozen prerequisites and are not dispatched again. Five jobs completed before the queue stopped
at a job boundary on source drift at 04:06:59 UTC: Mem2ActBench, LongMemEval's larger-budget run
and comparison, and MAB conflict-resolution and test-time-learning. No workload was interrupted.
The [successor queue](../eval/configs/benchmark-local-queue-final-v4-20260916.json) contains only
MAB accurate retrieval, MAB long-range understanding, the LoCoMo-Plus Cognitive slice, and the
24 capacity cells. It retains the prior manifest hash and completed-job inventory. Three unexecuted
successor manifests remain as provenance for the final ledger, process-tree timeout and
continuation fixes. The fresh capacity plan binds source `cc25ac5b` before any full cell starts.
The queue starts after the coding pilot so measured workloads do not compete.
This successor launched at 2026-09-16 07:00:19 UTC; the launch receipt and live status below
identify the active process and current job.

Inspect the live state without starting a duplicate:

```powershell
Get-Content .private-eval/benchmark-20260915/local-queue-final/status.json
Get-Content .private-eval/benchmark-20260915/local-queue-final-process.json
```

The launch receipt identifies the Windows launcher process; `status.json` identifies the
Python runner, which can have a different PID under a virtual environment. Per-job results
and logs remain in the private queue directory. A frozen plan passing validation is not a
completed run. If a job fails, preserve its log/checkpoint and reconcile the reason before
authorizing an explicitly identified local recovery; do not delete the failed attempt.
The capacity host identity includes BLAS thread limits, so all three thread-count variables
shown above must remain `1` when validating or executing that plan.

The successor runner emits periodic heartbeats with the child PID, elapsed time, deadline and
runtime identity. Diagnostic jobs have six-hour timeouts; the capacity job has a 72-hour timeout,
with a separate whole-cell watchdog covering seeding and worker shutdown. Timeouts preserve
uncertain reservations for investigation. On Windows, the watchdog terminates the launcher and
its descendants, including virtual-environment child Python processes, and records teardown
counts. `--stop-after-job <id>` pauses only after the named
job is validated. The canonical capacity `summary.json` binds all cell hashes and reports
integrity, resource, latency and backlog gates separately; a zero process exit alone is insufficient.

Capacity summaries reuse the existing strict operation/resource validator and report 95%
intervals over five fresh database/process repetitions. They retain per-repeat tails, missing
timings, throughput and lifecycle outcomes. Five-repeat intervals are exploratory, and one-host
completion cannot grant the unchanged two-host acceptance gate.

## Reviewable public evidence

Export new immutable artifacts with checksum sidecars and `eval.benchmark.validate_report`.
Keep raw provider text, prompts, dataset records, oracle output, local paths and spending
journals private. Historical artifacts are not rewritten. The chart renderer consumes validated
artifact fields and recalculates percentages from counts; it does not accept display-only values.
Public preparation is not permission to merge, publish, release, or make leadership claims.
