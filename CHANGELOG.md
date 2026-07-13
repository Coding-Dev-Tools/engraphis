# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [Unreleased]

### Fixed
- **Dockerfile default entrypoint** was the v1 single-user API server
  (`engraphis-server`), which serves the same `static/index.html` as the v2 team
  dashboard but has no `/api/auth/*`, `/api/license/*`, or `/api/bootstrap` routes —
  every such call 401s with a bare `{"error":"unauthorized"}` regardless of actual
  login/license state. Any host that runs the image without an explicit start-command
  override (e.g. a fresh Railway service, or one that lost a custom command) got a
  dashboard UI that looked complete but was permanently "signed out, no features, no
  trial" no matter what the user did. Default is now `engraphis-dashboard --no-open`,
  matching `docker-compose.yml`'s existing default. `engraphis-server` is still
  available as an explicit override for single-user deployments.

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
