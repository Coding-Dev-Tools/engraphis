# AGENTS.md — Engraphis

Engraphis is a **local-first, open AI memory engine for agents** — Ebbinghaus decay,
interaction-aware reinforcement, bi-temporal facts, hybrid recall, and a native
`workspace → repo → session → memory` hierarchy. Python 3.9+ for the core (Python 3.10+
for the server/MCP stack), FastAPI, SQLite, and local embeddings; the external LLM is optional
and pluggable.

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
| Data | new v2 schema (`SCHEMA_VERSION = 16`) | `engraphis_v1.db` |
| Entry | `engraphis.MemoryEngine.create()` / `engraphis.create_memory_engine()` → `engraphis/factory.py` → `core/engine.py` | Internal reference only; never a public launcher |

**Rule:** build new capability on **v2** (`core/` + `backends/`) behind the interfaces.
Only touch the v1 server for compatibility fixes or to keep the reference running. When a
task is ambiguous, decide which side it belongs to *before* editing.

---

## 1. Commands

```bash
# ── Install ──────────────────────────────────────────────────────────────────
pip install numpy pytest            # v2 core + tests, fully offline (Python 3.9 floor job)
pip install -e ".[test]"            # full offline CI test/lint/typecheck dependencies
pip install -e ".[all,dev]"         # complete local stack: dashboard, MCP, embeddings, dev tools
# Config: process environment or owner-private ~/.engraphis/config.env; never a searched CWD .env

# ── Primary offline gate (no API key — KEEP THIS GREEN; mirrors CI's full-stack job) ──
ruff check .                                                        # pinned lint rules
python scripts/check_commercial_manifest.py                         # source/service boundary
python scripts/externalize_dashboard_assets.py                      # strict-CSP asset drift
python -m pytest tests/ -q                                          # full offline unit suite
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5   # retrieval eval gate
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5  # coding/conflict gate
python -m eval.ablation                                             # vector-only vs hybrid
python -m eval.reinforcement                                        # bounded retention trajectory
python -m eval.adversarial_memory_security                          # prompt/graph boundary
python -m eval.grounded                                             # grounded-abstain decision gate
python -m eval.code_arm                                             # coding-agent arm gate
pyright                                                             # core + backends typecheck

# ── External benchmarks (real numbers need torch + the dataset; see eval/external.py) ──
python -m eval.external --dataset locomo10.json --format locomo --k 10        # LoCoMo
python -m eval.external --dataset longmemeval_s.json --format longmemeval     # LongMemEval
python -m eval.external --dataset locomo10.json --format locomo --offline --limit 2  # plumbing check

# ── Unified dashboard + memory inspector ──
python -m scripts.start_dashboard    # http://127.0.0.1:8700
# Use this unified launcher; there is no separate Inspector service.

# ── Onboarding (writes owner-private ~/.engraphis/config.env; doctor verifies install) ──
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

# ── Compatibility server alias (v2, headless; needs the full install) ────────
python -m scripts.start_server      # same v2 app as engraphis-dashboard, without opening a browser
python -m scripts.cli recall "what do we know about X" -n vault    # CLI: ingest/recall/chat/thoughts/list

# ── v2 data migration (v1 flat namespaces → v2 scoped/bi-temporal) ───────────
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db --dry-run
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db

```

`requires-python >= 3.9` (ruff targets `py39`). CI tests the NumPy-only core on 3.9, the full
offline stack on 3.10–3.14, and Pyright on 3.11; dedicated jobs also exercise encryption and built
artifacts, and further jobs run the coverage gate (`--cov-fail-under=60`), repo hygiene, the Pi
extension, browser accessibility, and the Docker smoke. `.github/workflows/ci.yml` is
authoritative when the matrix changes.

---

## 2. The v2 recall pipeline (where the real work is)

`core/recall.py::RecallEngine.recall()` is the heart of the system. Flow:

```
query
  └─ SearchFilter (scope + valid_at/known_at anchors)    core/interfaces.py
     └─ optional QueryPlanner (off by default; original + at most 2 routes)
                                                       core/query_planner.py
     └─ 4 retrieval arms (executed deterministically, then fused):
        • vector   — VectorIndex.search (cosine)         backends/vector_*.py
        • lexical  — Store.fts_search (FTS5/BM25 + LIKE fallback)   core/store.py
        • graph    — Personalized PageRank over entities+links      core/recall.py + core/graphrank.py
                     (graph_mode="1hop" keeps the old expansion for ablation)
        • code     — symbols/files/calls with memory bridges          core/engine.py
     └─ priority-weighted query/arm RRF + six-term score  core/scoring.py
     └─ rerank top-N                                      backends/reranker.py
     └─ optional post-rerank memory-type maxima
     └─ context packing (token budget) + optional explicit reinforcement
                                                       core/recall.py / core/store.py
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
already computed at write time as joint evidence for strongly overlapping unkeyed text — no LLM
call on untrusted input. The dependency-free hashing embedder is lexical, so genuinely reworded
mutable facts need a stable `subject_key`/`claim_kind` (or explicit correction), not a cosine
threshold. An INVALIDATE also records
`metadata.supersedes` on the new record so the chain is queryable (why/timeline/Inspector).
After the decision, **memory evolution** (`MemoryEngine._evolve`, A-MEM-style) auto-links the
new memory to its closest live neighbors (bounded, idempotent, audited) and gives them a small
reinforcement touch. `remember()` is a thin wrapper that returns just the resulting id; use
`remember_with_resolution()` when you need the decision detail. `MemoryEngine.ingest()` is the
extract-then-remember path: with an `Extractor` configured (`ENGRAPHIS_EXTRACTOR=llm`) raw text
is distilled into discrete facts first; the offline default is passthrough.

---

## 3. Non-negotiable conventions (load-bearing)

1. **Interfaces before implementations.** Every module in `core/`, including `core/engine.py`,
   depends only on the Protocols in `core/interfaces.py` (`Embedder`, `VectorIndex`,
   `LexicalIndex`, `GraphStore`, `Reranker`, `LLM`) and injected collaborators. The sole outer
   composition root is `engraphis/factory.py`, which may import concrete backends and selects the
   dependency-light `IdentityReranker` default. `engraphis/__init__.py` registers that provider so
   the compatibility `MemoryEngine.create()` entry point delegates outward; new callers may use
   `engraphis.create_memory_engine()` directly. **Never import a concrete backend anywhere inside
   `core/`.** Swapping `sqlite-vec`→Qdrant, or a local embedder for an API, must be a *config
   change, not a refactor*.
2. **Forgetting lowers retrieval priority; it never hard-deletes.** Decay adjusts
   `stability`. Hard deletion is explicit, governed, and audited (`Store.audit`).
3. **Truth is temporal.** Resolve contradictions by **invalidation, not overwrite**:
   `Store.close_validity()` / `invalidate_edge()` set `valid_to`. Preserve history; support
   `as_of` time-travel reads.
4. **Everything is scoped.** Every memory carries a `Scope` + `workspace/repo/session`.
   Every read takes a `SearchFilter`. Scope promotion is an explicit operation.
5. **Memory is typed** (`working` / `episodic` / `semantic` / `procedural`), each with its
   own weight profile (`scoring.DEFAULT_WEIGHTS`) and lifecycle. The append-only event ledger is
   outside that type system: use `record_event` for raw occurrences and an episodic memory when
   the outcome must be recalled or consolidated.
6. **Provenance always.** Set `provenance` on memories and edges so "why is this known?"
   is answerable.
7. **Prove "better" with a number.** No retrieval/quality claim ships without an eval.
   Keep the CI gate green; extend `eval/` when you change ranking.
8. **Local-first & offline-capable.** The core must run with **only `numpy`** (deterministic
   embedder + NumPy index). Do not add hard dependencies to `core/`; gate heavy imports
   (sentence-transformers, sqlite-vec) behind the backend factories.

---

## 4. Core algorithms cheat-sheet (`core/scoring.py`, `core/store.py`)

- **Ordinary recall score** (`score_memory`):
  `score = w_r·retention + w_s·semantic + w_l·lexical + w_g·graph + w_i·importance − w_x·staleness`.
  Arm scores are **min-max normalized before fusion** so no arm dominates by raw scale.
  Recency (`c`) is used only by the separate queryless proactive agenda, avoiding a second
  age penalty alongside retention in ordinary recall. Default weights: `r1.0 s1.0 l0.5 g0.7 i0.6 c0.3 x0.8`, overridden per memory type.
- **Ebbinghaus retention:** `R(t) = exp(−Δt_days / S)`.
- **Reinforcement (spacing effect):** each event adds
  `(α·min(S, 1) + boost)·ln(1 + 1/access_count)`, `α = 0.3`, with a 100-day cap.
  The cumulative trajectory is logarithmic and bounded; this is `Store.reinforce()`.
- **Interaction boosts** (`scoring.INTERACTION_BOOST`): view/read 0.05 · recall 0.15 ·
  react 0.20 · engage 0.30 · reply 0.50 · create 1.00.
- **Reciprocal Rank Fusion:** `1 / (k + rank + 1)`, `k = 60`.

These are pure, unit-tested functions — change them only with a corresponding `tests/` +
`eval/` update.

---

## 5. Data model cheat-sheet (`core/interfaces.py`, `core/schema.py` — `SCHEMA_VERSION = 16`)

- **Scope hierarchy:** `workspace → repo → session → memory`. Scopes: `session|repo|workspace|user`.
- **Bi-temporal validity on every record:** world-time `valid_from/valid_to` +
  system-time `ingested_at/expired_at`. Reads hide facts outside their validity window
  unless `include_invalid=True` or an `as_of` anchor is given.
- **IDs:** ULID, time-sortable, **typed prefixes** (`ws_`, `repo_`, `ses_`, `mem_`, `ent_`,
  `edg_`, `sym_`, `evt_`, `job_`, `aud_`, `dev_`, `rcpt_`, `vlt_`, `src_`) — `core/ids.py`.
  Lexicographic sort == chronological.
- **Tables:** `workspaces`, `repos`, `sessions`, `memories`, `mem_vectors`, `embedding_state`,
  `mem_fts` (FTS5 + plain-table fallback), `entities`, `edges` (bi-temporal), `mem_links`,
  `memory_entities`, `symbols`, `code_edges`, `code_files`, `code_memory_links`,
  `operation_receipts`, `events`, `audit`, `memory_tombstones`, `source_vaults`,
  `source_imports`, `source_import_items`, `schema_migrations`.
- **Local document sources:** source collections are scope-bound, resumable manifests. Their
  paths, digests, and import state are provenance; source folders never create implicit memory
  scopes. `kind="documents"` is the source-neutral adapter, while `kind="obsidian"` retains
  the rich Markdown adapter and its existing lineage. Import jobs persist the optional session
  target, and schema checks keep source-job lineage and per-job items in that exact session.
- **Erasure markers contain no memory content.** `memory_tombstones.export_class` is strictly
  `never_export|remote_erasure`; only `remote_erasure` may cross a sync boundary.
- **Vectors are stored L2-normalized** so cosine similarity == dot product.

---

## 6. Gotchas

- **Offline by default at the public factory:** `engraphis.MemoryEngine.create()` and
  `engraphis.create_memory_engine()` select a deterministic hashing embedder + NumPy index, so
  tests need no model download or network. Pass `embed_model=...` to load a real embedding model;
  choose `vector_backend="sqlite-vec"` separately when you need native exact-KNN acceleration.
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

### PR delivery protocol for automated maintenance

When an agent is maintaining an open pull request, the matching PR branch or an isolated
worktree is the delivery boundary:

1. Implement every attributable, safe, scoped review or CI fix in that matching branch or
   worktree. After tests and lint pass, inspect `git status` and the complete diff, then commit
   and push every clean, attributable fix and PR worktree file with an ordinary non-force push.
   A verified fix must not be left only in a local checkout.
2. Keep unrelated user edits, ambiguous files, credentials, generated databases, logs, and
   secrets out of the PR. Preserve ambiguous work in its original checkout or a separate
   worktree and report the exact separation needed; never mix it into an otherwise clean PR.
3. After each push, recheck the remote CI/workflow results, logs, and current review threads.
   Continue with safe, attributable iterations until the PR is merge-ready, and report exact
   files, commits, tests, remaining review items, and blockers.
4. Never force-push, merge, deploy, publish, delete branches/files, change credentials, rerun
   workflows, post GitHub comments, resolve review threads, or send external messages without
   explicit approval. Merge remains prohibited unless the user explicitly approves it, even
   when checks are green.

---

## 7. Source-of-truth docs

- **`README.md`** — installation, product surfaces, configuration, and public API usage.
- **`CHANGELOG.md`** — shipped capability and release history. Keep phase/status ledgers out of
  this operating manual.
- **`docs/HOSTED_PLANS.md`** — concise pricing, plan contents, trial, and hosted-service boundary.
- **`docs/MCP_TOOLS.md`** — standalone inventory of the public MCP surface; keep it synchronized
  with `engraphis/mcp_server.py`.
- **`docs/SYNC.md`** — cloud sync (Pro): architecture, the convergent merge, CLI usage, and the
  untrusted-bundle security model.
- **`docs/DOCUMENT_IMPORT.md`** — universal local-document import, source safety, re-import,
  conflicts, format adapters, and dashboard/CLI flows. Keep it synchronized with the parser,
  importer, dashboard, and import-report schema.
- **`docs/OBSIDIAN_IMPORT.md`** — the rich Obsidian Markdown adapter (frontmatter, wikilinks,
  aliases, attachments) and its compatibility command. It supplements, rather than replaces,
  the universal document-import guide.
- **`AGENTS.md`** (this file) + **`CLAUDE.md`** — how to work in the repo.
- **`skills/engraphis-memory/`** — portable Agent Skill (SKILL.md + `references/`) that teaches any
  MCP-capable agent the *memory discipline* (when to remember/recall, scoping, tool selection).
  Shipped as a Claude Code plugin via `.claude-plugin/` (`marketplace.json` + `plugin.json`). It
  documents the tool surface in `engraphis/mcp_server.py`, so keep tool names/params in sync when
  you change that file — this is a docs-drift surface like `README.md`.

> When code and docs disagree, the code wins — then fix the doc in the same change.
