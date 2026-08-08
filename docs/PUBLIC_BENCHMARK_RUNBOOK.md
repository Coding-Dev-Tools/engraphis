# Public benchmark runbook

This runbook is the operator sequence for a reproducible public run. It covers retrieval evidence
and, when separately configured, official end-to-end evaluation. The code and checked-in benchmark
contracts are authoritative: see [BENCHMARKS.md](../BENCHMARKS.md),
[eval/EVIDENCE.md](../eval/EVIDENCE.md), [eval/BASELINES.md](../eval/BASELINES.md), and
[docs/LUNA_BENCHMARK_PLAN.md](LUNA_BENCHMARK_PLAN.md).

## 1. Lock the run

Create an owner-controlled restricted run directory outside the repository or under an ignored
path. Set it once, then record one immutable manifest before execution:

```bash
export ENGRAPHIS_BENCHMARK_RUN_DIR=/path/to/restricted/benchmark-run
```

- repository commit, clean or dirty state, Python version, OS, hardware, package lock, and command;
- exact dataset and benchmark-repository revisions plus SHA-256 digests;
- embedding, reranker, reader, tokenizer, and evaluator model IDs and immutable revisions;
- configuration, prompt, chunking, scope, resolution, graph, reranker, retry, and seed settings;
- token counter definition and fixed budgets: `256, 512, 1024, 2048, 4096`;
- run ID, start time, operator, and output paths.

Canonical runs must use a clean worktree, complete source dataset, immutable revisions, and the
`engraphis-benchmark/v2` envelope. Never change the manifest after the first scored question.

### Two locked records

Use one `engraphis-public-benchmark-manifest/v1` execution manifest for each candidate, baseline,
and benchmark point. It identifies local dataset bytes, the checked-out commit, models, a pinned
profile, and restricted/public output paths. Run it through the allowlisted orchestrator:

```bash
python -m scripts.run_public_benchmark --manifest "$ENGRAPHIS_BENCHMARK_RUN_DIR/point.json"
python -m scripts.run_public_benchmark --manifest "$ENGRAPHIS_BENCHMARK_RUN_DIR/point.json" \
  --execute --claims-input "$ENGRAPHIS_BENCHMARK_RUN_DIR/reviewed-claims.json"
```

The first command is a redacted dry-run. The second is the only form that starts the pinned local
commands. Execution requires a protected, pre-reviewed claims JSON array (or object with a
`claims` array), snapshots it into the manifest's restricted output, and refuses a missing
dataset, hash mismatch, commit mismatch, dirty worktree, or attempts to reuse the claims output
as the input.

Use one separate `engraphis-public-benchmark-series/v1` manifest as the predeclared comparison
contract. It records the required baseline and budget matrix, the frozen holdout, and distinct
restricted and public artifact locations. Its structural validator does not prove that any point ran.
Treat the series as completed only after validated artifacts exist for every declared point. A
single point never qualifies as a full comparative public result.

## 2. Tune on development, then freeze the holdout

Split the available data into development and holdout before tuning. Tune only on development:
embedding or reranker choice, chunking, candidate depth, retrieval profile, graph settings, and
context packing. Select one configuration, hash it, and freeze it.

Run the frozen configuration and every baseline on the untouched holdout. Do not select a budget,
baseline, question subset, or model after inspecting holdout results. Report paired per-question
results, exclusions, stratified or paired-bootstrap 95% intervals, and all five fixed budgets.

## 3. Run the complete matrix

Every canonical holdout run must include these labels at every fixed budget:

| Baseline or variant | Required purpose |
| --- | --- |
| `no_retrieval` | Full-history or no-memory reference, when the harness can represent it |
| `lexical_only` | Lexical retrieval contribution |
| `dense_only` | Dense retrieval contribution |
| `dense_lexical_rrf` | Strong hybrid retrieval baseline |
| `full_hybrid` | Shipped configuration |
| `no_graph` | Graph contribution |
| `no_reranker` | Reranker contribution |
| `no_temporal_resolution` | Write-time truth-resolution contribution |
| `whole_document` | Chunking comparison, where supported |

Use the exact baseline semantics in [eval/BASELINES.md](../eval/BASELINES.md). A baseline that
cannot be executed faithfully must fail or be marked unavailable, never relabeled as a result.

### Official LongMemEval-V2 adapter matrix

The adapter's public evidence matrix is narrower and explicit: six variants at five budgets, for
30 official runs. Materialize it in the restricted run directory before any scored question:

```bash
python -m eval.longmemeval_v2_matrix \
  --output "$ENGRAPHIS_BENCHMARK_RUN_DIR/longmemeval-v2/configs"
```

The generated manifest contains `balanced`, `planner`, `episodic_cap_2`,
`planner_episodic_cap_2`, `context_k_2`, and `planner_context_k_2`. The two `context_k=2` variants
are matched retrieval-depth comparators for the two memory-type-cap variants; without them, a cap
effect could be only a smaller candidate set. Every variant runs at 256, 512, 1,024, 2,048, and
4,096 evidence tokens.

## 4. Execute in stages

Run the offline gate first:

```bash
python -m pytest tests/ -q
python -m ruff check .
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.ablation
```

Then run a no-network pilot on a small, predeclared development slice. Check schema, hashes,
token accounting, exclusions, complete baseline coverage, and resumability. Only then run the full
holdout. For external datasets, use the complete dataset and canonical mode where supported:

```bash
python -m eval.external --dataset longmemeval_s.json --format longmemeval --canonical \
  --embed-revision <40-character-model-commit>
python -m eval.external --dataset locomo10.json --format locomo --canonical \
  --embed-revision <40-character-model-commit> \
  --locomo-repair-manifest eval/datasets/locomo10_repair_manifest.json
```

For `eval.external`, `--canonical` enforces complete source-case coverage and a pinned semantic
embedding revision. Its JSON records the source-data SHA-256 and selected embedding provenance,
but remains a private diagnostic report, not an `engraphis-benchmark/v2` public artifact, and
must not be passed to `eval.benchmark --canonical`.

The LoCoMo repair manifest is SHA-256-bound to the official raw file and covers only three
otherwise-unresolvable evidence references. Mechanical delimiter/zero-padding typos are normalized
separately. The adapter rejects mismatched source hashes, stale repairs, and every unresolved final
ID, and records the applied manifest in its report. This is reference-integrity repair for the
private retrieval diagnostic, not correction of the benchmark's semantic QA labels.

Execute each frozen point from its locked manifest rather than composing a new shell command at
release time. The point runner currently accepts only the in-repo canonical harness. Keep the
LoCoMo and LongMemEval external adapters as diagnostics until their official harness and complete
comparison matrix are represented by the pinned LongMemEval-V2 path. The series manifest is the
release checklist for all of those points.

For official LongMemEval-V2, add the exact pinned official checkout to `PYTHONPATH` and execute
each generated manifest cell through the Engraphis wrapper. The wrapper consumes all eight
`--engraphis-*` receipt arguments and delegates the remaining arguments unchanged to the official
harness:

```bash
export ENGRAPHIS_LMV2_RUN="$ENGRAPHIS_BENCHMARK_RUN_DIR/longmemeval-v2"
export PYTHONPATH="/path/to/LongMemEval-V2:$PYTHONPATH"

python -m eval.run_longmemeval_v2 \
  --engraphis-execution-manifest "$ENGRAPHIS_LMV2_RUN/receipts/balanced-1024.json" \
  --engraphis-per-question "$ENGRAPHIS_LMV2_RUN/output/balanced-1024.jsonl" \
  --engraphis-questions "$ENGRAPHIS_LMV2_RUN/data/questions.json" \
  --engraphis-haystack "$ENGRAPHIS_LMV2_RUN/data/haystack.json" \
  --engraphis-trajectories "$ENGRAPHIS_LMV2_RUN/data/trajectories.json" \
  --engraphis-memory-config "$ENGRAPHIS_LMV2_RUN/configs/balanced-1024.json" \
  --engraphis-matrix-manifest "$ENGRAPHIS_LMV2_RUN/configs/manifest.json" \
  --engraphis-seed 42 \
  <official LongMemEval-V2 harness arguments for balanced-1024>
```

The official checkout must be clean and exactly
`6f020ac2fc3275e46c706d3406e02c3ed79b7be2`. The wrapper writes the execution manifest only after
the official harness returns successfully, rejects duplicate question IDs, and verifies exact
set equality between every source question ID and output question ID. It records both counts,
source/config/output hashes, the delegated-argument digest, checkout state, and environment. A
partial output cannot acquire a completion receipt.

Then export the bound, redacted evidence:

```bash
python -m eval.longmemeval_v2_evidence \
  --per-question "$ENGRAPHIS_LMV2_RUN/output/balanced-1024.jsonl" \
  --questions "$ENGRAPHIS_LMV2_RUN/data/questions.json" \
  --haystack "$ENGRAPHIS_LMV2_RUN/data/haystack.json" \
  --trajectories "$ENGRAPHIS_LMV2_RUN/data/trajectories.json" \
  --memory-config "$ENGRAPHIS_LMV2_RUN/configs/balanced-1024.json" \
  --execution-manifest "$ENGRAPHIS_LMV2_RUN/receipts/balanced-1024.json" \
  --matrix-manifest "$ENGRAPHIS_LMV2_RUN/configs/manifest.json" \
  --ablation balanced --token-budget 1024 --seed 42 \
  --upstream-revision 6f020ac2fc3275e46c706d3406e02c3ed79b7be2 \
  --output artifacts/longmemeval-v2-balanced-1024.json
```

Repeat both commands for every manifest cell, changing the variant, budget, paths, and official
harness arguments together. The exporter rejects an execution receipt whose hashes, seed,
checkout, row count, or source-question coverage do not match the requested artifact. Each
per-question adapter record also exposes inserted and retrieved counts by memory type. A
memory-type-cap claim is accepted only when at least two inserted memory types are populated.
Hosted productivity runs follow the smoke, pilot, and full ceilings in
[docs/LUNA_BENCHMARK_PLAN.md](LUNA_BENCHMARK_PLAN.md).

## 5. Keep private and public artifacts separate

Private artifacts may contain raw questions, answers, prompts, retrieved context, per-question
debug details, and resumable checkpoints. Store them outside git with restricted access.

Public artifacts contain only the sorted redacted envelope, whole-input/source-file digests,
non-content question IDs needed to prove complete coverage, configuration and model provenance,
aggregate metrics, confidence intervals, exclusions, failure summaries, and checksum. They contain
no raw questions, answers, prompts, context, credentials, or user data. They contain no per-record
content hashes or fingerprints. Generate charts only from the public aggregate artifact.

## 6. Validate claims before publication

Convert the report to the canonical immutable artifact, then validate the exact claims file:

```bash
python -m eval.benchmark --input report.json --output artifacts/run.json --canonical
python -m eval.public_readiness \
  --artifact artifacts/run.json \
  --claims artifacts/claims.json
python -m eval.public_readiness --series "$ENGRAPHIS_BENCHMARK_RUN_DIR/comparison-series.json"
```

Publication stops on any validation error, missing baseline, incomplete budget curve, dirty source,
mutable revision, mismatched hash, or redaction violation. Publish the artifact checksum beside
the report and identify the artifact and command for every number in public prose or charts.

## 7. Protected CI policy

Both checked-in benchmark workflows are manual-only, use the protected
`public-benchmark-protected` environment, and run on the dedicated self-hosted benchmark runner.
Pull requests run only offline tests and fixture evaluations. Neither workflow publishes a release,
submits a leaderboard entry, or sends an external message.

### Hosted Luna full stage

`.github/workflows/public-benchmarks.yml` is limited to the hosted Luna full stage. Before dispatch,
an authorized operator must complete and review the smoke and pilot reports, affirm that review in
the workflow input, supply a safe unique run ID, and enter the exact full-run call ceiling reported
by `python -m eval.hosted_luna --dry-run --full`. A ceiling mismatch stops before any hosted call.

The protected self-hosted runner must configure `ENGRAPHIS_BENCHMARK_STATE_ROOT` as an owner-only,
persistent directory outside the Git checkout. The workflow keeps resumable private checkpoints
and its generated public report there, so checkout cleanup or a workflow rerun cannot reset the
provider-call ledger. The hosted job:

1. verifies the exact clean commit;
2. rejects unsafe run IDs, missing prerequisite review, and any operator/dry-run ceiling mismatch;
3. binds the zero-call dry-run to the full stage before execution;
4. resumes the persisted full-stage ledger without repeating completed attempts;
5. validates the `engraphis-hosted-evidence/v1` checksum and aggregate-only schema through
   `python -m eval.public_readiness`;
6. copies only that validated public JSON and its checksum into the upload directory; and
7. stops closed if the model, usage accounting, dataset, retry policy, or run binding differs.

### Offline retrieval point

`.github/workflows/public-retrieval-benchmarks.yml` executes one locked retrieval point. It makes no
hosted model call and has a fixed 24-hour job ceiling, but it still requires protected-environment
approval plus the `execution_authorized` attestation because self-hosted compute is cost-bearing.
The runner must provide `ENGRAPHIS_BENCHMARK_PYTHON` inside the protected environments mount. The
operator supplies a SHA-bound lock containing the exact `pip freeze --all --exclude-editable`
output for that pre-provisioned interpreter. The workflow performs no package or model download.

The retrieval job:

1. verifies the exact clean checkout and rejects unsafe run IDs or mounted-file paths;
2. verifies the protected environment-lock checksum, exact installed package set, and `pip check`;
3. binds the point run ID, checkout root and commit, model/dataset revisions, token budgets, and
   baseline to the approved comparison-series manifest;
4. requires the point output directory to equal its owner-only run state directory;
5. resolves a pre-reviewed claims JSON only from the protected claims mount, then snapshots it
   immutably into the owner-only run state before the benchmark begins;
6. validates the declared series contract, emits a redacted dry-run plan, and only then executes the
   allowlisted offline command;
7. validates the public artifact and that exact staged claims file through `eval.public_readiness`; and
8. copies only regular, non-symlink public artifact and claims files into the upload directory.

The workflows may prepare artifacts, but publication, release tags, leaderboard submission, and
external messages remain explicit human actions.

## 8. No-claim boundaries

Do not claim any of the following unless the corresponding independent evidence is present:

- retrieval hit or recall as end-to-end LLM answer accuracy;
- deterministic productivity-fixture results as general model intelligence;
- context reduction as provider billing, latency, storage, or cost savings;
- performance on a partial, unpinned, or noncanonical dataset as a public leaderboard result;
- superiority to hybrid RRF when only dense-only comparisons were run;
- graph, temporal resolution, reinforcement, reranking, or adaptive-policy gains without an
  executed ablation against the same frozen baseline and budget;
- full-history quality when full history exceeded the reader budget or was not a valid baseline;
- hosted-service or third-party ranking without the required environment and external evaluation.

If a required resource is unavailable, publish the run as incomplete or unavailable with the exact
reason. Never substitute a different model, dataset, tokenizer, evaluator, or retry policy and keep
the original claim.
