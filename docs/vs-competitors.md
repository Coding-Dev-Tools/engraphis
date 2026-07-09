# Engraphis vs. mem0, Zep, and Letta

Landing-page comparison copy. Three page-ready sections (one per competitor) plus a combined
matrix. The angle is the same throughout and it's the honest one: **Engraphis owns
self-hosted + coding-agent-native + no per-operation metering.** Where a competitor is the better
pick, this doc says so — a comparison nobody believes sells nothing.

> ⚠️ **Pricing is a moving target.** All competitor prices below are as of **2026-06-30**.
> Re-verify every figure against the vendor's live pricing page before
> this copy is published anywhere public. Don't ship a stale number.
>
> ⚠️ **Engraphis's own paid tiers (Pro/Team/Enterprise) are coming soon — not for sale today.**
> The "flat per-seat ($20 Pro)" figures below are a *target price*, not a live checkout. The free
> core is available now; license keys are not sold yet.

---

## The one-paragraph version

Hosted memory clouds (mem0, Zep) make you send your agent's context to their servers and bill you
per memory or per retrieval. Agent frameworks (Letta) bundle memory inside a heavier runtime you
have to adopt whole. Engraphis is the opposite of both: a **drop-in memory layer that runs on your
machine**, free and Apache-2.0 at the core, with a native MCP server so coding agents plug in
without SDK glue — and a flat per-seat license instead of usage metering, because your data never
touches our servers to meter in the first place.

---

## Engraphis vs. mem0

**mem0** is an Apache-2.0 memory library with a hosted SaaS on top. It's the closest category
comparison and a genuinely good product — the difference is the deployment model and where the
paywall falls.

| | mem0 | **Engraphis** |
|---|---|---|
| Core license | Apache-2.0 | **Apache-2.0** |
| Default deployment | Hosted-first SaaS | **Self-hosted-first; local by default** |
| Pricing shape | Free → $19 Starter → $79 Growth → $249 Pro/mo | **Free core, flat per-seat ($20 Pro), no metering** |
| Billed per memory / retrieval | Yes (hosted) | **No — runs on your box** |
| Where your context lives | mem0's cloud (hosted tier) | **Your machine** |
| Graph memory | **Gated to a paid tier** | **In the free core** |
| Coding-agent integration | SDK glue | **Native MCP server (18 tools)** |
| Bi-temporal truth + `as_of` reads | Limited | **First-class (`why` / `timeline`)** |
| Code symbol graph ("what calls this") | No | **Yes (`index_repo` / `search_code`)** |
| Offline / air-gapped | No | **Yes** |

**The wedge:** mem0 gates a *real capability* — graph memory — behind its paywall, and its cost
model meters you per operation on the hosted tier. Engraphis keeps the entire recall engine
(graph included) free forever and never charges by the lookup, because it never holds your data.

**Pick mem0 if:** you want a managed cloud you don't operate, and shipping context to a third party
is acceptable for your use case. **Pick Engraphis if:** the data can't leave your box, you don't
want per-operation billing, or you want the graph arm without a subscription.

---

## Engraphis vs. Zep

**Zep** is a cloud, credit-metered memory service. It's capable, but it made one move that defines
this comparison: it **deprecated its self-hosted Community Edition**, vacating the on-prem/private
niche entirely.

| | Zep | **Engraphis** |
|---|---|---|
| Deployment | Cloud only (**self-hosted CE deprecated**) | **Self-hosted, first-class** |
| Pricing shape | Free (10k credits/mo) → $125 Flex (50k) → $375 Flex Plus (200k) → custom | **Free core, flat per-seat ($20 Pro)** |
| Billing model | Credit-metered per operation | **No metering — flat license** |
| Where your context lives | Zep's cloud | **Your machine** |
| Offline / air-gapped | No | **Yes** |
| Coding-agent integration | SDK | **Native MCP server** |
| On-prem / regulated / residency-bound | **No longer served** | **The core use case** |

**The wedge:** Zep's retreat from self-hosting is the single clearest opening in the category. Any
team that is disqualified from a hosted memory cloud — regulated, on-prem, air-gapped, residency-
bound — is a customer Zep has walked away from and Engraphis is built for. On price, Zep's entry
*paid* tier ($125/mo, credit-metered) sits far above a flat per-seat license.

**Pick Zep if:** you're happy in their cloud and its credit model fits your volume. **Pick
Engraphis if:** you need to self-host at all — that door is now closed on Zep's side.

---

## Engraphis vs. Letta (MemGPT)

**Letta** (formerly MemGPT) is an open-source *agent framework* with memory built in. Its free tier
is genuinely generous — self-hosted with *all* features — and Engraphis matches that philosophy
exactly rather than fighting it. The real difference is scope: framework vs. layer.

| | Letta | **Engraphis** |
|---|---|---|
| What it is | **Agent framework** with memory inside | **Drop-in memory layer** (bring your own agent) |
| Free tier | Self-hosted, **all features** | **Self-hosted, all features** (same philosophy) |
| Cloud pricing | Free → $20/mo Pro (20 agents) → enterprise | Free core, $20 Pro (Inspector/team), no metering |
| Adoption cost | Adopt the framework | **Add one MCP server; keep your stack** |
| Coding-agent-native (Claude Code, Cursor, …) | Via the framework | **Native MCP, no framework buy-in** |
| Code symbol graph | No | **Yes** |
| Bi-temporal truth + supersession view | Limited | **First-class + Inspector diffs** |

**The wedge:** Letta asks you to build *on* it; Engraphis asks you to *add* it. If you already have
an agent (or use Claude Code / Cursor / Cline / Zed / Windsurf), Engraphis is memory you bolt on in
one command without changing your architecture. Note Letta's free-tier stance — "self-hosted, all
features" — is exactly the line Engraphis holds too: the free core is never crippled; only
team/hosting/compliance is paid.

**Pick Letta if:** you want an opinionated agent runtime and are happy to build within it. **Pick
Engraphis if:** you already have an agent and just want to give it durable, local memory.

---

## Combined matrix

| Capability | mem0 | Zep | Letta | **Engraphis** |
|---|:--:|:--:|:--:|:--:|
| Open-source core | ✓ | — | ✓ | **✓** |
| Self-hosted, first-class | partial | **deprecated** | ✓ | **✓** |
| Runs offline / air-gapped | — | — | ✓ | **✓** |
| No per-operation metering | — | — | ✓ | **✓** |
| Graph memory in the free tier | — | n/a | ✓ | **✓** |
| Native MCP server for coding agents | — | — | partial | **✓** |
| Code symbol graph (what-calls-this) | — | — | — | **✓** |
| Bi-temporal truth + `as_of` reads | partial | ✓ | partial | **✓** |
| Supersession view with word-level diffs | — | — | — | **✓** |
| Drop-in (no framework buy-in) | ✓ | ✓ | — | **✓** |

_As-of 2026-06-30; competitor rows are best-effort from public pages and must be re-verified before
publishing. "partial" and "n/a" are deliberately conservative — when unsure, understate the
competitor's gap rather than overstate it._

---

## How to prove the differentiators (not just assert them)

Two of the rows above are backed by reproducible artifacts in this repo, which is what separates a
credible comparison from marketing:

- **Recall quality** — see [`BENCHMARKS.md`](../BENCHMARKS.md). The offline harness is a
  correctness floor; the LoCoMo / LongMemEval numbers are the head-to-head quality comparison (run
  them with a real embedder and publish the table there).
- **Supersession / bi-temporal truth** — the Inspector's supersession-chain view with word-level
  diffs (`engraphis-inspector`, :8710) is a live demo of a fact changing over time — the single
  most screenshot-worthy differentiator, and one no closed competitor exposes.
