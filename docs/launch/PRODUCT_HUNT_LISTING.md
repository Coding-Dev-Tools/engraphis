# Engraphis: Product Hunt Listing and Maker Strategy

**Product:** Engraphis  
**Maker:** Jaixii (Coding-Dev-Tools)  
**Target:** Product Hunt Launch Day (12:01 AM PT)

---

## 1. Product Title and Taglines

### Product Title
**Engraphis**

### Primary Tagline (<60 characters)
> **`Local memory engine for AI coding agents with zero drift`** (58 chars)

### Alternative Taglines
1. **Token Efficiency Angle:**  
   > *Cut agent context tokens by 71% with local-first memory* (56 chars)
2. **Reliability and Grounded Accuracy Angle:**  
   > *Persistent agent memory with bi-temporal facts and citations* (60 chars)
3. **Developer-First Architecture Angle:**  
   > *SQLite-native hybrid memory and knowledge graph for AI agents* (60 chars)

---

## 2. Product Descriptions

### Short Description (PH Teaser Field / Directory Snippet, ~250 chars)
> Stop letting AI coding agents burn tokens and forget critical decisions. Engraphis is an open-source, local-first memory engine for Claude Code, Cursor, and MCP agents with 4-arm hybrid recall, bi-temporal facts, and 71.1% context token reduction.

### Full Product Description (Product Hunt Listing Body)

**The context window is not a memory system.**

If you have used Claude Code, Cursor, Codex, or autonomous coding agents on large codebases, you know the problems:
* **Context amnesia:** As sessions grow, conversation compaction drops hard-won architectural decisions and bug fixes.
* **Flat vector noise:** Standard vector databases dump unranked snippets into prompt space without respecting repo or session boundaries.
* **Silent overwrites and hallucination:** Changing an auth scheme or dependency often causes the agent to overwrite historical facts without provenance, or cite hallucinated conventions.

**Engraphis fixes agent memory at the infrastructure level.**

Engraphis is a local-first, inspectable memory engine running on SQLite with offline local embeddings. Instead of dumping raw chat logs or naive vector embeddings into prompts, Engraphis structures agent memory into a native **workspace -> repo -> session -> memory** hierarchy with bi-temporal auditability.

### Key Architectural Capabilities

* **4-Arm Hybrid Recall Pipeline:** Queries execute across four deterministic retrieval arms: semantic vectors (cosine), lexical search (FTS5 BM25), graph personalized PageRank over entity relations, and code-aware symbol/call bridges. Arm scores are min-max normalized and fused via Reciprocal Rank Fusion (RRF).
* **Grounded Recall with Citations:** When an agent recalls project conventions, Engraphis answers strictly with `[1]`, `[2]` citations or **abstains** if semantic support falls below the confidence floor. No nearest-neighbor guesses masquerading as facts.
* **Bi-Temporal Fact Validity:** Real projects evolve. When decisions change (such as migrating from JWT to PASETO), old memories are invalidated with `valid_to` timestamps rather than overwritten. Agents can run time-travel queries (`as_of`) and inspect historical reasoning chains.
* **71.1% Context Token Reduction:** Structure-aware memory chunking and budget-capped packing drop top-5 retrieved context tokens from 740.3 to 214.3 per query. Serialized JSON-shape payloads drop by 54.7% without losing retrieval recall.
* **Smart MCP Server Built-in:** Native Model Context Protocol (MCP) tools (`engraphis_session`, `engraphis_recall_context`) allow any MCP-capable agent (Claude Code, Cursor, Antigravity, OpenCode) to read, recall, and hand off sessions cleanly.
* **Local-First and Inspectable:** Runs 100% locally on your machine with zero external API requirements. Inspect memories, entity graphs, and decay curves through the visual dashboard at `localhost:8700`.

### Pricing and Open Source Model
* **Free and Open Source Core (Apache-2.0):** The local memory engine, SQLite backends, local embeddings, visual dashboard, and MCP tools are 100% free and open source forever.
* **Engraphis Pro ($10/mo) and Team ($20/seat/mo):** Adds encrypted Cloud Sync across workstations, remote consolidation automation, team role management, and included access to the Jev advisory decision engine.

Get started in 30 seconds:
```bash
pip install "engraphis[all]"
engraphis-dashboard
```

---

## 3. Product Hunt Media and Video Submission

* **Product Hunt Video URL:** `https://www.youtube.com/watch?v=qw-rTnAA_Z4` (or `https://youtu.be/qw-rTnAA_Z4`)  
  *Title:* "Inspectable Memory Graph for Coding Agents | Engraphis" (live UI demo showing the knowledge graph in action).
* **Gallery Slides (1270x760 px):**
  1. Hero Card: `docs/launch/graphic-a-architecture.png`
  2. Benchmark Card: `docs/launch/graphic-b-token-benchmark.png`
  3. Knowledge Graph UI: `docs/launch/gallery-knowledge-graph.png` (1270x760)
  4. Context Reduction Flow: `docs/launch/gallery-context-efficiency.png` (1270x760)
  5. Benefit Flow: `docs/launch/gallery-benefit-flow.png` (1270x760)

---

## 4. Categories and Topic Tags

* **Primary Category:** Developer Tools
* **Secondary Category:** Artificial Intelligence
* **Product Hunt Topic Tags:**
  1. `Developer Tools`
  2. `Artificial Intelligence`
  3. `Open Source`
  4. `Productivity`
  5. `Tech`
  6. `Software Engineering`

---

## 5. Master Maker / Founder First Comment

*(Post immediately upon launch at 12:01 AM PT)*

```markdown
Hey Product Hunt! 👋 I'm Jaixii, creator of Engraphis.

I build software with AI agents all day (Claude Code, Cursor, Codex, and custom harnesses). Like many of you, I hit the exact same wall:

Agents work well inside short sessions, but lose track of project history over multi-week efforts.

Here is what breaks in practice:
1. **Compaction amnesia:** After 30 turns, conversation compaction drops earlier decisions. The agent reverts right back to patterns you told it not to use.
2. **Flat namespaces:** Most RAG setups treat an entire codebase as one flat bucket. Scratchpad notes bleed into repo rules, and staging configs end up in prod prompts.
3. **Silent overwrites:** When you migrate auth from JWT to PASETO, typical vector tools either overwrite the old record (erasing history) or retrieve both at once, confusing the agent.
4. **Token waste:** Dumping whole files into context burns money and slows down every response.

I built Engraphis to solve agent memory as a structured systems problem.

### How Engraphis works:

* **Local-First on SQLite:** Runs on your machine with SQLite, local embeddings, and zero mandatory cloud calls. Your code and memories stay private.
* **Scoped Hierarchy:** Every memory lives in a clear `workspace -> repo -> session -> memory` hierarchy. Agents only pull from their active scope, with clean handoffs across sessions.
* **4-Arm Hybrid Recall:** Engraphis fuses four retrieval arms: dense vector cosine, lexical BM25 (FTS5), graph Personalized PageRank over entities, and code symbol/call links using Reciprocal Rank Fusion (RRF).
* **Bi-Temporal Validity:** Facts carry world-time (`valid_from`/`valid_to`) and system-time. Contradictions close validity windows instead of deleting history. You can inspect how decisions evolved or query state `as_of` a prior git commit.
* **Grounded Recall or Abstain:** For verified facts, Engraphis returns extractive citations (`[1]`, `[2]`). When support is weak, it strictly abstains instead of guessing.

### Benchmark numbers (deterministic fixtures):
- **71.1% Token Reduction:** Structure-aware chunking dropped top-5 context size from 740.3 to 214.3 tokens per question with Recall@5 = 1.000.
- **54.7% Recall Payload Reduction:** Serialized JSON payload dropped from 24,590 to 11,138 tokens across 260 timed recalls.
- **Zero Hallucinations:** On grounded decision checks, 5/5 answerable queries grounded with citations, and 6/6 ungrounded probes were rejected cleanly.

### Getting started:
The core engine is Apache-2.0 and free forever:
```bash
pip install "engraphis[all]"
engraphis-dashboard
```

For developers who want multi-machine sync and managed decision acceleration (Jev), Engraphis Pro is $10/month with an email-confirmed 7-day free trial (no card required).

I am here all day for an open AMA. Ask me anything about agent memory architecture, token economics, bi-temporal indexing, SQLite embeddings, or MCP integrations.

What has been the most frustrating thing your coding agent forgot? Let me know below! 👇
```

---

## 6. Community Engagement Strategy and Product Hunt Policy Compliance

### Product Hunt Policy Compliance Rules
Product Hunt algorithms penalize vote solicitation. Violations trigger downranking.

* Do not ask for upvotes or votes.
* Do not offer perks or discounts in exchange for votes.
* Do not share direct auto-voting links.
* Acceptable phrasing:
  - "We launched Engraphis on Product Hunt today. I would appreciate your feedback on our memory architecture."
  - "Take a look at our launch discussion and share your perspective on the benchmark results."
  - "Join our AMA on Product Hunt today where I am answering technical questions on agent memory."
  - Always link directly to the post page: `https://www.producthunt.com/posts/engraphis`.
