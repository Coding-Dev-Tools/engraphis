# Handoff — Memory-Systems Research & Competitive Gap Analysis (2026-07-01)

**What this is:** an external research pass (SOTA agentic-memory systems, mid-2026) cross-checked
against the current engraphis codebase, plus a prioritized punch list to close the gap and get
closer to a sellable product. This is a *strategy/roadmap* handoff, not a session recap — read
it alongside, not instead of, the existing docs:

- `AGENTS.md` — architecture and conventions (still current, verified against code this pass).
- `HANDOFF-2026-07-01.md` — most recent coding-session state (commits, landmines, git status).
- `RELEASE_READINESS.md` — quality gate + pre-monetization checklist.
- `docs/GO_TO_MARKET.md` — positioning and pricing (still largely correct; this doc refines it).

Everything below was verified against the working tree, not assumed: `engraphis/mcp_server.py`
(15 `@mcp.tool` functions confirmed via `grep -c`), `engraphis/core/scoring.py` and
`core/resolve.py` (formula and thresholds — `RELATED_SIM_FLOOR=0.15`, `SUBJECT_TOKEN_JACCARD=0.40`,
`DUP_TOKEN_JACCARD=0.85` — read directly from source), `engraphis/static/index.html` (549 lines,
single-file vanilla-JS dashboard), and `tests/` (17 files, 1,699 lines).

---

## 1. Verdict up front

The core retrieval/scoring engine is **architecturally sound and, on paper, competitive** with
the named field (mem0, Zep/Graphiti, Letta/MemGPT) on the dimensions that matter most for the
self-hosted/coding-agent niche: bi-temporal truth, deterministic conflict resolution, scoping,
and a code-symbol graph nothing else in the category has. It is **not yet competitive on two
things buyers will actually check**: published benchmark numbers, and a UI that looks like a
product rather than a working prototype. Both are fixable without a rearchitecture. Neither is
a surprise — `RELEASE_READINESS.md` already flagged the benchmark gap; this pass adds fresh
external numbers to size it, plus a concrete UI critique and an updated technical roadmap based
on what the field did in the last ~12 months (entity-linked multi-signal retrieval, memory
evolution, sleep-time consolidation) that engraphis doesn't have yet.

---

## 2. State of the art, mid-2026 (what the leaders actually do now)

### mem0 — the volume leader (41k GitHub stars, AWS Agent SDK's default memory provider)
Shipped a new "token-efficient" algorithm in April 2026 that changed its architecture in ways
worth tracking:
- **Single-pass ADD-only extraction** — an LLM call per turn distills facts and *accumulates*
  them rather than running UPDATE/DELETE classification on every write (a reversal from the
  original 2025 ADD/UPDATE/DELETE/NOOP design in their ECAI 2025 paper).
- **Entity linking across memories** — entities are extracted, embedded, and linked so retrieval
  can boost on entity matches, not just semantic similarity.
- **Multi-signal retrieval** — semantic (vector) + BM25 keyword + entity match, scored in
  parallel and fused. This is structurally the same shape as engraphis's vector+lexical+graph
  RRF fusion, arrived at independently.
- **Published numbers:** 92.5 on LoCoMo, 94.4 on LongMemEval, under ~7,000 tokens per retrieval
  call, p95 search latency 0.2s.

### Zep / Graphiti — the temporal-graph leader
Builds a genuine temporal knowledge graph (not just bi-temporal *fields* on flat records —
graph edges themselves carry validity windows), with graph-based multi-hop retrieval. Reported
63.8% on LongMemEval (GPT-4o) vs. mem0's older-generation 49.0% on the same eval — the gap was
specifically attributed to temporal reasoning quality, not raw recall. Memory footprint is much
larger (600k+ tokens/conversation) — Zep trades token efficiency for reasoning depth. Notably,
**Zep deprecated self-hosted Community Edition** — this is the opening `GO_TO_MARKET.md` already
identifies and it's still open.

### Letta (MemGPT) — the OS-metaphor / sleep-time leader
Treats the LLM as an OS managing its own memory (main context / recall store / archival store),
with the model deciding what to page in/out via function calls. The newer development worth
tracking: **sleep-time compute** — a second, background agent asynchronously consolidates and
reorganizes memory while the primary agent keeps serving requests in real time, rather than
doing all extraction/consolidation synchronously on the write path. This is the production
answer to "when does distillation happen without blocking the write."

### A-Mem — the memory-evolution / Zettelkasten approach (research, increasingly cited)
Each memory is a note with LLM-generated keywords/tags/context, linked bidirectionally to
related notes. The distinguishing idea: **memory evolution** — writing a new note can trigger
*updates to existing notes*, so the network's understanding of old memories improves as new
ones arrive, not just the reverse. Engraphis has the *primitive* for this (`Store.add_link` /
`engraphis_link`) but not the *automatic* trigger — AGENTS.md §6 already names this gap exactly
("neighbors don't get auto-updated when a new note changes how they should be understood").

### HippoRAG — the graph-retrieval reference
Builds a knowledge graph from OpenIE triples and retrieves via **Personalized PageRank** seeded
from query-linked nodes, in a single step, rather than multi-hop traversal or 1-hop expansion.
This is the standard the field measures "real" graph memory against — engraphis's current graph
arm (1-hop entity expansion, documented as a deliberate Phase-1 scope choice in AGENTS.md §2) is
one step below this, and one step below Graphiti's approach too.

### Benchmarks now standard in the field
LoCoMo and LongMemEval are the two references everyone quotes; BEAM (1M/10M context) is
emerging for scale claims. Current public numbers cluster around 90–95% on LoCoMo/LongMemEval
for the leaders (mem0's new algorithm, ByteRover 2.0 at 92.2–92.8%). **Engraphis has never run
either benchmark** — `eval/harness.py` + `eval/ablation.py` return 1.000/1.000/1.000 on tiny,
self-authored fixtures (`sample.jsonl`, `codemem.jsonl`, 30 questions total). That's a pipeline
correctness check, not a competitive claim, and `RELEASE_READINESS.md` already says so — but it
means today engraphis has **zero externally-comparable numbers** while the entire category now
publishes them as a matter of course.

Sources: [mem0 token-efficient algorithm](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm), [mem0 temporal reasoning update](https://mem0.ai/blog/the-token-efficient-memory-algorithm-now-has-temporal-reasoning), [mem0 2026 benchmarks](https://mem0.ai/blog/ai-memory-benchmarks-in-2026), [mem0 arXiv paper](https://arxiv.org/abs/2504.19413), [Zep/Graphiti vs mem0 comparison](https://rohitraj.tech/en/notes/open-source-ai-agent-memory-mem0-vs-zep-letta-2026), [Letta sleep-time compute](https://www.letta.com/blog/sleep-time-compute/), [A-Mem paper](https://arxiv.org/html/2502.12110v1), [Arize A-Mem glossary](https://arize.com/glossary/agentic-memory-a-mem/), [ByteRover LoCoMo benchmark](https://www.byterover.dev/blog/benchmark-ai-agent-memory), [Atlan 2026 framework comparison](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/).

---

## 3. Engraphis vs. the field — gap table

| Capability | mem0 | Zep/Graphiti | Letta | Engraphis today | Verdict |
|---|---|---|---|---|---|
| Fact extraction from raw text | LLM, automatic | LLM, automatic | Model-driven (agentic) | **None** — caller passes pre-distilled text to `remember()` | Gap |
| Conflict resolution | LLM-classified ADD/accumulate | Temporal-graph edges | Self-edited by model | Deterministic token-Jaccard (`resolve.py`, thresholds 0.15/0.40/0.85) | Different, not worse — but ceiling on paraphrase detection |
| Temporal truth | Recent addition | Core strength (edge-level validity) | Partial | Bi-temporal on every record (`valid_from/valid_to` + `ingested_at/expired_at`), surfaced via `why`/`timeline` tools | **Competitive**, arguably a strength — few competitors expose supersession chains this directly in a tool |
| Graph retrieval | Entity-linking (flat) | Multi-hop temporal graph | N/A | 1-hop entity expansion only | Gap vs. Zep, roughly parity with mem0 |
| Memory evolution (new writes update old notes) | No | Partial | No | Linking primitive exists, no auto-trigger | Gap (matches AGENTS.md's own note) |
| Background consolidation | No | No | **Sleep-time compute (shipped)** | Not started (Phase 4) | Gap |
| Code-aware memory | No | No | No | **Code-symbol graph** (`backends/codegraph.py`, tree-sitter + regex fallback) | **Engraphis-only** — the real wedge |
| Local-first / self-hosted, no metering | No (hosted-first) | No (self-host CE deprecated) | Yes (self-hosted, full features) | Yes, numpy-only core | Competitive, matches Letta's best positioning |
| Published benchmark numbers | 92.5 / 94.4 (LoCoMo/LongMemEval) | 63.8 (LongMemEval) | N/A public | **None** | **Biggest gap** |
| Governed forget/pin/correct | Partial | Partial | Yes | Yes, audited, never hard-delete | Competitive |
| Product UI | Cloud dashboard | Cloud dashboard | Cloud + self-hosted ADE | Single-file vanilla-JS local dashboard (v1, legacy) | Gap — see §5 |

---

## 4. Technical roadmap, prioritized

1. **Run a real benchmark.** This is the single highest-leverage thing left — it's also already
   priority #1 in `RELEASE_READINESS.md` and `HANDOFF-2026-07-01.md`, and this research pass
   confirms *why*: every credible competitor now publishes LoCoMo/LongMemEval numbers, so "no
   number" reads as "worse," even where the architecture is fine. Wire the real
   `sentence-transformers` embedder + LoCoMo or LongMemEval-S into `eval/harness.py`'s existing
   structure and publish whatever comes out, good or bad — a mediocre real number is more
   credible than a perfect fixture score.

2. **Optional LLM-based fact extraction, kept behind an interface.** Every SOTA system
   (mem0, A-Mem, Letta) auto-distills raw text into facts; engraphis requires the caller to
   pre-distill. This is a real usability gap for anyone not writing careful `remember()` calls
   by hand. Fix without breaking the numpy-only core guarantee (AGENTS.md §3.8): add an
   `Extractor` protocol to `core/interfaces.py`, a no-op passthrough as the offline default, and
   a pluggable LLM-backed implementation behind the existing `LLM` interface — config change,
   not a refactor, matching how every other backend swap in this codebase already works.

3. **Cheap semantic upgrade to `resolve.py` before reaching for an LLM judge.**
   `RELEASE_READINESS.md` already documents the known ceiling (Jaccard misses paraphrased
   contradictions). A middle step that doesn't require an LLM call: use the *embedding cosine
   similarity* already computed for the vector-neighbor search as an additional signal alongside
   token-Jaccard in `resolve()` — still deterministic and offline, catches paraphrase cases
   Jaccard alone misses, and is a smaller change than wiring in an LLM judge. Reserve the LLM
   judge as an *optional* upgrade behind the same `Extractor`/`LLM` interface from item 2.

4. **Automatic memory evolution on write** (A-Mem-style), building on the existing
   `Store.add_link`/`engraphis_link` primitive: when `resolve()` finds related neighbors above
   `RELATED_SIM_FLOOR`, don't just decide ADD/NOOP/INVALIDATE for the new memory — optionally
   re-score or re-tag the neighbors it's related to, so the network improves bidirectionally.
   This is explicitly named as unfinished in AGENTS.md §6; it's a natural extension of code that
   already exists rather than new surface area.

5. **Graph arm: move from 1-hop expansion toward Personalized PageRank**, per the Phase 2 plan
   already in `MASTER_PLAN.md` §18 and `docs/IMPLEMENTATION.md`. This is the one item on this
   list that's a genuine size-of-effort project, not a small addition — sequence it after 1–4.

6. **Consolidation / "sleep-time" loop (Phase 4, not started at all).** Letta's shipped
   sleep-time compute is validation that this pattern is now table stakes for "long-running
   agent" positioning, not a research curiosity. Given engraphis's local-first stance, frame it
   as a local background job (not a cloud service) — e.g. a `scripts/consolidate.py` that runs
   episodic→semantic distillation via the same optional `LLM` interface, schedulable by the
   user (cron / Task Scheduler), consistent with "your machine, your control."

None of items 1–4 require new hard dependencies in `core/`; all fit the existing
interface-injection pattern. Item 5 is the only one that's architecturally nontrivial.

---

## 5. UI/UX assessment

Current state (`engraphis/static/index.html`, verified by direct read): a single 549-line file,
vanilla JS, no build step, no framework — CDN-loaded `marked.js` + `DOMPurify` (post the XSS
fix) + `vis-network` for the graph view. Eleven views exist: Overview, Memories, Import, Recall,
Chat, Thoughts, Graph, Timeline, Vaults, Health, Settings. Dark theme with CSS variables, a
working spinner/empty-state/toast pattern, and a `@media(max-width:1000px)` breakpoint — this is
a genuinely functional local dev dashboard, not a stub. But it has three concrete problems for a
sellable product:

- **It's the v1 legacy surface**, not built on the v2 service layer — GO_TO_MARKET.md §10
  already flags this: it's the best candidate for a first Pro deliverable, but today it's a
  single-user local tool with no hosting or auth story, sitting on the codebase side that
  AGENTS.md says not to build new capability on.
- **No accessibility pass**: no `aria-label`s on nav/buttons, SVG icons with no `<title>`/`role`,
  color-only status indicators (health-dot, vault-dot) with no text fallback. This isn't
  cosmetic — it's a WCAG gap that will surface in any procurement/compliance review for the
  regulated-team ICP `docs/GO_TO_MARKET.md` names as an expansion segment.
- **Fixed-width design (≥1000px assumed)**, single monolithic file with no componentization —
  fine for a local tool one person opens on a laptop, not something you can iterate on as a team
  or extend into a hosted multi-user product without a rewrite anyway.

**The most sellable thing this UI could show that nobody else's does well: the bi-temporal
supersession chain.** `why`/`timeline` are already unique tools; the Timeline view exists but per
the dashboard read is a flat chronological feed. A visual "this fact superseded that fact, here's
when and why" diff view — literally rendering the ADD/NOOP/INVALIDATE decision history from
`resolve()` — would be a distinctive screenshot for the landing page and demo, because it's the
one capability in the gap table where engraphis is ahead, not catching up.

**Recommendations, in order:**
1. Don't polish the v1 dashboard further as a Pro bet — it's built on the codebase side AGENTS.md
   says to deprecate. Rebuild the Pro "Memory Inspector" against the **v2 `MemoryService`**
   layer that already backs the MCP server, so the UI and the AI-facing tools stay in sync by
   construction instead of by discipline.
2. Keep it simple technically (a lightweight SPA — Vite + vanilla or a minimal framework — is
   enough; no need for a heavy stack), but componentize it and run an accessibility pass from
   day one rather than retrofitting.
3. Build the supersession-chain / "why did this change" view first — it's the highest-leverage
   screen for both the sales demo and actual daily use, and it's demonstrating a real
   capability, not a mockup.
4. Add multi-user login as the actual gate for Pro (per GO_TO_MARKET.md §10's own
   recommendation) — this turns "polished UI" into "something worth $20/seat" rather than a
   nicer free feature.

---

## 6. Commercial / go-to-market refinement

`docs/GO_TO_MARKET.md` is thorough and its core call — own self-hosted + coding-agent-native +
no-per-token-cost, since Zep abandoned self-hosting and Letta/mem0 are cloud-first — still holds
up against this pass's fresh research. Three refinements from the SOTA scan:

- **Lead with the code-symbol graph, not just "memory."** Nothing in the mem0/Zep/Letta/A-Mem/
  HippoRAG research surfaced a competitor targeting code-aware memory for coding agents
  specifically. Every other differentiator (bi-temporal, deterministic resolution, local-first)
  is "we do X better/cheaper"; the code graph is "we do X and nobody else does it at all." The
  landing page / demo script should open with an agent using `engraphis_search_code` /
  `index_repo`, not the generic remember/recall loop.
- **Publish a number even if it's not class-leading.** The field moved fast in the last year —
  mem0 went from a 49% LongMemEval baseline to 94.4% in about a year by shipping entity linking
  and multi-signal retrieval, both of which engraphis's RRF fusion already does structurally.
  A real, even middling, LoCoMo/LongMemEval number changes the pitch from "trust us" to "here's
  the number, here's the architecture, here's why it'll keep improving" — buyers weight the
  second framing far more than polish.
- **The why/timeline capability is underpriced in the current positioning.** GO_TO_MARKET.md
  frames bi-temporal history as parity with Zep/Graphiti. Given how UI-forward Zep's own
  positioning is around "knowing when things changed," and that engraphis exposes this as two
  dedicated MCP tools rather than a general graph query, this is closer to a distinct selling
  point than a checkbox — worth a dedicated line in the pitch, not just a table row.

Everything else in `docs/GO_TO_MARKET.md` §5–§10 (pricing anchors, the "don't price Pro until
something is shipped" discipline, the dashboard-as-first-Pro-deliverable call) still stands and
doesn't need restating here.

---

## 7. Punch list (do these, roughly in this order)

1. Run a real LoCoMo or LongMemEval-S pass with the actual `sentence-transformers` embedder;
   publish the number honestly, whatever it is. *(Highest leverage; already flagged elsewhere,
   reconfirmed by this pass.)*
2. Add embedding-cosine as a second signal in `core/resolve.py` alongside token-Jaccard —
   small, deterministic, closes the "misses paraphrased conflicts" known limitation partway.
3. Add an optional `Extractor` interface for LLM-based fact distillation (config-gated, offline
   default = passthrough) — closes the "no fact extraction" usability gap.
4. Wire automatic neighbor re-scoring into `resolve()`'s related-memory path (memory evolution) —
   extends code that already exists (`add_link`).
5. Decide the Pro-dashboard rebuild: target `MemoryService` (v2), not v1; scope a
   supersession-chain view + accessibility pass + multi-user login before calling it Pro-ready.
6. Scope the PPR graph-arm upgrade and the consolidation/sleep-time loop as their own tracked
   work — bigger, sequence after 1–5.
7. Update `docs/GO_TO_MARKET.md`'s demo script to open with the code-graph, not generic recall.

---

## 8. What's still accurate and doesn't need re-litigating

The existing `RELEASE_READINESS.md` "Before you charge money" list (trademark clearance,
encryption-at-rest, per-token tenant auth, CI matrix/PyPI publish, v1 input hardening) is
unaffected by this research pass and remains the non-memory-specific pre-monetization checklist.
This document is additive to that one, focused specifically on retrieval/memory quality and
product surface, not security or packaging.
