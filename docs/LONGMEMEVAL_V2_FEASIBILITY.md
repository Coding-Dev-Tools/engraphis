# Official LongMemEval-V2 feasibility

Status: **PARTIAL preflight; BLOCKED scored pilot and full matrix** pending suitable compute and
separate spending authorization. No official answer score has been produced.

The [official harness](https://github.com/xiaowu0162/LongMemEval-V2) was checked out at
`6f020ac2fc3275e46c706d3406e02c3ed79b7be2` in a separate Python 3.11.15 environment. Its evaluation
CLI starts successfully. `eval.longmemeval_v2_matrix` generated all six variants × five budgets
(256/512/1,024/2,048/4,096). Generating these configurations is not executing the matrix.

The pinned data revision is `f152293e235517d504809563c833d7190b8c713b` of
[`xiaowu0162/longmemeval-v2`](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2).
The dataset repository advertises 7,120,369,667 bytes including screenshot archives. No data or
model payload is silently replaced with a text-only or hashing substitute under the official label.

| Prescribed component | Immutable model revision | Weight shard bytes | Local feasibility |
|---|---|---:|---|
| Qwen/Qwen3.5-9B reader | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` | 19,306,310,880 | Exceeds 12 GiB VRAM before cache/runtime |
| Qwen/Qwen3-Embedding-8B | `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af` | 15,134,634,568 | Exceeds 12 GiB VRAM before runtime |

The host has an RTX 3080 Ti with 12,288 MiB VRAM and 34,221,301,760 bytes of physical RAM.
Combined unquantized weight bytes exceed physical RAM before runtime overhead. This rules out
the simple all-GPU configuration on this host; CPU offload, sequential unloading, WSL and
quantization have not been qualified. It does not prove every possible local configuration
impossible. [vLLM's installation documentation](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/)
also excludes native Windows support; a Linux/WSL serving environment would need verification.

The upstream judge defaults to **GPT-5.2 with medium reasoning**. The Luna contract used for the
coding campaign does not replace this official evaluator. The owner's later **Codex OAuth only**
instruction excludes the original API-key judge proposal. An official-compatible OAuth judge
path retaining the prescribed model has not been verified. The coding OAuth adapter must not
be represented as controlling an unmodified upstream judge automatically.

## Compute proposal for review

As checked on 2026-09-16, [Runpod pricing](https://www.runpod.io/pricing) lists A100 PCIe 80 GiB
at $1.59/hour. A feasibility reservation of one GPU for two hours is **$3.18**. Allow **$0.03**
for 100 GB of active disk over two hours at $0.10/GB/month, plus a $0.79 contingency: **$4.00
compute ceiling**, subject to confirming the actual offer before renting. This is a planning
estimate, not a purchased resource or a guarantee that two hours finishes the pilot.

Start with ten frozen questions in one variant and a single budget, retain the official reader
and embedding, and measure startup, ingestion, memory peaks, query time and judge call counts.
The original API judge estimate used the [official GPT-5.2 price](https://developers.openai.com/api/docs/models/gpt-5.2) of $1.75/M input
and $14/M output. A provisional judge cap of twenty calls, each at 32,768 input and 4,096 output
tokens, or $2.29376 at ordinary input rates, with a **$3.00** proposed allowance for cache-write
pricing and rounding. That API route and its combined **$7.00** proposal are now retired,
unexecuted and unapproved. The **$4.00 compute proposal** remains available for review; judging
must first be qualified through Codex OAuth without changing the official model. Subscription
usage cannot be priced as an API invoice. Zero automatic retries and durable pre-dispatch
reservations remain required. Oversized prompts stop instead of being clipped.

This estimate includes ingestion/reader compute on the rented GPU, judge evaluation, storage
and contingency. Corrections beyond the frozen official procedure are excluded and would need
a new budget. The full 30-cell compute/QA budget must be calculated from measured pilot rates,
actual question counts and retained usage. No guessed full-matrix duration is presented as an
estimate reliable enough to approve.
