# Additional benchmark diagnostics

This document records reproducible setup and the execution status of three upstream memory and
agent diagnostics. Their measurements are separate from the historical fixture registry and from
upstream agent, judge or action scores. Local semantic retrieval execution began on 2026-09-16:
the Mem2ActBench small slice and two MemoryAgentBench splits are complete; the remaining
MemoryAgentBench splits and LoCoMo-Plus are scheduled serially.
No upstream agent, LLM judge, generated tool call or paid model request has run. `DATA_READY`
describes source preparation; completed local retrieval does not change the upstream score status.

The public-safe source lock is
[`eval/configs/additional-benchmark-sources.json`](../eval/configs/additional-benchmark-sources.json).
It records relative private filenames, source URLs, revisions, SHA256 digests, and cardinalities.
The corresponding raw and derived inputs live under the ignored
`.private-eval/benchmark-20260915/additional-data/` directory and must not be published. A source
branch name alone is not an acceptable future campaign pin.

The executable preflight also applies the harness's dataset validation. MemoryAgentBench repeats
some accepted-answer strings and reuses upstream QA IDs across context variants. The loader now
deduplicates only exactly equal accepted strings, preserving their order, and qualifies colliding
QA IDs with the source case and ordinal while retaining the original ID privately. No question,
answer alternative or source context is discarded, and original dataset bytes remain unchanged.

The stricter Mem2ActBench harness check found three further rows with partially missing gold
sources after the initial 17 malformed/unmatched exclusions. The original 400 rows and initial
383-row conversion are retained privately. The validated v2 input has 380 questions, 482 memory
records across those cases, and 20 total exclusions with four explicit reason categories in the
source lock. Exclusions are source limitations, not scored product failures.

## What the Engraphis adapter measures

[`eval/agent_benchmarks.py`](../eval/agent_benchmarks.py) translates supported exports into the
existing deterministic retrieval harness. It can report rank-sensitive retrieval and context
coverage with the default offline hashing embedder, or with an explicitly pinned local embedding
model. It does not reproduce upstream model orchestration, answer judging, summarization judging,
or tool execution. An adapter artifact must retain its source revision, dataset hash, reader
identity, embedding identity, and claim boundary before it can support a public number.

The supported command shape is:

```text
python -m eval.agent_benchmarks --dataset <JSON-or-JSONL> --format memoryagentbench --k 5 --json <report.json> --artifact <artifact.json>
python -m eval.agent_benchmarks --dataset <JSON-or-JSONL> --format locomo_plus --k 5 --json <report.json> --artifact <artifact.json>
python -m eval.agent_benchmarks --dataset <QA-JSONL> --format mem2actbench --conversations <conversation-JSONL> --k 5 --json <report.json> --artifact <artifact.json>
```

`--artifact` is the redacted immutable envelope path. It is suitable for audit only after the
input data, reader, model, and report have been independently checked. `--embed-model` requires a
lowercase 40 character immutable `--embed-revision`; a mutable model tag cannot be promoted to a
public result. `--no-resolve` is available when the campaign deliberately measures retrieval
without write-path conflict resolution.

## Pinned upstream sources

| Benchmark | Exact code and data revision | License and data prerequisites | Upstream boundary | Engraphis status |
|---|---|---|---|---|
| [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench/tree/fe1735de8cf8b9908e1e3d3b5612afc815698062) | Code `fe1735de8cf8b9908e1e3d3b5612afc815698062`; [Hugging Face dataset](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/tree/7ea066982b140a19337e17e60d45d4076e042faf) revision `7ea066982b140a19337e17e60d45d4076e042faf`. The four split files and derived hashes are in the source lock. | Repository MIT; dataset card reports MIT. Upstream recommends Python 3.10.16 and its `requirements.txt`. The official model runs require provider credentials and model settings. | The upstream README maps task-specific exact or substring matching and judge-based tasks. Engraphis reports retrieval and answer-token context coverage only. | `DATA_READY`: four JSONL splits, 146 cases and 3,671 questions, passed the loader. `PARTIAL`: local retrieval is queued; no upstream agent or judge score exists. |
| [LoCoMo-Plus](https://github.com/xjtuleeyf/Locomo-Plus/tree/059f4e3d38f7f1f96765e8e2cb7de3097551bffb) | Code and checked-in data at `059f4e3d38f7f1f96765e8e2cb7de3097551bffb`; the two source files, builder scripts, and derived hash are in the source lock. | GitHub metadata does not declare a license at this revision. Verify redistribution rights before copying or publishing data. Upstream requires Python 3, the two source JSON files, and an OpenAI-compatible API key/base URL for its generation or judge path. | The upstream README describes six categories and an LLM judge with correct `1`, partial `0.5`, wrong `0`. Engraphis supports the unified-input cue-to-evidence retrieval slice and does not report that judge score. | `DATA_READY_COGNITIVE`: the pinned builder produced 2,387 unified rows; the default Cognitive slice has 401 cases and passed the loader. `PARTIAL`: local retrieval is queued; no upstream reader or judge score exists. The all-category strict loader remains blocked by upstream missing-answer and missing-evidence rows. |
| [Mem2ActBench](https://github.com/Cantaloupe-M/Mem2ActBench/tree/b00726940b5abbe9bd324bdd7a2cb272f5c62a29) | Code revision `b00726940b5abbe9bd324bdd7a2cb272f5c62a29`. The checked-in small pair and derived hashes are in the source lock. | Repository MIT. The small QA and conversation files are sufficient for adapter plumbing. Upstream construction and model runs require its Python dependencies, an OpenAI-compatible credential/base URL, and upstream source or extraction artifacts. | The upstream task targets memory-grounded tool calls. Engraphis maps the expected tool-call JSON into evidence text and measures retrieval/context coverage; it does not execute a tool or claim action success. | `DATA_READY_WITH_EXCLUSIONS`: the raw 400 QA rows are retained privately; 380 strict rows passed the loader after 20 explicit malformed or unmatched rows were excluded. `COMPLETE` for the small local retrieval diagnostic; no generated tool call or upstream action score exists. |

## Prepared input inventory

The preparation stage downloaded pinned public files, converted their formats and validated the
loaders. Subsequent local retrieval execution uses the frozen queue and pinned embedding model;
its status is reported separately below. Original source bytes remain unchanged.

| Source | Private prepared input | Loader evidence | Caveat |
|---|---|---|---|
| MemoryAgentBench | Four JSONL split files derived from the pinned Parquet files | 146 cases, 3,671 questions | Split-level hashes and cardinalities are in the source lock. |
| LoCoMo-Plus | `prepared/locomo-plus/unified_input_samples_v2.json` with 2,387 rows | Default Cognitive selection: 401 cases and 401 questions | The 1,986 original rows are retained in the unified file. Strict all-category loading is blocked by an upstream missing answer and four missing evidence rows; no rows were silently repaired. |
| Mem2ActBench | Prepared QA JSONL plus the copied conversation and statistics JSONL | 380 cases, 380 questions, 482 memory records | The raw QA file has 400 rows. Twenty rows are recorded as explicit exclusions by reason in the private preparation metadata and source lock. |

The private data directory is ignored by Git. The tracked source lock contains only relative filenames,
source URLs, revisions, digests, and counts, so it does not redistribute raw or derived benchmark data.

## Primary methodology references

- [MemoryAgentBench paper](https://arxiv.org/abs/2507.05257) and its [pinned upstream README](https://github.com/HUST-AI-HYZ/MemoryAgentBench/tree/fe1735de8cf8b9908e1e3d3b5612afc815698062/README.md) define the four competency families and the upstream metric mapping.
- [LoCoMo-Plus paper](https://arxiv.org/abs/2602.10715) and its [pinned repository README](https://github.com/xjtuleeyf/Locomo-Plus/tree/059f4e3d38f7f1f96765e8e2cb7de3097551bffb/README.md) define the Cognitive extension, unified input, and judge protocol.
- [Mem2ActBench paper](https://aclanthology.org/2026.acl-long.370/) and its [pinned repository README](https://github.com/Cantaloupe-M/Mem2ActBench/tree/b00726940b5abbe9bd324bdd7a2cb272f5c62a29/README.md) define memory-grounded tool-call tasks and the paired session/QA export.

These pages are methodology references. Their reported results are not Engraphis results and are
not copied into the public registry.
## Reproducible setup recipes

The preparation and loader commands below have run against the private inputs. The example score commands
remain queueable examples and have not run in this audit.

### MemoryAgentBench

The official repository points to the Hugging Face dataset. The four pinned Parquet files were
converted with `pyarrow` in the isolated preparation environment at
`.private-eval/benchmark-20260915/additional-data/.prep-venv`. The resulting JSONL files are:

```text
python -c "from eval.agent_benchmarks import load_memoryagentbench; from pathlib import Path; root=Path('.private-eval/benchmark-20260915/additional-data/prepared/memoryagentbench'); print([(p.name, len(load_memoryagentbench(str(p)))) for p in sorted(root.glob('*.jsonl'))])"
python -m eval.agent_benchmarks --dataset .private-eval/benchmark-20260915/additional-data/prepared/memoryagentbench/accurate_retrieval.jsonl --format memoryagentbench --k 5 --json .private-eval/benchmark-20260915/additional-data/memoryagentbench-report.json --artifact .private-eval/benchmark-20260915/additional-data/memoryagentbench-artifact.json
```

The first command is the executed loader check. The second is a future retrieval-only score command.
The conversion and hashes are data preparation, not the upstream evaluation. Do not substitute a
mutable `main` snapshot, omit a split, or call the retrieval report an upstream accuracy result.

### LoCoMo-Plus

The official repository requires both source files to build its unified input. The pinned builder was
run in the isolated preparation environment, producing the private unified file:

```text
Set-Location .private-eval/benchmark-20260915/additional-data/raw/locomo-plus/data
& ..\..\..\.prep-venv\Scripts\python.exe unified_input.py
Set-Location ../../../../../../
python -c "from eval.agent_benchmarks import load_locomo_plus; print(len(load_locomo_plus('.private-eval/benchmark-20260915/additional-data/prepared/locomo-plus/unified_input_samples_v2.json')))"
python -m eval.agent_benchmarks --dataset .private-eval/benchmark-20260915/additional-data/prepared/locomo-plus/unified_input_samples_v2.json --format locomo_plus --k 5 --json .private-eval/benchmark-20260915/additional-data/locomo-plus-report.json --artifact .private-eval/benchmark-20260915/additional-data/locomo-plus-artifact.json
```

The builder and first Python command are the executed data and loader checks. The score command is
future retrieval-only work. The default adapter selection is the new `Cognitive` category. Pass
`--include-original-locomo` only when the report explicitly labels the five original categories
as a second slice. The upstream LLM judge is a separate attended experiment and requires its own
model, prompt, judge revision, and cost record.

### Mem2ActBench

The checked-in small pair is the lowest-cost plumbing input. The raw QA and conversation files were
copied privately. The public source lock records 400 raw rows, 380 retained rows, 20 explicit
source-preparation exclusions and 482 prepared memory records; these are input-preparation counts,
not product metrics emitted by the public run artifact.
Invoke the paired loader on the prepared files:

```text
python -c "from eval.agent_benchmarks import load_mem2actbench; print(len(load_mem2actbench('.private-eval/benchmark-20260915/additional-data/prepared/mem2actbench/toolmembench_small/qa_dataset_validated_v2.jsonl', '.private-eval/benchmark-20260915/additional-data/prepared/mem2actbench/toolmembench_small/toolmem_conversation.jsonl')))"
python -m eval.agent_benchmarks --dataset .private-eval/benchmark-20260915/additional-data/prepared/mem2actbench/toolmembench_small/qa_dataset_validated_v2.jsonl --format mem2actbench --conversations .private-eval/benchmark-20260915/additional-data/prepared/mem2actbench/toolmembench_small/toolmem_conversation.jsonl --k 5 --json .private-eval/benchmark-20260915/additional-data/mem2actbench-small-report.json --artifact .private-eval/benchmark-20260915/additional-data/mem2actbench-small-artifact.json
```

The first command is the loader check; the second illustrates the CLI. The executed frozen queue
used k=10, a 1,500-token budget, pinned MiniLM and `--no-resolve`, as recorded in its artifact.
It retained all 380 validated cases. The 20 declared exclusions remain visible in the public source
lock as preparation metadata. Retrieval coverage remains separate from actual tool execution
success.

## Metrics and reporting rules

The adapter report may contain rank-sensitive retrieval metrics such as recall@k, hit@k, MRR, and
nDCG, plus packed context usage and answer-token coverage diagnostics. A missing evidence list is
unscored for retrieval; it must not become a perfect row by default. For cognitive examples whose
reference answer is absent, report evidence retrieval only and mark answer quality unavailable.
For Mem2ActBench, expected tool-call JSON is an evidence target, not a generated action.

A future public artifact that publishes source-preparation metadata must include, at minimum:

- exact code commit and data revision or file digest for every source;
- source license status and the permitted redistribution boundary;
- split/cardinality and exclusion counts, with no silent filtering;
- reader, tokenizer, embedding model and immutable revision, vector backend, `k`, conflict policy,
  and context budget;
- deterministic seed, run count, paired comparison plan, confidence interval method, and failures;
- the adapter claim boundary, including retrieval-only, no upstream judge, and no tool execution;
- raw private outputs kept outside the public envelope, with public-safe checksums only.

No score from these adapters is publishable as an upstream benchmark score until the upstream
metric implementation and its model or judge are run from their own pinned source. The existing
Engraphis acceptance corpus, five-arm acceptance gate, capacity harness, and external benchmark
policy remain separate evidence tracks.

## Current decision

| Diagnostic | Decision | Reason |
|---|---|---|
| MemoryAgentBench | `PARTIAL` | Conflict-resolution (8 cases/800 questions) and test-time-learning (6 cases/700 questions) completed. Accurate retrieval and long-range understanding remain. No upstream agent or judge score exists. |
| LoCoMo-Plus | `PARTIAL` | The 401-row Cognitive slice is queued for local retrieval. All-category strict loading remains blocked by upstream missing-answer and missing-evidence rows. No upstream judge score exists. |
| Mem2ActBench | `COMPLETE` for the declared small retrieval diagnostic | All 380 validated cases completed. The public source lock records 20 preparation exclusions; no tool call or upstream action score was executed. |

The [Mem2ActBench artifact](benchmark-evidence/mem2actbench-small-20260916.json) and its SHA-256
sidecar record retrieved-source recall **99.82%**, packed-source recall **99.48%**, retrieved
answer-token coverage **41.13%** and packed coverage **41.03%**. The public artifact records 380
retained cases. The source-preparation lock additionally records 482 memory records and 20
exclusions; those figures describe input preparation, not product quality. The retrieval scores
describe one execution of this retained slice, with no confidence interval or action-success claim.
Small candidate sets make source recall
comparatively easy; a near-perfect source score is not proof of accurate tool arguments.

Two MemoryAgentBench retrieval diagnostics completed with k=10 and 1,500-token contexts:

| Split | Retrieval-scored questions | Retrieved recall | Packed recall | Packed hit rate | Packed answer-token coverage |
|---|---:|---:|---:|---:|---:|
| [Conflict resolution](benchmark-evidence/mab-conflict-resolution-20260916.json) | 800 / 800 | 27.96% | 26.51% | 69.63% | 71.17% |
| [Test-time learning](benchmark-evidence/mab-test-time-learning-20260916.json) | 568 / 700 | 5.38% | 5.38% | 81.16% | 44.71% |

The other 132 test-time questions lack usable retrieval labels and remain visible; all 700
have answer-token diagnostics. These are adapted source-evidence measures with one execution
and no reported confidence interval. Recall can be low while hit rate is high when many source
units are labeled relevant; neither number is an upstream learning, conflict-resolution or
generated-answer score. The declared `--no-resolve` ingestion also means this conflict split
does not exercise Engraphis's automatic correction resolution.

The [queue manifest](../eval/configs/benchmark-local-queue-20260916.json) binds the prepared input
bytes and producer source. Live progress is in
`.private-eval/benchmark-20260915/local-queue/status.json`. The statuses above are the launch
snapshot after Mem2ActBench completed, not automatic claims for later jobs.

Local retrieval-only execution is within the authorized campaign. The durable serial queue uses
the pinned MiniLM model, k=10, a 1,500-token context budget, distinct case checkpoints and explicit
`--no-resolve`; completed artifacts must still be inspected before making a quality claim.
Use `--checkpoint-dir <private-directory>` to retain work and `--token-budget 1500` to state the
context ceiling. Source/model/configuration drift stops recovery. Upstream model, judge or tool
evaluations remain separate work requiring their own compatible harness and spending approval.
