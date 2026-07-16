# Security Policy

Engraphis is **local-first**: by default it binds to `127.0.0.1`, stores everything in a
local SQLite file, and sends nothing to third parties except the LLM provider *you*
configure. Most risk surfaces only appear when you expose it on a network or feed it
untrusted content.

## Reporting a vulnerability

Email **security@engraphis.dev** with details and a proof of concept. Please do not open a
public issue for undisclosed vulnerabilities. We aim to acknowledge within 3 business days
and to ship a fix or mitigation for confirmed high-severity issues within 30 days.
Coordinated disclosure is appreciated.

## Threat model & controls

### 1. Untrusted ingested content / memory poisoning
Memories may originate from web pages, tool output, or other untrusted sources.

**Controls:**
- Size caps on content, title, keywords, and metadata
- Control-character stripping (NUL, BEL, ANSI/terminal escapes)
- Strict typing/enums for memory type and scope
- Provenance on every memory (`provenance.source`)
- No destructive overwrite: contradictions resolved by bi-temporal invalidation
- Governance is explicit, scope-checked, and audited
- Optional LLM extraction and retention supervision send bounded content to the configured
  provider only when explicitly enabled. Keep both disabled for a fully local write path.

> Note: input validation reduces blast radius but cannot judge truthfulness. Treat recalled
> memories as untrusted context, and prefer scoping to limit what any one agent sees.

**Dashboard XSS (fixed):** Memory content rendered as markdown is now sanitized via
DOMPurify at all render sites. Verified against payloads with `onerror` handlers.

### 2. Network exposure & authentication
- **Loopback by default** (`ENGRAPHIS_HOST=127.0.0.1`)
- **Optional bearer token** (`ENGRAPHIS_API_TOKEN`): constant-time comparison
- **CORS allow-list** defaults to loopback only
- If exposed beyond localhost: put behind reverse proxy with **TLS + rate limiting**

### 3. Scope isolation
- Every read takes a `SearchFilter`; tools only return memories within requested `workspace`/`repo`
- Every write targeting a memory by ID re-validates scope membership
- **Hard workspace binding** (`ENGRAPHIS_WORKSPACES`): comma-separated allow-list makes
  workspace a hard boundary — requests outside the list are refused before touching the store

### 4. Secrets & data at rest
- `.env`, `*.db`, `*.db-wal`, `*.db-shm` are git-ignored; never logged
- **Encryption at rest (opt-in):** `ENGRAPHIS_DB_KEY` / `ENGRAPHIS_DB_KEY_FILE` +
  `pip install "engraphis[encryption]"` → AES-256 via SQLCipher. Whole-file; lose key = lose data.
  Off by default; without it, protect with filesystem permissions + full-disk encryption.
- Review your LLM provider's data-handling terms.

### 5. Code indexing
`engraphis_index_repo` parses source files under a path you give it — same trust boundary as
any other local tool the agent has. Path is attacker-controlled if agent's instructions are.
`max_files`/`max_file_bytes` bound resource use, not access scope. Traversal does not follow
file symlinks outside the root, prunes dependency/build directories during the walk, and honors
the root `.engraphisignore` without allowing negation rules to re-expose hardcoded excludes.
In Team mode, filesystem indexing and folder imports require the admin role.

### 6. Local resource and database ingestion
- Uploaded and folder-imported files are size/count bounded, marked `trusted:false`, and parsed
  as data. Missing optional PDF/OCR/transcription tools fail explicitly.
- The import UI's `derive_facts` option is a separate explicit opt-in. If an LLM/custom
  extractor is configured, selected file content may be sent to that provider; leave it off
  for a strictly local import. The default and chunk extractors remain fully local.
- Audio/video transcription runs only when `ENGRAPHIS_WHISPER_MODEL` is configured. Depending on
  the faster-whisper model name, the underlying library may download a model; use an absolute
  local model path when strictly offline operation is required.
- PostgreSQL introspection makes an outbound connection using the caller-provided DSN and requires
  admin privileges in Team mode. The DSN is never stored, returned, placed in receipts, or included
  in an error; only a one-way source digest is retained. Use a read-only database account and limit
  network reachability at the OS/firewall layer.

### 7. Read-only graph server
`engraphis-graph-server` has no mutation routes, disables recall reinforcement and receipt writes,
and refuses non-loopback binding unless `ENGRAPHIS_GRAPH_TOKEN` (or `ENGRAPHIS_API_TOKEN`) is set.
The token protects access, not transport confidentiality; use TLS at a reverse proxy off-host.

### 8. Privacy receipts
Operation receipts exclude raw memory/query content, workspace/repo names, raw IDs, and actor
identities. Each workspace chain has a separately maintained local count/head anchor, so ordinary
row modification, reordering, interior deletion, and tail truncation are detected. The actor/scope
digests are pseudonymous, not anonymous: predictable values may be guessable. The chain is
unkeyed and its local anchor lives in the same database, so an attacker able to rewrite the whole
database can recompute both. Preserve an exported `head` + `count` outside the database and pass
them back as `expected_head` / `expected_count` when independent evidence is required.

### 9. Supply chain
- Core runs on `numpy` alone; heavy components gated behind extras
- Code-graph backend falls back to regex indexer on any failure
- Pin versions and run `pip audit` in your environment

### 10. Team mode & license keys (commercial layer)

Team mode (`ENGRAPHIS_TEAM_MODE`, ON by default unless set to `0` + a `team` license) adds per-user sessions:

- **Passwords:** PBKDF2-HMAC-SHA256, 600k iterations, ≥10 chars; constant-time verification;
  no user-enumeration timing oracle
- **Sessions:** 32-byte tokens, `HttpOnly; SameSite=Strict`, stored hashed (SHA-256), 12h TTL;
  revoked on logout/disable; role changes are enforced on the next request
- **Agent API tokens:** stored hashed, capped per user, and permanently revoked on account
  disable or password reset
- **Roles** enforced in HTTP layer (viewer < member < admin); last active admin protected
- **Login throttle:** 5 failures/15 min → 60s lockout
- **License keys:** Ed25519-signed, verified against pinned vendor public key
- **Enforcement is online-only:** signature-valid key alone does NOT unlock paid features.
  Device must register with vendor license server and hold a 24h Ed25519-signed lease
  bound to its machine ID. Fails closed: no reachable server ⇒ no paid features
  (24h grace via lease TTL). This makes revocation real, caps seats server-side, and
  closes offline bypasses.

## Known limitations
- Rate limiting: in-process limiter ships (`ENGRAPHIS_RATE_LIMIT`, off by default);
  use reverse proxy for multi-process/distributed
- Encryption at rest is opt-in (SQLCipher via `ENGRAPHIS_DB_KEY`); separate users/sessions
  DB and relay DB not yet SQLCipher-encrypted
- Managed relay bundles are HTTPS-protected in transit but remain plaintext at rest until
  client-side end-to-end encryption ships. Team personal folders and `secret` memories are never
  uploaded to the shared-account relay.
- Per-token scope/tenant authorization is partial: isolate distinct tenants by running
  one instance each
- Legacy v1 REST server/dashboard is a compatibility surface; prefer v2/MCP path
- **Open-core enforcement ceiling:** client is source-available Python — a determined user
  can patch locally to unlock purely-local paid surfaces (analytics/export/automation).
  Online-only enforcement defeats casual key-sharing, forged trials, and revoked keys.
  Features executing on vendor server (cloud sync, team invite relay) remain genuinely
  non-bypassable.

## Supported versions
Pre-1.0: security fixes land on `main` and latest published release. Pin a version and
watch releases for advisories.
