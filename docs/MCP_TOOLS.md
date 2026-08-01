# MCP tool reference

Engraphis exposes MCP tools for writing and recalling memory, managing history, indexing code, and
checking the local store. Start with `engraphis_recall_context` when an agent needs prompt-ready
context, and use `engraphis_remember` when it learns a durable fact.

Trust boundary: `engraphis_remember` is for a deliberate local-agent fact and defaults to
`source=agent, trusted=true`. Web, import, sync, tool, and other external source labels are
server-downgraded to untrusted even if a caller supplies `trusted=true`; use `engraphis_ingest`
for raw text, which is always untrusted. MCP recall and context are prompt-safe by default and
exclude untrusted records. The service-level `include_untrusted=True` option is reserved for
explicit inspection workflows and must not be copied into a model prompt.

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
| Governance | `engraphis_forget` | Retires a memory by closing its validity window. It does not delete history. |
| Governance | `engraphis_pin` | Prevents future automatic decay or pruning. |
| Governance | `engraphis_correct` | Replaces memory content without losing the previous version. |
| Governance | `engraphis_promote` | Widens a memory's scope while preserving and linking its narrower history. |
| Session | `engraphis_start_session` / `engraphis_end_session` | Starts or closes a work session. Exact retries are safe; `force_new=true` creates another session. |
| Operations | `engraphis_stats` | Returns memory counts for health checks. |
| Operations | `engraphis_check_update` | Refreshes the release cache and reports whether a newer version is available. |

For parameter details and return shapes, see the tool descriptions exposed by the MCP server. The
[agent connection guide](AGENT_CONNECT.md) explains local and hosted connections, and the
[Kilo Code guide](KILO_CODE_INTEGRATION.md) shows a complete editor integration.
