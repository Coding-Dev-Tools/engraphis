# Engraphis MCP Recipe: Cursor

Connect Cursor to Engraphis for scoped, bi-temporal repository memory with provenance citations and explicit abstention.

## 1. Install and initialize

```bash
python -m pip install "engraphis[mcp]"
engraphis-init
engraphis-init --prefetch
```

## 2. Add to `.cursor/mcp.json` (or global Cursor MCP settings)

Paste the `ENGRAPHIS_DB_PATH` printed by `engraphis-init`:

```json
{
  "mcpServers": {
    "engraphis": {
      "command": "engraphis-mcp",
      "env": {
        "ENGRAPHIS_DB_PATH": "/absolute/path/to/engraphis.db"
      }
    }
  }
}
```

## 3. Optional `.cursorrules` memory rule

```text
At the start of a task, call engraphis_session and engraphis_recall_context for this repo before proposing architectural changes. When a design constraint changes, record the updated decision with engraphis_remember and explain what it supersedes. Never invent decision history when recall abstains.
```

Run the offline 60-second check anytime: `engraphis-audit --repo .`
