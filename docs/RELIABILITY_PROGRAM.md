# Engraphis reliability program: implementation and release evidence

Status: implementation candidate, not a released or deployed system. Prepared 2026-09-05.

The program strengthens coding-agent memory while preserving offline local use,
scoped temporal history, provenance, explicit erasure and server-owned entitlements.
It does not establish semantic-model superiority, 100,000-memory end-to-end agent
capacity, production readiness, or a completed user study.

## Source and delivery boundary

- Public implementation checkpoint: `c37ba0eb18408fe500cd70cd73fb5dcd7679b89c`, originally on
  `feat/context-packing-and-perf-v2`. Delivery branch: `codex/reliable-agent-memory`.
  Released v1.7.1 and the PR base are `cb03dbe104394b7760ef402a2b1917eac5e8accc`.
- Private cloud base: `8cd1f3cb819c4bfb55ac44a74aa009a8b709efac`.
- Website changes are local to the adjacent `engraphis.com` repository.
- The authorized PR review includes the four original unmerged commits, the existing gist
  documentation edit and website changes. Gist behavior and its documentation were corrected
  together. No merge, deployment, credential rotation or release is authorized by PR submission.
- Exactly four internal workers contributed bounded implementation work, followed by a separate
  four-worker review of core, interfaces, private cloud and historical local work. The parent integrated both batches.
  No descendant delegation, Orca routing or separate user-visible tasks were used.
- Current working-tree source hashes and results are recorded in [the evidence directory](evidence/reliability/).
  A base commit alone does not identify the uncommitted implementation.

## Findings register

| ID | Classification and impact | Implemented response | Evidence / remaining boundary |
|---|---|---|---|
| R01 | Reproduced: distinct facts disappeared in context packing | Remove cross-memory clause pruning; retain source-extractive summaries and complete-unit budget fallback | `tests/test_context_evidence_preservation.py`; numeric, condition, environment, title/source/pronoun bindings, multilingual and custom-counter cases. Existing memory identity/family deduplication remains. No semantic compression claim. |
| R02 | Reproduced: browsing admitted future facts and hid still-current facts | Service listing delegates canonical Store temporal/scope predicates through `core/browsing.py` | `tests/test_memory_browsing.py`; current, future, expired, late-known and historical cases |
| R03 | Reproduced: Library searched only its first fetched subset | Server text/type filtering, exact count and bounded cursor pages | 1,201-record oldest-result and complete unique traversal; browser loading/search/page recovery |
| R04 | Reproduced: auxiliary Ask failure discarded a successful answer | Independent answer and preview state, deadlines and cancellation | Browser partial-success, timeout and stale-workspace cases |
| R05 | Reproduced: concurrent engines could insert duplicate writes; native batch publication could fail after canonical commit | Embed before writer reservation; discover/resolve/persist under SQLite transaction; store-sharing native batch publication joins that transaction | Separate-instance/process and native batch rollback tests in `test_storage_concurrency_repair.py` |
| R06 | Reproduced: incomplete derived index could miss canonical truth | Durable content-free repair work, idempotent retries, canonical fallback and readiness diagnostics | Outage, restart, erase and interrupted-repair tests; external adapters need stable index identity |
| R07 | Reproduced: resolver evaluation missed false NOOP loss | Real write-path acceptance and false-NOOP/distinct-survival metrics; environment-role resolver correction | Original unit fixture: 44 pairs (38 corrections, six distinct facts). New real-write fixture: 10 pairs. Neither is independent held-out user evidence. JSON commands identify the actual dataset and retain input/source hashes. |
| R08 | Reproduced quadratic fresh-insert work and measured candidate scan/verification costs | Fresh FTS inserts avoid full mirror scans while retaining orphan repair. Bounded scans sort the scoped first batch, then use vector-first keysets. Native verification checks every expected vector and exact unique-row cardinality | Controlled 10,000-row insertion comparison: 16.65 s with the former delete forced, 2.50 s corrected. Both final 10k/100k matrices completed. Concurrency, native restart and intermediate-scope latency remain material limits; details below. |
| R09 | Reproduced diagnostic gaps | Real rolled-back write probe; JSON doctor; private tokens for new setup; installation intent profiles | Setup/update regression tests; live Windows evidence only |
| R10 | Reproduced Windows long-path object-store failure | Confined extended paths and short unique staging filenames | Cloud object-store long-path regression and full suite |
| R11 | Source-backed hosted trust risk | Separate edge assertion secret, shared Durable Object budgets, persisted revocation | Actual workerd tests; production bindings, secret rotation and geographic behavior unverified |
| R12 | Product-policy decision: readable processing needs explicit approval | Persisted local workspace policy and cloud revision-bound authority; legacy work paused | Local policy/browser tests, cloud migration/worker tests. Backend-first deployment remains required. |
| R13 | Confirmed public contract drift | Team 10 days / Pro 3; secret-free cloud product export; generated MCP schemas consumed by Pi/Prime | Contract check spans public/cloud/site; site edits unpublished |
| R14 | Structural risk: concentrated modules | Narrow browsing, vector search/repair, setup profile and processing-control modules; compatibility facades retained | Broader Store/service/renderer and migration-executor extraction deferred to separately proven changes |
| R15 | Reproduced: MCP gist reread lost selected evidence and exceeded context budgets; response caps could detach qualifiers | Gist is a compatibility alias for canonical packed context; response caps keep or omit context whole and refresh usage | MCP budget, qualifier and retrieval-preservation regressions; generated contract refreshed |
| R16 | Reproduced: the unmerged 500-memory graph window excluded older two-hop evidence | Restore the established 12,000-memory window | Graph regression includes 501 newer unrelated memories; smaller windows require independent quality evidence |
| R17 | Reproduced: Classic consent copy described processing as enabled by default | Explain explicit workspace approval and link to the selected workspace's Ledger controls | Browser and authorization-placement tests; opening controls does not enable processing |
| R18 | Reproduced: concurrent SQLite cloud policy updates accepted stale enables; encoded auth paths escaped the stricter abuse budget | Conditional revision update with checked rowcount and first-row race handling; classify the upstream-equivalent decoded path | Independent SQLite policy writers and actual workerd regression; production deployment remains unverified |

## Architecture and compatibility decisions

1. SQLite remains the canonical authority. New writes reserve its writer after embedding;
   resolution never treats an incomplete external index as proof that a fact is absent.
   Required canonical state and index repair notifications commit together.
2. Derived vectors remain repairable. `MemoryEngine.repair_vector_index(limit=100)`
   explicitly retries durable work. A stable per-index `index_identity` is required for
   trustworthy completeness. Unidentified external adapters use canonical search conservatively.
   No background repair scheduler was added.
3. Schema 17 is additive: vector generation, index targets and pending memory IDs contain
   no memory text or credentials. Existing transactional migration and verified pre-migration
   backup behavior remain intact. Dirty native startup retains a full verification/rebuild;
   only a verified unchanged startup skips replay.
4. `MemoryService.list_memories` is the shared Python/REST browsing boundary.
   Existing response fields remain, with `total_count` and `next_cursor` added.
   Text/type filters execute on the server. Cursors bind scope/query/time anchors and ordering.
   Database changes invalidate the cursor with `409 cursor_stale`; clients restart with
   the same filters. Cursors are not durable across server restarts or arbitrary worker routing.
5. Context budgets may omit evidence when a complete safe unit cannot fit. Omission is preferable
   to an altered claim. Existing chunk truncation/reason and usage omission counts remain available.
   Recall also reports `vector_search_source` and `vector_index_repairs_pending`.
   Regex token accounting is exact only for its declared tokenizer, not every model tokenizer.
6. Public MCP schemas are generated from registered tools into `docs/MCP_CONTRACT.json` and
   both shipped integrations. Smart and Classic stay distinct. Configured scope/host agent
   defaults and Prime's strict local argument checks remain adapter responsibilities.
7. Processing approval is private local client state keyed by workspace ID, separate from
   synced workspace settings and encrypted sync. Missing/corrupt legacy state is off.
   A truthy legacy environment variable cannot grant approval; a false override can deny it.
   Cloud enforces its own persisted revision at upload, job submission, execution and publication.
   Every cloud policy command requires the current revision and advances it, including an
   explicit opt-out when already off. Stale replays fail without mutation. The local client
   rechecks its current intent before and after the cloud acknowledgement; only a still-current
   opt-out may retry a revision conflict. Enabling never silently retries against a newer policy.
8. New setup configurations get a private API token so the existing audited prompt-approval
   journey works. Existing configuration is not overwritten. Doctor explains tokenless limits.
   Explicit installation capabilities survive updates.
9. No retrieval/model/backend default was changed on the strength of synthetic performance results.
   Removing evidence-losing behavior is a correctness repair, not a compression-quality win.
10. Fresh canonical inserts do not need the ordinary FTS update deletion. A one-time orphan
    inventory under the writer reservation preserves recovery for inconsistent mirrors;
    ordinary updates and explicit repairs retain replacement behavior. This removes measured
    quadratic insert work without changing retrieval ranking.
11. Bounded vector scans use the scope-driven sorted first batch, then vector-first keysets
    strictly beyond the last returned ID, all within one owned snapshot. This avoids repeated
    large sorts and retains the one-batch path for narrow scopes. It is a measured tradeoff:
    the 5% selectivity diagnostic was slower than repeated scope-driven batches.
12. Native readiness still verifies the complete content of every expected nonzero vector.
    Native IDs are unique, so equal total cardinality then rejects all extra/orphan rows.
    This removes redundant reverse scans while preserving missing, stale, zero and wrong-dimension
    rejection. It does not replace verification with a count-only check.

## Acceptance and validation

See [validation.json](evidence/reliability/validation.json) for commands, environments,
counts, skips and source hashes. The deterministic tests do not stand in for user or
paid-model evaluations. Browser approval controls use an isolated test token.

The final combined PR review suite passed **4,752 public tests**, with **37 skipped** and two warnings.
Production and test source stayed unchanged throughout that run; the before/after hashes and
skip reasons are in [the PR source receipt](evidence/reliability/public-pr-source-final.json).
The private suite passed **1,157 tests**, with **two PostgreSQL integration tests skipped**.
All seven required offline evaluation commands passed on the final public core/backend source.
Pi's 21 unit tests and actual MCP restart journey passed; Prime passed 136 tests with one skip.
The reviewed UI passed nine focused Chromium scenarios; the website passed 24 unit tests and
11 Chromium/axe scenarios. The edge passed 13 tests, including four actual workerd scenarios.
These focused results overlap other gates and are not a unique-test total.

Earlier 4,736-public/1,155-private implementation checkpoints remain recorded separately.
The first combined PR run reproduced two obsolete assertions about automatic processing copy
and the Ledger cache version. Both failed in isolation, were corrected to enforce the current
approval contract, and passed before the clean complete rerun. The review also corrected two
misleading consent-error messages. [The review receipt](evidence/reliability/pr-review.json)
records the findings, historical-branch/stash reconciliation and remaining boundaries.

The [paid evaluation proposal](PAID_EVALUATION_PROPOSAL.md) specifies a 720-call matrix and a
proposed $31 API limit. Its five-arm runner, frozen input selection and exact execution binding
remain prerequisites; no paid run is authorized or recorded by this implementation.

Current local environment: Windows 11 build 26100, Python 3.12.10, SQLite 3.49.1,
NumPy 2.4.5, FastAPI 0.141.1, MCP 1.29.0, Pydantic 2.13.4.
Native SQLite-vector checks use an isolated sqlite-vec 0.1.9 installation.
Tests force `ENGRAPHIS_EXTRACTOR=none`.

Required local commands:
```powershell
ruff check .
pyright
python scripts/check_commercial_manifest.py --website-root ../engraphis.com --cloud-contract <export.json>
python scripts/export_mcp_contract.py --check
python scripts/externalize_dashboard_assets.py
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.ablation
python -m eval.reinforcement
python -m eval.adversarial_memory_security
python -m eval.grounded
python -m eval.code_arm
```

Run Pi and Prime tests from their own integration directories; their Python test
package names otherwise collide with the root suite. Browser CI installs Chromium,
Firefox and WebKit. The full supported Python/platform CI matrix, remote PR checks,
PostgreSQL integration, Docker smoke and production restore remain separate evidence.

## Measured storage scale

[The complete scale report](evidence/reliability/vector-scale-summary-20260905.md) and its
checksummed JSON artifacts include all 12 final backend/size/concurrency cells, mixed writes,
reopen/rebuild checks, hardware, exact commands, source hashes and retained incomplete runs.
Both final backends returned matching result IDs for identical synthetic inputs.

At 100,000 total records, with 25% eligible for each scoped search:

| Backend | Median search, 1 worker | 4 workers | 16 workers | Reopen |
|---|---:|---:|---:|---:|
| NumPy | 0.496 s | 2.341 s | 8.633 s | 0.014 s |
| sqlite-vec 0.1.9 | 0.357 s | 1.488 s | 5.296 s | 35.132 s |

These measurements cover the file-backed index and canonical storage, with precomputed
256-dimensional vectors. They exclude embedding, extraction, resolution, full hybrid recall,
context packing and agent task execution. Reopen uses a new connection with the operating-system
disk cache still warm. Search latency includes shared-connection lock waits; throughput also
includes executor queue time. Thirty-two samples per cell are descriptive, not tail-latency
confidence bounds. Native 100k population took 104.00 s and full mirror rebuild took 61.31 s.

The first-sorted-batch method measured 235.48 ms at 5% selectivity versus 132.03 ms for repeated
scope-driven batches in the diagnostic. Broader scopes improved substantially. The slow NumPy
baseline is the initial bounded-scan implementation candidate, not released Engraphis; the base
version materialized its scoped matrix once. No released-product speedup is inferred from that
comparison. The separate FTS and native-verification experiments are controlled method ablations.

The 100k concurrent agent operating goal remains unproven. Shared-connection serialization,
native verification/startup cost, intermediate scope sizes and complete engine workloads are
the next performance priorities. No backend, ranking or grounding default was changed.

## Dependency-ordered remaining backlog and exit conditions

| Order | Work | Acceptance / dependency |
|---|---|---|
| 1 | Review the implementation and exact final dependency locks, then run remote supported-platform gates | No attributable test/type/lint failures; preserve unrelated edits; identify every candidate revision |
| 2 | Validate a staged backend-first processing migration and edge configuration | Old workers stopped; legacy schedules/jobs paused; no unconfirmed upload; no legacy-secret fallback; revocation persists; restore drill succeeds |
| 3 | Repeat and extend the completed storage/index measurements | Address shared-connection waits, native verification cost and scope tradeoffs; use independent repetitions and representative hardware; preserve complete paired reports |
| 4 | Independent held-out coding corpus and matched five-arm model acceptance | Human-reviewed task IDs/data hashes frozen before tuning; no-memory/full-history/lexical/dense/hybrid use matched budgets; approved cost proposal before paid calls |
| 5 | Full 100,000-memory mixed engine/agent workload, imports and recovery benchmark | Declare latency/error budgets, exercise real remember/recall at 1/4/16 concurrency, and preserve correctness; no extrapolation from index-only timings. One million remains the unrun stress track. |
| 6 | Further migration executor, request/view-state and repository extraction | One subsystem per change; compatibility and historical reads unchanged; include interruption/rollback tests |
| 7 | Target-user onboarding study | Measure install-to-useful-recall and successful corrections with consent; no fabricated adoption or usability numbers |
| 8 | Bounded pilot release | Exact revisions pass staged restore/auth/privacy gates; owner and alerts assigned; explicit release/deployment approval |
| 9 | Observe, then retire duplicated surfaces | Content-free cross-session recall/correction/support metrics; usage and compatibility requirements satisfied before removal |

Milestone 1's reproduced defects and much of milestones 2, 4 and 5 now have implementations
and local regression evidence. Milestone 3 has stronger offline tests and completed 10k/100k
storage/index measurements; independent semantic/task evidence remains pending. Milestone 6 is a prepared
release process, not an executed release. None of those operational gates is waived.

## Migration, recovery and rollout

Public schema 16 → 17:
- Stop writers and take an independently restorable backup before a pilot. Ordinary startup
  retains the existing versioned migration/backup checks; a separate migration executor is
  future work.
- Verify the `*.pre-migration-v17.bak` artifact and schema/version/integrity checks on a disposable
  restored copy. Preserve original IDs, history, provenance and visibility.
- Do not point old binaries at a migrated live database as a rollback strategy. Restore to a
  separate path with writers stopped; reconcile later writes and erasure tombstones through
  governed recovery before cutover. Restoration is a data operation requiring operator approval.
- Pending derived-index work is recoverable without changing canonical memories.
  Run bounded repair until pending reaches zero; canonical fallback protects reads meanwhile.

Cloud migration `0a5c9e2b7d31` and release order:
- Follow the private `docs/PROCESSING_AUTHORIZATION_ROLLOUT.md`.
- Stop old compute/worker binaries, back up, apply migration, and start only compatible services.
- Configure independent edge assertion credentials plus revocation/abuse Durable Object bindings.
  Preparing source does not rotate secrets or install production bindings.
- Ship the local client, notify existing users that uploads are paused, and obtain explicit
  workspace approval. Verify opt-out/re-enable/new-snapshot sequences with real staging services.
- If cloud acknowledgement is unavailable, local opt-out stops new client uploads immediately;
  existing submitted work may continue until the cloud receives revocation. Show pending state.
- An expired client cannot refresh credentials to acknowledge cloud opt-out. Its local control
  remains off with confirmation pending. Independent cloud entitlement checks reject new input,
  exclude schedules, prevent queued snapshot reads and suppress results if entitlement expires
  during computation; those cases have local regression evidence.
- Do not restore old services that infer consent from connection while accepting managed input.

Pilot rollback triggers: any lost distinct write, scope/temporal leakage, unexpected readable
upload, stale approval resurrection, erased-record resurrection, revoked access acceptance,
migration integrity failure, or incomplete required release evidence. Pause the affected feature,
preserve audit evidence, restore only through the approved recovery path, and retain local use.
