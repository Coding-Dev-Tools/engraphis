# Engraphis — Landing Page Copy (ready to use)

## Hero
**Your AI agents keep forgetting. Engraphis fixes that — on your machine.**
**Now with a beautiful WebUI — see everything your agents remember.**

The local-first memory engine for AI agents. Durable, scoped, explainable memory that persists
across sessions and repositories. A full-featured dashboard WebUI to browse, search, and manage
your knowledge graph — all running on your own machine. Free and open-source at the core.

[ Get started ]   [ See the WebUI ]   [ Star on GitHub ]

> `pip install "engraphis[server]" && engraphis-dashboard` — opens the dashboard in your browser

## The WebUI
One command opens the full product. 11 tabs. No cloud. No signup.

- **Overview** — memory counts, retention distribution, weekly growth, decay forecast
- **Memories** — browse every memory by workspace, sorted by retention
- **Recall** — search across the entire bank with score breakdowns
- **Knowledge Graph** — interactive D3 force-directed graph of entities and relationships
- **Consolidate** — run consolidation sweeps on demand, see what got distilled
- **Chat** — ask questions against your memory; the LLM answers grounded in context
- **Import** — drag & drop or paste markdown files
- Plus: Timeline, Audit, Settings, and Vaults management

**Desktop shortcut?** Run `engraphis-dashboard --install-shortcuts` — one click, Desktop icon,
Start Menu entry. Windows, macOS, Linux.

Also includes the **Memory Inspector** (`engraphis-inspector`, :8710) — a focused view with
supersession-chain diffs (see exactly when a fact changed and why).

## The problem
Every new session, your coding agent starts from zero. It re-asks what package manager you use,
re-learns the codebase, forgets why you chose PASETO over JWT. Hosted memory services solve this —
by making you ship your private context to their cloud and bill you per memory.

## The Engraphis way
- **Local-first.** Runs on your machine. No API key for memory, no per-token cost, no data exfiltration.
- **Product WebUI.** Dashboard and Inspector — browse, search, manage your knowledge graph in a
  beautiful dark-themed UI. No other memory engine ships a local product UI.
- **Agent-native.** A built-in MCP server means Claude Code, Cursor, Cline, Zed, and Windsurf plug
  in directly — `remember` and `recall`, scoped to workspace, repo, and session.
- **It actually remembers the right things.** Hybrid recall (vector + keyword + knowledge graph),
  an Ebbinghaus forgetting curve so stale facts fade and reinforced ones stick, and bi-temporal
  truth so contradictions are versioned, not clobbered.
- **Raw in, structured memory out.** Point the ingest pipeline at transcripts, notes, or docs and
  get back deduplicated, linked, typed facts — near-duplicates reinforce instead of cloning, stale
  facts get superseded, and every new fact wires itself into the graph.
- **Grounded, not guessed.** Every memory carries provenance and a retention score, so answers cite
  *why* something is known instead of hallucinating it.

## How it works (30 seconds)
1. `pip install "engraphis[all]"` — one install gets the dashboard, MCP server, and engine.
2. Your agent calls `engraphis_remember` when it learns something worth keeping.
3. Next session — even after a restart — `engraphis_recall` brings it back, scoped to the repo.
4. Open the dashboard anytime to browse, search, and audit what your agents know.

## Why not a hosted memory cloud?
| | Hosted memory clouds | **Engraphis** |
|---|---|---|
| Where your context lives | Their servers | **Your machine** |
| Product WebUI | Cloud-only (if any) | **Local dashboard + Inspector** |
| Pricing | Per memory / per retrieval | **Free core, flat self-hosted** |
| Coding-agent integration | SDK glue | **Native MCP server** |
| Offline / air-gapped | No | **Yes** |
| Self-hosting | Limited or deprecated | **First-class** |

## Pricing

> **The paid tiers below are coming soon — nothing is for sale yet.** The free Community
tier is available today. Pro/Team/Enterprise are on the roadmap; the gating code exists and is
exercised in tests, but license keys are **not sold** (no checkout is live yet). Prices are
targets, not live. Do not pay anyone for Engraphis today.

A hosted memory cloud meters you per memory and per retrieval, and keeps your context on its
servers. Engraphis is a flat per-seat license for software that runs on your box — the core is free
forever and you never pay by the lookup.

- **Community — Free, available now (Apache-2.0).** Full engine, MCP server, dashboard, Inspector,
  CLI, library. Self-hosted. Never feature-limited — you get the whole recall engine, not a crippled
  core.
- **Pro — coming soon (target ~$20/seat/mo).** Memory Inspector UI, team memory sync, priority support.
- **Team — coming soon (target from ~$100/mo).** SSO/RBAC, audit exports, scale adapters, SLA.
- **Enterprise — coming soon (custom).** BYOC, encryption-at-rest, security review, dedicated support.

## FAQ
**Do I need an API key?** Not for memory — that's 100% local. Only optional chat/synthesis uses
your chosen LLM.
**Which agents work?** Anything that speaks MCP: Claude Code, Cursor, Cline, Zed, Windsurf — plus
a REST API and Python library for everything else.
**Is my data private?** Yes. It stays in a local SQLite database. Nothing is sent anywhere unless
you enable LLM features.
**Can I run it in production?** Yes — Docker image, optional bearer-token auth, loopback-by-default
networking. See SECURITY.md.

## Call to action
**Give your agents a memory.** [ Install Engraphis ] · [ Start the WebUI ] · [ Star on GitHub ]
