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
four pinned configs in `eval/configs/longmemeval_v2_engraphis*.json`. Materialize the exact 20-run
matrix in that restricted run directory with:

```bash
python -m eval.longmemeval_v2_matrix --output "$ENGRAPHIS_EVIDENCE_RUN_DIR/configs"
```

Run the pinned official harness once per manifest entry. Keep upstream per-question files and any
private comparison data outside the repository; export only redacted evidence with pinned dataset,
reader, embedder, configuration, and seed metadata.

`eval.resource_hierarchy` is evaluation-only. It derives file/section overviews from path, heading,
and chunk-order metadata. If its held-out gate does not improve quality by at least three percentage
points at three budgets without more context and within the latency bound, schema 7 is retained and
no resource hierarchy is built.
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
