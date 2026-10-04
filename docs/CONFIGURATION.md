# Configuration reference

Configuration comes from process environment variables and the owner-private
`~/.engraphis/config.env` file. `ENGRAPHIS_ENV_FILE` can select another absolute, owner-private
regular file. Process variables take precedence, and Engraphis never searches the working
directory for `.env` files.

The table lists the current supported settings. The public package runs in customer mode; hosted
control-plane, relay, compute, and worker implementations are private services.

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_ENV_FILE` | `~/.engraphis/config.env` | Optional trusted config leaf selected before trusted values load. Its bounded dependency-free parser performs no interpolation. An explicit value must be an absolute path to an owner-private regular file; arbitrary working-directory `.env` files are ignored. |
| `ENGRAPHIS_DB_PATH` | Source: `<repo>/engraphis.db`; installed: platform user-data directory | SQLite database file. Installed defaults are `%LOCALAPPDATA%\engraphis\engraphis.db` (Windows), `~/Library/Application Support/engraphis/engraphis.db` (macOS), and `$XDG_DATA_HOME/engraphis/engraphis.db` or `~/.local/share/engraphis/engraphis.db` (Linux). The environment variable overrides every default; a relative value is resolved from the trusted `~/.engraphis/config.env` directory so launch CWD cannot select a different workspace database. |
| `ENGRAPHIS_SQLITE_DURABILITY` | `durable` | Writable file databases use WAL and FULL commit synchronization. Explicit `balanced` selects NORMAL, which can lose recent acknowledged writes after OS/power failure. Effective settings appear in diagnostics; see [SQLite durability](https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/SQLITE_DURABILITY.md). |
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Dashboard port. A platform-injected `$PORT` (Railway/Fly/Heroku) takes precedence over this value for the dashboard bind; Compose pins both to `ENGRAPHIS_COMPOSE_PORT` so the mapping stays in sync |
| `ENGRAPHIS_SERVICE_MODE` | `customer` | The public package supports only `customer`; hosted vendor, relay, compute, and worker roles are not distributed here |
| `ENGRAPHIS_API_TOKEN` | Not set | Optional bearer credential for this single-user local customer node; never reuse a hosted credential |
| `ENGRAPHIS_CORS_ORIGINS` | loopback on `ENGRAPHIS_PORT` | Comma-separated REST CORS allow-list; defaults to `127.0.0.1` and `localhost` on the configured port |
| `ENGRAPHIS_INDEX_ROOTS` | Working, home, and temporary directories | Optional path-separator-delimited absolute-path allow-list that replaces the default roots accepted by local code indexing |
| `ENGRAPHIS_HTTP_INDEX_ROOT` | First `ENGRAPHIS_INDEX_ROOTS` entry, or current directory | Single root for dashboard and REST `POST /api/code/index`; submitted paths resolve beneath it. An explicit root (or fallback entry) must be absolute; an explicit HTTP root is included in the engine-approved set. MCP and CLI indexing continue to use `ENGRAPHIS_INDEX_ROOTS`. |
| `ENGRAPHIS_DB_KEY` | Not set | Encrypt the database at rest (SQLCipher). Or use `ENGRAPHIS_DB_KEY_FILE` |
| `ENGRAPHIS_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | sentence-transformers model |
| `ENGRAPHIS_MCP_PRELOAD_EMBEDDER` | `auto` | Standalone MCP launchers import optional semantic dependencies on the launcher thread on Windows before serving requests. Set `0` to disable or `1` to enable on any platform; model loading and backend fallback policy remain unchanged. |
| `ENGRAPHIS_EMBED_REVISION` | Not set | Optional immutable lowercase 40-hex Hugging Face commit for the embedding model. Loaded Hub commits or local artifact manifests identify persistent vector spaces; unresolved mutable identities keep vector recall fail-closed. |
| `ENGRAPHIS_RERANK_MODEL` | Not set | Optional sentence-transformers cross-encoder reranker |
| `ENGRAPHIS_RERANK_REVISION` | Not set | Optional immutable lowercase 40-hex Hugging Face commit for the reranker |
| `ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS` | `false` | When enabled, require a 40-hex commit before loading remote embedding models, rerankers, or chunk tokenizers; `local:` selectors and filesystem paths remain permitted |
| `ENGRAPHIS_REQUIRE_EXACT_BACKENDS` | `false` | When enabled, dashboard and standalone MCP startup fails if a configured optional backend is unavailable instead of silently falling back |
| `ENGRAPHIS_EXTRACTOR` | `none` | `none` = verbatim; `chunk` = offline structure-aware chunks; `llm` = free-form LLM facts; `llm_structured` = schema-validated facts + graph metadata |
| `ENGRAPHIS_CHUNK_TOKENIZER_MODEL` | Not set | Optional Hugging Face tokenizer used to enforce chunk budgets with the downstream reader's real tokenization; requires the optional `transformers` package |
| `ENGRAPHIS_CHUNK_TOKENIZER_REVISION` | Not set | Optional immutable tokenizer/model revision recorded in the chunk-counter identity; pin this for reproducible benchmark artifacts |
| `ENGRAPHIS_GRAPH_EXTRACTOR` | `regex` | `regex` = offline heuristic NER; `none` = disable heuristic text extraction (validated `llm_structured` metadata still feeds the graph) |
| `ENGRAPHIS_RETENTION_SUPERVISOR` | `none` | `none` = deterministic only; `llm` = sends a bounded excerpt to the configured provider for advisory ephemeral/normal/critical classification |
| `ENGRAPHIS_ALLOW_AUTOMATIC_CRITICAL_RETENTION` | `false` | Opt in only when an LLM supervisor may automatically assign the long-lived `critical` class; explicit user-selected critical retention is unaffected |
| `ENGRAPHIS_WHISPER_MODEL` | Not set | Enables local faster-whisper audio/video transcription |
| `ENGRAPHIS_POSTGRES_DSN` | Not set | CLI-only PostgreSQL source; used for the connection and never stored |
| `ENGRAPHIS_POSTGRES_CONNECT_TIMEOUT` | `10` | PostgreSQL introspection connection timeout in seconds (bounded to 1--120) |
| `ENGRAPHIS_POSTGRES_STATEMENT_TIMEOUT_MS` | `30000` | Per-introspection PostgreSQL statement timeout in milliseconds (bounded to 1--300000) |
| `ENGRAPHIS_GRAPH_TOKEN` | Not set | Bearer token for `engraphis-graph-server`; required off-loopback |
| `ENGRAPHIS_GRAPH_HOST` / `ENGRAPHIS_GRAPH_PORT` | `127.0.0.1` / `8720` | Read-only graph/recall server bind address |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | `openai \| anthropic \| google \| openrouter \| custom` |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | Model name (provider-specific) |
| `ENGRAPHIS_LLM_API_KEY` | Not set | API key for chat/synthesis, `llm` / `llm_structured` extraction, and structured consolidation |
| `ENGRAPHIS_LLM_BASE_URL` | Not set | Base URL for openrouter / custom OpenAI-compatible endpoints |
| `ENGRAPHIS_DECISION_BACKEND` | `none` | `none` or `local` keeps advisory decisions local; `managed` uses the saved Cloud session and included finite allowance for four concrete workflows after service acceptance; Team usage is pooled by licensed seat count. `auto` selects managed when configured and never switches to BYOK; explicit `byok` uses a personal TypeSafe key and may incur provider charges. Managed custom questions and query planning are unsupported. Legacy `typesafe`, `jev`, and `system1` mean BYOK. Remote calls also require per-call consent. |
| `ENGRAPHIS_DECISION_MODEL` | `jev-1.13.0` | Pinned model accepted by the Jev transport; other model identifiers are rejected. |
| `TYPESAFE_API_KEY` | Not set | Personal credential for explicit BYOK decisions; `JEV_API_KEY` is a fallback alias. Managed decisions use the saved Cloud session instead. |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Direct BYOK provider origin; does not change the managed session's bound Cloud control origin. |
| `ENGRAPHIS_LLM_AUTO_EXTRACT` | `0` | Opt in to switching the running engine to `llm_structured` after a successful live connection test; the dashboard's extraction Off button persists `0`, and its On button restores `1` |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | *(none)* | Proxies trusted for forwarded client/TLS headers (`*` only when the service is reachable exclusively through that proxy) |
| `ENGRAPHIS_LOCAL_TRUSTED_PEERS` | *(none)* | Exact peers/CIDRs treated as local without forwarding headers; use only for trusted Docker/LAN peers, never public deployments |
| `ENGRAPHIS_UPDATE_CACHE` | `86400` | Update-check cache TTL in seconds, bounded to `1..31622400`; this is never a cache-file path |
| `ENGRAPHIS_UPDATE_CHECK` | Off | Opt-in release reminder surfaced in the dashboard, server startup log, and MCP. Update checks run only when this is set to an affirmative value; `0` keeps them off. |
| `ENGRAPHIS_UPDATE_URL` | Not set | Overrides the release-check source URL; the outbound client accepts HTTPS and rejects private/reserved destinations. |
| `ENGRAPHIS_CLOUD_CONTROL_URL` | hosted default | Official entitlement, organization, and credential control API. A saved rotating credential stays bound to the control endpoint recorded for its family; reconnect to change it. |
| `ENGRAPHIS_CLOUD_COMPUTE_URL` | hosted default | Official Analytics and managed-automation API. A saved rotating credential stays bound to its recorded compute endpoint; reconnect to change it. |
| `ENGRAPHIS_CLOUD_ORGANIZATION_ID` | Not set | Hosted organization bound to this customer session |
| `ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL` | Not set | Bootstrap-only rotating hosted credential; after first use the owner-only cloud session replacement takes precedence |
| `ENGRAPHIS_CLOUD_TOKEN_SUBJECT` | `member` | Subject fixed during hosted bootstrap (`device` or `member`); set explicitly with an environment-only refresh credential |
| `ENGRAPHIS_CLOUD_ACCESS_TOKEN` | Not set | Optional short-lived access token for ephemeral jobs |
| `ENGRAPHIS_MANAGED_COMPUTE_CONSENT` | *(unset)* | Deny-only operator override: `0` pauses readable managed processing. A truthy value cannot grant approval. Each workspace requires explicit confirmation in Manage → Settings; encrypted sync is separate |

Managed Jev is currently `not_yet_available` pending release acceptance and service-capacity
qualification. After enablement, paid Pro users and paid Team named seats (including viewers) can
use four fixed advisory workflows at no additional charge within a finite allowance. Pro usage is
owner-scoped; Team questions share one organization pool sized by licensed seat count. The account
portal reports current availability and usage. No fixed quota is published, overage is not charged,
and service-protection limits may pause requests. A command review evaluates two questions; each
of the other three workflows evaluates one. Subscribers do not need a provider key for managed
use. These rules are service-enforced, not client configuration overrides. Setting a backend does
not enable the service or satisfy provider terms, release acceptance, capacity, or quality gates.
See [hosted plans](HOSTED_PLANS.md#included-system-1-decision-engine-jev).

The four managed workflows require a command, a pair of facts, query/evidence, or goal/output
context and their fixed question schemas. Arbitrary `custom` and `query_planning` payloads are
rejected before credential refresh or network requests. Experimental recall route planning
requires explicit BYOK; managed failure never silently selects a personal key. Remote consent
and `public` or `internal` classification remain mandatory, and `offline_mode=true` prevents
remote requests. Viewers can use direct Classic advisory decisions; generic Smart stateful
execution still requires admin and the private Team tool catalog is unchanged.

Managed transport callers can supply `request_key` for explicit retry deduplication. A duplicate
admitted key returns 409 without an additional provider call or usage increment; there is no
stored answer replay or automatic retry. Admitted errors remain counted. This parameter is not
an environment setting or an MCP/dashboard field.

The optional cross-encoder reranker is model- and hardware-dependent. Treat its quality and
latency as deployment-specific until a versioned model identity, exact configuration, and
reproducible evaluation artifact are available for the comparison being reported.

See [`.env.example`](../.env.example) for the full variable inventory. Supply values through the
process environment or the trusted config file above; copying it to an arbitrary `./.env` does
not make Engraphis load it.
