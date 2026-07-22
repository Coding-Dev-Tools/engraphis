# Engraphis MCP tools — reference

All 29 tools, grouped by job. Parameters are `name (type, default)` — no default means required.
Every tool returns a JSON string; on failure it returns `"Error: <reason>"` instead of raising.
Governance tools (`forget`/`pin`/`correct`/`link`) verify the memory actually belongs to the
`workspace`/`repo` you pass **before** changing anything, so you can't touch memories outside a
scope you were already given.

Group index: [Write](#write) · [Recall and read](#recall-and-read) · [History](#history-bi-temporal) · [Governance](#governance) ·
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
- `scope (str, None)` — `session` | `repo` | `workspace` | `user`; omitted preserves the
  compatible default (`repo` when `repo` or a repo-backed `session_id` is present, otherwise
  `workspace`). Session visibility must be explicit. See SCOPING.
- `title (str, "")` — optional short title.
- `importance (float, 0.0)` — `0..1`; higher resists decay.
- `keywords (list[str], None)` — optional, aids lexical recall.
- `dedupe (bool, True)` — check against similar existing memories first: an exact restatement
  **reinforces** the existing one (`op:"noop"`); a same-subject update **supersedes** the old one
  (`op:"invalidate"`, old closed not deleted). Set `False` only for intentionally repeated
  episodic log entries.
- `retention_class (str, None)` — optional host classification: `ephemeral` | `normal` |
  `critical`; advisory and bounded, never a silent discard.
- `retention_reason (str, "")` — short content-free rationale for that classification.

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

## Recall and read

### `engraphis_recall`
Retrieve the memories most relevant to a query (hybrid vector + lexical + graph, fused + reranked).
Call it before answering or acting when prior context would help.

- `query (str)` — natural language, e.g. `"how do we handle auth?"`.
- `workspace (str, None)` — restrict to this workspace.
- `repo (str, None)` — restrict to this repo (requires `workspace`).
- `session_id (str, None)` — exact session context (requires `workspace`); inherits repo/workspace
  ancestors while excluding every other session.
- `mtypes (list[str], None)` — restrict to these memory types.
- `k (int, 8)` — max results, `1..50`.

Returns `{query, count, context, memories:[{id, title, content, scope, mtype, repo_id, score,
arm, retention, provenance}]}`. `context` is a token-budgeted pack ready to drop into your prompt.
`count:0` with a `note` means that workspace/repo isn't known yet — not an error.
Successful calls reinforce returned memories and append a privacy-safe operation receipt. The MCP
tool is therefore stateful and non-idempotent even though its primary purpose is retrieval.

### `engraphis_recall_grounded`
Answer a question **strictly from** stored memories, with `[n]` citations — or **abstain** when
nothing in scope supports it. Use when you want a grounded, non-hallucinated answer and would
rather get "insufficient evidence" than a guess. The default answer is deterministic and
extractive; optional LLM synthesis is accepted only when its claims remain cited.

- `query (str)` — the question, e.g. `"which auth scheme did we standardise on?"`.
- `workspace (str, None)`, `repo (str, None)`, `session_id (str, None)`,
  `mtypes (list[str], None)`, `k (int, 8)`.
- `min_support (float, None)` — absolute support floor `0..1`; raise it to demand stronger
  evidence before answering.
- `synthesize (bool, false)` — ask a configured LLM for cited prose; falls back safely.

Returns `{query, grounded, abstained, answer, support, reason, synthesized, citations:[{n, id,
title, content, score, support, provenance}]}`. When `grounded` is false, `answer` is empty and
`reason` says why (insufficient evidence, or unknown workspace/repo).
Resolved calls append a privacy-safe receipt, including abstentions, and cited memories are
reinforced. This surface is stateful and non-idempotent.

### `engraphis_answer`
Backward-compatible grounded-answer alias with the same state effects. Prefer
`engraphis_recall_grounded` for new configs; keep using this only if an existing agent already
references it.

### `engraphis_recall_proactive`
Conscious recall with **no query**: high-importance, recent, well-reinforced memories. Use at the
start of a task to load context before you know what to ask.

- `workspace (str)`, `repo (str, None)`, `k (int, 10)`.

Returns `{memories:[…], last_session:{summary, open_threads, outcome}}`. When `repo` is given,
`last_session` is the authenticated caller's most recent *ended* session for that repo (or `{}`
if none); standalone trusted callers retain the unfiltered local handoff behavior.

### `engraphis_proactive_context`
Build a task-aware context packet from proactive recall, optional current agent state, and the
last-session handoff. Use at task start when an agent needs ready-to-use, cited context rather
than the raw queryless memory list.

- `workspace (str)`, `repo (str, None)`, `task (str, "")`, `agent_state (str, "")`,
  `k (int, 10)`, `synthesize (bool, false)`.

Returns `{context_summary, suggested_memories, citations, suggested_queries, last_session,
grounded, synthesized, reason}`.
When `task` or `agent_state` is non-empty, its task-specific recall appends a privacy-safe receipt
without reinforcing memories, so the MCP tool is conservatively stateful and non-idempotent.


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
All five preserve history (bi-temporal close, never a hard delete) and are audited. All verify
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

### `engraphis_promote`
Widen a live memory's visibility without editing it in place. The wider record is stored first;
the narrow source is then bi-temporally closed and linked, with provenance, pinning, sensitivity,
and learned stability inherited.

- `memory_id (str)`, `target_scope (str)`, `workspace (str)`, `repo (str, None)`,
  `reason (str, "")`.

`target_scope` must be strictly wider: session → repo/workspace or repo → workspace. User-scope
promotion is not yet supported because records remain workspace-bound. Returns
`{id, promoted_from, from_scope, scope, op, reason, receipt}`.

### `engraphis_pin`
Exempt a memory from automatic decay/pruning — for durable conventions and identity facts.

- `memory_id (str)`, `workspace (str)`, `repo (str, None)`, `pinned (bool, True)`.

Returns `{id, pinned}`.

### `engraphis_link`
Explicitly connect two memories (A-MEM-style) when a plain recall wouldn't surface the relation.

- `a (str)`, `b (str)`, `workspace (str)`, `repo (str, None)`, `relation (str, "related")` —
  e.g. `caused_by`, `fixed_by`.
- `layer (str, None)` — `temporal` | `entity` | `causal` | `semantic`; omitted means infer
  from `relation`.
- `reason (str, "")` — optional rationale/context for why the relationship exists; persisted
  with the link and shown by inspection/graph APIs.

Returns `{a, b, relation, layer, reason, linked:true, receipt}`.

---

## Code

### `engraphis_index_repo`
Incrementally parse a repository into the code-symbol graph: modules/files, functions, classes,
methods, variables, docstrings/comments, definitions, calls, imports, inheritance, and
implementation edges. AST via tree-sitter when available, dependency-free regex fallback
otherwise. Existing memories that mention symbols are linked into the same traversal graph.

- `workspace (str)`, `repo (str)`, `root_path (str)` — local path to the repo root,
  `languages (list[str], None)` — omit to index every supported language found.

Returns `{files_indexed, files_unchanged, files_removed, symbols, edges, code_memory_links,
backend}`. Re-indexing hashes files, skips unchanged content, and removes deleted files only after
a complete scan. Reads local files at `root_path`; nothing is sent anywhere.

### `engraphis_search_code`
Find definitions by name, with their callers — structural search that costs far fewer tokens than
grepping or dumping files, and answers "what calls this / what breaks if I change it".

- `query (str)` — symbol or partial name, `workspace (str)`, `repo (str)` (must be indexed first),
  `limit (int, 20)`.

Returns `{query, symbols:[{name, fqname, kind, file, span, signature, docstring,
called_by:[…], linked_memories:[…]}]}`.

### `engraphis_code_path`
Find the shortest path across definitions, calls, imports, aliases, and code↔memory links.

- `source (str)`, `target (str)` — symbol, file, or memory id.
- `workspace (str)`, `repo (str)`, `max_depth (int, 8)`.

Returns `{found, source, target, hops, path, edges}` with direction and provenance fields.

### `engraphis_code_impact`
Estimate commit/PR impact from repo-relative changed files.

- `changed_files (list[str])`, `workspace (str)`, `repo (str)`.

Returns risk score/level, touched symbols, inbound edges, dependent files, linked memories,
communities affected, hotspots, and potential conflict zones.

### `engraphis_export_code_graph`
Return portable `graph.json` data plus a human-readable Markdown report and self-contained HTML.

- `workspace (str)`, `repo (str)`.

---

## Sessions

### `engraphis_start_session`
Open a session to group this work's memories and enable cross-session resume.

- `workspace (str)`, `repo (str, None)`, `agent (str, "")` (e.g. `"claude-code"`),
  `goal (str, "")`, `force_new (bool, false)`.

By default this is idempotent per exact `(workspace, repo, authenticated user, agent, goal)` task
identity. Different users, agents, or goals automatically open distinct sessions. An exact retry
returns the same active session with `reused:true`. Use `force_new=true` only to branch a second
session when every identity field matches. A new session returns `reused:false` plus
`{session_id, workspace, repo, goal, status:"active", bootstrap:{summary, open_threads, outcome}}`.
If a previous same-user/agent session in this repo ended with a summary/open threads, `bootstrap`
carries them so you resume without crossing an identity boundary. Pass `session_id` to `remember`
and `end_session`.

Authenticated host adapters must provide a stable non-empty user `id` and ownership `email` when
binding request context. Malformed non-`None` principals fail closed; only `None` selects trusted
standalone/system mode. Ownerless legacy sessions are not exposed to authenticated callers.

### `engraphis_end_session`
Close a session with a summary/outcome so the next one picks up the thread.

- `session_id (str)`, `summary (str, "")`, `outcome (str, "")` (e.g. `shipped`, `blocked`),
  `open_threads (list[str], None)` — surfaced for the next same-user/agent session in this repo.

Returns `{session_id, status:"summarized", summary, open_threads}`.

---

### `engraphis_ingest`
Store raw, undistilled text (transcripts, notes, logs). With `ENGRAPHIS_EXTRACTOR=llm`
configured server-side, the text is first distilled into discrete typed facts — each stored
with the same conflict resolution and evolution as `remember`. Without an extractor it
behaves exactly like `remember` (passthrough). Prefer `remember` when you already have one
crisp fact.

- `content (str, required)`; `workspace (str, required)`; `repo (str, None)`;
  `session_id (str, None)`; `mtype (str, "semantic")` — default type for unclassified facts;
  `scope (str, None)` — omitted defaults to repo for repo/session context, otherwise workspace.

Returns `{workspace, repo, count, extracted, facts: [{id, op, superseded?}]}`.

### `engraphis_ingest_postgres_schema`
Inspect a live PostgreSQL catalog into schema memories plus typed database/schema/table/column/
constraint graph nodes. The DSN is used for the connection only and is never stored or returned.

- `dsn (str)`, `workspace (str)`, `repo (str, None)`, `schemas (list[str], None)`.

### `engraphis_consolidate`
One sleep-time consolidation sweep: recurring episodic memories on the same subject become a
single durable semantic digest (linked to sources via `consolidates` links), and fully-decayed
transient memories are archived (bi-temporal close — audited, recoverable, pinned exempt).
`dry_run=true` is the pure default. Live deterministic retries skip already-consolidated
sources, but structured results may cite only part of a large cluster and let an identical later
call process the remainder, so the public tool is conservatively non-idempotent. Call it at
session end or on a schedule (`python -m scripts.consolidate` is the cron-able equivalent).

With `profiles=true` it also rolls every live memory mentioning an entity into one durable
semantic *profile* digest (linked via `profiles`) — a per-subject knowledge profile that grows
with use.

- `workspace (str, required)`; `repo (str, None)`; `dry_run (bool, true)`;
  `profiles (bool, false)`; `structured (bool, false)`; `supersede_sources (bool, false)`.
  `supersede_sources=true` requires `structured=true` and bi-temporally closes only the source
  episodes cited by validated structured facts.

Returns `{clusters_found, digests_created, archived, skipped_already_consolidated, compaction, dry_run}`
— `compaction` is the context tokens the sweep saved (before → after). With `profiles=true` a
`profiles` block is added (`entities_considered, profiles_created, skipped_existing, compaction`).

## Ops

### `engraphis_receipts`
List content-free, SHA-256-chained operation receipts for a workspace.

- `workspace (str)`, `limit (int, 100)`.

### `engraphis_verify_receipts`
Recompute hashes and validate chain order plus the independently stored local head/count anchor.
Optionally pass a previously exported `expected_head` / `expected_count` for verification against
an anchor kept outside the database. Returns `{valid, count, head, anchored, errors}`.

### `engraphis_export_receipts`
Return the receipt-only export bundle plus verification result; raw memory/query contents and
actor/workspace names are excluded.

### `engraphis_stats`
Memory counts (overall or for one workspace) — handy for onboarding/health checks.

- `workspace (str, None)`.

Returns `{memories, by_type, workspaces, sessions, schema_version}`.

### `engraphis_check_update`
Report whether a newer Engraphis release is available, so an agent can proactively remind the
user to upgrade. Cached ~24h and fail-silent; honors `ENGRAPHIS_UPDATE_CHECK=0` (then `enabled`
is false). The default GitHub source is overridable via `ENGRAPHIS_UPDATE_URL`.

- `force (bool, false)` — bypass the ~24h cache and re-check the release source now.

Returns `{enabled, current, latest, update_available, url, notice}`.

---

## Quick decision guide

- Learned a durable fact → `remember`. Raw thing that happened → `record_event`.
- Need raw context and have a question → `recall`. Need raw context and don't yet → `recall_proactive`. Need a task-ready packet → `proactive_context`.
- "Why?" / "since when?" → `why` / `timeline` (not `recall` — those see history).
- Fact is wrong → `correct` (keeps the chain). Fact is obsolete with no replacement → `forget`.
- Fact applies more broadly than first believed → `promote` (widens without duplicate recall).
- Must never fade → `pin`. Two facts belong together → `link`.
- Working in code → `index_repo`, then `search_code`; use `code_path`/`code_impact` for structural
  questions and PR triage.
- Multi-step task → wrap in `start_session` … `end_session`.
- Have a blob, not a fact → `ingest`. Memory getting noisy → `consolidate` (dry-run first).
