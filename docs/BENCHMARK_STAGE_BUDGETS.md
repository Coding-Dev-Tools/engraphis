# Stage budgets and approval boundaries

Updated 2026-09-16 following the owner's **Codex OAuth only** instruction. The selected
reader/extractor is GPT-5.6 Luna, medium reasoning, through the native Codex ChatGPT sign-in.
API keys, inherited gateways and provider fallbacks are rejected. The amounts below are
**API-price usage proxies**, retained for continuity with the approved proposal; they are not
subscription charges or invoices. The [official model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
lists $0.20/M input and $1.20/M output tokens. Reservations use $0.25/M input to cover the
1.25× cache-write category. Provider-reported usage and the price proxy are stored separately.

Every call reserves the proxy for 32,768 input and 4,096 output tokens: **$0.013108**, rounded
up to integer microdollars. Codex's native interface does not expose a provider hard output cap;
the output allowance is checked against reported usage after completion. An overage invalidates
the call and stops execution. This does not guarantee a hard subscription-token ceiling.
At most one corrective reader call follows an initial failed oracle; transport
retries are zero. Each peer attempt also permits at most 32 budgeted internal extraction calls.
Local embedding, reranking, deterministic oracles and this PC's compute incur no API charge.
No model judge is included in the coding proposal, so semantic prose grading is not claimed.

The owner subsequently approved **only the core development pilot**, with an upper limit of
**$4.51111111**. Its frozen proposal and execution ledger retain the lower **$3.932400** ceiling
and maximum 300 calls. The private approval binds this campaign, stage and ledger location.
The OAuth successor deducts two unscored setup generations from those 300 calls. It retains
the failed setup check's $0.008346 proxy reservation and $0.000546 for the earlier setup usage,
leaving **298 calls and a $3.923508 proxy allowance** for the 150 scored attempts. The combined
allowance remains $3.932400 and never becomes an API-key spending authorization.
All peer, larger coding, official benchmark and rented-compute stages remain unapproved.
The earlier inherited API gateway returned HTTP 402 and is retired from this campaign. No
scored API-key call was dispatched. Native Codex OAuth authentication, Luna availability,
medium effort and response usage have now been verified. No account top-up is required by
this runner. The OAuth successor receipt retains the same core-only authorization and records
the owner's route change; it does not authorize any additional stage.

| Stage | Attempts | Initial reader | Correction maximum | Peer internal maximum | Total calls maximum | API-price proxy ceiling |
|---|---:|---:|---:|---:|---:|---:|
| Development pilot, five core arms | 150 | 150 | 150 | 0 | 300 | $3.932400 |
| Peer pilot, three companion arms | 90 | 90 | 90 | 1,920 | 2,100 | $27.526800 |
| Development, all eight arms | 1,920 | 1,920 | 1,920 | 15,360 | 19,200 | $251.673600 |
| Validation, all eight arms | 1,920 | 1,920 | 1,920 | 15,360 | 19,200 | $251.673600 |
| Held out, all eight arms, three repetitions | 17,280 | 17,280 | 17,280 | 138,240 | 172,800 | $2,265.062400 |

Each row requires a separate approval; only the first row is approved. The proxy sum is
$2,799.868800; it is not an API spending authorization or a subscription invoice estimate.
Development baselines, candidate reruns, additional corrections, paid
external diagnostics and new model graders are additional separately reviewed work. Unsupported
cells may make no calls; successful one-turn readers may use substantially less than the allowance.
The pilot measures actual internal call counts and usage before any larger stage is proposed.

The executable source of these figures is `eval.benchmark_campaign.budget_proposal` for the
frozen campaign manifest. The generated
[historical API stage proposals](../eval/configs/benchmark-stage-proposals-20260916.json) bind campaign
`fa7995b8ce8cc27a7aa57dc14cfe1361a542d716e66b85f7eb53f55e3ac0716d`. They retain their original
proposal status and are retained unchanged. They cannot be executed by the OAuth-only runner.
The subsequent core-only authorization is a separate private receipt.
Inspect a stage without executing it by passing `--stage <stage>` and
omitting `--execute`. The durable ledger path is fixed by the approval. Pending reservations
survive crashes and continue consuming the ceiling until reconciled; uncertain calls are not
silently reissued. The runner stops on budget exhaustion or a reported model mismatch.

The official LongMemEval-V2 feasibility proposal is separate and retains its prescribed models;
see [LONGMEMEVAL_V2_FEASIBILITY.md](LONGMEMEVAL_V2_FEASIBILITY.md). No rented compute is approved.
