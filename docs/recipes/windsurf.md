# Engraphis MCP Recipe: Windsurf

Connect Windsurf Cascade to Engraphis for local, bi-temporal repository memory with citations or explicit abstention.

## 1. Install and initialize

```bash
python -m pip install "engraphis[mcp]"
engraphis-init
engraphis-init --prefetch
```

## 2. Add to `~/.codeium/windsurf/mcp_config.json`

Use the `ENGRAPHIS_DB_PATH` printed by `engraphis-init`:

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

Run the offline 60-second check anytime: `engraphis-audit --repo .`
