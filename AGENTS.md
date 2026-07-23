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

| | **v2 — current architecture (build here)** | **v1 — legacy reference server** |
|---|---|---|
| Status | Primary scoped, bi-temporal, interface-driven implementation. | Compatibility/reference implementation with flat namespaces. |
| Model | Scoped + bi-temporal + typed; interface-driven. | Single flat `namespace` string per memory. |
| Code | `engraphis/core/`, `engraphis/backends/`, `eval/`, `tests/`, `scripts/migrate_to_v2.py` | `engraphis/app.py`, `config.py`, `models.py`, `routes/`, `stores/`, `engines/`, `llm/`, `static/` |
| Data | new v2 schema (`SCHEMA_VERSION = 4`) | `engraphis_v1.db` |
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
python -m pytest tests/ -q                                          # unit tests (offline)
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5   # retrieval eval gate
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5  # larger eval; covers conflict resolution
python -m eval.ablation                                             # vector-only vs 1-hop vs PPR
ruff check .                                                        # lint (line-length 100, py39)

# ── External benchmarks (real numbers need torch + the dataset; see eval/external.py) ──
python -m eval.external --dataset locomo10.json --format locomo --k 10        # LoCoMo
python -m eval.external --dataset longmemeval_s.json --format longmemeval     # LongMemEval
python -m eval.external --dataset locomo10.json --format locomo --offline --limit 2  # plumbing check

# ── Unified dashboard + memory inspector ──
python -m scripts.start_dashboard    # http://127.0.0.1:8700
# Use this unified launcher; there is no separate Inspector service.

# ── Onboarding (writes .env with an absolute DB path; doctor mode verifies install) ──
engraphis-init                   # or: python -m scripts.init
engraphis-init --check

# ── Customer-side hosted session ───────────────────────────────────────────
ENGRAPHIS_CLOUD_CONTROL_URL=https://api.engraphis.com
ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL=...  # secret; prefer the owner-only session file
ENGRAPHIS_CLOUD_TOKEN_SUBJECT=member    # device or member, fixed at bootstrap
# Authorization, billing, relay, compute, and worker implementations are private services.

# ── Sleep-time consolidation (schedulable local job; also an MCP tool) ────────
python -m scripts.consolidate --db engraphis.db --workspace acme --dry-run

# ── Sync (local shared-folder transport or hosted Cloud Sync — see docs/SYNC.md) ──
python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis --dry-run
python -m scripts.sync --db engraphis.db --workspace acme --relay https://relay.engraphis.com  # or bare --relay + ENGRAPHIS_RELAY_URL

# ── Run the v1 server (needs the full install) ───────────────────────────────
python -m scripts.start_server      # http://127.0.0.1:8700  (dashboard at /, schema at /openapi.json)
python -m scripts.test_routes       # HTTP smoke test — requires a running server + httpx
python -m scripts.cli recall "what do we know about X" -n vault    # CLI: ingest/recall/chat/thoughts/list

# ── v2 data migration (v1 flat namespaces → v2 scoped/bi-temporal) ───────────
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db --dry-run
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db

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
        • graph    — Personalized PageRank over entities+links      core/recall.py + core/graphrank.py
                     (graph_mode="1hop" keeps the old expansion for ablation)
     └─ RRF fusion + six-term weighted score             core/scoring.py
     └─ rerank top-N                                      backends/reranker.py
     └─ context packing (token budget) + reinforce()      core/recall.py / core/store.py
```

Backends are selected by `get_embedder()` / `get_vector_index()` / `get_reranker()` and
injected through `MemoryEngine` — never imported directly inside `core/` (see §3.1).

**Grounded recall** (`MemoryEngine.grounded_recall()` → `core/grounded.py`) wraps `recall()`:
it answers *strictly from* the retrieved memories with `[n]` citations, or **abstains** when the
absolute query↔memory support (max of semantic cosine and lexical Jaccard, recomputed here — the
recall score is per-query-normalised and can't gate a fixed threshold) is below
`GROUNDED_SUPPORT_FLOOR`. Offline and deterministic (extractive answer) by default; an optional
`LLM` (injected, never imported in `core/`) can synthesise prose under the same source/abstain
contract, degrading to the extractive answer on any error. The abstain gate is what makes
"grounded, not guessed" real — an off-topic query doesn't get the nearest-neighbour dressed up as
fact. Measured by `eval/grounded.py` (answerable→ground, off-topic→abstain).

The write path (`MemoryEngine.remember_with_resolution()`) mirrors this: embed → find
same-scope neighbors via the vector index → `core/resolve.py::resolve()` decides
ADD / NOOP (reinforce, don't duplicate) / INVALIDATE (close old validity, insert new) from
**two deterministic signals** — token-overlap on the text itself, plus the embedding cosine
already computed at write time (catches paraphrased restatements/contradictions,
`PARAPHRASE_EMBED_SIM`) — no LLM call on untrusted input. An INVALIDATE also records
`metadata.supersedes` on the new record so the chain is queryable (why/timeline/Inspector).
After the decision, **memory evolution** (`MemoryEngine._evolve`, A-MEM-style) auto-links the
new memory to its closest live neighbors (bounded, idempotent, audited) and gives them a small
reinforcement touch. `remember()` is a thin wrapper that returns just the resulting id; use
`remember_with_resolution()` when you need the decision detail. `MemoryEngine.ingest()` is the
extract-then-remember path: with an `Extractor` configured (`ENGRAPHIS_EXTRACTOR=llm`) raw text
is distilled into discrete facts first; the offline default is passthrough.

---

## 3. Non-negotiable conventions (load-bearing)

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

## 5. Data model cheat-sheet (`core/interfaces.py`, `core/schema.py` — `SCHEMA_VERSION = 4`)

- **Scope hierarchy:** `workspace → repo → session → memory`. Scopes: `session|repo|workspace|user`.
- **Bi-temporal validity on every record:** world-time `valid_from/valid_to` +
  system-time `ingested_at/expired_at`. Reads hide facts outside their validity window
  unless `include_invalid=True` or an `as_of` anchor is given.
- **IDs:** ULID, time-sortable, **typed prefixes** (`ws_`, `repo_`, `ses_`, `mem_`, `ent_`,
  `edg_`, `sym_`, `evt_`, `job_`, `aud_`, `dev_`, `rcpt_`) — `core/ids.py`.
  Lexicographic sort == chronological.
- **Tables:** `workspaces`, `repos`, `sessions`, `memories`, `mem_vectors`,
  `mem_fts` (FTS5 + plain-table fallback), `entities`, `edges` (bi-temporal), `mem_links`,
  `symbols`, `code_edges`, `code_files`, `code_memory_links`, `operation_receipts`,
  `events`, `audit`, `schema_migrations`.
- **Vectors are stored L2-normalized** so cosine similarity == dot product.

---

## 6. Gotchas

- **Offline by default in core:** `MemoryEngine.create()` uses a deterministic hashing
  embedder + NumPy index, so tests need no model download or network. Real models load only
  when you pass `embed_model=...` / `vector_backend="sqlite-vec"`.
- **First full-stack run downloads `all-MiniLM-L6-v2` (~80 MB)** for the ST embedder.
- **FTS5 may be missing** on some SQLite builds → `Store` auto-falls back to `LIKE`
  (`self.has_fts5`). Don't assume BM25 is available.
- **Secrets & data are git-ignored:** `.env`, `engraphis_v1.db`, `*.db-wal`, `*.db-shm`. Never
  commit, print, or paste their contents.
- **Git history is authoritative:** use `git log` / `git blame` for implementation history and
  `CHANGELOG.md` for release-level summaries. Keep commits logical and descriptive.
- **Synced-folder flakiness:** if the repo sits on OneDrive (or any host-to-sandbox mount), a
  transient `SyntaxError`, `AttributeError` for a method you just added, or a shell command
  reading back fewer lines than you just wrote is mid-sync, not your code. A single re-run is
  sometimes not enough — if a file's content looks stale from the shell after an edit, the
  reliable fix is to rewrite that file's content directly from the shell (e.g. a heredoc) and
  re-verify with `wc -l`/`grep` before trusting a test run against it; clearing `__pycache__`
  alone does not fix this (the staleness is in the source, not in cached bytecode).

---

## 7. Source-of-truth docs

- **`README.md`** — installation, product surfaces, configuration, and public API usage.
- **`CHANGELOG.md`** — shipped capability and release history. Keep phase/status ledgers out of
  this operating manual.
- **`docs/SYNC.md`** — cloud sync (Pro): architecture, the convergent merge, CLI usage, the
  untrusted-bundle security model, and positioning vs. file-syncers like Obsidian Sync.
- **`AGENTS.md`** (this file) + **`CLAUDE.md`** — how to work in the repo.
- **`skills/engraphis-memory/`** — portable Agent Skill (SKILL.md + `references/`) that teaches any
  MCP-capable agent the *memory discipline* (when to remember/recall, scoping, tool selection).
  Shipped as a Claude Code plugin via `.claude-plugin/` (`marketplace.json` + `plugin.json`). It
  documents the tool surface in `engraphis/mcp_server.py`, so keep tool names/params in sync when
  you change that file — this is a docs-drift surface like `README.md`.

> When code and docs disagree, the code wins — then fix the doc in the same change.
