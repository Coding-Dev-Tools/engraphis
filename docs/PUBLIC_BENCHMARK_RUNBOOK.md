# Public benchmark runbook

This runbook is the operator sequence for a reproducible public run. It covers retrieval evidence
and, when separately configured, official end-to-end evaluation. The code and checked-in benchmark
contracts are authoritative: see [BENCHMARKS.md](../BENCHMARKS.md),
[eval/EVIDENCE.md](../eval/EVIDENCE.md), [eval/BASELINES.md](../eval/BASELINES.md), and
[docs/LUNA_BENCHMARK_PLAN.md](LUNA_BENCHMARK_PLAN.md).

## 1. Lock the run

Create a private run directory outside the repository or under an ignored path. Record one
immutable manifest before execution:

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
profile, and private/public output paths. Run it through the allowlisted orchestrator:

```bash
python -m scripts.run_public_benchmark --manifest private/point.json
python -m scripts.run_public_benchmark --manifest private/point.json --execute
```

The first command is a redacted dry-run. The second is the only form that starts the pinned local
commands, and it refuses a missing dataset, hash mismatch, commit mismatch, or dirty worktree.

Use one separate `engraphis-public-benchmark-series/v1` manifest as the predeclared comparison
contract. It records the required baseline and budget matrix, the frozen holdout, and distinct
private and public artifact locations. Its structural validator does not prove that any point ran.
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
python -m eval.external --dataset longmemeval_s.json --format longmemeval --canonical
python -m eval.external --dataset locomo10.json --format locomo --canonical
```

For `eval.external`, `--canonical` enforces complete source-case coverage only. Its ordinary JSON
is a private diagnostic report, not an `engraphis-benchmark/v2` public artifact, and must not be
passed to `eval.benchmark --canonical`.

Execute each frozen point from its locked manifest rather than composing a new shell command at
release time. The point runner currently accepts only the in-repo canonical harness. Keep the
LoCoMo and LongMemEval external adapters as diagnostics until their official harness and complete
comparison matrix are represented by the pinned LongMemEval-V2 path. The series manifest is the
release checklist for all of those points.

For official LongMemEval-V2, use the pinned adapter and upstream harness described in
[BENCHMARKS.md](../BENCHMARKS.md), then create the redacted evidence artifact with the exporter
documented in [eval/EVIDENCE.md](../eval/EVIDENCE.md). Hosted productivity runs follow the smoke,
pilot, and full ceilings in [docs/LUNA_BENCHMARK_PLAN.md](LUNA_BENCHMARK_PLAN.md).

## 5. Keep private and public artifacts separate

Private artifacts may contain raw questions, answers, prompts, retrieved context, per-question
debug details, and resumable checkpoints. Store them outside git with restricted access.

Public artifacts must contain only the sorted redacted envelope, hashes, configuration and model
provenance, aggregate metrics, confidence intervals, exclusions, failure summaries, and checksum.
They must contain no raw questions, answers, prompts, context, credentials, user data, or
question-derived identifiers. Generate charts only from the public aggregate artifact.

## 6. Validate claims before publication

Convert the report to the canonical immutable artifact, then validate the exact claims file:

```bash
python -m eval.benchmark --input report.json --output artifacts/run.json --canonical
python -m eval.public_readiness \
  --artifact artifacts/run.json \
  --claims artifacts/claims.json
python -m eval.public_readiness --series private/comparison-series.json
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
