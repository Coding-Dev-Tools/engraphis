# Engraphis

**The local-first memory engine for AI agents.** Give your agents memory that persists across
sessions and repositories — Ebbinghaus forgetting-curve decay, interaction-aware reinforcement,
bi-temporal facts, and hybrid (vector + lexical + graph) recall — running entirely on your own
machine. No API key for the memory layer, no rate limits, no per-token cost, no data leaving
your box.

You bring the LLM (OpenAI, Anthropic, Google, OpenRouter, or any OpenAI-compatible endpoint)
only for the optional chat/synthesis features. The memory engine itself is 100% local:
SQLite + local embeddings.

> **Why Engraphis?** Hosted memory services make you ship your agent's memory to someone else's
> cloud and meter you per operation. Engraphis is the opposite: self-hosted, private, and free at
> the core — with a native **MCP server** so coding agents (Claude Code, Cursor, Cline, Zed,
> Windsurf) plug in and stop forgetting.

- **Local-first & private** — runs offline; the core depends only on `numpy`.
- **Agent-native** — an MCP server exposes 17 tools: remember/recall/ingest, bi-temporal why/timeline,
  proactive recall, governance (forget/pin/correct), code search, and session handoff.
- **Self-maintaining facts** — writes are deterministically conflict-resolved (no LLM required):
  a near-duplicate reinforces the existing memory instead of cloning it, and a same-subject
  update supersedes the old one instead of leaving a silent contradiction.
- **Principled recall** — six-term score over retention, semantic, lexical, graph, importance,
  and recency, fused with Reciprocal Rank Fusion and reranked.
- **Truth is temporal** — contradictions invalidate (bi-temporal `valid_from/valid_to`) instead
  of overwriting; ask `as_of` questions, or just call `engraphis_why` / `engraphis_timeline`.
- **Code-aware** — `engraphis_index_repo` parses a repository into a function/class/call-graph
  (AST via tree-sitter, regex fallback with zero dependencies) so `engraphis_search_code`
  answers "what calls this" far more cheaply than grepping or dumping files. **No other memory
  engine in the category has this.**
- **Graph recall that walks** — the graph arm is Personalized PageRank over entities, mentions,
  and memory links (HippoRAG-style, pure NumPy): multi-hop associations surface without an
  explicit hop count.
- **Self-improving network** — A-MEM-style evolution: each new memory auto-links to its closest
  related notes and strengthens them, so the network gets better in both directions.
- **Optional fact extraction** — feed raw transcripts/notes to `engraphis_ingest`; with
  `ENGRAPHIS_EXTRACTOR=llm` they're distilled into discrete typed facts first (offline default:
  passthrough — no behaviour change, no new dependency).
- **Sleep-time consolidation** — a schedulable local job (`engraphis-consolidate`) distills
  recurring episodes into durable semantic digests and archives fully-decayed transients —
  audited, recoverable, pinned memories exempt. Your machine, your schedule; no cloud service.
- **Memory Inspector** — a product UI (`engraphis-inspector`, :8710) over the same service
  layer as the MCP tools: search, why/history, timeline, health, audit, and a supersession-chain
  view with word-level diffs — *see exactly when a fact changed and why*.
- **Scoped** — every memory lives in a `workspace → repo → session` hierarchy.

---

## Install

```bash
pip install -e .                # core library — numpy only, fully offline
pip install -e ".[mcp]"         # + MCP server (for Claude Code / Cursor / agents)
pip install -e ".[server]"      # + REST server & dashboard
pip install -e ".[all]"         # everything
```

> The first run with real embeddings downloads `all-MiniLM-L6-v2` (~80 MB). Without
> sentence-transformers installed, the engine automatically falls back to a deterministic
> offline embedder so it always runs.

---

## Free forever vs. Pro

The engine — recall, bi-temporal history, governance, code graph, MCP server, single-user
Inspector — is free and Apache-2.0, permanently. A license key (verified **offline**; no
phone-home, in keeping with local-first) unlocks the paid layer:

| | Free | Pro ($20/mo) | Team ($35/user/mo) |
|---|---|---|---|
| Memory engine + 17 MCP tools | ✓ | ✓ | ✓ |
| Memory Inspector (single-user) | ✓ | ✓ | ✓ |
| Analytics dashboard (growth, retention, decay forecast) | | ✓ | ✓ |
| Compliance export (full bi-temporal JSON dump) | | ✓ | ✓ |
| Multi-user Inspector: logins, roles, seat management | | | ✓ |
| Priority support | | ✓ | ✓ |

Paste your key into the Inspector's license dialog (the plan badge, top-left) or set
`ENGRAPHIS_LICENSE_KEY`. Get a key at <https://engraphis.dev/pro>.

---

## Quickstart A — MCP server (the headline)

Plug Engraphis into any MCP-capable agent. With Claude Code:

```bash
pip install -e ".[mcp]"
engraphis-init                  # writes .env (DB location) + prints the exact snippets
claude mcp add engraphis -- engraphis-mcp
```

`engraphis-init --check` is the doctor: verifies the install, extras, and DB writability.

For Cursor / Cline / Zed / Windsurf, add to your MCP config:

```json
{
  "mcpServers": {
    "engraphis": { "command": "engraphis-mcp" }
  }
}
```

Your agent now has 17 tools:

| Category | Tool | What it does |
|---|---|---|
| Write | `engraphis_remember` | Store a fact; deterministically resolved against similar memories (add/reinforce/supersede) |
| Write | `engraphis_record_event` | Append a lightweight episodic log entry (lower ceremony than remember) |
| Write | `engraphis_link` | Explicitly connect two related memories |
| Read | `engraphis_recall` | Hybrid vector + lexical + graph recall for a query |
| Read | `engraphis_recall_proactive` | "What should I know right now" — no query needed |
| Read | `engraphis_why` | The current answer to a question, plus whatever it superseded |
| Read | `engraphis_timeline` | Full bi-temporal history of a fact, oldest first |
| Code | `engraphis_index_repo` | Parse a repo into the code symbol graph (functions/classes/calls) |
| Code | `engraphis_search_code` | Find symbols by name, with their callers |
| Governance | `engraphis_forget` | Retire a memory — bi-temporal close, never a hard delete |
| Governance | `engraphis_pin` | Exempt a memory from future automatic decay/pruning |
| Governance | `engraphis_correct` | Replace a memory's content without losing the history |
| Session | `engraphis_start_session` / `engraphis_end_session` | Session lifecycle with cross-session handoff (open threads/summary carry forward) |
| Ops | `engraphis_stats` | Memory counts for health/onboarding checks |

Example flow an agent runs on its own:

```text
engraphis_remember(content="We use pnpm for all frontend repos.", workspace="acme", repo="web")
engraphis_recall(query="which package manager for the frontend?", workspace="acme", repo="web")
  → "We use pnpm for all frontend repos."   (survives across sessions and restarts)

engraphis_remember(content="We switched the rate limit from 100 to 500 req/min.", ...)
engraphis_why(query="what is the rate limit", ...)
  → answer: 500 req/min · supersedes: the old 100 req/min memory (closed, not deleted)
```

---

## Quickstart A′ — install the memory-discipline skill (portable)

The MCP server gives your agent the 17 tools; the **`engraphis-memory` skill** teaches it *when and
how* to use them — what to store, how to scope it, and which tool answers which question. It follows
the [Agent Skills](https://agentskills.io) standard, so the same skill works in Claude Code, Codex,
and OpenCode.

Install as a Claude Code plugin:

```
/plugin marketplace add Coding-Dev-Tools/engraphis
/plugin install engraphis-memory@engraphis
```

Or with `npx skills` (any skills-compatible agent):

```
npx skills add https://github.com/Coding-Dev-Tools/engraphis
```

Or manually — copy `skills/engraphis-memory/` into your agent's skills directory (`.claude/skills/`,
`~/.codex/skills/`, or `~/.opencode/skills/`).

The skill is plain markdown (`skills/engraphis-memory/SKILL.md` + `references/`), pairs with the MCP
server above, and adds no dependencies.

---

## Quickstart B — REST server + dashboard

```bash
pip install -e ".[server]"
cp .env.example .env            # optional: set your LLM provider + key
python -m scripts.start_server  # http://127.0.0.1:8700  (dashboard at /, OpenAPI at /docs)
```

Use it from Python:

```python
import httpx
with httpx.Client(base_url="http://127.0.0.1:8700", timeout=60) as c:
    c.post("/memory/insert", json={"key": "theme", "content": "User prefers dark mode.",
                                   "namespace": "preferences"})
    r = c.post("/memory/query", json={"namespace": "preferences",
                                      "query": "what theme does the user prefer?", "maxChunks": 5})
    print(r.json()["data"]["llmContextMessage"])
```

Run it in Docker:

```bash
docker compose up --build       # binds 8700, persists the DB in a named volume
```

---

## Quickstart C — Python library (no server)

```python
from engraphis.service import MemoryService

mem = MemoryService.create("engraphis.db")          # or ":memory:"
mem.remember("Auth migrated from JWT to PASETO because key rotation was painful.",
             workspace="acme", repo="api", mtype="episodic")
hit = mem.recall("why did we change auth?", workspace="acme", repo="api")
print(hit["context"])
```

The same validated `MemoryService` backs the MCP server, so behavior is identical everywhere.

---

## How it works

```
query
  └─ SearchFilter (scope + as_of time anchor)
     └─ 3 retrieval arms (parallel, then fused):
        • vector   — cosine over local embeddings
        • lexical  — FTS5/BM25 (with a LIKE fallback)
        • graph    — entity-expansion over the bi-temporal knowledge graph
     └─ Reciprocal Rank Fusion + six-term weighted score
     └─ rerank → context packing (token budget) → reinforce
```

**Key algorithms**

- **Ebbinghaus retention:** `R(t) = exp(−Δt / S)` — memories decay unless reinforced.
- **Spacing-effect reinforcement:** `S_new = S·(1 + α·ln(1 + access_count)) + boost`.
- **Interaction boosts:** view `0.05` · recall `0.15` · react `0.20` · reply `0.50` · create `1.00`.
- **Six-term recall score:** `w_r·retention + w_s·semantic + w_l·lexical + w_g·graph + w_i·importance + w_c·recency − w_x·staleness`, per-memory-type weights.

Memories are **typed** (`working` / `episodic` / `semantic` / `procedural`) and **scoped**
(`session` / `repo` / `workspace` / `user`), each with its own lifecycle and weight profile.

---

## Configuration

All via environment (or `.env`). Common keys:

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Server port |
| `ENGRAPHIS_API_TOKEN` | — | If set, REST API requires `Authorization: Bearer <token>` |
| `ENGRAPHIS_CORS_ORIGINS` | loopback | Comma-separated CORS allow-list |
| `ENGRAPHIS_DB_PATH` | `./engraphis.db` | SQLite database file |
| `ENGRAPHIS_EMBED_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model (empty → offline embedder) |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | `openai \| anthropic \| google \| openrouter \| custom` |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | LLM model name |
| `ENGRAPHIS_LLM_API_KEY` | — | LLM API key (only for chat/synthesis) |
| `ENGRAPHIS_LOOP_INTERVAL` | `60` | Background consolidation seconds (0 = off) |

See `.env.example` for the full list.

---

## Project structure

```
engraphis/
├── engraphis/
│   ├── core/                # v2 engine — interfaces, store, recall, scoring, resolve, schema, ids
│   ├── backends/            # pluggable embedder / vector index / reranker / codegraph (offline fallbacks)
│   ├── service.py           # validated MemoryService facade (no MCP dependency)
│   ├── mcp_server.py        # MCP server — 17 tools (write/read/governance/code/session)
│   ├── config.py            # env-driven settings
│   ├── app.py               # REST server (FastAPI) + dashboard + auth middleware
│   ├── routes/ stores/ engines/ llm/   # REST server surface
│   └── static/              # dashboard
├── eval/                    # offline retrieval eval harness + datasets (incl. conflict-resolution cases)
├── tests/                   # pytest suite (offline, numpy-only core)
├── scripts/                 # start_server, cli, migrate_to_v2, seed_from_obsidian
├── Dockerfile / docker-compose.yml
└── pyproject.toml
```

---

## Development

The offline quality gate (no network, no API key) — keep it green:

```bash
pip install numpy pytest ruff
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.ablation
ruff check .
```

For the code-symbol graph with real AST parsing (optional; falls back to a regex
indexer without it): `pip install -e ".[code]"`.

See `AGENTS.md` for architecture and conventions, `SECURITY.md` for the threat model, and
`CHANGELOG.md` for release notes.

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). "Engraphis" is a trademark of the
Engraphis project; the license does not grant trademark rights.
