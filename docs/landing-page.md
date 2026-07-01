# Engraphis — Landing Page Copy (ready to use)

## Hero
**Your AI agents keep forgetting. Engraphis fixes that — on your machine.**

The local-first memory engine for AI agents. Durable, scoped, explainable memory that persists
across sessions and repositories. Free and open-source at the core. Your data never leaves your box.

[ Get started ]   [ Add to Claude Code ]   [ Star on GitHub ]

> `claude mcp add engraphis -- engraphis-mcp`

## The problem
Every new session, your coding agent starts from zero. It re-asks what package manager you use,
re-learns the codebase, forgets why you chose PASETO over JWT. Hosted memory services solve this —
by making you ship your private context to their cloud and bill you per memory.

## The Engraphis way
- **Local-first.** Runs on your machine. No API key for memory, no per-token cost, no data exfiltration.
- **Agent-native.** A built-in MCP server means Claude Code, Cursor, Cline, Zed, and Windsurf plug
  in directly — `remember` and `recall`, scoped to workspace, repo, and session.
- **It actually remembers the right things.** Hybrid recall (vector + keyword + knowledge graph),
  an Ebbinghaus forgetting curve so stale facts fade and reinforced ones stick, and bi-temporal
  truth so contradictions are versioned, not clobbered.
- **Explainable.** Every memory carries provenance and a retention score. Ask *why* something is known.

## How it works (30 seconds)
1. `pip install -e ".[mcp]"` and register the MCP server with your agent.
2. Your agent calls `engraphis_remember` when it learns something worth keeping.
3. Next session — even after a restart — `engraphis_recall` brings it back, scoped to the repo.

## Why not a hosted memory cloud?
| | Hosted memory clouds | **Engraphis** |
|---|---|---|
| Where your context lives | Their servers | **Your machine** |
| Pricing | Per memory / per retrieval | **Free core, flat self-hosted** |
| Coding-agent integration | SDK glue | **Native MCP server** |
| Offline / air-gapped | No | **Yes** |
| Self-hosting | Limited or deprecated | **First-class** |

## Pricing
- **Community — Free (Apache-2.0).** Full engine, MCP server, CLI, library. Self-hosted.
- **Pro — ~$20/seat/mo.** Memory Inspector UI, team memory sync, priority support.
- **Team — from ~$100/mo.** SSO/RBAC, audit exports, scale adapters, SLA.
- **Enterprise — custom.** BYOC, encryption-at-rest, security review, dedicated support.

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
**Give your agents a memory.** [ Install Engraphis ] · [ Read the docs ] · [ Star on GitHub ]
