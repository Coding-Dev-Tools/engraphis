# Engraphis

[![PyPI version](https://img.shields.io/pypi/v/engraphis.svg)](https://pypi.org/project/engraphis/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](https://github.com/Coding-Dev-Tools/engraphis/blob/main/LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-yellow?style=for-the-badge&logo=buy-me-a-coffee)](https://buymeacoffee.com/Jaixii)

https://engraphis.com/

https://discord.com/invite/Wfr2ejBmY

**Give coding agents durable project memory so the next session can retrieve the current decision, its evidence, and its history.**

<p align="center">
  <img src="docs/images/engraphis-benefit-flow.png" alt="Project history becomes scoped memory, hybrid recall, and bounded cited context for an agent" width="100%">
  <br>
  <sup>Preserve a project decision · retrieve its supporting evidence · hand the next agent a bounded context</sup>
</p>

---

> **Open-core boundary:** this repository contains the free local engine, dashboard, MCP server,
> and customer-side clients. Hosted sync, analytics, automation, and team services run on the
> official hosted service; their server implementations are not distributed here.

## Measured token and context savings

<p align="center">
  <img src="docs/images/context-efficiency.png" alt="Dark chart showing Engraphis using 98.21 percent less long-history context, 73.0 percent less retrieved content per question, 73.9 percent fewer tokens in the smallest useful memory, a 55.38 percent smaller memory response, and 47.8 percent less repeated-memory context after consolidation" width="100%">
  <br>
  <sup>Less repeated history means more room for the task, tools, and useful evidence.</sup>
</p>

> **Evidence boundary:** External LoCoMo-derived figures are not canonical. The historical
> workload run used an unpinned model revision and has no checked-in raw dataset artifact.
> Treat its 98.21% context figure as directional until an immutable rerun produces a validated
> public artifact and checksum. The checked-in deterministic fixtures below remain reproducible.

<details>
<summary>See benchmark details and reproduce the results</summary>

### Controlled before-and-after example

| Retrieval mode | Mean returned memory content | Recall@5 |
|---|---:|---:|
| Whole documents | 808.8 tokens | 1.000 |
| Engraphis structure-aware chunks | 218.4 tokens | 1.000 |

The chunked mode returns the relevant passage instead of the whole document: **590.4 fewer tokens
per question**. Under the same model-context budget, that leaves roughly **590 tokens** for task
instructions or other relevant evidence.

### Measurement details and reproducibility

The table below records every current token/context efficiency measurement and its counting
boundary.

| What is counted | Comparison | Measured reduction | Quality held constant |
|---|---|---|---|
| Cumulative reader context across a 1,986-question LoCoMo diagnostic | Full-history replay: **49,915,394** tokens → Engraphis: **891,857** tokens | **49,023,537 fewer context tokens** (**98.2133% lower**) | Focused retrieval used far less context; uncapped full history retained higher retrieval recall |
| Retrieved top-5 memory content, averaged per question | Whole documents: **808.8** tokens → structure-aware chunks: **218.4** tokens | **590.4 fewer tokens per question** (**73.0% lower**, about **3.7× smaller**) | Recall@5 **1.000** in both modes across 6 documents and 18 questions |
| Smallest returned memory that contains the reference evidence | Whole documents: **162.2** tokens → chunks: **42.4** tokens | **119.8 fewer tokens to evidence** (**73.9% lower**, about **3.8× smaller**) | The same 18 questions had a returned evidence-holding memory in both modes |
| Serialized MCP recall response across 260 timed CodeMem recalls | Full result: **17,172** `engraphis.regex.v1` tokens → compact result: **7,663** tokens | **9,509 response tokens avoided** (**55.38% lower**) | Recall@5, hit@5, and answer-token recall all **1.000** |
| Repeated-memory consolidation fixture | 12 related episodic memories: **230** tokens → one digest: **120** tokens | **110 tokens removed from the active digest** (**47.8% lower**) | Original memories remain available for provenance and audit |
| Small histories across 26 CodeMem agent tasks | Always retrieve: **2,194** total agent-facing tokens and **26** memory calls → adaptive: **1,942** tokens and **0** memory calls | **252 tokens avoided** (**11.5% lower**) and all 26 unnecessary searches skipped | Both completed **24/26** tasks with the same deterministic offline task agent |
| Packed prompt-context usage in the same CodeMem performance fixture | Hard budget: **1,500** tokens; observed mean: **87.73**; observed maximum: **106** | A hard cap prevents a recall from exceeding its configured context budget | This is usage accounting, not a before/after savings comparison |

The compact MCP response avoids duplicating full memory bodies when the packed context and source
list are enough. That can reduce what an agent must inspect or pass onward, but the fixtures do
**not** measure model-provider charges, end-to-end task time, or customer cost savings.

The measures are deliberately separate and **must not be added together**: chunking counts the
content of retrieved memory records before `ContextPacker`, whereas compact recall counts the
serialized MCP response returned to a client. “Tokens to evidence” is the size of the smallest
retrieved memory record holding the reference evidence; it is not latency or end-to-end answer
accuracy. Chunking creates more focused stored records (24 chunks rather than 6 whole-document
memories in this fixture), so this is a context-efficiency result, not a storage-reduction claim.

Reproduce the quality and token/context measurements without a network connection or API key:

```bash
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.grounded
python -m eval.chunking_eval
python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10 --json
python -m eval.productivity --dataset eval/datasets/codemem.jsonl
```

These are small deterministic correctness and efficiency fixtures, not official LoCoMo /
LongMemEval QA scores or a third-party leaderboard result. Compact-response counts use the exact
`engraphis.regex.v1` counter; the chunking evaluation uses its documented deterministic
normalized-character estimator. Chunking measures retrieved memory content, while compact recall
measures serialized MCP response size. See [`BENCHMARKS.md`](BENCHMARKS.md) for definitions,
limitations, canonical external-evaluation requirements, and the no-unsupported-claims policy.

</details>

---

## What Engraphis gives an agent

An agent should not have to reconstruct a project from scattered chat history on every task.
Engraphis turns local project knowledge into scoped, time-aware memory; retrieves the evidence
that supports the current question; and returns a bounded, attributable context packet.

The core task is continuity: retrieve the current, supported project decision without dragging the
whole history into the next prompt. See [measured token and context savings](#measured-token-and-context-savings)
for the short version of how much less history an agent has to carry.

| Agent need | What Engraphis changes |
|---|---|
| Remember a project across sessions | Stores typed memory in a `workspace → repo → session` hierarchy and provides a last-session handoff. |
| Find support for the current task | Fuses vector, lexical, graph, and code-aware retrieval instead of relying on one search signal. |
| Know what is true now and what changed | Preserves bi-temporal history and supersession chains instead of silently overwriting a fact. |
| Avoid confident guesses | Returns cited evidence or explicitly abstains when support is too weak. |
| Avoid dragging the whole project into every prompt | Packs context to a configured hard budget and can return a compact MCP response. |
| Keep knowledge in the operator's control | Runs local-first and offline-capable, with scopes, audit records, and optional privacy-safe receipts. |

### See the behavior in reproducible fixtures

The examples below use synthetic, checked-in evaluation inputs. They show three different
contracts: retrieving focused evidence, returning an answer only with support, and explicitly
abstaining when no support exists.

<p align="center">
  <img src="docs/images/evidence-backed-agent-examples.png" alt="Three evidence-backed examples: focused context keeps Recall at 5 while reducing returned content, answerable questions return cited support, and unsupported questions explicitly abstain" width="100%">
  <br>
  <sup>Each card names its deterministic offline fixture and test scope. The examples are illustrative; they are not customer data or external benchmark results.</sup>
</p>

Run `python -m eval.chunking_eval` and `python -m eval.grounded` to reproduce the behavior;
the former measures evidence retrieval and context size, while the latter measures the
answer-versus-abstain decision.

## Full Engraphis install: pip install "engraphis[all]"

Engraphis-Dashboard opens `http://127.0.0.1:8700`. Local memory needs no cloud account,
signup, or API key and stays in a SQLite file on your machine.

**Ledger** is the primary local interface for recall, memories, graph exploration, provenance,
workspaces, and manual consolidation. **Classic** preserves the former full tool suite; both use
the same local data. Switch in **Manage → Settings → Interface** (Ledger) or **Settings →
Appearance & Engine** (Classic).

### Managed compute

Managed compute is separate from Cloud Sync. A connected installation may send a bounded,
non-secret snapshot for a hosted proposal; the hosted service must read it to produce a proposal,
so this is not end-to-end-encrypted processing. Local-only installations send nothing. Set
`ENGRAPHIS_MANAGED_COMPUTE_CONSENT=0` to opt out; `ENGRAPHIS_RETENTION_SUPERVISOR=none` keeps
retention supervision local (the default).

### Start it on every platform

| Platform | How |
|----------|-----|
| **Windows** | Double-click **Engraphis Dashboard** on your Desktop or Start Menu (install: `engraphis-dashboard --install-shortcuts`) |
| **macOS** | Double-click **Engraphis Dashboard.app** on your Desktop (install: same command) |
| **Linux** | Desktop entry in Applications → Development (GNOME/KDE/etc.) |
| **Docker** | `docker compose up`: see `docker-compose.yml` for the one-command deployment |
| **Any** | `engraphis-dashboard` in a terminal |

### Accessibility-first inspection, built in

Inspect memories, supersession diffs, recall scores, timelines, links, consolidation, and audit
records in the dashboard. The offline graph renderer is vendored, and the interface is keyboard-
navigable with light and dark themes.

---

## How it works

Engraphis gives agents durable, scoped, *explainable* project knowledge. The local engine combines
Ebbinghaus decay, bi-temporal facts, and hybrid vector/lexical/graph recall; it runs offline with
SQLite, local embeddings, and `numpy` only.

- **Grounded and governed:** deterministic conflict resolution, cited answers or abstention,
  explicit correction/promotion/forgetting, and a complete history.
- **Agent-ready:** MCP tools, hard-budget context packets, handoffs, and code-aware retrieval.
- **Auditable:** content-free receipt chains, provenance, and temporal/entity/code relationships.
- **Practical:** local file and code ingest, optional PDF/OCR/transcription, and SQLCipher at rest.

### Optional LLM providers

The memory engine, embeddings, conflict resolution, and recall stay local without an LLM. An
explicitly configured provider adds structured extraction, cited synthesis, consolidation, and
retention supervision. Configure it in **Settings → Connect an LLM**. The activity view records
outcomes, never keys, prompts, or raw provider responses. See the
[LLM provider guide](docs/LLM_PROVIDERS.md) for setup and privacy choices.

> Privacy boundary: text sent to an explicitly selected provider leaves the local process under
> that provider's terms. Use `ENGRAPHIS_RETENTION_SUPERVISOR=none` (the default) and the offline
> `chunk` extractor when ingestion must remain entirely local.

Choose and configure an external LLM with the [LLM provider guide](docs/LLM_PROVIDERS.md),
including OpenAI, Anthropic, Google, OpenRouter, Ollama, Cohere Command, Command Code, and
compatible endpoints.

---

## Install

```bash
pip install "engraphis[all]"        # self-hosted dashboard, MCP, code graph, documents, transcription, PostgreSQL, and Cloud Sync
pip install "engraphis[server]"     # dashboard + REST API
pip install "engraphis[mcp]"        # MCP server only
pip install "engraphis[documents]"  # PDF + image OCR bindings
pip install "engraphis[transcription]" # faster-whisper audio/video
pip install "engraphis[postgres]"   # PostgreSQL schema introspection
pip install "engraphis[code]"       # tree-sitter code graph indexing
pip install "engraphis[cloud-sync]" # Cloud Sync client crypto/runtime
pip install "engraphis[encryption]" # SQLCipher encryption-at-rest extra
pip install engraphis               # core library: numpy only, fully offline
```

The official Docker image includes the local Tesseract executable for image OCR. Outside
Docker, the `documents` extra installs its Python bindings; install Tesseract through your
operating system as well if you enable image OCR.

The NumPy-only core library supports Python 3.9+. Current patched releases of the WebUI
stack, MCP SDK, image parser, and Cloud Sync client require Python 3.10+, so use Python 3.10
or newer for the `server`, `mcp`, `documents`, `cloud-sync`, or `all` installation paths.

The default `NumpyVectorIndex` performs an exact full scan. There is no universal memory-count
cutoff because latency depends on vector size, hardware, filters, and the rest of the recall
pipeline. Measure your machine with `python -m eval.vector_scale`, then run
`python -m eval.performance` on a representative corpus. If exact scans miss your latency target,
create the engine with `vector_backend="sqlite-vec"` and remeasure. See [BENCHMARKS.md](BENCHMARKS.md)
for the reproducible commands and reporting limits.

`MemoryEngine.create()` and `MemoryService.create()` default to the exact NumPy index, even when
`sqlite-vec` is installed, so the default remains portable and deterministic. `sqlite-vec` and
SQLCipher load incompatible SQLite native libraries in one process: with `vector_backend="auto"`
Engraphis falls back to NumPy; an explicit `vector_backend="sqlite-vec"` fails with an actionable
error. Run accelerated search in a fresh process when using the SQLCipher extra.

`sqlcipher3-binary` publishes CPython manylinux x86-64 wheels. On that target,
`engraphis[encryption]` installs the driver. The cross-platform `all` extra deliberately
omits it so `all` remains resolvable on macOS, Windows, Linux ARM, and musl; on those
targets, provision a compatible SQLCipher driver separately before enabling a database
key. Plaintext SQLite remains the explicit default on every platform.

> **Linux / macOS:** if `pip install` fails with `error: externally-managed-environment`,
> your system Python is marked read-only (PEP 668). Install into a virtual environment
> instead. Run `python3 -m venv venv && source venv/bin/activate && pip install "engraphis[server]"`
> Alternatively, use Docker (`docker compose up`). `pipx install "engraphis[server]"` also works.

> First run downloads `all-MiniLM-L6-v2` (~80 MB). Without it, the engine falls back
> to a deterministic offline embedder so it always runs.

---

## Quickstart: dashboard (the headline)

```bash
pip install "engraphis[server]"
engraphis-dashboard                   # → http://127.0.0.1:8700
engraphis-dashboard --install-shortcuts   # → Desktop + Start Menu icons
```

### Docker

```bash
docker compose up                     # → http://127.0.0.1:8700
```

A fresh clone needs no `.env`: the service runs `engraphis-dashboard --no-open` and stores the v2
database plus the optional customer-side cloud session and non-authoritative entitlement display
cache on a named volume mounted at `/data`. Generic `.env` settings can supply optional runtime
configuration, but Compose deliberately keeps its container bind address and `/data` paths fixed;
that prevents a desktop `ENGRAPHIS_HOST` or `ENGRAPHIS_DB_PATH` from breaking container reachability
or persistence. To use another loopback port, set `ENGRAPHIS_COMPOSE_PORT` in `.env` or the shell:

```dotenv
ENGRAPHIS_COMPOSE_PORT=8787
```

Then open `http://127.0.0.1:8787`. License issuance, trials, leases, and revocations remain on the private control plane.
`engraphis-server` and `engraphis server` are headless compatibility aliases
for this same v2 service, so every public surface has the same scoped recall and retention model.

Compose publishes only on loopback by default. To expose it on a LAN, set a strong API token and
the exact URL clients will use:

```dotenv
ENGRAPHIS_API_TOKEN=<a-long-random-secret>
ENGRAPHIS_DASHBOARD_URL=http://<host-LAN-IP>:8700
```

Then start the token-required LAN overlay (Docker Compose v2.24.4+):

```bash
docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d
```

The URL variable alone does not expose or secure the service. The LAN overlay refuses to render
without `ENGRAPHIS_API_TOKEN`; it replaces the loopback port mapping with an all-IPv4-interface mapping.
After this opt-in, other machines on the LAN can use
`http://<host-LAN-IP>:8700`.

The Docker image includes the streamable HTTP MCP endpoint at `/mcp/` (the `/mcp` path redirects
there). Configure `ENGRAPHIS_DASHBOARD_URL` to the exact LAN IP or hostname clients use so MCP's
DNS-rebinding protection accepts the request. For example, use
`http://192.168.10.151:8700` for direct LAN access, or `http://engraphis.local` behind Traefik.
For an HTTP-enabled deployment, use the dashboard port (replace `8700` with your
`ENGRAPHIS_COMPOSE_PORT` value when you override it):

```json
{
  "engraphis": {
    "transport": "http",
    "enabled": true,
    "url": "http://<host-LAN-IP>:8700/mcp/"
  }
}
```

When `ENGRAPHIS_API_TOKEN` is set, configure the client to send
`Authorization: Bearer <ENGRAPHIS_API_TOKEN>`. Remote requests without a token are rejected.

Set `ENGRAPHIS_API_TOKEN` to require API authentication and `ENGRAPHIS_DB_KEY` to encrypt
the local database at rest. Hosted-plan credentials configure customer clients; they do not
install premium server implementations into this image. See `docker-compose.yml` for options.

---

## Quickstart: MCP server (for coding agents)

```bash
pip install "engraphis[mcp]"
engraphis-init                     # writes .env + prints config snippets
claude mcp add engraphis -- engraphis-mcp
cmd mcp add engraphis -- engraphis-mcp  # Command Code CLI
```

For Command Code scopes, verification, and its optional Provider API setup, see the
[Command Code section of the LLM provider guide](docs/LLM_PROVIDERS.md#command-code).

Your agent now has 31 tools for memory, recall, grounded answers, timelines, consolidation, code
graph work, and privacy-safe receipts. The full inventory, including `engraphis_check_update`, is
in the [MCP tool reference](docs/MCP_TOOLS.md).

For unattended jobs, `engraphis_start_session`, `engraphis_remember`, and
`engraphis_record_event` use workspace `default` when `workspace` is omitted.

### Review gate for MCP, REST, imports, and sync

Every public write enters review as `pending`, regardless of a caller-supplied `source` or
`trusted` label. That includes MCP, dashboard/REST intent writes, imports, sync, and extractor
output. Detector matches are instead `quarantined` immediately. Pending and quarantined records
remain inspectable and auditable, but cannot enter model-ready recall/context, resolution,
links, graph/code backfill, derived prompt context, or public `why`/`timeline` history.
Corrections, promotions, and merges fail closed unless every input is explicitly approved.

Approval creates a fresh `approved` successor and preserves the reviewed source plus an audit
link; it never relabels the source in place. There is deliberately no MCP tool or general REST
approval endpoint. A local owner can approve through the dashboard's **Approve for prompt**
action after configuring `ENGRAPHIS_API_TOKEN` (short-lived browser session plus CSRF confirmation),
or from an interactive terminal:

```bash
python -m scripts.approve_memory mem_... --reason "verified against the owner runbook"
```

The command rejects redirected input and requires typing its displayed confirmation. Hosted
owner/admin approval is performed by the hosted service, not this local package. The direct
in-process `MemoryEngine` remains a documented trusted-code boundary for code that already has
local database authority; do not expose it to untrusted transports. Existing stores can be
inspected without writes, then migrated deliberately:

```bash
python -m scripts.rescan_poisoning --db engraphis.db
python -m scripts.rescan_poisoning --db engraphis.db --apply
```

The dry run opens the database read-only. The applying pass demotes historical non-approved
records to pending review, quarantines detected payloads, retires their derived bridges, and
records an audit event.

## Quickstart: repository graph

```bash
pip install "engraphis[code]"
engraphis-graph index -w acme -r api --root .
engraphis-graph search -w acme -r api "UserService"
# `query`/`explain` blend code search with your stored memories: query matches symbol
# and file NAMES (a full question sentence won't match anything), and explain's answer
# is drawn from memories recorded against the repo; both are empty on a fresh index.
engraphis-graph query -w acme -r api "UserService"
engraphis-graph explain -w acme -r api "why does deploy depend on approval?"
engraphis-graph path -w acme -r api UserService DatabasePool
engraphis-graph impact -w acme -r api --root . --git-range origin/main...HEAD
engraphis-graph prs -w acme -r api --base main --head HEAD
engraphis-graph export -w acme -r api -o engraphis-graph-out
engraphis-graph install-merge-driver --root .
```

The export contains `graph.json`, a self-contained `graph.html`, and `GRAPH_REPORT.md`.
Indexing supports Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++, SQL, and
Terraform. Tree-sitter is used when available; the dependency-free regex backend remains a
functional fallback. Definitions, methods, calls, imports, ownership, variables,
inheritance/implementation, and docstrings/comments are indexed. Indexing is incremental by
content hash, honors `.engraphisignore`, and does not follow file symlinks outside the repository
root. Call edges are name-based and best-effort rather than type-resolved. The optional Git merge
driver validates bounded graph JSON and deterministically unions nodes and edges instead of
choosing one export side.

For a read-only recall and graph API that can be shared without exposing write operations:

```bash
pip install "engraphis[server]"
engraphis-graph-server                 # API at http://127.0.0.1:8720; schema at /openapi.json
```

A non-loopback bind fails closed unless `ENGRAPHIS_GRAPH_TOKEN` (or
`ENGRAPHIS_API_TOKEN`) is set. See [the v3 architecture/design document](docs/ARCHITECTURE_V3.md).

---

## Quickstart: Python library

```python
from engraphis.service import MemoryService

mem = MemoryService.create("engraphis.db")
mem.remember("Auth migrated from JWT to PASETO.", workspace="acme", repo="api")
hit = mem.recall("why did we change auth?", workspace="acme", repo="api")
print(hit["context"])
```

The same `MemoryService` backs the dashboard and the MCP server.

Agent hosts can avoid retrieval when their existing history already fits:

```python
decision = mem.adaptive_context(
    "what should the agent do next?",
    current_history,
    workspace="acme",
    repo="api",
    max_context_tokens=8_192,
    retrieval_token_budget=1_024,
)
prompt_context = decision["context"]
```

The decision is `history_bypass` when the history fits, `retrieval` when compact evidence is
strong, and `history_fallback` when weak retrieval should widen back to recent raw history.

For an agent prompt, prefer `engraphis_recall_context`: it returns one hard-budget packed
`context` plus compact `sources`, deterministic `usage` accounting (`budget_tokens`, `context_tokens`,
`source_tokens`, `saved_tokens`, `savings_ratio`, `packed_count`, `omitted_count`, and
`token_counter`), and optional diagnostics. Accounting is exact for the named counter; inject the
reader's tokenizer when reader-model token parity is required. `engraphis_recall` remains the compatible full-recall
surface; use `response_mode="compact"` when the packed context is enough and full memory bodies
would duplicate it. Both default to the `balanced` retrieval profile and `planning="off"`.
Opt-in `planning="auto"` keeps the original query, admits at most two deterministic or injected
query routes, and fuses them before reranking against the original query. `mtype_limits`, when
provided, are post-rerank maximum counts rather than relevance boosts. Every packed response has a
stable `context_revision` derived from the token-counter identity and ordered packed excerpts, so a
host can retain an unchanged prompt prefix. Planner output, per-query rankings, cap drops, and
fallback reasons appear only with `diagnostics=True`.

The offline planner is the default injected implementation. An application can opt into an LLM
planner without coupling `core/` to a provider:

```python
from engraphis.backends.query_planner import LLMQueryPlanner
from engraphis.core.engine import MemoryEngine

engine = MemoryEngine.create(
    "engraphis.db",
    query_planner=LLMQueryPlanner(my_llm),
)
result = engine.recall(
    "why does ReleaseGate depend on AuditLog?",
    workspace_id="ws_...",
    planning="auto",
    mtype_limits={"working": 1, "semantic": 3},
)
```

Planner failures and provider deadlines fail open to the original single-query plan. Planned recall
remains opt-in until the checked-in budget, safety, and official LongMemEval-V2 gates justify a
default change.

For bi-temporal reads, `valid_at` selects what was true at a Unix timestamp and `known_at` selects
what Engraphis had learned then. `as_of` remains a compatibility alias for `valid_at`; supplying
both is allowed only when they match.

For a mutable claim, pass a stable `subject_key` and optional `claim_kind`, such as
`subject_key="api.rate_limit", claim_kind="configured_value"`. Offline conflict resolution
deterministically adds, reinforces, relates, or supersedes records while preserving temporal
history; it does not need an LLM. Matching claim identities let it supersede substantially
reworded mutable facts. Without them, the dependency-free lexical embedder cannot reliably infer
that a paraphrase is a contradiction, so keep both records or use an explicit `correct` operation.

---

## Govern memories without losing history

Engraphis separates automatic write resolution from explicit human governance:

| Operation | Use it when | What happens to history |
|---|---|---|
| `remember` | Adding or restating one fact | Adds, reinforces, safely supersedes, or relates an uncertain neighbor |
| `correct` | Replacing one known-wrong memory | Closes the old validity window and links the replacement |
| `promote` | A narrow learning now applies more broadly | Writes a wider-scope successor and closes/links the source instead of editing scope in place |
| `merge` | Combining two or more overlapping memories | Retires every source and creates one memory that supersedes all of them |
| `forget` | Removing a memory from live recall | Bi-temporally closes it; the audit/history record remains |
| `consolidate` | Distilling recurring episodic memories automatically | Creates linked semantic digests; sources stay live unless explicit supersession is requested |

Manual N→1 merge is available through `MemoryService.merge()` and `POST /api/merge`:

```python
a = mem.remember("Deploys happen Friday at 3pm.", workspace="acme")
b = mem.remember("We deploy Fridays around 15:00.", workspace="acme")

merged = mem.merge(
    [a["id"], b["id"]],
    "Deploys ship every Friday at approximately 15:00.",
    workspace="acme",
    reason="deduplicate the deployment schedule",
)
print(merged["compaction"])
```

All sources must belong to the named workspace. The result inherits the strictest source
sensitivity, remains untrusted if any source was untrusted, and stays pinned if any source was
pinned. The full multi-predecessor chain remains visible through inspection, Why, and Timeline.

---

## Free forever vs. hosted plans

The core engine, local dashboard, MCP server, and manual consolidation are Apache-2.0 and free.
**Pro and Team are services** that provide optional access to the official hosted service; its
control-plane, billing, relay, compute, and Team identity modules live in a private repository.
They do not limit the local core. See
[hosted plans](docs/HOSTED_PLANS.md), [licensing](docs/LICENSING.md), and
[Cloud Sync](docs/SYNC.md) for service boundaries, lifecycle, and pricing.

[Subscribe to Pro](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=readme_pricing#billing)
to support the project and add hosted services.

[Compare hosted plans](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=readme_intro#billing)
when you are ready to evaluate the service boundary and billing options.

| | Free (available now) | Pro: $10/mo or $100/yr | Team: $20/seat/mo or $200/seat/yr |
|---|---|---|---|
| Dashboard WebUI (with built-in inspector) | ✓ | ✓ | ✓ |
| Memory engine + 31 MCP tools | ✓ | ✓ | ✓ |
| Version-chain diffs, offline knowledge graph | ✓ | ✓ | ✓ |
| Manual local consolidation (dry-run by default) | ✓ | ✓ | ✓ |
| Local workspace export (JSON: memories, sessions, audit) | ✓ | ✓ | ✓ |
| Hosted Cloud Sync | | ✓ | ✓ |
| Hosted Analytics | | ✓ | ✓ |
| Hosted Auto Consolidation + retention policy | | ✓ | ✓ |
| Hosted Auto Dreaming + managed proposals | | ✓ | ✓ |
| Priority support | | ✓ | ✓ |
| Hosted multi-user dashboard: invitations, logins, roles, seat management | | | ✓ |
| Hosted Team audit log + CSV export | | | ✓ |
| 72-hour pending invitations (resend/revoke) | | | ✓ |
| Scoped, expiring per-user agent and sync tokens | | | ✓ |

---

## MCP tools

Engraphis exposes 31 MCP tools across memory, recall, code graphs, governance, sessions, and
privacy-safe audit receipts. The focused [MCP tool reference](docs/MCP_TOOLS.md) is the source for
the full inventory and parameters.

---

## Graphs and privacy-safe receipts

Memory, entity, and code relationships live in one local graph. Engraphis also provides
content-free operation receipts for inspectable audit evidence. See the
[architecture](docs/ARCHITECTURE_V3.md), [MCP tool reference](docs/MCP_TOOLS.md), and
[security policy](SECURITY.md) for the data model, tools, and guarantees.

---

## Cloud sync

Cloud Sync is an optional hosted Pro/Team service. The public package includes the customer client
and deterministic merge implementation; hosted relay and account operations are separate. See
[Cloud Sync](docs/SYNC.md) for setup, encryption, merge behavior, and the local folder exchange.

---

## Security and trust boundaries

Engraphis is local-first and binds to loopback by default. Read the
[security policy](SECURITY.md) before remote deployment or integrating external resources; it
covers supported versions, data protections, threat model, and vulnerability reporting.

---

## Encryption at rest

Set `ENGRAPHIS_DB_KEY` (or `ENGRAPHIS_DB_KEY_FILE`) and install the extra:

```bash
pip install "engraphis[encryption]"
```

The entire main memory database file is transparently encrypted with AES-256 via SQLCipher;
full-text search, the graph, and every query keep working unchanged. Customer authentication
and managed-service state use their respective deployment protections. When a key is set for the main database, Engraphis
**fails loud** rather than silently falling back to plaintext. Generate a strong key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

> An existing plaintext database cannot be opened with a key: migrate it (dump → import
> into a fresh keyed DB). See `.env.example` for all encryption options.

---

## Import files and folders

Import supported documents and code through the dashboard, a local folder, or MCP. Optional
extractors add offline chunking, structured LLM extraction, document OCR, transcription, and
PostgreSQL schema ingestion. See the [MCP tool reference](docs/MCP_TOOLS.md),
[architecture guide](docs/ARCHITECTURE_V3.md), and [security policy](SECURITY.md) for formats,
configuration, and local-resource safeguards.

---

## Consolidation and automation

Manual consolidation is free, local, and dry-run by default; use the dashboard, SDK, CLI, or
MCP. Hosted Pro and Team automation is optional managed compute that produces reviewable
proposals rather than silently changing local data. See [hosted plans](docs/HOSTED_PLANS.md),
[licensing](docs/LICENSING.md), and the [MCP tool reference](docs/MCP_TOOLS.md) for scope and use.

---

## Configuration

All via environment (or `.env`):

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_DB_PATH` | Source: `<repo>/engraphis.db`; installed: platform user-data directory | SQLite database file. Installed defaults are `%LOCALAPPDATA%\engraphis\engraphis.db` (Windows), `~/Library/Application Support/engraphis/engraphis.db` (macOS), and `$XDG_DATA_HOME/engraphis/engraphis.db` or `~/.local/share/engraphis/engraphis.db` (Linux). The environment variable overrides every default. |
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Dashboard port |
| `ENGRAPHIS_SERVICE_MODE` | `customer` | The public package supports only `customer`; hosted vendor, relay, compute, and worker roles are not distributed here |
| `ENGRAPHIS_API_TOKEN` | Not set | Optional bearer credential for this single-user local customer node; never reuse a hosted credential |
| `ENGRAPHIS_CORS_ORIGINS` | loopback on `ENGRAPHIS_PORT` | Comma-separated REST CORS allow-list; defaults to `127.0.0.1` and `localhost` on the configured port |
| `ENGRAPHIS_WORKSPACES` | Not set | Optional comma-separated server-side workspace allow-list |
| `ENGRAPHIS_INDEX_ROOTS` | Working, home, and temporary directories | Optional path-separator-delimited absolute-path allow-list that replaces the default roots accepted by local code indexing |
| `ENGRAPHIS_HTTP_INDEX_ROOT` | First `ENGRAPHIS_INDEX_ROOTS` entry, or current directory | Single root for dashboard and REST `POST /api/code/index`; submitted paths resolve beneath it. An explicit root (or fallback entry) must be absolute; an explicit HTTP root is included in the engine-approved set. MCP and CLI indexing continue to use `ENGRAPHIS_INDEX_ROOTS`. |
| `ENGRAPHIS_DB_KEY` | Not set | Encrypt the database at rest (SQLCipher). Or use `ENGRAPHIS_DB_KEY_FILE` |
| `ENGRAPHIS_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | sentence-transformers model |
| `ENGRAPHIS_EXTRACTOR` | `none` | `none` = verbatim; `chunk` = offline structure-aware chunks; `llm` = free-form LLM facts; `llm_structured` = schema-validated facts + graph metadata |
| `ENGRAPHIS_CHUNK_TOKENIZER_MODEL` | Not set | Optional Hugging Face tokenizer used to enforce chunk budgets with the downstream reader's real tokenization; requires the optional `transformers` package |
| `ENGRAPHIS_CHUNK_TOKENIZER_REVISION` | Not set | Optional immutable tokenizer/model revision recorded in the chunk-counter identity; pin this for reproducible benchmark artifacts |
| `ENGRAPHIS_GRAPH_EXTRACTOR` | `regex` | `regex` = offline heuristic NER; `none` = disable heuristic text extraction (validated `llm_structured` metadata still feeds the graph) |
| `ENGRAPHIS_RETENTION_SUPERVISOR` | `none` | `none` = deterministic only; `llm` = sends a bounded excerpt to the configured provider for advisory ephemeral/normal/critical classification |
| `ENGRAPHIS_ALLOW_AUTOMATIC_CRITICAL_RETENTION` | `false` | Opt in only when an LLM supervisor may automatically assign the long-lived `critical` class; explicit user-selected critical retention is unaffected |
| `ENGRAPHIS_WHISPER_MODEL` | Not set | Enables local faster-whisper audio/video transcription |
| `ENGRAPHIS_POSTGRES_DSN` | Not set | CLI-only PostgreSQL source; used for the connection and never stored |
| `ENGRAPHIS_POSTGRES_CONNECT_TIMEOUT` | `10` | PostgreSQL introspection connection timeout in seconds (bounded to 1–120) |
| `ENGRAPHIS_POSTGRES_STATEMENT_TIMEOUT_MS` | `30000` | Per-introspection PostgreSQL statement timeout in milliseconds (bounded to 1–300000) |
| `ENGRAPHIS_GRAPH_TOKEN` | Not set | Bearer token for `engraphis-graph-server`; required off-loopback |
| `ENGRAPHIS_GRAPH_HOST` / `ENGRAPHIS_GRAPH_PORT` | `127.0.0.1` / `8720` | Read-only graph/recall server bind address |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | `openai \| anthropic \| google \| openrouter \| custom` |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | Model name (provider-specific) |
| `ENGRAPHIS_LLM_API_KEY` | Not set | API key for chat/synthesis, `llm` / `llm_structured` extraction, and structured consolidation |
| `ENGRAPHIS_LLM_BASE_URL` | Not set | Base URL for openrouter / custom OpenAI-compatible endpoints |
| `ENGRAPHIS_LLM_AUTO_EXTRACT` | `0` | Opt in to switching the running engine to `llm_structured` after a successful live connection test; the dashboard's extraction Off button persists `0`, and its On button restores `1` |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | *(none)* | Proxies trusted for forwarded client/TLS headers (`*` only when the service is reachable exclusively through that proxy) |
| `ENGRAPHIS_LOCAL_TRUSTED_PEERS` | *(none)* | Exact peers/CIDRs treated as local without forwarding headers; use only for trusted Docker/LAN peers, never public deployments |
| `ENGRAPHIS_CLOUD_CONTROL_URL` | hosted default | Official entitlement, organization, and credential control API |
| `ENGRAPHIS_CLOUD_COMPUTE_URL` | hosted default | Official Analytics and managed-automation API |
| `ENGRAPHIS_CLOUD_ORGANIZATION_ID` | Not set | Hosted organization bound to this customer session |
| `ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL` | Not set | Bootstrap-only rotating hosted credential; after first use the owner-only cloud session replacement takes precedence |
| `ENGRAPHIS_CLOUD_TOKEN_SUBJECT` | `member` | Subject fixed during hosted bootstrap (`device` or `member`); set explicitly with an environment-only refresh credential |
| `ENGRAPHIS_CLOUD_ACCESS_TOKEN` | Not set | Optional short-lived access token for ephemeral jobs |
| `ENGRAPHIS_MANAGED_COMPUTE_CONSENT` | *(auto)* | Operator override only; default follows whether a cloud session is configured (connected = allowed, local-only = never). `0` opts a connected installation out; `1` permits local snapshot preparation but does not create a cloud credential or authorize an upload |

See `.env.example` for the full customer-runtime and managed-service client options.

---

## Project structure

```
engraphis/
├── engraphis/
│   ├── core/                # v2 engine: interfaces, store, recall, scoring, schema, sync
│   ├── backends/            # pluggable embedder / vector index / reranker / codegraph / sync transports / encryption
│   ├── service.py           # validated MemoryService facade
│   ├── mcp_server.py        # MCP server: 31 tools
│   ├── dashboard_app.py     # dashboard WebUI (FastAPI)
│   ├── dashboard_assets/    # primary Ledger interface + graph engine
│   ├── classic_assets/      # selectable full operator dashboard backup
│   ├── read_only_api.py     # token-protected recall/repository-graph HTTP surface
│   ├── hosted_client.py     # hosted URLs, plan labels, and endpoint validation only
│   ├── licensing.py         # compatibility facade for hosted presentation metadata
│   ├── cloud_session.py     # rotating hosted customer-session client
│   ├── cloud_features.py    # consented managed-feature protocol client
│   ├── config.py / app.py   # env settings / REST server
│   └── static/              # compatibility dashboard asset paths
├── eval/                    # offline retrieval eval harness + datasets
├── tests/                   # pytest suite (300+ tests, offline numpy-only core)
├── scripts/                 # dashboard, server, graph, CLI, connect, update, consolidation, sync
├── docs/                    # product, API, hosting, sync, and provider guides
├── Dockerfile / docker-compose.yml
└── pyproject.toml
```

New capability belongs in the v2 path (`engraphis/core/`, `engraphis/backends/`, and
`MemoryService`) behind the interfaces in `core/interfaces.py`. The flat-namespace v1 server
under `engraphis/app.py`, `routes/`, `stores/`, and `engines/` remains a compatibility/reference
surface; `engraphis-dashboard`, the MCP server, and the Python quickstart above use v2.

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
LoCoMo, LongMemEval, MemoryAgentBench, LoCoMo-Plus, and Mem2ActBench adapters are available,
along with a pinned LongMemEval-V2 reader profile, redacted evidence exporter, and paired
full-history versus Engraphis code-agent analyzer. External adapters measure only the layer they
declare; retrieval or tool-argument context coverage is not presented as end-to-end answer,
action, or task success. Reproduction commands and remaining official-run requirements are in
[`BENCHMARKS.md`](BENCHMARKS.md).

---

## Release evidence

Each tagged release includes `release-evidence.json` and a reproducible CycloneDX JSON SBOM as
GitHub Release assets. The evidence binds the matching tag and commit to the built wheel and
source distribution hashes, SBOM hash, source-input hashes, and the completed release-gate checks.
It is intentionally limited: it does not attest to publication, hosted services, payments,
deployments, or runtime data; the SBOM describes the build job's Python environment rather than an
operating-system or container image.

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). "Engraphis" is a trademark of the
Engraphis project; the license does not grant trademark rights. Code already distributed
under Apache-2.0 keeps that grant; later releases cannot retroactively withdraw it. The
official hosted control plane, its production credentials and records, managed operations,
support, and future separately delivered commercial modules are outside the public source
grant. See [`docs/LICENSING.md`](docs/LICENSING.md) for the complete boundary.
