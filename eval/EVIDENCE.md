# External benchmark evidence

`eval.benchmark.report_envelope()` is the public-artifact boundary. It records
the dataset and optional source digests, commit and dirty-state digest, command,
configuration digest, environment, model metadata, and token-counting scope.
It removes raw questions, answers, returned context, and prompts from every
record while retaining SHA-256 digests for same-input verification.

After an official LongMemEval-V2 run, keep the upstream `per_question.jsonl`
private in an operator-controlled run directory and create a redacted artifact. Set the
directory once; the public repository never assumes an internal filesystem layout:

```bash
export ENGRAPHIS_EVIDENCE_RUN_DIR=/path/to/restricted/longmemeval-v2

python -m eval.longmemeval_v2_evidence \
  --per-question output/per_question.jsonl \
  --questions data/questions.json \
  --haystack data/haystack.json \
  --trajectories data/trajectories.json \
  --memory-config "$ENGRAPHIS_EVIDENCE_RUN_DIR/configs/balanced-1024.json" \
  --matrix-manifest "$ENGRAPHIS_EVIDENCE_RUN_DIR/configs/manifest.json" \
  --ablation balanced --token-budget 1024 --seed 42 \
  --upstream-revision <40-character-official-harness-commit> \
  --output artifacts/longmemeval-v2.json
```

The command writes sorted JSON and an adjacent `.sha256` checksum, refusing to
replace a different artifact. It preserves the official harness QA score and
its fixed-reader memory-context item-content token count. That count excludes
chat-prompt framing and inter-item separators, so it is not a total provider
prompt-token claim. It is not a canonical Engraphis retrieval artifact until a
complete run also supplies the required five-budget evidence curve.

The evidence exporter verifies the memory-config digest against one exact matrix cell and records
the upstream harness revision, seed, reader, embedder, backend, planning mode, and type limits. It
records an intentionally redacted command label. Keep
API keys and raw prompt material only in the private official-run environment.

## Planned-recall gates

Run the repository-local 40-task stress matrix before any official or hosted run:

```bash
python -m eval.planned_recall
python -m eval.resource_hierarchy
```

`eval.planned_recall` evaluates balanced recall, planner only, type limits only, and planner plus
limits at 256, 512, 1024, 2048, and 4096 tokens. It records exact injected tokens, p50/p95 latency,
planner failures, context revisions, provider cached-input tokens when supplied, and deterministic
paired-bootstrap deltas. This is fixture-scoped regression evidence, not a third-party benchmark.

The official LongMemEval-V2 adapter accepts the same `planning` and `mtype_limits` controls. Use the
four pinned configs in `eval/configs/longmemeval_v2_engraphis*.json`. Materialize the exact 30-run
matrix, including the two matched `context_k=2` variants at every budget, in that restricted run directory with:

```bash
python -m eval.longmemeval_v2_matrix --output "$ENGRAPHIS_EVIDENCE_RUN_DIR/configs"
```

Run the pinned official harness once per manifest entry. Keep upstream per-question files and any
private comparison data outside the repository; export only redacted evidence with pinned dataset,
reader, embedder, configuration, and seed metadata.

`python -m eval.planned_recall` is a report-only command. Its successful exit does not mean that
an experimental candidate passed. To require a particular gate, run:

```bash
python -m eval.planned_recall --require-gate planner --gate-level repository-local
python -m eval.planned_recall --require-gate planner --gate-level default
```

`--require-gate` accepts `planner` or `planner_type_limits`; its default level is `default`,
which also requires the local, safety, and opt-in eligibility booleans. Missing, false, or
non-boolean evidence fails closed with exit code 1 after the report is printed. Python promotion
callers use `require_gate(report, candidate, level="default")`. Synthetic results alone never
set official-run or default eligibility to true. Keeping the existing balanced default does not
require promoting either experiment.

`eval.resource_hierarchy` is evaluation-only. It derives file/section overviews from path, heading,
and chunk-order metadata. If its held-out gate does not improve quality by at least three percentage
points at three budgets without more context and within the latency bound, no resource hierarchy
is built. The shipped memory schema remains version 18; legacy `retain_7`/`bump_to_8` labels in the
isolated prototype are not instructions to migrate the product database.

## Production factory performance diagnostics

The default `eval.performance` fixture mode retains its deterministic, in-memory constructor and
`engraphis-performance/v1` result contract. For disk/backend diagnostics, provide `--engine-config`
with an explicit JSON configuration. A fully offline example is:

```json
{
  "storage": "disk",
  "vector_backend": "numpy",
  "sqlite_durability": "durable"
}
```

```bash
python -m eval.performance --dataset eval/datasets/codemem.jsonl --engine-config engine.json --json
python -m eval.performance --dataset fixed-1000-plus.jsonl --engine-config engine.json --acceptance-matrix --processes 5 --json
```

Set `storage_root` to an existing absolute directory on the disk to measure. Each spawned worker
gets a fresh temporary database there, reopens its populated database before recall, and closes
and removes only that temporary database afterward. `storage="memory"` measures the same factory
without a disk reopen. The factory uses exact `numpy` or `sqlite-vec` selection; `auto` and backend
fallbacks are rejected. `sqlite_durability="balanced"` is an explicit alternative to `durable`;
the report records the observed SQLite journal mode and synchronous setting.

To select already-cached semantic models, add `embed_model="local:org/model"` and an exact
lowercase 40-character `embed_revision`. Optional reranking uses `rerank_model` and
`rerank_revision` under the same policy. Absolute local model directories instead require
`embed_artifact_sha256` or `rerank_artifact_sha256`, using the existing
`engraphis-local-artifact-v1` directory-content digest. Directory bytes are verified before and
after loading. No implicit downloads or fallback models are permitted. Missing local assets or
dependencies fail the run, and tests exercise this path with model doubles rather than downloads.

Additive `phases` and per-process resource fields distinguish empty-engine construction, ingestion,
populated disk reopen, first-pass recall, steady-state recall and executor queue wait. Construction
includes configured local-model verification/loading. Recall latency excludes executor queue wait
and MCP/HTTP transport. First-pass recall is not uncached disk IO. RSS values are optional process
lifetime peak watermarks sampled at named boundaries, not isolated phase peaks. Python/MCP process
startup, provider queues and hardware power-loss durability are unmeasured by this diagnostic.
An acceptance matrix's `valid` value establishes protocol coverage, not an SLA or quality win.
Factory runs are separate local diagnostics and do not inherit the historical fixture evidence ID.

Factory mode enables recall diagnostics and records recognized, content-free stage timings under
`phases.recall_stages`, separately for cold and warm calls. Reports include sample counts and
p50/p95/p99 for the observed preparation, planning, embedding, candidate filtering, vector/lexical/
graph/code search, fusion/scoring, reranking, selection, reinforcement, support/provenance, packing
and response metadata stages. Unexecuted or unavailable stages are omitted; observed rounded zero
durations are retained. Warmup calls are excluded from these summaries. `engine_recall` is the
enclosing total and must not be added to the disjoint stages. Repeated arm/page work accumulates
within each stage. These measurements include diagnostics overhead and exclude transport,
database-lock attribution and answer generation. Fixture mode keeps diagnostics disabled and
does not emit these stage summaries. Full retrieval traces and memory content are never copied
into the stage timing fields.

## Consolidation ranking preference

The post-normalization consolidation bonus is measured by a deterministic paired
fixture that compares digest-intent and source-intent rankings with and without the
production bonus. Run `python -m eval.consolidation_ranking`; digest top-1 preference
must not regress against the no-bonus baseline, and raw-detail/source evidence must
remain retrievable before changing the preference or shipping a new release.

## Adversarial memory prompt boundary

Run `python -m eval.adversarial_memory_security` for the deterministic v2 prompt-boundary
gate. It checks write-time quarantine, review-pending content exclusion, direct and
support-derived graph-edge exclusion, and availability of trusted control evidence. This is a
fixed regression fixture, not a claim about real-world poisoning prevalence or detector recall.

## Context-efficiency guardrail

Run `python -m eval.context_efficiency_guardrails` after changes to context packing, recall, or
grounded-answer construction. Its compact offline fixture only passes when a hard token budget
reduces reader context versus replaying every source **and** the supported operational answer stays
grounded and cited, an off-topic request abstains, and an explicitly untrusted instruction-shaped
source is neither cited nor echoed. The JSON reports deterministic reader-context accounting with
the named regex counter; it is not a provider-billing or LLM-output-quality claim.
