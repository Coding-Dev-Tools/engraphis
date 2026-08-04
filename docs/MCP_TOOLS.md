# MCP tool reference

`engraphis-mcp` is the zero-configuration Smart MCP gateway. It initially exposes nine concise
tools: `engraphis_session`, `engraphis_recall_context`, `engraphis_remember`,
`engraphis_discover_actions`, `engraphis_execute_read`, `engraphis_execute_action`,
`engraphis_get_memory`, `engraphis_update_memory`, and `engraphis_conflict_review`. Agents use
the routine tools directly; for any advanced capability, they discover the best action and execute
the returned, version-bound capability ID. Discovery returns the precise schema and side-effect
class, and execution revalidates availability, scope, authorization, and arguments.

No user profile choice or tool switching is required. The dashboard `/mcp` endpoint and
`engraphis-mcp-http` use this Smart surface by default. `engraphis-mcp-classic` (or
`engraphis-mcp-http --classic`) preserves the 33 direct tools below for integrations that pin
their historical names and response shapes.

Hosts which already own chat history should use `POST /api/adaptive-context`, not an MCP action.
The gateway works in general MCP clients without native deferred tool search; clients that
explicitly support OpenAI's deferred `tool_search` can apply it as an optional host optimization.

## Classic direct-tool inventory

The following inventory applies to the Classic compatibility server. Start with
`engraphis_recall_context` when an agent needs prompt-ready context, and use
`engraphis_remember` when it learns a durable fact.

Retrieval responses (`engraphis_recall`, `engraphis_recall_context`,
`engraphis_recall_grounded`, and `engraphis_answer`) always declare
`degraded_mode`, `semantic_support`, and `embedding_mode`. A `true` degraded flag means
the active backend is not a declared semantic embedder (the bundled deterministic fallback
is feature hashing with lexical overlap). In that mode vector retrieval and semantic-cosine
evidence are disabled; recall remains lexical/graph/code based and grounded answers use
lexical support only.

Trust boundary: normal local-agent memory creation is prompt-visible immediately after validation;
it does not require owner approval. The default `agent` source covers `engraphis_remember`,
`engraphis_ingest`, and dashboard intent writes. External sources remain `pending` regardless of
a caller-supplied `trusted` label, and detector matches are `quarantined` immediately. Pending
and quarantined records are available only to explicit inspection workflows and never appear in
prompt-ready MCP recall or context, `engraphis_why`, or `engraphis_timeline`, nor can they feed
resolution, links, graph/code backfill, or derived prompt context. `include_untrusted=True` is
inspection-only and must never be copied into a model prompt.

MCP deliberately has no approval tool. Approval is only for external or quarantined evidence: it
creates a fresh, audited `approved` successor while retaining the reviewed source and its
provenance. In the local product it is available only through the CSRF-bound dashboard review
action (with `ENGRAPHIS_API_TOKEN`) or the interactive TTY command
`python -m scripts.approve_memory MEM_ID --reason "..."`; the command rejects redirected input
and requires a typed confirmation. Hosted approval is an owner/admin action of the private hosted
service. Direct in-process `MemoryEngine` use is a trusted-code boundary for code that already has
local database authority, not a transport permission.

For the full memory trust model and existing-store migration procedure, see the
[memory write trust model](WRITE_REVIEW.md).

| Category | Tool | What it does |
|---|---|---|
| Write | `engraphis_remember` | Stores a fact and resolves it as a new memory, reinforcement, safe supersession, or related memory. |
| Write | `engraphis_record_event` | Appends a lightweight episodic event. |
| Write | `engraphis_link` | Connects two related memories. |
| Write | `engraphis_ingest` | Applies the configured extractor (`chunk`, `llm`, or `llm_structured`). With `none`, it stores one verbatim memory. |
| Write | `engraphis_ingest_postgres_schema` | Stores a PostgreSQL schema snapshot and typed graph. The DSN is never stored. |
| Write | `engraphis_consolidate` | Runs a dry-run or live consolidation sweep. A live call can write resolved facts and receipts. |
| Stateful read | `engraphis_recall_context` | Returns hard-budget context, compact sources, token usage, and optional diagnostics. Recommended for agent prompts. |
| Stateful read | `engraphis_recall` | Runs hybrid vector, lexical, and graph recall. It records a receipt without strengthening weak matches. |
| Stateful read | `engraphis_recall_grounded` | Returns a cited answer or abstains when the evidence is too weak. It records a receipt and reinforces cited memories. |
| Stateful read | `engraphis_answer` | Backward-compatible alias for `engraphis_recall_grounded`. |
| Pure read | `engraphis_recall_proactive` | Returns high-signal, queryless context and a last-session handoff. It does not reinforce or record a receipt. |
| Stateful read | `engraphis_proactive_context` | Builds task-aware cited context and records a receipt without reinforcement. |
| Read | `engraphis_why` | Returns the current answer and the memories it superseded. |
| Read | `engraphis_timeline` | Returns complete bi-temporal history, oldest first. |
| Code | `engraphis_index_repo` | Incrementally parses a repository into the code and memory graph. Each run records a receipt. |
| Code | `engraphis_search_code` | Finds symbols, callers, and linked memories. |
| Code | `engraphis_code_path` | Finds a path across definitions, calls, imports, and memories. |
| Code | `engraphis_code_impact` | Ranks changed-file impact using dependents, communities, memories, and hotspots. |
| Code | `engraphis_export_code_graph` | Exports graph JSON, Markdown, and HTML. |
| Audit | `engraphis_receipts` | Lists content-free hashed operation receipts. |
| Audit | `engraphis_context_savings` | Summarizes packed-context usage by workspace, repository, and token-counter identity. |
| Audit | `engraphis_verify_receipts` | Verifies the receipt chain, local tail anchor, and an optional saved head/count. |
| Audit | `engraphis_export_receipts` | Exports a shareable receipt-only audit bundle. |
| Governance | `engraphis_retire` | Retires a memory by closing its validity window. It does not delete history. |
| Governance | `engraphis_secure_erase` | Irreversibly removes one leaked memory and local indexes; reports local-backup and external-copy limitations. |
| Compatibility | `engraphis_forget` | Deprecated alias for `engraphis_retire`; preserves the legacy response shape. |
| Governance | `engraphis_pin` | Prevents future automatic decay or pruning. |
| Governance | `engraphis_correct` | Replaces memory content without losing the previous version; governed provenance remains pending unless separately approved. |
| Governance | `engraphis_promote` | Widens an explicitly approved memory's scope while preserving and linking its narrower history. |
| Session | `engraphis_start_session` / `engraphis_end_session` | Starts or closes a work session. Exact retries are safe; `force_new=true` creates another session. |
| Operations | `engraphis_stats` | Returns memory counts for health checks. |
| Operations | `engraphis_check_update` | Refreshes the release cache and reports whether a newer version is available. |

All four recall tools (`engraphis_recall`, `engraphis_recall_context`,
`engraphis_recall_grounded`, and the `engraphis_answer` alias) accept `planning="off"|"auto"`
and optional `mtype_limits`, for example `{"working": 1, "semantic": 3}`. Planning is off by
default. Type limits are post-rerank maxima and can intentionally return fewer than `k`; they do not
raise a memory type's relevance. Responses include a stable `context_revision`. Planner details,
per-query rankings, type-limit drops, and fallback reasons are returned only when
`diagnostics=true`. Every planned query remains inside the caller's scope, temporal, trust, and
prompt-eligibility filters, and grounded recall still measures support against the original query.

For parameter details and return shapes, see the tool descriptions exposed by the MCP server. The
[agent connection guide](AGENT_CONNECT.md) explains local and hosted connections, and the
[Kilo Code guide](KILO_CODE_INTEGRATION.md) shows a complete editor integration.
