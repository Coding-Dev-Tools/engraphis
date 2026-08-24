# Benchmarks

This guide explains what Engraphis measures, how to reproduce each evaluation, and the limits of
those results. When this document and the code disagree, the code is the source of truth.

For the locked operator sequence for a public canonical run, see
[`docs/PUBLIC_BENCHMARK_RUNBOOK.md`](docs/PUBLIC_BENCHMARK_RUNBOOK.md).

### Public numeric evidence registry

Every exact public aggregate retained below comes from the checked-in, public-safe
[`offline-fixtures-v1.json`](docs/benchmark-evidence/offline-fixtures-v1.json) artifact. Its
SHA-256 is
`0f60b0868444f676fe14c5f94d7db2c475e22669930c4d760881d0842eaa6800`, also recorded in the
adjacent `.sha256` file. The artifact contains no raw questions, answers, prompts, customer data,
or per-record content fingerprints.

The fixture-suite digest is
`4d7e40607319cd4bf8caee3897f1e416dbe5b81998b37a7e4839409ee2923537`. The artifact defines
the digest algorithm and records the SHA-256 of every suite and dataset file. Each evidence ID
also binds its exact command through `sha256(UTF-8 exact command)`:

| Evidence ID | Exact command | Config digest |
|---|---|---|
| `offline-chunking` | `python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5` | `c1c8196aa7e1568ef3844a9fb2d76b87f342c39108e32d6ad144b885a76143b8` |
| `offline-performance` | `python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10 --json` | `bbe4aca81e58d4830e50a8fc7729a1d15b71d97a6299bccd79432b7f119677d7` |
| `offline-grounded` | `python -m eval.grounded` | `590442e51e3642c10489165759919dc86ffac62c182937330c153e7f8d5fc26f` |

External, model-dependent, latency, consolidation, and productivity numbers are not published
until a redacted immutable artifact with the same three bindings exists. Use the
[public benchmark runbook](docs/PUBLIC_BENCHMARK_RUNBOOK.md) to produce that evidence; absence
from this registry means no public number is claimed.

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
- **Grounded**: `eval/grounded.py`: answerable → cite, off-topic → abstain. Exact fixture
  outcomes are evidence ID `offline-grounded` in the registry above.
- **Chunking (quality per token)**: `eval/chunking_eval.py` over `eval/datasets/longdoc.jsonl`
  ingests a multi-topic corpus twice: once as one memory per document (`whole`) and once with
  sub-file `ChunkingExtractor` (`chunked`), then queries both through the real recall pipeline.
  The checked-in corpus is explicitly marked trusted eval data so the measurement isolates
  chunking from the production trust gate, which excludes arbitrary raw imports from normal
  agent context. On the deterministic embedder, **recall@5 is 1.000 for both modes; mean
  retrieved top-5 content falls from 740.3 to 214.3 tokens (526.0 fewer, 71.1% lower, about
  3.5× smaller), while the smallest returned evidence-holding memory falls from 162.2 to 42.4
  tokens (119.8 fewer, 73.9% lower, about 3.8× smaller).** These aggregates are evidence ID
  `offline-chunking` in the registry above. Pass `--embed-model
  sentence-transformers/all-MiniLM-L6-v2` to run a model-dependent experiment; do not publish
  that result without a new immutable artifact and pinned model revision.
- **Full-pipeline latency + quality**: `eval/performance.py` times the shipped semantic +
  lexical + graph + fusion + scoring + rerank + packing path after warmup, with reinforcement
  disabled so repeated measurements do not mutate their corpus. It reports p50/p95/p99 latency,
  retrieval quality, packed context tokens, and full/compact JSON-shape payload proxies in one
  JSON-safe schema. Payload proxies are sampled once per question, independently of the number
  of timed iterations; they are not serialized MCP envelopes or transport responses. In the
  registered CodeMem run, 26 payload samples total **23,810** full-proxy
  `engraphis.regex.v1` tokens versus **10,202** compact-proxy tokens, avoiding **13,608** proxy
  tokens (**57.15% lower**), while 260 recalls are timed. Packed context across the same 26
  samples averages **85.38** tokens and reaches **108** under a 1,500-token cap; Recall@5,
  hit@5, and answer-token recall remain 1.000. These aggregates are evidence ID
  `offline-performance` in the registry above. `--filler-memories`, `--candidate-k`, and
  `--retrieval-profile` make scaling and routing experiments executable, but their results need
  separate evidence before publication.
- **Exact vector scale envelope**: `eval/vector_scale.py` measures the production
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
- **Dashboard graph layout settle**: `eval/graph_every_bench.py` drives the Every-node
  dashboard engine's real worker (`engraphis-graph-every-worker.js`) through a
  `prepare → settled` round-trip over synthetic node/link loads and reports wall-clock settle
  time plus the scaling ratio across sizes. It measures initial layout cost only: camera pans
  and zooms never touch the worker (they are GPU-uniform updates), so no per-frame number can
  come out of this harness and none should be quoted. Results are host- and Node-version
  dependent local diagnostics, not registered public evidence; run the harness on the target
  class of machine before quoting a figure.

The context-economy and productivity tools intentionally report when a small workload does not
benefit from memory, and the external loaders expose retrieval-quality tradeoffs rather than
hiding them. Their prior local results are not retained as public numbers because no matching
redacted immutable artifact is checked in. Run the registered protocol and publish the resulting
artifact before making a quantitative claim.

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

# External retrieval diagnostics (downloads all-MiniLM-L6-v2; not QA/leaderboard results)
python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10
python -m eval.external --dataset locomo10.json      --format locomo      --k 10
# Complete external-dataset coverage with an immutable embedding revision. This remains a
# private diagnostic; it is not an official benchmark-harness or public evidence artifact.
python -m eval.external --dataset longmemeval_s.json --format longmemeval --canonical \
  --embed-revision <40-character-model-commit> --json external-longmemeval.json
python -m eval.external --dataset locomo10.json --format locomo --canonical --no-resolve \
  --embed-revision <40-character-model-commit> \
  --locomo-repair-manifest eval/datasets/locomo10_repair_manifest.json \
  --json external-locomo.json
python -m eval.context_economy --dataset locomo10.json --format locomo \
  --embed-model sentence-transformers/all-MiniLM-L6-v2 --token-budget 512 --k 10 --no-resolve
```

Canonical external mode requires an exact lowercase 40-character embedding commit and a semantic
embedder; dependency or model-load failure is fatal instead of silently falling back to hashing.
Every report records `embedding`, `dataset_sha256`, `source_cases`, `normalized_cases`, and
`configuration` provenance so a result can be attributed to the actual data and retrieval setup.

The official ten-conversation LoCoMo JSON contains delimiter-packed IDs, two mechanical ID
typos, and three references that cannot be normalized syntactically. The adapter normalizes only
the unambiguous forms. The checked-in repair manifest is bound to the official source SHA-256,
names every remaining replacement/removal, must be fully consumed, and is recorded in the JSON
report with its own hash. Any source update, unused repair, or unresolved ID fails the run. This
repairs retrieval references only; it does not claim to correct LoCoMo's semantic answer labels.

A private pinned retrieval diagnostic was inspected during development, but its result artifact is
not checked into the public evidence registry. This document therefore publishes none of that
run's workload counts or scores. Reproduce it from the hash-bound source and repair manifest,
export a public-safe immutable artifact, and validate its checksum before adding quantitative
claims. Any future values remain evidence-retrieval metrics, not end-to-end QA accuracy or an
official LoCoMo leaderboard score.

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
`python -m eval.longmemeval_v2_evidence`. The exporter requires the completion manifest written by
the pinned runner after a successful, complete official run. It binds the exact per-question
output, questions, haystack, trajectories, memory configuration, matrix manifest, seed, clean
official checkout, and recorded environment. The public artifact keeps the official QA score,
fixed-reader context token count, aggregate source-file digests, repository state, and artifact
checksum. It removes raw questions, answers, prompts, reader output, and retrieved context, and
does not publish per-record content fingerprints. See the
[`public benchmark runbook`](docs/PUBLIC_BENCHMARK_RUNBOOK.md) for the end-to-end operator sequence.

### LongMemEval-V2 memory-module adapter

`eval.longmemeval_v2.EngraphisLongMemEvalV2Memory` follows the official
`memory_modules.memory.Memory` interface at LongMemEval-V2 commit
`6f020ac2fc3275e46c706d3406e02c3ed79b7be2`. When imported in that environment, its
`@register_memory` decorator registers `memory_type="engraphis"`; use the checked-in
[`eval/configs/longmemeval_v2_engraphis.json`](eval/configs/longmemeval_v2_engraphis.json)
with the official harness. The config pins `Qwen/Qwen3-Embedding-8B` to revision
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`; mutable embedding revisions are rejected, and a
canonical adapter run fails instead of relabeling the deterministic offline fallback as Qwen.
First materialize the six declared variants at all five token budgets:

```bash
python -m eval.longmemeval_v2_matrix \
  --output "$ENGRAPHIS_EVIDENCE_RUN_DIR/configs"
```

This writes a 30-run manifest: balanced, planner, episodic-cap, planner-plus-episodic-cap, and
matched `context_k=2` comparators for both capped variants, each at 256, 512, 1,024, 2,048, and
4,096 evidence tokens. Run each manifest cell through `python -m eval.run_longmemeval_v2` with all
eight `--engraphis-*` completion-receipt arguments. The wrapper imports the adapter before the
official registry builds the memory module, forces the pinned reader processor revision, and
delegates the remaining official harness arguments unchanged. Only after a successful return does
it verify that the output question IDs exactly cover the source question IDs and write the
immutable execution manifest.

The checked-in configuration is canonical only when the adapter resolves the pinned Qwen reader
processor at `c202236235762e1c871ad0ccb60c8ee5ba337b9a`. The wrapper refuses a dirty or non-pinned
official checkout and refuses to start if the optional processor dependency or immutable revision
is unavailable; the local regex counter is never silently relabeled as a reader budget. The
recorded budget counts each returned context item's content with that reader tokenizer (without
prompt framing or inter-item separators), so it is a hard **evidence-item content** budget, not a
claim about total chat-prompt tokens. Packed sources are returned as separate context items,
preserving the largest fitting evidence prefix instead of dropping one oversized monolithic item.
Every official per-question row reports inserted and retrieved counts by memory type. A
memory-type-cap claim additionally requires at least two populated inserted types, so a nominal cap
over a single-type workload cannot qualify as evidence. The adapter does not download benchmark
data or call the reader/evaluator; the official harness owns those steps.

## External evidence status and remaining executions

1. **Run the official LongMemEval-V2 reader and evaluator.** The adapter, pinned runner, and
   redacted evidence exporter are implemented. The exact upstream commit boots in an isolated
   Python 3.11 environment and the wrapper reaches the official harness CLI. The dataset, pinned
   Qwen reader, and embedding assets require substantial storage and compute; no canonical QA
   score is claimed until that run completes.
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

### Upstream-data diagnostics awaiting public artifacts

The LoCoMo-Plus, MemoryAgentBench, and Mem2ActBench adapters have been exercised against upstream
data and exposed useful product gaps. Their earlier local envelopes are not present in the
checked-in evidence registry, so this document withholds their case counts, retrieval scores,
token coverage, and throughput measurements. Rerun each adapter with `--artifact`, publish the
redacted immutable envelope and checksum, and add its suite/config binding before quoting a
number. Until then these lanes demonstrate executable plumbing only, not leaderboard,
answer-quality, or marketing results.

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
2. **Adaptive retrieval work**: `--candidate-depth adaptive` is an opt-in performance experiment.
   It keeps wider graph/code pools and reduces routine lexical/balanced pools while reporting the
   requested and actual depth. A local experiment motivated this option, but no public number is
   retained because its machine-specific artifact is not in the evidence registry. Keep the
   default fixed until complete external categories meet predeclared quality margins.
3. **Packing-pressure consolidation**: prioritize memory families that are frequently recalled,
   repeatedly omitted, or costly per useful token. Count write/index/storage cost as well as later
   reader-context savings.
4. **Tokenizer-aware ingestion**: implemented behind the chunk extractor. The dependency-free
   default remains `engraphis.chars4.v1`; an explicitly configured Hugging Face reader tokenizer
   enforces prose chunk and overlap budgets and records its identity in chunk metadata. Continue
   measuring tokens-to-evidence, recall, and storage/index growth together before recommending a
   model-specific default.
5. **Bulk ingestion**: add batch embedding plus a transaction-aware vector upsert path, then rerun
   the complete MemoryAgentBench Test-Time Learning input. Gate this on identical stored-memory,
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
