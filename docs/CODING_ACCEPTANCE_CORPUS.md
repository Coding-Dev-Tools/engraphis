# Coding-memory acceptance corpus

`eval.coding_acceptance` defines the versioned acceptance manifest and validates
structural and provenance declarations. It does not execute model calls, prove
authorship, or authorize evaluation spending. The checked-in executable corpus in
`eval/datasets/coding_memory_v1/` is implementation-authored and synthetic; it is
an offline plumbing and regression lane, not independent human evidence or
real-customer data. Existing deterministic fixtures retain their implementation-
authored or synthetic labels.

## Checked-in implementation corpus

`eval.coding_corpus` loads and verifies the implementation-authored v1 corpus. It
contains exactly 40 compact, family-specific disposable repository fixtures and
400 scenarios: one scenario for each of the ten categories below in each family.
The 40 families are arranged as ten template groups with four distinct variants
per group. Each variant has its own product context, region, transport, values,
helper relationship, document constraints and scope identifiers. This provides
curated family variation while making the shared template lineage explicit for
leakage review; it must not be described as 40 independently authored repositories.

Each family source artifact contains a runnable `service.py`, operating contract,
configuration and README. Every scenario has a separately hashed Python oracle.
The source and oracle hashes are checked against the bytes on disk before a
scenario is returned. The attestation is also bound and explicitly states
`origin=implementation_team`, synthetic disposable fixtures, no independent
human authorship and no real-customer provenance. Oracles run in a disposable
directory with the fixture on `PYTHONPATH`; the generated implementation is
deliberately initially incorrect, so an oracle only passes after the target
repository is actually changed. There are no unconditional pass stubs.

Materialize or verify the artifacts with:

```console
python -m eval.coding_corpus --materialize --verify
python -m pytest tests/test_coding_corpus.py -q
```

The loader keeps the acceptance manifest's exact fields and leaves the existing
five-arm binding validator separate. Runtime-only fields live in `runtime.json`,
so executable source paths, task contracts and session operations cannot silently
change the acceptance schema. The stable Python API is:

```python
from eval.coding_corpus import load_corpus, run_oracle, run_reader

corpus = load_corpus()
scenario = corpus.get("atlas-green:corrections")
context = corpus.context(scenario)       # replayed, scoped and time-filtered
oracle = run_oracle(scenario)            # disposable repository execution

def reader(request):
    # request.scenario, request.prompt and request.context are immutable inputs.
    return {"answer": "...", "citations": [request.context[0].id]}

score = run_reader(scenario, reader, oracle_passed=oracle.passed)
```

`Scenario.task` provides the prompt, target files, expected change, answerable
flag, answer tokens, required/forbidden/untrusted evidence IDs, scope and
`valid_at`/`known_at` anchors. `SessionOperation` supports `remember`, `event`,
`correct` and `invalidate`; `replay_session` and `Corpus.replay` expose the
deterministic ledger for offline readers. `Reader` is an injectable callback
returning a string, `ReaderResponse(answer, citations)`, or an equivalent mapping.
`ScenarioScore` reports `task_success`, `evidence_retained`, `citation_validity`,
`answer_token_coverage`, `answer_completeness`, `abstention_correct`, and a
separate tuple of `critical_violations`. `task_success`,
`structural_correctness` and `structural_completeness` are the supplied
deterministic oracle result; they are `None` when no oracle result is supplied.
Token coverage is only a diagnostic and never claims semantic completeness.
`answer_completeness` is `None` until a real structural/semantic grader supplies
it. `evidence_retained` is also `None` for unsupported tasks with no required
evidence, rather than a free perfect score. `Corpus.read(..., oracle_passed=...)`
combines context, callback and typed scoring when the caller already ran the
repository oracle.

The frozen split is by family: eight development families, eight validation
families and 24 held-out families, with the SHA-256 seed protocol below. The
loader never evaluates held-out answers itself; a campaign must reserve those
families until its predeclared final comparison. This lane is suitable for local
oracle/replay/adapter checks and deterministic reader plumbing. It does not
establish independent authorship, external validity, model quality, production
capacity or a paid-evaluation result. Because four families share each of ten
template groups, family-clustered intervals must either cluster at the template
group level or be labeled descriptive; the 40 family IDs cannot support a claim
of 40 independent repository draws.

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
The existing manifest validator checks the hash format, not referenced bytes or
task behavior. `eval.coding_corpus` supplies that implementation-authored loader
boundary for the checked-in lane; independent-corpus submissions still require
their own artifact loader and adjudication evidence before claiming acceptance
coverage.

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
The generated structural fixture in `tests/test_coding_acceptance.py` is a test
of this validator, never independent evidence. The checked-in 400-row executable
corpus is a separate implementation-authored regression lane and carries the
same limitation.

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
