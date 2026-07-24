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
- **Optional bearer token** (`ENGRAPHIS_API_TOKEN`): constant-time comparison (single
  shared implementation in the local customer runtime)
- **No local Team identity service:** the public dashboard is single-user. Team logins,
  invitations, roles, named seats, token rotation, and organization audit are hosted and are
  not mounted by this package.
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
Canonical roots are restricted to the working, home, or temporary directories by default.
Set `ENGRAPHIS_INDEX_ROOTS` to a path-separator-delimited absolute-path operator allow-list to
replace those defaults for nonstandard mounts or a narrower deployment boundary.
Dashboard and REST `POST /api/code/index` use the stricter single-root boundary
`ENGRAPHIS_HTTP_INDEX_ROOT`: submitted paths resolve beneath that root. It defaults to the first
`ENGRAPHIS_INDEX_ROOTS` entry, or the current directory. An explicit HTTP root (or fallback
entry) must be absolute; an explicit HTTP root is included in the engine-approved set. MCP and
CLI indexing retain the `ENGRAPHIS_INDEX_ROOTS` allow-list semantics.
`max_files`/`max_file_bytes` bound resource use, not access within an allowed root. Traversal
does not follow file symlinks outside the root, prunes dependency/build directories during the
walk, and honors the root `.engraphisignore` without allowing negation rules to re-expose
hardcoded excludes.
Anyone who can reach an authenticated local mutation route has the authority of that local
installation, so do not share its bearer token.

### 6. Local resource and database ingestion
- Uploaded and folder-imported files are size/count bounded, marked `trusted:false`, and parsed
  as data. Missing optional PDF/OCR/transcription tools fail explicitly.
- The import UI's `derive_facts` option is a separate explicit opt-in. If an LLM/custom
  extractor is configured, selected file content may be sent to that provider; leave it off
  for a strictly local import. The default and chunk extractors remain fully local.
- Audio/video transcription runs only when `ENGRAPHIS_WHISPER_MODEL` is configured. Depending on
  the faster-whisper model name, the underlying library may download a model; use an absolute
  local model path when strictly offline operation is required.
- PostgreSQL introspection makes an outbound connection using the caller-provided DSN. The DSN is
  never stored, returned, placed in receipts, or included in an error; only a one-way source digest
  is retained. Use a read-only database account and limit network reachability at the OS/firewall
  layer.

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

### 10. Hosted-service clients

- **Cloud authorization:** the public package accepts only short-lived scoped access tokens or
  a rotating refresh credential bound to its bootstrap `device` or `member` subject. It contains
  no paid-key parser, signer, issuer, local feature gate, or long-lived-key relay exchange.
- **Server authority:** every hosted and cost-bearing operation is authorized by the private
  control plane; local plan labels and upgrade URLs are presentation metadata only.
- **Managed-compute boundaries:** Analytics, Auto Dreaming, and Auto Consolidation upload a
  bounded snapshot for authorized customers. Secret-class memories are excluded before
  serialization and rejected again by the hosted service.
- **Trial and grace are separate:** an email-confirmed trial lasts exactly 3 active days. A
  separately bounded, maximum-24-hour local workspace-write grace never extends the trial,
  subscription, Cloud Sync, managed compute, Team access, seats, or credentials.
- **Remote URL validation:** hosted endpoints require HTTPS except explicit loopback use,
  reject embedded credentials and redirects, and block private/reserved literal targets.
- **Bounded I/O:** credential-bearing JSON responses are read through strict byte limits;
  malformed, oversized, or authoritative denial responses fail closed.
- **HTTP response headers:** every entrypoint sends `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, a `frame-ancestors 'none'` CSP, `Referrer-Policy`, and
  `Permissions-Policy`; HSTS is added on HTTPS requests only. Override the CSP with
  `ENGRAPHIS_CSP` (empty string disables) or HSTS with `ENGRAPHIS_HSTS`.
### 11. Private hosted service boundary

The public package accepts only customer mode. License issuance, billing fulfillment, Team
identity, hosted relay storage, managed compute, worker execution, transactional email, and
vendor administration are built and operated from a private repository. Their signing keys,
credentials, databases, rate limits, and operational runbooks are not distributed in this
repository or its release artifacts.

## Known limitations
- Rate limiting: in-process limiter ships (`ENGRAPHIS_RATE_LIMIT`, off by default);
  use reverse proxy for multi-process/distributed
- Encryption at rest is opt-in for the local memory DB (SQLCipher via
  `ENGRAPHIS_DB_KEY`). Protect customer credential state and backups separately.
- Managed relay bundles are HTTPS-protected in transit but remain plaintext at rest until
  client-side end-to-end encryption ships. `secret` memories are never uploaded.
- Per-token scope/tenant authorization is partial: isolate distinct tenants by running
  one instance each
- Legacy v1 REST server/dashboard is a compatibility surface; prefer v2/MCP path
- **Open-core enforcement ceiling:** the Apache-licensed client can be modified. Do not rely
  on local checks to protect proprietary value. Paid authority and cost-bearing features
  execute on the private hosted service, where customer forks cannot bypass authorization.

## Supported versions
The latest published stable release is the supported line. Release-candidate documentation on
`main` does not retire the previously published line: support moves only when matching artifacts
are available from both PyPI and GitHub Releases. Pin a version and watch releases for
advisories.
