# Benchmarks

This guide explains what Engraphis measures, how to reproduce each evaluation, and the limits of
those results. When this document and the code disagree, the code is the source of truth.

For the locked operator sequence for a public canonical run, see
[`docs/PUBLIC_BENCHMARK_RUNBOOK.md`](docs/PUBLIC_BENCHMARK_RUNBOOK.md).

## What we measure today (all offline, no API key)

Most Engraphis evals score **retrieval**, not end-to-end QA. The separate productivity benchmark
runs a complete offline agent attempt and correction loop, but it is not an official
frontier-model QA score.

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
  sub-file `ChunkingExtractor` (`chunked`), then queries both through the real recall pipeline.
  The checked-in corpus is explicitly marked trusted eval data so the measurement isolates
  chunking from the production trust gate, which excludes arbitrary raw imports from normal
  agent context. This is the first cut of the context-reduction metric (item 3 below). On the
  deterministic embedder:
  **recall@5 1.000 for both, at ~73% fewer context tokens (809 → 219) and ~4× smaller
  tokens-to-evidence (162 → 42).** Pass `--embed-model sentence-transformers/all-MiniLM-L6-v2`
  for a real retrieval number (recall should then favour chunked on larger corpora, not just
  tie).
- **Full-pipeline latency + quality**: `eval/performance.py` times the shipped semantic +
  lexical + graph + fusion + scoring + rerank + packing path after warmup, with reinforcement
  disabled so repeated measurements do not mutate their corpus. It reports p50/p95/p99 latency,
  retrieval quality, and packed context tokens in one JSON-safe schema. `--filler-memories`
  provides deterministic corpus scaling, and every report records the runtime, architecture,
  embedder, vector backend, corpus size, warmups, and iteration count. `--candidate-k` and
  `--retrieval-profile` make adaptive-depth/routing experiments executable instead of changing
  production defaults from an unmeasured hunch.
- **NumPy vector scale envelope**: `eval/vector_scale.py` measures the production
  `NumpyVectorIndex` directly at requested corpus sizes with deterministic normalized vectors and
  queries. It records a corpus fingerprint, result hashes, environment, and observed
  p50/p95/p99 search envelopes. It intentionally has no pass/fail latency threshold: the output
  describes the measured machine and workload, not a universal capacity cutoff. Pair it with
  `eval/performance.py` before making a deployment decision because direct vector search excludes
  the rest of the recall pipeline. Its `engraphis-vector-scale/v1` JSON is a local diagnostic, not
  an `engraphis-benchmark/v2` public evidence artifact.
- **Proactive ranking calibration**: `eval/proactive_ranking.py` compares the previous and current
  importance-retention floors on a small deterministic queryless-ranking fixture. It reports
  top-1 accuracy and minimum expected margins for that fixture only. It is a scoring regression,
  not evidence of general recall quality or user-task performance.
- **Workload context economy**: `eval/context_economy.py` compares three executable strategies
  across every question in a workload: uncapped full-history replay, a contiguous recency window
  at the same hard budget, and shipped Engraphis hybrid recall + packing. It reports evidence and
  answer-token quality, cumulative reader-context tokens, a conservative total that charges one
  complete source-token pass to indexing, and the query-count break-even point. The default is
  deterministic/offline; `--embed-model` enables a real retrieval model, while
  `--format locomo|longmemeval` reuses the established external loaders.
- **Agent productivity**: `eval/productivity.py` compares a capped full-history baseline,
  always-on retrieval, and
  adaptive context through a complete answer-and-correction loop. It reports completed tasks,
  first-attempt errors, abstentions, corrections, agent turns, memory calls, wall-clock latency,
  and all question/context/output tokens. The bundled agent is deterministic, receives no gold
  answer, and is identified in every report; inject a real agent callable for model-specific
  results. Optional provider telemetry is reported separately from the deterministic token
  counter and is not a provider billing estimate.

The workload benchmark is also allowed to say “this workload is too small for a memory layer.”
On the 44-memory / 26-question CodeMem regression fixture, every case already fits inside a
64-token recency window. Full-history and recency therefore use the same 1,180 cumulative reader
tokens at perfect evidence/answer-token quality, while Engraphis uses 1,375–1,377 reader tokens
plus a conservative 631-token indexing pass. That is an honest no-break-even boundary result:
the benefit being measured begins when history is long or reused enough to outweigh retrieval
framing and indexing.

The adaptive policy removes that small-workload penalty. On the same 26 CodeMem tasks, every
history fit the 512-token prompt allowance, so adaptive routing bypassed all 26 memory calls.
It used **1,942** total agent-facing tokens versus **2,194** for always-on retrieval
(**11.5% lower**) while both strategies completed **24/26** tasks with the bundled deterministic
agent. This demonstrates the bypass behavior and token accounting, not general LLM intelligence.

The complementary real-model LoCoMo workload diagnostic covers 10 conversations and 1,986
questions with `all-MiniLM-L6-v2`, `k=10`, a 512-token reader budget, and conflict resolution
disabled. **This is an unpinned, noncanonical workload diagnostic of reader-context use only, not
answer quality or leaderboard accuracy.** Engraphis used **891,857** cumulative reader-context
tokens versus **49,915,394** for uncapped full history, **98.2133% lower**. Charging one complete
246,539-token corpus pass to indexing produces a conservative Engraphis total of **1,138,396**,
still **97.7193% lower**, with a calculated break-even at query 10. The quality tradeoff is
explicit:

| LoCoMo workload method (unpinned, noncanonical context-use diagnostic; not answer quality or leaderboard accuracy) | Retrieval recall | Hit rate | Answer-token recall | Mean reader context |
|---|---:|---:|---:|---:|
| Engraphis hybrid recall | **0.600457** | **0.657417** | **0.679614** | **449.07** tokens |
| Same-budget recency window | 0.011289 | 0.012614 | 0.339941 | 487.87 tokens |
| Uncapped full history | 0.996997 | 0.997477 | 0.917247 | 25,133.63 tokens |

This diagnostic supports a precise statement: Engraphis recovered much more useful evidence than
a same-budget recency window while using a small fraction of full-history context. It does not
support “same quality as full history,” provider-billing, or end-to-end answer-accuracy claims.
The embedding model revision was not pinned in that run, so rerun it with an immutable revision
before treating the numbers as canonical release evidence.

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
  --candidate-k 25 --candidate-depth adaptive --retrieval-profile auto --iterations 10
python -m eval.context_economy --dataset eval/datasets/codemem.jsonl \
  --token-budget 512 --k 5
python -m eval.productivity --dataset eval/datasets/codemem.jsonl \
  --max-context-tokens 512 --retrieval-token-budget 256
python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 \
  --iterations 5 --filler-memories 1000
# Direct NumPy search envelope at representative corpus sizes; timings are machine-specific.
python -m eval.vector_scale --sizes 1000,10000,100000 --queries 20 --iterations 3 --json
# Deterministic queryless-ranking calibration fixture.
python -m eval.proactive_ranking
# Canonical latency/resource protocol: requires >=1,000 queries and five processes.
python -m eval.performance --dataset fixed-1000-plus.jsonl --acceptance-matrix --processes 5

# Real retrieval numbers (downloads all-MiniLM-L6-v2)
python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10
python -m eval.external --dataset locomo10.json      --format locomo      --k 10
python -m eval.context_economy --dataset locomo10.json --format locomo \
  --embed-model sentence-transformers/all-MiniLM-L6-v2 --token-budget 512 --k 10 --no-resolve
```

## What we do NOT yet claim

- **No official end-to-end LLM QA accuracy.** The deterministic productivity agent measures the
  complete local control loop, not a frontier answering model. Official LoCoMo / LongMemEval QA
  still requires a pinned answering model and evaluator.
- **No hosted-service latency comparison.** The in-repo p50/p95/p99 benchmark covers the local
  reference pipeline and records its environment; unlike environments are not compared.
- **No neutral third-party ranking.** We have not run an external eval platform.
- **No provider bill estimate.** Context-economy counts reader evidence under its named counter.
  It excludes system/tool prompts, questions, completions, prompt caching, provider pricing,
  compute, and storage. Its indexing-inclusive total is a conservative text-volume proxy.

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

Official LongMemEval-V2 output can be converted into a public-safe QA artifact with
`python -m eval.longmemeval_v2_evidence`. The exporter keeps the official QA score, fixed-reader
context token count, latency, model revisions, source digests, repository state, and artifact
checksum. It removes raw questions, answers, prompts, reader output, and retrieved context before
the artifact can be written. See [`eval/EVIDENCE.md`](eval/EVIDENCE.md) for the exact command.

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

## External evidence status and remaining executions

1. **Run the official LongMemEval-V2 reader and evaluator.** The adapter, pinned runner, and
   redacted evidence exporter are implemented. The exact upstream commit boots in an isolated
   Python 3.11 environment and the wrapper reaches the official harness CLI. Dataset revision
   `f152293e235517d504809563c833d7190b8c713b` publishes 7,120,369,667 bytes before the pinned
   Qwen reader and embedding model assets. A full official run therefore still requires those
   resources, sufficient compute, and evaluator configuration; no canonical QA score is claimed
   until that run completes.
2. **Publish production-backend latency.** Run `eval/performance.py` with the real embedder and
   sqlite-vec/backend configuration on a fixed machine class and corpus scale.
3. **Run the fixed-budget curve on the complete official datasets.** The v2 harness now measures
   every question at 256, 512, 1,024, 2,048, and 4,096 evidence tokens and validates the
   per-question records, aggregates, and pinned reader-tokenizer identity. Publish the curve only
   after complete official runs produce immutable artifacts for every point.
4. **Run an external evaluation platform** once (1)–(3) exist.

Do not make all evidence lanes variants of explicit factual recall. Executable offline adapters
now cover:

- [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench): incremental multi-turn
  learning, long-range understanding, and conflict/consolidation inputs.
- [LoCoMo-Plus](https://github.com/xjtuleeyf/Locomo-Plus): an old implicit constraint must affect
  a later response even when the later cue does not restate the remembered fact.
- [Mem2ActBench](https://github.com/Cantaloupe-M/Mem2ActBench): memory must select a tool and
  ground its arguments, not merely return a passage. The current adapter measures retrieval and
  expected tool-argument context coverage, not generated tool-call success.

```bash
python -m eval.agent_benchmarks --dataset memoryagentbench.json \
  --format memoryagentbench
python -m eval.agent_benchmarks --dataset locomo_plus.json \
  --format locomo_plus
python -m eval.agent_benchmarks --dataset qa_dataset.jsonl \
  --conversations toolmem_conversation.jsonl --format mem2actbench \
  --artifact artifacts/mem2actbench.json
```

Use `--artifact` on any of these commands to write a redacted, immutable evidence envelope plus
an adjacent SHA256 file. The ordinary console/`--json` report is private run material and may
contain source questions for debugging.

### Upstream-data diagnostic baseline (2026-07-30)

These runs use the dependency-free deterministic embedder on upstream data. They validate the
adapters and expose product gaps; they are noncanonical diagnostics, not leaderboard or marketing
claims. The artifact validator accepted every completed envelope.

| Upstream source | Executed scope | Result and boundary |
|---|---|---|
| LoCoMo-Plus commit `059f4e3d38f7f1f96765e8e2cb7de3097551bffb` | All 401 Cognitive cases, 40,270 source memories | Recall@10 **0.1259**, hit@10 **0.1272**, MRR@10 **0.0744**, answer-token context coverage **0.5095**. This is cue-evidence retrieval, not answer-judge accuracy. The low retrieval score is useful negative evidence: implicit-constraint recall remains a real product gap. |
| MemoryAgentBench commit `455306dcabc3842526eb83cd4e225e5d486c5c5d`, official Hugging Face `Accurate_Retrieval` first row | 100 questions | Recall@10 **0.5100**, hit@10 **0.8600**, answer-token context coverage **0.8500**. Gold evidence was derived only where an accepted answer occurred in a source chunk. |
| The same source, `Conflict_Resolution` first row | 100 questions | Recall@10 **0.4600**, hit@10 **0.6400**, answer-token context coverage **0.6800**. This plain-context export measures retrieval, not structured temporal invalidation. |
| The same source, `Long_Range_Understanding` first row | 1 question | Answer-token context coverage **0.2658**. The export supplied no evidence IDs and no accepted answer occurred verbatim in a source chunk, so retrieval was deliberately left unscored rather than reported as a false perfect score. |
| The same source, `Test_Time_Learning` first row | One 5.88 MB context | The no-resolution ingest did not complete within a five-minute local smoke ceiling. This is a measured large-ingest throughput gap, not a failed quality score; batch embedding and transaction work should precede a complete split run. |
| Mem2ActBench upstream smoke | 2 public rows | Recall@10, hit@10, MRR@10, and NDCG@10 **1.0000**; expected tool-call JSON token coverage **0.5714**. This is retrieval/context coverage, not generated action success. |

The MemoryAgentBench loader accepts both its aligned public JSON export and the Hugging Face
dataset-server `rows[].row` envelope. Rows without gold evidence remain useful for answer-token
coverage, but are excluded from retrieval aggregates and counted separately as
`retrieval_scored_questions`.

For paired code-agent runs, execute the same tasks with the same model, tools, machine, and
deterministic success oracle under `full_history` and `engraphis`. Then analyze the content-free
run records with:

```bash
python -m eval.code_agent_ab --full-history full-history.jsonl \
  --engraphis engraphis.jsonl --output paired-report.json
```

The analyzer rejects unmatched task IDs and different success oracles, then reports paired
bootstrap intervals for task success, input/output/tool tokens, retries, latency, and optional
cost. Its aggregate output does not echo task IDs or oracle commands. It does not launch an agent
or invent a task-success oracle.

## Optimization experiments to run before changing defaults

1. **Budget-aware packing**: compare full source, safe summary, sentence-aligned safe summary
   excerpt, and raw-source excerpt at fixed budgets. Gate on support/answer retention and
   qualifier preservation, not token count alone.
2. **Adaptive retrieval work**: `--candidate-depth adaptive` is now an opt-in performance
   experiment. It keeps wider graph/code pools and reduces routine lexical/balanced pools while
   reporting the requested and actual depth. Sample and CodeMem kept every offline quality metric
   at 1.0 with balanced depth reduced from 50 to 15; CodeMem plus 1,000 fillers reduced local
   median recall latency from 20.666 ms to 18.991 ms in a 260-recall comparison, an 8.1%
   reduction. These are machine-specific regression results, not production latency claims. Keep
   the default fixed until complete external categories meet predeclared quality margins.
3. **Packing-pressure consolidation**: prioritize memory families that are frequently recalled,
   repeatedly omitted, or costly per useful token. Count write/index/storage cost as well as later
   reader-context savings.
4. **Tokenizer-aware ingestion**: implemented behind the chunk extractor. The dependency-free
   default remains `engraphis.chars4.v1`; an explicitly configured Hugging Face reader tokenizer
   enforces prose chunk and overlap budgets and records its identity in chunk metadata. Continue
   measuring tokens-to-evidence, recall, and storage/index growth together before recommending a
   model-specific default.
5. **Bulk ingestion**: add batch embedding plus a transaction-aware vector upsert path, then rerun
   the 5.88 MB MemoryAgentBench Test-Time Learning row. Gate this on identical stored-memory,
   provenance, graph-link, and temporal-resolution outcomes, not throughput alone.
6. **Scoped caches**: benchmark query embeddings and repeat-recall results keyed by workspace,
   repo, time anchors, profile, and corpus version. Test invalidation correctness before claiming
   latency gains.
7. **Privacy-safe real usage**: use `engraphis_context_savings` to let each workspace inspect
   aggregate source/context/saved tokens already present in content-free receipts. Keep unlike
   token counters separate and require a valid receipt chain before treating totals as auditable.

## Evaluation question

The predeclared question is whether the full vector + lexical/BM25 + sparse PPR graph + calibrated
rerank pipeline, bi-temporal resolution, and grounded abstention produce higher evidence recall
per injected token than the registered baselines. The answer must come from a complete,
machine-readable artifact with paired confidence intervals; otherwise the release reports
“no demonstrated improvement.”
