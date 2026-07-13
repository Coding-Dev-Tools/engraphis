# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [Unreleased]

### Added
- **Personal vs. shared folders + a redesigned Team dashboard.** A folder can now be
  created `visibility='personal'` (owned by, and visible/usable only to, the creating
  dashboard user) or `shared` (the whole team — the previous, still-default behaviour).
  Enforcement lives at MemoryService's single workspace-authorization chokepoint
  (`_authorize_workspace` via `_clean_ws`), so *every* scoped read/write inherits it; a
  non-owner (even an admin) cannot list, read, write, rename, or delete another user's
  personal folder. The current dashboard user is threaded to the service via a
  request-scoped `contextvars` value set by the team auth gate — no per-user restriction
  exists outside team mode (MCP/CLI/sync/tests are unchanged). `list_workspaces` now
  returns `visibility`/`owner` and omits other users' personal folders; personal folders
  are also excluded from the shared-account relay sync so "personal" never leaves the
  device. The **Team dashboard** gains a team overview (seat usage + activity, surfacing
  `/api/auth/overview`), a Folders panel that creates and manages shared/personal folders
  (folder creation now lives here — the Workspaces tab is selection-only in team mode),
  members with last-active, and a team audit log with CSV export (surfacing
  `/api/auth/audit` + `/audit/export`). New/updated: `service.py`,
  `routes/v2_api.py`, `dashboard_app.py`, `static/index.html`;
  tests in `tests/test_personal_folders.py`, `tests/test_dashboard_v2.py`,
  `tests/test_sync_dashboard.py`.

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
  deep-nesting DoS guard and its test on 3.12).

## [0.8.8] - 2026-07-13

### Security
- Hardened license validation and trial consumption tracking
- Improved offline trial tamper resistance

### Fixed
- Test reliability for import-folder security boundary

## [0.8.7] - 2026-07-12

### Added
- **Dashboard "Import files & folders"** restored on v2 engine
- **Kilo Code integration docs** (`docs/KILO_CODE_INTEGRATION.md`)

### Fixed
- Dashboard auth: session handling, role badges, member management
- License cloud enforcement: lease validation, online-only gating
- Service layer: workspace operations, memory reorder, merge

## [Unreleased] — restore "Import files & folders"

### Added
- Dashboard "Import files & folders" section restored on v2 engine
  (`engraphis/service.py`, `routes/v2_api.py`, `static/index.html`, Workspaces tab)
- Server-side path import (`MemoryService.import_folder()`) and drag-and-drop upload
  (`MemoryService.import_files()`), both member-gated and bounded
- Imported memories marked untrusted by default; 21 new tests

### Security
- Path-traversal guard on folder import: rejects paths outside allowed roots
  with per-file containment re-check to defeat symlink escape

## [0.8.5] - 2026-07-12

### Fixed
- Logout no longer re-triggers sign-in modal loop
- Team bootstrap: trial/license endpoints now accessible before first admin exists
- Expired/revoked Team license no longer locks out all logins (admin recovery path)
- Trial start now idempotent (no 400 on repeated calls mid-trial)
- Team trial grants 5 seats (was 1), enabling actual team evaluation
- Cloud-license test isolation (prevents false passes against production relay)
- Dashboard handles empty workspaces gracefully
- Static assets (dashboard HTML, vendor JS) now ship correctly in wheel

## [0.8.4] - 2026-07-12

### Security
- Paid features now require live, machine-bound lease from license server
- Client fails closed when server unreachable (bounded offline grace)
- Local/offline trial grants removed; trials are server-issued and tracked per device
- Issued keys are cloud-enforced by default

## [0.8.3] - 2026-07-12

### Fixed
- Empty workspace `/api/memories` returns `[]` instead of 500
- Online-only license enforcement: cloud-mode keys validated per request

## [0.8.2] - 2026-07-12

### Fixed
- Static package discovery: `engraphis/static/__init__.py` added
- Vendor glob: recursive pattern so `static/vendor/` bundles ship in wheel

## [0.8.2] - 2026-07-12

### Fixed
- Dashboard 500 on `GET /` — `static/index.html` was missing from wheel (packaging bug)
- Dashboard 500 on fresh install — `GET /api/memories` crashed on empty workspace

## [0.5.5] - 2026-07-12

### Security
- Per-key server-side license enforcement (opt-in at issuance)
- Trial consumption now durable across state-dir wipes
- License cache re-checks expiry; cloud-mode caches bounded for revocation propagation
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
- Team mode: PBKDF2 logins, viewer/member/admin roles, seat limits
- Grounded recall with cited answers and abstain gate
- Sleep-time consolidation with compaction accounting
- Personalized PageRank graph arm (HippoRAG-style)
- Offline Ed25519-signed license keys (no phone-home)
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
