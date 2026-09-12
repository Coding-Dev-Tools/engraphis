# Reliability rework: implementation and release evidence

This is the execution register for the approved memory-first reliability program.
It records engineering work and remaining gates separately. It is not a release,
capacity, independent-quality, or production-restoration claim.

The September 11 release-readiness implementation continues this register from public
`cd71929344c0c63070954e9963b7bc5c45ebfa30`. The full-product gate inventory, evidence
schema, ownership and publication/rollback procedure are in
[RELEASE_READINESS.md](RELEASE_READINESS.md). The earlier identities below are historical
baselines. Current results belong to the candidate ledger, not to those older revisions.

The implementation starts from public source
`8d9770d6676c7c19aabe21c4d0e6bcebff9a4d59` (source version 1.7.2).
The separately verified published release was 1.7.1 on September 6, 2026.
Private cloud starts from `25b5c8e8211d6daf6972792c562742268523edc3`;
website work starts from `b67edda97cee437cff2d900e1a0786b9fc87677b`.
Final validation is bound to source and artifact identities in the delivery receipt.
The original checkout's active graph/layout changes remain separate.

## Findings register

| Priority / classification | Failure and consequence | Implemented response and acceptance evidence | Remaining limit |
| --- | --- | --- | --- |
| P1 reproduced defect | Delayed sync publication restored an erased or outdated external vector. | `core/vector_repair.py` publishes current canonical state under the writer reservation and acknowledges the applied generation. `tests/test_sync_index_repair.py` covers delayed publication, erasure, newer updates, provider failures and native rollback. | Arbitrary synchronous providers can still occupy the writer while publishing. |
| P1 reproduced defect | A blocked vector update prevented later queued erasures from being repaired. | Repair traversal prioritizes canonical deletions and makes bounded progress past deferred updates. Focused regressions cover a one-operation budget, blocked embedding spaces, provider failures and later erasure. | A provider that cannot delete still leaves durable repair debt; deletion is not falsely acknowledged. |
| P2 reproduced contention | Finding one erasure behind 1,000 updates acquired 1,002 writer reservations. | [Repair discovery](INDEX_REPAIR_MAINTENANCE.md) classifies paged canonical headers before reserving the writer, then revalidates inside it. Tests check independent writer progress, stale hints, and immediate stopping after the attempt budget. | Total discovery, repeated scans, failed-deletion fairness and provider latency remain separate scheduling work. |
| P1 reproduced defect | Separate engines accepted multiple governed successors of one record. | `core/mutations.py` validates prepared versions and source claims inside the transaction; schema 18 retains content-free command receipts. `tests/test_governed_concurrency.py` exercises corrections, approvals, promotions and merges through independent engines and spawned processes. | Receipts coordinate processes sharing the canonical database; they are not a new distributed multi-database transaction protocol. |
| P2 reproduced defect | A completed promotion or merge could not be retried after its session closed. | Existing receipts replay before transient active-session and embedding requirements. Tests reopen the engine, disable embedding, replay the result and reject removed successors; new writes still recheck session activity under the writer. | Changed requests are new operations and remain subject to current session and source guards. |
| P1 reproduced defect | A shared claim key or consolidation lineage collapsed distinct repository facts. | Packing deduplicates repeated canonical IDs, preserves full ownership attribution and budgets it. `tests/test_context_scope_grounding.py` retains distinct repositories, values, conditions and title-bound subjects. | Stronger semantic compression remains an experiment; no ranking default changed. |
| P1 reproduced defect | Synthesis shortened qualified evidence while reporting a grounded answer. | Complete cited source units must survive synthesis; otherwise the full extractive answer is returned. The same regression module checks exceptions, negation, bindings and multilingual conditions. | Answer coverage defaults to `unknown`; valid citations do not establish completeness. |
| P2 reproduced defect | Content saved while the label request failed, leaving an ambiguous partial edit. | One revision operation covers content, title, type and importance with an expected version, operation ID, provenance and history. `tests/test_memory_revisions.py` covers lost responses, interrupted commits, retries, conflicts and REST/MCP behavior. | Legacy metadata-update entrypoints retain their in-place compatibility behavior. |
| P2 reproduced defect | Unrelated audit activity invalidated Library pages; embedding preparation held the writer. | Portable cursor v2 uses scope/type revisions and frozen temporal anchors. Revision/title preparation precedes the writer reservation. Tests traverse 1,201 records amid unrelated activity and validate stale edits after preparation. | Relevant edits deliberately require a typed refresh. History traversal remains in the service facade. |
| P2 reproduced defect | Repository-filtered history omitted a workspace-wide successor after promotion. | Record history now includes broader-scope lineage while retaining exact root ownership, same-repository narrow records, caller/session authorization and frozen pagination. `tests/test_history_scope.py` covers service and REST journeys and hostile cross-scope pointers. | This adds no user-scope write or promotion capability. |
| P2 reproduced defect | Hidden Explore renderers kept running, including late responses after navigation. | A lifecycle adapter uses existing renderer pause/resume/destroy APIs; browser regressions cover hidden views, replacements and retained view state. | Every-node finite layout preparation cannot pause mid-job through its current public API. Renderer algorithms and active graph WIP are preserved. |
| P2 source-backed limitation | Large modules mix requests, state, mutations and publication. | Narrow mutation, browsing, read-snapshot, diagnostics, request, revision, history and lifecycle modules sit behind existing public facades. | Further extraction follows behavior and measured contention; module size alone does not justify replacement. |
| P2 source-backed limitation | Ordinary startup repeated completed graph transformations. | Durable versioned execution markers commit with each transformation and the verified migration backup path. `tests/test_startup_transform_gates.py` covers interruption, rollback and exactly-once completion on reopen. | Index readiness/rebuild work is separate and must still be measured at operating scale. |
| P2 reproduced workflow defect | Home treated suggestions as unresolved decisions; Ask conflated support and coverage. | Home reads a bounded actionable review inbox; Ask separates answer/preview errors, retries, cancellation and unknown coverage. Browser and `test_workflow_diagnostics.py` regressions check observed state. | Browser cancellation stops waiting and late UI application; it does not promise server-side computation cancellation. |
| P2 source-backed hosted limitation | General traffic exhaustion could obstruct logout; shared networks needed account-level budgets. | Private edge has a separate bounded logout path and verified principal/organization budgets. Actual Workers-runtime tests cover copied-cookie revocation, forged headers and shared-network cases. | Deployed bindings, rotation, operational limits and alert delivery require release-specific verification. |
| P1 reproduced hosted defect | Reapproval at the same data generation could retain an old snapshot or stale analytics result. | Private snapshot replacement accepts only a previously retired policy revision; same-policy collisions still fail. Derived analytics discovery is replaced transactionally, and old-policy/orphan results are rejected before object reads. | This is API/worker fixture and CI evidence; hosted cutover and restore evidence remain separate gates. |
| Release / missing evidence | Restoration, 100k capacity, independent coding task quality and published claims were not established by local fixtures. | Strict corpus/capacity validation, explicit paid proposals, a pinned website contract and private restore-evidence checks prevent incomplete evidence being promoted to a release claim. | Real hardware runs, independent authors, human journeys, hosted restore/cutover and publication remain open. |

## Architecture and compatibility decisions

1. **SQLite remains authoritative.** Prepare expensive work, reserve the writer,
   revalidate current truth, apply memory/provenance/lineage/receipt together, then
   repair derived state from canonical records. External publication cannot use a
   captured vector to override a newer generation or erasure.
2. **Public facades remain stable.** Existing Python, REST and MCP signatures keep
   their fields and delegate to the guarded operations. The additive revision
   endpoint does not confer human-approval authority on agents.
3. **Edits have an explicit identity.** `memory-command/v1` uses a portable `mv1:`
   version and workspace-bound operation ID. Identical retries return the receipt;
   different reuse and stale versions produce typed conflicts. Erased or retired
   results cannot be recreated by replaying a receipt.
4. **Reads have bounded isolation where supported.** File-backed Library browsing
   borrows up to four live read-only snapshots with a five-second default lease.
   Pool exhaustion and deadline expiry have safe retryable responses. In-memory,
   caller-owned and unsupported injected connections retain compatible reads;
   ordinary recall is not claimed to use this pool.
5. **Evidence correctness precedes compression.** Distinct records remain separate
   unless duplication is demonstrated. Synthesis is conservative; task-level
   completeness is a separate field. Numerical retrieval defaults stay unchanged.
6. **Memory tasks organize the UI.** Persistent project selection, a single revision,
   shared inspector/history and independent request states support the first useful
   journey. Existing JavaScript, packaged assets, themes, CSP and renderers remain.
7. **Diagnostics expose observations, not private content.** Diagnostics v1 has
   bounded counts and timings. Unobserved scope/time/trust exclusions are `null`.
   `/api/build` identifies installed source, schema and capabilities without paths
   or credentials; its hash describes package files at the first build-info request.

## Dependency-ordered remaining execution

| Order | Work | Acceptance / dependency | Rollback condition |
| --- | --- | --- | --- |
| 1 | Finish applicable local and CI gates for this candidate; review the complete attributable diff. | Current source identities, installed artifacts, full offline suite, browser suites, contracts and private runtime tests. | Any integrity, scope, trust or migration regression blocks delivery. |
| 2 | Extend preparation boundaries and extract lineage/repository operations incrementally. | Imports, consolidation, sync and historical reads preserve provenance; filesystem/model preparation does not retain a writer reservation. | Preserve the previous facade behavior until each migrated path passes interruption and compatibility checks. |
| 3 | Add optional coordinated repair scheduling with deadlines, backoff and backlog age. | Idempotent generations; no resurrection after erasure; bounded provider calls and interruption recovery. Offline library requires no background service. | Disable scheduling on missed deadlines or growing backlog; retain canonical fallback and durable queue. |
| 4 | Complete independent quality and capacity evidence before optimization. | [Corpus protocol](CODING_ACCEPTANCE_CORPUS.md), [capacity protocol](ENGINE_CAPACITY_PROTOCOL.md), exact approved [paid matrix](PAID_EVALUATION_PROPOSAL.md). Family-separated 400 tasks, both machines, all 48 cells and paired uncertainty are required. | Incomplete evidence or failure of the one-percentage-point non-inferiority gate retains defaults. |
| 5 | Optimize measured contention/startup/vector/embedding/graph bottlenecks, one at a time. | Compare matched complete-engine workloads, including queueing, actual semantic embeddings and real backends; account for failures and resource usage. | Revert an algorithm/default change that violates correctness or declared quality limits. |
| 6 | Finish installed-product and human UI acceptance. | Windows/macOS/Linux; Chromium plus Firefox/WebKit correction/history; screen-reader, keyboard, reduced motion and reflow checks. At least 11 of 12 first-time developers complete install to useful cross-session recall unaided within ten minutes; investigate every scope error. | Preserve drafts and existing paths until replacement parity; ambiguous saves block progression. |
| 7 | Complete backend-first hosted cutover and recovery proof. | Exact client/control/compute/worker/edge/schema/grants; old/new policy combinations; revocation persistence; erasure/member/token/opt-out/entitlement reconciliation while fenced; verified alerts and ownership. | Keep submissions and restored services fenced on missing policy, reconciliation or operational evidence. |
| 8 | Run the consented bounded pilot, then simplify duplicate surfaces. | Five developers, one clean week before twenty repositories, then two weeks observation. Metrics local by default; external collection requires opt-in. | Stop for lost evidence, leakage, resurrection, unexpected processing, revoked access or migration-integrity failure. |

Paid calls, external participants, publication, deployment, merges and credential
changes have not been performed by these local changes. Ordinary local engineering
and verification are already authorized; missing hardware and independent evidence
are execution constraints, not reasons to claim completion or invent results.

## Resource-import transaction follow-up

Legacy folder/upload imports now isolate each file, including its chunks, FTS,
canonical vectors, transactional index rows and receipts, before returning a
recoverable per-file error. An optional fact-derivation failure rolls back its
complete derived prefix while retaining the successful source import. Unexpected
failures and final commit failures still abort the service-owned batch; a caller's
preceding transaction remains caller-owned.

The additional counterexamples at `dc4382d1` included an FTS failure leaving three
canonical records despite a two-import/one-error report, and a second-chunk
embedding failure leaving an unreported first chunk. Fourteen new regression
cases reproduced these integrity failures against that unchanged dependency
checkout. Review also reproduced failed savepoint rollback/release being treated
as a recoverable file error. Savepoint settlement now raises `SavepointError` and
aborts the enclosing operation, including optional conflict repair.

This follow-up changes no schema, public signature, response field, ranking or
approval rule. There is no data migration. Reverting the patch restores the prior
partial-write risk; it does not reconcile fragments left by earlier imports.
Do not delete suspected fragments automatically: retain provenance and inspect
the applicable import report before governed correction or erasure.

Preparation remains the next dependency. Folder enumeration, resource parsing,
chunking, embedding and explicitly enabled derivation still occur inside the
legacy batch writer. Move them through immutable prepared commands, with current
workspace/embedding validation and explicit post-commit index publication, in a
separate change. Preserve the existing caller-owned separate-index rejection until
that publication contract exists. These integrity tests establish no throughput,
100k capacity or production recovery claim.

## Schema 17 to 18 and recovery

Schema 18 adds memory-command receipts/source claims, portable browsing revisions
and completed-transformation markers. The existing backup verification and writer
reservation protect migration. Interrupted transformations roll back their data
and marker together; a reopen safely retries unfinished work. Completed graph
transformations do not repeat during ordinary startup.

Before a release upgrade, identify the exact database, encryption connector and
artifact; stop writers; retain the verified pre-migration backup and its hash;
exercise the upgrade on a disposable copy. Check schema/integrity, scope/time
behavior, erasure markers, record lineage and external repair state. Start only
compatible clients after the candidate passes these checks.

Never downgrade a migrated live database in place. If recovery needs an earlier
artifact, restore the verified backup into a separate fenced location and reconcile
all post-backup changes before serving it. This includes explicit erasures and,
for hosted services, membership changes, token revocations, opt-outs and entitlement
changes. Rebuild derived indexes only from the reconciled canonical state. A
backup integrity check or a matching evidence-file hash alone is not a restore drill.

This candidate does not claim a successful production restoration. The private
restore-release checker validates a supplied, hash-bound evidence package and
retains the release fence until the required categories are represented; it does
not execute those operational reconciliations on production data.

## September 11 release candidate work

The original 16-file working tree was inventoried before selecting release work.
Thirteen nongraph changes were copied into an isolated candidate; the three active
graph files remain in their original checkout. The candidate removes an ineffective
per-write regex-list preparation change and retains bounded lookup batching with
scope/time regression coverage. Original user edits were not rewritten.

| Work | Implemented evidence | Remaining acceptance |
| --- | --- | --- |
| Durable SQLite writers | WAL/FULL is the default, with an explicit balanced policy and effective diagnostics. `tests/test_sqlite_durability.py` covers startup enforcement, injected/read-only connectors, interrupted writes and `SQLITE_FULL` rollback/retry. | Physical power failure and the complete operational restore contract remain unverified. |
| Consolidation evidence | Visibility lookups are chunked at the Store's 500-ID bound. Scope, historical visibility, missing evidence and failed batches are covered by `tests/test_consolidate_recall.py`. | A best-effort lookup failure hides citations; it does not erase canonical source records. |
| Pi dependency repair | Hono resolves to patched 4.13.7. Pi verification, type checking, packaging and genuine MCP integration passed locally; MCP remains below major version 2. | Final candidate CI and release artifact checks are indexed separately. |
| Complete-engine measurement | Serializable factory configuration supports real files, pinned local semantic models, exact NumPy/sqlite-vec backends and rerankers. Opt-in recall phases separate embedding, retrieval, ranking and packing. | Full writer occupancy, production-load calibration and measured optimization remain open. |
| Capacity acceptance | Lifecycle RSS includes startup, backlog is sampled and recomputed, and the complete matrix validator enforces prebound hosts, WAL/FULL, all scheduled outcomes, RAM and the required 100k latency limits. | No primary 48-cell matrix was executed. The separate 16 GiB reference host remains necessary. |
| Installed journeys | A packaged stdlib runner performs actual MCP/HTTP writes, restart recall, correction and historical reads. PR CI and release jobs cover Windows, macOS and Linux, with artifact/dependency identities retained. | Cached Windows source semantic startup passed in four fresh processes, taking 20-23 seconds. This is not semantic qualification of all installed platforms. |
| Evidence and publication | Candidate ledger validation checks identities, hashes, dependencies, outcomes and selected evaluation booleans. All four publication/repair writes require [owner qualification](RELEASE_QUALIFICATION.md). | Owner-protected environment/authority setup, final approval and all missing mandatory evidence remain open. |
| Public claims | Fresh [offline fixture evidence](benchmark-evidence/offline-fixtures-v2.json) reproduces retained public aggregates and binds the current engine/eval source. Historical v1 evidence is preserved. | Planner variants still require successful promotion gates; no retrieval default or leadership claim is promoted. |
| Website contract | Active commercial/MCP/install guidance is generated and checked against a selected shipped public contract in the website candidate. | The live portal, authenticated provider journeys and combined deployed identities require attended acceptance. |

Final commits, distributions, dependency inventories, raw execution results and gate
owners belong to the private `candidate-ledger.json` package described in
[RELEASE_READINESS.md](RELEASE_READINESS.md). A passing subset is retained without
turning an incomplete mandatory gate into PASS. The first integrated local run was
interrupted by host disk exhaustion; its failure log is retained separately from
subsequent executions. Disposable tests do not certify recovery of customer data.

The next release-critical dependencies are sufficient reference-host storage, both
capacity hosts, the independently authored executable corpus, controlled staging
mailboxes/provider journeys, a reconciled restore drill, first-time developer
acceptance and the three-week bounded pilot. Existing private Cloud checks and
local website checks are not substituted for those observations. Paid evaluation
still requires a fresh approved budget before any call.
