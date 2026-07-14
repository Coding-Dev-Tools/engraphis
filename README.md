# Engraphis

[![Version](https://img.shields.io/badge/version-0.9.0-blue.svg)](https://github.com/Coding-Dev-Tools/engraphis)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](https://github.com/Coding-Dev-Tools/engraphis/blob/main/LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-yellow?style=for-the-badge&logo=buy-me-a-coffee)](https://buymeacoffee.com/Jaixii)

https://engraphis.com/

https://discord.com/invite/Wfr2ejBmY

**Give your AI agents a memory. See it, search it, and watch it self-maintain — all in a beautiful WebUI on your own machine.**

<br>

<p align="center">
  <img src="docs/images/knowledge-graph.png" alt="Engraphis Knowledge Graph tab — force-directed entity-relation network" width="100%">
  <br>
  <sup>Knowledge Graph · run <code>engraphis-dashboard</code> to see it live</sup>
</p>

<br>

---

## The WebUI — one command, local-first

```bash
pip install "engraphis[server]"
engraphis-dashboard
```

Opens `http://127.0.0.1:8700` in your browser. No cloud, no signup, no API key for memory.
Everything lives in a single SQLite file on your machine.

**You'll see the full product** — a dark-themed (with multiple theme options in left sidebar), sidebar-navigated dashboard with 12 tabs:

| Tab | What you see |
|-----|-------------|
| **Overview** | Live memory counts, memory-type mix, and a health summary at a glance |
| **Analytics** *(Pro)* | Growth, retention distribution, decay forecast, resolver mix, and top entities — plus a one-click shareable HTML report and a cross-workspace portfolio view |
| **Recall** | Hybrid search across the memory bank — each result shows its score breakdown (retention, semantic, lexical, graph, importance, recency) |
| **Memories** | Browse and curate every memory by workspace — click into a full reader with type and retention pills, drag-to-reorder, inline title/type edits |
| **Proactive** | "What should I know right now" — importance × recency × retention, plus the last session handoff |
| **Why** | The current answer to a question, and the facts it superseded |
| **Timeline** | Bi-temporal history of a topic — what was believed, and when |
| **Audit** | Full governance ledger — who did what, when, and why |
| **Knowledge Graph** | Interactive force-directed graph of entities and their relationships — click any node to see every linked memory |
| **Consolidate** | Run a consolidation sweep on demand — see what got distilled and what got pruned |
| **Automation** *(Pro)* | Scheduled consolidation + retention policies that keep the store clean on autopilot (dashboard config, plus `scripts/auto_maintain` for cron / Task Scheduler) |
| **Workspaces** | Create, rename, describe, copy, merge, and delete workspaces; import files & folders; drag-and-drop upload |
| **Team** | Multi-user access with PBKDF2 logins, password reset, admin / member / viewer roles, seat management, and team audit log (Team) |
| **Settings** | License activation (Pro/Team), cloud sync, appearance, and engine/store info |

The dashboard is powered by the v2 engine — the same `MemoryService` that backs the MCP server
and the Python library. What you see in the UI is what your agents get.

### Start it on every platform

| Platform | How |
|----------|-----|
| **Windows** | Double-click **Engraphis Dashboard** on your Desktop or Start Menu (install: `engraphis-dashboard --install-shortcuts`) |
| **macOS** | Double-click **Engraphis Dashboard.app** on your Desktop (install: same command) |
| **Linux** | Desktop entry in Applications → Development (GNOME/KDE/etc.) |
| **Docker** | `docker compose up` — see `docker-compose.yml` for the one-command deployment |
| **Any** | `engraphis-dashboard` in a terminal |

### Accessibility-first inspection, built in

The dashboard has the focused memory-inspection view built in — no separate app or port:

- Open any memory to see its **supersession chain with word-level diffs** — exactly when a fact changed and why
- **Offline knowledge graph** (vendored renderer — no CDN, works air-gapped)
- Score breakdowns on every recall, Why/Timeline/link browsing, proactive recall, consolidation, audit trail
- Keyboard-navigable, ARIA-annotated, light/dark mode

> The standalone Inspector (`:8710`) was retired 2026-07-10 and folded into the one dashboard on `:8700`.

---

## What's under the UI

Your agents forget everything between sessions. Engraphis fixes that — on your machine. Every new
session, your coding agent starts from zero: re-asking which package manager you use, re-learning
the codebase, forgetting why you chose PASETO over JWT. Engraphis gives agents durable, scoped,
*explainable* memory.

Under the hood: Ebbinghaus forgetting-curve decay, interaction-aware reinforcement, bi-temporal
facts, and hybrid (vector + lexical + graph) recall. The engine is 100% local: SQLite + local
embeddings. You bring the LLM only for optional chat/synthesis.

- **Local-first & private** — runs offline; the core depends only on `numpy`.
- **MCP-native** — 18 tools for Claude Code, Cursor, Cline, Zed, Windsurf.
- **Self-maintaining facts** — writes are deterministically conflict-resolved (no LLM required).
- **Principled recall** — six-term score over retention, semantic, lexical, graph, importance, recency.
- **Bi-temporal truth** — contradictions invalidate instead of overwriting (`engraphis_why` / `engraphis_timeline`).
- **Grounded, not guessed** — cited answers or explicit abstain; provenance on every memory.
- **Code-aware** — AST-powered symbol graph: `engraphis_index_repo` → `engraphis_search_code`.
- **Sleep-time consolidation** — scheduled job distills recurring episodes, reports its compaction.
- **Scoped** — `workspace → repo → session` hierarchy.
- **Encryption at rest** — optional SQLCipher (AES-256) whole-database encryption via `ENGRAPHIS_DB_KEY`. No plaintext fallback when a key is set.
- **Cloud sync** — cross-device and cross-team memory sync with deterministic CRDT merge (folder transport for self-hosting, managed relay for zero-setup). One-click "Sync now" or automatic cadence in the dashboard.
- **Import & ingest** — drag-and-drop file upload, server-side folder import, and LLM-powered fact extraction from raw text.

---

## Why it wins

| Axis | mem0 | Zep | Engraphis |
|---|---|---|---|
| Product WebUI (local, no cloud) | ✗ | ✗ | **✓ (dashboard with built-in inspector)** |
| Open & self-hostable engine | ✓ | partial | **✓ fully open, local-first** |
| Forgetting/decay | partial | ✗ | **✓** |
| Bi-temporal graph | partial | ✓ | **✓** |
| Native multi-repo model | ✗ | ✗ | **✓ (unique)** |
| Code-aware (AST/symbol graph) | ✗ | ✗ | **✓ (unique)** |
| Cloud sync (CRDT merge) | ✗ | ✗ | **✓ (deterministic, no conflict copies)** |
| Encryption at rest | ✗ | ✗ | **✓ (SQLCipher)** |
| MCP-native for coding agents | ✓ | ✗ | **✓** |

---

## Install

```bash
pip install "engraphis[all]"        # dashboard + MCP server + code graph + encryption + everything
pip install "engraphis[server]"     # dashboard + REST API
pip install "engraphis[mcp]"        # MCP server only
pip install "engraphis[encryption]" # SQLCipher encryption-at-rest extra
pip install engraphis               # core library — numpy only, fully offline
```

> **Linux / macOS:** if `pip install` fails with `error: externally-managed-environment`,
> your system Python is marked read-only (PEP 668). Install into a virtual environment
> instead — `python3 -m venv venv && source venv/bin/activate && pip install "engraphis[server]"`
> — or use Docker (`docker compose up`). `pipx install "engraphis[server]"` also works.

> First run downloads `all-MiniLM-L6-v2` (~80 MB). Without it, the engine falls back
> to a deterministic offline embedder so it always runs.

---

## Quickstart — dashboard (the headline)

```bash
pip install "engraphis[server]"
engraphis-dashboard                   # → http://127.0.0.1:8700
engraphis-dashboard --install-shortcuts   # → Desktop + Start Menu icons
```

### Docker

```bash
docker compose up                     # → http://127.0.0.1:8700
```

The default entrypoint is `engraphis-dashboard --no-open`. Set `ENGRAPHIS_API_TOKEN` to require
authentication, `ENGRAPHIS_DB_KEY` to encrypt the database at rest, and `ENGRAPHIS_LICENSE_KEY`
to unlock Pro/Team features. See `docker-compose.yml` for all options.

---

## Quickstart — MCP server (for coding agents)

```bash
pip install "engraphis[mcp]"
engraphis-init                     # writes .env + prints config snippets
claude mcp add engraphis -- engraphis-mcp
```

Your agent now has 18 tools — remember, recall (grounded + proactive), why, timeline,
forget, pin, correct, ingest, consolidate, index_repo, search_code, link, record_event,
start/end_session, stats. See the [MCP tools table](#mcp-tools) below.

---

## Quickstart — Python library

```python
from engraphis.service import MemoryService

mem = MemoryService.create("engraphis.db")
mem.remember("Auth migrated from JWT to PASETO.", workspace="acme", repo="api")
hit = mem.recall("why did we change auth?", workspace="acme", repo="api")
print(hit["context"])
```

The same `MemoryService` backs the dashboard and the MCP server.

---

## Free forever vs. Pro

The engine, dashboard, MCP server, and governance tools are free and Apache-2.0,
permanently. A license key unlocks the paid layer — **verified offline** (no phone-home)
for self-hosted keys, or **cloud-enforced** (machine-bound lease, revocable) for
commercial deployments. **Pro is $10/mo ($100/yr), Team is $20/seat/mo ($200/seat/yr)** —
and you can unlock every Pro feature with a **3-day free trial right in the dashboard**
(Settings → License), no key and no card.

| | Free (available now) | Pro — $10/mo or $100/yr | Team — $20/seat/mo or $200/seat/yr |
|---|---|---|---|
| Dashboard WebUI (with built-in inspector) | ✓ | ✓ | ✓ |
| Memory engine + 18 MCP tools | ✓ | ✓ | ✓ |
| Version-chain diffs, offline knowledge graph | ✓ | ✓ | ✓ |
| Cloud sync (folder + managed relay) | | ✓ | ✓ |
| Auto-sync (hands-off cadence) | | ✓ | ✓ |
| Analytics: growth, retention, decay forecast + entities | | ✓ | ✓ |
| Analytics HTML report (self-contained, shareable) | | ✓ | ✓ |
| Automated maintenance: scheduled consolidation + retention policies | | ✓ | ✓ |
| Signed compliance export (checksummed bi-temporal bundle) | | ✓ | ✓ |
| Priority support | | ✓ | ✓ |
| Multi-user dashboard: logins, roles, seat management | | | ✓ |
| Team audit log + CSV export | | | ✓ |
| Team invite emails (vendor relay, zero email setup) | | | ✓ |

---

## MCP tools

| Category | Tool | What it does |
|---|---|---|
| Write | `engraphis_remember` | Store a fact; deterministically resolved (add/reinforce/supersede) |
| Write | `engraphis_record_event` | Append a lightweight episodic log entry |
| Write | `engraphis_link` | Explicitly connect two related memories |
| Write | `engraphis_ingest` | Store raw text; Engraphis extracts the discrete facts worth keeping |
| Write | `engraphis_consolidate` | Run one sleep-time consolidation sweep: distill recurring episodes |
| Read | `engraphis_recall` | Hybrid vector + lexical + graph recall |
| Read | `engraphis_recall_grounded` | Cited answer from retrieved memories — or abstain |
| Read | `engraphis_recall_proactive` | "What should I know right now" — no query needed |
| Read | `engraphis_why` | Current answer + what it superseded |
| Read | `engraphis_timeline` | Full bi-temporal history, oldest first |
| Code | `engraphis_index_repo` | Parse a repo into the code symbol graph |
| Code | `engraphis_search_code` | Find symbols by name, with callers |
| Governance | `engraphis_forget` | Retire a memory — bi-temporal close, never deleted |
| Governance | `engraphis_pin` | Exempt from future automatic decay/pruning |
| Governance | `engraphis_correct` | Replace content without losing history |
| Session | `engraphis_start_session` / `engraphis_end_session` | Session lifecycle with cross-session handoff |
| Ops | `engraphis_stats` | Memory counts for health checks |

---

## Cloud sync

Cloud sync keeps your memory store consistent across all your machines — and, on the Team
tier, across a group — without giving up local-first ownership. It ships two transports:

- **Folder transport** — any shared directory (Dropbox, iCloud, Syncthing, a git repo, a
  mounted drive). Zero infrastructure.
- **Managed relay** — HTTPS against the Engraphis relay, authenticated by your license key.
  One-click in the dashboard or `python -m scripts.sync --relay`.

Sync is a **state-based CRDT**: deterministic merge, no conflict copies, no data loss.
Every field resolves by a commutative, idempotent rule so `merge(A, B) == merge(B, A)`.
See [`docs/SYNC.md`](docs/SYNC.md) for architecture, security model, and CLI usage.

---

## Encryption at rest

Set `ENGRAPHIS_DB_KEY` (or `ENGRAPHIS_DB_KEY_FILE`) and install the extra:

```bash
pip install "engraphis[encryption]"
```

The entire database file is transparently encrypted with AES-256 via SQLCipher — full-text
search, the graph, and every query keep working unchanged. When a key is set, Engraphis
**fails loud** rather than silently falling back to plaintext. Generate a strong key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

> An existing plaintext database cannot be opened with a key — migrate it (dump → import
> into a fresh keyed DB). See `.env.example` for all encryption options.

---

## Import files & folders

Drag-and-drop or server-side import, both member-gated and bounded:

- **Dashboard upload** — the Workspaces tab's "Import files & folders" section accepts files
  directly from the browser.
- **Server-side folder import** — `MemoryService.import_folder()` reads a directory on the
  machine running Engraphis, one memory per file, with path-traversal guards.
- **MCP ingest** — `engraphis_ingest` accepts raw text and extracts discrete facts
  (when `ENGRAPHIS_EXTRACTOR=llm` is configured; otherwise stores verbatim).

All imported memories are marked **untrusted** by default.

---

## Configuration

All via environment (or `.env`):

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_DB_PATH` | `./engraphis.db` | SQLite database file |
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Dashboard port |
| `ENGRAPHIS_API_TOKEN` | — | If set, REST API requires `Authorization: Bearer <token>` |
| `ENGRAPHIS_DB_KEY` | — | Encrypt the database at rest (SQLCipher). Or use `ENGRAPHIS_DB_KEY_FILE` |
| `ENGRAPHIS_EMBED_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `ENGRAPHIS_EXTRACTOR` | `none` | `none` = store verbatim; `llm` = extract facts via LLM before storing |
| `ENGRAPHIS_GRAPH_EXTRACTOR` | `regex` | `regex` = dependency-free NER (offline); `none` = disable graph population |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | `openai \| anthropic \| google \| openrouter \| custom` |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | Model name (provider-specific) |
| `ENGRAPHIS_LLM_API_KEY` | — | LLM API key (only for chat/synthesis and `extractor=llm`) |
| `ENGRAPHIS_LLM_BASE_URL` | — | Base URL for openrouter / custom OpenAI-compatible endpoints |
| `ENGRAPHIS_LICENSE_KEY` | — | Pro/Team key (or `~/.engraphis/license.key`) |
| `ENGRAPHIS_TEAM_MODE` | `1` | Team mode is ON by default (per-user logins + roles). Set `0` to disable |
| `ENGRAPHIS_LOOP_INTERVAL` | `60` | Background consolidation loop interval in seconds (0 = disabled) |
| `ENGRAPHIS_DECAY_HALFLIFE_DAYS` | `7` | Ebbinghaus decay half-life (higher = memories persist longer) |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted reverse-proxy IPs for TLS termination (`*` = trust all) |
| `ENGRAPHIS_RELAY_URL` | built-in | Managed sync relay URL (Pro/Team) |
| `ENGRAPHIS_AUTOSYNC_LOOP` | `1` | Kill switch for the in-process auto-sync loop (0 = off) |

See `.env.example` for the full list including commercial/vendor, email delivery, and
cloud-license enforcement options.

---

## Project structure

```
engraphis/
├── engraphis/
│   ├── core/                # v2 engine — interfaces, store, recall, scoring, schema, sync
│   ├── backends/            # pluggable embedder / vector index / reranker / codegraph / sync transports / encryption
│   ├── service.py           # validated MemoryService facade
│   ├── mcp_server.py        # MCP server — 18 tools
│   ├── dashboard_app.py     # dashboard WebUI (FastAPI)
│   ├── autosync.py          # background auto-sync loop (Pro/Team)
│   ├── licensing.py         # license verification (offline + cloud)
│   ├── analytics.py         # Pro analytics engine
│   ├── automation.py        # scheduled maintenance policies (Pro)
│   ├── billing.py           # Polar webhook fulfillment
│   ├── config.py / app.py   # env settings / REST server
│   └── static/              # dashboard frontend
├── eval/                    # offline retrieval eval harness + datasets
├── tests/                   # pytest suite (300+ tests, offline numpy-only core)
├── scripts/                 # start_dashboard, inspector, cli, init, consolidate, sync
├── docs/                    # SYNC.md, KILO_CODE_INTEGRATION.md
├── Dockerfile / docker-compose.yml
└── pyproject.toml
```

---

## Development

The offline quality gate (no network, no API key):

```bash
pip install numpy pytest ruff
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.ablation
ruff check .
```

Numbers, not assertions: the offline harness is a **correctness floor** (deterministic embedder).
LoCoMo / LongMemEval competitive numbers run separately with a real embedder — see
[`BENCHMARKS.md`](BENCHMARKS.md).

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). "Engraphis" is a trademark of the
Engraphis project; the license does not grant trademark rights.
