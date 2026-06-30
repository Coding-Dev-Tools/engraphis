# AGENTS.md — Engraphis

Engraphis is a **local-first, open AI memory engine for agents** — Ebbinghaus decay,
interaction-aware reinforcement, bi-temporal facts, hybrid recall, and a native
`workspace → repo → session → memory` hierarchy. Python 3.11 / FastAPI, SQLite, local
embeddings; the external LLM is optional and pluggable.

This is the canonical operating manual for any AI agent working in this repo. `CLAUDE.md`
imports it. Read §0 before editing anything.

---

## 0. Read this first — two architectures live in one package

There are **two parallel codebases** under `engraphis/`. Confusing them is the single
most common mistake here.

| | **v2 — the target (build here)** | **v1 — legacy neocortex clone (running server)** |
|---|---|---|
| Status | The future, per `MASTER_PLAN.md`. Phases 0–1 done. | Working reference implementation; flat namespaces. |
| Model | Scoped + bi-temporal + typed; interface-driven. | Single flat `namespace` string per memory. |
| Code | `engraphis/core/`, `engraphis/backends/`, `eval/`, `tests/`, `scripts/migrate_to_v2.py` | `engraphis/app.py`, `config.py`, `models.py`, `routes/`, `stores/`, `engines/`, `llm/`, `static/` |
| Data | new v2 schema (`SCHEMA_VERSION = 2`) | `neocortex.db` |
| Entry | `MemoryEngine.create()` → `core/engine.py` | `python -m scripts.start_server` → FastAPI on :8700 |

**Rule:** build new capability on **v2** (`core/` + `backends/`) behind the interfaces.
Only touch the v1 server for compatibility fixes or to keep the reference running. When a
task is ambiguous, decide which side it belongs to *before* editing.

---

## 1. Commands

```bash
# ── Install ──────────────────────────────────────────────────────────────────
pip install numpy pytest            # v2 core + tests, fully OFFLINE (this is what CI does)
pip install -e ".[dev]"             # full stack: FastAPI server, ST embeddings, ruff
cp .env.example .env                # only needed for the v1 server / LLM features

# ── Quality gate (offline, no API key — KEEP THIS GREEN; mirrors .github/workflows/ci.yml) ──
python -m pytest tests/ -q                                         # 36 unit tests
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5  # retrieval eval gate
python -m eval.ablation                                            # vector-only vs hybrid
ruff check .                                                       # lint (line-length 100, py39)

# ── Run the v1 server (needs the full install) ───────────────────────────────
python -m scripts.start_server      # http://127.0.0.1:8700  (dashboard at /, OpenAPI at /docs)
python -m scripts.test_routes       # HTTP smoke test — requires a running server + httpx
python -m scripts.cli recall "what do we know about X" -n vault    # CLI: ingest/recall/chat/thoughts/list

# ── v2 data migration (v1 flat namespaces → v2 scoped/bi-temporal) ───────────
python -m scripts.migrate_to_v2 --old neocortex.db --new engraphis_v2.db --dry-run
python -m scripts.migrate_to_v2 --old neocortex.db --new engraphis_v2.db

# ── Seed memories from an Obsidian/markdown vault (v1) ───────────────────────
python -m scripts.seed_from_obsidian "C:/path/to/Vault" --namespace vault
```

`requires-python >= 3.9` (ruff targets `py39`); CI and the recommended dev environment use **3.11**.

---

## 2. The v2 recall pipeline (where the real work is)

`core/recall.py::RecallEngine.recall()` is the heart of the system. Flow:

```
query
  └─ SearchFilter (scope + as_of time anchor)            core/interfaces.py
     └─ 3 retrieval arms (run in parallel, then fused):
        • vector   — VectorIndex.search (cosine)         backends/vector_*.py
        • lexical  — Store.fts_search (FTS5/BM25 + LIKE fallback)   core/store.py
        • graph    — 1-hop entity expansion (full PPR is Phase 2)   core/recall.py
     └─ RRF fusion + six-term weighted score             core/scoring.py
     └─ rerank top-N                                      backends/reranker.py
     └─ context packing (token budget) + reinforce()      core/recall.py / core/store.py
```

Backends are selected by `get_embedder()` / `get_vector_index()` / `get_reranker()` and
injected through `MemoryEngine` — never imported directly inside `core/` (see §3.1).

---

## 3. Non-negotiable conventions (load-bearing — from `MASTER_PLAN.md` §4)

1. **Interfaces before implementations.** `core/` and `engines/` depend only on the
   Protocols in `core/interfaces.py` (`Embedder`, `VectorIndex`, `LexicalIndex`,
   `GraphStore`, `Reranker`, `LLM`). **Never import a concrete backend inside `core/`** —
   inject it. Swapping `sqlite-vec`→Qdrant, or a local embedder for an API, must be a
   *config change, not a refactor*.
2. **Forgetting lowers retrieval priority; it never hard-deletes.** Decay adjusts
   `stability`. Hard deletion is explicit, governed, and audited (`Store.audit`).
3. **Truth is temporal.** Resolve contradictions by **invalidation, not overwrite**:
   `Store.close_validity()` / `invalidate_edge()` set `valid_to`. Preserve history; support
   `as_of` time-travel reads.
4. **Everything is scoped.** Every memory carries a `Scope` + `workspace/repo/session`.
   Every read takes a `SearchFilter`. Scope promotion is an explicit operation.
5. **Memory is typed** (`working` / `episodic` / `semantic` / `procedural`), each with its
   own weight profile (`scoring.DEFAULT_WEIGHTS`) and lifecycle. Treat them differently.
6. **Provenance always.** Set `provenance` on memories and edges so "why is this known?"
   is answerable.
7. **Prove "better" with a number.** No retrieval/quality claim ships without an eval.
   Keep the CI gate green; extend `eval/` when you change ranking.
8. **Local-first & offline-capable.** The core must run with **only `numpy`** (deterministic
   embedder + NumPy index). Do not add hard dependencies to `core/`; gate heavy imports
   (sentence-transformers, sqlite-vec) behind the backend factories.

---

## 4. Core algorithms cheat-sheet (`core/scoring.py`, `core/store.py`)

- **Six-term recall score** (`score_memory`):
  `score = w_r·retention + w_s·semantic + w_l·lexical + w_g·graph + w_i·importance + w_c·recency − w_x·staleness`.
  Arm scores are **min-max normalized before fusion** so no arm dominates by raw scale.
  Default weights: `r1.0 s1.0 l0.5 g0.7 i0.6 c0.3 x0.8`, overridden per memory type.
- **Ebbinghaus retention:** `R(t) = exp(−Δt_days / S)`.
- **Reinforcement (spacing effect):** `S_new = S·(1 + α·ln(1 + access_count)) + boost`, `α = 0.3`.
  Stability grows sub-linearly with use; this is `Store.reinforce()`.
- **Interaction boosts** (`scoring.INTERACTION_BOOST`): view/read 0.05 · recall 0.15 ·
  react 0.20 · engage 0.30 · reply 0.50 · create 1.00.
- **Reciprocal Rank Fusion:** `1 / (k + rank + 1)`, `k = 60`.

These are pure, unit-tested functions — change them only with a corresponding `tests/` +
`eval/` update.

---

## 5. Data model cheat-sheet (`core/interfaces.py`, `core/schema.py` — `SCHEMA_VERSION = 2`)

- **Scope hierarchy:** `workspace → repo → session → memory`. Scopes: `session|repo|workspace|user`.
- **Bi-temporal validity on every record:** world-time `valid_from/valid_to` +
  system-time `ingested_at/expired_at`. Reads hide facts outside their validity window
  unless `include_invalid=True` or an `as_of` anchor is given.
- **IDs:** ULID, time-sortable, **typed prefixes** (`ws_`, `repo_`, `ses_`, `mem_`, `ent_`,
  `edg_`, `sym_`, `evt_`, `job_`, `aud_`) — `core/ids.py`. Lexicographic sort == chronological.
- **Tables:** `workspaces`, `repos`, `sessions`, `memories`, `mem_vectors`,
  `mem_fts` (FTS5 + plain-table fallback), `entities`, `edges` (bi-temporal), `mem_links`,
  `symbols`, `code_edges`, `events`, `audit`, `schema_migrations`.
- **Vectors are stored L2-normalized** so cosine similarity == dot product.

---

## 6. Status — what's real vs planned (`MASTER_PLAN.md` §18)

- **Done:** **Phase 0** (interface contracts, scoped + bi-temporal schema, v1→v2 migration,
  eval harness + CI) and **Phase 1** (hybrid recall, six-term score, RRF, rerank;
  SentenceTransformer embedder + `sqlite-vec` index, each with an offline fallback). 36 tests green.
- **Planned (do not assume these exist yet):** Phase 2 — LLM fact extraction +
  `ADD/UPDATE/NOOP/INVALIDATE` conflict resolution, full Personalized PageRank, A-MEM
  linking. Phase 3 — tree-sitter code-symbol graph, **MCP server**, session lifecycle
  (no MCP server exists in the tree today). Phase 4 — consolidation
  (episodic→semantic→procedural) + scope promotion. Phase 5 — security/SDKs. Phase 6 — Rust hot path.
- The **v1 FastAPI server** is the legacy neocortex clone and still runs; treat it as a
  compatibility/reference surface, not the place for new capability.

---

## 7. Gotchas

- **Offline by default in core:** `MemoryEngine.create()` uses a deterministic hashing
  embedder + NumPy index, so tests need no model download or network. Real models load only
  when you pass `embed_model=...` / `vector_backend="sqlite-vec"`.
- **First full-stack run downloads `all-MiniLM-L6-v2` (~80 MB)** for the ST embedder.
- **FTS5 may be missing** on some SQLite builds → `Store` auto-falls back to `LIKE`
  (`self.has_fts5`). Don't assume BM25 is available.
- **Secrets & data are git-ignored:** `.env`, `neocortex.db`, `*.db-wal`, `*.db-shm`. Never
  commit, print, or paste their contents.
- **No `.git` in this checkout** — don't rely on `git log`/`blame` in-session. CI
  (`.github/workflows/ci.yml`) runs against the GitHub `main` branch.
- **Synced-folder flakiness:** if the repo sits on OneDrive, a transient `SyntaxError` or
  stale-attribute error immediately after an edit is mid-sync, not your code — re-run once.
- **`README.md` "Project Structure" is stale** (it names a `neocortex/` package; the real
  package is `engraphis/`). Trust the tree in this file.

---

## 8. Source-of-truth docs

- **`MASTER_PLAN.md`** — the build spec and final authority for v2: ontology §5,
  architecture §6, recall §7, write path §8, schema §12, algorithms §13, eval §14, roadmap §18.
- **`docs/IMPLEMENTATION.md`** — Phase 0–1 status and the interface design contract.
- **`README.md`** — v1 server usage + the REST API table (note the stale package name).
- **`AGENTS.md`** (this file) + **`CLAUDE.md`** — how to work in the repo.

> When code and docs disagree, the code wins — then fix the doc in the same change.
