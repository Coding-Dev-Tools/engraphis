# Engraphis MCP tools — reference

All 15 tools, grouped by job. Parameters are `name (type, default)` — no default means required.
Every tool returns a JSON string; on failure it returns `"Error: <reason>"` instead of raising.
Governance tools (`forget`/`pin`/`correct`/`link`) verify the memory actually belongs to the
`workspace`/`repo` you pass **before** changing anything, so you can't touch memories outside a
scope you were already given.

Group index: [Write](#write) · [Read](#read) · [History](#history) · [Governance](#governance) ·
[Code](#code) · [Sessions](#sessions) · [Ops](#ops).

---

## Write

### `engraphis_remember`
Store a memory so it can be recalled later, across turns, sessions, and repos.

- `content (str)` — the fact/decision/convention/procedure.
- `workspace (str)` — top-level scope (org/product), e.g. `"acme"`.
- `repo (str, None)` — repository scope; omit for workspace-wide facts.
- `session_id (str, None)` — from `engraphis_start_session`, if this belongs to a session.
- `mtype (str, "semantic")` — `semantic` | `episodic` | `procedural` | `working`. See CONVENTIONS.
- `scope (str, "repo")` — `session` | `repo` | `workspace` | `user`. See SCOPING.
- `title (str, "")` — optional short title.
- `importance (float, 0.0)` — `0..1`; higher resists decay.
- `keywords (list[str], None)` — optional, aids lexical recall.
- `dedupe (bool, True)` — check against similar existing memories first: an exact restatement
  **reinforces** the existing one (`op:"noop"`); a same-subject update **supersedes** the old one
  (`op:"invalidate"`, old closed not deleted). Set `False` only for intentionally repeated
  episodic log entries.

Returns `{id, workspace, repo, scope, mtype, stored:true, op}` where `op` is `add` | `noop` |
`invalidate` (with `superseded:[old_id,…]`).

> Prefer `dedupe=True` (default). It is what keeps the store contradiction-free without an LLM.

### `engraphis_record_event`
Append a lightweight episodic log entry — lower ceremony than `remember`, for raw events you may
later consolidate into a durable fact.

- `kind (str)` — e.g. `decision`, `bug`, `fix`, `tried_and_failed`, `review_comment`.
- `content (str)` — what happened.
- `workspace (str)`, `repo (str, None)`, `session_id (str, None)`.

Returns `{id, kind}`. Three similar events about the same thing is a signal to promote it into a
`semantic`/`procedural` memory with `remember`.

---

## Read

### `engraphis_recall`
Retrieve the memories most relevant to a query (hybrid vector + lexical + graph, fused + reranked).
Call it before answering or acting when prior context would help.

- `query (str)` — natural language, e.g. `"how do we handle auth?"`.
- `workspace (str, None)` — restrict to this workspace.
- `repo (str, None)` — restrict to this repo (requires `workspace`).
- `mtypes (list[str], None)` — restrict to these memory types.
- `k (int, 8)` — max results, `1..50`.

Returns `{query, count, context, memories:[{id, title, content, scope, mtype, repo_id, score,
arm, retention, provenance}]}`. `context` is a token-budgeted pack ready to drop into your prompt.
`count:0` with a `note` means that workspace/repo isn't known yet — not an error.

### `engraphis_recall_proactive`
Conscious recall with **no query**: high-importance, recent, well-reinforced memories. Use at the
start of a task to load context before you know what to ask.

- `workspace (str)`, `repo (str, None)`, `k (int, 10)`.

Returns `{memories:[…], last_session:{summary, open_threads, outcome}}`. When `repo` is given,
`last_session` is the most recent *ended* session for that repo (or `{}` if none) — the handoff.

---

## History (bi-temporal)

### `engraphis_why`
Surface the current answer **and** what it superseded. Use for "why is it like this" / "what did
we used to do" — it looks past the live view into history, which plain recall does not.

- `query (str)`, `workspace (str)`, `repo (str, None)`, `k (int, 5)`.

Returns `{query, answer:[…live…], supersedes:[…what they replaced…]}`.

### `engraphis_timeline`
Every version of a fact in chronological order, including superseded ones.

- `query (str)`, `workspace (str)`, `repo (str, None)`, `limit (int, 20)`.

Returns `{query, history:[{…memory fields…, valid_from, valid_to}]}`, oldest first.

---

## Governance
All four preserve history (bi-temporal close, never a hard delete) and are audited. All verify
ownership against the `workspace`/`repo` you pass.

### `engraphis_correct`  *(preferred fix)*
Replace a memory's content without losing history: old content is closed, the correction is stored
as a new memory that records what it corrected — so the audit trail and `engraphis_why` still work.

- `memory_id (str)`, `new_content (str)`, `workspace (str)`, `repo (str, None)`, `reason (str, "")`.

Returns `{id, superseded:[old_id], reason}`. Prefer this over forget-then-remember.

### `engraphis_forget`
Retire a memory: it stops appearing in recall, history preserved.

- `memory_id (str)`, `workspace (str)`, `repo (str, None)`, `reason (str, "")`.

Returns `{id, status:"forgotten", reason}`. Use `correct` instead when you have replacement content.

### `engraphis_pin`
Exempt a memory from automatic decay/pruning — for durable conventions and identity facts.

- `memory_id (str)`, `workspace (str)`, `repo (str, None)`, `pinned (bool, True)`.

Returns `{id, pinned}`.

### `engraphis_link`
Explicitly connect two memories (A-MEM-style) when a plain recall wouldn't surface the relation.

- `a (str)`, `b (str)`, `workspace (str)`, `repo (str, None)`, `relation (str, "related")` —
  e.g. `caused_by`, `fixed_by`.

Returns `{a, b, relation, linked:true}`.

---

## Code

### `engraphis_index_repo`
Parse a repository into the code-symbol graph: function/class/method definitions plus best-effort
call/import edges. Run once when you start in a repo (or after large changes) so `search_code` has
something to search. AST via tree-sitter when available, dependency-free regex fallback otherwise.

- `workspace (str)`, `repo (str)`, `root_path (str)` — local path to the repo root,
  `languages (list[str], None)` — omit to index every supported language found.

Returns `{files_indexed, symbols, edges, backend}`. Re-indexing is safe (per-file replace, not
duplicate). Reads local files at `root_path` — same trust boundary as any local tool; nothing is
sent anywhere.

### `engraphis_search_code`
Find definitions by name, with their callers — structural search that costs far fewer tokens than
grepping or dumping files, and answers "what calls this / what breaks if I change it".

- `query (str)` — symbol or partial name, `workspace (str)`, `repo (str)` (must be indexed first),
  `limit (int, 20)`.

Returns `{query, symbols:[{name, fqname, kind, file, span, signature,
called_by:[{src, file, line}]}]}`.

---

## Sessions

### `engraphis_start_session`
Open a session to group this work's memories and enable cross-session resume.

- `workspace (str)`, `repo (str, None)`, `agent (str, "")` (e.g. `"claude-code"`),
  `goal (str, "")`.

Returns `{session_id, workspace, repo, goal, status:"active", bootstrap:{summary, open_threads,
outcome}}`. If a previous session in this repo was ended with a summary/open threads, `bootstrap`
carries them so you resume. Pass `session_id` to `remember` and `end_session`.

### `engraphis_end_session`
Close a session with a summary/outcome so the next one picks up the thread.

- `session_id (str)`, `summary (str, "")`, `outcome (str, "")` (e.g. `shipped`, `blocked`),
  `open_threads (list[str], None)` — surfaced automatically when the next session in this repo starts.

Returns `{session_id, status:"summarized", summary, open_threads}`.

---

## Ops

### `engraphis_stats`
Memory counts (overall or for one workspace) — handy for onboarding/health checks.

- `workspace (str, None)`.

Returns `{memories, by_type, workspaces, sessions, schema_version}`.

---

## Quick decision guide

- Learned a durable fact → `remember`. Raw thing that happened → `record_event`.
- Need context and have a question → `recall`. Need context and don't yet → `recall_proactive`.
- "Why?" / "since when?" → `why` / `timeline` (not `recall` — those see history).
- Fact is wrong → `correct` (keeps the chain). Fact is obsolete with no replacement → `forget`.
- Must never fade → `pin`. Two facts belong together → `link`.
- Working in code → `index_repo` once, then `search_code`.
- Multi-step task → wrap in `start_session` … `end_session`.
