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

| | **v2 — the target (build here)** | **v1 — legacy reference server (running server)** |
|---|---|---|
| Status | The v2 target design. Phases 0–1 done; parts of 2/3/5 done (see §6). | Working reference implementation; flat namespaces. |
| Model | Scoped + bi-temporal + typed; interface-driven. | Single flat `namespace` string per memory. |
| Code | `engraphis/core/`, `engraphis/backends/`, `eval/`, `tests/`, `scripts/migrate_to_v2.py` | `engraphis/app.py`, `config.py`, `models.py`, `routes/`, `stores/`, `engines/`, `llm/`, `static/` |
| Data | new v2 schema (`SCHEMA_VERSION = 2`) | `engraphis_v1.db` |
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

# ── v2 Memory Inspector (product UI over MemoryService; same layer as the MCP server) ──
python -m scripts.inspector          # http://127.0.0.1:8710 (auth: ENGRAPHIS_API_TOKEN)

# ── Onboarding (writes .env with an absolute DB path; doctor mode verifies install) ──
engraphis-init                   # or: python -m scripts.init
engraphis-init --check

# ── Commercial layer (gates live ONLY in inspector/app.py) ──
python -m scripts.license_admin keygen                 # vendor keypair → .secrets/ (gitignored)
python -m scripts.license_admin issue --email a@b.co --plan team --seats 5 --days 365
ENGRAPHIS_LICENSE_KEY=ENGR1...   # or ~/.engraphis/license.key; free tier = no key
# Team mode is ON by default (multi-user Inspector). Set ENGRAPHIS_TEAM_MODE=0 to disable.
# A 'team' license is required to add seats beyond the first admin.

# ── Sleep-time consolidation (schedulable local job; also an MCP tool) ────────
python -m scripts.consolidate --db engraphis.db --workspace acme --dry-run

# ── Cloud sync (Pro; schedulable job over a shared folder OR the managed relay — see docs/SYNC.md) ──
python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis --dry-run
python -m scripts.sync --db engraphis.db --workspace acme --relay https://sync.engraphis.app  # or bare --relay + ENGRAPHIS_RELAY_URL

# ── Run the v1 server (needs the full install) ───────────────────────────────
python -m scripts.start_server      # http://127.0.0.1:8700  (dashboard at /, OpenAPI at /docs)
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

## 6. Status — what's real vs planned

- **Done — Phase 0:** interface contracts, scoped + bi-temporal schema, v1→v2 migration, eval
  harness + CI.
- **Done — Phase 1:** hybrid recall, six-term score, RRF, rerank; SentenceTransformer embedder +
  `sqlite-vec` index, each with an offline fallback.
- **Done — Phase 2:** deterministic (no-LLM) write-path conflict resolution
  (`core/resolve.py`, ADD/NOOP/INVALIDATE, two signals: token-Jaccard + embedding cosine — see
  §2); **LLM-based fact extraction** behind the `Extractor` protocol
  (`backends/extractor.py`, offline default = passthrough, `ENGRAPHIS_EXTRACTOR=llm` to
  enable, `MemoryEngine.ingest()` / `engraphis_ingest`); **Personalized PageRank** graph arm
  (`core/graphrank.py`, default; `graph_mode="1hop"` retained for ablation); **A-MEM-style
  evolution** (`MemoryEngine._evolve`: new writes auto-link to related live neighbors and
  reinforce them — bounded, idempotent, audited).
- **Done — partial Phase 3:** **MCP server exists** (`engraphis/mcp_server.py`, 18 tools:
  write/read (incl. **grounded recall** — `engraphis_recall_grounded`: cited answer or abstain)/
  governance/code/session — do not assume only `remember`/`recall` exist, check the
  tool list) and a **code-symbol graph** (`backends/codegraph.py`, tree-sitter with a
  dependency-free regex fallback; `MemoryEngine.index_repo()` / `engraphis_search_code`).
  Languages: Python, JavaScript, TypeScript, C#, C, C++ (C#/C/C++ are regex-level today —
  a `CompositeSymbolIndexer` routes them to the regex backend even when tree-sitter is
  installed, so no untested AST grammar maps ship; AST maps can move them to the primary
  later with no caller change). An unknown `languages=` filter is rejected with an
  actionable error instead of silently indexing nothing. Traversal prunes build/dep dirs
  *during* the walk and honours a root `.engraphisignore` (gitignore-style; hardcoded
  default excludes are non-negotiable — an untrusted repo can't `!`-re-expose them);
  symlinked files are not followed out of root. Call-graph edges are name-based, not
  type-resolved (best-effort, documented as such in `backends/codegraph.py`).
  **Not done:** incremental/file-watcher re-indexing (today `index_repo` is a full
  re-scan; idempotent per file, not incremental), git-as-world-time signal.
- **Done — partial Phase 5:** input validation/sanitization, optional bearer auth, CORS
  allow-list, governance tools (`forget`/`pin`/`correct`, audited, never a hard delete),
  Apache-2.0 licensing/packaging. **Not done:** encryption at rest, built-in rate limiting,
  per-token tenant authorization — see `SECURITY.md`.
- **Done — manual merge (N→1 governance op):** `MemoryEngine.merge()` /
  `MemoryService.merge()` combine several selected memories into one — the multi-input
  generalization of `correct`. Sources are bi-temporally closed (retired into history,
  never hard-deleted), the new memory records `supersedes` on every source (so the
  supersession chain renders — `service._chain_for` now walks *all* predecessors, not a
  single line) plus a `merges` link back to each. Safety-inherits the strictest of its
  sources: `trusted:false` if any source is untrusted (no laundering) and the highest
  `sensitivity`; pinned if any source was pinned. Audited on both sides with a
  token-compaction number. Exposed on the dashboard (`POST /api/merge`, multi-select +
  merge modal in the Memories tab) over the shared `MemoryService`; not yet an MCP tool.
  Distinct from `consolidate`, which is automatic, episodic-only, and *non-destructive*
  (sources stay live). Tests: `tests/test_merge.py`.
- **Done — Phase 4 (first shipping cut):** the consolidation loop
  (`core/consolidate.py` + `scripts/consolidate.py` + `engraphis_consolidate` MCP tool +
  Inspector button): recurring episodics → semantic digests (linked `consolidates`, audited),
  decayed transients archived bi-temporally; deterministic offline, optional LLM summarizer.
  Every sweep reports **compaction** — estimated context tokens before/after
  (`textutil.estimate_tokens`, ~4 chars/token, offline) — under `report["compaction"]`, so the
  payoff is a number (§3.7). Opt-in **entity profiles** (`consolidate_profiles`, `profiles=True`,
  `--profiles`): roll every live memory mentioning an entity (`store.list_entities` + name match)
  into one durable `semantic` profile digest, linked `profiles`, provenance
  `source='profile_consolidation'`, idempotent + audited — the local-first analog of a
  per-subject knowledge profile. Framed local-first: a user-schedulable job, not a cloud service.
  **Not done:** scope promotion; procedural distillation.
- **New — v2 Memory Inspector** (`engraphis/inspector/`, `python -m scripts.inspector`,
  :8710): product UI over `MemoryService` (same layer as the MCP server, so UI and tools
  can't drift). Flagship screen: the supersession chain with word-level diffs — rendering
  `resolve()`'s decision history. Accessible (ARIA tabs/labels, keyboard nav, text+color
  status), no build step, content rendered via textContent only. Optional bearer auth
  (`ENGRAPHIS_API_TOKEN`); multi-user login is the remaining Pro gate.
- **Done — Cloud sync (Pro, first cut):** convergent multi-device / team sync over any
  shared folder. `core/sync.py::SyncEngine` is a state-based CRDT merge over memory rows
  (bi-temporal, deterministic, idempotent) that reuses the `resolve()`/validity machinery —
  union by ULID, earliest-invalidation + max-reinforcement lattice, deterministic LWW with
  a content-hash tiebreak; scope reconciled *by name* on apply. `SyncTransport` interface
  (`core/interfaces.py`) + two backends: `FolderTransport` (`backends/sync_folder.py`, works
  over Dropbox/iCloud/Syncthing/git) and the managed `RelayTransport`
  (`backends/sync_relay.py`) against the license-gated server (`inspector/sync_relay.py`,
  mounted by `inspector/cloud_mount.py` on both `app.py` and `dashboard_app.py`; Team seat
  enforcement is server-side). `get_transport("folder"|"relay", …)` selects between them;
  gated CLI `python -m scripts.sync --remote <dir>` **or** `--relay [<url>]`
  (`require_feature("sync")` lives in the script — `core/` stays license-free). The
  untrusted-bundle apply path is validated/clamped and **scope-confined** (a bundle can't
  cross a workspace/repo boundary; `secret` memories aren't exported; provenance is stamped).
  See `docs/SYNC.md`.
  **Not done:** end-to-end encryption of relay bundles (client-side encrypt/decrypt; the
  relay already stores opaque bytes), HLC per-field clock, entity/edge graph sync,
  `engraphis_sync` MCP tool + Inspector "Devices" panel.
- **Not done at all:** Phase 6 — Rust hot path.
- The **v1 FastAPI server** is the legacy reference server and still runs; treat it as a
  compatibility/reference surface, not the place for new capability.

---

## 7. Gotchas

- **Offline by default in core:** `MemoryEngine.create()` uses a deterministic hashing
  embedder + NumPy index, so tests need no model download or network. Real models load only
  when you pass `embed_model=...` / `vector_backend="sqlite-vec"`.
- **First full-stack run downloads `all-MiniLM-L6-v2` (~80 MB)** for the ST embedder.
- **FTS5 may be missing** on some SQLite builds → `Store` auto-falls back to `LIKE`
  (`self.has_fts5`). Don't assume BM25 is available.
- **Secrets & data are git-ignored:** `.env`, `engraphis_v1.db`, `*.db-wal`, `*.db-shm`. Never
  commit, print, or paste their contents.
- **Real commit history exists as of 2026-07-08** — the "single Initial commit, everything else
  uncommitted" state described here through 2026-07-01 is resolved; work since has landed as
  logical, descriptive commits (see `git log`). `CHANGELOG.md` is still worth reading for a
  higher-level summary, but `git log`/`blame` are reliable again for recent work. Keep committing
  in logical chunks rather than letting changes pile up uncommitted.
- **Synced-folder flakiness:** if the repo sits on OneDrive (or any host-to-sandbox mount), a
  transient `SyntaxError`, `AttributeError` for a method you just added, or a shell command
  reading back fewer lines than you just wrote is mid-sync, not your code. A single re-run is
  sometimes not enough — if a file's content looks stale from the shell after an edit, the
  reliable fix is to rewrite that file's content directly from the shell (e.g. a heredoc) and
  re-verify with `wc -l`/`grep` before trusting a test run against it; clearing `__pycache__`
  alone does not fix this (the staleness is in the source, not in cached bytecode).

---

## 8. Source-of-truth docs

- **`README.md`** — v1 server usage + the REST API table.
- **`docs/SYNC.md`** — cloud sync (Pro): architecture, the convergent merge, CLI usage, the
  untrusted-bundle security model, and positioning vs. file-syncers like Obsidian Sync.
- **`AGENTS.md`** (this file) + **`CLAUDE.md`** — how to work in the repo.
- **`skills/engraphis-memory/`** — portable Agent Skill (SKILL.md + `references/`) that teaches any
  MCP-capable agent the *memory discipline* (when to remember/recall, scoping, tool selection).
  Shipped as a Claude Code plugin via `.claude-plugin/` (`marketplace.json` + `plugin.json`). It
  documents the tool surface in `engraphis/mcp_server.py`, so keep tool names/params in sync when
  you change that file — this is a docs-drift surface like `README.md`.

> When code and docs disagree, the code wins — then fix the doc in the same change.
