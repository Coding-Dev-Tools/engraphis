# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

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
  which does not sanitize embedded HTML by design) was inserted into `innerHTML` unsanitized at
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
