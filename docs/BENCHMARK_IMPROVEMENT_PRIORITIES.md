# Engraphis improvement priorities from benchmark evidence

Decision brief, 2026-09-16. **Analysis COMPLETE; opt-in product controls IMPLEMENTED.**
The campaign remains PARTIAL and the coding pilot remains BLOCKED. This review uses
[the expansion results](BENCHMARK_EXPANSION_RESULTS.md), their checksummed artifacts,
and the v2 implementation reviewed at `cc25ac5b` / `fefee937`, followed by the
opt-in changes in this working tree.
The implementation adds the opt-in controls and diagnostic fields described below.
Historical result artifacts remain immutable, and production defaults remain unchanged.

The external numbers retain their original provenance: the four LoCoMo/LongMemEval
run artifacts record `ca790261` with distinct dirty-state hashes. They are not fresh
executions of the current follow-up working tree. Their producer lists bind the harness/metrics
files, but do not individually hash the core packer and recall files. Those two core
files have no committed changes between `ca790261` and `cc25ac5b`; this supports code
inspection but does not reconstruct every dirty working-tree byte at execution time.
Use a clean, fully bound baseline for the next product comparison.

Prioritize **evidence packing, faithful use of exact values, and retrieval depth**.
The objective is more correct user tasks within explicit context, latency and usage
limits. Select useful tradeoffs for different workloads; the available evidence does
not identify one universally optimal configuration.

| Priority | Observed evidence | Proposed change | User benefit and decisive measurement |
|---|---|---|---|
| 1. Retain useful evidence during packing | LongMemEval: 97.53% retrieved source recall, 57.07% packed at 1,500 tokens; 81.09% packed at 4,096 | An opt-in packer that selects complete, relevant evidence units across sources before spending the remaining budget | Better answers about long sessions, dates and conditions at the same context ceiling; measure answer-bearing span coverage and task correctness |
| 2. Preserve values from memory through the final edit/action | All 12 multilingual memory cells retained the required source; none produced the complete literal value | Source-bound typed values and an explicit exact-copy contract in the agent integration, with bounded schema/test feedback | Correct configuration, localized labels, paths and tool arguments; measure executable task/action success and exact-value preservation |
| 3. Retrieve enough useful sources for the available context | LoCoMo k=10 to k=20 increased packed recall 65.51% to 72.79% at the same 1,500-token cap | Workload-specific, opt-in result depth; evaluate candidate depth, reranking and graph/code contributions separately | Better continuity and multi-source answers; measure useful packed evidence, task success, total input usage and isolated latency |

These are three separately attributable experiments. Do not combine them before their
individual effects are understood. An integrity defect found during capacity execution
takes precedence over quality or speed tuning.

## Implemented opt-in controls

The first implementation pass keeps the production and benchmark defaults unchanged:

- `packing_mode="coverage"` uses a two-pass packer that admits complete, query-relevant
  evidence units across distinct sources before expanding them. It reports the selected
  mode, packed IDs, exact token usage and source-bound exact-value metadata. The existing
  `"legacy"` mode remains the default and is still used by existing callers.
- Smart/Classic `engraphis_remember` and the Python/service APIs accept `exact_value` plus an
  `exact_value_type`. The value must be copied verbatim from the authorized source (or be
  accompanied by an explicit source span); ambiguous matches are rejected. Coverage
  packing carries the binding with the evidence so an integration can validate a file or
  tool argument after the model proposes an action. The compact Smart write schema stays
  within its existing payload budget. Compact recall candidate rows omit exact bindings;
  admitted bindings remain in `packed_sources` (or MCP `recall_context`'s `sources`) alongside
  the returned context.
- `retrieval_recipe="conversation"` selects the measured `k=20` / 1,500-token starting
  point, while `"long_session"` selects `k=10` / 4,096 tokens when the caller leaves
  `k` and the token budget at their defaults. An explicit caller value always wins, and
  `"default"` preserves historical behavior. The selected recipe is returned in traces
  for paired analysis.
- Packed chunks now expose a stable `evidence_unit_id`, source span, subject/claim
  identity, qualifiers, validity times, and source attribution. These fields are
  content-bounded provenance for the selected excerpt; they do not expose evaluator
  labels or turn source presence into a citation-entailment score.
- `make_action_contract` / `validate_action_contract` provide an opt-in file/tool
  boundary for typed literals. The validator reports authorization, source identity,
  schema/destination presence, and exact literal preservation independently. It rejects
  unbound, changed, ambiguous, or unauthorized values without changing the production
  write or recall defaults.
  Serialized proposals must be JSON objects and satisfy the same destination and
  source checks as mappings. Only explicit boolean authorization is accepted.
- MemoryAgentBench rows retain `evidence_label_provenance`, label cardinality and the
  perfect-top-k cardinality ceiling. The adapter also emits a clearly named packed
  answer-token sufficient-evidence proxy; this is a diagnostic for label quality and
  context selection, not generated-answer correctness or citation entailment.
- Adaptive candidate-depth recalls report `adaptive_stop_reason` and a bounded
  packed-candidate coverage ratio over the selected packing input. This ratio does
  not measure the complete retrieval pool or gold evidence coverage. Capacity worker failures retain content-free
  traceback coordinates in private artifacts, while public operation rows keep the
  existing redaction boundary.

The Smart and Classic `recall_context` schemas retain their historical advertised
`token_budget=1024` default. Internally, omitted values remain distinct from explicitly
supplied values. A non-default recipe may choose its measured budget only when the caller
omits the budget; an explicit 1,024-token budget is honored on both MCP surfaces.

These controls are experiment surfaces, not quality promotions. Run development and
validation measurements first, freeze the candidate, and use the untouched holdout plus
the existing non-inferiority and critical-integrity gates before changing a default.

### Follow-up development diagnostic, 2026-09-19

The [source-bound action and packing fixture](benchmark-evidence/evidence-contracts-20260919.json)
checks 14 action-boundary cases, all passing, including exact destination matching,
source identity, escaped Unicode, explicit authorization and source-span consistency.
Its fixed 35-token packing example retains one source in 35 tokens with legacy packing
and three sources in 33 tokens with coverage packing. Reproduce the current results
with `python -m eval.evidence_contracts`. This small development diagnostic does not
measure external benchmark scores, generated answers, or a held-out quality gain.

## 1. Packing is the clearest engine opportunity

The LongMemEval retrieved-to-packed gap is **40.46 percentage points** at 1,500 tokens.
The larger-budget experiment recovered **24.03 points** (paired 95% interval
**+21.68 to +26.24**) while mean context increased from **1,499.4 to 4,095.1** tokens.
This identifies a context-selection bottleneck and a measured budget tradeoff, without
establishing generated-answer improvement. The temporal category is particularly useful
development material: source recall was 96.06% retrieved and 45.12% packed at 1,500.
Sources: [baseline analysis](benchmark-evidence/external-baselines-20260916.json) and
[budget comparison](benchmark-evidence/longmemeval-budget-comparison-20260916.json).

The code explains a plausible mechanism. [The loader](../eval/external.py) represents an
entire LongMemEval session as one memory. [The current packer](../engraphis/core/context.py)
selects one candidate at a time and gives its excerpt the available remaining budget.
Sentence selection protects qualifiers and keeps complete evidence units, but does not
reserve space for useful units from other sources. Whole-session granularity, selection
order and qualifier competition are hypotheses to test; the aggregate result alone does
not assign losses among them.

The reported 40.46-point difference compares the already selected retrieved set with
packed source IDs. Earlier candidate discovery, reranking and final-k truncation can
lower retrieved recall, but do not explain that particular retrieved-to-packed gap.

Implement the first candidate behind the existing
[`ContextPacker` interface](../engraphis/core/interfaces.py). Keep retrieval, ingestion,
source visibility and source IDs fixed. Select bounded, query-relevant sentence/turn
groups across the retrieved sources, then expand groups while budget remains. Each group
must retain its subject, exact value, applicable condition, time and source attribution.
Keep values and their qualifiers together, including dates, negation, units, scope,
Unicode literals and code identifiers. Omit a group when its complete binding cannot fit.
Charge all added headers and metadata to the declared budget. Preserve existing
[packing regressions](../tests/test_context_packing.py), duplicate-ID semantics and
custom-token-counter behavior.

Record why evidence was omitted: absent from candidates, ranked below the returned set,
score-tail omission, unit too large, or exhausted budget. This permits diagnosis without
relaxing correctness safeguards. Test sentence/turn chunk indexing only as a later,
separately versioned ingestion experiment with parent-source IDs and offsets; changing
ingestion and packing together would obscure which change helped.
The packing envelope reports the observed subset of these reasons (`duplicate`, `budget`,
`score_tail`, `missing_record`, and `unit_too_large`); it cannot infer evidence that never
reached the bounded candidate set.

Acceptance must go beyond source-ID presence. The current
[retrieval harness](../eval/harness.py) gives packed-source credit when any excerpt of
that source is present. Several tiny, irrelevant snippets could improve this metric
without helping a reader. Add independently annotated answer-bearing spans and
condition/value bindings on development examples, and score the actual answer or edit.
Gold spans are evaluator-only data and must never enter retrieval or packing inputs.
Compare all three fixed budgets (512 / 1,500 / 4,096), with packing at 1,500 the primary
development comparison. Treat current 4,096-token packing as a useful reference, not a
promised target for a smaller context.

## 2. Improve the memory-to-action interface

The [combined pilot](benchmark-evidence/core-pilot-combined-20260916.json) contains
96 completed eligible memory-arm cells across eight categories. All 96 retained the
required source IDs, while 59 passed the executable task. These are repeated budgets
and arms from one synthetic family, not 96 independent tasks. They direct investigation
past source discovery; ID retention does not prove full supporting content or correct use.

The strongest reviewed example is the multilingual fixture: 12/12 memory cells retained
the source, 11/12 cited its required ID, and 0/12 preserved the complete composite label.
The full value was supplied to the reader. Scope-owner selection, poisoning and
paraphrase tasks also have retained failures. Do not interpret those task failures as a
proven authorization bypass, or the zero declared critical counter as a security pass.
Correction, temporal-history and condition/value tasks passed in all four memory arms
at all three budgets in this family; preserve those successful behaviors as regressions.

Build an opt-in integration that represents an exact value as a typed field associated
with its existing source, subject, claim kind, owner and time. Existing
[`subject_key` / `claim_kind` resolution](../engraphis/core/resolve.py) supplies claim
identity; it does not itself establish a typed value or verify extraction. Derive any
value from authorized source text, retain its exact span, and reject ambiguous extraction.
Schemas can specify a string or number and the destination field, but cannot contain a
hidden expected answer. Treat memory text as data; the host supplies the output contract.

Require exact copying only for fields whose user contract demands it. Preserve all
Unicode characters and prefixes for literal fields while allowing legitimate paraphrase
of explanatory prose. Add a structured, source-bound answer and proposed edit, then
validate the resulting file or tool arguments. A schema/test feedback experiment can
return a field/type or ordinary test failure without revealing an evaluator-only answer.
Keep the existing correction-call ceiling and count every correction's usage and latency.

Separate two questions in the experiment: whether retrieval improved, and whether the
integration made the same retrieved evidence easier to use. Hold evidence constant for
the integration comparison. Apply common prompt/schema changes to every matched arm;
an Engraphis-specific presentation comparison must be labeled a product-integration
comparison. Preserve the exact oracle; making it easier is not product improvement.

Use the same integration contract in real Smart/Classic MCP responses and session-start
context. Measure the evidence text, serialized response, complete reader input and native
usage independently. For actions, the [Mem2Act diagnostic](benchmark-evidence/mem2actbench-small-20260916.json)
has 99.48% packed-source recall but only 41.03% expected-tool-call token coverage and no
executed actions. Add a disposable tool executor with argument/schema checks, state
transition checks, duplicate-call detection and abstention. Token coverage is not an
action-success estimate; upstream action scores require their actual harness.

## 3. Match retrieval depth and routing to the workload

The [LoCoMo depth comparison](benchmark-evidence/locomo-depth-comparison-20260916.json)
supports an opt-in `MemoryEngine.recall(k=20, token_budget=1500)` recipe for similar short
conversation evidence: **+7.28 points** packed recall (95% **+6.51 to +8.11**), with mean
context **471.5 to 936.3** tokens. It used the pinned MiniLM embedding and annotated
dialogue ingestion without conflict resolution. Its ingestion setting is not a general
recommendation for user corrections.

Keep each API's baseline explicit. [`MemoryEngine.recall`](../engraphis/core/engine.py)
defaults to k=8; both Smart and Classic
[`recall_context`](../engraphis/mcp_server.py) default to k=50 and 1,024 context tokens.
The measured k=10 to k=20 comparison does not justify replacing those defaults with 20.
Also, [`recall.py`](../engraphis/core/recall.py) ties its ordinary rerank pool to 4*k,
so varying k can affect more than the number returned. Distinguish arm candidates,
rerank candidates, returned memories and packed evidence in the traces.
Record both `candidate_k_requested` and `candidate_k_used`: the latter reports actual
arm depth after prompt-only widening. An adaptive policy can resolve to the same depth
as fixed mode at a particular k/ceiling; confirm that a proposed ablation changed the
executed path. Compare real requested/used depths rather than configuration names.

Evaluate returned depth and candidate depth independently before adding a reranker.
Keep the reader, embedding, packer and total context ceiling identical. Compare the
existing balanced path with a strong dense-plus-lexical companion control; preserve the
five-arm corpus contract. Test graph/code profiles on tasks that require a relationship
or indexed symbol, with actual indexing, bridge creation, rename/delete and reindexing.
The current [balanced profile](../engraphis/core/retrieval_policy.py) does not enable the
code arm; code-profile value needs an explicit comparison. Planner/type limits,
graph-seed fallback and cross-encoder changes remain separate opt-in hypotheses.
Code-profile tests need an explicit repository scope and populated code index; graph
tests need the relevant entities/links. Record actual reranker identity and fallback,
so an unavailable cross-encoder cannot silently become an identity-versus-identity test.

Source depth should increase only when the selected experiment shows useful evidence
fits and improves tasks within the workload's latency/usage limits. A budget-limited
long session benefits from packing work before more candidate expansion. Sparse-evidence
and unsupported questions need false-answer/abstention checks when depth increases.

## Measurement repairs required before selecting a winner

The completed MAB accurate-retrieval artifact became available during this review and
passed checksum, envelope and per-row source-recall recomputation. Its 2,000 questions
include only 363 retrieval-labeled questions; 1,637 lack matching labels and remain
explicit. These are missing labels, not missing executions.

All four prepared MAB splits lack explicit source IDs. The
[adapter](../eval/agent_benchmarks.py) constructs diagnostic labels from chunks containing
an accepted answer string. Common answer strings can match many chunks. This means
all-labeled-chunk recall can be intrinsically low at k=10, and those labels are not
independently adjudicated supporting evidence:

| Completed MAB split | Retrieval-scored / questions | Observed source recall | Mean cardinality ceiling at k=10 |
|---|---:|---:|---:|
| [Accurate retrieval](benchmark-evidence/mab-accurate-retrieval-20260916.json) | 363 / 2,000 | 46.54% | 78.30% |
| [Conflict resolution](benchmark-evidence/mab-conflict-resolution-20260916.json) | 800 / 800 | 27.96% | 80.28% |
| [Test-time learning](benchmark-evidence/mab-test-time-learning-20260916.json) | 568 / 700 | 5.38% | 18.98% |

The ceiling is recomputable from each artifact: average `min(1, 10 / N)` over
`retrieval_scored` rows, where N is the number of unique `supporting_ids`. It assumes
perfect top-k selection, ignores the context budget and is not a replacement score.
For test-time learning, 505/568 labeled rows have more than ten labeled chunks; one has
3,454. Preserve original metrics and add label provenance, label-count distributions,
sufficient-evidence diagnostics and the appropriate upstream outcome. Do not optimize
for repeated answer-bearing text. The `--no-resolve` conflict split does not measure
production contradiction resolution.

Before another model stage, preserve partial initial/correction progress on exceptions,
derive usage from durable native-call records, and retain safe structured native error
codes. The stopped pilot has 120 eligible completions, one error and 14 unattempted cells;
its frozen records and reservations remain unchanged. Recovery must bind the never-started
cells, existing allocations and source revisions without replaying failed calls.
Repairing the invalid long-document corpus contract is measurement work, not an engine
quality gain. Do not use the excluded v1 fixture to tune packing.

## Capacity work follows measured bottlenecks

The 24-cell one-host queue remains active; it has not established a 100k-memory or
16-process performance result. Keep it serial and keep bound producer bytes frozen.
Once complete, choose an operational patch from its phase timings and fresh repetitions:
query embedding/search/scoring/packing, startup/reopen, seeding/import, or write contention.
Separate operation time from dispatch/IPC queue delay and verification; report startup
and seeding separately from the scheduled-operation latency distribution.
Conditional candidates are import batching, native-index reopen/repair, bounded exact
search work and scope-aware caching. A cache must include authorization/scope, time,
query/configuration and store revision, and invalidate on corrections and erasure.
There is no measured basis yet for picking a faster default backend or approximate index.

The [capacity protocol](ENGINE_CAPACITY_PROTOCOL.md) already fixes the relevant limits:
100k recall p95 at most 2 seconds for 16 agents on the 32 GiB reference host, sampled
lifecycle RSS below 75% of observed RAM, no observed sustained backlog growth under its
declared rule, and zero integrity violations. Evaluate every fresh repetition and both
read/mixed workloads. Use repetition-level uncertainty, keep missing attempts visible,
and preserve the separate 16 GiB/four-agent and two-host qualification requirements.

## Selection and release sequence

1. Finish artifact reconciliation and corpus validity checks; freeze the baseline and
   workload-specific failure taxonomy. Add consented or appropriately licensed tasks
   from real repository histories, including mixed imports/reimports, handoffs, exact
   values, corrections, scope boundaries, poisoning and code changes. Keep private
   customer data private and record source/oracle lineage.
2. Tune the three candidates separately on the 80 development scenarios. Use public
   benchmark diagnostics as development evidence, not a new untouched holdout. Keep
   source labels and expected answers outside model-visible inputs and configurations.
3. Freeze candidate configurations for the 80 validation scenarios and choose the
   candidate using predeclared task, evidence, latency and usage criteria. Then freeze
   the selected code/prompt/configuration before comparing baseline and candidate on
   240 held-out scenarios, at all three budgets and three repetitions. No tuning on
   inspected holdout failures; another round requires a new holdout.
4. Report executable task success, citation entailment, completeness, abstention,
   corrections, total native usage and latency independently. Keep the one-point task
   non-inferiority margin and paired 95% intervals clustered by repository family;
   the lower bound must strictly exceed -0.01 for statistical non-inferiority.
   Shared template families require template-lineage treatment or descriptive labeling.
   Missing/error pairs and inadequate precision remain indeterminate. Require zero
   critical scope, authorization, acknowledged-write and erasure-resurrection violations.
5. Preserve `implementation_team` labeling. Independent-human acceptance, matched peer
   runs, official LongMemEval-V2 execution and broader claims retain their separate
   requirements. Use native Codex OAuth only for authorized reader stages;
   later stage budgets still need approval. This brief authorizes no spending or release.

**Useful configuration choices now:** the measured k=20 / 1,500-token recipe for similar
short-history engine calls; k=10 / 4,096 tokens when long-session evidence retention is
worth the measured context increase; compact MCP output with scope and source identity
preserved. Verify task outcomes for each. Exact literals need faithful consumption even
when retrieval succeeds, and the unfinished capacity study cannot yet select a backend.

Review verification: checked the two external comparison bindings and source artifacts,
recomputed coding category counts and MAB source-recall/label ceilings, inspected v2
packing/routing/MCP contracts, and used three bounded read-only reviews. The follow-up
implementation added the opt-in controls above and offline regression coverage; it made
no new model call or benchmark execution and promoted no default.
