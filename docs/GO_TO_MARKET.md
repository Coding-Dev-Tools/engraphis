# Engraphis — Go-to-Market & Pricing

> A working plan, not legal/financial advice. Pricing and licensing are business decisions;
> validate the trademark ("Engraphis") and license terms with a professional before charging.

## 1. One-liner & positioning

**Engraphis is the local-first memory layer for AI agents.** Self-hosted, private, free at the
core, with a native MCP server so coding agents stop forgetting across sessions and repos.

Positioning statement: *For developers and teams building with coding agents who can't or won't
ship their context to a hosted memory cloud, Engraphis is a self-hosted memory engine that gives
agents durable, scoped, explainable memory — unlike mem0/Zep/Letta, it runs entirely on your
own machine with no per-operation metering.*

## 2. The wedge & ICP

- **Beachhead:** individual developers and small teams using **Claude Code, Cursor, Cline, Zed,
  Windsurf**. They feel session amnesia most acutely; the MCP demo is undeniable (the agent
  remembers a convention across a restart).
- **Expansion:** privacy-/residency-sensitive teams (regulated, on-prem, air-gapped) who are
  disqualified from hosted memory clouds, and general-agent builders (support, voice, RAG apps).
- **Entry trigger:** "my agent keeps re-asking / re-learning the codebase," or "we can't send our
  context to a third-party cloud."

## 3. Competitive landscape (mid-2026)

| Product | Model | Pricing shape | Gap Engraphis exploits |
|--------|-------|---------------|------------------------|
| **mem0** | Apache-2.0 + SaaS | Free → $19 Starter → $79 Growth → $249 Pro/mo; graph memory gated to Pro | Hosted-first; you meter per memory/retrieval call |
| **Letta (MemGPT)** | OSS + cloud | Free (self-hosted, all features) → $20/mo Pro cloud (20 agents) → enterprise | Heavier agent framework, not a drop-in memory layer |
| **Zep** | Cloud, credit-based | Free (10k credits/mo) → $125/mo Flex (50k credits/mo) → $375/mo Flex Plus (200k credits/mo) → custom Enterprise; **self-hosted CE deprecated** | **Abandoned self-hosting** — leaves the on-prem/private niche open; priced well above a flat per-seat model |

_(Verified against vendor pricing pages, 2026-06-30 — re-check before publishing; these move.)_

**Takeaway:** don't out-feature the funded players broadly. **Own self-hosted + coding-agent-native
+ no-per-token-cost.** Zep's retreat from self-hosting is the clearest opening.

## 4. Business model — open-core (recommended)

Three layers:

1. **Free OSS core (Apache-2.0)** — the engine + **MCP server** + CLI + library. This is the
   distribution and trust engine; it's what makes the wedge demo work. Optimize for adoption.
2. **Paid Pro / Team** — the things teams need once memory is shared and persistent: shared/team
   memory sync, hosted option, Memory Inspector UI, SSO/RBAC, encryption-at-rest, audit/compliance
   exports, Postgres/Qdrant adapters for scale, and priority support/SLA. (These map to the
   project's own Phase 5 "team-deployable" milestone.)
3. **Optional managed cloud (later)** — only if you want a SaaS motion; not required to launch.

Because Engraphis is self-hosted (you don't hold customer data), prefer a **flat per-seat
commercial license or annual support contract** over usage metering — usage-based billing is
awkward when the data never touches your servers.

## 5. Suggested pricing (anchored to competitors)

| Tier | Price (anchor) | Who | What |
|------|----------------|-----|------|
| **Community** | Free, Apache-2.0 | Individuals, OSS | Full engine + MCP + CLI + library, self-hosted |
| **Pro** | ~$20/seat/mo | Power users, small teams | Memory Inspector UI, team memory sync, priority support |
| **Team** | ~$100–250/mo (≤10 seats) | Startups | SSO/RBAC, audit exports, scale adapters, SLA |
| **Enterprise** | Custom (annual) | Regulated/on-prem | BYOC, encryption-at-rest, security review, dedicated support |

Anchors: mem0 $19/$79/$249, Letta $20, Zep $125/$375 (credit-metered, not seat-based, so not a
clean apples-to-apples comparison — but its entry paid tier is still far above a flat per-seat
price). Keep a generous free core so adoption
compounds; gate on *team/compliance* features, not on core recall quality. Note Letta's own
free tier is "self-hosted, ALL features" — Engraphis should match that positioning exactly
(free core is never feature-limited, only team/hosting/compliance is paid) rather than mem0's
model of gating a real capability (graph memory) behind a paywall.

## 6. Free vs paid split (the open-core line)

- **Always free:** local engine, hybrid recall, MCP server, CLI, Python library, single-user REST
  server, bearer-token auth, Docker.
- **Paid:** multi-user/shared memory, hosted control plane, web Memory Inspector, SSO/RBAC,
  encryption-at-rest, compliance/audit exports, Postgres/Qdrant/LanceDB adapters, SLA support.

## 7. Launch motion

1. **Ship the demo** — a 60-second screencast: agent learns a convention, restarts, recalls it via
   the MCP server. This is the whole pitch.
2. **Developer-led distribution** — GitHub README + MCP registry listings + posts in the Claude
   Code / Cursor / MCP communities; "Show HN: self-hosted memory for coding agents."
3. **Docs site** — quickstarts for Claude Code and Cursor; "why self-hosted" page targeting the
   privacy/residency ICP.
4. **Design-partner Team tier** — 3–5 teams on the paid tier for case studies before public pricing.
5. **Benchmark report** — publish LoCoMo/LongMemEval numbers (with the real embedder) to establish
   credibility vs named competitors.

## 8. Metrics to watch

GitHub stars & MCP installs (awareness) → weekly active self-hosted instances (activation) →
Pro/Team conversions and design-partner logos (revenue) → retention of stored memories per
instance (stickiness).

## 9. Honest risks

- The space is funded (mem0 $24M, Letta $10M). Win a niche, not the whole market.
- "Memory for coding agents" is only credible **with the MCP server shipped** — it now is.
- Finish the trademark check and keep the core genuinely useful for free, or adoption stalls.

## 10. Reality check — does $20/mo/seat have anything to attach to *today*?

Short answer: **not yet, and that's fine — sequence it, don't fake it.** Everything the Pro row
in §5 lists (Memory Inspector UI as a *product*, team memory sync, priority support) is a
roadmap item, not a shipped, billable feature. What's actually true today:

- The **free core is genuinely strong**: hybrid recall, bi-temporal history (`why`/`timeline`),
  self-maintaining facts (deterministic conflict resolution), governance (forget/pin/correct,
  scope-checked), a code-symbol graph, and a 15-tool MCP server — all covered by 127 passing
  tests. That is a real, demonstrable advantage over "just a vector store," and it's free,
  matching Letta's "self-hosted = all features" positioning from §5.
- The **v1 dashboard** (the closest thing to a "Memory Inspector UI" that exists) had two real
  bugs fixed this pass: the Knowledge Graph fragmented entities across documents (clicking a
  node could open the wrong memory or none), and markdown rendering had no HTML sanitization
  (a stored-XSS hole via the same untrusted-content path the whole product's threat model is
  built around). Fixed and tested — see `CHANGELOG.md`. It is *not yet* a polished, hosted,
  supportable Pro product; it's a working local dev dashboard that ships in the same OSS repo.
- **Nothing here is multi-user, hosted, or has SSO/audit exports.** There is no billing
  integration and no design partner validating that anyone will pay $20/mo for what's listed.

**Recommendation:** don't price a Pro tier publicly until at least one of its listed features
actually exists as something distinct from the free core. The lowest-effort credible path from
here: take the now-working dashboard, hardened and hosted (so the customer doesn't run their own
server for *that* piece), add basic multi-user login, and sell *that* as Pro — it is the only
item in §5/§6's Pro list with a working prototype today. Everything else (SSO/RBAC, audit
exports, scale adapters) is still genuinely unbuilt. Until then, $20/mo is a well-anchored
*target price* (validated against Letta's identical $20 Pro price; Zep's entry paid tier is
actually **$125/mo** credit-metered, not $25 — that $25 figure is Flex's per-10k-credit overage
rate, not its base plan price, a distinction the first pass of this research got wrong; see
Sources below). Corrected, Zep is ~6x Engraphis's target, which if anything makes $20/mo look
conservative rather than aggressive — but Zep's usage-metered model isn't a clean comparison to
a flat per-seat price either way, so weight the mem0/Letta anchors more heavily than Zep's.

### Sources
mem0 pricing (mem0.ai/pricing); Letta pricing (letta.com, aiagentslist.com/agents/letta); Zep
pricing (getzep.com/pricing — fetched directly 2026-07-01; Flex is $125/mo for 50k credits/mo
with $25/10k-credit overage, Flex Plus is $375/mo for 200k credits/mo with $75/40k-credit
overage, free tier is 10k credits/mo — an earlier pass of this doc had mistaken the overage
rates for the base plan prices and understated the free tier's credit allowance 10x); Zep
self-host status (vectorize.io/articles/zep-alternatives); mem0/Zep/Letta benchmark
(rohitraj.tech/en/notes/open-source-ai-agent-memory-mem0-vs-zep-letta-2026); open-core model
(handbook.opencoreventures.com/open-core-business-model). mem0/Letta pricing verified
2026-06-30 against mem0.ai/pricing and letta.com; Zep corrected 2026-07-01 against a direct
fetch of getzep.com/pricing — re-check all three before publishing, these tiers move.
