# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [Unreleased]

### Added
- **Agent Connect for hosted Team instances.** Members can mint SHA-256-hashed per-user
  bearer tokens in Settings and use the hosted v2 store through `POST /api/remember`,
  the existing read routes, token management under `/api/auth/token*`, and
  `GET /api/auth/connect-info`. Tokens retain the user's role and personal-folder scope;
  viewers are read-only and disabling a user invalidates their tokens immediately.
- **Authenticated MCP-over-HTTP at `/mcp`.** When the MCP extra is installed, the
  dashboard mounts the same 20 tools as the standalone server and injects its existing
  `MemoryService`, avoiding a second SQLite writer. The endpoint requires an active Team
  entitlement plus a member/admin cookie or bearer token; connect-info reports whether
  the mount is actually available.
- **One-click Railway hosting.** Added `railway.json`, the README deploy button, and
  `docs/HOSTING_RAILWAY.md` for persistent volumes, forwarded HTTPS headers, Team
  entitlement bootstrap, member invites, and HTTP/MCP agent connection.
- **Two new MCP context tools.** The MCP inventory grows from 18 to 20 with
  `engraphis_answer`, a compatibility alias for the existing grounded-recall contract,
  and `engraphis_proactive_context`, also available at `POST /api/proactive-context`.
  Proactive packets include bounded task/agent state, cited memories, suggested queries,
  and the previous session handoff. Optional LLM prose is accepted only when every claim
  carries a valid citation.
- **Structured LLM ingestion and consolidation.** `ENGRAPHIS_EXTRACTOR=llm_structured`
  validates typed facts, entities, relations, keywords, and confidence; that metadata is
  preserved through storage and automatically feeds the graph. Settings now includes a
  **Connect your LLM** card backed by `/api/llm/status` and `/api/llm/test`.
  Consolidation adds schema-validated facts and explicit source supersession across the
  service, REST, MCP, and CLI surfaces, with deterministic fallback on provider/schema
  failure.
- **Opt-in deterministic memory intelligence APIs.** Added conflict triage for duplicate,
  refinement, contradiction, and obsolete candidates, plus a serializable `UserModel`
  that learns interaction preferences and reranks recall results. These helpers do not
  mutate the store or alter default recall unless a caller invokes them.

### Changed
- **Team mode is opt-out by default.** `ENGRAPHIS_TEAM_MODE=0` (or false/no/off) disables
  Team plumbing. A fresh solo install stays open, first-admin setup requires a live Team
  entitlement, and an existing team's authentication wall remains active if its license
  lapses so private data never becomes public.
- Pre-login license status and trial routes now allow a fresh instance to start a Team
  trial before first-admin setup. Purchased keys bootstrap through
  `ENGRAPHIS_LICENSE_KEY` or the license file; `/api/license/activate` remains admin-only.
- Package fallback metadata and all user-facing tool inventories now agree on version
  `0.9.5` and 20 MCP tools.

### Fixed
- **Agent Connect and dashboard lifecycle:** corrected generated endpoint URLs, retained
  one-time token visibility, enforced member access on `/mcp`, closed previously injected
  stores, and made connect-info reflect the real optional MCP mount.
- **License and billing enforcement:** authoritative revocations override cached
  entitlement immediately while transient failures may use an unexpired lease; runtime
  public-key replacement is test-only; trial verification links survive signing failures;
  webhook reservations are retry-safe after crashes; subscription seat baselines no
  longer suppress the first real update; and registry writes no longer occur inside the
  SQLite trial transaction.
- **Memory and retrieval integrity:** audit writes are committed durably, recall excludes
  non-live rows, sync dry-runs validate remote repository mappings, graph provenance is
  pruned per memory instead of deleting shared edges, SQLite-vector distances are
  converted to cosine similarity, and entity expansion matches complete names.
- **Structured-data safety:** extraction metadata survives ingest unchanged, proactive and
  consolidation inputs are bounded, structured consolidation rejects source IDs outside
  the requested cluster, and synthesized context cannot replace deterministic output
  without valid citations.
- **Dashboard graph navigation:** focusing an isolated node now retains the requested node
  through the delayed renderer retry instead of reporting a false “Entity not in view.”

### Documentation
- Updated the README, Agent Connect, Railway, Kilo Code, bundled memory skill, benchmark
  command, and package-version fallback to match the shipped routes, tool count, setup
  order, and extractor/consolidation options; removed the unused shortcut icon helper.

## [0.9.5] - 2026-07-14

### Changed
- **Team mode is now ON by default (opt-out).** `ENGRAPHIS_TEAM_MODE` defaults to on;
  set `ENGRAPHIS_TEAM_MODE=0` (or false/no/off) to disable. The per-user login wall is
  no longer raised just because the mode flag is on — it now requires a *live* `team`
  feature entitlement (`licensing.has_feature("team")`), checked at request time in
  `dashboard_app.py` and reflected in `/api/auth/state`. Solo / no-license installs stay
  fully open, and the wall appears the moment a team license key is added — even via the
  dashboard UI at runtime. A `team` license is still required to *add seats* beyond the
  first admin (bootstrap admin is created unconditionally). Docs (`.env.example`,
  `AGENTS.md`, `README.md`, `SECURITY.md`, `scripts/init.py`) and team-mode test fixtures
  updated.
- **Team-invite email rewritten to separate "join" from "activate a key".** The old
  invite conflated the two, so members pasted the shared team key into the hosted/Railway
  dashboard, saw it "work" (it just re-activated a license already active there), and
  thought they'd joined — when joining means signing in with email + password. The email
  now frames two distinct options: **Option 1** (required to join) sign in to the team
  dashboard with email + the admin-set password — explicitly *no license key needed here,
  don't paste one*; **Option 2** (optional) run Engraphis on your own machine and access
  the team's memories locally — that is what the shared team key is for (LOCAL
  `http://127.0.0.1:8700` → Settings → License, then Settings → Cloud Sync to pull the
  converged team store down to a local offline copy). Invites now always carry a
  clickable sign-in link: `dashboard_url` resolves explicit arg → `ENGRAPHIS_DASHBOARD_URL`
  → `DEFAULT_TEAM_DASHBOARD_URL` (`https://team.engraphis.com/`). A footer with the
  canonical site + repo links is added as env-overridable module constants
  (`SITE_URL`/`REPO_URL`) so the URLs can't drift per-email. `tests/test_billing.py`.

### Fixed
- **Intermittent `database is locked` from `set_service`.** `routes/v2_api.set_service`
  swapped the global `MemoryService` without closing the previously-bound service's store
  connection, so under heavy test churn a deferred-GC close of the old SQLite/WAL handle
  collided with the next `MemoryService.create` on the same path. The prior store is now
  closed on swap (best-effort, never blocks the swap on a close error).

### Docs
- **README now documents three previously-undocumented shipped features** (the features
  themselves shipped in 0.9.3): sub-file chunking (`ENGRAPHIS_EXTRACTOR=chunk` + the
  `eval.chunking_eval` whole-file-vs-chunked harness), auto-dreaming (the background
  cross-cluster-inference loop, accumulation + idle trigger, `dream_inference`
  provenance/auditability), and every automation dream knob exposed via the dashboard
  Automation tab and the `GET/POST /automation` + `POST /maintenance/run` API. Also: a
  **Team early-access beta** callout (top + feature/pricing tables + Free-vs-Pro section)
  and a **daily-update reminder for maintainers** near the top (code wins; fix the doc in
  the same change).

### Chore
- `.gitignore` now excludes `automation.json` / `autosync.json` (regenerable local
  runtime state from `engraphis/automation.py`, not source content).

## [0.9.4] - 2026-07-14

### Fixed
- **The dashboard (`engraphis-dashboard` / `http://127.0.0.1:8700`) would not start.**
  `scripts/start_dashboard.py` runs uvicorn against `engraphis.dashboard_app:app`, but
  `dashboard_app.py` only defined the `create_app()` factory and never built a module-level
  `app` instance — so uvicorn aborted with `Attribute "app" not found` and nothing bound
  port 8700. The missing `app = create_app()` (present in `engraphis/app.py` and
  `engraphis/redirector.py`, but dropped from `dashboard_app.py`) is now restored. The
  background autosync/dreaming/revalidation loops inside `create_app()` are pytest-guarded,
  so importing the module under test is side-effect-free.
- **Flaky `database is locked` dashboard test.**
  `test_consolidate_inference_pass_is_pro_gated` opened two FastAPI `TestClient` lifespans
  back-to-back on the same temp DB file; the first app's still-open SQLite connection
  blocked the second's schema init. Split into two one-client test functions, matching
  the convention already documented above `test_analytics_and_export_*` (two TestClients
  in one test reproducibly deadlock). Full suite now green (693 passed, 3 skipped).

## [0.9.3] - 2026-07-14

### Added
- **Email-verified self-serve trial + abuse protections on the trial endpoint.**
  Starting a trial now requires a verified email and sends a one-time confirmation link
  before any license is issued; the request path is rate-limited so the endpoint can't be
  used to spam or farm trials. This raises the bar significantly above the previous
  device-only gate while keeping the same paste-a-key activation flow on the dashboard.
  `tests/test_cloud_license.py`, `tests/test_dashboard_v2.py`,
  `tests/test_online_only_enforcement.py`.
- **Deterministic, offline sub-file chunking on the write path (`ENGRAPHIS_EXTRACTOR=chunk`).**
  A third `Extractor` alongside passthrough/LLM: `ChunkingExtractor` splits a document into
  retrieval-sized `ExtractedFact` chunks that preserve meaning — markdown headings start new
  chunks and become the title, fenced code blocks stay intact, prose is packed to a token
  budget (`ENGRAPHIS_CHUNK_TOKENS`, default 256) with a sentence-level overlap
  (`ENGRAPHIS_CHUNK_OVERLAP`, default 32); a hard per-document cap
  (`ENGRAPHIS_CHUNK_MAX`, default 200) bounds amplification. numpy/stdlib only, so it runs
  under the offline gate and is byte-identical across runs. This lifts recall on long,
  multi-topic documents that previously became one diluted memory. New: `ChunkingExtractor`
  in `backends/extractor.py`; `tests/test_chunking_extractor.py`.
- **File/folder imports chunk too.** With `ENGRAPHIS_EXTRACTOR=chunk`,
  `import_folder`/`import_files` split each file into several retrieval-sized memories
  (each still `trusted:false`, stamped with `metadata.chunk={index,of,heading}`) instead of
  one; the LLM extractor is deliberately never applied to the local import path (no external
  calls on untrusted disk files). A file still counts as one imported unit.
  `tests/test_import_chunking.py`.
- **Chunking eval + `longdoc` dataset.** `eval/chunking_eval.py` +
  `eval/datasets/longdoc.jsonl` compare whole-file vs chunked ingestion through the real
  recall pipeline. On the offline embedder: identical recall@5 (1.000) at **~73% fewer
  context tokens** (826 → 224) and ~4× smaller tokens-to-evidence — the "quality per token"
  number `BENCHMARKS.md` calls for. `tests/test_chunking_eval.py`.
- **"Dreaming" trigger for automated maintenance.** `automation.should_dream` / `dream_due`
  run a consolidation sweep *before* the cadence when enough new episodic memories have
  accumulated **and** the store has gone quiet (`dream_min_new` / `dream_idle_minutes` policy
  knobs); wired into `scripts/auto_maintain.py`. Purely additive to the existing cadence, so
  cron behaviour is unchanged; still Pro-gated. `tests/test_dreaming_trigger.py`.
- **Associative cross-cluster inference (dream pass 4).** `consolidate.infer_links` /
  `consolidate(infer=True)` proposes evidence-only links between memories in *different,
  dissimilar* subject clusters that share a bridging entity — the "connect distant dots" step
  same-subject distillation never reaches. **Off by default** (`infer=False`); the pass
  follows the sweep's own `dry_run` flag, so a dry-run proposes into the report and a real
  run applies. Applied inferences are low-salience (`importance=0.25`), `trusted:false`,
  `source='dream_inference'`, linked to their sources and audited, so a bad inference is
  visible, downweighted, and never merge-eligible into a trusted fact. Fan-out capped,
  idempotent. Entity matching is now word-boundary (so `Redis` won't fire on
  `rediscovered`) and the per-sweep text scan is computed once, not per entity.
  `tests/test_inference.py`.
- **Inference is reachable from the maintenance path.** A new `infer` policy knob (off
  by default) runs the inference pass inside `run_maintenance` — manual *or* the dream loop
  — following the sweep's `dry_run`. `/api/consolidate` takes `infer` (`false` by default);
  `/api/automation` round-trips `infer`; the dashboard Automation tab has an Inference
  toggle. `tests/test_dashboard_v2.py` (policy round-trip + `/maintenance/run` proposes the
  Redis bridge), `tests/test_dashboard_dream_ui.py`.
- **Dreaming runs without cron.** A dashboard background loop (`_maybe_start_dreaming`,
  mirroring auto-sync) runs a maintenance sweep whenever `automation.dream_due` fires — opt-in,
  Pro-gated, fault-isolated, with an `ENGRAPHIS_DREAM_LOOP=0` kill switch. The `/api/automation`
  policy round-trips the `dream` / `dream_min_new` / `dream_idle_minutes` knobs, and the
  dashboard's Automation tab surfaces them as form controls (toggle + thresholds). The
  trigger now scopes its accumulation/idle count to the policy's `workspaces` (a burst in
  an out-of-scope workspace no longer fires a sweep). `tests/test_dreaming_trigger.py`,
  `tests/test_dashboard_dream_ui.py`, `tests/test_dashboard_v2.py`.

### Fixed
- **First-run team-mode bootstrap hardened.** The admin-creation path no longer depends
  on an external relay round-trip succeeding to provision the first seat, and concurrent
  first-admin requests are serialized so only one unlicensed bootstrap admin can ever be
  created. Subsequent seat additions still require an active Team license.
- **First-run team-mode bootstrap fixed (frontend).** The admin-account screen now triggers
  the trial/activation step before provisioning the first admin, so a fresh self-hosted
  instance no longer deadlocks on the team-feature gate with no way to proceed.
  No backend change; frontend-only.
- `MemoryService.create` now defaults `extractor` from `settings.extractor`
  (`ENGRAPHIS_EXTRACTOR`) when unset — mirroring the existing `graph_extractor` fallback — so
  the dashboard and automated-maintenance front ends honor the config knob, not just the MCP
  server and CLI. An explicit `extractor="none"` still overrides the environment.

### Security
- **Closed a Pro-feature bypass on the manual consolidate endpoint.** The inference pass
  (a paid capability) was reachable through the free housekeeping endpoint without a
  license; it is now gated at the route and reinforced inside the service layer, so no
  caller can reach the Pro-only path without a server-approved license. The free manual
  consolidate action is unchanged. `tests/test_dashboard_v2.py`, `tests/test_inference.py`.
- **Strengthened license enforcement and revocation handling.** Reaffirmed that every paid
  surface requires a live, server-validated lease and fails closed when the server is
  unreachable; tightened the verification so licenses can't be forged client-side, and
  serverside-issued seats can't be minted without a valid license. Revoked or refunded keys
  are now re-confirmed against the server on a background interval so they degrade promptly
  rather than remaining usable until lease expiry, while legitimate offline customers are
  never stalled. `tests/test_online_only_enforcement.py`, `tests/test_cloud_license.py`.

## [0.9.2] - 2026-07-13

### Added
- **Personal vs. shared folders + a redesigned Team dashboard.** A folder can now be
  created `visibility='personal'` (owned by, and visible/usable only to, the creating
  dashboard user) or `shared` (the whole team — the previous, still-default behaviour).
  Enforcement runs through a single workspace-authorization chokepoint, so every scoped
  read/write inherits it and a non-owner cannot access another user's personal folder.
  Personal folders are excluded from relay sync so they stay on-device. The **Team
  dashboard** gains a team overview (seat usage + activity), a Folders panel that creates
  and manages shared/personal folders (folder creation now lives here — the Workspaces
  tab is selection-only in team mode), members with last-active, and a team audit log with
  CSV export. New/updated: `service.py`, `routes/v2_api.py`, `dashboard_app.py`,
  `static/index.html`; tests in `tests/test_personal_folders.py`,
  `tests/test_dashboard_v2.py`, `tests/test_sync_dashboard.py`.

### Changed
- README expanded with the missing features (cloud sync, encryption, import/ingest,
  workspace ops, Docker, config, and more) and now links to the Engraphis Discord.

## [0.9.0] - 2026-07-13

### Added
- **Team invite emails now carry the shared Team license key + dashboard URL** so a
  newly added member can activate Pro features (analytics, export, automation, cloud
  sync) on their own machine and take one server-enforced seat. Updates
  `cloud_license`, `inspector.license_cloud`, `inspector.webhooks`, and
  `routes.v2_team`; the add-user response now reports `pro_activation_sent` and
  `dashboard_url_configured`.
- **Automatic v1→v2 database migration on startup**: a pre-existing v1-shaped
  `engraphis.db` (no `workspace_id` column) is backed up and migrated to the v2
  schema, so existing installs upgrade cleanly without manual SQL.

### Fixed
- **Dockerfile default entrypoint** is now `engraphis-dashboard --no-open` (was the v1
  single-user `engraphis-server`), so a fresh container serves a working team dashboard
  with auth/license/trial routes instead of a permanently signed-out UI.
  `engraphis-server` remains available as an explicit override for single-user
  deployments.
- **CI**: ruff lint errors and core-floor (numpy-only) test collection —
  fastapi-dependent tests now skip cleanly on the minimal core floor. `loads_strict`
  now rejects pathologically deep JSON on every Python version (3.12's JSON scanner
  no longer raises RecursionError for ~1000-deep input, which had broken the
  deep-nesting parsing guard and its test on 3.12).

## [0.8.8] - 2026-07-13

### Security
- Hardened license validation and trial consumption tracking
- Improved offline trial tamper resistance

## [0.8.7] - 2026-07-12

### Added
- **Dashboard "Import files & folders"** restored on v2 engine
- **Kilo Code integration docs** (`docs/KILO_CODE_INTEGRATION.md`)

### Fixed
- Dashboard auth: session handling, role badges, member management
- License cloud enforcement: lease validation, online-only gating
- Service layer: workspace operations, memory reorder, merge

## [0.8.6] - 2026-07-12

### Added
- Dashboard "Import files & folders" section restored on v2 engine
  (`engraphis/service.py`, `routes/v2_api.py`, `static/index.html`, Workspaces tab)
- Server-side path import and drag-and-drop upload, both member-gated and bounded
- Imported memories marked untrusted by default; 21 new tests

### Security
- Hardened folder import against path-traversal and containment bypasses

## [0.8.5] - 2026-07-12

### Fixed
- Logout no longer re-triggers sign-in modal loop
- Team bootstrap: trial/license endpoints now accessible before first admin exists
- Expired/revoked Team license no longer locks out all logins
- Trial start now idempotent (no 400 on repeated calls mid-trial)
- Team trial grants 5 seats (was 1), enabling actual team evaluation
- Cloud-license test isolation (prevents false passes against production relay)
- Dashboard handles empty workspaces gracefully
- Static assets (dashboard HTML, vendor JS) now ship correctly in wheel

## [0.8.4] - 2026-07-12

### Security
- Paid features now require a live, server-issued license lease
- Offline handling degrades gracefully with bounded grace when the server is unreachable
- Local/offline trial grants removed; trials are server-issued and tracked per device
- Issued keys are server-enforced by default

## [0.8.3] - 2026-07-12

### Fixed
- Empty workspace `/api/memories` returns `[]` instead of 500
- Online-only license enforcement: cloud-mode keys validated per request

## [0.8.2] - 2026-07-12

### Fixed
- Static package discovery: `engraphis/static/__init__.py` added
- Vendor glob: recursive pattern so `static/vendor/` bundles ship in wheel
- Dashboard 500 on `GET /` — `static/index.html` was missing from wheel (packaging bug)
- Dashboard 500 on fresh install — `GET /api/memories` crashed on empty workspace

## [0.5.5] - 2026-07-12

### Security
- Per-key server-side license enforcement (opt-in at issuance)
- Trial consumption now durable across reinstalls
- License expiry/revocation now propagate promptly
- Team-mode logins require live Team license

### Fixed
- Team dashboard "Add member" email delivery (with vendor relay fallback)
- Persistent-volume startup crash on managed hosts (Railway/Fly)
- Team invite emails work out of the box via vendor relay fallback

### Added
- Team invite emails via vendor relay (zero email setup required)

---

## Earlier versions (condensed)

### 0.5.x — 0.7.x
- MCP server with 18 tools
- Memory Inspector product UI (`engraphis-inspector`, port 8710)
- Dashboard rebuilt on v2 engine with recall, governance, consolidate, analytics
- Team mode: login auth, viewer/member/admin roles, seat limits
- Grounded recall with cited answers and abstain gate
- Sleep-time consolidation with compaction accounting
- Personalized PageRank graph arm (HippoRAG-style)
- Offline signed license keys (no phone-home)
- Pro analytics dashboard and compliance export
- Code-symbol graph via tree-sitter or regex fallback
- Docker + docker-compose deployment
- 300+ tests, eval harness, ablation suite

### 0.1.0 — 2026-07-09
- Initial public release: local-first AI memory engine for agents
- Ebbinghaus decay, interaction-aware recall, bi-temporal facts
- Background consolidation; you bring the LLM

---

**Security reporting:** Email **security@engraphis.dev** for vulnerability disclosure.
