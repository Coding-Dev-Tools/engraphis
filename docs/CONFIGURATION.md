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
| `ENGRAPHIS_SQLITE_MMAP` | `0` | Opt-in 64 MiB memory-mapped SQLite reads (`1`, `true`, or `yes`) for local plaintext databases; skipped for SQLCipher and off by default for synced/shared folders. |
| `ENGRAPHIS_VECTOR_CACHE_BYTES` | `67108864` | Maximum byte budget for the in-process `NumpyVectorIndex` decoded matrix cache (default 64 MiB; `0` disables retention). |
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Dashboard port. A platform-injected `$PORT` (Railway/Fly/Heroku) takes precedence over this value for the dashboard bind; Compose pins both to `ENGRAPHIS_COMPOSE_PORT` so the mapping stays in sync |
| `ENGRAPHIS_SERVICE_MODE` | `customer` | The public package supports only `customer`; hosted vendor, relay, compute, and worker roles are not distributed here |
| `ENGRAPHIS_API_TOKEN` | Not set | Optional bearer credential for remote API/operator access to this single-user node; never reuse a hosted credential. Machine clients and `/mcp` present it as `Authorization: Bearer`. Browsers never type it: the dashboard opens automatically, or with a one-time link when `ENGRAPHIS_BROWSER_AUTO_SESSION=0`. |
| `ENGRAPHIS_BROWSER_AUTO_SESSION` | `true` | A browser that reaches the dashboard opens it without a credential, so the deployment's network reachability is the dashboard's boundary. Set to `false` to require a one-time sign-in link (`engraphis-dashboard --link`, or `POST /api/auth/link` with the bearer): single use, 15 minutes, 12-hour session. |
| `ENGRAPHIS_CORS_ORIGINS` | loopback on `ENGRAPHIS_PORT` | Comma-separated REST CORS allow-list; defaults to `127.0.0.1` and `localhost` on the configured port |
| `ENGRAPHIS_INDEX_ROOTS` | Working, home, and temporary directories | Optional path-separator-delimited absolute-path allow-list that replaces the default roots accepted by local code indexing |
| `ENGRAPHIS_HTTP_INDEX_ROOT` | First `ENGRAPHIS_INDEX_ROOTS` entry, or current directory | Single root for dashboard and REST `POST /api/code/index`; submitted paths resolve beneath it. An explicit root (or fallback entry) must be absolute; an explicit HTTP root is included in the engine-approved set. MCP and CLI indexing continue to use `ENGRAPHIS_INDEX_ROOTS`. |
| `ENGRAPHIS_DB_KEY` | Not set | Encrypt the database at rest (SQLCipher). Or use `ENGRAPHIS_DB_KEY_FILE` |
| `ENGRAPHIS_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` (or `nvidia/nv-embedqa-e5-v5` when `ENGRAPHIS_EMBED_PROVIDER=nvidia-nim`) | Embedding model selector. Uses local sentence-transformers by default (prefix `local:` to forbid downloads). Prefix `nvidia-nim` or `nvidia-nim:<model>` to opt into the NVIDIA NIM embeddings preset. |
| `ENGRAPHIS_EMBED_PROVIDER` | `local` *(unset)* | Embedding provider: `local` / `sentence-transformers` (default, offline-capable), `nvidia-nim` (opt-in NVIDIA NIM OpenAI-compatible embeddings with asymmetric `input_type`), or `api` / `openai-compatible` (generic `/v1/embeddings` endpoint). |
| `ENGRAPHIS_EMBED_DEVICE` | *(unset; default CPU/loader default)* | Optional PyTorch device selector for local sentence-transformers embeddings: `auto` (use CUDA when PyTorch reports an available GPU, else CPU), `cuda`, `cuda:<index>`, or `cpu`. Falls back to CPU when CUDA is unavailable unless `ENGRAPHIS_REQUIRE_EXACT_BACKENDS=true`. |
| `ENGRAPHIS_EMBED_BASE_URL` | Not set | Optional base URL override for `api` or `nvidia-nim` embeddings (for `nvidia-nim`, defaults to `https://integrate.api.nvidia.com/v1`, or set to a self-hosted NIM URL such as `http://127.0.0.1:8000/v1`). Non-loopback endpoints must use HTTPS. |
| `ENGRAPHIS_EMBED_API_KEY` | Not set | Optional API key for `api` or `nvidia-nim` embeddings. For `nvidia-nim`, falls back to `NVIDIA_API_KEY`; for `api`, falls back to `ENGRAPHIS_LLM_API_KEY`. Not required for loopback self-hosted NIM endpoints. |
| `NVIDIA_API_KEY` | Not set | Standard NVIDIA API Catalog credential used by the opt-in `nvidia-nim` embedding preset when `ENGRAPHIS_EMBED_API_KEY` is unset. Never printed or logged. |
| `NVIDIA_NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Optional base URL alias for a self-hosted or regional NVIDIA NIM embeddings endpoint when `ENGRAPHIS_EMBED_BASE_URL` is unset. |
| `ENGRAPHIS_MCP_PRELOAD_EMBEDDER` | `auto` | Standalone MCP launchers import optional semantic dependencies on the launcher thread on Windows before serving requests. Set `0` to disable or `1` to enable on any platform; model loading and backend fallback policy remain unchanged. |
| `ENGRAPHIS_EMBED_REVISION` | Not set | Optional immutable lowercase 40-hex Hugging Face commit for local sentence-transformers models, or operator-chosen `space_version` string for persistent `nvidia-nim` / `api` embedding stores. Loaded Hub commits, local artifact manifests, or API space versions identify persistent vector spaces; unresolved mutable identities keep vector recall fail-closed. |
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
| `ENGRAPHIS_LLM_PROVIDER` | `openai` (auto-routes to `managed` on connected Pro/Team) | `managed \| openai \| anthropic \| google \| openrouter \| custom`. Connected active or trial Pro/Team installations automatically use `managed` (up to 100 free OpenRouter AI LLM calls per person per day) when no personal API key is set. |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | Model name (provider-specific; defaults to `nvidia/nemotron-3-ultra-550b-a55b:free` in `managed` mode) |
| `ENGRAPHIS_LLM_API_KEY` | Not set | API key for BYOK chat/synthesis, `llm` / `llm_structured` extraction, and structured consolidation (not required when using included Pro/Team `managed` LLM) |
| `ENGRAPHIS_LLM_BASE_URL` | Not set | Base URL for openrouter / custom OpenAI-compatible endpoints |
| `ENGRAPHIS_DECISION_BACKEND` | `none` (auto-routes to `managed` on connected Pro/Team) | `none` or `local` keeps advisory decisions local; `managed` uses the saved Cloud session and an included free daily allowance of up to 250 Jev calls per person per day (active paid and trial); `auto` selects managed when configured and never switches to BYOK; explicit `byok` uses a personal TypeSafe key and may incur provider charges. Managed custom questions and query planning are unsupported. Legacy `typesafe`, `jev`, and `system1` mean BYOK. Remote calls also require per-call consent. |
| `ENGRAPHIS_DECISION_MODEL` | `jev-1.13.0` | Pinned model accepted by the Jev transport; other model identifiers are rejected. |
| `TYPESAFE_API_KEY` | Not set | Personal credential for explicit BYOK decisions; `JEV_API_KEY` is a fallback alias. Managed decisions use the saved Cloud session instead. |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Direct BYOK provider origin; does not change the managed session's bound Cloud control origin. |
| `ENGRAPHIS_LLM_AUTO_EXTRACT` | `0` | Opt in to switching the running engine to `llm_structured` after a successful live connection test; the dashboard's extraction Off button persists `0`, and its On button restores `1` |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | *(none)* | Proxies trusted for forwarded client/TLS headers (`*` only when the service is reachable exclusively through that proxy) |
| `ENGRAPHIS_LOCAL_TRUSTED_PEERS` | *(none)* | Exact peers/CIDRs treated as local without forwarding headers; use only for trusted Docker/LAN peers, never public deployments |
| `ENGRAPHIS_DASHBOARD_URL` | Not set | The exact URL clients use to reach this dashboard. Its host name is accepted by the `/mcp` DNS-rebinding guard, by automatic dashboard browser access, and, without an API token, by the dashboard API |
| `ENGRAPHIS_ALLOWED_HOSTS` | *(none)* | Comma-separated extra host names (ports ignored) that the token-free dashboard API and read-only graph server answer. Without a token they otherwise accept only exactly `localhost`, IP addresses, the bind host and the `ENGRAPHIS_DASHBOARD_URL` host, which keeps DNS-rebinding websites out; with a token every name works |
| `ENGRAPHIS_UPDATE_CACHE` | `86400` | Update-check cache TTL in seconds, bounded to `1..31622400`; this is never a cache-file path |
| `ENGRAPHIS_UPDATE_CHECK` | Off | Opt-in release reminder surfaced in the dashboard, server startup log, and MCP. Update checks run only when this is set to an affirmative value; `0` keeps them off. |
| `ENGRAPHIS_UPDATE_URL` | Not set | Overrides the release-check source URL; the outbound client accepts HTTPS and rejects private/reserved destinations. |
| `ENGRAPHIS_CLOUD_CONTROL_URL` | hosted default | Official entitlement, organization, and credential control API. A saved rotating credential stays bound to the control endpoint recorded for its family; reconnect to change it. |
| `ENGRAPHIS_CLOUD_COMPUTE_URL` | hosted default | Official Analytics and managed-automation API. A saved rotating credential stays bound to its recorded compute endpoint; reconnect to change it. |
| `ENGRAPHIS_CLOUD_ORGANIZATION_ID` | Not set | Hosted organization bound to this customer session |
| `ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL` | Not set | Bootstrap-only rotating hosted credential; after first use the owner-only cloud session replacement takes precedence |
| `ENGRAPHIS_CLOUD_TOKEN_SUBJECT` | `member` | Subject fixed during hosted bootstrap (`device` or `member`); set explicitly with an environment-only refresh credential |
| `ENGRAPHIS_CLOUD_ACCESS_TOKEN` | Not set | Optional short-lived access token for ephemeral jobs |
| `ENGRAPHIS_MANAGED_COMPUTE_CONSENT` | *(unset)* | Deny-only operator override: `0` pauses readable managed processing. A truthy value cannot grant approval. Each workspace requires explicit confirmation in Settings → Preferences; encrypted sync is separate |

Managed Jev is available for paid plans; the hosted control service enables it with
its provider key. Every legitimate Pro user and each eligible Team named seat, including
paid viewers, with an active paid, trial, or test entitlement receives managed Jev at no
additional customer charge and without a personal provider key.

Each paid individual receives up to **250 Jev calls per person per day**, free, plus up to **100 AI LLM calls per person per day** via OpenRouter's free tier on active paid and trial plans. Usage is per person, not pooled across a Team and not monthly. Each evaluated call counts once, including a batched request; calls beyond the daily maximum are not served. Admitted attempts that fail or are interrupted remain counted, and no overage is charged. The account portal reports that member's remaining calls for the current day.

No latency, accuracy, or cost-saving
guarantee is established by configuration or a successful health check. These rules are
service-enforced, not client configuration overrides. Selecting a backend does not
enable the service or satisfy release, provider-terms, capacity, or quality gates. See
[hosted plans](HOSTED_PLANS.md#included-system-1-decision-engine-jev).

The four managed workflows require a command, a pair of facts, query/evidence, or
goal/output context and their fixed question schemas. Arbitrary `custom` and
`query_planning` payloads are rejected before credential refresh or network requests.
Experimental recall route planning requires explicit BYOK; managed failure never
silently selects a personal key. Remote consent and `public` or `internal`
classification remain mandatory, and `offline_mode=true` prevents remote requests.
Viewers can use direct Classic advisory decisions; generic Smart stateful execution
still requires admin and the private Team tool catalog is unchanged.

Managed transport callers can supply `request_key` for explicit retry deduplication. A
duplicate admitted key returns 409 without an additional provider call or usage
increment; there is no stored answer replay or automatic retry. Admitted errors remain
counted. This parameter is not an environment setting or an MCP/dashboard field.

The optional cross-encoder reranker is model- and hardware-dependent. Treat its quality and
latency as deployment-specific until a versioned model identity, exact configuration, and
reproducible evaluation artifact are available for the comparison being reported.

## Optional NVIDIA CUDA and NVIDIA NIM embeddings

Engraphis defaults to local, offline-capable CPU embeddings and never makes remote embedding
calls unless you explicitly opt in.

### Local CUDA acceleration (optional)

When running local `sentence-transformers` embeddings on a host with a CUDA-capable NVIDIA GPU
and CUDA-enabled PyTorch, opt in via `ENGRAPHIS_EMBED_DEVICE`:

```dotenv
ENGRAPHIS_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
ENGRAPHIS_EMBED_DEVICE=auto   # auto | cuda | cuda:0 | cpu
```

- `auto` selects `cuda` when `torch.cuda.is_available()` is true and otherwise falls back cleanly
  to `cpu` (including on CPU-only or PyTorch-free installs).
- `cuda` or `cuda:<index>` requests that GPU explicitly; if CUDA is unavailable, Engraphis logs a
  warning and falls back to `cpu` unless `ENGRAPHIS_REQUIRE_EXACT_BACKENDS=true`, in which case
  startup fails with an actionable error.

### NVIDIA NIM embeddings preset (optional)

To use NVIDIA NIM's OpenAI-compatible `/v1/embeddings` API (hosted NVIDIA API Catalog or a
self-hosted NIM microservice), configure either `ENGRAPHIS_EMBED_PROVIDER=nvidia-nim` or the
`nvidia-nim` model prefix. Engraphis automatically sends `input_type="passage"` when indexing
memories and `input_type="query"` during recall and grounded support checks.

**Hosted NVIDIA API Catalog:**

```dotenv
ENGRAPHIS_EMBED_PROVIDER=nvidia-nim
ENGRAPHIS_EMBED_MODEL=nvidia/nv-embedqa-e5-v5
ENGRAPHIS_EMBED_REVISION=nim-nv-embedqa-e5-v5-v1
NVIDIA_API_KEY=<nvidia-api-key>
```

**Self-hosted NVIDIA NIM microservice (local GPU host, no external egress):**

```dotenv
ENGRAPHIS_EMBED_MODEL=nvidia-nim:nvidia/nv-embedqa-e5-v5
ENGRAPHIS_EMBED_BASE_URL=http://127.0.0.1:8000/v1
ENGRAPHIS_EMBED_REVISION=selfhosted-nim-e5-v5-v1
# NVIDIA_API_KEY is optional for loopback self-hosted NIM endpoints
```

**Privacy and storage notes:**

- **Explicit opt-in:** Remote NIM requests occur only when `nvidia-nim` (or `api`) is configured.
  When targeting a remote URL (such as `https://integrate.api.nvidia.com/v1`), non-secret memory
  text and recall queries are transmitted over HTTPS to generate vectors. Self-hosted loopback NIM
  containers keep all text on your host.
- **Secret-memory guards:** Engraphis's credential detector (`reject_secrets`) rejects secret-like
  payloads before any embedder runs, and memories marked `sensitivity="secret"` are never sent to
  remote embedding APIs during writes or vector-space rebuilds.
- **Vector-space versioning and dimension safety:** Persistent SQLite stores require a non-empty
  `ENGRAPHIS_EMBED_REVISION` (`space_version`) for API embedders so Engraphis fingerprints
  `(endpoint, model, dimension, space_version)`. Switching between local models (e.g. 384-dim) and
  NVIDIA NIM models (e.g. 1024-dim) triggers the guarded versioned rebuild rather than mixing
  incompatible vector dimensions in one store. Never commit API keys; store `NVIDIA_API_KEY` or
  `ENGRAPHIS_EMBED_API_KEY` in the process environment or owner-private `~/.engraphis/config.env`.

See [`.env.example`](../.env.example) for the full variable inventory. Supply values through the
process environment or the trusted config file above; copying it to an arbitrary `./.env` does
not make Engraphis load it.
