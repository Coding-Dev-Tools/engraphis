# Engraphis benchmarks

> "Better needs a number, not an assertion." (AGENTS.md §3.7) — this file holds the numbers, and
> is explicit about what each one does and does not prove. Nothing here is hand-typed: every
> figure in §1 is reproducible with the commands shown, offline, in under a minute.

There are two tiers of measurement, and conflating them is dishonest:

1. **The offline regression harness (§1).** Runs in CI with the deterministic hashing embedder —
   no model download, no network, no GPU. It proves the *pipeline is wired correctly and stays
   correct* (write-path resolution → hybrid recall → scoring → fusion → rerank). It is a
   correctness floor, **not** a semantic-quality or competitive score. The datasets are small,
   curated regression fixtures and most metrics saturate at `k ≥ 2` by design.
2. **External competitive benchmarks (§2).** LoCoMo and LongMemEval with a *real* embedder. These
   are the numbers that compare Engraphis to mem0 / Zep / Letta on quality. They need `torch` +
   the datasets and are **run separately** (they don't run in the offline sandbox/CI). This file
   documents the exact commands; publish the results here once run on a machine with the models.

---

## 1. Offline regression harness (reproducible, deterministic embedder)

Environment: Python 3.10+, `numpy` only, `PYTHONPYCACHEPREFIX=/tmp/pyc`. No `torch`, no
`sentence-transformers`, no network. This is exactly what `.github/workflows/ci.yml` runs.

### 1.1 Retrieval quality — `eval.harness`

```bash
python -m eval.harness --dataset eval/datasets/sample.jsonl  --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
```

| Dataset | Questions | k | recall@k | hit@k | answer-token recall |
|---------|----------:|--:|---------:|------:|--------------------:|
| `sample.jsonl`  |  4 | 1 | 1.000 | 1.000 | 1.000 |
| `sample.jsonl`  |  4 | 5 | 1.000 | 1.000 | 1.000 |
| `codemem.jsonl` | 26 | 1 | **0.962** | 0.962 | 0.962 |
| `codemem.jsonl` | 26 | 2 | 1.000 | 1.000 | 1.000 |
| `codemem.jsonl` | 26 | 5 | 1.000 | 1.000 | 1.000 |

The harness routes ingestion *and* querying through `MemoryEngine` — the same write-path conflict
resolution, hybrid vector+lexical+graph recall, six-term scoring, RRF fusion, and rerank that ship
in production. It measures what ships, not a bare index lookup. The single miss at `k=1` on
`codemem` (25/26) is recovered by `k=2`; the fixtures are deliberately small, so treat 1.000 as
"pipeline healthy," not "state of the art."

### 1.2 Component ablation — `eval.ablation`

```bash
python -m eval.ablation                       # sample.jsonl, k=5 (saturates)
# codemem.jsonl swept across k (shown below):
```

recall@k on `codemem.jsonl`, retrieval arms only (no write-path resolution):

| k | vector-only | hybrid (1-hop) | hybrid (PPR) |
|--:|------------:|---------------:|-------------:|
| 1 | 0.885 | 0.885 | 0.885 |
| 2 | 1.000 | 1.000 | 1.000 |
| 5 | 1.000 | 1.000 | 1.000 |

**Honest reading:** on these tiny fixtures the graph arm (1-hop / PPR) does not separate from
vector-only — there aren't enough multi-hop questions for it to matter, and everything saturates
by `k=2`. The ablation's *value here is the scaffold*, not the gap: it proves quality can be
attributed to each stage. The gap that makes graph recall and the lexical arm earn their place
only shows up on the multi-hop, long-context external datasets in §2 with a real embedder. Do not
cite the equal ablation rows as evidence the arms are redundant — cite §2 once it's run.

### 1.3 What the offline gate asserts

Running the full gate green means: the write path resolves conflicts without dropping live facts,
all three retrieval arms fuse and rank, and the end-to-end pipeline recovers the supporting memory
for every fixture question by `k=2`. That is a regression guarantee, not a leaderboard.

---

## 2. External competitive benchmarks (run separately, then publish here)

These require `pip install -e ".[dev]"` (pulls `torch` + `sentence-transformers`) and the dataset
files, which are **not** committed and **not** available in the offline CI sandbox. Run them on a
machine with the models, then paste the results into the empty table below.

```bash
# LoCoMo (long-conversation memory)
python -m eval.external --dataset locomo10.json      --format locomo      --k 10

# LongMemEval (long-context memory QA)
python -m eval.external --dataset longmemeval_s.json --format longmemeval

# Plumbing check only (offline, no real embedder — verifies the loader, not quality)
python -m eval.external --dataset locomo10.json --format locomo --offline --limit 2
```

| Benchmark | Metric | Engraphis | mem0 | Zep | Letta | Notes |
|-----------|--------|----------:|-----:|----:|------:|-------|
| LoCoMo | recall@10 | _TBD_ | | | | real embedder required |
| LongMemEval-S | accuracy | _TBD_ | | | | real embedder required |

**Rule for this table:** every cell is either a number you produced with the command above on a
named commit + embedder, or blank. No estimates, no "expected" values. A blank cell is more honest
than a guessed one, and this is the table that actually differentiates Engraphis in market — so it
has to be unimpeachable.

---

## 3. Reproducibility notes

- Numbers in §1 were captured on `numpy 2.2.6`, Python 3.10, deterministic embedder (`dim=256`),
  `IdentityReranker`. They are deterministic: same input → same output, every run.
- `answer_token_recall` measures how much of the gold answer's token set appears in the retrieved
  memories' text — a cheap proxy for "did we retrieve enough to answer," not answer generation
  quality.
- The deterministic embedder is a hashing stand-in with no learned semantics. It is the floor:
  a real embedder (§2) can only do better on semantic matches, so §1 passing is necessary but far
  from sufficient. Never quote §1 as a quality or competitive result.
