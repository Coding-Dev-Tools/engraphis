# Benchmarks

This file is the honest status of what Engraphis measures today, how to reproduce it, and what
it does **not** yet claim. It exists because the README linked a `BENCHMARKS.md` that had never
been written; when this and the code disagree, the code wins (CLAUDE.md).

## What we measure today (all offline, no API key)

Engraphis's eval harness scores **retrieval**, not end-to-end QA. That distinction is deliberate
and stated everywhere the numbers appear (`eval/external.py`).

- **Correctness gate** — `eval/harness.py` over `eval/datasets/sample.jsonl` and
  `codemem.jsonl` (conflict resolution) and `graph_multihop.jsonl` (multi-hop graph recall).
  Runs on the deterministic embedder, so it is a plumbing/regression floor, not a competitive
  number. This is the gate CI enforces.
- **Ablation** — `eval/ablation.py`: vector-only vs. 1-hop graph vs. Personalized-PageRank arm,
  to show the graph arm actually earns its place.
- **External benchmarks** — `eval/external.py` loads **LoCoMo** and **LongMemEval** and pushes
  them through the *real* `MemoryEngine` write path (conflict resolution + evolution) and hybrid
  recall with a real sentence-transformers embedder. It reports `recall_at_k` / `hit_at_k` /
  `answer_token_recall` — i.e. *did the evidence come back*, not *did an LLM answer correctly*.
- **Grounded** — `eval/grounded.py`: answerable → cite, off-topic → abstain.
- **Chunking (quality per token)** — `eval/chunking_eval.py` over `eval/datasets/longdoc.jsonl`
  ingests a multi-topic corpus twice — one memory per document (`whole`) vs. sub-file
  `ChunkingExtractor` (`chunked`) — and queries both through the real recall pipeline. This is
  the first cut of the context-reduction metric (item 3 below). On the deterministic embedder:
  **recall@5 1.000 for both, at ~73% fewer context tokens (826 → 224) and ~4× smaller
  tokens-to-evidence (162 → 42).** Pass `--embed-model sentence-transformers/all-MiniLM-L6-v2`
  for a real retrieval number (recall should then favour chunked on larger corpora, not just
  tie).

### Reproduce

```bash
# Correctness gate (deterministic, no download)
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.harness --dataset eval/datasets/graph_multihop.jsonl --k 5
python -m eval.ablation

# Real retrieval numbers (downloads all-MiniLM-L6-v2)
python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10
python -m eval.external --dataset locomo10.json      --format locomo      --k 10
```

## What we do NOT yet claim

- **No end-to-end QA accuracy.** The LoCoMo / LongMemEval percentages that mem0, Zep, and
  others publish depend on an answering LLM + a judge. Engraphis isolates the retrieval
  half it owns and says so; it does not yet run the answering layer.
- **No published latency.** There is no measured p50/p95 recall latency in-repo; we have not
  measured our equivalent. The Rust hot path (Phase 6) is not started.
- **No neutral third-party ranking.** We have not run an external eval platform.

## Plan to produce publishable numbers

1. **Add a QA layer to `eval/external.py`.** Optional answering model + judge on top of the
   existing retrieval pipeline, so we can report end-to-end accuracy on the same datasets the
   field quotes — reusing the retrieval harness underneath.
2. **Measure recall latency.** Instrument `RecallEngine.recall()` end to end (parallel arms →
   RRF → score → rerank → pack) and publish p50/p95 on a fixed corpus and machine class.
3. **Adopt a context-reduction metric.** Report **recall@k against tokens injected** — recall
   at a fixed token budget, and tokens-to-first-correct-evidence. It is a natural fit: Engraphis
   already does token-budget context packing in recall and already reports a **compaction**
   number from consolidation (`core/consolidate.py::_compaction`). Wire those two together into
   one "quality per token" curve — arguably our strongest story, since decay + consolidation
   are built to raise it.
4. **Run an external eval platform** for a neutral comparison once (1)–(3) exist.

## Where our design should win

Framed honestly, the retrieval stack is already richer than a vector DB or a vector+keyword
system: four arms (vector + lexical/BM25 + PPR graph + rerank), bi-temporal truth with
supersession, Ebbinghaus decay + reinforcement, and grounded recall that abstains below a support
floor. The open question these benchmarks must answer is whether that richness converts into
higher recall **per token injected** — that is the number to chase, and the reason metric (3) is
the priority.