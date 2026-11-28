# Engraphis MCP Recipe: Zed

Connect Zed's Agent Panel to Engraphis via `context_servers` (Apache-2.0 local SQLite MCP server).

## 1. Install and initialize

```bash
python -m pip install "engraphis[mcp]"
engraphis-init
engraphis-init --prefetch
```

## 2. Add to Zed `settings.json`

Use the `ENGRAPHIS_DB_PATH` printed by `engraphis-init`:

```json
{
  "context_servers": {
    "engraphis": {
      "command": {
        "path": "engraphis-mcp",
        "args": [],
        "env": {
          "ENGRAPHIS_DB_PATH": "/absolute/path/to/engraphis.db"
        }
      }
    }
  }
}
```

Run the offline 60-second check anytime: `engraphis-audit --repo .`
