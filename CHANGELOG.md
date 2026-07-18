# Changelog

All notable changes to Engraphis are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use SemVer.

## [Unreleased]

Follow-up audit of the Team / Pro / licensing / relay surfaces after 0.9.8.

### Security

- `verify_lease` now rejects a non-finite `expires` (`NaN`, `Infinity`). Both `now > nan`
  and `now > inf` are False, so such a lease previously passed the expiry check and never
  expired — the one fail-open in lease verification. It also rejects a signed body that
  decodes to valid-but-non-dict JSON, and a non-numeric `expires`, with `LicenseError`
  rather than an uncaught `AttributeError`/`ValueError`. Exploiting any of these still
  required the vendor signing key, so no issued lease is affected.

### Fixed

- The relay sweeps `trial_pending` rows that lapsed over a day ago. Previously a magic
  link that was never opened (bounced mail, a link scanner that never follows) was only
  ever cleared when the same `machine_id` asked again, letting a caller at the
  `/start-trial` rate-limit ceiling grow the table without bound on the volume that also
  holds `relay.db`. The one-day retention window is deliberate: sweeping at expiry would
  downgrade "this link has expired — request a new trial" into "this link is invalid or
  has already been used", non-deterministically, depending on whether an unrelated device
  happened to reserve in between.
- A revoking Polar webhook (`order.refunded`, `subscription.revoked`) that carries neither
  a subscription id nor an order id is no longer answered `202`. A 2xx stops Polar's
  redelivery, so an unexpected payload shape silently dropped the revocation and left a
  refunded customer with a working paid key. It now answers retryably on first delivery
  and converges to `202` once a redelivery proves the payload is deterministically
  unmappable — a permanent 5xx loop risks the endpoint being disabled, which would then
  drop real `order.paid` fulfillments.

- Opening a trial magic link no longer redeems it. `GET /license/v1/start-trial/verify`
  now renders a confirmation page and the grant happens on the `POST` its button sends.
  Corporate mail gateways and antivirus link-prescanners (Outlook Safe Links, Proofpoint
  URL Defense) GET every URL in an email before the recipient sees it, which silently
  burned the one-time grant and left a legitimate first attempt looking "already used" —
  worst at exactly the corporate mail estates most likely to be evaluating Team. The GET
  is read-only: it never deletes a lapsed row, so a prescanner cannot destroy what it
  cannot redeem. The token stays in the query string, so no request body is parsed and no
  multipart dependency is involved.
  The confirm form posts to a query-only relative reference, so it resolves against the
  path the page was actually served from — `ENGRAPHIS_RELAY_PUBLIC_URL` may legitimately
  carry a path (`validate_cloud_base_url` preserves it), and a root-absolute action would
  have rendered fine and then posted to a 404.
- `GET /license/v1/verify/{key_id}` and both `/license/v1/start-trial/verify` handlers now
  share the `/register` + `/team-invite` per-IP burst budget. These were the remaining
  unauthenticated relay routes with no limit; the trial-verify pair matters most, since
  both touch SQLite — and the POST takes `BEGIN IMMEDIATE` on the same `relay.db` that
  carries seat claims and sync bundles — before they can tell the token is junk.
- Every `/start-trial/verify` response (success, each error, and the 429) sends
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. The request URL carries
  the one-time token, so the error pages are as Referer-leaky as the success page that
  holds the key; they previously used separate inline header literals and had drifted.

### Changed

- `GET /api/auth/users` checks `admin` at the route, matching `auth.min_role()`. The
  middleware already enforced admin, so this is defense in depth with no behaviour change;
  the route previously said `member`, which was dead code that misrepresented the policy.

## [0.9.8] - 2026-07-18

Hardening release focused on dependable installation, upgrades, startup, dashboard use,
and safe hosted deployment.

### Security

- The relay's team-trial magic link is built from `ENGRAPHIS_RELAY_PUBLIC_URL` instead of
  the request `Host` header, so a forged header can no longer aim a trial email at an
  attacker's origin. **Issuer/vendor relays that offer trials must set this variable** —
  trial signup returns 503 until they do, before any state is written. Customer-operated
  sync relays remain clients of the managed issuer and do not set vendor secrets.
- The vendor-admin fallback to `ENGRAPHIS_API_TOKEN` is removed. A relay that never set
  `ENGRAPHIS_VENDOR_ADMIN_TOKEN` now fails closed on `/license/v1` administration instead
  of accepting the per-instance service token.
- Relay bundle storage is capped per account as well as per workspace
  (`ENGRAPHIS_RELAY_MAX_ACCOUNT_BYTES`, default 2 GiB;
  `ENGRAPHIS_RELAY_MAX_WORKSPACES_PER_ACCOUNT`, default 64), closing an unbounded-growth
  path available to a single authenticated key.
- `/license/v1/register` and `/license/v1/team-invite` share one per-IP burst budget
  (`ENGRAPHIS_REGISTER_RATE_PER_MINUTE`, default 60) and run their Ed25519 verification in
  a worker thread. Both routes verify caller-supplied keys, so the budget is deliberately
  shared: alternating between them buys no extra work.
- Every entrypoint sends baseline response headers — CSP, `X-Frame-Options: DENY`,
  `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and HSTS over HTTPS
  only. Override with `ENGRAPHIS_CSP` / `ENGRAPHIS_HSTS`; set either to an empty string to
  omit that header where a fronting proxy supplies its own.
- A fresh deployment with no admin account, no paid license, and no API token refuses
  remote `/api` calls with 403 while still serving loopback and safe license discovery.
  Hosted trial creation and remote first-admin setup require the deployment API token,
  preventing trial-consumption denial of service and an account-takeover race.
- Loopback/bootstrap trust now rejects all common forwarding metadata, including
  `X-Forwarded-Proto`; a same-host TLS proxy can no longer make an internet request
  look like an unproxied local setup request.
- Inspector first-admin setup now uses the auth store's atomic empty-database gate, so
  concurrent different-email requests cannot both create administrators.

### Added

- MCP clients now receive canonical recall, session, durable-memory, and handoff guidance
  through the server's initialization instructions.
- The dashboard exposes a small `/api` service index, and the graph CLI documents its
  public commands without showing the internal merge-driver command.
- Regression coverage now exercises the sqlite-vec backend, workspace-aware entity recall,
  installed database migration, encryption packaging, CLI startup, update paths, and release
  artifacts.

### Changed

- Installed builds now keep the default database in the platform user-data directory.
  Existing package-directory databases are copied with SQLite's backup API, validated, and
  preserved as recovery copies; source checkouts retain their repository-local default.
- `engraphis-update` discovers the highest stable SemVer tag, validates explicit versions,
  fails closed on fetch errors, refuses dirty editable worktrees, and keeps pip, pipx, Git,
  and documents the source-rebuild path for locally built Docker images.
- Dashboard styling and navigation were reworked with five selectable themes, responsive
  mobile behavior, semantic landmarks, improved keyboard focus, clearer confirmations, and
  fully self-hosted browser assets.
- Console launchers now validate arguments before optional imports, report actionable startup
  failures, display reachable IPv4/IPv6 URLs and resolved database paths, and advertise the
  current dashboard and API routes.
- Optional-dependency bounds and extras were refreshed. The cross-platform `all` extra no
  longer pulls the platform-limited SQLCipher driver, while encryption continues to fail
  closed when no compatible driver is available.
- The release workflow now pins actions by commit, runs the full test/evaluation and package
  validation gates, matches release tags to package versions, and reserves publishing for
  validated tag pushes. Bundled browser-library license notices are included in distributions.
- Installation, hosting, sync, graph-query, MCP tool-count, and database-location guidance was
  synchronized with the current commands and runtime behavior.

### Fixed

- Installed `engraphis-init` configuration is now loaded from the current directory's
  `.env` without parent traversal, while explicit environment variables retain precedence.
  Upgrading no longer opens a fresh platform-default database instead of the database the
  user selected through `engraphis-init`.
- A failed dashboard memory-detail request can no longer retain a prior memory identity or
  leave write controls enabled, preventing a later Save from modifying the wrong memory.
- A fresh hosted deployment now renders an actionable, non-data bootstrap screen when remote
  API access is denied by default; it offers the safe Team-trial path or deployment-variable
  setup without exposing account-wide license activation to a signed-out browser.
- Dashboard, REST, Inspector, MCP, licensing, sync, billing, and provider failures now return
  bounded user-facing messages rather than raw exceptions or upstream response bodies.
- Trusted-proxy handling now evaluates the rightmost forwarded hop, supports exact/CIDR
  allow-lists, and prevents untrusted forwarding headers from changing URLs or secure-cookie
  decisions. Interactive API documentation is disabled on user-facing servers by default.
- Dashboard handlers now read memory, workspace, member, and token identifiers from escaped
  `data-*` attributes instead of interpolating untrusted values into inline JavaScript.
- Repository-graph JSON output now escapes non-ASCII labels so Windows console encodings do
  not turn successful `impact`, `prs`, or query commands into exit-code 2 failures.
- A server-only installation now includes the multipart parser required by dashboard import
  routes instead of depending on the unrelated MCP extra to provide it transitively.
- `engraphis-mcp --help` works without importing the optional MCP stack; server-only and
  explicitly offline configurations no longer emit misleading missing-dependency warnings.
- Dashboard and legacy-server launch failures retain database recovery details instead of
  collapsing them into generic errors, and invalid port values are rejected cleanly.
- SQLite vector selection is now tested in both accelerated and offline-fallback modes, while
  memory writes remain durable and audited if an index update fails.
- The zero-configuration Compose dashboard now admits its Docker host bridge while both
  published ports remain loopback-only; widening a port requires an API token.
- Git-installed updates retain their recorded PEP 610 remote, and failed editable updates
  restore the original branch without exposing a Python traceback.
- Customer-operated sync relays are separated from the managed license/trial/invite service,
  and the sample `.env` no longer overrides installed database defaults with a relative path.
- MCP end-of-session guidance again represents completed work with an empty unresolved list
  instead of persisting a fake open thread.

## [0.9.7] - 2026-07-17

### Security
- Vendor-wide license administration on the shared relay (`/license/v1` revoke /
  keys-by-email / deactivate / device listing) now authenticates with a dedicated
  `ENGRAPHIS_VENDOR_ADMIN_TOKEN`, separated from the per-instance service token
  `ENGRAPHIS_API_TOKEN` (which falls back with a logged warning until the operator
  sets the new variable) — one leaked automation credential can no longer revoke
  customers' keys. *(The fallback was removed in 0.9.8: it made this separation
  nominal on any relay that set the common variable.)*
- Team-mode login gained a per-source-IP failure throttle (25 failures / 15 min)
  alongside the existing per-email lockout, closing the credential-stuffing sweep
  that tried each address once; lockouts now surface as a typed
  `AccountLockedError` mapped to HTTP 429 + `Retry-After` (previously 401, or a
  429 derived by substring-matching the error message).

### Fixed
- `remember`/`remember_with_resolution` are now atomic across the neighbor-resolve →
  insert sequence (engine-level write lock): concurrent near-duplicate writes can no
  longer both resolve ADD and store duplicates instead of NOOP/INVALIDATE.
- The Inspector's `/api/auth/login`/`setup` no longer run PBKDF2 (600k iterations)
  on the asyncio event loop — password hashing moved to a worker thread, so a burst
  of logins can't stall every other request.
- A failed vector-index upsert on the write path is now logged and audited
  (`index_upsert_failed`) instead of silently swallowed — previously the memory
  stayed invisible to semantic recall with no trace.
- URLs built from a bind host are now IPv6-safe and connectable (`engraphis.netutil`):
  `ENGRAPHIS_HOST=::` no longer yields the malformed `http://:::8700` in the printed
  dashboard URL, the :8710 redirector target, or `Settings.base_url`; wildcard binds
  map to loopback.
- The Docker image no longer bakes an IPv4-only bind: the entrypoint defaults
  `ENGRAPHIS_HOST` to dual-stack `::` when the kernel has IPv6 (what Railway's
  private-network healthchecks require) and `0.0.0.0` otherwise, so wiping the
  service's env vars can't regress the 2026-07-16 healthcheck outage.

### Changed
- Consolidated four per-app bearer-token checks into one constant-time
  `inspector.auth.bearer_ok` helper (scheme now matched case-insensitively per
  RFC 7235 everywhere); extracted the ~230-line code-graph HTML/Markdown export
  templates from `core/engine.py` into `core/codegraph_export.py`; documented the
  v1/v2 split in `engraphis/routes/__init__`; entity ancestor-widening in graph
  recall now applies to `workspace_id` symmetrically with `repo_id`; filtered
  sqlite-vec searches cap their geometric widening with a single full scan.

### Added
- Schema v3 logical graph layers (`temporal`, `entity`, `causal`, `semantic`), privacy-safe
  SHA-256 receipt chains, optional LLM/host retention supervision, and a persistent code↔memory
  bridge.
- Incremental multi-language repository indexing (Python, JS/TS, Go, Rust, Java, C#, C/C++,
  SQL, Terraform), docstrings/comments, variables, inheritance/implementation, weighted
  communities, hotspots, path queries, git/PR impact analysis, portable JSON/HTML/Markdown
  exports, and a graph union merge driver.
- Local multi-format resource ingestion for text/code/HTML/DOCX, optional PDF/image OCR and
  faster-whisper transcription, plus live PostgreSQL schema introspection with DSN redaction.
- Seven MCP tools for code paths/impact/export, PostgreSQL schema ingestion, and receipt
  list/verify/export, bringing the tool surface from 20 to 27.
- `engraphis-graph` workflow CLI and token-protected `engraphis-graph-server` read-only HTTP
  surface.

### Changed
- Railway hosting now supports Pro solo single-admin deployments: any active Pro or Team
  entitlement can bootstrap the first admin and activates the login wall, while member
  seats and direct hosted agent writes remain Team-only. The hosting guide now covers both
  Pro solo sync-relay and Team member flows.

### Fixed
- 1-hop graph recall (and the PPR large-graph fallback) now honors `graph_layers`, matching
  the PPR arm — `Store.neighbors()` gained a `layers` filter.
- `FolderTransport.push()` no longer follows peer-planted symlinks in the shared sync folder
  (unpredictable temp name + `O_CREAT|O_EXCL|O_NOFOLLOW`), closing an arbitrary-file-write
  vector that mirrored the already-hardened read side.
- `engraphis-graph-server` treats an empty `--host`/`ENGRAPHIS_GRAPH_HOST` as non-loopback
  (it binds all interfaces), so the bearer-token requirement can no longer be skipped.
- Caller-supplied `metadata.retention_supervision` is stripped at the service boundary; only
  the validated `retention_class` presets can influence importance/stability.
- `merge_workspaces()` no longer duplicates symbols/code edges when both workspaces indexed
  the same file in a same-named repo — the losing snapshot's rows are cleared, and its
  memory↔code links are re-pointed at the surviving same-fqname symbols.
- `engraphis-graph impact/prs` reject leading-dash git revisions (git option injection), and
  graph exports refuse a symlinked output directory and are written atomically without
  following pre-planted symlinks.
- The unified graph endpoint bounds entity edges and code edges/links per request
  (`limit`-derived cap) so a large workspace graph or indexed repo can't produce unbounded
  viewer-role responses.
- Relay sync fails closed when a workspace's settings are unreadable rather than treating a
  possibly-personal folder as shared — in the sync CLI and in the dashboard/background
  `_sync_all` path; resource extraction enforces its own raw-size cap.

## [0.9.6] - 2026-07-16

### Added
- **Agent Connect for hosted Team instances.** Members can mint SHA-256-hashed per-user
  bearer tokens in Settings and use the hosted v2 store through `POST /api/remember`,
  the existing read routes, token management under `/api/auth/token*`, and
  `GET /api/auth/connect-info`. Tokens retain the user's role and personal-folder scope;
  viewers are read-only and disabling a user invalidates their tokens immediately.
- **Authenticated MCP-over-HTTP at `/mcp`.** When the MCP extra is installed, the
  dashboard mounts the same 20 tools as the standalone server and injects its existing
  `MemoryService`, avoiding a second SQLite writer. The endpoint requires an active Team
  entitlement and per-user bearer token, enforces viewer/member/admin roles per tool, and
  reports actual mount availability through connect-info.
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
  `0.9.6` and 20 MCP tools.

### Fixed
- **Agent Connect and dashboard lifecycle:** corrected generated endpoint URLs, retained
  one-time token visibility, made `/mcp` bearer-only, bound MCP sessions to their initiating
  user, rechecked tool roles on every call, retained DNS-rebinding protection, closed
  previously injected stores, and made connect-info reflect the real optional MCP mount.
- **License and Team enforcement:** authoritative revocations override cached entitlement
  and persist tombstones for previously unrecorded keys; transient failures may use only
  an unexpired lease; public license/trial bootstrap routes close after the first Team user;
  trial rate limits trust forwarded addresses only from configured proxies; managed
  requests use explicit client headers; retired managed relay URLs are canonicalized
  across key issuance, license/trial, invite, and sync clients; and configured keys
  that fall back to free after transient outages retry automatically.
- **Billing fulfillment reliability:** Polar bodies are bounded while streaming, durable
  reservation failures remain retryable, seat baselines and completion claims commit
  atomically, trial seat updates cannot mint paid-period keys, and subscription replacements
  revoke superseded keys only after the new key is registered.
- **Python and packaging compatibility:** rate-limit buckets and audit exports use
  timezone-aware UTC APIs, package metadata uses the SPDX license format, and the
  deterministic fallback matches the default embedding model’s 384 dimensions.
- **Memory and retrieval integrity:** audit writes are committed durably, recall excludes
  non-live rows, mixed embedding dimensions no longer crash recall and have a backed-up
  repair path, sync enforces workspace/repository boundaries in both directions, graph
  provenance is pruned per memory instead of deleting shared edges, SQLite-vector distances
  are converted to cosine similarity, entity expansion matches complete names, and the
  sentence-transformers adapters support both legacy and renamed dimension APIs.
- **Structured-data safety:** extraction metadata survives ingest unchanged, proactive and
  consolidation inputs are bounded, structured consolidation rejects source IDs outside
  the requested cluster, and synthesized context cannot replace deterministic output
  without valid citations.
- **Dashboard graph navigation:** focusing an isolated node now retains the requested node
  through the delayed renderer retry instead of reporting a false “Entity not in view.”
- **Dashboard typography:** replaced sub-12px text and the flat type ramp with a consistent
  12/16/24/32px hierarchy while preserving responsive layout.

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
