# Engraphis — Launch copy (paste-ready)

Built from your own landing page + GTM positioning. Post the code publicly and get
`pip install engraphis` working **first** — every version below assumes a stranger can try it
in one command. Fill the two brackets (`REPO_URL`, `DEMO_URL`) and go.

---

## 1. Show HN (Hacker News)

**Title:**
`Show HN: Engraphis – local-first memory for AI coding agents (MCP, no cloud)`

**Body:**
```
Every new session, my coding agent started from zero — re-asking what package manager I use,
re-learning the codebase, forgetting past decisions. The hosted memory services fix this by
making you ship your context to their cloud and bill per memory. I wanted the opposite, so I
built Engraphis.

It's a local-first memory engine for agents. The core is 100% on your machine (SQLite + local
embeddings, numpy-only), Apache-2.0, no API key for memory, no per-token cost, no data leaving
your box. You bring your own LLM only for optional chat/synthesis.

The headline is a native MCP server — `claude mcp add engraphis -- engraphis-mcp` — so Claude
Code, Cursor, Cline, Zed, and Windsurf plug in and stop forgetting across sessions and repos.

What's actually different under the hood:
- Bi-temporal facts: contradictions get versioned (valid_from/valid_to), not clobbered — you
  can ask "why do we know this?" and "what did we believe as of last week?"
- Hybrid recall: vector + keyword + a knowledge graph (Personalized PageRank), fused and reranked.
- An Ebbinghaus forgetting curve so stale facts decay and reinforced ones stick.
- Code-aware: it indexes a repo into a function/class/call graph so "what calls this?" is cheap.

Try it in ~2 min:
  pip install engraphis
  claude mcp add engraphis -- engraphis-mcp

Repo: REPO_URL
60-sec demo: DEMO_URL

It's early (v0.1, beta) and I'd love feedback — especially on recall quality vs. mem0/Zep/Letta
and on what you'd need before trusting it with a real project's memory.
```
*Post Tue–Thu, ~8–10am ET. Reply to every comment for the first few hours — HN rewards presence.*

---

## 2. Reddit (r/LocalLLaMA, r/ClaudeAI, r/cursor)

**Title:**
`I built a local-first memory engine for coding agents — MCP server, runs on your machine, no per-token cost`

**Body:**
```
My agents kept forgetting everything between sessions, and I didn't want to send my private
codebase context to a hosted memory cloud that bills per memory. So I made Engraphis — a
self-hosted memory engine with a native MCP server.

- Local + private: SQLite + local embeddings, offline by default, Apache-2.0.
- Plugs into Claude Code / Cursor / Cline / Zed / Windsurf via MCP (`remember` / `recall`,
  scoped to workspace → repo → session).
- Actually remembers the right things: hybrid recall (vector + keyword + graph), an Ebbinghaus
  decay curve, and bi-temporal truth so contradictions are versioned instead of overwritten.
- There's a Memory Inspector UI where you can see exactly when/why a fact changed.

Install:
  pip install engraphis
  claude mcp add engraphis -- engraphis-mcp

Repo + 60s demo in the comments. It's v0.1 and I'm looking for honest feedback — what would you
need before you'd let it hold your project's memory?
```
*Put the links in the first comment, not the post body — several of these subs down-rank link posts.*

---

## 3. X / Twitter thread

```
1/ Your AI coding agent forgets everything between sessions.

Hosted memory tools "fix" this by making you ship your context to their cloud and billing you
per memory.

I built the opposite: Engraphis — local-first memory that runs on your machine. Free core, open
source. 🧵

2/ One command to give any MCP agent long-term memory:

  pip install engraphis
  claude mcp add engraphis -- engraphis-mcp

Works with Claude Code, Cursor, Cline, Zed, Windsurf. Your data never leaves your box.

3/ It's not just a vector store:
• bi-temporal facts (contradictions are versioned, not clobbered — ask "why do we know this?")
• hybrid recall: vector + keyword + knowledge graph
• an Ebbinghaus forgetting curve so stale facts fade, reinforced ones stick
• indexes your repo's call graph

4/ There's also a Memory Inspector UI: search, timeline, and the exact word-level diff of when a
fact changed and why.

60-second demo: DEMO_URL

5/ It's early (v0.1) and open source (Apache-2.0). Repo + docs: REPO_URL

If your agent keeps re-learning your codebase, I'd love for you to try it and tell me where recall
falls short.
```

---

## Notes
- Everything above is claims your README already backs up. Don't add benchmarks you haven't run
  honestly — "v0.1, looking for feedback" is a stronger and safer launch posture than big numbers.
- Pick **one** primary channel for launch day (Show HN is the highest-leverage for a dev tool).
  Post the others the same week, not the same hour.
- The single biggest predictor of a good launch here is the **60-second demo video**. Make that
  before you write another word.
