# Security Policy

Engraphis is **local-first**: by default it binds to `127.0.0.1`, stores everything in a
local SQLite file, and sends nothing to third parties except the LLM provider *you*
configure. Most risk surfaces only appear when you expose it on a network or feed it
untrusted content. This document describes the threat model and the controls that ship today.

## Reporting a vulnerability

Email **security@engraphis.dev** with details and a proof of concept. Please do not open a
public issue for undisclosed vulnerabilities. We aim to acknowledge within 3 business days
and to ship a fix or mitigation for confirmed high-severity issues within 30 days.
Coordinated disclosure is appreciated.

## Threat model & controls

### 1. Untrusted ingested content / memory poisoning  *(primary threat)*
Memories are written by agents and may originate from web pages, tool output, or other
untrusted sources. A poisoned memory could try to inject instructions, smuggle terminal
escape sequences, or exhaust storage.

**Controls (in `engraphis/service.py`, inherited by every front end including the MCP server):**
- **Size caps** on content (100 KB), title (1 KB), keywords (64 × 128 chars), and metadata
  (16 KB) to bound resource use.
- **Control-character stripping** removes NUL, BEL, and ANSI/terminal escapes before storage,
  defanging hidden-instruction and terminal-injection payloads.
- **Strict typing/enums** for memory type and scope; invalid values are rejected with an
  actionable error rather than silently coerced.
- **Provenance on every memory** (`provenance.source`) so "why is this known?" is answerable
  and poisoned sources are traceable.
- **No destructive overwrite**: contradictions are resolved by bi-temporal invalidation
  (`valid_to`), preserving history and an `audit` trail. The write-path conflict resolver
  (`core/resolve.py`) makes this automatic — a same-subject update closes the old fact instead
  of leaving both versions live — and is fully deterministic (token-overlap heuristics, no LLM
  call on untrusted content).
- **Governance is explicit, scope-checked, and audited**: `engraphis_forget` / `engraphis_pin` /
  `engraphis_correct` / `engraphis_link` let a human or agent retire, fix, or connect a memory
  after the fact. Every one of these requires `workspace` (and optionally `repo`) and verifies
  the target memory actually belongs to that scope *before* mutating it — a caller that has
  only ever seen a `memory_id` from one workspace's recall/why output cannot act on a different
  workspace's memory by reusing that id. Every action is recorded in `Store.audit`, and none of
  them hard-delete (`forget`/`correct` close validity, same as automatic conflict resolution).

> Note: input validation reduces blast radius but cannot judge *truthfulness*. Treat recalled
> memories as untrusted context, and prefer scoping (below) to limit what any one agent sees.

**Rendering untrusted content (v1 dashboard, `engraphis/static/index.html`):** the dashboard
renders memory content as markdown via `marked`, which (v5+) does not sanitize embedded HTML by
design — a memory whose content included e.g. `<img src=x onerror="...">` would previously
execute arbitrary JavaScript in the dashboard the moment a human viewed it, entirely independent
of the size/control-character checks above (those live in `service.py`/the v2 path; the v1
dashboard's `/memory/*` routes don't call into them). Fixed by routing every markdown render
through a `renderMd()` helper that pipes `marked.parse()` output through `DOMPurify.sanitize()`
before it reaches `innerHTML`, at all three render sites (viewing a memory, and both live
editor previews). Verified against a payload with an `onerror` handler: the attribute is
stripped, ordinary markdown (headings, bold, links) renders unchanged.

### 2. Network exposure & authentication
- **Loopback by default** (`ENGRAPHIS_HOST=127.0.0.1`).
- **Optional bearer token** (`ENGRAPHIS_API_TOKEN`): when set, every REST route except
  `/memory/health`, `/docs`, `/openapi.json`, `/redoc`, `/static`, and the dashboard requires
  `Authorization: Bearer <token>`. Tokens are compared in **constant time**.
- **CORS allow-list** defaults to loopback only and never combines a `*` origin with
  credentials. Override with `ENGRAPHIS_CORS_ORIGINS`.
- If you expose Engraphis beyond localhost, put it behind a reverse proxy that terminates
  **TLS** and adds **rate limiting** (not built in).

### 3. Scope isolation
Every read takes a `SearchFilter`; the MCP `recall` tool only returns memories within the
requested `workspace`/`repo`. Every governance/write tool that targets an existing memory by id
(`forget`/`pin`/`correct`/`link`) re-validates that the id belongs to the `workspace`/`repo` the
caller named, not just that the id exists — so knowing an id from one workspace's output doesn't
let you mutate another workspace's memory. Use scopes to keep one agent/tenant from reading or
altering another's memories. Cross-tenant *authorization* (per-token scope binding, so a given
MCP client can only ever name workspaces it's allowed to) is on the roadmap, not yet enforced —
within a single instance any client can still name any workspace it can guess; run one instance
per trust boundary if you need hard isolation today.

### 4. Secrets & data at rest
- `.env`, `*.db`, `*.db-wal`, `*.db-shm` are git-ignored; never commit or log them. The
  server does not log API keys.
- The SQLite database is **not encrypted at rest** yet (planned, Phase 5). Protect it with
  filesystem permissions and full-disk encryption. Restrict the DB file to the service user.
- You choose the LLM provider; review their data-handling terms, since prompts/snippets of
  recalled memory may be sent there during chat/thought-synthesis features.

### 5. Code indexing reads local files
`engraphis_index_repo` (and `MemoryEngine.index_repo`) parses source files under a path you
give it into the code symbol graph. This is the same trust boundary as any other local tool
the calling agent already has — nothing is uploaded — but it means the path is attacker-
controlled if the agent's instructions are (e.g. a prompt-injected agent pointed at a sensitive
directory). `max_files`/`max_file_bytes` bound resource use, not access scope; point it at
repos you intend to index, the same way you'd scope any other local tool.

### 6. Supply chain
- The core runs on `numpy` alone; heavy/optional components (sentence-transformers, sqlite-vec,
  the `mcp` server, FastAPI, tree-sitter) are gated behind extras so the attack surface stays
  small for embedded use. The optional code-graph backend (`backends/codegraph.py`) tolerates
  tree-sitter API changes across versions defensively and falls back to a dependency-free
  regex indexer on any import or parse failure, rather than failing the write path. Pin
  versions and run `pip audit` in your environment.

## Known limitations (not yet mitigated)
- No built-in rate limiting (use a reverse proxy).
- No encryption at rest (use disk/FS encryption).
- No per-token scope/tenant authorization (isolate by instance).
- The legacy v1 REST server/dashboard is a compatibility surface; new *capability* work targets
  the v2 core and the MCP server, but concrete vulnerabilities found in v1 (like the dashboard
  XSS above) are still fixed there directly rather than deferred. v1's request models still have
  no size caps or control-character stripping at the API layer (unlike v2's `service.py`) — a
  large or control-character-laden payload posted to `/memory/insert`/`/documents`/`/files/create`
  is stored and processed as-is. Not yet hardened; use v1 for local/trusted use only until it is.
- The deterministic conflict resolver and code-symbol-graph call edges are both heuristic
  (token overlap; name-based, not type-resolved) — neither is a security boundary, but don't
  treat either as ground truth without spot-checking on anything load-bearing.

## Supported versions
Pre-1.0: security fixes land on `main` and the latest published release. Pin a version and
watch releases for advisories.
