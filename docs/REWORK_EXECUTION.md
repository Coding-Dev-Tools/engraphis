# Reliability rework: implementation and release evidence

This is the execution register for the approved memory-first reliability program.
It records engineering work and remaining gates separately. It is not a release,
capacity, independent-quality, or production-restoration claim.

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
| 6 | Finish installed-product and human UI acceptance. | Windows/macOS/Linux; Chromium plus Firefox/WebKit correction/history; screen-reader, keyboard, reduced motion and reflow checks. Twelve target developers, at least ten unassisted journeys, and investigation of every scope error. | Preserve drafts and existing paths until replacement parity; ambiguous saves block progression. |
| 7 | Complete backend-first hosted cutover and recovery proof. | Exact client/control/compute/worker/edge/schema/grants; old/new policy combinations; revocation persistence; erasure/member/token/opt-out/entitlement reconciliation while fenced; verified alerts and ownership. | Keep submissions and restored services fenced on missing policy, reconciliation or operational evidence. |
| 8 | Run the consented bounded pilot, then simplify duplicate surfaces. | Five developers, one clean week before twenty repositories, then two weeks observation. Metrics local by default; external collection requires opt-in. | Stop for lost evidence, leakage, resurrection, unexpected processing, revoked access or migration-integrity failure. |

Paid calls, external participants, publication, deployment, merges and credential
changes have not been performed by these local changes. Ordinary local engineering
and verification are already authorized; missing hardware and independent evidence
are execution constraints, not reasons to claim completion or invent results.

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
