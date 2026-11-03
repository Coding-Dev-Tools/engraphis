# Engraphis Product Hunt Visual & Media Production Kit

This document provides the complete media production specification for the **Engraphis Product Hunt Launch**, including:
1. **60-90 Second Product Hunt Demo Video Script & Storyboard** (Scene-by-scene, narration, screen cues, audio SFX).
2. **Product Hunt Gallery Slides Specification** (5 slides, 1270x760 px, layout, copy, UI mockups).
3. **Standalone Production-Grade SVG Graphics** (`docs/launch/graphic-a-architecture.svg` and `docs/launch/graphic-b-token-benchmark.svg`).

---

## 1. 60-90 Second Product Hunt Demo Video Script & Storyboard

- **Target Duration:** 80 seconds (paced for dynamic tech demo, ~140 wpm).
- **Target Resolution:** 1920x1080 (16:9) / 60fps.
- **Audio Mix:** Upbeat, crisp ambient synth/tech groove (ducked -14dB under voiceover), punchy mechanical UI keystrokes, subtle high-tech swooshes on scene cuts.
- **Tone:** Technical, direct, engineer-to-engineer, confident, no marketing fluff.

---

### Scene 1: The Hook (0:00 - 0:15)
- **Goal:** Immediately identify the single most frustrating bottleneck of AI coding agents (Claude Code, Cursor, Codex).

| Time | Audio / Voiceover (Spoken Narration) | Visual Screen Cues | On-Screen Text & SFX |
| :--- | :--- | :--- | :--- |
| **0:00 - 0:07** | *"Every time you open a new chat in Cursor or Claude Code, your agent forgets yesterday's hard-won architectural decisions."* | Split screen: Left side shows a Cursor terminal; right side shows Claude Code. The agent begins re-reading the entire 40,000-line codebase from scratch. A progress spinner churns. | **Text:** `CONTEXT AMNESIA DETECTED`<br>**SFX:** Low bass drone, quiet terminal keyclicks. |
| **0:07 - 0:15** | *"It blows your token budget re-reading files, hallucinates stale APIs, and ignores the bug you fixed two hours ago."* | Red overlay flashing over the token counter as it climbs rapidly past 80k tokens. Red warning banner pops up: *"Error: Rate limit reached / Stale context."* | **Lower Third:** `High Cost &bull; Stale Decisions &bull; 80k+ Context Bloat`<br>**SFX:** Warning buzz / glitch click. |

---

### Scene 2: The Problem in Cursor & Claude Code (0:15 - 0:35)
- **Goal:** Show the root failure of brute-force context stuffing versus true temporal memory.

| Time | Audio / Voiceover (Spoken Narration) | Visual Screen Cues | On-Screen Text & SFX |
| :--- | :--- | :--- | :--- |
| **0:15 - 0:25** | *"Context windows are getting larger, but shoving 100,000 tokens of raw source into every prompt isn't memory; it's expensive noise."* | Zoom into the terminal prompt. We see Claude trying to decide between two conflicting database migration patterns because both exist in git history. Claude outputs a conflicting hallucinated function. | **Callout:** `Naive Context Dumps = Context Drift`<br>**SFX:** High-speed scroll whoosh. |
| **0:25 - 0:35** | *"What coding agents actually need is bi-temporal truth: knowing what was true, what changed, and why it changed."* | The messy context dump shatters into structured cards with valid time horizons: `valid_from` &rarr; `valid_to`. | **Text Overlay:** `Bi-Temporal Validity: Truth Across Time`<br>**SFX:** Glass chime / snap transition. |

---

### Scene 3: The Engraphis Solution & Local Graph Dashboard (0:35 - 0:55)
- **Goal:** Introduce Engraphis, SQLite persistence, and show off the local web dashboard at `localhost:8700`.

| Time | Audio / Voiceover (Spoken Narration) | Visual Screen Cues | On-Screen Text & SFX |
| :--- | :--- | :--- | :--- |
| **0:35 - 0:42** | *"Meet Engraphis: the local-first, open-source AI memory engine built entirely on SQLite."* | Smooth pan into modern dark mode browser window navigating to `http://localhost:8700`. The sleek Engraphis dashboard appears with glowing emerald accents and real-time ledger metrics. | **Logo Reveal:** `engraphis &bull; Local-First Memory Engine`<br>**Badge:** `100% Local SQLite &bull; Zero Telemetry`<br>**SFX:** Upbeat beat drop. |
| **0:42 - 0:49** | *"It structures memory across four isolated tiers: workspace, repository, session, and individual bi-temporal facts."* | Click on **Explore** tab in the dashboard. The interactive force-directed knowledge graph expands, connecting code symbols, decisions, and architectural commits with animated edges. | **Visual:** Cursor hovers over node `mem_01hq7w8...` showing its linked AST symbols and commit hash.<br>**SFX:** Soft blip on node hover. |
| **0:49 - 0:55** | *"And our 4-arm hybrid recall fuses dense vectors, full-text BM25, graph PageRank, and AST code symbols in under 12 milliseconds."* | Graphic overlay showing the 4 parallel retrieval lanes converging into the RRF fusion engine. | **Animated Overlay:** `Vector + BM25 + PageRank + AST Symbols &rarr; <12ms`<br>**SFX:** Fast 4-step chime. |

---

### Scene 4: 71.1% Token Savings & MCP in Action (0:55 - 1:15)
- **Goal:** Prove real-world efficiency with the MCP integration and empirical token benchmarks.

| Time | Audio / Voiceover (Spoken Narration) | Visual Screen Cues | On-Screen Text & SFX |
| :--- | :--- | :--- | :--- |
| **0:55 - 1:04** | *"Connecting takes thirty seconds via the Model Context Protocol. Watch Claude query Engraphis through standard MCP tools."* | Quick switch to Claude Code CLI terminal. Developer asks: *"Why do we use PASSIVE mode in SQLite WAL checkpoints?"* Claude instantly calls `engraphis_recall_context`. | **Terminal Input:** `> Claude recalls from Engraphis MCP`<br>**Tool Call:** `engraphis_recall_context(query="WAL PASSIVE")`<br>**SFX:** Fast rhythmic mechanical typing sound. |
| **1:04 - 1:10** | *"Instead of re-reading fifty files, Claude gets the exact verified fact with provenance citations [1], cutting retrieved tokens by 71.1%."* | Claude answers in 2 seconds with `[1]` citation pointing to the exact decision record. A dynamic comparison bar chart slides in showing 740 tokens collapsing to 214 tokens. | **Highlight Badge:** `-71.1% Retrieved Tokens`<br>**Citation Pill:** `Grounded Citation [1] &bull; 100% Precision`<br>**SFX:** Cash register / success tone. |
| **1:10 - 1:15** | *"And if evidence doesn't exist? Engraphis deterministically abstains instead of guessing."* | Live demo of hallucination bait query: *"What's our AWS egress IP?"* &rarr; Result: `ABSTAIN [Support < 0.25]`. Zero hallucination. | **Callout:** `Anti-Hallucination Abstain Gate`<br>**SFX:** Solid lock snap sound. |

---

### Scene 5: Call to Action & Open Source (1:15 - 1:20+)
- **Goal:** Drive downloads, GitHub stars, and community onboarding.

| Time | Audio / Voiceover (Spoken Narration) | Visual Screen Cues | On-Screen Text & SFX |
| :--- | :--- | :--- | :--- |
| **1:15 - 1:20** | *"Zero cloud lock-in. Zero telemetry. One pip install. Give your AI coding agent persistent memory today."* | Terminal zooms into focus with `pip install engraphis` followed by `engraphis-init`. The GitHub repo page appears with the Star button highlighted and clicked. | **Final Screen:**<br>`pip install engraphis`<br>`github.com/Coding-Dev-Tools/engraphis`<br>`Product Hunt Launch &bull; Upvote & Star`<br>**SFX:** Warm resonant outro chord. |

---

## 2. Product Hunt Gallery Slides Specification

Product Hunt requires images formatted at **1270x760 px (16:9 aspect ratio)**, maximum 3MB file size.
All slides adhere to the Engraphis design language: **Deep Slate dark theme (`#0f172a`), Emerald/Teal accents (`#10b981`), crisp monospaced metadata, and clean sans-serif typography**.

---

### Slide 1: The Hero Slide
- **Role:** Immediate visual impact, clear value proposition, instant comprehension for PH voters scrolling the feed.
- **Dimensions:** 1270 &times; 760 px.
- **Visual Composition:**
  - **Left 55%:** Hero messaging stack:
    - Top badge: `&bull; LOCAL-FIRST AI AGENT MEMORY` (Emerald `#10b981` pill).
    - Headline (42pt bold, `#f8fafc`): *"Persistent, Bi-Temporal Memory for AI Coding Agents."*
    - Subheadline (18pt, `#94a3b8`): *"Stop feeding entire codebases to Claude and Cursor. 4-arm hybrid recall on local SQLite with 71.1% fewer tokens and zero hallucination."*
    - Quick CTA Pills: `pip install engraphis` | `100% Local SQLite` | `MCP Native`.
  - **Right 45%:** 3D-angled glassmorphic card showing the Engraphis interactive knowledge graph with illuminated emerald and cyan nodes linking `AST: engine.py` &rarr; `DECISION: WAL PASSIVE` &rarr; `PROVENANCE: commit a7f9d`.

---

### Slide 2: The Architecture & 4-Arm Recall Slide
- **Role:** Deep technical credibility for developers and AI engineers.
- **Dimensions:** 1270 &times; 760 px (Identical to `docs/launch/graphic-a-architecture.svg`).
- **Visual Composition:**
  - **Top Level:** Scoped Hierarchy Flow (`Workspace ws_...` &rarr; `Repo repo_...` &rarr; `Session ses_...` &rarr; `Memory mem_...`).
  - **Mid Level:** 4 Distinct Colored Parallel Arm Columns:
    1. *Vector Arm* (`#10b981`): Dense semantic cosine via `all-MiniLM` or `sqlite-vec`.
    2. *Lexical Arm* (`#06b6d4`): SQLite FTS5 / BM25 token matching with LIKE fallback.
    3. *Graph Arm* (`#8b5cf6`): Bi-temporal Personalized PageRank over entity-decision graphs.
    4. *Code Arm* (`#f59e0b`): AST symbol tables, caller/callee hierarchy, file bridges.
  - **Bottom Level:** Reciprocal Rank Fusion (RRF `k=60`), 6-term scoring formula (`w_r&middot;R(t) + w_s&middot;sem + w_l&middot;lex...`), Ebbinghaus decay gate, and grounded output with `[n]` citations.

---

### Slide 3: Interactive Dashboard UI (`localhost:8700`)
- **Role:** Prove the product is tangible, mature, and visually stunning to operate.
- **Dimensions:** 1270 &times; 760 px.
- **Visual Composition:**
  - Realistic high-fidelity screenshot of the Engraphis Dashboard running on dark theme `Slate`:
    - **Sidebar (Left):** Workspace switcher (`acme-corp`), Repo selector (`backend-api`), Primary navigation (`Home`, `Ask`, `Library`, `Connections`, `Explore`, `Activity`, `Settings`).
    - **Center Panel (Explore / Relations View):** Interactive node-link graph with clustering around modules. Glowing selection ring around a specific decision node: *"Why PASSIVE checkpoint was chosen"*.
    - **Right Panel (Memory Inspector):** Metadata drawer displaying:
      - Temporal sliders: `valid_from: 2026-03-12`, `valid_to: &infin; (Active)`.
      - Stability score: `S = 14.2 days` (Ebbinghaus decay curve visual).
      - Provenance breadcrumb: `Recorded by Claude Code &bull; Session ses_01hq...`.
    - **Header Badge:** `RUNNING ON LOCALHOST:8700 &bull; ZERO CLOUD CALLS`.

---

### Slide 4: Empirical Benchmark Evidence
- **Role:** Concrete proof of efficiency and anti-hallucination guarantees.
- **Dimensions:** 1270 &times; 760 px (Identical to `docs/launch/graphic-b-token-benchmark.svg`).
- **Visual Composition:**
  - **4 Top KPI Cards:**
    - `-71.1%` Retrieved Tokens per inquiry (740.3 &rarr; 214.3 tokens).
    - `1.000` Recall@5 and Hit@5 retrieval accuracy across offline test suites.
    - `100%` Grounded Abstention (6/6 off-topic queries rejected, 0 hallucinations).
    - `-54.7%` Compact JSON payload compression (24,590 &rarr; 11,138 tokens).
  - **Left Visual:** Direct side-by-side bar comparison: Brute-force context dump (740 tokens, \$145/dev/month) vs Engraphis Grounded Recall (214 tokens, \$38/dev/month).
  - **Right Visual:** Dual-path engine test card: Grounded answer with citation `[1]` on answerable query vs strict refusal `ABSTAIN [Support < 0.25]` on out-of-scope query.

---

### Slide 5: Zero-Friction 3-Step Setup
- **Role:** Eliminates all developer adoption hesitation; demonstrates instant integration.
- **Dimensions:** 1270 &times; 760 px.
- **Visual Composition:**
  - **Step 1 Card (Install):**
    - Code block: `pip install engraphis`
    - Subtext: *"No Docker required. Pure Python + SQLite. Runs anywhere."*
  - **Step 2 Card (Initialize):**
    - Code block: `engraphis-init`
    - Subtext: *"Creates local SQLite database and launches dashboard at localhost:8700."*
  - **Step 3 Card (Connect to your agent):**
    - Ready-to-copy JSON configuration for Claude Desktop / Claude Code / Cursor:
      ```json
      "mcpServers": {
        "engraphis": {
          "command": "python",
          "args": ["-m", "engraphis.mcp_server"]
        }
      }
      ```
    - Subtext: *"Instantly exposes `engraphis_recall_context` and `engraphis_remember` to any MCP client."*

---

## 3. Standalone SVG Visual Assets

The two standalone production-ready SVG graphics are located in the repository:
1. `docs/launch/graphic-a-architecture.svg` (and mirror at `docs/images/product-hunt-architecture.svg`)
2. `docs/launch/graphic-b-token-benchmark.svg` (and mirror at `docs/images/product-hunt-token-benchmark.svg`)

Both SVGs feature:
- Clean responsive `viewBox="0 0 1270 760"`.
- Native dark mode styling (`#0f172a` background, `#1e293b` surfaces, `#334155` borders).
- Signature Engraphis palette (Emerald `#10b981`, Cyan `#06b6d4`, Violet `#8b5cf6`, Amber `#f59e0b`).
- Zero external font dependencies (uses system monospace & modern UI sans stacks for crisp, universal rendering).
