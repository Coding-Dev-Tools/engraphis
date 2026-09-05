# Paid evaluation proposal (pending approval)

No paid evaluation has run. Prices were checked on 2026-09-05 against
[the official GPT-5.6 Luna model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

## Immediately reviewable existing-runner option

The existing `eval.hosted_luna` runner supports the exact model `gpt-5.6-luna`,
medium reasoning, fresh isolated read-only Codex attempts, zero transport retries,
durably reserved call ceilings, and three strategies: full history, retrieval, adaptive.

| Stage | Tasks | Repetitions | First attempts | Ceiling including one correction |
|---|---:|---:|---:|---:|
| Smoke | 1 | 1 | 3 | 6 |
| Pilot | 5 | 1 | 15 | 30 |
| CodeMem full | 26 | 3 | 234 | 468 |

The smoke dry run was executed without a model call and reported ceiling 6.
This runner's Codex usage is not an API invoice. API token rates below are reference
estimates only; they do not impose a dollar ceiling on Codex account usage.
It measures structured CodeMem outcomes and recovery, not arbitrary repository task success.
See `LUNA_BENCHMARK_PLAN.md` for its predeclared scoring and checkpoint rules.

## Proposed matched five-arm API experiment

A separate approval is recommended for this bounded experiment after the five-arm adapter,
input hashes and exact run binding are reviewable. It must use the existing evidence/ledger
infrastructure and the official [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2)
adapter. No private user memories are needed.

Model: exact `gpt-5.6-luna`, reasoning medium, Responses API, standard processing.
No tool fees, paid embeddings, LLM judge or retry/correction calls are included.
Use deterministic task oracles and report answer support/completeness separately.
Fix a local semantic embedder/version before comparing lexical, dense and hybrid retrieval;
the hashing embedder is not a semantic dense baseline.

Arms: no memory; full history; lexical; dense; hybrid.
Apply identical task/system prompts, generation limits, fresh state and source visibility.
Retrieval arms receive the same packing budget; full history receives its complete eligible
history within the shared hard request ceiling. Over-limit tasks fail preflight or are
reported as a separate common-budget stratum, never silently truncated in just one arm.

| Stage | Tasks | Arms | Repetitions | Calls | Maximum API token cost |
|---|---:|---:|---:|---:|---:|
| Smoke | 1 | 5 | 1 | 5 | $0.18 |
| Pilot | 5 | 5 | 1 | 25 | $0.89 |
| CodeMem fixture comparison | 26 | 5 | 3 | 390 | $13.82 |
| LongMemEval-V2 adapter pilot | 20 | 5 | 3 | 300 | $10.63 |
| Total | | | | 720 | $25.51 |

Cost assumptions: at most 128,000 uncached input tokens and 8,192 total output/reasoning
tokens per call; input $0.20/million and output $1.20/million. Per-call maximum is
`128000*0.20/1e6 + 8192*1.20/1e6 = $0.0354304`.
No caching or batch discount is assumed. Requests stay below the model's long-context
pricing threshold. Token-cost total is $25.509888 before tax.
Proposed approved API spend limit: **$31**, leaving approximately 20% headroom.
The runner must reserve the maximum per-call charge before dispatch, enforce token/call
ceilings durably across crashes, and stop rather than change model or pricing assumptions.

This is an exact budgeted proposal, not authorization to execute. Final execution requires:
1. Reviewable five-arm adapter and fake-client budget/interruption tests.
2. Frozen source, model/tokenizer/embedding revision, prompts and dataset hashes.
3. Exactly 20 LongMemEval-V2 question IDs selected with a declared seed/category rule before
   seeing outcomes; official dataset license/version verified. This subset is an adapter pilot,
   not a full benchmark score.
4. The same source eligibility, temporal/scope rules and resource limits in every arm.
5. Explicit approval of the $31 API limit and the exact run binding.

Do not describe CodeMem or implementation-authored fixtures as independent held-out evidence.
An independent coding-agent corpus and production workload study require separately
frozen tasks and a new cost proposal. They cannot be replaced by these 720 model calls.

Predeclare category-level evidence retention, false NOOP/merge, unsupported-answer and scope
violations, abstention, task outcome, input/output tokens, latency and cost. Critical correctness
violations fail acceptance. Report paired task-level differences and task-cluster bootstrap
95% intervals; missing/error outcomes remain in the report. Retain existing defaults when
improvement or non-inferiority is not established. A 20-question pilot is not a reliable basis
for a broad product claim.
