# Engraphis + Kilo Code — Technical User Manual

**How Engraphis works, how to set up Kilo Code, and how to wire the two together so your coding agent stops forgetting.**

This manual is written for someone who wants the full technical picture: what Engraphis actually is, how its memory engine behaves, how Kilo Code talks to it over MCP, and the exact configuration to make the connection reliable and optimal. It deliberately covers both layers — the *transport* (getting the pipe connected) and the *orchestration* (how to use it well once it's connected), because those are two different problems and most confusion comes from mixing them up.

---

## 0. The two-layer mental model (read this first)

There are two separate questions hiding inside "connect Kilo Code to Engraphis," and they are usually where people talk past each other:

1. **Transport layer — "get the pipes connected."** This is: install the Engraphis MCP server, tell Kilo Code how to launch it, confirm the tools show up. It's a plumbing task. When it's done, Kilo Code can *see* 27 `engraphis_*` tools. Success here is binary — either the tools appear or they don't.

2. **Orchestration layer — "use the memory well."** This is: *when* should the agent remember vs. recall, how should memories be scoped (`workspace → repo → session`), which of the 28 tools answers which question, and how to keep the store clean over time. This is where the actual value is, and it's a discipline, not a config.

You need both. A perfect config with no discipline gives you an agent that has memory tools and never uses them correctly. Good discipline with a broken config gives you an agent that wants to remember and can't. **Section 3 is the transport layer. Sections 4–6 are the orchestration layer.** Do them in order.

---

## 1. What Kilo Code is (and what role it plays here)

Kilo Code is an open-source AI coding agent that runs as a VS Code extension (and a CLI). For the purposes of this integration, the only thing that matters is: **Kilo Code is an MCP client.** MCP (Model Context Protocol) is the open standard that lets an AI agent call external tools exposed by a "server." Kilo Code speaks MCP; Engraphis ships an MCP server. That's the entire basis of the integration — no plugin, no bespoke API, no glue code.

Kilo Code supports two MCP transport types:

- **Local (STDIO)** — the server runs as a child process on your machine and communicates over standard input/output. Lower latency, no network exposure, simpler. **This is what you want for Engraphis**, because Engraphis is a local-first engine that lives on your machine.
- **Remote (HTTP/SSE)** — the server is hosted over HTTP. Only relevant if you're pointing at a shared/hosted Engraphis instance, which is the exception, not the rule.

Kilo Code stores MCP configuration in a JSON-with-comments file (`kilo.jsonc`) at two levels: **global** (`~/.config/kilo/kilo.jsonc`, applies to every project) and **project-level** (`kilo.jsonc` or `.kilo/kilo.jsonc` in a project root, which takes precedence). You can edit these through the extension UI (**Settings → MCP → Add Server**) or by hand.

---

## 2. What Engraphis is (the engine Kilo Code will be talking to)

Engraphis is a **local-first, open memory engine for AI agents.** The problem it solves: your coding agent forgets everything between sessions. Every new session it re-asks which package manager you use, re-learns the codebase, forgets why you chose one library over another. Engraphis gives the agent durable, scoped, *explainable* memory that persists across sessions and repositories.

Everything runs on your machine. The whole store is a single SQLite file. Local embeddings mean no API key is required for the memory layer itself (an external LLM is optional and only used for chat/synthesis). It's Apache-2.0 licensed and self-hostable.

You interact with Engraphis through three surfaces, all backed by the *same* engine (`MemoryService`), so they can never drift apart:

- **The dashboard WebUI** (`engraphis-dashboard`, `http://127.0.0.1:8700`) — a visual product to see, search, and curate memory.
- **The MCP server** (`engraphis-mcp`) — the 28 tools your coding agent calls. **This is the surface Kilo Code uses.**
- **The Python library** (`from engraphis.service import MemoryService`) — for direct programmatic use.

### 2.1 The five ideas that make it more than a vector store

These are the properties that matter when you're deciding how to use it well:

1. **Scoped.** Every memory lives in a `workspace → repo → session` hierarchy. A memory can be visible at `session`, `repo`, `workspace`, or `user` level. This is what lets one agent work across many repos without cross-contaminating context.

2. **Typed.** Every memory is one of four types — `semantic` (durable facts/conventions), `episodic` (events/decisions that happened), `procedural` (how-tos), or `working` (transient scratch). Each type has its own scoring weights and lifecycle. Getting scope + type right is ~90% of using Engraphis well.

3. **Bi-temporal.** Truth is temporal. When a fact changes, Engraphis does **not** overwrite the old one — it *invalidates* it (closes its validity window) and stores the new version, recording that the new one supersedes the old. History is preserved, so "we used to do X, then switched to Y because Z" stays answerable forever. This is the single biggest difference from a plain vector store.

4. **Self-maintaining.** Writes are *deterministically* conflict-resolved with no LLM call: on each write, Engraphis checks the new content against similar existing memories and decides **ADD** (new), **NOOP** (near-duplicate — reinforce the existing one instead of duplicating), or **INVALIDATE** (same subject, changed — supersede the old one). Decay follows the Ebbinghaus forgetting curve; use reinforces (spacing effect). Forgetting *lowers retrieval priority* — it never hard-deletes.

5. **Explainable / grounded.** Every memory carries provenance ("why is this known?"). Recall can return a cited answer or explicitly *abstain* when nothing in scope actually supports the query, so you get "insufficient evidence" instead of a confident guess.

### 2.2 How recall actually works (so you know what you're getting)

When the agent calls `engraphis_recall`, the query runs through three retrieval arms **in parallel**, which are then fused:

- **Vector** — cosine similarity over local embeddings.
- **Lexical** — FTS5/BM25 full-text (with a `LIKE` fallback on SQLite builds without FTS5).
- **Graph** — Personalized PageRank over an entity/link graph.

The three are combined with Reciprocal Rank Fusion, then scored by a six-term weighted function over **retention, semantic similarity, lexical match, graph centrality, importance, and recency** (minus a staleness penalty), then the top results are reranked and packed into a token budget. The upshot: recall is hybrid and principled, not just nearest-neighbor. You don't have to do anything to get this — it's what `engraphis_recall` does by default.

---

## 3. Transport layer — connecting Kilo Code to Engraphis

This is the "get the pipes connected" part. Three steps: install the server, register it with Kilo Code, verify.

### 3.1 Install the Engraphis MCP server

Engraphis is a Python package. Install the MCP variant:

```bash
pip install "engraphis[mcp]"
```

Then run the one-time initializer, which writes an `.env` with an absolute DB path and prints config snippets:

```bash
engraphis-init
```

This gives you a console command, `engraphis-mcp`, which is the actual MCP server (it speaks stdio — exactly the transport Kilo Code's "Local (STDIO)" type expects). You can sanity-check that it's on your PATH:

```bash
engraphis-mcp --help    # or just confirm the command resolves
```

> **Note on the database.** The memory store is a single SQLite file. `engraphis-init` sets `ENGRAPHIS_DB_PATH` to an absolute path in your `.env`. If you also run the dashboard, point it at the *same* DB path so the WebUI and the agent share one memory store. Mismatched DB paths is the #1 cause of "I remembered something but can't see it in the dashboard."

### 3.2 Register the server in Kilo Code

You have two equivalent options.

**Option A — the UI (recommended for first-timers).** In VS Code: open Kilo Code **Settings → MCP → Add Server → Local (stdio)**. Fill in:

- **Name:** `engraphis`
- **Command / Arguments:** see the platform note below.

**Option B — edit the config file directly.** MCP servers live under the top-level `mcp` key in `kilo.jsonc`. Put it in `~/.config/kilo/kilo.jsonc` for every project, or `.kilo/kilo.jsonc` in a specific project root (project-level wins if both exist).

**macOS / Linux** — the executable can be used directly:

```jsonc
{
  "mcp": {
    "engraphis": {
      "type": "local",
      "command": ["engraphis-mcp"],
      "environment": {
        "ENGRAPHIS_DB_PATH": "/absolute/path/to/engraphis.db"
      },
      "enabled": true,
      "timeout": 15000
    }
  }
}
```

**Windows** — wrap console commands with `cmd /c` (this is Kilo Code's documented pattern for local servers on Windows):

```jsonc
{
  "mcp": {
    "engraphis": {
      "type": "local",
      "command": ["cmd", "/c", "engraphis-mcp"],
      "environment": {
        "ENGRAPHIS_DB_PATH": "C:\\Users\\you\\engraphis.db"
      },
      "enabled": true,
      "timeout": 15000
    }
  }
}
```

Notes on the fields:

- `type: "local"` selects STDIO transport. Do **not** use `remote` unless you are deliberately pointing at a hosted HTTP Engraphis instance.
- `command` is an **array** (executable first, then args). If `engraphis-mcp` isn't on PATH inside VS Code's environment, use the absolute path to the console script, or invoke it as `["python", "-m", "engraphis.mcp_server"]`.
- `environment` is where you pin the DB path (and any LLM/extractor settings, below). Kilo Code also supports `{env:VARIABLE_NAME}` syntax to pull from your real environment.
- `timeout` is in milliseconds; the default for local servers is 10 s. Bump it to `15000` because Engraphis lazily loads its embedding model on the *first* tool call, which can take a moment.

### 3.3 Verify the pipe is connected

Reload Kilo Code (or toggle the server off/on in **Settings → MCP**). You should now see the `engraphis_*` tools available. The fastest end-to-end check is to ask Kilo Code to call the health tool:

> "Call `engraphis_stats` and show me the result."

A JSON response with memory counts means the transport layer is fully working. If it errors, jump to Section 7 (Troubleshooting).

### 3.4 (Optional) Auto-approve the read tools

Kilo Code gates each MCP tool call behind an approval prompt. The permission key is the namespaced name `{server}_{tool}`. For a smooth loop, auto-approve the read-only tools (they can't damage anything) while keeping writes/governance manual until you trust the flow. In `kilo.jsonc`:

```jsonc
{
  "permission": {
    "engraphis_recall": "allow",
    "engraphis_recall_grounded": "allow",
    "engraphis_recall_proactive": "allow",
    "engraphis_why": "allow",
    "engraphis_timeline": "allow",
    "engraphis_search_code": "allow",
    "engraphis_stats": "allow"
  }
}
```

You can also click **Approve Always** on any tool at runtime to write the same rule. A blanket `"engraphis_*": "allow"` works too, but auto-approving *writes* means the agent can reshape your memory without you seeing it — approve those consciously at first.

---

## 4. The 28 tools — the orchestration surface

Once connected, Kilo Code sees these. Do **not** assume only `remember`/`recall` exist — the value is in the rest. This is the full surface, grouped by what question each one answers.

| Category | Tool | What it does |
|---|---|---|
| **Write** | `engraphis_remember` | Store a fact; deterministically resolved to add / reinforce (noop) / supersede (invalidate). |
| Write | `engraphis_record_event` | Append a lightweight episodic log entry — lower ceremony than remember; repeats are a promotion signal. |
| Write | `engraphis_link` | Explicitly connect two related memories (e.g. a bug ↔ its fix). |
| Write | `engraphis_ingest` | Store raw/undistilled text; extracts discrete facts first when an LLM extractor is configured. |
| Write | `engraphis_ingest_postgres_schema` | Convert a live PostgreSQL catalog into schema memories + graph nodes; the DSN is never stored. |
| **Read** | `engraphis_recall` | Hybrid vector + lexical + graph recall; returns packed context + scored memories. |
| Read | `engraphis_recall_grounded` | Cited answer assembled *only* from retrieved memories — or abstains if nothing supports it. |
| Read | `engraphis_answer` | Backward-compatible grounded-answer alias; prefer `engraphis_recall_grounded` for new configs. |
| Read | `engraphis_recall_proactive` | "What should I know right now" — no query; high-importance/recent/reinforced memories + last-session handoff. |
| Read | `engraphis_proactive_context` | Build a task-aware, cited context packet from proactive recall, current agent state, and the last-session handoff. |
| Read | `engraphis_why` | The current answer to a question **plus** what it superseded (bi-temporal). |
| Read | `engraphis_timeline` | Every version of a fact, oldest → newest, with `valid_from`/`valid_to`. |
| **Code** | `engraphis_index_repo` | Incrementally parse a multi-language repo and link symbols to relevant memories. |
| Code | `engraphis_search_code` | Find symbols, callers, docstrings, and linked decisions/incidents/procedures. |
| Code | `engraphis_code_path` | Explain a path across files, definitions, calls, imports, and memories. |
| Code | `engraphis_code_impact` | Rank commit/PR impact by dependents, communities, memories, and hotspots. |
| Code | `engraphis_export_code_graph` | Portable graph JSON + Markdown + self-contained HTML. |
| **Audit** | `engraphis_receipts` | List content-free hashed operation receipts. |
| Audit | `engraphis_verify_receipts` | Verify the tamper-evident receipt chain. |
| Audit | `engraphis_export_receipts` | Export a privacy-safe receipt-only audit bundle. |
| **Governance** | `engraphis_forget` | Retire a memory — bi-temporal close, never a hard delete. |
| Governance | `engraphis_pin` | Exempt a memory from automatic decay/pruning (identity/durable facts). |
| Governance | `engraphis_correct` | Replace a memory's content without losing history — keeps the "why" chain. |
| Governance | `engraphis_promote` | Widen scope while preserving and linking the narrow-scope history. |
| **Session** | `engraphis_start_session` | Open a session; its `bootstrap` returns the last session's summary + open threads for resume. |
| Session | `engraphis_end_session` | Close a session with a summary + `open_threads` for next time. |
| **Ops** | `engraphis_stats` | Memory counts by type/workspace — health/onboarding checks. |
| Maintenance | `engraphis_consolidate` | Sleep-time sweep: recurring episodes → semantic digest; decayed transients archived. Dry-run by default. |

---

## 5. Orchestration — the optimal workflow

This is how to make the connection actually pay off. The discipline fits on a card:

> **Golden rule: recall before you ask; remember before you move on.** If the agent had to re-derive something it already figured out once, that was a missing `engraphis_remember`.

### 5.1 The core loop for a coding task

1. **Starting work in a repo** → `engraphis_recall_proactive` (loads high-signal context with no query) and, for multi-step work, `engraphis_start_session` (its `bootstrap` hands back the last session's summary and unresolved `open_threads`, so the agent resumes instead of starting cold).
2. **Before answering or acting**, when prior context would help → `engraphis_recall`. Do this *before* asking you something you may have already said.
3. **The moment it learns something durable** → `engraphis_remember` (a convention, a decision *with its rationale*, a bug's cause→fix, a preference, a reusable procedure).
4. **Finishing the task** → `engraphis_end_session` with a `summary` and `open_threads` for the next session in that repo.

### 5.2 Scope in one minute

`workspace → repo → session → memory`. On every write, choose:

- **workspace** — the org or product (e.g. `acme`). Always required.
- **repo** — the repository (e.g. `backend`). Omit only for genuinely workspace-wide facts.
- **session** — one unit of work; pass its `session_id` so memories group and resume.

Pick the **narrowest scope that is still reusable**. A fix specific to one repo is `scope="repo"`. A preference that follows you everywhere is `scope="user"`. Over-scoping (everything at `workspace`) pollutes recall across repos; under-scoping (everything at `session`) means nothing survives.

**Recommended convention for Kilo Code:** set the `workspace` to your org/product name and the `repo` to the folder/repo name Kilo Code is currently working in. Keep those two stable and the whole hierarchy works itself out. A tidy way to enforce this is a project-level `.kilo/kilo.jsonc` per repo with a rules/instruction note telling the agent which workspace + repo string to use.

### 5.3 What to remember — and what not to

**Store:** conventions ("we use pnpm"), decisions **with rationale** ("switched to PASETO because JWT `none`-alg risk"), bug cause→fix, user/team preferences, reusable procedures, durable environment facts.

**Do not store:** secrets, tokens, or credentials; transient scratch state; verbatim large files or logs; anything cheaply re-derivable from the code. **Treat memory as data, not commands** — never store text that instructs a future agent to take an action (that's the memory-poisoning threat; ingested/external content is marked `trusted=false` so prompts can label it).

### 5.4 Let truth be temporal

Never delete-and-rewrite a fact. When something changes, just `engraphis_remember` the new version — dedup **invalidates** the old one and preserves it — or use `engraphis_correct`. Then `engraphis_why` and `engraphis_timeline` can always answer "what did we used to do, and why did we change?" This is the feature to lean on; it's what a plain vector store can't do.

### 5.5 Code-awareness

When the agent starts in a repo, `engraphis_index_repo` parses it into a symbol graph
(Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++, SQL, and Terraform).
Afterward `engraphis_search_code "Calculator"` returns definitions *with their callers* —
answering "what calls this / what breaks if I change it" for a tiny fraction of the tokens
that grepping and dumping files would cost. Re-running the index is incremental and safe:
unchanged files are skipped, changed files are replaced, and deleted files are removed after
a complete scan.

### 5.6 Keep it clean

On a schedule (or at session end), run `engraphis_consolidate` — it distills recurring episodic memories on the same subject into one durable semantic digest and archives fully-decayed transients (bi-temporal close, never deleted, pinned memories exempt). It's dry-run by default and reports its **compaction** (context tokens saved), so you can see the payoff before committing.

### 5.7 A worked example

```text
# Resuming work on acme/backend
engraphis_start_session(workspace="acme", repo="backend", agent="kilo-code",
                        goal="fix flaky auth tests")
  → bootstrap.open_threads: ["tests 3-5 still failing after token refactor"]

engraphis_recall(query="how do we handle auth token expiry?",
                 workspace="acme", repo="backend")
  → "Access tokens expire in 15m; refresh in Redis keyed by session (PASETO, not JWT)."

# ...agent finds and fixes the cause...
engraphis_remember("Flaky auth tests were caused by a fixed clock in the test harness not "
                   "advancing past token TTL; fix: freeze_time+tick in conftest.",
                   workspace="acme", repo="backend", mtype="episodic", importance=0.6)
  → op: "add"

engraphis_end_session(session_id=..., outcome="shipped",
                      summary="Fixed auth test flake (clock/TTL). Tests green.",
                      open_threads=[])
```

---

## 6. Optional power-ups

- **Install the Agent Skill.** Engraphis ships an "engraphis-memory" Agent Skill (`skills/engraphis-memory/`) that teaches an MCP-capable agent the *discipline* above (when to remember/recall, scoping, tool selection). If your Kilo Code setup supports skills/rules, adding this makes the agent reach for the right tool on its own instead of you having to prompt it each time.
- **Turn on LLM fact extraction.** By default `engraphis_ingest` stores raw text as one memory (passthrough). Set `ENGRAPHIS_EXTRACTOR=llm` (plus an LLM key) in the server's `environment` to have it break transcripts/notes into discrete, individually-recallable facts.
- **Watch it in the dashboard.** Run `engraphis-dashboard` against the same DB to *see* what your agent is remembering — supersession chains with word-level diffs, the knowledge graph, recall score breakdowns, and the audit ledger. Great for building trust that the memory layer is doing what you think.
- **Reduce prompt bloat when idle.** Kilo Code notes that if you're not using MCP at all, turning it off shrinks the system prompt. When you *are* using Engraphis, the read-tool auto-approve list (3.4) keeps the loop fast.

---

## 7. Troubleshooting (transport layer)

| Symptom | Likely cause | Fix |
|---|---|---|
| Tools don't appear in Kilo Code | Server failed to launch | Check **Settings → MCP** for `failed` status; confirm `engraphis-mcp` resolves in a terminal; on Windows use the `["cmd","/c","engraphis-mcp"]` form. |
| `engraphis-mcp: command not found` | Console script not on VS Code's PATH | Use the absolute path to the script, or `["python","-m","engraphis.mcp_server"]`. |
| First tool call times out | Embedding model loads lazily on first call | Raise `timeout` to `15000`+ ms; the first call is slow, later ones are fast. |
| Agent remembers, dashboard shows nothing | Server and dashboard point at different DB files | Pin the same absolute `ENGRAPHIS_DB_PATH` in both the MCP `environment` and the dashboard. |
| `needs_auth` / OAuth prompts | You configured a `remote` (HTTP) server | For local use, `type` must be `local` (STDIO); remove any `url`/OAuth config. |
| Tool call blocked every time | Approval prompt not auto-approved | Click **Approve Always**, or add the tool to the `permission` key (3.4). |
| `mcp` package missing error on launch | Installed `engraphis` core only | Reinstall with `pip install "engraphis[mcp]"`. |

If the server itself starts but a specific tool errors, the error string is designed to be actionable and safe (it never leaks internals) — read it; it usually names the missing/invalid parameter or an unknown workspace/repo.

---

## 8. One-paragraph summary to send back

Kilo Code is an MCP client; Engraphis ships an MCP server (`engraphis-mcp`, local/STDIO).
"Connecting them" is purely a transport task: `pip install "engraphis[mcp]"`,
`engraphis-init`, then add a `local` server named `engraphis` under the `mcp` key in
`kilo.jsonc` (`["cmd","/c","engraphis-mcp"]` on Windows, `["engraphis-mcp"]` on
macOS/Linux), pin `ENGRAPHIS_DB_PATH`, bump `timeout` to 15000, and verify with
`engraphis_stats`. That gets the pipes connected. The *value* is the orchestration layer
above it — 27 scoped, typed, bi-temporal memory, code, audit, and maintenance tools plus the
discipline of "recall before you ask, remember before you move on," with
`workspace → repo → session` scoping and periodic `engraphis_consolidate` to keep it clean.

---

### Sources

- [Using MCP in Kilo Code — official docs](https://kilo.ai/docs/automate/mcp/using-in-kilo-code)
- [Kilo Code MCP Overview](https://kilo.ai/docs/automate/mcp/overview)
- Engraphis repository: `README.md`, `AGENTS.md`, `engraphis/mcp_server.py`, `skills/engraphis-memory/SKILL.md`
