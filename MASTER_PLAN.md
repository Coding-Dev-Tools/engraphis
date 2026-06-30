# Engraphis — Master Plan

**The open, local-first memory engine for AI agents — unified across conversations, tasks, and code, with native multi-repository and multi-session continuity.**

> This document is the build specification for Engraphis. It supersedes the prototype vision in `README.md` (a local clone of neocortex's published architecture) and defines what Engraphis must become to be the best agentic-memory system available — better than neocortex, mem0, Zep, and Letta — with a code-aware, multi-repo, multi-session model that none of them ship natively.

**Status:** Plan v1 · **Audience:** the implementing AI developer · **License intent:** open source (Apache-2.0 or MIT)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Opportunity & Competitive Landscape](#2-the-opportunity--competitive-landscape)
3. [Current State Assessment](#3-current-state-assessment)
4. [Design Principles](#4-design-principles)
5. [The Memory Ontology](#5-the-memory-ontology)
6. [System Architecture](#6-system-architecture)
7. [Retrieval Pipeline (Recall)](#7-retrieval-pipeline-recall)
8. [Write Path: Ingestion, Extraction & Consolidation](#8-write-path-ingestion-extraction--consolidation)
9. [Code-Aware Memory (Flagship Wedge)](#9-code-aware-memory-flagship-wedge)
10. [Multi-Repository & Multi-Session Model](#10-multi-repository--multi-session-model)
11. [Interfaces & Integration (REST, MCP, SDKs)](#11-interfaces--integration)
12. [Data Model & Schema](#12-data-model--schema)
13. [Core Algorithms](#13-core-algorithms)
14. [Evaluation & Benchmarking](#14-evaluation--benchmarking)
15. [Performance & Scaling](#15-performance--scaling)
16. [Security, Privacy & Multi-Tenancy](#16-security-privacy--multi-tenancy)
17. [Observability & Memory Inspector](#17-observability--memory-inspector)
18. [Phased Roadmap](#18-phased-roadmap)
19. [Risks & Mitigations](#19-risks--mitigations)
20. [Appendix: Glossary & References](#20-appendix-glossary--references)

---

## 1. Executive Summary

### What we are building

Engraphis is a **memory engine for AI agents** that an agent reads from and writes to across time. It combines four ideas that, today, are split across competing systems:

1. **Human-like forgetting** (Ebbinghaus decay + interaction-reinforcement) — like neocortex.
2. **Self-maintaining facts** (LLM extraction + ADD/UPDATE/DELETE/NOOP consolidation) — like mem0.
3. **A bi-temporal knowledge graph** (facts have validity intervals; contradictions invalidate rather than overwrite) — like Zep/Graphiti.
4. **Self-organizing, self-linking memories** (atomic notes with generated links + memory evolution) — like A-MEM.

On top of that shared foundation, Engraphis adds the **two capabilities nobody ships natively** and that the user explicitly needs:

- **Multi-repository awareness** — memory is organized by a real hierarchy (`workspace → repository → session → memory`), with scoped sharing so knowledge learned in one repo can be promoted to cross-repo or user-global scope. Code symbols, decisions, and conventions link *across* repositories.
- **Multi-session continuity** — every agent run is a first-class `session` with a lifecycle (open → active → summarized → consolidated). Sessions produce episodic logs that consolidate into durable semantic and procedural memory, so the agent genuinely *carries knowledge forward* between runs and between tools.

### Why it wins

| Axis | neocortex | mem0 | Zep | Engraphis (target) |
|---|---|---|---|---|
| Open & self-hostable engine | ✗ (closed alpha, cloud) | ✓ | partial | **✓ fully open, local-first** |
| Forgetting/decay | ✓ | partial | ✗ | **✓** |
| Self-maintaining facts | ✗ | ✓ | ✓ | **✓** |
| Bi-temporal graph | ✗ | partial | ✓ | **✓** |
| Native multi-repo model | ✗ | ✗ | ✗ | **✓ (unique)** |
| Native multi-session lifecycle | partial | partial | partial | **✓ (first-class)** |
| Code-aware (AST/symbol graph) | ✗ | ✗ | ✗ | **✓ (unique)** |
| MCP-native for coding agents | ✗ | ✓ (OpenMemory) | ✗ | **✓** |

The strategic insight: **neocortex is a closed, cloud-gated alpha with flat namespaces.** Its moat is speed and a decay model — both reproducible. Its weakness is that it is not open, not local-first at the engine level, and has no concept of repositories, sessions, or code. Engraphis wins by being **open + local-first + code-aware + multi-repo/multi-session**, while matching the decay/recall quality and closing the gap on retrieval quality with a hybrid pipeline and a bi-temporal graph.

### The wedge

Lead with **the memory layer for coding agents** (Claude Code, Cursor, Codex CLI, Cline, Zed, Windsurf) via an MCP server, because that audience feels session amnesia most acutely and has clear, demonstrable wins (cross-repo conventions, "why did we do X", bug→fix recall, no re-explaining the codebase). The same engine serves general agents (chat, voice, support) — so the addressable market is "all agents," but adoption starts where the pain is sharpest and the demo is undeniable.

---

## 2. The Opportunity & Competitive Landscape

### 2.1 The field, honestly assessed

Agentic memory is a crowded, fast-moving space. To be "better than anything on GitHub" you must understand exactly what each system does well, because the winning design is a *synthesis* of their best ideas plus the two structural capabilities none of them have.

**neocortex / OpenHuman (tinyhumansai)** — *the system to beat.*
- **Idea:** the brain compresses by forgetting noise. Memories decay (Ebbinghaus) unless reinforced by interactions (view/react/reply/create), recalls, or being built upon. "Conscious Recall" proactively surfaces memories that are both recent and repeatedly interacted with.
- **Claims:** chews through 10M+ tokens at up to ~4000 tokens/sec; ~$0.2/Mn tokens; a "context" role that injects memory without consuming LLM tokens. Reports strong RAGAS (Answer Relevancy 0.97, Context Precision 0.75), TemporalBench (100% on recency), and Vending-Bench results.
- **Reach:** SDKs for Python, TS/JS, Go, Rust, Dart, C++, C#, Java; plugins for LangGraph, CrewAI, ElevenLabs, Raycast, Agno, Pipecat, Mastra, Autogen, OpenClaw.
- **Weaknesses to exploit:** **closed alpha, API-key gated, cloud-dependent** (the engine is not open; only SDKs are). **Flat namespaces** — no repositories, no sessions, no code awareness. No bi-temporal graph; contradiction handling is shallow. Decay is the main trick, and decay alone discards information that becomes relevant later.

**mem0** — *the consolidation reference.*
- **Idea:** a two-phase pipeline. **Extraction** distills salient facts from recent messages + a rolling summary via a lightweight LLM call. **Update** compares each candidate fact against the top-K semantically similar existing memories and issues an `ADD / UPDATE / DELETE / NOOP` decision so the store stays consistent. Memory types: working, factual, episodic, semantic. `Mem0ᵍ` adds a graph store. Ships **OpenMemory MCP** (local-first, shared across MCP tools).
- **Reported results:** ~26% LLM-as-judge gain over OpenAI Memory on LOCOMO, ~91% latency reduction, ~90% token savings; ~10K memories/user with sub-100ms updates.
- **Weaknesses to exploit:** no code awareness; graph is secondary; no decay/forgetting model; multi-repo/session is not a native concept.

**Zep / Graphiti** — *the temporal-graph reference.*
- **Idea:** a **bi-temporal knowledge graph**. Every edge carries validity intervals: `t_valid`/`t_invalid` (when the fact was true in the world) and `t_created`/`t_expired` (when the system learned/retired it). New contradicting information **closes the old edge's validity window** rather than deleting it, so current-state queries stay clean while history remains queryable. Strong DMR/temporal results. Graphiti is open source (Neo4j-backed).
- **Weaknesses to exploit:** graph-first design is heavier to operate (Neo4j); no decay; no code awareness; no native repo/session hierarchy.

**Letta / MemGPT** — *the context-management reference.*
- **Idea:** OS-inspired tiered memory. **Memory blocks** (e.g. `human`, `persona`) are discrete, self-editable units the agent rewrites via tools; an external store is paged in/out of the context window. Excellent for *in-context* state management.
- **Weaknesses to exploit:** it is an agent runtime, not a drop-in memory layer; retrieval/forgetting/graph are not the focus; no code or repo model.

**A-MEM** — *the self-organizing reference.* Zettelkasten-style atomic notes; each new note gets LLM-generated keywords/tags/description and **links to related notes**, and can trigger **evolution** (updating neighbors' context). Great for emergent structure; lacks decay, temporality, code, and a repo/session model.

**HippoRAG (1/2)** — *the associative-retrieval reference.* Builds a graph from OpenIE triples and retrieves via **Personalized PageRank** seeded from query-linked nodes — strong for multi-hop. HippoRAG-2 notes the entity-centric design loses context. Useful technique to borrow for graph-aware recall.

**codebase-memory-mcp / RANGER / CodeRAG** — *the code-graph reference.* Tree-sitter AST parsing across 60+ languages to extract defs/calls/imports/refs into a per-repo knowledge graph; incremental re-indexing via file-watcher + content hash; injected into agents (e.g. Claude Code `PreToolUse` hooks) for ~100x token savings on structural questions. Research finds **AST-derived graphs are more reliable than LLM-extracted KGs for code.** These are *code-structure* tools — they do not do durable cross-session episodic/semantic memory, decay, or general facts. Engraphis fuses both worlds.

### 2.2 The unclaimed territory

Plotting every system on two axes — *(a) durable, self-maintaining, temporal memory* and *(b) code/repo/session structure* — the top-right quadrant is **empty**. mem0/Zep own (a); codebase-memory-mcp owns (b); nobody owns both. **Engraphis targets the top-right:** a durable, decaying, bi-temporal, self-organizing memory engine that is *also* code-aware and natively multi-repo/multi-session, fully open and local-first.

That is the one-sentence reason Engraphis can be the best: **it is the synthesis nobody has shipped, plus the two structural capabilities (multi-repo, multi-session) the user specifically needs and the market lacks.**

---

## 3. Current State Assessment

The existing `engraphis/` codebase is a competent, working clone of neocortex's *published* architecture. It is a good foundation — keep the spirit, replace the load-bearing internals.

### 3.1 What exists and is worth keeping

- **Clean FastAPI service** with ~20 routes and SDK-compatibility shims for the upstream `tinyhumansai` client (a real adoption asset — keep it).
- **SQLite storage** with WAL, thread-local connections, sensible schema for memories/chunks/entities/edges/events/interactions/thoughts/jobs/vaults.
- **Ebbinghaus retention** `R(t)=exp(-t/S)` with stability growth `S·(1+α·ln(1+n))` and interaction boosts — a correct, defensible decay core.
- **A background "consciousness loop"** (decay pass → recall → LLM thought synthesis → persist) — the right *shape* for a consolidation/reflection loop.
- **Pluggable external LLM** across 5 providers; **local embeddings** via sentence-transformers — the local-first instinct is correct and is a differentiator vs neocortex.

### 3.2 The critical gaps (what blocks "ultimate")

| # | Gap | Today | Consequence | Fix (section) |
|---|---|---|---|---|
| 1 | **O(n) brute-force recall** | `all_vectors()` loads every vector and loops `np.dot` | Falls over at ~10⁴–10⁵ memories; cannot touch neocortex's scale/speed claims | §6, §15 |
| 2 | **Flat namespaces** | `namespace` is a single string | No repositories, no sessions, no scoping — blocks the headline feature | §5, §10 |
| 3 | **No session model** | none | No multi-session continuity, no episodic→semantic consolidation | §5, §10 |
| 4 | **Weak entity extraction** | regex/capitalization NER | Noisy graph, poor relations, no real knowledge graph | §8 |
| 5 | **No conflict resolution** | upsert overwrites by key | Stale/contradictory facts accumulate; no temporal truth | §8 |
| 6 | **Single memory type** | `memory_type` exists but unused | No episodic/semantic/procedural/working distinction | §5 |
| 7 | **No bi-temporal graph** | edges have no validity intervals | Cannot answer "what was true then" / clean current-state | §5, §12 |
| 8 | **Weak embeddings** | `all-MiniLM-L6-v2` (384-d, dated) | Retrieval quality ceiling far below SOTA | §6 |
| 9 | **No hybrid retrieval / rerank** | pure cosine | Misses lexical/graph signal; lower precision | §7 |
| 10 | **No MCP server** | REST only | Coding agents can't natively plug in — misses the wedge | §11 |
| 11 | **No code awareness** | none | No AST/symbol graph; the flagship differentiator is absent | §9 |
| 12 | **No evaluation harness** | smoke tests only | Can't *prove* "better than neocortex" | §14 |

### 3.3 Verdict

The architecture is sound as a v0 prototype but the internals are not what an "ultimate" system needs. The plan below **keeps the service shell, SDK-compat, decay math, and local-first ethos**, and replaces storage/index/extraction/retrieval/consolidation with production-grade equivalents — then layers the multi-repo, multi-session, and code-aware capabilities that define the product.

---

## 4. Design Principles

1. **Local-first, open, no lock-in.** The engine runs fully offline on a laptop (SQLite + embedded ANN + local embeddings). Cloud/LLM is optional and pluggable. This is the structural advantage over neocortex; never compromise it.
2. **Forgetting is a feature, but never lossy by accident.** Decay lowers *retrieval priority*; it does not hard-delete. Hard deletion is explicit, governed, and audited. Bi-temporal invalidation preserves history.
3. **Truth is temporal.** Every fact can become false. Model validity in time; resolve contradictions by invalidation, not overwrite.
4. **Memory is typed.** Episodic, semantic, procedural, and working memory have different lifecycles, decay rates, and retrieval rules. Treat them differently.
5. **Structure beats similarity.** "Similar" ≠ "important" (neocortex's own thesis). Combine semantic similarity with lexical match, graph proximity, recency, importance, and reinforcement.
6. **Everything is scoped.** Every memory has a scope (`session` / `repo` / `workspace` / `user-global`) and a visibility. Promotion between scopes is a first-class, explicit operation.
7. **Interfaces before implementations.** Storage, vector index, graph, embedder, reranker, and LLM are all behind narrow interfaces so the hot path can be swapped (Python → Rust) without rearchitecting.
8. **Provenance always.** Every memory and fact records where it came from (session, document, message, commit), so the agent — and the user — can audit *why* something is "known."
9. **Safety against memory poisoning.** Treat ingested content as untrusted. Guard consolidation against injection, drift, and contradiction loops (see §16).
10. **Prove it with benchmarks.** No claim of "better" ships without a number on LoCoMo / LongMemEval / a code-memory eval, tracked in CI.

---

## 5. The Memory Ontology

This is the conceptual core. Get this right and the rest follows.

### 5.1 The scope hierarchy

```
User (identity)
└── Workspace            e.g. "personal", "acme-corp"   (tenant / billing / encryption boundary)
    ├── Repository/Project  e.g. "engraphis", "web-app", "infra"   (a codebase OR a data domain)
    │   └── Session         one agent run / conversation / task   (episodic boundary)
    │       └── Memory      atomic unit (see types below)
    └── (cross-repo & user-global memories live at Workspace/User level)
```

- A **Repository** is the unit the user cares about ("multiple repositories"). For coding agents it maps to a Git repo; for general agents it is a "project" or data domain. It carries its own code graph, conventions, and memories.
- A **Session** is the unit of "multiple sessions": a bounded agent run with a start, an activity log (episodic), and an end that triggers summarization + consolidation.
- **Scopes** govern visibility: `session` (ephemeral, this run), `repo` (durable, this codebase), `workspace` (cross-repo, e.g. "we standardize on pnpm"), `user` (global preferences/identity). Retrieval can target one or several scopes; **promotion** moves a memory up a scope when it proves broadly true (e.g. a convention seen in 3 repos → workspace).

> This hierarchy is the single most important departure from every competitor. neocortex/mem0/Zep have at most `user`+`namespace`/`session`. None model `repository` as a first-class entity with its own graph, nor scope-promotion across repos.

### 5.2 Memory types (each with distinct lifecycle)

Grounded in the agent-memory literature (Generative Agents; the 2025–26 surveys; mem0's typing):

| Type | What it stores | Example | Decay | Consolidation |
|---|---|---|---|---|
| **Working** | Transient state for the current step/session | "currently editing `auth.py`, test 3 failing" | Fast (session-bounded) | Discarded or summarized at session end |
| **Episodic** | What happened — events, decisions, tool calls, failures, outcomes | "On 2026-06-20 we switched from JWT to PASETO because of key-rotation pain" | Medium | Distilled into semantic/procedural over time |
| **Semantic** | De-contextualized facts, preferences, conventions | "This repo uses PASETO for auth"; "User prefers tabs" | Slow | Merged/updated; bi-temporal validity |
| **Procedural** | Reusable skills, playbooks, runnable recipes | "How to add a new API route in this repo (steps + code template)" | Very slow | Refined on reuse; versioned |

**Key dynamic — consolidation as the engine of learning:** repeated episodic signals (e.g. the user corrects the same thing twice) consolidate into a semantic fact; a successfully repeated multi-step task consolidates into a procedural skill (cf. Voyager's skill library). This episodic→semantic→procedural flow is what makes the agent *get smarter across sessions*, not just *recall more*.

### 5.3 The atomic memory (A-MEM influence)

Every memory is an **atomic note** with structured attributes, so the store is self-organizing:

```jsonc
{
  "id": "mem_01H...",
  "type": "semantic",                 // working|episodic|semantic|procedural
  "scope": "repo",                    // session|repo|workspace|user
  "workspace_id": "...", "repo_id": "...", "session_id": "...",
  "title": "Auth uses PASETO",
  "content": "The engraphis API authenticates with PASETO v4 tokens...",
  "summary": "PASETO for auth",       // short form for context packing
  "keywords": ["auth", "paseto", "security"],   // LLM-generated
  "links": ["mem_...", "mem_..."],    // generated links to related notes
  "entities": ["PASETO", "auth.py"],  // graph anchors
  "embedding": [/* f32 */],
  "importance": 0.0,                  // 0..1, scored at creation (Generative Agents)
  "stability": 1.0, "access_count": 0, "last_access": <ts>, "surprise": 1.0,  // decay state
  "valid_from": <ts>, "valid_to": null,           // world-time validity (bi-temporal)
  "ingested_at": <ts>, "expired_at": null,        // system-time validity (bi-temporal)
  "provenance": { "source": "session", "ref": "ses_...", "doc": "...", "commit": "..." },
  "pinned": false, "sensitivity": "normal"        // governance
}
```

This unifies the best ideas: typed + scoped (ours) · decaying (neocortex) · bi-temporal (Zep) · self-linking with importance (A-MEM + Generative Agents) · provenance & governance (production needs).

---

## 6. System Architecture

### 6.1 Component overview

```
                         ┌──────────────────────────────────────────────┐
   Agents / Clients      │                 ENGRAPHIS                     │
 ┌───────────────┐       │                                              │
 │ Claude Code   │─MCP──▶│  ┌────────────┐   ┌───────────────────────┐  │
 │ Cursor / Codex│       │  │  Gateway   │   │   Consolidation /     │  │
 │ Cline / Zed   │       │  │ REST · MCP │   │   Reflection Loop     │  │
 └───────────────┘       │  │  · SDKs    │   │ (episodic→semantic→   │  │
 ┌───────────────┐       │  └─────┬──────┘   │  procedural, decay)   │  │
 │ LangGraph     │─SDK──▶│        │          └───────────▲───────────┘  │
 │ CrewAI / chat │       │  ┌─────▼───────────────────┐  │              │
 └───────────────┘       │  │     WRITE PATH          │  │              │
                         │  │ ingest → extract facts  │  │              │
                         │  │ → resolve conflicts     │  │              │
                         │  │ → link → embed → store  │  │              │
                         │  └─────┬───────────────────┘  │              │
                         │        │                      │              │
                         │  ┌─────▼──────────────────────┴───────────┐  │
                         │  │             STORAGE CORE                │  │
                         │  │  Memory store · Vector index (ANN) ·    │  │
                         │  │  Lexical index (FTS/BM25) · Bi-temporal │  │
                         │  │  Knowledge graph · Code symbol graph ·  │  │
                         │  │  Event ledger                           │  │
                         │  └─────┬───────────────────────────────────┘  │
                         │        │                                      │
                         │  ┌─────▼───────────────────┐                  │
                         │  │      RETRIEVAL PATH      │                  │
                         │  │ hybrid candidates →      │                  │
                         │  │ score (retention×rel×    │                  │
                         │  │ importance×recency) →    │                  │
                         │  │ rerank → pack context    │                  │
                         │  └──────────────────────────┘                  │
                         └──────────────────────────────────────────────┘
   Pluggable: Embedder · Reranker · LLM (local or API) · Vector backend · Graph backend
```

### 6.2 Recommended stack (the decision)

You asked me to choose. **Recommendation: evolve the Python core behind clean interfaces, replace the load-bearing internals, and plan a Rust hot-path core for the performance phase.** Concretely:

| Layer | Choice | Why |
|---|---|---|
| **Service / orchestration** | **Python 3.11+ / FastAPI** (keep) | Preserves working code, SDK-compat, fastest path to value; ecosystem for LLM/embeddings is Python-first. |
| **Memory + metadata store** | **SQLite (WAL)** default; Postgres adapter for teams | Local-first, zero-config, portable single file; Postgres when multi-user/server. |
| **Vector index** | **`sqlite-vec`** default (embedded, portable, in the same file); **LanceDB** adapter for larger local stores; **Qdrant** adapter for server scale | Replaces O(n) brute force. `sqlite-vec` keeps the local-first single-file story; LanceDB (IVF-PQ, columnar, zero-copy) for 10⁶+; Qdrant (Rust HNSW, ~20–30 ms @ ~95% recall) for hosted scale. All behind one `VectorIndex` interface. |
| **Lexical index** | **SQLite FTS5 (BM25)** | Hybrid retrieval needs a lexical arm; FTS5 is built in and free. |
| **Knowledge graph** | **Relational tables in SQLite** with adjacency + recursive CTEs; **Kùzu** (embedded graph DB) adapter when graph walks dominate | Avoids forcing Neo4j (Zep's operational weight); keeps local-first. Kùzu is an embedded, columnar graph DB for heavier PPR/multi-hop. |
| **Embeddings** | **Pluggable.** Default local: a strong small open model (e.g. BGE-M3 / Qwen3-Embedding-0.6B class). Optional API: Voyage-3, OpenAI v3-large, Cohere Embed-3. Separate **code** embedding (Qwen3 / Voyage-code) for code chunks. | `all-MiniLM-L6-v2` is dated; SOTA open models (Qwen3-Embedding, BGE-M3) raise the retrieval ceiling materially. Keep it swappable; embed-dim is per-model. |
| **Reranker** | **Pluggable cross-encoder** (BGE-reranker-v2 / Qwen3-Reranker local; Cohere Rerank-3.5 API) | Cross-encoder reranking is the single biggest precision win on top of hybrid candidates. |
| **Hot-path core (later)** | **Rust via PyO3** for vector scan + decay scoring + graph walk | When benchmarks demand it, drop the latency-critical loop into Rust to credibly match neocortex's throughput — without touching the Python orchestration. |

**Why not a full rewrite (Rust/Go greenfield) now?** Three reasons. (1) The bottleneck today is *architecture* (brute-force search, no graph, no sessions), not language — fixing architecture in Python captures ~90% of the win immediately. (2) The LLM/embedding/reranker ecosystem is Python-native; a rewrite pays a large integration tax. (3) Clean interfaces let you extract a Rust core or standalone service *later*, exactly where profiling proves it matters, at a fraction of the risk. Escalate to the Rust core (and optionally a standalone service) only when you hit the performance triggers in §15.

**Net:** this is option "evolve Python" maturing into "Python + Rust hot path" — the highest expected-value path to an "ultimate" system that ships, stays local-first, and still reaches neocortex-class speed.

### 6.3 Interface contracts (define these first)

```python
class Embedder(Protocol):
    def embed(self, texts: list[str], *, kind: Literal["text","code"]) -> np.ndarray: ...
    @property
    def dim(self) -> int: ...

class VectorIndex(Protocol):
    def upsert(self, ids: list[str], vecs: np.ndarray, meta: list[dict]) -> None: ...
    def search(self, vec: np.ndarray, k: int, *, filter: dict) -> list[tuple[str, float]]: ...
    def delete(self, ids: list[str]) -> None: ...

class LexicalIndex(Protocol):
    def search(self, query: str, k: int, *, filter: dict) -> list[tuple[str, float]]: ...

class GraphStore(Protocol):
    def upsert_node(self, node: Node) -> None: ...
    def upsert_edge(self, edge: Edge) -> None: ...           # carries valid_from/valid_to
    def invalidate_edge(self, edge_id: str, at: float) -> None: ...
    def neighbors(self, node_ids: list[str], *, hops: int, at: float | None) -> list[Edge]: ...
    def ppr(self, seeds: list[str], *, at: float | None) -> dict[str, float]: ...  # personalized PageRank

class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]: ...

class LLM(Protocol):
    def complete(self, messages: list[dict], **kw) -> str: ...
    def extract_json(self, prompt: str, schema: dict) -> dict: ...   # structured extraction
```

Everything else is built against these. Swapping `sqlite-vec` for Qdrant, or the local embedder for Voyage, or the Python scorer for a Rust one, is a config change — not a refactor.

---

## 7. Retrieval Pipeline (Recall)

Retrieval is where quality is won or lost. The current pure-cosine path is replaced by a **hybrid → score → rerank → pack** pipeline.

### 7.1 Stages

1. **Scope resolution.** Translate the request into a scope filter: which `session`/`repo`/`workspace`/`user` memories are visible, and a time anchor `as_of` (default: now). Bi-temporal: only facts where `valid_from ≤ as_of < valid_to` and `expired_at is null`.
2. **Hybrid candidate generation** (run in parallel, union the results):
   - **Semantic:** ANN search over embeddings (top-N, e.g. 100).
   - **Lexical:** BM25/FTS5 over content + keywords (top-N) — catches exact identifiers, error strings, symbol names that embeddings blur.
   - **Graph:** seed entities/symbols extracted from the query, run **personalized PageRank** (HippoRAG-style) over the bi-temporal graph to pull in associatively-related memories (top-N) — this is what gives multi-hop / "why" recall.
3. **Fusion + scoring.** Merge candidates (reciprocal-rank fusion) and compute the **Engraphis recall score** (the heart of "Conscious Recall", improved):

   ```
   score = w_r · retention            // Ebbinghaus R(t)=exp(-Δt/S), reinforced by access/interaction
         + w_s · semantic_similarity  // cosine to query
         + w_l · lexical_score        // normalized BM25
         + w_g · graph_proximity      // PPR mass from query seeds
         + w_i · importance           // LLM-scored salience at creation (Generative Agents)
         + w_c · recency              // exp decay on world-time, for tie-breaking/temporal Qs
         − w_x · staleness_penalty    // down-weight facts near/after valid_to
   ```

   Weights are per-memory-type (procedural weights importance/graph higher; working weights recency higher) and tunable per query mode. This **fixes neocortex's core limitation** ("similar ≠ important") by making similarity one term among six rather than the whole score.
4. **Cross-encoder rerank.** Take top-K (e.g. 30) by score, rerank with a cross-encoder against the query, keep top-k (e.g. 8–12). This is the largest precision gain and directly lifts RAGAS/LoCoMo numbers.
5. **Context assembly & token budgeting.** Pack the final set under a token budget, preferring `summary` over `content` when space is tight, grouping by scope, and emitting structured blocks with provenance. Expose a **dedicated `context` role** payload (neocortex's trick) so hosts can inject memory without spending system-prompt tokens.

### 7.2 Conscious / proactive recall

Beyond query-driven recall, a **proactive recall** endpoint (and MCP resource) surfaces "what should be top-of-mind now" given recent session activity — combining reinforcement (interaction signals) with recency and importance, no explicit query needed. This mirrors neocortex's Conscious Recall but is scope- and session-aware: at session start it auto-loads the repo's high-importance semantic + procedural memories and any unresolved threads from the last session ("last time we were mid-refactor of X").

### 7.3 Retrieval modes

- `precise` — high reranker weight, tight k (debugging a specific symbol).
- `associative` — high graph/PPR weight (exploration, "what relates to this").
- `temporal` — `as_of` time travel and recency weighting ("what did we believe in May").
- `procedural` — bias to procedural memory (how-to / playbooks).

---

## 8. Write Path: Ingestion, Extraction & Consolidation

The write path is what keeps the store *true and lean*. This is mem0's domain, extended with bi-temporality and typing.

### 8.1 Ingestion

Accept raw inputs — chat turns, documents, tool-call traces, code diffs, git events. Normalize to a common envelope (content + provenance + scope + timestamps). Large inputs are **chunked** (semantic/code-aware chunking, not fixed-size) and embedded.

### 8.2 Fact extraction (replace regex NER)

For conversational/document inputs, call the LLM with a **structured-extraction** prompt to emit candidate atomic facts with `{type, content, entities, valid_from?, importance}`. For code inputs, extraction is **AST-based** (§9), not LLM — research shows AST-derived structure is more reliable for code than LLM extraction. Entities/relations populate the knowledge graph; importance is scored 0–1 at creation (mundane low, decisions/preferences high).

### 8.3 Conflict resolution (ADD / UPDATE / DELETE / NOOP + invalidate)

For each candidate fact, retrieve the top-K most similar existing memories in scope and ask the resolver (LLM with tool-calling, deterministic fallbacks) to choose:

- **ADD** — genuinely new → insert atomic note, generate links (A-MEM).
- **UPDATE** — refines an existing fact → edit in place, bump stability.
- **NOOP** — already known → just reinforce (interaction boost) the existing memory.
- **DELETE/INVALIDATE** — contradicts an existing fact → **do not overwrite**; set the old fact's `valid_to = now` (bi-temporal close, Zep-style), insert the new fact with `valid_from = now`. History stays queryable; current-state stays clean.

This combination — mem0's decisioning + Zep's temporal invalidation — is stronger than either alone and is a concrete benchmark win on LongMemEval's "knowledge-update" category.

### 8.4 Linking & evolution (A-MEM)

On ADD, generate links to the nearest related notes and optionally **evolve** neighbors (update their context/keywords when the new note changes how they should be understood). Links are first-class graph edges and feed the PPR retrieval arm.

### 8.5 The consolidation / reflection loop (replace the naive thought loop)

The current loop (decay → recall → synthesize thought → persist) becomes a real **sleep-time consolidation** cycle, run on a schedule and/or at session close:

1. **Decay pass.** Apply Ebbinghaus decay to stability across scopes (lower retrieval priority for the un-recalled). Never deletes.
2. **Episodic→semantic consolidation.** Cluster recent episodic events; where a pattern repeats (e.g. the same correction twice), emit/strengthen a semantic fact. Down-rank the raw episodics.
3. **Reflection.** Periodically read the highest-importance recent memories and synthesize higher-level insights ("the team keeps hitting flaky tests in module X") — Generative-Agents-style — stored as semantic memories with links to evidence.
4. **Procedural distillation.** Detect successfully repeated multi-step task sequences; store/refine a procedural skill (steps + code template + when-to-use), versioned.
5. **Summarization with drift guards.** Compress long episodic logs into session summaries — but **guard against semantic drift** (repeated lossy re-summarization). Keep originals (decayed, not deleted); regenerate summaries from sources rather than from prior summaries; flag low-confidence merges for review.
6. **Promotion.** When a repo-scoped fact is corroborated across N repos, propose promotion to workspace scope.

### 8.6 Forgetting (governed)

Decay controls *priority*. A separate, explicit **pruning policy** can hard-delete only: working memories past TTL, content the user marks ephemeral, or memories below a retention floor *and* unpinned *and* past a minimum age — always audited, never silent. Pinned memories never decay below a floor.

---

## 9. Code-Aware Memory (Flagship Wedge)

This is the differentiator that makes Engraphis indispensable to coding agents and that no general memory system (neocortex/mem0/Zep) has. It fuses *code-structure intelligence* (codebase-memory-mcp/RANGER) with *durable agent memory* (the rest of Engraphis).

### 9.1 The code symbol graph (per repository)

On connecting a repository, build a **code knowledge graph** by parsing with **Tree-sitter** (60+ languages). Extract definitions (functions, methods, classes, interfaces, enums, types — with signatures, return types, decorators, export status), plus call sites, imports, references, and (where resolvable) implementations. Nodes are symbols/files/modules; edges are `defines`, `calls`, `imports`, `references`, `implements`, `tests`. Each symbol node gets a **code embedding** (code-specialized model) for semantic code search.

> Use **AST-derived structure as the source of truth** for code relationships (more reliable than LLM extraction), and reserve the LLM for *natural-language enrichment* (summaries of what a function does, why a module exists).

### 9.2 Incremental indexing

A **file watcher** with content-hashing re-indexes only changed files on save/commit (adaptive polling fallback). Indexing is fast and local; the average repo indexes in seconds and stays current without manual steps. Git is a first-class signal: commits, branches, and blame provide *world-time* for code facts (when a convention was introduced, when a function changed).

### 9.3 Code-grounded memory types

- **Semantic (code):** "This repo's auth lives in `auth/paseto.py`"; "We use Result types, never exceptions, in `core/`." Linked to the symbol graph so it stays anchored as code moves.
- **Episodic (dev):** decisions ("chose PASETO over JWT — see ADR-014"), bug→fix pairs (symptom, root cause, fixing commit), failed approaches ("tried X, it deadlocked"), review comments, incident notes. **This is the "why" memory** agents desperately lack.
- **Procedural (dev):** repo-specific playbooks ("how to add a migration", "how to add an API route") as steps + code templates, distilled from observed successful sequences and refined on reuse.

### 9.4 Why this beats grep/embeddings-only code tools

Pure code search (grep, vector) answers "where is X". Engraphis also answers "**why** is X like this", "what did we **try before**", "what **breaks** if I change X" (via the call graph + bug history), and "**how** do we usually do Y here" (procedural). It carries that across sessions and repos. Structural queries hit the symbol graph (≈100x fewer tokens than dumping files); rationale/history queries hit episodic memory; conventions hit semantic memory — all in one recall call.

### 9.5 Agent integration patterns

- **MCP tools** for explicit remember/recall (§11).
- **A `PreToolUse`-style hook** (Claude Code pattern): intercept the agent's Grep/Glob/read and inject relevant symbols + prior decisions as `additionalContext`, so memory augments the agent's normal search transparently.
- **Session bootstrap:** on session start in a repo, auto-load that repo's high-importance conventions, procedural skills, and unresolved threads.

---

## 10. Multi-Repository & Multi-Session Model

The headline capability. Two dimensions: **across repos** (space) and **across sessions** (time).

### 10.1 Sessions as first-class objects

```jsonc
{
  "id": "ses_...",
  "workspace_id": "...", "repo_id": "...",
  "agent": "claude-code", "user": "...",
  "started_at": <ts>, "ended_at": null,
  "status": "active",                 // open|active|summarized|consolidated
  "goal": "refactor auth to PASETO",
  "episodic_log": [/* event refs */],
  "summary": null,                    // produced at close
  "open_threads": [/* unresolved items carried forward */],
  "outcome": null                     // success|abandoned|blocked + notes
}
```

**Lifecycle:** open → active (events stream into the episodic log) → on close, the consolidation loop (§8.5) produces a **session summary**, extracts durable facts, distills procedural skills, and records **open threads** (what was left unfinished). The next session in that repo bootstraps from the summary + open threads → genuine continuity. This is the concrete fix for "Claude Code / Cursor forget everything between sessions."

### 10.2 Cross-session continuity

- **Handoff:** session N's `open_threads` + summary become session N+1's bootstrap context ("last time: mid-refactor of `auth.py`, tests 3–5 failing, decided on PASETO").
- **Longitudinal facts:** because facts are bi-temporal, the agent can answer "when did we switch to PASETO and why" across dozens of sessions.
- **Cross-tool:** since memory lives in Engraphis (not the tool), a decision made in Cursor is recalled in Claude Code — the OpenMemory-style "store in one tool, recall in another" property, but scoped and code-aware.

### 10.3 Cross-repository knowledge

- **Scoped sharing:** workspace-scope memories are visible to all repos in the workspace ("org standardizes on pnpm + conventional commits"); repo-scope stays local.
- **Cross-repo entity resolution:** the same library, API, or concept appearing in multiple repos resolves to one canonical graph entity, so "how do we use Redis" aggregates across repos while remaining attributable per repo.
- **Promotion:** a convention observed in ≥N repos is proposed for workspace scope (§8.5) — the system *learns the org's norms* from repeated evidence.
- **Cross-repo retrieval mode:** recall can target "this repo", "all my repos", or "workspace", with results labeled by origin repo.

### 10.4 Identity & provenance

Every memory is attributable to its origin (session, message, document, commit, repo). This powers audit ("why does the agent think this?"), trust, and safe forgetting, and is essential for the security model (§16).

> **Net effect:** an agent using Engraphis behaves like a senior engineer who has worked across all your repos for months — it remembers decisions, knows the conventions, recalls what was tried, and picks up exactly where the last session left off. That experience is the product.

---

## 11. Interfaces & Integration

Three surfaces, one engine. **MCP is the wedge; REST is the backbone; SDKs and plugins are reach.**

### 11.1 MCP server (priority #1 for adoption)

Ship a first-class **Model Context Protocol** server so any MCP client (Claude Code, Cursor, Codex CLI, Cline, Zed, Windsurf, Claude Desktop) plugs in with a config snippet. Design the tool surface to be small, high-signal, and safe:

**Tools (write):**
- `remember(content, type?, scope?, importance?, metadata?)` — store a memory (explicit).
- `record_event(kind, content, refs?)` — append an episodic event (decision/bug/fix/try).
- `link(a, b, relation?)` — relate two memories/entities.
- `forget(id, reason)` / `pin(id)` / `correct(id, new_content)` — governance, audited.
- `start_session(repo, goal?)` / `end_session(outcome?)` — session lifecycle.

**Tools (read):**
- `recall(query, scope?, mode?, as_of?, k?)` — hybrid recall, returns packed context + provenance.
- `recall_proactive(repo?)` — "what should I know right now" (conscious recall).
- `search_code(query|symbol)` — symbol-graph + semantic code search.
- `why(symbol|decision)` — rationale/history for a piece of code.
- `timeline(entity, from?, to?)` — bi-temporal history of a fact.

**Resources:** expose the repo's conventions, open threads, and session summary as MCP **resources** so hosts can surface them without a tool call. **Prompts:** ship slash-command-style prompts (`/remember`, `/recall`, `/handoff`).

> Design note: keep tool count tight and descriptions crisp — coding agents choose tools from descriptions, so each tool's "when to use" must be unambiguous. Provide a `PreToolUse` hook recipe for Claude Code that auto-injects context around Grep/Glob.

### 11.2 REST API (backbone + SDK-compat)

Keep and extend the existing FastAPI routes; **retain `tinyhumansai` SDK-compatibility** so anyone pointed at neocortex can switch to a local Engraphis by changing a base URL (a real migration lever). Add scope/session/temporal parameters to every relevant route. New route groups: `/workspaces`, `/repos`, `/sessions`, `/graph`, `/code`, `/admin`. Maintain OpenAPI docs.

### 11.3 SDKs & plugins (reach, after core is proven)

- **SDKs:** Python and TypeScript first (covers the vast majority of agent frameworks); generate others from the OpenAPI spec as demand appears. (neocortex's 8-language SDK spread is breadth marketing — match it later, not first.)
- **Framework plugins:** LangGraph/LangChain, CrewAI, LlamaIndex, Mastra, Agno, AutoGen — thin adapters over the SDK. Prioritize LangGraph + CrewAI (largest agent audiences).
- **Editor/CLI:** a small CLI (`engraphis ...`) and the MCP server cover Claude Code/Cursor/Codex without bespoke plugins.

---

## 12. Data Model & Schema

Concrete SQLite schema (Postgres adapter mirrors it). Extends — does not discard — the current tables.

```sql
-- Tenancy & structure ---------------------------------------------------------
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL,
  settings TEXT DEFAULT '{}'                       -- JSON: decay params, models, policies
);
CREATE TABLE repos (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL, root_path TEXT, vcs_remote TEXT, primary_lang TEXT,
  created_at REAL, indexed_at REAL, settings TEXT DEFAULT '{}',
  UNIQUE(workspace_id, name)
);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, repo_id TEXT,
  agent TEXT, "user" TEXT, goal TEXT, status TEXT DEFAULT 'open',
  started_at REAL, ended_at REAL, summary TEXT, open_threads TEXT DEFAULT '[]',
  outcome TEXT
);

-- Memories (the atomic note) --------------------------------------------------
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL, repo_id TEXT, session_id TEXT,
  scope TEXT NOT NULL DEFAULT 'repo',              -- session|repo|workspace|user
  mtype TEXT NOT NULL DEFAULT 'semantic',          -- working|episodic|semantic|procedural
  title TEXT, content TEXT NOT NULL, summary TEXT,
  keywords TEXT DEFAULT '[]', metadata TEXT DEFAULT '{}',
  importance REAL DEFAULT 0.0, surprise REAL DEFAULT 1.0,
  stability REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0, last_access REAL,
  valid_from REAL, valid_to REAL,                  -- world-time (bi-temporal)
  ingested_at REAL, expired_at REAL,               -- system-time (bi-temporal)
  pinned INTEGER DEFAULT 0, sensitivity TEXT DEFAULT 'normal',
  provenance TEXT DEFAULT '{}'
);
CREATE INDEX idx_mem_scope ON memories(workspace_id, repo_id, scope, mtype);
CREATE INDEX idx_mem_valid ON memories(valid_from, valid_to, expired_at);

-- Vectors: sqlite-vec virtual table (or external index keyed by memory id) -----
CREATE VIRTUAL TABLE mem_vec USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[1024]);
-- Lexical: FTS5 over content+keywords ----------------------------------------
CREATE VIRTUAL TABLE mem_fts USING fts5(id UNINDEXED, title, content, keywords);

-- Knowledge graph (bi-temporal) ----------------------------------------------
CREATE TABLE entities (
  id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT,
  name TEXT, etype TEXT, canonical_id TEXT,        -- canonical_id => cross-repo resolution
  embedding_ref TEXT, created_at REAL, UNIQUE(workspace_id, repo_id, name, etype)
);
CREATE TABLE edges (
  id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT,
  src TEXT NOT NULL, dst TEXT NOT NULL, relation TEXT NOT NULL, weight REAL DEFAULT 1.0,
  valid_from REAL, valid_to REAL, ingested_at REAL, expired_at REAL,  -- bi-temporal edges
  provenance TEXT DEFAULT '{}'
);
CREATE INDEX idx_edge_src ON edges(workspace_id, src, valid_to, expired_at);
CREATE TABLE mem_links (a TEXT, b TEXT, relation TEXT, created_at REAL);  -- A-MEM links

-- Code symbol graph -----------------------------------------------------------
CREATE TABLE symbols (
  id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, kind TEXT,    -- function|class|method|type|...
  name TEXT, fqname TEXT, file TEXT, span TEXT, signature TEXT,
  lang TEXT, exported INTEGER, content_hash TEXT, embedding_ref TEXT, updated_at REAL
);
CREATE TABLE code_edges (
  id TEXT PRIMARY KEY, repo_id TEXT, src TEXT, dst TEXT,
  relation TEXT,                                            -- calls|imports|references|implements|tests
  file TEXT, line INTEGER
);

-- Event ledger & jobs (keep existing, scope-extended) ------------------------
CREATE TABLE events (
  id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT, session_id TEXT,
  kind TEXT, content TEXT, refs TEXT DEFAULT '[]', interaction_level TEXT, ts REAL
);
CREATE TABLE audit (                                         -- governance trail
  id TEXT PRIMARY KEY, ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT
);
```

Embedding dimension is per-model (e.g. 1024 for BGE-M3; 384 stays valid for MiniLM during migration). The vector table is one `VectorIndex` backend; LanceDB/Qdrant backends key on `memories.id`.

---

## 13. Core Algorithms

Reference pseudocode for the load-bearing logic.

### 13.1 Recall score

```python
def recall_score(m, q, *, now, weights, ppr):
    dt_days   = (now - m.last_access) / 86400
    retention = exp(-dt_days / max(m.stability, 1e-3))           # Ebbinghaus
    sem       = cosine(q.vec, m.vec)                              # semantic arm
    lex       = bm25_norm(q.text, m)                             # lexical arm
    graph     = ppr.get(m.id, 0.0)                               # PPR mass from query seeds
    recency   = exp(-(now - (m.valid_from or m.ingested_at)) / RECENCY_TAU)
    stale     = stale_penalty(m, now)                           # near/after valid_to
    w = weights[m.mtype]                                         # per-type weights
    return (w.r*retention + w.s*sem + w.l*lex + w.g*graph
            + w.i*m.importance + w.c*recency - w.x*stale)
```

### 13.2 Decay & reinforcement (keep + correct)

```python
# Reinforcement on access (spacing effect): stability grows sub-linearly with use.
def reinforce(m, signal="recall"):
    boost = INTERACTION_BOOST.get(signal, 0.1)                  # view .05 … create 1.0
    m.access_count += 1
    m.stability = m.stability * (1 + ALPHA*log(1+m.access_count)) + boost
    m.last_access = now()

# Decay lowers stability for the un-recalled — priority only, never deletes.
def decay(m, halflife_days):
    dt = (now() - m.last_access)/86400
    if dt <= 0: return
    m.stability = max(m.stability * 0.5**(dt/halflife_days), STABILITY_FLOOR if m.pinned else 0.01)
```

### 13.3 Conflict resolution (write path)

```python
def resolve(candidate, scope, llm):
    neighbors = hybrid_search(candidate, scope, k=8)
    decision  = llm.extract_json(RESOLVE_PROMPT(candidate, neighbors), DECISION_SCHEMA)
    match decision.op:
        case "ADD":        m = create_note(candidate); link_and_maybe_evolve(m, neighbors)
        case "UPDATE":     update_note(decision.target, candidate); reinforce(decision.target,"create")
        case "NOOP":       reinforce(decision.target, "recall")
        case "INVALIDATE": close_validity(decision.target, at=now())      # Zep-style
                           m = create_note(candidate, valid_from=now())
    audit(decision)
    return decision
```

### 13.4 Bi-temporal query

```python
def visible(memories, as_of=None):
    t = as_of or now()
    return [m for m in memories
            if (m.valid_from or 0) <= t and (m.valid_to is None or t < m.valid_to)
            and m.expired_at is None]
```

### 13.5 Graph-augmented retrieval (PPR seed)

```python
def graph_arm(query, scope):
    seeds = link_entities(extract_entities(query), scope)        # query → graph nodes
    mass  = graph.ppr(seeds, at=query.as_of)                     # personalized PageRank
    return top_k_memories_by_anchor(mass, k=100)                 # memories anchored to hot nodes
```

---

## 14. Evaluation & Benchmarking

"Better than neocortex" is a measurable claim. Build the eval harness early (Phase 1) and run it in CI so every change is scored. **You cannot optimize what you don't measure, and you cannot market what you can't prove.**

### 14.1 Public benchmarks to target

| Benchmark | What it tests | Why it matters here |
|---|---|---|
| **LoCoMo** | Very long-term multi-session conversational QA (1,540 Qs: single/multi-hop, temporal, open-domain) | The canonical multi-session memory test; mem0/Zep/A-MEM all report it — direct comparison. |
| **LongMemEval** | 500 Qs across single/multi-session recall, **knowledge-update**, temporal reasoning | Knowledge-update + temporal categories directly reward our conflict-resolution + bi-temporal design. |
| **RAGAS** (e.g. Sherlock corpus) | Answer relevancy, context precision/recall | neocortex publishes RAGAS — beat it on the same setup. |
| **TemporalBench** | Ordering, state-at-time, recency, interval | Our bi-temporal model should win the temporal categories. |
| **BABILong / HotPotQA** | Long-context reasoning / multi-hop | Stress the graph/PPR arm. |
| **Vending-Bench** | Long-horizon agentic decision-making (P&L over simulated days) | neocortex highlights it; shows memory→better decisions over time. |

### 14.2 A code-memory eval (build this — it doesn't exist yet, and owning it is strategic)

No standard benchmark measures cross-repo/cross-session *code* memory. Create **Engraphis-CodeMem**: a reproducible suite over real open-source repos with tasks like — recall a past architectural decision; answer "why is X this way"; reuse a procedural skill from a prior session; apply a cross-repo convention; resume an interrupted task from open-threads. Score accuracy, token cost, and time-to-context vs. a no-memory and a vector-only baseline. Publishing this (with a leaderboard) both proves the wedge and sets the category's terms.

### 14.3 Internal regression & ablations

- **CI gate:** a fixed eval set scored every PR; block merges that regress recall accuracy or latency beyond thresholds.
- **Ablations:** quantify each arm (semantic-only vs +lexical vs +graph vs +rerank) and each mechanism (decay on/off, consolidation on/off, bi-temporal on/off) so the design choices are evidence-backed, not assumed.
- **Targets (v1 ship bar):** ≥ parity with mem0/Zep published LoCoMo/LongMemEval numbers using a *local* model stack; win the temporal + knowledge-update categories; win Engraphis-CodeMem decisively vs vector-only.

---

## 15. Performance & Scaling

### 15.1 Targets

| Metric | v1 (Python + embedded ANN) | After Rust hot path |
|---|---|---|
| Recall latency (p95, 10⁵ memories, local) | < 150 ms (incl. rerank) | < 50 ms |
| Ingestion throughput (embed-bound) | thousands of facts/min | limited mainly by embedder/LLM |
| Memory scale (single SQLite + sqlite-vec) | 10⁵–10⁶ memories | 10⁶+ (LanceDB/Qdrant backend) |
| Code index (avg repo) | seconds, incremental on save | sub-second incremental |

### 15.2 How we get there without premature optimization

1. **Kill O(n) first** — `sqlite-vec`/LanceDB ANN replaces brute force (the single biggest win; pure architecture).
2. **Cache embeddings & rerank selectively** — only rerank top-K, only embed on change (content-hash).
3. **Batch + async** — batch embeddings, run hybrid arms concurrently, do consolidation off the hot path (background/at session close).
4. **Profile, then Rust** — when p95 latency or ingestion throughput misses targets at scale, move *only* the hot loop (vector scan + decay scoring + graph walk) to a **Rust/PyO3** module behind the existing `VectorIndex`/scoring interfaces. No rearchitecture.

### 15.3 When to escalate the stack (explicit triggers)

- **Introduce Rust core** when: profiling shows the scoring/scan loop is the bottleneck at target scale *and* the Python optimizations above are exhausted.
- **Introduce a standalone service / server DB (Qdrant + Postgres)** when: multi-user/team deployments, > ~10⁶ memories, or concurrent-write contention appear. Until then, embedded + local-first is a feature, not a limitation.

---

## 16. Security, Privacy & Multi-Tenancy

Memory is a high-value attack surface (the 2025–26 literature flags **memory poisoning** and "mnemonic" integrity as the central risks). Treat ingested content as untrusted.

- **Local-first & encryption.** Default to on-device storage. Offer encryption-at-rest (SQLCipher/Postgres TDE) for the DB and a secrets policy for API keys. No memory leaves the machine unless an external LLM/embedder is explicitly configured.
- **Tenancy isolation.** `workspace` is the hard isolation boundary (separate DB/encryption key per workspace option). Scope filters are enforced server-side on every read/write — never trust client-supplied scope alone.
- **Poisoning & injection defenses.** Sanitize and *quarantine* facts extracted from untrusted content; require corroboration (or explicit user confirmation) before a contradicting fact invalidates a high-importance one; rate-limit and anomaly-flag bulk contradictions (a poisoning signature). Keep provenance so any poisoned memory is traceable and reversible.
- **Drift governance.** Regenerate summaries from sources, not from prior summaries; periodically re-validate high-importance semantic facts against their episodic evidence; flag low-confidence consolidations for human review (cf. SSGM-style stability/safety governance).
- **PII & sensitivity.** A `sensitivity` flag on memories drives redaction in context packing and stricter retention/forgetting; provide a "forget about subject X" operation that invalidates + (on request) hard-deletes with audit.
- **Auditability.** Every mutation (add/update/invalidate/forget/promote) writes to the `audit` table with actor, action, target, and reason.

---

## 17. Observability & Memory Inspector

Make memory *visible* — for trust, debugging, and demos (the dashboard is also marketing).

- **Memory Inspector UI** (evolve the existing static dashboard): browse by workspace/repo/session/type; see decay curves, importance, access counts, and bi-temporal validity timelines per fact; view the knowledge graph and code symbol graph; trace provenance ("why does the agent know this?").
- **Recall explainer:** for any recall, show which arm (semantic/lexical/graph) surfaced each result and the score breakdown — invaluable for tuning weights and for user trust.
- **Live activity feed:** memories being created/updated/invalidated/decayed in real time (neocortex demos this; it's compelling and easy).
- **Metrics:** Prometheus-style counters/histograms (recall latency by arm, ingestion rate, store size, consolidation actions, contradiction rate) for ops and for the benchmark harness.

---

## 18. Phased Roadmap

Each phase ends with a demoable capability and a benchmark number. Phases are sequenced so the wedge (code + MCP + sessions) lands as early as is responsible, on top of a sound core.

### Phase 0 — Foundations & interfaces (1–2 wks)
Define the interface contracts (§6.3); introduce `workspace/repo/session` entities and the new `memories` schema (§12) with a migration from the current DB; stand up the eval harness skeleton (§14) and CI. **Exit:** new schema live, old data migrated, LoCoMo/LongMemEval runnable end-to-end (even if scores are baseline).

### Phase 1 — Real retrieval core (2–3 wks)
Replace brute-force with `sqlite-vec` ANN; add FTS5 lexical arm; implement hybrid fusion + the six-term recall score + cross-encoder rerank; upgrade default embeddings (BGE-M3/Qwen3 class) behind the `Embedder` interface. **Exit:** ≥ parity with mem0/Zep published LoCoMo numbers using a local stack; ablations show each arm's contribution.

### Phase 2 — Bi-temporal facts & self-maintenance (2–3 wks)
LLM fact extraction; ADD/UPDATE/NOOP/INVALIDATE conflict resolution; bi-temporal graph (validity intervals) and `as_of` time-travel queries; A-MEM linking + evolution. **Exit:** win LongMemEval knowledge-update + temporal categories vs the Phase 1 baseline.

### Phase 3 — The wedge: code-aware + MCP + sessions (3–4 wks)
Tree-sitter code symbol graph with incremental indexing; session lifecycle + summaries + open-threads + bootstrap; the MCP server (tools/resources/prompts) and a Claude Code `PreToolUse` hook recipe; `search_code` / `why` / `timeline`. **Exit:** a coding agent (Claude Code/Cursor) demonstrably resumes work across sessions and answers cross-repo "why" questions; Engraphis-CodeMem v0 passing vs vector-only baseline.

### Phase 4 — Consolidation & cross-repo intelligence (2–3 wks)
The full consolidation/reflection loop (episodic→semantic→procedural, reflection, drift-guarded summarization); scope promotion; cross-repo entity resolution; governed forgetting + pinning. **Exit:** measurable cross-session learning (accuracy improves as sessions accumulate on Vending-Bench-style long-horizon eval); conventions auto-promote across repos.

### Phase 5 — Hardening, security, reach (3–4 wks)
Poisoning/drift defenses (§16); encryption-at-rest; Postgres/Qdrant adapters; Memory Inspector UI + recall explainer; Python + TypeScript SDKs; LangGraph + CrewAI plugins; docs site. **Exit:** team-deployable; SDKs published; security review passed.

### Phase 6 — Performance core & polish (as needed, trigger-driven)
Only if §15 triggers fire: Rust/PyO3 hot-path core; LanceDB tuning; throughput benchmarks targeting neocortex-class speed; public benchmark report + Engraphis-CodeMem leaderboard. **Exit:** published numbers beating named competitors on chosen axes.

> **Sequencing rationale:** core retrieval quality (P1) and temporal truth (P2) must exist before the wedge (P3) is credible; consolidation (P4) needs sessions (P3) to consolidate from; performance (P6) is deferred until correctness is proven and profiling justifies it. Ship demoable value from Phase 3 onward.

---

## 19. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Scope creep** (this plan is large) | High | Phases are independently shippable; P1–P3 deliver the differentiated product. Cut SDK breadth and server backends first if time-pressed. |
| **LLM-in-the-write-path cost/latency** (extraction, resolution) | Med | Use small/local models for extraction/resolution; batch; make it async/off-hot-path; deterministic fallbacks for NOOP/dedup. |
| **Consolidation semantic drift** | Med | Regenerate from sources not summaries; keep originals; confidence-flag merges; re-validate high-importance facts (§16). |
| **Graph complexity / Neo4j-style ops burden** | Med | Stay in SQLite/Kùzu (embedded); graph is one retrieval arm, not the whole store; PPR is bounded by scope. |
| **Embedding/model churn** | Med | Everything behind `Embedder`/`Reranker` interfaces; store embed-model + dim per memory; support re-embedding migrations. |
| **Memory poisoning** | Med | Provenance, quarantine, corroboration-before-invalidate, anomaly flags, full audit/reversibility. |
| **"Better than neocortex" unproven** | Med | Benchmark harness in CI from Phase 0; never claim without a number; own Engraphis-CodeMem. |
| **Competitors add multi-repo/code** | Low–Med | Open + local-first + the unified synthesis is hard to replicate quickly; move fast on the wedge and the eval leaderboard. |

---

## 20. Appendix: Glossary & References

### Glossary

- **Bi-temporal** — modeling two time axes: *world-time* (`valid_from`/`valid_to`, when a fact is true) and *system-time* (`ingested_at`/`expired_at`, when the system knew it).
- **Conscious / proactive recall** — surfacing relevant memory without an explicit query, from recent activity + decay.
- **Consolidation** — converting episodic events into durable semantic facts and procedural skills.
- **Decay (Ebbinghaus)** — `R(t)=exp(-t/S)`; retrieval priority falls with time-since-access unless reinforced.
- **PPR** — Personalized PageRank over the knowledge graph, seeded from query entities (associative recall).
- **Scope** — visibility/ownership level of a memory: `session` < `repo` < `workspace` < `user`.
- **Promotion** — moving a memory to a broader scope once it proves broadly true.

### Key references (research backing this plan)

- neocortex / OpenHuman — *The Fastest AI Memory Model* — https://github.com/tinyhumansai/neocortex · https://tinyhumans.ai/neocortex
- Mem0 — *Building Production-Ready AI Agents with Scalable Long-Term Memory* — https://arxiv.org/abs/2504.19413 · https://github.com/mem0ai/mem0 · OpenMemory MCP: https://mem0.ai/blog/introducing-openmemory-mcp
- Zep / Graphiti — *A Temporal Knowledge Graph Architecture for Agent Memory* — https://arxiv.org/abs/2501.13956 · https://github.com/getzep/graphiti
- Letta / MemGPT — *Memory Blocks* / *LLMs as Operating Systems* — https://www.letta.com/blog/memory-blocks/ · https://arxiv.org/abs/2310.08560
- A-MEM — *Agentic Memory for LLM Agents* (Zettelkasten) — https://arxiv.org/abs/2502.12110
- HippoRAG — neurobiologically-inspired long-term memory / PPR retrieval — https://arxiv.org/abs/2405.14831
- Generative Agents — *Interactive Simulacra of Human Behavior* (recency × importance × relevance, reflection) — https://arxiv.org/abs/2304.03442
- Benchmarks — LoCoMo: https://github.com/snap-research/locomo · LongMemEval: https://arxiv.org/abs/2410.10813 · State of AI Agent Memory 2026: https://mem0.ai/blog/state-of-ai-agent-memory-2026
- Embeddings/rerankers — Qwen3-Embedding: https://github.com/QwenLM/Qwen3-Embedding · MTEB leaderboard (BGE-M3, NV-Embed, etc.)
- Vector indexes — sqlite-vec · LanceDB · Qdrant benchmarks: https://qdrant.tech/benchmarks/
- Code-aware memory — codebase-memory-mcp: https://github.com/DeusData/codebase-memory-mcp · *AST-Derived Graphs vs LLM-Extracted KGs*: https://arxiv.org/abs/2601.08773 · RANGER: https://arxiv.org/abs/2509.25257
- Surveys — *Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers* (2026); *Rethinking Memory in LLM-based Agents* — https://arxiv.org/abs/2505.00675 ; SSGM governance — https://arxiv.org/abs/2603.11768

---

*End of plan. Build Phase 0–3 to have a differentiated, demoable product; Phase 4–6 to make it the best available.*
