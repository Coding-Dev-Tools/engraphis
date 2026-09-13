# Candidate evidence and release decisions

[REWORK_EXECUTION.md](REWORK_EXECUTION.md) remains the execution register. Each frozen
candidate has one private `candidate-ledger.json` alongside its immutable evidence.
Use `scripts/check_release_readiness.py` to validate this package. A complete local
checklist does not establish deployment, independent acceptance or human identity.

Release readiness and competitive leadership are separate decisions. An unresolved
mandatory release gate blocks release. Leadership evidence is required before making
comparative leadership claims, and does not prevent release of an otherwise qualified
product. Neither decision authorizes publication or spending.

## Freeze and identity

Preserve unrelated work. Review and commit the selected engine and website changes;
record the exact Cloud/Team/edge source revisions and their compatible public-engine
pin. Rebuild from those Git objects. Keep wheel, source archive, dependency inventories,
container/Worker archives and their hashes. Do not promote a checkout-built diagnostic
wheel or an older successful CI run to evidence for a changed candidate.

The ledger schema is `engraphis-product-readiness/v1`. Its `components` object contains
exactly `engine`, `cloud`, `team`, `edge`, and `website`. Each identity records its full
Git `commit`, relative `artifact_path`, and `artifact_sha256`; the checker verifies the
referenced component bytes. Attach compatible schema, contract, dependency and
runtime identities as additional fields. Unknown artifact identity stays null. The
candidate ID is SHA-256 of sorted compact JSON of the entire components object.
Changing any component invalidates the previous candidate's receipts.

Keep evidence outside the checkout to permit clean-tree verification. Public-engine
checks use the real release-artifact verifier and reproducibility workflow. Private
images and edge bundles retain the private exact-artifact signing and acceptance
procedure. This package indexes those proofs; it does not replace their verifiers.

## Gate inventory and ownership

Every gate requires `id`, `status` (`PASS`, `FAIL`, `UNVERIFIED`), accountable `owner`,
`depends_on`, `blockers`, and hashed `evidence` references. Pending work names its actual
dependency. A PASS has no blockers, passed dependencies, and candidate-bound receipts.
Use stable role owners until a named person explicitly accepts responsibility.

| Gate | Required acceptance | Accountable role |
| --- | --- | --- |
| automated | Full offline, typing, security, browser, integration, packaging, reproducibility and release checks on selected artifacts. Require evaluation booleans, not merely exit-zero reports. | Release engineering |
| memory_integrity | Zero critical scope leaks, unauthorized actions, lost acknowledged writes within the durability contract, erasure resurrection or migration-integrity violations in fault acceptance. | Core reliability |
| installed_journeys | Fresh Windows/macOS/Linux install, cross-session recall, correction/history, import recovery and upgrades; genuine MCP and dashboard readiness, including semantic startup. | Integration engineering |
| capacity | All 48 cells, 240 repetitions and 480,000 scheduled operations under [the capacity protocol](ENGINE_CAPACITY_PROTOCOL.md), with real semantic models and exact backends. | Performance engineering |
| responsiveness | At 100k memories, queue-inclusive recall p95 <=1s for 4 agents/16 GiB and <=2s for 16 agents/32 GiB. Report limits in other cells. | Performance engineering |
| resource_stability | No sustained backlog growth at declared load; process-tree peak, including startup, below 75% of reference RAM. Failures/timeouts remain in results. | Performance engineering |
| recovery | Ordinary-data backup RPO <=15m and restore RTO <=60m. Reconcile erasures, revocations, memberships and consent before reopening. | Operations |
| hosted_journeys | Actual mailbox/OAuth, no-card trials, voluntary test-mode purchase, provider callbacks, entitlements, Team roles, sync/consent/revocation, two-organization isolation, backups and alerts. | Hosted operations |
| independent_quality | Trusted executable corpus and independent acceptance; default changes meet zero critical regressions and family-clustered 95% noninferiority margin of 1 percentage point. | Independent evaluation lead |
| usability | Every critical scripted journey; >=11/12 first-time developers install and achieve useful cross-session recall unaided within ten minutes; keyboard, screen reader, reduced motion and correction/history. | Product acceptance |
| pilot | Five developers/one clean week, then twenty repositories/two further weeks; stop on critical correctness, isolation, consent, revocation or recovery failure. | Pilot owner |
| competitive_coding | Frozen strongest development-selected peer, >=5 percentage point verified task-success lift with paired 95% interval excluding zero; two model families, three repetitions and pilot-powered independent sample size. | Independent evaluation lead |
| external_benchmarks | Completed official external evaluations, including LongMemEval-V2, before broader memory claims. | Independent evaluation lead |

The last two gates are leadership gates. The actual independent corpus must follow
[CODING_ACCEPTANCE_CORPUS.md](CODING_ACCEPTANCE_CORPUS.md): 400 scenarios/40 families,
80 development, 80 validation, 240 held out. Repetitions are not independent tasks.
Freeze the selected memory-layer competitor configurations and identical resource ceilings;
evaluate complete agent systems separately. Keep exact peer identities in the private
comparison manifest until qualified results are ready. Retain eligible cross-session history
and isolate experiments. Publish category failures and complete efficiency curves.

## Receipts and validation

Each evidence reference has a relative POSIX `path` and a file `sha256`. Receipt schema
`engraphis-readiness-receipt/v1` requires the exact `candidate_id`, `gate_id`, boolean
`passed`, UTC-offset-bearing `observed_at`, `evidence_kind` (`automated` or `attended`),
concise `summary`, and nonempty `artifacts` referencing the actual execution outputs.
Automated receipts require an explicit checked JSON execution outcome; attachments
retain supporting logs. Contradictory JSON failures cannot be hidden as attachments.
Planned-recall reports require an explicit `planned_recall_gate` candidate and level;
an arbitrary true boolean cannot substitute for the selected evaluation gate.
Capacity matrix reports require the actual `capacity_acceptance_pass`, responsiveness
and resource-stability booleans, complete observed execution and the full operation
counts. Synthetic `protocol_observations_pass` and report-only command success do not
qualify. This rule also applies when a report is attached as supporting evidence.
Keep raw logs private; retain content-free summaries for publication. Hashes establish
byte identity, not that a human observation occurred. Independent acceptance must be
performed by actual participants and reviewed by its accountable owner.

```console
python scripts/check_release_readiness.py --ledger EVIDENCE/candidate-ledger.json --evidence-root EVIDENCE
python scripts/check_release_readiness.py --ledger EVIDENCE/candidate-ledger.json --evidence-root EVIDENCE --engine-root . --require-release
```

The first command checks consistency and may return success for an honestly incomplete
ledger. `--require-release` additionally requires every release gate and a clean matching
engine checkout; `--require-leadership` requires both decisions. All modes retain
`publication_authorized=false` and `execution_authenticity_verified=false`.
Actual publication additionally requires the protected, owner-signed approval in
[RELEASE_QUALIFICATION.md](RELEASE_QUALIFICATION.md), verified immediately before each
normal or repair write. Its environment, authority and approval remain owner setup;
this source change does not configure or issue them.

Planner experiments remain off by default. To require their existing optimization gate:

```console
python -m eval.planned_recall --require-gate planner --gate-level repository-local
python -m eval.planned_recall --require-gate planner_type_limits --gate-level default
```

A failed experimental promotion gate is retained as a failure and forbids promotion.
It does not silently alter the accepted baseline. Create fresh public fixture evidence
with `python scripts/export_offline_evidence.py --output NEW_ARTIFACT.json`; historical
artifacts are immutable. Update current prose and hashes only from those new results.

## Recovery and release package

Select explicit [SQLite durability](SQLITE_DURABILITY.md) and record its effective value
for every writer. Stop writers before a migration; keep the verified backup/hash and
exercise the upgrade on a disposable copy. Restore into a separate fenced location,
reconcile post-backup erasure and authority changes, then rebuild derived indexes from
current canonical state. Never downgrade the live schema to accommodate an older binary.

The publication proposal must contain candidate identities, compatibility matrix, all
gate receipts, dependency/security reports, release notes, installation/upgrade commands,
signed private artifacts where required, exact rollback artifacts and restore instructions.
Assign an unused distribution version before final publication qualification; a local
wheel carrying the current source version is not permission to replace a published release.

Paid experiments require a new model/run-count/cost proposal with durable caps before
execution. Prior proposals supply methodology only. Prices, trial policy, hosted boundaries
and user-controlled processing consent remain unchanged.
