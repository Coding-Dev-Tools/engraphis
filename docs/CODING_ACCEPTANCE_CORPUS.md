# Independent coding-memory acceptance corpus

`eval.coding_acceptance` defines a versioned manifest and validates structural
and provenance declarations. It does not contain an independently authored corpus,
execute model calls, prove authorship, or authorize evaluation spending.
Existing deterministic fixtures remain useful regression tests and keep their
implementation-authored or synthetic labels.

## Frozen task structure

Require **400 scenarios in 40 repository families**. Each family supplies exactly
one scenario in each of these ten categories:

| Category | Required behavior to label |
| --- | --- |
| Corrections | Real remember/correct/recall sequence; false NOOP and false merge |
| Temporal history | Future/current/expired facts, known-at and valid-at history |
| Scope boundaries | Workspace/repository/session ownership and equal-content facts |
| Paraphrases | Retrieval with a pinned semantic embedder; no supplied similarities |
| Code relationships | Symbols, files/calls and explicit evidence bridges |
| Unsupported questions | Grounded abstention and unsupported-answer rate |
| Poisoning | Untrusted ingested instructions cannot change review/authorization |
| Conditions and values | Complete subjects, numbers, units, negation and conditions |
| Long documents | Evidence beyond early chunks; budget omissions remain explicit |
| Multilingual | Whole conditions and titles retained across languages |

The split is by family, never by question: **80 development, 80 validation and
240 held-out** scenarios. Sort families by SHA-256 of `split_seed:family_id`; assign
the first eight families to development, the next eight to validation and the
remaining 24 to held-out. Freeze the family identities and seed before tuning.
Human reviewers must also check related forks, shared templates and paraphrased
tasks across families; an identifier validator cannot detect semantic leakage.

Each scenario binds an ID, family, category, split, declared origin, author IDs,
source SHA-256, oracle SHA-256 and required evidence IDs. The manifest binds the
implementation authors, two distinct reviewers, a timezone-aware freeze time and
an authorship-attestation SHA-256. Independent task authors, implementation
authors and reviewers must not overlap. Licensing, consent, authorship method,
family lineage and review/adjudication should be recorded in the attestation.

Source and oracle hashes reference separately frozen task artifacts. Those
artifacts must contain the initial history/repository, ordered operations, query,
scope/time anchors, expected memory transitions, evidence units, unsupported
claims, deterministic task oracle and safe disposable setup/cleanup instructions.
This manifest validator checks the hash format, not the referenced bytes or the
behavior of those tasks. A future corpus loader must verify the bytes and execute
the sequence before a report can claim coverage.

## Validator use and evidence boundaries

```console
python -m eval.coding_acceptance
python -m eval.coding_acceptance --corpus corpus.json --attestation authorship.txt
python -m eval.coding_acceptance --corpus generated-fixture.json --fixture
```

With no corpus, the command emits portable JSON Schema. The Python validator also
enforces cross-row counts, categories, exact family splits, unique IDs, author
separation and the matched arms/budgets. Candidate independent validation requires
the actual attestation file to match its digest. `--fixture` permits a structural
check while retaining the declared origin.

Synthetic and implementation-authored manifests are rejected by the independent
candidate mode. Changing only a manifest's origin cannot relabel its scenarios.
No automated metadata check can prove that every declaration is truthful: even a
matching attestation returns `independently_authored_verified=false`,
`publication_ready=false` and at most `authorship_status=attested_unverified`.
The generated 400-row test data is explicitly a test of this validator, never
400 completed acceptance tasks or independent evidence.

## Matched comparison bindings

Use **five arms** (no memory, full history, lexical, dense, hybrid) at **three
retrieval budgets** (512, 1,500, 4,096). A binding file contains exactly 15 cells.
All cells share corpus/prompt/source-visibility hashes, reader and tokenizer
identities/revisions, local semantic embedding identity/revision, random seed,
and hard input/output ceilings. Hashing cannot be declared a semantic dense
baseline. Validate with:

```console
python -m eval.coding_acceptance --bindings bindings.json
```

Each cell contains `arm`, `token_budget`, `corpus_sha256`, `prompt_sha256`,
`reader_id`, `reader_revision`, `tokenizer_id`, `tokenizer_revision`,
`max_input_tokens`, `max_output_tokens`, `embedding_id`, `embedding_revision`,
`embedding_semantic`, `source_visibility_sha256`, `history_overflow_policy` and
`seed`. All fields except arm/budget must be identical. The overflow policy is
`fail_preflight`: full history receives all eligible history within the common
hard input ceiling, or that task fails preflight for every matched arm. Do not
silently truncate full history. No-memory/full-history are repeated in each
budget stratum and must not be counted as new independent tasks.

Bindings are a preflight contract, not an execution adapter. Validation reports
zero model calls and `paid_run_authorized=false`. The cost and execution-approval
boundary remains [the separate paid evaluation proposal](PAID_EVALUATION_PROPOSAL.md).

## Scoring and release gates

Keep candidate discovery, write resolution, ranking, packing, grounding and final
task success separate. Citation validity asks whether a cited source supports a
claim; answer completeness asks whether all required evidence was covered. Count
false NOOPs, false merges, unsupported assertions, missed conditions, scope/time
violations and abstention independently. Record context/model tokens, latency,
memory, disk and costs without using token reduction as evidence of correctness.

Run development tasks while tuning; freeze the candidate and all bindings before
validation; reserve held-out families for the final comparison. Require zero
critical integrity/authorization violations. Predeclare one percentage point as
the task-success non-inferiority margin and require paired family-cluster 95%
intervals, with category-level failures reported. If uncertainty cannot exclude
the declared degradation, retain the current default. Missing/error outcomes
stay in the denominator; do not silently retry or exclude difficult tasks.

The remaining work is actual independent authorship and adjudication, frozen
artifact/license verification, the executable five-arm adapter, task-oracle and
budget-interruption tests, and approved paired validation/held-out runs. Passing
this validator establishes none of those operational results.

## Matched outcome aggregation

`eval.task_pairs` analyzes one matched arm/budget comparison using the full corpus
manifest and its 15 run bindings. It does not execute tasks or call models.

```console
python -m eval.task_pairs --baseline baseline.json --candidate candidate.json --corpus corpus.json --bindings bindings.json --output paired-results.json
```

The default comparison is full history versus hybrid, budget 1,500, 240 held-out
tasks and three repetitions. Outcomes bind `scenario_id`, source/oracle hashes,
the canonical run-binding hash, `repetition` (zero-based), `status`, boolean
`task_success`, `evidence_retained_ids`, `critical_violations`, implementation
source hash and a unique `run_id`. Only arm identity and the explicitly reported
implementation source identity may differ between paired conditions. The common
reader, model, tokenizer, prompts, resource limits and source visibility are bound
through the validated matrix. Duplicate attempts and incompatible bindings fail.

Missing attempts remain zero-success/zero-retention failures in the predeclared
denominator and disable non-inferiority. Tasks with no required evidence report
retention as unavailable, rather than receiving a free perfect score. Task success
and evidence retention have separate means and candidate-minus-baseline intervals;
required-evidence coverage does not establish citation validity or answer completeness.

The 95% percentile bootstrap first averages paired effects within each repository
family, then resamples families as blocks. Repeated answers never become new
independent families. Report category intervals as exploratory; they have no
multiple-comparison adjustment and cannot select a winning default independently.
At least 24 held-out families, 240 tasks and three repetitions are required before
the statistical non-inferiority check at margin 0.01 is eligible. The lower bound
must strictly exceed -0.01; incomplete/error pairs, critical violations, synthetic
authorship and degenerate intervals disable that check. Constant effects produce
no defensible resampling uncertainty and cannot manufacture a pass, even when
both conditions report perfect scores. These minima are not a power guarantee.

Even statistical support retains `default_action=keep_current_defaults` and
`independently_authored_verified=false`: matching declarations and attestations
cannot prove real execution or independent authorship. Completing one pair is
also not execution of the full five-arm corpus. Local tests and benchmarking are
already authorized; paid calls remain governed by their separate proposal.
