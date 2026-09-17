# Local user journey evidence

`eval.user_journeys` is a bounded, executable regression runner for seven
coding-agent memory journeys. It uses real v2 service, engine, document-import,
code-index, repair, MCP, session, and erasure APIs against disposable local
stores. It does not call a paid model provider, contact a network service, or
use a production database.

Run all journeys locally:

```text
python -m eval.user_journeys
```

Run one journey, or select several, with repeated `--journey` arguments:

```text
python -m eval.user_journeys --journey code_memory_bridge
python -m eval.user_journeys --journey mixed_document_import --journey erase_restart
```

The process prints one JSON envelope to stdout. `run_journeys()` returns the
same structure to Python callers, and `verify_envelope()` checks its canonical
SHA-256 payload and envelope digests. The envelope contains journey names,
boolean checks, integer counts, diagnostic duration, and a safe exception type
on failure. It excludes memory IDs, memory or document text, database paths,
source paths, and exception messages.

## Journey coverage

| ID | Runtime path | Evidence recorded |
| --- | --- | --- |
| `mixed_document_import` | `DocumentImporter.preview()` and `import_scan()` with Markdown, text, and JSON records, a repeat import, and one revised document | Format dispatch, completed import, repeat skips, temporal revision, source-neutral receipts, and live-document count |
| `mcp_context_budget` | Direct calls to the Classic and Smart MCP wrappers bound to one disposable `MemoryService`; the NumPy-only Python 3.9 core job uses an explicit service-level fallback because MCP is an optional Python 3.10+ extra | JSON serialization, context-budget accounting, contract parity, source counts, and dynamically discovered tool counts when MCP is installed; the fallback records the dependency gate and verifies the same core budget contract |
| `session_handoff` | `MemoryService.start_session()`, `end_session()`, `remember()`, and `recall()` | Exact active-session reuse, new session after end, bootstrap handoff, and next-session workspace context |
| `code_memory_bridge` | `index_repo()` and `search_code()` over a temporary Python repository, followed by code-profile recall | Indexed symbols/edges, symbol-to-memory linkage, recalled memory bridge, and observed code retrieval arm |
| `concurrent_corrections` | Two independent `MemoryEngine` connections with synchronized embedding and the same `subject_key`/`claim_kind` | Two committed corrections, one live claim, and preserved temporal history |
| `index_repair` | A local external-index adapter whose publication fails once, then `MemoryEngine.repair_vector_index()` | Durable repair queue creation, canonical replay, acknowledged queue, and indexed rows |
| `erase_restart` | File-backed `MemoryService.secure_erase(confirmed=True)` followed by close and a fresh open | Confirmed erasure, tombstone, no record before close, and no record after restart |

The counts in a result are observations from those runtime actions. They are
not test-case counts and must not be reported as unit-test coverage or as a
throughput sample. A failed check is a regression signal for the named path;
the runner does not substitute a partial pass or silently skip an unavailable
API.

## Claim boundary

Each invocation is one bounded local run. `duration_ms` is a diagnostic wall
clock measurement that includes local setup and teardown for the journey. It
does not establish latency percentiles, throughput, capacity, multi-agent
scale, production reliability, or a comparison with another product. The
`index_repair` adapter is a small in-process test double used to exercise the
real durable repair protocol; it is not evidence about a hosted vector
provider. The code journey uses a compact temporary fixture, and the
concurrency journey uses two local connections; neither establishes broad
repository or deployment diversity.

This runner is separate from the five-arm acceptance campaign and from
external datasets. It is functional evidence for user-visible continuity and
maintenance behavior. It does not claim independent authorship, real-customer
workloads, human validation, or model quality. Repeat runs, external datasets,
and any production-like embedding or capacity campaign require their own
frozen protocol and reporting boundary.

The retained current invocation passed all seven journeys:
[public artifact](benchmark-evidence/user-journeys-20260917-review-fix.json), with its adjacent checksum.
Reproduce the export with
`python -m scripts.export_user_journey_evidence --output <new-artifact.json>`.

The retained artifact is the full MCP-enabled path.  In the Python 3.9 core-floor
environment, `mcp_context_budget` cannot load the optional MCP package; the runner
does not silently claim transport or tool-discovery coverage there.  It records the
gate and verifies the dependency-light service recall budget instead.
