# Hosted Luna productivity benchmark

This plan measures whether Engraphis helps a real hosted coding model complete memory-dependent
tasks with less context, fewer memory calls, and no material loss of answer quality. It extends
the deterministic retrieval benchmarks; it does not replace them.

## Questions the benchmark must answer

1. Does adaptive context preserve task completion compared with sending the full history?
2. Does it use fewer total input tokens than always retrieving memory?
3. Does it avoid unnecessary memory calls when the relevant history already fits?
4. Does the wider-context fallback prevent mistakes when retrieval confidence is weak?
5. What happens to corrections, latency, output tokens, and reasoning tokens?

## Frozen experiment

| Setting | Value |
|---|---|
| Model | `gpt-5.6-luna` (exact match required; no fallback model) |
| Reasoning effort | `medium` |
| Dataset | CodeMem productivity fixture |
| Strategies | full history, always retrieve, adaptive |
| Prompt | identical task instruction and answer contract for every strategy |
| Scoring | deterministic fixture oracle; model self-grading is not used |
| State | fresh ephemeral thread for every attempt |
| Filesystem | empty temporary working directory, read-only sandbox |
| Context handling | supplied memory is fenced as untrusted evidence |
| Retries | identical policy for every strategy; retry transport failures only |

Every report must record the dataset hash, repository revision and dirty-state hash, SDK/runtime
version, exact model, reasoning effort, run time, and runner configuration. A run is invalid if
the requested model is unavailable or the result reports a different model.

## Staged run matrix and hard ceilings

| Stage | Tasks | Repetitions | Expected first attempts | Absolute call ceiling |
|---|---:|---:|---:|---:|
| Smoke | 1 | 1 | 3 | 6 |
| Pilot | 5 | 1 | 15 | 30 |
| Full | 26 | 3 | 234 | 468 |

The ceiling includes correction attempts with retries set to zero. The runner must calculate and print the projected
maximum before making a hosted call, require an explicit ceiling, stop before exceeding it, and
support resuming without repeating completed attempts. A full run proceeds only after the smoke
report confirms the exact model, usable token accounting, valid structured answers, and no tool
or filesystem dependence.

## Measurements

Record each strategy and task separately:

- task completion against the deterministic oracle
- mistakes and correction attempts
- end-to-end turns
- Engraphis memory calls
- wall-clock latency
- input, cached-input, output, reasoning, and total tokens when the runtime provides them
- model/runtime errors, schema failures, retries, and any tool use

First-attempt completion is the clean strategy comparison. If that attempt fails, the benchmark
also measures whether one identical full-history correction can recover the task; final completion
therefore measures recoverability, not the purity of the original context strategy.

Report paired differences by task, not only overall averages. For repeated full runs, first
aggregate each task's paired repetitions, then bootstrap-resample those task clusters (not the
flattened run/task rows) for the paired mean difference and 95% confidence interval. Publish the
descriptive median task difference for token, latency, mistake, and completion measurements. Keep
missing token fields explicitly missing; never infer or substitute them.

## Predeclared success criteria

The adaptive strategy is considered successful only if all of these hold:

1. Its task completion is non-inferior to full history and always-retrieve by no more than one
   task out of 26 (3.85 percentage points).
2. It reduces median total input tokens versus always-retrieve.
3. It reduces memory calls on the short-history cases that already fit in the prompt.
4. Weak-confidence cases either widen context or abstain; they must not silently use a narrow,
   low-confidence answer.
5. No strategy has an advantage from different prompts, retry counts, retained thread state, or
   access to repository files.

Results that miss a criterion are still published internally as evidence, but they are not
converted into a marketing claim.

## Artifact policy

### Private resumable record

Write one append-only JSONL record per completed attempt. It may include:

- stable run and task identifiers
- strategy, repetition, attempt, completion result, and error class
- timings and returned token counters
- model, effort, SDK/runtime version, configuration, and hashes

It must not include credentials, raw memory context, raw prompts, or unrestricted model answers.
If an answer is needed to resume or audit scoring, store only the minimal normalized answer in a
git-ignored run directory.

### Public aggregate report

Generate a deterministic JSON report containing only configuration, hashes, counts, aggregate
metrics, paired differences, confidence intervals, exclusions, and failure summaries. Marketing
charts must be generated from this report, and every displayed number must identify the report
and command that produced it.

## Failure and safety rules

Fail closed and preserve the checkpoint when any of these occurs:

- exact Luna model cannot be selected
- authentication is absent or invalid
- usage accounting required for the experiment is missing
- output violates the answer schema
- a response relies on tools or repository files
- the explicit hosted-call ceiling would be crossed
- the configured zero-or-one transport retry is exhausted
- the service reports quota, billing, or rate-limit exhaustion

Canonical smoke, pilot, and full commands use zero retries. A retry-enabled exploratory run gets a
larger projected ceiling. Every started call is reserved durably before launch, so restarts cannot
reset the budget; if crashes or retries exhaust that bound before completion, the run is terminal
under that binding and must not be presented as complete evidence.

Never print credentials or authentication files. Never silently switch models, reasoning effort,
datasets, prompts, or retry policy.

## Execution sequence

1. Run the offline unit and fake-client tests.
2. Run a no-network dry run and inspect the projected calls and artifact paths.
3. Run the one-task smoke stage with a ceiling of six calls.
4. Verify exact model identity, structured answers, token counters, checkpoint resumption, and
   absence of tool use.
5. Run the five-task pilot and inspect paired task-level results.
6. Freeze the runner revision and configuration.
7. Run three repetitions of the full 26-task set.
8. Generate the aggregate report, confidence intervals, and chart source data.
9. Independently recalculate every public number from the aggregate report before updating
   README marketing material.

## Runner commands

Install the optional hosted adapter in the benchmark environment (`pip install "engraphis[hosted-eval]"`),
then inspect the zero-call plan before authorizing anything:

```bash
python -m eval.hosted_luna --dry-run
python -m eval.hosted_luna --smoke --max-hosted-calls 6 \
  --private-records .private-eval/luna-smoke.jsonl \
  --public-report .hosted-eval-results/luna-smoke.public.json
python -m eval.hosted_luna --pilot --max-hosted-calls 30 \
  --private-records .private-eval/luna-pilot.jsonl \
  --public-report .hosted-eval-results/luna-pilot.public.json
python -m eval.hosted_luna --full --max-hosted-calls 468 \
  --private-records .private-eval/luna-full.jsonl \
  --public-report .hosted-eval-results/luna-full.public.json
```

The runner accepts only `gpt-5.6-luna` and starts a fresh empty-directory read-only Codex thread
for each attempt. The checkpoint path is private and must not be committed. The command writes a
content-free public evidence artifact and prints its path and SHA-256 checksum. Full-run strategy
order rotates across repetitions so each strategy runs first, second, and third once. A full run
must use an explicit ceiling calculated by the dry run; no model call is made by this repository's
tests. Repo-local generated reports must stay under the ignored `.hosted-eval-results/` directory
so they do not change the repository fingerprint and invalidate a resumable run; copy a vetted
public artifact elsewhere only after the run is complete.

## Merge gate

Before merging the automation:

- all offline tests and retrieval evaluation gates pass
- the hosted dependency remains optional and is imported lazily
- Python 3.9 core compatibility is unchanged; the hosted runner states its newer requirement
- dry-run and fake-client tests make no network calls
- interrupted runs resume without duplicating completed attempts
- public artifacts contain no raw contexts, answers, prompts, credentials, or user data
- documentation distinguishes deterministic fixture evidence from hosted-model evidence
