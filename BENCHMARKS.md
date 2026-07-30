# Benchmarks

This file is the honest status of what Engraphis measures today, how to reproduce it, and what
it does **not** yet claim. It exists because the README linked a `BENCHMARKS.md` that had never
been written; when this and the code disagree, the code wins (CLAUDE.md).

## What we measure today (all offline, no API key)

Engraphis's eval harness scores **retrieval**, not end-to-end QA. That distinction is deliberate
and stated everywhere the numbers appear (`eval/external.py`).

- **Correctness gate**: `eval/harness.py` over `eval/datasets/sample.jsonl` and
  `codemem.jsonl` (conflict resolution) and `graph_multihop.jsonl` (multi-hop graph recall).
  Runs on the deterministic embedder, so it is a plumbing/regression floor, not a public
  performance claim. This is the gate CI enforces.
- **Ablation**: `eval/ablation.py`: vector-only vs. 1-hop graph vs. Personalized-PageRank arm,
  to show the graph arm actually earns its place.
- **External benchmarks**: `eval/external.py` loads **LoCoMo** and **LongMemEval** and pushes
  them through the *real* `MemoryEngine` write path (conflict resolution + evolution) and hybrid
  recall with a real sentence-transformers embedder. It reports `recall_at_k` / `hit_at_k` /
  `answer_token_recall`: i.e. *did the evidence come back*, not *did an LLM answer correctly*.
  It retains source categories and abstention/no-evidence questions as explicit exclusions from
  retrieval-only aggregates rather than silently dropping them. `eval.longmemeval_v2` is a local,
  text-only adapter for the official LongMemEval-V2 `insert(trajectory)` / `query(query,
  query_image=None)` memory interface; it does not download data or call a model.
- **Grounded**: `eval/grounded.py`: answerable → cite, off-topic → abstain.
- **Chunking (quality per token)**: `eval/chunking_eval.py` over `eval/datasets/longdoc.jsonl`
  ingests a multi-topic corpus twice: once as one memory per document (`whole`) and once with
  sub-file `ChunkingExtractor` (`chunked`), then queries both through the real recall pipeline. This is
  the first cut of the context-reduction metric (item 3 below). On the deterministic embedder:
  **recall@5 1.000 for both, at ~73% fewer context tokens (809 → 219) and ~4× smaller
  tokens-to-evidence (162 → 42).** Pass `--embed-model sentence-transformers/all-MiniLM-L6-v2`
  for a real retrieval number (recall should then favour chunked on larger corpora, not just
  tie).
- **Full-pipeline latency + quality**: `eval/performance.py` times the shipped semantic +
  lexical + graph + fusion + scoring + rerank + packing path after warmup, with reinforcement
  disabled so repeated measurements do not mutate their corpus. It reports p50/p95/p99 latency,
  retrieval quality, and packed context tokens in one JSON-safe schema. `--filler-memories`
  provides deterministic corpus scaling, and every report records the runtime, architecture,
  embedder, vector backend, corpus size, warmups, and iteration count.

### Reproduce

```bash
# Correctness gate (deterministic, no download)
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.harness --dataset eval/datasets/graph_multihop.jsonl --k 5
python -m eval.ablation
python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10
python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 \
  --iterations 5 --filler-memories 1000
# Canonical latency/resource protocol: requires >=1,000 queries and five processes.
python -m eval.performance --dataset fixed-1000-plus.jsonl --acceptance-matrix --processes 5

# Real retrieval numbers (downloads all-MiniLM-L6-v2)
python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10
python -m eval.external --dataset locomo10.json      --format locomo      --k 10
```

## What we do NOT yet claim

- **No end-to-end QA accuracy.** Official LoCoMo / LongMemEval QA scores depend on an answering model and evaluator. Engraphis isolates retrieval and does not present that result as end-to-end answer accuracy.
- **No hosted-service latency comparison.** The in-repo p50/p95/p99 benchmark covers the local
  reference pipeline and records its environment; unlike environments are not compared.
- **No neutral third-party ranking.** We have not run an external eval platform.

Every publishable run should emit the `engraphis-benchmark/v2` envelope: dataset/config hashes,
per-question records, explicit exclusions, fixed-budget context curves, and deterministic
stratified or paired bootstrap confidence intervals. Every run names its token counter.
Noncanonical offline fixtures may identify a deterministic estimate; canonical public evidence
requires the exact pinned reader tokenizer and immutable model revision. The lightweight CI
fixtures validate that machinery; they are not a claim about external benchmark performance.

The benchmark context metric reads strict recall usage fields rather than inferring prompt size:
`budget_tokens`, `context_tokens`, `source_tokens`, `saved_tokens`, `savings_ratio`,
`packed_count`, `omitted_count`, and `token_counter`. Use `engraphis_recall_context` for a
hard-budget prompt packet; legacy `engraphis_recall` remains available in full or compact response
mode for compatibility.

### Canonical public artifacts

Use `python -m eval.benchmark --input report.json --output artifacts/run.json` to validate a
report and write sorted, immutable JSON plus `run.json.sha256`. The command permits an identical
retry but refuses to replace a different artifact at the same path. For an official
LongMemEval-V2 run, add `--canonical`: this requires a profile with an exact benchmark repository
revision, dataset revision, reader model revision, and embedding model revision. The checked-in
profile pins immutable upstream commits; replacing any revision with a mutable tag fails
validation. Canonical profiles label the baseline (`no_retrieval`, `lexical_only`, `dense_only`,
`dense_lexical_rrf`, `full_hybrid`, `full_history`, `no_graph`, `no_reranker`,
`no_temporal_resolution`, or `whole_document`) and declare the required fixed context-budget
matrix: 256, 512, 1024, 2048, and 4096 tokens. Canonical in-repo reports rerun every question at
all five budgets and validate each aggregate against its per-question evidence. The checked-in
LongMemEval-V2 memory-module configuration sets the official adapter's operating point to 1,024
tokens; that single official point must not be presented as a five-point curve.

`eval.external --canonical` refuses `--limit` and rejects a normalized output that omitted source
cases. Retrieval-only abstention/no-evidence records remain visible in the artifact's
`exclusions`; they are not counted as evidence-retrieval scores.

### LongMemEval-V2 memory-module adapter

`eval.longmemeval_v2.EngraphisLongMemEvalV2Memory` follows the official
`memory_modules.memory.Memory` interface at LongMemEval-V2 commit
`6f020ac2fc3275e46c706d3406e02c3ed79b7be2`. When imported in that environment, its
`@register_memory` decorator registers `memory_type="engraphis"`; use the checked-in
[`eval/configs/longmemeval_v2_engraphis.json`](eval/configs/longmemeval_v2_engraphis.json)
with the official harness. The config pins `Qwen/Qwen3-Embedding-8B` to revision
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`; mutable embedding revisions are rejected, and a
canonical adapter run fails instead of relabeling the deterministic offline fallback as Qwen.
Run `python -m eval.run_longmemeval_v2` with the official harness arguments and the pinned
checkout on `PYTHONPATH`. This wrapper performs the upstream registry import in the required order
before delegating to `evaluation.harness`; a direct upstream invocation must otherwise import
`eval.longmemeval_v2` before calling `build_memory`.

The checked-in configuration is canonical only when the adapter resolves the pinned Qwen reader
processor at `c202236235762e1c871ad0ccb60c8ee5ba337b9a`. The wrapper also forces the audited
official harness's otherwise-unpinned `AutoProcessor` call to that same revision. It refuses to
start if the optional processor dependency or immutable revision is unavailable; the local regex
counter is never silently relabeled as a reader budget. The recorded budget counts each returned
context item's content with that reader tokenizer (without prompt framing or inter-item
separators), so it is a hard **evidence-item content** budget, not a claim about total chat-prompt
tokens. Packed sources are returned as separate context items, preserving the largest fitting
evidence prefix instead of dropping one oversized monolithic item. The adapter does not download
benchmark data or call the reader/evaluator; the official harness owns those steps.

## Next steps for external publishable numbers

1. **Add a QA layer to `eval/external.py`.** Optional answering model + judge on top of the
   existing retrieval pipeline, so the official datasets can report end-to-end accuracy while
   reusing the retrieval harness underneath.
2. **Publish production-backend latency.** Run `eval/performance.py` with the real embedder and
   sqlite-vec/backend configuration on a fixed machine class and corpus scale.
3. **Run the fixed-budget curve on the complete official datasets.** The v2 harness now measures
   every question at 256, 512, 1,024, 2,048, and 4,096 evidence tokens and validates the
   per-question records, aggregates, and pinned reader-tokenizer identity. Publish the curve only
   after complete official runs produce immutable artifacts for every point.
4. **Run an external evaluation platform** once (1)–(3) exist.

## Evaluation question

The predeclared question is whether the full vector + lexical/BM25 + sparse PPR graph + calibrated
rerank pipeline, bi-temporal resolution, and grounded abstention produce higher evidence recall
per injected token than the registered baselines. The answer must come from a complete,
machine-readable artifact with paired confidence intervals; otherwise the release reports
“no demonstrated improvement.”
