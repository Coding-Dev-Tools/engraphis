# Engraphis

A self-hosted AI memory system that replicates the [Engraphis](https://github.com/tinyhumansai/neocortex) architecture — Ebbinghaus forgetting-curve decay, interaction-aware reinforcement, and conscious thought synthesis — running entirely on your machine with no API key, no rate limits, and no per-token cost for the memory layer.

You choose the external LLM (OpenAI, Anthropic, Google, OpenRouter, or any OpenAI-compatible endpoint) for thought synthesis and chat. The memory engine itself is 100% local: SQLite + sentence-transformers embeddings.

---

## Quick Start

### 1. Install

```bash
cd engraphis
pip install -r requirements.txt
```

> The embedding model (`all-MiniLM-L6-v2`, ~80 MB) downloads automatically on first use.

### 2. Configure

```bash
copy .env.example .env
```

Edit `.env` and set your LLM provider + API key:

```ini
ENGRAPHIS_LLM_PROVIDER=openai          # openai | anthropic | google | openrouter | custom
ENGRAPHIS_LLM_MODEL=gpt-4o-mini
ENGRAPHIS_LLM_API_KEY=sk-your-key-here

# For OpenRouter:
# ENGRAPHIS_LLM_PROVIDER=openrouter
# ENGRAPHIS_LLM_MODEL=anthropic/claude-3.5-sonnet
# ENGRAPHIS_LLM_API_KEY=sk-or-your-key
# ENGRAPHIS_LLM_BASE_URL=https://openrouter.ai/api/v1

# For Anthropic:
# ENGRAPHIS_LLM_PROVIDER=anthropic
# ENGRAPHIS_LLM_MODEL=claude-3-5-haiku-20241022
# ENGRAPHIS_LLM_API_KEY=sk-ant-your-key

# For a custom OpenAI-compatible endpoint:
# ENGRAPHIS_LLM_PROVIDER=custom
# ENGRAPHIS_LLM_MODEL=your-model
# ENGRAPHIS_LLM_API_KEY=your-key
# ENGRAPHIS_LLM_BASE_URL=https://your-endpoint/v1
```

### 3. Start the server

```bash
python -m scripts.start_server
```

You'll see:
```
Engraphis — starting on 127.0.0.1:8700
  Database:     ./neocortex.db
  Embed model:  sentence-transformers/all-MiniLM-L6-v2
  LLM provider: openai / gpt-4o-mini
  Loop interval: 60s
  SDK base URL: http://127.0.0.1:8700
  Docs:         http://127.0.0.1:8700/docs
```

### 4. Verify

In another terminal:

```bash
python -m scripts.test_routes
```

---

## Migrating from Obsidian Vault

Seed your existing Obsidian vault (or any folder of markdown files) into the memory system:

```bash
python -m scripts.seed_from_obsidian "C:/Users/home/OneDrive/Documents/Obsidian Vault Local"
```

Each `.md` file becomes a searchable memory with:
- `document_id` = relative file path
- `title` = first `# H1` heading or filename
- `content` = full file text
- `metadata` = `{file, tags, links, word_count}`

Use `--namespace vault` (default) or a custom namespace. Use `--limit 50` to test with a subset first.

---

## Usage

### CLI

```bash
# Store a memory
python -m scripts.cli ingest "User prefers dark mode" -n preferences -k theme

# Store a file
python -m scripts.cli ingest-file notes.md -n vault

# Recall relevant memories
python -m scripts.cli recall "What does the user prefer?" -n preferences

# Chat with memory context (uses your configured LLM)
python -m scripts.cli chat "What do you know about Alice?"

# Generate consolidated thoughts
python -m scripts.cli thoughts -n vault

# List documents
python -m scripts.cli list -n vault

# Delete a namespace
python -m scripts.cli delete-namespace test --force
```

### Python (direct API)

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:8700", timeout=60) as c:
    # Store
    c.post("/memory/insert", json={
        "key": "theme-pref",
        "content": "User prefers dark mode",
        "namespace": "preferences",
    })

    # Recall
    r = c.post("/memory/query", json={
        "namespace": "preferences",
        "query": "What does the user prefer?",
        "maxChunks": 5,
    })
    print(r.json()["data"]["llmContextMessage"])

    # Chat with memory
    r = c.post("/memory/conversations", json={
        "messages": [{"role": "user", "content": "What do you know about me?"}],
    })
    print(r.json()["data"]["answer"])
```

### Upstream SDK Compatibility

If you have the official `tinyhumansai` SDK installed, point it at your local server — no code changes needed:

```powershell
$env:TINYHUMANS_BASE_URL = "http://127.0.0.1:8700"
$env:TINYHUMANS_TOKEN = "local-dev"
```

```python
import tinyhumansai as api

client = api.TinyHumansMemoryClient(token="local-dev")
client.insert_memory(item={
    "key": "theme",
    "content": "User prefers dark mode",
    "namespace": "preferences",
})
ctx = client.recall_memory(namespace="preferences", prompt="user preferences")
print(ctx.context)
```

---

## API Reference

All routes are under `/memory` and return `{"data": ...}`. Full interactive docs at `http://127.0.0.1:8700/docs`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/memory/insert` | Upsert a memory (key → documentId) |
| `POST` | `/memory/query` | Recall context for a prompt |
| `POST` | `/memory/admin/delete` | Delete an entire namespace |
| `POST` | `/memory/documents` | Insert a single document |
| `POST` | `/memory/documents/batch` | Insert multiple documents |
| `GET` | `/memory/documents` | List documents |
| `GET` | `/memory/documents/{id}` | Get a single document |
| `DELETE` | `/memory/documents/{id}` | Delete a single document |
| `POST` | `/memory/queries` | Query memory context (optional LLM answer) |
| `POST` | `/memory/conversations` | Chat with memory context |
| `POST` | `/memory/interactions` | Record interaction signals |
| `POST` | `/memory/interact` | Mirrored interaction recording |
| `POST` | `/memory/memories/thoughts` | Generate consolidated thoughts (LLM) |
| `POST` | `/memory/memories/recall` | Recall from Ebbinghaus bank by retention |
| `POST` | `/memory/memories/context` | Recall context by namespace |
| `POST` | `/memory/recall` | Recall highest-retention memories |
| `POST` | `/memory/chat` | Chat with memory (mirrored) |
| `GET` | `/memory/admin/graph-snapshot` | Entity-relation graph snapshot |
| `GET` | `/memory/ingestion/jobs/{id}` | Get ingestion job status |
| `GET` | `/memory/health` | Health check |

---

## How It Works

### Architecture (from the Engraphis paper)

```
┌─────────────────────────────────────────────────────────┐
│                    MEMORY LAYER                         │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐    │
│  │ Semantic │  │ Entity   │  │ State-Transition   │    │
│  │ Vectors  │  │ Graph    │  │ Event Ledger       │    │
│  │ (SQLite) │  │ (SQLite) │  │ (SQLite)           │    │
│  └──────────┘  └──────────┘  └────────────────────┘    │
└─────────────────────────────────────────────────────────┘
                         ▲ │
    ┌─ Phase 1 ─────────┘ │  reweight (Phase 4)
    │ Ingest: embed,      │  R = e^(-t/S)
    │ extract entities    │  S grows with access
    │ append events       │
    │                      ▼
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
│ Phase 1 │→ │ Phase 2 │→ │ Phase 3 │→ │ Phase 4 │
│ Ingest  │  │ Recall  │  │ Action  │  │Reweight │
│         │  │+ Thought│  │(LLM)    │  │+ Write  │
└─────────┘  └─────────┘  └─────────┘  └─────────┘
```

### Key Algorithms

**Ebbinghaus Retention** — memories decay over time unless reinforced:
```
R(t) = exp(-t / S)
S_new = S × (1 + 0.3 × log(1 + access_count))
```

**Interaction-Aware Boost** — engagement signals strengthen memories:
```
view → +0.05, react → +0.20, reply → +0.50, create → +1.00
```

**Conscious Recall** — retrieval scored by `retention × cosine_similarity × surprise`

**Thought Synthesis** — background loop calls your LLM to produce latent-state JSON:
```json
{"inference": "...", "contradiction": "...", "follow_up": "...", "next_action": "..."}
```

### Background Loop

Every `ENGRAPHIS_LOOP_INTERVAL` seconds (default 60), the server:
1. Runs a **decay pass** — reduces stability for stale memories
2. Recalls top-K memories per namespace
3. Calls your LLM to **synthesize a thought**
4. **Persists the thought** as a new memory artifact

Set `ENGRAPHIS_LOOP_INTERVAL=0` to disable.

---

## Configuration Reference

| Env Var | Default | Description |
|---------|---------|-------------|
| `ENGRAPHIS_HOST` | `127.0.0.1` | Server bind address |
| `ENGRAPHIS_PORT` | `8700` | Server port |
| `ENGRAPHIS_DB_PATH` | `./neocortex.db` | SQLite database file |
| `ENGRAPHIS_EMBED_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `ENGRAPHIS_LLM_PROVIDER` | `openai` | LLM provider |
| `ENGRAPHIS_LLM_MODEL` | `gpt-4o-mini` | LLM model name |
| `ENGRAPHIS_LLM_API_KEY` | — | LLM API key |
| `ENGRAPHIS_LLM_BASE_URL` | provider default | Custom endpoint URL |
| `ENGRAPHIS_LLM_EXTRA_HEADERS` | — | JSON string of extra headers |
| `ENGRAPHIS_LOOP_INTERVAL` | `60` | Background loop seconds (0=off) |
| `ENGRAPHIS_LOOP_TOP_K` | `20` | Max memories per loop cycle |
| `ENGRAPHIS_DECAY_HALFLIFE_DAYS` | `7` | Ebbinghaus half-life |

---

## Project Structure

```
engraphis/
├── neocortex/
│   ├── config.py            # Settings (env-driven)
│   ├── models.py            # Pydantic request/response models
│   ├── app.py               # FastAPI app + background loop
│   ├── stores/              # SQLite storage layer
│   │   ├── __init__.py      # Schema, connections, vector serialization
│   │   ├── vectors.py       # Memory CRUD + retention metadata
│   │   ├── graph.py         # Entity-relation graph
│   │   └── ledger.py        # Events, interactions, thoughts, jobs
│   ├── engines/             # Core memory algorithms
│   │   ├── embedder.py      # sentence-transformers + chunking
│   │   ├── ingest.py        # Phase 1: embed + extract entities
│   │   ├── recall.py        # Phase 2: conscious recall
│   │   ├── reweight.py      # Phase 4: Ebbinghaus decay + reinforcement
│   │   └── thoughts.py      # Phase 2: LLM thought synthesis
│   ├── llm/
│   │   └── client.py        # External LLM client (5 providers)
│   └── routes/
│       └── memory.py        # All 20 API routes
├── scripts/
│   ├── start_server.py      # Launch uvicorn
│   ├── cli.py               # Interactive CLI
│   ├── seed_from_obsidian.py# Migrate vault → memory
│   ├── test_routes.py       # Smoke tests
│   └── sdk_compat.py        # Upstream SDK compatibility helper
├── pyproject.toml
├── requirements.txt
├── .env.example
└── README.md
```

---

## LLM Provider Examples

### OpenAI
```ini
ENGRAPHIS_LLM_PROVIDER=openai
ENGRAPHIS_LLM_MODEL=gpt-4o-mini
ENGRAPHIS_LLM_API_KEY=sk-...
```

### Anthropic
```ini
ENGRAPHIS_LLM_PROVIDER=anthropic
ENGRAPHIS_LLM_MODEL=claude-3-5-haiku-20241022
ENGRAPHIS_LLM_API_KEY=sk-ant-...
```

### Google Gemini
```ini
ENGRAPHIS_LLM_PROVIDER=google
ENGRAPHIS_LLM_MODEL=gemini-2.0-flash
ENGRAPHIS_LLM_API_KEY=AIza...
```

### OpenRouter (access many models through one API)
```ini
ENGRAPHIS_LLM_PROVIDER=openrouter
ENGRAPHIS_LLM_MODEL=anthropic/claude-3.5-sonnet
ENGRAPHIS_LLM_API_KEY=sk-or-...
ENGRAPHIS_LLM_BASE_URL=https://openrouter.ai/api/v1
```

### Custom OpenAI-compatible endpoint
```ini
ENGRAPHIS_LLM_PROVIDER=custom
ENGRAPHIS_LLM_MODEL=your-model-name
ENGRAPHIS_LLM_API_KEY=your-key
ENGRAPHIS_LLM_BASE_URL=https://your-endpoint/v1
ENGRAPHIS_LLM_EXTRA_HEADERS={"X-Title":"my-app"}
```

---

## License

MIT — same as the upstream Engraphis repo.
