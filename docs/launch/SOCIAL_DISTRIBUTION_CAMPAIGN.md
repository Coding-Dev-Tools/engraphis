# Engraphis: Multi-Channel Social Distribution Campaign

**Product:** Engraphis (by Jaixii / Coding-Dev-Tools)  
**Launch Window:** Product Hunt Launch (12:01 AM PT)  
**License:** Apache-2.0 (Local-first, SQLite, Open Core)  
**GitHub:** https://github.com/Coding-Dev-Tools/engraphis  
**Product Hunt:** https://www.producthunt.com/posts/engraphis  
**PyPI:** `pip install "engraphis[all]"`  
**Local Dashboard:** `http://127.0.0.1:8700`

---

## 1. X / Twitter Launch Master Thread (10-Part Thread)

### Tweet 1 (Hook and Announcement)
Stop letting your AI coding agents get amnesia every time you close your terminal. 🧠⚡

Today we are launching Engraphis on Product Hunt: an open-source, local-first memory engine for AI agents (Claude Code, Cursor, Windsurf, Codex).

Apache-2.0. SQLite-powered. 100% offline-capable.

Check us out on Product Hunt today:
https://www.producthunt.com/posts/engraphis

Watch the Demo: https://www.youtube.com/watch?v=qw-rTnAA_Z4

---

### Tweet 2 (The Core Problem)
The dirty secret of 200k+ token context windows:

Context windows are RAM, not long-term memory.
* Every compaction loses vital architectural decisions
* Sessions do not talk to each other
* Re-reading large codebases burns massive token budgets
* Agents hallucinate stale patterns after you refactor

Dumping raw text into a naive vector DB does not solve this; it makes it worse. Here is why:

---

### Tweet 3 (The Architecture: Local SQLite and Bi-Temporal Truth)
Most memory tools overwrite facts. In production codebases, truth is temporal:
"We used JWT until last Tuesday, now we use PASETO."

Engraphis stores facts with bi-temporal validity:
* World-time (`valid_from` / `valid_to`)
* System-time (`ingested_at` / `expired_at`)

Contradictions invalidate past states instead of overwriting history. Time-travel queries (`as_of`) work out of the box.

All in a local, single-file SQLite database. Your code never leaves your machine.

---

### Tweet 4 (Hybrid 4-Arm Recall)
Vector search alone fails on code. If you search for an error code or an exact symbol, embeddings get fuzzy.

Engraphis executes 4 retrieval arms deterministically, then fuses them:
1. Vector: Cosine similarity via local embeddings
2. Lexical: SQLite FTS5 / BM25 for exact symbols and keywords
3. Graph: Personalized PageRank over entity and decision links
4. Code: AST symbol graph (files, calls, bridges)

Fused with Reciprocal Rank Fusion (RRF) and governed by Ebbinghaus retention decay.

---

### Tweet 5 (Grounded Recall and Abstention)
When your agent guesses, it breaks production.

Engraphis features Grounded Recall:
* Every retrieved context snippet is cited with explicit source markers `[1]`, `[2]`
* If support falls below the semantic and lexical floor, Engraphis strictly abstains

No hallucinations. If the memory does not exist, the agent says "I don't know" rather than inventing a broken config.

---

### Tweet 6 (Hard Benchmarks: 71.1% Token Reduction)
Here are our reproducible offline fixture runs:

* Context Size: Top-5 context reduced from 740.3 to 214.3 tokens per query (71.1% reduction)
* Payload Size: JSON proxy payload dropped 54.7% (24,590 to 11,138 tokens)
* Retrieval Quality: Recall@5 = 1.000
* Grounded Decisions: 11/11 decisions correct (5/5 grounded, 6/6 rejected/abstentions)

Real context reduction with zero accuracy penalty.

Run the benchmarks locally: `python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5`

---

### Tweet 7 (10-Second Setup with MCP)
You can plug Engraphis into Claude Code, Cursor, Windsurf, or Codex in 30 seconds via Model Context Protocol (MCP):

```bash
pip install "engraphis[all]"
engraphis-dashboard
```

Add to your `claude_desktop_config.json` or agent MCP settings:
```json
{
  "mcpServers": {
    "engraphis": {
      "command": "engraphis-mcp"
    }
  }
}
```

The smart tool `engraphis_recall_context` automatically packs memories into your agent context budget.

---

### Tweet 8 (Local Dashboard and Visual Knowledge Graph)
Inspect what your agent actually knows.

Fire up `engraphis-dashboard` to explore `http://127.0.0.1:8700`:
* Interactive knowledge graph of entities and links
* Visual bi-temporal timeline slider
* Full audit log of memories, invalidations, and evolutions
* Interactive recall tester to debug queries before agent runs

Watch it in action : https://www.youtube.com/watch?v=qw-rTnAA_Z4

---

### Tweet 9 (Open Source and Local Sovereignty)
Engraphis Core is Apache-2.0:
* Free forever for local and commercial use
* Zero external LLM requirement for core retrieval
* Works on Python 3.9+ with NumPy and SQLite
* Runs on your laptop, CI runners, or air-gapped environments

Need cross-device team sync? Engraphis Pro and Team offer encrypted relay sync and our advisory decision engine, Jev.

---

### Tweet 10 (Join Us on Product Hunt)
We are live on Product Hunt right now!

If you believe AI agents need inspectable, local-first memory instead of ephemeral amnesia and vendor lock-in, check out our launch today:

Product Hunt: https://www.producthunt.com/posts/engraphis  
GitHub: https://github.com/Coding-Dev-Tools/engraphis  
PyPI: https://pypi.org/project/engraphis/  
Technical writeup: publish the dev.to draft before staging this thread.  

Let us know what you think in the comments!

---

## 2. LinkedIn Founder Announcement Post

Today is launch day for Engraphis on Product Hunt: https://www.producthunt.com/posts/engraphis

If you use AI coding agents like Claude Code, Cursor, Windsurf, or Codex, you have probably run into the session amnesia problem.

You spend three hours guiding an agent through a refactor (such as migrating authentication from legacy JWTs to PASETO tokens, setting error-handling conventions, and documenting database quirks).

Then you open a new session tomorrow, and the agent writes code using the old JWT patterns you just removed.

Why? Because current agent setups treat context windows as memory. But context windows are RAM. When a conversation gets long, compaction kicks in: the model summarizes past turns, drops nuance, and hallucinates stale facts. When the session closes, the state is gone.

Dumping raw text into a vector database does not solve this:
1. Truth is bi-temporal. A fact is not just true or false; it has a validity window in the codebase (`valid_from` to `valid_to`) and an ingestion window (`ingested_at` to `expired_at`). Overwriting history breaks time-travel debugging; ignoring validity makes agents hallucinate deprecated patterns.
2. Vectors alone fail on code. When an agent needs an exact compiler flag, an AST symbol, or a database migration step, cosine similarity is too fuzzy.
3. Privacy matters. Source code and architectural decisions should not be shipped off to third-party vector SaaS providers by default.

That is why I built Engraphis: an open-source (Apache-2.0), local-first memory engine for AI coding agents.

### How it works:
* Single-file SQLite storage: Runs completely on your machine with zero cloud dependency.
* 4-Arm Hybrid Retrieval: Fuses dense vector embeddings, SQLite FTS5/BM25 lexical search, Personalized PageRank over entity graphs, and AST code-symbol graphs using Reciprocal Rank Fusion (RRF).
* Bi-temporal contradiction resolution: When you record a new architectural choice, Engraphis closes the validity window of the old memory and preserves the audit chain instead of overwriting history.
* Grounded recall and abstention: Retrieved memories return strict `[1]`, `[2]` citations. If semantic and lexical evidence is insufficient, Engraphis abstains rather than letting the agent guess.
* 71.1% Context Reduction: In our deterministic offline benchmark fixtures, structure-aware recall reduced mean context consumption from 740.3 to 214.3 tokens per query while preserving Recall@5 at 1.000.

Engraphis connects to modern agent harnesses via the Model Context Protocol (MCP) and includes a local web dashboard (`localhost:8700`) to visually inspect your project's knowledge graph.

If you want durable, auditable, and sovereign memory for your coding agents, check out our Product Hunt launch today:

Product Hunt: https://www.producthunt.com/posts/engraphis  
GitHub: https://github.com/Coding-Dev-Tools/engraphis  

I would love to hear your thoughts, feedback, and architectural critiques in the comments.

---

## 3. Hacker News "Show HN" Submission

**Title:** Show HN: Engraphis - Local-first, bi-temporal memory engine for AI coding agents (Apache-2.0)

**URL / Link:** https://github.com/Coding-Dev-Tools/engraphis  

**Submission Body:**

Hi HN,

I am Jaixii. Today we are releasing Engraphis, an open-source, local-first memory engine designed specifically for AI coding agents (Claude Code, Cursor, Windsurf, Codex, etc.).

Repo: https://github.com/Coding-Dev-Tools/engraphis  
PyPI: https://pypi.org/project/engraphis/  
Product Hunt: https://www.producthunt.com/posts/engraphis  
Demo: https://www.youtube.com/watch?v=qw-rTnAA_Z4  

### Why build this?
The standard approach to agent memory is naive RAG: chunk conversation text, embed it, dump it into a vector store, and run top-k cosine search on the next turn.

In practice, this causes three systemic failure modes in coding workflows:
1. Temporal contradiction: On Monday an agent learns "we use Redis for caching." On Thursday you refactor to Valkey. A cosine query for "cache backend" matches both. Naive vector search returns both as equally valid, causing the agent to flip-flop or suggest deprecated architecture.
2. Lexical failure on symbols: Embedding models often compress symbol names (such as `AUTH_REVOKED_TOKEN_EXPIRY_MS` vs `AUTH_TOKEN_EXPIRY_MS`) into near-identical vectors, losing the exact distinction the compiler requires.
3. Context inflation and amnesia: In multi-hour coding sessions, context compaction destroys subtle architectural decisions, while stuffing hundreds of retrieved chunks into the prompt skyrockets token costs.

### How Engraphis is architected
Engraphis runs locally on SQLite (schema version 19). The core requires only Python 3.9+ and NumPy; no remote API keys or heavy vector daemons are required.

Key components:
1. Bi-Temporal Truth (Valid Time vs. Transaction Time): Every memory record has world-time bounds (`valid_from` / `valid_to`) and system-time bounds (`ingested_at` / `expired_at`). When a new memory contradicts an older one, `core/resolve.py` evaluates token overlap and semantic similarity to invalidate the superseded record (`valid_to = now()`), linking them via `metadata.supersedes`. Reads support `as_of` time-travel queries, so you can inspect what the agent knew at any git commit.
2. 4-Arm Hybrid Retrieval:
   * Vector: Cosine similarity via local embeddings (hashing embedder offline, or SentenceTransformers `all-MiniLM-L6-v2`)
   * Lexical: SQLite FTS5 with BM25 ranking (falling back to LIKE if FTS5 is omitted from your SQLite build)
   * Graph: Personalized PageRank (PPR) over entity and relation links (`core/graphrank.py`)
   * Code: AST symbol/file/caller graph bridges
   Results are fused via Reciprocal Rank Fusion (RRF, k=60) and scored with Ebbinghaus exponential decay curves with interaction reinforcement.
3. Grounded Recall with Explicit Abstention:
   `MemoryEngine.grounded_recall()` returns answers with bracketed citations (`[1]`, `[2]`). If the maximum of semantic cosine similarity and lexical Jaccard index falls below `GROUNDED_SUPPORT_FLOOR`, the engine strictly abstains rather than allowing the model to hallucinate.
4. Context Budget Packing:
   Through our Model Context Protocol (MCP) tool `engraphis_recall_context`, retrieved memories are packed into a strict token budget. In our registered offline fixtures (18 questions), structure-aware chunking dropped top-5 retrieved context from 740.3 to 214.3 tokens per question (a 71.1% reduction) with Recall@5 remaining at 1.000.

### Trade-offs and Limitations
* SQLite Concurrency: As a single-file SQLite database, write throughput is bounded by SQLite write locking. For an individual developer or an agent harness this is negligible (<5ms write), but high-throughput multi-agent swarms writing thousands of concurrent memories require queue serialization.
* Offline Deterministic Hashing vs Heavy Models: The default offline embedder uses a deterministic feature hashing scheme so tests and core functions run without downloading weights. Real semantic generalization requires `pip install "engraphis[all]"` which downloads `all-MiniLM-L6-v2` (~80MB).
* Graph Expansion: Personalized PageRank calculates random-walk stationary distributions across linked entities. On very large graphs (>50k entities), we cap graph iterations to prevent latency degradation.

### Quick Start
```bash
pip install "engraphis[all]"
engraphis-dashboard
# Opens visual memory inspector and knowledge graph at http://127.0.0.1:8700
```

To run offline tests and reproduction benchmarks:
```bash
git clone https://github.com/Coding-Dev-Tools/engraphis
cd engraphis
pip install -e ".[test]"
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
```

The core is Apache-2.0. We have docs on MCP setup for Claude Code, Cursor, Windsurf, and custom agents at `docs/AGENT_CONNECT.md`.

I will be around all day to answer technical questions about our bi-temporal schema, graph ranking implementation, and SQLite vector indexing.

---

## 4. Developer Reddit Posts

### Post A: r/LocalLLaMA
**Title:** Engraphis: Open-source, local-first memory engine for AI coding agents (SQLite, bi-temporal, offline embeddings, Apache-2.0)

Hey r/LocalLLaMA,

When running local models (via Ollama, vLLM, LM Studio) or local agent workflows (Claude Code, Cursor, Continue), memory is usually either non-existent or tied to cloud vector databases that upload private code.

We open-sourced Engraphis under Apache-2.0: a persistent, 100% offline-capable memory engine designed specifically for local AI coding agents.

GitHub: https://github.com/Coding-Dev-Tools/engraphis  
Product Hunt: https://www.producthunt.com/posts/engraphis  
Demo: https://www.youtube.com/watch?v=qw-rTnAA_Z4  

Why local developers might care:
1. Zero Cloud Dependency: The core runs on Python 3.9+ with plain NumPy and SQLite. It does not send your prompts, code symbols, or memories to any external API.
2. Deterministic Offline Embeddings: It comes with a built-in deterministic hashing embedder so you do not need to download an 80MB model to get started. If you do want dense semantic search, it supports local `sentence-transformers` (`all-MiniLM-L6-v2`) or `sqlite-vec`.
3. Bi-Temporal Validity: When you update a config or refactor a function, Engraphis tracks `valid_from` / `valid_to` along with ingestion time. When a new memory contradicts an older one, it invalidates the old record instead of silently overwriting it or letting both exist in conflict.
4. Hybrid 4-Arm Recall: Fuses vector cosine similarity, SQLite FTS5 (BM25), Personalized PageRank over linked knowledge graphs, and AST code symbol graphs via Reciprocal Rank Fusion (RRF).
5. Inspectable Local Dashboard: Run `engraphis-dashboard` and open `http://127.0.0.1:8700` to visually inspect your agent memory graph, timeline slider, and audit logs.
6. Smart MCP Integration: Connects directly into Claude Code, Cursor, Windsurf, or OpenCode via Model Context Protocol (`engraphis-mcp`). It uses token-budgeted context packing that cuts retrieval token payloads by over 71% in our fixtures (740 to 214 tokens).

Install:
```bash
pip install "engraphis[all]"
engraphis-dashboard
```

Would love to get feedback from this community on our retrieval pipeline and local SQLite schema.

---

### Post B: r/ClaudeAI
**Title:** Give Claude Code and Cursor persistent, cross-session memory with Engraphis (MCP, local SQLite, cuts context tokens by 71%)

Hey r/ClaudeAI,

If you use Claude Code in your terminal or Claude in Cursor, you know the frustration of session compaction.

When sessions run long:
* Auto-compaction kicks in and summarizes earlier instructions, dropping key context.
* When you restart Claude Code tomorrow, it forgets your project conventions, bug investigations, and architectural agreements.
* Re-prompting Claude or dumping entire architecture files every session burns tokens fast.

We built an open-source tool called Engraphis (Apache-2.0) that solves this using a local-first memory engine connected directly via Model Context Protocol (MCP).

GitHub: https://github.com/Coding-Dev-Tools/engraphis  
Product Hunt: https://www.producthunt.com/posts/engraphis  
Demo: https://www.youtube.com/watch?v=qw-rTnAA_Z4  

How it works with Claude:
1. Persistent Memory Across Sessions: Tell Claude "Remember that our API error responses use RFC 7807 problem details" or let Claude record it automatically. In a brand new session tomorrow, Claude remembers.
2. 71% Token Reduction: Instead of stuffing full files into context, Engraphis tool `engraphis_recall_context` retrieves only verified facts, packed into a bounded token budget (reduced from 740 to 214 tokens per query in our benchmarks).
3. No Stale Contradictions: When you change a decision, Engraphis closes the validity of the old decision (`valid_to = now()`), so Claude does not give conflicting answers.
4. 100% Local and Private: Runs in a local SQLite file on your machine. Your proprietary code never goes to a third-party vector cloud.
5. Visual Dashboard: Run `engraphis-dashboard` to view an interactive visual knowledge graph of everything Claude has learned at `http://127.0.0.1:8700`.

### Setup for Claude Code / Claude Desktop:

1. Install Engraphis:
```bash
pip install "engraphis[all]"
```

2. Add to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "engraphis": {
      "command": "engraphis-mcp"
    }
  }
}
```

3. Open the visual dashboard anytime:
```bash
engraphis-dashboard
```

We are launching on Product Hunt today. Take a look at the repo and share your thoughts.
