# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [Unreleased]

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
  in `backends/extractor.py`; `tests/test_chunking_extractor.py`. Design note:
  `docs/proposals/chunking-extractor.md`.
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
  cron behaviour is unchanged; still Pro-gated. `tests/test_dreaming_trigger.py`. Design note:
  `docs/proposals/auto-dreaming-consolidation.md`.
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
