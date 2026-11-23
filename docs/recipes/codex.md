# Engraphis MCP Recipe: Codex

Connect Codex CLI to Engraphis for scoped, bi-temporal decision recall across sessions (Apache-2.0, local SQLite).

## 1. Install and initialize

```bash
python -m pip install "engraphis[mcp]"
engraphis-init --prefetch
```

## 2. Register Engraphis in Codex

Run the exact command printed by `engraphis-init` (including your `ENGRAPHIS_DB_PATH`), or run:

```bash
codex mcp add engraphis -- engraphis-mcp
```

## 3. Verify across two sessions

1. Record one repository constraint with `engraphis_remember`.
2. Start a fresh Codex session and query that decision with `engraphis_recall_context`.
3. Inspect stored records and links anytime with `engraphis-dashboard` at `http://127.0.0.1:8700`.
