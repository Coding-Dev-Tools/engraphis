# Engraphis MCP Recipe: Cline

Connect Cline to Engraphis over local stdio MCP (9 Smart MCP tools, offline SQLite, Apache-2.0).

## 1. Install and initialize

```bash
python -m pip install "engraphis[mcp]"
engraphis-init --prefetch
```

## 2. Add to `cline_mcp_settings.json`

Use the exact `ENGRAPHIS_DB_PATH` printed by `engraphis-init`:

```json
{
  "mcpServers": {
    "engraphis": {
      "command": "engraphis-mcp",
      "disabled": false,
      "env": {
        "ENGRAPHIS_DB_PATH": "/absolute/path/to/engraphis.db"
      }
    }
  }
}
```

## 3. Sharing one local embedding model across multiple editors

If you run Cline alongside Claude Code or Cursor, launch one shared loopback HTTP server (`engraphis-mcp-http`) and point each client at `["--connect", "http://127.0.0.1:8711/mcp"]`. See [docs/AGENT_CONNECT.md](../AGENT_CONNECT.md).
