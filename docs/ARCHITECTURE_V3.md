# Engraphis v3 architecture

This document is the design outline for the repo-graph, intent-native memory, resource-ingestion,
retention-supervision, and privacy-receipt additions introduced in the schema-3 era (the
current schema version is 16).

```mermaid
flowchart LR
    Agent["Agent / host LLM"] --> Intent["remember · link · recall_context (compact) · recall"]
    CLI["engraphis-graph CLI"] --> Service["MemoryService"]
    MCP["Smart MCP (9 tools) / Classic MCP (35 tools)"] --> Service
    HTTP["Dashboard + read-only graph HTTP"] --> Service
    Import["Local resources / PostgreSQL catalog"] --> Extractors["Optional local extractors"]
    Extractors --> Service

    Service --> Engine["MemoryEngine"]
    Engine --> Recall["Hybrid recall\nvector + lexical + layered graph"]
    Engine --> Resolver["ADD · NOOP · INVALIDATE"]
    Engine --> Code["Incremental code index\nAST + regex fallback"]
    Engine --> Retention["Optional retention supervisor"]

    Recall --> SQLite[("One SQLite file")]
    Resolver --> SQLite
    Code --> SQLite
    Retention --> SQLite

    SQLite --> Layers["Temporal · Entity · Causal · Semantic"]
    SQLite --> Bridge["Code ↔ Memory links"]
    SQLite --> Receipts["Hashed receipt chain"]
    SQLite --> History["Bi-temporal history"]
```

## Design outline

1. **One service contract.** MCP, REST, the dashboard, the CLI, and the read-only server call
   `MemoryService`; none implements independent memory semantics.
2. **One database, logical graph overlays.** `edges`, `mem_links`, and `code_edges` carry a
   `layer` tag (`temporal`, `entity`, `causal`, or `semantic`). Filters select overlays without
   creating separate stores.
3. **Structural and experiential knowledge stay normalized.** Code symbols remain in
   `symbols`/`code_edges`; memories remain in `memories`; `code_memory_links` connects them.
   Re-indexing can replace a changed file without copying memory text or losing history.
4. **Incremental code indexing.** `code_files` records content hashes. Unchanged files are
   skipped, changed files are transactionally replaced, deleted files are removed only after a
   complete scan, and bounded/truncated scans never delete unseen state.
5. **Offline community analysis.** Weighted label propagation identifies structural
   communities without adding an igraph/Leiden dependency to the core. Degree, cross-file degree,
   god-node flags, and surprising cross-file relationships feed impact analysis.
6. **Intent-native recall.** `intent_recall` maps cognitive intents such as `explain`,
   `summarize history`, and `locate code` onto layered graph traversal plus normal vector/lexical
   recall. Existing `engraphis_remember`, `engraphis_link`, and `engraphis_recall` remain the
   canonical MCP primitives, avoiding duplicate APIs.
7. **Local resource adapters.** Text, code, HTML, and DOCX use the standard library. PDF, image
   OCR, audio/video transcription, and PostgreSQL introspection are optional backends. Missing
   tools fail actionably; nothing uploads implicitly.
8. **Advisory retention supervision.** A host or configured LLM may classify a write as
   `ephemeral`, `normal`, or `critical`. The engine clamps importance/stability and records the
   decision; it never silently discards or hard-deletes a memory.
9. **Privacy-safe receipts.** Remember/link/recall and indexing operations append content-free,
   SHA-256-chained receipts with an independently maintained local head/count anchor. Raw
   memory/query text, workspace names, IDs, and actor identities are excluded from the exported
   payload. A previously exported head/count can be supplied during verification to anchor the
   chain outside the database.
10. **Local-safe exposure.** The public dashboard is single-user and supports an optional local
    bearer token. The optional `engraphis-graph-server` exposes only read operations and refuses a
    non-loopback bind without a bearer token. Team identity, roles, and seats are hosted services.

## Schema additions

- Layer columns on `edges`, `mem_links`, and `code_edges`; durable rationale on `mem_links`.
- `code_files` for incremental indexing.
- `code_memory_links` for code/experience traversal.
- `operation_receipts` plus `receipt_chain_heads` for the privacy-safe hash chain and tail anchor.
- `symbols.docstring` for extracted documentation.

Migration is additive and idempotent. Pre-v3 edge layers are inferred exactly once; explicitly
selected layers are never reclassified when a database is reopened.

## Vector backend compatibility

`MemoryEngine.create()` and `MemoryService.create()` default to the exact NumPy index, even when
`sqlite-vec` is installed, so the default remains portable and deterministic. `sqlite-vec` and
SQLCipher load incompatible SQLite native libraries in one process: with `vector_backend="auto"`
Engraphis falls back to NumPy; an explicit `vector_backend="sqlite-vec"` fails with an actionable
error. Packaged dashboard, REST, and MCP entrypoints use the `auto` setting, so an installed
`vector` extra is selected without changing the deterministic constructor contract. Run accelerated
search in a fresh process when using the SQLCipher extra.

The active vector space also needs a durable, secret-free identity. Sentence Transformers use the
resolved Hub commit or a manifest of local artifacts; an unresolved mutable model leaves persistent
vector recall gated rather than mixing embeddings. `ApiEmbedder` remains valid for ephemeral calls
without identity, but persistent use requires an operator/provider `space_version`. Its provider
root and `/v1` base forms normalize to one `/v1/embeddings` endpoint.

## Query planning

Recall defaults to the `balanced` retrieval profile and `planning="off"`. The explicit `fast`
profile keeps vector + lexical retrieval while skipping graph traversal for small or
latency-sensitive vaults. Opt-in
`planning="auto"` keeps the original query, admits at most two deterministic or injected query
routes, and fuses them before reranking against the original query. `mtype_limits`, when provided,
are post-rerank maximum counts rather than relevance boosts. Every packed response has a stable
`context_revision` derived from the token-counter identity and ordered packed excerpts, so a host
can retain an unchanged prompt prefix. Planner output, per-query rankings, cap drops, and fallback
reasons appear only with `diagnostics=True`.

The offline planner is the default injected implementation. An application can opt into an LLM
planner without coupling `core/` to a provider:

```python
from engraphis.backends.query_planner import LLMQueryPlanner
from engraphis.core.engine import MemoryEngine

engine = MemoryEngine.create(
    "engraphis.db",
    query_planner=LLMQueryPlanner(my_llm),
)
result = engine.recall(
    "why does ReleaseGate depend on AuditLog?",
    workspace_id="ws_...",
    planning="auto",
    mtype_limits={"working": 1, "semantic": 3},
)
```

Planner failures and provider deadlines fail open to the original single-query plan. Planned recall
remains opt-in until the checked-in budget, safety, and official LongMemEval-V2 gates justify a
default change.

## Repo workflow

```bash
engraphis-graph index -w acme -r api --root .
engraphis-graph search -w acme -r api "UserService"
engraphis-graph query -w acme -r api "where is token rotation implemented?"
engraphis-graph explain -w acme -r api "why does deploy depend on approval?"
engraphis-graph path -w acme -r api UserService DatabasePool
engraphis-graph impact -w acme -r api --root . --git-range origin/main...HEAD
engraphis-graph prs -w acme -r api --base main --head HEAD
engraphis-graph export -w acme -r api -o engraphis-graph-out
```

`export` writes `graph.json`, `graph.html`, and `GRAPH_REPORT.md`. A local union merge driver can
be installed with:

```bash
engraphis-graph install-merge-driver --root .
```

## Optional resource tools

```bash
pip install "engraphis[documents]"      # PDF + image OCR bindings
pip install "engraphis[transcription]"  # faster-whisper
pip install "engraphis[postgres]"       # psycopg
```

For transcription, set `ENGRAPHIS_WHISPER_MODEL` to a local model path or an explicitly chosen
model name. For PostgreSQL CLI ingestion, set `ENGRAPHIS_POSTGRES_DSN`; the value is never stored.
