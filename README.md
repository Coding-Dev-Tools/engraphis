# Engraphis

[![Version](https://img.shields.io/badge/version-0.9.6-blue.svg)](https://github.com/Coding-Dev-Tools/engraphis)
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

>Open-Source users: Remember to Update regularly! Improvements and fixes twice a day. Invite your friends!
>
> **Beta:** the **Team** layer (multi-user dashboard, seats, roles, audit log, team invite
> emails, cloud sync relay) is **early-access beta** — expect rough edges and breaking
> changes before it stabilizes. The single-user engine, dashboard, and MCP server are stable.

## The WebUI — one command, local-first

```bash
pip install "engraphis[server]"
engraphis-dashboard
```

Opens `http://127.0.0.1:8700` in your browser. No cloud, no signup, no API key for memory.
Everything lives in a single SQLite file on your machine.

**You'll see the full product** — a dark-themed (with multiple theme options in left sidebar), sidebar-navigated dashboard with 14 tabs:

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
| **Automation** *(Pro)* | Scheduled consolidation + retention policies on autopilot — plus **auto-dreaming**: a background consolidation + cross-cluster inference loop that fires when the store has accumulated enough new memories *and* gone idle. Configurable from the dashboard (cadence, dream trigger, idle threshold, inference toggle) or the `GET/POST /api/automation` API, and via `scripts/auto_maintain` for cron / Task Scheduler |
| **Workspaces** | Create, rename, describe, copy, merge, and delete workspaces; import files & folders; drag-and-drop upload |
| **Team** *(beta)* | Multi-user access with PBKDF2 logins, password reset, admin / member / viewer roles, seat management, and team audit log (Team) — **early-access beta** |
| **Settings** | License activation (Pro/Team), cloud sync, LLM provider setup/test, Agent Connect token management, appearance, and engine/store info |

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
embeddings. You bring an LLM only for optional chat, synthesis, structured extraction,
or structured consolidation.

- **Local-first & private** — runs offline; the core depends only on `numpy`.
- **MCP-native** — 27 tools for Claude Code, Cursor, Cline, Zed, Windsurf.
- **Self-maintaining facts** — writes are deterministically conflict-resolved (no LLM required).
- **Principled recall** — six-term score over retention, semantic, lexical, graph, importance, recency.
- **Bi-temporal truth** — contradictions invalidate instead of overwriting (`engraphis_why` / `engraphis_timeline`).
- **Grounded, not guessed** — cited answers or explicit abstain; provenance on every memory.
- **Task-ready context** — bounded proactive packets combine task/agent state, cited memories, suggested follow-ups, and the last-session handoff; optional LLM prose is accepted only when its citations validate.
- **Composable intelligence** — opt-in deterministic conflict triage (`duplicate` / `refinement` / `contradiction` / `obsolete`) and `UserModel` recall reranking helpers; neither changes default recall unless called.
- **Code-aware** — incremental multi-language symbol/call/import graph, code↔memory links,
  path queries, communities/hotspots, git/PR impact analysis, and portable graph exports.
- **Sleep-time consolidation** — scheduled job distills recurring episodes, reports its compaction.
- **Scoped** — `workspace → repo → session` hierarchy.
- **Encryption at rest** — optional SQLCipher (AES-256) whole-database encryption via `ENGRAPHIS_DB_KEY`. No plaintext fallback when a key is set.
- **Cloud sync** — cross-device and cross-team memory sync with deterministic CRDT merge (folder transport for self-hosting, managed relay for zero-setup). One-click "Sync now" or automatic cadence in the dashboard.
- **Import & ingest** — local documents/code/DOCX plus optional PDF OCR, image OCR,
  audio/video transcription, and live PostgreSQL schema introspection.

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

## Host on Railway (team, no install for members)

The dashboard ships as a Docker image that defaults to the v2 **team** dashboard
(multi-user logins, roles, seats, cloud-license revocation). Deploy one instance for
your team; members sign in at your URL and connect their agents over HTTP/MCP — they
never install Engraphis locally. See [`docs/HOSTING_RAILWAY.md`](docs/HOSTING_RAILWAY.md)
for the 5-minute guide (volume, custom domain, bootstrap the Team entitlement, create the
first admin, invite members, and connect agents).

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new?template=https://raw.githubusercontent.com/Coding-Dev-Tools/engraphis/main/railway.json)

> The button provisions a service from this repo's Dockerfile. After it builds, you add
> a persistent `/data` volume (so activated keys + memories survive redeploys) and set
> `ENGRAPHIS_FORWARDED_ALLOW_IPS=*` — both one-click steps in the Railway dashboard;
> full walk-through in the hosting guide.

## Install

```bash
pip install "engraphis[all]"        # dashboard + MCP server + code graph + available platform extras
pip install "engraphis[server]"     # dashboard + REST API
pip install "engraphis[mcp]"        # MCP server only
pip install "engraphis[documents]"  # PDF + image OCR bindings
pip install "engraphis[transcription]" # faster-whisper audio/video
pip install "engraphis[postgres]"   # PostgreSQL schema introspection
pip install "engraphis[encryption]" # SQLCipher encryption-at-rest extra
pip install engraphis               # core library — numpy only, fully offline
```

The core library supports Python 3.9+. The upstream MCP SDK requires Python 3.10+, so
use Python 3.10 or newer for the `mcp` or `all` installation paths.
`sqlcipher3-binary` currently publishes Linux wheels; on Windows, `all` installs without
that optional driver and `engraphis[encryption]` requires a compatible SQLCipher build.

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

Your agent now has 27 tools — remember, recall (grounded + proactive), proactive context,
grounded answer alias, why, timeline, forget, pin, correct, ingest, consolidate, index_repo,
search/code path/impact/export, privacy receipts, PostgreSQL schema ingestion, link,
record_event, start/end_session, and stats. See the [MCP tools table](#mcp-tools) below.

## Quickstart — repository graph

```bash
pip install "engraphis[code]"
engraphis-graph index -w acme -r api --root .
engraphis-graph query -w acme -r api "where is token rotation implemented?"
engraphis-graph explain -w acme -r api "why does deploy depend on approval?"
engraphis-graph path -w acme -r api UserService DatabasePool
engraphis-graph impact -w acme -r api --root . --git-range origin/main...HEAD
engraphis-graph export -w acme -r api -o engraphis-graph-out
```

The export contains `graph.json`, a self-contained `graph.html`, and `GRAPH_REPORT.md`.
See [the v3 architecture/design document](docs/ARCHITECTURE_V3.md).

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

The core engine, single-user dashboard, standalone MCP server, and governance tools are
free and Apache-2.0, permanently. Paid Pro/Team keys are **server-authoritative**: the
vendor signature is checked locally, then the key must hold a current machine-bound lease
from the configured/vendor relay. Revoked, expired, or seat-exceeded keys fail closed;
an unexpired lease provides bounded grace for transient network failures. **Pro is $10/mo
($100/yr), Team is $20/seat/mo ($200/seat/yr)**, and the dashboard offers a **3-day
server-issued Pro or Team trial** after email confirmation — no card required.

> **Team is early-access beta.** Multi-user logins, seats, roles, the team audit log,
> team invite emails, and the cloud-sync relay are all in active development — expect
> rough edges and breaking changes. Pro (single-user paid features) is stable. Free is
> stable.

| | Free (available now) | Pro — $10/mo or $100/yr | Team — $20/seat/mo or $200/seat/yr |
|---|---|---|---|
| Dashboard WebUI (with built-in inspector) | ✓ | ✓ | ✓ |
| Memory engine + 27 MCP tools | ✓ | ✓ | ✓ |
| Version-chain diffs, offline knowledge graph | ✓ | ✓ | ✓ |
| Cloud sync (folder + managed relay) | | ✓ | ✓ |
| Auto-sync (hands-off cadence) | | ✓ | ✓ |
| Analytics: growth, retention, decay forecast + entities | | ✓ | ✓ |
| Analytics HTML report (self-contained, shareable) | | ✓ | ✓ |
| Automated maintenance: scheduled consolidation + retention policies + **auto-dreaming** | | ✓ | ✓ |
| Signed compliance export (checksummed bi-temporal bundle) | | ✓ | ✓ |
| Priority support | | ✓ | ✓ |
| Multi-user dashboard: logins, roles, seat management *(beta)* | | | ✓ |
| Team audit log + CSV export *(beta)* | | | ✓ |
| Team invite emails (vendor relay, zero email setup) *(beta)* | | | ✓ |

---

## MCP tools

| Category | Tool | What it does |
|---|---|---|
| Write | `engraphis_remember` | Store a fact; deterministically resolved (add/reinforce/supersede) |
| Write | `engraphis_record_event` | Append a lightweight episodic log entry |
| Write | `engraphis_link` | Explicitly connect two related memories |
| Write | `engraphis_ingest` | Apply the configured extractor (`chunk`, `llm`, or `llm_structured`); `none` stores one verbatim memory |
| Write | `engraphis_ingest_postgres_schema` | Introspect a live PostgreSQL catalog into memory + typed graph nodes; DSN is never stored |
| Write | `engraphis_consolidate` | Run a sleep-time sweep; optionally build entity profiles or schema-validated LLM facts |
| Read | `engraphis_recall` | Hybrid vector + lexical + graph recall |
| Read | `engraphis_recall_grounded` | Cited answer from retrieved memories — or abstain |
| Read | `engraphis_answer` | Backward-compatible grounded-answer alias |
| Read | `engraphis_recall_proactive` | "What should I know right now" — no query needed |
| Read | `engraphis_proactive_context` | Task-aware context packet with cited memories and session handoff |
| Read | `engraphis_why` | Current answer + what it superseded |
| Read | `engraphis_timeline` | Full bi-temporal history, oldest first |
| Code | `engraphis_index_repo` | Incrementally parse a repo into the code/memory graph |
| Code | `engraphis_search_code` | Find symbols by name, callers, and linked memories |
| Code | `engraphis_code_path` | Shortest path across definitions, calls, imports, and memories |
| Code | `engraphis_code_impact` | Rank changed files by symbols, dependents, communities, memories, and hotspots |
| Code | `engraphis_export_code_graph` | Portable graph JSON + Markdown + HTML report |
| Audit | `engraphis_receipts` | List content-free hashed operation receipts |
| Audit | `engraphis_verify_receipts` | Verify the receipt chain, local tail anchor, and optional externally saved head/count |
| Audit | `engraphis_export_receipts` | Export the shareable receipt-only audit bundle |
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

Drag-and-drop or server-side import, role-gated and bounded:

- **Dashboard upload** — accepts text, Markdown, code, JSON/CSV/HTML, DOCX, and exported
  Google Workspace documents directly; optional adapters add PDF text extraction, image OCR,
  and audio/video transcription. Native `.gdoc` pointer files contain no document body, so
  export them as DOCX, PDF, HTML, or plain text before local ingestion.
- **Server-side folder import** — `MemoryService.import_folder()` reads a directory on the
  machine running Engraphis. Large resources are chunked deterministically even when the
  configured extractor is `none`; path-traversal guards still apply.
- **PostgreSQL** — `engraphis_ingest_postgres_schema`, `POST /api/resources/postgres`, or
  `engraphis-graph postgres` converts tables, columns, constraints, and foreign keys into a
  schema memory and entity graph. The DSN is never persisted.
- **MCP ingest** — `engraphis_ingest` accepts raw text and applies the configured extractor
  (`chunk`, `llm`, or `llm_structured`); with `none` it stores one verbatim memory.
- **Sub-file chunking** — set `ENGRAPHIS_EXTRACTOR=chunk` to split long, multi-topic
  documents into retrieval-sized, structure-aware pieces (headings start new chunks;
  ~256-token target with sentence-level overlap) *without an LLM*. Each chunk becomes
  its own memory, so recall returns the relevant **passage** instead of a whole file —
  a big context-reduction win on long docs. Works across all three ingest paths
  (dashboard upload, `import_folder`, and `engraphis_ingest`). Measure the payoff with
  the bundled eval: `python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5`
  (whole-file vs. chunked, same recall pipeline, offline).
- **Structured LLM extraction** — `ENGRAPHIS_EXTRACTOR=llm_structured` validates typed
  facts, entities, relations, keywords, and confidence before storage. Its preserved
  entity/relation metadata feeds the knowledge graph automatically.

Files imported through the dashboard or `import_folder()` are marked **untrusted** by
default; MCP ingest remains an authenticated agent write.

---

## Consolidation and automated maintenance

Manual consolidation is free. The Pro **Automation** tab (and the
`GET/POST /api/automation` plus `POST /api/maintenance/run` API) can keep the store
clean without you clicking anything,
using a maintenance **policy** with two modes that compose:

- **Scheduled maintenance** — a consolidation + retention sweep on a fixed cadence
  (`cadence_hours`). Recurring episodic memories are distilled into semantic digests,
  and memories fading below `archive_below` retention are archived bi-temporally (pinned
  memories are always protected).
- **Auto-dreaming** — a *background* consolidation + **cross-cluster inference** loop
  (no cron needed — it runs inside the dashboard process) that fires when **both** hold:
  the store has accumulated ≥ `dream_min_new` new episodic memories since the last sweep,
  *and* the store has been idle for `dream_idle_minutes`. Dreaming emits low-salience
  `dream_inference` memories (cross-cluster/entity profiles, marked untrusted and linked
  back to their sources) so inferred knowledge is auditable and never silently promoted.

Knobs (dashboard Automation tab ↔ `/api/automation` API): `enabled`, `cadence_hours`,
`consolidate`, `min_cluster`, `archive_below`, `dream`, `dream_min_new`,
`dream_idle_minutes`, `infer`. Headless / no-dashboard-open: `python -m scripts.auto_maintain --apply`
(via Task Scheduler or cron).

Manual consolidation can also use schema-validated LLM output through
`MemoryService.consolidate`, `POST /api/consolidate`, `engraphis_consolidate`, or
`python -m scripts.consolidate --structured`. Source memories remain live by default;
`supersede_sources` / `--supersede-sources` closes them only after validated replacement
facts are written.

---

## Configuration

All via environment (or `.env`):

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_DB_PATH` | `<project/package root>/engraphis.db` | SQLite database file |
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Dashboard port |
| `ENGRAPHIS_API_TOKEN` | — | If set, REST API requires `Authorization: Bearer <token>` |
| `ENGRAPHIS_DB_KEY` | — | Encrypt the database at rest (SQLCipher). Or use `ENGRAPHIS_DB_KEY_FILE` |
| `ENGRAPHIS_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | sentence-transformers model |
| `ENGRAPHIS_EXTRACTOR` | `none` | `none` = verbatim; `chunk` = offline structure-aware chunks; `llm` = free-form LLM facts; `llm_structured` = schema-validated facts + graph metadata |
| `ENGRAPHIS_GRAPH_EXTRACTOR` | `regex` | `regex` = offline heuristic NER; `none` = disable heuristic text extraction (validated `llm_structured` metadata still feeds the graph) |
| `ENGRAPHIS_RETENTION_SUPERVISOR` | `none` | `none` = deterministic only; `llm` = sends a bounded excerpt to the configured provider for advisory ephemeral/normal/critical classification |
| `ENGRAPHIS_WHISPER_MODEL` | — | Enables local faster-whisper audio/video transcription |
| `ENGRAPHIS_POSTGRES_DSN` | — | CLI-only PostgreSQL source; used for the connection and never stored |
| `ENGRAPHIS_POSTGRES_CONNECT_TIMEOUT` | `10` | PostgreSQL introspection connection timeout in seconds (bounded to 1–120) |
| `ENGRAPHIS_POSTGRES_STATEMENT_TIMEOUT_MS` | `30000` | Per-introspection PostgreSQL statement timeout in milliseconds (bounded to 1–300000) |
| `ENGRAPHIS_GRAPH_TOKEN` | — | Bearer token for `engraphis-graph-server`; required off-loopback |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | `openai \| anthropic \| google \| openrouter \| custom` |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | Model name (provider-specific) |
| `ENGRAPHIS_LLM_API_KEY` | — | API key for chat/synthesis, `llm` / `llm_structured` extraction, and structured consolidation |
| `ENGRAPHIS_LLM_BASE_URL` | — | Base URL for openrouter / custom OpenAI-compatible endpoints |
| `ENGRAPHIS_LICENSE_KEY` | — | Pro/Team key (or `~/.engraphis/license.key`) |
| `ENGRAPHIS_TEAM_MODE` | `1` | Mount Team features by default; the auth wall activates for a live Team license or an existing team. Set `0` to disable |
| `ENGRAPHIS_LOOP_INTERVAL` | `60` | Background consolidation loop interval in seconds (0 = disabled) |
| `ENGRAPHIS_DECAY_HALFLIFE_DAYS` | `7` | Ebbinghaus decay half-life (higher = memories persist longer) |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted reverse-proxy IPs for TLS termination (`*` = trust all) |
| `ENGRAPHIS_RELAY_URL` | `https://team.engraphis.com` | Managed sync, license, trial, and invite relay (Pro/Team); the retired Railway URL is migrated automatically |
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
│   ├── mcp_server.py        # MCP server — 27 tools
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
