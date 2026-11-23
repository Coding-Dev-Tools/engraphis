# Engraphis MCP Recipe: Claude Code

Give Claude Code local, scoped, bi-temporal project memory over stdio MCP (Apache-2.0, SQLite, zero cloud required).

## 1. Install and pre-cache local embeddings

```bash
python -m pip install "engraphis[mcp]"
engraphis-init --prefetch
```

## 2. Register Engraphis in Claude Code

Run the exact command printed by `engraphis-init` (including your local `ENGRAPHIS_DB_PATH`), or register the default command:

```bash
claude mcp add engraphis -- engraphis-mcp
```

## 3. Verify across two fresh sessions

1. Start Claude Code in your repository and ask it to remember one architectural decision (including why it replaced an earlier approach).
2. Exit and open a brand-new session.
3. Ask why that decision was made and verify it cites the stored memory record rather than guessing from code shape.

Run the offline 60-second check anytime: `engraphis-audit --repo .`
