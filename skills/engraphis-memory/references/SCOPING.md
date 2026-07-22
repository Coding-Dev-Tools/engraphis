# Scoping — `workspace → repo → session → memory`

Scoping is the highest-leverage decision in Engraphis. Every write sets a scope; every read is
filtered by one. Get it right and memories surface exactly when useful; get it wrong and they
either leak everywhere or never come back.

## Two orthogonal axes — don't conflate them

| Axis | Question it answers | Values | Set by |
|---|---|---|---|
| **scope** | *Who/where can see this?* | `session` · `repo` · `workspace` · `user` | `scope=` on `remember` |
| **type** (`mtype`) | *What kind of thing is this?* | `working` · `episodic` · `semantic` · `procedural` | `mtype=` on `remember` |

A convention is `mtype="semantic"` and probably `scope="repo"`. A user's editor preference is
`mtype="semantic"` but `scope="user"`. Same type, different visibility. Type is covered in
[CONVENTIONS.md](CONVENTIONS.md); this file is about scope.

## The hierarchy

```
workspace            org or product        ("acme")           — always required on a write
  └─ repo            a repository          ("backend")        — omit only for workspace-wide facts
       └─ session    one unit of work      (session_id)       — from engraphis_start_session
            └─ memory                                          — the fact itself
```

Names are **stable identifiers**, not prose. Reuse the exact same `workspace`/`repo` strings every
time — recall filters match on them literally. Pick the repository's canonical name for `repo`
(what you'd `git clone`), and a durable org/product name for `workspace`.

## What each scope means

- **`session`** — visible only within one session. Transient working state ("currently editing the
  auth refactor on branch X"). Ends with the session.
- **`repo`** — the default, and the right answer most of the time. Facts true for one repository:
  conventions, decisions, bug fixes. Requires a `repo`.
- **`workspace`** — true across every repo in the org/product: shared standards, cross-repo
  architecture, team norms. Set `repo=None`.
- **`user`** — follows the human across everything: their preferences and working style, regardless
  of workspace or repo.

## Choose the narrowest scope that stays reusable

Ask: *where would I want this to resurface?* Then scope there — no wider.

- A fix for a quirk in `backend` only → `scope="repo"`.
- "The whole org uses trunk-based dev" → `scope="workspace"`.
- "This developer prefers tabs, hates mocks" → `scope="user"`.
- "I'm mid-way through step 3 of this task" → `scope="session"` (or just an `open_thread`).

Over-scoping (everything `workspace`) pollutes recall in unrelated repos. Under-scoping (everything
`session`) means nothing survives the task. When unsure between `repo` and `workspace`, start at
`repo` — promoting later is cheap; retracting a leaked fact is not.

## Sessions and handoff

A session groups a task's memories and enables resume:

1. `engraphis_start_session(workspace, repo, agent, goal)` → returns `session_id`, `reused`, and a
   `bootstrap` carrying the previous same-user/agent session's `summary` + `open_threads` for this
   repo.
2. Pass `session_id` to `engraphis_remember` / `engraphis_record_event` during the task.
3. `engraphis_end_session(session_id, summary, outcome, open_threads)` — `open_threads` are the
   unresolved items; they auto-surface for the next same-user/agent session in this repo.

Starting is idempotent per exact `(workspace, repo, authenticated user, agent, goal)` identity.
Different users, agents, or goals automatically open separate sessions. `reused=true` therefore
means a retry found the same active task. Use `force_new=true` only to branch a second session when
every identity field matches; use this escape hatch deliberately because parallel duplicate task
sessions make ownership and handoff ambiguous.

An authenticated host integration must bind both a stable non-empty user `id` and an ownership
`email`. A malformed non-`None` principal is rejected; it never collapses into an anonymous or
legacy owner. `None` is reserved for trusted standalone/system operation with no user boundary.

Use sessions for any multi-step task. `open_threads` is how the next agent avoids re-discovering
where you stopped.

## Promotion (widening scope)

A learning often starts narrow (a session observation) and proves durable. Promote the existing
memory with `engraphis_promote(memory_id, target_scope, workspace, repo?, reason?)`:

- Session note that turns out to be a real repo convention → `target_scope="repo"`.
- Repo fact that turns out to hold org-wide → `target_scope="workspace"`.

Promotion must be strictly wider. Engraphis writes/deduplicates the wider record first, then
bi-temporally closes the narrow source and links them with `promotes`; pinning, sensitivity,
provenance, and learned stability are inherited. Automatic promotion is not assumed — promote
deliberately when evidence shows the learning applies more broadly.

Promotion to `user` is not yet supported: current records remain workspace-bound, so calling it
"wider" would be misleading until user-principal ownership exists in the schema.

## Reads are scoped too

`engraphis_recall` is hierarchy-aware. A repo context sees that repo plus its workspace/user
ancestors; a session context sees that exact session plus its repo/workspace/user ancestors.
Other sessions never leak into repo/workspace recall. A `repo` or `session_id` filter requires a
`workspace`. If recall returns no results and a `note` says the workspace/repo/session is unknown,
you simply have not written there yet.
