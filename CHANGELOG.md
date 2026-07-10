# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [0.1.0] — 2026-07-09

Initial public release. Self-hosted AI memory engine for agents — Ebbinghaus
decay, interaction-aware recall, bi-temporal facts, and background consolidation.
Local-first; you bring the LLM.

### Added
- Full MCP server with 18 tools (`engraphis-mcp` entry point).
- `MemoryService` transport-agnostic facade over the v2 engine.
- Memory Inspector product UI (`engraphis-inspector`, port 8710).
- Dashboard rebuilt on v2 engine with recall, governance, consolidate, analytics.
- Team mode with PBKDF2 logins, viewer/member/admin roles, seat limits.
- Grounded recall with cited answers and abstain gate.
- Sleep-time consolidation with compaction accounting.
- Personalized PageRank graph arm (HippoRAG-style).
- Offline Ed25519-signed license keys (no phone-home).
- Pro analytics dashboard and compliance export.
- Code-symbol graph via tree-sitter or regex fallback.
- Docker + docker-compose deployment.
- 300+ tests, eval harness, ablation suite.

### Security
- 100% parameterized SQL, constant-time auth, PBKDF2-HMAC-SHA256.
- Input sanitization, workspace isolation, CORS loopback defaults.
- `Secure` cookie flag when served over HTTPS.
- Rate-limit dict pruning to prevent memory leaks.
- Path traversal guard on folder import.
- Startup warning when API is exposed without authentication.
- LLM error messages sanitized (no internal details leaked to clients).

### Fixed
- `INSERT OR REPLACE` no longer cascade-deletes vectors on memory update.
- `_graph_arm_1hop` correctly resolves entity names to node IDs.
- `audit()` now commits immediately (no lost entries on crash).
- `_row_to_edge` preserves `workspace_id` and `repo_id` from DB rows.
- LLM client handles malformed API responses without `KeyError`.
- `why()`/`timeline()`/`proactive()` return 400 when no workspace exists.
- Entity extraction stopwords expanded to reduce noisy graph nodes.

## [Unreleased] — dashboard-v2 rebuild + pre-launch cleanup pass

### Changed
- **Pricing copy across README, `docs/landing-page.md`, `docs/vs-competitors.md`, and both
  dashboard UIs now says Pro/Team are "coming soon," not for sale.** There is no live checkout yet, so language implying a
  real purchase flow ("Get Engraphis Pro →", "see pricing →") was misleading. No functional
  change — the license gating and its tests are unaffected.

### Added
- **Dashboard rebuilt on the v2 engine** (`engraphis/dashboard_app.py`, `routes/v2_api.py`,
  `routes/v2_team.py`, `scripts/start_dashboard.py`): the v1 dashboard's exact look/UX, now
  served over the v2 `MemoryService` where real data actually lives (the v1 dashboard's
  `vaults`/`documents` tables don't exist in a v2 database). Recall/why/timeline/proactive/audit,
  governance (pin/forget/correct), consolidate, analytics + compliance export (Pro-gated), and
  multi-user Team mode (PBKDF2 logins, viewer/member/admin roles, per-seat license limits) — all
  on one app, leaving the v1 server and Inspector untouched.

### Security
- **Team mode now actually gates the dashboard's data endpoints.** The initial dashboard-v2 build
  only wired auth onto `/api/auth/users*`; recall, memory detail, pin/forget/correct, consolidate,
  analytics, and the compliance export had no request-level auth check at all, so enabling
  `ENGRAPHIS_TEAM_MODE` didn't require a login to read or mutate memories — only to manage users.
  Fixed with the same `_auth_gate` middleware pattern the Inspector already uses: every `/api/*`
  route (other than the auth bootstrap endpoints) now requires a valid team session when team mode
  + a team license are active, falling back to the existing optional bearer token otherwise. Also
  fixed the team users/sessions table being created inside the main memory database instead of a
  sibling `*.users.db` file (contrary to `inspector/auth.py`'s own documented design), which meant
  password/session-token hashes were reachable through `/api/export` and ordinary DB backups. Wired
  `ENGRAPHIS_WORKSPACES` enforcement into the dashboard's service construction, which had silently
  been missing. New regression tests: `test_team_mode_gates_data_endpoints`,
  `test_team_users_db_is_separate_from_memory_db`.

### Changed
- Relocated `docs/{GO_TO_MARKET,LAUNCH_PLAN,MONETIZATION-FAST,SHIP-IT-PLAN,LAUNCH-POST}.md` and
  the root `HANDOFF-*.md` scratch notes into a new gitignored `internal/` folder ahead of public
  launch — kept on disk, no longer tracked/public. Cleaned up the resulting dangling file
  references left in code comments and remaining docs (`licensing.py`, `analytics.py`,
  `inspector/*`, `scripts/init.py`, `scripts/license_admin.py`, `AGENTS.md`,
  `RELEASE_READINESS.md`, `docs/RELEASE.md`, `docs/vs-competitors.md`) — the substance of every
  comment is unchanged, only the removed file's pointer is gone.

### Fixed
- Stripped NUL-byte corruption from `AGENTS.md`, `RELEASE_READINESS.md`, `docs/RELEASE.md`, and
  `docs/vs-competitors.md` introduced by a sandbox filesystem quirk while editing them this pass
  (stale cached file length padding a shrunk file with NULs instead of truncating it — the same
  class of issue AGENTS.md §7 already documents for `.py` files). No content was lost; confirmed
  byte-for-byte before committing the fix.
- Split a test that opened two sequential `TestClient` app lifespans in one pytest function
  (`test_analytics_and_export_gated_then_unlocked`) into two functions — that pattern reproducibly
  deadlocks under pytest in this environment (fastapi 0.139/starlette 1.3/anyio 4.14), independent
  of this repo's code (a bare two-FastAPI-`TestClient` repro hangs identically).

Full offline gate verified green with the `[test]` extras actually installed (not just the
numpy-only core floor): 303 passed, 2 skipped, `ruff` clean, `eval.harness` (sample + codemem) and
`eval.ablation` all at 1.0.

## [Unreleased] — commercial layer: license keys, Pro analytics/export, Team mode

### Added
- **Grounded recall — cited answers, or an explicit abstain** (`core/grounded.py`,
  `MemoryEngine.grounded_recall`, `MemoryService.grounded_recall`, `engraphis_recall_grounded`
  MCP tool — brings the surface to 18 tools). Unlike `recall` (which returns memories and leaves
  synthesis to the caller), this answers *strictly from* the retrieved memories with `[n]`
  citations and **abstains** ("insufficient evidence") when nothing in scope actually supports the
  query — so an off-topic question can't get the vector index's nearest, but irrelevant, neighbour
  dressed up as fact. The abstain verdict is a fixed threshold (`GROUNDED_SUPPORT_FLOOR = 0.25`) on
  an absolute query↔memory support signal (max of stopword-filtered semantic cosine and lexical
  Jaccard) recomputed independently of the per-query recall score. Offline and deterministic
  (extractive answer that never introduces an uncited claim); an optional injected `LLM` can
  synthesise prose under the same source/abstain contract, fenced against memory-poisoning
  injection and degrading to the extractive answer on any error. Citations include only the sources
  that individually clear the floor. Tests: 21 in `tests/test_grounded.py`; new
  `eval/grounded.py` scores the abstain gate (answerable→ground, off-topic→abstain: 10/10 on the
  fixture, locked into CI via a pytest assertion).
- **Consolidation compaction accounting** — every consolidation sweep now reports its payoff as
  a number: `report["compaction"]` gives the estimated context tokens before/after for the
  distill pass (`tokens_before/after/saved/reduction_pct`), the tokens freed by archiving decayed
  transients, and a combined `total_tokens_saved`; each digest/archive entry carries its own
  figures. Backed by a dependency-free `core.textutil.estimate_tokens` (~4 chars/token, offline —
  no `tiktoken`). Flows through `MemoryService.consolidate`, the `engraphis_consolidate` MCP tool,
  and the `scripts.consolidate` CLI.
- **Entity profiles (opt-in consolidation pass)** — `core.consolidate.consolidate_profiles`
  (`consolidate(..., profiles=True)`, `--profiles`, `min_mentions`) rolls every live memory that
  mentions a graph entity into one durable `semantic` "Profile: <name>" digest, linked `profiles`
  with provenance `source='profile_consolidation'`. Deterministic + offline (optional LLM
  summarizer), idempotent, audited, scoped, never a hard delete — the local-first analog of a
  per-subject knowledge profile that grows with use. Adds `Store.list_entities`.
- Tests: 7 new in `tests/test_consolidate.py` (compaction savings on a real cluster, archive
  freed-tokens, dry-run estimates without writing; profile creation/linkage/provenance, the
  `profiles=True` flag path, idempotency, `min_mentions`, dry-run).
- **Offline signed license keys** (`engraphis/licensing.py`): Ed25519-signed
  `ENGR1.<payload>.<sig>` keys verified pure-stdlib (RFC 8032 implementation, tested against
  the RFC's own vectors) — no phone-home, no license server, no new dependency. Vendor CLI:
  `python -m scripts.license_admin keygen|issue|verify` (private key lives in gitignored
  `.secrets/`). Free tier is the absence of a key, never an error; a bad/expired key degrades
  to free with the reason surfaced in the UI.
- **Pro: Analytics dashboard** — `/api/analytics` + an Analytics tab in the Inspector:
  weekly growth, retention distribution, decay forecast (what the consolidation sweep will
  archive in 7/30 days), resolver action mix, most-connected entities. Data layer is a pure
  tested function (`engraphis/analytics.py`); charts are dependency-free inline SVG.
- **Pro: Compliance export** — `/api/export` + an export button in the Audit tab: full
  bi-temporal workspace dump (live + superseded memories, sessions, audit trail) as
  downloadable JSON (`MemoryService.export_workspace`).
- **Team mode** (`ENGRAPHIS_TEAM_MODE=1` + a Team license): multi-user Inspector with
  PBKDF2 logins, hashed session cookies (HttpOnly, SameSite=Strict), first-run admin setup,
  seat limits from the signed key, and server-side roles — viewer (read) < member
  (+ governance) < admin (+ consolidate/users/license/export). Bearer token still works as a
  service account for scripts. Single-user setups are byte-for-byte unchanged.
- **Upgrade UX without nagging**: plan badge + license dialog (paste-to-activate via
  `/api/license/activate`), locked-feature teasers rendered only where a locked feature was
  explicitly opened, and a guided first-run empty state with a copyable MCP config snippet.
- Tests: 40+ new (RFC 8032 vectors, key tamper/expiry/seats, role matrix, login throttle,
  402 gating, analytics math) — all offline, `importorskip`-guarded like the rest.

- **Per-user audit attribution**: in team mode, pin/forget/correct audit rows record the
  signed-in user's email as the actor (service + engine already supported `actor`; the
  Inspector now passes it) — the Team tier's audit trail answers *who*, not just what.
- **`engraphis-init`** — one command from `pip install` to a configured, agent-connected
  setup: writes `.env` with an **absolute** DB path (the silent default previously landed in
  the package directory — site-packages on pip installs), optional `--token`, prints exact
  Claude Code / Cursor / Cline / Zed MCP snippets with the DB path pinned via `env`, and
  `--check` is a doctor (install, extras, DB writability, license state).
- Inspector polish: relative timestamps in the audit trail (exact time on hover), `/`
  focuses the active tab's search box, Dockerfile documents the Inspector port (8710).

### Docs
- `docs/LAUNCH_PLAN.md` — monetization architecture, tier table, payments plan (merchant of
  record), UI/UX roadmap, launch checklist. `SECURITY.md` §6 documents the team-auth design.
- README: corrected the MCP tool count (17), added `engraphis-init` to the quickstart.
- **Positioning pass.** README now leads outcome-first ("your agents
  forget… Engraphis fixes that") with the mechanism as proof, names the ingest pipeline ("raw in,
  structured memory out"), and reframes provenance as an anti-hallucination guarantee ("grounded,
  not guessed"). `docs/landing-page.md` mirrors those and adds a value-anchored pricing frame.
- **`BENCHMARKS.md`** — honest, two-tier benchmark doc. §1 is the reproducible offline regression
  harness (deterministic embedder: `codemem` recall@1 = 0.962, saturates at k≥2; ablation arms tie
  on the small fixtures) framed explicitly as a *correctness floor, not a competitive score*; §2
  documents the LoCoMo/LongMemEval commands with a blank results table to fill on a machine with a
  real embedder (no fabricated numbers).
- **`docs/vs-competitors.md`** — page-ready "Engraphis vs mem0 / Zep / Letta" comparisons plus a
  combined matrix, with fair "pick them if…" callouts. Competitor prices carry the `GO_TO_MARKET.md`
  as-of-2026-06-30 caveat and a re-verify-before-publishing warning.

## [Unreleased] — v1 dashboard drill-down + polish pass

### Fixed
- **Knowledge-graph clicks now actually open things.** Clicking a node used to render the
  memory into the (hidden) Memories view, so nothing visibly happened. A node now opens a
  slide-over panel listing *every* memory behind that entity (new endpoint
  `GET /memory/entity/{name}/memories`: event-linked first, content mentions second, with
  previews + retention); clicking a memory opens a full reader modal in place. Edge clicks
  open a relation panel with both endpoints one click away.
- Command palette could fire the wrong action when the list was filtered (index mismatch).

### Added
- **Universal memory reader modal** — recall results, Overview activity, Timeline events,
  Health stale list, palette hits, and graph memories all click through to the same reader
  (markdown-rendered via DOMPurify, retention/stability pills, Reinforce / Open-in-Memories /
  Mark-read / Delete actions). Timeline events without a document open the entity panel.
- Graph controls: entity search (focus + open), physics Freeze/Unfreeze, double-click zoom.
- Design refresh layered over the existing tokens: radial-gradient background, glass sidebar,
  gradient brand/stat text, glow hovers, view transitions, keyboard/focus-visible states,
  `prefers-reduced-motion` support, ARIA roles on nav/panels/modal.
- Chat: assistant replies render as sanitized markdown instead of plain text.

## [Unreleased] — competitive-parity pass (memory quality + product surface)

### Added
- **Paraphrase-aware conflict resolution** (`core/resolve.py`): embedding cosine as a second
  deterministic signal (`PARAPHRASE_EMBED_SIM=0.90`) alongside token-Jaccard — reworded
  restatements/contradictions now supersede instead of duplicating. Op is INVALIDATE, never
  NOOP, so no new fact can be silently discarded. Closes the known "misses paraphrased
  conflicts" ceiling partway; the LLM judge remains an optional upgrade path.
- **Memory evolution on write** (A-MEM-style, `MemoryEngine._evolve`): every ADD/INVALIDATE
  auto-links the new memory to up to 3 closest live neighbors (`related`), reinforces them
  lightly, and audits the action (`evolve`). Bounded, idempotent (`Store.add_link` now dedupes
  per pair+relation), disable via `MemoryEngine(auto_evolve=False)`.
- **Supersession pointers**: INVALIDATE now records `metadata.supersedes=[old_id]` on the new
  record, making the full chain queryable (not audit-only) — powers the Inspector chain view.
- **Fact extraction interface** (`Extractor` protocol in `core/interfaces.py`;
  `backends/extractor.py`): `PassthroughExtractor` (offline default) and `LLMExtractor`
  (multi-provider via the existing v1 LLM client; defensive JSON parsing; degrades to
  passthrough — ingest never loses a write). New `MemoryEngine.ingest()`,
  `MemoryService.ingest()`, MCP tool `engraphis_ingest`, config `ENGRAPHIS_EXTRACTOR`.
- **Personalized PageRank graph arm** (`core/graphrank.py`, pure NumPy): HippoRAG-style
  seeded random walk over entity↔entity edges (bi-temporal), memory↔entity mentions, and
  memory↔memory links. Default (`RecallEngine(graph_mode="ppr")`); `"1hop"` retained.
  `eval.ablation` now reports vector-only / hybrid-1hop / hybrid-ppr.
- **Sleep-time consolidation** (Phase 4 first cut, `core/consolidate.py`): recurring episodic
  clusters (token-Jaccard, union-find) → one semantic digest linked `consolidates` to sources;
  fully-decayed unpinned transients archived via bi-temporal close (audited, recoverable).
  Deterministic offline; optional LLM digest text. Runners: `scripts/consolidate.py`
  (cron/Task Scheduler), MCP tool `engraphis_consolidate` (dry-run default), Inspector button.
- **Memory Inspector** (`engraphis/inspector/` + `scripts/inspector.py`, :8710): v2 product UI
  over `MemoryService` — search, why/history, timeline, proactive "start here", health +
  consolidation, audit trail, and the **supersession-chain view with word-level diffs**.
  Accessible from day one (ARIA tablist, keyboard nav, aria-live regions, text+color status),
  single-file no-build frontend, `textContent`-only rendering (no stored-content innerHTML),
  optional bearer auth. New console scripts `engraphis-inspector`, `engraphis-consolidate`.
- **External benchmark adapter** (`eval/external.py`): LoCoMo + LongMemEval loaders normalized
  into the existing harness (same engine write/recall path). Measures retrieval
  (evidence recall@k) honestly — not judge-scored QA — and says so in the report.
  `--offline` plumbing check; real numbers need torch + dataset on the operator's machine.
- **MCP server: 15 → 17 tools** (`engraphis_ingest`, `engraphis_consolidate`); skill docs
  (`skills/engraphis-memory/`) updated in the same change.
- **stats() now reports live counts** plus `total_rows` (live + preserved history).

### Fixed
- Repaired two working-tree files truncated by the synced-drive bug (`routes/memory.py` tail,
  `models.py` tail) — restored from HEAD + preserved the concurrent session's `/memory/prune`
  endpoint and `PruneRequest` model.

## [Unreleased] — v1-hardening pass

### Security
- **v1 REST input hardening** (SECURITY.md): request models now strip control characters and
  cap length on stored/name text fields (parity with v2 `service.py`), and the file-upload path
  caps body size — oversized or control-character-laden payloads to
  `/memory/insert`/`/documents`/`/documents/upload` are rejected or defanged, not stored as-is.
- **Optional in-process rate limiting** for the v1 REST API (`ENGRAPHIS_RATE_LIMIT` /
  `ENGRAPHIS_RATE_WINDOW`): a per-client-IP sliding window returning 429 + `Retry-After`, off by
  default. Front multi-process/distributed deployments with a reverse proxy.

### Changed
- `config.Settings` gains `rate_limit`/`rate_window`; adds `tests/test_v1_hardening.py`.

## [Unreleased] — read-isolation pass

### Security
- **Cross-tenant read isolation is now enforceable server-side** (`ENGRAPHIS_WORKSPACES`).
  `recall`/`why`/`timeline`/`recall_proactive` previously took the caller's asserted `workspace`
  at face value, so any MCP client that knew or guessed a workspace name could read it
  (SECURITY.md §3, handoff §4.2). `MemoryService` can now be *bound* to a comma-separated
  workspace allow-list: every read and write whose workspace is outside the list is refused at a
  single choke point (`_clean_ws` -> `_authorize_workspace`) before it reaches the store, and
  workspace-less global `recall`/`stats` are refused outright. An empty binding leaves the
  single-tenant local behavior unchanged, so existing installs are unaffected. Covered by
  `tests/test_workspace_isolation.py` (8 tests) plus a standalone cross-tenant read repro.

### Changed
- `config.Settings` gains `allowed_workspaces` (from `ENGRAPHIS_WORKSPACES`);
  `MemoryService`/`MemoryService.create` accept `allowed_workspaces`, wired from the MCP server.

## [Unreleased] — competitive-feature pass

Closes the gap between "secure, well-tested MVP" and the differentiators MASTER_PLAN.md
claims against mem0/Zep/Letta: self-maintaining facts, bi-temporal "why"/history, and a
code-aware symbol graph. All additions are local-first (no LLM or network dependency).

### Added
- **Deterministic write-path conflict resolution** (`core/resolve.py`,
  `MemoryEngine.remember_with_resolution`): every `remember()` call now checks same-scope
  neighbors via the vector index and decides ADD / NOOP (reinforce a near-duplicate instead
  of cloning it) / INVALIDATE (close the superseded fact, never delete it) from token-overlap
  on the text — no LLM call, matching the local-first/numpy-only core constraint.
  `remember()`'s return signature is unchanged (still a plain id); `remember_with_resolution()`
  and the service/MCP layer surface the decision (`op`, `superseded`, `resolution`).
- **Bi-temporal tools**: `engraphis_why` (the live answer plus what it superseded) and
  `engraphis_timeline` (full chronological history of a fact, including invalidated versions) —
  the concrete payoff of the bi-temporal schema that wasn't reachable via any tool before.
- **Governance tools**: `engraphis_forget` (bi-temporal close, audited, never a hard delete),
  `engraphis_pin` (exempt from future decay/pruning), `engraphis_correct` (replace content
  without losing history) — previously there was no way for an agent or user to fix or remove
  a bad memory once written. All three (plus `engraphis_link`) require `workspace`/`repo` and
  verify the target memory actually belongs to that scope before mutating it, so a caller can't
  act on a memory it only knows the id of from a different workspace's output (caught in this
  pass's own security review — see `SECURITY.md` §1/§3).
- **Proactive recall + session handoff**: `engraphis_recall_proactive` ("what should I know
  right now" with no query) and a real fix for cross-session continuity — `start_session` now
  returns the repo's previous *ended* session's summary/open-threads as `bootstrap`, instead of
  the open_threads field existing in the schema but never being surfaced by any tool.
- **`engraphis_record_event` / `engraphis_link`**: lightweight episodic logging and explicit
  A-MEM-style memory-to-memory linking (`Store.add_link`/`get_links`, the `mem_links` table —
  previously defined in the schema and unused).
- **Code-symbol graph** (`backends/codegraph.py`, `MemoryEngine.index_repo`/`search_code`,
  `engraphis_index_repo`/`engraphis_search_code`): parses a repo into function/class/method
  definitions plus best-effort calls/imports edges. AST via `tree-sitter`
  (`pip install "engraphis[code]"`) when installed; a dependency-free regex indexer otherwise,
  so the core's numpy-only guarantee holds. Populates the `symbols`/`code_edges` tables that
  existed in the schema since Phase 0 but were never written to.
- Eval: 2 new `codemem.jsonl` cases specifically exercise conflict resolution end-to-end
  (a superseded fact must stop being "current", not just remain retrievable); `codemem.jsonl`
  is now part of the CI gate, not just a documented manual command.
- Tests: `test_resolve.py`, `test_codegraph.py`, `test_ingest_entities.py`, plus substantial
  additions to `test_engine.py`/`test_service.py`/`test_mcp_server.py`/`test_core_store.py` for
  all of the above (127 tests total, up from 55; 0 skipped when the `server`/`mcp` extras are
  installed).

### Fixed
- **Knowledge Graph: clicking a node could open the wrong document, or none** (v1 dashboard,
  `engraphis/static/index.html` + `engines/ingest.py`). Root cause: entity extraction built its
  regex input as `f"{title}\n\n{content}"`, and the capitalized-word pattern matched *across*
  that boundary — e.g. title "Meeting Notes" + content "Alice Johnson met..." produced one
  garbled entity "Meeting Notes\n\nAlice Johnson" instead of two clean ones. The same real
  person mentioned cleanly in a second document became a *different*, separately-named node, so
  each fragment's `documents` list only ever held part of the truth — the dashboard's click
  handler (`network.on('click', ...)` → `showMem`) was reading correct data, but the graph
  handed it fragmented entities to click on. Fixed by extracting entities from `title` and
  `content` as independent regex passes and merging by name (`_extract_entities_from_doc`), plus
  tightening the capitalized-word pattern to keep hyphenated words intact (`Follow-up` no longer
  sheds an orphan `Follow` node). Regression-tested end-to-end in `test_ingest_entities.py`
  (title/content no longer bridge, hyphenated titles stay whole, the same entity across two
  documents resolves to one node listing both, cross-namespace entities stay isolated).

### Changed
- **`eval/harness.py` now exercises the real pipeline**: it previously called the vector index
  directly, bypassing `RecallEngine`'s scoring/RRF/rerank *and* the write-path resolver — so the
  CI gate measured plumbing, not the shipped recall quality. It now ingests and queries through
  `MemoryEngine`, same as production.
- MCP tool count: 5 → 15. `engraphis_remember` gained an optional `dedupe` parameter (default
  on) to opt out of conflict resolution for cases where repeats are meaningful (e.g. recurring
  episodic log entries). `engraphis_end_session` gained `open_threads`.
- Docs: `AGENTS.md` §6 rewritten (previously said "no MCP server exists in the tree today",
  which the prior pass had already made false); `docs/IMPLEMENTATION.md`, `README.md`,
  `SECURITY.md`, `CLAUDE.md` updated to match.

### Security
- See `SECURITY.md` §5 (new): `index_repo` reads local files at an agent-supplied path — same
  trust boundary as any other local tool, documented explicitly. Governance tools give users an
  audited way to correct the memory-poisoning blast radius after the fact, not just reduce it
  on write.
- **Stored XSS in the v1 dashboard**: memory content rendered as markdown via `marked` (v12,
  which does not sanitize embedded HTML) was inserted into `innerHTML` unsanitized at
  three sites — viewing a memory and both live editor previews. A memory containing e.g.
  `<img src=x onerror="...">` would run arbitrary JavaScript the moment a human viewed it in the
  dashboard, independent of and in addition to the memory-poisoning threat model above (that
  content is explicitly untrusted — MASTER_PLAN.md §16 — is exactly why this mattered). Fixed by
  piping every markdown render through `DOMPurify.sanitize()` (new `renderMd()` helper); verified
  the `onerror` attribute is stripped while ordinary markdown renders unchanged. See
  `SECURITY.md` §1.

## [Unreleased] — release-readiness pass

### Added
- **MCP server** (`engraphis-mcp`, `engraphis.mcp_server`) exposing `engraphis_remember`,
  `engraphis_recall`, `engraphis_start_session`, `engraphis_end_session`, and `engraphis_stats`
  so Claude Code, Cursor, Cline, Zed, and Windsurf can use Engraphis as agent memory.
- **`MemoryService`** (`engraphis.service`) — a transport-agnostic, fully validated facade over
  the v2 engine, usable as a plain Python library (no MCP dependency, offline-capable).
- **Input validation & sanitization** on the write path (size caps, control-character stripping,
  strict enums, metadata limits, provenance) as a memory-poisoning defense.
- **Optional bearer-token auth** (`ENGRAPHIS_API_TOKEN`) on the REST API with constant-time
  comparison, plus a configurable CORS allow-list (`ENGRAPHIS_CORS_ORIGINS`).
- `LICENSE` (Apache-2.0), `NOTICE`, `SECURITY.md` (threat model), and this `CHANGELOG.md`.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore` for one-command self-hosting.
- Larger offline eval suite (`eval/datasets/codemem.jsonl`, 24 questions) for the coding-agent
  memory wedge.
- New tests: `test_service.py`, `test_mcp_server.py`, `test_app_auth.py` (55 tests total).

### Changed
- **Rebranded to a clean, independent Engraphis identity**: removed third-party SDK-compat
  framing, renamed the default database to `engraphis.db`, and updated docs/positioning.
- License moved to **Apache-2.0** (from MIT) for clearer patent/trademark posture.
- `pyproject.toml` restructured for **open-core**: dependency-light core (`numpy` only) with
  `server` / `mcp` / `all` extras and an `engraphis-mcp` entry point.
- Tightened CORS defaults to loopback (no wildcard-with-credentials).
- README rewritten around the MCP wedge and self-hosted install paths.

### Fixed
- Resolved all `ruff` lint findings; the offline gate (pytest + eval + ablation + ruff) is green.

### Security
- See `SECURITY.md`. Not yet mitigated: encryption-at-rest, built-in rate limiting, and
  per-token tenant authorization (run one instance per trust boundary for hard isolation).
