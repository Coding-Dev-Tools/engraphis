# Engraphis — Implementation Guide (Phases 0–1)

This is the runnable foundation for the build described in [`MASTER_PLAN.md`](../MASTER_PLAN.md).
Phase 0 delivers the **architectural backbone**; Phase 1 delivers the **real
retrieval core** (hybrid recall + scoring + rerank + engine facade). Everything is
tested (36/36 passing) and dependency-light so it runs anywhere offline.

> Status: Phases 0–1 complete as described below. Since then, partial Phase 2/3/5 work has
> shipped on top of this foundation — deterministic write-path conflict resolution
> (`core/resolve.py`), the MCP server (`mcp_server.py`, `service.py`), governance/bi-temporal/
> proactive tools, and a code-symbol graph (`backends/codegraph.py`). See `CHANGELOG.md` for
> what shipped when and `RELEASE_READINESS.md` for current status; `AGENTS.md` §6 is the
> up-to-date phase tracker. The rest of this document is a historical snapshot of the Phase 0–1
> foundation and is still accurate for that layer. Remaining Phases 2–6 work is specified in
> `MASTER_PLAN.md` §18.

## What's here

```
engraphis/
├── core/
│   ├── interfaces.py   # Protocols (Embedder, VectorIndex, LexicalIndex, GraphStore,
│   │                   #   Reranker, LLM) + records (MemoryRecord, Node, Edge,
│   │                   #   Candidate, SearchFilter) + enums (MemoryType, Scope)
│   ├── ids.py          # ULID-style, time-sortable, prefixed identifiers
│   ├── schema.py       # v2 SQLite DDL (workspaces→repos→sessions→memories, bi-temporal
│   │                   #   graph, code symbol graph, events, audit) + FTS5 (with fallback)
│   └── store.py        # Store: CRUD, bi-temporal visibility, FTS, graph, sessions, audit
├── backends/
│   ├── vector_numpy.py          # NumpyVectorIndex — brute-force cosine (Phase-0 reference)
│   └── embedder_deterministic.py# DeterministicEmbedder — offline hashing embedder
scripts/
└── migrate_to_v2.py    # v1 (neocortex.db) → v2 scoped/bi-temporal migration
eval/
├── harness.py          # ingest → query → score retrieval (offline-runnable)
├── metrics.py          # recall@k, hit@k, answer-token-recall
└── datasets/sample.jsonl
tests/                  # 18 unit tests (ids, store, vector index, migration, eval)
conftest.py             # repo-root on sys.path; ignores legacy scripts/test_*.py
.github/workflows/ci.yml# pytest + eval gate
```

## Design contract (read this first)

Everything is built against the Protocols in `engraphis/core/interfaces.py`. Concrete
implementations are swappable via configuration — that is how the system goes from the
Phase-0 NumPy reference index to a `sqlite-vec`/LanceDB/Qdrant ANN in Phase 1, and from
the deterministic embedder to a real model (BGE-M3 / Qwen3 / Voyage), **without changing
anything above the interface boundary**. Do not let engines import a concrete backend
directly; pass interfaces in.

## How to run

```bash
pip install numpy pytest           # Phase-0 has no other hard deps

# Unit tests (18)
python -m pytest tests/ -q

# Retrieval eval (offline; the CI quality gate)
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5

# Migrate an existing v1 database (dry-run first)
python -m scripts.migrate_to_v2 --old neocortex.db --new engraphis_v2.db --dry-run
python -m scripts.migrate_to_v2 --old neocortex.db --new engraphis_v2.db
```

## Key concepts implemented

- **Scope hierarchy** (`workspace → repo → session → memory`) with `Scope` enum — the
  multi-repo / multi-session foundation. Every read takes a `SearchFilter`.
- **Bi-temporal validity** — `valid_from/valid_to` (world-time) and
  `ingested_at/expired_at` (system-time). `Store.list_memories` hides facts outside their
  validity window by default; `close_validity()` invalidates without deleting; `as_of`
  enables time-travel queries. (`test_core_store.py::test_bitemporal_visibility`.)
- **Typed memory** — `MemoryType` (working/episodic/semantic/procedural).
- **Reinforcement** — `Store.reinforce()` grows stability sub-linearly with access
  (spacing effect), the basis of the decay/recall model.
- **Hybrid arms (stubs ready)** — vector (`NumpyVectorIndex`) + lexical (`Store.fts_search`)
  + graph (`Store.neighbors`); Phase 1 fuses + reranks them (MASTER_PLAN §7).

## Phase 1 — the retrieval core (done)

New modules, all behind the Phase-0 interfaces:

```
engraphis/core/
├── scoring.py   # Ebbinghaus retention, recency, staleness, per-type weights,
│                #   reciprocal-rank fusion, six-term score_memory()
├── recall.py    # RecallEngine: vector+lexical+graph arms → RRF → weighted score
│                #   → rerank → context packing → reinforcement (MASTER_PLAN §7)
└── engine.py    # MemoryEngine facade: remember() / recall() / sessions; .create()
engraphis/backends/
├── reranker.py        # IdentityReranker (offline) + CrossEncoderReranker + factory
├── embedder_st.py     # SentenceTransformerEmbedder + get_embedder() fallback
└── vector_sqlitevec.py# SqliteVecVectorIndex + get_vector_index() fallback to NumPy
eval/ablation.py        # vector-only vs hybrid comparison (recall@k)
```

Quick start with the engine:

```python
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType

eng = MemoryEngine.create("engraphis.db")          # local-first; offline-capable defaults
wid = eng.store.get_or_create_workspace("default")
rid = eng.store.get_or_create_repo(wid, "my-repo")

eng.remember("We deploy via GitHub Actions to AWS ECS.", workspace_id=wid, repo_id=rid,
             mtype=MemoryType.SEMANTIC, importance=0.8)
res = eng.recall("how do we deploy?", workspace_id=wid, k=5)
print(res.context)          # packed, provenance-tagged context for the LLM
```

To go production-grade, pass real backends (no code change above the interface):

```python
eng = MemoryEngine.create("engraphis.db",
    embed_model="BAAI/bge-m3",                       # real embeddings
    vector_backend="sqlite-vec",                     # ANN in the same file
    rerank_model="BAAI/bge-reranker-v2-m3")          # cross-encoder rerank
```

Run the ablation: `python -m eval.ablation`.

## What Phase 2 adds (next)

1. LLM fact extraction + ADD/UPDATE/NOOP/INVALIDATE conflict resolution (MASTER_PLAN §8.3).
2. Full Personalized PageRank graph arm over the bi-temporal graph (replacing the
   current 1-hop entity expansion).
3. A-MEM linking + memory evolution; the consolidation/reflection loop (§8.5).
4. Wire LoCoMo / LongMemEval datasets into the harness; keep the CI gate.

## Note on the dev environment

The repo lives in a OneDrive-synced folder. The unit suite was verified green (18/18)
from a clean checkout. If you ever see a transient `SyntaxError` or stale-attribute error
immediately after an edit, it is OneDrive mid-sync, not the code — re-run once sync
settles (local Windows checkouts are not affected).
